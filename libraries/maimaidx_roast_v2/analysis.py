from __future__ import annotations

from dataclasses import replace
from statistics import mean, median, pstdev
from typing import Any

from .domain import Candidate, Evidence, EvidencePack, RoastReport, StyleSpec
from .peer import attach_peer_profile


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


def _row_key(row: dict[str, Any]) -> tuple[str, str, int]:
    return (
        str(row.get("song_id") or row.get("music_id") or ""),
        str(row.get("type") or row.get("chart_type") or "SD").upper(),
        _i(row.get("level_index"), -1),
    )


def _rating_ds_cap(rating: int) -> float:
    if rating < 12000:
        return 13.0
    if rating < 13000:
        return 13.4
    if rating < 14000:
        return 13.8
    if rating < 14500:
        return 14.2
    if rating < 15000:
        return 14.5
    if rating < 16000:
        return 14.8
    return 15.0


def _recommendation_profile(rows: list[dict[str, Any]], rating: int) -> tuple[float, float, float]:
    # 98.5% 以上才算“有证据的能力圈”：既不会被低分曲目拉宽，
    # 也不会因为 B35 恰好全是高分而丢掉 B15 的真实上沿。
    proven = [
        _f(item.get("ds"))
        for item in rows
        if _f(item.get("ds")) > 0 and _f(item.get("achievement")) >= 98.5
    ]
    if len(proven) < 8:
        proven = [
            _f(item.get("ds"))
            for item in rows
            if _f(item.get("ds")) > 0 and _f(item.get("achievement")) >= 97.0
        ]
    if not proven:
        proven = [_f(item.get("ds")) for item in rows if _f(item.get("ds")) > 0]
    center = _percentile(proven, 0.5)
    upper = _percentile(proven, 0.85)
    # 用已稳定成绩的 85 分位做上沿，再只放宽一小格。这样 13k 玩家
    # 不会因为一首偶然打高的谱面被推去碰远超当前能力圈的高定数。
    data_cap = upper + (0.12 if rating >= 14000 else 0.20)
    cap = min(_rating_ds_cap(rating), data_cap) if data_cap > 0 else _rating_ds_cap(rating)
    return center, upper, round(max(1.0, cap), 2)


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


