"""B50 找内鬼看板渲染。"""

from __future__ import annotations

from typing import List, Optional

from PIL import Image, ImageDraw

from ..config import SIYUAN, footer_generated
from .image import DrawText, image_to_base64
from .maimaidx_best_50 import ScoreBaseImage
from .maimaidx_model import ChartInfo
from .maimaidx_theme import Theme, pic

_CARD_W = 270
_ROW_H = 114
_MARGIN_X = 16
_HEADER_H = 235
_FOOTER_H = 80


def render_impostor_board(
    charts: List[ChartInfo],
    *,
    reveal_index: Optional[int] = None,
    theme: str = None,
) -> Image.Image:
    if theme is None:
        theme = Theme.get_default().value

    img_w = 1400
    img_h = _HEADER_H + _ROW_H + _FOOTER_H
    im = Image.open(pic('b50_bg.png')).convert('RGBA').resize(
        (img_w, img_h), Image.Resampling.LANCZOS,
    )
    overlay = Image.new('RGBA', (img_w - 40, _HEADER_H - 30), (20, 20, 40, 220))
    ImageDraw.Draw(overlay).rounded_rectangle(
        (0, 0, img_w - 40, _HEADER_H - 30),
        radius=20,
        fill=(20, 20, 40, 220),
    )
    im.alpha_composite(overlay, (20, 15))

    dr = ImageDraw.Draw(im)
    dt = DrawText(dr, SIYUAN)
    title = 'B50 找内鬼 · 答案揭晓' if reveal_index else 'B50 找内鬼'
    dt.draw(img_w // 2, 66, 42, title, (255, 255, 255, 255), 'mm', 2, (0, 0, 0, 120))
    if reveal_index:
        hint = f'第 {reveal_index} 张不属于题主 · 来自另一位群友的成绩'
    else:
        hint = '5 张卡片中有 1 张不属于题主 · 发送 1～5 作答'
    dt.draw(img_w // 2, 126, 19, hint, (190, 200, 220, 255), 'mm')

    score_drawer = ScoreBaseImage(im, theme=theme)
    if not score_drawer._diff:
        score_drawer.load_image(theme)
    score_drawer.whiledraw(charts, dx=False, height=_HEADER_H)

    # 给卡片编号；揭晓时框出内鬼。
    dr = ImageDraw.Draw(im)
    dt = DrawText(dr, SIYUAN)
    for index in range(len(charts)):
        x = _MARGIN_X + index * (_CARD_W + 6)
        cx = x + _CARD_W // 2
        dr.ellipse((cx - 19, 198, cx + 19, 236), fill=(20, 20, 40, 245), outline=(255, 255, 255, 255), width=2)
        dt.draw(cx, 217, 20, str(index + 1), (255, 255, 255, 255), 'mm')
        if reveal_index == index + 1:
            dr.rounded_rectangle(
                (x + 2, _HEADER_H + 2, x + _CARD_W - 2, _HEADER_H + _ROW_H - 2),
                radius=10,
                outline=(255, 85, 85, 255),
                width=7,
            )

    footer_bar = Image.new('RGBA', (img_w, 44), (20, 20, 40, 230))
    im.alpha_composite(footer_bar, (0, img_h - 44))
    dt = DrawText(ImageDraw.Draw(im), SIYUAN)
    dt.draw(
        img_w // 2, img_h - 22, 16,
        f'B50 找内鬼 | {footer_generated()}',
        (255, 255, 255, 255), 'mm',
    )
    return im.convert('RGB')


def impostor_image_segment(charts, **kwargs):
    from nonebot.adapters.onebot.v11 import MessageSegment

    return MessageSegment.image(image_to_base64(render_impostor_board(charts, **kwargs)))
