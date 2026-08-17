from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from statistics import median
from typing import Any

from ..maimaidx_best_50 import _music_is_new
from ..maimaidx_datasource import get_user_b50, get_user_records
from ..image import music_picture
from ..maimaidx_music import mai


def _cover_path(song_id: str) -> str:
    if not song_id:
        return ""
    try:
        return str(music_picture(song_id))
    except (TypeError, ValueError):
        return ""


def _chart(value: Any, *, pool: str = "") -> dict:
    song_id = str(getattr(value, "song_id", "") or "")
    music = mai.total_list.by_id(song_id)
    basic = getattr(music, "basic_info", None)
    return {
        "song_id": song_id,
        "title": str(getattr(value, "title", "") or ""),
        "type": str(getattr(value, "type", "SD") or "SD"),
        "level": str(getattr(value, "level", "") or ""),
        "level_index": int(getattr(value, "level_index", 0) or 0),
        "ds": float(getattr(value, "ds", 0) or 0),
        "achievement": float(getattr(value, "achievements", 0) or 0),
        "ra": int(getattr(value, "ra", 0) or 0),
        "fc": str(getattr(value, "fc", "") or ""),
        "fs": str(getattr(value, "fs", "") or ""),
        "artist": str(getattr(basic, "artist", "") or ""),
        "genre": str(getattr(basic, "genre", "") or ""),
        "version": str(getattr(basic, "version", "") or ""),
        "cover_path": _cover_path(song_id),
        "pool": pool,
    }

def _is_new(song_id: str) -> bool:
    music = mai.total_list.by_id(str(song_id))
    return bool(music and _music_is_new(music))


def _history_timestamp(value: Any) -> float | None:
    try:
        text = str(value or "").strip().replace("Z", "+00:00")
        if not text:
            return None
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _history_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except (TypeError, ValueError, OverflowError):
            return None


def _percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * max(0.0, min(1.0, p))
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    ratio = position - lower
    return ordered[lower] * (1.0 - ratio) + ordered[upper] * ratio


def _pairwise_slopes(points: list[dict[str, Any]]) -> list[float]:
    result: list[float] = []
    for left_index, left in enumerate(points):
        left_date = _history_date(left.get("date"))
        if left_date is None:
            continue
        for right in points[left_index + 1:]:
            right_date = _history_date(right.get("date"))
            if right_date is None:
                continue
            days = (right_date - left_date).days
            if days <= 0:
                continue
            result.append((int(right.get("rating") or 0) - int(left.get("rating") or 0)) / days)
    return result


def _quality_label(point_count: int, span_days: int, coverage: float, *, volatile: bool) -> tuple[str, str]:
    if point_count < 4 or span_days < 7:
        return "insufficient", "样本不足：至少需要 4 个历史点且跨度不少于 7 天"
    if not volatile and point_count >= 14 and span_days >= 21 and coverage >= 0.5:
        return "high", "高：样本、时间跨度与日期覆盖较充分"
    if not volatile and point_count >= 7 and span_days >= 14 and coverage >= 0.3:
        return "medium", "中：可判断近期方向，预测仍按保守系数折减"
    if volatile:
        return "low", "低：历史存在明显回落或异常跳变，仅作弱参考"
    return "low", "低：样本已达到最低门槛，仅作保守参考"


def _trend_status(
    *,
    delta: int,
    robust_slope: float,
    last_gain_days: int | None,
    reset_detected: bool,
    volatile: bool,
) -> tuple[str, str]:
    if reset_detected:
        return "reset", "检测到疑似版本切换或 Rating 重置"
    if volatile and delta < 0:
        return "volatile", "近期数据回落且波动较大"
    if delta <= 2 or (last_gain_days is not None and last_gain_days >= 7):
        return "plateau", "近期基本横盘，预测已按平台期收紧"
    if robust_slope >= 3.0:
        return "rising_fast", "近期推分较快"
    if robust_slope >= 0.75:
        return "rising", "近期稳步上升"
    if robust_slope > 0:
        return "steady", "近期缓慢上升"
    return "volatile", "近期方向不稳定"