def _aggregate(label: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    achievements = [_f(row.get("achievement")) for row in rows if _f(row.get("achievement")) > 0]
    ds_values = [_f(row.get("ds")) for row in rows if _f(row.get("ds")) > 0]
    ra_values = [_i(row.get("ra")) for row in rows if _i(row.get("ra")) > 0]
    peer_rows = [row for row in rows if row.get("peer_avg") is not None]
    peer_samples = [_i(row.get("peer_sample_count")) for row in peer_rows if _i(row.get("peer_sample_count")) > 0]
    return {
        "label": label,
        "count": len(rows),
        "avg_achievement": round(mean(achievements), 4) if achievements else None,
        "avg_ds": round(mean(ds_values), 2) if ds_values else None,
        "avg_ra": round(mean(ra_values), 1) if ra_values else None,
        "peer_avg": round(mean([_f(row.get("peer_avg")) for row in peer_rows]), 4) if peer_rows else None,
        "peer_gap": round(mean([_f(row.get("peer_gap")) for row in peer_rows]), 4) if peer_rows else None,
        "peer_matched": len(peer_rows),
        "peer_sample_avg": round(mean(peer_samples)) if peer_samples else None,
    }


def _build_ds_bands(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    specs = (
        ("<13", lambda ds: ds < 13.0),
        ("13.0–13.5", lambda ds: 13.0 <= ds < 13.6),
        ("13.6–13.9", lambda ds: 13.6 <= ds < 14.0),
        ("14.0–14.5", lambda ds: 14.0 <= ds < 14.6),
        ("14.6–15.0", lambda ds: ds >= 14.6),
    )
    return [_aggregate(label, [row for row in rows if predicate(_f(row.get("ds")))]) for label, predicate in specs]


def _build_difficulty_bands(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    labels = {0: "BASIC", 1: "ADVANCED", 2: "EXPERT", 3: "MASTER", 4: "Re:MASTER"}
    return [
        _aggregate(label, [row for row in rows if _i(row.get("level_index"), -1) == index])
        for index, label in labels.items()
        if any(_i(row.get("level_index"), -1) == index for row in rows)
    ]


def _build_genre_profiles(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    genres: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        genre = str(row.get("genre") or "").strip()
        if genre:
            genres.setdefault(genre, []).append(row)
    profiles = [_aggregate(label, items) for label, items in genres.items() if len(items) >= 2]
    profiles.sort(key=lambda item: (-int(item.get("count") or 0), -float(item.get("avg_achievement") or 0)))
    return profiles[:8]


def _unique_rows(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int]] = set()
    for row in rows:
        key = _row_key(row)
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
        if len(result) >= limit:
            break
    return result


def _song_groups(b35: list[dict[str, Any]], b15: list[dict[str, Any]], rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    with_peer = [row for row in rows if row.get("peer_gap") is not None]
    peer_strong = sorted(with_peer, key=lambda row: _f(row.get("peer_gap")), reverse=True)
    peer_weak = sorted(with_peer, key=lambda row: _f(row.get("peer_gap")))
    top_ra = sorted(rows, key=lambda row: _i(row.get("ra")), reverse=True)
    floors = sorted(b35, key=lambda row: _i(row.get("ra")))[:3] + sorted(b15, key=lambda row: _i(row.get("ra")))[:2]
    unusual = sorted(
        [row for row in with_peer if row.get("peer_appear_rate") is not None],
        key=lambda row: _f(row.get("peer_appear_rate")),
    )
    evidence_cards = _unique_rows(peer_strong[:2] + peer_weak[:2] + top_ra[:2] + floors[:3], 9)
    return {
        "peer_strong": _unique_rows(peer_strong, 4),
        "peer_weak": _unique_rows(peer_weak, 4),
        "top_ra": _unique_rows(top_ra, 4),
        "floors": _unique_rows(floors, 5),
        "unusual": _unique_rows(unusual, 3),
        "evidence_cards": evidence_cards,
    }


def _candidate_risk(achievement: float, target: float, ds: float, stable_upper: float) -> str:
    gap = target - achievement
    if ds <= stable_upper + 0.061 and gap <= (0.55 if target == 100.0 else 0.22):
        return "稳妥"
    if ds <= stable_upper + 0.21 and gap <= (1.35 if target == 100.0 else 0.40):
        return "进阶"
    return "冲刺"


def _candidate_marginal(candidate: Candidate, state: dict[tuple[str, str, int], int], capacity: int) -> int:
    key = (candidate.song_id, candidate.chart_type.upper(), candidate.level_index)
    if key in state:
        return max(0, candidate.target_ra - state[key])
    if len(state) < capacity:
        return max(0, candidate.target_ra)
    return max(0, candidate.target_ra - min(state.values(), default=0))


def _apply_candidate(candidate: Candidate, state: dict[tuple[str, str, int], int], capacity: int) -> None:
    key = (candidate.song_id, candidate.chart_type.upper(), candidate.level_index)
    if key not in state and len(state) >= capacity and state:
        floor_key = min(state, key=state.get)
        state.pop(floor_key, None)
    state[key] = max(state.get(key, 0), candidate.target_ra)


def _build_candidates(
    all_charts: list[dict[str, Any]],
    b35: list[dict[str, Any]],
    b15: list[dict[str, Any]],
    *,
    rating: int,
    ds_center: float,
    ds_upper: float,
    ds_cap: float,
    high_count: int,
) -> list[Candidate]:
    b35_keys = {_row_key(row) for row in b35}
    b15_keys = {_row_key(row) for row in b15}
    old_floor = min((_i(row.get("ra")) for row in b35), default=0)
    new_floor = min((_i(row.get("ra")) for row in b15), default=0)
    raw: list[Candidate] = []
    for song in all_charts:
        achievement = _f(song.get("achievement"))
        ds = _f(song.get("ds"))
        if not song.get("title") or ds <= 0 or ds > ds_cap + 1e-6 or achievement >= 100.5:
            continue
        # 推荐只接受已经接近目标线的成绩；低于此线宁可不给，也不把
        # “理论收益”包装成可执行路线。
        if achievement < (99.15 if rating < 14500 else 98.9):
            continue
        song_id, chart_type, level_index = _row_key(song)
        key = (song_id, chart_type, level_index)
        pool = str(song.get("pool") or ("new" if key in b15_keys else "old"))
        current_ra = _i(song.get("ra"))
        baseline = current_ra if key in b35_keys or key in b15_keys else (
            new_floor if pool == "new" and len(b15) >= 15 else old_floor if pool != "new" and len(b35) >= 35 else 0
        )
        target_achievement = 100.0 if achievement < 100.0 else 100.5
        target_ra = _calc_ra(ds, target_achievement)
        independent_gain = max(0, target_ra - baseline)
        if independent_gain < 2 and achievement < 100.0:
            target_achievement = 100.5
            target_ra = _calc_ra(ds, target_achievement)
            independent_gain = max(0, target_ra - baseline)
        if independent_gain < 2:
            continue
        risk = _candidate_risk(achievement, target_achievement, ds, ds_upper)
        if risk == "冲刺":
            continue
        if ds >= 14.6 and high_count == 0 and achievement < 99.7:
            continue
        gap = max(0.0, target_achievement - achievement)
        risk_penalty = 0 if risk == "稳妥" else 28
        priority = independent_gain * 4.0 - gap * 16.0 - max(0.0, ds - ds_upper) * 36.0 - abs(ds - ds_center) * 2.0 - risk_penalty
        raw.append(Candidate(
            song_id=song_id,
            title=str(song.get("title") or ""),
            level=str(song.get("level") or ""),
            ds=ds,
            achievement=achievement,
            estimated_gain=independent_gain,
            target="SSS" if target_achievement == 100.0 else "SSS+",
            reason=f"当前 {achievement:.4f}%，距目标 {gap:.4f} pp；按 {pool.upper()} 槽位逐步替换计算",
            cover_path=str(song.get("cover_path") or ""),
            artist=str(song.get("artist") or ""),
            genre=str(song.get("genre") or ""),
            level_index=level_index,
            chart_type=chart_type,
            pool=pool,
            target_achievement=target_achievement,
            current_ra=current_ra,
            target_ra=target_ra,
            priority_score=round(priority, 3),
            risk=risk,
        ))

    old_state = {_row_key(row): _i(row.get("ra")) for row in b35}
    new_state = {_row_key(row): _i(row.get("ra")) for row in b15}
    remaining = list(raw)
    route: list[Candidate] = []
    cumulative = 0
    advanced_count = 0
    while remaining and len(route) < 5:
        scored: list[tuple[float, int, Candidate]] = []
        for candidate in remaining:
            if candidate.risk == "进阶" and advanced_count >= 2:
                continue
            state = new_state if candidate.pool == "new" else old_state
            capacity = 15 if candidate.pool == "new" else 35
            marginal = _candidate_marginal(candidate, state, capacity)
            if marginal < 2:
                continue
            conservative_bonus = 22 if candidate.risk == "稳妥" else 0
            score = candidate.priority_score + marginal * 2.5 + conservative_bonus
            scored.append((score, marginal, candidate))
        if not scored:
            break
        if len(route) < 4 and any(item[2].risk == "稳妥" for item in scored):
            scored = [item for item in scored if item[2].risk == "稳妥"]
        score, marginal, chosen = max(scored, key=lambda item: (item[0], item[1], -item[2].ds))
        cumulative += marginal
        chosen = replace(chosen, estimated_gain=marginal, priority_score=round(score, 3), route_step=len(route) + 1, cumulative_gain=cumulative)
        route.append(chosen)
        if chosen.risk == "进阶":
            advanced_count += 1
        state = new_state if chosen.pool == "new" else old_state
        _apply_candidate(chosen, state, 15 if chosen.pool == "new" else 35)
        remaining = [item for item in remaining if (item.song_id, item.chart_type, item.level_index) != (chosen.song_id, chosen.chart_type, chosen.level_index)]
    return route


def _song_evidence(row: dict[str, Any]) -> Evidence:
    song_id, chart_type, level_index = _row_key(row)
    pieces = [
        f"{row.get('title') or '未知曲目'}",
        f"{chart_type} {row.get('level') or ''}",
        f"定数 {_f(row.get('ds')):.1f}",
        f"达成率 {_f(row.get('achievement')):.4f}%",
        f"RA {_i(row.get('ra'))}",
    ]
    if row.get("peer_avg") is not None:
        pieces.extend([
            f"同段 B50 入选均值 {_f(row.get('peer_avg')):.4f}%",
            f"差值 {_f(row.get('peer_gap')):+.4f} pp",
            f"样本 n={_i(row.get('peer_sample_count'))}",
        ])
    return Evidence(
        f"song:{song_id}:{chart_type}:{level_index}",
        "用户成绩证据",
        "；".join(pieces),
        "player_score",
        confidence="high" if row.get("peer_avg") is None else "peer_aggregate",
    )


def build_evidence_pack(snapshot: dict, peer_stats: dict | None = None) -> EvidencePack:
    b35 = [dict(item, pool="old") for item in list(snapshot.get("b35") or [])[:35]]
    b15 = [dict(item, pool="new") for item in list(snapshot.get("b15") or [])[:15]]
    all_charts = [dict(item) for item in list(snapshot.get("all_charts") or [])]
    rows = b35 + b15
    rating = _i(snapshot.get("rating"))
    peer = attach_peer_profile(rows, rating, peer_stats)
    achievements = [_f(row.get("achievement")) for row in rows if _f(row.get("achievement")) > 0]
    b35_avg = mean([_f(row.get("achievement")) for row in b35]) if b35 else 0.0
    b15_avg = mean([_f(row.get("achievement")) for row in b15]) if b15 else 0.0
    ds_center, ds_upper, ds_cap = _recommendation_profile(rows, rating)
    high = [row for row in rows if _f(row.get("ds")) >= 14.6]
    high_avg = mean([_f(row.get("achievement")) for row in high]) if high else None
    old_floor = min((_i(row.get("ra")) for row in b35), default=0)
    new_floor = min((_i(row.get("ra")) for row in b15), default=0)
    ceiling = max((_i(row.get("ra")) for row in rows), default=0)
    pool_profiles = [_aggregate("B35", b35), _aggregate("B15", b15)]
    ds_bands = _build_ds_bands(rows)
    difficulty_bands = _build_difficulty_bands(rows)
    genre_profiles = _build_genre_profiles(rows)
    song_groups = _song_groups(b35, b15, rows)
    candidates = _build_candidates(
        all_charts, b35, b15, rating=rating, ds_center=ds_center,
        ds_upper=ds_upper, ds_cap=ds_cap, high_count=len(high),
    )
    top3_gain = sum(item.estimated_gain for item in candidates[:3])
    b35_b15_gap = b35_avg - b15_avg
    achievement_stddev = pstdev(achievements) if len(achievements) >= 2 else 0.0
    sssp_count = sum(1 for value in achievements if value >= 100.5)
    sss_only_count = sum(1 for value in achievements if 100.0 <= value < 100.5)
    evidence = [
        Evidence("rating", "当前 Rating", str(rating), "snapshot"),
        Evidence("b35_avg", "B35 平均达成率", f"{b35_avg:.4f}%", "b35"),
        Evidence("b15_avg", "B15 平均达成率", f"{b15_avg:.4f}%", "b15"),
        Evidence("b35_b15_gap", "B35 与 B15 平均差", f"{b35_b15_gap:+.4f} pp", "b50"),
        Evidence("achievement_stddev", "B50 达成率波动", f"σ {achievement_stddev:.4f} pp", "b50"),
        Evidence("high_avg", "14+ 平均达成率", f"{high_avg:.4f}%" if high_avg is not None else "暂无 14+ 样本", "high_ds", confidence="high" if high else "unavailable"),
        Evidence("floors", "B35 / B15 槽位地板", f"{old_floor} / {new_floor}", "b50"),
        Evidence("recommendation_ds_cap", "保守推荐定数上限", f"{ds_cap:.2f}", "capability_profile"),
        Evidence("route_gain", "前三步边际累计收益", f"+{top3_gain} Rating", "route_simulation"),
        Evidence("top3_gain", "推荐路线前三步收益", f"+{top3_gain} Rating", "route_simulation"),
        Evidence("sss_count", "B50 SSS / SSS+ 数量", f"{sss_only_count + sssp_count} / {sssp_count}", "b50"),
    ]
    if peer.get("available"):
        evidence.append(Evidence(
            "peer_profile", "同段位置",
            f"{peer.get('bucket')}；ARPI {peer.get('arpi'):+.4f} pp；{peer.get('position')}；匹配 {peer.get('matched')}/{len(rows)}；同段玩家 {peer.get('player_count')}；{peer.get('confidence_text')}",
            "anonymized_peer_aggregate", confidence=str(peer.get("confidence") or "low"),
        ))
    for row in song_groups.get("evidence_cards", []):
        evidence.append(_song_evidence(row))
    metrics = {
        "achievement_median": median(achievements) if achievements else 0.0,
        "b35_avg": b35_avg, "b15_avg": b15_avg, "high_avg": high_avg,
        "high_count": len(high), "b35_floor": old_floor, "b15_floor": new_floor,
        "ceiling": ceiling, "chart_count": len(rows),
        "recommendation_ds_center": ds_center, "recommendation_stable_upper": ds_upper,
        "recommendation_ds_cap": ds_cap, "b35_b15_gap": b35_b15_gap,
        "achievement_stddev": achievement_stddev, "sss_only_count": sss_only_count,
        "sss_count": sss_only_count + sssp_count, "sssp_count": sssp_count, "top3_estimated_gain": top3_gain,
        "pool_profiles": pool_profiles, "route_count": len(candidates),
        "conservative_route_count": sum(1 for item in candidates if item.risk == "稳妥"),
    }
    return EvidencePack(
        nickname=str(snapshot.get("nickname") or "Player"), rating=rating,
        b35=b35, b15=b15, all_charts=all_charts, evidence=evidence,
        candidates=candidates, metrics=metrics, peer=peer, ds_bands=ds_bands,
        difficulty_bands=difficulty_bands, genre_profiles=genre_profiles,
        song_groups=song_groups, trend=dict(snapshot.get("trend") or {}),
    )


def build_report_fallback(pack: EvidencePack, style: StyleSpec) -> RoastReport:
    metrics = pack.metrics
    address = f"{style.address}，" if style.address else ""
    suffix = f" {style.suffix}" if style.suffix else ""
    gap = _f(metrics.get("b35_b15_gap"))
    if gap >= 0.25:
        structure = "旧曲基本盘明显强于新曲适应"
    elif gap <= -0.25:
        structure = "新曲适应不错，但旧曲地板仍有整理空间"
    else:
        structure = "B35 与 B15 结构接近，整体比较均衡"
    peer_text = ""
    if pack.peer.get("available"):
        peer_text = f"；同段位于{pack.peer.get('position')}，ARPI {pack.peer.get('arpi'):+.4f} pp"
    headline = f"{address}{structure}，先走保守边际收益路线{suffix}"
    summary = (
        f"{address}{structure}{peer_text}。推荐上限约 "
        f"{metrics.get('recommendation_ds_cap', 0):.2f}，前三步逐槽替换累计预计 "
        f"+{metrics.get('top3_estimated_gain', 0)} Rating。{suffix}"
    ).strip()
    evidence_cards = pack.song_groups.get("evidence_cards", [])
    named = "、".join(str(row.get("title") or "") for row in evidence_cards[:4]) or "当前 B50 曲目"
    analysis = (
        f"B35 平均 {metrics.get('b35_avg', 0):.4f}%，B15 平均 {metrics.get('b15_avg', 0):.4f}%，"
        f"差值 {gap:+.4f} pp；B50 达成率标准差 {metrics.get('achievement_stddev', 0):.4f} pp。"
        f"本次引用的成绩证据包括：{named}。推荐器只保留接近目标线、且不超过已稳定定数上沿一小档的曲目；"
        "每一步收益都会在上一步替换槽位后重算，不把多首歌重复按同一个地板相加。"
    )
    strengths = [
        f"《{row.get('title')}》比同段 B50 入选均值高 {_f(row.get('peer_gap')):+.4f} pp"
        for row in pack.song_groups.get("peer_strong", [])[:3]
    ] or [f"B35 平均达成率 {metrics.get('b35_avg', 0):.4f}%", f"最高单曲 RA {metrics.get('ceiling', 0)}"]
    weaknesses = [
        f"《{row.get('title')}》比同段 B50 入选均值低 {_f(row.get('peer_gap')):+.4f} pp"
        for row in pack.song_groups.get("peer_weak", [])[:3]
    ] or ["优先整理 B35/B15 地板，而不是盲目跨级冲高难"]
    peer_takeaways = [str(pack.peer.get("position_detail") or "同段样本不足，暂不下结论")]
    actions = [
        "先完成标记为“稳妥”的寸止曲，再考虑进阶曲",
        "每完成一首后重新生成锐评，让后续边际收益按新地板重算",
        "同段差距只作脱敏聚合参考，训练目标仍以个人稳定达成为准",
    ]
    return RoastReport(
        headline=headline, summary=summary, analysis=analysis,
        strengths=strengths, weaknesses=weaknesses, peer_takeaways=peer_takeaways,
        actions=actions, recommendations=[candidate.__dict__ for candidate in pack.candidates[:5]],
        claims=[{"text": structure, "evidence_ids": ["b35_avg", "b15_avg", "b35_b15_gap"]}],
        style=style,
    )
