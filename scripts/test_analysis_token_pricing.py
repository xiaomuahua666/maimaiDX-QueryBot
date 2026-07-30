"""锐评 Token usage 提取与计费回归测试（无需启动 NoneBot）。"""

import ast
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional


ROOT = Path(__file__).resolve().parent.parent
break_source = (ROOT / "libraries" / "maimaidx_break.py").read_text(encoding="utf-8")
assert "self._migrate_analysis_token_rates_default()" in break_source


def load_functions(path: Path, names: set[str], namespace: dict) -> dict:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    selected = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    assert {node.name for node in selected} == names
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


pricing_config = {
    "analysis_input_tokens_per_break": 4000,
    "analysis_output_tokens_per_break": 1000,
    "analysis_min_cost": 2,
    "analysis_max_cost": 20,
    "analysis_fallback_cost": 4,
    "analysis_price_multiplier": 5,
    "analysis_precharge_cost": 10,
}


def config_int(key: str, default: int) -> int:
    return int(pricing_config.get(key, default))


pricing = load_functions(
    ROOT / "libraries" / "maimaidx_break.py",
    {
        "analysis_price_multiplier",
        "analysis_precharge_cost",
        "analysis_token_cost",
        "format_analysis_cost_line",
    },
    {"Optional": Optional, "math": math, "_config_int": config_int},
)
cost = pricing["analysis_token_cost"]
assert cost(0, 0) == 10
assert cost(4000, 1000) == 10
assert cost(4001, 1000) == 15
assert cost(8000, 2000) == 20
assert cost(16000, 4000) == 40
assert cost(999999, 999999) == 100
assert cost(0, 0, usage_available=False) == 20

line = pricing["format_analysis_cost_line"](
    charged=40,
    balance=21,
    input_tokens=16000,
    output_tokens=4000,
)
assert "锐评消耗 40 BREAK" in line
assert "输入 16,000 / 输出 4,000 Token" in line
assert "输入每 4,000 Token + 输出每 1,000 Token" in line
assert "基础价合计向上取整后 ×5" in line
assert "最低 10、最高 100" in line

usage_helpers = load_functions(
    ROOT / "libraries" / "b50_analysis" / "llm.py",
    {"_i", "_response_token_usage"},
    {"Any": Any},
)
usage = usage_helpers["_response_token_usage"](
    SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=12345,
            completion_tokens=2345,
            total_tokens=14690,
            prompt_tokens_details=SimpleNamespace(cached_tokens=1000),
        )
    )
)
assert usage == {
    "available": True,
    "input_tokens": 12345,
    "output_tokens": 2345,
    "total_tokens": 14690,
    "cached_input_tokens": 1000,
}
assert not usage_helpers["_response_token_usage"](
    SimpleNamespace(usage=None)
)["available"]
assert not usage_helpers["_response_token_usage"](
    {"usage": {"total_tokens": 12000}}
)["available"]
dict_usage = usage_helpers["_response_token_usage"](
    {
        "usage": {
            "input_tokens": 9000,
            "output_tokens": 1200,
            "total_tokens": 10200,
        }
    }
)
assert dict_usage["input_tokens"] == 9000
assert dict_usage["output_tokens"] == 1200


class FakeBreakDb:
    def __init__(self):
        self.balance = 20
        self.adjustment = None
        self.usage = None

    def add_balance(self, qqid, delta, reason, *, meta=None):
        self.adjustment = (qqid, delta, reason, meta)
        self.balance += delta
        return self.balance

    def record_usage(self, qqid, kind, break_delta=0):
        self.usage = (qqid, kind, break_delta)

    def settle_analysis_reservation(self, qqid, cost, reserved, *, meta=None):
        self.adjustment = (qqid, reserved - cost, "b50_analysis_settlement", meta)
        self.balance += reserved - cost
        self.usage = (qqid, "analysis", -cost)
        return self.balance

fake_db = FakeBreakDb()
settlement = load_functions(
    ROOT / "libraries" / "maimaidx_break.py",
    {"settle_analysis_charge"},
    {
        "Optional": Optional,
        "break_db": fake_db,
        "is_superuser_exempt": lambda _qqid: False,
        "analysis_price_multiplier": lambda: 5,
        "log": SimpleNamespace(info=lambda *_args, **_kwargs: None),
    },
)
charged = settlement["settle_analysis_charge"](
    10001,
    40,
    reserved=10,
    token_usage={"input_tokens": 16000, "output_tokens": 4000},
)
assert charged == 40
assert fake_db.adjustment[1:3] == (-30, "b50_analysis_settlement")
assert fake_db.adjustment[3]["pricing"] == "token_x_multiplier"
assert fake_db.balance == -10
assert fake_db.usage == (10001, "analysis", -40)

analysis_command = (ROOT / "command" / "mai_b50_analysis.py").read_text(
    encoding="utf-8"
)
assert "break_billing" not in analysis_command
assert "reserve_analysis_charge" in analysis_command
assert "refund_analysis_charge" in analysis_command
assert "format_analysis_pricing_help" in analysis_command

print("analysis token pricing tests: ok")
