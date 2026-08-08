"""
现代化排行榜图片渲染器（maimai 风格）。

为群 rating 榜、单曲成绩榜、群吃分榜、寸止/锁血榜以及今日吃分推荐
提供统一的明亮卡片风格可视化输出：渐变背景、名牌卡片、QQ 头像、
rating 徽章、进度条，以及底部统计面板（环形分布 / 评级分布条）。

所有图形元素均程序化绘制，不依赖 static/mai/pic 资源，本地可直接渲染。
"""
from __future__ import annotations

import asyncio
import math
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import httpx
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from ..config import footer_generated
from .image import DrawText, image_safe_text, music_picture
from .maimaidx_game_assets import (
    bold_font,
    draw_rank_sprite,
    draw_rating_badge,
    num_font,
    rating_color,
    rating_badge_width,
)


# ----------------------------------------------------------------------
# 调色板（明亮 maimai 风）
# ----------------------------------------------------------------------
_SKY_TOP = (178, 232, 255, 255)
_SKY_MID = (214, 240, 255, 255)
_PINK_BOT = (255, 218, 236, 255)
_WHITE_PANEL = (255, 255, 255, 248)
_WHITE_SOFT = (255, 255, 255, 200)
_CARD_BORDER = (255, 255, 255, 230)
_SHADOW = (60, 70, 110, 45)
_TEXT = (44, 52, 80, 255)
_TEXT_SOFT = (110, 120, 150, 255)
_MUTED = (150, 158, 182, 255)
_GOLD = (255, 196, 76, 255)
_SILVER = (196, 204, 220, 255)
_BRONZE = (226, 154, 96, 255)
_RANK_BG = (255, 255, 255, 235)
_ACCENT = (124, 129, 255, 255)
_GREEN = (72, 200, 130, 255)
_RED = (235, 92, 116, 255)
_BLUE = (88, 168, 255, 255)

_DIFF_COLORS = [
    (72, 196, 120, 255),   # Basic 绿
    (245, 186, 60, 255),   # Advanced 黄
    (240, 110, 130, 255),  # Expert 红
    (156, 96, 220, 255),   # Master 紫
    (210, 150, 255, 255),  # Re:Master
]

# 评级配色（SSS+ ... D），用于评级分布条与评级徽章
_RATE_COLORS: Dict[str, Tuple[int, int, int, int]] = {
    'SSSp': (255, 120, 200, 255), 'SSS': (255, 140, 210, 255),
    'SSp': (255, 180, 90, 255), 'SS': (255, 198, 120, 255),
    'Sp': (180, 240, 90, 255), 'S': (190, 235, 120, 255),
    'AAA': (90, 220, 220, 255), 'AA': (120, 200, 255, 255),
    'A': (150, 180, 255, 255),
    'BBB': (200, 170, 255, 255), 'BB': (216, 160, 240, 255),
    'B': (230, 180, 220, 255),
    'C': (210, 210, 210, 255), 'D': (190, 190, 190, 255),
}
_RATE_ORDER = ['SSSp', 'SSS', 'SSp', 'SS', 'Sp', 'S', 'AAA', 'AA', 'A',
               'BBB', 'BB', 'B', 'C', 'D']

# rating 段位配色
_RATING_TIERS: List[Tuple[int, Tuple[int, int, int, int]]] = [
    (1000, (180, 188, 200, 255)),
    (2000, (92, 200, 130, 255)),
    (4000, (96, 165, 250, 255)),
    (7000, (168, 120, 230, 255)),
    (10000, (245, 158, 66, 255)),
    (12000, (236, 98, 130, 255)),
    (13000, (206, 150, 110, 255)),
    (14000, (206, 214, 230, 255)),
    (14500, (255, 209, 102, 255)),
    (15000, (120, 220, 255, 255)),
]
_RAINBOW = (255, 110, 190, 255)

# rating 分布桶（用于环形图）
_RATING_BUCKETS = [
    (16000, '≥16000', (255, 120, 200, 255)),
    (15000, '15000–15999', (120, 220, 255, 255)),
    (14500, '14500–14999', (255, 209, 102, 255)),
    (14000, '14000–14499', (206, 214, 230, 255)),
    (13000, '13000–13999', (206, 150, 110, 255)),
    (12000, '12000–12999', (236, 98, 130, 255)),
    (0,     '<12000',     (150, 160, 190, 255)),
]


def _font_bold(size: int):
    return bold_font(size)


def _font_mono(size: int):
    # 数字统一使用 Torus SemiBold（与 B50 达成率数字一致）
    return num_font(size)

def _draw_title(d, x, y, size, text, color, anchor_pos='lt'):
    """标题/文案统一走 _font_bold 回退链路（缺字体也不崩），并做符号替换。"""
    d.text((x, y), image_safe_text(text), font=_font_bold(size), fill=color, anchor=anchor_pos)


# ----------------------------------------------------------------------
# 基础绘制
# ----------------------------------------------------------------------
def _vertical_gradient(w: int, h: int, stops) -> Image.Image:
    img = Image.new('RGBA', (w, h), stops[0][1])
    px = img.load()
    for y in range(h):
        t = y / max(1, h - 1)
        for i in range(len(stops) - 1):
            p0, c0 = stops[i]
            p1, c1 = stops[i + 1]
            if p0 <= t <= p1:
                k = (t - p0) / max(1e-6, p1 - p0)
                c = tuple(int(c0[j] + (c1[j] - c0[j]) * k) for j in range(4))
                break
        else:
            c = stops[-1][1]
        for x in range(w):
            px[x, y] = c
    return img


def _round_mask(size, radius):
    mask = Image.new('L', size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0], size[1]), radius=radius, fill=255)
    return mask


def _paste_round(im: Image.Image, sprite: Image.Image, pos, radius: int):
    sprite = sprite.convert('RGBA')
    if radius > 0:
        sprite.putalpha(_round_mask(sprite.size, radius))
    im.alpha_composite(sprite, pos)


