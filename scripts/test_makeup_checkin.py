"""AWMC 补签阶梯价格与连续签到修复回归测试。"""

import ast
import json
import sqlite3
import sys
import time
import types
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Optional


ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "libraries" / "maimaidx_break.py"
source = SOURCE.read_text(encoding="utf-8")
tree = ast.parse(source)

names = {"parse_makeup_checkin_costs", "calculate_makeup_streak"}
selected = [
    node
    for node in tree.body
    if isinstance(node, ast.FunctionDef) and node.name in names
]
assert {node.name for node in selected} == names
namespace = {"date": date, "Optional": Optional}
exec(compile(ast.Module(body=selected, type_ignores=[]), str(SOURCE), "exec"), namespace)

parse_costs = namespace["parse_makeup_checkin_costs"]
repair = namespace["calculate_makeup_streak"]
assert parse_costs("30,60,90") == (30, 60, 90)
assert parse_costs("坏配置") == (30, 60, 90)

today = date(2026, 7, 22)
target = date(2026, 7, 21)
assert repair("2026-07-20", 8, target, today) == ("2026-07-21", 9)
assert repair("2026-07-19", 8, target, today) == ("2026-07-21", 1)
assert repair(
    "2026-07-22",
    1,
    target,
    today,
    previous_checkin_date=date(2026, 7, 20),
    previous_streak=8,
) == ("2026-07-22", 10)
assert repair("2026-07-22", 1, target, today) == ("2026-07-22", 2)
try:
    repair("2026-07-21", 9, target, today)
except ValueError as exc:
    assert "已经签到" in str(exc)
else:
    raise AssertionError("重复补签必须被拒绝")

assert "CREATE TABLE IF NOT EXISTS break_makeup_checkin" in source
assert "PRIMARY KEY (qqid, target_date)" in source
assert "def makeup_yesterday(" in source
assert "if used >= len(costs):" in source
assert "balance < cost" in source
assert "self._conn.rollback()" in source

# 用内存 SQLite 执行真实 BreakDatabase.makeup_yesterday，避免触碰工作区数据。
create_sql = next(
    ast.literal_eval(node.value)
    for node in tree.body
    if isinstance(node, ast.Assign)
    and any(getattr(target, "id", None) == "_CREATE_SQL" for target in node.targets)
)
default_config = next(
    ast.literal_eval(node.value)
    for node in tree.body
    if isinstance(node, ast.AnnAssign)
    and getattr(node.target, "id", None) == "DEFAULT_CONFIG"
)
class_nodes = [
    node
    for node in tree.body
    if (
        isinstance(node, ast.ClassDef)
        and node.name in {"MakeupCheckinResult", "BreakDatabase"}
    )
]


class FakeInsufficientError(Exception):
    def __init__(self, required, current, qqid=None):
        self.required = required
        self.current = current
        self.qqid = qqid


module_name = "makeup_checkin_db_test"
test_module = types.ModuleType(module_name)
sys.modules[module_name] = test_module
db_namespace = test_module.__dict__
db_namespace.update({
    "__name__": module_name,
    "RLock": RLock,
    "dataclass": dataclass,
    "Optional": Optional,
    "sqlite3": sqlite3,
    "time": time,
    "date": date,
    "datetime": datetime,
    "timedelta": timedelta,
    "timezone": timezone,
    "json": json,
    "DB_DIR": Path("."),
    "DB_PATH": Path(":memory:"),
    "DEFAULT_CONFIG": default_config,
    "BreakInsufficientError": FakeInsufficientError,
    "parse_makeup_checkin_costs": parse_costs,
    "calculate_makeup_streak": repair,
    "log": type("Log", (), {"info": staticmethod(lambda *args, **kwargs: None)})(),
    "contextmanager": contextmanager,
})
module = ast.Module(
    body=[ast.ImportFrom(module="__future__", names=[ast.alias("annotations")], level=0), *class_nodes],
    type_ignores=[],
)
module = ast.fix_missing_locations(module)
exec(compile(module, str(SOURCE), "exec"), db_namespace)
BreakDatabase = db_namespace["BreakDatabase"]
db = object.__new__(BreakDatabase)
db._initialized = True
db._conn = sqlite3.connect(":memory:")
db._conn.row_factory = sqlite3.Row
db._conn.executescript(create_sql)
db._conn.execute(
    "INSERT INTO break_config (key, value) VALUES (?, ?)",
    ("makeup_checkin_costs", "30,60,90"),
)
# makeup_yesterday 的「今天」以 UTC+8 为准；CI（UTC）在北京时间 0:00~8:00
# 运行时 date.today() 会比 _today_cn() 少一天，断言必须同基准。
today_real = datetime.now(timezone(timedelta(hours=8))).date()
target_real = date.fromordinal(today_real.toordinal() - 1)
prior_real = date.fromordinal(target_real.toordinal() - 1)
db._ensure_user(10001)
db._conn.execute(
    "UPDATE break_users SET balance=200, streak=8, last_checkin_date=? WHERE qqid=10001",
    (prior_real.isoformat(),),
)
db._conn.commit()
result = db.makeup_yesterday(10001)
assert (result.cost, result.balance, result.streak, result.monthly_no) == (30, 170, 9, 1)
row = db._conn.execute(
    "SELECT balance, streak, last_checkin_date FROM break_users WHERE qqid=10001"
).fetchone()
assert tuple(row) == (170, 9, target_real.isoformat())
try:
    db.makeup_yesterday(10001)
except ValueError as exc:
    assert "已经签到" in str(exc)
else:
    raise AssertionError("同一天不得重复补签")

used_month = today_real.strftime("%Y-%m")
db._ensure_user(10002)
db._conn.execute(
    "UPDATE break_users SET balance=200 WHERE qqid=10002"
)
db._conn.execute(
    """INSERT INTO break_makeup_checkin
       (qqid, target_date, used_month, monthly_no, cost, streak, created_at)
       VALUES (10002, '1900-01-01', ?, 1, 30, 1, ?)""",
    (used_month, time.time()),
)
db._conn.commit()
second = db.makeup_yesterday(10002)
assert (second.cost, second.monthly_no, second.next_cost) == (60, 2, 90)

db._ensure_user(10003)
db._conn.execute("UPDATE break_users SET balance=500 WHERE qqid=10003")
for index, cost in enumerate((30, 60, 90), start=1):
    db._conn.execute(
        """INSERT INTO break_makeup_checkin
           (qqid, target_date, used_month, monthly_no, cost, streak, created_at)
           VALUES (10003, ?, ?, ?, ?, 1, ?)""",
        (f"1900-01-0{index}", used_month, index, cost, time.time()),
    )
db._conn.commit()
try:
    db.makeup_yesterday(10003)
except ValueError as exc:
    assert "次数已用完" in str(exc)
else:
    raise AssertionError("每月第四次补签必须被拒绝")

command_source = (ROOT / "command" / "mai_break.py").read_text(encoding="utf-8")
assert "awmc_makeup_checkin = on_command(" in command_source
assert "await asyncio.to_thread(break_db.makeup_yesterday, qqid)" in command_source
assert "setattr(awmc_makeup_checkin, '_maimaidx_busy_surcharge_exempt', True)" in command_source

print("makeup checkin tests: ok")
