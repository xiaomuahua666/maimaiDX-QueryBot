#!/usr/bin/env python3
"""BREAK SQLite schema must be safe to execute on every bot restart."""

from __future__ import annotations

import ast
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

print("break schema idempotent tests: ok")
