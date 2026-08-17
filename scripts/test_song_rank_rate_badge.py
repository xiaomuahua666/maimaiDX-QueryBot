#!/usr/bin/env python3
"""Regression test for score-rank badges in the per-song group leaderboard."""

import ast
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "libraries" / "maimaidx_group_rating.py"

tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
helper = next(
    node
    for node in tree.body
    if isinstance(node, ast.FunctionDef) and node.name == "_song_score_info"
)
namespace = {}
exec(compile(ast.Module(body=[helper], type_ignores=[]), str(SOURCE), "exec"), namespace)
build_score_info = namespace["_song_score_info"]

for rate in ("sssp", "ssp", "sss", "aa", "a"):
    record = SimpleNamespace(
        achievements=100.5,
        fc="app",
        fs="fsdp",
        dxScore=3012,
        rate=rate,
        level="14+",
        level_index=4,
    )
    info = build_score_info(record, 3)
    assert info["rate"] == rate, info
    assert info["level_index"] == 4, info

legacy_record = SimpleNamespace(
    achievements=97.0,
    fc="",
    fs="",
    level="12",
)
legacy_info = build_score_info(legacy_record, 2)
assert legacy_info["rate"] == "", legacy_info
assert legacy_info["dxScore"] == 0, legacy_info
assert legacy_info["level_index"] == 2, legacy_info

print("song rank rate badge tests: ok")
