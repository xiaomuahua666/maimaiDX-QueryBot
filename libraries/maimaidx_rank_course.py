"""舞萌 DX 段位认定课题、个人成绩与匿名样本统计。"""

from __future__ import annotations

import json
import math
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from threading import RLock
from typing import Any, Iterable, Optional

from PIL import Image, ImageDraw, ImageFont, ImageOps


ROOT = Path(__file__).resolve().parent.parent
COURSE_FILE = ROOT / "libraries" / "assets" / "rank_courses.json"
STATS_FILE = ROOT / "libraries" / "assets" / "rank_course_chart_stats.json"
DIFFICULTY_NAMES = ("BASIC", "ADVANCED", "EXPERT", "MASTER", "Re:MASTER")
DIFFICULTY_COLORS = (
    (91, 190, 100),
    (239, 190, 45),
    (237, 81, 75),
    (164, 82, 210),
    (217, 101, 180),
)
_stats_lock = RLock()
_live_stats: Optional[dict[str, Any]] = None
_live_stats_loaded_at = 0.0


@dataclass(frozen=True)
class LifeRule:
    initial: int
    great: int
    good: int
    miss: int
    heal: int


@dataclass(frozen=True)
class ThemeColors:
    body_bg: tuple[int, int, int, int]
    card_bg: tuple[int, int, int, int]
    header_bg: tuple[int, int, int, int]
    header_deep: tuple[int, int, int, int]
    primary: tuple[int, int, int]
    accent: tuple[int, int, int]
    soft: tuple[int, int, int, int]
    warm: tuple[int, int, int]
    text_main: tuple[int, int, int]
    text_sub: tuple[int, int, int]
    line_color: tuple[int, int, int, int]
    family_label: str


THEME_NORMAL = ThemeColors(
    body_bg=(237, 246, 247, 255),
    card_bg=(255, 255, 255, 255),
    header_bg=(20, 104, 116, 255),
    header_deep=(14, 61, 76, 255),
    primary=(18, 126, 132),
    accent=(36, 178, 169),
    soft=(220, 243, 241, 255),
    warm=(235, 185, 72),
    text_main=(22, 43, 54),
    text_sub=(86, 105, 114),
    line_color=(203, 222, 224, 255),
    family_label="STANDARD COURSE",
)

THEME_SHIN = ThemeColors(
    body_bg=(247, 242, 247, 255),
    card_bg=(255, 255, 255, 255),
    header_bg=(112, 57, 104, 255),
    header_deep=(57, 34, 67, 255),
    primary=(139, 66, 125),
    accent=(196, 82, 128),
    soft=(242, 226, 239, 255),
    warm=(220, 176, 77),
    text_main=(48, 34, 52),
    text_sub=(108, 91, 110),
    line_color=(226, 210, 224, 255),
    family_label="SHIN COURSE",
)


def _get_theme(course_name: str) -> ThemeColors:
    if "真" in course_name or "里" in course_name:
        return THEME_SHIN
    return THEME_NORMAL


@dataclass(frozen=True)
class RankCourse:
    name: str
    song_ids: tuple[int, ...]
    level_indexes: tuple[int, ...]
    life: LifeRule


@dataclass
class CourseTrack:
    song_id: int
    level_index: int
    title: str
    level: str
    ds: Optional[float]
    notes: tuple[int, ...]
    achievement: Optional[float] = None
    sample: Optional[dict[str, Any]] = None


def _normalize_rank_name(value: str) -> str:
    return (
        value.strip()
        .replace("裏", "里")
        .replace("傳", "传")
        .replace("伝", "传")
        .replace("皆傳", "皆传")
        .replace("段位表", "")
        .replace("段位", "")
        .strip()
    )


@lru_cache(maxsize=1)
def load_rank_courses() -> tuple[dict[str, Any], dict[str, RankCourse]]:
    raw = json.loads(COURSE_FILE.read_text(encoding="utf-8"))
    courses: dict[str, RankCourse] = {}
    for item in raw["courses"]:
        life = LifeRule(**item["life"])
        course = RankCourse(
            name=item["name"],
            song_ids=tuple(int(x) for x in item["song_ids"]),
            level_indexes=tuple(int(x) for x in item["level_indexes"]),
            life=life,
        )
        courses[course.name] = course
    return raw, courses


