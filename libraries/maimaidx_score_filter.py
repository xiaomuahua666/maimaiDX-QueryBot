"""Shared score-record validation helpers."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, TypeVar


T = TypeVar("T")
MusicResolver = Callable[[str], Any]


def _field(record: Any, *names: str) -> Any:
    if isinstance(record, dict):
        for name in names:
            if name in record:
                return record[name]
        return None
    for name in names:
        if hasattr(record, name):
            return getattr(record, name)
    return None


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _resolve_dx_score_max(record: Any, music_resolver: MusicResolver | None) -> int | None:
    explicit_max = _positive_int(
        _field(
            record,
            "dxScoreMax",
            "dx_score_max",
            "deluxscoreMax",
            "delux_score_max",
        )
    )
    if explicit_max is not None:
        return explicit_max

    song_id = _field(record, "song_id", "songId", "id")
    level_index = _field(record, "level_index", "levelIndex")
    if song_id is None or level_index is None:
        return None

    if music_resolver is None:
        try:
            from .maimaidx_music import mai

            music_resolver = mai.total_list.by_id
        except Exception:
            return None

    try:
        music = music_resolver(str(song_id))
        chart = music.charts[int(level_index)]
        return _positive_int(sum(chart.notes) * 3)
    except Exception:
        return None


def is_anomalous_perfect_score(
    record: Any,
    *,
    music_resolver: MusicResolver | None = None,
) -> bool:
    """Return whether a record is the level-15 full-DX/AP+/101 anomaly.

    Unknown or zero DX-score maxima are treated as inconclusive and retained.
    Achievement comparison follows the four-decimal value shown to users.
    """
    level = str(_field(record, "level") or "").strip().replace("＋", "+")
    try:
        ds = float(_field(record, "ds") or 0)
    except (TypeError, ValueError):
        ds = 0.0
    if ds < 15.0 and level not in {"15", "15+"}:
        return False

    try:
        achievements = float(_field(record, "achievements"))
        dx_score = int(_field(record, "dxScore", "dx_score", "deluxscore"))
    except (TypeError, ValueError):
        return False

    fc = str(_field(record, "fc", "comboStatus", "combo_status") or "").lower()
    if round(achievements, 4) != 101.0 or fc not in {"app", "ap+"}:
        return False

    dx_score_max = _resolve_dx_score_max(record, music_resolver)
    return dx_score_max is not None and dx_score == dx_score_max


def filter_anomalous_scores(
    records: Iterable[T],
    *,
    music_resolver: MusicResolver | None = None,
) -> list[T]:
    """Compatibility helper that preserves scores instead of deleting them.

    The anomaly is a user-level group eligibility signal. Personal B50 and
    full-record views must retain the original chart.
    """
    return list(records)


def has_anomalous_group_score(
    records: Iterable[Any],
    *,
    music_resolver: MusicResolver | None = None,
) -> bool:
    """Return whether any score disqualifies its owner from group features."""
    return any(
        is_anomalous_perfect_score(record, music_resolver=music_resolver)
        for record in records
    )


def is_user_group_eligible(
    userinfo: Any = None,
    records: Iterable[Any] = (),
    *,
    music_resolver: MusicResolver | None = None,
) -> bool:
    """Check group ranking/game eligibility without modifying score data."""
    candidates = list(records or ())
    charts = _field(userinfo, "charts") if userinfo is not None else None
    if charts is not None:
        candidates.extend(_field(charts, "sd") or ())
        candidates.extend(_field(charts, "dx") or ())
    return not has_anomalous_group_score(
        candidates,
        music_resolver=music_resolver,
    )
