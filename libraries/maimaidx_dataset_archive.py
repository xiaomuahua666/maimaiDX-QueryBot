"""上传 B50 / 扫码后自动落盘，用于拓展公开数据集。

与「开启存储数据」个人功能分离：
- 默认按 data_share（可 opt-out）写入 user_scores
- 已开启个人存储时同样落盘（含成绩偏少的情况）
- 查分器拉取失败时，回退用机台 PC 全量成绩构建快照
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Iterable, List, Optional, Sequence, Set

from loguru import logger as log

from .maimaidx_data_storage import DailySnapshot, ScoreRecord, data_storage
from .maimaidx_share_snapshot import (
    MIN_SHARE_RECORDS,
    build_daily_snapshot,
    maybe_save_share_snapshot,
    playinfo_to_score_records,
)

_archive_tasks: Set[asyncio.Task] = set()


def collect_archive_qqids(*candidates: object) -> List[int]:
    seen: set[int] = set()
    out: List[int] = []
    for raw in candidates:
        if raw is None:
            continue
        try:
            qq = int(raw)
        except (TypeError, ValueError):
            continue
        if not qq or qq in seen:
            continue
        seen.add(qq)
        out.append(qq)
    return out


def _sharing_or_enabled(qqid: int) -> bool:
    from .maimaidx_data_share import data_share

    return data_storage.is_enabled(qqid) or data_share.is_sharing_enabled(qqid)


def _compute_ra(ds: float, achievement: float) -> tuple[int, str]:
    """轻量 RA 计算，避免导入 maimaidx_best_50（其依赖 NoneBot）。"""
    if achievement < 50:
        base_ra, rate = 7.0, "D"
    elif achievement < 60:
        base_ra, rate = 8.0, "C"
    elif achievement < 70:
        base_ra, rate = 9.6, "B"
    elif achievement < 75:
        base_ra, rate = 11.2, "BB"
    elif achievement < 80:
        base_ra, rate = 12.0, "BBB"
    elif achievement < 90:
        base_ra, rate = 13.6, "A"
    elif achievement < 94:
        base_ra, rate = 15.2, "AA"
    elif achievement < 97:
        base_ra, rate = 16.8, "AAA"
    elif achievement < 98:
        base_ra, rate = 20.0, "S"
    elif achievement < 99:
        base_ra, rate = 20.3, "Sp"
    elif achievement < 99.5:
        base_ra, rate = 20.8, "SS"
    elif achievement < 100:
        base_ra, rate = 21.1, "SSp"
    elif achievement < 100.5:
        base_ra, rate = 21.6, "SSS"
    else:
        base_ra, rate = 22.4, "SSSp"
    ratio = 1.005 if achievement >= 100.5 else achievement / 100
    return int(ds * ratio * base_ra), rate


def _preferred_prober_sources(*, fish: bool = False, lxns: bool = False) -> List[Optional[str]]:
    sources: List[Optional[str]] = []
    if fish:
        sources.append("divingfish")
    if lxns:
        sources.append("lxns")
    # 再按用户偏好 / 默认兜底试一次
    sources.append(None)
    # 去重且保序
    seen: set[object] = set()
    out: List[Optional[str]] = []
    for s in sources:
        key = s or ""
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def maybe_archive_from_playcount(
    qqid: int, *, source: str = "share_qrcode_pc"
) -> bool:
    """用机台 PC 全量成绩写入贡献快照（查分器不可用时的兜底）。"""
    qqid = int(qqid)
    if not _sharing_or_enabled(qqid):
        return False
    try:
        from .maimaidx_playcount_db import PlayCountDatabase

        pc_db = PlayCountDatabase()
    except Exception as exc:
        log.warning(f"[DatasetArchive] 无法导入 PC 模块 qq={qqid}: {exc}")
        return False

    records = pc_db.get_user_play_counts(qqid) or []
    if len(records) < MIN_SHARE_RECORDS:
        return False

    score_records: List[ScoreRecord] = []
    for r in records:
        try:
            # playcount 里 dx_rating 字段实际存的是定数 ds
            ds = float(getattr(r, "dx_rating", 0) or 0)
            ach = float(getattr(r, "achievements", 0) or 0)
            if ds > 0:
                ra, rate = _compute_ra(ds, ach)
            else:
                ra, rate = 0, str(getattr(r, "rate", "") or "")
            score_records.append(
                ScoreRecord(
                    song_id=int(r.song_id),
                    title=str(getattr(r, "title", "") or ""),
                    level=str(getattr(r, "level", "") or ""),
                    level_index=int(r.level_index),
                    ds=ds,
                    achievements=ach,
                    rate=str(rate or getattr(r, "rate", "") or ""),
                    ra=int(ra),
                    fc=getattr(r, "fc", None) or None,
                    fs=getattr(r, "fs", None) or None,
                    dxScore=int(getattr(r, "dx_score", 0) or 0),
                )
            )
        except Exception:
            continue

    if len(score_records) < MIN_SHARE_RECORDS:
        return False

    top_ra = sorted((x.ra for x in score_records), reverse=True)[:50]
    rating = int(sum(top_ra))
    nickname = str(qqid)
    try:
        from .maimaidx_account_db import account_db

        binding = account_db.get(str(qqid))
        if binding and getattr(binding, "user_name", None):
            nickname = str(binding.user_name)
    except Exception:
        pass

    snap = DailySnapshot(
        date=datetime.now().strftime("%Y-%m-%d"),
        qqid=qqid,
        nickname=nickname,
        rating=rating,
        records=score_records,
        record_count=len(score_records),
        source=source,
    )
    if data_storage.is_enabled(qqid):
        ok = data_storage.save_daily_snapshot(snap)
    else:
        # 共享贡献：同日已有更厚快照则跳过；上传/扫码场景由调用方 force 刷新查分器路径
        from .maimaidx_data_share import data_share

        if not data_share.is_sharing_enabled(qqid):
            return False
        existing = data_storage.load_daily_snapshot(qqid, snap.date)
        if existing and int(existing.record_count or 0) >= int(snap.record_count or 0):
            return False
        ok = data_storage.save_daily_snapshot(snap)
    if ok:
        log.info(
            f"[DatasetArchive] PC 兜底落盘 qq={qqid} source={source} "
            f"records={snap.record_count} rating≈{rating}"
        )
    return bool(ok)


async def archive_user_scores_for_dataset(
    qqids: Sequence[int],
    *,
    fish: bool = False,
    lxns: bool = False,
    source: str = "share_upload",
    retries: int = 3,
    retry_delay: float = 4.0,
    allow_playcount_fallback: bool = True,
) -> bool:
    """上传/扫码后尽量写入完整成绩快照。任一 qqid 成功即返回 True。"""
    from .maimaidx_datasource import get_user_records

    ids = collect_archive_qqids(*qqids)
    if not ids:
        return False

    sources = _preferred_prober_sources(fish=fish, lxns=lxns)
    any_ok = False

    for qqid in ids:
        if not _sharing_or_enabled(qqid):
            log.debug(f"[DatasetArchive] 跳过 qq={qqid}（已关闭共享且未开启个人存储）")
            continue

        saved = False
        last_err: Optional[BaseException] = None
        for force_source in sources:
            for attempt in range(max(1, int(retries))):
                try:
                    userinfo, dev_records = await get_user_records(
                        qqid=qqid,
                        force_source=force_source,
                        force_refresh=True,
                    )
                    records = list(dev_records or [])
                    if not records:
                        raise RuntimeError("empty_records")

                    if data_storage.is_enabled(qqid):
                        snap = build_daily_snapshot(
                            qqid, userinfo, records, source=source
                        )
                        if snap is None:
                            score_records = playinfo_to_score_records(records)
                            if not score_records:
                                raise RuntimeError("no_score_records")
                            snap = DailySnapshot(
                                date=datetime.now().strftime("%Y-%m-%d"),
                                qqid=qqid,
                                nickname=(
                                    getattr(userinfo, "nickname", None)
                                    or getattr(userinfo, "username", None)
                                    or str(qqid)
                                ),
                                rating=int(getattr(userinfo, "rating", 0) or 0),
                                records=score_records,
                                record_count=len(score_records),
                                source=source,
                            )
                        ok = data_storage.save_daily_snapshot(snap)
                    else:
                        ok = maybe_save_share_snapshot(
                            qqid,
                            userinfo,
                            records,
                            source=source,
                            force=True,
                        )
                    if ok:
                        log.info(
                            f"[DatasetArchive] 查分器落盘成功 qq={qqid} "
                            f"source={source} prober={force_source or 'auto'} "
                            f"records={len(records)} attempt={attempt + 1}"
                        )
                        saved = True
                        any_ok = True
                        break
                except Exception as exc:
                    last_err = exc
                    if attempt + 1 < retries:
                        await asyncio.sleep(retry_delay * (attempt + 1))
            if saved:
                break

        if saved:
            continue

        if allow_playcount_fallback:
            pc_source = (
                "share_qrcode_pc"
                if source.startswith("share_qrcode")
                else f"{source}_pc"
            )
            if maybe_archive_from_playcount(qqid, source=pc_source):
                any_ok = True
                continue

        if last_err is not None:
            log.warning(
                f"[DatasetArchive] 落盘失败 qq={qqid} source={source}: "
                f"{type(last_err).__name__}: {last_err}"
            )
        else:
            log.warning(f"[DatasetArchive] 落盘失败 qq={qqid} source={source}")

    return any_ok


def schedule_dataset_archive(
    qqids: Iterable[int],
    *,
    fish: bool = False,
    lxns: bool = False,
    source: str = "share_upload",
    delay: float = 2.0,
) -> None:
    """后台调度，不阻塞上传/扫码回执。"""
    ids = collect_archive_qqids(*qqids)
    if not ids:
        return

    async def _runner() -> None:
        if delay > 0:
            await asyncio.sleep(delay)
        await archive_user_scores_for_dataset(
            ids, fish=fish, lxns=lxns, source=source
        )

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.warning("[DatasetArchive] 无事件循环，跳过调度")
        return

    task = loop.create_task(
        _runner(), name=f"maimaidx-dataset-archive-{ids[0]}"
    )
    _archive_tasks.add(task)
    task.add_done_callback(_archive_tasks.discard)
