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


def _calc_ra(ds: float, achievement: float) -> int:
    thresholds = ((50, 7), (60, 8), (70, 9.6), (75, 11.2), (80, 12), (90, 13.6),
                  (94, 15.2), (97, 16.8), (98, 20), (99, 20.3), (99.5, 20.8),
                  (100, 21.1), (100.5, 21.6), (101, 22.4))
    base = 7.0
    for threshold, value in thresholds:
        if achievement >= threshold:
            base = value
    return int(ds * min(100.5, achievement) / 100 * base)


def build_evidence_pack(snapshot: dict) -> EvidencePack:
    b35 = list(snapshot.get("b35") or [])[:35]
    b15 = list(snapshot.get("b15") or [])[:15]
    all_charts = list(snapshot.get("all_charts") or [])
    rows = b35 + b15
    achievements = [_f(x.get("achievement")) for x in rows if _f(x.get("achievement")) > 0]
    b35_avg = mean([_f(x.get("achievement")) for x in b35]) if b35 else 0
    b15_avg = mean([_f(x.get("achievement")) for x in b15]) if b15 else 0
    high = [x for x in rows if _f(x.get("ds")) >= 14]
    high_avg = mean([_f(x.get("achievement")) for x in high]) if high else 0
    floor = min([_i(x.get("ra")) for x in b35], default=0)
    ceiling = max([_i(x.get("ra")) for x in rows], default=0)
    evidence = [
        Evidence("rating", "当前 Rating", str(_i(snapshot.get("rating"))), "snapshot"),
        Evidence("b35_avg", "B35 平均达成率", f"{b35_avg:.4f}%", "b35"),
        Evidence("b15_avg", "B15 平均达成率", f"{b15_avg:.4f}%", "b15"),
        Evidence("high_avg", "14+ 平均达成率", f"{high_avg:.4f}%", "high_ds"),
        Evidence("b35_floor", "B35 最低 RA", str(floor), "b35"),
        Evidence("ceiling", "最高单曲 RA", str(ceiling), "b50"),
    ]
    candidates: list[Candidate] = []
    baseline = floor
    for song in all_charts:
        ach = _f(song.get("achievement"))
        ds = _f(song.get("ds"))
        if not song.get("title") or ds <= 0 or ach >= 100.5:
            continue
        gain = max(0, _calc_ra(ds, 100.0) - _i(song.get("ra"), baseline))
        if gain < 2:
            continue
        candidates.append(Candidate(
            song_id=str(song.get("song_id") or ""), title=str(song.get("title") or ""),
            level=str(song.get("level") or ""), ds=ds, achievement=ach,
            estimated_gain=gain, target="SSS" if ach < 100 else "SSS+",
            reason="距离可兑现的推分目标较近",
        ))
    candidates.sort(key=lambda x: (-x.estimated_gain, abs(x.achievement - 99.5), x.ds))
    metrics = {
        "achievement_median": median(achievements) if achievements else 0,
        "b35_avg": b35_avg,
        "b15_avg": b15_avg,
        "high_avg": high_avg,
        "high_count": len(high),
        "b35_floor": floor,
        "ceiling": ceiling,
        "chart_count": len(rows),
    }
    return EvidencePack(
        nickname=str(snapshot.get("nickname") or "Player"),
        rating=_i(snapshot.get("rating")), b35=b35, b15=b15,
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
