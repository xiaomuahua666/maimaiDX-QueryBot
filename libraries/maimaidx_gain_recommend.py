from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from ..config import footer_generated, log
from .maimaidx_best_50 import _is_latest_version, computeRa
from .maimaidx_data_storage import DailySnapshot, ScoreRecord, data_storage
from .maimaidx_music import mai


@dataclass
class _Ability:
    avg_gain: float
    improve_rate: float


@dataclass
class _Recommend:
    song_id: int
    title: str
    level: str
    ds: float
    fit_diff: float
    achv_now: float
    achv_target: float
    need: float
    ra_now: int
    ra_target: int
    net_gain: int
    probability: float
    score: float
    zone: str


def _song_key(song_id: int, level_index: int) -> Tuple[int, int]:
    return int(song_id), int(level_index)


def _level_bucket(ds: float) -> str:
    if ds < 12:
        return "<12"
    if ds < 13:
        return "12.x"
    if ds < 14:
        return "13.x"
    if ds < 14.7:
        return "14.x"
    return "14.7+"


def _build_b50(records: List[ScoreRecord]) -> tuple[list[ScoreRecord], list[ScoreRecord], dict[tuple[int, int], ScoreRecord]]:
    records_sorted = sorted(records, key=lambda x: int(x.ra), reverse=True)
    b15 = sorted([r for r in records_sorted if _is_latest_version(r)], key=lambda x: int(x.ra), reverse=True)[:15]
    b35 = sorted([r for r in records_sorted if not _is_latest_version(r)], key=lambda x: int(x.ra), reverse=True)[:35]
    b50_map = {_song_key(r.song_id, r.level_index): r for r in (b35 + b15)}
    return b35, b15, b50_map


def _default_abilities() -> Dict[str, _Ability]:
    return {b: _Ability(avg_gain=0.05, improve_rate=0.35) for b in ['<12', '12.x', '13.x', '14.x', '14.7+']}


def _load_recent_snapshots(qqid: int, limit: int = 20) -> List[DailySnapshot]:
    metas = data_storage.list_snapshots(qqid, limit=limit)
    out: List[DailySnapshot] = []
    for m in reversed(metas):  # 时间正序
        sid = m.get("snapshot_id", "")
        snap = data_storage.load_snapshot_by_id(qqid, sid)
        if snap:
            out.append(snap)
    return out


def _calc_user_ability(snaps: List[DailySnapshot]) -> Dict[str, _Ability]:
    attempts: Dict[str, int] = {}
    improved_cnt: Dict[str, int] = {}
    gain_sum: Dict[str, float] = {}

    for i in range(1, len(snaps)):
        prev = {_song_key(r.song_id, r.level_index): r for r in snaps[i - 1].records}
        curr = {_song_key(r.song_id, r.level_index): r for r in snaps[i].records}
        for key, old_r in prev.items():
            new_r = curr.get(key)
            if not new_r:
                continue
            ds = float(new_r.ds or old_r.ds or 0.0)
            b = _level_bucket(ds)
            attempts[b] = attempts.get(b, 0) + 1
            d = float(new_r.achievements) - float(old_r.achievements)
            if d > 0.0001:
                improved_cnt[b] = improved_cnt.get(b, 0) + 1
                gain_sum[b] = gain_sum.get(b, 0.0) + d

    abilities: Dict[str, _Ability] = {}
    for b in ["<12", "12.x", "13.x", "14.x", "14.7+"]:
        total = attempts.get(b, 0)
        inc_n = improved_cnt.get(b, 0)
        avg_gain = (gain_sum.get(b, 0.0) / inc_n) if inc_n > 0 else 0.03
        improve_rate = (inc_n / total) if total > 0 else 0.35
        # 稳定下限，避免样本过小时全零
        avg_gain = max(0.02, min(0.2, avg_gain))
        improve_rate = max(0.2, min(0.85, improve_rate))
        abilities[b] = _Ability(avg_gain=avg_gain, improve_rate=improve_rate)
    return abilities


def _fit_diff(song_id: int, level_index: int, fallback_ds: float) -> float:
    try:
        music = mai.total_list.by_id(str(song_id))
        if music and music.stats and level_index < len(music.stats) and music.stats[level_index]:
            f = music.stats[level_index].fit_diff
            if f is not None:
                return float(f)
    except Exception:
        pass
    return float(fallback_ds)


def _pick_zone(prob: float, net_gain: int) -> str:
    if prob >= 0.65 and net_gain >= 4:
        return "稳赚"
    if prob >= 0.45:
        return "均衡"
    return "冲刺"


