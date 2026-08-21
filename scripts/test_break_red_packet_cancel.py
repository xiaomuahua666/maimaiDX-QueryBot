"""BREAK 红包主动收回（cancel_red_packet）退回逻辑回归测试。

不启动 NoneBot：用 AST 抽取 BreakDatabase 的相关方法 + 模块级依赖符号，
注入内存 SQLite，mock 掉 is_plugin_admin，验证退回四步与权限/前置校验。
"""

from __future__ import annotations

import ast
import sqlite3
from contextlib import contextmanager
import sys
import time
import types as _types
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "libraries" / "maimaidx_break.py"


def today_beijing() -> str:
    return datetime.now(timezone(timedelta(hours=8))).date().isoformat()

# ---- mock 掉 cancel_red_packet 内部延迟 import 的 is_plugin_admin ----
# 源码里是 `from .maimaidx_bot_admin import is_plugin_admin`，
# 相对 import 会解析为 nonebot_plugin_maimaidx.libraries.maimaidx_bot_admin。
# 同时注册顶层和包内路径，确保两种解析都能命中。
ADMIN_SET: set[int] = set()
fake_admin_mod = _types.ModuleType("maimaidx_bot_admin")


def _is_plugin_admin(user_id):
    return int(user_id) in ADMIN_SET


fake_admin_mod.is_plugin_admin = _is_plugin_admin
sys.modules["maimaidx_bot_admin"] = fake_admin_mod

# 构造包层级，使相对 import `from .maimaidx_bot_admin` 可解析
_pkg_root = _types.ModuleType("nonebot_plugin_maimaidx")
_pkg_lib = _types.ModuleType("nonebot_plugin_maimaidx.libraries")
sys.modules.setdefault("nonebot_plugin_maimaidx", _pkg_root)
sys.modules.setdefault("nonebot_plugin_maimaidx.libraries", _pkg_lib)
sys.modules["nonebot_plugin_maimaidx.libraries.maimaidx_bot_admin"] = fake_admin_mod


# ---- AST 抽取：模块级符号 + BreakDatabase 类 ----
# 把 cancel_red_packet 里的 `from .maimaidx_bot_admin import is_plugin_admin`
# 替换为直接引用已注入 namespace 的 _is_plugin_admin，避开相对 import 解析。
_src_text = SOURCE.read_text(encoding="utf-8")
_src_text = _src_text.replace(
    "from .maimaidx_bot_admin import is_plugin_admin",
    "is_plugin_admin = _is_plugin_admin",
)
tree = ast.parse(_src_text)

# 需要的模块级符号（dataclass / 辅助函数 / 常量）
MODULE_LEVEL_NAMES = {
    "RedPacketCreateResult",
    "RedPacketClaimResult",
    "RedPacketRefundResult",
    "RedPacketStatus",
    "_parse_config_int",
    "calculate_red_packet_claim",
    "BreakInsufficientError",
}
# BreakDatabase 里需要的方法
NEEDED_METHODS = {
    "_db_lock",
    "_ensure_user",
    "_ensure_daily",
    "_today",
    "_append_log",
    "get_balance",
    "get_config",
    "expire_red_packets",
    "create_red_packet",
    "claim_red_packet",
    "get_red_packet_status",
    "cancel_red_packet",
}

module_nodes = []
for node in tree.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        if node.name in MODULE_LEVEL_NAMES:
            module_nodes.append(node)
    elif isinstance(node, ast.Assign):
        # 保留 _parse_config_int 之外的常量若被引用；这里只取 dataclass/函数
        pass

# 单独处理 BreakDatabase：抽取其内部需要的方法，重组成一个新类
class_node = next(
    n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "BreakDatabase"
)
methods = [
    node
    for node in class_node.body
    if isinstance(node, ast.FunctionDef) and node.name in NEEDED_METHODS
]
found = {node.name for node in methods}
assert found == NEEDED_METHODS, f"缺少方法: {NEEDED_METHODS - found}"

# BreakInsufficientError 来自 maimaidx_error，源文件里是 import 进来的，
# 不在模块级定义，需 stub。其他模块级符号应能抽到。
assert "BreakInsufficientError" not in {n.name for n in module_nodes}, (
    "BreakInsufficientError 应来自 import，不该在源文件定义"
)


