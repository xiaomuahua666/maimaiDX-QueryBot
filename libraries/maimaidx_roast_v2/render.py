from __future__ import annotations

import io
import math
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

from ...config import SIYUAN, TBFONT, footer_generated, maiconfig, maimaidir
from ..maimaidx_game_assets import draw_rating_badge
from ..maimaidx_theme import Theme, pic, resolve_theme_path
from .domain import EvidencePack, RoastReport


W = 1600
PAGE_X = 48
CONTENT_X = 88
CONTENT_R = W - CONTENT_X
CONTENT_W = CONTENT_R - CONTENT_X
GAP = 24

BG = (241, 246, 251)
SURFACE = (252, 253, 255, 224)
CARD = (250, 252, 255, 238)
INK = (32, 41, 53)
MUTED = (96, 111, 130)
BLUE = (55, 112, 214)
BLUE_SOFT = (232, 241, 255)
GREEN = (42, 158, 105)
GREEN_SOFT = (229, 246, 237)
ORANGE = (230, 137, 42)
ORANGE_SOFT = (255, 242, 226)
RED = (207, 76, 79)
RED_SOFT = (253, 235, 237)
PURPLE = (124, 91, 190)
LINE = (216, 226, 238)
WHITE = (255, 255, 255)

DIFFICULTY_COLORS = (
    (45, 174, 86),
    (236, 177, 45),
    (224, 72, 77),
    (159, 77, 190),
    (147, 104, 214),
)


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@lru_cache(maxsize=96)
def _font_cached(path: str, size: int):
    candidates = [
        Path(path),
        SIYUAN,
        TBFONT,
        Path(__file__).resolve().parents[2] / "GenSenMaruGothicTW-Regular.ttf",
    ]
    for candidate in candidates:
        try:
            if candidate.is_file():
                return ImageFont.truetype(str(candidate), size)
        except OSError:
            continue
    return ImageFont.load_default()


def _font(path: Path, size: int):
    return _font_cached(str(path), int(size))


def _text_width(font: Any, text: str) -> float:
    try:
        return float(font.getlength(str(text)))
    except AttributeError:
        box = font.getbbox(str(text))
        return float(box[2] - box[0])


def _wrap_lines(text: str, font: Any, max_width: int, max_lines: int | None = None) -> list[str]:
    lines: list[str] = []
    paragraphs = str(text or "").replace("\r", "").split("\n")
    for paragraph in paragraphs or [""]:
        paragraph = paragraph.strip()
        if not paragraph:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        current = ""
        for char in paragraph:
            candidate = current + char
            if current and _text_width(font, candidate) > max_width:
                lines.append(current)
                current = char
            else:
                current = candidate
        if current:
            lines.append(current)
    if not lines:
        lines = [""]
    if max_lines is not None and len(lines) > max_lines:
        lines = lines[:max_lines]
        tail = lines[-1].rstrip("。；，、 ")
        while tail and _text_width(font, tail + "…") > max_width:
            tail = tail[:-1]
        lines[-1] = tail + "…"
    return lines


def _fit_line(text: str, font: Any, max_width: int) -> str:
    value = " ".join(str(text or "").replace("\r", " ").replace("\n", " ").split())
    if _text_width(font, value) <= max_width:
        return value
    tail = value
    while tail and _text_width(font, tail + "…") > max_width:
        tail = tail[:-1]
    return tail + "…"


def _draw_lines(
    draw: ImageDraw.ImageDraw,
    lines: Iterable[str],
    x: int,
    y: int,
    font: Any,
    fill: tuple[int, ...],
    step: int,
) -> int:
    current = y
    for line in lines:
        if line:
            draw.text((x, current), line, font=font, fill=fill)
        current += step
    return current


def _panel(
    im: Image.Image,
    box: tuple[int, int, int, int],
    *,
    radius: int = 18,
    fill: tuple[int, int, int, int] = CARD,
    outline: tuple[int, int, int, int] | tuple[int, int, int] | None = LINE,
    width: int = 2,
) -> None:
    x1, y1, x2, y2 = (int(value) for value in box)
    if x2 <= x1 or y2 <= y1:
        return
    layer = Image.new("RGBA", (x2 - x1, y2 - y1), (0, 0, 0, 0))
    layer_draw = ImageDraw.Draw(layer)
    layer_draw.rounded_rectangle(
        (0, 0, layer.width - 1, layer.height - 1),
        radius=radius,
        fill=fill,
        outline=outline,
        width=width,
    )
    im.alpha_composite(layer, (x1, y1))


def _build_background(height: int) -> Image.Image:
    try:
        path = resolve_theme_path(maimaidir, Theme.get_default().value, "b50_bg.png")
        if path.is_file():
            with Image.open(path) as source:
                tile = source.convert("RGBA")
            scale = W / max(1, tile.width)
            tile = tile.resize((W, max(1, int(tile.height * scale))), Image.Resampling.LANCZOS)
            background = Image.new("RGBA", (W, height), BG + (255,))
            y = 0
            flip = False
            while y < height:
                current = tile.transpose(Image.Transpose.FLIP_TOP_BOTTOM) if flip else tile
                background.alpha_composite(current, (0, y))
                y += current.height
                flip = not flip
            background = background.filter(ImageFilter.GaussianBlur(1.0))
            background = ImageEnhance.Color(background).enhance(0.72)
            background = ImageEnhance.Brightness(background).enhance(1.04)
            background.alpha_composite(Image.new("RGBA", background.size, (248, 251, 255, 62)))
            return background
    except Exception:
        pass
    background = Image.new("RGBA", (W, height), BG + (255,))
    # 精简部署没有主题背景时，也保留轻量的机台节奏纹理，避免报告变成一
    # 张纯白表格；纹理只在底层，正文面板仍保持高对比度。
    pattern = Image.new("RGBA", background.size, (0, 0, 0, 0))
    pattern_draw = ImageDraw.Draw(pattern)
    for offset in range(-height, W + height, 150):
        pattern_draw.line(
            (offset, 0, offset + height, height),
            fill=(92, 157, 218, 22),
            width=34,
        )
    for y in range(90, height, 360):
        pattern_draw.rectangle((0, y, W, y + 3), fill=(237, 142, 66, 28))
    background.alpha_composite(pattern)
    return background


