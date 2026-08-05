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
    "_ticket_queue_wait_message",
    "_confirm_ticket_delivery",
    "_run_ticket_with_retries",
    "_exception_detail",
    "_execute_ticket",
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
    "_ticket_queue_lock": asyncio.Lock(),
    "_ticket_queue_waiting": 0,
    "MessageEvent": object,
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
queue_message = namespace["_ticket_queue_wait_message"](3)
assert "已进入队列" in queue_message
assert "前面还有 3 个请求" in queue_message


# 直接运行队列包装器，验证全局串行和取消清理；实际发票流程由假函数替代。
execution_order = []
first_started = asyncio.Event()
release_first = asyncio.Event()
failing_started = asyncio.Event()
release_failing = asyncio.Event()


async def fake_execute_ticket_now(event, multiple, *, qrcode_override="", notify=None):
    execution_order.append(multiple)
    if multiple == 1:
        first_started.set()
        await release_first.wait()
    if multiple == 5:
        failing_started.set()
        await release_failing.wait()
    if multiple == 6:
        raise RuntimeError("fake ticket failure")
    return f"done-{multiple}"


namespace["_execute_ticket_now"] = fake_execute_ticket_now
notifications = {2: [], 3: [], 4: []}


async def collect_notify(multiple):
    async def notify(text):
        notifications[multiple].append(text)

    return await namespace["_execute_ticket"](
        object(), multiple, notify=notify
    )


async def exercise_queue():
    first = asyncio.create_task(collect_notify(1))
    await first_started.wait()
    second = asyncio.create_task(collect_notify(2))
    third = asyncio.create_task(collect_notify(3))
    await asyncio.sleep(0)
    assert notifications[2] and "前面还有 1 个请求" in notifications[2][0]
    assert notifications[3] and "前面还有 2 个请求" in notifications[3][0]
    release_first.set()
    assert await first == "done-1"
    assert await second == "done-2"
    assert await third == "done-3"
    assert execution_order == [1, 2, 3]

    # 排队中的任务取消后不得残留等待计数，也不能阻塞后续发票。
    failing = asyncio.create_task(collect_notify(5))
    await failing_started.wait()
    cancelled = asyncio.create_task(collect_notify(4))
    await asyncio.sleep(0)
    assert namespace["_ticket_queue_waiting"] == 1
    cancelled.cancel()
    try:
        await cancelled
    except asyncio.CancelledError:
        pass
    assert namespace["_ticket_queue_waiting"] == 0
    release_failing.set()
    assert await failing == "done-5"

    # 执行中的任务失败也必须释放锁，后续请求可以继续进入。
    failed = asyncio.create_task(collect_notify(6))
    try:
        await failed
    except RuntimeError as exc:
        assert "fake ticket failure" in str(exc)
    else:
        raise AssertionError("发票失败应向调用方抛出")
    assert namespace["_ticket_queue_waiting"] == 0
    assert not namespace["_ticket_queue_lock"].locked()


asyncio.run(exercise_queue())


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
assert "async def _execute_ticket_now(" in source
assert "async def _execute_ticket(" in source
assert "_ticket_queue_lock.acquire()" in source
assert "_ticket_queue_lock.locked() or _ticket_queue_waiting > 0" in source
assert "前面还有" in source
assert "processing_time_estimator.record(_TICKET_TIMING_KEY" in source
assert "状态未知" in source
assert "_TICKET_AUTO_RETRIES = 2" in source

print("ticket sync flow tests: ok")
