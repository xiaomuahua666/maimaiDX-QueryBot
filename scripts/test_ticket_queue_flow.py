"""AWMC v2 同步发票、到账确认与预计耗时回归测试。"""

import ast
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Optional


ROOT = Path(__file__).resolve().parent.parent
ACCOUNT_PATH = ROOT / "command" / "mai_account.py"
tree = ast.parse(ACCOUNT_PATH.read_text(encoding="utf-8"))
names = {
    "_pick",
    "_normalize_charge_payload",
    "_ticket_stock",
    "_charge_payload_user_id",
    "_ticket_estimate",
    "_format_wait_duration",
    "_ticket_wait_message",
    "_confirm_ticket_delivery",
    "_run_ticket_with_retries",
    "_exception_detail",
}
selected = [
    node
    for node in tree.body
    if (
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in names
    )
    or (isinstance(node, ast.ClassDef) and node.name == "TicketRetryableError")
]
assert {
    node.name
    for node in selected
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
} == names


async def no_sleep(_seconds):
    return None


@asynccontextmanager
async def machine_session():
    yield


class FakeEstimator:
    def __init__(self):
        self.values = {}
        self.records = []

    def estimate(self, operation, *, fallback_seconds):
        return self.values.get(operation, (int(fallback_seconds), 0))

    def record(self, operation, duration):
        self.records.append((operation, duration))


estimator = FakeEstimator()
namespace = {
    "Any": Any,
    "Awaitable": Awaitable,
    "Callable": Callable,
    "Optional": Optional,
    "maiconfig": SimpleNamespace(
        awmc_ticket_estimate_seconds=80.0,
        awmc_ticket_settlement_delay_seconds=2.0,
    ),
    "processing_time_estimator": estimator,
    "_TICKET_TIMING_KEY": "ticket:processing_seconds",
    "_LEGACY_TICKET_TIMING_KEY": "ticket_queue:seconds_per_request",
    "_TICKET_AUTO_RETRIES": 2,
    "asyncio": SimpleNamespace(sleep=no_sleep, TimeoutError=asyncio.TimeoutError),
    "time": __import__("time"),
    "re": __import__("re"),
    "log": SimpleNamespace(info=lambda *_: None, warning=lambda *_: None),
    "machine_session": machine_session,
    "redact": lambda value: str(value),
    "httpx": SimpleNamespace(
        TimeoutException=type("HttpxTimeout", (Exception,), {}),
        ConnectError=type("HttpxConnectError", (Exception,), {}),
        HTTPStatusError=type("HttpxStatusError", (Exception,), {}),
    ),
}
exec(
    compile(ast.Module(body=selected, type_ignores=[]), str(ACCOUNT_PATH), "exec"),
    namespace,
)

# 新键优先；没有新样本时继承旧队列计时样本。
assert namespace["_ticket_estimate"]() == (80, 0)
estimator.values["ticket_queue:seconds_per_request"] = (68, 7)
assert namespace["_ticket_estimate"]() == (68, 7)
estimator.values["ticket:processing_seconds"] = (61, 3)
assert namespace["_ticket_estimate"]() == (61, 3)
message = namespace["_ticket_wait_message"](61, 3)
assert "正在处理" in message
assert "最近 3 次真实处理时间" in message
assert "确认票券到账后才扣 BREAK" in message
assert "队列" not in message


async def succeeds_on_third(attempt):
    if attempt < 3:
        raise namespace["TicketRetryableError"]("explicit failure")
    return 7


result = asyncio.run(
    namespace["_run_ticket_with_retries"](succeeds_on_third, max_retries=2)
)
assert result == (7, 3)


ambiguous_calls = []


async def ambiguous_failure(attempt):
    ambiguous_calls.append(attempt)
    raise RuntimeError("timeout with unknown result")


try:
    asyncio.run(
        namespace["_run_ticket_with_retries"](ambiguous_failure, max_retries=2)
    )
except RuntimeError:
    pass
else:
    raise AssertionError("状态未知的写请求不得自动重试")
assert ambiguous_calls == [1]


class FakeSwApi:
    def __init__(self, stock=2, user_id="123"):
        self.stock = stock
        self.user_id = user_id
        self.calls = []

    async def get_user_charge(self, _qrcode):
        self.calls.append("charge")
        return {
            "returnCode": 1,
            "userId": self.user_id,
            "userChargeList": [{"chargeId": 2, "stock": self.stock}],
        }


fake_api = FakeSwApi()
namespace["sw_api"] = fake_api
stock = asyncio.run(
    namespace["_confirm_ticket_delivery"]("SGWCMAID...", 2, "123", 1)
)
assert stock == 2
assert fake_api.calls == ["charge"]

namespace["sw_api"] = FakeSwApi(stock=1)
try:
    asyncio.run(
        namespace["_confirm_ticket_delivery"]("SGWCMAID...", 2, "123", 1)
    )
except RuntimeError as exc:
    assert "到账复核未增加" in str(exc)
else:
    raise AssertionError("库存未增加时不得结算")

source = ACCOUNT_PATH.read_text(encoding="utf-8")
confirm_source = source[
    source.index("async def _confirm_ticket_delivery("):
    source.index("def _ticket_valid_timestamp(")
]
assert confirm_source.count("sw_api.get_user_charge(qrcode)") == 1
assert "get_charge_queue" not in source
assert "maiqueue" not in source
assert "processing_time_estimator.record(_TICKET_TIMING_KEY" in source
assert "状态未知" in source
assert "_TICKET_AUTO_RETRIES = 2" in source

print("ticket sync flow tests: ok")
