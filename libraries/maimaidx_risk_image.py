"""
B50 风险预警的现代化图片渲染。

与排行榜 / 报告统一的明亮卡片风格：
- Hero：昵称、存档天数、风险曲目数、最高风险分、风险分布条
- 列表：方形曲绘、曲名、难度 pill、B35/B15 标签、达成率、ra、
  风险分（按高/中/低配色）、风险原因标签
"""
from __future__ import annotations

from io import BytesIO
from typing import Dict, List, Optional

from PIL import Image, ImageDraw

from .image import music_picture
from .maimaidx_leaderboard_image import (
    _ACCENT, _CARD_BORDER, _DIFF_COLORS, _GREEN, _MUTED, _TEXT,
    _TEXT_SOFT, _brand_mark, _card, _cover_placeholder, _finalize,
    _font_bold, _font_mono, _footer, _make_bg, _period_chip, _text_len,
    _truncate,
)

# 风险等级配色
_HIGH = (235, 92, 116, 255)    # 高风险 红
_WARN = (245, 158, 66, 255)    # 中风险 橙
_LOW = (96, 165, 250, 255)     # 低风险 蓝
_SAFE = (72, 200, 130, 255)    # 安全 绿

_WIDTH = 1080
_MX = 40


def _risk_color(score: int):
    if score >= 40:
        return _HIGH
    if score >= 25:
        return _WARN
    if score >= 14:
        return _LOW
    return _SAFE


def _risk_label(score: int) -> str:
    if score >= 40:
        return '高危'
    if score >= 25:
        return '警示'
    if score >= 14:
        return '关注'
    return '平稳'


def _cover(song_id: Optional[int], size: int, color=_ACCENT) -> Image.Image:
    try:
        if song_id:
            p = music_picture(song_id)
            return Image.open(p).convert('RGBA').resize((size, size))
    except Exception:
        pass
    return _cover_placeholder(size, '♪', color)


