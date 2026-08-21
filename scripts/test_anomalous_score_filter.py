"""Regression tests for level-15 anomaly group eligibility."""

from types import SimpleNamespace

from libraries.maimaidx_score_filter import (
    filter_anomalous_scores,
    has_anomalous_group_score,
    is_anomalous_perfect_score,
    is_user_group_eligible,
)


def record(**updates):
    values = {
        "song_id": 123,
        "level_index": 3,
        "level": "15",
        "ds": 15.0,
        "achievements": 101.0,
        "fc": "app",
        "dxScore": 3000,
        "dxScoreMax": 3000,
    }
    values.update(updates)
    return SimpleNamespace(**values)


bad = record()
assert is_anomalous_perfect_score(bad)
scores = [record(achievements=100.9999), bad]
assert filter_anomalous_scores(scores) == scores
assert has_anomalous_group_score(scores)
assert not is_user_group_eligible(records=scores)

# The same full perfect below level 15 is legitimate for group features.
lower_level = record(level="14+", ds=14.9)
assert not is_anomalous_perfect_score(lower_level)
assert is_user_group_eligible(records=[lower_level])

# All three conditions are required; incomplete records must be retained.
assert not is_anomalous_perfect_score(record(achievements=100.9999))
assert not is_anomalous_perfect_score(record(fc="ap"))
assert not is_anomalous_perfect_score(record(dxScore=2999))
assert not is_anomalous_perfect_score(record(dxScoreMax=0))

# Common raw API aliases and AP+ spelling are accepted.
raw = {
    "id": 123,
    "levelIndex": 3,
    "level": "15",
    "achievements": "101.0000",
    "comboStatus": "AP+",
    "dx_score": "3000",
    "dx_score_max": "3000",
}
assert is_anomalous_perfect_score(raw)

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

# B50 chart containers are checked without mutating their contents.
charts = SimpleNamespace(sd=[record(achievements=100.0)], dx=[bad])
userinfo = SimpleNamespace(charts=charts)
assert not is_user_group_eligible(userinfo)
assert charts.dx == [bad]

print("anomalous score eligibility tests: ok")
