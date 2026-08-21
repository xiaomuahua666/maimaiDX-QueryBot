from __future__ import annotations

from dataclasses import dataclass

from ..maimaidx_break import analysis_daily_free_enabled, break_db, is_superuser_exempt


@dataclass(frozen=True)
class Quote:
    listed_cost: int
    daily_free: bool
    billing_disabled: bool


def prepare_quote(qqid: int, cost: int) -> Quote:
    cost = max(0, int(cost))
    disabled = not break_db.billing_enabled() or is_superuser_exempt(qqid)
    daily_free = bool(
        analysis_daily_free_enabled()
        and break_db.service_is_free(qqid, "analysis")
    )
    if not disabled:
        break_db.ensure_service_affordable(qqid, "analysis", cost)
    return Quote(cost, daily_free, disabled)


def commit_quote(qqid: int, quote: Quote) -> dict:
    if quote.billing_disabled:
        return {"charged": 0, "balance": break_db.get_balance(qqid), "free": False}
    result = break_db.settle_service_success(
        qqid,
        "analysis",
        quote.listed_cost,
        meta={"kind": "roast_v2", "pricing": "fixed_quote"},
    )
    return {
        "charged": int(getattr(result, "charged", 0) or 0),
        "balance": int(getattr(result, "balance", break_db.get_balance(qqid)) or 0),
        "free": bool(getattr(result, "free", False)),
        "freedom": bool(getattr(result, "freedom", False)),
        "free_window": bool(getattr(result, "free_window", False)),
    }