def get_rank_course(name: str) -> Optional[RankCourse]:
    normalized = _normalize_rank_name(name)
    return load_rank_courses()[1].get(normalized)


def rank_course_help() -> str:
    meta, courses = load_rank_courses()
    names = list(courses)
    return (
        f"舞萌DX 段位表（{meta['region']} / 默认 {meta['version']}）\n"
        f"普通：{' · '.join(names[:10])}\n"
        f"真段：{' · '.join(names[10:20])}\n"
        f"皆传：{' · '.join(names[20:])}\n"
        "查询：段位表 真二段（可在消息中 @ 玩家）"
    )


@lru_cache(maxsize=1)
def _load_bundled_course_stats() -> dict[str, Any]:
    if not STATS_FILE.exists():
        return {"player_count": 0, "charts": {}}
    return json.loads(STATS_FILE.read_text(encoding="utf-8"))


def _percentile(values: list[float], fraction: float) -> float:
    pos = (len(values) - 1) * fraction
    lower = int(pos)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (pos - lower)


def build_course_stats_from_players(
    players: Iterable[dict[str, Any]],
    *,
    min_samples: int = 3,
    fallback: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """将已同意共享的全量成绩聚合为不可反查个人的谱面统计。"""
    wanted = {
        f"{song_id}:{level_index}"
        for course in load_rank_courses()[1].values()
        for song_id, level_index in zip(course.song_ids, course.level_indexes)
    }
    values: dict[str, list[float]] = defaultdict(list)
    player_count = 0
    freshest_record_at = 0.0
    for player in players:
        records = player.get("records") or []
        if not records:
            continue
        player_count += 1
        freshest_record_at = max(
            freshest_record_at, float(player.get("fetched_at") or 0)
        )
        seen: set[str] = set()
        for record in records:
            song_id = int(getattr(record, "song_id", 0) or 0)
            level_index = int(getattr(record, "level_index", 0) or 0)
            key = f"{song_id}:{level_index}"
            if key not in wanted or key in seen:
                continue
            seen.add(key)
            values[key].append(float(getattr(record, "achievements", 0.0) or 0.0))

    bundled_charts = (fallback or {}).get("charts") or {}
    charts: dict[str, dict[str, Any]] = {}
    live_chart_count = 0
    for key in sorted(wanted, key=lambda item: tuple(map(int, item.split(":")))):
        samples = sorted(values.get(key, []))
        if len(samples) < max(1, min_samples):
            if key in bundled_charts:
                charts[key] = dict(bundled_charts[key], sample_source="bundled")
            continue
        n = len(samples)
        live_chart_count += 1
        charts[key] = {
            "sample_count": n,
            "avg_achievement": round(statistics.fmean(samples), 4),
            "p25": round(_percentile(samples, 0.25), 4),
            "median": round(_percentile(samples, 0.5), 4),
            "p75": round(_percentile(samples, 0.75), 4),
            "clear_rate": round(sum(value >= 97.0 for value in samples) / n, 4),
            "sss_rate": round(sum(value >= 100.0 for value in samples) / n, 4),
            "sample_source": "live",
        }
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "server player cache (opt-out excluded)",
        "player_count": player_count,
        "freshest_record_at": freshest_record_at or None,
        "live_chart_count": live_chart_count,
        "charts": charts,
    }


def invalidate_course_stats() -> None:
    global _live_stats, _live_stats_loaded_at
    with _stats_lock:
        _live_stats = None
        _live_stats_loaded_at = 0.0