@lru_cache(maxsize=256)
def _load_cover_cached(path: str, width: int, height: int) -> Image.Image | None:
    try:
        candidate = Path(path)
        if not candidate.is_file():
            return None
        with Image.open(candidate) as source:
            image = ImageOps.fit(source.convert("RGBA"), (width, height), method=Image.Resampling.LANCZOS)
        mask = Image.new("L", (width, height), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, width - 1, height - 1), radius=16, fill=255)
        image.putalpha(mask)
        return image
    except Exception:
        return None


def _load_cover(path: str, size: tuple[int, int]) -> Image.Image | None:
    return _load_cover_cached(str(path or ""), int(size[0]), int(size[1]))


@lru_cache(maxsize=48)
def _load_score_texture_cached(level_index: int, width: int, height: int, opacity: int) -> Image.Image | None:
    names = ("basic", "advanced", "expert", "master", "remaster")
    try:
        name = names[max(0, min(len(names) - 1, int(level_index)))]
        path = pic(f"b50_score_{name}.png")
        if not path.is_file():
            return None
        with Image.open(path) as source:
            texture = ImageOps.fit(source.convert("RGBA"), (width, height), method=Image.Resampling.LANCZOS)
        alpha = texture.getchannel("A").point(lambda value: value * opacity // 255)
        mask = Image.new("L", texture.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, width - 1, height - 1), radius=18, fill=255)
        alpha = Image.composite(alpha, Image.new("L", texture.size, 0), mask)
        texture.putalpha(alpha)
        return texture
    except Exception:
        return None


