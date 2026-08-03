"""AWMC v2 预览/道具只读查询与计费回归测试。"""

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
ACCOUNT_PATH = ROOT / "command" / "mai_account.py"
tree = ast.parse(ACCOUNT_PATH.read_text(encoding="utf-8"))
names = {
    "_pick",
    "_nested_preview",
    "_merged_preview",
    "_preview_line",
    "_format_user_preview",
    "_flatten_user_items",
    "_format_user_items",
    "_flatten_gate_status",
    "_format_gate_status",
}
selected = [
    node
    for node in tree.body
    if isinstance(node, ast.FunctionDef) and node.name in names
]
assert {node.name for node in selected} == names
namespace = {
    "Any": Any,
    "Optional": __import__("typing").Optional,
    "_ITEM_KIND_LABELS": {
        1: "姓名框",
        2: "称号",
        9: "角色",
        15: "钥匙(高风险类型)",
    },
}
exec(compile(ast.Module(body=selected, type_ignores=[]), str(ACCOUNT_PATH), "exec"), namespace)

preview = namespace["_format_user_preview"](
    {
        "userId": 123456789,
        "userPreview": {
            "userName": "TEST",
            "playerRating": 15000,
            "playCount": 321,
        },
    }
)
assert "用户名：TEST" in preview
assert "Rating：15000" in preview
assert "总游玩次数：321" in preview
assert "123456789" not in preview

items_payload = {
    "userItemList": [
        {"itemKind": 2, "itemId": 100, "stock": 1},
        {"itemKind": 9, "userItemList": [{"itemId": 200}, {"itemId": 201}]},
        {"itemKind": 15, "itemId": 300},
    ]
}
rows = namespace["_flatten_user_items"](items_payload)
assert len(rows) == 4
items = namespace["_format_user_items"](items_payload)
assert "称号（kind=2）：100" in items
assert "角色（kind=9）：200、201" in items
assert "钥匙(高风险类型)（kind=15）：300" in items

gates = namespace["_format_gate_status"](
    {
        "userKaleidxScopeList": [
            {"gateId": 7, "isGateFound": True, "isKeyFound": 1, "isClear": False},
            {"gateId": 3, "isGateFound": False, "isKeyFound": 0, "isClear": True},
        ]
    }
)
assert "Gate 3：发现 否 · 钥匙 否 · 通关 是" in gates
assert "Gate 7：发现 是 · 钥匙 是 · 通关 否" in gates

client_source = (ROOT / "libraries" / "maimaidx_sw_api.py").read_text(encoding="utf-8")
assert 'self._api_path("user/preview")' in client_source
assert 'self._api_path("user/item-list")' in client_source
assert 'self._api_path("user/kaleidx-scope")' in client_source
assert "async def get_user_data(" in client_source

account_source = ACCOUNT_PATH.read_text(encoding="utf-8")
assert 'account_preview = on_command("mai预览")' in account_source
assert 'account_items = on_command("mai道具")' in account_source
assert 'account_gate_status = on_command("mai门状态"' in account_source
assert 'service="awmc_gate_status"' in account_source
assert 'break_db.get_config("awmc_read_cost", "5")' in account_source
assert account_source.index("result = await fetch(binding.qrcode)") < account_source.index(
    "charge = break_db.settle_service_success(",
    account_source.index("async def _run_paid_awmc_read("),
)

break_source = (ROOT / "libraries" / "maimaidx_break.py").read_text(encoding="utf-8")
assert "'awmc_read_cost': '5'" in break_source

print("awmc read query tests: ok")
