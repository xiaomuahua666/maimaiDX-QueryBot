from __future__ import annotations

from statistics import mean, median

from .domain import Candidate, Evidence, EvidencePack, RoastReport, StyleSpec


def _f(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _i(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


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
    return ordered[lower] * (1 - ratio) + ordered[upper] * ratio


def _rating_ds_cap(rating: int) -> float:
    if rating < 12000:
        return 13.4
    if rating < 13000:
        return 13.8
    if rating < 14000:
        return 14.3
    if rating < 14500:
        return 14.6
    if rating < 15000:
        return 14.9
    return 15.0


def _recommendation_profile(rows: list[dict], rating: int) -> tuple[float, float, float]:
    proven = [
        _f(item.get("ds")) for item in rows
        if _f(item.get("ds")) > 0 and _f(item.get("achievement")) >= 97
    ]
    if not proven:
        proven = [_f(item.get("ds")) for item in rows if _f(item.get("ds")) > 0]
    center = _percentile(proven, 0.5)
    upper = _percentile(proven, 0.85)
    data_cap = upper + (0.35 if rating >= 14000 else 0.25)
    cap = min(_rating_ds_cap(rating), data_cap) if data_cap > 0 else _rating_ds_cap(rating)
    return center, upper, max(1.0, cap)


def _calc_ra(ds: float, achievement: float) -> int:
    if achievement < 50:
        base = 7.0
    elif achievement < 60:
        base = 8.0
    elif achievement < 70:
        base = 9.6
    elif achievement < 75:
        base = 11.2
    elif achievement < 80:
        base = 12.0
    elif achievement < 90:
        base = 13.6
    elif achievement < 94:
        base = 15.2
    elif achievement < 97:
        base = 16.8
    elif achievement < 98:
        base = 20.0
    elif achievement < 99:
        base = 20.3
    elif achievement < 99.5:
        base = 20.8
    elif achievement < 100:
        base = 21.1
    elif achievement < 100.5:
        base = 21.6
    else:
        base = 22.4
    return int(ds * min(100.5, achievement) / 100 * base)


def build_evidence_pack(snapshot: dict) -> EvidencePack:
    b35 = list(snapshot.get("b35") or [])[:35]
    b15 = list(snapshot.get("b15") or [])[:15]
    all_charts = list(snapshot.get("all_charts") or [])
    rows = b35 + b15
    achievements = [_f(x.get("achievement")) for x in rows if _f(x.get("achievement")) > 0]
    b35_avg = mean([_f(x.get("achievement")) for x in b35]) if b35 else 0
    b15_avg = mean([_f(x.get("achievement")) for x in b15]) if b15 else 0
    rating = _i(snapshot.get("rating"))
    ds_center, ds_upper, recommended_ds_cap = _recommendation_profile(rows, rating)
    high = [
        x for x in rows
        if _f(x.get("ds")) >= 14.6
        or (str(x.get("level") or "").endswith("+") and _f(x.get("ds")) >= 14)
    ]
    high_avg = mean([_f(x.get("achievement")) for x in high]) if high else 0
    floor = min([_i(x.get("ra")) for x in b35], default=0)
    new_floor = min([_i(x.get("ra")) for x in b15], default=0)
    ceiling = max([_i(x.get("ra")) for x in rows], default=0)
    evidence = [
        Evidence("rating", "当前 Rating", str(_i(snapshot.get("rating"))), "snapshot"),
        Evidence("b35_avg", "B35 平均达成率", f"{b35_avg:.4f}%", "b35"),
        Evidence("b15_avg", "B15 平均达成率", f"{b15_avg:.4f}%", "b15"),
        Evidence(
            "high_avg",
            "14+ 平均达成率",
            f"{high_avg:.4f}%" if high else "暂无 14+ 样本",
            "high_ds",
            confidence="high" if high else "unavailable",
        ),
        Evidence("b35_floor", "B35 最低 RA", str(floor), "b35"),
        Evidence("ceiling", "最高单曲 RA", str(ceiling), "b50"),
        Evidence("recommendation_ds_cap", "推荐定数上限", f"{recommended_ds_cap:.1f}", "capability_profile"),
    ]
    candidates: list[Candidate] = []
    b35_keys = {(str(x.get("song_id") or ""), _i(x.get("level_index"))) for x in b35}
    b15_keys = {(str(x.get("song_id") or ""), _i(x.get("level_index"))) for x in b15}
    for song in all_charts:
        ach = _f(song.get("achievement"))
        ds = _f(song.get("ds"))
        min_achievement = 98.0 if rating < 14000 else 97.0
        if (
            not song.get("title")
            or ds <= 0
            or ds > recommended_ds_cap + 1e-6
            or ach < min_achievement
            or ach >= 100.5
        ):
            continue
        song_id = str(song.get("song_id") or "")
        level_index = _i(song.get("level_index"))
        key = (song_id, level_index)
        pool = str(song.get("pool") or ("new" if key in b15_keys else "old"))
        current_ra = _i(song.get("ra"))
        if key in b35_keys or key in b15_keys:
            baseline = current_ra
        elif pool == "new":
            baseline = new_floor if len(b15) >= 15 else 0
        else:
            baseline = floor if len(b35) >= 35 else 0
        target_achievement = 100.0 if ach < 100 else 100.5
        target_ra = _calc_ra(ds, target_achievement)
        gain = max(0, target_ra - baseline)
        if ach < 100 and gain < 2:
            target_achievement = 100.5
            target_ra = _calc_ra(ds, target_achievement)
            gain = max(0, target_ra - baseline)
        if gain < 2:
            continue
        achievement_gap = max(0.0, target_achievement - ach)
        ds_stretch = max(0.0, ds - ds_upper)
        priority_score = gain * 4.0 - achievement_gap * 10.0 - ds_stretch * 22.0 - abs(ds - ds_center) * 2.0
        candidates.append(Candidate(
            song_id=song_id, title=str(song.get("title") or ""),
            level=str(song.get("level") or ""), ds=ds, achievement=ach,
            estimated_gain=gain, target="SSS" if target_achievement == 100 else "SSS+",
            reason=f"当前 {ach:.4f}%，距离 {target_achievement:.1f}% 目标 {achievement_gap:.4f}%",
            cover_path=str(song.get("cover_path") or ""),
            artist=str(song.get("artist") or ""),
            genre=str(song.get("genre") or ""),
            level_index=level_index,
            chart_type=str(song.get("type") or "SD"),
            pool=pool,
            target_achievement=target_achievement,
            current_ra=current_ra,
            target_ra=target_ra,
            priority_score=round(priority_score, 3),
        ))
    candidates.sort(key=lambda x: (-x.priority_score, x.target_achievement - x.achievement, -x.estimated_gain, x.ds))
    metrics = {
        "achievement_median": median(achievements) if achievements else 0,
        "b35_avg": b35_avg,
        "b15_avg": b15_avg,
        "high_avg": high_avg if high else None,
        "high_count": len(high),
        "b35_floor": floor,
        "b15_floor": new_floor,
        "ceiling": ceiling,
        "chart_count": len(rows),
        "recommendation_ds_center": ds_center,
        "recommendation_ds_cap": recommended_ds_cap,
    }
    return EvidencePack(
        nickname=str(snapshot.get("nickname") or "Player"),
        rating=rating, b35=b35, b15=b15,
        all_charts=all_charts, evidence=evidence,
        candidates=candidates[:8], metrics=metrics,
    )

def build_report_fallback(pack: EvidencePack, style: StyleSpec) -> RoastReport:
    m = pack.metrics
    address = f"{style.address}，" if style.address else ""
    suffix = f" {style.suffix}" if style.suffix else ""
    if m["b35_avg"] >= m["b15_avg"] + 0.35:
        headline = f"{address}基本盘比上限更稳，当前最值得补的是新曲适应{suffix}"
        weakness = "B15 平均达成率明显落后于 B35"
    elif m["high_avg"] and m["high_avg"] < m["b35_avg"] - 0.4:
        headline = f"{address}中低难度很稳，但 14+ 还没有完全接住{suffix}"
        weakness = "高定数成绩出现明显断层"
    else:
        headline = f"{address}成绩结构比较均衡，接下来适合做稳定增分{suffix}"
        weakness = "还可以继续压低 B35 底部成绩"
    strengths = [f"B35 平均达成率 {m['b35_avg']:.4f}%", f"最高单曲 RA {m['ceiling']}"]
    actions = ["先处理最接近目标线的曲目", "优先选择与当前定数分布接近的谱面", "每次训练记录目标达成率变化"]
    recommendations = [c.__dict__ for c in pack.candidates[:3]]
    return RoastReport(
        headline=headline, summary=headline, strengths=strengths,
        weaknesses=[weakness], actions=actions,
        recommendations=recommendations,
        claims=[{"text": weakness, "evidence_ids": ["b35_avg", "b15_avg", "high_avg"]}],
        style=style,
    )
