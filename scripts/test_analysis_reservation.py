"""锐评预扣、成功结算与失败退款的 SQLite 回归测试。"""

import ast
import json
import sqlite3
import time
from datetime import date
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
    "_ensure_user",
    "_today",
    "_ensure_daily",
    "get_balance",
    "_append_log",
    "try_reserve_analysis",
    "refund_analysis_reservation",
    "settle_analysis_reservation",
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
    "json": json,
    "time": time,
}
exec(compile(ast.Module(body=[test_class], type_ignores=[]), str(source_path), "exec"), namespace)

db = namespace["ReservationDb"]()
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
    """
)

now = time.time()
db._conn.execute(
    "INSERT INTO break_users (qqid, balance, created_at, updated_at) VALUES (1, 10, ?, ?)",
    (now, now),
)
db._conn.commit()
assert db.try_reserve_analysis(1, 6)
assert db.get_balance(1) == 4
daily = db._conn.execute("SELECT * FROM break_daily_usage WHERE qqid=1").fetchone()
assert daily["break_spent"] == 0

balance = db.settle_analysis_reservation(1, 12, 6, meta={"pricing": "test"})
assert balance == -2
user = db._conn.execute("SELECT * FROM break_users WHERE qqid=1").fetchone()
daily = db._conn.execute("SELECT * FROM break_daily_usage WHERE qqid=1").fetchone()
assert user["total_analysis_count"] == 1
assert daily["analysis_count"] == 1
assert daily["break_spent"] == 12

db._conn.execute(
    "INSERT INTO break_users (qqid, balance, created_at, updated_at) VALUES (2, 8, ?, ?)",
    (now, now),
)
db._conn.commit()
assert db.try_reserve_analysis(2, 6)
assert db.refund_analysis_reservation(2, 6, meta={"reason": "test"}) == 8
daily = db._conn.execute("SELECT * FROM break_daily_usage WHERE qqid=2").fetchone()
assert daily["analysis_count"] == 0
assert daily["break_spent"] == 0
assert daily["break_gained"] == 0

db._conn.execute(
    "INSERT INTO break_users (qqid, balance, created_at, updated_at) VALUES (3, 5, ?, ?)",
    (now, now),
)
db._conn.commit()
assert not db.try_reserve_analysis(3, 6)
assert db.get_balance(3) == 5

logs = db._conn.execute(
    "SELECT delta, reason FROM break_log ORDER BY id"
).fetchall()
assert [(row["delta"], row["reason"]) for row in logs] == [
    (-6, "b50_analysis_precharge"),
    (-6, "b50_analysis_settlement"),
    (-6, "b50_analysis_precharge"),
    (6, "b50_analysis_refund"),
]

print("analysis reservation tests: ok")