def _reason_chips(im, d, x, y, max_w, reasons, color):
    """把风险原因渲染成一排小 pill，返回换行后的 y。"""
    chip_h = 24
    gap_x = 8
    gap_y = 8
    pad_x = 12
    cx = x
    cy = y
    for reason in reasons:
        f = _font_bold(13)
        tw = int(_text_len(d, reason, f))
        cw = tw + pad_x * 2
        if cx + cw > x + max_w and cx > x:
            cx = x
            cy += chip_h + gap_y
        _card(im, (cx, cy, cx + cw, cy + chip_h), radius=12,
              fill=color[:3] + (28,), shadow=False)
        d.text((cx + pad_x, cy + chip_h // 2), reason, font=f,
               fill=color, anchor='lm')
        cx += cw + gap_x
    return cy + chip_h


def _draw_item(im, d, x, y, w, idx, item):
    """绘制单条风险曲目卡，返回卡片高度。"""
    score = int(item.get('score', 0))
    color = _risk_color(score)
    zone = item.get('zone', '')
    level_index = min(max(int(item.get('level_index', 3)), 0), 4)
    diff_c = _DIFF_COLORS[level_index]

    # 先估算原因 chip 占行（1~2 行），决定卡片高度
    reasons = list(item.get('reasons') or [])
    chip_line_h = 24
    n_lines = 1
    if reasons:
        f = _font_bold(13)
        cx = 0
        max_text_w = w - 96 - 220 - 24
        for reason in reasons:
            cw = int(_text_len(d, reason, f)) + 24 + 8
            if cx + cw > max_text_w and cx > 0:
                n_lines += 1
                cx = 0
            cx += cw
    reasons_h = n_lines * chip_line_h + max(0, n_lines - 1) * 8
    card_h = max(118, 82 + reasons_h + 18)

    _card(im, (x, y, x + w, y + card_h), radius=18,
          fill=(255, 255, 255, 245), border=_CARD_BORDER)

    # 左侧风险等级色条
    bar_layer = Image.new('RGBA', (6, card_h - 24), (0, 0, 0, 0))
    ImageDraw.Draw(bar_layer).rounded_rectangle(
        (0, 0, 6, card_h - 24), radius=3, fill=color)
    im.alpha_composite(bar_layer, (x + 14, y + 12))

    # 方形曲绘
    cov_size = 68
    cov = _cover(item.get('song_id'), cov_size, diff_c)
    im.alpha_composite(cov, (x + 30, y + (card_h - cov_size) // 2))

    tx = x + 110
    title_f = _font_bold(22)
    d.text((tx, y + 16),
           _truncate(d, item.get('title', ''), title_f, w - 96 - 220),
           font=title_f, fill=_TEXT)

    # 难度 pill
    lvl_text = f" {item.get('level', '')} "
    lvl_f = _font_bold(14)
    lw = int(_text_len(d, lvl_text, lvl_f)) + 14
    _card(im, (tx, y + 48, tx + lw, y + 72), radius=12,
          fill=(90, 100, 140, 255), shadow=False)
    d.text((tx + 7, y + 60), lvl_text, font=lvl_f,
           fill=(255, 255, 255, 255), anchor='lm')

    # B35/B15 标签
    if zone:
        zf = _font_bold(14)
        zw = int(_text_len(d, zone, zf)) + 18
        _card(im, (tx + lw + 10, y + 48, tx + lw + 10 + zw, y + 72),
              radius=12, fill=diff_c, shadow=False)
        d.text((tx + lw + 10 + zw // 2, y + 60), zone,
               font=zf, fill=(255, 255, 255, 255), anchor='mm')

    # 达成率 + ra
    meta_x = tx + lw + (10 + int(_text_len(d, zone or '', _font_bold(14))) + 18 if zone else 0) + 12
    d.text((meta_x, y + 60),
           f"{float(item.get('achv', 0)):.4f}%  ·  {int(item.get('ra', 0))} ra",
           font=_font_mono(14), fill=_TEXT_SOFT, anchor='lm')

    # 原因 chips
    _reason_chips(im, d, tx, y + 82, w - 96 - 220 - 24, reasons, color)

    # 右侧风险分
    rx = x + w - 24
    d.text((rx, y + 18), f'{score}', font=_font_mono(40),
           fill=color, anchor='rt')
    d.text((rx, y + 60), '风险分', font=_font_bold(13),
           fill=_MUTED, anchor='rt')
    lab_f = _font_bold(15)
    lab_w = int(_text_len(d, _risk_label(score), lab_f)) + 22
    _card(im, (rx - lab_w, y + card_h - 32, rx, y + card_h - 10),
          radius=11, fill=color[:3] + (235,), shadow=False)
    d.text((rx - lab_w // 2, y + card_h - 21), _risk_label(score),
           font=lab_f, fill=(255, 255, 255, 255), anchor='mm')
    return card_h


def render_risk_report(nickname: str,
                       snap_days: int,
                       items: List[Dict],
                       b50_total: int = 50,
                       user_name: str = 'Milk') -> BytesIO:
    """
    渲染 B50 风险预警图。

    items: 每条含 title/level/level_index/song_id/ra/achv/zone/score/reasons
    """
    width = _WIDTH
    mx = _MX
    inner_w = width - mx * 2
    items = items or []

    n_high = sum(1 for i in items if int(i.get('score', 0)) >= 40)
    n_warn = sum(1 for i in items if 25 <= int(i.get('score', 0)) < 40)
    n_low = sum(1 for i in items if 14 <= int(i.get('score', 0)) < 25)
    n_safe = max(0, int(b50_total) - len(items))
    top_score = max((int(i.get('score', 0)) for i in items), default=0)

    # ---- 布局估算 ----
    hero_h = 178
    y = 118 + hero_h + 20
    if items:
        y += 44
        tmp = Image.new('RGBA', (inner_w, 200), (0, 0, 0, 0))
        td = ImageDraw.Draw(tmp)
        for i, it in enumerate(items, 1):
            y += _draw_item(tmp, td, mx, 0, inner_w, i, it) + 12
        y -= 12
    else:
        y += 130
    y += 24
    total_h = y + 80

    im = _make_bg(width, total_h)
    d = ImageDraw.Draw(im)
    _brand_mark(im, width, user_name or nickname or 'Milk')
    _period_chip(im, width, 'B50 风险')

    d.text((mx + 200, 44), 'B50 风险预警', font=_font_bold(32), fill=_TEXT)
    d.text((mx + 200, 86),
           f'{nickname}  ·  近 {snap_days} 天存档  ·  地板 / 寸止 / 锁血 / 下滑综合评分',
           font=_font_bold(17), fill=_TEXT_SOFT)

    # ---- Hero ----
    _card(im, (mx, 118, mx + inner_w, 118 + hero_h), radius=24,
          fill=(255, 255, 255, 230))

    # 左：最高风险分
    d.text((mx + 30, 118 + 22), '最高风险分',
           font=_font_bold(16), fill=_MUTED)
    top_col = _risk_color(top_score) if items else _SAFE
    d.text((mx + 30, 118 + 44), f'{top_score}',
           font=_font_mono(46), fill=top_col)
    if items:
        lab_f = _font_bold(14)
        lab_txt = _risk_label(top_score)
        lw = int(_text_len(d, lab_txt, lab_f)) + 22
        _card(im, (mx + 30, 118 + 100, mx + 30 + lw, 118 + 126),
              radius=13, fill=top_col[:3] + (235,), shadow=False)
        d.text((mx + 30 + lw // 2, 118 + 113), lab_txt,
               font=lab_f, fill=(255, 255, 255, 255), anchor='mm')
    else:
        d.text((mx + 30, 118 + 104), '暂无可评估曲目',
               font=_font_bold(15), fill=_MUTED)

    # 中：四个统计
    stats = [
        (str(len(items)), '风险曲目', _HIGH if n_high else _TEXT),
        (str(b50_total), 'B50 总数', _TEXT),
        (str(n_high), '高危', _HIGH),
        (str(n_warn), '警示', _WARN),
    ]
    sx = mx + 280
    sw = 116
    for i, (val, label, col) in enumerate(stats):
        cx = sx + i * (sw + 14)
        _card(im, (cx, 118 + 26, cx + sw, 118 + 96), radius=16,
              fill=(245, 247, 252, 255), shadow=False)
        d.text((cx + sw // 2, 118 + 50), val,
               font=_font_mono(30), fill=col, anchor='mm')
        d.text((cx + sw // 2, 118 + 78), label,
               font=_font_bold(14), fill=_TEXT_SOFT, anchor='mm')

    # 风险分布条（全宽，高/警/关/平稳；含义见下方图例）
    bar_y = 118 + 134
    dist_total = max(1, n_high + n_warn + n_low + n_safe)
    bx = mx + 30
    bw = inner_w - 60
    ph = 16
    counts = [n_high, n_warn, n_low, n_safe]
    cols = [_HIGH, _WARN, _LOW, _SAFE]
    layer = Image.new('RGBA', (bw, ph), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    acc = 0
    for cnt, c in zip(counts, cols):
        sw_seg = int(round(bw * cnt / dist_total))
        ld.rectangle((acc, 0, acc + sw_seg, ph), fill=c)
        acc += sw_seg
    mask = Image.new('L', (bw, ph), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, bw, ph), radius=8, fill=255)
    layer.putalpha(mask)
    im.alpha_composite(layer, (bx, bar_y))
    # 图例
    leg_y = bar_y + 24
    lx = mx + 30
    for cnt, c, name in ((n_high, _HIGH, '高危'), (n_warn, _WARN, '警示'),
                         (n_low, _LOW, '关注'), (n_safe, _SAFE, '平稳')):
        d.ellipse((lx, leg_y, lx + 10, leg_y + 10), fill=c)
        d.text((lx + 16, leg_y + 5), f'{name} {cnt}',
               font=_font_bold(13), fill=_TEXT_SOFT, anchor='lm')
        lx += int(_text_len(d, f'{name} {cnt}', _font_bold(13))) + 28

    # ---- 列表 ----
    y = 118 + hero_h + 20
    if items:
        d.text((mx, y + 8), '风险曲目', font=_font_bold(22), fill=_TEXT)
        y += 44
        for i, it in enumerate(items, 1):
            h = _draw_item(im, d, mx, y, inner_w, i, it)
            y += h + 12
        y -= 12
    else:
        _card(im, (mx, y, mx + inner_w, y + 120), radius=20,
              fill=(255, 255, 255, 220))
        d.text((width // 2, y + 60),
               '当前 B50 暂无明显挤出风险，继续保持！',
               font=_font_bold(20), fill=_GREEN, anchor='mm')
        y += 120

    _footer(im, width, total_h)
    return _finalize(im)


__all__ = ['render_risk_report']
