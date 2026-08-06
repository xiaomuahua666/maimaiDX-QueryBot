"""
「我有多菜」：根据用户 rating 与查分器全体用户对比，统计超过的人数和百分比，并绘制档位条状图。

排版（单栏纵向）：顶部个人信息卡片 → 统计文案（超过X%）→ 档位分布图表 → 底部署名。
背景：配置 maimaidx_how_weak_bg 时使用自定义图，未配置时使用 b50_bg.png。
支持 rating 数据缓存与成图缓存，时长由 maimaidx_rating_cache_seconds 控制（默认 15 分钟）。
CPU 密集的绘图放入线程池执行，避免阻塞事件循环（单核占满因 Python GIL + PIL 为 CPU 密集型）。
"""
import asyncio
import time
from io import BytesIO
from pathlib import Path
from typing import List, Optional, Tuple, Union

from PIL import Image, ImageDraw, ImageFont

from ..config import SHANGGUMONO, SIYUAN, TBFONT, footer_designed_pipe_generated, maiconfig, maimaidir, static
from .image import DrawText, draw_centered_design_footer, image_to_base64
from .maimaidx_api_data import maiApi
from .maimaidx_error import UserDisabledQueryError, UserNotFoundError, UserNotExistsError
from .maimaidx_model import UserInfo, UserRanking
from .maimaidx_gold_water import _find_ra_pic, _find_match_level
from nonebot.adapters.onebot.v11 import MessageSegment

# ---------- 布局常量（单栏纵向流） ----------
INFO_BLOCK_W = 800
INFO_BLOCK_H = 200
# 画布外边距、区块间距
PADDING = 32
SECTION_GAP = 24
# 内容区最大宽度（个人信息与图表视觉对齐）
CONTENT_MAX_W = 720
# 个人信息卡片：缩放后高度上限、圆角
PROFILE_CARD_MAX_H = 180
PROFILE_CARD_RADIUS = 16
# 统计区：主句字号、副句字号、行高
STATS_HERO_SIZE = 26
STATS_SUB_SIZE = 16
STATS_LINE_GAP = 10
# 图表面板：圆角、标题与图间距、外边距
CHART_PANEL_RADIUS = 16
CHART_TITLE_H = 28
CHART_MARGIN = 24
# 底栏
FOOTER_H = 40
FOOTER_FONT_SIZE = 12

# ---------- rating 档位（查分器一致：含 16500+ 一档，共 26 档） ----------
RATING_BRACKET_THRESHOLDS: Tuple[int, ...] = (
    0, 1000, 2000, 4000, 7000, 10000, 12000, 13000, 14000, 14500,
    15000, 15100, 15200, 15300, 15400, 15500, 15600, 15700, 15800, 15900,
    16000, 16100, 16200, 16300, 16400, 16500,
)
NUM_BRACKETS = len(RATING_BRACKET_THRESHOLDS)


# ---------- 档位与人数统计 ----------
def get_bracket_index(rating: int) -> int:
    """根据 rating 返回档位索引 [0, NUM_BRACKETS-1]。"""
    for i in range(NUM_BRACKETS - 1, -1, -1):
        if rating >= RATING_BRACKET_THRESHOLDS[i]:
            return i
    return 0


def _bracket_counts(rank_list: List[UserRanking]) -> List[int]:
    """统计每个档位的人数。"""
    counts = [0] * NUM_BRACKETS
    for u in rank_list:
        idx = get_bracket_index(u.ra)
        counts[idx] += 1
    return counts