def load_course_stats() -> dict[str, Any]:
    """读取服务器近期全量成绩并聚合；冷启动或低样本谱面使用随包统计。"""
    global _live_stats, _live_stats_loaded_at
    try:
        from ..config import log, maiconfig
        from .maimaidx_data_share import data_share
        from .maimaidx_player_cache import player_cache_db
    except (ImportError, ValueError):
        return _load_bundled_course_stats()

    ttl = max(0, int(maiconfig.maimaidx_rank_course_stats_cache_seconds))
    now = time.time()
    with _stats_lock:
        if _live_stats is not None and now - _live_stats_loaded_at < ttl:
            return _live_stats
        max_age_days = max(1, int(maiconfig.maimaidx_rank_course_sample_max_age_days))
        max_players = max(1, int(maiconfig.maimaidx_rank_course_sample_max_players))
        try:
            players = player_cache_db.list_recent_full_records(
                since_ts=now - max_age_days * 86400,
                min_records=30,
                limit=max_players,
            )
            opted_out = set(data_share.list_opted_out())
            shared = [
                player for player in players if str(player["qqid"]) not in opted_out
            ]
        except Exception as exc:
            log.warning(f"[RankCourse] 实时样本聚合失败，使用内置样本: {exc}")
            return _load_bundled_course_stats()
        if not shared:
            return _load_bundled_course_stats()
        _live_stats = build_course_stats_from_players(
            shared,
            min_samples=max(1, int(maiconfig.maimaidx_rank_course_min_samples)),
            fallback=_load_bundled_course_stats(),
        )
        _live_stats_loaded_at = now
        return _live_stats


def _record_map(records: Iterable[Any]) -> dict[tuple[int, int], Any]:
    result = {}
    for record in records:
        song_id = int(getattr(record, "song_id", 0) or 0)
        level_index = int(getattr(record, "level_index", 0) or 0)
        result[(song_id, level_index)] = record
    return result


def build_course_tracks(
    course: RankCourse,
    music_list: Iterable[Any],
    records: Iterable[Any] = (),
    course_stats: Optional[dict[str, Any]] = None,
) -> list[CourseTrack]:
    music_by_id = {int(music.id): music for music in music_list}
    scores = _record_map(records)
    stats = (course_stats or load_course_stats()).get("charts") or {}
    tracks: list[CourseTrack] = []
    for song_id, level_index in zip(course.song_ids, course.level_indexes):
        music = music_by_id.get(song_id)
        if music is None:
            raise LookupError(f"段位课题曲 ID {song_id} 不在当前曲库中")
        level = music.level[level_index] if level_index < len(music.level) else "?"
        ds = float(music.ds[level_index]) if level_index < len(music.ds) else None
        chart = music.charts[level_index] if level_index < len(music.charts) else None
        notes = tuple(chart.notes) if chart is not None and chart.notes else ()
        record = scores.get((song_id, level_index))
        achievement = (
            float(getattr(record, "achievements", 0.0)) if record is not None else None
        )
        tracks.append(
            CourseTrack(
                song_id=song_id,
                level_index=level_index,
                title=music.title,
                level=level,
                ds=ds,
                notes=notes,
                achievement=achievement,
                sample=stats.get(f"{song_id}:{level_index}"),
            )
        )
    return tracks


def _weighted_note_count(notes: tuple[int, ...]) -> int:
    if len(notes) >= 5:
        tap, hold, slide, touch, brk = notes[:5]
    elif len(notes) >= 4:
        tap, hold, slide, brk = notes[:4]
        touch = 0
    else:
        return 0
    return tap + touch + 2 * hold + 3 * slide + 5 * brk


def optimistic_damage(track: CourseTrack, rule: LifeRule) -> int:
    """仅由达成率反推最有利判定组合，不能替代机台判定明细。"""
    if track.achievement is None:
        return 0
    weighted = _weighted_note_count(track.notes)
    if weighted <= 0:
        return 0
    loss_units = max(0.0, (101.0 - track.achievement) * weighted / 10.0)
    candidates = (
        math.floor(loss_units / 2) * rule.great,
        math.floor(loss_units / 5) * rule.good,
        math.floor(loss_units / 10) * rule.miss,
    )
    return min(candidates)


def estimate_life(course: RankCourse, tracks: list[CourseTrack]) -> Optional[int]:
    if len(tracks) != 4 or any(track.achievement is None for track in tracks):
        return None
    life = course.life.initial
    for index, track in enumerate(tracks):
        life = max(0, life - optimistic_damage(track, course.life))
        if life == 0:
            return 0
        if index < 3:
            life = min(course.life.initial, life + course.life.heal)
    return life


