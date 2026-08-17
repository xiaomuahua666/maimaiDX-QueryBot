"""同段 B50 聚合数据的独立适配层。

这里只读取已经脱敏的 peer_stats 聚合文件，不调用旧锐评的上下文构建器。
同段均值的口径是“同一 Rating 分段中、该谱面进入 B50 的玩家均值”。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _bucket_key(rating: int, bucket_size: int) -> str:
    size = max(1, int(bucket_size or 200))
    lower = (max(0, int(rating)) // size) * size
    return f"{lower}-{lower + size - 1}"


def _stat_key(row: dict[str, Any], *, typed: bool) -> str:
    song_id = str(row.get("song_id") or row.get("music_id") or "")
    level_index = _i(row.get("level_index"), -1)
    if typed:
        chart_type = str(row.get("type") or row.get("chart_type") or "SD").upper()
        return f"{song_id}:{chart_type}:{level_index}"
    return f"{song_id}:{level_index}"


def _age_days(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            stamp = float(value)
        else:
            text = str(value).strip().replace("Z", "+00:00")
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            stamp = parsed.timestamp()
        return max(0.0, (datetime.now(timezone.utc).timestamp() - stamp) / 86400)
    except (TypeError, ValueError, OverflowError):
        return None


def _confidence(player_count: int, matched: int, coverage: float, *, stale: bool) -> tuple[str, str]:
    if player_count >= 50 and matched >= 35 and coverage >= 0.7:
        level, text = "high", "高置信：样本和谱面覆盖充足"
    elif player_count >= 20 and matched >= 15 and coverage >= 0.3:
        level, text = "medium", "中置信：可辅助判断，细小差距不作定论"
    elif player_count > 0 and matched > 0:
        level, text = "low", "低置信：样本或覆盖不足，仅作弱参考"
    else:
        level, text = "unavailable", "不可用：没有足够同段聚合数据"
    if stale and level == "high":
        level, text = "medium", "中置信：同段数据较旧，建议只作方向参考"
    return level, text


def attach_peer_profile(rows: list[dict[str, Any]], rating: int, peer_stats: dict | None) -> dict[str, Any]:
    """为成绩行附加逐谱同段字段，并返回可直接放进 Prompt 的摘要。"""
    for row in rows:
        for key in ("peer_avg", "peer_gap", "peer_sample_count", "peer_appear_rate"):
            row.pop(key, None)
    empty = {
        "available": False,
        "bucket": "",
        "player_count": 0,
        "matched": 0,
        "coverage": 0.0,
        "arpi": None,
        "avg_peer": None,
        "avg_gap": None,
        "appear_rate": None,
        "confidence": "unavailable",
        "confidence_text": "不可用：没有足够同段聚合数据",
        "position": "未知",
        "position_detail": "同段样本不足，跳过同段结论",
        "generated_at": "",
        "age_days": None,
        "stale": False,
        "schema": "none",
    }
    if not isinstance(peer_stats, dict):
        return empty

    bucket_size = _i(peer_stats.get("rating_bucket_size"), 200)
    bucket = _bucket_key(rating, bucket_size)
    raw_bucket = (peer_stats.get("buckets") or {}).get(bucket) or {}
    chart_stats = raw_bucket.get("charts") or {}
    if not isinstance(chart_stats, dict):
        return {**empty, "bucket": bucket}

    matched_gaps: list[float] = []
    matched_peers: list[float] = []
    appearance: list[float] = []
    legacy_matches = 0
    typed_matches = 0
    for row in rows:
        stat = chart_stats.get(_stat_key(row, typed=True))
        schema = "typed"
        if not isinstance(stat, dict):
            stat = chart_stats.get(_stat_key(row, typed=False))
            schema = "legacy"
        if not isinstance(stat, dict):
            continue
        peer_avg = stat.get("avg_achievement")
        if peer_avg is None:
            continue
        peer_avg_f = _f(peer_avg)
        achievement = _f(row.get("achievement"))
        gap = achievement - peer_avg_f
        appear = _f(stat.get("b50_appear_rate"), 0.0)
        if 0 < appear <= 1:
            appear *= 100
        row["peer_avg"] = round(peer_avg_f, 4)
        row["peer_gap"] = round(gap, 4)
        row["peer_sample_count"] = _i(stat.get("sample_count"))
        row["peer_appear_rate"] = round(appear, 2) if appear else None
        matched_gaps.append(gap)
        matched_peers.append(peer_avg_f)
        if appear:
            appearance.append(appear)
        if schema == "typed":
            typed_matches += 1
        else:
            legacy_matches += 1

    total = len(rows)
    matched = len(matched_gaps)
    coverage = matched / total if total else 0.0
    player_count = _i(raw_bucket.get("player_count"))
    confidence, confidence_text = _confidence(player_count, matched, coverage, stale=False)
    generated_at = str(peer_stats.get("generated_at") or "")
    age_days = _age_days(peer_stats.get("generated_at"))
    stale = age_days is not None and age_days > 45
    confidence, confidence_text = _confidence(player_count, matched, coverage, stale=stale)

    arpi = sum(matched_gaps) / matched if matched else None
    avg_peer = sum(matched_peers) / matched if matched else None
    avg_appear = sum(appearance) / len(appearance) if appearance else None
    distribution = raw_bucket.get("arpi_distribution") or {}
    p25 = _f(distribution.get("p25"), 0.0)
    median = _f(distribution.get("median"), 0.0)
    p75 = _f(distribution.get("p75"), 0.0)
    if matched and distribution.get("median") is not None:
        if arpi >= p75:
            position, detail = "上四分位", f"ARPI {arpi:+.4f}，高于同段大多数玩家"
        elif arpi <= p25:
            position, detail = "下四分位", f"ARPI {arpi:+.4f}，低于同段大多数玩家"
        else:
            position, detail = "中位区间", f"ARPI {arpi:+.4f}，接近同段常态"
    else:
        position, detail = "未知", "同段 ARPI 样本不足，跳过分位结论"

    schema = "typed" if typed_matches and not legacy_matches else "legacy" if legacy_matches else "none"
    return {
        "available": matched > 0,
        "bucket": bucket,
        "player_count": player_count,
        "matched": matched,
        "coverage": round(coverage, 4),
        "arpi": round(arpi, 4) if arpi is not None else None,
        "avg_peer": round(avg_peer, 4) if avg_peer is not None else None,
        "avg_gap": round(arpi, 4) if arpi is not None else None,
        "appear_rate": round(avg_appear, 2) if avg_appear is not None else None,
        "confidence": confidence,
        "confidence_text": confidence_text,
        "position": position,
        "position_detail": detail,
        "p25": round(p25, 4) if distribution.get("p25") is not None else None,
        "median": round(median, 4) if distribution.get("median") is not None else None,
        "p75": round(p75, 4) if distribution.get("p75") is not None else None,
        "generated_at": generated_at,
        "age_days": round(age_days, 1) if age_days is not None else None,
        "stale": stale,
        "schema": schema,
    }


__all__ = ["attach_peer_profile"]
