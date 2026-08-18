"""Regression tests for impossible perfect-score filtering."""

from types import SimpleNamespace

from libraries.maimaidx_score_filter import (
    filter_anomalous_scores,
    is_anomalous_perfect_score,
)


def record(**updates):
    values = {
        "song_id": 123,
        "level_index": 3,
        "achievements": 101.0,
        "fc": "app",
        "dxScore": 3000,
        "dxScoreMax": 3000,
    }
    values.update(updates)
    return SimpleNamespace(**values)


bad = record()
assert is_anomalous_perfect_score(bad)
assert filter_anomalous_scores([record(achievements=100.9999), bad]) == [
    record(achievements=100.9999)
]

# All three conditions are required; incomplete records must be retained.
assert not is_anomalous_perfect_score(record(achievements=100.9999))
assert not is_anomalous_perfect_score(record(fc="ap"))
assert not is_anomalous_perfect_score(record(dxScore=2999))
assert not is_anomalous_perfect_score(record(dxScoreMax=0))

# Common raw API aliases and AP+ spelling are accepted.
raw = {
    "id": 123,
    "levelIndex": 3,
    "achievements": "101.0000",
    "comboStatus": "AP+",
    "dx_score": "3000",
    "dx_score_max": "3000",
}
assert is_anomalous_perfect_score(raw)

# Sega userMusicDetail uses achievement ×10000, numeric comboStatus and
# deluxscoreMax as the achieved DX score (not the theoretical maximum).
sega_raw = {
    "musicId": 123,
    "level": 3,
    "achievement": 1010000,
    "comboStatus": 4,
    "deluxscoreMax": 3000,
}
sega_music = SimpleNamespace(
    charts=[
        SimpleNamespace(notes=(1,)),
        SimpleNamespace(notes=(1,)),
        SimpleNamespace(notes=(1,)),
        SimpleNamespace(notes=(250, 250, 250, 250)),
    ]
)
assert is_anomalous_perfect_score(
    sega_raw, music_resolver=lambda _: sega_music
)
assert not is_anomalous_perfect_score(
    {**sega_raw, "deluxscoreMax": 2999},
    music_resolver=lambda _: sega_music,
)

# Normalized records omit the maximum, so derive it from the chart note count.
music = SimpleNamespace(
    charts=[
        SimpleNamespace(notes=(1,)),
        SimpleNamespace(notes=(1,)),
        SimpleNamespace(notes=(1,)),
        SimpleNamespace(notes=(250, 250, 250, 250)),
    ]
)
without_max = record()
del without_max.dxScoreMax
assert is_anomalous_perfect_score(without_max, music_resolver=lambda _: music)
assert not is_anomalous_perfect_score(without_max, music_resolver=lambda _: None)


def broken_resolver(_):
    raise RuntimeError("catalog unavailable")


assert not is_anomalous_perfect_score(without_max, music_resolver=broken_resolver)

print("anomalous score filter tests: ok")
