"""个人猜歌数据图：多模式趋势 + 扇形占比 + 记录明细。"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Sequence, Tuple

from PIL import Image, ImageDraw

from ..config import SIYUAN, TBFONT
from .image import DrawText, image_to_base64
from .maimaidx_guess_score import GuessScoreManager

_BG = (28, 34, 48, 255)
_CARD = (40, 48, 66, 255)
_PANEL = (48, 58, 78, 255)
_TITLE = (255, 232, 200, 255)
_TEXT = (236, 240, 248, 255)
_MUTED = (150, 162, 184, 255)
_LINE = (70, 82, 108, 255)
_GRID = (58, 68, 92, 255)

_MODE_COLORS = {
    'song': (74, 144, 217, 255),
    'pic': (230, 140, 70, 255),
    'audio': (72, 180, 120, 255),
    'chart': (60, 180, 170, 255),
    'letter': (200, 120, 200, 255),
    'rating': (240, 190, 80, 255),
    'impostor': (235, 88, 112, 255),
    'duel': (255, 168, 92, 255),
    '20q': (132, 112, 244, 255),
}


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


def _rounded(dr: ImageDraw.ImageDraw, box: Tuple[int, int, int, int], radius: int, fill) -> None:
    dr.rounded_rectangle(box, radius=radius, fill=fill)


def _draw_multi_line_chart(
    dr: ImageDraw.ImageDraw,
    dt: DrawText,
    *,
    labels: Sequence[str],
    series: Dict[str, List[int]],
    x: int,
    y: int,
    w: int,
    h: int,
) -> None:
    # 标题与图例分行，避免标题和玩法色点重叠。
    dt.draw(
        x + 20, y + 14, 24,
        f'近 30 日积分趋势（{len(GuessScoreManager.GUESS_MODES)} 种玩法）',
        _TITLE, 'lt', 1, (0, 0, 0, 100),
    )
    lx = x + 20
    ly = y + 48
    for mode in GuessScoreManager.GUESS_MODES:
        color = _MODE_COLORS[mode]
        label = GuessScoreManager.MODE_LABELS[mode]
        dr.ellipse((lx, ly - 5, lx + 10, ly + 5), fill=color)
        dt.draw(lx + 16, ly, 14, label, color, 'lm')
        lx += 96

    left, top, right, bottom = x + 52, y + 76, x + w - 24, y + h - 40
    dr.line((left, top, left, bottom), fill=_LINE, width=2)
    dr.line((left, bottom, right, bottom), fill=_LINE, width=2)

    all_vals = [v for mode in GuessScoreManager.GUESS_MODES for v in series.get(mode, [])]
    if not all_vals or max(all_vals) <= 0:
        dt.draw(
            (left + right) // 2, (top + bottom) // 2, 20,
            '暂无明细（猜对 / 开字母结算后会记入趋势）', _MUTED, 'mm',
        )
        return

    max_v = max(all_vals)
    min_v = 0
    if max_v == min_v:
        max_v = min_v + 1

    for gy in range(5):
        yy = int(top + (bottom - top) * gy / 4)
        dr.line((left, yy, right, yy), fill=_GRID, width=1)
        val = int(max_v - (max_v - min_v) * gy / 4)
        dt.draw(left - 8, yy, 13, str(val), _MUTED, 'rm')

    n = max(len(labels), 1)
    for mode in GuessScoreManager.GUESS_MODES:
        pts = series.get(mode) or [0] * n
        if len(pts) < n:
            pts = list(pts) + [0] * (n - len(pts))
        color = _MODE_COLORS[mode]
        coords: List[Tuple[int, int]] = []
        for i, v in enumerate(pts[:n]):
            px = left if n == 1 else int(left + (right - left) * i / (n - 1))
            ratio = (v - min_v) / (max_v - min_v)
            py = int(bottom - ratio * (bottom - top))
            coords.append((px, py))
        if len(coords) >= 2:
            dr.line(coords, fill=color, width=3)
        for i, (px, py) in enumerate(coords):
            if pts[i] > 0:
                dr.ellipse((px - 3, py - 3, px + 3, py + 3), fill=color)

    if labels:
        tick_idx = sorted({0, (n - 1) // 2, n - 1})
        for i in tick_idx:
            px = left if n == 1 else int(left + (right - left) * i / (n - 1))
            dt.draw(px, bottom + 8, 13, labels[i], _MUTED, 'mt')


def _draw_donut(
    dr: ImageDraw.ImageDraw,
    dt: DrawText,
    *,
    cx: int,
    cy: int,
    radius: int,
    labels: Sequence[str],
    values: Sequence[int],
    modes: Sequence[str],
    title: str,
    unit: str,
    legend_x: int,
    hole_ratio: float = 0.58,
) -> None:
    """环形扇形图：表达各玩法在合计中的占比，图例在环右侧。"""
    # 小标题在环上方，与环/卡片标题留足空隙
    dt.draw(cx, cy - radius - 36, 17, title, _TITLE, 'mm', 1, (0, 0, 0, 80))

    n = max(len(labels), len(values), len(modes))
    raw = [max(0, int(values[i]) if i < len(values) else 0) for i in range(n)]
    total = sum(raw)
    bbox = (cx - radius, cy - radius, cx + radius, cy + radius)
    hole_r = int(radius * hole_ratio)

    if total <= 0:
        dr.ellipse(bbox, outline=_GRID, width=10)
        dr.ellipse(
            (cx - hole_r, cy - hole_r, cx + hole_r, cy + hole_r),
            fill=_CARD,
        )
        dt.draw(cx, cy - 8, 15, '暂无数据', _MUTED, 'mm')
        dt.draw(cx, cy + 14, 12, f'0{unit}', _MUTED, 'mm')
    else:
        # 从正上方起顺时针；零值跳过，避免 0° 空扇
        start = -90.0
        for i, v in enumerate(raw):
            if v <= 0:
                continue
            extent = 360.0 * v / total
            mode = modes[i] if i < len(modes) else ''
            color = _MODE_COLORS.get(mode, _LINE)
            end = start + extent
            # 整圆时用 ellipse，避免 pieslice 缝隙
            if abs(extent - 360.0) < 1e-6:
                dr.ellipse(bbox, fill=color)
            else:
                dr.pieslice(bbox, start, end, fill=color)
            start = end
        dr.ellipse(
            (cx - hole_r, cy - hole_r, cx + hole_r, cy + hole_r),
            fill=_CARD,
        )
        dt.draw(cx, cy - 10, 16, f'{total}{unit}', _TEXT, 'mm')
        dt.draw(cx, cy + 14, 12, '合计', _MUTED, 'mm')

    # 图例：色点 + 模式名 + 数值 + 占比
    ly = cy - radius + 4
    for i in range(n):
        mode = modes[i] if i < len(modes) else ''
        color = _MODE_COLORS.get(mode, _MUTED)
        label = labels[i] if i < len(labels) else mode
        v = raw[i]
        pct = (100.0 * v / total) if total > 0 else 0.0
        dr.ellipse((legend_x, ly - 5, legend_x + 10, ly + 5), fill=color)
        dt.draw(legend_x + 16, ly, 14, label, color, 'lm')
        dt.draw(legend_x + 16, ly + 18, 12, f'{v}{unit}  {pct:.0f}%', _MUTED, 'lm')
        ly += 42


def draw_personal_guess_stats(stats: dict) -> Image.Image:
    """根据 GuessScoreManager.build_user_guess_stats 结果出图。"""
    width = 1080
    n_modes = len(GuessScoreManager.GUESS_MODES)
    mode_cols = min(4, max(1, n_modes))
    mode_rows = (n_modes + mode_cols - 1) // mode_cols
    header_h = 150
    chart_h = 340
    mode_h = 82 + mode_rows * 118 + max(0, mode_rows - 1) * 12 + 20
    pie_h = max(420, 150 + n_modes * 42)
    recent_rows = max(1, len(stats.get('recent') or []))
    recent_h = 56 + recent_rows * 36 + 24
    footer_h = 56
    margin = 28
    gap = 18
    height = header_h + chart_h + mode_h + pie_h + recent_h + footer_h + margin * 2 + gap * 4

    im = Image.new('RGBA', (width, height), _BG)
    dr = ImageDraw.Draw(im)
    dt = DrawText(dr, _font())

    y = margin
    _rounded(dr, (margin, y, width - margin, y + header_h), 18, _CARD)
    name = stats.get('name') or stats.get('uid') or '玩家'
    dt.draw(margin + 28, y + 28, 36, f'{name} 的猜歌数据', _TITLE, 'lt', 2, (0, 0, 0, 120))
    total = int(stats.get('total_score') or 0)
    rank = int(stats.get('total_rank') or 0)
    dt.draw(
        margin + 28, y + 78, 20,
        f'本群总分 {total}  ·  总榜第 {rank} 名',
        _TEXT, 'lt', 1, (0, 0, 0, 80),
    )
    period = stats.get('period_snapshot') or {}
    period_bits: List[str] = []
    for key, label in (
        ('daily', '今日'),
        ('weekly', '本周'),
        ('monthly', '本月'),
        ('season', '赛季'),
    ):
        score, prank = period.get(key, (0, 0))
        period_bits.append(f'{label} {score}（#{prank}）')
    dt.draw(margin + 28, y + 112, 16, '  ·  '.join(period_bits), _MUTED, 'lt')

    y += header_h + gap
    _rounded(dr, (margin, y, width - margin, y + chart_h), 18, _CARD)
    daily = stats.get('daily_series') or {}
    _draw_multi_line_chart(
        dr, dt,
        labels=daily.get('labels') or [],
        series={m: daily.get(m) or [] for m in GuessScoreManager.GUESS_MODES},
        x=margin, y=y, w=width - margin * 2, h=chart_h,
    )

    y += chart_h + gap
    _rounded(dr, (margin, y, width - margin, y + mode_h), 18, _CARD)
    dt.draw(
        margin + 28, y + 20, 26,
        f'{n_modes} 种玩法记录', _TITLE, 'lt', 1, (0, 0, 0, 100),
    )
    modes = stats.get('modes') or {}
    card_gap = 12
    card_w = (width - margin * 2 - 40 - card_gap * (mode_cols - 1)) // mode_cols
    card_x0 = margin + 20
    card_y = y + 58
    for i, mode in enumerate(GuessScoreManager.GUESS_MODES):
        row, col = divmod(i, mode_cols)
        cx = card_x0 + col * (card_w + card_gap)
        cy = card_y + row * (118 + card_gap)
        color = _MODE_COLORS[mode]
        _rounded(dr, (cx, cy, cx + card_w, cy + 118), 14, _PANEL)
        info = modes.get(mode) or {}
        dt.draw(cx + 12, cy + 16, 18, GuessScoreManager.MODE_LABELS[mode], color, 'lt')
        dt.draw(cx + 12, cy + 48, 16, f'次数 {int(info.get("count") or 0)}', _TEXT, 'lt')
        dt.draw(cx + 12, cy + 74, 16, f'积分 {int(info.get("points") or 0)}', _TEXT, 'lt')
        last_at = info.get('last_at') or '—'
        dt.draw(cx + 12, cy + 98, 13, f'最近 {last_at}', _MUTED, 'lt')

    y += mode_h + gap
    _rounded(dr, (margin, y, width - margin, y + pie_h), 18, _CARD)
    dt.draw(margin + 28, y + 16, 26, '玩法占比', _TITLE, 'lt', 1, (0, 0, 0, 100))
    radar = stats.get('radar') or {}
    labels = radar.get('labels') or [GuessScoreManager.MODE_LABELS[m] for m in GuessScoreManager.GUESS_MODES]
    mode_keys = radar.get('modes') or list(GuessScoreManager.GUESS_MODES)
    points = radar.get('points') or [0] * len(GuessScoreManager.GUESS_MODES)
    counts = radar.get('counts') or [0] * len(GuessScoreManager.GUESS_MODES)
    mid = width // 2
    # 环心下移，给卡片标题与子标题留白；图例在环右侧，避免压住标题/明细
    pie_cy = y + 230
    pie_r = 100
    has_data = any(int(v) > 0 for v in points) or any(int(v) > 0 for v in counts)
    if not has_data:
        dt.draw(mid, pie_cy, 20, '暂无占比数据（结算后自动生成）', _MUTED, 'mm')
    else:
        _draw_donut(
            dr, dt,
            cx=margin + 160, cy=pie_cy, radius=pie_r,
            labels=labels, values=points, modes=mode_keys,
            title='积分占比', unit='分',
            legend_x=margin + 160 + pie_r + 28,
        )
        _draw_donut(
            dr, dt,
            cx=mid + 160, cy=pie_cy, radius=pie_r,
            labels=labels, values=counts, modes=mode_keys,
            title='次数占比', unit='次',
            legend_x=mid + 160 + pie_r + 28,
        )
        dt.draw(
            mid, y + pie_h - 22, 13,
            '扇形为各模式占合计的比例；环心为合计积分 / 次数',
            _MUTED, 'mm',
        )

    y += pie_h + gap
    _rounded(dr, (margin, y, width - margin, y + recent_h), 18, _CARD)
    dt.draw(margin + 28, y + 20, 26, '近期明细', _TITLE, 'lt', 1, (0, 0, 0, 100))
    recent = stats.get('recent') or []
    ry = y + 58
    if not recent:
        dt.draw(margin + 28, ry, 18, '暂无明细记录', _MUTED, 'lt')
    else:
        for row in recent:
            mode = row.get('mode') or 'song'
            label = GuessScoreManager.MODE_LABELS.get(mode, mode)
            color = _MODE_COLORS.get(mode, _TEXT)
            at = row.get('at') or ''
            pts = int(row.get('points') or 0)
            dt.draw(margin + 28, ry, 17, at, _MUTED, 'lt')
            dt.draw(margin + 200, ry, 17, label, color, 'lt')
            dt.draw(margin + 320, ry, 17, f'+{pts} 分', _TEXT, 'lt')
            ry += 36

    note = stats.get('note') or ''
    dt.draw(margin + 28, height - footer_h + 8, 14, note, _MUTED, 'lt')
    dt.draw(width - margin - 28, height - footer_h + 8, 14, '猜歌数据 · 本群个人', _MUTED, 'rt')
    return im


def personal_guess_stats_image_b64(stats: dict) -> str:
    return image_to_base64(draw_personal_guess_stats(stats))
