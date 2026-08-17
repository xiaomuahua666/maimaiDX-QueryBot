from __future__ import annotations

from typing import Any

from ..maimaidx_best_50 import _music_is_new
from ..maimaidx_datasource import get_user_b50, get_user_records
from ..image import music_picture
from ..maimaidx_music import mai


def _cover_path(song_id: str) -> str:
    if not song_id:
        return ""
    try:
        return str(music_picture(song_id))
    except (TypeError, ValueError):
        return ""


def _chart(value: Any, *, pool: str = "") -> dict:
    song_id = str(getattr(value, "song_id", "") or "")
    music = mai.total_list.by_id(song_id)
    basic = getattr(music, "basic_info", None)
    return {
        "song_id": song_id,
        "title": str(getattr(value, "title", "") or ""),
        "type": str(getattr(value, "type", "SD") or "SD"),
        "level": str(getattr(value, "level", "") or ""),
        "level_index": int(getattr(value, "level_index", 0) or 0),
        "ds": float(getattr(value, "ds", 0) or 0),
        "achievement": float(getattr(value, "achievements", 0) or 0),
        "ra": int(getattr(value, "ra", 0) or 0),
        "fc": str(getattr(value, "fc", "") or ""),
        "fs": str(getattr(value, "fs", "") or ""),
        "artist": str(getattr(basic, "artist", "") or ""),
        "genre": str(getattr(basic, "genre", "") or ""),
        "version": str(getattr(basic, "version", "") or ""),
        "cover_path": _cover_path(song_id),
        "pool": pool,
    }

def _is_new(song_id: str) -> bool:
    music = mai.total_list.by_id(str(song_id))
    return bool(music and _music_is_new(music))


async def fetch_snapshot(qqid: int) -> dict:
    user = await get_user_b50(qqid=qqid)
    charts = getattr(user, "charts", None)
    b35 = [_chart(item, pool="old") for item in (getattr(charts, "sd", None) or [])]
    b15 = [_chart(item, pool="new") for item in (getattr(charts, "dx", None) or [])]
    all_charts = list(b35) + list(b15)
    try:
        _, records = await get_user_records(qqid=qqid)
    except Exception:
        records = []
    seen = {(x["song_id"], x["level_index"]) for x in all_charts}
    for record in records or []:
        item = _chart(record)
        key = (item["song_id"], item["level_index"])
        if key not in seen:
            item["pool"] = "new" if _is_new(item["song_id"]) else "old"
            all_charts.append(item)
            seen.add(key)
    return {
        "nickname": str(getattr(user, "nickname", "Player") or "Player"),
        "rating": int(getattr(user, "rating", 0) or 0),
        "b35": b35,
        "b15": b15,
        "all_charts": all_charts,
    }
