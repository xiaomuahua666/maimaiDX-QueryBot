"""Head-to-Head 对战战绩：两人重叠曲目胜率与分差对比图。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Union

from nonebot.adapters.onebot.v11 import MessageSegment
from PIL import Image, ImageDraw

from ..config import SIYUAN, footer_generated
from .image import DrawText, draw_centered_design_footer, generate_frosted_card, image_to_base64
from .maimaidx_best_50 import filter_utage_records
from .maimaidx_error import UserDisabledQueryError, UserNotFoundError, UserNotExistsError
from .maimaidx_music_info import fetch_b50_wmc_tags, get_b50_tag_stats

ACCENT = (124, 129, 255, 255)
TEXT = (45, 50, 95, 255)
SUBTEXT = (90, 95, 140, 255)
WIN_A = (72, 168, 108, 255)
WIN_B = (220, 100, 110, 255)
TIE = (140, 145, 170, 255)


@dataclass
class _BattleRow:
    title: str
    level: str
    ach_a: float
    ach_b: float
    delta: float
    winner: str


def _song_key(r) -> Tuple[int, int]:
    return int(r.song_id), int(r.level_index)


def _short_title(title: str, n: int = 16) -> str:
    t = title.strip()
    return t if len(t) <= n else t[: n - 1] + '…'


def _merge_records(records) -> Dict[Tuple[int, int], object]:
    best: Dict[Tuple[int, int], object] = {}
    for r in records:
        k = _song_key(r)
        o = best.get(k)
        if o is None or float(r.achievements) > float(o.achievements):
            best[k] = r
    return best


def _compare_overlap(
    map_a: Dict[Tuple[int, int], object],
    map_b: Dict[Tuple[int, int], object],
) -> Tuple[List[_BattleRow], int, int, int, float]:
    rows: List[_BattleRow] = []
    win_a = win_b = ties = 0
    margin_sum = 0.0

    for key in map_a.keys() & map_b.keys():
        ra = map_a[key]
        rb = map_b[key]
        ach_a = float(ra.achievements)
        ach_b = float(rb.achievements)
        delta = ach_a - ach_b
        title = getattr(ra, 'title', '') or getattr(rb, 'title', '')
        level = ra.level
        if abs(delta) < 0.0001:
            winner = 'tie'
            ties += 1
        elif delta > 0:
            winner = 'a'
            win_a += 1
            margin_sum += delta
        else:
            winner = 'b'
            win_b += 1
            margin_sum += abs(delta)
        rows.append(_BattleRow(title=title, level=level, ach_a=ach_a, ach_b=ach_b, delta=delta, winner=winner))

    rows.sort(key=lambda x: abs(x.delta), reverse=True)
    decided = win_a + win_b
    avg_margin = margin_sum / decided if decided else 0.0
    return rows, win_a, win_b, ties, avg_margin


def _draw_h2h(
    nick_a: str,
    nick_b: str,
    rating_a: int,
    rating_b: int,
    rows: List[_BattleRow],
    win_a: int,
    win_b: int,
    ties: int,
    avg_margin: float,
    tag_gap: Optional[List[Tuple[str, int, int]]],
) -> Image.Image:
    width = 1000
    top_h = 200
    row_h = 32
    show_rows = rows[:10]
    list_h = 56 + max(1, len(show_rows)) * row_h
    tag_h = 120 if tag_gap else 0
    footer_h = 40
    height = top_h + list_h + tag_h + 24 + footer_h

    im = Image.new('RGBA', (width, height), (245, 247, 255, 255))
    im = generate_frosted_card(im, (24, 24, width - 24, top_h), alpha=0.52)
    im = generate_frosted_card(im, (24, top_h + 16, width - 24, top_h + 16 + list_h), alpha=0.52)
    if tag_gap:
        im = generate_frosted_card(im, (24, top_h + 16 + list_h + 16, width - 24, height - footer_h), alpha=0.52)

    dr = ImageDraw.Draw(im)
    dt = DrawText(dr, SIYUAN)
    dt.draw(width // 2, 36, 30, 'Head-to-Head 对战战绩', ACCENT, 'mm', 2, (255, 255, 255, 240))

    total = win_a + win_b + ties
    dt.draw(80, 88, 22, nick_a, WIN_A, 'lt', 2, (255, 255, 255, 230))
    dt.draw(80, 118, 18, f'Rating {rating_a}', SUBTEXT, 'lt')
    dt.draw(80, 148, 20, f'胜 {win_a} 场', WIN_A, 'lt', 1, (255, 255, 255, 220))

    dt.draw(width - 80, 88, 22, nick_b, WIN_B, 'rt', 2, (255, 255, 255, 230))
    dt.draw(width - 80, 118, 18, f'Rating {rating_b}', SUBTEXT, 'rt')
    dt.draw(width - 80, 148, 20, f'胜 {win_b} 场', WIN_B, 'rt', 1, (255, 255, 255, 220))

    center = f'重叠 {total} 谱  ·  平局 {ties}  ·  平均分差 {avg_margin:.4f}%'
    dt.draw(width // 2, 170, 17, center, TEXT, 'mm', 1, (255, 255, 255, 220))

    y = top_h + 36
    dt.draw(44, y, 20, '分差最大曲目', ACCENT, 'lt', 2, (255, 255, 255, 230))
    y += 32
    if not show_rows:
        dt.draw(48, y, 17, '两人暂无重叠游玩谱面', SUBTEXT, 'lt')
    else:
        for i, row in enumerate(show_rows, 1):
            if row.winner == 'a':
                color = WIN_A
                mark = '◀'
            elif row.winner == 'b':
                color = WIN_B
                mark = '▶'
            else:
                color = TIE
                mark = '='
            line = (
                f'{i}. {_short_title(row.title)} [{row.level}]  '
                f'{row.ach_a:.4f}% vs {row.ach_b:.4f}%  ({row.delta:+.4f}%) {mark}'
            )
            dt.draw(48, y, 15, line, color, 'lt', 1, (255, 255, 255, 220))
            y += row_h

    if tag_gap:
        y = top_h + 16 + list_h + 36
        dt.draw(44, y, 20, 'B50 配置标签差（A−B）', ACCENT, 'lt', 2, (255, 255, 255, 230))
        y += 32
        for tag, ca, cb in tag_gap[:6]:
            dt.draw(48, y, 15, f'{tag}: {ca} vs {cb}  ({ca - cb:+d})', TEXT, 'lt', 1, (255, 255, 255, 220))
            y += 26

    draw_centered_design_footer(
        im, dt, footer_generated(),
        color=SUBTEXT,
        margin_x=48,
        start_font_size=14,
        min_font_size=10,
        bottom_gap=8,
    )
    return im


def _tag_compare(stats_a: dict, stats_b: dict) -> List[Tuple[str, int, int]]:
    cfg_a = stats_a.get('配置') or {}
    cfg_b = stats_b.get('配置') or {}
    tags = sorted(set(cfg_a) | set(cfg_b), key=lambda t: abs(cfg_a.get(t, 0) - cfg_b.get(t, 0)), reverse=True)
    out: List[Tuple[str, int, int]] = []
    for t in tags:
        ca = int(cfg_a.get(t, 0))
        cb = int(cfg_b.get(t, 0))
        if ca != cb:
            out.append((t, ca, cb))
    return out[:6]


async def generate_head_to_head(
    qqid_a: int,
    qqid_b: int,
    nick_a: str,
    nick_b: str,
) -> Union[str, MessageSegment]:
    from .maimaidx_datasource import get_user_records

    try:
        user_a, rec_a = await get_user_records(qqid=qqid_a)
        user_b, rec_b = await get_user_records(qqid=qqid_b)
    except (UserNotFoundError, UserNotExistsError, UserDisabledQueryError) as e:
        return str(e)

    rec_a = filter_utage_records(rec_a or [])
    rec_b = filter_utage_records(rec_b or [])
    if not rec_a or not rec_b:
        return '需要开发者 Token 拉取双方全量成绩才能对战（至少一方无成绩）。'

    map_a = _merge_records(rec_a)
    map_b = _merge_records(rec_b)
    rows, win_a, win_b, ties, avg_margin = _compare_overlap(map_a, map_b)
    if not rows:
        return f'{nick_a} 与 {nick_b} 暂无重叠游玩谱面，无法生成对战战绩。'

    import asyncio

    tags_a, tags_b = await asyncio.gather(
        fetch_b50_wmc_tags(user_a),
        fetch_b50_wmc_tags(user_b),
    )
    stats_a = get_b50_tag_stats(user_a, tags_a)
    stats_b = get_b50_tag_stats(user_b, tags_b)
    tag_gap = _tag_compare(stats_a, stats_b)

    im = _draw_h2h(
        nick_a,
        nick_b,
        int(user_a.rating or 0),
        int(user_b.rating or 0),
        rows,
        win_a,
        win_b,
        ties,
        avg_margin,
        tag_gap,
    )
    return MessageSegment.image(image_to_base64(im))
