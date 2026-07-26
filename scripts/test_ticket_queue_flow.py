"""发票队列优先、到账单次确认与动态超时回归测试。"""

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
    "_matching_charge_task",
    "_ticket_submission_task_id",
    "_ticket_queue_ahead",
    "_ticket_task_result_code",
    "_charge_payload_user_id",
    "_ticket_queue_units",
    "_ticket_wait_plan",
    "_format_wait_duration",
    "_ticket_wait_message",
    "_ticket_task_state",
    "_await_ticket_delivery",
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


class FakeLog:
    def info(self, *_args):
        pass

    def warning(self, *_args):
        pass


class FakeTime:
    def __init__(self):
        self.value = 0.0

    def monotonic(self):
        self.value += 0.1
        return self.value

    def perf_counter(self):
        self.value += 0.1
        return self.value


async def no_sleep(_seconds):
    return None


@asynccontextmanager
async def machine_session():
    yield


config = SimpleNamespace(
    awmc_ticket_poll_interval_seconds=1.0,
    awmc_ticket_poll_timeout_seconds=120.0,
    awmc_ticket_max_poll_timeout_seconds=600.0,
    awmc_ticket_seconds_per_request=80.0,
)


class FakeEstimator:
    seconds = 80
    samples = 0
    records = []

    def estimate(self, _operation, *, fallback_seconds):
        return (self.seconds or int(fallback_seconds), self.samples)

    def record(self, operation, duration):
        self.records.append((operation, duration))


estimator = FakeEstimator()
namespace = {
    "Any": Any,
    "Awaitable": Awaitable,
    "Callable": Callable,
    "Optional": Optional,
    "maiconfig": config,
    "processing_time_estimator": estimator,
    "_TICKET_QUEUE_UNIT_TIMING_KEY": "ticket_queue:seconds_per_request",
    "_TICKET_AUTO_RETRIES": 2,
    "json": __import__("json"),
    "re": __import__("re"),
    "asyncio": SimpleNamespace(sleep=no_sleep, TimeoutError=asyncio.TimeoutError),
    "time": FakeTime(),
    "log": FakeLog(),
    "machine_session": machine_session,
    "_ensure_business_success": lambda payload: None,
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


retry_calls = []
retry_notices = []


async def succeeds_on_third(attempt):
    retry_calls.append(attempt)
    if attempt < 3:
        raise namespace["TicketRetryableError"]("explicit failure")
    return 7


async def retry_notify(message):
    retry_notices.append(message)


retry_result = asyncio.run(
    namespace["_run_ticket_with_retries"](
        succeeds_on_third, notify=retry_notify, max_retries=2
    )
)
assert retry_result == (7, 3)
assert retry_calls == [1, 2, 3]
assert len(retry_notices) == 2


failed_calls = []


async def always_explicit_failure(attempt):
    failed_calls.append(attempt)
    raise namespace["TicketRetryableError"]("explicit failure")


try:
    asyncio.run(
        namespace["_run_ticket_with_retries"](
            always_explicit_failure, max_retries=2
        )
    )
except namespace["TicketRetryableError"] as exc:
    assert "已自动重试 2 次" in str(exc)
else:
    raise AssertionError("连续三次明确失败后应返回失败")
assert failed_calls == [1, 2, 3]


ambiguous_calls = []


async def ambiguous_failure(attempt):
    ambiguous_calls.append(attempt)
    raise RuntimeError("timeout with unknown result")


try:
    asyncio.run(
        namespace["_run_ticket_with_retries"](
            ambiguous_failure, max_retries=2
        )
    )
except RuntimeError as exc:
    assert "unknown result" in str(exc)
else:
    raise AssertionError("结果不确定的失败不应盲目重试")
assert ambiguous_calls == [1]

assert namespace["_ticket_submission_task_id"](
    {"code": 0, "data": {"taskId": "task-1"}}
) == "task-1"
assert namespace["_ticket_queue_ahead"]({"queuePosition": 3}) == 2
assert namespace["_ticket_queue_ahead"]({"msg": "前方还有 4 个请求"}) == 4
assert namespace["_ticket_queue_ahead"](
    {"code": 0, "msg": "排队成功，当前队列任务数≈1"}
) == 1
queue_success = {
    "status": "done",
    "msg": '充值成功, result={"returnCode": 1, "apiName": "UpsertUserChargelogApi"}',
}
queue_failure = {
    "status": "done",
    "msg": '充值成功, result={"returnCode": 0, "apiName": "UpsertUserChargelogApi"}',
}
queue_other_failure = {
    "status": "done",
    "msg": '充值失败, result={"returnCode": -1, "apiName": "UpsertUserChargelogApi"}',
}
assert namespace["_ticket_task_result_code"](queue_success) == 1
assert namespace["_ticket_task_result_code"](queue_failure) == 0
assert namespace["_ticket_task_result_code"](queue_other_failure) == -1
assert namespace["_ticket_task_state"](queue_success) == "success"
assert namespace["_ticket_task_state"](queue_failure) == "failed"
assert namespace["_ticket_task_state"](queue_other_failure) == "failed"
assert namespace["_charge_payload_user_id"](
    {"userId": 13225939, "userChargeList": []}
) == "13225939"
assert namespace["_ticket_queue_units"](0) == 1
assert namespace["_ticket_queue_units"](1) == 1
assert namespace["_ticket_wait_plan"](0) == (80, 120.0, 0)
assert namespace["_ticket_wait_plan"](1) == (80, 120.0, 0)
assert namespace["_ticket_wait_plan"](2) == (160, 200.0, 0)
assert namespace["_ticket_wait_plan"](10) == (800, 600.0, 0)
estimator.seconds = 68
estimator.samples = 7
assert namespace["_ticket_wait_plan"](2) == (136, 176.0, 7)
message = namespace["_ticket_wait_message"](2, 136, 176, 7)
assert "队列预计有 2 个请求待处理" in message
assert "根据最近 7 次真实处理时间估算" in message
assert "确认票券到账后才扣 BREAK" in message


class FakeSwApi:
    def __init__(self):
        self.calls = []
        self.queues = [
            {
                "code": 0,
                "tasks": [
                    {
                        "taskId": "task-1",
                        "chargeId": 2,
                        "userId": "123",
                        "status": "processing",
                        "ts": "new",
                    }
                ],
            },
            {
                "code": 0,
                "tasks": [
                    {
                        "taskId": "task-1",
                        "chargeId": 2,
                        "userId": "123",
                        "status": "completed",
                        "ts": "new",
                        "msg": '充值成功, result={"returnCode": 1}',
                    }
                ],
            },
        ]