def _font_path() -> Path:
    configured: list[Path] = []
    try:
        from ..config import SHANGGUMONO, SIYUAN

        configured.extend((Path(SIYUAN), Path(SHANGGUMONO)))
    except (ImportError, ValueError):
        pass
    candidates = (
        *configured,
        ROOT / "GenSenMaruGothicTW-Regular.ttf",
        ROOT / "static" / "font" / "ResourceHanRoundedCN-Bold.ttf",
        Path("/System/Library/Fonts/PingFang.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    )
    return next((path for path in candidates if path.exists()), candidates[0])


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(_font_path()), size)


def _fit_font(text: str, max_width: int, start: int, minimum: int = 18):
    for size in range(start, minimum - 1, -1):
        font = _font(size)
        if font.getlength(text) <= max_width:
            return font
    return _font(minimum)


def _rounded_cover(song_id: int, size: int = 168) -> Image.Image:
    from .image import music_picture

    path = music_picture(song_id)
    if path.exists():
        cover = Image.open(path).convert("RGB")
        cover = ImageOps.fit(cover, (size, size), Image.Resampling.LANCZOS)
    else:
        cover = Image.new("RGB", (size, size), (232, 237, 246))
        placeholder = ImageDraw.Draw(cover)
        placeholder.text(
            (size // 2, size // 2),
            "NO COVER",
            font=_font(18),
            fill=(120, 130, 150),
            anchor="mm",
        )
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, size - 1, size - 1), radius=8, fill=255
    )
    cover.putalpha(mask)
    return cover


def _sample_label(track: CourseTrack) -> str:
    sample = track.sample or {}
    n = int(sample.get("sample_count") or 0)
    if not n or sample.get("avg_achievement") is None:
        return "暂无公开样本"
    source = (
        "服务器实时匿名样本"
        if sample.get("sample_source") == "live"
        else "内置匿名样本"
    )
    return f"{source}  ·  n={n}"


def _sample_stat_line(track: CourseTrack) -> str:
    sample = track.sample or {}
    if not sample.get("sample_count") or sample.get("avg_achievement") is None:
        return "样本积累后将显示均值、中位数、四分位区间与 SSS 率"
    return (
        f"均值 {float(sample['avg_achievement']):.4f}%   "
        f"中位 {float(sample.get('median') or 0):.4f}%   "
        f"P25–P75 {float(sample.get('p25') or 0):.4f}%–"
        f"{float(sample.get('p75') or 0):.4f}%   "
        f"SSS率 {float(sample.get('sss_rate') or 0) * 100:.1f}%"
    )


def _note_count(track: CourseTrack) -> int:
    return sum(int(value or 0) for value in track.notes)


def _position_label(track: CourseTrack) -> str:
    if track.achievement is None:
        return "未游玩"
    sample = track.sample or {}
    if not sample.get("sample_count"):
        return "已有成绩"
    value = track.achievement
    if value >= float(sample.get("p75") or 999):
        return "样本前 25%"
    if value >= float(sample.get("median") or 999):
        return "高于样本中位"
    if value >= float(sample.get("p25") or 999):
        return "接近样本中位"
    return "样本后 25%"


