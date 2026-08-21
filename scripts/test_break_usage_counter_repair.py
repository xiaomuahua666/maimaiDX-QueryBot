#!/usr/bin/env python3
"""AWMC 查分/分析累计在旧 MySQL 风格脏数据上的回归测试。"""

import ast
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import time
from contextlib import contextmanager
from threading import RLock
from typing import Optional


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "libraries" / "maimaidx_break.py"
tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
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
    "_append_log",
    "record_usage",
    "get_user_row",
    "get_daily_row",
}
methods = [
    node
    for node in class_node.body
    if isinstance(node, ast.FunctionDef) and node.name in method_names
]
assert {node.name for node in methods} == method_names

test_class = ast.ClassDef(
    name="UsageDb",
    bases=[],
    keywords=[],
    body=[
        ast.Assign(
            targets=[ast.Name(id="_lock", ctx=ast.Store())],
            value=ast.Call(
                func=ast.Name(id="RLock", ctx=ast.Load()), args=[], keywords=[]
            ),
        ),
        *methods,
    ],
    decorator_list=[],
)
ast.fix_missing_locations(test_class)
namespace = {
    "date": date,
    "datetime": datetime,
    "timedelta": timedelta,
    "timezone": timezone,
    "json": json,
    "time": time,
    "RLock": RLock,
    "Optional": Optional,
    "contextmanager": contextmanager,
}
exec(
    compile(ast.Module(body=[test_class], type_ignores=[]), str(SOURCE), "exec"),
    namespace,
)

db = namespace["UsageDb"]()
db._conn = sqlite3.connect(":memory:")
db._conn.row_factory = sqlite3.Row
db._conn.executescript(
    """
    CREATE TABLE break_users (
        qqid INTEGER, balance INTEGER, streak INTEGER,
        last_checkin_date TEXT, total_query_count INTEGER,
        total_analysis_count INTEGER, last_query_at REAL,
        last_analysis_at REAL, created_at REAL, updated_at REAL
    );
    -- 故意没有 (qqid, date) 主键，复现旧 MySQL 迁移表。
    CREATE TABLE break_daily_usage (
        qqid INTEGER, date TEXT, free_used INTEGER,
        query_count INTEGER, analysis_count INTEGER,
        break_spent INTEGER, break_gained INTEGER
    );
    CREATE TABLE break_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, qqid INTEGER,
        delta INTEGER, reason TEXT, meta TEXT, created_at REAL
    );
    """
)

today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
now = time.time()
db._conn.execute(
    "INSERT INTO break_users VALUES (1, 20, 0, NULL, NULL, NULL, NULL, NULL, ?, ?)",
    (now, now),
)
# 同一天多行且最早一行是 0；MAX 才是完整累计，普通 fetchone 会误读 0。
db._conn.executemany(
    "INSERT INTO break_daily_usage VALUES (1, ?, ?, ?, ?, ?, ?)",
    [
        (today, 0, 0, 0, 0, 0),
        (today, 1, 5, 2, 16, 3),
        (today, 1, 3, 1, 10, 2),
    ],
)
db._conn.commit()

daily = db.get_daily_row(1)
assert daily["query_count"] == 5
assert daily["analysis_count"] == 2
assert daily["break_spent"] == 16

user = db.get_user_row(1)
assert user["total_query_count"] == 5
assert user["total_analysis_count"] == 2

# 即使 DDL 修复临时失败，NULL + 1 也不会继续保持 NULL；读取侧仍取
# 用户累计与去重日累计中的较大值，不丢历史。
db.record_usage(1, "query", break_delta=-1)
raw_user = db._conn.execute(
    "SELECT total_query_count FROM break_users WHERE qqid=1"
).fetchone()
assert raw_user["total_query_count"] == 1
assert db.get_daily_row(1)["query_count"] == 6
assert db.get_user_row(1)["total_query_count"] == 6

source = SOURCE.read_text(encoding="utf-8")
for required in (
    "self._repair_break_usage_schema_mysql()",
    "PRIMARY KEY (`qqid`, `date`)",
    "MAX(COALESCE(`query_count`, 0))",
    "SUM(`query_count`) AS queries",
    "COALESCE(total_query_count, 0) + 1",
    "COALESCE(total_analysis_count, 0) + 1",
):
    assert required in source, required

print("break usage counter repair tests: ok")
