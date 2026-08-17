#!/usr/bin/env python3
"""小游戏每日 BREAK 双层上限（全局 40 + 每游戏预设）行为测试。

不依赖完整插件环境的导入：直接从 maimaidx_break.py 抽取所需片段，
在内存 SQLite 上构建最小 BreakDatabase 实例进行验证。
"""

from __future__ import annotations

import ast
import json
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Dict, List, Optional


ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "libraries" / "maimaidx_break.py").read_text(encoding="utf-8")
TREE = ast.parse(SRC)


def today_beijing() -> str:
    return datetime.now(timezone(timedelta(hours=8))).date().isoformat()


def _assign_value(name: str):
    for node in TREE.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return ast.literal_eval(node.value)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and node.target.id == name and node.value is not None:
            return ast.literal_eval(node.value)
    raise RuntimeError(f"missing top-level {name}")


def _class_node(name: str) -> ast.ClassDef:
    for node in TREE.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise RuntimeError(f"missing class {name}")


_CREATE_SQL = _assign_value("_CREATE_SQL")
DEFAULT_CONFIG = _assign_value("DEFAULT_CONFIG")

# 抽取 BreakDatabase 类，并去掉类内对插件子模块的延迟相对导入。
class_src = ast.get_source_segment(SRC, _class_node("BreakDatabase"))
assert class_src is not None
class_src = class_src.replace(
    "from .maimaidx_card import card_manager",
    "card_manager = _test_card_manager",
)

# 抽取所需 dataclass 与工具函数。
def _src_of(name: str) -> str:
    for node in TREE.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name == name:
            start = node.lineno
            if isinstance(node, ast.ClassDef) and node.decorator_list:
                start = min(d.lineno for d in node.decorator_list)
            lines = SRC.splitlines()
            return "\n".join(lines[start - 1: node.end_lineno])
    raise RuntimeError(f"missing {name}")


dataclass_src = "\n".join(
    _src_of(n) for n in ("GuessBreakReward", "GameBreakAward")
)
parse_src = _src_of("_parse_config_int")

module_src = (
    "from __future__ import annotations\n"
    f"{dataclass_src}\n\n{parse_src}\n\n{class_src}\n"
)


class _Log:
    def info(self, _m):
        pass

    def warning(self, _m):
        pass


class FakeCardManager:
    """可控的双倍卡桩：测试时切换 active 即可。"""

    active = False
    remaining = 0.0
    expires = 0.0

    def double_break_info(self, qqid, *, now=None):
        if self.active:
            return (True, self.remaining, self.expires)
        return (False, 0.0, 0.0)


_TEST_CARD = FakeCardManager()


ns: dict = {
    "dataclass": __import__("dataclasses").dataclass,
    "Optional": Optional,
    "Dict": Dict,
    "List": List,
    "date": date,
    "datetime": datetime,
    "timedelta": timedelta,
    "timezone": timezone,
    "time": time,
    "json": json,
    "RLock": RLock,
    "log": _Log(),
    "BreakInsufficientError": type("BreakInsufficientError", (Exception,), {}),
    "_test_card_manager": _TEST_CARD,
    "DEFAULT_CONFIG": DEFAULT_CONFIG,
}
exec(compile(ast.parse(module_src), "maimaidx_break_extracted", "exec"), ns)

BreakDatabase = ns["BreakDatabase"]
# 跳过真实 __init__（它会建连真实数据库）；手动注入内存连接。
BreakDatabase.__init__ = lambda self: None  # type: ignore


