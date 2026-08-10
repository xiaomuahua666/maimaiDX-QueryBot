"""“我的 AWMC”展示 20 条 BREAK 流水，并精简 Ref 记录条数。"""

import ast
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import List


ROOT = Path(__file__).resolve().parent.parent
BREAK_PATH = ROOT / "libraries" / "maimaidx_break.py"
IMAGE_PATH = ROOT / "libraries" / "maimaidx_awmc_image.py"

tree = ast.parse(BREAK_PATH.read_text(encoding="utf-8"))
class_node = next(
    node
    for node in tree.body
    if isinstance(node, ast.ClassDef) and node.name == "BreakDatabase"
)
method = next(
    node
    for node in class_node.body
    if isinstance(node, ast.FunctionDef) and node.name == "get_recent_logs"
)
assert isinstance(method.args.defaults[-1], ast.Constant)
assert method.args.defaults[-1].value == 20

test_class = ast.ClassDef(
    name="RecentLogDb",
    bases=[],
    keywords=[],
    body=[method],
    decorator_list=[],
)
ast.fix_missing_locations(test_class)
namespace = {
    "List": List,
    "BreakLogEntry": lambda **values: SimpleNamespace(**values),
}
exec(
    compile(ast.Module(body=[test_class], type_ignores=[]), str(BREAK_PATH), "exec"),
    namespace,
)

db = namespace["RecentLogDb"]()
db._conn = sqlite3.connect(":memory:")
db._conn.row_factory = sqlite3.Row
db._conn.execute(
    "CREATE TABLE break_log ("
    "qqid INTEGER, delta INTEGER, reason TEXT, meta TEXT, created_at REAL)"
)
db._conn.executemany(
    "INSERT INTO break_log VALUES (?, ?, ?, ?, ?)",
    [(10001, index, f"reason-{index}", None, float(index)) for index in range(25)],
)
logs = db.get_recent_logs(10001)
assert len(logs) == 20
assert [entry.delta for entry in logs] == list(range(24, 4, -1))

break_source = BREAK_PATH.read_text(encoding="utf-8")
image_source = IMAGE_PATH.read_text(encoding="utf-8")
assert "recent_logs=break_db.get_recent_logs(qqid, 20)" in break_source
assert "最近 BREAK 记录（最多 20 条）" in break_source
assert "最近账号功能记录（最多 5 条）" in break_source
assert "profile.recent_account_logs[:5]" in break_source
assert "entry[\"ref_id\"]" in break_source
assert "min(20, len(recent_break))" in image_source
assert "recent_break[:20]" in image_source
assert "min(5, len(recent_acc))" in image_source
assert "recent_acc[:5]" in image_source
assert "entry.get('ref_id'" in image_source

print("break recent logs tests: ok")
