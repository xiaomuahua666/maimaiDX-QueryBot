from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

from ...config import SIYUAN, TBFONT, footer_generated, maiconfig, maimaidir
from ..maimaidx_game_assets import draw_rating_badge
from ..maimaidx_theme import Theme, pic, resolve_theme_path
from .domain import EvidencePack, RoastReport

W, H = 1600, 4400
BG = (243, 247, 252)
INK = (35, 43, 54)
MUTED = (99, 112, 128)
BLUE = (55, 112, 214)
GREEN = (47, 161, 107)
ORANGE = (230, 139, 45)
RED = (207, 76, 79)
LINE = (218, 227, 237)


def _font(path: Path, size: int):
    try:
        return ImageFont.truetype(str(path), size)
    except OSError:
        return ImageFont.load_default()


def _text_width(font: Any, text: str) -> float:
    try:
        return float(font.getlength(text))
    except AttributeError:
        box = font.getbbox(text)
        return float(box[2] - box[0])


def _wrap_lines(text: str, font: Any, max_width: int, max_lines: int | None = None) -> list[str]:
    lines: list[str] = []
    for paragraph in str(text or "").splitlines() or [""]:
        current = ""
        for char in paragraph:
            candidate = current + char
            if current and _text_width(font, candidate) > max_width:
                lines.append(current)
                current = char
            else:
                current = candidate
        lines.append(current or "")
    if max_lines is not None and len(lines) > max_lines:
        lines = lines[:max_lines]
        tail = lines[-1]
        while tail and _text_width(font, tail + "…") > max_width:
            tail = tail[:-1]
        lines[-1] = tail + "…"
    return lines


def _load_background() -> Image.Image:
    try:
        path = resolve_theme_path(maimaidir, Theme.get_default().value, "b50_bg.png")
        if path.exists():
            image = Image.open(path).convert("RGBA")
            image = ImageOps.fit(image, (W, H), method=Image.Resampling.LANCZOS)
            image = image.filter(ImageFilter.GaussianBlur(1.2))
            return ImageEnhance.Brightness(image).enhance(0.78)
    except Exception:
        pass
    return Image.new("RGBA", (W, H), BG + (255,))


def _load_cover(path: str, size: tuple[int, int]) -> Image.Image | None:
    try:
        candidate = Path(path)
        if not candidate.exists():
            return None
        image = Image.open(candidate).convert("RGBA")
        image = ImageOps.fit(image, size, method=Image.Resampling.LANCZOS)
        mask = Image.new("L", size, 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=16, fill=255)
        image.putalpha(mask)
        return image
    except Exception:
        return None


def _load_score_texture(level_index: int, size: tuple[int, int]) -> Image.Image | None:
    names = ("basic", "advanced", "expert", "master", "remaster")
    try:
        name = names[max(0, min(len(names) - 1, int(level_index)))]
        path = pic(f"b50_score_{name}.png")
        if not path.exists():
            return None
        texture = Image.open(path).convert("RGBA")
        texture = ImageOps.fit(texture, size, method=Image.Resampling.LANCZOS)
        alpha = texture.getchannel("A").point(lambda value: int(value * 0.26))
        texture.putalpha(alpha)
        return texture
    except Exception:
        return None


def _paste_game_card_texture(im: Image.Image, box: tuple[int, int, int, int], level_index: int) -> None:
    x1, y1, x2, y2 = box
    texture = _load_score_texture(level_index, (x2 - x1, y2 - y1))
    if texture is None:
        return
    mask = Image.new("L", texture.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, texture.width - 1, texture.height - 1), radius=18, fill=255)
    texture.putalpha(ImageChops.multiply(texture.getchannel("A"), mask))
    im.alpha_composite(texture, (x1, y1))


def _section(draw: ImageDraw.ImageDraw, y: int, title: str) -> int:
    draw.text((92, y), title, font=_font(SIYUAN, 40), fill=INK)
    draw.line((92, y + 61, W - 92, y + 61), fill=LINE, width=2)
    return y + 88


