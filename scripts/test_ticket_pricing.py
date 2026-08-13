#!/usr/bin/env python3
"""发票 BREAK 定价与旧默认值迁移规则回归测试。"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent.parent
BREAK_SOURCE = ROOT / "libraries" / "maimaidx_break.py"
ACCOUNT_SOURCE = ROOT / "command" / "mai_account.py"


def _top_level_node(path: Path, name: str, node_type: type[ast.AST]) -> ast.AST:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, node_type) and getattr(node, "name", None) == name:
            return node
    raise AssertionError(f"{name} not found in {path}")


break_tree = ast.parse(BREAK_SOURCE.read_text(encoding="utf-8"))
default_config = None
for node in break_tree.body:
    if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == "DEFAULT_CONFIG":
        default_config = ast.literal_eval(node.value)
        break
assert default_config is not None
assert default_config["ticket_cost_per_multiplier"] == "10"

service_cost_node = _top_level_node(
    ACCOUNT_SOURCE, "_service_cost", ast.AsyncFunctionDef
)


class _ImmediateAwaitable:
    def __init__(self, value):
        self.value = value

    def __await__(self):
        if False:
            yield None
        return self.value


namespace = {
    "asyncio": SimpleNamespace(
        to_thread=lambda fn, *args: _ImmediateAwaitable(fn(*args)),
    ),
    "break_db": SimpleNamespace(
        get_config=lambda key, fallback: "10" if key == "ticket_cost_per_multiplier" else fallback
    )
}
exec(
    compile(ast.Module(body=[service_cost_node], type_ignores=[]), str(ACCOUNT_SOURCE), "exec"),
    namespace,
)
service_cost = namespace["_service_cost"]
import asyncio

assert asyncio.run(service_cost("ticket", multiple=2)) == 20
assert asyncio.run(service_cost("ticket", multiple=3)) == 30
assert asyncio.run(service_cost("ticket", multiple=5)) == 50

migration_source = ast.get_source_segment(
    BREAK_SOURCE.read_text(encoding="utf-8"),
    next(
        node
        for node in next(
            node
            for node in break_tree.body
            if isinstance(node, ast.ClassDef) and node.name == "BreakDatabase"
        ).body
        if isinstance(node, ast.FunctionDef) and node.name == "_migrate_ticket_cost_default"
    ),
)
assert migration_source is not None
assert "{'2', '3'}" in migration_source
assert "倍率 ×10" in migration_source

print("ticket pricing: 2x=20, 3x=30, 5x=50; migration: OK")
