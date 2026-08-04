#!/usr/bin/env python3
"""Official QQ score reports must bill through the platform identity adapter."""

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
from nonebot_plugin_maimaidx.command import mai_score  # noqa: E402
from nonebot_plugin_maimaidx.libraries.maimaidx_break import break_db  # noqa: E402


async def main() -> None:
    event = SimpleNamespace(user_id="official-qq-openid-not-an-integer")
    payer = 987654321
    calls: list[tuple[object, object, int, dict]] = []
    billing_events: list[object] = []

    originals = {
        "resolve_score_qqid": mai_score.resolve_score_qqid,
        "storage_enabled": mai_score.data_storage.is_enabled,
        "billing_user_id": mai_score.billing_user_id,
        "generate_progress_report": mai_score.generate_progress_report,
        "generate_daily_report": mai_score.generate_daily_report,
        "finish_score": mai_score._finish_score,
        "get_config": break_db.get_config,
    }

    def fake_billing_user_id(received_event):
        billing_events.append(received_event)
        return payer

    async def fake_finish_score(matcher, result, qqid, **kwargs):
        calls.append((matcher, result, qqid, kwargs))

    mai_score.resolve_score_qqid = lambda _event: 123456789
    mai_score.data_storage.is_enabled = lambda _qqid: True
    mai_score.billing_user_id = fake_billing_user_id
    mai_score.generate_progress_report = lambda _qqid, days: ("progress", days)
    mai_score.generate_daily_report = lambda _qqid: ("daily", _qqid)
    mai_score._finish_score = fake_finish_score
    break_db.get_config = lambda _key, fallback: fallback

    try:
        await mai_score._weekly_report(event)
        await mai_score._monthly_report(event)
        await mai_score._annual_report(event)
        await mai_score._daily_report(event)
    finally:
        mai_score.resolve_score_qqid = originals["resolve_score_qqid"]
        mai_score.data_storage.is_enabled = originals["storage_enabled"]
        mai_score.billing_user_id = originals["billing_user_id"]
        mai_score.generate_progress_report = originals["generate_progress_report"]
        mai_score.generate_daily_report = originals["generate_daily_report"]
        mai_score._finish_score = originals["finish_score"]
        break_db.get_config = originals["get_config"]

    assert billing_events == [event] * 4
    expected = [
        (mai_score.weekly_report, ("progress", 7), "weekly_report", 1),
        (mai_score.monthly_report, ("progress", 30), "monthly_report", 2),
        (mai_score.annual_report, ("progress", 365), "annual_report", 3),
        (mai_score.daily_report, ("daily", 123456789), None, 0),
    ]
    assert len(calls) == len(expected)
    for (matcher, result, qqid, kwargs), expected_call in zip(calls, expected):
        expected_matcher, expected_result, service_name, service_cost = expected_call
        assert matcher is expected_matcher
        assert result == expected_result
        assert qqid == 123456789
        assert kwargs["billing_qqid"] == payer
        assert kwargs["billing_event"] is event
        assert kwargs["service_name"] == service_name
        assert kwargs["service_cost"] == service_cost


asyncio.run(main())
print("qq score report billing tests: ok")
