from __future__ import annotations

import io
import math
import textwrap
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from ...config import SIYUAN, TBFONT, footer_generated, maiconfig
from .domain import EvidencePack, RoastReport

W, H = 1600, 2500
BG = (246, 249, 252)
INK = (34, 42, 52)
MUTED = (100, 112, 126)
BLUE = (49, 112, 213)
GREEN = (51, 158, 103)
ORANGE = (228, 139, 48)
RED = (205, 76, 77)


def _font(path, size):
    try:
        return ImageFont.truetype(str(path), size)
    except OSError:
        # Local CI fixtures may omit the production font pack; keep rendering
        # testable and let production use the bundled CJK fonts when present.
        return ImageFont.load_default()


def _wrap(draw, text: str, font, width: int) -> list[str]:
    result = []
    for paragraph in str(text or "").splitlines() or [""]:
        font_size = int(getattr(font, "size", 16) or 16)
        result.extend(textwrap.wrap(paragraph, width=max(8, width // max(1, int(font_size * 0.95))) or 8) or [""])
    return result


def _section(draw, y: int, title: str) -> int:
    draw.text((80, y), title, font=_font(SIYUAN, 42), fill=INK)
    draw.line((80, y + 62, W - 80, y + 62), fill=(220, 227, 235), width=2)
    return y + 90


def _bar(draw, x: int, y: int, label: str, value: float, color: tuple[int, int, int], suffix: str = "%") -> None:
    draw.text((x, y), label, font=_font(SIYUAN, 28), fill=INK)
    draw.rounded_rectangle((x + 250, y + 4, x + 760, y + 34), radius=15, fill=(226, 232, 240))
    draw.rounded_rectangle((x + 250, y + 4, x + 250 + int(510 * max(0.0, min(1.0, value / 100))), y + 34), radius=15, fill=color)
    draw.text((x + 790, y - 3), f"{value:.2f}{suffix}", font=_font(TBFONT, 28), fill=INK)


def _draw_chart(draw, pack: EvidencePack, y: int) -> int:
    y = _section(draw, y, "数据画像")
    m = pack.metrics
    _bar(draw, 90, y, "B35 平均", float(m.get("b35_avg", 0)), BLUE)
    _bar(draw, 90, y + 62, "B15 平均", float(m.get("b15_avg", 0)), ORANGE)
    _bar(draw, 90, y + 124, "14+ 平均", float(m.get("high_avg", 0)), GREEN)
    y += 215
    # Compact B35/B15 composition chart.
    draw.text((90, y), "Rating 结构", font=_font(SIYUAN, 28), fill=INK)
    total = max(1, int(m.get("ceiling", 0)))
    b35_ra = sum(int(x.get("ra", 0) or 0) for x in pack.b35)
    b15_ra = sum(int(x.get("ra", 0) or 0) for x in pack.b15)
    x, bar_w = 330, 950
    for label, value, color in (("B35", b35_ra, BLUE), ("B15", b15_ra, ORANGE)):
        width = int(bar_w * value / max(1, b35_ra + b15_ra))
        draw.rectangle((x, y + 2, x + width, y + 38), fill=color)
        draw.text((x + width + 18, y - 3), f"{label} {value}", font=_font(TBFONT, 26), fill=INK)
        y += 58
    return y + 28


def render_report(pack: EvidencePack, report: RoastReport) -> io.BytesIO:
    im = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(im)
    draw.rounded_rectangle((48, 42, W - 48, H - 42), radius=28, fill=(255, 255, 255), outline=(223, 230, 238), width=3)
    draw.text((86, 84), f"{pack.nickname} · B50 锐评", font=_font(SIYUAN, 48), fill=INK)
    draw.text((88, 150), f"Rating {pack.rating}", font=_font(TBFONT, 38), fill=BLUE)
    draw.text((W - 520, 154), "ROAST V2", font=_font(TBFONT, 30), fill=MUTED)
    y = 245
    draw.rounded_rectangle((80, y, W - 80, y + 210), radius=20, fill=(237, 245, 255))
    draw.text((112, y + 28), "一句话总结", font=_font(SIYUAN, 30), fill=BLUE)
    summary_font = _font(SIYUAN, 42)
    sy = y + 78
    for line in _wrap(draw, report.summary, summary_font, W - 260)[:3]:
        draw.text((112, sy), line, font=summary_font, fill=INK)
        sy += 60
    y += 260
    y = _draw_chart(draw, pack, y)
    for title, items, color in (("亮点", report.strengths, GREEN), ("短板", report.weaknesses, RED), ("行动建议", report.actions, BLUE)):
        y = _section(draw, y, title)
        for item in items[:4]:
            draw.ellipse((100, y + 8, 122, y + 30), fill=color)
            draw.text((145, y), str(item), font=_font(SIYUAN, 30), fill=INK)
            y += 52
        y += 16
    y = _section(draw, y, "推荐路线")
    for item in report.recommendations[:3]:
        draw.rounded_rectangle((90, y, W - 90, y + 92), radius=14, fill=(247, 249, 252))
        draw.text((120, y + 15), str(item.get("title") or "未知曲目"), font=_font(SIYUAN, 30), fill=INK)
        draw.text((760, y + 19), f"{item.get('level', '')} · {item.get('target', '')}", font=_font(TBFONT, 27), fill=BLUE)
        draw.text((120, y + 55), str(item.get("reason") or ""), font=_font(SIYUAN, 23), fill=MUTED)
        y += 112
    footer_y = min(H - 105, max(1120, y + 34))
    footer = footer_generated(maiconfig.botName)
    draw.line((90, footer_y, W - 90, footer_y), fill=(226, 232, 240), width=2)
    draw.text((90, footer_y + 20), footer, font=_font(SIYUAN, 22), fill=MUTED)
    draw.text((W - 540, footer_y + 20), "数据由成绩快照计算 · V2", font=_font(SIYUAN, 22), fill=MUTED)
    im = im.crop((0, 0, W, min(H, footer_y + 72)))
    out = io.BytesIO()
    im.save(out, format="PNG", optimize=True)
    out.seek(0)
    return out