def _forecast_7d(
    points: list[dict[str, Any]],
    *,
    as_of: date,
    quality: str,
    status: str,
    reset_detected: bool,
) -> dict[str, Any]:
    note = "仅基于近 30 天历史快照的保守区间，不是涨分承诺。"
    point_count = len(points)
    first_date = _history_date(points[0].get("date")) if points else None
    last_date = _history_date(points[-1].get("date")) if points else None
    span_days = (last_date - first_date).days if first_date is not None and last_date is not None else 0
    base = {
        "available": False,
        "forecast_days": 7,
        "horizon_days": 7,
        "date": (as_of + timedelta(days=7)).isoformat(),
        "quality": quality,
        "confidence": quality,
        "method": "robust_median_slope_with_recent_and_plateau_damping",
        "note": note,
    }
    if point_count < 4 or span_days < 7:
        return {**base, "reason": "insufficient_history"}
    if reset_detected:
        return {**base, "reason": "rating_reset_detected"}
    if int(points[-1].get("rating") or 0) < int(points[0].get("rating") or 0):
        return {**base, "reason": "negative_rating_trend"}

    slopes = _pairwise_slopes(points)
    recent_cutoff = (last_date or as_of) - timedelta(days=13)
    recent_points = [point for point in points if (_history_date(point.get("date")) or date.min) >= recent_cutoff]
    recent_slopes = _pairwise_slopes(recent_points)
    if not slopes or not recent_slopes:
        return {**base, "reason": "insufficient_slope_data"}

    overall_slope = median(slopes)
    recent_slope = median(recent_slopes)
    net_rate = max(
        0.0,
        (int(points[-1].get("rating") or 0) - int(points[0].get("rating") or 0)) / max(1, span_days),
    )
    conservative_slope = max(0.0, min(overall_slope, recent_slope, net_rate))
    if status == "plateau":
        conservative_slope = 0.0

    confidence_factor = {
        "high": 0.75,
        "medium": 0.60,
        "low": 0.45,
    }.get(quality, 0.0)
    effective_slope = conservative_slope * confidence_factor
    current_rating = int(points[-1].get("rating") or 0)
    gain_mid = max(0, round(effective_slope * 7))

    upper_cap = max(0.5, conservative_slope * 1.5, net_rate * 1.25)
    upper_slope = max(conservative_slope, min(max(0.0, _percentile(slopes, 0.75)), upper_cap))
    # 零游玩、零增长始终是未来七天的合理下界，不能让“保守区间”
    # 暗示用户必然涨分。
    gain_low = 0
    gain_high = max(gain_mid, int(upper_slope * confidence_factor * 7 + 0.9999))
    if conservative_slope <= 0:
        gain_low = gain_mid = gain_high = 0

    return {
        **base,
        "available": True,
        "reason": "ok",
        "rating_low": current_rating + gain_low,
        "rating_mid": current_rating + gain_mid,
        "rating_high": current_rating + gain_high,
        "gain_low": gain_low,
        "gain_mid": gain_mid,
        "gain_high": gain_high,
        "slope_per_day": round(conservative_slope, 3),
    }


