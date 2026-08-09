"""
牌子（版本称号）进度的现代化图片渲染。

与排行榜 / 报告 / B50 风险统一的明亮卡片风格：
- Hero：牌子名、目标条件、剩余总数、完成进度条
- 难度统计：Basic/Advanced/Expert/Master(/Re:Master) 各自剩余/总数与进度条
- 剩余曲目列表：方形曲绘、曲名、难度 pill、定数、当前成绩
"""
from __future__ import annotations

from io import BytesIO
from typing import Dict, List, Optional

from PIL import Image, ImageDraw

from .image import music_picture
from .maimaidx_leaderboard_image import (
    _ACCENT, _CARD_BORDER, _DIFF_COLORS, _GREEN, _MUTED, _RED, _TEXT,
    _TEXT_SOFT, _bar, _brand_mark, _card, _cover_placeholder, _finalize,
    _font_bold, _font_mono, _footer, _make_bg, _period_chip, _text_len,
    _truncate, image_safe_text,
)

_WIDTH = 1080
_MX = 40

_DIFF_NAMES = ['Basic', 'Advanced', 'Expert', 'Master', 'Re:Master']


def _cover(song_id: Optional[int], size: int, color=_ACCENT) -> Image.Image:
    try:
        if song_id:
            p = music_picture(song_id)
            return Image.open(p).convert('RGBA').resize((size, size))
    except Exception:
        pass
    return _cover_placeholder(size, '♪', color)