def _player_visual_assets(
    plate_name: Optional[str],
    additional_rating: Optional[int],
    course_name: Optional[str] = None,
) -> tuple[
    Optional[Image.Image],
    Optional[Image.Image],
    Optional[Image.Image],
    Optional[Image.Image],
    Optional[Image.Image],
    Optional[Image.Image],
]:
    """加载姓名框、玩家段位贴图、课题段位贴图三项资源。"""
    try:
        from ..config import maimaidir
        from .maimaidx_theme import Theme, resolve_theme_path

        theme = Theme.get_default().value
        player_plate = None
        if plate_name:
            from .maimaidx_table_image import open_plate_image

            _plate = open_plate_image(
                plate_name, resolve_theme_path(maimaidir, theme, "UI_Plate_550101.png")
            )
            if _plate:
                player_plate = _plate.resize((800, 130)).convert("RGBA")
        if not player_plate:
            p = resolve_theme_path(maimaidir, theme, "UI_Plate_550101.png")
            if p.exists():
                player_plate = Image.open(p).convert("RGBA").resize((800, 130))

        dani_plate = None
        if additional_rating is not None:
            dani_path = resolve_theme_path(
                maimaidir, theme, _dani_plate_filename(additional_rating)
            )
            if dani_path.exists():
                dani_plate = Image.open(dani_path).convert("RGBA")

        course_dani_plate = None
        if course_name:
            fname = _course_dani_plate_filename(course_name)
            if fname:
                cdp_path = resolve_theme_path(maimaidir, theme, fname)
                if cdp_path.exists():
                    course_dani_plate = Image.open(cdp_path).convert("RGBA")

        logo_img = None
        p_logo = resolve_theme_path(maimaidir, theme, "logo.png")
        if p_logo.exists():
            logo_img = Image.open(p_logo).convert("RGBA").resize((249, 120))

        icon_img = None
        p_icon = resolve_theme_path(maimaidir, theme, "UI_Icon_509506.png")
        if p_icon.exists():
            icon_img = Image.open(p_icon).convert("RGBA").resize((120, 120))

        name_img = None
        p_name = resolve_theme_path(maimaidir, theme, "Name.png")
        if p_name.exists():
            name_img = Image.open(p_name).convert("RGBA")

        return player_plate, dani_plate, course_dani_plate, logo_img, icon_img, name_img
    except (ImportError, OSError, ValueError):
        return None, None, None, None, None, None


# Maps course name → UI_DNM_DaniPlate_XX index.
# 初段-十段 → 01-10; 真初段-真十段 → 12-21; 真皆传 → 22; 里皆传 → 23.
_COURSE_DANI_INDEX: dict[str, int] = {
    "初段": 1,
    "二段": 2,
    "三段": 3,
    "四段": 4,
    "五段": 5,
    "六段": 6,
    "七段": 7,
    "八段": 8,
    "九段": 9,
    "十段": 10,
    "真初段": 12,
    "真二段": 13,
    "真三段": 14,
    "真四段": 15,
    "真五段": 16,
    "真六段": 17,
    "真七段": 18,
    "真八段": 19,
    "真九段": 20,
    "真十段": 21,
    "真皆传": 22,
    "里皆传": 23,
}


def _dani_plate_filename(additional_rating: int) -> str:
    dani = max(0, int(additional_rating))
    index = dani if dani <= 10 else dani + 1
    return f"UI_DNM_DaniPlate_{index:02d}.png"


def _course_dani_plate_filename(course_name: str) -> Optional[str]:
    """根据段位课题名称返回对应的 UI_DNM_DaniPlate 文件名，找不到则返回 None。"""
    idx = _COURSE_DANI_INDEX.get(course_name)
    if idx is None:
        return None
    return f"UI_DNM_DaniPlate_{idx:02d}.png"


def _draw_sample_distribution(
    draw: ImageDraw.ImageDraw,
    track: CourseTrack,
    box: tuple[int, int, int, int],
    color: tuple[int, int, int],
    theme: ThemeColors,
) -> None:
    sample = track.sample or {}
    if not sample.get("sample_count"):
        return
    left, top, right, bottom = box
    scale_min, scale_max = 97.0, 101.0

    def x_for(value: float) -> int:
        ratio = (max(scale_min, min(scale_max, value)) - scale_min) / (
            scale_max - scale_min
        )
        return round(left + ratio * (right - left))

    center = (top + bottom) // 2
    draw.line((left, center, right, center), fill=theme.line_color, width=6)
    p25 = x_for(float(sample.get("p25") or scale_min))
    median = x_for(float(sample.get("median") or scale_min))
    p75 = x_for(float(sample.get("p75") or scale_max))
    draw.rounded_rectangle(
        (p25, top, max(p25 + 4, p75), bottom), radius=4, fill=(*color, 95)
    )
    draw.line((median, top - 2, median, bottom + 2), fill=(*color, 255), width=4)
    if track.achievement is not None:
        player_x = x_for(track.achievement)
        draw.ellipse(
            (player_x - 7, center - 7, player_x + 7, center + 7),
            fill=(29, 39, 57),
            outline=(255, 255, 255),
            width=2,
        )


