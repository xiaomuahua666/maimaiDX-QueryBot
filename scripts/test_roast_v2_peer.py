#!/usr/bin/env python3
"""Roast V2 peer distribution fallback regression tests."""

from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
os.environ.setdefault("MAIMAIDXPATH", str(ROOT / "static"))
os.environ.setdefault("MAIMAIDX_STORAGE_FAIL_FAST", "false")

import nonebot

nonebot.init()

from nonebot_plugin_maimaidx.libraries.maimaidx_roast_v2.analysis import build_evidence_pack  # noqa: E402


def _snapshot(gaps: list[float]) -> tuple[dict, dict[str, dict]]:
    rows: list[dict] = []
    charts: dict[str, dict] = {}
    for index, gap in enumerate(gaps):
        song_id = str(index + 1)
        rows.append({
            "song_id": song_id,
            "title": f"Peer {index + 1}",
            "type": "SD",
            "level_index": 3,
            "level": "14",
            "ds": 14.0,
            "achievement": 100.0,
            "ra": 300 + index,
        })
        charts[f"{song_id}:SD:3"] = {
            "avg_achievement": 100.0 - gap,
            "sample_count": 60,
            "b50_appear_rate": 0.4,
        }
    return {
        "nickname": "PeerTester",
        "rating": 15050,
        "b35": rows[:35],
        "b15": rows[35:50],
        "all_charts": [],
    }, charts


def _peer_stats(charts: dict[str, dict], *, player_count: int = 80, distribution: dict | None = None) -> dict:
    bucket = {"player_count": player_count, "charts": charts}
    if distribution is not None:
        bucket["arpi_distribution"] = distribution
    return {
        "generated_at": "2026-08-01T00:00:00+00:00",
        "rating_bucket_size": 200,
        "buckets": {"15000-15199": bucket},
    }


gaps = [round(-0.1 + index * 0.01, 4) for index in range(50)]
snapshot, charts = _snapshot(gaps)

# Production peer files may have per-chart aggregates but no player ARPI
# distribution. In that case the rail must show the user's matched-chart gap
# distribution, not blank player-percentile labels.
fallback_pack = build_evidence_pack(snapshot, _peer_stats(charts))
fallback = fallback_pack.peer
assert fallback["available"]
assert fallback["arpi"] == 0.145
assert fallback["distribution_kind"] == "chart_peer_gap"
assert fallback["distribution_label"] == "匹配谱面差值分布"
assert fallback["distribution_count"] == 50
assert fallback["position_basis"] == "matched_chart_gap"
assert fallback["position"] == "平均高于同段"
assert fallback["p25"] == 0.0225
assert fallback["median"] == 0.145
assert fallback["p75"] == 0.2675
assert all(fallback[key] is not None for key in ("p25", "median", "p75"))
fallback_evidence = next(item for item in fallback_pack.evidence if item.evidence_id == "peer_profile")
assert "谱面差值 P25/中位/P75" in fallback_evidence.value
assert "玩家 ARPI 分位" not in fallback_evidence.value

# A complete player-level ARPI distribution remains the authoritative source
# for actual player percentile placement.
player_pack = build_evidence_pack(snapshot, _peer_stats(charts, distribution={
    "count": 80,
    "p25": 0.1,
    "median": 0.2,
    "p75": 0.3,
}))
player = player_pack.peer
assert player["distribution_kind"] == "player_arpi"
assert player["distribution_label"] == "同段玩家 ARPI 分布"
assert player["distribution_count"] == 80
assert player["position_basis"] == "player_arpi_quartile"
assert player["position"] == "中位区间"
assert (player["p25"], player["median"], player["p75"]) == (0.1, 0.2, 0.3)
player_evidence = next(item for item in player_pack.evidence if item.evidence_id == "peer_profile")
assert "玩家 ARPI 分位：中位区间" in player_evidence.value

# Partial distributions are not silently presented as player percentiles.
partial_pack = build_evidence_pack(snapshot, _peer_stats(charts, distribution={"median": 0.2}))
assert partial_pack.peer["distribution_kind"] == "chart_peer_gap"
assert partial_pack.peer["p25"] == 0.0225

quartiles = {"p25": 0.1, "median": 0.2, "p75": 0.3}
for insufficient_count in (1, 19):
    insufficient_pack = build_evidence_pack(
        snapshot,
        _peer_stats(charts, distribution={"count": insufficient_count, **quartiles}),
    )
    assert insufficient_pack.peer["distribution_kind"] == "chart_peer_gap"
    assert insufficient_pack.peer["distribution_count"] == 50
    assert insufficient_pack.peer["p25"] == 0.0225

missing_count_pack = build_evidence_pack(snapshot, _peer_stats(charts, distribution=quartiles))
assert missing_count_pack.peer["distribution_kind"] == "chart_peer_gap"
assert missing_count_pack.peer["distribution_count"] == 50

threshold_pack = build_evidence_pack(
    snapshot,
    _peer_stats(charts, distribution={"count": 20, **quartiles}),
)
assert threshold_pack.peer["distribution_kind"] == "player_arpi"
assert threshold_pack.peer["distribution_count"] == 20
assert (threshold_pack.peer["p25"], threshold_pack.peer["median"], threshold_pack.peer["p75"]) == (
    0.1,
    0.2,
    0.3,
)

# The production symptom reported by users was an average gap around +0.0561
# with a medium-confidence peer bucket. It must receive a descriptive position
# instead of falling back to "未知".
medium_snapshot, medium_charts = _snapshot([0.0561] * 20)
medium = build_evidence_pack(
    medium_snapshot,
    _peer_stats(medium_charts, player_count=30),
).peer
assert medium["confidence"] == "medium"
assert medium["arpi"] == 0.0561
assert medium["position"] == "平均高于同段"
assert (medium["p25"], medium["median"], medium["p75"]) == (0.0561, 0.0561, 0.0561)
medium_pack = build_evidence_pack(
    medium_snapshot,
    _peer_stats(medium_charts, player_count=30),
)
assert medium_pack.song_groups["peer_strong"]
assert medium_pack.song_groups["peer_weak"] == []

empty_player_dist = build_evidence_pack(snapshot, _peer_stats(
    charts,
    player_count=0,
    distribution={"count": 0, "p25": 0.0, "median": 0.0, "p75": 0.0},
))
assert empty_player_dist.peer["distribution_kind"] == "chart_peer_gap"

# Sparse matches still receive a descriptive direction, but the neutral band
# is deliberately wider and the text forbids extrapolating to player rank.
sparse_snapshot, sparse_charts = _snapshot([0.06, 0.06])
sparse = build_evidence_pack(
    sparse_snapshot,
    _peer_stats(sparse_charts, player_count=5),
).peer
assert sparse["confidence"] == "low"
assert sparse["position"] == "接近同段"
assert sparse["p25"] == sparse["median"] == sparse["p75"] == 0.06
assert "不能外推为全部同段玩家排名" in sparse["position_detail"]

print("roast v2 peer tests: ok")
