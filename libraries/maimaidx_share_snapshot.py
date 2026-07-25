"""脱敏贡献快照：查询/缓存已有全量成绩时静默写入 user_scores。

与「开启存储数据」个人功能分离：
- 个人存储：enabled_users，供周报/牌子统计等
- 贡献快照：data_share 默认开启，opt-out 后不写；导出脚本会再过滤一次
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional, Sequence

from loguru import logger as log

from .maimaidx_data_storage import DailySnapshot, ScoreRecord, data_storage

# 与公开导出默认门槛对齐
MIN_SHARE_RECORDS = 30


def _data_share():
    from .maimaidx_data_share import data_share

    return data_share


def playinfo_to_score_records(records: Sequence[Any]) -> List[ScoreRecord]:
    out: List[ScoreRecord] = []
    for r in records or []:
        try:
            out.append(
                ScoreRecord(
                    song_id=int(r.song_id),
                    title=str(getattr(r, "title", "") or ""),
                    level=str(getattr(r, "level", "") or ""),
                    level_index=int(r.level_index),
                    ds=float(r.ds),
                    achievements=float(r.achievements),
                    rate=str(getattr(r, "rate", "") or ""),
                    ra=int(r.ra),
                    fc=getattr(r, "fc", None),
                    fs=getattr(r, "fs", None),
                    dxScore=int(getattr(r, "dxScore", 0) or 0),
                )
            )
        except Exception:
            continue
    return out


def build_daily_snapshot(
    qqid: int,
    userinfo: Any,
    records: Sequence[Any],
    *,
    source: str,
    target_date: Optional[str] = None,
) -> Optional[DailySnapshot]:
    score_records = playinfo_to_score_records(records)
    if len(score_records) < MIN_SHARE_RECORDS:
        return None
    nickname = str(
        getattr(userinfo, "nickname", None)
        or getattr(userinfo, "username", None)
        or qqid
    )
    rating = int(getattr(userinfo, "rating", 0) or 0)
    return DailySnapshot(
        date=target_date or datetime.now().strftime("%Y-%m-%d"),
        qqid=int(qqid),
        nickname=nickname,
        rating=rating,
        records=score_records,
        record_count=len(score_records),
        source=source,
    )


def _today_snapshot_is_good_enough(qqid: int, candidate: DailySnapshot) -> bool:
    """同日已有不低于候选的快照则跳过，减少重复写盘。"""
    existing = data_storage.load_daily_snapshot(int(qqid), candidate.date)
    if not existing:
        return False
    try:
        if int(existing.record_count or 0) >= int(candidate.record_count or 0) and int(
            existing.rating or 0
        ) >= int(candidate.rating or 0):
            return True
    except Exception:
        return False
    return False


def maybe_save_share_snapshot(
    qqid: Optional[int],
    userinfo: Any,
    records: Sequence[Any],
    *,
    source: str = "share_query",
    target_date: Optional[str] = None,
    force: bool = False,
) -> bool:
    """若用户未 opt-out 且成绩足够厚，静默写入贡献快照。"""
    if not qqid:
        return False
    try:
        qq = int(qqid)
    except (TypeError, ValueError):
        return False
    if not _data_share().is_sharing_enabled(qq):
        return False

    snap = build_daily_snapshot(
        qq, userinfo, records, source=source, target_date=target_date
    )
    if snap is None:
        return False
    if not force and _today_snapshot_is_good_enough(qq, snap):
        return False
    try:
        ok = data_storage.save_daily_snapshot(snap)
        if ok:
            log.debug(
                f"[ShareSnapshot] qq={qq} source={source} "
                f"records={snap.record_count} rating={snap.rating}"
            )
        return bool(ok)
    except Exception as e:
        log.warning(f"[ShareSnapshot] 写入失败 qq={qq}: {e}")
        return False
