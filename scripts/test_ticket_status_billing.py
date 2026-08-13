#!/usr/bin/env python3
"""舞萌票券状态查询的成功后计费与 FREEDOM 文案回归测试。"""

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path = [item for item in sys.path if item and Path(item).resolve() != ROOT]
sys.path.insert(0, str(ROOT.parent))

os.environ.setdefault("COMMAND_START", '["", "/"]')
os.environ.setdefault("MAIMAIDXPATH", str(ROOT / "static"))
os.environ.setdefault("MAIMAIDX_STORAGE_FAIL_FAST", "false")
os.environ.setdefault("MAIMAIDX_MESSAGE_STATS_ENABLED", "false")
os.environ.setdefault("MAIMAIDX_BUSY_SURCHARGE_ENABLED", "false")

import nonebot

nonebot.init()

import nonebot_plugin_maimaidx  # noqa: F401, E402
from nonebot_plugin_maimaidx.command import mai_account  # noqa: E402
from nonebot_plugin_maimaidx.libraries import maimaidx_break  # noqa: E402


async def test_ticket_status_success_billing() -> None:
    payer = 123456789
    event = SimpleNamespace(user_id=payer)
    calls: list[str] = []
    settled: list[tuple[int, str, int, dict]] = []
    original = {
        "binding": mai_account._binding_or_error,
        "machine_session": mai_account.machine_session,
        "service_cost": mai_account._service_cost,
        "ensure": maimaidx_break.break_db.ensure_service_affordable,
        "settle": maimaidx_break.break_db.settle_service_success,
        "log": mai_account._log,
    }

    @asynccontextmanager
    async def machine_session():
        calls.append("machine_enter")
        yield

    async def fetch(qrcode: str):
        calls.append(f"fetch:{qrcode}")
        return {"userChargeList": []}

    def formatter(_payload) -> str:
        calls.append("format")
        return "票券结果"

    def settle(qqid: int, service: str, cost: int, *, meta: dict):
        calls.append("settle")
        settled.append((qqid, service, cost, meta))
        return SimpleNamespace(
            service=service,
            charged=cost,
            listed_cost=cost,
            free=False,
            freedom=False,
            freedom_remaining=0,
            balance=9,
        )

    mai_account._binding_or_error = lambda _event: (
        str(payer), SimpleNamespace(qrcode="SGWCMAID-TEST"), None
    )
    mai_account.machine_session = machine_session
    async def service_cost(service: str) -> int:
        return 1 if service == "ticket_status" else 0

    mai_account._service_cost = service_cost
    maimaidx_break.break_db.ensure_service_affordable = (
        lambda qqid, service, cost: calls.append(f"ensure:{qqid}:{service}:{cost}")
    )
    maimaidx_break.break_db.settle_service_success = settle
    mai_account._log = lambda *_args, **_kwargs: "ref-test"
    try:
        text = await mai_account._run_paid_awmc_read(
            event,
            service="ticket_status",
            fetch=fetch,
            formatter=formatter,
        )
    finally:
        mai_account._binding_or_error = original["binding"]
        mai_account.machine_session = original["machine_session"]
        mai_account._service_cost = original["service_cost"]
        maimaidx_break.break_db.ensure_service_affordable = original["ensure"]
        maimaidx_break.break_db.settle_service_success = original["settle"]
        mai_account._log = original["log"]

    assert calls == [
        f"ensure:{payer}:ticket_status:1",
        "machine_enter",
        "fetch:SGWCMAID-TEST",
        "format",
        "settle",
    ]
    assert settled == [
        (payer, "ticket_status", 1, {"operation": "ticket_status"})
    ]
    assert "舞萌票券状态消耗 1 BREAK" in text
    assert "余额 9 BREAK" in text


async def test_ticket_status_failure_does_not_settle() -> None:
    payer = 123456789
    event = SimpleNamespace(user_id=payer)
    settled = False
    original = {
        "binding": mai_account._binding_or_error,
        "machine_session": mai_account.machine_session,
        "service_cost": mai_account._service_cost,
        "ensure": maimaidx_break.break_db.ensure_service_affordable,
        "settle": maimaidx_break.break_db.settle_service_success,
    }

    @asynccontextmanager
    async def machine_session():
        yield

    async def fetch(_qrcode: str):
        raise RuntimeError("upstream failed")

    def settle(*_args, **_kwargs):
        nonlocal settled
        settled = True

    mai_account._binding_or_error = lambda _event: (
        str(payer), SimpleNamespace(qrcode="SGWCMAID-TEST"), None
    )
    mai_account.machine_session = machine_session
    async def service_cost(_service: str) -> int:
        return 1

    mai_account._service_cost = service_cost
    maimaidx_break.break_db.ensure_service_affordable = lambda *_args: None
    maimaidx_break.break_db.settle_service_success = settle
    try:
        try:
            await mai_account._run_paid_awmc_read(
                event,
                service="ticket_status",
                fetch=fetch,
                formatter=lambda _payload: "unused",
            )
        except RuntimeError as exc:
            assert str(exc) == "upstream failed"
        else:
            raise AssertionError("upstream failure should propagate")
    finally:
        mai_account._binding_or_error = original["binding"]
        mai_account.machine_session = original["machine_session"]
        mai_account._service_cost = original["service_cost"]
        maimaidx_break.break_db.ensure_service_affordable = original["ensure"]
        maimaidx_break.break_db.settle_service_success = original["settle"]

    assert not settled


def test_ticket_status_configuration_and_routes() -> None:
    account_source = (ROOT / "command" / "mai_account.py").read_text(encoding="utf-8")
    break_source = (ROOT / "libraries" / "maimaidx_break.py").read_text(encoding="utf-8")
    assert "'ticket_status_cost': '1'" in break_source
    assert 'break_db.get_config, "ticket_status_cost", "1"' in account_source
    assert '"ticket_status": "舞萌票券状态"' in account_source
    assert 'service="ticket_status"' in account_source
    pending_pos = account_source.index('if operation == "ticket_status":')
    pending_end = account_source.index('if operation == "region":', pending_pos)
    pending_branch = account_source[pending_pos:pending_end]
    assert "_run_paid_awmc_read(" in pending_branch
    assert "service=operation" in pending_branch
    assert "settle_service_success(" not in pending_branch


def test_ticket_status_freedom_wording() -> None:
    original = maimaidx_break.break_db.get_freedom_savings_total
    maimaidx_break.break_db.get_freedom_savings_total = lambda _qqid: 66
    try:
        text = mai_account._charge_text(
            SimpleNamespace(
                service="ticket_status",
                charged=0,
                listed_cost=1,
                free=False,
                freedom=True,
                freedom_remaining=90,
                balance=10,
            ),
            123456789,
        )
    finally:
        maimaidx_break.break_db.get_freedom_savings_total = original

    assert text == (
        "🛡️ 舞萌票券状态 FREEDOM 减免了 1 BREAK"
        "（剩余 1分30秒，一共省下了 66 BREAK）"
    )


asyncio.run(test_ticket_status_success_billing())
asyncio.run(test_ticket_status_failure_does_not_settle())
test_ticket_status_configuration_and_routes()
test_ticket_status_freedom_wording()
print("ticket status billing tests: ok")