async def _compute_today_gain(qqid: int, top_n: int = 12) -> str:
    yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
    yesterday_snap = data_storage.load_daily_snapshot(qqid, yesterday)
    if not yesterday_snap:
        return (
            '昨日无存档，无法生成吃分推荐。\n'
            '请确保已开启数据存储，并等待每日凌晨自动存档或手动存档至少一天。'
        )

    snaps = _load_recent_snapshots(qqid, limit=20)
    if len(snaps) >= 2:
        abilities = _calc_user_ability(snaps)
    else:
        today_snap = data_storage.load_daily_snapshot(qqid, datetime.now().strftime('%Y-%m-%d'))
        if today_snap:
            abilities = _calc_user_ability([yesterday_snap, today_snap])
        else:
            abilities = _default_abilities()

    from .maimaidx_datasource import get_user_records

    _ui, dev_records = await get_user_records(qqid=qqid)
    records = list(dev_records or [])
    from .maimaidx_best_50 import filter_utage_records
    records = filter_utage_records(records)
    if not records:
        return '未读取到全量成绩，无法推荐。'

    score_records = [
        ScoreRecord(
            song_id=int(r.song_id),
            title=r.title,
            level=r.level,
            level_index=int(r.level_index),
            ds=float(r.ds),
            achievements=float(r.achievements),
            rate=r.rate,
            ra=int(r.ra),
            fc=getattr(r, 'fc', None),
            fs=getattr(r, 'fs', None),
            dxScore=getattr(r, 'dxScore', 0),
        )
        for r in records
    ]
    b35, b15, b50_map = _build_b50(score_records)
    b35_tail = int(b35[-1].ra) if b35 else 0
    b15_tail = int(b15[-1].ra) if b15 else 0

    targets = [97.0, 98.0, 99.0, 99.5, 100.0, 100.5]
    picks: List[_Recommend] = []

    for r in records:
        achv_now = float(r.achievements)
        if achv_now >= 100.5:
            continue
        ds = float(r.ds)
        fit = _fit_diff(int(r.song_id), int(r.level_index), ds)
        bucket = _level_bucket(ds)
        abi = abilities.get(bucket, _Ability(avg_gain=0.05, improve_rate=0.35))

        ease = 1.0 + max(-0.4, min(0.4, (ds - fit) * 0.4))
        expected_gain = abi.avg_gain * ease

        best: Optional[_Recommend] = None
        key = _song_key(int(r.song_id), int(r.level_index))
        in_b50 = key in b50_map
        ra_now = int(r.ra)

        for t in targets:
            if t <= achv_now + 1e-9:
                continue
            need = t - achv_now
            if need > 0.45:
                continue
            ra_target = int(computeRa(ds, t))
            base = ra_now if in_b50 else (b15_tail if _is_latest_version(r) else b35_tail)
            net = max(0, ra_target - base)
            if net <= 0:
                continue
            ratio = expected_gain / max(need, 1e-6)
            prob = max(0.1, min(0.95, abi.improve_rate * ratio))
            score = net * prob
            cand = _Recommend(
                song_id=int(r.song_id),
                title=r.title,
                level=r.level,
                ds=ds,
                fit_diff=fit,
                achv_now=achv_now,
                achv_target=t,
                need=need,
                ra_now=ra_now,
                ra_target=ra_target,
                net_gain=net,
                probability=prob,
                score=score,
                zone=_pick_zone(prob, net),
            )
            if best is None or cand.score > best.score:
                best = cand

        if best:
            picks.append(best)

    if not picks:
        return "今天没有明显吃分候选（可能是当前 B50 已很满，或可提升空间较小）。", None

    picks.sort(key=lambda x: x.score, reverse=True)
    top = picks[: max(1, min(20, top_n))]

    groups: Dict[str, List[dict]] = {"稳赚": [], "均衡": [], "冲刺": []}
    for p in top:
        if len(groups[p.zone]) >= 4:
            continue
        groups[p.zone].append({
            'song_id': p.song_id,
            'title': p.title,
            'level': p.level,
            'ds': p.ds,
            'fit_diff': p.fit_diff,
            'achv_now': p.achv_now,
            'achv_target': p.achv_target,
            'need': p.need,
            'net_gain': p.net_gain,
            'probability': p.probability,
        })

    summary = [
        f'昨日存档 {yesterday}',
        f'能力样本 {max(len(snaps), 1)} 天',
        f'候选 {len(picks)} 首',
    ]
    log.debug(f"[today_gain] qq={qqid} picks={len(picks)} top={len(top)}")
    return summary, groups


async def generate_today_gain_recommendation(qqid: int, top_n: int = 12) -> str:
    """文字版吃分推荐（保留兼容）。"""
    result = await _compute_today_gain(qqid, top_n)
    if isinstance(result, str):
        return result
    summary, groups = result
    lines = ['今日吃分推荐（对比昨日存档 + 实时成绩，拟合难度 + B35/B15 门槛净收益）',
             '基准：' + ' · '.join(summary)]
    for zone in ["稳赚", "均衡", "冲刺"]:
        arr = groups[zone]
        if not arr:
            continue
        lines.append(f"\n【{zone}】")
        for i, p in enumerate(arr, 1):
            lines.append(
                f"{i}. {p['title']} [{p['level']}] "
                f"{p['achv_now']:.4f}%->{p['achv_target']:.1f}% "
                f"净增{p['net_gain']:+d}ra "
                f"成功率{p['probability']*100:.0f}% "
                f"(拟合{p['fit_diff']:.2f}/定数{p['ds']:.2f})"
            )
    return "\n".join(lines) + f"\n\n{footer_generated()}"


async def generate_today_gain_recommendation_image(qqid: int, top_n: int = 12):
    """成功的推荐渲染成现代卡片图片，缺少存档/成绩等错误仍返回文字提示。"""
    result = await _compute_today_gain(qqid, top_n)
    if isinstance(result, str):
        return result
    summary, groups = result
    from nonebot.adapters.onebot.v11 import MessageSegment
    from PIL import Image
    from .maimaidx_leaderboard_image import render_gain_recommendation
    from .image import image_to_base64

    # 用户昵称 + 近 14 天 rating 趋势 + 当前 rating
    snaps = _load_recent_snapshots(qqid, limit=14)
    trend = [(s.date, int(s.rating)) for s in snaps if s.rating is not None]
    current_rating = int(trend[-1][1]) if trend else None
    user_name = (snaps[-1].nickname if snaps else '') or str(qqid)
    bio = render_gain_recommendation(
        groups, summary, user_name=user_name,
        rating_trend=trend, current_rating=current_rating,
    )
    return MessageSegment.image(image_to_base64(Image.open(bio)))
