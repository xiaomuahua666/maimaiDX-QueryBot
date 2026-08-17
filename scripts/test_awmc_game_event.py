"""AWMC 活动事件接口、原始 businessData 与 EVENT 映射回归测试。"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
ACCOUNT_PATH = ROOT / "command" / "mai_account.py"
CLIENT_PATH = ROOT / "libraries" / "maimaidx_sw_api.py"

tree = ast.parse(ACCOUNT_PATH.read_text(encoding="utf-8"))
names = {"_pick", "_game_event_rows", "_format_user_game_event"}
selected = [
    node for node in tree.body
    if isinstance(node, ast.FunctionDef) and node.name in names
]
assert {node.name for node in selected} == names
namespace: dict[str, Any] = {
    "Any": Any,
    "json": json,
    "_GAME_EVENT_MAPPINGS": {
        26080521: "乐曲 11811、11812、11813、11814",
        24021661: "chargeId=5（付费 5 倍票券解放）",
    },
}
exec(compile(ast.Module(body=selected, type_ignores=[]), str(ACCOUNT_PATH), "exec"), namespace)

payload = {
    "type": 1,
    "gameEventList": [
        {"id": 26080521, "startDate": "2026-01-01 00:00:00.000000", "enable": 1},
        {"id": 26080521, "startDate": "2026-02-01 00:00:00.000000", "enable": 1},
        {"id": 24021661, "disableArea": ""},
    ],
}
formatted = namespace["_format_user_game_event"](payload)
assert formatted.count("EVENT 26080521") == 2, "同一 EVENT ID 的多条记录不得去重"
assert "乐曲 11811、11812、11813、11814" in formatted
assert "chargeId=5" in formatted
assert "完整 businessData" in formatted
assert '"gameEventList"' in formatted

client_source = CLIENT_PATH.read_text(encoding="utf-8")
assert "async def get_user_game_event" in client_source
assert 'self._api_path("user/game-event")' in client_source
assert 'json_body=self._machine_body(qrcode)' in client_source

account_source = ACCOUNT_PATH.read_text(encoding="utf-8")
assert 'account_game_event = on_command("maievent"' in account_source
assert 'service="awmc_game_event"' in account_source
assert '"awmc_game_event_cost", "2"' in account_source

print("awmc game event tests: ok")