class _BreakInsufficientError(Exception):
    def __init__(self, need=0, have=0, qqid=None):
        super().__init__(f"need {need} have {have}")
        self.need = need
        self.have = have
        self.qqid = qqid


test_class = ast.ClassDef(
    name="BreakDatabase",
    bases=[],
    keywords=[],
    body=methods,
    decorator_list=[],
)
ast.fix_missing_locations(test_class)

namespace: dict = {
    "date": date,
    "datetime": datetime,
    "timedelta": timedelta,
    "timezone": timezone,
    "time": time,
    "uuid": __import__("uuid"),
    "json": __import__("json"),
    "random": __import__("random"),
    "RLock": __import__("threading").RLock,
    "BreakInsufficientError": _BreakInsufficientError,
    "_is_plugin_admin": _is_plugin_admin,
    # dataclass/field 装饰器
    "dataclass": __import__("dataclasses").dataclass,
    "field": __import__("dataclasses").field,
    "contextmanager": contextmanager,
}
exec(
    compile(
        ast.Module(body=module_nodes + [test_class], type_ignores=[]),
        str(SOURCE),
        "exec",
    ),
    namespace,
)

BreakDatabase = namespace["BreakDatabase"]


def _make_db() -> BreakDatabase:
    """构造 BreakDatabase 实例但跳过 __init__ 的真实 DB 初始化。"""
    db = object.__new__(BreakDatabase)
    db._initialized = True
    db._lock = __import__("threading").RLock()
    db._conn = sqlite3.connect(":memory:")
    db._conn.row_factory = sqlite3.Row
    db._conn.executescript(
        """
        CREATE TABLE break_users (
            qqid INTEGER PRIMARY KEY, balance INTEGER NOT NULL DEFAULT 0,
            streak INTEGER NOT NULL DEFAULT 0, last_checkin_date TEXT,
            total_query_count INTEGER NOT NULL DEFAULT 0,
            total_analysis_count INTEGER NOT NULL DEFAULT 0,
            last_query_at REAL, last_analysis_at REAL,
            created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        CREATE TABLE break_daily_usage (
            qqid INTEGER NOT NULL, date TEXT NOT NULL,
            free_used INTEGER NOT NULL DEFAULT 0,
            query_count INTEGER NOT NULL DEFAULT 0,
            analysis_count INTEGER NOT NULL DEFAULT 0,
            break_spent INTEGER NOT NULL DEFAULT 0,
            break_gained INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (qqid, date)
        );
        CREATE TABLE break_log (
            qqid INTEGER NOT NULL, delta INTEGER NOT NULL,
            reason TEXT NOT NULL, meta TEXT, created_at REAL NOT NULL
        );
        CREATE TABLE break_red_packet (
            id TEXT PRIMARY KEY, group_id INTEGER NOT NULL,
            sender_qqid INTEGER NOT NULL, total_amount INTEGER NOT NULL,
            total_count INTEGER NOT NULL, remaining_amount INTEGER NOT NULL,
            remaining_count INTEGER NOT NULL, status TEXT NOT NULL,
            created_at REAL NOT NULL, expires_at REAL NOT NULL, finished_at REAL
        );
        CREATE TABLE break_red_packet_claim (
            packet_id TEXT NOT NULL, qqid INTEGER NOT NULL,
            amount INTEGER NOT NULL, claimed_at REAL NOT NULL,
            PRIMARY KEY (packet_id, qqid)
        );
        CREATE TABLE break_config (
            key TEXT PRIMARY KEY, value TEXT NOT NULL
        );
        """
    )
    return db


SENDER = 1001
CLAIMER = 1002
OUTSIDER = 1003
GROUP = 8888


def _seed_user(db, qqid, balance):
    db._conn.execute(
        "INSERT INTO break_users (qqid, balance, streak, created_at, updated_at) "
        "VALUES (?, ?, 0, ?, ?)",
        (qqid, balance, time.time(), time.time()),
    )
    db._conn.execute(
        "INSERT INTO break_daily_usage (qqid, date, free_used, query_count, "
        "analysis_count, break_spent, break_gained) VALUES (?, ?, 0, 0, 0, 0, 0)",
        (qqid, today_beijing()),
    )
    db._conn.commit()