def _draw_summary_icon(
    draw: ImageDraw.ImageDraw, center: tuple[int, int], kind: str
) -> None:
    cx, cy = center
    if kind == "life":
        # Compact heart silhouette; drawn as raster primitives to match the report style.
        draw.ellipse((cx - 11, cy - 9, cx + 1, cy + 3), fill=(255, 255, 255))
        draw.ellipse((cx - 1, cy - 9, cx + 11, cy + 3), fill=(255, 255, 255))
        draw.polygon(
            ((cx - 11, cy - 2), (cx + 11, cy - 2), (cx, cy + 13)),
            fill=(255, 255, 255),
        )
    elif kind == "warning":
        draw.ellipse(
            (cx - 12, cy - 12, cx + 12, cy + 12),
            outline=(255, 255, 255),
            width=3,
        )
        draw.line((cx, cy - 7, cx, cy + 3), fill=(255, 255, 255), width=3)
        draw.ellipse((cx - 2, cy + 7, cx + 2, cy + 11), fill=(255, 255, 255))
    else:
        for offset, bar_height in ((-10, 11), (-2, 19), (6, 26)):
            draw.rounded_rectangle(
                (cx + offset, cy + 13 - bar_height, cx + offset + 6, cy + 13),
                radius=2,
                fill=(255, 255, 255),
            )


