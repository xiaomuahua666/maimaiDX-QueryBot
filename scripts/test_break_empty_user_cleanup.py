"""BREAK read paths must not create users; startup cleanup keeps real data."""

import ast
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
source_path = ROOT / "libraries" / "maimaidx_break.py"
tree = ast.parse(source_path.read_text(encoding="utf-8"))
class_node = next(
    node
    for node in tree.body
    if isinstance(node, ast.ClassDef) and node.name == "BreakDatabase"
)
method_names = {
    "_prune_unbound_hash_users",
    "_prune_empty_users",
    "_today",
    "get_balance",
    "is_daily_free_available",
    "get_user_row",
    "get_daily_row",
    "is_checked_in_today",
    "get_recent_logs",
}
methods = [
    node
    for node in class_node.body
    if isinstance(node, ast.FunctionDef) and node.name in method_names
]
assert {node.name for node in methods} == method_names


class _BreakLogEntry:
    def __init__(self, delta, reason, created_at, meta):
        self.delta = delta
        self.reason = reason
        self.created_at = created_at
        self.meta = meta


class _Log:
    def info(self, _message):
        pass

    def warning(self, _message):
        pass


test_class = ast.ClassDef(
    name="CleanupDb",
    bases=[],
    keywords=[],
    body=methods,
    decorator_list=[],
)
ast.fix_missing_locations(test_class)
namespace = {
    "date": date,
    "datetime": datetime,
    "timedelta": timedelta,
    "timezone": timezone,
    "log": _Log(),
    "List": list,
    "BreakLogEntry": _BreakLogEntry,
}
exec(
    compile(ast.Module(body=[test_class], type_ignores=[]), str(source_path), "exec"),
    namespace,
)

db = namespace["CleanupDb"]()
db._conn = sqlite3.connect(":memory:")
db._conn.row_factory = sqlite3.Row
db._conn.executescript(
    """
    CREATE TABLE break_users (
        qqid INTEGER PRIMARY KEY, balance INTEGER, streak INTEGER,
        last_checkin_date TEXT, total_query_count INTEGER,
        total_analysis_count INTEGER, last_query_at REAL,
        last_analysis_at REAL, created_at REAL, updated_at REAL
    );
    CREATE TABLE break_daily_usage (
        qqid INTEGER, date TEXT, free_used INTEGER, query_count INTEGER,
        analysis_count INTEGER, break_spent INTEGER, break_gained INTEGER
    );
    CREATE TABLE break_group_checkin (first_qqid INTEGER);
    CREATE TABLE break_makeup_checkin (qqid INTEGER);
    CREATE TABLE break_log (qqid INTEGER);
    CREATE TABLE break_guess_daily (qqid INTEGER);
    CREATE TABLE break_service_daily (
        qqid INTEGER, success_count INTEGER, free_used INTEGER, break_spent INTEGER
    );
    CREATE TABLE break_daily_reward (qqid INTEGER);
    CREATE TABLE break_red_packet (id TEXT, sender_qqid INTEGER);
    CREATE TABLE break_red_packet_claim (packet_id TEXT, qqid INTEGER);
    CREATE TABLE break_gamble_pool (qqid INTEGER);
    CREATE TABLE break_gamble_pool_payout (qqid INTEGER);
    """
)

# Read-only access for an unknown user must stay read-only.
assert db.get_balance(999) == 0
assert db.is_daily_free_available(999)
assert db.get_user_row(999) == {}
assert db.get_daily_row(999) == {}
assert not db.is_checked_in_today(999)
assert db._conn.execute("SELECT COUNT(*) FROM break_users").fetchone()[0] == 0
assert db._conn.execute("SELECT COUNT(*) FROM break_daily_usage").fetchone()[0] == 0

rows = [
    (1, 0, 0, None, None, None, None, None, 1, 1),  # empty shell
    (2, 5, 0, None, 0, 0, None, None, 1, 1),       # balance: keep
    (3, 0, 0, None, 0, 0, None, None, 1, 1),       # log: keep
    (4, 0, 0, None, 0, 0, None, None, 1, 1),       # usage: keep
    (1234567890123456, 99, 0, None, 0, 0, None, None, 1, 1),  # legacy hash: remove
]
db._conn.executemany(
    "INSERT INTO break_users VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
)
db._conn.execute(
    "INSERT INTO break_daily_usage VALUES (1, ?, 0, 0, 0, 0, 0)",
    (date.today().isoformat(),),
)
db._conn.execute("INSERT INTO break_log VALUES (3)")
db._conn.execute(
    "INSERT INTO break_daily_usage VALUES (4, ?, 0, 1, 0, 0, 0)",
    (date.today().isoformat(),),
)
db._conn.execute(
    "INSERT INTO break_log VALUES (?)",
    (1234567890123456,),
)
db._conn.commit()

db._prune_unbound_hash_users()
assert db._conn.execute(
    "SELECT COUNT(*) FROM break_users WHERE qqid=1234567890123456"
).fetchone()[0] == 0
assert db._conn.execute(
    "SELECT COUNT(*) FROM break_log WHERE qqid=1234567890123456"
).fetchone()[0] == 0

db._prune_empty_users()
remaining = {
    row[0] for row in db._conn.execute("SELECT qqid FROM break_users ORDER BY qqid")
}
assert remaining == {2, 3, 4}
assert db._conn.execute(
    "SELECT COUNT(*) FROM break_daily_usage WHERE qqid=1"
).fetchone()[0] == 0

# A legacy MySQL migration may not have optional payout history yet.  Cleanup
# should still remove a genuinely empty shell instead of aborting the pass.
db._conn.execute(
    "INSERT INTO break_users VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    (5, 0, 0, None, 0, 0, None, None, 1, 1),
)
db._conn.execute("DROP TABLE break_gamble_pool_payout")
db._conn.commit()
db._prune_empty_users()
assert db.get_user_row(5) == {}

# 脏日志（NULL/非法 delta、created_at）不应让账号概览渲染直接抛异常。
db2 = namespace["CleanupDb"]()
db2._conn = sqlite3.connect(":memory:")
db2._conn.row_factory = sqlite3.Row
db2._conn.executescript(
    """
    CREATE TABLE break_log (
        qqid INTEGER, delta INTEGER, reason TEXT, meta TEXT, created_at REAL
    );
    """
)
db2._conn.execute(
    "INSERT INTO break_log VALUES (1, NULL, 'admin_grant:freedom', NULL, 123.0)"
)
db2._conn.execute("INSERT INTO break_log VALUES (1, 'bad', '', NULL, NULL)")
db2._conn.commit()
entries = db2.get_recent_logs(1, 20)
assert entries[0].delta == 0
assert entries[0].reason == "admin_grant:freedom"
assert entries[1].delta == 0
assert entries[1].created_at == 0.0
assert entries[1].reason == ""

print("BREAK empty-user cleanup tests: ok")
