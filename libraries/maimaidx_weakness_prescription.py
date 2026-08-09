"""弱项处方单：根据 B50 底力短板标签，推荐贴合当前水平的练习曲目。"""

from __future__ import annotations

import asyncio
import base64
import statistics
from collections import defaultdict
from dataclasses import dataclass
from io import BytesIO
from typing import Dict, List, Tuple, Union

from nonebot.adapters.onebot.v11 import MessageSegment
from PIL import ImageDraw
from loguru import logger as log

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
from .maimaidx_game_assets import bold_font, num_font
from .maimaidx_leaderboard_image import (
    _ACCENT, _DIFF_COLORS, _GOLD, _GREEN, _MUTED, _RED,
    _TEXT, _TEXT_SOFT, _brand_mark, _card, _finalize, _footer, _make_bg,
    _draw_title, _period_chip, _truncate, image_safe_text,
)

_SSS_THRESHOLD = 97.0
_MAX_PICKS = 12
_WMC_CONCURRENCY = 12
_WMC_TIMEOUT = 10.0
_WMC_CANDIDATE_LIMIT = 120
# 推荐达成率区间：已会打、离 SSS 不远
_ACHV_SWEET_MIN = 90.0
_ACHV_SWEET_MAX = _SSS_THRESHOLD
# 定数允许偏离 B50 中位的天花板
_DS_BAND = 0.8


@dataclass
class _Pick:
    title: str
    level: str
    level_index: int
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
    known = set(CONFIG_TAGS_ORDER)
    present = sorted(
        ((t, int(c)) for t, c in counts.items() if int(c) > 0),
        key=lambda x: (
            x[1],
            0 if x[0] in known else 1,
            CONFIG_TAGS_ORDER.index(x[0]) if x[0] in known else 999,
        ),
    )
    if present:
        return present[:top_n]
    return [(t, 0) for t in CONFIG_TAGS_ORDER[:top_n]]


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
    return [
        str(t.get('label', ''))
        for t in (tags_data or {}).get('radarTags', [])
        if t.get('label') and float(t.get('score', 0) or 0) >= 10
    ]


async def _fetch_wmc_tag_map(
    api: WmcAPI,
    items: List[Tuple[tuple, str]],
    *,
    radar_threshold: int = 0,
) -> Dict[tuple, dict]:
    """受限并发拉取 WMC 标签；单个超时/异常只跳过该谱面。"""
    if not items:
        return {}
    sem = asyncio.Semaphore(_WMC_CONCURRENCY)

    async def _one(cache_key: tuple, chart_key: str):
        async with sem:
            try:
                return await asyncio.wait_for(
                    api.get_tags(
                        chart_key,
                        radar_threshold=radar_threshold,
                        feature_threshold=0.3,
                    ),
                    timeout=_WMC_TIMEOUT,
                )
            except Exception as e:
                log.warning(f'[weakness] WMC tags failed key={chart_key} err={type(e).__name__}: {e}')
                return None

    results = await asyncio.gather(*(_one(k, ck) for k, ck in items))
    out: Dict[tuple, dict] = {}
    for (cache_key, _chart_key), result in zip(items, results):
        if isinstance(result, dict):
            tags = result.get('tags')
            if isinstance(tags, dict):
                out[cache_key] = tags
    log.info(f'[weakness] WMC tags fetched {len(out)}/{len(items)}')
    return out


_WIDTH = 1080
_MX = 40
_INNER_W = _WIDTH - _MX * 2

_LEVEL_INDEX = {
    'BAS': 0, 'BSC': 0,
    'ADV': 1,
    'EXP': 2,
    'MAS': 3, 'MST': 3,
    'ReM': 4,
}
_LEVEL_NAMES = ['BAS', 'ADV', 'EXP', 'MAS', 'ReM']


def _level_index(level: str) -> int:
    return _LEVEL_INDEX.get(str(level or '').strip()[:3], 2)