def _paste_game_texture(
    im: Image.Image,
    box: tuple[int, int, int, int],
    level_index: int,
    *,
    opacity: int = 30,
) -> None:
    x1, y1, x2, y2 = box
    texture = _load_score_texture_cached(level_index, x2 - x1, y2 - y1, opacity)
    if texture is None:
        texture = Image.new("RGBA", (x2 - x1, y2 - y1), (0, 0, 0, 0))
        texture_draw = ImageDraw.Draw(texture)
        color = (*_difficulty_color(level_index), max(10, opacity))
        texture_draw.rectangle((0, 0, texture.width, 14), fill=color)
        for offset in range(-texture.height, texture.width, 54):
            texture_draw.line(
                (offset, texture.height, offset + texture.height, 0),
                fill=(*_difficulty_color(level_index), max(6, opacity // 2)),
                width=9,
            )
        mask = Image.new("L", texture.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, texture.width - 1, texture.height - 1), radius=18, fill=255,
        )
        texture.putalpha(Image.composite(texture.getchannel("A"), Image.new("L", texture.size, 0), mask))
    im.alpha_composite(texture, (x1, y1))


def _draw_rating_fallback(im: Image.Image, x: int, y: int, rating: int, height: int = 42) -> tuple[int, int]:
    """当主题缺少游戏 Rating 贴图时，用同样的徽章层级保证视觉统一。"""
    if rating >= 15000:
        color = (116, 207, 239, 255)
        label = "RAINBOW"
    elif rating >= 14500:
        color = (255, 205, 92, 255)
        label = "紫星"
    elif rating >= 14000:
        color = (205, 214, 232, 255)
        label = "白星"
    elif rating >= 13000:
        color = (209, 154, 112, 255)
        label = "铜星"
    else:
        color = (98, 166, 235, 255)
        label = "RATING"
    width = 290
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    layer_draw = ImageDraw.Draw(layer)
    layer_draw.rounded_rectangle((0, 0, width - 1, height - 1), radius=12, fill=color, outline=WHITE, width=2)
    for offset in range(-height, width, 24):
        layer_draw.line((offset, height, offset + height, 0), fill=(255, 255, 255, 46), width=7)
    layer_draw.rounded_rectangle((8, 7, 96, height - 8), radius=8, fill=(255, 255, 255, 72))
    layer_draw.text((52, height // 2), label, font=_font(SIYUAN, 15), fill=INK, anchor="mm")
    layer_draw.text((width - 14, height // 2), f"{int(rating):05d}", font=_font(TBFONT, 25), fill=INK, anchor="rm")
    im.alpha_composite(layer, (x, y))
    return width, height


def _difficulty_color(level_index: Any) -> tuple[int, int, int]:
    index = max(0, min(4, _i(level_index)))
    return DIFFICULTY_COLORS[index]


def _section(draw: ImageDraw.ImageDraw, y: int, title: str, subtitle: str = "") -> int:
    draw.text((CONTENT_X + 4, y), title, font=_font(SIYUAN, 38), fill=INK)
    if subtitle:
        draw.text((CONTENT_X + 4, y + 50), subtitle, font=_font(SIYUAN, 20), fill=MUTED)
        line_y = y + 82
        next_y = y + 104
    else:
        line_y = y + 58
        next_y = y + 80
    draw.line((CONTENT_X + 4, line_y, CONTENT_R - 4, line_y), fill=LINE, width=2)
    return next_y


def _row_key(row: dict[str, Any]) -> tuple[str, str, int]:
    return (
        str(row.get("song_id") or row.get("music_id") or ""),
        str(row.get("chart_type") or row.get("type") or "SD").upper(),
        _i(row.get("level_index"), -1),
    )


def _select_evidence(pack: EvidencePack, limit: int = 12) -> list[tuple[dict[str, Any], str, tuple[int, int, int]]]:
    groups = pack.song_groups or {}
    ordered = (
        ("同段优势", GREEN, groups.get("peer_strong") or []),
        ("同段短板", RED, groups.get("peer_weak") or []),
        ("上限证据", PURPLE, groups.get("top_ra") or []),
        ("槽位地板", ORANGE, groups.get("floors") or []),
        ("选曲特征", BLUE, groups.get("unusual") or []),
        ("成绩证据", BLUE, groups.get("evidence_cards") or []),
    )
    selected: list[tuple[dict[str, Any], str, tuple[int, int, int]]] = []
    seen: set[tuple[str, str, int]] = set()
    for label, color, rows in ordered:
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = _row_key(row)
            if not key[0] or key in seen:
                continue
            seen.add(key)
            selected.append((row, label, color))
            if len(selected) >= limit:
                return selected

    fallback = [(row, "B35 样本", BLUE) for row in sorted(pack.b35, key=lambda item: _i(item.get("ra")), reverse=True)]
    fallback.extend((row, "B15 样本", ORANGE) for row in sorted(pack.b15, key=lambda item: _i(item.get("ra")), reverse=True))
    for row, label, color in fallback:
        key = _row_key(row)
        if not key[0] or key in seen:
            continue
        seen.add(key)
        selected.append((row, label, color))
        if len(selected) >= limit:
            break
    return selected


def _recommendation_rows(pack: EvidencePack, report: RoastReport, limit: int = 5) -> list[dict[str, Any]]:
    candidate_map = {_row_key(item.__dict__): item.__dict__ for item in pack.candidates}
    selected: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int]] = set()
    raw_rows = list(report.recommendations or []) + [item.__dict__ for item in pack.candidates]
    for raw in raw_rows:
        if not isinstance(raw, dict):
            continue
        key = _row_key(raw)
        if not key[0] or key in seen:
            continue
        factual = dict(candidate_map.get(key) or raw)
        if raw.get("reason"):
            factual["reason"] = str(raw.get("reason"))
        seen.add(key)
        selected.append(factual)
        if len(selected) >= limit:
            break
    return selected


def _measure_bullet_group(items: list[str], width: int) -> int:
    font = _font(SIYUAN, 23)
    height = 44
    for item in items[:4]:
        lines = _wrap_lines(str(item), font, width - 54, max_lines=2)
        height += max(48, len(lines) * 32 + 12)
    return height + 8


def _measure_layout(pack: EvidencePack, report: RoastReport) -> dict[str, Any]:
    summary_title_lines = _wrap_lines(report.headline, _font(SIYUAN, 35), CONTENT_W - 80, max_lines=3)
    summary_lines = _wrap_lines(report.summary, _font(SIYUAN, 29), CONTENT_W - 80, max_lines=12)
    summary_h = max(230, 54 + len(summary_title_lines) * 48 + 12 + len(summary_lines) * 42 + 68)
    ds_rows = list(pack.ds_bands or [])
    diagnostics_h = 104 + 480
    ds_h = 104 + 62 + max(1, len(ds_rows)) * 68 + 18
    evidence = _select_evidence(pack, limit=8)
    evidence_rows = max(1, math.ceil(len(evidence) / 2))
    evidence_h = 104 + evidence_rows * 160 + max(0, evidence_rows - 1) * 12
    analysis_lines = _wrap_lines(report.analysis or report.summary, _font(SIYUAN, 28), CONTENT_W - 72, max_lines=40)
    headline_lines = _wrap_lines(report.headline, _font(SIYUAN, 31), CONTENT_W - 72, max_lines=3)
    narrative_h = 48 + len(headline_lines) * 42 + 18 + len(analysis_lines) * 41 + 34
    left_h = _measure_bullet_group(report.strengths, 660) + _measure_bullet_group(report.peer_takeaways, 660)
    right_h = _measure_bullet_group(report.weaknesses, 660) + _measure_bullet_group(report.actions, 660)
    insight_h = max(260, max(left_h, right_h) + 34)
    analysis_h = 104 + narrative_h + 18 + insight_h
    recommendations = _recommendation_rows(pack, report, limit=5)
    route_cards_h = len(recommendations) * 188 + max(0, len(recommendations) - 1) * 14 if recommendations else 92
    routes_h = 104 + route_cards_h + (48 if recommendations else 0)
    method_h = 150
    footer_h = 92
    height = (
        58 + 166 + 22 + summary_h + 24 + diagnostics_h + 24 + ds_h + 24
        + evidence_h + 24 + analysis_h + 24 + routes_h + 24 + method_h + footer_h + 42
    )
    return {
        "height": max(2200, int(height)),
        "summary_title_lines": summary_title_lines,
        "summary_lines": summary_lines,
        "summary_h": summary_h,
        "diagnostics_h": diagnostics_h,
        "ds_rows": ds_rows,
        "ds_h": ds_h,
        "evidence": evidence,
        "evidence_h": evidence_h,
        "analysis_lines": analysis_lines,
        "analysis_headline_lines": headline_lines,
        "narrative_h": narrative_h,
        "insight_h": insight_h,
        "analysis_h": analysis_h,
        "recommendations": recommendations,
        "routes_h": routes_h,
        "method_h": method_h,
        "footer_h": footer_h,
    }


def _draw_header(im: Image.Image, draw: ImageDraw.ImageDraw, pack: EvidencePack, y: int) -> int:
    name_font = _font(SIYUAN, 47)
    draw.text((CONTENT_X, y + 7), _fit_line(pack.nickname, name_font, 860), font=name_font, fill=INK)
    draw.text((CONTENT_X, y + 72), "B50 成绩分析报告", font=_font(SIYUAN, 31), fill=MUTED)
    badge_w, _ = draw_rating_badge(im, CONTENT_X + 520, y + 70, pack.rating, height=42)
    if badge_w == 0:
        _draw_rating_fallback(im, CONTENT_X + 520, y + 70, pack.rating, height=42)
    right_x = CONTENT_R - 430
    draw.text((right_x, y + 16), "PERFORMANCE REPORT", font=_font(TBFONT, 25), fill=BLUE)
    draw.text((right_x, y + 55), f"B50 · {len(pack.b35)} + {len(pack.b15)}", font=_font(TBFONT, 24), fill=INK)
    quality = str((pack.peer or {}).get("confidence_text") or "个人成绩快照")
    draw.text((right_x, y + 94), _fit_line(quality, _font(SIYUAN, 19), 420), font=_font(SIYUAN, 19), fill=MUTED)
    return y + 166


def _draw_summary(im: Image.Image, draw: ImageDraw.ImageDraw, pack: EvidencePack, report: RoastReport, layout: dict[str, Any], y: int) -> int:
    height = layout["summary_h"]
    _panel(im, (CONTENT_X - 8, y, CONTENT_R + 8, y + height), radius=24, fill=(235, 244, 255, 236), outline=(195, 219, 250, 255))
    draw.rounded_rectangle((CONTENT_X + 16, y + 24, CONTENT_X + 27, y + height - 24), radius=6, fill=BLUE)
    draw.text((CONTENT_X + 48, y + 24), "核心结论", font=_font(SIYUAN, 24), fill=BLUE)
    cursor = _draw_lines(draw, layout["summary_title_lines"], CONTENT_X + 48, y + 62, _font(SIYUAN, 35), INK, 48)
    _draw_lines(draw, layout["summary_lines"], CONTENT_X + 48, cursor + 6, _font(SIYUAN, 29), INK, 42)
    metrics = pack.metrics or {}
    peer_position = str((pack.peer or {}).get("position") or "同段数据不足")
    chips = (
        ("B35/B15 差", f"{_f(metrics.get('b35_b15_gap')):+.4f} pp", BLUE),
        ("同段定位", peer_position, GREEN if (pack.peer or {}).get("available") else MUTED),
        ("保守路线", f"{_i(metrics.get('route_count'))} 首候选", ORANGE),
    )
    chip_y = y + height - 58
    chip_w = 386
    for index, (label, value, color) in enumerate(chips):
        x = CONTENT_X + 48 + index * (chip_w + 20)
        draw.text((x, chip_y), label, font=_font(SIYUAN, 18), fill=MUTED)
        value_font = _font(SIYUAN, 20)
        draw.text((x + 122, chip_y - 1), _fit_line(value, value_font, 252), font=value_font, fill=color)
    return y + height


def _draw_achievement_scale(draw: ImageDraw.ImageDraw, x: int, y: int, width: int, label: str, value: float | None, color: tuple[int, int, int], peer_value: float | None = None) -> None:
    label_font = _font(SIYUAN, 22)
    value_font = _font(TBFONT, 24)
    draw.text((x, y), label, font=label_font, fill=INK)
    bar_x = x + 170
    bar_w = width - 315
    bar_y = y + 11
    draw.rounded_rectangle((bar_x, bar_y, bar_x + bar_w, bar_y + 20), radius=10, fill=(225, 232, 241))
    if value is None:
        draw.text((bar_x + bar_w + 22, y), "暂无数据", font=label_font, fill=MUTED)
        return
    low, high = 97.0, 101.0
    ratio = max(0.0, min(1.0, (_f(value) - low) / (high - low)))
    px = bar_x + int(bar_w * ratio)
    draw.rounded_rectangle((bar_x, bar_y, max(bar_x + 6, px), bar_y + 20), radius=10, fill=color)
    draw.ellipse((px - 7, bar_y + 3, px + 7, bar_y + 17), fill=WHITE, outline=color, width=3)
    if peer_value is not None:
        peer_ratio = max(0.0, min(1.0, (_f(peer_value) - low) / (high - low)))
        peer_x = bar_x + int(bar_w * peer_ratio)
        draw.line((peer_x, bar_y - 5, peer_x, bar_y + 25), fill=MUTED, width=3)
    draw.text((bar_x + bar_w + 22, y - 2), f"{_f(value):.4f}%", font=value_font, fill=INK)


def _pool_peer_avg(pack: EvidencePack, label: str) -> float | None:
    for item in pack.metrics.get("pool_profiles", []) or []:
        if str(item.get("label") or "") == label and item.get("peer_avg") is not None:
            return _f(item.get("peer_avg"))
    return None


def _draw_structure_panel(im: Image.Image, draw: ImageDraw.ImageDraw, pack: EvidencePack, box: tuple[int, int, int, int]) -> None:
    x1, y1, x2, y2 = box
    _panel(im, box, radius=20, fill=CARD)
    draw.text((x1 + 28, y1 + 22), "成绩结构", font=_font(SIYUAN, 29), fill=INK)
    draw.text((x2 - 240, y1 + 28), "刻度 97%–101%", font=_font(SIYUAN, 17), fill=MUTED)
    metrics = pack.metrics or {}
    _draw_achievement_scale(draw, x1 + 28, y1 + 78, x2 - x1 - 56, "B35 平均", metrics.get("b35_avg"), BLUE, _pool_peer_avg(pack, "B35"))
    _draw_achievement_scale(draw, x1 + 28, y1 + 138, x2 - x1 - 56, "B15 平均", metrics.get("b15_avg"), ORANGE, _pool_peer_avg(pack, "B15"))
    _draw_achievement_scale(draw, x1 + 28, y1 + 198, x2 - x1 - 56, "14+ 平均", metrics.get("high_avg"), GREEN)
    b35_ra = sum(_i(item.get("ra")) for item in pack.b35)
    b15_ra = sum(_i(item.get("ra")) for item in pack.b15)
    total = max(1, b35_ra + b15_ra)
    stack_x, stack_y, stack_w = x1 + 28, y1 + 264, x2 - x1 - 56
    draw.text((stack_x, stack_y), "Rating 构成", font=_font(SIYUAN, 20), fill=MUTED)
    stack_y += 34
    b35_w = int(stack_w * b35_ra / total)
    draw.rounded_rectangle((stack_x, stack_y, stack_x + stack_w, stack_y + 27), radius=13, fill=(226, 232, 241))
    if b35_w:
        draw.rounded_rectangle((stack_x, stack_y, stack_x + b35_w, stack_y + 27), radius=13, fill=BLUE)
    if b15_ra:
        draw.rounded_rectangle((stack_x + b35_w, stack_y, stack_x + stack_w, stack_y + 27), radius=13, fill=ORANGE)
    draw.text((stack_x, stack_y + 35), f"B35 {b35_ra}", font=_font(TBFONT, 19), fill=BLUE)
    draw.text((stack_x + 210, stack_y + 35), f"B15 {b15_ra}", font=_font(TBFONT, 19), fill=ORANGE)
    chips = (
        ("B35 地板", str(_i(metrics.get("b35_floor"))), ORANGE),
        ("B15 地板", str(_i(metrics.get("b15_floor"))), ORANGE),
        ("最高 RA", str(_i(metrics.get("ceiling"))), PURPLE),
        ("波动", f"σ {_f(metrics.get('achievement_stddev')):.4f} pp", BLUE),
        ("≥SSS", str(_i(metrics.get("sss_count"))), GREEN),
        ("其中 SSS+", str(_i(metrics.get("sssp_count"))), GREEN),
    )
    # Keep both chip rows inside the fixed 430px panel; the previous spacing
    # let the second row fall into the following section heading.
    chip_y = y1 + 360
    chip_gap = 10
    chip_w = (x2 - x1 - 56 - chip_gap * 2) // 3
    for index, (label, value, color) in enumerate(chips):
        row, col = divmod(index, 3)
        cx = x1 + 28 + col * (chip_w + chip_gap)
        cy = chip_y + row * 34
        draw.rounded_rectangle((cx, cy, cx + chip_w, cy + 32), radius=8, fill=(244, 247, 251), outline=LINE, width=1)
        draw.text((cx + 12, cy + 3), label, font=_font(SIYUAN, 14), fill=MUTED)
        value_font = _font(TBFONT, 18)
        draw.text((cx + chip_w - 12, cy + 3), _fit_line(value, value_font, chip_w - 96), font=value_font, fill=color, anchor="ra")


def _draw_peer_panel(im: Image.Image, draw: ImageDraw.ImageDraw, pack: EvidencePack, box: tuple[int, int, int, int]) -> None:
    x1, y1, x2, y2 = box
    peer = pack.peer or {}
    _panel(im, box, radius=20, fill=(249, 252, 255, 239), outline=(205, 223, 242, 255))
    draw.text((x1 + 28, y1 + 22), "同段定位", font=_font(SIYUAN, 29), fill=INK)
    if not peer.get("available"):
        draw.rounded_rectangle((x1 + 28, y1 + 82, x2 - 28, y1 + 158), radius=16, fill=(240, 244, 249))
        draw.text((x1 + 48, y1 + 101), "同段聚合样本不足", font=_font(SIYUAN, 25), fill=MUTED)
        lines = _wrap_lines(str(peer.get("confidence_text") or "本页仅展示个人成绩结构，不生成同段结论。"), _font(SIYUAN, 20), x2 - x1 - 72, max_lines=4)
        _draw_lines(draw, lines, x1 + 36, y1 + 185, _font(SIYUAN, 20), MUTED, 31)
    else:
        draw.text((x1 + 28, y1 + 70), str(peer.get("bucket") or "同段"), font=_font(TBFONT, 22), fill=MUTED)
        arpi = _f(peer.get("arpi"))
        arpi_color = GREEN if arpi >= 0 else RED
        draw.text((x1 + 28, y1 + 104), f"ARPI {arpi:+.4f} pp", font=_font(TBFONT, 35), fill=arpi_color)
        position = str(peer.get("position") or "未知")
        position_w = min(190, max(105, int(_text_width(_font(SIYUAN, 21), position)) + 34))
        draw.rounded_rectangle((x2 - 28 - position_w, y1 + 101, x2 - 28, y1 + 143), radius=14, fill=GREEN_SOFT if arpi >= 0 else RED_SOFT)
        draw.text((x2 - 28 - position_w // 2, y1 + 122), position, font=_font(SIYUAN, 21), fill=arpi_color, anchor="mm")
        p25, median, p75 = peer.get("p25"), peer.get("median"), peer.get("p75")
        rail_x, rail_y, rail_w = x1 + 32, y1 + 176, x2 - x1 - 64
        draw.rounded_rectangle((rail_x, rail_y, rail_x + rail_w, rail_y + 15), radius=7, fill=(218, 228, 240))
        values = [_f(value) for value in (p25, median, p75, arpi) if value is not None]
        low = min(values, default=-0.5)
        high = max(values, default=0.5)
        pad = max(0.1, (high - low) * 0.25)
        low, high = low - pad, high + pad
        for value, color, line_width in ((p25, MUTED, 2), (median, BLUE, 3), (p75, MUTED, 2)):
            if value is None:
                continue
            px = rail_x + int(rail_w * (_f(value) - low) / max(0.001, high - low))
            draw.line((px, rail_y - 6, px, rail_y + 21), fill=color, width=line_width)
        player_x = rail_x + int(rail_w * (arpi - low) / max(0.001, high - low))
        draw.ellipse((player_x - 8, rail_y - 1, player_x + 8, rail_y + 15), fill=arpi_color, outline=WHITE, width=2)
        draw.text((rail_x, rail_y + 27), f"P25 {p25 if p25 is not None else '—'}", font=_font(TBFONT, 16), fill=MUTED)
        draw.text((rail_x + rail_w // 2, rail_y + 27), f"中位 {median if median is not None else '—'}", font=_font(TBFONT, 16), fill=BLUE, anchor="ma")
        draw.text((rail_x + rail_w, rail_y + 27), f"P75 {p75 if p75 is not None else '—'}", font=_font(TBFONT, 16), fill=MUTED, anchor="ra")
        coverage = max(0.0, min(1.0, _f(peer.get("coverage"))))
        progress_y = y1 + 248
        draw.text((x1 + 28, progress_y), "谱面覆盖", font=_font(SIYUAN, 19), fill=MUTED)
        progress_x = x1 + 142
        progress_w = x2 - x1 - 170
        draw.rounded_rectangle((progress_x, progress_y + 5, progress_x + progress_w, progress_y + 23), radius=9, fill=(224, 232, 241))
        draw.rounded_rectangle((progress_x, progress_y + 5, progress_x + int(progress_w * coverage), progress_y + 23), radius=9, fill=BLUE)
        draw.text((x2 - 28, progress_y + 30), f"{_i(peer.get('matched'))}/{_i(pack.metrics.get('chart_count'))} · {coverage * 100:.1f}%", font=_font(TBFONT, 18), fill=INK, anchor="ra")
        rows = (
            ("同段玩家", str(_i(peer.get("player_count")))),
            ("平均重合", f"{_f(peer.get('appear_rate')):.2f}%" if peer.get("appear_rate") is not None else "—"),
            ("数据质量", str(peer.get("confidence") or "low").upper()),
        )
        row_y = y1 + 330
        for label, value in rows:
            draw.text((x1 + 28, row_y), label, font=_font(SIYUAN, 18), fill=MUTED)
            draw.text((x2 - 28, row_y - 1), value, font=_font(TBFONT, 20), fill=INK, anchor="ra")
            row_y += 38
    trend = pack.trend or {}
    if trend.get("available"):
        delta = _i(trend.get("delta"))
        color = GREEN if delta >= 0 else RED
        draw.line((x1 + 28, y2 - 58, x2 - 28, y2 - 58), fill=LINE, width=1)
        draw.text((x1 + 28, y2 - 43), "近期趋势", font=_font(SIYUAN, 18), fill=MUTED)
        draw.text((x2 - 28, y2 - 45), f"{delta:+d} Rating", font=_font(TBFONT, 21), fill=color, anchor="ra")


def _draw_diagnostics(im: Image.Image, draw: ImageDraw.ImageDraw, pack: EvidencePack, y: int) -> int:
    start = y
    y = _section(draw, y, "成绩画像", "个人成绩负责定量，同段聚合只在覆盖与样本足够时参与判断")
    left_w = 908
    _draw_structure_panel(im, draw, pack, (CONTENT_X, y, CONTENT_X + left_w, y + 480))
    _draw_peer_panel(im, draw, pack, (CONTENT_X + left_w + GAP, y, CONTENT_R, y + 480))
    return start + 104 + 480


def _draw_ds_table(im: Image.Image, draw: ImageDraw.ImageDraw, pack: EvidencePack, layout: dict[str, Any], y: int) -> int:
    start = y
    y = _section(draw, y, "定数段分析", "同一张表同时看个人稳定度、同段差距和样本可靠性")
    rows = layout["ds_rows"]
    table_h = 62 + max(1, len(rows)) * 68 + 18
    _panel(im, (CONTENT_X, y, CONTENT_R, y + table_h), radius=18, fill=(251, 252, 254, 242))
    columns = (
        ("定数段", 160), ("曲数", 82), ("个人均分", 178), ("同段均分", 178),
        ("差值", 150), ("平均 RA", 132), ("同段样本", 130), ("判断", 300),
    )
    x = CONTENT_X + 22
    for label, width in columns:
        draw.text((x, y + 19), label, font=_font(SIYUAN, 18), fill=MUTED)
        x += width
    draw.line((CONTENT_X + 16, y + 54, CONTENT_R - 16, y + 54), fill=LINE, width=2)
    if not rows:
        draw.text((CONTENT_X + 24, y + 82), "暂无定数段数据", font=_font(SIYUAN, 22), fill=MUTED)
    for index, row in enumerate(rows):
        row_y = y + 62 + index * 68
        if index % 2:
            draw.rounded_rectangle((CONTENT_X + 14, row_y, CONTENT_R - 14, row_y + 60), radius=9, fill=(246, 249, 252))
        count = _i(row.get("count"))
        user_avg = row.get("avg_achievement")
        peer_avg = row.get("peer_avg")
        gap = row.get("peer_gap")
        sample = row.get("peer_sample_avg")
        if count <= 0:
            verdict, verdict_color = "暂无样本", MUTED
        elif gap is None:
            verdict, verdict_color = "仅个人数据", MUTED
        elif _f(gap) >= 0.20:
            verdict, verdict_color = "同段优势", GREEN
        elif _f(gap) <= -0.20:
            verdict, verdict_color = "重点补强", RED
        else:
            verdict, verdict_color = "接近同段", BLUE
        values = (
            str(row.get("label") or "—"), str(count),
            f"{_f(user_avg):.4f}%" if user_avg is not None else "—",
            f"{_f(peer_avg):.4f}%" if peer_avg is not None else "—",
            f"{_f(gap):+.4f} pp" if gap is not None else "—",
            f"{_f(row.get('avg_ra')):.1f}" if row.get("avg_ra") is not None else "—",
            f"n≈{_i(sample)}" if sample is not None else "—", verdict,
        )
        x = CONTENT_X + 22
        for col_index, ((_, width), value) in enumerate(zip(columns, values)):
            color = verdict_color if col_index in (4, 7) else INK if col_index != 6 else MUTED
            font = _font(TBFONT if col_index in (1, 2, 3, 4, 5, 6) else SIYUAN, 19)
            draw.text((x, row_y + 20), _fit_line(value, font, width - 12), font=font, fill=color)
            x += width
    return start + layout["ds_h"]


def _draw_cover_placeholder(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int]) -> None:
    x1, y1, x2, y2 = box
    draw.rounded_rectangle(box, radius=16, fill=(225, 232, 241))
    draw.text(((x1 + x2) // 2, (y1 + y2) // 2), "♪", font=_font(TBFONT, 34), fill=MUTED, anchor="mm")


def _draw_evidence_card(im: Image.Image, draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], row: dict[str, Any], label: str, label_color: tuple[int, int, int]) -> None:
    x1, y1, x2, y2 = box
    _panel(im, box, radius=18, fill=(250, 252, 255, 242))
    _paste_game_texture(im, box, _i(row.get("level_index")), opacity=24)
    diff_color = _difficulty_color(row.get("level_index"))
    draw.rounded_rectangle((x1, y1, x1 + 9, y2), radius=5, fill=diff_color)
    # Evidence is intentionally secondary to the long-form analysis: keep the
    # cover and metadata readable, but fit the entire card inside the compact
    # 160px row without allowing the old 120px layout to overlap the footer.
    cover_box = (x1 + 20, y1 + 38, x1 + 104, y1 + 122)
    cover = _load_cover(str(row.get("cover_path") or ""), (84, 84))
    if cover is not None:
        im.alpha_composite(cover, (cover_box[0], cover_box[1]))
    else:
        _draw_cover_placeholder(draw, cover_box)
    text_x = x1 + 120
    text_r = x2 - 24
    chip_font = _font(SIYUAN, 14)
    chip_w = min(126, max(78, int(_text_width(chip_font, label)) + 24))
    _panel(im, (text_x, y1 + 13, text_x + chip_w, y1 + 39), radius=9, fill=(*label_color, 26), outline=(*label_color, 74), width=1)
    draw.text((text_x + chip_w // 2, y1 + 26), label, font=chip_font, fill=label_color, anchor="mm")
    pool = "B15" if str(row.get("pool") or "").lower() == "new" else "B35"
    draw.text((text_r, y1 + 18), pool, font=_font(TBFONT, 15), fill=MUTED, anchor="ra")
    title_font = _font(SIYUAN, 21)
    draw.text((text_x, y1 + 47), _fit_line(str(row.get("title") or "未知曲目"), title_font, text_r - text_x - 12), font=title_font, fill=INK)
    chart_type = str(row.get("type") or row.get("chart_type") or "SD").upper()
    meta = f"{chart_type} · {row.get('level') or '—'} · DS {_f(row.get('ds')):.1f} · RA {_i(row.get('ra'))}"
    meta_font = _font(TBFONT, 16)
    draw.text((text_x, y1 + 78), _fit_line(meta, meta_font, text_r - text_x - 12), font=meta_font, fill=diff_color)
    draw.text((text_x, y1 + 103), f"{_f(row.get('achievement')):.4f}%", font=_font(TBFONT, 21), fill=INK)
    if row.get("peer_avg") is not None:
        peer_gap = _f(row.get("peer_gap"))
        peer_color = GREEN if peer_gap >= 0 else RED
        peer_text = f"同段 {_f(row.get('peer_avg')):.4f}% · {peer_gap:+.4f} pp · n={_i(row.get('peer_sample_count'))}"
        peer_font = _font(SIYUAN, 15)
        draw.text((text_x, y1 + 132), _fit_line(peer_text, peer_font, text_r - text_x - 12), font=peer_font, fill=peer_color)
    else:
        detail = str(row.get("genre") or row.get("artist") or "来自用户成绩快照")
        detail_font = _font(SIYUAN, 15)
        draw.text((text_x, y1 + 132), _fit_line(detail, detail_font, text_r - text_x - 12), font=detail_font, fill=MUTED)


def _draw_evidence(im: Image.Image, draw: ImageDraw.ImageDraw, layout: dict[str, Any], y: int) -> int:
    start = y
    evidence = layout["evidence"]
    y = _section(draw, y, "成绩证据", f"{len(evidence)} 张真实成绩卡 · 按同段强弱、上限和槽位地板去重选取")
    if not evidence:
        _panel(im, (CONTENT_X, y, CONTENT_R, y + 96), radius=16, fill=CARD)
        draw.text((CONTENT_X + 28, y + 32), "暂无可展示的成绩证据", font=_font(SIYUAN, 24), fill=MUTED)
        return start + layout["evidence_h"]
    card_w = (CONTENT_W - GAP) // 2
    card_h = 160
    for index, (row, label, color) in enumerate(evidence):
        row_index, col = divmod(index, 2)
        x = CONTENT_X + col * (card_w + GAP)
        card_y = y + row_index * (card_h + 12)
        _draw_evidence_card(im, draw, (x, card_y, x + card_w, card_y + card_h), row, label, color)
    return start + layout["evidence_h"]


def _draw_bullet_group(draw: ImageDraw.ImageDraw, x: int, y: int, width: int, title: str, items: list[str], color: tuple[int, int, int]) -> int:
    draw.text((x, y), title, font=_font(SIYUAN, 25), fill=color)
    current = y + 42
    font = _font(SIYUAN, 23)
    if not items:
        draw.text((x, current), "暂无足够证据", font=font, fill=MUTED)
        return current + 48
    for item in items[:4]:
        lines = _wrap_lines(str(item), font, width - 54, max_lines=2)
        draw.ellipse((x + 2, current + 7, x + 18, current + 23), fill=color)
        _draw_lines(draw, lines, x + 34, current, font, INK, 32)
        current += max(48, len(lines) * 32 + 12)
    return current + 8


def _draw_analysis(im: Image.Image, draw: ImageDraw.ImageDraw, report: RoastReport, layout: dict[str, Any], y: int) -> int:
    start = y
    y = _section(draw, y, "专业分析", "结论先落在数据上，再用自定义风格表达；数值与曲目不会交给模型重算")
    narrative_h = layout["narrative_h"]
    _panel(im, (CONTENT_X, y, CONTENT_R, y + narrative_h), radius=20, fill=(255, 251, 244, 240), outline=(242, 215, 175, 255))
    draw.text((CONTENT_X + 32, y + 24), "分析正文", font=_font(SIYUAN, 23), fill=ORANGE)
    cursor = _draw_lines(draw, layout["analysis_headline_lines"], CONTENT_X + 32, y + 61, _font(SIYUAN, 31), INK, 42)
    _draw_lines(draw, layout["analysis_lines"], CONTENT_X + 32, cursor + 12, _font(SIYUAN, 28), (68, 61, 52), 41)
    insights_y = y + narrative_h + 18
    insight_h = layout["insight_h"]
    _panel(im, (CONTENT_X, insights_y, CONTENT_R, insights_y + insight_h), radius=20, fill=(249, 251, 254, 239))
    col_w = (CONTENT_W - 80 - GAP) // 2
    left_x = CONTENT_X + 28
    right_x = left_x + col_w + GAP
    left_cursor = _draw_bullet_group(draw, left_x, insights_y + 24, col_w, "亮点", report.strengths, GREEN)
    _draw_bullet_group(draw, left_x, left_cursor + 8, col_w, "同段观察", report.peer_takeaways, BLUE)
    right_cursor = _draw_bullet_group(draw, right_x, insights_y + 24, col_w, "风险与短板", report.weaknesses, RED)
    _draw_bullet_group(draw, right_x, right_cursor + 8, col_w, "行动建议", report.actions, ORANGE)
    return start + layout["analysis_h"]


def _draw_route_card(im: Image.Image, draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], row: dict[str, Any], index: int) -> None:
    x1, y1, x2, y2 = box
    _panel(im, box, radius=18, fill=(250, 252, 255, 242))
    _paste_game_texture(im, box, _i(row.get("level_index")), opacity=22)
    diff_color = _difficulty_color(row.get("level_index"))
    draw.rounded_rectangle((x1, y1, x1 + 9, y2), radius=5, fill=diff_color)
    draw.rounded_rectangle((x1 + 18, y1 + 18, x1 + 61, y1 + 61), radius=14, fill=BLUE_SOFT)
    draw.text((x1 + 39, y1 + 39), str(_i(row.get("route_step"), index + 1) or index + 1), font=_font(TBFONT, 20), fill=BLUE, anchor="mm")
    cover = _load_cover(str(row.get("cover_path") or ""), (112, 112))
    cover_x, cover_y = x1 + 76, y1 + 38
    if cover is not None:
        im.alpha_composite(cover, (cover_x, cover_y))
    else:
        _draw_cover_placeholder(draw, (cover_x, cover_y, cover_x + 112, cover_y + 112))
    text_x = x1 + 216
    gain_x = x2 - 306
    title_font = _font(SIYUAN, 27)
    draw.text((text_x, y1 + 20), _fit_line(str(row.get("title") or "未知曲目"), title_font, gain_x - text_x - 24), font=title_font, fill=INK)
    risk = str(row.get("risk") or "稳妥")
    risk_color = GREEN if risk == "稳妥" else ORANGE if risk == "进阶" else RED
    risk_bg = GREEN_SOFT if risk == "稳妥" else ORANGE_SOFT if risk == "进阶" else RED_SOFT
    risk_w = max(70, int(_text_width(_font(SIYUAN, 18), risk)) + 28)
    draw.rounded_rectangle((gain_x - risk_w - 16, y1 + 24, gain_x - 16, y1 + 58), radius=12, fill=risk_bg)
    draw.text((gain_x - 16 - risk_w // 2, y1 + 41), risk, font=_font(SIYUAN, 18), fill=risk_color, anchor="mm")
    chart_type = str(row.get("chart_type") or row.get("type") or "SD").upper()
    meta = f"{chart_type} · {row.get('level') or '—'} · DS {_f(row.get('ds')):.1f} · 当前 {_f(row.get('achievement')):.4f}% / RA {_i(row.get('current_ra'))}"
    draw.text((text_x, y1 + 61), _fit_line(meta, _font(TBFONT, 19), gain_x - text_x - 30), font=_font(TBFONT, 19), fill=diff_color)
    target_achievement = _f(row.get("target_achievement"), 100.0)
    target = str(row.get("target") or "SSS")
    draw.text((text_x, y1 + 94), f"目标 {target} {target_achievement:.1f}% · 目标 RA {_i(row.get('target_ra'))}", font=_font(SIYUAN, 20), fill=BLUE)
    reason_lines = _wrap_lines(str(row.get("reason") or "按当前槽位逐步替换计算"), _font(SIYUAN, 18), gain_x - text_x - 30, max_lines=2)
    _draw_lines(draw, reason_lines, text_x, y1 + 126, _font(SIYUAN, 18), MUTED, 27)
    gain = max(0, _i(row.get("estimated_gain")))
    target_ra = _i(row.get("target_ra"))
    baseline = max(0, target_ra - gain)
    _panel(im, (gain_x, y1 + 22, x2 - 22, y2 - 22), radius=16, fill=(231, 247, 239, 235), outline=(178, 224, 199, 255))
    center_x = (gain_x + x2 - 22) // 2
    draw.text((center_x, y1 + 54), f"+{gain} RA", font=_font(TBFONT, 35), fill=GREEN, anchor="mm")
    draw.text((center_x, y1 + 91), f"{target_ra} - {baseline}", font=_font(TBFONT, 19), fill=INK, anchor="mm")
    cumulative = _i(row.get("cumulative_gain"))
    draw.text((center_x, y1 + 125), f"前 {index + 1} 步累计 +{cumulative}" if cumulative else "边际收益", font=_font(SIYUAN, 17), fill=MUTED, anchor="mm")


def _draw_routes(im: Image.Image, draw: ImageDraw.ImageDraw, layout: dict[str, Any], y: int) -> int:
    start = y
    recommendations = layout["recommendations"]
    y = _section(draw, y, "保守推分路线", "收益按顺序逐槽替换计算；完成一首后重新生成，后续地板会随之变化")
    if not recommendations:
        _panel(im, (CONTENT_X, y, CONTENT_R, y + 92), radius=16, fill=CARD)
        draw.text((CONTENT_X + 28, y + 30), "当前没有足够接近目标线的可靠候选，不建议硬冲高难。", font=_font(SIYUAN, 24), fill=MUTED)
        return start + layout["routes_h"]
    for index, row in enumerate(recommendations):
        card_y = y + index * (188 + 14)
        _draw_route_card(im, draw, (CONTENT_X, card_y, CONTENT_R, card_y + 188), row, index)
    note_y = y + len(recommendations) * 188 + max(0, len(recommendations) - 1) * 14 + 18
    draw.text((CONTENT_X + 4, note_y), "更多详情请前往吃分推荐喵", font=_font(SIYUAN, 21), fill=BLUE)
    return start + layout["routes_h"]


def _draw_method(im: Image.Image, draw: ImageDraw.ImageDraw, pack: EvidencePack, y: int, height: int) -> int:
    _panel(im, (CONTENT_X, y, CONTENT_R, y + height), radius=18, fill=(245, 249, 253, 232), outline=(205, 220, 236, 255))
    draw.text((CONTENT_X + 28, y + 22), "数据口径", font=_font(SIYUAN, 24), fill=BLUE)
    peer = pack.peer or {}
    if peer.get("available"):
        peer_line = f"同段 {peer.get('bucket')} · 玩家 {_i(peer.get('player_count'))} · 匹配 {_i(peer.get('matched'))}/{_i(pack.metrics.get('chart_count'))} · {peer.get('confidence_text')}"
    else:
        peer_line = "同段聚合不可用：本页不生成同段定论，仅展示个人成绩结构。"
    method = f"{peer_line}\n推荐定数上限 {_f(pack.metrics.get('recommendation_ds_cap')):.2f}；路线收益为当前快照下的逐槽模拟值，不是涨分承诺。"
    lines = _wrap_lines(method, _font(SIYUAN, 20), CONTENT_W - 56, max_lines=3)
    _draw_lines(draw, lines, CONTENT_X + 28, y + 60, _font(SIYUAN, 20), MUTED, 30)
    return y + height


def _draw_footer(draw: ImageDraw.ImageDraw, y: int) -> int:
    draw.line((CONTENT_X, y, CONTENT_R, y), fill=LINE, width=2)
    left = footer_generated(maiconfig.botName)
    right = "数据由成绩快照计算 · 同段仅使用脱敏聚合 · ROAST V2"
    left_font = _font(SIYUAN, 19)
    right_font = _font(SIYUAN, 18)
    draw.text((CONTENT_X, y + 24), _fit_line(left, left_font, 820), font=left_font, fill=MUTED)
    draw.text((CONTENT_R, y + 25), _fit_line(right, right_font, 540), font=right_font, fill=MUTED, anchor="ra")
    return y + 92


def render_report(pack: EvidencePack, report: RoastReport) -> io.BytesIO:
    layout = _measure_layout(pack, report)
    height = layout["height"]
    im = _build_background(height)
    _panel(im, (PAGE_X, 42, W - PAGE_X, height - 42), radius=30, fill=SURFACE, outline=(209, 222, 237, 255), width=3)
    draw = ImageDraw.Draw(im)
    y = 58
    y = _draw_header(im, draw, pack, y)
    y += 22
    y = _draw_summary(im, draw, pack, report, layout, y)
    y += 24
    # The long-form review is the primary reading experience. Put it before
    # the supporting evidence tables so users see the useful commentary first.
    y = _draw_analysis(im, draw, report, layout, y)
    y += 24
    y = _draw_diagnostics(im, draw, pack, y)
    y += 24
    y = _draw_ds_table(im, draw, pack, layout, y)
    y += 24
    y = _draw_evidence(im, draw, layout, y)
    y += 24
    y = _draw_routes(im, draw, layout, y)
    y += 24
    y = _draw_method(im, draw, pack, y, layout["method_h"])
    y += 20
    _draw_footer(draw, y)
    out = io.BytesIO()
    im.convert("RGB").save(out, format="PNG", compress_level=6)
    out.seek(0)
    return out
