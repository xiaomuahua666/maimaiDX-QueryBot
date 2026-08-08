"""弱项处方单：根据 B50 底力短板标签，推荐贴合当前水平的练习曲目。"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Tuple, Union

from nonebot.adapters.onebot.v11 import MessageSegment
from PIL import Image, ImageDraw

from ..config import SIYUAN, footer_generated
from .image import DrawText, draw_centered_design_footer, generate_frosted_card, image_to_base64
from .maimaidx_api_data import maiApi
from .maimaidx_best_50 import filter_utage_records
from .maimaidx_error import UserDisabledQueryError, UserNotFoundError, UserNotExistsError
from .maimaidx_music import mai
from .maimaidx_music_info import get_b50_tag_stats, get_chart_tags_by_group
from .maimaidx_tag_analysis import CONFIG_TAGS_ORDER
from .maimaidx_wmc_api import (
    WmcAPI,
    diff_value_for_wmc,
    kind_for_wmc,
    make_chart_key,
    resolve_wmc_base_url,
    song_id_for_wmc,
)

ACCENT = (124, 129, 255, 255)
TEXT = (45, 50, 95, 255)
SUBTEXT = (90, 95, 140, 255)
MUTED = (120, 126, 145, 255)
TAG_FILL = (255, 200, 120, 220)
TAG_STROKE = (230, 150, 60, 255)

_SSS_THRESHOLD = 97.0
_MAX_PICKS = 12
# 推荐达成率区间：已会打、离 SSS 不远
_ACHV_SWEET_MIN = 90.0
_ACHV_SWEET_MAX = _SSS_THRESHOLD
# 定数允许偏离 B50 中位的天花板
_DS_BAND = 0.8


@dataclass
class _Pick:
    title: str
    level: str
    ds: float
    achv: float
    ra: int
    tags: List[str]
    score: float


def _level_bucket(ds: float) -> str:
    if ds < 12:
        return '<12'
    if ds < 13:
        return '12.x'
    if ds < 14:
        return '13.x'
    if ds < 14.7:
        return '14.x'
    return '14.7+'


def _identify_weak_tags(stats: Dict[str, Dict[str, int]], top_n: int = 3) -> List[Tuple[str, int]]:
    counts = stats.get('配置') or {}
    ranked = sorted(
        ((t, counts.get(t, 0)) for t in CONFIG_TAGS_ORDER),
        key=lambda x: (x[1], CONFIG_TAGS_ORDER.index(x[0])),
    )
    return ranked[:top_n]


def _b50_ds_ref(userinfo) -> Tuple[float, float, float]:
    """B50 定数参考：中位、下限、上限。"""
    ds_list: List[float] = []
    for chart_list in (
        getattr(userinfo.charts, 'sd', None) or [],
        getattr(userinfo.charts, 'dx', None) or [],
    ):
        for c in chart_list:
            ds_list.append(float(c.ds))
    if not ds_list:
        return 13.5, 12.5, 14.5
    ds_list.sort()
    mid = statistics.median(ds_list)
    return float(mid), float(ds_list[0]), float(ds_list[-1])


def _ability_profile(records) -> Dict[str, float]:
    """各定数段已游玩谱面的中位达成率。"""
    buckets: Dict[str, List[float]] = defaultdict(list)
    for r in records:
        buckets[_level_bucket(float(r.ds))].append(float(r.achievements))
    return {b: float(statistics.median(v)) for b, v in buckets.items() if v}


def _achv_floor_for(ds: float, ability: Dict[str, float]) -> float:
    """该定数段内视为「会打」的最低达成率。"""
    bucket_med = ability.get(_level_bucket(ds))
    if bucket_med is None:
        return 85.0
    # 低于段位中位太多，多半是偶发低分，不作为练习推荐
    return max(80.0, bucket_med - 10.0)


def _score_pick(
    *,
    matched: List[str],
    weak_count: int,
    achv: float,
    ds: float,
    ref_ds: float,
    in_b50: bool,
) -> float:
    tag_part = (len(matched) / max(weak_count, 1)) * 35.0
    if achv >= _ACHV_SWEET_MIN:
        proximity = min(1.0, (achv - _ACHV_SWEET_MIN) / (_ACHV_SWEET_MAX - _ACHV_SWEET_MIN))
    else:
        proximity = max(0.0, (achv - 80.0) / (_ACHV_SWEET_MIN - 80.0)) * 0.35
    ds_gap = abs(ds - ref_ds)
    ds_part = max(0.0, 1.0 - ds_gap / _DS_BAND) * 30.0
    b50_part = 8.0 if in_b50 else 0.0
    # 定数明显高于 B50 中心时额外惩罚
    over_penalty = max(0.0, ds - ref_ds - _DS_BAND) * 25.0
    return tag_part + proximity * 30.0 + ds_part + b50_part - over_penalty


def _short_title(title: str, n: int = 20) -> str:
    t = title.strip()
    return t if len(t) <= n else t[: n - 1] + '…'


def _wmc_config_tags(tags_data: dict) -> List[str]:
    """从 v.wmc.pub /tags 响应中提取配置标签（radarTags）。"""
    return [str(t.get('label', '')) for t in (tags_data or {}).get('radarTags', []) if t.get('label')]


def _draw_prescription(
    nickname: str,
    weak_tags: List[Tuple[str, int]],
    picks: List[_Pick],
    ref_ds: float,
) -> Image.Image:
    width = 920
    row_h = 34
    tag_panel_h = 120
    list_h = max(1, len(picks)) * row_h + 56
    footer_h = 40
    height = 88 + tag_panel_h + 24 + list_h + footer_h

    im = Image.new('RGBA', (width, height), (245, 247, 255, 255))
    im = generate_frosted_card(im, (24, 72, width - 24, 72 + tag_panel_h), alpha=0.52)
    im = generate_frosted_card(im, (24, 72 + tag_panel_h + 24, width - 24, height - footer_h), alpha=0.52)

    dr = ImageDraw.Draw(im)
    dt = DrawText(dr, SIYUAN)
    dt.draw(32, 28, 30, '弱项处方单', ACCENT, 'lt', 2, (255, 255, 255, 240))
    dt.draw(
        32, 62, 18,
        f'{nickname}  ·  已游玩且定数≈{ref_ds:.1f}、接近SSS的短板练习曲',
        SUBTEXT, 'lt', 1, (255, 255, 255, 220),
    )

    y = 92
    dt.draw(44, y, 22, '短板标签', ACCENT, 'lt', 2, (255, 255, 255, 230))
    x = 44
    y += 36
    for tag, cnt in weak_tags:
        label = f'{tag}（B50×{cnt}）'
        tw = int(dt.get_box(label, 16)[2]) + 28
        dr.rounded_rectangle([x, y, x + tw, y + 30], radius=10, fill=TAG_FILL, outline=TAG_STROKE, width=2)
        dt.draw(x + tw // 2, y + 15, 16, label, TEXT, 'mm', 1, (255, 255, 255, 240))
        x += tw + 12

    y = 72 + tag_panel_h + 44
    dt.draw(44, y, 22, '推荐练习', ACCENT, 'lt', 2, (255, 255, 255, 230))
    y += 36
    if not picks:
        dt.draw(
            48, y, 18,
            '暂无贴合水平的匹配曲目（可提高相近定数成绩后再试）',
            MUTED, 'lt',
        )
    else:
        for i, p in enumerate(picks, 1):
            tag_txt = '、'.join(p.tags[:3])
            line = (
                f'{i}. {_short_title(p.title)} [{p.level}]  {p.achv:.4f}%  '
                f'定数{p.ds:.1f}  ra{p.ra}  标签:{tag_txt}'
            )
            dt.draw(48, y, 16, line, TEXT, 'lt', 1, (255, 255, 255, 220))
            y += row_h

    draw_centered_design_footer(
        im, dt, footer_generated(),
        color=SUBTEXT,
        margin_x=48,
        start_font_size=14,
        min_font_size=10,
        bottom_gap=8,
    )
    return im


async def generate_weakness_prescription(qqid: int) -> Union[str, MessageSegment]:
    import asyncio
    from ..config import maiconfig
    from .maimaidx_datasource import get_user_records

    try:
        userinfo, records = await get_user_records(qqid=qqid)
    except (UserNotFoundError, UserNotExistsError, UserDisabledQueryError) as e:
        return str(e)

    # 预拉取 v.wmc.pub 标签（B50 谱面）
    wmc_cache = {}
    api = None
    wmc_key = maiconfig.wmc_api_key
    if wmc_key:
        api = WmcAPI(resolve_wmc_base_url(maiconfig), wmc_key)
        tasks = []
        task_keys = []
        for chart_list in (getattr(userinfo.charts, 'sd', None) or [], getattr(userinfo.charts, 'dx', None) or []):
            if not chart_list:
                continue
            for chart in chart_list:
                sid = getattr(chart, 'song_id', None)
                li = getattr(chart, 'level_index', 0)
                if sid is None:
                    continue
                music = mai.total_list.by_id(str(sid))
                if not music:
                    continue
                wmc_sid = song_id_for_wmc(music)
                kind = kind_for_wmc(music)
                diff_val = diff_value_for_wmc(li)
                key = make_chart_key(wmc_sid, kind, diff_val)
                tasks.append(api.get_tags(key, radar_threshold=30, feature_threshold=0.3))
                task_keys.append((wmc_sid, kind, diff_val))
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for k, r in zip(task_keys, results):
                if isinstance(r, dict) and r.get("tags"):
                    wmc_cache[k] = r["tags"]

    stats = get_b50_tag_stats(userinfo, wmc_tags_cache=wmc_cache or None)
    if not any(stats.get('配置', {}).values()):
        return (
            '无法生成弱项处方：B50 谱面暂无标签数据。\n'
            '可能原因：未配置 WMC_API_KEY（v.wmc.pub 谱面印象），或当前 B50 曲目尚未被收录标签。'
        )

    weak_tags = _identify_weak_tags(stats)
    weak_set = {t for t, _ in weak_tags}
    ref_ds, _, _ = _b50_ds_ref(userinfo)
    ds_min = max(0.0, ref_ds - 1.2)
    ds_max = ref_ds + _DS_BAND

    records = filter_utage_records(records or [])
    if not records:
        return '未读取到全量成绩，无法推荐练习曲目（需开发者 Token）。'

    ability = _ability_profile(records)

    b50_keys = set()
    for chart_list in (
        getattr(userinfo.charts, 'sd', None) or [],
        getattr(userinfo.charts, 'dx', None) or [],
    ):
        for c in chart_list:
            b50_keys.add((int(c.song_id), int(c.level_index)))

    # 先筛出定数/达成率达标的候选，再批量补拉它们的 v.wmc.pub 标签，
    # 避免本地标签 JSON 覆盖不全导致处方无曲可推。
    candidate_records = []
    seen_chart_keys = set()
    for r in records:
        achv = float(r.achievements)
        if achv >= _SSS_THRESHOLD:
            continue
        ds = float(r.ds)
        if ds < ds_min or ds > ds_max:
            continue
        if achv < _achv_floor_for(ds, ability):
            continue
        music = mai.total_list.by_id(str(r.song_id))
        title = (getattr(music, 'title', None) or getattr(r, 'title', '') or '').strip()
        if not title:
            continue
        candidate_records.append((r, music, title))

    if api and candidate_records:
        tasks = []
        task_meta = []
        sem = asyncio.Semaphore(16)

        async def _fetch_tags(cache_key: tuple, chart_key: str):
            async with sem:
                return await api.get_tags(chart_key, radar_threshold=30, feature_threshold=0.3)

        for r, music, _title in candidate_records:
            wmc_sid = song_id_for_wmc(music)
            kind = kind_for_wmc(music)
            diff_val = diff_value_for_wmc(int(r.level_index))
            cache_key = (wmc_sid, kind, diff_val)
            if cache_key in wmc_cache or cache_key in seen_chart_keys:
                continue
            seen_chart_keys.add(cache_key)
            tasks.append(_fetch_tags(cache_key, make_chart_key(wmc_sid, kind, diff_val)))
            task_meta.append(cache_key)
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for k, r in zip(task_meta, results):
                if isinstance(r, dict) and r.get("tags"):
                    wmc_cache[k] = r["tags"]

    picks: List[_Pick] = []
    for r, music, title in candidate_records:
        achv = float(r.achievements)
        ds = float(r.ds)
        wmc_sid = song_id_for_wmc(music)
        kind = kind_for_wmc(music)
        diff_val = diff_value_for_wmc(int(r.level_index))
        tags_data = wmc_cache.get((wmc_sid, kind, diff_val))
        if tags_data:
            cfg_tags = _wmc_config_tags(tags_data)
        else:
            groups = get_chart_tags_by_group(title, int(r.level_index))
            cfg_tags = groups.get('配置') or []
        matched = [t for t in cfg_tags if t in weak_set]
        if not matched:
            continue

        in_b50 = (int(r.song_id), int(r.level_index)) in b50_keys
        score = _score_pick(
            matched=matched,
            weak_count=len(weak_set),
            achv=achv,
            ds=ds,
            ref_ds=ref_ds,
            in_b50=in_b50,
        )
        if score <= 0:
            continue
        picks.append(
            _Pick(
                title=title,
                level=r.level,
                ds=ds,
                achv=achv,
                ra=int(r.ra),
                tags=matched,
                score=score,
            )
        )

    picks.sort(key=lambda x: (-x.score, -x.achv, abs(x.ds - ref_ds)))
    picks = picks[:_MAX_PICKS]

    nickname = userinfo.nickname or userinfo.username or '未知'
    im = _draw_prescription(nickname, weak_tags, picks, ref_ds)
    return MessageSegment.image(image_to_base64(im))
