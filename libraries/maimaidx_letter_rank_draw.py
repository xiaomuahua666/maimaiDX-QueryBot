"""舞萌开字母排行榜图（积分 / 贡献 / 时间，带头像）。"""

from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw

from ..config import SIYUAN, TBFONT
from .image import DrawText, image_to_base64
from .maimaidx_api_data import maiApi
from .maimaidx_guess_letter import (
    LetterSettlement,
    format_elapsed,
    letter_image_footer,
    star_text_draw,
    stars_for_draw,
)
from .maimaidx_letter_stats import LetterMemberStats

_BG = (42, 28, 22, 255)
_CARD = (58, 40, 32, 255)
_TITLE = (255, 214, 170, 255)
_TEXT = (245, 236, 224, 255)
_MUTED = (180, 160, 145, 255)
_LINE = (90, 66, 54, 255)
_OK = (120, 210, 140, 255)
_ACCENT = (255, 180, 120, 255)

# 开字母图统一底部留白（与 letter_image_footer 配套）
LETTER_FOOTER_ZONE = 40


def _draw_letter_footer(im: Image.Image, font: DrawText) -> None:
    """所有开字母相关图共用同一套 footer。"""
    font.draw(
        im.width // 2,
        im.height - 22,
        13,
        letter_image_footer(),
        _MUTED,
        "mm",
    )


