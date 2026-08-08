"""
日报 / 周报 / 月报 / 年报 / 存档对比的现代化图片渲染。

与排行榜统一的明亮卡片风格，包含 Rating 曲线、核心变化、新增/提升曲目卡、
难度分布、寸止/锁血命中等丰富统计。所有图形程序化绘制，缺素材时自动回退。
"""
from __future__ import annotations

from io import BytesIO
from typing import Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFilter

from ..config import log
from .image import DrawText, music_picture, rounded_corners
from .maimaidx_game_assets import draw_rating_badge, num_font, rating_badge_width
# 直接复用排行榜渲染器中的基础元件，保持视觉统一
from .maimaidx_leaderboard_image import (
    _ACCENT, _CARD_BORDER, _DIFF_COLORS, _GOLD, _GREEN, _MUTED,
    _RED, _TEXT, _TEXT_SOFT, _bar, _brand_mark, _card, _fallback_avatar,
    _finalize, _font_bold, _font_mono, _footer, _make_bg, _paste_round,
    _period_chip, _stat_box, _text_len, _truncate,
)

_DIFF_NAMES = ['Basic', 'Advanced', 'Expert', 'Master', 'Re:Master']


def _delta_color(v: float):
    if v > 0:
        return _GREEN
    if v < 0:
        return _RED
    return _MUTED


def _sign(v) -> str:
    return f'+{v}' if (isinstance(v, (int, float)) and v > 0) else str(v)


def _cover(song_id: int, size: int = 72) -> Image.Image:
    try:
        p = music_picture(song_id)
        img = Image.open(p).convert('RGBA').resize((size, size))
        return rounded_corners(img, 12, (True, True, True, True))
    except Exception:
        return _fallback_avatar(size, '♪', _ACCENT)


