from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple

from nonebot.adapters.onebot.v11 import MessageSegment
from PIL import Image, ImageDraw

from ..config import SIYUAN, achievementList, footer_generated, log, maiconfig, static
from .image import DrawText, draw_centered_design_footer, generate_frosted_card, image_to_base64, music_picture, rounded_corners
from .maimaidx_best_50 import _is_latest_version
from .maimaidx_data_storage import DailySnapshot, ScoreRecord, data_storage

ACCENT = (124, 129, 255, 255)
TEXT = (45, 50, 95, 255)
SUBTEXT = (90, 95, 140, 255)
MUTED = (120, 126, 145, 255)
POSITIVE = (72, 168, 108, 255)
NEGATIVE = (220, 100, 110, 255)

_MARGIN = 44
_PANEL_ALPHA = 0.52
_CARD_W = 300
_CARD_H = 88
_CARD_H_TALL = 102  # 含 ra 增量 + 达成换行


@dataclass
class _DiffEntry:
    title: str
    level: str
    level_index: int
    ds: float
    ra_delta: int
    achv_delta: float
    achv_now: float


def _song_key(r: ScoreRecord) -> Tuple[int, int]:
    return int(r.song_id), int(r.level_index)


def _build_b50(records: List[ScoreRecord]) -> Tuple[List[ScoreRecord], List[ScoreRecord], List[ScoreRecord]]:
    sorted_records = sorted(records, key=lambda x: int(x.ra), reverse=True)
    b15 = sorted([r for r in sorted_records if _is_latest_version(r)], key=lambda x: int(x.ra), reverse=True)[:15]
    b35 = sorted([r for r in sorted_records if not _is_latest_version(r)], key=lambda x: int(x.ra), reverse=True)[:35]
    b50 = b35 + b15
    return b35, b15, b50


def _is_sun(a: float) -> bool:
    for x in achievementList:
        if (x - 0.1) < a <= x:
            return True
    return False


def _is_lock(a: float) -> bool:
    for x in achievementList:
        step = 0.01 if x != int(x) else 0.1
        if x <= a < (x + step):
            return True
    return False


def _date_str(offset_days: int = 0) -> str:
    return (datetime.now() - timedelta(days=offset_days)).strftime('%Y-%m-%d')


def _snapshot_by_date(qqid: int, date: str) -> DailySnapshot | None:
    return data_storage.load_daily_snapshot(qqid, date)


def _collect_snapshots(qqid: int, days: int) -> List[DailySnapshot]:
    metas = data_storage.list_snapshots(qqid, limit=240)
    if not metas:
        return []
    now = datetime.now()
    cutoff = now - timedelta(days=days)
    selected: List[DailySnapshot] = []
    for m in metas:
        stored_at = m.get('stored_at') or ''
        if not stored_at:
            continue
        try:
            dt = datetime.fromisoformat(stored_at)
        except Exception:
            continue
        if dt >= cutoff:
            snap = data_storage.load_snapshot_by_id(qqid, m.get('snapshot_id', ''))
            if snap:
                selected.append(snap)
    return selected


