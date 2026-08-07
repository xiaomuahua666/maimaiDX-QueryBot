#!/usr/bin/env python3
"""Regression checks for completion-table badge path selection."""

import ast
import tempfile
from pathlib import Path
from typing import List, Optional


ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATH = ROOT / "libraries" / "maimaidx_music_info.py"
SOURCE = SOURCE_PATH.read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)

functions = [
    node
    for node in TREE.body
    if isinstance(node, ast.FunctionDef)
    and node.name in {"_plate_badge_paths", "_load_plate_badge"}
]


class FakeOpenedImage:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def load(self):
        return None

    def convert(self, mode):
        assert mode == "RGBA"
        return self

    def resize(self, size):
        return "badge", size


class FakeImage:
    Image = FakeOpenedImage
    behavior = {}

    @classmethod
    def open(cls, path):
        result = cls.behavior.get(path, FileNotFoundError(path))
        if isinstance(result, Exception):
            raise result
        return result


class FakeLog:
    def warning(self, _message):
        pass

    def error(self, _message):
        pass

with tempfile.TemporaryDirectory() as temp_dir:
    plate_versiondir = Path(temp_dir)
    namespace = {
        "Image": FakeImage,
        "List": List,
        "Optional": Optional,
        "Path": Path,
        "log": FakeLog(),
        "plate_versiondir": plate_versiondir,
    }
    module = ast.Module(body=functions, type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(SOURCE_PATH), "exec"), namespace)
    badge_paths = namespace["_plate_badge_paths"]

    assert badge_paths("星", "宙&星", "极") == [
        plate_versiondir / "星極.png",
        plate_versiondir / "宙&星極.png",
    ]
    assert badge_paths("霸", "舞", "者") == [
        plate_versiondir / "霸者.png",
        plate_versiondir / "舞者.png",
    ]
    assert badge_paths("镜", "镜", "神") == [plate_versiondir / "镜神.png"]

    load_badge = namespace["_load_plate_badge"]
    short_path, combined_path = badge_paths("星", "宙&星", "极")
    FakeImage.behavior = {
        short_path: ValueError("corrupt palette"),
        combined_path: FakeOpenedImage(),
    }
    assert load_badge("星", "宙&星", "极") == ("badge", (1000, 161))

    FakeImage.behavior = {}
    assert load_badge("星", "宙&星", "极") is None

assert "badge = _load_plate_badge(version, _ver, plan)" in SOURCE
assert "if badge is None:" in SOURCE
assert "except (OSError, ValueError)" in SOURCE
assert "完成表徽标资源缺失或损坏" in SOURCE

print("completion-table badge path tests: OK")