# ---------- 个人信息块（与常规 B50 一致，供等比缩放后贴图） ----------
async def _draw_b50_style_info_block(userinfo: UserInfo, qqid: Optional[int]) -> Image.Image:
    """绘制与常规 B50 一致的个人信息块：牌子、QQ 头像、DX rating 图、五位数字、昵称、段位、B35+B15 文案。"""
    left = 20
    rating_val = int(userinfo.rating or 0)
    add_rating_val = int(userinfo.additional_rating or 0) if userinfo.additional_rating is not None else 0
    im = Image.new('RGBA', (INFO_BLOCK_W, INFO_BLOCK_H), (0, 0, 0, 0))
    from .maimaidx_theme import Theme as _Th, resolve_theme_path as _rtp
    _t = _Th.get_default().value
    _tp = lambda f: _rtp(maimaidir, _t, f)
    # 牌子底
    if userinfo.plate:
        from .maimaidx_table_image import plate_version_path
        _pp = plate_version_path(userinfo.plate)
        if _pp.exists():
            plate = Image.open(_pp).resize((800, 130)).convert('RGBA')
        else:
            plate = Image.open(_tp('UI_Plate_550101.png')).resize((800, 130)).convert('RGBA')
    else:
        plate = Image.open(_tp('UI_Plate_550101.png')).resize((800, 130)).convert('RGBA')
    im.paste(plate, (left, 60))
    icon = Image.open(_tp('UI_Icon_509506.png')).resize((120, 120)).convert('RGBA')
    im.paste(icon, (left + 5, 65), icon)
    if qqid:
        try:
            qq_logo = Image.open(BytesIO(await maiApi.qqlogo(qqid=qqid))).convert('RGBA').resize((120, 120))
            im.paste(qq_logo, (left + 5, 65), qq_logo)
        except Exception:
            pass
    dx_rating = Image.open(_tp(_find_ra_pic(rating_val))).resize((186, 35)).convert('RGBA')
    im.paste(dx_rating, (left + 135, 72), dx_rating)
    rating_str = f'{rating_val:05d}'
    for n, i in enumerate(rating_str):
        num_img = Image.open(_tp(f'UI_NUM_Drating_{i}.png')).resize((17, 20)).convert('RGBA')
        im.paste(num_img, (left + 220 + 15 * n, 80), num_img)
    name_img = Image.open(_tp('Name.png')).convert('RGBA')
    im.paste(name_img, (left + 135, 115), name_img)
    match_level = Image.open(_tp(_find_match_level(add_rating_val))).resize((80, 32)).convert('RGBA')
    im.paste(match_level, (left + 325, 120), match_level)
    class_level = Image.open(_tp('UI_FBR_Class_00.png')).resize((90, 54)).convert('RGBA')
    im.paste(class_level, (left + 320, 60), class_level)
    rating_bar = Image.open(_tp('UI_CMN_Shougou_Rainbow.png')).resize((270, 27)).convert('RGBA')
    im.paste(rating_bar, (left + 135, 160), rating_bar)
    dr = ImageDraw.Draw(im)
    sy = DrawText(dr, SIYUAN)
    tb = DrawText(dr, TBFONT)
    userName = userinfo.nickname or userinfo.username or '未知'
    sy.draw(left + 145, 135, 25, userName, (0, 0, 0, 255), 'lm')
    sd_list = (userinfo.charts and userinfo.charts.sd) or []
    dx_list = (userinfo.charts and userinfo.charts.dx) or []
    sd_ra = sum(r.ra for r in sd_list)
    dx_ra = sum(r.ra for r in dx_list)
    tb.draw(
        left + 270, 172, 17,
        f'B35: {sd_ra} + B15: {dx_ra} = {rating_val}',
        (0, 0, 0, 255), 'mm', 3, (255, 255, 255, 255)
    )
    return im


# ---------- 背景：未配置自定义时使用深色主题渐变 ----------
# 渐变端点色（上 → 下）：深蓝灰 → 近黑，便于与卡片/图表区分
_GRADIENT_TOP = (18, 24, 38, 255)
_GRADIENT_BOTTOM = (6, 8, 14, 255)


def _dark_gradient_bg(width: int, height: int) -> Image.Image:
    """未配置自定义背景时使用深色主题垂直渐变。"""
    strip = Image.new("RGBA", (1, height))
    draw = ImageDraw.Draw(strip)
    for y in range(height):
        t = y / max(height - 1, 1)
        r = int(_GRADIENT_TOP[0] * (1 - t) + _GRADIENT_BOTTOM[0] * t)
        g = int(_GRADIENT_TOP[1] * (1 - t) + _GRADIENT_BOTTOM[1] * t)
        b = int(_GRADIENT_TOP[2] * (1 - t) + _GRADIENT_BOTTOM[2] * t)
        draw.point((0, y), fill=(r, g, b, 255))
    return strip.resize((width, height), Image.NEAREST)


