"""锐评预扣、成功结算与失败退款的 SQLite 回归测试。"""

import ast
import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Optional


ROOT = Path(__file__).resolve().parent.parent
source_path = ROOT / "libraries" / "maimaidx_break.py"
tree = ast.parse(source_path.read_text(encoding="utf-8"))
class_node = next(
    node
    for node in tree.body
    if isinstance(node, ast.ClassDef) and node.name == "BreakDatabase"
)
method_names = {
    "_db_lock",
    "_ensure_user",
    "_today",
    "_ensure_daily",
    "get_balance",
    "_append_log",
    "try_reserve_analysis",
    "refund_analysis_reservation",
    "settle_analysis_reservation",
    "service_is_free",
    "settle_analysis_daily_free",
}
methods = [
    node
    for node in class_node.body
    if isinstance(node, ast.FunctionDef) and node.name in method_names
]
assert {node.name for node in methods} == method_names

test_class = ast.ClassDef(
    name="ReservationDb",
    bases=[],
    keywords=[],
    body=[
        ast.Assign(
            targets=[ast.Name(id="_lock", ctx=ast.Store())],
            value=ast.Call(func=ast.Name(id="RLock", ctx=ast.Load()), args=[], keywords=[]),
        ),
        *methods,
    ],
    decorator_list=[],
)
ast.fix_missing_locations(test_class)
namespace = {
    "Optional": Optional,
    "RLock": RLock,
    "date": date,
    "datetime": datetime,
    "timedelta": timedelta,
    "timezone": timezone,
    "json": json,
    "time": time,
    "contextmanager": contextmanager,
    "DAILY_FREE_SERVICES": frozenset({'upload', 'analysis'}),
    "analysis_daily_free_enabled": lambda: True,
}
exec(compile(ast.Module(body=[test_class], type_ignores=[]), str(source_path), "exec"), namespace)

db = namespace["ReservationDb"]()
# 上游 billing_enabled 计费总开关默认开启；ReservationDb 未提取该方法，注入桩
db.billing_enabled = lambda: True
db._conn = sqlite3.connect(":memory:")
db._conn.row_factory = sqlite3.Row
db._conn.executescript(
    """
    CREATE TABLE break_users (
        qqid INTEGER PRIMARY KEY, balance INTEGER NOT NULL DEFAULT 0,
        total_analysis_count INTEGER NOT NULL DEFAULT 0,
        last_analysis_at REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL
    );
    CREATE TABLE break_daily_usage (
        qqid INTEGER NOT NULL, date TEXT NOT NULL,
        free_used INTEGER NOT NULL DEFAULT 0, query_count INTEGER NOT NULL DEFAULT 0,
        analysis_count INTEGER NOT NULL DEFAULT 0, break_spent INTEGER NOT NULL DEFAULT 0,
        break_gained INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (qqid, date)
    );
    CREATE TABLE break_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, qqid INTEGER NOT NULL,
        delta INTEGER NOT NULL, reason TEXT NOT NULL, meta TEXT, created_at REAL NOT NULL
    );
    CREATE TABLE break_service_daily (
        qqid INTEGER NOT NULL, date TEXT NOT NULL, service TEXT NOT NULL,
        success_count INTEGER NOT NULL DEFAULT 0, free_used INTEGER NOT NULL DEFAULT 0,
        break_spent INTEGER NOT NULL DEFAULT 0, last_at REAL NOT NULL,
        PRIMARY KEY (qqid, date, service)
    );
    """
)

now = time.time()
db._conn.execute(
    "INSERT INTO break_users (qqid, balance, created_at, updated_at) VALUES (1, 20, ?, ?)",
    (now, now),
)
db._conn.commit()
assert db.try_reserve_analysis(1, 10)
assert db.get_balance(1) == 10
daily = db._conn.execute("SELECT * FROM break_daily_usage WHERE qqid=1").fetchone()
assert daily is None  # 预扣失败/成功前不应为只读检查创建空的每日记录

balance = db.settle_analysis_reservation(1, 40, 10, meta={"pricing": "test"})
assert balance == -20
user = db._conn.execute("SELECT * FROM break_users WHERE qqid=1").fetchone()
daily = db._conn.execute("SELECT * FROM break_daily_usage WHERE qqid=1").fetchone()
assert user["total_analysis_count"] == 1
assert daily["analysis_count"] == 1
assert daily["break_spent"] == 40

db._conn.execute(
    "INSERT INTO break_users (qqid, balance, created_at, updated_at) VALUES (2, 18, ?, ?)",
    (now, now),
)
db._conn.commit()
assert db.try_reserve_analysis(2, 10)
assert db.refund_analysis_reservation(2, 10, meta={"reason": "test"}) == 18
daily = db._conn.execute("SELECT * FROM break_daily_usage WHERE qqid=2").fetchone()
assert daily is None

# 退款流水写入失败时，余额增加也必须回滚。
assert db.try_reserve_analysis(2, 10)
original_append_log = db._append_log
db._append_log = lambda *_args, **_kwargs: (_ for _ in ()).throw(
    RuntimeError("ledger unavailable")
)
try:
    try:
        db.refund_analysis_reservation(2, 10, meta={"reason": "test rollback"})
    except RuntimeError as exc:
        assert str(exc) == "ledger unavailable"
    else:
        raise AssertionError("退款流水失败必须向上报告")
finally:
    db._append_log = original_append_log
assert db.get_balance(2) == 8
# 回滚后的连接仍可继续完成退款。
assert db.refund_analysis_reservation(2, 10, meta={"reason": "retry"}) == 18

db._conn.execute(
    "INSERT INTO break_users (qqid, balance, created_at, updated_at) VALUES (4, 0, ?, ?)",
    (now, now),
)
db._conn.commit()
assert db.service_is_free(4, "analysis")
assert db.settle_analysis_daily_free(4, meta={"pricing": "daily_free"})
assert not db.service_is_free(4, "analysis")
assert not db.settle_analysis_daily_free(4, meta={"pricing": "daily_free"})
daily = db._conn.execute(
    "SELECT analysis_count FROM break_daily_usage WHERE qqid=4"
).fetchone()
service = db._conn.execute(
    "SELECT success_count, free_used FROM break_service_daily "
    "WHERE qqid=4 AND service='analysis'"
).fetchone()
assert daily["analysis_count"] == 1
assert (service["success_count"], service["free_used"]) == (1, 1)

db._conn.execute(
    "INSERT INTO break_users (qqid, balance, created_at, updated_at) VALUES (3, 9, ?, ?)",
    (now, now),
)
db._conn.commit()
assert not db.try_reserve_analysis(3, 10)
assert db.get_balance(3) == 9

logs = db._conn.execute(
    "SELECT delta, reason FROM break_log ORDER BY id"
).fetchall()
assert [(row["delta"], row["reason"]) for row in logs] == [
    (-10, "b50_analysis_precharge"),
    (-30, "b50_analysis_settlement"),
    (-10, "b50_analysis_precharge"),
    (10, "b50_analysis_refund"),
    (-10, "b50_analysis_precharge"),
    (10, "b50_analysis_refund"),
    (0, "b50_analysis_daily_free"),
]

print("analysis reservation tests: ok")