def _build_rating_trend(
    history: list[dict[str, Any]],
    *,
    current_rating: int = 0,
    current_date: date | datetime | str | None = None,
    window_days: int = 30,
) -> dict[str, Any]:
    as_of = _history_date(current_date) or datetime.now().date()
    window_days = max(1, int(window_days))
    cutoff = as_of - timedelta(days=window_days - 1)
    by_date: dict[date, tuple[float, int]] = {}
    for item in history or []:
        if not isinstance(item, dict):
            continue
        point_date = _history_date(item.get("date") or item.get("stored_at"))
        try:
            rating = int(item.get("rating") or 0)
        except (TypeError, ValueError):
            continue
        if point_date is None or rating <= 0 or point_date < cutoff or point_date > as_of:
            continue
        timestamp = _history_timestamp(item.get("stored_at") or item.get("date"))
        order = timestamp if timestamp is not None else float("-inf")
        previous = by_date.get(point_date)
        if previous is None or order >= previous[0]:
            by_date[point_date] = (order, rating)
    if int(current_rating or 0) > 0:
        by_date[as_of] = (float("inf"), int(current_rating))

    points = [
        {"date": point_date.isoformat(), "rating": rating}
        for point_date, (_, rating) in sorted(by_date.items())
    ]
    base: dict[str, Any] = {
        "available": len(points) >= 2,
        "window_days": window_days,
        "as_of": as_of.isoformat(),
        "points": points,
        "point_count": len(points),
    }
    if len(points) < 2:
        base.update({
            "delta": 0,
            "span_days": 0,
            "coverage": round(float(len(points)), 3),
            "average_per_day": None,
            "quality": "insufficient",
            "quality_text": "样本不足：至少需要 2 个历史点才能判断趋势",
            "status": "insufficient",
            "status_text": "暂无可判断的 Rating 趋势",
            "forecast": _forecast_7d(
                points, as_of=as_of, quality="insufficient", status="insufficient", reset_detected=False,
            ),
        })
        return base

    point_dates = [_history_date(point.get("date")) for point in points]
    span_days = max(0, (point_dates[-1] - point_dates[0]).days) if point_dates[0] and point_dates[-1] else 0
    delta = int(points[-1]["rating"]) - int(points[0]["rating"])
    coverage = len(points) / max(1, span_days + 1)
    changes: list[tuple[int, int]] = []
    last_gain_date: date | None = None
    for index in range(1, len(points)):
        previous_date = point_dates[index - 1]
        point_date = point_dates[index]
        if previous_date is None or point_date is None:
            continue
        days = max(1, (point_date - previous_date).days)
        change = int(points[index]["rating"]) - int(points[index - 1]["rating"])
        changes.append((change, days))
        if change > 0:
            last_gain_date = point_date
    reset_detected = any(change <= -100 for change, _ in changes)
    daily_rates = [change / days for change, days in changes]
    volatile = reset_detected or sum(1 for change, _ in changes if change < 0) >= 2 or any(
        abs(rate) >= 80 for rate in daily_rates
    )
    slopes = _pairwise_slopes(points)
    robust_slope = median(slopes) if slopes else 0.0
    last_date = point_dates[-1]
    last_gain_days = (last_date - last_gain_date).days if last_date is not None and last_gain_date is not None else None
    quality, quality_text = _quality_label(len(points), span_days, coverage, volatile=volatile)
    if quality == "insufficient":
        status = "insufficient"
        status_text = "趋势样本不足，仅展示客观 Rating 变化"
    else:
        status, status_text = _trend_status(
            delta=delta,
            robust_slope=robust_slope,
            last_gain_days=last_gain_days,
            reset_detected=reset_detected,
            volatile=volatile,
        )
    base.update({
        "delta": delta,
        "span_days": span_days,
        "coverage": round(min(1.0, coverage), 3),
        "average_per_day": round(delta / span_days, 2) if span_days else None,
        "robust_slope_per_day": round(robust_slope, 3),
        "positive_steps": sum(1 for change, _ in changes if change > 0),
        "flat_steps": sum(1 for change, _ in changes if change == 0),
        "negative_steps": sum(1 for change, _ in changes if change < 0),
        "last_gain_days_ago": last_gain_days,
        "quality": quality,
        "quality_text": quality_text,
        "status": status,
        "status_text": status_text,
        "reset_detected": reset_detected,
        "forecast": _forecast_7d(
            points,
            as_of=as_of,
            quality=quality,
            status=status,
            reset_detected=reset_detected,
        ),
    })
    return base


def _rating_trend(qqid: int, *, current_rating: int = 0) -> dict[str, Any]:
    try:
        from ..maimaidx_data_storage import data_storage

        if not data_storage.is_enabled(int(qqid)):
            return {}
        history = data_storage.get_rating_history(int(qqid), days=120)
        return _build_rating_trend(history, current_rating=current_rating, window_days=30)
    except Exception:
        return {}


async def fetch_snapshot(qqid: int) -> dict:
    user = await get_user_b50(qqid=qqid)
    current_rating = int(getattr(user, "rating", 0) or 0)
    charts = getattr(user, "charts", None)
    b35 = [_chart(item, pool="old") for item in (getattr(charts, "sd", None) or [])]
    b15 = [_chart(item, pool="new") for item in (getattr(charts, "dx", None) or [])]
    all_charts = list(b35) + list(b15)
    try:
        _, records = await get_user_records(qqid=qqid)
    except Exception:
        records = []
    seen = {(x["song_id"], x["level_index"]) for x in all_charts}
    for record in records or []:
        item = _chart(record)
        key = (item["song_id"], item["level_index"])
        if key not in seen:
            item["pool"] = "new" if _is_new(item["song_id"]) else "old"
            all_charts.append(item)
            seen.add(key)
    trend = await asyncio.to_thread(_rating_trend, qqid, current_rating=current_rating)
    return {
        "nickname": str(getattr(user, "nickname", "Player") or "Player"),
        "rating": current_rating,
        "b35": b35,
        "b15": b15,
        "all_charts": all_charts,
        "trend": trend,
    }
