#!/usr/bin/env python3
"""从脱敏公开成绩集生成段位课题谱面的紧凑统计文件。"""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
COURSE_FILE = ROOT / "libraries" / "assets" / "rank_courses.json"
PLAYERS_DIR = ROOT / "data" / "public_dataset" / "players"
OUTPUT_FILE = ROOT / "libraries" / "assets" / "rank_course_chart_stats.json"


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    pos = (len(values) - 1) * fraction
    lo = int(pos)
    hi = min(lo + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (pos - lo)


def main() -> None:
    course_data = json.loads(COURSE_FILE.read_text(encoding="utf-8"))
    wanted = {
        f"{song_id}:{level_index}"
        for course in course_data["courses"]
        for song_id, level_index in zip(course["song_ids"], course["level_indexes"])
    }
    values: dict[str, list[float]] = defaultdict(list)
    player_count = 0
    dataset_generated_at = None

    meta_file = PLAYERS_DIR.parent / "dataset_meta.json"
    if meta_file.exists():
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        dataset_generated_at = meta.get("generated_at")

    for path in sorted(PLAYERS_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        latest = data.get("latest") or {}
        records = latest.get("records") or []
        if not records:
            continue
        player_count += 1
        for record in records:
            key = f"{record.get('song_id')}:{record.get('level_index')}"
            if key in wanted:
                values[key].append(float(record.get("achievements") or 0.0))

    charts = {}
    for key in sorted(wanted, key=lambda item: tuple(map(int, item.split(":")))):
        samples = sorted(values.get(key, []))
        n = len(samples)
        charts[key] = {
            "sample_count": n,
            "avg_achievement": round(statistics.fmean(samples), 4) if samples else None,
            "p25": round(_percentile(samples, 0.25), 4) if samples else None,
            "median": round(_percentile(samples, 0.5), 4) if samples else None,
            "p75": round(_percentile(samples, 0.75), 4) if samples else None,
            "clear_rate": round(sum(v >= 97.0 for v in samples) / n, 4) if n else None,
            "sss_rate": round(sum(v >= 100.0 for v in samples) / n, 4) if n else None,
        }

    output = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset_generated_at": dataset_generated_at,
        "source": "data/public_dataset/players/* latest.records (opt-out excluded)",
        "player_count": player_count,
        "charts": charts,
    }
    OUTPUT_FILE.write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote {OUTPUT_FILE}: {len(charts)} charts, {player_count} players")


if __name__ == "__main__":
    main()
