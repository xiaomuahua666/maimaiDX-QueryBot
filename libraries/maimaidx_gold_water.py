# 含金量 / 含水量：基于常规 b50 与拟合难度
import statistics
import traceback
from io import BytesIO
from typing import List, Optional, Tuple, Union

from nonebot.adapters.onebot.v11 import MessageSegment
from PIL import Image, ImageDraw

from ..config import *
from .maimaidx_theme import pic
from .image import DrawText, draw_centered_design_footer, image_to_base64, music_picture
from .maimaidx_error import UserDisabledQueryError, UserNotFoundError, UserNotExistsError, format_command_error
from .maimaidx_model import ChartInfo, UserInfo
from .maimaidx_music import mai
from .maimaidx_api_data import maiApi

try:
    from .maimaidx_best_50 import changeColumnWidth, coloumWidth, dxScore
except Exception:
    changeColumnWidth = lambda s, n: s[:n] + '...' if len(s) > n else s
    coloumWidth = len
    dxScore = lambda x: int(x) if x else 0

# 底部文案
GOLD_WATER_DESIGNER = 'raincore'


def _get_fit_diff_for_chart(record: ChartInfo) -> Optional[float]:
    """从曲目表取该谱面拟合定数。"""
    try:
        music = mai.total_list.by_id(str(record.song_id))
        if not music or not getattr(music, 'stats', None):
            return None
        if record.level_index >= len(music.stats) or not music.stats[record.level_index]:
            return None
        fit = getattr(music.stats[record.level_index], 'fit_diff', None)
        return float(fit) if fit is not None else None
    except Exception:
        return None


def _b50_list(userinfo: UserInfo) -> List[ChartInfo]:
    """常规 b50 的 50 条成绩（B35 + B15）顺序。"""
    sd = (userinfo.charts and userinfo.charts.sd) or []
    dx = (userinfo.charts and userinfo.charts.dx) or []
    return list(sd) + list(dx)


def get_b50_gold_water_pairs(userinfo: UserInfo) -> List[Tuple[ChartInfo, float, float]]:
    """
    获取 b50 中每条成绩的 (record, 含金量, 含水量)。
    含金量 = 拟合难度 - 常规难度；含水量 = 常规难度 - 拟合难度。
    仅包含有拟合难度的谱面。
    """
    out = []
    for r in _b50_list(userinfo):
        fit = _get_fit_diff_for_chart(r)
        if fit is None:
            continue
        gold = round(fit - r.ds, 3)   # 含金量
        water = round(r.ds - fit, 3)  # 含水量
        out.append((r, gold, water))
    return out


def _stats_and_message(
    values: List[float],
    kind: str,
) -> Tuple[float, float, float, float, float, str]:
    """values 为中位数、平均值、最大、最小；(中位数+平均值)/2 为阈值；返回 (中位数, 平均, 最大, 最小, 阈值, 结论文案)。"""
    if not values:
        return (0.0, 0.0, 0.0, 0.0, 0.0, f'b50{kind}暂无数据')
    med = statistics.median(values)
    mean = statistics.mean(values)
    mx = max(values)
    mn = min(values)
    threshold = (med + mean) / 2
    if kind == '含金量':
        if threshold < 0.2:
            msg = 'b50含金量一般...'
        else:
            msg = 'b50含金量很高！太厉害了'
    else:
        if threshold < 0.2:
            msg = 'b50含水量一般 太厉害了！'
        else:
            msg = 'b50含水量很高...'
    return (med, mean, mx, mn, threshold, msg)


def _dx_max(song_id: int, level_index: int) -> int:
    """该谱面 DX 满分（notes 和 *3）。"""
    try:
        music = mai.total_list.by_id(str(song_id))
        if not music or not music.charts or level_index >= len(music.charts):
            return 0
        notes = music.charts[level_index].notes
        return sum(notes) * 3
    except Exception:
        return 0