def _bar(draw: ImageDraw.ImageDraw, x: int, y: int, label: str, value: float | None, color: tuple[int, int, int]) -> None:
    label_font = _font(SIYUAN, 27)
    number_font = _font(TBFONT, 27)
    draw.text((x, y), label, font=label_font, fill=INK)
    left, right = x + 250, x + 770
    draw.rounded_rectangle((left, y + 5, right, y + 34), radius=15, fill=(225, 232, 241))
    if value is None:
        draw.text((right + 28, y - 2), "暂无数据", font=label_font, fill=MUTED)
        return
    ratio = max(0.0, min(1.0, float(value) / 100.5))
    if ratio > 0:
        draw.rounded_rectangle((left, y + 5, left + int((right - left) * ratio), y + 34), radius=15, fill=color)
    draw.text((right + 28, y - 2), f"{value:.2f}%", font=number_font, fill=INK)


def _draw_chart(draw: ImageDraw.ImageDraw, pack: EvidencePack, y: int) -> int:
    y = _section(draw, y, "数据画像")
    metrics = pack.metrics
    _bar(draw, 102, y, "B35 平均", metrics.get("b35_avg"), BLUE)
    _bar(draw, 102, y + 61, "B15 平均", metrics.get("b15_avg"), ORANGE)
    _bar(draw, 102, y + 122, "14+ 平均", metrics.get("high_avg"), GREEN)
    y += 208
    draw.text((102, y), "Rating 构成", font=_font(SIYUAN, 27), fill=INK)
    b35_ra = sum(int(x.get("ra", 0) or 0) for x in pack.b35)
    b15_ra = sum(int(x.get("ra", 0) or 0) for x in pack.b15)
    total = max(1, b35_ra + b15_ra)
    left, width = 345, 930
    for label, value, color in (("B35", b35_ra, BLUE), ("B15", b15_ra, ORANGE)):
        bar_width = int(width * value / total)
        draw.rounded_rectangle((left, y + 2, left + bar_width, y + 37), radius=10, fill=color)
        draw.text((left + bar_width + 18, y - 3), f"{label} {value}", font=_font(TBFONT, 25), fill=INK)
        y += 58
    chips = [
        ("B50 曲目", str(metrics.get("chart_count", 0)), BLUE),
        ("14+ 样本", str(metrics.get("high_count", 0)), GREEN if metrics.get("high_count") else MUTED),
        ("最低 RA", str(metrics.get("b35_floor", 0)), ORANGE),
        ("最高 RA", str(metrics.get("ceiling", 0)), RED),
    ]
    chip_x = 102
    for label, value, color in chips:
        draw.rounded_rectangle((chip_x, y + 10, chip_x + 275, y + 75), radius=15, fill=(248, 250, 253), outline=LINE, width=2)
        draw.text((chip_x + 18, y + 20), label, font=_font(SIYUAN, 20), fill=MUTED)
        draw.text((chip_x + 18, y + 43), value, font=_font(TBFONT, 25), fill=color)
        chip_x += 296
    diagnostic_chips = [
        ("B35 / B15 差", f"{float(metrics.get('b35_b15_gap', 0)):+.2f}%", BLUE),
        ("达成率波动", f"σ {float(metrics.get('achievement_stddev', 0)):.2f}%", ORANGE),
        ("SSS / SSS+", f"{metrics.get('sss_count', 0)} / {metrics.get('sssp_count', 0)}", GREEN),
        ("Top3 可兑现", f"+{metrics.get('top3_estimated_gain', 0)}", RED),
    ]
    chip_x = 102
    for label, value, color in diagnostic_chips:
        draw.rounded_rectangle((chip_x, y + 92, chip_x + 275, y + 157), radius=15, fill=(248, 250, 253), outline=LINE, width=2)
        draw.text((chip_x + 18, y + 102), label, font=_font(SIYUAN, 20), fill=MUTED)
        draw.text((chip_x + 18, y + 125), value, font=_font(TBFONT, 25), fill=color)
        chip_x += 296
    return y + 187