def _analyze(old: DailySnapshot, new: DailySnapshot) -> Dict:
    old_b35, old_b15, old_b50 = _build_b50(old.records)
    new_b35, new_b15, new_b50 = _build_b50(new.records)

    old_map = {_song_key(r): r for r in old_b50}
    new_map = {_song_key(r): r for r in new_b50}

    new_entries: List[ScoreRecord] = [r for k, r in new_map.items() if k not in old_map]
    improved: List[_DiffEntry] = []
    for k, nr in new_map.items():
        orr = old_map.get(k)
        if not orr:
            continue
        ra_delta = int(nr.ra) - int(orr.ra)
        achv_delta = float(nr.achievements) - float(orr.achievements)
        if ra_delta > 0 or achv_delta > 1e-6:
            improved.append(
                _DiffEntry(
                    title=nr.title,
                    level=nr.level,
                    level_index=int(nr.level_index),
                    ds=float(nr.ds or 0),
                    ra_delta=ra_delta,
                    achv_delta=achv_delta,
                    achv_now=float(nr.achievements),
                )
            )
    improved.sort(key=lambda x: (x.ra_delta, x.achv_delta), reverse=True)

    # 提升曲目按难度分布
    diff_dist = [0, 0, 0, 0, 0]
    for e in improved:
        if 0 <= e.level_index < 5:
            diff_dist[e.level_index] += 1

    return {
        'rating_delta': int(new.rating) - int(old.rating),
        'old_rating': int(old.rating),
        'new_rating': int(new.rating),
        'b35_delta': sum(int(r.ra) for r in new_b35) - sum(int(r.ra) for r in old_b35),
        'b15_delta': sum(int(r.ra) for r in new_b15) - sum(int(r.ra) for r in old_b15),
        'b35_new_sum': sum(int(r.ra) for r in new_b35),
        'b15_new_sum': sum(int(r.ra) for r in new_b15),
        'b35_tail_delta': (int(new_b35[-1].ra) - int(old_b35[-1].ra)) if (old_b35 and new_b35) else 0,
        'b15_tail_delta': (int(new_b15[-1].ra) - int(old_b15[-1].ra)) if (old_b15 and new_b15) else 0,
        'new_entries': new_entries,
        'improved': improved,
        'improved_count': len(improved),
        'new_count': len(new_entries),
        'total_improved_ra': sum(e.ra_delta for e in improved),
        'best_entry': improved[0] if improved else None,
        'diff_dist': diff_dist,
        'sun_list': [x for x in improved if _is_sun(x.achv_now)],
        'lock_list': [x for x in improved if _is_lock(x.achv_now)],
    }


def _resolve_report_bg_path() -> Path | None:
    custom = maiconfig.maimaidx_tag_analysis_bg
    if custom:
        p = Path(custom)
        if not p.is_absolute():
            p = static / custom
        if p.is_file():
            return p
    try:
        from .maimaidx_theme import Theme, resolve_theme_path
        from ..config import maimaidir

        p = resolve_theme_path(maimaidir, Theme.get_default().value, 'b50_bg.png')
        return p if p.exists() else None
    except Exception:
        return None


