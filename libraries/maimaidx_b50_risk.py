"""B50 风险预警：结合存档历史分析地板、下滑、寸止/锁血等挤出风险。"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Dict, List, Tuple, Union

from nonebot.adapters.onebot.v11 import MessageSegment
from PIL import Image

from ..config import achievementList
from .maimaidx_best_50 import _is_latest_version
from .maimaidx_data_storage import DailySnapshot, ScoreRecord, data_storage
from .maimaidx_image_executor import run_image_cpu
from .maimaidx_risk_image import render_risk_report


@dataclass
class _RiskItem:
    title: str
    level: str
    ra: int
    achv: float
    zone: str
    score: int
    reasons: List[str]
    song_id: int = 0
    level_index: int = 3


def _song_key(r: ScoreRecord) -> Tuple[int, int]:
    return int(r.song_id), int(r.level_index)


def _build_b50(records: List[ScoreRecord]) -> Tuple[List[ScoreRecord], List[ScoreRecord], List[ScoreRecord]]:
    # 不计宴会场谱面（song_id >= 100000）
    valid = [r for r in records if int(getattr(r, 'song_id', 0) or 0) < 100000]
    sorted_records = sorted(valid, key=lambda x: int(x.ra), reverse=True)
    b15 = sorted([r for r in sorted_records if _is_latest_version(r)], key=lambda x: int(x.ra), reverse=True)[:15]
    b35 = sorted([r for r in sorted_records if not _is_latest_version(r)], key=lambda x: int(x.ra), reverse=True)[:35]
    return b35, b15, b35 + b15


def _is_sun(a: float) -> bool:
    for x in achievementList:
        if (x - 0.1) < a <= x:
            return True
    return False


def _is_lock(a: float) -> bool:
    for x in achievementList:
        step = 0.01 if x != int(x) else 0.1
        if x <= a < (x + step):
            return True
    return False


def _zone_label(r: ScoreRecord) -> str:
    return 'B15' if _is_latest_version(r) else 'B35'


def _analyze_risks(snaps: List[DailySnapshot]) -> Tuple[str, List[_RiskItem], int]:
    latest = snaps[-1]
    b35, b15, b50 = _build_b50(latest.records)
    if not b50:
        return latest.nickname or str(latest.qqid), [], 0

    b35_floor = int(b35[-1].ra) if b35 else 0
    b15_floor = int(b15[-1].ra) if b15 else 0

    history: List[Dict[Tuple[int, int], ScoreRecord]] = []
    for s in snaps:
        _, _, cur_b50 = _build_b50(s.records)
        history.append({_song_key(r): r for r in cur_b50})

    items: List[_RiskItem] = []
    for r in b50:
        key = _song_key(r)
        zone = _zone_label(r)
        floor = b15_floor if zone == 'B15' else b35_floor
        ra = int(r.ra)
        achv = float(r.achievements)
        reasons: List[str] = []
        score = 0

        if floor and ra <= floor:
            reasons.append('地板位')
            score += 40
        elif floor and ra - floor <= 3:
            reasons.append(f'贴地板(差{ra - floor})')
            score += 28
        elif floor and ra - floor <= 8:
            reasons.append(f'近地板(差{ra - floor})')
            score += 14

        if _is_sun(achv):
            reasons.append('寸止')
            score += 22
        if _is_lock(achv):
            reasons.append('锁血')
            score += 18

        if len(history) >= 2:
            prev_ra = int(history[-2].get(key, r).ra)
            if ra < prev_ra:
                reasons.append(f'较上次-{prev_ra - ra}ra')
                score += 20
        if len(history) >= 3:
            oldest_ra = int(history[0].get(key, r).ra)
            if ra < oldest_ra - 2:
                reasons.append(f'较早期-{oldest_ra - ra}ra')
                score += 12

        if not reasons:
            continue
        items.append(
            _RiskItem(
                title=r.title,
                level=r.level,
                ra=ra,
                achv=achv,
                zone=zone,
                score=score,
                reasons=reasons,
                song_id=int(getattr(r, 'song_id', 0) or 0),
                level_index=int(getattr(r, 'level_index', 3) or 3),
            )
        )

    items.sort(key=lambda x: (-x.score, x.ra))
    return latest.nickname or str(latest.qqid), items[:15], len(b50)


def _draw_risk_report(nickname: str, items: List[_RiskItem], snap_days: int,
                      b50_total: int = 50, user_name: str = "Milk"):
    """兼容旧入口：把 _RiskItem 转成 dict 后交给现代化渲染器。"""
    payload = [
        {
            "title": it.title, "level": it.level, "level_index": it.level_index,
            "song_id": it.song_id, "ra": it.ra, "achv": it.achv,
            "zone": it.zone, "score": it.score, "reasons": it.reasons,
        }
        for it in items
    ]
    from io import BytesIO
    bio = render_risk_report(nickname, snap_days, payload,
                             b50_total=b50_total, user_name=user_name)
    return Image.open(BytesIO(bio.read())).convert("RGBA")


async def generate_b50_risk_warning(qqid: int) -> Union[str, MessageSegment]:
    if not data_storage.is_enabled(qqid):
        return '你尚未开启数据存储，请先发送「开启存储数据」后再使用 B50 风险预警。'

    metas = data_storage.list_snapshots(qqid, limit=30)
    if len(metas) < 2:
        return (
            '存档数量不足（至少需要 2 份快照）。\n'
            '请等待每日自动存档，或发送「立即存储数据」积累历史后再试。'
        )

    snaps: List[DailySnapshot] = []
    for m in reversed(metas):
        sid = m.get('snapshot_id', '')
        snap = data_storage.load_snapshot_by_id(qqid, sid) if sid else None
        if snap:
            snaps.append(snap)

    if len(snaps) < 2:
        return '无法读取有效存档，请稍后再试。'

    nickname, items, b50_total = _analyze_risks(snaps)
    payload = [
        {
            "title": it.title, "level": it.level, "level_index": it.level_index,
            "song_id": it.song_id, "ra": it.ra, "achv": it.achv,
            "zone": it.zone, "score": it.score, "reasons": it.reasons,
        }
        for it in items
    ]
    bio = await run_image_cpu(
        render_risk_report, nickname, len(snaps), payload,
        b50_total=b50_total, user_name=nickname,
    )
    # render_risk_report 已返回编码好的 PNG BytesIO，直接转 base64，避免把 BytesIO
    # 当成 PIL Image 调用 .save 导致 AttributeError。
    return MessageSegment.image(
        'base64://' + base64.b64encode(bio.getvalue()).decode()
    )