def _draw_tag_chips(im, d, x, y, tags, max_w):
    """绘制短板标签胶囊，自动换行。"""
    cx = x
    cy = y
    chip_h = 30
    gap = 10
    for tag, cnt in tags:
        label = f'{tag}  B50×{cnt}'
        font = bold_font(15)
        tw = int(d.textlength(image_safe_text(label), font=font))
        cw = tw + 28
        if cx + cw > x + max_w and cx > x:
            cx = x
            cy += chip_h + 10
        _card(im, (cx, cy, cx + cw, cy + chip_h), radius=15,
              fill=(255, 240, 210, 255), border=(230, 170, 80), border_w=1, shadow=False)
        d.text((cx + cw // 2, cy + chip_h // 2), image_safe_text(label),
               font=font, fill=(120, 80, 20), anchor='mm')
        cx += cw + gap
    return cy + chip_h - y


def _draw_pick_card(im, d, x, y, w, rank, p):
    """绘制一条推荐练习曲卡片，返回卡片高度。"""
    card_h = 86
    _card(im, (x, y, x + w, y + card_h), radius=18,
          fill=(255, 255, 255, 235), shadow=False)

    li = p.level_index
    diff_col = _DIFF_COLORS[li] if 0 <= li < len(_DIFF_COLORS) else _ACCENT

    # 左侧排名圆
    rank_colors = {1: _GOLD, 2: (196, 204, 220), 3: (226, 154, 96)}
    rc = rank_colors.get(rank, _MUTED)
    d.ellipse((x + 18, y + 18, x + 62, y + 62), fill=rc)
    d.text((x + 40, y + 40), str(rank), font=num_font(24),
           fill=(255, 255, 255), anchor='mm')

    tx = x + 80
    title = _truncate(d, p.title, bold_font(20), w - 200)
    d.text((tx, y + 14), image_safe_text(title), font=bold_font(20), fill=_TEXT)

    # 难度标签
    lv_font = bold_font(13)
    lv_label = _LEVEL_NAMES[li] if 0 <= li < len(_LEVEL_NAMES) else 'EXP'
    lv_w = int(d.textlength(image_safe_text(lv_label), font=lv_font)) + 20
    d.rounded_rectangle((tx, y + 44, tx + lv_w, y + 66), radius=8, fill=diff_col)
    d.text((tx + lv_w // 2, y + 55), image_safe_text(lv_label),
           font=lv_font, fill=(255, 255, 255), anchor='mm')

    info_x = tx + lv_w + 14
    d.text((info_x, y + 44), f'Lv.{p.level} · {p.ds:.1f}',
           font=bold_font(14), fill=_TEXT_SOFT, anchor='lt')
    d.text((info_x + 142, y + 44), f'RA {p.ra}',
           font=num_font(14), fill=_TEXT_SOFT, anchor='lt')

    # 右侧达成率
    ax = x + w - 18
    achv_col = _GREEN if p.achv >= 95 else (_GOLD if p.achv >= 90 else _RED)
    d.text((ax, y + 18), f'{p.achv:.4f}%',
           font=num_font(22), fill=achv_col, anchor='rt')
    score_pct = max(0.0, min(1.0, p.score / 100.0))
    bar_w = 130
    d.rounded_rectangle((ax - bar_w, y + 56, ax, y + 64), radius=4,
                        fill=(225, 230, 242, 255))
    fw = max(4, int(bar_w * score_pct))
    d.rounded_rectangle((ax - bar_w, y + 56, ax - bar_w + fw, y + 64),
                        radius=4, fill=_ACCENT)

    # 标签行
    if p.tags:
        tag_txt = '匹配：' + '、'.join(p.tags[:3])
        d.text((tx, y + 68), image_safe_text(tag_txt),
               font=bold_font(12), fill=_MUTED)
    return card_h


def render_weakness_prescription(
    nickname: str,
    weak_tags: List[Tuple[str, int]],
    picks: List[_Pick],
    ref_ds: float,
    *,
    user_name: str = 'Milk',
) -> BytesIO:
    """现代化卡片风格弱项处方单，返回 PNG BytesIO。"""
    nickname = nickname or '未知'
    hero_h = 150
    top = 118
    y = top + hero_h + 16

    tags_content_h = max(1, (len(weak_tags) + 2) // 3) * 40
    tags_h = 56 + tags_content_h
    tags_y = y
    y += tags_h + 16

    if picks:
        list_h = 56 + len(picks) * (86 + 12)
    else:
        list_h = 56 + 64
    list_y = y
    y += list_h + 16
    canvas_h = y + 56

    im = _make_bg(_WIDTH, canvas_h)
    d = ImageDraw.Draw(im)
    _brand_mark(im, _WIDTH, user_name)
    _period_chip(im, _WIDTH, '弱项处方')

    d.text((_MX + 200, 44), '弱项处方单', font=bold_font(32), fill=_TEXT)
    d.text((_MX + 200, 86),
           f'{nickname}  ·  定数参考 {ref_ds:.1f}  ·  接近 SSS 的短板练习曲',
           font=bold_font(16), fill=_TEXT_SOFT)

    # ---- Hero ----
    _card(im, (_MX, 118, _MX + _INNER_W, 118 + hero_h), radius=24,
          fill=(255, 255, 255, 230))
    d.text((_MX + 30, 118 + 24), '定数参考',
           font=bold_font(16), fill=_MUTED)
    d.text((_MX + 30, 118 + 48), f'{ref_ds:.1f}',
           font=num_font(46), fill=_ACCENT)
    d.text((_MX + 30, 118 + 108),
           f'短板标签 {len(weak_tags)} 项  ·  推荐曲目 {len(picks)} 首',
           font=bold_font(15), fill=_TEXT_SOFT)

    # Hero 右侧统计块
    stat_x = _MX + 300
    stat_w = (_INNER_W - 330) // 2 - 8
    top_score = picks[0].score if picks else 0
    top_achv = picks[0].achv if picks else 0
    stats = [
        ('最高匹配度', f'{top_score:.0f}', _GOLD),
        ('最佳达成率', f'{top_achv:.2f}%', _GREEN if picks else _MUTED),
    ]
    for i, (label, val, col) in enumerate(stats):
        sx = stat_x + i * (stat_w + 16)
        _card(im, (sx, 118 + 24, sx + stat_w, 118 + 24 + 110), radius=16,
              fill=(245, 247, 252, 255), shadow=False)
        d.text((sx + 18, 118 + 44), label, font=bold_font(14), fill=_MUTED)
        d.text((sx + 18, 118 + 78), str(val), font=num_font(28), fill=col)

    # ---- 短板标签 ----
    _card(im, (_MX, tags_y, _MX + _INNER_W, tags_y + tags_h), radius=20,
          fill=(255, 255, 255, 225))
    _draw_title(d, _MX + 22, tags_y + 16, 20, '短板标签', _ACCENT)
    if weak_tags:
        _draw_tag_chips(im, d, _MX + 22, tags_y + 50, weak_tags, _INNER_W - 44)
    else:
        d.text((_MX + 22, tags_y + 50), '暂无明显短板标签',
               font=bold_font(15), fill=_MUTED)

    # ---- 推荐练习 ----
    _card(im, (_MX, list_y, _MX + _INNER_W, list_y + list_h), radius=20,
          fill=(255, 255, 255, 225))
    _draw_title(d, _MX + 22, list_y + 16, 20, '推荐练习', _ACCENT)
    ry = list_y + 56
    if not picks:
        _card(im, (_MX + 22, ry, _MX + _INNER_W - 22, ry + 64), radius=14,
              fill=(245, 247, 252, 255), shadow=False)
        d.text((_MX + _INNER_W // 2, ry + 32),
               '暂无贴合水平的匹配曲目，可提高相近定数成绩后再试',
               font=bold_font(15), fill=_MUTED, anchor='mm')
    else:
        for i, p in enumerate(picks, 1):
            _draw_pick_card(im, d, _MX + 22, ry, _INNER_W - 44, i, p)
            ry += 86 + 12

    _footer(im, _WIDTH, canvas_h)
    return _finalize(im)


async def generate_weakness_prescription(qqid: int) -> Union[str, MessageSegment]:
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
        wmc_items = []
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
                wmc_items.append(((wmc_sid, kind, diff_val), make_chart_key(wmc_sid, kind, diff_val)))
        wmc_cache = await _fetch_wmc_tag_map(api, wmc_items, radar_threshold=0)

    stats = get_b50_tag_stats(userinfo, wmc_tags_cache=wmc_cache or None)
    if not any(stats.get('配置', {}).values()):
        return (
            '无法生成弱项处方：你的 B50 谱面暂无配置标签数据。\n'
            '可能原因：当前 B50 曲目尚未被标签库收录。\n'
            '多打几首已标注配置的谱面后再来试试吧～'
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
        candidate_records.sort(
            key=lambda item: _score_pick(
                matched=[],
                weak_count=max(1, len(weak_set)),
                achv=float(item[0].achievements),
                ds=float(item[0].ds),
                ref_ds=ref_ds,
                in_b50=(int(item[0].song_id), int(item[0].level_index)) in b50_keys,
            ),
            reverse=True,
        )
        wmc_items = []
        seen_chart_keys = set(wmc_cache)
        for r, music, _title in candidate_records:
            if len(wmc_items) >= _WMC_CANDIDATE_LIMIT:
                break
            wmc_sid = song_id_for_wmc(music)
            kind = kind_for_wmc(music)
            diff_val = diff_value_for_wmc(int(r.level_index))
            cache_key = (wmc_sid, kind, diff_val)
            if cache_key in seen_chart_keys:
                continue
            seen_chart_keys.add(cache_key)
            wmc_items.append((cache_key, make_chart_key(wmc_sid, kind, diff_val)))
        wmc_cache.update(await _fetch_wmc_tag_map(api, wmc_items, radar_threshold=0))

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
                level_index=int(r.level_index),
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
    from .maimaidx_image_executor import run_image_cpu

    def _render():
        bio = render_weakness_prescription(
            nickname, weak_tags, picks, ref_ds, user_name=nickname,
        )
        return MessageSegment.image(
            'base64://' + base64.b64encode(bio.getvalue()).decode()
        )

    return await run_image_cpu(_render)
