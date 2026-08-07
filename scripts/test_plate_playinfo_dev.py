#!/usr/bin/env python3
"""Prevent completion tables from adding fields to strict score models."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "libraries" / "maimaidx_music_info.py").read_text(encoding="utf-8")
start = SOURCE.index("async def draw_plate_table(")
draw_plate_source = SOURCE[start:]

assert ".table_level" not in draw_plate_source
assert "model_copy(update={'ds':" in draw_plate_source
assert "_music = mai.total_list.by_id(_d.song_id)" in draw_plate_source
assert "_key = _music.level[4]" in draw_plate_source
assert "_key = _music.level[3]" in draw_plate_source

print("completion-table PlayInfoDev compatibility: OK")