def _card(im: Image.Image, box, radius=22, fill=_WHITE_PANEL,
          border=None, border_w=2, shadow=True):
    x1, y1, x2, y2 = box
    w, h = x2 - x1, y2 - y1
    if shadow:
        sh = Image.new('RGBA', (w + 40, h + 40), (0, 0, 0, 0))
        sd = ImageDraw.Draw(sh)
        sd.rounded_rectangle((20, 24, 20 + w, 24 + h), radius=radius, fill=_SHADOW)
        sh = sh.filter(ImageFilter.GaussianBlur(10))
        im.alpha_composite(sh, (x1 - 20, y1 - 20))
    layer = Image.new('RGBA', (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.rounded_rectangle((0, 0, w, h), radius=radius, fill=fill)
    if border:
        d.rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, outline=border, width=border_w)
    im.alpha_composite(layer, (x1, y1))


def _text_len(d, text, font):
    return d.textlength(image_safe_text(text), font=font)


def _truncate(d, text, font, max_w):
    text = image_safe_text(str(text))
    if d.textlength(text, font=font) <= max_w:
        return text
    ell = '…'
    while text and d.textlength(text + ell, font=font) > max_w:
        text = text[:-1]
    return text + ell


def _bar(im, x, y, w, h, ratio, color, bg=(225, 230, 242, 255), radius=8):
    ratio = max(0.0, min(1.0, ratio))
    # 简洁现代风：细轨道 + 纯色填充 + 末端小圆点，不加厚重高光
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((x, y, x + w, y + h), radius=radius, fill=bg)
    if ratio <= 0:
        return
    fw = max(h, int(w * ratio))
    d.rounded_rectangle((x, y, x + fw, y + h), radius=radius, fill=color)
    # 顶部极细高光线，增加质感但不抢戏
    if h >= 10:
        d.rounded_rectangle((x + 2, y + 1, x + fw - 2, y + h // 2 - 1),
                            radius=max(2, radius - 2), fill=(255, 255, 255, 55))
    # 末端圆点（滑块感）
    if fw >= h + 2 and h >= 10:
        cx = x + fw
        cr = h // 2 + 2
        d.ellipse((cx - cr, y - 2, cx + cr, y + h + 2), fill=(255, 255, 255, 235))
        d.ellipse((cx - cr + 3, y + 1, cx + cr - 3, y + h - 1), fill=color)


def _stacked_bar(im, x, y, w, h, segments, radius=8):
    """segments: [(value, color), ...]"""
    total = sum(v for v, _ in segments) or 1
    bar = Image.new('RGBA', (w, h), (0, 0, 0, 0))
    bd = ImageDraw.Draw(bar)
    cx = 0
    for val, color in segments:
        seg_w = int(round(w * val / total))
        bd.rectangle((cx, 0, cx + seg_w, h), fill=color)
        cx += seg_w
    # 圆角遮罩，保证整体两端圆润
    mask = Image.new('L', (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w, h), radius=radius, fill=255)
    bar.putalpha(mask)
    im.alpha_composite(bar, (x, y))


# ----------------------------------------------------------------------
# 背景与装饰
# ----------------------------------------------------------------------
def _make_bg(w: int, h: int) -> Image.Image:
    im = _vertical_gradient(w, h, [
        (0.0, _SKY_TOP), (0.45, _SKY_MID), (1.0, _PINK_BOT),
    ])
    # 圆点纹理
    d = ImageDraw.Draw(im)
    step = 48
    for yy in range(0, h, step):
        for xx in range((yy // step) % 2 * 24, w, step):
            d.ellipse((xx, yy, xx + 4, yy + 4), fill=(255, 255, 255, 90))
    # 顶部柔光
    glow = Image.new('RGBA', (w, 360), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((w // 2 - 300, -220, w // 2 + 300, 220), fill=(255, 255, 255, 70))
    im.alpha_composite(glow.filter(ImageFilter.GaussianBlur(30)), (0, 0))
    return im


def _brand_mark(im: Image.Image, w: int, label: str = 'Milk'):
    """左上角品牌标识：简洁文字小牌。"""
    d = ImageDraw.Draw(im)
    font = _font_bold(20)
    tw = _text_len(d, label, font)
    chip_w = int(tw + 36)
    box = (36, 30, 36 + chip_w, 78)
    _card(im, box, radius=24, fill=(255, 255, 255, 235), shadow=False)
    d.text((box[0] + chip_w // 2, box[1] + 24), label, font=font, fill=_ACCENT, anchor='mm')


def _period_chip(im: Image.Image, w: int, text: str):
    """右上角周期/类型小牌。"""
    if not text:
        return
    d = ImageDraw.Draw(im)
    font = _font_bold(18)
    tw = _text_len(d, text, font)
    chip_w = int(tw + 36)
    x1 = w - 36 - chip_w
    box = (x1, 30, x1 + chip_w, 76)
    _card(im, box, radius=23, fill=(255, 255, 255, 220), shadow=False)
    d.text((x1 + chip_w // 2, box[1] + 23), image_safe_text(text),
           font=font, fill=_TEXT_SOFT, anchor='mm')


def _footer(im: Image.Image, w: int, h: int, extra: str = ''):
    d = ImageDraw.Draw(im)
    official = footer_generated()
    design = 'Milk Design'
    parts = [design, official]
    if extra:
        parts.insert(1, extra)
    text = '  ·  '.join(parts)
    font = _font_bold(14)
    tw = _text_len(d, text, font)
    x = (w - tw) / 2 - 24
    y = h - 50
    _card(im, (int(x), int(y), int(x + tw + 48), int(y + 38)), radius=19,
          fill=(255, 255, 255, 210), shadow=False)
    d.text((w // 2, y + 19), image_safe_text(text), font=font, fill=_TEXT_SOFT, anchor='mm')


def _finalize(im: Image.Image) -> BytesIO:
    bio = BytesIO()
    im.convert('RGB').save(bio, format='PNG', optimize=True)
    bio.seek(0)
    return bio


# ----------------------------------------------------------------------
# 头像
# ----------------------------------------------------------------------
async def _fetch_avatars(qqs: Sequence[int], size: int = 96) -> Dict[int, Image.Image]:
    """并发拉取 QQ 头像；失败回退到首字头像。"""
    result: Dict[int, Image.Image] = {}
    if not qqs:
        return result

    async def _one(qq: int):
        if not qq or not str(qq).isdigit() or int(qq) <= 0:
            return qq, None
        try:
            async with httpx.AsyncClient(timeout=8) as cli:
                r = await cli.get('http://q1.qlogo.cn/g',
                                  params={'b': 'qq', 'nk': qq, 's': 100})
                r.raise_for_status()
            img = Image.open(BytesIO(r.content)).convert('RGBA')
            return qq, img.resize((size, size))
        except Exception:
            return qq, None

    tasks = [_one(q) for q in set(qqs) if q]
    for qq, img in await asyncio.gather(*tasks):
        if img is not None:
            result[qq] = img
    return result


def _fallback_avatar(size: int, name: str, color) -> Image.Image:
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((0, 0, size, size), fill=color)
    initial = (name.strip() or '?')[0]
    font = _font_bold(int(size * 0.5))
    bbox = d.textbbox((0, 0), image_safe_text(initial), font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text((size / 2 - tw / 2 - bbox[0], size / 2 - th / 2 - bbox[1]),
           image_safe_text(initial), font=font, fill=(255, 255, 255, 255))
    return img


def _cover_placeholder(size: int, char: str = '♪', color=_ACCENT) -> Image.Image:
    """方形曲绘占位图。"""
    img = Image.new('RGBA', (size, size), color)
    d = ImageDraw.Draw(img)
    font = _font_bold(int(size * 0.5))
    bbox = d.textbbox((0, 0), image_safe_text(char), font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text((size / 2 - tw / 2 - bbox[0], size / 2 - th / 2 - bbox[1]),
           image_safe_text(char), font=font, fill=(255, 255, 255, 255))
    return img


def _get_avatar(avatars: Dict[int, Image.Image], qq: int, name: str, color,
                size: int = 72) -> Image.Image:
    img = avatars.get(qq)
    if img is None:
        return _fallback_avatar(size, name, color)
    return img.resize((size, size))


# ----------------------------------------------------------------------
# 排名徽章 & rating 徽章
# ----------------------------------------------------------------------
def _rank_medal(rank: int):
    if rank == 1:
        return _GOLD, (150, 100, 20, 255)
    if rank == 2:
        return _SILVER, (110, 120, 140, 255)
    if rank == 3:
        return _BRONZE, (130, 80, 40, 255)
    return (255, 255, 255, 235), _TEXT_SOFT


def _draw_rank_badge(im: Image.Image, cx: int, cy: int, rank: int, radius: int = 26):
    fill, fg = _rank_medal(rank)
    d = ImageDraw.Draw(im)
    d.ellipse((cx - radius, cy - radius, cx + radius, cy + radius),
              fill=(0, 0, 0, 30))
    d.ellipse((cx - radius, cy - radius + 3, cx + radius, cy + radius + 3),
              fill=(0, 0, 0, 25))
    d.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=fill)
    if rank <= 3:
        d.ellipse((cx - radius, cy - radius, cx + radius, cy + radius),
                  outline=(255, 255, 255, 230), width=3)
    font = _font_mono(26 if rank < 100 else 20)
    d.text((cx, cy), str(rank), font=font, fill=fg, anchor='mm')


def _draw_rating_badge(im: Image.Image, right_x: int, cy: int, rating: int,
                       height: int = 34) -> Tuple[int, int]:
    """右侧对齐绘制 rating 徽章：优先游戏 UI_CMN_DXRating 贴图，否则回退彩色胶囊。"""
    bw, bh = draw_rating_badge(im, right_x - rating_badge_width(height),
                               cy - height // 2, rating, height=height)
    if bw > 0:
        return bw, bh
    # 回退：彩色胶囊
    color = rating_color(rating)
    d = ImageDraw.Draw(im)
    mono = _font_mono(int(height * 0.7))
    lab_font = _font_bold(10)
    num_w = _text_len(d, str(rating), mono)
    pill_w = int(num_w + 24 + 44)
    pill_h = height
    x = right_x - pill_w
    y = cy - pill_h // 2
    _card(im, (x, y, x + pill_w, y + pill_h), radius=pill_h // 2,
          fill=color, shadow=False)
    d.rounded_rectangle((x + 6, y + 6, x + 46, y + pill_h - 6),
                        radius=(pill_h - 12) // 2, fill=(0, 0, 0, 90))
    d.text((x + 26, y + pill_h // 2), 'RATING', font=lab_font,
           fill=(255, 255, 255, 230), anchor='mm')
    d.text((x + 54 + num_w / 2, y + pill_h // 2), str(rating),
           font=mono, fill=(255, 255, 255, 255), anchor='mm')
    return pill_w, pill_h


# ----------------------------------------------------------------------
# 统计面板
# ----------------------------------------------------------------------
def _stat_box(im, x, y, w, h, value, label, color=_TEXT):
    _card(im, (x, y, x + w, y + h), radius=16, fill=(255, 255, 255, 235), shadow=False)
    d = ImageDraw.Draw(im)
    vfont = _font_mono(26)
    lfont = _font_bold(14)
    d.text((x + w // 2, y + h // 2 - 10), str(value), font=vfont, fill=color, anchor='mm')
    d.text((x + w // 2, y + h // 2 + 18), str(label), font=lfont, fill=_MUTED, anchor='mm')


def _donut(im, cx, cy, r, data, total, center_text, center_sub):
    """data: [(label, value, color), ...]"""
    d = ImageDraw.Draw(im)
    width = 22
    start = -90.0
    # 背景环
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(255, 255, 255, 180))
    for label, value, color in data:
        if value <= 0:
            continue
        extent = 360.0 * value / total
        d.pieslice((cx - r, cy - r, cx + r, cy + r), start, start + extent,
                   fill=color)
        start += extent
    # 挖空
    inner = r - width
    d.ellipse((cx - inner, cy - inner, cx + inner, cy + inner), fill=_PINK_BOT)
    cfont = _font_mono(34)
    sfont = _font_bold(14)
    d.text((cx, cy - 12), str(center_text), font=cfont, fill=_TEXT, anchor='mm')
    d.text((cx, cy + 18), str(center_sub), font=sfont, fill=_MUTED, anchor='mm')


def _legend(im, x, y, data, total, line_h=30):
    d = ImageDraw.Draw(im)
    font = _font_bold(15)
    num_font = _font_mono(15)
    for label, value, color in data:
        d.ellipse((x, y + 6, x + 14, y + 20), fill=color)
        d.text((x + 22, y + 4), label, font=font, fill=_TEXT_SOFT)
        cnt = f'{value}人'
        pct = f'{value / total * 100:.1f}%' if total else '0%'
        d.text((x + 150, y + 4), cnt, font=font, fill=_TEXT)
        d.text((x + 210, y + 4), pct, font=num_font, fill=_MUTED)
        y += line_h
    return y


# ----------------------------------------------------------------------
# 群 rating 排行榜
# ----------------------------------------------------------------------
async def render_rating_ranking(
    rows: Sequence[Tuple[int, str, int]],
    title: str = '群 Rating 排行',
    subtitle: str = '',
    self_qq: Optional[int] = None,
    self_rank: Optional[int] = None,
    all_rows: Optional[Sequence[Tuple[int, str, int]]] = None,
) -> BytesIO:
    """rows: 显示的前 N 行 [(uid, name, rating), ...] 已降序；
    all_rows: 全群数据，用于统计面板（默认等于 rows）。"""
    width = 1080
    mx = 40
    inner_w = width - mx * 2
    row_h = 96
    gap = 14
    n = len(rows)
    stats_h = 360
    header_h = 110
    content_h = header_h + n * (row_h + gap) + stats_h + 40
    total_h = content_h + 90

    im = _make_bg(width, total_h)
    d = ImageDraw.Draw(im)
    _brand_mark(im, width)
    _period_chip(im, width, 'Rating')
    avatars = await _fetch_avatars([r[0] for r in rows])

    # 大标题
    _draw_title(d, mx + 120, 44, 34, title, _TEXT)
    if subtitle:
        _draw_title(d, mx + 120, 88, 18, subtitle, _TEXT_SOFT)

    y = header_h + 20
    max_ra = max((r[2] for r in rows), default=1) or 1
    bar_max = max(max_ra, int(math.ceil(max_ra / 1000) * 1000))

    for idx, (uid, name, ra) in enumerate(rows):
        rank = idx + 1
        is_self = (self_qq is not None and uid == self_qq)
        box = (mx, y, mx + inner_w, y + row_h)
        _card(im, box, radius=20,
              fill=(255, 255, 255, 245) if not is_self else (238, 240, 255, 250),
              border=_ACCENT if is_self else _CARD_BORDER,
              border_w=3 if is_self else 2)
        # 排名徽章
        _draw_rank_badge(im, mx + 36, y + row_h // 2, rank)
        # 头像
        av = _get_avatar(avatars, uid, name, rating_color(ra), 66)
        _paste_round(im, av, (mx + 78, y + 15), radius=14)
        # 名字
        nfont = _font_bold(26)
        name_text = _truncate(d, name, nfont, 300)
        d.text((mx + 158, y + 22), name_text, font=nfont, fill=_TEXT)
        if is_self:
            d.text((mx + 158 + _text_len(d, name_text, nfont) + 10, y + 28),
                   '（你）', font=_font_bold(16), fill=_ACCENT)
        # 进度条 + 刻度
        bar_x = mx + 158
        bar_w = inner_w - 158 - 200
        _bar(im, bar_x, y + 58, bar_w, 14, ra / bar_max, rating_color(ra),
             bg=(225, 230, 242, 200))
        sfont = _font_mono(12)
        d.text((bar_x, y + 76), '0', font=sfont, fill=_MUTED)
        d.text((bar_x + bar_w, y + 76), f'{bar_max}', font=sfont, fill=_MUTED, anchor='rt')
        # rating 徽章
        _draw_rating_badge(im, mx + inner_w - 14, y + row_h // 2, ra)
        y += row_h + gap

    # ---------- 统计面板 ----------
    panel_y = y + 10
    _card(im, (mx, panel_y, mx + inner_w, panel_y + stats_h - 20), radius=24,
          fill=(255, 255, 255, 200))
    d.text((mx + 28, panel_y + 22), '全群 Rating 段位分布',
           font=_font_bold(20), fill=_TEXT)

    # 统计基于全群数据，而非仅前 N 名
    stats_rows = all_rows if all_rows is not None else rows
    ratings = [r[2] for r in stats_rows]
    total_count = len(stats_rows)
    avg = sum(ratings) / len(ratings) if ratings else 0
    median = sorted(ratings)[len(ratings) // 2] if ratings else 0
    max_ra_all = max(ratings, default=0)
    box_w = (inner_w - 56 - 3 * 14) // 4
    sb_y = panel_y + 60
    for i, (val, lab) in enumerate([
        (total_count, '总人数'), (f'{avg:.0f}', '平均分'),
        (f'{max_ra_all}', '最高分'), (f'{median:.0f}', '中位数'),
    ]):
        _stat_box(im, mx + 28 + i * (box_w + 14), sb_y, box_w, 80, val, lab)

    # 环形 + 图例
    bucket_counts = []
    for threshold, label, color in _RATING_BUCKETS:
        cnt = sum(1 for r in ratings if (r >= threshold if threshold > 0 else r < 12000)
                  and (r < threshold + 1000 if threshold > 0 else True))
        if cnt:
            bucket_counts.append((label, cnt, color))
    total = sum(c for _, c, _ in bucket_counts) or 1
    donut_cx = mx + 150
    donut_cy = panel_y + 230
    _donut(im, donut_cx, donut_cy, 80, bucket_counts, total, str(total_count), '总人数')
    _legend(im, mx + 280, panel_y + 150, bucket_counts, total)

    _footer(im, width, total_h, f'显示前 {n} / 共 {total_count} 人')
    return _finalize(im)


# ----------------------------------------------------------------------
# 单曲成绩排行榜
# ----------------------------------------------------------------------
def _rate_label(rate_key: str) -> str:
    mapping = {
        'sssp': 'SSSp', 'sss': 'SSS', 'ssp': 'SSp', 'ss': 'SS',
        'sp': 'Sp', 's': 'S', 'aaa': 'AAA', 'aa': 'AA', 'a': 'A',
        'bbb': 'BBB', 'bb': 'BB', 'b': 'B', 'c': 'C', 'd': 'D',
    }
    return mapping.get((rate_key or '').lower(), (rate_key or '').upper())


def _draw_rate_badge(im, x, y, rate_key, scale=1.0):
    # 优先使用游戏内评级贴图（与 B50 一致）
    h = int(44 * scale)
    sw, sh = draw_rank_sprite(im, x, y + (int(40 * scale) - h) // 2, h, rate_key)
    if sw > 0:
        return sw, sh
    label = _rate_label(rate_key)
    color = _RATE_COLORS.get(label, _MUTED)
    d = ImageDraw.Draw(im)
    fs = int(28 * scale)
    font = _font_mono(fs)
    tw = _text_len(d, label, font)
    w = int(tw + 24 * scale)
    h = int(40 * scale)
    _card(im, (x, y, x + w, y + h), radius=int(10 * scale),
          fill=color, shadow=False)
    d.text((x + w // 2, y + h // 2), label, font=font,
           fill=(255, 255, 255, 255), anchor='mm')
    return w, h


def _draw_fc_badge(im, x, y, fc, fs_code):
    d = ImageDraw.Draw(im)
    badges = []
    if fc:
        labels = {'fc': 'FC', 'fcp': 'FC+', 'ap': 'AP', 'app': 'AP+'}
        colors = {'fc': (90, 200, 120, 255), 'fcp': (70, 180, 110, 255),
                  'ap': (255, 196, 76, 255), 'app': (255, 170, 60, 255)}
        badges.append((labels.get(fc, fc.upper()), colors.get(fc, _GREEN)))
    if fs_code:
        labels = {'fs': 'SYNC', 'fsp': 'SYNC+', 'fdx': 'FDX',
                  'fsd': 'FDX', 'fdxp': 'FDX+', 'fsdp': 'FDX+'}
        colors = {'fs': (120, 200, 240, 255), 'fsp': (90, 180, 230, 255),
                  'fdx': (170, 130, 240, 255), 'fsd': (170, 130, 240, 255),
                  'fdxp': (200, 110, 230, 255), 'fsdp': (200, 110, 230, 255)}
        badges.append((labels.get(fs_code, fs_code.upper()), colors.get(fs_code, _BLUE)))
    font = _font_bold(15)
    cx = x
    for text, color in badges:
        tw = _text_len(d, text, font)
        w = int(tw + 16)
        _card(im, (cx, y, cx + w, y + 28), radius=14, fill=color, shadow=False)
        d.text((cx + w // 2, y + 14), text, font=font, fill=(255, 255, 255, 255), anchor='mm')
        cx += w + 8
    return cx


async def render_song_leaderboard(
    rows: Sequence[Tuple[int, str, dict]],
    music_title: str,
    diff_name: str,
    level_index: int = 3,
    top_n: int = 10,
    total_players: Optional[int] = None,
    self_qq: Optional[int] = None,
    cover_path: Optional[str] = None,
) -> BytesIO:
    """rows: [(uid, name, score_info), ...] score_info: achievements/fc/fs/dxScore..."""
    width = 1080
    mx = 40
    inner_w = width - mx * 2
    n = len(rows)
    row_h = 84
    gap = 12
    header_h = 220
    stats_h = 400
    content_h = header_h + n * (row_h + gap) + stats_h + 40
    total_h = content_h + 90

    im = _make_bg(width, total_h)
    d = ImageDraw.Draw(im)
    _brand_mark(im, width)
    _period_chip(im, width, '单曲排行')
    avatars = await _fetch_avatars([r[0] for r in rows])

    # 顶部曲绘卡片
    hcard_y = 92
    _card(im, (mx, hcard_y, mx + inner_w, hcard_y + 110), radius=22,
          fill=(255, 255, 255, 235))
    # 封面
    try:
        if cover_path and Path(cover_path).is_file():
            cover = Image.open(cover_path).convert('RGBA').resize((88, 88))
        else:
            cover = _cover_placeholder(88, '♪', _DIFF_COLORS[level_index])
    except Exception:
        cover = _cover_placeholder(88, '♪', _DIFF_COLORS[level_index])
    # 曲绘统一使用方形
    im.alpha_composite(cover, (mx + 16, hcard_y + 11))
    d.text((mx + 124, hcard_y + 22),
           _truncate(d, music_title, _font_bold(28), inner_w - 340),
           font=_font_bold(28), fill=_TEXT)
    # 难度 pill
    diff_color = _DIFF_COLORS[min(level_index, 4)]
    dfont = _font_bold(18)
    dtext = f'{diff_name}'
    dw = _text_len(d, dtext, dfont)
    _card(im, (mx + 124, hcard_y + 64, mx + 124 + int(dw) + 32, hcard_y + 96),
          radius=16, fill=diff_color, shadow=False)
    d.text((mx + 124 + 16 + dw / 2, hcard_y + 80), dtext, font=dfont,
           fill=(255, 255, 255, 255), anchor='mm')
    # Top N 标签
    tag = f'群内 Top {n}'
    tfont = _font_bold(16)
    tw = _text_len(d, tag, tfont)
    _card(im, (mx + 124 + int(dw) + 46, hcard_y + 66,
               mx + 124 + int(dw) + 46 + int(tw) + 28, hcard_y + 94),
          radius=14, fill=(235, 238, 250, 255), shadow=False)
    d.text((mx + 124 + int(dw) + 60 + tw / 2, hcard_y + 80), tag,
           font=tfont, fill=_TEXT_SOFT, anchor='mm')

    y = header_h + 20
    achvs = [float(r[2].get('achievements', 0)) for r in rows]

    for idx, (uid, name, info) in enumerate(rows):
        rank = idx + 1
        is_self = (self_qq is not None and uid == self_qq)
        achv = float(info.get('achievements', 0))
        box = (mx, y, mx + inner_w, y + row_h)
        _card(im, box, radius=18,
              fill=(255, 255, 255, 245) if not is_self else (238, 240, 255, 250),
              border=_ACCENT if is_self else _CARD_BORDER,
              border_w=3 if is_self else 2)
        _draw_rank_badge(im, mx + 32, y + row_h // 2, rank, radius=24)
        av = _get_avatar(avatars, uid, name, rating_color(int(achv * 100)), 60)
        _paste_round(im, av, (mx + 70, y + 12), radius=12)
        nfont = _font_bold(24)
        d.text((mx + 144, y + 14), _truncate(d, name, nfont, 220), font=nfont, fill=_TEXT)
        d.text((mx + 144, y + 50), f'#{uid}',
               font=_font_mono(13), fill=_MUTED)
        # 达成率
        d.text((mx + 380, y + row_h // 2), f'{achv:.4f}%',
               font=_font_mono(34),
               fill=_TEXT, anchor='lm')
        # DX 分
        dx = info.get('dxScore')
        if dx is not None:
            d.text((mx + 620, y + 20), f'{dx}',
                   font=_font_mono(18), fill=_TEXT_SOFT)
        # FC/FS
        _draw_fc_badge(im, mx + 620, y + 48, info.get('fc'), info.get('fs'))
        # 评级徽章
        rate = info.get('rate') or ''
        rw, rh = _draw_rate_badge(im, mx + inner_w - 120, y + (row_h - 40) // 2, rate, 1.0)
        y += row_h + gap

    # ---------- 统计面板 ----------
    panel_y = y + 10
    _card(im, (mx, panel_y, mx + inner_w, panel_y + stats_h - 20), radius=24,
          fill=(255, 255, 255, 200))
    d.text((mx + 28, panel_y + 22), '成绩分布 · 本群',
           font=_font_bold(20), fill=_TEXT)

    # 统计盒
    tp = total_players if total_players is not None else n
    ap_cnt = sum(1 for r in rows if (r[2].get('fc') or '') in ('ap', 'app'))
    fc_cnt = sum(1 for r in rows if (r[2].get('fc') or '') in ('fc', 'fcp', 'ap', 'app'))
    dx_vals = [r[2].get('dxScore') for r in rows if r[2].get('dxScore') is not None]
    avg_dx = sum(dx_vals) / len(dx_vals) if dx_vals else 0
    if achvs:
        mean_a = sum(achvs) / len(achvs)
        std = math.sqrt(sum((a - mean_a) ** 2 for a in achvs) / len(achvs))
    else:
        mean_a = std = 0
    stats = [
        (tp, '游玩人数'),
        (f'{ap_cnt / n * 100:.1f}%' if n else '0%', 'AP率'),
        (f'{fc_cnt / n * 100:.1f}%' if n else '0%', 'FC率'),
        (f'{avg_dx:.0f}', '平均DX分'),
        (f'{std:.2f}', '标准差'),
    ]
    box_w = (inner_w - 56 - 4 * 12) // 5
    sb_y = panel_y + 60
    for i, (val, lab) in enumerate(stats):
        _stat_box(im, mx + 28 + i * (box_w + 12), sb_y, box_w, 76, val, lab)

    # 平均达成率条
    bar_y = panel_y + 156
    d.text((mx + 28, bar_y), '平均达成率',
           font=_font_bold(15), fill=_TEXT_SOFT)
    d.text((mx + inner_w - 28, bar_y), f'{mean_a:.4f}%',
           font=_font_mono(16), fill=_TEXT, anchor='rt')
    _bar(im, mx + 28, bar_y + 28, inner_w - 56, 14, mean_a / 101, _BLUE)

    # 评级分布
    rate_counts = {k: 0 for k in _RATE_ORDER}
    for r in rows:
        lbl = _rate_label(r[2].get('rate') or '')
        if lbl in rate_counts:
            rate_counts[lbl] += 1
    seg_y = bar_y + 60
    d.text((mx + 28, seg_y), '评级分布',
           font=_font_bold(15), fill=_TEXT_SOFT)
    segs = [(c, _RATE_COLORS[k]) for k, c in rate_counts.items() if c > 0]
    if not segs:
        segs = [(1, (220, 224, 234, 255))]
    _stacked_bar(im, mx + 28, seg_y + 26, inner_w - 56, 26, segs)
    # 图例
    lx = mx + 28
    ly = seg_y + 60
    lfont = _font_mono(13)
    for k, c in rate_counts.items():
        if c <= 0:
            continue
        d.ellipse((lx, ly + 4, lx + 10, ly + 14), fill=_RATE_COLORS[k])
        d.text((lx + 14, ly), f'{k} {c}', font=lfont, fill=_TEXT_SOFT)
        lx += 86
        if lx > mx + inner_w - 120:
            lx, ly = mx + 28, ly + 22

    # FC 分布
    fc_segs = [
        (sum(1 for r in rows if (r[2].get('fc') or '') in ('ap', 'app')), (255, 196, 76, 255)),
        (sum(1 for r in rows if (r[2].get('fc') or '') in ('fc', 'fcp')), (72, 200, 130, 255)),
        (sum(1 for r in rows if not (r[2].get('fc') or '')), (180, 190, 210, 255)),
    ]
    fc_segs = [s for s in fc_segs if s[0] > 0]
    if not fc_segs:
        fc_segs = [(1, (220, 224, 234, 255))]
    fcy = ly + 30
    d.text((mx + 28, fcy), 'FC 分布',
           font=_font_bold(15), fill=_TEXT_SOFT)
    _stacked_bar(im, mx + 28, fcy + 26, inner_w - 56, 22, fc_segs)
    ap_n = sum(1 for r in rows if (r[2].get('fc') or '') in ('ap', 'app'))
    fc_n = sum(1 for r in rows if (r[2].get('fc') or '') in ('fc', 'fcp'))
    no_n = n - ap_n - fc_n
    fl_font = _font_mono(13)
    fl_items = [
        (f'AP {ap_n}', (255, 196, 76, 255)),
        (f'FC {fc_n}', (72, 200, 130, 255)),
        (f'Not FC {no_n}', (180, 190, 210, 255)),
    ]
    flx = mx + 28
    for txt, col in fl_items:
        d.ellipse((flx, fcy + 58, flx + 10, fcy + 68), fill=col)
        d.text((flx + 14, fcy + 55), txt, font=fl_font, fill=_TEXT_SOFT)
        flx += 100

    _footer(im, width, total_h, f'共 {n} 人')
    return _finalize(im)


# ----------------------------------------------------------------------
# 群吃分榜（rating 增量）
# ----------------------------------------------------------------------
async def render_gain_ranking(
    rows: Sequence[Tuple[int, str, int, int, int]],
    title: str, subtitle: str,
    self_qq: Optional[int] = None,
) -> BytesIO:
    width = 1080
    mx = 40
    inner_w = width - mx * 2
    n = len(rows)
    row_h = 92
    gap = 12
    stats_h = 180
    content_h = 120 + n * (row_h + gap) + stats_h + 30
    total_h = content_h + 90

    im = _make_bg(width, total_h)
    d = ImageDraw.Draw(im)
    _brand_mark(im, width)
    _period_chip(im, width, '吃分榜')
    avatars = await _fetch_avatars([r[0] for r in rows])

    _draw_title(d, mx + 150, 44, 32, title, _TEXT)
    _draw_title(d, mx + 150, 86, 18, subtitle, _TEXT_SOFT)

    y = 140
    max_delta = max((abs(r[4]) for r in rows), default=1) or 1
    total_delta = sum(r[4] for r in rows)

    for idx, (uid, name, old_r, new_r, delta) in enumerate(rows):
        rank = idx + 1
        is_self = (self_qq is not None and uid == self_qq)
        box = (mx, y, mx + inner_w, y + row_h)
        _card(im, box, radius=18,
              fill=(255, 255, 255, 245) if not is_self else (238, 240, 255, 250),
              border=_ACCENT if is_self else _CARD_BORDER,
              border_w=3 if is_self else 2)
        _draw_rank_badge(im, mx + 34, y + row_h // 2, rank, radius=24)
        av = _get_avatar(avatars, uid, name, rating_color(new_r), 60)
        _paste_round(im, av, (mx + 72, y + 16), radius=12)
        nfont = _font_bold(24)
        d.text((mx + 148, y + 16), _truncate(d, name, nfont, 340),
               font=nfont, fill=_TEXT)
        d.text((mx + 148, y + 54), f'{old_r}  →  {new_r}',
               font=_font_bold(18), fill=_TEXT_SOFT)
        bar_color = _GREEN if delta > 0 else (_RED if delta < 0 else _MUTED)
        bar_x = mx + 500
        bar_w = inner_w - 500 - 150
        _bar(im, bar_x, y + 40, bar_w, 14, max(0, delta) / max_delta, bar_color)
        sign = '+' if delta > 0 else ''
        d.text((mx + inner_w - 24, y + row_h // 2), f'{sign}{delta}',
               font=_font_mono(30),
               fill=bar_color, anchor='rm')
        d.text((mx + inner_w - 24, y + row_h - 14), 'ra',
               font=_font_mono(13), fill=_MUTED, anchor='rt')
        y += row_h + gap

    # 统计面板
    panel_y = y + 10
    _card(im, (mx, panel_y, mx + inner_w, panel_y + stats_h - 30), radius=22,
          fill=(255, 255, 255, 200))
    avg_d = total_delta / n if n else 0
    box_w = (inner_w - 56 - 2 * 14) // 3
    for i, (val, lab) in enumerate([
        (n, '参与人数'), (f'{avg_d:+.1f}', '平均增量'), (f'{total_delta:+d}', '总增量'),
    ]):
        _stat_box(im, mx + 28 + i * (box_w + 14), panel_y + 24, box_w, 80, val, lab)

    _footer(im, width, total_h, f'共 {n} 人')
    return _finalize(im)


# ----------------------------------------------------------------------
# 群寸止 / 锁血榜
# ----------------------------------------------------------------------
async def render_sun_lock_ranking(
    rows: Sequence[Tuple[int, str, int, int]],
    title: str, subtitle: str,
    mode: str = 'sun',
    self_qq: Optional[int] = None,
) -> BytesIO:
    width = 1080
    mx = 40
    inner_w = width - mx * 2
    n = len(rows)
    row_h = 88
    gap = 12
    stats_h = 160
    content_h = 120 + n * (row_h + gap) + stats_h + 20
    total_h = content_h + 90

    im = _make_bg(width, total_h)
    d = ImageDraw.Draw(im)
    _brand_mark(im, width)
    _period_chip(im, width, '寸止/锁血' if mode == 'sun' else '锁血/寸止')
    avatars = await _fetch_avatars([r[0] for r in rows])

    _draw_title(d, mx + 150, 44, 32, title, _TEXT)
    _draw_title(d, mx + 150, 86, 18, subtitle, _TEXT_SOFT)

    main_color = _GOLD if mode == 'sun' else (120, 200, 255, 255)
    max_main = max((r[2] if mode == 'sun' else r[3] for r in rows), default=1) or 1
    total_main = 0

    y = 140
    for idx, (uid, name, sun_c, lock_c) in enumerate(rows):
        rank = idx + 1
        cnt = sun_c if mode == 'sun' else lock_c
        total_main += cnt
        other = lock_c if mode == 'sun' else sun_c
        other_label = '锁血' if mode == 'sun' else '寸止'
        is_self = (self_qq is not None and uid == self_qq)
        box = (mx, y, mx + inner_w, y + row_h)
        _card(im, box, radius=18,
              fill=(255, 255, 255, 245) if not is_self else (238, 240, 255, 250),
              border=_ACCENT if is_self else _CARD_BORDER,
              border_w=3 if is_self else 2)
        _draw_rank_badge(im, mx + 34, y + row_h // 2, rank, radius=24)
        av = _get_avatar(avatars, uid, name, main_color, 58)
        _paste_round(im, av, (mx + 72, y + 15), radius=12)
        nfont = _font_bold(24)
        d.text((mx + 146, y + 16), _truncate(d, name, nfont, 340),
               font=nfont, fill=_TEXT)
        d.text((mx + 146, y + 54), f'{other_label} {other} 条',
               font=_font_bold(16), fill=_TEXT_SOFT)
        bar_x = mx + 500
        bar_w = inner_w - 500 - 130
        _bar(im, bar_x, y + 38, bar_w, 14, cnt / max_main, main_color)
        d.text((mx + inner_w - 24, y + row_h // 2), f'{cnt}',
               font=_font_mono(32), fill=main_color, anchor='rm')
        d.text((mx + inner_w - 24, y + row_h - 14), '条',
               font=_font_bold(13), fill=_MUTED, anchor='rt')
        y += row_h + gap

    panel_y = y + 10
    _card(im, (mx, panel_y, mx + inner_w, panel_y + stats_h - 30), radius=22,
          fill=(255, 255, 255, 200))
    avg = total_main / n if n else 0
    box_w = (inner_w - 56 - 14) // 2
    for i, (val, lab) in enumerate([(n, '参与人数'), (f'{avg:.1f}', '平均条数')]):
        _stat_box(im, mx + 28 + i * (box_w + 14), panel_y + 24, box_w, 80, val, lab)

    _footer(im, width, total_h, f'共 {n} 人')
    return _finalize(im)


# ----------------------------------------------------------------------
# 今日吃分推荐
# ----------------------------------------------------------------------
_ZONE_META = {
    '稳赚': {'color': _GREEN, 'icon': '▲', 'desc': '高成功率 · 收益稳定'},
    '均衡': {'color': _GOLD, 'icon': '◆', 'desc': '收益与难度兼顾'},
    '冲刺': {'color': _RED, 'icon': '★', 'desc': '高收益 · 需突破'},
}


def render_gain_recommendation(sections: Dict[str, List[dict]],
                               summary_lines: List[str]) -> BytesIO:
    width = 1080
    mx = 40
    inner_w = width - mx * 2
    card_h = 108
    gap = 12
    section_gap = 18

    y = 130
    for zone in ('稳赚', '均衡', '冲刺'):
        items = sections.get(zone) or []
        if not items:
            continue
        y += 50 + section_gap
        y += len(items) * (card_h + gap)
    y += 30
    total_h = y + 80

    im = _make_bg(width, total_h)
    d = ImageDraw.Draw(im)
    _brand_mark(im, width)
    _period_chip(im, width, '吃分推荐')
    _draw_title(d, mx + 150, 44, 32, '今日吃分推荐', _TEXT)
    _draw_title(d, mx + 150, 86, 17, ' · '.join(summary_lines), _TEXT_SOFT)

    y = 140
    for zone in ('稳赚', '均衡', '冲刺'):
        items = sections.get(zone) or []
        if not items:
            continue
        meta = _ZONE_META[zone]
        # 分区标题
        _card(im, (mx, y, mx + 170, y + 42), radius=21,
              fill=meta['color'][:3] + (235,), shadow=False)
        d.text((mx + 24, y + 21), f"{meta['icon']} {zone}",
               font=_font_bold(20),
               fill=(255, 255, 255, 255), anchor='lm')
        d.text((mx + 188, y + 21), meta['desc'],
               font=_font_bold(16), fill=_TEXT_SOFT, anchor='lm')
        y += 52
        for p in items:
            box = (mx, y, mx + inner_w, y + card_h)
            _card(im, box, radius=18, fill=(255, 255, 255, 245), border=_CARD_BORDER)
            # 方形曲绘
            cov_size = 76
            try:
                cov = Image.open(music_picture(p.get('song_id'))).convert('RGBA').resize((cov_size, cov_size))
            except Exception:
                cov = _cover_placeholder(cov_size, "♪", meta["color"])
            im.alpha_composite(cov, (mx + 14, y + 16))
            # 曲名
            nfont = _font_bold(23)
            d.text((mx + 104, y + 16),
                   _truncate(d, p['title'], nfont, 360),
                   font=nfont, fill=_TEXT)
            # 难度
            lvl_font = _font_bold(15)
            lvl_text = f' {p["level"]} '
            lw = _text_len(d, lvl_text, lvl_font) + 16
            _card(im, (mx + 104, y + 52, mx + 104 + int(lw), y + 78),
                  radius=14, fill=(90, 100, 140, 255), shadow=False)
            d.text((mx + 112, y + 65), lvl_text, font=lvl_font,
                   fill=(255, 255, 255, 255), anchor='lm')
            d.text((mx + 104 + int(lw) + 12, y + 65),
                   f"拟合 {p['fit_diff']:.2f} / 定数 {p['ds']:.2f}",
                   font=_font_bold(15), fill=_TEXT_SOFT, anchor='lm')
            # 达成率变化
            d.text((mx + 470, y + 24), f"{p['achv_now']:.4f}%",
                   font=_font_mono(20), fill=_MUTED)
            d.text((mx + 470, y + 56),
                   f"→ {p['achv_target']:.1f}%  (需 +{p['need']:.4f})",
                   font=_font_bold(15), fill=_TEXT)
            # 成功率条
            prob_x = mx + 720
            prob_w = inner_w - 720 - 130
            prob = max(0.0, min(1.0, float(p['probability'])))
            _bar(im, prob_x, y + 32, prob_w, 14, prob, meta['color'])
            d.text((prob_x, y + 54), f"成功率 {prob * 100:.0f}%",
                   font=_font_bold(14), fill=_TEXT_SOFT)
            # 净增
            d.text((mx + inner_w - 24, y + card_h // 2 - 4),
                   f"+{int(p['net_gain'])}",
                   font=_font_mono(34),
                   fill=meta['color'], anchor='rm')
            d.text((mx + inner_w - 24, y + card_h - 16), 'ra',
                   font=_font_mono(14), fill=_MUTED, anchor='rt')
            y += card_h + gap
        y += section_gap

    _footer(im, width, total_h)
    return _finalize(im)


__all__ = [
    'render_rating_ranking',
    'render_song_leaderboard',
    'render_gain_ranking',
    'render_sun_lock_ranking',
    'render_gain_recommendation',
    'rating_color',
]
