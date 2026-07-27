"""本群猜歌排行榜图（带头像）。"""

from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw

from ..config import SIYUAN, TBFONT
from .image import DrawText, image_to_base64
from .maimaidx_api_data import maiApi

_BG = (28, 34, 48, 255)
_CARD = (40, 48, 66, 255)
_TITLE = (255, 232, 200, 255)
_TEXT = (236, 240, 248, 255)
_MUTED = (150, 162, 184, 255)
_LINE = (70, 82, 108, 255)
_OK = (120, 210, 140, 255)
_GOLD = (255, 200, 80, 255)
_SILVER = (200, 210, 230, 255)
_BRONZE = (210, 150, 100, 255)
_FOOTER = 40


def _font() -> Path:
    for candidate in (
        SIYUAN,
        TBFONT,
        Path('/System/Library/Fonts/PingFang.ttc'),
        Path('/System/Library/Fonts/STHeiti Light.ttc'),
        Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'),
    ):
        try:
            path = Path(candidate)
            if path.exists():
                return path
        except Exception:
            continue
    return Path(SIYUAN)


def _circle_avatar(img: Image.Image, size: int) -> Image.Image:
    img = img.resize((size, size), Image.Resampling.LANCZOS)
    mask = Image.new('L', (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    out = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    out.paste(img.convert('RGBA'), (0, 0), mask)
    return out


def _placeholder_avatar(name: str, size: int) -> Image.Image:
    out = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    dr = ImageDraw.Draw(out)
    dr.ellipse((0, 0, size - 1, size - 1), fill=(58, 68, 92, 255), outline=_MUTED, width=2)
    dt = DrawText(dr, _font())
    ch = (name or '?').strip()[:1] or '?'
    dt.draw(size // 2, size // 2, max(14, size // 2), ch, _TITLE, 'mm')
    return out


def _qq_from_uid(uid: str) -> Optional[int]:
    s = str(uid).strip()
    if s.isdigit() and 5 <= len(s) <= 12:
        return int(s)
    return None


async def _fetch_avatar(qq: Optional[int], name: str, size: int) -> Image.Image:
    if qq is not None:
        try:
            raw = await maiApi.qqlogo(qqid=qq)
            if raw:
                return _circle_avatar(Image.open(BytesIO(raw)), size)
        except Exception:
            pass
    return _placeholder_avatar(name, size)


async def _load_avatars(
    rows: Sequence[Tuple[Optional[int], str]], size: int,
) -> List[Image.Image]:
    tasks = [_fetch_avatar(qq, name, size) for qq, name in rows]
    if not tasks:
        return []
    return list(await asyncio.gather(*tasks))


def _rank_color(index: int) -> Tuple[int, int, int, int]:
    if index == 0:
        return _GOLD
    if index == 1:
        return _SILVER
    if index == 2:
        return _BRONZE
    return _TEXT


def _draw_rank_panel(
    *,
    title: str,
    subtitle: str,
    rows: List[Tuple[str, str, str, Image.Image]],
) -> Image.Image:
    """rows: (rank_label, name, detail, avatar)"""
    width = 860
    row_h = 68
    header_h = 110
    footer_h = _FOOTER
    n = max(1, len(rows))
    height = header_h + row_h * n + footer_h
    im = Image.new('RGBA', (width, height), _BG)
    dr = ImageDraw.Draw(im)
    font = DrawText(dr, _font())
    dr.rounded_rectangle((20, 20, width - 20, height - 20), radius=20, fill=_CARD)
    font.draw(44, 40, 34, title, _TITLE, 'lt', 2, (0, 0, 0, 120))
    font.draw(44, 82, 16, subtitle, _MUTED, 'lt')
    y = header_h
    if not rows:
        font.draw(44, y + 10, 22, '暂无记录', _MUTED, 'lt')
        return im
    for idx, (rank_label, name, detail, avatar) in enumerate(rows):
        if idx > 0:
            dr.line((44, y, width - 44, y), fill=_LINE, width=1)
        av = avatar.resize((48, 48), Image.Resampling.LANCZOS)
        im.alpha_composite(av, (48, y + 10))
        color = _rank_color(idx)
        font.draw(112, y + 12, 22, rank_label, color, 'lt')
        font.draw(168, y + 12, 22, name, _TEXT, 'lt')
        font.draw(168, y + 38, 16, detail, _MUTED, 'lt')
        y += row_h
    return im


def image_b64(im: Image.Image) -> str:
    return image_to_base64(im)


async def render_guess_rank_image(
    ranking: List[Tuple[str, str, int]],
    *,
    title: str = '本群猜歌积分榜',
    subtitle: str = '按累计积分排名',
) -> Image.Image:
    """ranking: [(uid, name, score), ...]"""
    avatars = await _load_avatars(
        [(_qq_from_uid(uid), name) for uid, name, _ in ranking], 48,
    )
    rows: List[Tuple[str, str, str, Image.Image]] = []
    for i, (uid, name, score) in enumerate(ranking):
        rows.append((
            f'#{i + 1}',
            name,
            f'{score} 分',
            avatars[i] if i < len(avatars) else _placeholder_avatar(name, 48),
        ))
    return await asyncio.to_thread(_draw_rank_panel, title=title, subtitle=subtitle, rows=rows)