def _find_ra_pic(rating: int) -> str:
    """与 DrawBest._findRaPic 一致。"""
    if rating < 1000:
        num = '01'
    elif rating < 2000:
        num = '02'
    elif rating < 4000:
        num = '03'
    elif rating < 7000:
        num = '04'
    elif rating < 10000:
        num = '05'
    elif rating < 12000:
        num = '06'
    elif rating < 13000:
        num = '07'
    elif rating < 14000:
        num = '08'
    elif rating < 14500:
        num = '09'
    elif rating < 15000:
        num = '10'
    else:
        num = '11'
    return f'UI_CMN_DXRating_{num}.png'


def _find_match_level(add_rating: int) -> str:
    """与 DrawBest._findMatchLevel 一致。"""
    if add_rating <= 10:
        num = f'{add_rating:02d}'
    else:
        num = f'{add_rating + 1:02d}'
    return f'UI_DNM_DaniPlate_{num}.png'


# 与 ScoreBaseImage 一致的卡片文字/背景色
_T_COLOR = [
    (255, 255, 255, 255), (255, 255, 255, 255), (255, 255, 255, 255),
    (255, 255, 255, 255), (138, 0, 226, 255),
]
_ID_COLOR = [
    (129, 217, 85, 255), (245, 189, 21, 255), (255, 129, 141, 255),
    (159, 81, 220, 255), (138, 0, 226, 255),
]


