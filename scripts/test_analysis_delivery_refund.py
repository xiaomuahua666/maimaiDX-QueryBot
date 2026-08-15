#!/usr/bin/env python3
"""锐评成品未送达时必须退款，送达后才允许进入结算。"""

from __future__ import annotations

import asyncio
import io
import os
import sys
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
from nonebot_plugin_maimaidx.command import mai_b50_analysis  # noqa: E402
from nonebot_plugin_maimaidx.libraries.maimaidx_break import (  # noqa: E402
    AnalysisChargeReservation,
)


async def run() -> None:
    event = SimpleNamespace(user_id=123456789, get_user_id=lambda: "123456789")
    matcher = SimpleNamespace()
    reservation = AnalysisChargeReservation(10)
    refunds: list[tuple] = []
    notices: list[str] = []
    originals = {
        "send": mai_b50_analysis.plugin_send,
        "finish": mai_b50_analysis.plugin_finish,
        "refund": mai_b50_analysis.refund_analysis_charge,
        "send_timeout": mai_b50_analysis.maiconfig.b50_send_timeout_seconds,
    }

    async def failed_send(*_args, **_kwargs):
        raise RuntimeError("upload rejected")

    async def finish_notice(_matcher, message, **_kwargs):
        notices.append(str(message))

    mai_b50_analysis.plugin_send = failed_send
    mai_b50_analysis.plugin_finish = finish_notice
    mai_b50_analysis.refund_analysis_charge = (
        lambda *args, **kwargs: refunds.append((args, kwargs)) or 10
    )
    try:
        delivered = await mai_b50_analysis._deliver_result_or_refund(
            matcher, event, io.BytesIO(b"png"), 123456789, reservation,
        )
    finally:
        mai_b50_analysis.plugin_send = originals["send"]
        mai_b50_analysis.plugin_finish = originals["finish"]
        mai_b50_analysis.refund_analysis_charge = originals["refund"]

    assert delivered is False
    assert len(refunds) == 1
    assert refunds[0][0][:2] == (123456789, reservation)
    assert refunds[0][1]["reason"] == "发送结果:RuntimeError"
    assert notices and "预扣已全额退回" in notices[0]

    sends: list[dict] = []
    refunds.clear()

    async def successful_send(*_args, **kwargs):
        sends.append(kwargs)

    mai_b50_analysis.plugin_send = successful_send
    mai_b50_analysis.refund_analysis_charge = (
        lambda *args, **kwargs: refunds.append((args, kwargs)) or 10
    )
    try:
        delivered = await mai_b50_analysis._deliver_result_or_refund(
            matcher, event, io.BytesIO(b"png"), 123456789, reservation,
        )
    finally:
        mai_b50_analysis.plugin_send = originals["send"]
        mai_b50_analysis.refund_analysis_charge = originals["refund"]

    assert delivered is True
    assert refunds == []
    assert sends == [{
        "event": event,
        "mention_sender": False,
        "publish_qq_image": True,
    }]

    notices.clear()
    refunds.clear()

    async def hung_send(*_args, **_kwargs):
        await asyncio.sleep(2)

    mai_b50_analysis.plugin_send = hung_send
    mai_b50_analysis.plugin_finish = finish_notice
    mai_b50_analysis.refund_analysis_charge = (
        lambda *args, **kwargs: refunds.append((args, kwargs)) or 10
    )
    mai_b50_analysis.maiconfig.b50_send_timeout_seconds = 0.5
    try:
        delivered = await mai_b50_analysis._deliver_result_or_refund(
            matcher, event, io.BytesIO(b"png"), 123456789, reservation,
        )
    finally:
        mai_b50_analysis.plugin_send = originals["send"]
        mai_b50_analysis.plugin_finish = originals["finish"]
        mai_b50_analysis.refund_analysis_charge = originals["refund"]
        mai_b50_analysis.maiconfig.b50_send_timeout_seconds = originals["send_timeout"]

    assert delivered is False
    assert len(refunds) == 1
    assert refunds[0][1]["reason"] == "发送结果:AnalysisStageTimeoutError"
    assert notices and "图片发送超时" in notices[0]
    assert "预扣已全额退回" in notices[0]


asyncio.run(run())
print("analysis delivery refund tests: ok")
