"""猜Rating隐藏B50渲染：隐藏成绩/名字/Rating，只保留曲绘/等级/FC/FS。"""

from __future__ import annotations

from typing import List, Optional

from PIL import Image, ImageDraw

from ..config import (
    SIYUAN,
    TBFONT,
    fcl,
    footer_generated,
    fsl,
    maimaidir,
    score_Rank_l,
)
from .image import DrawText, image_to_base64, music_picture
from .maimaidx_model import ChartInfo
from .maimaidx_theme import Theme, resolve_theme_path

# ─────────────── 颜色常量 ───────────────

_BG = (30, 30, 46, 255)
_CARD = (45, 45, 65, 255)
_TITLE = (200, 200, 230, 255)
_MUTED = (140, 140, 170, 255)
_ACCENT = (124, 129, 255, 255)
_HINT = (100, 200, 140, 255)

# 难度底色（与标准B50一致）
_BG_COLOR = [
    (111, 212, 61, 255),    # Basic
    (248, 183, 9, 255),     # Advanced
    (255, 129, 141, 255),   # Expert
    (159, 81, 220, 255),    # Master
    (219, 170, 255, 255),   # Re:Master
]

# 难度底图（类变量缓存）
_diff_bgs: List[Image.Image] = []


def _ensure_diff_bgs():
    global _diff_bgs
    if _diff_bgs:
        return
    _diff_bgs = [Image.new('RGBA', (270, 108), color) for color in _BG_COLOR]


def _board_font():
    for candidate in (SIYUAN, TBFONT):
        try:
            from pathlib import Path
            p = Path(candidate)
            if p.exists():
                return p
        except Exception:
            continue
    return SIYUAN


def _load_theme_file(theme: str, filename: str) -> Optional[Image.Image]:
    p = resolve_theme_path(maimaidir, theme, filename)
    if p.exists():
        return Image.open(p)
    return None


def _draw_hidden_card(
    im: Image.Image,
    chart: ChartInfo,
    x: int,
    y: int,
    theme: str,
    sy: DrawText,
    tb: DrawText,
) -> None:
    """绘制单张隐藏信息卡片：只显示曲绘、难度底色、版本标、等级图标、FC/FS。"""
    _ensure_diff_bgs()
    idx = min(chart.level_index, 4)

    # 难度底色
    im.alpha_composite(_diff_bgs[idx], (x, y))

    # 曲绘
    try:
        cover = Image.open(music_picture(chart.song_id)).resize((75, 75))
        im.alpha_composite(cover, (x + 12, y + 12))
    except Exception:
        pass

    # SD/DX 版本标
    try:
        ver = _load_theme_file(theme, f'{chart.type.upper()}.png')
        if ver:
            im.alpha_composite(ver.resize((37, 14)), (x + 51, y + 91))
    except Exception:
        pass

    # 等级图标 (SSS / SS+ / S 等)
    rate_key = getattr(chart, 'rate', None) or 'd'
    if rate_key.islower() and rate_key in score_Rank_l:
        rate_name = score_Rank_l[rate_key]
    else:
        rate_name = rate_key
    try:
        rate_icon = _load_theme_file(theme, f'UI_TTR_Rank_{rate_name}.png')
        if rate_icon:
            im.alpha_composite(rate_icon.resize((63, 28)), (x + 92, y + 78))
    except Exception:
        pass

    # FC 图标
    if chart.fc:
        try:
            fc_icon = Image.open(
                f'{maimaidir}/pic/UI_MSS_MBase_Icon_{fcl[chart.fc]}.png'
            ).resize((34, 34))
            im.alpha_composite(fc_icon, (x + 154, y + 77))
        except Exception:
            pass

    # FS 图标
    if chart.fs:
        try:
            fs_icon = Image.open(
                f'{maimaidir}/pic/UI_MSS_MBase_Icon_{fsl[chart.fs]}.png'
            ).resize((34, 34))
            im.alpha_composite(fs_icon, (x + 185, y + 77))
        except Exception:
            pass

    # 只在底部画一个占位符方块，隐藏DX星星
    # 完全不绘制任何文字信息（ID、曲名、达成率、ds→ra 全部隐藏）


def render_hidden_b50(
    charts: List[ChartInfo],
    display_count: int,
    time_left: float,
    *,
    theme: str = None,
) -> Image.Image:
    """渲染隐藏信息的B50图。

    Args:
        charts: 随机抽取的谱面列表
        display_count: 展示数量（用于布局计算）
        time_left: 剩余时间（秒）
        theme: 主题名
    Returns:
        PIL Image
    """
    if theme is None:
        theme = Theme.get_default().value

    # 计算布局
    cols = 5
    rows = (len(charts) + cols - 1) // cols
    card_w = 270
    margin_x = 16
    dy = 114  # 行间距

    header_h = 200
    footer_h = 80
    img_w = margin_x * 2 + cols * card_w
    img_h = header_h + rows * dy + footer_h

    im = Image.new('RGBA', (img_w, img_h), _BG)
    dr = ImageDraw.Draw(im)
    sy = DrawText(dr, _board_font())
    tb = DrawText(dr, _board_font())

    # 圆角背景
    dr.rounded_rectangle(
        (10, 10, img_w - 10, img_h - 10),
        radius=18,
        fill=_CARD,
    )

    # 标题
    sy.draw(img_w // 2, 40, 36, '猜猜TA的Rating是多少？', _TITLE, 'mm', 2, (0, 0, 0, 100))

    # 倒计时
    s = max(0, int(time_left))
    if s >= 60:
        time_text = f'⏱ {s // 60}分{s % 60}秒'
    else:
        time_text = f'⏱ {s}秒'
    sy.draw(img_w // 2, 85, 22, time_text, _HINT, 'mm')

    # 提示
    sy.draw(
        img_w // 2, 120, 16,
        f'发送数字作答 · 可修改 · 展示{len(charts)}首/共50首',
        _MUTED, 'mm',
    )

    # 装饰线
    dr.line((60, 155, img_w - 60, 155), fill=_ACCENT, width=2)

    # 副标题
    sy.draw(
        img_w // 2, 178, 15,
        '隐藏了成绩、DX分、曲名、达成率、Rating',
        (100, 100, 130, 255), 'mm',
    )

    # 绘制卡片
    y = header_h
    for num, chart in enumerate(charts):
        col = num % cols
        if col == 0 and num > 0:
            y += dy
        x = margin_x + col * card_w
        _draw_hidden_card(im, chart, x, y, theme, sy, tb)

    # Footer
    sy.draw(
        img_w // 2,
        img_h - 28,
        13,
        f'猜Rating | {footer_generated()}',
        _MUTED, 'mm',
    )

    return im


def hidden_b50_image_segment(charts, display_count, time_left, **kwargs):
    """返回 MessageSegment 格式的隐藏B50图。"""
    from nonebot.adapters.onebot.v11 import MessageSegment
    im = render_hidden_b50(charts, display_count, time_left, **kwargs)
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

    # 同步渲染（DrawBest.draw 是 async 但实际不需要 await）
    import asyncio
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # 在异步上下文中，用线程池
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(asyncio.run, _render())
            return future.result(timeout=30)
    else:
        return asyncio.run(_render())
