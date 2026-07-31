"""舞萌极限二选一：左右两张谱面对比看板。"""

from __future__ import annotations

from typing import Optional

from PIL import Image, ImageDraw

from ..config import SIYUAN, TBFONT, footer_generated
from .image import DrawText, image_to_base64
from .maimaidx_guess_duel import ChartRef, DuelRound


_BG = (24, 28, 42, 255)
_PANEL = (40, 48, 66, 255)
_TITLE = (255, 232, 200, 255)
_TEXT = (236, 240, 248, 255)
_MUTED = (150, 162, 184, 255)
_LEFT = (95, 178, 246, 255)
_RIGHT = (232, 132, 92, 255)
_HINT = (180, 200, 220, 255)

_DIFF_NAMES = ['Basic', 'Advanced', 'Expert', 'Master', 'Remaster']
_DIFF_BG = [
    (111, 212, 61, 255),
    (248, 183, 9, 255),
    (255, 129, 141, 255),
    (159, 81, 220, 255),
    (219, 170, 255, 255),
]


def _color_for_level(level_index: int) -> tuple:
    if 0 <= level_index < len(_DIFF_BG):
        return _DIFF_BG[level_index]
    return (180, 180, 180, 255)


def _load_cover(music_id: str) -> Optional[Image.Image]:
    try:
        from .image import music_picture
        path = music_picture(music_id)
        return Image.open(path).convert('RGBA').resize((220, 220))
    except Exception:
        return None