def make_db() -> "BreakDatabase":
    db = BreakDatabase()
    db._initialized = True
    db._conn = sqlite3.connect(":memory:")
    db._conn.row_factory = sqlite3.Row
    db._conn.executescript(_CREATE_SQL)
    db._lock = RLock()
    # 关键配置：全局 40 + 9 个每游戏上限
    cfg = {
        "guess_daily_break_global_cap": "40",
        "guess_daily_caps": (
            "song:20,cover:20,tune:20,chart:20,"
            "rating:15,impostor:15,duel:10,twentyq:20,letter:15"
        ),
        "guess_break_per_correct": "1",
    }
    for k, v in cfg.items():
        db._conn.execute(
            "INSERT OR IGNORE INTO break_config (key, value) VALUES (?, ?)", (k, v)
        )
    db._conn.commit()
    # 重置双倍卡桩
    _TEST_CARD.active = False
    _TEST_CARD.remaining = 0.0
    _TEST_CARD.expires = 0.0
    return db


def game_total(db, uid, today=None):
    today = today or today_beijing()
    row = db._conn.execute(
        "SELECT COALESCE(SUM(break_awarded),0) AS t "
        "FROM break_game_daily WHERE qqid=? AND date=?",
        (uid, today),
    ).fetchone()
    return int(row["t"])


def game_awarded(db, uid, game, today=None):
    today = today or today_beijing()
    row = db._conn.execute(
        "SELECT COALESCE(break_awarded,0) AS a "
        "FROM break_game_daily WHERE qqid=? AND date=? AND game=?",
        (uid, today, game),
    ).fetchone()
    return int(row["a"]) if row else 0


# ---- 测试用例 ----


def test_parse_caps():
    db = make_db()
    assert db.get_global_game_cap() == 40, db.get_global_game_cap()
    assert db.get_game_cap("song") == 20
    assert db.get_game_cap("rating") == 15
    assert db.get_game_cap("duel") == 10
    assert db.get_game_cap("unknown") == 0
    assert db.get_game_cap("letter") == 15


def test_per_game_cap():
    db = make_db()
    uid = 1001
    # song 上限 20，每次发 4 → 第 5 次满 20，第 6 次被截断为 0
    for i in range(5):
        r = db.award_game_break(uid, "song", 4, "t")
        assert r.awarded == 4 and not r.capped, (i, r)
    assert game_awarded(db, uid, "song") == 20
    r = db.award_game_break(uid, "song", 4, "t")
    assert r.awarded == 0 and r.capped, r
    # 全局其他游戏仍可发（song 已满仅影响 song）
    r = db.award_game_break(uid, "chart", 4, "t")
    assert r.awarded == 4 and not r.capped, r


def test_global_cap_across_games():
    db = make_db()
    uid = 1002
    # song 20 + cover 20 → 全局 40 用尽
    assert db.award_game_break(uid, "song", 20, "t").awarded == 20
    assert db.award_game_break(uid, "cover", 20, "t").awarded == 20
    assert game_total(db, uid) == 40
    # 第三款游戏应被全局上限截断为 0
    r = db.award_game_break(uid, "rating", 5, "t")
    assert r.awarded == 0 and r.capped, r
    assert game_awarded(db, uid, "rating") == 0


def test_double_card_exempts_all_caps():
    db = make_db()
    uid = 1003
    _TEST_CARD.active = True
    _TEST_CARD.remaining = 600.0
    _TEST_CARD.expires = time.time() + 600
    # 双倍卡：先翻倍(30*2=60)再豁免所有上限
    r = db.award_game_break(uid, "song", 30, "t")
    assert r.doubled and not r.capped, r
    assert r.awarded == 60, r.awarded
    assert game_awarded(db, uid, "song") == 60
    # 全局已超出 40，但双倍卡期间后续仍豁免
    r2 = db.award_game_break(uid, "rating", 10, "t")
    assert r2.doubled and r2.awarded == 20 and not r2.capped, r2
    # 关闭双倍卡后，全局 40 已远超 → 非双倍发放被截断
    _TEST_CARD.active = False
    r3 = db.award_game_break(uid, "impostor", 5, "t")
    assert not r3.doubled and r3.awarded == 0 and r3.capped, r3