def draw_rank_course(
    course: RankCourse,
    tracks: list[CourseTrack],
    *,
    player_name: Optional[str] = None,
    player_plate: Optional[str] = None,
    player_additional_rating: Optional[int] = None,
    score_note: Optional[str] = None,
) -> Image.Image:
    theme = _get_theme(course.name)
    width, height = 1080, 1620
    image = Image.new("RGBA", (width, height), theme.body_bg)
    draw = ImageDraw.Draw(image)

    _, _, course_dani, _, _, _ = _player_visual_assets(
        player_plate, player_additional_rating, course.name
    )

    # Header uses the course family color; chart difficulty colors stay local to cards.
    draw.rectangle((0, 0, width, 236), fill=theme.header_bg)
    draw.polygon(
        ((710, 0), (1080, 0), (1080, 236), (875, 236)),
        fill=theme.header_deep,
    )
    draw.polygon(
        ((0, 0), (390, 0), (315, 8), (0, 8)),
        fill=(*theme.warm, 255),
    )
    for offset in (0, 24, 48):
        draw.line(
            (760 + offset, 18, 965 + offset, 222),
            fill=(*theme.accent, 80),
            width=2,
        )

    draw.text(
        (42, 28),
        "舞萌2026  /  PRiSM PLUS",
        font=_font(18),
        fill=(225, 244, 244),
        anchor="la",
    )
    family_name = "真段位" if theme is THEME_SHIN else "普通段位"
    family_text = f"{family_name}  ·  {theme.family_label}"
    family_w = int(draw.textlength(family_text, font=_font(13))) + 28
    draw.rounded_rectangle(
        (42, 61, 42 + family_w, 91), radius=6, fill=(*theme.warm, 255)
    )
    draw.text(
        (56, 76), family_text, font=_font(13), fill=theme.header_deep[:3], anchor="lm"
    )
    draw.text(
        (42, 104),
        course.name,
        font=_font(58),
        fill=(255, 255, 255),
        anchor="la",
    )

    if course_dani is not None:
        orig_w, orig_h = course_dani.size
        dani_scale = min(230 / orig_w, 100 / orig_h)
        dani_w = int(orig_w * dani_scale)
        dani_h = int(orig_h * dani_scale)
        dani_resized = course_dani.resize((dani_w, dani_h), Image.Resampling.LANCZOS)
        dani_x = 1018 - dani_w
        dani_y = 55 + (104 - dani_h) // 2
        draw.rounded_rectangle(
            (dani_x - 18, 43, 1036, 171),
            radius=10,
            fill=(255, 255, 255, 28),
            outline=(255, 255, 255, 80),
            width=2,
        )
        image.alpha_composite(dani_resized, (dani_x, dani_y))

    life = estimate_life(course, tracks)
    if life is not None:
        summary = f"历史最佳成绩估算  ·  乐观 LIFE {life}"
        summary_kind = "life"
    elif score_note:
        summary = score_note
        summary_kind = "warning"
    else:
        summary = "课题配置与服务器匿名样本"
        summary_kind = "stats"
    draw.rounded_rectangle((42, 181, 660, 222), radius=7, fill=theme.header_deep)
    draw.rounded_rectangle((42, 181, 83, 222), radius=7, fill=(*theme.accent, 255))
    draw.rectangle((76, 181, 83, 222), fill=(*theme.accent, 255))
    _draw_summary_icon(draw, (62, 201), summary_kind)
    draw.text(
        (101, 202),
        summary,
        font=_fit_font(summary, 535, 18, 14),
        fill=(245, 251, 251),
        anchor="lm",
    )
    player_text = f"查询玩家  ·  {player_name}" if player_name else "公开课题参考"
    draw.text(
        (1036, 202),
        player_text,
        font=_fit_font(player_text, 330, 15, 12),
        fill=(224, 235, 238),
        anchor="rm",
    )

    rule = course.life
    rules = [("初始 LIFE", str(rule.initial), theme.primary)]
    if rule.great > 0:
        rules.append(("GREAT", f"-{rule.great}", (235, 115, 170)))
    if rule.good > 0:
        rules.append(("GOOD", f"-{rule.good}", (91, 190, 100)))
    if rule.miss > 0:
        rules.append(("MISS", f"-{rule.miss}", (150, 150, 150)))
    if rule.heal > 0:
        rules.append(("RECOVER", f"+{rule.heal}", (54, 139, 91)))

    draw.rounded_rectangle(
        (32, 250, 1048, 333),
        radius=10,
        fill=theme.card_bg,
        outline=theme.line_color,
        width=1,
    )
    num_rules = len(rules)
    rule_width = 1016 // num_rules
    for index, (label, value, color) in enumerate(rules):
        left = 32 + index * rule_width
        center = left + rule_width // 2
        if index:
            draw.line((left, 269, left, 314), fill=theme.line_color, width=1)
        draw.ellipse((center - 38, 268, center - 30, 276), fill=(*color, 255))
        draw.text(
            (center - 20, 272),
            label,
            font=_font(12),
            fill=theme.text_sub,
            anchor="lm",
        )
        draw.text(
            (center, 306),
            value,
            font=_font(24),
            fill=(*color, 255),
            anchor="mm",
        )

    TRACK_START_Y = 348
    TRACK_H = 282
    TRACK_GAP = 10

    y = TRACK_START_Y
    for index, track in enumerate(tracks, 1):
        difficulty_color = DIFFICULTY_COLORS[track.level_index]
        draw.rounded_rectangle(
            (32, y + 3, 1048, y + TRACK_H + 3),
            radius=10,
            fill=(*theme.header_deep[:3], 22),
        )
        draw.rounded_rectangle(
            (32, y, 1048, y + TRACK_H), radius=10, fill=theme.card_bg
        )
        draw.rounded_rectangle(
            (32, y, 40, y + TRACK_H), radius=7, fill=(*theme.primary, 255)
        )
        draw.rectangle((40, y, 1048, y + 5), fill=(*theme.accent, 255))

        draw.text(
            (57, y + 27),
            f"TRACK {index:02d}",
            font=_font(15),
            fill=theme.primary,
            anchor="lm",
        )
        difficulty_name = DIFFICULTY_NAMES[track.level_index]
        label_w = int(draw.textlength(difficulty_name, font=_font(12))) + 22
        draw.rounded_rectangle(
            (142, y + 15, 142 + label_w, y + 39),
            radius=5,
            fill=(*difficulty_color, 255),
        )
        draw.text(
            (153, y + 27),
            difficulty_name,
            font=_font(12),
            fill=(255, 255, 255),
            anchor="lm",
        )

        image.alpha_composite(_rounded_cover(track.song_id, 164), (56, y + 58))

        title_font = _fit_font(track.title, 570, 27, 18)
        draw.text(
            (240, y + 56),
            track.title,
            font=title_font,
            fill=theme.text_main,
            anchor="la",
        )
        ds_text = (
            f"LEVEL {track.level}    DS {track.ds:.1f}    NOTES {_note_count(track)}"
            if track.ds is not None
            else f"LEVEL {track.level}    NOTES {_note_count(track)}"
        )
        draw.text(
            (240, y + 96), ds_text, font=_font(17), fill=theme.primary, anchor="la"
        )

        score = (
            "未游玩"
            if track.achievement is None
            else f"个人最佳  {track.achievement:.4f}%"
        )
        score_color = (
            theme.text_main if track.achievement is not None else theme.text_sub
        )
        draw.text((240, y + 130), score, font=_font(22), fill=score_color, anchor="la")

        position = _position_label(track)
        draw.rounded_rectangle(
            (850, y + 49, 1031, y + 91),
            radius=7,
            fill=theme.soft,
            outline=(*theme.accent, 80),
            width=1,
        )
        draw.text(
            (940, y + 70),
            position,
            font=_fit_font(position, 168, 16, 13),
            fill=theme.primary,
            anchor="mm",
        )

        draw.line((240, y + 164, 1031, y + 164), fill=theme.line_color, width=1)
        sample_txt = _sample_label(track)
        draw.text(
            (240, y + 180),
            sample_txt,
            font=_fit_font(sample_txt, 785, 14, 11),
            fill=theme.text_sub,
            anchor="la",
        )
        stat_txt = _sample_stat_line(track)
        draw.text(
            (240, y + 204),
            stat_txt,
            font=_fit_font(stat_txt, 790, 15, 10),
            fill=theme.text_main,
            anchor="la",
        )

        _draw_sample_distribution(
            draw, track, (240, y + 233, 1032, y + 245), theme.accent, theme
        )
        draw.text(
            (240, y + 260), "97%", font=_font(10), fill=theme.text_sub, anchor="ma"
        )
        draw.text(
            (1032, y + 260), "101%", font=_font(10), fill=theme.text_sub, anchor="ma"
        )
        y += TRACK_H + TRACK_GAP

    footer_y = y + 9
    footer = "数据：ChiffonMai 段位表 / Diving-Fish 曲库 / 服务器近期脱敏成绩（低样本使用内置数据）"
    draw.text(
        (540, footer_y),
        footer,
        font=_fit_font(footer, 980, 15, 12),
        fill=theme.text_sub,
        anchor="mm",
    )
    draw.text(
        (540, footer_y + 26),
        "LIFE 为达成率反推的最有利判定组合，仅供练习参考，以机台结果为准",
        font=_font(12),
        fill=theme.text_sub,
        anchor="mm",
    )
    try:
        from ..config import footer_generated

        project_footer = footer_generated()
    except (ImportError, ValueError):
        project_footer = "Generated by maimaiDX QueryBot"
    draw.text(
        (540, footer_y + 51),
        project_footer,
        font=_fit_font(project_footer, 980, 13, 11),
        fill=theme.text_sub,
        anchor="mm",
    )
    return image.convert("RGB")


