"""猜 Rating 分级看板：隐藏身份与数值成绩，并按难度逐步减少辅助信息。"""

from __future__ import annotations

from typing import List

from PIL import Image, ImageDraw

from ..config import (
    SIYUAN,
    TBFONT,
    fcl,
    footer_generated,
    fsl,
    score_Rank_l,
)
from .image import DrawText, image_to_base64, music_picture
from .maimaidx_model import ChartInfo
from .maimaidx_theme import Theme, pic

# ─────────────── 与标准B50一致的布局常量 ───────────────

_CARD_W = 270
_ROW_H = 114
_COLS = 5
_MARGIN_X = 16
_HEADER_H = 235
_FOOTER_H = 80

# 文字颜色（与 ScoreBaseImage 一致）
_T_COLOR = [
    (255, 255, 255, 255),
    (255, 255, 255, 255),
    (255, 255, 255, 255),
    (255, 255, 255, 255),
    (138, 0, 226, 255),
]

_TITLE_COLOR = (255, 255, 255, 255)
_MUTED = (180, 180, 200, 255)

# 难度底图缓存
_diff_bgs: List[Image.Image] = []


def _ensure_diff_bgs():
    global _diff_bgs
    if _diff_bgs:
        return
    _diff_bgs = [
        Image.open(pic('b50_score_basic.png')),
        Image.open(pic('b50_score_advanced.png')),
        Image.open(pic('b50_score_expert.png')),
        Image.open(pic('b50_score_master.png')),
        Image.open(pic('b50_score_remaster.png')),
    ]


def _board_font():
    from pathlib import Path
    for candidate in (SIYUAN, TBFONT):
        p = Path(candidate)
        if p.exists():
            return p
    return SIYUAN


def _draw_hidden_card(
    im: Image.Image,
    chart: ChartInfo,
    x: int,
    y: int,
    sy: DrawText,
    *,
    show_rate: bool = True,
    show_fc_fs: bool = True,
    hide_cover: bool = False,
) -> None:
    """绘制单张隐藏卡片：曲绘、难度底图、版本标及可选的评级/FC/FS。

    与 ScoreBaseImage.whiledraw 相同坐标，文字区域用 AWMC =w= 占位。
    """
    _ensure_diff_bgs()
    idx = min(chart.level_index, 4)

    im.alpha_composite(_diff_bgs[idx], (x, y))

    if hide_cover:
        # 5 级：曲绘统一换成 0.png（占位曲绘），彻底隐藏所有曲绘信息
        try:
            cover = Image.open(music_picture(0)).resize((75, 75))
            im.alpha_composite(cover.convert('RGBA'), (x + 12, y + 12))
        except Exception:
            pass
    else:
        try:
            cover = Image.open(music_picture(chart.song_id)).resize((75, 75))
            im.alpha_composite(cover, (x + 12, y + 12))
        except Exception:
            pass

    try:
        ver = Image.open(pic(f'{chart.type.upper()}.png')).resize((37, 14))
        im.alpha_composite(ver, (x + 51, y + 91))
    except Exception:
        pass

    if show_rate:
        rate_key = getattr(chart, 'rate', None) or 'd'
        if rate_key.islower() and rate_key in score_Rank_l:
            rate_name = score_Rank_l[rate_key]
        else:
            rate_name = rate_key
        try:
            rate_icon = Image.open(pic(f'UI_TTR_Rank_{rate_name}.png')).resize((63, 28))
            im.alpha_composite(rate_icon, (x + 92, y + 78))
        except Exception:
            pass

    if show_fc_fs and chart.fc:
        try:
            fc_icon = Image.open(pic(f'UI_MSS_MBase_Icon_{fcl[chart.fc]}.png')).resize((34, 34))
            im.alpha_composite(fc_icon, (x + 154, y + 77))
        except Exception:
            pass

    if show_fc_fs and chart.fs:
        try:
            fs_icon = Image.open(pic(f'UI_MSS_MBase_Icon_{fsl[chart.fs]}.png')).resize((34, 34))
            im.alpha_composite(fs_icon, (x + 185, y + 77))
        except Exception:
            pass

    # 空白成绩区域占位（标准B50中曲名/达成率/ds→ra所在位置），颜色与标准B50一致
    sy.draw(x + 93, y + 14, 14, 'AWMC', _T_COLOR[idx], 'lm')
    sy.draw(x + 93, y + 38, 26, '=w=', _T_COLOR[idx], 'lm')
    sy.draw(x + 93, y + 65, 13, '??? -> ???', _T_COLOR[idx], 'lm')