async def _draw_gold_water_image(
    userinfo: UserInfo,
    pairs: List[Tuple[ChartInfo, float, float]],
    kind: str,
    qqid: Optional[int],
) -> Image.Image:
    """绘制含金量/含水量图。kind 为 '含金量' 或 '含水量'。"""
    if kind == '含金量':
        values = [p[1] for p in pairs]
    else:
        values = [p[2] for p in pairs]
    med, mean, mx, mn, _, conclusion = _stats_and_message(values, kind)

    # 画布最大宽度 = 曲目信息模块宽度（5 列卡片 16 + 276*5 = 1396，取 1400）
    w = 1400
    margin_side = 20
    gap_center = 60
    info_block_w = 800
    cards_bottom = 235 + 114 * 2
    content_height = cards_bottom + 55
    from .maimaidx_theme import Theme, resolve_theme_path
    _theme = Theme.get_default().value
    base = Image.open(resolve_theme_path(maimaidir, _theme, 'b50_bg.png')).convert('RGBA')
    bw, bh = base.size
    im = Image.new('RGBA', (w, content_height))
    im.paste(base.crop((0, 0, min(bw, w), min(bh, content_height))), (0, 0))
    if bw < w:
        right_col = base.crop((bw - 1, 0, bw, min(bh, content_height)))
        for x in range(bw, w):
            im.paste(right_col, (x, 0))
    dr = ImageDraw.Draw(im)
    sy = DrawText(dr, SIYUAN)
    tb = DrawText(dr, TBFONT)

    left = margin_side
    rating_val = int(userinfo.rating or 0)
    add_rating_val = int(userinfo.additional_rating or 0) if userinfo.additional_rating is not None else 0
    from .maimaidx_theme import Theme as _Th, resolve_theme_path as _rtp
    _t = _Th.get_default().value
    _tp = lambda f: _rtp(maimaidir, _t, f)
    dx_rating = Image.open(_tp(_find_ra_pic(rating_val))).resize((186, 35))
    Name = Image.open(_tp('Name.png'))
    MatchLevel = Image.open(_tp(_find_match_level(add_rating_val))).resize((80, 32))
    ClassLevel = Image.open(_tp('UI_FBR_Class_00.png')).resize((90, 54))
    rating_img = Image.open(_tp('UI_CMN_Shougou_Rainbow.png')).resize((270, 27))

    if userinfo.plate:
        from .maimaidx_table_image import open_plate_image
        plate = open_plate_image(userinfo.plate, _tp('UI_Plate_550101.png')).resize((800, 130))
    else:
        plate = Image.open(_tp('UI_Plate_550101.png')).resize((800, 130))
    im.alpha_composite(plate, (left, 60))
    icon = Image.open(_tp('UI_Icon_509506.png')).resize((120, 120))
    im.alpha_composite(icon, (left + 5, 65))
    if qqid:
        try:
            qqLogo = Image.open(BytesIO(await maiApi.qqlogo(qqid=qqid)))
            im.alpha_composite(qqLogo.convert('RGBA').resize((120, 120)), (left + 5, 65))
        except Exception:
            pass
    im.alpha_composite(dx_rating, (left + 135, 72))
    rating_str = f'{rating_val:05d}'
    for n, i in enumerate(rating_str):
        im.alpha_composite(
            Image.open(_tp(f'UI_NUM_Drating_{i}.png')).resize((17, 20)), (left + 220 + 15 * n, 80)
        )
    im.alpha_composite(Name, (left + 135, 115))
    im.alpha_composite(MatchLevel, (left + 325, 120))
    im.alpha_composite(ClassLevel, (left + 320, 60))
    im.alpha_composite(rating_img, (left + 135, 160))

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

    # 含金量信息：仅文字，与个人信息同一高度（约 60～172 区间内垂直居中）
    avg_val = round(mean, 3)
    summary_lines = [
        f'您的b50平均{kind}为: {avg_val}',
        f'{kind}最大为: {round(mx, 3)}, 最小为: {round(mn, 3)}',
        conclusion,
    ]
    stats_area_left = left + info_block_w + gap_center
    stats_center_x = (stats_area_left + w - margin_side) // 2
    info_top, info_bottom = 60, 172
    line_h = 28
    stats_base_y = (info_top + info_bottom - (len(summary_lines) - 1) * line_h) // 2
    font_sizes = (22, 22, 24)
    for i, line in enumerate(summary_lines):
        sy.draw(stats_center_x, stats_base_y + i * line_h, font_sizes[i], line, (0, 0, 0, 255), 'mm')

    # 10 张曲目卡：与常规 b50 相同 UI（_diff 底、曲绘、版本、评价、fc/fs、dx 星、定数显示为拟合定数 + 含金量/含水量）
    _diff = [
        Image.open(pic('b50_score_basic.png')),
        Image.open(pic('b50_score_advanced.png')),
        Image.open(pic('b50_score_expert.png')),
        Image.open(pic('b50_score_master.png')),
        Image.open(pic('b50_score_remaster.png')),
    ]
    dy = 114
    y_row0 = 235
    if kind == '含金量':
        sorted_pairs = sorted(pairs, key=lambda p: p[1], reverse=True)
    else:
        sorted_pairs = sorted(pairs, key=lambda p: p[2], reverse=True)
    show = sorted_pairs[:10]
    for num, (rec, gold, water) in enumerate(show):
        row, col = num // 5, num % 5
        x = 16 + col * 276
        y = y_row0 + row * dy

        fit_diff = round(rec.ds + gold, 1)
        val = gold if kind == '含金量' else water
        level_index = getattr(rec, 'level_index', 0)
        if level_index >= len(_diff):
            level_index = 0

        cover = Image.open(music_picture(rec.song_id)).resize((75, 75))
        version = Image.open(pic(f'{rec.type.upper()}.png')).resize((37, 14))
        from .maimaidx_theme import Theme as _Th, resolve_theme_path as _rtp
        _t = _Th.get_default().value
        rate_key = getattr(rec, 'rate', None) or 'sss'
        if rate_key and rate_key.islower() and rate_key in score_Rank_l:
            rate = Image.open(_rtp(maimaidir, _t, f'UI_TTR_Rank_{score_Rank_l[rate_key]}.png')).resize((63, 28))
        else:
            rate = Image.open(_rtp(maimaidir, _t, f'UI_TTR_Rank_{rate_key if rate_key else "SSS"}.png')).resize((63, 28))
        im.alpha_composite(_diff[level_index], (x, y))
        im.alpha_composite(cover, (x + 12, y + 12))
        im.alpha_composite(version, (x + 51, y + 91))
        im.alpha_composite(rate, (x + 92, y + 78))
        if getattr(rec, 'fc', None) and rec.fc:
            fc = Image.open(pic(f'UI_MSS_MBase_Icon_{fcl[rec.fc]}.png')).resize((34, 34))
            im.alpha_composite(fc, (x + 154, y + 77))
        if getattr(rec, 'fs', None) and rec.fs:
            fs = Image.open(pic(f'UI_MSS_MBase_Icon_{fsl[rec.fs]}.png')).resize((34, 34))
            im.alpha_composite(fs, (x + 185, y + 77))
        dxscore = _dx_max(rec.song_id, level_index)
        if dxscore:
            dxnum = dxScore(rec.dxScore / dxscore * 100)
            if dxnum:
                im.alpha_composite(
                    Image.open(pic(f'UI_GAM_Gauge_DXScoreIcon_0{dxnum}.png')).resize((47, 26)), (x + 217, y + 80)
                )

        tb.draw(x + 26, y + 98, 13, rec.song_id, _ID_COLOR[level_index], anchor='mm')
        title = rec.title
        if coloumWidth(title) > 18:
            title = changeColumnWidth(title, 17) + '...'
        sy.draw(x + 93, y + 14, 14, title, _T_COLOR[level_index], anchor='lm')
        tb.draw(x + 93, y + 38, 30, f'{rec.achievements:.4f}%', _T_COLOR[level_index], anchor='lm')
        tb.draw(x + 219, y + 65, 15, f'{rec.dxScore}/{dxscore or 0}', _T_COLOR[level_index], anchor='mm')
        tb.draw(x + 93, y + 65, 15, f'{fit_diff} ({val:+.3f})', _T_COLOR[level_index], anchor='lm')

    draw_centered_design_footer(
        im,
        sy,
        footer_designed_pipe_generated(GOLD_WATER_DESIGNER),
        color=(124, 129, 255, 255),
        margin_x=80,
        start_font_size=14,
        min_font_size=9,
        bottom_gap=24,
    )
    return im