def _age_packet(db, packet_id, seconds):
    """把红包 created_at 调到指定秒数前，使"发红包满 90 秒"条件成立。"""
    db._conn.execute(
        "UPDATE break_red_packet SET created_at=? WHERE id=?",
        (time.time() - seconds, packet_id),
    )
    db._conn.commit()


def test_cancel_after_partial_claim_refunds_remaining():
    """被领过一次且满 90 秒后收回：剩余金额退回发送者，状态变 expired。"""
    global ADMIN_SET
    ADMIN_SET = set()
    db = _make_db()
    _seed_user(db, SENDER, 1000)
    _seed_user(db, CLAIMER, 0)

    created = db.create_red_packet(SENDER, GROUP, total_amount=100, total_count=5)
    assert created.packet_id
    assert db.get_balance(SENDER) == 900
    assert db.get_balance(CLAIMER) == 0

    claim = db.claim_red_packet(CLAIMER, GROUP)
    assert claim.completed is False
    claimed_amount = claim.amount
    assert claimed_amount > 0

    _age_packet(db, created.packet_id, 91)

    sender_before = db.get_balance(SENDER)
    expected_refund = 100 - claimed_amount

    result = db.cancel_red_packet(SENDER, GROUP)
    assert result.packet_id == created.packet_id
    assert result.sender_qqid == SENDER
    assert result.group_id == GROUP
    assert result.refund == expected_refund

    assert db.get_balance(SENDER) == sender_before + expected_refund

    status = db.get_red_packet_status(GROUP)
    assert status.status == "expired"
    assert status.remaining_amount == expected_refund

    logs = db._conn.execute(
        "SELECT reason, delta FROM break_log WHERE qqid=? AND reason='red_packet_refund'",
        (SENDER,),
    ).fetchall()
    assert len(logs) == 1
    assert int(logs[0]["delta"]) == expected_refund

    usage = db._conn.execute(
        "SELECT break_spent FROM break_daily_usage WHERE qqid=? AND date=?",
        (SENDER, today_beijing()),
    ).fetchone()
    assert int(usage["break_spent"]) == 100 - expected_refund

    print("cancel after partial claim (aged): ok")


def test_cancel_before_90s_rejected():
    """发红包未满 90 秒就收回：拒绝。"""
    global ADMIN_SET
    ADMIN_SET = set()
    db = _make_db()
    _seed_user(db, SENDER, 1000)
    db.create_red_packet(SENDER, GROUP, total_amount=50, total_count=2)

    try:
        db.cancel_red_packet(SENDER, GROUP)
    except ValueError as exc:
        assert "未满 90 秒" in str(exc)
    else:
        raise AssertionError("未满 90 秒的红包不应允许收回")

    print("cancel before 90s rejected: ok")


def test_cancel_after_90s_without_claim_allowed():
    """满 90 秒但没人领过：允许收回（仅看 90 秒，不要求有人领过）。"""
    global ADMIN_SET
    ADMIN_SET = set()
    db = _make_db()
    _seed_user(db, SENDER, 1000)
    created = db.create_red_packet(SENDER, GROUP, total_amount=50, total_count=2)
    _age_packet(db, created.packet_id, 91)

    sender_before = db.get_balance(SENDER)
    result = db.cancel_red_packet(SENDER, GROUP)
    assert result.refund == 50
    assert db.get_balance(SENDER) == sender_before + 50

    print("cancel after 90s without claim allowed: ok")


def test_cancel_by_outsider_rejected():
    """非发送者非管理员收回：拒绝。"""
    global ADMIN_SET
    ADMIN_SET = set()
    db = _make_db()
    _seed_user(db, SENDER, 1000)
    _seed_user(db, CLAIMER, 0)
    _seed_user(db, OUTSIDER, 0)
    created = db.create_red_packet(SENDER, GROUP, total_amount=50, total_count=3)
    db.claim_red_packet(CLAIMER, GROUP)
    _age_packet(db, created.packet_id, 91)

    try:
        db.cancel_red_packet(OUTSIDER, GROUP)
    except ValueError as exc:
        assert "无权" in str(exc)
    else:
        raise AssertionError("无关用户不应允许收回")

    print("cancel by outsider rejected: ok")