def _song_card(im, x, y, w, title, level, achv, ra, *,
               ra_delta=None, achv_delta=None, level_index=3, song_id=None):
    """绘制曲目小卡，返回高度。"""
    h = 92
    _card(im, (x, y, x + w, y + h), radius=16, fill=(255, 255, 255, 235),
          border=_CARD_BORDER)
    cov = _cover(song_id or 0, 68)
    im.alpha_composite(cov, (x + 8, y + 12))
    d = ImageDraw.Draw(im)
    nf = _font_bold(18)
    d.text((x + 88, y + 12), _truncate(d, title, nf, w - 100), font=nf, fill=_TEXT)
    # 难度色点 + 等级
    diff_c = _DIFF_COLORS[min(max(level_index, 0), 4)]
    d.ellipse((x + 88, y + 40, x + 100, y + 52), fill=diff_c)
    d.text((x + 106, y + 46), f'{level}', font=_font_mono(15), fill=_TEXT_SOFT, anchor='lm')
    # 达成率
    d.text((x + 88, y + 62), f'{achv:.4f}%', font=_font_mono(18), fill=_TEXT)
    # ra / delta
    if ra_delta is not None:
        if ra:
            d.text((x + w - 12, y + 22), f'{ra}', font=_font_mono(16), fill=_TEXT_SOFT, anchor='rt')
        d.text((x + w - 12, y + 52), _sign(ra_delta),
               font=_font_bold(24), fill=_delta_color(ra_delta), anchor='rt')
    elif ra:
        d.text((x + w - 14, y + h // 2), f'{ra}',
               font=_font_mono(22), fill=_TEXT_SOFT, anchor='rm')
    return h


def _draw_curve(im, x, y, w, h, points: List[int], labels: List[str]):
    """Rating 曲线：渐变面积填充 + 折线 + 端点高亮。"""
    _card(im, (x, y, x + w, y + h), radius=20, fill=(255, 255, 255, 220))
    d = ImageDraw.Draw(im)
    d.text((x + 24, y + 18), 'Rating 走势', font=_font_bold(22), fill=_TEXT)

    if not points:
        d.text((x + w // 2, y + h // 2), '暂无数据', font=_font_bold(20), fill=_MUTED, anchor='mm')
        return

    left, top, right, bottom = x + 64, y + 64, x + w - 32, y + h - 48
    min_v, max_v = min(points), max(points)
    span = max_v - min_v
    if span <= 0:
        span = max(10, max_v * 0.01)
        min_v -= span // 2
        max_v += span // 2

    # 仅两个数据点（如日报）：画「旧 vs 新」对比柱，更直观
    if len(points) == 2:
        bar_w = 90
        gap = (right - left - bar_w * 2) // 3
        bx = [left + gap, left + gap * 2 + bar_w]
        for i, v in enumerate(points):
            ratio = (v - min_v) / span
            bh = max(8, int((bottom - top - 24) * ratio))
            by = bottom - bh
            color = _MUTED if i == 0 else _ACCENT
            d.rounded_rectangle((bx[i], by, bx[i] + bar_w, bottom), radius=10, fill=color)
            d.text((bx[i] + bar_w // 2, by - 10), str(v), font=_font_mono(18), fill=_TEXT, anchor='mb')
            d.text((bx[i] + bar_w // 2, bottom + 12), labels[i] if i < len(labels) else '',
                   font=_font_mono(14), fill=_MUTED, anchor='mt')
        delta = points[1] - points[0]
        d.text((left + (right - left) // 2, top + 6), _sign(delta),
               font=_font_bold(28), fill=_delta_color(delta), anchor='mt')
        return

    # 网格
    for gy in range(5):
        yy = int(top + (bottom - top) * gy / 4)
        d.line((left, yy, right, yy), fill=(225, 230, 242, 255), width=1)
        val = int(max_v - span * gy / 4)
        d.text((left - 10, yy), str(val), font=_font_mono(13), fill=_MUTED, anchor='rm')

    n = len(points)
    coords: List[Tuple[int, int]] = []
    for i, v in enumerate(points):
        px = left if n == 1 else int(left + (right - left) * i / (n - 1))
        ratio = (v - min_v) / span
        py = int(bottom - ratio * (bottom - top))
        coords.append((px, py))

    # 面积渐变填充
    if len(coords) >= 2:
        area = Image.new('RGBA', (w, h), (0, 0, 0, 0))
        ad = ImageDraw.Draw(area)
        poly = [(p[0] - x, p[1] - y) for p in coords] + [(right - x, bottom - y), (left - x, bottom - y)]
        ad.polygon(poly, fill=(124, 129, 255, 55))
        area = area.filter(ImageFilter.GaussianBlur(2))
        im.alpha_composite(area, (x, y))
        d.line(coords, fill=_ACCENT, width=4)
    for i, (px, py) in enumerate(coords):
        d.ellipse((px - 5, py - 5, px + 5, py + 5), fill=_ACCENT, outline=(255, 255, 255, 255), width=2)
        if i in (0, len(coords) - 1) or n <= 8:
            d.text((px, py - 12), str(points[i]), font=_font_mono(14), fill=_TEXT, anchor='mb')
    if labels:
        d.text((left, bottom + 10), labels[0], font=_font_mono(13), fill=_MUTED)
        d.text((right, bottom + 10), labels[-1], font=_font_mono(13), fill=_MUTED, anchor='rt')


def _diff_distribution(im, x, y, w, h, dist: List[int]):
    _card(im, (x, y, x + w, y + h), radius=20, fill=(255, 255, 255, 220))
    d = ImageDraw.Draw(im)
    d.text((x + 24, y + 18), '提升曲目难度分布', font=_font_bold(20), fill=_TEXT)
    total = sum(dist) or 1
    bar_y = y + 58
    bar_h = 22
    # 堆叠条
    cx = x + 24
    bar_w = w - 48
    segs = [(dist[i], _DIFF_COLORS[i]) for i in range(5) if dist[i] > 0]
    if not segs:
        d.text((x + w // 2, bar_y + bar_h // 2), '本周期无提升曲目',
               font=_font_bold(16), fill=_MUTED, anchor='mm')
        return
    for i, (val, color) in enumerate(segs):
        sw = int(bar_w * val / total)
        if i == len(segs) - 1:
            sw = x + 24 + bar_w - cx
        d.rounded_rectangle((cx, bar_y, cx + sw, bar_y + bar_h), radius=6, fill=color)
        cx += sw
    # 圆角遮罩
    # 图例
    lx = x + 24
    ly = bar_y + bar_h + 14
    lf = _font_mono(14)
    bf = _font_bold(14)
    for i in range(5):
        c = dist[i]
        if c <= 0:
            continue
        d.ellipse((lx, ly + 4, lx + 10, ly + 14), fill=_DIFF_COLORS[i])
        d.text((lx + 14, ly), _DIFF_NAMES[i], font=bf, fill=_TEXT_SOFT)
        d.text((lx + 14, ly + 18), f'{c} 首 · {c / total * 100:.0f}%', font=lf, fill=_MUTED)
        lx += 150
        if lx > x + w - 120:
            lx, ly = x + 24, ly + 40


def _sun_lock_panel(im, x, y, w, h, sun_list, lock_list):
    _card(im, (x, y, x + w, y + h), radius=20, fill=(255, 255, 255, 220))
    d = ImageDraw.Draw(im)
    d.text((x + 24, y + 18), '寸止 / 锁血命中', font=_font_bold(20), fill=_TEXT)
    col_w = (w - 60) // 2
    for ci, (label, lst, color) in enumerate([
        ('锁血', lock_list, (120, 200, 255, 255)),
        ('寸止', sun_list, _GOLD),
    ]):
        cx = x + 24 + ci * (col_w + 12)
        cy = y + 56
        d.rounded_rectangle((cx, cy, cx + 30, cy + 24), radius=8, fill=color)
        d.text((cx + 38, cy + 12), label, font=_font_bold(17), fill=_TEXT, anchor='lm')
        d.text((cx + col_w - 12, cy + 12), f'{len(lst)}',
               font=_font_mono(20), fill=color, anchor='rm')
        ly = cy + 36
        if not lst:
            d.text((cx + 8, ly), '无命中', font=_font_bold(15), fill=_MUTED)
            continue
        for e in lst[:4]:
            d.text((cx + 8, ly), _truncate(d, e.title, _font_bold(15), col_w - 70),
                   font=_font_bold(15), fill=_TEXT_SOFT)
            d.text((cx + col_w - 8, ly), f'{e.achv_now:.2f}%',
                   font=_font_mono(14), fill=_MUTED, anchor='rt')
            ly += 24


def render_report(
    title: str,
    nickname: str,
    period: str,
    points: List[int],
    labels: List[str],
    data: Dict,
    period_tag: str = '日报',
) -> BytesIO:
    width = 1080
    mx = 40
    inner_w = width - mx * 2

    new_entries = list(data.get('new_entries') or [])[:6]
    improved = list(data.get('improved') or [])
    top_improved = improved[:6]
    sun_list = list(data.get('sun_list') or [])[:4]
    lock_list = list(data.get('lock_list') or [])[:4]

    # ---------- 高度估算 ----------
    y = 110
    hero_h = 130
    y += hero_h + 16
    curve_h = 260 if len(points) != 2 else 150
    y += curve_h + 16
    stats_h = 96
    y += stats_h + 16
    # 曲目卡两列
    cards_title_h = 36
    card_h = 92
    n_new = len(new_entries)
    n_imp = len(top_improved)
    col_n = max(n_new, n_imp, 1)
    cards_h = cards_title_h + col_n * (card_h + 10) + 8
    y += cards_h + 16
    dist_h = 130
    y += dist_h + 16
    sun_h = 200
    y += sun_h + 24
    total_h = y

    im = _make_bg(width, total_h)
    d = ImageDraw.Draw(im)
    _brand_mark(im, width)
    _period_chip(im, width, period_tag)

    # 标题
    d.text((mx + 150, 36), title, font=_font_bold(34), fill=_TEXT)
    d.text((mx + 150, 82), f'{nickname}  ·  {period}', font=_font_bold(18), fill=_TEXT_SOFT)

    # ---------- Hero：rating 变化 ----------
    hy = 110
    _card(im, (mx, hy, mx + inner_w, hy + hero_h), radius=22, fill=(255, 255, 255, 230))
    old_r = data.get('old_rating', 0)
    new_r = data.get('new_rating', 0)
    delta = data.get('rating_delta', 0)
    # 旧 rating
    d.text((mx + 32, hy + 30), '旧 Rating', font=_font_bold(16), fill=_MUTED)
    d.text((mx + 32, hy + 60), f'{old_r}', font=_font_mono(40), fill=_TEXT_SOFT)
    # 箭头
    ax = mx + 240
    d.text((ax, hy + hero_h // 2), '→', font=_font_bold(40), fill=_MUTED, anchor='mm')
    # 新 rating + 徽章
    d.text((mx + 300, hy + 30), '新 Rating', font=_font_bold(16), fill=_MUTED)
    d.text((mx + 300, hy + 64), f'{new_r}', font=_font_mono(40), fill=_TEXT)
    bh = 40
    draw_rating_badge(im, mx + 520, hy + 58, new_r, height=bh)
    # delta 大数字
    dc = _delta_color(delta)
    d.text((mx + inner_w - 24, hy + 38), _sign(delta), font=_font_bold(46), fill=dc, anchor='rt')
    d.text((mx + inner_w - 24, hy + 92), 'Rating 变化', font=_font_bold(15), fill=_MUTED, anchor='rt')

    # ---------- 曲线 ----------
    cy = hy + hero_h + 16
    _draw_curve(im, mx, cy, inner_w, curve_h, points, labels)

    # ---------- 统计盒 ----------
    sy = cy + curve_h + 16
    best = data.get('best_entry')
    best_txt = f'{best.title[:8]} +{best.ra_delta}' if best else '无'
    boxes = [
        (data.get('improved_count', 0), '提升曲目'),
        (data.get('new_count', 0), '新增进B50'),
        (data.get('total_improved_ra', 0), '总提升 ra'),
        (best_txt, '最佳单曲'),
    ]
    bw = (inner_w - 3 * 14) // 4
    for i, (val, lab) in enumerate(boxes):
        _stat_box(im, mx + i * (bw + 14), sy, bw, stats_h, val, lab)

    # ---------- 曲目卡两列 ----------
    ly0 = sy + stats_h + 16
    col_w = (inner_w - 16) // 2
    # 左：提升 TOP
    d.text((mx + 8, ly0), 'B50 提升 TOP', font=_font_bold(20), fill=_TEXT)
    # 右：新增
    d.text((mx + col_w + 24, ly0), 'B50 新增曲目', font=_font_bold(20), fill=_TEXT)
    card_y = ly0 + 32
    for i in range(col_n):
        if i < n_imp:
            e = top_improved[i]
            rec = _find_record(data.get('new_b50'), e)
            song_id = int(rec.song_id) if rec else 0
            _song_card(im, mx, card_y + i * (card_h + 10), col_w,
                       e.title, e.level, e.achv_now, int(rec.ra) if rec else 0,
                       ra_delta=e.ra_delta, achv_delta=e.achv_delta,
                       level_index=e.level_index, song_id=song_id)
        if i < n_new:
            r = new_entries[i]
            _song_card(im, mx + col_w + 16, card_y + i * (card_h + 10), col_w,
                       r.title, r.level, float(r.achievements), int(r.ra),
                       level_index=int(r.level_index), song_id=int(r.song_id))

    # ---------- 难度分布 + 寸止/锁血 ----------
    dy = card_y + col_n * (card_h + 10) + 16
    _diff_distribution(im, mx, dy, inner_w, dist_h, list(data.get('diff_dist') or [0] * 5))
    sly = dy + dist_h + 16
    _sun_lock_panel(im, mx, sly, inner_w, sun_h, sun_list, lock_list)

    _footer(im, width, total_h, period_tag)
    return _finalize(im)


def _find_record(new_b50, e):
    for r in (new_b50 or []):
        if getattr(r, 'title', None) == e.title and getattr(r, 'level', None) == e.level:
            return r
    return None


__all__ = ['render_report']