def render_hidden_b50(
    charts: List[ChartInfo],
    display_count: int,
    *,
    total_chart_count: int = 50,
    difficulty: int = 1,
    show_rate: bool = True,
    show_fc_fs: bool = True,
    hide_cover: bool = False,
    theme: str = None,
) -> Image.Image:
    """渲染隐藏信息B50图。使用标准 b50_bg.png 背景 + 标准卡片底图。

    Args:
        charts: 随机抽取的谱面列表
        display_count: 展示数量
        theme: 主题名
    Returns:
        PIL Image（与标准B50同尺寸 1400×N）
    """
    if theme is None:
        theme = Theme.get_default().value

    rows = (len(charts) + _COLS - 1) // _COLS
    img_w = 1400
    img_h = _HEADER_H + rows * _ROW_H + _FOOTER_H

    # 使用标准 B50 背景
    im = Image.open(pic('b50_bg.png')).convert('RGBA').resize((img_w, img_h), Image.Resampling.LANCZOS)
    # 文字统一画在透明图层上最后合成：ImageDraw.text 会直接写入半透明像素，
    # 导致 QQ 缩略图（白底）与预览（黑底）显示不一致
    text_layer = Image.new('RGBA', im.size, (0, 0, 0, 0))
    sy = DrawText(ImageDraw.Draw(text_layer), _board_font())

    # ── 头部遮挡：半透明圆角矩形覆盖玩家信息区域 ──
    header_overlay = Image.new('RGBA', (img_w - 40, _HEADER_H - 30), (20, 20, 40, 210))
    dr_ov = ImageDraw.Draw(header_overlay)
    dr_ov.rounded_rectangle(
        (0, 0, img_w - 40, _HEADER_H - 30),
        radius=20,
        fill=(20, 20, 40, 210),
    )
    im.alpha_composite(header_overlay, (20, 15))

    # 标题
    sy.draw(img_w // 2, 70, 42, '猜猜TA的Rating是多少？', _TITLE_COLOR, 'mm', 2, (0, 0, 0, 120))

    # 提示
    sy.draw(
        img_w // 2, 130, 17,
        f'难度 {difficulty} · 发送数字作答 · 可修改 · '
        f'展示{len(charts)}首/共{total_chart_count}首',
        _MUTED, 'mm',
    )

    # ── 绘制卡片（与标准B50相同坐标）──
    y = _HEADER_H
    for num, chart in enumerate(charts):
        col = num % _COLS
        if col == 0 and num > 0:
            y += _ROW_H
        x = _MARGIN_X + col * (_CARD_W + 6)
        _draw_hidden_card(
            im, chart, x, y, sy,
            show_rate=show_rate,
            show_fc_fs=show_fc_fs,
            hide_cover=hide_cover,
        )

    # ── Footer：明显一点的底栏 ──
    footer_bar = Image.new('RGBA', (img_w, 44), (20, 20, 40, 220))
    im.alpha_composite(footer_bar, (0, img_h - 44))
    sy.draw(
        img_w // 2, img_h - 22, 16,
        f'猜Rating | {footer_generated()}',
        _TITLE_COLOR, 'mm',
    )

    im.alpha_composite(text_layer)
    return im.convert('RGB')


def hidden_b50_image_segment(charts, display_count, **kwargs):
    """返回 MessageSegment 格式的隐藏B50图。"""
    from nonebot.adapters.onebot.v11 import MessageSegment
    im = render_hidden_b50(charts, display_count, **kwargs)
    return MessageSegment.image(image_to_base64(im))


def reveal_b50_image_segment(
    sd_best: List[ChartInfo],
    dx_best: List[ChartInfo],
    target_name: str,
    target_rating: int,
    *,
    theme: str = None,
):
    """揭晓B50：发送完整B50图（复用标准 DrawBest）。"""
    from nonebot.adapters.onebot.v11 import MessageSegment
    from .maimaidx_best_50 import DrawBest
    from .maimaidx_model import Data, UserInfo

    if theme is None:
        theme = Theme.get_default().value

    userinfo = UserInfo(
        additional_rating=None,
        nickname=target_name,
        plate=None,
        rating=target_rating,
        username=target_name,
        charts=Data(sd=sd_best or None, dx=dx_best or None),
    )
    drawer = DrawBest(userinfo, qqid=None, theme=theme)

    async def _render():
        im = await drawer.draw()
        return MessageSegment.image(image_to_base64(im))

    import asyncio
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(asyncio.run, _render())
            return future.result(timeout=30)
    else:
        return asyncio.run(_render())