    async def get_charge_queue(self):
        self.calls.append("queue")
        return self.queues.pop(0)

    async def get_user_charge(self, _qrcode):
        self.calls.append("charge")
        return {
            "returnCode": 1,
            "userCharge": {
                "userChargeList": [{"chargeId": 2, "stock": 2}],
                "userFreeChargeList": [],
            },
        }


fake_api = FakeSwApi()
namespace["sw_api"] = fake_api
stock = asyncio.run(
    namespace["_await_ticket_delivery"](
        "SGWCMAID...",
        2,
        "123",
        1,
        "old",
        task_id="task-1",
        timeout=120,
        timing_started_at=0.0,
        timing_units=1,
    )
)
assert stock == 2
assert fake_api.calls == ["queue", "queue", "charge"]
assert len(estimator.records) == 1


class FailedQueueApi:
    def __init__(self):
        self.calls = []

    async def get_charge_queue(self):
        self.calls.append("queue")
        return {
            "code": 0,
            "tasks": [
                {
                    "taskId": "task-failed",
                    "chargeId": 2,
                    "userId": "123",
                    "status": "done",
                    "ts": "newer",
                    "msg": '充值失败, result={"returnCode": 0}',
                }
            ],
        }

    async def get_user_charge(self, _qrcode):
        self.calls.append("charge")
        raise AssertionError("异常 returnCode 时不应查询票券库存")


failed_api = FailedQueueApi()
namespace["sw_api"] = failed_api
try:
    asyncio.run(
        namespace["_await_ticket_delivery"](
            "SGWCMAID...",
            2,
            "123",
            0,
            "old",
            task_id="task-failed",
            timeout=120,
            timing_started_at=0.0,
            timing_units=1,
        )
    )
except RuntimeError as exc:
    assert "returnCode=0" in str(exc)
else:
    raise AssertionError("队列异常 returnCode 应被判定为失败")
assert failed_api.calls == ["queue"]
assert len(estimator.records) == 2

source = ACCOUNT_PATH.read_text(encoding="utf-8")
await_source = source[
    source.index("async def _await_ticket_delivery("):
    source.index("def _ticket_valid_timestamp(")
]
assert await_source.count("sw_api.get_user_charge(qrcode)") == 1
assert await_source.index("last_task_status == \"success\"") < await_source.index(
    "sw_api.get_user_charge(qrcode)"
)
assert "processing_time_estimator.record(" in source
assert "timing_started_at=queue_started_at" in source
assert "_TICKET_AUTO_RETRIES = 2" in source
assert "verified_stock, attempts = await _run_ticket_with_retries(" in source
assert "UID {current_uid}" not in await_source
assert "UID {mai_uid}" not in await_source
public_detail = namespace["_exception_detail"](
    RuntimeError("：UID 11470224 与当前绑定账号不一致")
)
assert "11470224" not in public_detail
assert "UID" not in public_detail
assert "账号标识[已隐藏]" in public_detail

print("ticket queue flow tests: ok")