def _get_report_bg(width: int, height: int) -> Image.Image:
    im = Image.new('RGBA', (width, height), (245, 247, 255, 255))
    bg_path = _resolve_report_bg_path()
    if not bg_path:
        return im
    bg = Image.open(bg_path).convert('RGBA')
    bg_w, bg_h = bg.size
    for j in range(height // bg_h + 1):
        im.alpha_composite(bg, (0, j * bg_h))
    if width > bg_w:
        strip = bg.crop((bg_w - 1, 0, bg_w, bg_h))
        for i in range(width - bg_w):
            for j in range(height // bg_h + 1):
                im.alpha_composite(strip, (bg_w + i, j * bg_h))
    return im


def _frosted_panel(im: Image.Image, x: int, y: int, w: int, h: int) -> Image.Image:
    return generate_frosted_card(im, (x, y, x + w, y + h), alpha=_PANEL_ALPHA)


def _delta_color(v: int | float) -> Tuple[int, int, int, int]:
    if v > 0:
        return POSITIVE
    if v < 0:
        return NEGATIVE
    return MUTED


def _paste_cover_card(
    im: Image.Image,
    dt: DrawText,
    x: int,
    y: int,
    r: ScoreRecord,
    *,
    ra_delta: int | None = None,
    achv_delta: float | None = None,
) -> int:
    """绘制曲目卡片，返回实际卡片高度。"""
    tall = ra_delta is not None
    card_h = _CARD_H_TALL if tall else _CARD_H
    dr = ImageDraw.Draw(im)
    dr.rounded_rectangle(
        (x, y, x + _CARD_W, y + card_h),
        radius=16,
        fill=(255, 255, 255, 200),
        outline=(*ACCENT[:3], 180),
        width=2,
    )
    try:
        cover = Image.open(music_picture(r.song_id)).convert('RGBA').resize((74, 74))
        cover = rounded_corners(cover, 12, (True, True, True, True))
        im.alpha_composite(cover, (x + 8, y + 7))
    except Exception:
        pass
    name = r.title if len(r.title) <= 15 else (r.title[:14] + '…')
    dt.draw(x + 90, y + 12, 19, name, TEXT, 'lt', 1, (255, 255, 255, 220))
    dt.draw(x + 90, y + 38, 17, f'[{r.level}]  {r.achievements:.4f}%', SUBTEXT, 'lt', 1, (255, 255, 255, 200))
    if tall:
        dt.draw(x + 90, y + 58, 15, f'ra {int(r.ra)}  ·  ra {ra_delta:+d}', SUBTEXT, 'lt', 1, (255, 255, 255, 200))
        if achv_delta is not None:
            dt.draw(x + 90, y + 76, 15, f'达成 {achv_delta:+.4f}%', SUBTEXT, 'lt', 1, (255, 255, 255, 200))
    else:
        dt.draw(x + 90, y + 60, 16, f'ra {int(r.ra)}', SUBTEXT, 'lt', 1, (255, 255, 255, 200))
    return card_h


def _draw_line_chart(
    dr: ImageDraw.ImageDraw,
    dt: DrawText,
    points: List[int],
    labels: List[str],
    x: int,
    y: int,
    w: int,
    h: int,
) -> None:
    dt.draw(x + 24, y + 18, 28, 'Rating 曲线', ACCENT, 'lt', 2, (255, 255, 255, 240))
    if not points:
        dt.draw(x + 24, y + 70, 20, '暂无数据', MUTED, 'lt')
        return

    left, top, right, bottom = x + 56, y + 72, x + w - 28, y + h - 36
    min_v, max_v = min(points), max(points)
    if min_v == max_v:
        min_v -= 10
        max_v += 10

    dr.line((left, top, left, bottom), fill=(*ACCENT[:3], 120), width=2)
    dr.line((left, bottom, right, bottom), fill=(*ACCENT[:3], 120), width=2)

    coords: List[Tuple[int, int]] = []
    n = len(points)
    for i, v in enumerate(points):
        px = left if n == 1 else int(left + (right - left) * i / (n - 1))
        ratio = (v - min_v) / (max_v - min_v)
        py = int(bottom - ratio * (bottom - top))
        coords.append((px, py))

    for gy in range(5):
        yy = int(top + (bottom - top) * gy / 4)
        dr.line((left, yy, right, yy), fill=(255, 255, 255, 80), width=1)

    if len(coords) >= 2:
        dr.line(coords, fill=ACCENT, width=4)
    for i, (px, py) in enumerate(coords):
        dr.ellipse((px - 6, py - 6, px + 6, py + 6), fill=ACCENT, outline=(255, 255, 255, 255), width=2)
        if i in (0, len(coords) - 1):
            dt.draw(px, py - 14, 17, str(points[i]), TEXT, 'mb', 1, (255, 255, 255, 230))
    if labels:
        dt.draw(left, bottom + 8, 15, labels[0], MUTED, 'lt', 1, (255, 255, 255, 200))
        dt.draw(right, bottom + 8, 15, labels[-1], MUTED, 'rt', 1, (255, 255, 255, 200))


def _short_song(r: ScoreRecord) -> str:
    t = r.title.strip()
    return t if len(t) <= 22 else t[:21] + '…'


def _find_record_for_entry(new_b50: List[ScoreRecord], e: _DiffEntry) -> ScoreRecord | None:
    for rr in new_b50:
        if rr.title == e.title and rr.level == e.level:
            return rr
    return None


def _draw_report(
    title: str,
    nickname: str,
    points: List[int],
    labels: List[str],
    data: Dict,
    old_dt: str,
    new_dt: str,
) -> Image.Image:
    width = 1600
    added = data['new_entries'][:6]
    inc = data['improved'][:10]
    lock_list = data['lock_list'][:5]
    sun_list = data['sun_list'][:5]
    new_b50: List[ScoreRecord] = data.get('new_b50') or []

    new_cards_h = (_CARD_H + 16) if added[:2] else 0
    new_list_count = max(0, len(added) - 2)
    new_panel_h = 56 + new_cards_h + (28 if new_list_count else 0) + new_list_count * 26 + 24

    top_improved = inc[:2]
    imp_cards_h = (_CARD_H_TALL + 16) if top_improved else 0
    imp_list = inc[2:] if len(inc) > 2 else (inc if not top_improved else [])
    imp_panel_h = 56 + imp_cards_h + (28 if imp_list else 0) + max(1, len(imp_list)) * 32 + 24

    lock_panel_h = 56 + 40 + max(1, len(lock_list)) * 28 + 40 + max(1, len(sun_list)) * 28 + 24

    y_chart = 108
    chart_h = 300
    y_row2 = y_chart + chart_h + 20
    row2_h = max(248, new_panel_h)
    y_row3 = y_row2 + row2_h + 20
    row3_h = max(imp_panel_h, lock_panel_h)
    footer_h = 52
    height = y_row3 + row3_h + footer_h

    im = _get_report_bg(width, height)
    chart_x, chart_w = _MARGIN, width - _MARGIN * 2
    core_w = 500
    new_x = _MARGIN + core_w + 20
    new_w = width - _MARGIN - new_x
    imp_w = width - _MARGIN * 2 - 520
    lock_x = _MARGIN + imp_w + 20
    lock_w = 500

    im = _frosted_panel(im, chart_x, y_chart, chart_w, chart_h)
    im = _frosted_panel(im, _MARGIN, y_row2, core_w, row2_h)
    im = _frosted_panel(im, new_x, y_row2, new_w, row2_h)
    im = _frosted_panel(im, _MARGIN, y_row3, imp_w, row3_h)
    im = _frosted_panel(im, lock_x, y_row3, lock_w, row3_h)
    im = _frosted_panel(im, _MARGIN, height - footer_h, width - _MARGIN * 2, footer_h - 8)

    dr = ImageDraw.Draw(im)
    dt = DrawText(dr, SIYUAN)

    dt.draw(_MARGIN, 32, 44, title, ACCENT, 'lt', 2, (255, 255, 255, 240))
    dt.draw(_MARGIN, 78, 22, f'{nickname}  ·  {old_dt}  →  {new_dt}', SUBTEXT, 'lt', 1, (255, 255, 255, 220))

    _draw_line_chart(dr, dt, points, labels, chart_x, y_chart, chart_w, chart_h)

    dt.draw(_MARGIN + 24, y_row2 + 18, 28, '核心变化', ACCENT, 'lt', 2, (255, 255, 255, 240))
    for i, (label, val) in enumerate([
        ('Rating', data['rating_delta']),
        ('B35', data['b35_delta']),
        ('B15', data['b15_delta']),
        ('B35 末位', data['b35_tail_delta']),
        ('B15 末位', data['b15_tail_delta']),
    ]):
        sy = y_row2 + 68 + i * 34
        dt.draw(_MARGIN + 28, sy, 20, label, SUBTEXT, 'lt', 1, (255, 255, 255, 200))
        dt.draw(_MARGIN + core_w - 32, sy, 22, f'{val:+d}', _delta_color(val), 'rt', 1, (255, 255, 255, 220))

    dt.draw(new_x + 24, y_row2 + 18, 28, 'B50 新增曲目', ACCENT, 'lt', 2, (255, 255, 255, 240))
    if not added:
        dt.draw(new_x + 28, y_row2 + 72, 20, '无新增', MUTED, 'lt')
    else:
        cy = y_row2 + 58
        for i, r in enumerate(added[:2]):
            _paste_cover_card(im, dt, new_x + 20 + i * (_CARD_W + 16), cy, r)
        if new_list_count:
            dt.draw(new_x + 28, cy + _CARD_H + 12, 18, '其余新增', SUBTEXT, 'lt', 1, (255, 255, 255, 200))
            for i, r in enumerate(added[2:6]):
                dt.draw(
                    new_x + 32, cy + _CARD_H + 36 + i * 26, 17,
                    f'{i + 3}. {_short_song(r)} [{r.level}]  ra {int(r.ra)}',
                    TEXT, 'lt', 1, (255, 255, 255, 220),
                )

    dt.draw(_MARGIN + 24, y_row3 + 18, 28, 'B50 提升曲目', ACCENT, 'lt', 2, (255, 255, 255, 240))
    if not inc:
        dt.draw(_MARGIN + 28, y_row3 + 72, 20, '无提升', MUTED, 'lt')
    else:
        cy = y_row3 + 58
        for i, e in enumerate(top_improved):
            rec = _find_record_for_entry(new_b50, e)
            if rec:
                _paste_cover_card(
                    im, dt, _MARGIN + 20 + i * (_CARD_W + 16), cy, rec,
                    ra_delta=e.ra_delta,
                    achv_delta=e.achv_delta,
                )
        list_y = cy + (imp_cards_h if top_improved else 0) + 8
        show_list = imp_list if top_improved else inc
        if top_improved and imp_list:
            dt.draw(_MARGIN + 28, list_y, 18, '更多提升', SUBTEXT, 'lt', 1, (255, 255, 255, 200))
            list_y += 26
        for i, e in enumerate(show_list):
            idx = i + 3 if top_improved else i + 1
            dt.draw(
                _MARGIN + 32, list_y + i * 32, 18,
                f'{idx}. {e.title[:20]} [{e.level}]  ra {e.ra_delta:+d}  达成 {e.achv_delta:+.4f}%',
                TEXT, 'lt', 1, (255, 255, 255, 220),
            )

    dt.draw(lock_x + 24, y_row3 + 18, 28, '锁血 / 寸止', ACCENT, 'lt', 2, (255, 255, 255, 240))
    dt.draw(lock_x + 28, y_row3 + 64, 22, '锁血命中', TEXT, 'lt', 1, (255, 255, 255, 230))
    if not lock_list:
        dt.draw(lock_x + 28, y_row3 + 96, 18, '无', MUTED, 'lt')
    for i, e in enumerate(lock_list):
        dt.draw(
            lock_x + 32, y_row3 + 96 + i * 28, 17,
            f'{i + 1}. {e.title[:16]}  {e.achv_now:.4f}%  ra {e.ra_delta:+d}',
            SUBTEXT, 'lt', 1, (255, 255, 255, 210),
        )
    sun_y = y_row3 + 64 + 40 + max(1, len(lock_list)) * 28 + 16
    dt.draw(lock_x + 28, sun_y, 22, '寸止命中', TEXT, 'lt', 1, (255, 255, 255, 230))
    if not sun_list:
        dt.draw(lock_x + 28, sun_y + 32, 18, '无', MUTED, 'lt')
    for i, e in enumerate(sun_list):
        dt.draw(
            lock_x + 32, sun_y + 32 + i * 28, 17,
            f'{i + 1}. {e.title[:16]}  {e.achv_now:.4f}%  ra {e.ra_delta:+d}',
            SUBTEXT, 'lt', 1, (255, 255, 255, 210),
        )

    draw_centered_design_footer(
        im, dt, footer_generated(),
        color=SUBTEXT,
        margin_x=_MARGIN,
        start_font_size=16,
        min_font_size=10,
        bottom_gap=max(12, footer_h // 2),
    )
    return im


def _fmt_date(s: DailySnapshot) -> str:
    return s.stored_at.replace('T', ' ') if s.stored_at else s.date


def _report_title(period_days: int) -> str:
    if period_days <= 7:
        return 'MAIMAI 周报'
    if period_days <= 31:
        return 'MAIMAI 月报'
    return 'MAIMAI 年报'


async def generate_daily_report(qqid: int) -> MessageSegment | str:
    """对比今日与昨日存档（按日历日，非 24 小时滚动窗口）。"""
    today = _snapshot_by_date(qqid, _date_str(0))
    yesterday = _snapshot_by_date(qqid, _date_str(1))
    if not today:
        return '今日尚无存档，请先使用「立即存储数据」。'
    if not yesterday:
        return '昨日无存档，无法生成日报。请确保已开启数据存储并至少积累一天历史（每日凌晨自动存档或手动存档）。'

    points = [int(yesterday.rating), int(today.rating)]
    labels = [yesterday.date, today.date]
    data = _analyze(yesterday, today)
    _, _, data['new_b50'] = _build_b50(today.records)
    nickname = today.nickname or str(qqid)
    from .maimaidx_report_image import render_report
    im = Image.open(render_report(
        'MAIMAI 日报', nickname,
        f'{_fmt_date(yesterday)}  →  {_fmt_date(today)}',
        points, labels, data, period_tag='日报',
    ))
    log.debug(f'[progress_report] qq={qqid} daily delta={data["rating_delta"]}')
    return MessageSegment.image(image_to_base64(im))


async def generate_progress_report(qqid: int, period_days: int) -> MessageSegment | str:
    snaps = _collect_snapshots(qqid, period_days)
    if len(snaps) < 2:
        return f'近{period_days}天可用快照不足（至少需要2次存档）。请先使用「立即存储数据」并等待后续自动存档。'

    latest = snaps[0]
    oldest = snaps[-1]
    curve = list(reversed(snaps))
    points = [int(s.rating) for s in curve]
    labels = [s.date for s in curve]

    data = _analyze(oldest, latest)
    _, _, data['new_b50'] = _build_b50(latest.records)
    title = _report_title(period_days)
    nickname = latest.nickname or str(qqid)
    tag = '周报' if period_days <= 7 else ('月报' if period_days <= 31 else '年报')
    from .maimaidx_report_image import render_report
    im = Image.open(render_report(
        title, nickname,
        f'{_fmt_date(oldest)}  →  {_fmt_date(latest)}',
        points, labels, data, period_tag=tag,
    ))
    log.debug(f'[progress_report] qq={qqid} period={period_days} snapshots={len(snaps)} delta={data["rating_delta"]}')
    return MessageSegment.image(image_to_base64(im))


async def generate_progress_report_between(qqid: int, old_snapshot_id: str, new_snapshot_id: str) -> MessageSegment | str:
    old = data_storage.load_snapshot_by_id(qqid, old_snapshot_id.strip())
    new = data_storage.load_snapshot_by_id(qqid, new_snapshot_id.strip())
    if not old:
        return f'未找到起始存档：{old_snapshot_id}'
    if not new:
        return f'未找到结束存档：{new_snapshot_id}'
    if old.stored_at and new.stored_at and old.stored_at > new.stored_at:
        old, new = new, old

    points = [int(old.rating), int(new.rating)]
    labels = [old.date, new.date]
    data = _analyze(old, new)
    _, _, data['new_b50'] = _build_b50(new.records)
    nickname = new.nickname or str(qqid)
    from .maimaidx_report_image import render_report
    im = Image.open(render_report(
        'MAIMAI 存档对比', nickname,
        f'{_fmt_date(old)}  →  {_fmt_date(new)}',
        points, labels, data, period_tag='对比',
    ))
    return MessageSegment.image(image_to_base64(im))