async def generate_rank_course_image(
    rank_name: str,
    *,
    qqid: Optional[int] = None,
    username: Optional[str] = None,
) -> Image.Image:
    course = get_rank_course(rank_name)
    if course is None:
        raise ValueError(f"无法识别段位「{rank_name}」")

    from .maimaidx_music import mai

    records = []
    player_name = None
    player_plate = None
    player_additional_rating = None
    score_note = None
    if qqid or username:
        try:
            from .maimaidx_datasource import get_user_records

            userinfo, records = await get_user_records(qqid=qqid, username=username)
            player_name = getattr(userinfo, "nickname", None) or username
            player_plate = getattr(userinfo, "plate", None)
            player_additional_rating = getattr(userinfo, "additional_rating", None)
        except Exception as exc:
            from .maimaidx_error import (
                LxnsDataError,
                UserDisabledQueryError,
                UserNotExistsError,
                UserNotFoundError,
            )

            if isinstance(
                exc,
                (
                    LxnsDataError,
                    UserDisabledQueryError,
                    UserNotExistsError,
                    UserNotFoundError,
                ),
            ):
                score_note = "个人成绩不可用，仅展示课题与公开样本"
            else:
                raise
    import asyncio

    course_stats = await asyncio.to_thread(load_course_stats)
    tracks = build_course_tracks(course, mai.total_list, records, course_stats)
    return draw_rank_course(
        course,
        tracks,
        player_name=player_name,
        player_plate=player_plate,
        player_additional_rating=player_additional_rating,
        score_note=score_note,
    )