def _draw_card(
    im: Image.Image,
    ref: ChartRef,
    *,
    x: int,
    y: int,
    side_label: str,
    side_color: tuple,
    reveal_value: Optional[str] = None,
    hide_level: bool = False,
    hide_diff_badge: bool = False,
) -> None:
    card_w, card_h = 580, 620
    dr = ImageDraw.Draw(im)
    # 卡片底
    dr.rounded_rectangle(
        (x, y, x + card_w, y + card_h), radius=18, fill=_PANEL,
        outline=side_color, width=4,
    )
    # 顶部彩色条
    dr.rounded_rectangle(
        (x, y, x + card_w, y + 56), radius=18, fill=side_color,
    )
    dt = DrawText(dr, SIYUAN)
    dt.draw(x + card_w // 2, y + 28, 28, side_label, (255, 255, 255, 255), 'mm')

    # 封面
    cover = _load_cover(ref.music_id)
    cover_x = x + (card_w - 220) // 2
    cover_y = y + 80
    if cover is not None:
        im.alpha_composite(cover, (cover_x, cover_y))
    else:
        dr.rectangle(
            (cover_x, cover_y, cover_x + 220, cover_y + 220),
            fill=(60, 64, 80, 255),
        )
        dt.draw(
            cover_x + 110, cover_y + 110, 22, '?',
            (220, 220, 220, 255), 'mm',
        )

    # 难度色条：作答阶段按题型隐藏等级/难度，避免题面直接泄答案
    diff = _DIFF_NAMES[ref.level_index] if 0 <= ref.level_index < len(_DIFF_NAMES) else '?'
    diff_x = cover_x
    diff_y = cover_y + 220
    if hide_diff_badge:
        dr.rounded_rectangle(
            (diff_x, diff_y, diff_x + 220, diff_y + 30),
            radius=6, fill=(90, 96, 112, 255),
        )
        dt.draw(
            diff_x + 110, diff_y + 15, 18,
            '谱面 · ?', (220, 220, 220, 255), 'mm',
        )
    else:
        diff_color = _color_for_level(ref.level_index)
        badge = diff if hide_level else f'{diff}  {ref.level}'
        dr.rounded_rectangle(
            (diff_x, diff_y, diff_x + 220, diff_y + 30),
            radius=6, fill=diff_color,
        )
        dt.draw(
            diff_x + 110, diff_y + 15, 18,
            badge, (40, 40, 40, 255), 'mm',
        )

    # 标题
    title_y = diff_y + 50
    title = ref.title
    if len(title) > 18:
        title = title[:17] + '…'
    dt.draw(x + card_w // 2, title_y, 22, title, _TEXT, 'mm')

    # 类型徽标（SD/DX）
    type_text = 'DX 谱面' if ref.is_dx else '标准谱面'
    type_color = (110, 200, 230, 255) if ref.is_dx else (220, 170, 90, 255)
    type_y = title_y + 36
    dt.draw(x + card_w // 2, type_y, 18, type_text, type_color, 'mm')

    # 揭晓时显示答案；作答中显示提示
    info_y = type_y + 50
    if reveal_value is not None:
        dt.draw(
            x + card_w // 2, info_y, 30, reveal_value,
            side_color, 'mm', 1, (0, 0, 0, 160),
        )
    else:
        dt.draw(
            x + card_w // 2, info_y, 18, '发送「左/右」作答',
            _HINT, 'mm',
        )


def _header(
    im: Image.Image,
    dt: DrawText,
    *,
    title: str,
    subtitle: str,
    width: int,
) -> None:
    dr = ImageDraw.Draw(im)
    # 顶部信息条
    dr.rounded_rectangle(
        (24, 18, width - 24, 130), radius=18, fill=(28, 30, 46, 245),
    )
    dt.draw(width // 2, 50, 30, title, _TITLE, 'mm', 1, (0, 0, 0, 180))
    dt.draw(width // 2, 95, 18, subtitle, _HINT, 'mm')


def _footer(im: Image.Image, dt: DrawText, width: int, height: int) -> None:
    dr = ImageDraw.Draw(im)
    dr.rectangle(
        (0, height - 32, width, height), fill=(20, 22, 32, 240),
    )
    dt.draw(
        width // 2, height - 16, 14,
        f'舞萌极限二选一 | {footer_generated()}',
        _MUTED, 'mm',
    )


def render_duel_board(
    round_obj: DuelRound,
    *,
    reveal: bool = False,
) -> Image.Image:
    width, height = 1280, 880
    im = Image.new('RGBA', (width, height), _BG)
    dr = ImageDraw.Draw(im)
    dt = DrawText(dr, SIYUAN)

    title = '舞萌极限二选一'
    if reveal:
        title = f'舞萌极限二选一 · 第 {round_obj.round_no} 轮答案揭晓'
    subtitle = (
        f'第 {round_obj.round_no}/{round_obj.total_rounds} 轮 · {round_obj.prompt}'
    )
    _header(im, dt, title=title, subtitle=subtitle, width=width)

    card_w, card_h = 580, 620
    gap = 30
    total = card_w * 2 + gap
    start_x = (width - total) // 2
    y = 150

    left_value = _format_answer(round_obj.left, round_obj.question_type) if reveal else None
    right_value = _format_answer(round_obj.right, round_obj.question_type) if reveal else None
    # 定数/等级题：作答阶段不显示等级数字；等级题连难度色条也藏，避免看图秒答
    qtype = round_obj.question_type
    hide_level = (not reveal) and qtype in ('ds', 'level')
    hide_diff_badge = (not reveal) and qtype == 'level'

    _draw_card(
        im, round_obj.left,
        x=start_x, y=y,
        side_label='左', side_color=_LEFT,
        reveal_value=left_value,
        hide_level=hide_level,
        hide_diff_badge=hide_diff_badge,
    )
    _draw_card(
        im, round_obj.right,
        x=start_x + card_w + gap, y=y,
        side_label='右', side_color=_RIGHT,
        reveal_value=right_value,
        hide_level=hide_level,
        hide_diff_badge=hide_diff_badge,
    )

    # 揭晓时高亮正确侧
    if reveal:
        hi = start_x if round_obj.answer == 1 else start_x + card_w + gap
        dr.rounded_rectangle(
            (hi - 6, y - 6, hi + card_w + 6, y + card_h + 6),
            radius=22, outline=(255, 215, 90, 255), width=6,
        )
        # 揭晓底部正确答案
        correct_side = '左' if round_obj.answer == 1 else '右'
        dt.draw(
            width // 2, height - 50, 20,
            f'✅ 正确答案是「{correct_side}」', (255, 215, 90, 255), 'mm',
        )

    _footer(im, dt, width, height)
    return im.convert('RGB')


def _format_answer(ref: ChartRef, qtype: str) -> str:
    if qtype == 'ds':
        return f'定数 {ref.ds:.1f}'
    if qtype == 'notes':
        return f'物量 {ref.notes_total}'
    if qtype == 'bpm':
        return f'BPM {ref.bpm}'
    if qtype == 'version':
        return f'{ref.version}'
    if qtype == 'break':
        return f'BREAK {ref.notes_break}'
    if qtype == 'touch':
        return f'TOUCH {ref.notes_touch}'
    if qtype == 'level':
        return f'等级 {ref.level}'
    return ''


def duel_image_segment(round_obj: DuelRound, *, reveal: bool = False):
    from nonebot.adapters.onebot.v11 import MessageSegment

    return MessageSegment.image(image_to_base64(render_duel_board(round_obj, reveal=reveal)))
