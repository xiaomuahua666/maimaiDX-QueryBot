#!/usr/bin/env python3
"""段位课题数据、样本统计、LIFE 估算与图片布局回归测试。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from PIL import Image


ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = ROOT / "libraries" / "maimaidx_rank_course.py"
MUSIC_DATA = Path("/tmp/maimai_music_data.json")

spec = importlib.util.spec_from_file_location("maimaidx_rank_course_test", MODULE_PATH)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

meta, courses = module.load_rank_courses()
assert meta["region"] == "CN"
assert "PRiSM PLUS" in meta["version"]
assert len(courses) == 22
assert courses["真二段"].song_ids == (791, 11199, 191, 11532)
assert courses["真二段"].level_indexes == (3, 3, 2, 3)
assert module.get_rank_course("裏皆伝").name == "里皆传"
assert (
    len(
        {
            (sid, li)
            for c in courses.values()
            for sid, li in zip(c.song_ids, c.level_indexes)
        }
    )
    == 88
)

stats = module._load_bundled_course_stats()
assert stats["player_count"] == 536
assert len(stats["charts"]) == 88
assert all(item["sample_count"] > 0 for item in stats["charts"].values())

sample_key = "791:3"
sample_players = [
    {
        "records": [
            SimpleNamespace(song_id=791, level_index=3, achievements=value),
            # 同一玩家的重复谱面不能重复计入样本。
            SimpleNamespace(song_id=791, level_index=3, achievements=value - 1),
        ],
        "fetched_at": 1000 + index,
    }
    for index, value in enumerate((98.0, 100.0, 101.0))
]
live_stats = module.build_course_stats_from_players(
    sample_players, min_samples=3, fallback=stats
)
assert live_stats["player_count"] == 3
assert live_stats["charts"][sample_key]["sample_source"] == "live"
assert live_stats["charts"][sample_key]["sample_count"] == 3
assert live_stats["charts"][sample_key]["median"] == 100.0
private_stats = module.build_course_stats_from_players(
    sample_players[:2], min_samples=3, fallback=stats
)
assert private_stats["charts"][sample_key]["sample_source"] == "bundled"
assert module._dani_plate_filename(0) == "UI_DNM_DaniPlate_00.png"
assert module._dani_plate_filename(10) == "UI_DNM_DaniPlate_10.png"
assert module._dani_plate_filename(11) == "UI_DNM_DaniPlate_12.png"


def make_music(item: dict):
    return SimpleNamespace(
        id=str(item["id"]),
        title=item["title"],
        level=item["level"],
        ds=item["ds"],
        charts=[SimpleNamespace(notes=chart["notes"]) for chart in item["charts"]],
    )


course = courses["真二段"]
if MUSIC_DATA.exists():
    music_json = json.loads(MUSIC_DATA.read_text(encoding="utf-8"))
    music_ids = {int(item["id"]) for item in music_json}
    missing = sorted(
        {sid for item in courses.values() for sid in item.song_ids} - music_ids
    )
    assert not missing, f"当前曲库缺少段位曲 ID: {missing}"
    music_by_id = {int(item["id"]): item for item in music_json}
    assert music_by_id[courses["二段"].song_ids[3]]["title"] == "＊ハロー、プラネット。"
    assert (
        music_by_id[courses["三段"].song_ids[2]]["title"]
        == "インドア系ならトラックメイカー"
    )
    assert music_by_id[courses["真三段"].song_ids[0]]["title"] == "ザムザ"
    assert music_by_id[courses["真十段"].song_ids[1]]["title"] == "Re:Unknown X"
    music_list = [make_music(item) for item in music_json]
else:
    titles = (
        "ロールプレイングゲーム",
        "悪戯センセーション",
        "脳漿炸裂ガール",
        "ヱデン",
    )
    music_list = [
        SimpleNamespace(
            id=str(song_id),
            title=title,
            level=["5", "7", "12+", "12+", "14+"],
            ds=[5.0, 7.0, 12.7, 12.8, 14.8],
            charts=[SimpleNamespace(notes=[300, 50, 100, 20]) for _ in range(5)],
        )
        for song_id, title in zip(course.song_ids, titles)
    ]
records = [
    SimpleNamespace(song_id=sid, level_index=li, achievements=100.5)
    for sid, li in zip(course.song_ids, course.level_indexes)
]
tracks = module.build_course_tracks(course, music_list, records)
assert [track.title for track in tracks] == [
    "ロールプレイングゲーム",
    "悪戯センセーション",
    "脳漿炸裂ガール",
    "ヱデン",
]
assert all(track.ds is not None and track.sample for track in tracks)
assert module.estimate_life(course, tracks) is not None

# 绘图测试不依赖额外静态资源。
module._rounded_cover = lambda _song_id, size=154: Image.new(
    "RGBA", (size, size), (225, 232, 243, 255)
)
module._player_visual_assets = lambda _plate, _rating, _course=None: (
    Image.new("RGBA", (800, 130), (214, 240, 245, 255)),  # plate
    Image.new("RGBA", (380, 168), (255, 210, 83, 255)),   # dani_plate
    Image.new("RGBA", (380, 168), (100, 180, 220, 255)),  # course_dani
    Image.new("RGBA", (249, 120), (200, 200, 200, 255)),  # logo
    Image.new("RGBA", (120, 120), (100, 100, 100, 255)),  # icon
    Image.new("RGBA", (200, 40),  (255, 255, 255, 255)),  # name_img
)
preview = module.draw_rank_course(
    course,
    tracks,
    player_name="TEST",
    player_plate="测试姓名框",
    player_additional_rating=0,
)
assert preview.size == (1080, 1570)
print("rank course: render OK")