# ---------- 图表配色（用户所在档位柱条使用 success 绿色） ----------
_THEME = {
    "bg": (13, 17, 23),           # #0d1117 主背景
    "surface": (22, 27, 34),      # #161b22 卡片/分区
    "border": (48, 54, 61),       # #30363d 边框/网格
    "text": (230, 237, 243),      # #e6edf3 主文字
    "muted": (139, 148, 158),     # #8b949e 次要文字
    "accent": (88, 166, 255),     # #58a6ff 强调（你的档位）
    "bar": (56, 139, 253),        # #388bfd 其他档位条
    "bar_dim": (33, 38, 45),     # #21262d 空条/弱化
    "success": (63, 185, 80),     # #3fb950 正向数据
}


# ---------- 档位条状图（用户档位绿色，仅显示前后 5 档） ----------
def _draw_bar_chart(
    user_bracket: int,
    bracket_counts: List[int],
) -> Image.Image:
    """绘制条状图：仅柱条与 Y 轴人数；用户所在档位为绿色，其余为蓝/灰。"""
    start = max(0, user_bracket - 5)
    end = min(NUM_BRACKETS, user_bracket + 6)
    n_bars = end - start
    counts_slice = bracket_counts[start:end]
    thresholds_slice = [RATING_BRACKET_THRESHOLDS[i] for i in range(start, end)]
    max_count = max(counts_slice) if counts_slice else 1

    font_path = str(SHANGGUMONO)
    font_axis = ImageFont.truetype(font_path, 14)

    # 图表比例放大（柱高、柱宽、留白均放大）
    bar_max_h = 200
    bar_w = 56
    gap = 12
    chart_inner_w = n_bars * bar_w + (n_bars - 1) * gap
    left_margin = 58
    right_margin = 32
    bottom_margin = 32
    chart_top = 20
    img_w = left_margin + chart_inner_w + right_margin
    img_h = chart_top + bar_max_h + bottom_margin

    im = Image.new('RGB', (img_w, img_h), color=_THEME["bg"])
    draw = ImageDraw.Draw(im)

    chart_left = left_margin
    chart_bottom = chart_top + bar_max_h
    for step in (0, 0.25, 0.5, 0.75, 1.0):
        y = chart_top + int(bar_max_h * (1 - step))
        draw.line([(chart_left, y), (chart_left + chart_inner_w, y)], fill=_THEME["border"], width=1)
    if max_count > 0:
        for val in [0, max_count // 4, max_count // 2, max_count * 3 // 4, max_count]:
            y = chart_bottom - int(bar_max_h * (val / max_count)) if max_count else chart_bottom
            draw.text((chart_left - 8, y), str(val), font=font_axis, fill=_THEME["muted"], anchor='rm')
    draw.text((chart_left - 8, chart_top - 14), "人数", font=font_axis, fill=_THEME["muted"], anchor='rm')

    base_x = chart_left
    for i, (cnt, th) in enumerate(zip(counts_slice, thresholds_slice)):
        x0 = base_x + i * (bar_w + gap)
        x1 = x0 + bar_w
        h = int(bar_max_h * (cnt / max_count)) if max_count and cnt else 0
        y0 = chart_bottom - h
        y1 = chart_bottom
        is_user = (start + i) == user_bracket
        fill_color = _THEME["success"] if is_user else (_THEME["bar"] if cnt else _THEME["bar_dim"])
        draw.rectangle([x0, y0, x1, y1], fill=fill_color, outline=_THEME["border"])
        if cnt > 0:
            draw.text((x0 + bar_w // 2, y0 - 6), str(cnt), font=font_axis, fill=_THEME["text"], anchor='mb')
        draw.text((x0 + bar_w // 2, chart_bottom + 8), str(th), font=font_axis, fill=_THEME["muted"], anchor='mt')
    return im


def _build_how_weak_image_sync(
    info_im: Image.Image,
    user_rating: int,
    total: int,
    rank: int,  # 按 rating 倒序的名次（第 1 名最高）
    percent: float,
    user_bracket: int,
    bracket_counts: List[int],
) -> str:
    """
    纯 CPU 的绘图与合成，在线程池中执行以避免阻塞事件循环。
    返回 base64 字符串（含 base64:// 前缀），供 MessageSegment.image 使用。
    """
    chart_im = _draw_bar_chart(user_bracket, bracket_counts)
    profile_scale = min(CONTENT_MAX_W / INFO_BLOCK_W, PROFILE_CARD_MAX_H / INFO_BLOCK_H)
    profile_w = int(INFO_BLOCK_W * profile_scale)
    profile_h = int(INFO_BLOCK_H * profile_scale)
    profile_scaled = info_im.resize((profile_w, profile_h), Image.LANCZOS)

    stats_hero_h = STATS_HERO_SIZE + STATS_LINE_GAP + STATS_SUB_SIZE
    chart_panel_inner_h = CHART_TITLE_H + chart_im.height
    chart_panel_h = chart_panel_inner_h + 2 * CHART_MARGIN
    total_w = max(CONTENT_MAX_W + 2 * PADDING, chart_im.width + 2 * (CHART_MARGIN + PADDING))
    total_h = (
        PADDING
        + profile_h
        + SECTION_GAP
        + stats_hero_h
        + SECTION_GAP
        + chart_panel_h
        + SECTION_GAP
        + FOOTER_H
    )

    bg_path = None
    if getattr(maiconfig, "maimaidx_how_weak_bg", None):
        p = Path(maiconfig.maimaidx_how_weak_bg)
        if not p.is_absolute():
            p = static / p
        if p.exists():
            bg_path = p
    if bg_path:
        try:
            full_im = Image.open(bg_path).convert("RGBA").resize((total_w, total_h), Image.LANCZOS)
        except Exception:
            full_im = _dark_gradient_bg(total_w, total_h)
    else:
        full_im = _dark_gradient_bg(total_w, total_h)

    dr = ImageDraw.Draw(full_im)
    cx = total_w // 2

    profile_left = cx - profile_w // 2
    profile_top = PADDING
    full_im.paste(profile_scaled, (profile_left, profile_top), profile_scaled)

    stats_y = profile_top + profile_h + SECTION_GAP
    font_hero = ImageFont.truetype(str(SIYUAN), STATS_HERO_SIZE)
    font_sub = ImageFont.truetype(str(SHANGGUMONO), STATS_SUB_SIZE)
    dr.text((cx, stats_y), f"超过了 {percent:.1f}% 的查分器用户", font=font_hero, fill=(*_THEME["text"], 255), anchor="mm")
    dr.text(
        (cx, stats_y + STATS_HERO_SIZE + STATS_LINE_GAP),
        f"Rating  {user_rating:05d}  ·  排名  {rank} / {total}  人",
        font=font_sub,
        fill=(*_THEME["muted"], 255),
        anchor="mm",
    )

    chart_panel_y = stats_y + stats_hero_h + SECTION_GAP
    chart_panel_w = chart_im.width + 2 * CHART_MARGIN
    chart_panel_x = cx - chart_panel_w // 2
    dr.rounded_rectangle(
        [
            chart_panel_x,
            chart_panel_y,
            chart_panel_x + chart_panel_w,
            chart_panel_y + chart_panel_h,
        ],
        radius=CHART_PANEL_RADIUS,
        fill=(*_THEME["surface"], 250),
        outline=(*_THEME["border"], 255),
    )
    font_chart_title = ImageFont.truetype(str(SHANGGUMONO), 14)
    dr.text(
        (chart_panel_x + chart_panel_w // 2, chart_panel_y + CHART_TITLE_H // 2),
        "档位分布",
        font=font_chart_title,
        fill=(*_THEME["muted"], 255),
        anchor="mm",
    )
    chart_x = cx - chart_im.width // 2
    chart_y = chart_panel_y + CHART_TITLE_H + CHART_MARGIN
    full_im.paste(chart_im, (chart_x, chart_y))

    nick = getattr(maiconfig, "botName", "maimai") or "maimai"
    footer_text = footer_designed_pipe_generated('raincore', nick)
    footer_dt = DrawText(dr, SHANGGUMONO)
    draw_centered_design_footer(
        full_im,
        footer_dt,
        footer_text,
        color=(*_THEME["muted"], 255),
        margin_x=PADDING,
        start_font_size=FOOTER_FONT_SIZE,
        min_font_size=9,
        bottom_gap=FOOTER_H // 2,
    )

    return image_to_base64(full_im)


# ---------- rating 缓存（我有多菜） ----------
_how_weak_cache: dict = {}  # key -> (value, expiry_timestamp)


def _cache_get(key: str):
    now = time.time()
    if key in _how_weak_cache:
        val, expiry = _how_weak_cache[key]
        if now < expiry:
            return val
        del _how_weak_cache[key]
    return None


def _cache_set(key: str, value, ttl_seconds: int):
    if ttl_seconds <= 0:
        return
    _how_weak_cache[key] = (value, time.time() + ttl_seconds)


# ---------- 主入口：拉取数据、拼图、背景、底部署名 ----------
async def generate_how_weak(
    qqid: Optional[int] = None,
    username: Optional[str] = None,
) -> Union[MessageSegment, str]:
    """
    获取当前用户 rating 与查分器全体排行，统计超过的人数和百分比，生成带个人信息块与档位条状图的一张图。
    背景：maimaidx_how_weak_bg 未配置时使用深色主题渐变。
    rating 数据使用缓存，时长由 maimaidx_rating_cache_seconds 控制。
    """
    ttl = getattr(maiconfig, 'maimaidx_rating_cache_seconds', 900) or 900
    cache_key_user = f"how_weak_u_{qqid}" if qqid is not None else f"how_weak_un_{username or ''}"
    # 成图缓存：同一用户短时间内再次请求直接返回，避免重复绘图占用 CPU
    img_cached = _cache_get(cache_key_user + "_img")
    if img_cached is not None:
        return MessageSegment.image(img_cached)

    userinfo = _cache_get(cache_key_user)
    rank_list = _cache_get("how_weak_rank")
    # 缓存未命中时并行请求用户 b50 与全服排行，减少总等待时间
    if userinfo is None and rank_list is None:
        from .maimaidx_datasource import get_user_b50
        u_fut = get_user_b50(qqid=qqid, username=username)
        r_fut = maiApi.rating_ranking()
        u_res, r_res = await asyncio.gather(u_fut, r_fut, return_exceptions=True)
        if isinstance(u_res, (UserNotFoundError, UserNotExistsError)):
            return '未绑定查分器或用户不存在，请先绑定后再试。'
        if isinstance(u_res, UserDisabledQueryError):
            return '该用户已被禁止查询。'
        if isinstance(u_res, Exception):
            return f'获取你的 rating 失败：{type(u_res).__name__}'
        if isinstance(r_res, Exception):
            return f'获取查分器排行失败：{type(r_res).__name__}'
        userinfo, rank_list = u_res, r_res
        _cache_set(cache_key_user, userinfo, ttl)
        _cache_set("how_weak_rank", rank_list, ttl)
    else:
        if userinfo is None:
            try:
                from .maimaidx_datasource import get_user_b50
                userinfo = await get_user_b50(qqid=qqid, username=username)
                _cache_set(cache_key_user, userinfo, ttl)
            except (UserNotFoundError, UserNotExistsError):
                return '未绑定查分器或用户不存在，请先绑定后再试。'
            except UserDisabledQueryError:
                return '该用户已被禁止查询。'
            except Exception as e:
                return f'获取你的 rating 失败：{type(e).__name__}'
        if rank_list is None:
            try:
                rank_list = await maiApi.rating_ranking()
                _cache_set("how_weak_rank", rank_list, ttl)
            except Exception as e:
                return f'获取查分器排行失败：{type(e).__name__}'
    user_rating = int(userinfo.rating or 0)

    total = len(rank_list)
    if total == 0:
        return '查分器暂无排行数据。'

    exceeded = sum(1 for u in rank_list if u.ra < user_rating)
    percent = (exceeded / total) * 100.0  # 百分比不变：超过了 xx% 的查分器用户
    # 排名按 rating 倒序：第 1 名最高，传入名次供绘图显示
    rank_display = total - exceeded  # 名次（1-based，1=最高 rating）
    user_bracket = get_bracket_index(user_rating)
    bracket_counts = _bracket_counts(rank_list)

    info_im = await _draw_b50_style_info_block(userinfo, qqid)
    # 将 CPU 密集的绘图放到线程池，避免阻塞事件循环（单核占满来自 Python GIL + PIL 单线程）
    base64_str = await asyncio.to_thread(
        _build_how_weak_image_sync,
        info_im,
        user_rating,
        total,
        rank_display,  # 显示为「排名 x/total」，x 为按 rating 倒序的名次
        percent,
        user_bracket,
        bracket_counts,
    )
    _cache_set(cache_key_user + "_img", base64_str, ttl)
    return MessageSegment.image(base64_str)