def _draw_song_card(im, d, x, y, w, song):
    card_h = 86
    _card(im, (x, y, x + w, y + card_h), radius=16,
          fill=(255, 255, 255, 245), border=_CARD_BORDER)
    level_index = min(max(int(song.get('level_index', 3)), 0), 4)
    diff_c = _DIFF_COLORS[level_index]
    cov_size = 62
    cov = _cover(song.get('song_id'), cov_size, diff_c)
    im.alpha_composite(cov, (x + 12, y + (card_h - cov_size) // 2))

    tx = x + 88
    nf = _font_bold(21)
    d.text((tx, y + 12),
           _truncate(d, image_safe_text(song.get('title', '')), nf, w - 230),
           font=nf, fill=_TEXT)

    lvl_text = f" {song.get('level', '')} "
    lvl_f = _font_mono(14)
    lw = int(_text_len(d, lvl_text, lvl_f)) + 14
    _card(im, (tx, y + 44, tx + lw, y + 68), radius=12,
          fill=(90, 100, 140, 255), shadow=False)
    d.text((tx + 7, y + 56), lvl_text, font=lvl_f,
           fill=(255, 255, 255, 255), anchor='lm')

    ds_txt = f"定数 {float(song.get('ds', 0)):.1f}"
    d.text((tx + lw + 12, y + 56), ds_txt,
           font=_font_bold(15), fill=_TEXT_SOFT, anchor='lm')

    # 右侧成绩
    record = str(song.get('record') or '').strip()
    if record:
        rec_f = _font_mono(20)
        rec_col = _GREEN if song.get('played') and _is_good(record, song) else _TEXT
        d.text((x + w - 18, y + 30), record,
               font=rec_f, fill=rec_col, anchor='rt')
        d.text((x + w - 18, y + 58), '当前成绩',
               font=_font_bold(12), fill=_MUTED, anchor='rt')
    else:
        d.text((x + w - 18, y + card_h // 2), '未游玩',
               font=_font_bold(18), fill=_MUTED, anchor='rm')
    return card_h


def _is_good(record: str, song: dict) -> bool:
    try:
        return float(record.rstrip('%')) >= 100.0
    except ValueError:
        return record.upper() in ('FC', 'FC+', 'AP', 'AP+', 'FSD', 'FSD+',
                                  'FSDX', 'FSDX+')


def render_plate_progress(*,
                          plate_title: str,
                          goal: str,
                          diffs: List[Dict],
                          songs: Optional[List[Dict]] = None,
                          list_title: str = '',
                          completed: bool = False,
                          notice: str = '',
                          user_name: str = 'Milk') -> BytesIO:
    """
    渲染牌子进度图。

    diffs: [{"name","remaining","total","color"}]
    songs: [{"song_id","title","level","level_index","ds","record","played"}]
    """
    width = _WIDTH
    mx = _MX
    inner_w = width - mx * 2
    songs = songs or []

    total_remain = sum(int(x.get('remaining', 0)) for x in diffs)
    total_charts = sum(int(x.get('total', 0)) for x in diffs) or 1
    done_charts = total_charts - total_remain
    overall_ratio = max(0.0, min(1.0, done_charts / total_charts))

    hero_h = 150
    y = 118 + hero_h + 18

    # 难度统计区
    diff_box_h = 96
    y += diff_box_h + 20

    if completed:
        y += 110
    elif notice:
        y += 70
    elif songs:
        y += 40 + len(songs) * (86 + 10) - 10
    else:
        y += 60
    y += 20
    total_h = y + 80

    im = _make_bg(width, total_h)
    d = ImageDraw.Draw(im)
    _brand_mark(im, width, user_name)
    _period_chip(im, width, '牌子进度')

    d.text((mx + 200, 44), f'{plate_title} 进度', font=_font_bold(32), fill=_TEXT)
    d.text((mx + 200, 86), goal, font=_font_bold(17), fill=_TEXT_SOFT)

    # ---- Hero ----
    _card(im, (mx, 118, mx + inner_w, 118 + hero_h), radius=24,
          fill=(255, 255, 255, 230))
    if completed:
        d.text((mx + 30, 118 + 26), '恭喜完成！',
               font=_font_bold(20), fill=_GREEN)
        d.text((mx + 30, 118 + 54), plate_title,
               font=_font_mono(46), fill=_GREEN)
        d.text((mx + 30, 118 + 110), '该牌子目标已全部达成喵～',
               font=_font_bold(16), fill=_TEXT_SOFT)
    else:
        d.text((mx + 30, 118 + 24), '剩余曲目',
               font=_font_bold(16), fill=_MUTED)
        d.text((mx + 30, 118 + 46), f'{total_remain}',
               font=_font_mono(46), fill=_TEXT)
        d.text((mx + 30, 118 + 114),
               f'已完成 {done_charts} / {total_charts}',
               font=_font_bold(15), fill=_TEXT_SOFT)

    # 右侧总进度条
    bar_x = mx + 300
    bar_w = inner_w - 330
    d.text((bar_x, 118 + 30), '总体完成度',
           font=_font_bold(16), fill=_MUTED)
    pct = overall_ratio * 100
    d.text((bar_x + bar_w, 118 + 26), f'{pct:.1f}%',
           font=_font_mono(28), fill=_ACCENT, anchor='rt')
    _bar(im, bar_x, 118 + 72, bar_w, 18, overall_ratio, _ACCENT)
    d.text((bar_x, 118 + 98), f'还需 {total_remain} 首即可完成',
           font=_font_bold(14), fill=_MUTED)

    # ---- 难度统计 ----
    y = 118 + hero_h + 18
    n = len(diffs)
    gap = 12
    box_w = (inner_w - gap * (n - 1)) // n
    for i, diff in enumerate(diffs):
        bx = mx + i * (box_w + gap)
        remaining = int(diff.get('remaining', 0))
        total = int(diff.get('total', 0)) or 1
        ratio = max(0.0, min(1.0, (total - remaining) / total))
        col = tuple(diff.get('color', _DIFF_COLORS[min(i, 4)]))
        _card(im, (bx, y, bx + box_w, y + diff_box_h), radius=18,
              fill=(255, 255, 255, 235), shadow=False)
        d.ellipse((bx + 16, y + 16, bx + 28, y + 28), fill=col)
        d.text((bx + 34, y + 22), str(diff.get('name', _DIFF_NAMES[i])),
               font=_font_bold(16), fill=_TEXT_SOFT, anchor='lm')
        d.text((bx + 16, y + 38), f'{remaining}',
               font=_font_mono(28), fill=_TEXT)
        d.text((bx + 16 + int(_text_len(d, f'{remaining}', _font_mono(28))) + 6,
                y + 50), f'/ {total}',
               font=_font_bold(14), fill=_MUTED, anchor='lm')
        _bar(im, bx + 16, y + diff_box_h - 20, box_w - 32, 10, ratio, col)
    y += diff_box_h + 20

    # ---- 列表 / 提示 ----
    if completed:
        _card(im, (mx, y, mx + inner_w, y + 110), radius=20,
              fill=(255, 255, 255, 220))
        d.text((width // 2, y + 40), '🎉 所有曲目均已达成目标',
               font=_font_bold(22), fill=_GREEN, anchor='mm')
        d.text((width // 2, y + 74), '继续挑战下一个牌子吧～',
               font=_font_bold(16), fill=_TEXT_SOFT, anchor='mm')
    elif notice:
        _card(im, (mx, y, mx + inner_w, y + 70), radius=18,
              fill=(255, 255, 255, 225))
        d.text((width // 2, y + 35), notice,
               font=_font_bold(18), fill=_TEXT_SOFT, anchor='mm')
    elif songs:
        d.text((mx, y + 6), list_title or '剩余曲目',
               font=_font_bold(22), fill=_TEXT)
        y += 40
        for song in songs:
            h = _draw_song_card(im, d, mx, y, inner_w, song)
            y += h + 10
        y -= 10

    _footer(im, width, total_h)
    return _finalize(im)


__all__ = ['render_plate_progress']