def _draw_featured(draw: ImageDraw.ImageDraw, im: Image.Image, pack: EvidencePack, y: int) -> int:
    y = _section(draw, y, "代表曲目")
    rows = sorted(pack.b35 + pack.b15, key=lambda item: int(item.get("ra", 0) or 0), reverse=True)[:3]
    if not rows:
        draw.text((104, y), "暂无可展示的曲目数据", font=_font(SIYUAN, 26), fill=MUTED)
        return y + 55
    card_w, card_h, gap = 440, 150, 24
    for index, item in enumerate(rows):
        x = 92 + index * (card_w + gap)
        card_box = (x, y, x + card_w, y + card_h)
        draw.rounded_rectangle(card_box, radius=18, fill=(250, 251, 253), outline=LINE, width=2)
        _paste_game_card_texture(im, card_box, int(item.get("level_index", 0) or 0))
        cover = _load_cover(str(item.get("cover_path") or ""), (110, 110))
        if cover:
            im.alpha_composite(cover, (x + 18, y + 20))
        else:
            draw.rounded_rectangle((x + 18, y + 20, x + 128, y + 130), radius=16, fill=(226, 233, 242))
            draw.text((x + 50, y + 60), "♪", font=_font(TBFONT, 32), fill=MUTED)
        title = str(item.get("title") or "未知曲目")
        title_lines = _wrap_lines(title, _font(SIYUAN, 24), 270, max_lines=2)
        for line_index, line in enumerate(title_lines):
            draw.text((x + 148, y + 24 + line_index * 31), line, font=_font(SIYUAN, 24), fill=INK)
        draw.text((x + 148, y + 91), f"{item.get('level', '')} · RA {item.get('ra', 0)}", font=_font(TBFONT, 21), fill=BLUE)
        draw.text((x + 148, y + 116), f"{float(item.get('achievement', 0) or 0):.4f}%", font=_font(TBFONT, 20), fill=MUTED)
    return y + card_h + 35


def _draw_bullets(draw: ImageDraw.ImageDraw, y: int, items: list[str], color: tuple[int, int, int]) -> int:
    font = _font(SIYUAN, 28)
    for item in items[:4]:
        lines = _wrap_lines(str(item), font, W - 255, max_lines=3)
        draw.ellipse((100, y + 9, 124, y + 33), fill=color)
        for index, line in enumerate(lines):
            draw.text((148, y + index * 39), line, font=font, fill=INK)
        y += max(54, len(lines) * 39 + 12)
    return y + 12


