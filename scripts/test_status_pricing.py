#!/usr/bin/env python3
"""mymai / mai状态 BREAK 定价回归测试。"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent.parent
BREAK_SOURCE = ROOT / "libraries" / "maimaidx_break.py"
ACCOUNT_SOURCE = ROOT / "command" / "mai_account.py"


def _function_node(path: Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {path}")


break_tree = ast.parse(BREAK_SOURCE.read_text(encoding="utf-8"))
default_config = None
for node in break_tree.body:
    if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == "DEFAULT_CONFIG":
        default_config = ast.literal_eval(node.value)
        break
assert default_config is not None
assert default_config["awmc_status_cost"] == "2"

service_cost_node = _function_node(ACCOUNT_SOURCE, "_service_cost")
namespace = {
    "break_db": SimpleNamespace(
        get_config=lambda key, fallback: fallback,
    )
}
exec(
    compile(ast.Module(body=[service_cost_node], type_ignores=[]), str(ACCOUNT_SOURCE), "exec"),
    namespace,
)
service_cost = namespace["_service_cost"]
assert service_cost("awmc_status") == 2

charge_text_node = _function_node(ACCOUNT_SOURCE, "_charge_text")
namespace = {}
exec(
    compile(ast.Module(body=[charge_text_node], type_ignores=[]), str(ACCOUNT_SOURCE), "exec"),
    namespace,
)
charge_text = namespace["_charge_text"]
result = SimpleNamespace(
    service="awmc_status", free=False, charged=2, balance=98
)
text = charge_text(result)
assert "账号状态查询" in text
assert "消耗 2 BREAK" in text

source_text = ACCOUNT_SOURCE.read_text(encoding="utf-8")
assert 'service == "awmc_status"' in source_text
assert '"awmc_status": "账号状态查询"' in source_text
assert 'settle_service_success(' in source_text
assert 'insufficient_break' in source_text

print("mymai status pricing: default=2, label+charge wiring OK")