def test_partial_award_under_cap():
    db = make_db()
    uid = 1004
    db.award_game_break(uid, "song", 15, "t")
    db.award_game_break(uid, "cover", 15, "t")
    # 全局剩余 10；song 剩余 5 → 取 min(10,5)=5
    r = db.award_game_break(uid, "song", 10, "t")
    assert r.awarded == 5 and r.capped, r
    assert game_awarded(db, uid, "song") == 20
    assert game_total(db, uid) == 35


def test_multi_game_independence():
    db = make_db()
    uid = 1005
    assert db.award_game_break(uid, "song", 20, "t").awarded == 20
    assert db.award_game_break(uid, "chart", 20, "t").awarded == 20
    # 两款各自满 20，互不挤占；全局 40 同时用尽
    assert game_awarded(db, uid, "song") == 20
    assert game_awarded(db, uid, "chart") == 20
    assert game_total(db, uid) == 40
    # rating 被全局截断
    assert db.award_game_break(uid, "rating", 3, "t").awarded == 0


def test_daily_status_snapshot():
    db = make_db()
    uid = 1007
    assert db.award_game_break(uid, "twentyq", 16, "t").awarded == 16
    assert db.award_game_break(uid, "letter", 4, "t").awarded == 4
    total, games = db.get_game_break_daily_status(uid)
    assert total == 20
    assert games["twentyq"] == 16
    assert games["letter"] == 4


def test_award_guess_points_routes_game_key():
    db = make_db()
    uid = 1006
    # twentyq 上限 20，每次猜对发 1（guess_break_per_correct=1）
    for i in range(20):
        r = db.award_guess_points(uid, 1, game="twentyq")
        assert r.break_added == 1, (i, r)
    assert game_awarded(db, uid, "twentyq") == 20
    r = db.award_guess_points(uid, 1, game="twentyq")
    assert r.break_added == 0, r
    # guess_points（排行分）仍照常累计，不受上限影响
    row = db._conn.execute(
        "SELECT guess_points FROM break_guess_daily WHERE qqid=? AND date=?",
        (uid, today_beijing()),
    ).fetchone()
    assert int(row["guess_points"]) == 21


def test_seed_config_includes_new_keys():
    """新上限键必须进入 DEFAULT_CONFIG（_seed_config 的写入源），
    这样已有用户在重启时会通过 INSERT OR IGNORE 自动补齐。"""
    # 1) 键确实在 seed 源里
    assert "guess_daily_break_global_cap" in DEFAULT_CONFIG, "DEFAULT_CONFIG 缺少全局上限键"
    assert DEFAULT_CONFIG["guess_daily_break_global_cap"] == "40"
    assert "guess_daily_caps" in DEFAULT_CONFIG, "DEFAULT_CONFIG 缺少每游戏上限键"
    assert "rating:15" in DEFAULT_CONFIG["guess_daily_caps"]
    # 2) 模拟“旧库无新键”后由 seed 的写入逻辑补齐
    db = make_db()
    db._conn.execute("DELETE FROM break_config WHERE key='guess_daily_break_global_cap'")
    db._conn.execute("DELETE FROM break_config WHERE key='guess_daily_caps'")
    db._conn.commit()
    for k, v in DEFAULT_CONFIG.items():
        db._conn.execute(
            "INSERT OR IGNORE INTO break_config (key, value) VALUES (?, ?)", (k, v)
        )
    db._conn.commit()
    assert db.get_global_game_cap() == 40, "seed 未补齐全局上限"
    assert db.get_game_cap("rating") == 15, "seed 未补齐每游戏上限"


def run():
    tests = [
        test_parse_caps,
        test_per_game_cap,
        test_global_cap_across_games,
        test_double_card_exempts_all_caps,
        test_partial_award_under_cap,
        test_multi_game_independence,
        test_daily_status_snapshot,
        test_award_guess_points_routes_game_key,
        test_seed_config_includes_new_keys,
    ]
    for t in tests:
        t()
        print(f"  ok - {t.__name__}")
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    run()
