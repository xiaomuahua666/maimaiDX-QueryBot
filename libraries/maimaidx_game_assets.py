"""
游戏内素材与字体的统一加载（带缓存 + 缺失回退）。

- 评级贴图 UI_TTR_Rank_<RATE>.png（SSSp ... D）
- Rating 等级条 UI_CMN_DXRating_<NN>.png + 数字贴图 UI_NUM_Drating_<0-9>.png
- 数字字体 Torus SemiBold（与 B50 达成率数字一致）
- 中文粗体 ResourceHanRoundedCN

所有素材均通过主题路径解析，找不到时回退到程序化绘制，保证本地/精简部署可用。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

from ..config import SIYUAN, TBFONT


# rating 段位配色
_RATING_TIERS = [
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


def rating_color(rating: int):
    for threshold, color in _RATING_TIERS:
        if rating < threshold:
            return color
    return _RAINBOW


# ----------------------------------------------------------------------
# 字体
# ----------------------------------------------------------------------
@lru_cache(maxsize=8)
def bold_font(size: int) -> ImageFont.FreeTypeFont:
    """中文粗体（思源圆体）。"""
    for p in (SIYUAN, TBFONT):
        try:
            if Path(p).is_file():
                return ImageFont.truetype(str(p), size)
        except OSError:
            continue
    return ImageFont.load_default()


def bold_font_path() -> str:
    """返回中文粗体字体路径（供 DrawText 使用）。"""
    for p in (SIYUAN, TBFONT):
        if Path(p).is_file():
            return str(p)
    return str(SIYUAN)


@lru_cache(maxsize=32)
def num_font(size: int) -> ImageFont.FreeTypeFont:
    """数字字体（Torus SemiBold，与 B50 达成率一致）。"""
    for p in (TBFONT, SIYUAN):
        try:
            if Path(p).is_file():
                return ImageFont.truetype(str(p), size)
        except OSError:
            continue
    return ImageFont.load_default()


# ----------------------------------------------------------------------
# 主题素材路径
# ----------------------------------------------------------------------
def _theme_pic(filename: str) -> Optional[Path]:
    try:
        from .maimaidx_theme import Theme, resolve_theme_path
        from ..config import maimaidir
        p = resolve_theme_path(maimaidir, Theme.get_default().value, filename)
        return p if p.is_file() else None
    except Exception:
        return None


_RATE_MAP = {
    'sssp': 'SSSp', 'sss': 'SSS', 'ssp': 'SSp', 'ss': 'SS',
    'sp': 'Sp', 's': 'S', 'aaa': 'AAA', 'aa': 'AA', 'a': 'A',
    'bbb': 'BBB', 'bb': 'BB', 'b': 'B', 'c': 'C', 'd': 'D',
}


def _rate_sprite_name(rate_key: str) -> str:
    key = (rate_key or '').lower()
    return _RATE_MAP.get(key, (rate_key or 'D').upper())


@lru_cache(maxsize=32)
def _load_rank_sprite(rate_key: str) -> Optional[Image.Image]:
    name = _rate_sprite_name(rate_key)
    p = _theme_pic(f'UI_TTR_Rank_{name}.png')
    if not p:
        return None
    try:
        return Image.open(p).convert('RGBA')
    except Exception:
        return None


def has_rank_sprite(rate_key: str) -> bool:
    return _load_rank_sprite(rate_key) is not None


def draw_rank_sprite(im: Image.Image, x: int, y: int, height: int,
                     rate_key: str) -> Tuple[int, int]:
    """在 (x,y) 左上角绘制评级贴图，返回 (宽, 高)。失败时返回 (0,0)。"""
    sprite = _load_rank_sprite(rate_key)
    if sprite is None:
        return 0, 0
    w = int(sprite.width * height / sprite.height)
    resized = sprite.resize((w, height), Image.LANCZOS)
    im.alpha_composite(resized, (x, y))
    return w, height


# ----------------------------------------------------------------------
# Rating 等级条（游戏 UI_CMN_DXRating 贴图 + 数字贴图）
# ----------------------------------------------------------------------
def _rating_tier_num(rating: int) -> str:
    if rating < 1000:
        n = 1
    elif rating < 2000:
        n = 2
    elif rating < 4000:
        n = 3
    elif rating < 7000:
        n = 4
    elif rating < 10000:
        n = 5
    elif rating < 12000:
        n = 6
    elif rating < 13000:
        n = 7
    elif rating < 14000:
        n = 8
    elif rating < 14500:
        n = 9
    elif rating < 15000:
        n = 10
    else:
        n = 11
    return f'{n:02d}'


@lru_cache(maxsize=16)
def _load_rating_bar(num: str) -> Optional[Image.Image]:
    p = _theme_pic(f'UI_CMN_DXRating_{num}.png')
    if not p:
        return None
    try:
        return Image.open(p).convert('RGBA')
    except Exception:
        return None


@lru_cache(maxsize=10)
def _load_drating_digit(d: str) -> Optional[Image.Image]:
    p = _theme_pic(f'UI_NUM_Drating_{d}.png')
    if not p:
        return None
    try:
        return Image.open(p).convert('RGBA')
    except Exception:
        return None


def draw_rating_badge(im: Image.Image, x: int, y: int, rating: int,
                      height: int = 30) -> Tuple[int, int]:
    """
    绘制游戏风格 rating 徽章：UI_CMN_DXRating 等级条 + 5 位数字贴图。
    返回 (宽, 高)。缺素材时返回 (0,0) 由调用方回退。
    """
    num = _rating_tier_num(rating)
    bar = _load_rating_bar(num)
    if bar is None:
        return 0, 0
    w = int(bar.width * height / bar.height)
    bar_r = bar.resize((w, height), Image.LANCZOS)
    im.alpha_composite(bar_r, (x, y))

    digits = f'{int(rating):05d}'
    digit_h = int(height * 20 / 35)
    digit_w = int(height * 17 / 35)
    step = int(height * 15 / 35)
    dx0 = x + int(height * 85 / 35)
    dy = y + int(height * 8 / 35)
    any_digit = False
    for i, ch in enumerate(digits):
        dimg = _load_drating_digit(ch)
        if dimg is None:
            continue
        any_digit = True
        im.alpha_composite(dimg.resize((digit_w, digit_h), Image.LANCZOS),
                           (dx0 + i * step, dy))
    if not any_digit:
        # 数字贴图缺失时用 Torus 绘制
        d = ImageDraw.Draw(im)
        f = num_font(int(height * 0.5))
        d.text((dx0 + step * 2, y + height // 2), str(rating),
               font=f, fill=(255, 255, 255, 255), anchor='rm')
    return w, height


def rating_badge_width(height: int = 30) -> int:
    return int(664 * height / 130)


__all__ = [
    'bold_font', 'num_font',
    'draw_rank_sprite', 'has_rank_sprite',
    'draw_rating_badge', 'rating_badge_width',
]
