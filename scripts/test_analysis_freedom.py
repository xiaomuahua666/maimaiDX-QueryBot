#!/usr/bin/env python3
"""FREEDOM 生效时锐评不预扣并按实际价格记录减免。"""

from __future__ import annotations

import os
import sys
from pathlib import Path


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
from nonebot_plugin_maimaidx.libraries import maimaidx_break, maimaidx_card  # noqa: E402


def test_freedom_skips_analysis_precharge() -> None:
    payer = 123456789
    reserve_calls: list[tuple] = []
    originals = {
        "freedom_info": maimaidx_card.card_manager.freedom_info,
        "try_reserve": maimaidx_break.break_db.try_reserve_analysis,
        "precharge": maimaidx_break.analysis_precharge_cost,
    }
    maimaidx_card.card_manager.freedom_info = lambda _qqid: (True, 90, 0.0)
    maimaidx_break.break_db.try_reserve_analysis = (
        lambda *_args, **_kwargs: reserve_calls.append((_args, _kwargs)) or True
    )
    maimaidx_break.analysis_precharge_cost = lambda: 10
    try:
        reservation = maimaidx_break.reserve_analysis_charge(payer)
    finally:
        maimaidx_card.card_manager.freedom_info = originals["freedom_info"]
        maimaidx_break.break_db.try_reserve_analysis = originals["try_reserve"]
        maimaidx_break.analysis_precharge_cost = originals["precharge"]

    assert reservation.amount == 0
    assert reservation.freedom
    assert reservation.freedom_remaining == 90
    assert reserve_calls == []


def test_freedom_settlement_records_exemption() -> None:
    payer = 123456789
    reservation = maimaidx_break.AnalysisChargeReservation(
        0,
        freedom=True,
        freedom_remaining=90,
    )
    usage_calls: list[tuple] = []
    exemption_calls: list[tuple] = []
    originals = {
        "record_usage": maimaidx_break.break_db.record_usage,
        "record_exemption": maimaidx_break.break_db.record_freedom_exemption,
        "settle_reservation": maimaidx_break.break_db.settle_analysis_reservation,
        "add_balance": maimaidx_break.break_db.add_balance,
    }
    maimaidx_break.break_db.record_usage = (
        lambda *args, **kwargs: usage_calls.append((args, kwargs))
    )
    maimaidx_break.break_db.record_freedom_exemption = (
        lambda *args, **kwargs: exemption_calls.append((args, kwargs)) or 42
    )
    maimaidx_break.break_db.settle_analysis_reservation = (
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("FREEDOM must not settle a reservation")
        )
    )
    maimaidx_break.break_db.add_balance = (
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("FREEDOM must not deduct the final cost")
        )
    )
    try:
        charged = maimaidx_break.settle_analysis_charge(
            payer,
            8,
            reserved=reservation,
            token_usage={"input_tokens": 16000, "output_tokens": 4000},
        )
    finally:
        maimaidx_break.break_db.record_usage = originals["record_usage"]
        maimaidx_break.break_db.record_freedom_exemption = originals[
            "record_exemption"
        ]
        maimaidx_break.break_db.settle_analysis_reservation = originals[
            "settle_reservation"
        ]
        maimaidx_break.break_db.add_balance = originals["add_balance"]

    assert charged == 0
    assert usage_calls == [((payer, "analysis"), {"break_delta": 0})]
    assert len(exemption_calls) == 1
    args, kwargs = exemption_calls[0]
    assert args == (payer, "b50_analysis", 8)
    assert kwargs["meta"]["input_tokens"] == 16000
    assert kwargs["meta"]["output_tokens"] == 4000


def test_command_uses_freedom_footer() -> None:
    source = (ROOT / "command" / "mai_b50_analysis.py").read_text(encoding="utf-8")
    assert "if reserved.freedom:" in source
    assert "FREEDOM 生效，本次未预扣" in source
    assert "format_freedom_exemption(" in source
    assert "reserved.freedom_remaining" in source


def test_existing_reservation_refunds_even_if_billing_changes() -> None:
    """预扣是既成事实，处理中关闭计费也不能吞掉退款。"""
    payer = 123456789
    reservation = maimaidx_break.AnalysisChargeReservation(10)
    refund_calls: list[tuple] = []
    originals = {
        "billing_enabled": maimaidx_break.break_db.billing_enabled,
        "refund": maimaidx_break.break_db.refund_analysis_reservation,
        "balance": maimaidx_break.break_db.get_balance,
    }
    maimaidx_break.break_db.billing_enabled = lambda: False
    maimaidx_break.break_db.refund_analysis_reservation = (
        lambda *args, **kwargs: refund_calls.append((args, kwargs)) or 37
    )
    maimaidx_break.break_db.get_balance = lambda _qqid: 27
    try:
        balance = maimaidx_break.refund_analysis_charge(
            payer, reservation, reason="delivery failed",
        )
    finally:
        maimaidx_break.break_db.billing_enabled = originals["billing_enabled"]
        maimaidx_break.break_db.refund_analysis_reservation = originals["refund"]
        maimaidx_break.break_db.get_balance = originals["balance"]

    assert balance == 37
    assert refund_calls[0][0] == (payer, 10)
    assert refund_calls[0][1]["meta"]["reason"] == "delivery failed"


test_freedom_skips_analysis_precharge()
test_freedom_settlement_records_exemption()
test_command_uses_freedom_footer()
test_existing_reservation_refunds_even_if_billing_changes()
print("analysis freedom tests: ok")
