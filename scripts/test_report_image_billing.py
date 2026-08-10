#!/usr/bin/env python3
"""Reports must charge image generation and show its FREEDOM exemption."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path = [
    item
    for item in sys.path
    if item and Path(item).resolve() != ROOT
]
sys.path.insert(0, str(ROOT.parent))

os.environ.setdefault("COMMAND_START", '["", "/"]')
os.environ.setdefault("MAIMAIDXPATH", str(ROOT / "static"))
os.environ.setdefault("MAIMAIDX_STORAGE_FAIL_FAST", "false")
os.environ.setdefault("MAIMAIDX_MESSAGE_STATS_ENABLED", "false")
os.environ.setdefault("MAIMAIDX_BUSY_SURCHARGE_ENABLED", "false")

import nonebot

nonebot.init()

import nonebot_plugin_maimaidx  # noqa: F401, E402
from nonebot.adapters.onebot.v11 import MessageSegment  # noqa: E402
from nonebot_plugin_maimaidx.command import mai_account, mai_score  # noqa: E402
from nonebot_plugin_maimaidx.libraries import (  # noqa: E402
    maimaidx_break,
    maimaidx_card,
    maimaidx_timing,
)


async def test_report_always_charges_image() -> None:
    payer = 123456789
    event = SimpleNamespace(user_id=payer)
    run_kwargs: list[dict] = []
    settled: list[tuple[int, str, int]] = []
    charge_footers: list[list[str]] = []
    sent: list[tuple[object, dict]] = []
    freedom_active = [False]

    originals = {
        "run_timed": maimaidx_timing.run_timed,
        "ensure_service": maimaidx_break.break_db.ensure_service_affordable,
        "service_is_free": maimaidx_break.break_db.service_is_free,
        "get_balance": maimaidx_break.break_db.get_balance,
        "settle_service": maimaidx_break.break_db.settle_service_success,
        "replace_footer": maimaidx_break.replace_break_charge_footer,
        "freedom_savings": maimaidx_break.break_db.get_freedom_savings_total,
        "image_cost": maimaidx_break.image_render_cost,
        "freedom_active": maimaidx_card.card_manager.freedom_active,
        "build_footer": mai_score._build_footer,
        "plugin_finish": mai_score.plugin_finish,
    }

    async def run_timed(_coro, **kwargs):
        run_kwargs.append(kwargs)
        return MessageSegment.image("base64://aW1hZ2U="), 0.1

    async def finish(_matcher, message, **kwargs):
        sent.append((message, kwargs))

    maimaidx_timing.run_timed = run_timed
    maimaidx_break.break_db.ensure_service_affordable = lambda *_args: None
    maimaidx_break.break_db.service_is_free = lambda *_args: False
    maimaidx_break.break_db.get_balance = lambda _qqid: 100
    def settle_service(qqid, service, cost):
        settled.append((qqid, service, cost))
        return SimpleNamespace(
            charged=0 if freedom_active[0] else cost,
            balance=97,
            freedom=freedom_active[0],
            freedom_remaining=365 * 24 * 60 * 60 - 24,
        )

    maimaidx_break.break_db.settle_service_success = settle_service
    maimaidx_break.replace_break_charge_footer = lambda lines: (
        charge_footers.append(lines)
    )
    maimaidx_break.break_db.get_freedom_savings_total = lambda _qqid: 42
    maimaidx_break.image_render_cost = lambda: 1
    maimaidx_card.card_manager.freedom_active = lambda _qqid: freedom_active[0]
    mai_score._build_footer = lambda *_args, **_kwargs: "footer"
    mai_score.plugin_finish = finish
    try:
        await mai_score._finish_score(
            object(),
            object(),
            payer,
            billing_qqid=payer,
            billing_event=event,
            service_name="monthly_report",
            service_cost=2,
        )
        await mai_score._finish_score(
            object(),
            object(),
            payer,
            billing_qqid=payer,
            billing_event=event,
            service_name=None,
            service_cost=0,
        )
        freedom_active[0] = True
        await mai_score._finish_score(
            object(),
            object(),
            payer,
            billing_qqid=payer,
            billing_event=event,
            service_name="weekly_report",
            service_cost=1,
        )
    finally:
        maimaidx_timing.run_timed = originals["run_timed"]
        maimaidx_break.break_db.ensure_service_affordable = originals["ensure_service"]
        maimaidx_break.break_db.service_is_free = originals["service_is_free"]
        maimaidx_break.break_db.get_balance = originals["get_balance"]
        maimaidx_break.break_db.settle_service_success = originals["settle_service"]
        maimaidx_break.replace_break_charge_footer = originals["replace_footer"]
        maimaidx_break.break_db.get_freedom_savings_total = originals["freedom_savings"]
        maimaidx_break.image_render_cost = originals["image_cost"]
        maimaidx_card.card_manager.freedom_active = originals["freedom_active"]
        mai_score._build_footer = originals["build_footer"]
        mai_score.plugin_finish = originals["plugin_finish"]

    assert run_kwargs == [
        {"billing_qqid": payer, "render_charge": True},
        {"billing_qqid": payer, "render_charge": True},
        {"billing_qqid": payer, "render_charge": True},
    ]
    assert settled == [
        (payer, "monthly_report", 2),
        (payer, "weekly_report", 1),
    ]
    assert charge_footers == [
        ["💳 消耗 3 BREAK · 余额 97 BREAK"],
        [
            "🛡️ 周报（含生成图片） FREEDOM 减免了 2 BREAK"
            "（剩余 364天23小时59分36秒，一共省下了 42 BREAK）"
        ],
    ]
    assert len(sent) == 3


def test_freedom_image_wording() -> None:
    originals = {
        "image_cost": maimaidx_break.image_render_cost,
        "freedom_info": maimaidx_card.card_manager.freedom_info,
        "record_usage": maimaidx_break.break_db.record_usage,
        "record_exemption": maimaidx_break.break_db.record_freedom_exemption,
    }
    maimaidx_break.image_render_cost = lambda: 1
    maimaidx_card.card_manager.freedom_info = lambda _qqid: (
        True,
        365 * 24 * 60 * 60 - 24,
        0.0,
    )
    maimaidx_break.break_db.record_usage = lambda *_args, **_kwargs: None
    maimaidx_break.break_db.record_freedom_exemption = (
        lambda *_args, **_kwargs: 42
    )
    try:
        line = maimaidx_break.settle_image_render(123456789)
    finally:
        maimaidx_break.image_render_cost = originals["image_cost"]
        maimaidx_card.card_manager.freedom_info = originals["freedom_info"]
        maimaidx_break.break_db.record_usage = originals["record_usage"]
        maimaidx_break.break_db.record_freedom_exemption = originals["record_exemption"]

    assert line == (
        "🛡️ 生成图片 FREEDOM 减免了 1 BREAK"
        "（剩余 364天23小时59分36秒，一共省下了 42 BREAK）"
    )
    assert "渲染费" not in line


def test_service_freedom_wording() -> None:
    original = maimaidx_break.break_db.get_freedom_savings_total
    maimaidx_break.break_db.get_freedom_savings_total = lambda _qqid: 88
    try:
        line = mai_account._charge_text(
            SimpleNamespace(
                service="awmc_status",
                charged=0,
                listed_cost=2,
                free=False,
                freedom=True,
                freedom_remaining=90,
                balance=100,
            ),
            123456789,
        )
    finally:
        maimaidx_break.break_db.get_freedom_savings_total = original

    assert line == (
        "🛡️ 账号状态查询 FREEDOM 减免了 2 BREAK"
        "（剩余 1分30秒，一共省下了 88 BREAK）"
    )


asyncio.run(test_report_always_charges_image())
test_freedom_image_wording()
test_service_freedom_wording()
print("report image billing tests: ok")