def _board_font() -> Path:
    for candidate in (
        SIYUAN,
        TBFONT,
        Path("/System/Library/Fonts/PingFang.ttc"),
        Path("/System/Library/Fonts/STHeiti Light.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    ):
        try:
            path = Path(candidate)
            if path.exists():
                return path
        except Exception:
            continue
    return Path(SIYUAN)


def avatar_qq_candidate(uid: str, billing_id: int) -> Optional[int]:
    """QQ 号用于头像；非 QQ / 哈希 billing 返回 None（走占位）。"""
    s = str(uid).strip()
    if s.isdigit() and 5 <= len(s) <= 12:
        return int(s)
    bid = int(billing_id)
    if 10000 <= bid <= 4_000_000_000:
        return bid
    return None


def _circle_avatar(img: Image.Image, size: int) -> Image.Image:
    img = img.resize((size, size), Image.Resampling.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img.convert("RGBA"), (0, 0), mask)
    return out


def _placeholder_avatar(name: str, size: int) -> Image.Image:
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    dr = ImageDraw.Draw(out)
    dr.ellipse((0, 0, size - 1, size - 1), fill=(90, 66, 54, 255), outline=_ACCENT, width=2)
    dt = DrawText(dr, _board_font())
    ch = (name or "?").strip()[:1] or "?"
    dt.draw(size // 2, size // 2, max(14, size // 2), ch, _TITLE, "mm")
    return out


async def _fetch_avatar(qqid: Optional[int], name: str, size: int) -> Image.Image:
    if qqid is not None:
        try:
            raw = await maiApi.qqlogo(qqid=qqid)
            if raw:
                return _circle_avatar(Image.open(BytesIO(raw)), size)
        except Exception:
            pass
    return _placeholder_avatar(name, size)


async def _load_avatars(
    rows: Sequence[Tuple[Optional[int], str]], size: int
) -> List[Image.Image]:
    tasks = [_fetch_avatar(qq, name, size) for qq, name in rows]
    if not tasks:
        return []
    return list(await asyncio.gather(*tasks))


def _draw_rank_panel(
    *,
    title: str,
    subtitle: str,
    rows: List[Tuple[str, str, Image.Image]],
) -> Image.Image:
    """rows: (rank_label, line_text, avatar)"""
    width = 860
    row_h = 64
    header_h = 110
    footer_h = LETTER_FOOTER_ZONE
    n = max(1, len(rows))
    height = header_h + row_h * n + footer_h
    im = Image.new("RGBA", (width, height), _BG)
    dr = ImageDraw.Draw(im)
    font = DrawText(dr, _board_font())
    dr.rounded_rectangle((20, 20, width - 20, height - 20), radius=20, fill=_CARD)
    font.draw(44, 40, 34, title, _TITLE, "lt", 2, (0, 0, 0, 120))
    font.draw(44, 82, 16, stars_for_draw(subtitle), _MUTED, "lt")
    y = header_h
    if not rows:
        font.draw(44, y + 10, 22, "暂无记录", _MUTED, "lt")
        _draw_letter_footer(im, font)
        return im
    for idx, (rank_label, line, avatar) in enumerate(rows):
        if idx > 0:
            dr.line((44, y, width - 44, y), fill=_LINE, width=1)
        av = avatar.resize((44, 44), Image.Resampling.LANCZOS)
        im.alpha_composite(av, (48, y + 10))
        color = _OK if idx < 3 else _TEXT
        font.draw(108, y + 18, 22, rank_label, color, "lt")
        font.draw(168, y + 18, 22, line, _TEXT, "lt")
        y += row_h
    _draw_letter_footer(im, font)
    return im


async def render_score_board(
    members: List[LetterMemberStats],
    *,
    title: str = "开字母积分榜",
    subtitle: str = "按本群结算积分累计",
) -> Image.Image:
    avatars = await _load_avatars(
        [(avatar_qq_candidate(m.uid, m.billing_id), m.name) for m in members], 44
    )
    rows: List[Tuple[str, str, Image.Image]] = []
    for i, m in enumerate(members):
        rows.append(
            (
                f"#{i + 1}",
                f"{m.name}  ·  {m.score} 分  ·  {m.games} 局",
                avatars[i] if i < len(avatars) else _placeholder_avatar(m.name, 44),
            )
        )
    return await asyncio.to_thread(
        _draw_rank_panel, title=title, subtitle=subtitle, rows=rows
    )


async def render_contrib_board(
    members: List[LetterMemberStats],
    *,
    title: str = "开字母贡献榜",
    subtitle: str = "按本群贡献权重累计（开字母×1 / 补齐×3 / 开歌×4）",
) -> Image.Image:
    avatars = await _load_avatars(
        [(avatar_qq_candidate(m.uid, m.billing_id), m.name) for m in members], 44
    )
    rows: List[Tuple[str, str, Image.Image]] = []
    for i, m in enumerate(members):
        rows.append(
            (
                f"#{i + 1}",
                f"{m.name}  ·  权重 {m.weight}  ·  {m.games} 局",
                avatars[i] if i < len(avatars) else _placeholder_avatar(m.name, 44),
            )
        )
    return await asyncio.to_thread(
        _draw_rank_panel, title=title, subtitle=subtitle, rows=rows
    )


async def render_time_board(
    members: List[LetterMemberStats],
    *,
    title: str = "开字母时间榜",
    subtitle: str = "按个人最佳通关用时（越快越靠前）",
) -> Image.Image:
    avatars = await _load_avatars(
        [(avatar_qq_candidate(m.uid, m.billing_id), m.name) for m in members], 44
    )
    rows: List[Tuple[str, str, Image.Image]] = []
    for i, m in enumerate(members):
        el = format_elapsed(m.best_elapsed or 0.0)
        rows.append(
            (
                f"#{i + 1}",
                f"{m.name}  ·  最佳 {el}  ·  {m.games} 局",
                avatars[i] if i < len(avatars) else _placeholder_avatar(m.name, 44),
            )
        )
    return await asyncio.to_thread(
        _draw_rank_panel, title=title, subtitle=subtitle, rows=rows
    )


def _paint_settlement_split(
    settlement: LetterSettlement, avatars: List[Image.Image]
) -> Image.Image:
    """同步绘制结算分成图（供 to_thread）。"""
    width = 900
    row_h = 78
    header_h = 128
    footer_h = LETTER_FOOTER_ZONE
    rewards = list(settlement.rewards)
    n = max(1, len(rewards))
    height = header_h + row_h * n + footer_h
    im = Image.new("RGBA", (width, height), _BG)
    dr = ImageDraw.Draw(im)
    font = DrawText(dr, _board_font())
    dr.rounded_rectangle((20, 20, width - 20, height - 20), radius=20, fill=_CARD)
    font.draw(44, 36, 32, "本局榜单 · 贡献分成", _TITLE, "lt", 2, (0, 0, 0, 120))
    pool_suffix = (
        f"（限时×{settlement.reward_multiplier}）"
        if getattr(settlement, "reward_multiplier", 1) > 1
        else ""
    )
    font.draw(
        44,
        78,
        16,
        (
            f"用时 {settlement.elapsed_text} · {star_text_draw(settlement.stars)}"
            f"  ·  奖池 {settlement.score_pool} 分 / {settlement.break_pool} BREAK"
            f"{pool_suffix}"
        ),
        _MUTED,
        "lt",
    )
    font.draw(44, 100, 14, stars_for_draw(settlement.thresholds_text), _MUTED, "lt")

    if not rewards:
        font.draw(44, header_h + 12, 22, "本局无人有效贡献", _MUTED, "lt")
        _draw_letter_footer(im, font)
        return im

    y = header_h
    for idx, r in enumerate(rewards):
        if idx > 0:
            dr.line((44, y, width - 44, y), fill=_LINE, width=1)
        av = avatars[idx] if idx < len(avatars) else _placeholder_avatar(r.name, 48)
        im.alpha_composite(av.resize((48, 48), Image.Resampling.LANCZOS), (48, y + 14))
        rank_color = _OK if idx < 3 else _TEXT
        font.draw(112, y + 12, 22, f"#{idx + 1}", rank_color, "lt")
        font.draw(168, y + 12, 22, r.name, _TEXT, "lt")
        break_line = f"+{r.break_points} BREAK"
        if r.break_capped:
            break_line += " ⚠️ 该游戏达今日上限不发放奖励"
        font.draw(
            168,
            y + 40,
            16,
            f"{r.detail}  ·  权重 {r.weight}  →  +{r.score} 分  {break_line}",
            _MUTED,
            "lt",
        )
        y += row_h
    _draw_letter_footer(im, font)
    return im


async def render_settlement_split(settlement: LetterSettlement) -> Image.Image:
    """本局榜单+分成：贡献、权重、分到的分/BREAK（一张综合图）。"""
    rewards = list(settlement.rewards)
    if not rewards:
        return await asyncio.to_thread(_paint_settlement_split, settlement, [])
    avatars = await _load_avatars(
        [(avatar_qq_candidate(r.uid, r.billing_id), r.name) for r in rewards], 48
    )
    return await asyncio.to_thread(_paint_settlement_split, settlement, avatars)


def image_b64(im: Image.Image) -> str:
    return image_to_base64(im)