def test_cancel_by_admin_allowed():
    """管理员收回他人红包：允许，钱仍退回原发送者。"""
    global ADMIN_SET
    ADMIN_SET = {OUTSIDER}
    db = _make_db()
    _seed_user(db, SENDER, 1000)
    _seed_user(db, CLAIMER, 0)
    _seed_user(db, OUTSIDER, 0)
    created = db.create_red_packet(SENDER, GROUP, total_amount=60, total_count=3)
    claim = db.claim_red_packet(CLAIMER, GROUP)
    expected_refund = 60 - claim.amount
    _age_packet(db, created.packet_id, 91)

    sender_before = db.get_balance(SENDER)
    outsider_before = db.get_balance(OUTSIDER)

    result = db.cancel_red_packet(OUTSIDER, GROUP)
    assert result.refund == expected_refund
    assert db.get_balance(SENDER) == sender_before + expected_refund
    assert db.get_balance(OUTSIDER) == outsider_before

    print("cancel by admin allowed (refund to sender): ok")


def test_cancel_no_active_packet_rejected():
    """本群无进行中红包：拒绝。"""
    global ADMIN_SET
    ADMIN_SET = set()
    db = _make_db()
    _seed_user(db, SENDER, 1000)

    try:
        db.cancel_red_packet(SENDER, GROUP)
    except ValueError as exc:
        assert "没有进行中" in str(exc)
    else:
        raise AssertionError("无红包时不应允许收回")

    print("cancel no active packet rejected: ok")


def test_cancel_expired_packet_rejected():
    """已过期红包（已自动退过）不能再收回：走"没有进行中"分支，且不重复退款。"""
    global ADMIN_SET
    ADMIN_SET = set()
    db = _make_db()
    _seed_user(db, SENDER, 1000)
    _seed_user(db, CLAIMER, 0)
    created = db.create_red_packet(SENDER, GROUP, total_amount=40, total_count=2)
    db.claim_red_packet(CLAIMER, GROUP)

    db._conn.execute(
        "UPDATE break_red_packet SET expires_at=? WHERE id=?",
        (time.time() - 1, created.packet_id),
    )
    db._conn.commit()
    refunds = db.expire_red_packets()
    assert len(refunds) == 1
    sender_before_expire = db.get_balance(SENDER)

    try:
        db.cancel_red_packet(SENDER, GROUP)
    except ValueError as exc:
        assert "没有进行中" in str(exc)
    else:
        raise AssertionError("已过期红包不应允许收回")

    assert db.get_balance(SENDER) == sender_before_expire

    print("cancel expired packet rejected (no double refund): ok")


def test_cancel_completed_packet_rejected():
    """已领完（completed）红包：无 active，收回走"没有进行中"分支。"""
    global ADMIN_SET
    ADMIN_SET = set()
    db = _make_db()
    _seed_user(db, SENDER, 1000)
    _seed_user(db, CLAIMER, 0)
    db.create_red_packet(SENDER, GROUP, total_amount=30, total_count=1)
    db.claim_red_packet(CLAIMER, GROUP)
    status = db.get_red_packet_status(GROUP)
    assert status.status == "completed"

    try:
        db.cancel_red_packet(SENDER, GROUP)
    except ValueError as exc:
        assert "没有进行中" in str(exc)
    else:
        raise AssertionError("已领完红包不应允许收回")

    print("cancel completed packet rejected: ok")


if __name__ == "__main__":
    test_cancel_after_partial_claim_refunds_remaining()
    test_cancel_before_90s_rejected()
    test_cancel_after_90s_without_claim_allowed()
    test_cancel_by_outsider_rejected()
    test_cancel_by_admin_allowed()
    test_cancel_no_active_packet_rejected()
    test_cancel_expired_packet_rejected()
    test_cancel_completed_packet_rejected()
    print("break red packet cancel tests: ok")
