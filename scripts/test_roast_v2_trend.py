#!/usr/bin/env python3
"""Roast V2 monthly Rating trend and conservative forecast regression tests."""

from __future__ import annotations

import importlib.util
import sys
import types
from datetime import date, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_snapshot_module():
    package_names = (
        "nonebot_plugin_maimaidx",
        "nonebot_plugin_maimaidx.libraries",
        "nonebot_plugin_maimaidx.libraries.maimaidx_roast_v2",
    )
    package_paths = (ROOT, ROOT / "libraries", ROOT / "libraries" / "maimaidx_roast_v2")
    for name, path in zip(package_names, package_paths):
        package = types.ModuleType(name)
        package.__path__ = [str(path)]
        sys.modules[name] = package

    best = types.ModuleType("nonebot_plugin_maimaidx.libraries.maimaidx_best_50")
    best._music_is_new = lambda _music: False
    sys.modules[best.__name__] = best

    datasource = types.ModuleType("nonebot_plugin_maimaidx.libraries.maimaidx_datasource")
    datasource.get_user_b50 = None
    datasource.get_user_records = None
    sys.modules[datasource.__name__] = datasource

    image = types.ModuleType("nonebot_plugin_maimaidx.libraries.image")
    image.music_picture = lambda song_id: Path(str(song_id))
    sys.modules[image.__name__] = image

    music = types.ModuleType("nonebot_plugin_maimaidx.libraries.maimaidx_music")
    music.mai = types.SimpleNamespace(total_list=types.SimpleNamespace(by_id=lambda _song_id: None))
    sys.modules[music.__name__] = music

    module_name = "nonebot_plugin_maimaidx.libraries.maimaidx_roast_v2.snapshot"
    path = ROOT / "libraries" / "maimaidx_roast_v2" / "snapshot.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


snapshot = _load_snapshot_module()
build_trend = snapshot._build_rating_trend


def _history(start: date, ratings: list[int], *, step_days: int = 1) -> list[dict]:
    return [
        {
            "date": (start + timedelta(days=index * step_days)).isoformat(),
            "stored_at": f"{(start + timedelta(days=index * step_days)).isoformat()}T23:00:00",
            "rating": rating,
        }
        for index, rating in enumerate(ratings)
    ]


def test_window_dedupe_and_live_rating() -> None:
    as_of = date(2026, 8, 17)
    history = [
        {"date": "2026-07-18", "stored_at": "2026-07-18T20:00:00", "rating": 14900},
        {"date": "2026-07-19", "stored_at": "2026-07-19T10:00:00", "rating": 15000},
        {"date": "2026-07-19", "stored_at": "2026-07-19T22:00:00", "rating": 15002},
        {"date": "2026-08-10", "stored_at": "2026-08-10T22:00:00", "rating": 15020},
        {"date": "2026-08-17", "stored_at": "2026-08-17T08:00:00", "rating": 15021},
        {"date": "2026-08-18", "stored_at": "2026-08-18T08:00:00", "rating": 99999},
    ]
    trend = build_trend(history, current_rating=15025, current_date=as_of)
    assert trend["window_days"] == 30
    assert trend["points"][0] == {"date": "2026-07-19", "rating": 15002}
    assert trend["points"][-1] == {"date": "2026-08-17", "rating": 15025}
    assert all(point["date"] <= "2026-08-17" for point in trend["points"])