def _draw_routes(draw: ImageDraw.ImageDraw, im: Image.Image, report: RoastReport, y: int) -> int:
    y = _section(draw, y, "推荐路线")
    if not report.recommendations:
        draw.text((104, y), "当前没有足够证据生成可靠的推分路线。", font=_font(SIYUAN, 26), fill=MUTED)
        return y + 55
    for item in report.recommendations[:3]:
        row_h = 158
        card_box = (92, y, W - 92, y + row_h)
        draw.rounded_rectangle(card_box, radius=18, fill=(250, 251, 253), outline=LINE, width=2)
        _paste_game_card_texture(im, card_box, int(item.get("level_index", 0) or 0))
        cover = _load_cover(str(item.get("cover_path") or ""), (96, 96))
        if cover:
            im.alpha_composite(cover, (112, y + 21))
        else:
            draw.rounded_rectangle((112, y + 21, 208, y + 117), radius=14, fill=(226, 233, 242))
            draw.text((142, y + 50), "♪", font=_font(TBFONT, 28), fill=MUTED)
        title = str(item.get("title") or "未知曲目")
        title_lines = _wrap_lines(title, _font(SIYUAN, 28), 780, max_lines=1)
        draw.text((235, y + 18), title_lines[0], font=_font(SIYUAN, 28), fill=INK)
        meta = f"{item.get('chart_type', '')} · {item.get('level', '')} · 目标 {item.get('target', '')}"
        draw.text((235, y + 57), meta, font=_font(SIYUAN, 23), fill=BLUE)
        gain = max(0, int(item.get("estimated_gain", 0) or 0))
        gain_box = (W - 420, y + 18, W - 120, y + 68)
        draw.rounded_rectangle(gain_box, radius=16, fill=(229, 244, 236), outline=(187, 224, 202), width=2)
        draw.text((gain_box[0] + 150, y + 43), f"预计 +{gain} Rating", font=_font(SIYUAN, 23), fill=GREEN, anchor="mm")
        reason_lines = _wrap_lines(str(item.get("reason") or ""), _font(SIYUAN, 21), 1120, max_lines=2)
        for index, line in enumerate(reason_lines):
            draw.text((235, y + 94 + index * 26), line, font=_font(SIYUAN, 21), fill=MUTED)
        y += row_h + 16
    return y + 12


def render_report(pack: EvidencePack, report: RoastReport) -> io.BytesIO:
    im = _load_background()
    overlay = Image.new("RGBA", (W, H), (255, 255, 255, 208))
    im.alpha_composite(overlay)
    draw = ImageDraw.Draw(im)
    draw.rounded_rectangle((48, 42, W - 48, H - 42), radius=30, fill=(255, 255, 255, 238), outline=(220, 229, 239), width=3)
    draw.text((88, 82), f"{pack.nickname} · B50 锐评", font=_font(SIYUAN, 48), fill=INK)
    badge_w, _ = draw_rating_badge(im, 88, 143, pack.rating, height=46)
    if badge_w == 0:
        draw.text((90, 150), f"Rating {pack.rating}", font=_font(TBFONT, 38), fill=BLUE)
    draw.text((W - 520, 154), "ROAST V2 · DATA CARD", font=_font(TBFONT, 28), fill=MUTED)

    summary_font = _font(SIYUAN, 38)
    summary_lines = _wrap_lines(report.summary, summary_font, W - 260, max_lines=4)
    summary_y, summary_h = 245, 86 + len(summary_lines) * 58
    draw.rounded_rectangle((80, summary_y, W - 80, summary_y + summary_h), radius=22, fill=(235, 244, 255), outline=(210, 227, 250), width=2)
    draw.text((112, summary_y + 25), "一句话总结", font=_font(SIYUAN, 29), fill=BLUE)
    for index, line in enumerate(summary_lines):
        draw.text((112, summary_y + 70 + index * 56), line, font=summary_font, fill=INK)
    y = summary_y + summary_h + 36
    y = _draw_chart(draw, pack, y)
    y = _draw_featured(draw, im, pack, y)
    for title, items, color in (("亮点", report.strengths, GREEN), ("短板", report.weaknesses, RED), ("行动建议", report.actions, BLUE)):
        y = _section(draw, y, title)
        y = _draw_bullets(draw, y, items, color)
    y = _draw_routes(draw, im, report, y)

    footer_y = min(H - 105, max(1500, y + 34))
    footer = footer_generated(maiconfig.botName)
    draw.line((92, footer_y, W - 92, footer_y), fill=LINE, width=2)
    draw.text((92, footer_y + 20), footer, font=_font(SIYUAN, 21), fill=MUTED)
    draw.text((W - 540, footer_y + 20), "数据由成绩快照计算 · V2", font=_font(SIYUAN, 21), fill=MUTED)
    im = im.crop((0, 0, W, min(H, footer_y + 72)))
    out = io.BytesIO()
    im.convert("RGB").save(out, format="PNG", optimize=True)
    out.seek(0)
    return out
