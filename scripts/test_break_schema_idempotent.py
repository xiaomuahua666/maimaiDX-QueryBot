#!/usr/bin/env python3
"""BREAK SQLite schema must be safe to execute on every bot restart."""

from __future__ import annotations

import ast
import re
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
tree = ast.parse(
    (ROOT / "libraries" / "maimaidx_break.py").read_text(encoding="utf-8")
)

schema = None
for node in tree.body:
    if not isinstance(node, ast.Assign):
        continue
    if any(isinstance(target, ast.Name) and target.id == "_CREATE_SQL" for target in node.targets):
        schema = ast.literal_eval(node.value)
        break

assert isinstance(schema, str) and schema.strip()
assert "CREATE INDEX IF NOT EXISTS idx_break_makeup_month" in schema

conn = sqlite3.connect(":memory:")
conn.executescript(schema)
conn.executescript(schema)
conn.close()

source = (ROOT / "libraries" / "maimaidx_break.py").read_text(encoding="utf-8")
tree = ast.parse(source)
break_database = next(
    node for node in tree.body
    if isinstance(node, ast.ClassDef) and node.name == "BreakDatabase"
)
converter = next(
    node for node in break_database.body
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    and node.name == "_sqlite_ddl_to_mysql"
)
converter_source = ast.get_source_segment(source, converter)
assert converter_source is not None
converter_source = converter_source.replace("    @staticmethod\n", "", 1)
converter_source = "\n".join(
    line[4:] if line.startswith("    ") else line
    for line in converter_source.splitlines()
)
namespace = {"re": re}
exec(converter_source, namespace)

game_table = re.search(
    r"CREATE TABLE IF NOT EXISTS break_game_daily \((.*?)\);",
    schema,
    re.DOTALL,
)
assert game_table is not None
mysql_ddl = namespace["_sqlite_ddl_to_mysql"](
    "break_game_daily", game_table.group(1)
)
assert "`date` VARCHAR(191) NOT NULL" in mysql_ddl, mysql_ddl
assert "`game` VARCHAR(191) NOT NULL" in mysql_ddl, mysql_ddl
assert "PRIMARY KEY (`qqid`, `date`, `game`)" in mysql_ddl, mysql_ddl
assert "`date` TEXT" not in mysql_ddl, mysql_ddl
assert "`game` TEXT" not in mysql_ddl, mysql_ddl

print("break schema idempotent tests: ok")