def test_rising_forecast_is_conservative_and_deterministic() -> None:
    as_of = date(2026, 8, 17)
    history = _history(date(2026, 8, 3), [15000, 15002, 15004, 15006, 15008, 15010, 15012, 15014], step_days=2)
    first = build_trend(history, current_rating=15014, current_date=as_of)
    second = build_trend(history, current_rating=15014, current_date=as_of)
    assert first == second
    assert first["available"] is True
    assert first["point_count"] == 8
    assert first["span_days"] == 14
    assert first["quality"] == "medium"
    assert first["status"] == "rising"
    forecast = first["forecast"]
    assert forecast["available"] is True
    assert forecast["forecast_days"] == 7
    assert forecast["date"] == "2026-08-24"
    assert forecast["quality"] == "medium"
    assert forecast["confidence"] == "medium"
    assert forecast["rating_low"] == 15014
    assert forecast["gain_low"] == 0
    assert forecast["rating_low"] <= forecast["rating_mid"] <= forecast["rating_high"]
    assert forecast["rating_mid"] < 15021
    assert "不是涨分承诺" in forecast["note"]


def test_prediction_minimum_sample_gate() -> None:
    as_of = date(2026, 8, 17)
    two_points = build_trend(
        [
            {"date": "2026-08-16", "rating": 15000},
            {"date": "2026-08-17", "rating": 15020},
        ],
        current_rating=15020,
        current_date=as_of,
    )
    assert two_points["delta"] == 20
    assert two_points["quality"] == "insufficient"
    assert two_points["status"] == "insufficient"
    assert "样本不足" in two_points["status_text"]
    assert two_points["forecast"]["available"] is False

    three_points = build_trend(
        _history(date(2026, 8, 3), [15000, 15004, 15008], step_days=7),
        current_rating=0,
        current_date=as_of,
    )
    assert three_points["quality"] == "insufficient"
    assert three_points["status"] == "insufficient"
    assert three_points["forecast"]["available"] is False
    assert three_points["forecast"]["reason"] == "insufficient_history"

    short_span = build_trend(
        _history(date(2026, 8, 11), [15000, 15001, 15002, 15003], step_days=2),
        current_rating=15003,
        current_date=as_of,
    )
    assert short_span["span_days"] == 6
    assert short_span["forecast"]["available"] is False

    exact_minimum = build_trend(
        [
            {"date": "2026-08-10", "rating": 15000},
            {"date": "2026-08-12", "rating": 15002},
            {"date": "2026-08-14", "rating": 15004},
            {"date": "2026-08-17", "rating": 15007},
        ],
        current_rating=15007,
        current_date=as_of,
    )
    assert exact_minimum["span_days"] == 7
    assert exact_minimum["forecast"]["available"] is True


def test_plateau_and_reset_protection() -> None:
    as_of = date(2026, 8, 17)
    plateau = build_trend(
        _history(date(2026, 8, 3), [15000, 15005, 15005, 15005, 15005, 15005, 15005, 15005], step_days=2),
        current_rating=15005,
        current_date=as_of,
    )
    assert plateau["status"] == "plateau"
    assert plateau["last_gain_days_ago"] >= 7
    assert plateau["forecast"]["available"] is True
    assert plateau["forecast"]["rating_low"] == 15005
    assert plateau["forecast"]["rating_mid"] == 15005
    assert plateau["forecast"]["rating_high"] == 15005

    reset = build_trend(
        _history(date(2026, 8, 9), [15000, 15005, 14700, 14704, 14708], step_days=2),
        current_rating=14708,
        current_date=as_of,
    )
    assert reset["reset_detected"] is True
    assert reset["status"] == "reset"
    assert reset["quality"] == "low"
    assert reset["forecast"]["available"] is False
    assert reset["forecast"]["reason"] == "rating_reset_detected"

    declining = build_trend(
        _history(date(2026, 8, 9), [15000, 14998, 14996, 14994, 14992], step_days=2),
        current_rating=14992,
        current_date=as_of,
    )
    assert declining["reset_detected"] is False
    assert declining["forecast"]["available"] is False
    assert declining["forecast"]["reason"] == "negative_rating_trend"


if __name__ == "__main__":
    test_window_dedupe_and_live_rating()
    test_rising_forecast_is_conservative_and_deterministic()
    test_prediction_minimum_sample_gate()
    test_plateau_and_reset_protection()
    print("roast v2 trend tests: ok")
