#!/usr/bin/env python3
"""Self-contained regression test for rating-table cache invalidation."""

import ast
import hashlib
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import List, Optional


ROOT = Path(__file__).resolve().parent.parent
SOURCE_PATH = ROOT / "libraries" / "maimaidx_update_plate.py"
TREE = ast.parse(SOURCE_PATH.read_text(encoding="utf-8"))
FUNCTION_NAMES = {
    "_rating_table_signature",
    "_load_rating_manifest",
    "_record_rating_table_signature",
    "rating_table_is_current",
    "stale_rating_table_names",
}
FUNCTIONS = [
    node
    for node in TREE.body
    if isinstance(node, ast.FunctionDef) and node.name in FUNCTION_NAMES
]
assert {node.name for node in FUNCTIONS} == FUNCTION_NAMES


class RatingGridConfig:
    start_x = 140
    start_y = 450
    gap = 85
    row_count = 14


def song(song_id: int) -> SimpleNamespace:
    return SimpleNamespace(id=str(song_id), lv="3", ds=11.0, type="DX")


with tempfile.TemporaryDirectory() as temp_dir:
    rating_dir = Path(temp_dir)
    manifest_path = rating_dir / ".manifest.json"
    (rating_dir / "11.png").touch()
    mai = SimpleNamespace(
        total_level_data={"11": {"11.5": [], "11.0": [song(1)]}}
    )
    namespace = {
        "List": List,
        "Optional": Optional,
        "Path": Path,
        "RatingGridConfig": RatingGridConfig,
        "_RATING_MANIFEST_VERSION": 1,
        "_RATING_MANIFEST_PATH": manifest_path,
        "hashlib": hashlib,
        "json": json,
        "levelList": ["1", "2", "3", "4", "5", "6", "11"],
        "mai": mai,
        "rating_table_dir": rating_dir,
    }
    module = ast.Module(body=FUNCTIONS, type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(SOURCE_PATH), "exec"), namespace)

    assert not namespace["rating_table_is_current"]("11")
    namespace["_record_rating_table_signature"]("11")
    assert namespace["rating_table_is_current"]("11")

    mai.total_level_data["11"]["11.0"].append(song(2))
    assert not namespace["rating_table_is_current"]("11")
    assert namespace["stale_rating_table_names"]() == ["11"]

    namespace["_record_rating_table_signature"]("11")
    assert namespace["rating_table_is_current"]("11")
    RatingGridConfig.row_count = 13
    assert not namespace["rating_table_is_current"]("11")

print("rating table manifest invalidation: OK")
