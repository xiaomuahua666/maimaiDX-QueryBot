"""锐评同段证据置信度与 B35/B15 推分收益回归测试。"""

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
module_path = ROOT / "libraries" / "b50_analysis" / "context_builder.py"
spec = importlib.util.spec_from_file_location("b50_context_builder_test", module_path)
assert spec and spec.loader
context_builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(context_builder)


def chart(song_id: int, title: str, *, ra: int, achievement: float = 100.5) -> dict:
    return {
        "song_id": song_id,
        "title": title,
        "type": "SD",
        "level_index": 3,
        "level_label": "Master",
        "ds": 13.0,
        "achievements": achievement,
        "ra": ra,
    }


sd = [chart(i, f"Old {i}", ra=250) for i in range(1, 36)]
dx = [chart(100 + i, f"New {i}", ra=270) for i in range(1, 16)]
sd.append(chart(1001, "Old Push", ra=240, achievement=99.8))
dx.append(chart(1002, "New Push", ra=240, achievement=99.8))
sd.append(chart(1003, "Already SSS Plus", ra=282, achievement=100.5))
dx.append(chart(1004, "Already Theory", ra=282, achievement=101.0))

chart_stats = {}
for row in sd[:35] + dx[:15]:
    chart_stats[f"{row['song_id']}:{row['level_index']}"] = {
        "avg_achievement": 100.1,
        "sample_count": 40,
        "b50_appear_rate": 0.25,
    }

peer_stats = {
    "generated_at": "2026-07-28T00:00:00+00:00",
    "rating_bucket_size": 200,
    "buckets": {
        "15000-15199": {
            "player_count": 60,
            "charts": chart_stats,
            "arpi_distribution": {
                "count": 60,
                "mean": 0.2,
                "median": 0.2,
                "p25": 0.1,
                "p75": 0.3,
            },
        }
    },
}

context = context_builder.build_context(
    {
        "nickname": "Tester",
        "rating": 15050,
        "charts": {"sd": sd, "dx": dx},
    },
    peer_stats,
)

peer = context["b50_evidence_pack"]["peer_comparison"]
assert peer["player_count"] == 60
assert peer["matched"] == 50
assert peer["coverage"] == 1.0
assert peer["confidence"] == "high"
assert context["b50_evidence_pack"]["selected_evidence"][0]["peer_sample_count"] == 40

push_by_title = {row["title"]: row for row in context["push_candidates"]}
old_push = push_by_title["Old Push"]
new_push = push_by_title["New Push"]
assert old_push["rating_pool"] == "B35"
assert old_push["replacement_floor"] == 250
assert old_push["gain_100"] == 30
assert new_push["rating_pool"] == "B15"
assert new_push["replacement_floor"] == 270
assert new_push["gain_100"] == 10
assert old_push["estimated_gain"] == old_push["gain_100"]
assert new_push["estimated_gain"] == new_push["gain_100"]
assert "Already SSS Plus" not in push_by_title
assert "Already Theory" not in push_by_title
assert all(row["achievements"] < 100.5 for row in context["push_candidates"])

print("b50 evidence tests: ok")