async def generate_gold_content(qqid: Optional[int] = None, username: Optional[str] = None) -> Union[MessageSegment, str]:
    """含金量：常规 b50 拟合难度 - 常规难度，统计并出图。"""
    try:
        if username:
            qqid = None
        from .maimaidx_datasource import get_user_b50
        userinfo = await get_user_b50(qqid=qqid, username=username)
        pairs = get_b50_gold_water_pairs(userinfo)
        if not pairs:
            return '没有可用的拟合难度数据，无法计算含金量'
        im = await _draw_gold_water_image(userinfo, pairs, '含金量', qqid)
        return MessageSegment.image(image_to_base64(im))
    except (UserNotFoundError, UserNotExistsError, UserDisabledQueryError) as e:
        return str(e)
    except Exception as e:
        from .. import log
        log.error(traceback.format_exc())
        return format_command_error(e)


async def generate_water_content(qqid: Optional[int] = None, username: Optional[str] = None) -> Union[MessageSegment, str]:
    """含水量：常规难度 - 拟合难度，统计并出图。"""
    try:
        if username:
            qqid = None
        from .maimaidx_datasource import get_user_b50
        userinfo = await get_user_b50(qqid=qqid, username=username)
        pairs = get_b50_gold_water_pairs(userinfo)
        if not pairs:
            return '没有可用的拟合难度数据，无法计算含水量'
        im = await _draw_gold_water_image(userinfo, pairs, '含水量', qqid)
        return MessageSegment.image(image_to_base64(im))
    except (UserNotFoundError, UserNotExistsError, UserDisabledQueryError) as e:
        return str(e)
    except Exception as e:
        from .. import log
        log.error(traceback.format_exc())
        return format_command_error(e)
