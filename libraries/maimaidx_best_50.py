import math
import traceback
from datetime import datetime
from io import BytesIO
from typing import List, Optional, Tuple, Union, overload

from nonebot.adapters.onebot.v11 import MessageSegment
from PIL import Image, ImageDraw

from ..config import *
from .maimaidx_theme import pic
from .image import DrawText, draw_centered_design_footer, image_to_base64, music_picture
from .maimaidx_api_data import maiApi
from .maimaidx_error import *
from .maimaidx_model import ChartInfo, Data, PlayInfoDefault, PlayInfoDev, UserInfo
from .maimaidx_music import mai
from .maimaidx_playcount_db import PlayCountRecord, pc_db


def _prepare_b50_warnings(userinfo: UserInfo, qqid: Optional[int], username: Optional[str] = None) -> None:
    from .maimaidx_b50_warnings import prepare_b50_warnings, resolve_b50_source
    prepare_b50_warnings(userinfo, resolve_b50_source(qqid, username))


def _is_utage_song(song_id: Union[str, int]) -> bool:
    """
    判断歌曲是否为宴谱（宴会場/utage 类型）。

    判定规则（任一满足即视为宴谱）：
        1. 歌曲 ID（internalId）>= 100000 —— maimaiDX 约定 utage 谱面占用此 ID 段，
           是最可靠的判断方式，且不依赖曲目表能否查到（maimaidx_music.py、image.py
           等处也是同样约定）。
        2. 在曲目表中可查到且 basic_info.genre == "宴会場"。

    为何需要 ID 段判断：
        当 maimaidx_data_source = "dxdata"（使用本地 dxdata.json）时，
        DxDataSource._convert_song 只处理 type ∈ {dx, sd, std} 的 sheet，
        utage / utage2p 会被整体丢弃，于是 mai.total_list 根本不包含这些条目，
        基于 genre 的判断会漏过宴谱，导致宴谱成绩混入 b50 / b35 等统计。
        改为优先用 ID 段判断后，无论使用哪种数据源都能正确过滤宴谱。

    Args:
        song_id: 歌曲 ID

    Returns:
        True 如果是宴谱，False 否则
    """
    try:
        if int(str(song_id)) >= 100000:
            return True
    except (TypeError, ValueError):
        pass
    try:
        music = mai.total_list.by_id(str(song_id))
        if music and music.basic_info.genre == "宴会場":
            return True
    except Exception:
        pass
    return False


def filter_utage_records(records: List[Union[PlayInfoDev, PlayInfoDefault]]) -> List[Union[PlayInfoDev, PlayInfoDefault]]:
    """
    过滤掉宴谱成绩。
    
    Args:
        records: 成绩记录列表（支持 PlayInfoDev 或 PlayInfoDefault）
    
    Returns:
        过滤后的成绩记录列表（不含宴谱）
    """
    return [r for r in records if not _is_utage_song(r.song_id)]


class ScoreBaseImage:
    
    text_color = (124, 129, 255, 255)
    t_color = [
        (255, 255, 255, 255), 
        (255, 255, 255, 255), 
        (255, 255, 255, 255), 
        (255, 255, 255, 255), 
        (138, 0, 226, 255)
    ]
    id_color = [
        (129, 217, 85, 255), 
        (245, 189, 21, 255),  
        (255, 129, 141, 255), 
        (159, 81, 220, 255),
        (138, 0, 226, 255)
    ]
    bg_color = [
        (111, 212, 61, 255), 
        (248, 183, 9, 255), 
        (255, 129, 141, 255), 
        (159, 81, 220, 255), 
        (219, 170, 255, 255)
    ]
    id_diff = [Image.new('RGBA', (55, 10), color) for color in bg_color]
    
    _diff = []
    _rise = []
    title_bg = None
    title_lengthen_bg = None
    design_bg = None
    aurora_bg = None
    shines_bg = None
    pattern_bg = None
    rainbow_bg = None
    rainbow_bottom_bg = None

    @classmethod
    def _load_image(cls):
        """将部分图片保存在内存（使用默认主题）"""
        from .maimaidx_theme import Theme, resolve_theme_path
        _theme = Theme.get_default().value
        def _r(f):
            p = resolve_theme_path(maimaidir, _theme, f)
            return Image.open(p) if p.exists() else None
        cls._diff = [
            Image.open(pic('b50_score_basic.png')), 
            Image.open(pic('b50_score_advanced.png')), 
            Image.open(pic('b50_score_expert.png')), 
            Image.open(pic('b50_score_master.png')), 
            Image.open(pic('b50_score_remaster.png'))
        ]
        cls._rise = [
            Image.open(pic('rise_score_basic.png')),
            Image.open(pic('rise_score_advanced.png')),
            Image.open(pic('rise_score_expert.png')),
            Image.open(pic('rise_score_master.png')),
            Image.open(pic('rise_score_remaster.png'))
        ]
        cls.title_bg = _r('title.png')
        cls.title_lengthen_bg = _r('title-lengthen.png')
        cls.design_bg = _r('design.png')
        cls.aurora_bg = Image.open(pic('aurora.png')).convert('RGBA').resize((1400, 220))
        cls.shines_bg = Image.open(pic('bg_shines.png')).convert('RGBA')
        cls.pattern_bg = Image.open(pic('pattern.png'))
        cls.rainbow_bg = Image.open(pic('rainbow.png')).convert('RGBA')
        cls.rainbow_bottom_bg = Image.open(pic('rainbow_bottom.png')).convert('RGBA').resize((1200, 200))
    
    def __init__(self, image: Image.Image = None, theme: str = None) -> None:
        from .maimaidx_theme import Theme as _Theme
        if theme is None:
            theme = _Theme.get_default().value
        self._theme = theme
        self.play_counts: dict[tuple[int, int], int] = {}
        if not maiconfig.saveinmem:
            self.load_image(theme)
        
        if image is not None:
            self._im = image
            dr = ImageDraw.Draw(self._im)
            self._sy = DrawText(dr, SIYUAN)
            self._tb = DrawText(dr, TBFONT)
    
    def load_image(self, theme: str = None):
        """在图片不保存在内存时使用，按主题加载"""
        from .maimaidx_theme import Theme, resolve_theme_path
        if theme is None:
            theme = Theme.get_default().value
        def _r(f):
            p = resolve_theme_path(maimaidir, theme, f)
            return Image.open(p) if p.exists() else None
        self._diff = [
            Image.open(pic('b50_score_basic.png')), 
            Image.open(pic('b50_score_advanced.png')), 
            Image.open(pic('b50_score_expert.png')), 
            Image.open(pic('b50_score_master.png')), 
            Image.open(pic('b50_score_remaster.png'))
        ]
        self._rise = [
            Image.open(pic('rise_score_basic.png')),
            Image.open(pic('rise_score_advanced.png')),
            Image.open(pic('rise_score_expert.png')),
            Image.open(pic('rise_score_master.png')),
            Image.open(pic('rise_score_remaster.png'))
        ]
        self.title_bg = _r('title.png')
        self.title_lengthen_bg = _r('title-lengthen.png')
        self.design_bg = _r('design.png')
        self.aurora_bg = Image.open(pic('aurora.png')).convert('RGBA').resize((1400, 220))
        self.shines_bg = Image.open(pic('bg_shines.png')).convert('RGBA')
        self.pattern_bg = Image.open(pic('pattern.png'))
        self.rainbow_bg = Image.open(pic('rainbow.png')).convert('RGBA')
        self.rainbow_bottom_bg = Image.open(pic('rainbow_bottom.png')).convert('RGBA').resize((1200, 200))
    
    def whiledraw(
        self, 
        data: Union[List[ChartInfo], List[PlayInfoDefault], List[PlayInfoDev]], 
        dx: bool, 
        height: int = 0
    ) -> None:
        """
        循环绘制成绩
        
        Params:
            `data`: 数据
            `dx`: 是否为新版本成绩
            `height`: 起始高度
        """
        # y为第一排纵向坐标，dy为各行间距
        dy = 114
        if data and type(data[0]) == ChartInfo:
            # 当 height 非 0（compact_layout）时第二块紧接第一块，无视分组
            y = (height if height != 0 else 1085) if dx else 235
        else:
            # FC/AP B50 等传入 PlayInfoDev 时需与 B50 同布局，否则 height=0 会从 y=0 绘制盖住头部
            y = height if height != 0 else (1085 if dx else 235)
        for num, info in enumerate(data):
            if num % 5 == 0:
                x = 16
                y += dy if num != 0 else 0
            else:
                x += 276

            cover = Image.open(music_picture(info.song_id)).resize((75, 75))
            # info.type = 谱面类型 SD 标准谱面 / DX DX谱面
            version = Image.open(pic(f'{info.type.upper()}.png')).resize((37, 14))
            # 成绩图标：按评级选择 UI_TTR_Rank_*.png，与 config.score_Rank_l 对应（走主题路径）
            from .maimaidx_theme import resolve_theme_path as _rtp
            rate_key = getattr(info, 'rate', None) or 'D'
            if rate_key.islower() and rate_key in score_Rank_l:
                rate_name = score_Rank_l[rate_key]
            else:
                rate_name = rate_key
            rate = Image.open(_rtp(maimaidir, self._theme, f'UI_TTR_Rank_{rate_name}.png')).resize((63, 28))

            self._im.alpha_composite(self._diff[info.level_index], (x, y))
            self._im.alpha_composite(cover, (x + 12, y + 12))
            self._im.alpha_composite(version, (x + 51, y + 91))
            self._im.alpha_composite(rate, (x + 92, y + 78))
            if info.fc:
                fc = Image.open(pic(f'UI_MSS_MBase_Icon_{fcl[info.fc]}.png')).resize((34, 34))
                self._im.alpha_composite(fc, (x + 154, y + 77))
            if info.fs:
                fs = Image.open(pic(f'UI_MSS_MBase_Icon_{fsl[info.fs]}.png')).resize((34, 34))
                self._im.alpha_composite(fs, (x + 185, y + 77))
            
            try:
                music = mai.total_list.by_id(str(info.song_id))
                dxscore_max = sum(music.charts[info.level_index].notes) * 3 if music and info.level_index < len(music.charts) else 0
            except Exception:
                dxscore_max = 0
            dxnum = dx_star_count(info.dxScore, dxscore_max)
            if dxnum:
                self._im.alpha_composite(
                    Image.open(pic(f'UI_GAM_Gauge_DXScoreIcon_0{dxnum}.png')).resize((47, 26)), (x + 217, y + 80)
                )
            self._tb.draw(x + 219, y + 65, 15, f'{info.dxScore}/{dxscore_max}', self.t_color[info.level_index], anchor='mm')

            self._tb.draw(x + 26, y + 98, 13, info.song_id, self.id_color[info.level_index], anchor='mm')
            title = info.title
            pc_count = self.play_counts.get((info.song_id, info.level_index))
            max_title_width = 12 if pc_count else 18
            if coloumWidth(title) > max_title_width:
                title = changeColumnWidth(title, max_title_width - 1) + '...'
            self._sy.draw(x + 93, y + 14, 14, title, self.t_color[info.level_index], anchor='lm')
            self._tb.draw(x + 93, y + 38, 30, f'{info.achievements:.4f}%', self.t_color[info.level_index], anchor='lm')
            self._tb.draw(x + 93, y + 65, 15, f'{info.ds:.1f} -> {info.ra}', self.t_color[info.level_index], anchor='lm')
            if pc_count:
                self._sy.draw(x + 258, y + 14, 14, f'pc:{pc_count}', self.t_color[info.level_index], anchor='rm')

    def whiledraw_with_source(
        self,
        data: List[Tuple[ChartInfo, str]],
        start_y: int = 235,
    ) -> None:
        """绘制带来源昵称的成绩列表（合作 B50：每张卡下方显示来自谁的 b50）。"""
        dy = 114
        y = start_y
        for num, (info, source_nick) in enumerate(data):
            if num % 5 == 0:
                x = 16
                y += dy if num != 0 else 0
            else:
                x += 276

            cover = Image.open(music_picture(info.song_id)).resize((75, 75))
            version = Image.open(pic(f'{info.type.upper()}.png')).resize((37, 14))
            from .maimaidx_theme import resolve_theme_path as _rtp
            rate_key = getattr(info, 'rate', None) or 'D'
            if rate_key.islower() and rate_key in score_Rank_l:
                rate_name = score_Rank_l[rate_key]
            else:
                rate_name = rate_key
            rate = Image.open(_rtp(maimaidir, self._theme, f'UI_TTR_Rank_{rate_name}.png')).resize((63, 28))

            self._im.alpha_composite(self._diff[info.level_index], (x, y))
            self._im.alpha_composite(cover, (x + 12, y + 12))
            self._im.alpha_composite(version, (x + 51, y + 91))
            self._im.alpha_composite(rate, (x + 92, y + 78))
            if info.fc:
                fc = Image.open(pic(f'UI_MSS_MBase_Icon_{fcl[info.fc]}.png')).resize((34, 34))
                self._im.alpha_composite(fc, (x + 154, y + 77))
            if info.fs:
                fs = Image.open(pic(f'UI_MSS_MBase_Icon_{fsl[info.fs]}.png')).resize((34, 34))
                self._im.alpha_composite(fs, (x + 185, y + 77))

            try:
                music = mai.total_list.by_id(str(info.song_id))
                dxscore_max = sum(music.charts[info.level_index].notes) * 3 if music and info.level_index < len(music.charts) else 0
            except Exception:
                dxscore_max = 0
            dxnum = dx_star_count(info.dxScore, dxscore_max)
            if dxnum:
                self._im.alpha_composite(
                    Image.open(pic(f'UI_GAM_Gauge_DXScoreIcon_0{dxnum}.png')).resize((47, 26)), (x + 217, y + 80)
                )
            self._tb.draw(x + 219, y + 65, 15, f'{info.dxScore}/{dxscore_max}', self.t_color[info.level_index], anchor='mm')

            self._tb.draw(x + 26, y + 98, 13, info.song_id, self.id_color[info.level_index], anchor='mm')
            # 歌名左侧，预留右侧空间给昵称（约 10 字宽）
            title = info.title
            if coloumWidth(title) > 12:
                title = changeColumnWidth(title, 11) + '...'
            self._sy.draw(x + 93, y + 14, 14, title, self.t_color[info.level_index], anchor='lm')
            pc_count = self.play_counts.get((info.song_id, info.level_index))
            if pc_count:
                self._sy.draw(x + 258, y + 14, 14, f'pc:{pc_count}', self.t_color[info.level_index], anchor='rm')
            else:
                # 昵称在歌名右方，用思源字体避免中文乱码，小号 12，黑色，右对齐
                source_text = truncate_nickname(source_nick, 8)
                self._sy.draw(x + 258, y + 15, 12, source_text, (0, 0, 0, 255), anchor='rm')
            self._tb.draw(x + 93, y + 38, 30, f'{info.achievements:.4f}%', self.t_color[info.level_index], anchor='lm')
            self._tb.draw(x + 93, y + 65, 15, f'{info.ds:.1f} -> {info.ra}', self.t_color[info.level_index], anchor='lm')


class DrawBest(ScoreBaseImage):

    # B35 占 7 行，行间距 dy=114，B15 紧接其后时起始 y = 235 + 7*114
    _COMPACT_B15_START_Y = 235 + 7 * 114  # 1033

    def __init__(
        self,
        UserInfo: UserInfo,
        qqid: Optional[Union[int, str]] = None,
        *,
        compact_layout: bool = False,
        hide_logo: bool = False,
        play_counts: Optional[dict[tuple[int, int], int]] = None,
        max_display: int = 50,
        compact_subtitle: Optional[str] = None,
        source_label: Optional[str] = None,
        theme: str = None,
    ) -> None:
        from .maimaidx_theme import Theme, resolve_theme_path, get_user_theme
        if theme is None:
            # 自动按 qqid 查用户偏好；查不到走默认主题
            if qqid is not None:
                try:
                    theme = get_user_theme(int(qqid))
                except Exception:
                    theme = Theme.get_default().value
            else:
                theme = Theme.get_default().value
        self._theme = theme
        bg_path = resolve_theme_path(maimaidir, theme, 'b50_bg.png')
        super().__init__(Image.open(bg_path).convert('RGBA'), theme=theme)
        if play_counts:
            self.play_counts = play_counts
        self.userName = UserInfo.nickname or UserInfo.username or '未知'
        self.plate = UserInfo.plate
        self.addRating = int(UserInfo.additional_rating) if UserInfo.additional_rating is not None else 0
        # Rating 用于红框位置：dx_rating 等级图 (435,72)、五位数字 (520,80)、彩虹条 (435,160)
        self.Rating = int(UserInfo.rating) if UserInfo.rating is not None else 0
        # 查分器 sd=B35、dx=B15（与谱面类型 SD/DX 无关）
        self.sdBest = (UserInfo.charts and UserInfo.charts.sd) or []  # B35 区
        self.dxBest = (UserInfo.charts and UserInfo.charts.dx) or []   # B15 区
        self.qqid = qqid
        # True：fcallb50/apallb50，成绩连续列出无 B35/B15 间空白
        self.compact_layout = compact_layout
        # True：x代b50 等不绘制左侧 logo
        self.hide_logo = hide_logo
        # 最大显示条数（50=B50, 35=B35），用于 compact_layout 的副标题显示
        self.max_display = max_display
        self.compact_subtitle = compact_subtitle
        self.source_label = source_label

    def _findRaPic(self) -> str:
        """
        寻找指定的Rating图片
        
        Returns:
            `str` 返回图片名称
        """
        if self.Rating < 1000:
            num = '01'
        elif self.Rating < 2000:
            num = '02'
        elif self.Rating < 4000:
            num = '03'
        elif self.Rating < 7000:
            num = '04'
        elif self.Rating < 10000:
            num = '05'
        elif self.Rating < 12000:
            num = '06'
        elif self.Rating < 13000:
            num = '07'
        elif self.Rating < 14000:
            num = '08'
        elif self.Rating < 14500:
            num = '09'
        elif self.Rating < 15000:
            num = '10'
        else:
            num = '11'
        return f'UI_CMN_DXRating_{num}.png'

    def _findMatchLevel(self) -> str:
        """
        寻找匹配等级图片
        
        Returns:
            `str` 返回图片名称
        """
        if self.addRating <= 10:
            num = f'{self.addRating:02d}'
        else:
            num = f'{self.addRating + 1:02d}'
        return f'UI_DNM_DaniPlate_{num}.png'

    async def draw(self) -> Image.Image:
        """异步取头像，再把 Pillow 重绘交给独立图片线程池。"""
        qq_logo_bytes: Optional[bytes] = None
        if self.qqid:
            try:
                qq_logo_bytes = await maiApi.qqlogo(qqid=self.qqid)
            except Exception:
                pass
        from .maimaidx_image_executor import run_image_cpu
        return await run_image_cpu(self._draw_sync, qq_logo_bytes)

    def _draw_sync(self, qq_logo_bytes: Optional[bytes] = None) -> Image.Image:
        from .maimaidx_theme import resolve_theme_path as _rtp
        _t = self._theme
        _tp = lambda f: _rtp(maimaidir, _t, f)
        
        dx_rating = Image.open(_tp(self._findRaPic())).resize((186, 35))
        Name = Image.open(_tp('Name.png'))
        MatchLevel = Image.open(_tp(self._findMatchLevel())).resize((80, 32))
        ClassLevel = Image.open(_tp('UI_FBR_Class_00.png')).resize((90, 54))
        rating = Image.open(_tp('UI_CMN_Shougou_Rainbow.png')).resize((270, 27))

        if not self.hide_logo:
            logo = Image.open(_tp('logo.png')).resize((249, 120))
            self._im.alpha_composite(logo, (14, 60))
        if self.plate:
            from .maimaidx_table_image import open_plate_image
            plate = open_plate_image(self.plate, _tp('UI_Plate_550101.png')).resize((800, 130))
        else:
            plate = Image.open(_tp('UI_Plate_550101.png')).resize((800, 130))
        self._im.alpha_composite(plate, (300, 60))
        icon = Image.open(_tp('UI_Icon_509506.png')).resize((120, 120))
        self._im.alpha_composite(icon, (305, 65))
        if qq_logo_bytes:
            try:
                qqLogo = Image.open(BytesIO(qq_logo_bytes))
                self._im.alpha_composite(qqLogo.convert('RGBA').resize((120, 120)), (305, 65))
            except Exception:
                pass
        self._im.alpha_composite(dx_rating, (435, 72))
        Rating = f'{self.Rating:05d}'
        for n, i in enumerate(Rating):
            self._im.alpha_composite(
                Image.open(_tp(f'UI_NUM_Drating_{i}.png')).resize((17, 20)), (520 + 15 * n, 80)
            )
        self._im.alpha_composite(Name, (435, 115))
        self._im.alpha_composite(MatchLevel, (625, 120))
        self._im.alpha_composite(ClassLevel, (620, 60))
        self._im.alpha_composite(rating, (435, 160))

        self._sy.draw(445, 135, 25, self.userName, (0, 0, 0, 255), 'lm')
        if self.compact_layout:
            # 无视分组：用思源字体绘制含中文的副标题，避免「首」等字乱码
            subtitle = self.compact_subtitle or f'{self.max_display}首等于{self.Rating}'
            self._sy.draw(
                570, 172, 17,
                subtitle,
                (0, 0, 0, 255), 'mm', 3, (255, 255, 255, 255)
            )
        else:
            sdrating, dxrating = sum([_.ra for _ in self.sdBest]), sum([_.ra for _ in self.dxBest])
            self._sy.draw(
                570, 172, 17,
                f'B35: {sdrating} + B15: {dxrating} = {self.Rating}',
                (0, 0, 0, 255), 'mm', 3, (255, 255, 255, 255)
            )
        if self.source_label:
            self._sy.draw(
                570, 198, 14,
                f'数据源：{self.source_label}',
                (70, 70, 70, 255), 'mm', 2, (255, 255, 255, 220)
            )
        draw_centered_design_footer(
            self._im,
            self._sy,
            footer_designed_generated(),
            color=self.text_color,
            margin_x=60,
            start_font_size=14,
            min_font_size=10,
            bottom_gap=28,
        )

        self.whiledraw(self.sdBest, False)
        # compact_layout 时 B15 紧接 B35 绘制，不留空白
        self.whiledraw(
            self.dxBest, True,
            height=self._COMPACT_B15_START_Y if self.compact_layout else 0,
        )

        return self._im


def _find_ra_pic(rating: int) -> str:
    """根据 rating 数值返回对应的等级图片名（与 DrawBest._findRaPic 一致）。"""
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


class DrawCoopB50(ScoreBaseImage):
    """
    合作 B50 绘图。
    - 分组模式：sd_list(35) + dx_list(15)，与常规 b50 一致分 B35/B15 两段排版，中间留空。
    - 无视分组模式：merged_list(50)，连续一排。
    """

    # 与常规 b50 一致：B15 起始 y 坐标（B35 占 7 行后留空再画 B15）
    _B15_START_Y = 1085

    def __init__(
        self,
        nickname_a: str,
        nickname_b: str,
        qqid_a: Optional[Union[int, str]] = None,
        qqid_b: Optional[Union[int, str]] = None,
        *,
        merged_list: Optional[List[Tuple[ChartInfo, str]]] = None,
        sd_list: Optional[List[Tuple[ChartInfo, str]]] = None,
        dx_list: Optional[List[Tuple[ChartInfo, str]]] = None,
    ) -> None:
        from .maimaidx_theme import Theme, resolve_theme_path
        _theme = Theme.get_default().value
        super().__init__(Image.open(resolve_theme_path(maimaidir, _theme, 'b50_bg.png')).convert('RGBA'))
        self.nickname_a = nickname_a or '用户A'
        self.nickname_b = nickname_b or '用户B'
        self.qqid_a = qqid_a
        self.qqid_b = qqid_b
        if sd_list is not None and dx_list is not None:
            self.grouped = True
            self.sd_list = sd_list
            self.dx_list = dx_list
            self.merged_list = sd_list + dx_list
        else:
            self.grouped = False
            self.merged_list = merged_list or []
            self.sd_list = self.dx_list = None
        self.Rating = sum(info.ra for info, _ in self.merged_list)

    async def draw(self) -> Image.Image:
        from .maimaidx_theme import resolve_theme_path as _rtp
        _t = self._theme
        _tp = lambda f: _rtp(maimaidir, _t, f)
        logo = Image.open(_tp('logo.png')).resize((249, 120))
        dx_rating = Image.open(_tp(_find_ra_pic(self.Rating))).resize((186, 35))
        Name = Image.open(_tp('Name.png'))
        MatchLevel = Image.open(_tp('UI_DNM_DaniPlate_00.png')).resize((80, 32))
        ClassLevel = Image.open(_tp('UI_FBR_Class_00.png')).resize((90, 54))
        rating = Image.open(_tp('UI_CMN_Shougou_Rainbow.png')).resize((270, 27))

        self._im.alpha_composite(logo, (14, 60))
        plate = Image.open(_tp('UI_Plate_550101.png')).resize((800, 130))
        self._im.alpha_composite(plate, (300, 60))
        icon = Image.open(_tp('UI_Icon_509506.png')).resize((120, 120))
        self._im.alpha_composite(icon, (305, 65))
        if self.qqid_a:
            try:
                qqLogo = Image.open(BytesIO(await maiApi.qqlogo(qqid=self.qqid_a)))
                self._im.alpha_composite(qqLogo.convert('RGBA').resize((120, 120)), (305, 65))
            except Exception:
                pass
        self._im.alpha_composite(dx_rating, (435, 72))
        Rating_str = f'{self.Rating:05d}'
        for n, i in enumerate(Rating_str):
            self._im.alpha_composite(
                Image.open(_tp(f'UI_NUM_Drating_{i}.png')).resize((17, 20)), (520 + 15 * n, 80)
            )
        self._im.alpha_composite(Name, (435, 115))
        self._im.alpha_composite(MatchLevel, (625, 120))
        self._im.alpha_composite(ClassLevel, (620, 60))
        self._im.alpha_composite(rating, (435, 160))

        self._sy.draw(445, 135, 25, '合作 B50', (0, 0, 0, 255), 'lm')
        self._sy.draw(445, 165, 16, f'{truncate_nickname(self.nickname_a, 10)} · {truncate_nickname(self.nickname_b, 10)}', (0, 0, 0, 255), 'lm')
        ra_a = sum(info.ra for info, src in self.merged_list if src == self.nickname_a)
        ra_b = sum(info.ra for info, src in self.merged_list if src == self.nickname_b)
        name_a = truncate_nickname(self.nickname_a, 8)
        name_b = truncate_nickname(self.nickname_b, 8)
        rainbow_text = f'合作b50={name_a}贡献的<{ra_a}>+{name_b}贡献的<{ra_b}>'
        self._sy.draw(
            570, 172, 17,
            rainbow_text,
            (0, 0, 0, 255), 'mm', 3, (255, 255, 255, 255)
        )
        draw_centered_design_footer(
            self._im,
            self._sy,
            footer_designed_generated(),
            color=self.text_color,
            margin_x=60,
            start_font_size=14,
            min_font_size=10,
            bottom_gap=28,
        )
        if self.grouped:
            self.whiledraw_with_source(self.sd_list, start_y=235)
            self.whiledraw_with_source(self.dx_list, start_y=self._B15_START_Y)
        else:
            self.whiledraw_with_source(self.merged_list, start_y=235)
        return self._im


def dx_star_count(dx_score: int, dx_max: int) -> int:
    """由 dx 分子/分母计算星星数；须用浮点百分比，不可 round 到整数后再判星。"""
    if not dx_max or dx_max <= 0:
        return 0
    pct = min(100.0, max(0.0, float(dx_score) / float(dx_max) * 100.0))
    return dxScore(pct)


def dxScore(dx: float) -> int:
    """
    获取 DX 评分星星数量（0～5），用于选择 UI_GAM_Gauge_DXScoreIcon_0x.png。

    阈值与游戏一致：≥97% 五星、≥95% 四星、≥93% 三星、≥90% 二星、≥85% 一星。
    须用浮点百分比比较，不可先 round 成整数（否则 96.8% 会被算成 97% 而多一星）。
    """
    if dx is None:
        return 0
    p = float(dx)
    if p < 85:
        return 0
    if p < 90:
        return 1
    if p < 93:
        return 2
    if p < 95:
        return 3
    if p < 97:
        return 4
    return 5


def getCharWidth(o: int) -> int:
    widths = [
        (126, 1), (159, 0), (687, 1), (710, 0), (711, 1), (727, 0), (733, 1), (879, 0), (1154, 1), (1161, 0),
        (4347, 1), (4447, 2), (7467, 1), (7521, 0), (8369, 1), (8426, 0), (9000, 1), (9002, 2), (11021, 1),
        (12350, 2), (12351, 1), (12438, 2), (12442, 0), (19893, 2), (19967, 1), (55203, 2), (63743, 1),
        (64106, 2), (65039, 1), (65059, 0), (65131, 2), (65279, 1), (65376, 2), (65500, 1), (65510, 2),
        (120831, 1), (262141, 2), (1114109, 1),
    ]
    if o == 0xe or o == 0xf:
        return 0
    for num, wid in widths:
        if o <= num:
            return wid
    return 1


def coloumWidth(s: str) -> int:
    res = 0
    for ch in s:
        res += getCharWidth(ord(ch))
    return res


def changeColumnWidth(s: str, len: int) -> str:
    res = 0
    sList = []
    for ch in s:
        res += getCharWidth(ord(ch))
        if res <= len:
            sList.append(ch)
    return ''.join(sList)


def truncate_nickname(name: str, max_width: int = 8) -> str:
    """昵称过长时用...省略，按字符宽度计算。"""
    if not name:
        return '未知'
    if coloumWidth(name) <= max_width:
        return name
    return changeColumnWidth(name, max_width - 2) + '...'


@overload
def computeRa(ds: float, achievement: float) -> int:
    """
    单曲 Rating 算法（与游戏机制一致）：
    - 公式：rating = round( 定数 × (达成率/100) × 系数 )
    - 达成率按档位取系数（50→D 7.0, 60→C 8.0, …, 100.5→SSSp 22.4）
    - 游戏机制：达成率 ≥100.5 时 rating 不再变化，统一按 100.5 计算（避免同一定数同谱面因 100.5/100.6 等浮点误差导致 rating 不同）
    """
@overload
def computeRa(ds: float, achievement: float, *, onlyrate: bool = False) -> str:
    """
    计算评价
    
    Params:
        `ds`: 定数
        `achievement`: 成绩
        `onlyrate`: 是否只返回评价
    Returns:
        返回评价
    """
@overload
def computeRa(ds: float, achievement: float, *, israte: bool = False) -> Tuple[int, str]:
    """
    计算底分和评价
    
    Params:
        `ds`: 定数
        `achievement`: 成绩
        `israte`: 是否返回所有数据
    Returns:
        (底分, 评价)
    """
def computeRa(
    ds: float, 
    achievement: float, 
    *, 
    onlyrate: bool = False, 
    israte: bool = False
) -> Union[int, Tuple[int, str]]:
    if achievement < 50:
        baseRa = 7.0
        rate = 'D'
    elif achievement < 60:
        baseRa = 8.0
        rate = 'C'
    elif achievement < 70:
        baseRa = 9.6
        rate = 'B'
    elif achievement < 75:
        baseRa = 11.2
        rate = 'BB'
    elif achievement < 80:
        baseRa = 12.0
        rate = 'BBB'
    elif achievement < 90:
        baseRa = 13.6
        rate = 'A'
    elif achievement < 94:
        baseRa = 15.2
        rate = 'AA'
    elif achievement < 97:
        baseRa = 16.8
        rate = 'AAA'
    elif achievement < 98:
        baseRa = 20.0
        rate = 'S'
    elif achievement < 99:
        baseRa = 20.3
        rate = 'Sp'
    elif achievement < 99.5:
        baseRa = 20.8
        rate = 'SS'
    elif achievement < 100:
        baseRa = 21.1
        rate = 'SSp'
    elif achievement < 100.5:
        baseRa = 21.6
        rate = 'SSS'
    else:
        baseRa = 22.4
        rate = 'SSSp'

    # 达成率 ≥100.5 时 rating 封顶，统一用 1.005 避免浮点误差导致同谱面不同 rating
    if achievement >= 100.5:
        ratio = 1.005
    else:
        ratio = achievement / 100
    raw_ra = ds * ratio * baseRa
    ra = int(raw_ra)
    if israte:
        data = (ra, rate)
    elif onlyrate:
        data = rate
    else:
        data = ra

    return data


# 拟合 b50：直接按表上 ra 算，定数 1.0～15.0、达成率档位 → rating，不做公式兜底
# 档位阈值必须从高到低排列，以便 _get_fit_b50_table_column 按“第一个 achievement >= thresh”得到正确评级
_FIT_B50_TABLE_THRESHOLDS: List[Tuple[float, float, str]] = [
    (100.5, 22.4, 'SSSp'),
    (100.0, 21.6, 'SSS'),
    (99.999, 21.4, 'SSS'),
    (99.5, 21.1, 'SSp'),
    (99.0, 20.8, 'SSp'),
    (98.0, 20.3, 'SS'),
    (97.0, 20.0, 'SS'),
    (96.0, 20.2, 'Sp'),
    (95.0, 20.0, 'Sp'),
    (94.0, 15.2, 'AA'),
    (91.0, 20.96, 'S'),
    (90.0, 13.6, 'A'),
    (80.0, 12.0, 'BBB'),
    (75.0, 11.2, 'BB'),
    (70.0, 9.6, 'B'),
    (60.0, 8.0, 'C'),
    (50.0, 7.0, 'D'),
]

_FIT_B50_LEVEL_MIN = 1.0
_FIT_B50_LEVEL_MAX = 15.0


def _build_fit_b50_rating_table() -> dict:
    """按表生成 (level, 档位索引) -> rating；level 1.0～15.0 步进 0.1，每格用 定数×达成率×系数 取整。"""
    table = {}
    levels = [round(i * 0.1, 1) for i in range(10, 151)]
    for lev in levels:
        for idx, (thresh, coef, _) in enumerate(_FIT_B50_TABLE_THRESHOLDS):
            ratio = 1.005 if thresh >= 100.5 else thresh / 100
            table[(lev, idx)] = round(lev * ratio * coef)
    return table


_FIT_B50_RATING_TABLE: dict = _build_fit_b50_rating_table()


def _get_fit_b50_table_column(achievement: float) -> Tuple[int, str]:
    """按达成率取表列索引与 RANK：从高到低第一个 achievement >= 阈值的列；<50 用最后一列。"""
    for idx, (thresh, _, rate) in enumerate(_FIT_B50_TABLE_THRESHOLDS):
        if achievement >= thresh:
            return (idx, rate)
    return (len(_FIT_B50_TABLE_THRESHOLDS) - 1, 'D')


def computeRa_fit_b50(ds: float, achievement: float) -> Tuple[int, str]:
    """
    拟合 b50 单曲 rating：只查表，直接按表上的 ra。定数 1.0～15.0，超出按边界；达成率对档位取列。
    """
    level_key = round(ds, 1)
    level_key = max(_FIT_B50_LEVEL_MIN, min(_FIT_B50_LEVEL_MAX, level_key))
    col_idx, rate = _get_fit_b50_table_column(achievement)
    ra = _FIT_B50_RATING_TABLE[(level_key, col_idx)]
    return (ra, rate)


# FC 状态：fc / fcp（不含 ap / app）；AP 状态：ap / app
def _fc_records(records: List[PlayInfoDev]) -> List[PlayInfoDev]:
    return [r for r in records if r.fc and r.fc in ('fc', 'fcp')]


def _ap_records(records: List[PlayInfoDev]) -> List[PlayInfoDev]:
    return [r for r in records if r.fc and r.fc in ('ap', 'app')]


# 寸 b50：每个评级下限为 x，筛选达成率在 (x-0.1, x] 内的成绩
def _sun_b50_records(records: List[PlayInfoDev]) -> List[PlayInfoDev]:
    """筛选出达成率落在某评级下限 band (x-0.1, x] 内的成绩（x 取 achievementList）。"""
    bands = [(x - 0.1, x) for x in achievementList]
    out = []
    for r in records:
        a = r.achievements
        for lo, hi in bands:
            if lo < a <= hi:
                out.append(r)
                break
    return out


# 锁血 b50：按每个门槛 [x, x+步长) 筛选（整数+0.1、一位小数+0.01），与寸止 (x-0.1,x] 相反
def _lock_b50_records(records: List[PlayInfoDev]) -> List[PlayInfoDev]:
    """保留达成率落在任一 [门槛, 门槛+步长) 内的成绩。"""
    bands = [(x, x + (0.01 if x != math.floor(x) else 0.1)) for x in achievementList]
    out = []
    for r in records:
        a = r.achievements
        for lo, hi in bands:
            if lo <= a < hi:
                out.append(r)
                break
    return out


# 越级 b50：筛选达成率在 [0, 门槛) 的成绩，按 ra 倒序（需开发者 Token 获取全量成绩）
def _yueji_b50_records(records: List[PlayInfoDev], threshold: float) -> List[PlayInfoDev]:
    """筛选达成率在 [0, 门槛) 内的成绩（即 achievements < threshold）。"""
    return [r for r in records if r.achievements < threshold]


_cached_b15_versions: Optional[set[str]] = None
_cached_lib_has_new_flag: Optional[bool] = None


def invalidate_b15_version_cache() -> None:
    """曲库更新后清除 B15 相关缓存，使版本/新曲判定重新推断。"""
    global _cached_b15_versions, _cached_lib_has_new_flag
    _cached_b15_versions = None
    _cached_lib_has_new_flag = None


def _get_b15_version_set() -> set[str]:
    """曲库实际生效的 B15 版本名（按 plate_to_dx_version 与曲库推断）。"""
    global _cached_b15_versions
    if _cached_b15_versions is None:
        lib = {
            m.basic_info.version
            for m in mai.total_list
            if getattr(m, 'basic_info', None) and m.basic_info.version
        }
        gen = resolve_b15_generation(lib)
        _cached_b15_versions = set(get_b15_version_names_at_generation(gen))
    return _cached_b15_versions


def _library_has_new_flag() -> bool:
    """曲库是否带有可用的 is_new 新曲标记（水鱼源有；个别精简源可能全为 False）。"""
    global _cached_lib_has_new_flag
    if _cached_lib_has_new_flag is None:
        _cached_lib_has_new_flag = any(
            getattr(getattr(m, 'basic_info', None), 'is_new', False)
            for m in mai.total_list
        )
    return _cached_lib_has_new_flag


def _music_is_new(music) -> bool:
    """
    曲目是否为「新版本」(B15) 曲目（统一判定入口）。

    优先用曲库 is_new（新曲标记），与水鱼查分器 charts.dx(B15) 完全一致——
    避免变体 b50 / 上分把旧曲(B35)误判进 B15 区；
    仅当曲库无任何 is_new 标记时，回退到「当前版本名集合」判定。
    """
    if not (music and getattr(music, 'basic_info', None)):
        return False
    if _library_has_new_flag():
        return bool(getattr(music.basic_info, 'is_new', False))
    return getattr(music.basic_info, 'version', None) in _get_b15_version_set()


def _song_is_new(song_id: Union[str, int]) -> bool:
    try:
        return _music_is_new(mai.total_list.by_id(str(song_id)))
    except Exception:
        return False


def _is_latest_version(r: PlayInfoDev) -> bool:
    """
    成绩所属曲目是否为新版本(B15 区)曲目；否则归入旧版本(B35 区)。
    与谱面类型 SD/DX 无关；查分器 B15=dx、B35=sd。
    """
    return _song_is_new(r.song_id)


def _is_latest_version_chart(chart: ChartInfo) -> bool:
    return _song_is_new(chart.song_id)


def _chart_keys(charts: List[ChartInfo]) -> set[tuple[int, int]]:
    return {(c.song_id, c.level_index) for c in charts}


def regroup_b50_userinfo(userinfo: UserInfo) -> UserInfo:
    """
    按当前 B15 版本规则重分 charts.sd(B35) / charts.dx(B15)。
    查分器 API 可能分区滞后；本地合并后按国服当前 B15 版本重新分组。
    """
    if not userinfo or not userinfo.charts:
        return userinfo

    all_charts = list(userinfo.charts.sd or []) + list(userinfo.charts.dx or [])
    if not all_charts:
        return userinfo

    b15_list = sorted(
        [c for c in all_charts if _is_latest_version_chart(c)],
        key=lambda x: -x.ra,
    )[:15]
    b35_list = sorted(
        [c for c in all_charts if not _is_latest_version_chart(c)],
        key=lambda x: -x.ra,
    )[:35]

    old_sd = userinfo.charts.sd or []
    old_dx = userinfo.charts.dx or []
    if (
        _chart_keys(old_sd) == _chart_keys(b35_list)
        and _chart_keys(old_dx) == _chart_keys(b15_list)
    ):
        return userinfo

    total_ra = int(sum(c.ra for c in b35_list) + sum(c.ra for c in b15_list))
    return UserInfo(
        nickname=userinfo.nickname or userinfo.username or '未知',
        plate=userinfo.plate,
        additional_rating=userinfo.additional_rating if userinfo.additional_rating is not None else 0,
        rating=total_ra,
        username=userinfo.username,
        charts=Data(sd=b35_list, dx=b15_list),
    )


def _get_fit_diff(r: PlayInfoDev) -> Optional[float]:
    """从曲目表获取该谱面的拟合定数；无则返回 None。"""
    try:
        music = mai.total_list.by_id(str(r.song_id))
        if not music or not getattr(music, 'stats', None):
            return None
        if r.level_index >= len(music.stats) or not music.stats[r.level_index]:
            return None
        fit = getattr(music.stats[r.level_index], 'fit_diff', None)
        return float(fit) if fit is not None else None
    except Exception:
        return None


def _apply_fit_ra(records: List[PlayInfoDev]) -> List[PlayInfoDev]:
    """
    用拟合定数 + 用户达成率按 RANK 系数表重算每条成绩的 ra（和 rate），仅保留有拟合定数的记录。
    展示时定数统一为拟合定数（ds 改为 fit_diff）。
    """
    out = []
    for r in records:
        fit_diff = _get_fit_diff(r)
        if fit_diff is None:
            continue
        fit_ra, fit_rate = computeRa_fit_b50(fit_diff, r.achievements)
        fit_diff_rounded = round(fit_diff, 1)  # 拟合定数四舍五入保留一位小数
        out.append(r.model_copy(update={'ds': fit_diff_rounded, 'ra': fit_ra, 'rate': fit_rate}))
    return out


async def _fit_b50_common(
    qqid: Optional[int],
    username: Optional[str],
    by_group: bool,
) -> Union[MessageSegment, str]:
    """
    拟合 B50：用拟合定数 + 用户成绩算 ra，再按常规 B35/B15 或直接取前 50。
    by_group=True：按 B35/B15 分组各取前 35/15（拟合 ra 倒序）
    by_group=False：无视分组取前 50（拟合 ra 倒序），连续显示
    """
    try:
        if username:
            qqid = None
        # 拟合 b50 依赖水鱼独有的 chart_stats(fit_diff)，强制走水鱼
        from .maimaidx_datasource import get_user_b50
        userinfo = await get_user_b50(qqid=qqid, username=username, force_source='divingfish')
        dev = await maiApi.query_user_get_dev(qqid=qqid, username=username)
        records = list(dev.records or [])
        records = filter_utage_records(records)
        with_fit = _apply_fit_ra(records)
        if not with_fit:
            return '没有可用的拟合难度数据（需开发者 Token 获取全量成绩，且曲目 chart_stats 含 fit_diff）'

        if by_group:
            b15_list = sorted([r for r in with_fit if _is_latest_version(r)], key=lambda x: -x.ra)[:15]
            b35_list = sorted([r for r in with_fit if not _is_latest_version(r)], key=lambda x: -x.ra)[:35]
        else:
            top50 = sorted(with_fit, key=lambda x: -x.ra)[:50]
            b35_list = top50[:35]
            b15_list = top50[35:50]

        total_ra = int(sum(r.ra for r in b35_list) + sum(r.ra for r in b15_list))
        fit_userinfo = UserInfo(
            nickname=userinfo.nickname or userinfo.username or '未知',
            plate=userinfo.plate,
            additional_rating=userinfo.additional_rating if userinfo.additional_rating is not None else 0,
            rating=total_ra,
            username=userinfo.username,
            charts=Data(sd=b35_list, dx=b15_list),
        )

        # 尝试加载 PC 数据
        play_counts: dict[tuple[int, int], int] = {}
        if qqid:
            try:
                pc_records = pc_db.get_user_play_counts(qqid)
                for r in pc_records:
                    play_counts[(r.song_id, r.level_index)] = r.play_count
            except Exception:
                pass

        _prepare_b50_warnings(fit_userinfo, qqid, username)
        draw_best = DrawBest(fit_userinfo, qqid, compact_layout=not by_group, play_counts=play_counts or None)
        msg = MessageSegment.image(image_to_base64(await draw_best.draw()))
    except (UserNotFoundError, UserNotExistsError, UserDisabledQueryError) as e:
        msg = str(e)
    except Exception as e:
        log.error(traceback.format_exc())
        msg = format_command_error(e)
    return msg


async def _fc_ap_b50_common(
    qqid: Optional[int],
    username: Optional[str],
    filter_fn,
    by_group: bool,
) -> Union[MessageSegment, str]:
    """
    查分器 B35(sd)=35 首槽位，B15(dx)=15 首槽位，与谱面类型 SD/DX 无关。
    by_group=True: 按曲目版本划分 B35/B15 槽位各取前 35/15，ra 倒序
    by_group=False: 无视划分取前 50，再按 B35/B15 槽位显示
    使用当前定数重新计算rating。
    """
    try:
        if username:
            qqid = None
        from .maimaidx_datasource import get_user_records
        userinfo, records = await get_user_records(qqid=qqid, username=username)
        records = filter_utage_records(records)
        filtered = filter_fn(records)
        if not filtered:
            return '没有符合条件的成绩数据（需开发者 Token 获取全量成绩）'

        # 重新计算每条成绩的rating（使用当前定数）
        recalculated_records = []
        for r in filtered:
            # 获取当前歌曲的定数
            try:
                music = mai.total_list.by_id(str(r.song_id))
                if music and r.level_index < len(music.ds):
                    current_ds = round(float(music.ds[r.level_index]), 1)
                else:
                    current_ds = r.ds
            except Exception:
                current_ds = r.ds
            
            # 重新计算rating
            new_ra, new_rate = computeRa(current_ds, r.achievements, israte=True)
            
            # 创建新的记录对象
            recalculated = r.model_copy(update={
                'ds': current_ds,
                'ra': new_ra,
                'rate': new_rate,
            }) if hasattr(r, 'model_copy') else r
            
            recalculated_records.append(recalculated)

        if by_group:
            # 按曲目版本划分：归入 B15 区(查分器 dx) / B35 区(查分器 sd)，与谱面类型 SD/DX 无关
            b15_list = sorted([r for r in recalculated_records if _is_latest_version(r)], key=lambda x: -x.ra)[:15]
            b35_list = sorted([r for r in recalculated_records if not _is_latest_version(r)], key=lambda x: -x.ra)[:35]
        else:
            top50 = sorted(recalculated_records, key=lambda x: -x.ra)[:50]
            b35_list = top50[:35]
            b15_list = top50[35:50]

        # 重算 rating = B35 ra 和 + B15 ra 和，与常规 b50 一致显示在红框位置
        total_ra = int(sum(r.ra for r in b35_list) + sum(r.ra for r in b15_list))
        fc_userinfo = UserInfo(
            nickname=userinfo.nickname or userinfo.username or '未知',
            plate=userinfo.plate,
            additional_rating=userinfo.additional_rating if userinfo.additional_rating is not None else 0,
            rating=total_ra,
            username=userinfo.username,
            charts=Data(sd=b35_list, dx=b15_list),
        )
        # by_group 时与常规 b50 一致（B35/B15 间留空）；否则连续列出无空白

        # 尝试加载 PC 数据
        play_counts: dict[tuple[int, int], int] = {}
        if qqid:
            try:
                pc_records = pc_db.get_user_play_counts(qqid)
                for r in pc_records:
                    play_counts[(r.song_id, r.level_index)] = r.play_count
            except Exception:
                pass

        _prepare_b50_warnings(fc_userinfo, qqid, username)
        draw_best = DrawBest(fc_userinfo, qqid, compact_layout=not by_group, play_counts=play_counts or None)
        msg = MessageSegment.image(image_to_base64(await draw_best.draw()))
    except (UserNotFoundError, UserNotExistsError, UserDisabledQueryError) as e:
        msg = str(e)
    except Exception as e:
        log.error(traceback.format_exc())
        msg = format_command_error(e)
    return msg


async def generate_fc_b50(qqid: Optional[int] = None, username: Optional[str] = None) -> Union[MessageSegment, str]:
    """FC 状态 B50：按 B35/B15 分组，各取前 35 / 前 15，ra 倒序，带与常规 b50 一致的用户信息。"""
    return await _fc_ap_b50_common(qqid, username, _fc_records, by_group=True)


async def generate_fc_all_b50(qqid: Optional[int] = None, username: Optional[str] = None) -> Union[MessageSegment, str]:
    """FC 状态 B50：无视分组取前 50 首（ra 倒序），前 35 首显示在 B35 区，后 15 首显示在 B15 区。"""
    return await _fc_ap_b50_common(qqid, username, _fc_records, by_group=False)


async def generate_ap_b50(qqid: Optional[int] = None, username: Optional[str] = None) -> Union[MessageSegment, str]:
    """AP 状态 B50：按 B35/B15 分组，各取前 35 / 前 15，ra 倒序。"""
    return await _fc_ap_b50_common(qqid, username, _ap_records, by_group=True)


async def generate_ap_all_b50(qqid: Optional[int] = None, username: Optional[str] = None) -> Union[MessageSegment, str]:
    """AP 状态 B50：无视分组取前 50 首（ra 倒序），前 35 + 后 15 显示。"""
    return await _fc_ap_b50_common(qqid, username, _ap_records, by_group=False)


async def _sun_b50_common(
    qqid: Optional[int],
    username: Optional[str],
    by_group: bool,
    threshold: Optional[float] = None,
) -> Union[MessageSegment, str]:
    """
    寸 B50：筛选达成率在 (x-0.1, x]（x 为各评级下限）的成绩，按单曲 ra 排序。
    threshold 为 None 时取所有档位区间；指定时仅取 (threshold-0.1, threshold] 区间。
    by_group=True（寸b50）：按 B35/B15 分组各取前 35/15，ra 倒序。
    by_group=False（寸ab50）：无视分组取前 50 首 ra 倒序，连续列出。
    使用当前定数重新计算rating。
    """
    try:
        if username:
            qqid = None
        from .maimaidx_datasource import get_user_records
        userinfo, records = await get_user_records(qqid=qqid, username=username)
        records = filter_utage_records(records)
        if threshold is None:
            filtered = _sun_b50_records(records)
        else:
            filtered = _sun_b50_records_for_threshold(records, threshold)
        if not filtered:
            if threshold is None:
                return '没有符合条件的成绩数据（需开发者 Token 获取全量成绩；寸 b50 仅包含达成率在某评级下限 0.1 分内的成绩）'
            return f'没有达成率在 ({threshold - 0.1}, {threshold}] 内的成绩（可尝试其他档位如 97、98、99）。'

        # 重新计算每条成绩的rating（使用当前定数）
        recalculated_records = []
        for r in filtered:
            try:
                music = mai.total_list.by_id(str(r.song_id))
                if music and r.level_index < len(music.ds):
                    current_ds = round(float(music.ds[r.level_index]), 1)
                else:
                    current_ds = r.ds
            except Exception:
                current_ds = r.ds
            
            new_ra, new_rate = computeRa(current_ds, r.achievements, israte=True)
            
            recalculated = r.model_copy(update={
                'ds': current_ds,
                'ra': new_ra,
                'rate': new_rate,
            }) if hasattr(r, 'model_copy') else r
            
            recalculated_records.append(recalculated)

        if by_group:
            b15_list = sorted([r for r in recalculated_records if _is_latest_version(r)], key=lambda x: -x.ra)[:15]
            b35_list = sorted([r for r in recalculated_records if not _is_latest_version(r)], key=lambda x: -x.ra)[:35]
        else:
            top50 = sorted(recalculated_records, key=lambda x: -x.ra)[:50]
            b35_list = top50[:35]
            b15_list = top50[35:50]

        total_ra = int(sum(r.ra for r in b35_list) + sum(r.ra for r in b15_list))
        sun_userinfo = UserInfo(
            nickname=userinfo.nickname or userinfo.username or '未知',
            plate=userinfo.plate,
            additional_rating=userinfo.additional_rating if userinfo.additional_rating is not None else 0,
            rating=total_ra,
            username=userinfo.username,
            charts=Data(sd=b35_list, dx=b15_list),
        )
        # 尝试加载 PC 数据
        play_counts: dict[tuple[int, int], int] = {}
        if qqid:
            try:
                pc_records = pc_db.get_user_play_counts(qqid)
                for r in pc_records:
                    play_counts[(r.song_id, r.level_index)] = r.play_count
            except Exception:
                pass
        _prepare_b50_warnings(sun_userinfo, qqid, username)
        draw_best = DrawBest(sun_userinfo, qqid, compact_layout=not by_group, play_counts=play_counts or None)
        msg = MessageSegment.image(image_to_base64(await draw_best.draw()))
    except (UserNotFoundError, UserNotExistsError, UserDisabledQueryError) as e:
        msg = str(e)
    except Exception as e:
        log.error(traceback.format_exc())
        msg = format_command_error(e)
    return msg


async def _lock_b50_common(
    qqid: Optional[int],
    username: Optional[str],
    by_group: bool,
) -> Union[MessageSegment, str]:
    """
    锁血 B50：按每个门槛 [x, x+步长) 筛选（整数门槛 +0.1，一位小数 +0.01；与寸止相反），再按 ra 倒序。
    by_group=True：按 B35/B15 分组各取前 35/15。
    by_group=False：无视分组取前 50，前 35 在 B35 区、后 15 在 B15 区。
    使用当前定数重新计算rating。
    """
    try:
        if username:
            qqid = None
        from .maimaidx_datasource import get_user_records
        userinfo, records = await get_user_records(qqid=qqid, username=username)
        records = filter_utage_records(records)
        filtered = _lock_b50_records(records)
        if not filtered:
            return '没有达成率在任一 [门槛, 门槛+步长) 内的成绩。（需开发者 Token 获取全量成绩）'

        # 重新计算每条成绩的rating（使用当前定数）
        recalculated_records = []
        for r in filtered:
            try:
                music = mai.total_list.by_id(str(r.song_id))
                if music and r.level_index < len(music.ds):
                    current_ds = round(float(music.ds[r.level_index]), 1)
                else:
                    current_ds = r.ds
            except Exception:
                current_ds = r.ds
            
            new_ra, new_rate = computeRa(current_ds, r.achievements, israte=True)
            
            recalculated = r.model_copy(update={
                'ds': current_ds,
                'ra': new_ra,
                'rate': new_rate,
            }) if hasattr(r, 'model_copy') else r
            
            recalculated_records.append(recalculated)

        if by_group:
            b15_list = sorted([r for r in recalculated_records if _is_latest_version(r)], key=lambda x: -x.ra)[:15]
            b35_list = sorted([r for r in recalculated_records if not _is_latest_version(r)], key=lambda x: -x.ra)[:35]
        else:
            top50 = sorted(recalculated_records, key=lambda x: -x.ra)[:50]
            b35_list = top50[:35]
            b15_list = top50[35:50]

        total_ra = int(sum(r.ra for r in b35_list) + sum(r.ra for r in b15_list))
        lock_userinfo = UserInfo(
            nickname=userinfo.nickname or userinfo.username or '未知',
            plate=userinfo.plate,
            additional_rating=userinfo.additional_rating if userinfo.additional_rating is not None else 0,
            rating=total_ra,
            username=userinfo.username,
            charts=Data(sd=b35_list, dx=b15_list),
        )
        # 尝试加载 PC 数据
        play_counts: dict[tuple[int, int], int] = {}
        if qqid:
            try:
                pc_records = pc_db.get_user_play_counts(qqid)
                for r in pc_records:
                    play_counts[(r.song_id, r.level_index)] = r.play_count
            except Exception:
                pass
        _prepare_b50_warnings(lock_userinfo, qqid, username)
        draw_best = DrawBest(lock_userinfo, qqid, compact_layout=not by_group, play_counts=play_counts or None)
        msg = MessageSegment.image(image_to_base64(await draw_best.draw()))
    except (UserNotFoundError, UserNotExistsError, UserDisabledQueryError) as e:
        msg = str(e)
    except Exception as e:
        log.error(traceback.format_exc())
        msg = format_command_error(e)
    return msg


async def generate_pc50(
    qqid: Optional[int] = None,
    username: Optional[str] = None,
) -> Union[MessageSegment, str]:
    try:
        if username:
            qqid = None

        play_counts: dict[tuple[int, int], int] = {}
        try:
            pc_records = pc_db.get_user_play_counts(qqid) if qqid else []
        except Exception:
            pc_records = []
        for r in pc_records:
            play_counts[(r.song_id, r.level_index)] = r.play_count

        if not pc_records:
            return '你还没有PC数据，请先使用「更新pc数」命令同步数据。'

        last_update = max(r.updated_at for r in pc_records)
        last_update_str = datetime.fromtimestamp(last_update).strftime('%Y-%m-%d %H:%M:%S')

        # 统一走 datasource；直接调用水鱼会让 AWMCNET 用户在 pc50 中变成
        # “查无此人”，即使本地 PC 记录本身已经同步成功。
        from .maimaidx_datasource import get_user_b50, get_user_source
        source = get_user_source(qqid) if qqid else 'divingfish'
        userinfo = await get_user_b50(qqid=qqid, username=username)
        sdBest = list(userinfo.charts.sd or [])
        dxBest = list(userinfo.charts.dx or [])

        sdBest.sort(key=lambda x: play_counts.get((x.song_id, x.level_index), 0), reverse=True)
        dxBest.sort(key=lambda x: play_counts.get((x.song_id, x.level_index), 0), reverse=True)

        total_ra = int(sum(r.ra for r in sdBest) + sum(r.ra for r in dxBest))

        pc_userinfo = UserInfo(
            nickname=userinfo.nickname or userinfo.username or '未知',
            plate=userinfo.plate,
            additional_rating=userinfo.additional_rating if userinfo.additional_rating is not None else 0,
            rating=total_ra,
            username=userinfo.username,
            charts=Data(sd=sdBest, dx=dxBest),
        )

        _prepare_b50_warnings(pc_userinfo, qqid, username)
        source_names = {
            'awmcnet': 'AWMCNET',
            'divingfish': '水鱼查分器',
            'lxns': '落雪查分器',
        }
        draw_best = DrawBest(
            pc_userinfo,
            qqid,
            play_counts=play_counts or None,
            source_label=source_names.get(source, source),
        )
        msg = MessageSegment.image(image_to_base64(await draw_best.draw())) + MessageSegment.text(
            f'\n数据源: {source_names.get(source, source)}\n上次更新: {last_update_str}'
        )
    except (UserNotFoundError, UserNotExistsError, UserDisabledQueryError) as e:
        msg = str(e)
    except Exception as e:
        log.error(traceback.format_exc())
        msg = format_command_error(e)
    return msg


async def generate_pc_rank50(
    qqid: Optional[int] = None,
) -> Union[MessageSegment, str]:
    """从全量机台 PC 数据取游玩次数最多的 50 首谱面绘图（不限于 rating B50）。"""
    try:
        if not qqid:
            return '请指定要查询的 QQ 号。'

        try:
            pc_records = pc_db.get_user_play_counts(qqid)
        except Exception:
            pc_records = []

        if not pc_records:
            return '你还没有PC数据，请先使用「更新pc数」命令同步数据。'

        last_update = max(r.updated_at for r in pc_records)
        last_update_str = datetime.fromtimestamp(last_update).strftime('%Y-%m-%d %H:%M:%S')

        top50_records = pc_records[:50]
        play_counts = {(r.song_id, r.level_index): r.play_count for r in pc_records}
        charts = [_pc_record_to_chartinfo(r) for r in top50_records]
        b35_list = charts[:35]
        b15_list = charts[35:50]
        total_pc_top50 = sum(r.play_count for r in top50_records)

        nickname = f'QQ{qqid}'
        plate = None
        add_rating = 0
        rating = 0
        try:
            userinfo = await maiApi.query_user_b50(qqid=qqid)
            nickname = userinfo.nickname or userinfo.username or nickname
            plate = userinfo.plate
            add_rating = userinfo.additional_rating if userinfo.additional_rating is not None else 0
            rating = userinfo.rating if userinfo.rating is not None else 0
        except Exception:
            pass

        pc_userinfo = UserInfo(
            nickname=nickname,
            plate=plate,
            additional_rating=add_rating,
            rating=rating,
            username=None,
            charts=Data(sd=b35_list, dx=b15_list),
        )

        draw_best = DrawBest(
            pc_userinfo,
            qqid,
            compact_layout=True,
            play_counts=play_counts,
            compact_subtitle=f'游玩最多50首共{total_pc_top50}次',
        )
        msg = MessageSegment.image(image_to_base64(await draw_best.draw())) + MessageSegment.text(
            f'\n上次更新: {last_update_str}'
        )
    except Exception as e:
        log.error(traceback.format_exc())
        msg = format_command_error(e)
    return msg


async def generate_pca50(
    qqid: Optional[int] = None,
    username: Optional[str] = None,
) -> Union[MessageSegment, str]:
    try:
        if username:
            qqid = None

        play_counts: dict[tuple[int, int], int] = {}
        try:
            pc_records = pc_db.get_user_play_counts(qqid) if qqid else []
        except Exception:
            pc_records = []
        for r in pc_records:
            play_counts[(r.song_id, r.level_index)] = r.play_count

        if not pc_records:
            return '你还没有PC数据，请先使用「更新pc数」命令同步数据。'

        last_update = max(r.updated_at for r in pc_records)
        last_update_str = datetime.fromtimestamp(last_update).strftime('%Y-%m-%d %H:%M:%S')

        userinfo = await maiApi.query_user_b50(qqid=qqid, username=username)
        sdBest = list(userinfo.charts.sd or [])
        dxBest = list(userinfo.charts.dx or [])
        allCharts = sdBest + dxBest

        allCharts.sort(key=lambda x: play_counts.get((x.song_id, x.level_index), 0), reverse=True)

        b35_list = allCharts[:35]
        b15_list = allCharts[35:50]

        total_ra = int(sum(r.ra for r in b35_list) + sum(r.ra for r in b15_list))

        pc_userinfo = UserInfo(
            nickname=userinfo.nickname or userinfo.username or '未知',
            plate=userinfo.plate,
            additional_rating=userinfo.additional_rating if userinfo.additional_rating is not None else 0,
            rating=total_ra,
            username=userinfo.username,
            charts=Data(sd=b35_list, dx=b15_list),
        )

        _prepare_b50_warnings(pc_userinfo, qqid, username)
        draw_best = DrawBest(pc_userinfo, qqid, compact_layout=True, play_counts=play_counts or None)
        msg = MessageSegment.image(image_to_base64(await draw_best.draw())) + MessageSegment.text(f'\n上次更新: {last_update_str}')
    except (UserNotFoundError, UserNotExistsError, UserDisabledQueryError) as e:
        msg = str(e)
    except Exception as e:
        log.error(traceback.format_exc())
        msg = format_command_error(e)
    return msg


async def generate_all(qqid: Optional[int] = None, username: Optional[str] = None) -> Union[MessageSegment, str]:
    """
    生成 ab50（常规无视分组版）：无视 B35/B15 分组，直接取 rating 最高的 50 首。
    前 35 首显示在 B35 区，后 15 首显示在 B15 区，连续列出无空白。
    使用开发者Token获取全量成绩并重新计算rating。
    
    Params:
        `qqid`: QQ号
        `username`: 用户名
    Returns:
        `Union[MessageSegment, str]`
    """
    try:
        if username:
            qqid = None
        from .maimaidx_datasource import get_user_records
        userinfo, records = await get_user_records(qqid=qqid, username=username)
        records = filter_utage_records(records)

        if not records:
            return '没有成绩数据（需开发者 Token 获取全量成绩）'
        
        # 重新计算每条成绩的rating（使用当前定数）
        recalculated_records = []
        for r in records:
            # 获取当前歌曲的定数
            try:
                music = mai.total_list.by_id(str(r.song_id))
                if music and r.level_index < len(music.ds):
                    current_ds = round(float(music.ds[r.level_index]), 1)
                else:
                    current_ds = r.ds
            except Exception:
                current_ds = r.ds
            
            # 重新计算rating
            new_ra, new_rate = computeRa(current_ds, r.achievements, israte=True)
            
            # 创建新的记录对象
            recalculated = r.model_copy(update={
                'ds': current_ds,
                'ra': new_ra,
                'rate': new_rate,
            }) if hasattr(r, 'model_copy') else r
            
            recalculated_records.append(recalculated)
        
        # 按重新计算的 rating 倒序排序，取前 50
        top50 = sorted(recalculated_records, key=lambda x: -x.ra)[:50]
        
        # 分配到 B35 和 B15 区
        b35_list = top50[:35]
        b15_list = top50[35:50]
        
        # 重新计算总 rating
        total_ra = int(sum(r.ra for r in b35_list) + sum(r.ra for r in b15_list))
        
        # 创建新的 UserInfo
        all_userinfo = UserInfo(
            nickname=userinfo.nickname or userinfo.username or '未知',
            plate=userinfo.plate,
            additional_rating=userinfo.additional_rating if userinfo.additional_rating is not None else 0,
            rating=total_ra,
            username=userinfo.username,
            charts=Data(sd=b35_list, dx=b15_list),
        )
        
        # 尝试加载 PC 数据
        play_counts: dict[tuple[int, int], int] = {}
        if qqid:
            try:
                pc_records = pc_db.get_user_play_counts(qqid)
                for r in pc_records:
                    play_counts[(r.song_id, r.level_index)] = r.play_count
            except Exception:
                pass
        # 使用紧凑布局（无视分组）
        _prepare_b50_warnings(all_userinfo, qqid, username)
        draw_best = DrawBest(all_userinfo, qqid, compact_layout=True, play_counts=play_counts or None)
        msg = MessageSegment.image(image_to_base64(await draw_best.draw()))
        
    except (UserNotFoundError, UserNotExistsError, UserDisabledQueryError) as e:
        msg = str(e)
    except Exception as e:
        log.error(traceback.format_exc())
        msg = format_command_error(e)
    return msg


def _rating_to_display_dan(rating: int) -> int:
    """
    根据 rating 数值推导用于段位图显示的段位档位（0-10），与 _findRaPic 的区间对齐。
    用于 x代b50 等重算 rating 场景，使段位图与等级图样式一致。
    """
    if rating < 1000:
        return 0
    if rating < 2000:
        return 1
    if rating < 4000:
        return 2
    if rating < 7000:
        return 3
    if rating < 10000:
        return 4
    if rating < 12000:
        return 5
    if rating < 13000:
        return 6
    if rating < 14000:
        return 7
    if rating < 14500:
        return 8
    if rating < 15000:
        return 9
    return 10


def _pc_record_to_chartinfo(record: PlayCountRecord) -> ChartInfo:
    """将机台 PC 记录转换为 ChartInfo，供游玩排行图绘制。"""
    song_id = record.song_id
    level_index = max(0, min(4, record.level_index))
    achievements = record.achievements or 0.0
    rate = record.rate or ''
    fc = record.fc or ''
    fs = record.fs or ''
    title = record.title or ''
    type_ = 'SD'
    level = record.level or ''
    ds = float(record.dx_rating or 0)
    dx_score = int(record.dx_score or 0)
    ra = 0

    try:
        music = mai.total_list.by_id(str(song_id))
        if music and level_index < len(music.ds):
            title = music.title
            type_ = music.type
            level = music.level[level_index] if level_index < len(music.level) else level
            ds = round(float(music.ds[level_index]), 1)
    except Exception:
        pass

    if ds > 0 and achievements >= 0:
        computed = computeRa(ds, achievements, israte=True)
        if isinstance(computed, tuple):
            ra, rate_calc = computed
            if not rate:
                rate = rate_calc

    level_label = diffs[level_index] if level_index < len(diffs) else level
    return ChartInfo(
        achievements=achievements,
        fc=fc,
        fs=fs,
        level=level,
        level_index=level_index,
        title=title,
        type=type_,
        ds=ds,
        dxScore=dx_score,
        ra=ra,
        rate=rate,
        level_label=level_label,
        song_id=song_id,
    )


def _playinfo_to_chartinfo(play: PlayInfoDefault) -> ChartInfo:
    """
    将 PlayInfoDefault（query_user_plate 返回）转换为 ChartInfo。
    曲目展示信息（标题、定数、难度、类型、dxScore）以本地曲目表为准，保证显示正确。
    """
    song_id = play.song_id
    level_index = max(0, min(4, play.level_index))
    achievements = play.achievements or 0.0
    ra = play.ra or 0
    rate = play.rate or ''
    fc = play.fc or ''
    fs = play.fs or ''
    title = play.title or ''
    type_ = play.type or 'SD'
    level = play.level or ''
    ds = float(play.ds or 0)
    dx_score = int(play.dxScore or 0)

    try:
        music = mai.total_list.by_id(str(song_id))
        if music and level_index < len(music.ds):
            title = music.title
            type_ = music.type
            level = music.level[level_index] if level_index < len(music.level) else level
            ds = round(float(music.ds[level_index]), 1)
            # dxScore 仅使用接口返回的 play.dxScore，不再用达成率推算
    except Exception:
        pass

    # 单曲 rating：API 未返回或为 0 时用定数+达成率重算，保证「定数 -> ra」正确显示
    if (ra <= 0 or not rate) and ds > 0 and achievements >= 0:
        computed = computeRa(ds, achievements, israte=True)
        if isinstance(computed, tuple):
            ra_calc, rate_calc = computed
            if ra <= 0:
                ra = ra_calc
            if not rate:
                rate = rate_calc

    level_label = diffs[level_index] if level_index < len(diffs) else level
    return ChartInfo(
        achievements=achievements,
        fc=fc,
        fs=fs,
        level=level,
        level_index=level_index,
        title=title,
        type=type_,
        ds=ds,
        dxScore=dx_score,
        ra=ra,
        rate=rate,
        level_label=level_label,
        song_id=song_id,
    )


async def generate_version_b50(
    qqid: Optional[int] = None, 
    username: Optional[str] = None,
    version_name: str = ''
) -> Union[MessageSegment, str]:
    """
    x代b50：仅两条规则与常规 b50 不同——①按版本筛选 ②无视分组（直接 rating 倒序取前 50）。
    其余与常规 b50 一致：logo、排版（B35/B15 间留空）、副标题、加框分等。
    """
    try:
        if username:
            qqid = None
        
        version_key = platecn.get(version_name, version_name)
        if version_key not in version_map:
            return f'未知版本：{version_name}'
        
        version_list, display_name = version_map[version_key]
        all_records = await maiApi.query_user_plate(qqid=qqid, username=username, version=version_list)
        if not all_records:
            return f'未找到 {display_name} 版本的曲目记录'
        
        # 过滤宴谱
        all_records = filter_utage_records(all_records)
        
        # 仅此处无视分组：先转 ChartInfo（含重算的 ra），再按单曲 rating 倒序取前 50，保证顺序与显示一致
        all_charts = [_playinfo_to_chartinfo(r) for r in all_records]
        top50_charts = sorted(all_charts, key=lambda c: -c.ra)[:50]
        b35_list = top50_charts[:35]
        b15_list = top50_charts[35:50]
        
        # 与常规 b50 一致：rating 用本 50 首之和，加框分用 user_basic 的 additional_rating（lxns 为 0）
        total_ra = int(sum(c.ra for c in top50_charts))
        from .maimaidx_datasource import get_user_b50
        user_basic = await get_user_b50(qqid=qqid, username=username)
        additional_rating = user_basic.additional_rating if user_basic.additional_rating is not None else 0
        
        userinfo = UserInfo(
            additional_rating=additional_rating,
            nickname=user_basic.nickname,
            plate=user_basic.plate,
            rating=total_ra,
            username=user_basic.username,
            charts=Data(sd=b35_list, dx=b15_list),
        )
        # 尝试加载 PC 数据
        play_counts: dict[tuple[int, int], int] = {}
        if qqid:
            try:
                pc_records = pc_db.get_user_play_counts(qqid)
                for r in pc_records:
                    play_counts[(r.song_id, r.level_index)] = r.play_count
            except Exception:
                pass
        # 无视分组：50 首连续绘制，副标题「50 首 = rating」；隐藏左侧 logo
        _prepare_b50_warnings(userinfo, qqid, username)
        draw_best = DrawBest(userinfo, qqid, compact_layout=True, hide_logo=True, play_counts=play_counts or None)
        msg = MessageSegment.image(image_to_base64(await draw_best.draw()))
    except (UserNotFoundError, UserNotExistsError, UserDisabledQueryError) as e:
        msg = str(e)
    except Exception as e:
        log.error(traceback.format_exc())
        msg = format_command_error(e)
    return msg


async def _yueji_b50_common(
    qqid: Optional[int],
    username: Optional[str],
    threshold: float,
    by_group: bool,
) -> Union[MessageSegment, str]:
    """
    越级 B50：筛选条件为达成率 [0, 门槛)，按 ra 倒序取 B35/B15 或前 50 出图。默认门槛 97。
    by_group=True：按 B35/B15 分组各取前 35/15。
    by_group=False：无视分组取前 50，前 35 在 B35 区、后 15 在 B15 区。
    """
    try:
        if username:
            qqid = None
        from .maimaidx_datasource import get_user_records
        userinfo, records = await get_user_records(qqid=qqid, username=username)
        records = filter_utage_records(records)
        filtered = _yueji_b50_records(records, threshold)
        if not filtered:
            return (
                f'没有达成率在 [0, {threshold}) 内的成绩。'
                '（需开发者 Token 获取全量成绩；可尝试更大阈值如 98、99）'
            )

        if by_group:
            b15_list = sorted([r for r in filtered if _is_latest_version(r)], key=lambda x: -x.ra)[:15]
            b35_list = sorted([r for r in filtered if not _is_latest_version(r)], key=lambda x: -x.ra)[:35]
        else:
            top50 = sorted(filtered, key=lambda x: -x.ra)[:50]
            b35_list = top50[:35]
            b15_list = top50[35:50]

        total_ra = int(sum(r.ra for r in b35_list) + sum(r.ra for r in b15_list))
        yueji_userinfo = UserInfo(
            nickname=userinfo.nickname or userinfo.username or '未知',
            plate=userinfo.plate,
            additional_rating=userinfo.additional_rating if userinfo.additional_rating is not None else 0,
            rating=total_ra,
            username=userinfo.username,
            charts=Data(sd=b35_list, dx=b15_list),
        )
        # 尝试加载 PC 数据
        play_counts: dict[tuple[int, int], int] = {}
        if qqid:
            try:
                pc_records = pc_db.get_user_play_counts(qqid)
                for r in pc_records:
                    play_counts[(r.song_id, r.level_index)] = r.play_count
            except Exception:
                pass
        _prepare_b50_warnings(yueji_userinfo, qqid, username)
        draw_best = DrawBest(yueji_userinfo, qqid, compact_layout=not by_group, play_counts=play_counts or None)
        msg = MessageSegment.image(image_to_base64(await draw_best.draw()))
    except (UserNotFoundError, UserNotExistsError, UserDisabledQueryError) as e:
        msg = str(e)
    except Exception as e:
        log.error(traceback.format_exc())
        msg = format_command_error(e)
    return msg


async def generate_sun_b50(
    qqid: Optional[int] = None,
    username: Optional[str] = None,
    threshold: Optional[float] = None,
) -> Union[MessageSegment, str]:
    """寸 B50：达成率在 (x-0.1,x] 的成绩（可指定档位 x），按 B35/B15 分组各取前 35/15，ra 倒序。"""
    return await _sun_b50_common(qqid, username, by_group=True, threshold=threshold)


async def generate_sun_all_b50(
    qqid: Optional[int] = None,
    username: Optional[str] = None,
    threshold: Optional[float] = None,
) -> Union[MessageSegment, str]:
    """寸 ab50：达成率在 (x-0.1,x] 的成绩（可指定档位 x），无视分组取前 50 首 ra 倒序。"""
    return await _sun_b50_common(qqid, username, by_group=False, threshold=threshold)


async def generate_lock_b50(
    qqid: Optional[int] = None,
    username: Optional[str] = None,
) -> Union[MessageSegment, str]:
    """锁血 B50：筛选 [门槛, 门槛+步长)（整数+0.1、一位小数+0.01），按 B35/B15 分组各取前 35/15，ra 倒序。"""
    return await _lock_b50_common(qqid, username, by_group=True)


async def generate_lock_all_b50(
    qqid: Optional[int] = None,
    username: Optional[str] = None,
) -> Union[MessageSegment, str]:
    """锁血 ab50：筛选 [门槛, 门槛+步长)（整数+0.1、一位小数+0.01），无视分组取前 50，ra 倒序。"""
    return await _lock_b50_common(qqid, username, by_group=False)


async def generate_yueji_b50(
    qqid: Optional[int] = None,
    username: Optional[str] = None,
    threshold: float = 97.0,
) -> Union[MessageSegment, str]:
    """越级 B50：筛选 [0, 门槛) 的成绩按 ra 倒序，按 B35/B15 分组各取前 35/15。默认门槛 97。"""
    return await _yueji_b50_common(qqid, username, threshold, by_group=True)


async def generate_yueji_all_b50(
    qqid: Optional[int] = None,
    username: Optional[str] = None,
    threshold: float = 97.0,
) -> Union[MessageSegment, str]:
    """越级 ab50：筛选 [0, 门槛) 的成绩按 ra 倒序，无视分组取前 50。默认门槛 97。"""
    return await _yueji_b50_common(qqid, username, threshold, by_group=False)


async def generate_fit_b50(qqid: Optional[int] = None, username: Optional[str] = None) -> Union[MessageSegment, str]:
    """拟合 B50：用拟合定数+用户成绩算 ra，按 B35/B15 分组各取前 35/15，ra 倒序。"""
    return await _fit_b50_common(qqid, username, by_group=True)


async def generate_fit_all_b50(qqid: Optional[int] = None, username: Optional[str] = None) -> Union[MessageSegment, str]:
    """拟合 B50：用拟合定数+用户成绩算 ra，无视分组取前 50 首 ra 倒序，连续显示。"""
    return await _fit_b50_common(qqid, username, by_group=False)


async def _difficulty_b50_common(
    qqid: Optional[int],
    username: Optional[str],
    difficulty: str,
    by_group: bool,
) -> Union[MessageSegment, str]:
    """
    难度 B50：使用 DifficultyFilter 解析筛选条件，通过 b50_pipeline 生成图片。

    by_group=True（<难度>b50）：按 B35/B15 版本分组
    by_group=False（<难度>ab50）：无视分组取前 50

    支持的难度格式：
        - 难度等级：绿/黄/红/紫/白，或 Basic/Advanced/Expert/Master/Re:MASTER
        - 等级：13（=13.0~13.6）、13+（=13.7~13.9）、14.0（精确）、14.5（精确）
        - 范围：13-14、13.5-14.5
        - 组合：紫13、紫13+、紫13-14
    """
    from .maimaidx_difficulty_filter import DifficultyFilter
    from .maimaidx_b50_pipeline import b50_pipeline

    log.debug(f"[_difficulty_b50_common] 开始: difficulty='{difficulty}', by_group={by_group}")

    try:
        diff_filter = DifficultyFilter.parse(difficulty)
        log.debug(f"[_difficulty_b50_common] 解析成功: {diff_filter}")
    except ValueError as e:
        log.debug(f"[_difficulty_b50_common] 解析失败: {e}")
        return (
            f'无法识别难度「{difficulty}」，请使用如：紫b50、13b50、Master b50\n'
            f'（AI 锐评请发送：锐评一下 / 分析b50）'
        )

    result = await b50_pipeline(
        qqid=qqid,
        username=username,
        filter_fn=(
            diff_filter.matches
            if diff_filter.star_count is None
            else _make_star_filter(diff_filter)
        ),
        recalculate=True,
        by_group=by_group,
        empty_message=f"没有{diff_filter.display_name}的成绩数据",
    )

    log.debug(f"[_difficulty_b50_common] 返回结果类型: {type(result).__name__}")
    return result


def _make_star_filter(diff_filter):
    """构造 DX 星数筛选器；星数需要本地谱面 notes 计算理论最高 DX 分。"""
    def _matches(record):
        if not diff_filter.matches(record):
            return False
        try:
            music = mai.total_list.by_id(str(record.song_id))
            level_index = int(record.level_index)
            if not music or level_index >= len(music.charts):
                return False
            dx_max = sum(music.charts[level_index].notes) * 3
            return dx_star_count(int(record.dxScore or 0), dx_max) == diff_filter.star_count
        except (AttributeError, IndexError, TypeError, ValueError):
            return False
    return _matches


async def generate_difficulty_b50(
    qqid: Optional[int] = None,
    username: Optional[str] = None,
    difficulty: str = "",
) -> Union[MessageSegment, str]:
    """<难度>b50：筛选指定难度的成绩，按 B35/B15 分组显示。"""
    return await _difficulty_b50_common(qqid, username, difficulty, by_group=True)


async def generate_difficulty_all_b50(
    qqid: Optional[int] = None,
    username: Optional[str] = None,
    difficulty: str = "",
) -> Union[MessageSegment, str]:
    """<难度>ab50：筛选指定难度的成绩，无视分组直接取前50首。"""
    return await _difficulty_b50_common(qqid, username, difficulty, by_group=False)


# 评级提升映射：原评级 -> (提升后评级, 达成率下限)
_RATE_UPGRADE_MAP: Dict[str, Tuple[str, float]] = {
    'D': ('C', 60.0),
    'C': ('B', 70.0),
    'B': ('BB', 75.0),
    'BB': ('BBB', 80.0),
    'BBB': ('A', 90.0),
    'A': ('AA', 94.0),
    'AA': ('AAA', 97.0),
    'AAA': ('S', 98.0),
    'S': ('Sp', 99.0),
    'Sp': ('SS', 99.5),
    'SS': ('SSp', 100.0),
    'SSp': ('SSS', 100.0),  # SSp -> SSS 也是100%
    'SSS': ('SSSp', 100.5),
    'SSSp': ('SSSp', 100.5),  # 已达最高，保持不变
}


def _upgrade_achievement(achievement: float) -> Tuple[float, str, str]:
    """
    将达成率提升一个评级档次。
    返回: (新达成率, 原评级, 新评级)
    特例：原达成率 >= 100.5% 时保持原成绩
    """
    if achievement >= 100.5:
        return achievement, 'SSSp', 'SSSp'
    
    # 获取原评级
    _, original_rate = computeRa(1.0, achievement, israte=True)
    
    # 获取提升后的评级和达成率下限
    upgrade_info = _RATE_UPGRADE_MAP.get(original_rate)
    if not upgrade_info:
        return achievement, original_rate, original_rate
    
    new_rate, new_achievement = upgrade_info
    return new_achievement, original_rate, new_rate


async def _ideal_b50_common(
    qqid: Optional[int],
    username: Optional[str],
    by_group: bool,
) -> Union[MessageSegment, str]:
    """
    理想 B50：将每个成绩的评级提高一个档次后按 rating 排序。
    by_group=True（理想b50）：新版本取前15，旧版本取前35，按 B35/B15 分组显示
    by_group=False（理想ab50）：无视分组直接取前50首
    特例：原成绩达成率 >=100.5% 时保持原成绩
    """
    try:
        if username:
            qqid = None

        # 获取用户所有成绩（需要开发者 Token / 落雪 OAuth）
        from .maimaidx_datasource import get_user_records
        userinfo, records = await get_user_records(qqid=qqid, username=username)
        records = filter_utage_records(records)

        if not records:
            return '没有成绩数据（需开发者 Token 获取全量成绩）'

        # 提升每个成绩的评级
        upgraded_records: List[ChartInfo] = []
        for r in records:
            # 获取提升后的达成率
            new_achievement, original_rate, new_rate = _upgrade_achievement(r.achievements)
            
            # 获取当前歌曲的定数和标题
            try:
                music = mai.total_list.by_id(str(r.song_id))
                if music and r.level_index < len(music.ds):
                    current_ds = round(float(music.ds[r.level_index]), 1)
                    current_level = music.level[r.level_index] if r.level_index < len(music.level) else r.level
                    current_title = music.title
                    current_type = music.type
                else:
                    current_ds = r.ds
                    current_level = r.level
                    current_title = getattr(r, 'title', '')
                    current_type = getattr(r, 'type', 'SD')
            except Exception:
                current_ds = r.ds
                current_level = r.level
                current_title = getattr(r, 'title', '')
                current_type = getattr(r, 'type', 'SD')
            
            # 重新计算 rating 与评级，图标以实际重算后的评级为准
            new_ra, computed_rate = computeRa(current_ds, new_achievement, israte=True)
            
            # 获取 level_label
            level_label = diffs[r.level_index] if r.level_index < len(diffs) else current_level
            
            # 创建新的 ChartInfo，使用提升后的数据
            upgraded = ChartInfo(
                song_id=int(r.song_id),
                level=current_level,
                level_index=r.level_index,
                ds=current_ds,
                ra=new_ra,
                rate=computed_rate,
                achievements=new_achievement,
                fc=r.fc,
                fs=r.fs,
                dxScore=r.dxScore,
                title=current_title,
                type=current_type,
                level_label=level_label,
            )
            upgraded_records.append(upgraded)
        
        # 按新 rating 倒序排序
        upgraded_records.sort(key=lambda x: -x.ra)
        
        if by_group:
            # 理想b50：新版本取前15，旧版本取前35
            b15_list = sorted([r for r in upgraded_records if _is_latest_version(r)], key=lambda x: -x.ra)[:15]
            b35_list = sorted([r for r in upgraded_records if not _is_latest_version(r)], key=lambda x: -x.ra)[:35]
        else:
            # 理想ab50：无视分组取前50
            top50 = upgraded_records[:50]
            b35_list = top50[:35]
            b15_list = top50[35:50]
        
        if not b35_list and not b15_list:
            return '没有符合条件的成绩'
        
        # 计算总 rating
        total_ra = int(sum(r.ra for r in b35_list) + sum(r.ra for r in b15_list))
        
        ideal_userinfo = UserInfo(
            nickname=userinfo.nickname or userinfo.username or '未知',
            plate=userinfo.plate,
            additional_rating=userinfo.additional_rating if userinfo.additional_rating is not None else 0,
            rating=total_ra,
            username=userinfo.username,
            charts=Data(sd=b35_list, dx=b15_list),
        )
        
        # 尝试加载 PC 数据
        play_counts: dict[tuple[int, int], int] = {}
        if qqid:
            try:
                pc_records = pc_db.get_user_play_counts(qqid)
                for r in pc_records:
                    play_counts[(r.song_id, r.level_index)] = r.play_count
            except Exception:
                pass
        # 绘制 B50 图片，使用紧凑布局显示原成绩
        _prepare_b50_warnings(ideal_userinfo, qqid, username)
        draw_best = DrawBest(ideal_userinfo, qqid, compact_layout=not by_group, play_counts=play_counts or None)
        msg = MessageSegment.image(image_to_base64(await draw_best.draw()))
        
    except (UserNotFoundError, UserNotExistsError, UserDisabledQueryError) as e:
        msg = str(e)
    except Exception as e:
        log.error(traceback.format_exc())
        msg = format_command_error(e)
    return msg


async def generate_ideal_b50(
    qqid: Optional[int] = None,
    username: Optional[str] = None,
) -> Union[MessageSegment, str]:
    """理想b50：将每个成绩的评级提高一个档次，新版本取前15，旧版本取前35，按 B35/B15 分组显示。"""
    return await _ideal_b50_common(qqid, username, by_group=True)


async def generate_ideal_all_b50(
    qqid: Optional[int] = None,
    username: Optional[str] = None,
) -> Union[MessageSegment, str]:
    """理想ab50：将每个成绩的评级提高一个档次，无视分组直接取前50首。"""
    return await _ideal_b50_common(qqid, username, by_group=False)


async def generate_coop_b50(
    qqid_a: int,
    qqid_b: int,
    nickname_a: str,
    nickname_b: str,
) -> Union[MessageSegment, str]:
    """
    合作 B50（分组）：按常规 b50 分组与排版，B35 取两人 sd 合并后 ra 前 35，B15 取两人 dx 合并后 ra 前 15。
    """
    try:
        from .maimaidx_datasource import get_user_b50_or_fallback
        userinfo_a = await get_user_b50_or_fallback(qqid=qqid_a)
        userinfo_b = await get_user_b50_or_fallback(qqid=qqid_b)
    except (UserNotFoundError, UserNotExistsError, UserDisabledQueryError) as e:
        return str(e)

    sd_a = (userinfo_a.charts and userinfo_a.charts.sd) or []
    dx_a = (userinfo_a.charts and userinfo_a.charts.dx) or []
    sd_b = (userinfo_b.charts and userinfo_b.charts.sd) or []
    dx_b = (userinfo_b.charts and userinfo_b.charts.dx) or []

    sd_merged: List[Tuple[ChartInfo, str]] = [(c, nickname_a) for c in sd_a] + [(c, nickname_b) for c in sd_b]
    dx_merged: List[Tuple[ChartInfo, str]] = [(c, nickname_a) for c in dx_a] + [(c, nickname_b) for c in dx_b]
    sd_merged.sort(key=lambda x: x[0].ra, reverse=True)
    dx_merged.sort(key=lambda x: x[0].ra, reverse=True)
    # 同一谱面（song_id+level_index）只保留 ra 最高的一条，避免两人同谱面占两格
    def dedup_by_chart(items: List[Tuple[ChartInfo, str]]) -> List[Tuple[ChartInfo, str]]:
        seen: set = set()
        out: List[Tuple[ChartInfo, str]] = []
        for info, src in items:
            key = (info.song_id, info.level_index)
            if key not in seen:
                seen.add(key)
                out.append((info, src))
        return out
    sd_dedup = dedup_by_chart(sd_merged)
    dx_dedup = dedup_by_chart(dx_merged)
    sd_top35 = sd_dedup[:35]
    dx_top15 = dx_dedup[:15]

    if not sd_top35 and not dx_top15:
        return '两人均无 b50 数据，无法生成合作 B50。'

    try:
        drawer = DrawCoopB50(
            nickname_a, nickname_b,
            qqid_a=qqid_a, qqid_b=qqid_b,
            sd_list=sd_top35, dx_list=dx_top15,
        )
        img = await drawer.draw()
        return MessageSegment.image(image_to_base64(img))
    except Exception as e:
        log.error(traceback.format_exc())
        return format_command_error(e)


async def generate_coop_all_b50(
    qqid_a: int,
    qqid_b: int,
    nickname_a: str,
    nickname_b: str,
) -> Union[MessageSegment, str]:
    """
    合作 ab50（无视分组）：两人 b50 合并后按单曲 rating 排序取前 50，连续排版。
    """
    try:
        from .maimaidx_datasource import get_user_b50_or_fallback
        userinfo_a = await get_user_b50_or_fallback(qqid=qqid_a)
        userinfo_b = await get_user_b50_or_fallback(qqid=qqid_b)
    except (UserNotFoundError, UserNotExistsError, UserDisabledQueryError) as e:
        return str(e)

    merged: List[Tuple[ChartInfo, str]] = []
    sd_a = (userinfo_a.charts and userinfo_a.charts.sd) or []
    dx_a = (userinfo_a.charts and userinfo_a.charts.dx) or []
    for c in sd_a + dx_a:
        merged.append((c, nickname_a))
    sd_b = (userinfo_b.charts and userinfo_b.charts.sd) or []
    dx_b = (userinfo_b.charts and userinfo_b.charts.dx) or []
    for c in sd_b + dx_b:
        merged.append((c, nickname_b))

    merged.sort(key=lambda x: x[0].ra, reverse=True)
    # 同一谱面只保留 ra 最高的一条
    seen: set = set()
    merged_dedup: List[Tuple[ChartInfo, str]] = []
    for info, src in merged:
        key = (info.song_id, info.level_index)
        if key not in seen:
            seen.add(key)
            merged_dedup.append((info, src))
    top50 = merged_dedup[:50]
    if not top50:
        return '两人均无 b50 数据，无法生成合作 B50。'

    try:
        drawer = DrawCoopB50(
            nickname_a, nickname_b,
            qqid_a=qqid_a, qqid_b=qqid_b,
            merged_list=top50,
        )
        img = await drawer.draw()
        return MessageSegment.image(image_to_base64(img))
    except Exception as e:
        log.error(traceback.format_exc())
        return format_command_error(e)


async def generate(
    qqid: Optional[int] = None,
    username: Optional[str] = None,
    *,
    force_refresh: bool = False,
) -> Union[MessageSegment, str]:
    """
    生成b50
    
    Params:
        `qqid`: QQ号
        `username`: 用户名
        `icon`: 头像
    Returns:
        `Union[MessageSegment, str]`
    """
    try:
        if username:
            qqid = None
        from .maimaidx_datasource import get_user_b50
        userinfo = await get_user_b50(qqid=qqid, username=username, force_refresh=force_refresh)

        # 尝试加载 PC 数据
        play_counts: dict[tuple[int, int], int] = {}
        if qqid:
            try:
                pc_records = pc_db.get_user_play_counts(qqid)
                for r in pc_records:
                    play_counts[(r.song_id, r.level_index)] = r.play_count
            except Exception:
                pass

        _prepare_b50_warnings(userinfo, qqid, username)
        draw_best = DrawBest(userinfo, qqid, play_counts=play_counts or None)
        
        msg = MessageSegment.image(image_to_base64(await draw_best.draw()))
    except (UserNotFoundError, UserNotExistsError, UserDisabledQueryError) as e:
        msg = str(e)
    except Exception as e:
        log.error(traceback.format_exc())
        msg = format_command_error(e)
    return msg


# 鸟/鸟加达成率阈值（达成率百分比）
_BIRD_THRESHOLD = 100.0       # 鸟 SSS
_BIRD_PLUS_THRESHOLD = 100.5  # 鸟加 SSS+


def _classify_chart_records(userinfo: UserInfo) -> List[ChartInfo]:
    """汇总 B35(sd) + B15(dx) 的全部成绩记录，过滤空记录。"""
    records: List[ChartInfo] = []
    charts = getattr(userinfo, 'charts', None)
    if charts is None:
        return records
    for group in (getattr(charts, 'sd', None) or [], getattr(charts, 'dx', None) or []):
        for rec in group:
            if rec is not None:
                records.append(rec)
    return records


def _format_bird_rate_record(rec: ChartInfo) -> str:
    """格式化单条未达成记录：标题 [类型] 定数 达成率%。"""
    song_type = str(getattr(rec, 'type', '') or '').upper()
    type_tag = ' [DX]' if song_type == 'DX' else (' [SD]' if song_type in ('SD', 'STD') else '')
    title = getattr(rec, 'title', '未知') or '未知'
    ds = getattr(rec, 'ds', 0) or 0
    ach = float(getattr(rec, 'achievements', 0) or 0)
    return f'· {title}{type_tag} {ds:g} {ach:.4f}%'


async def _generate_bird_rate_common(
    qqid: Optional[int],
    username: Optional[str],
    threshold: float,
    label: str,
) -> str:
    """
    统计 B50（B35+B15）中达成率 >= threshold 的曲目比例，返回纯文本汇总。
    """
    try:
        if username:
            qqid = None
        from .maimaidx_datasource import get_user_b50
        userinfo = await get_user_b50(qqid=qqid, username=username)

        records = _classify_chart_records(userinfo)
        total = len(records)
        if total == 0:
            return '没有获取到 B50 成绩数据。'

        hit = [r for r in records if float(getattr(r, 'achievements', 0) or 0) >= threshold]
        miss = [r for r in records if float(getattr(r, 'achievements', 0) or 0) < threshold]
        hit_count = len(hit)
        rate = hit_count / total * 100.0

        # 未达成曲目按达成率从高到低排列，方便挑差一点就达标的
        miss.sort(key=lambda r: float(getattr(r, 'achievements', 0) or 0), reverse=True)

        nick = getattr(userinfo, 'nickname', None) or getattr(userinfo, 'username', None) or str(qqid or username or '未知')
        rating = int(getattr(userinfo, 'rating', 0) or 0)

        lines = [
            f'【B50 {label}率】 {nick}',
            f'Rating: {rating}',
            f'{label}达成: {hit_count}/{total}（{rate:.2f}%）',
        ]
        if miss:
            lines.append(f'未达成 {label}（{len(miss)} 首）:')
            lines.extend(_format_bird_rate_record(r) for r in miss)
        else:
            lines.append(f'太强了！B50 全部达成 {label}，{label}满贯喵～')
        return '\n'.join(lines)
    except (UserNotFoundError, UserNotExistsError, UserDisabledQueryError) as e:
        return str(e)
    except Exception as e:
        log.error(traceback.format_exc())
        return format_command_error(e)


async def generate_b50_bird_rate(
    qqid: Optional[int] = None,
    username: Optional[str] = None,
) -> str:
    """B50 鸟率：达成率 >= 100.0000% (SSS) 的曲目比例。"""
    return await _generate_bird_rate_common(qqid, username, _BIRD_THRESHOLD, '鸟')


async def generate_b50_bird_plus_rate(
    qqid: Optional[int] = None,
    username: Optional[str] = None,
) -> str:
    """B50 鸟加率：达成率 >= 100.5000% (SSS+) 的曲目比例。"""
    return await _generate_bird_rate_common(qqid, username, _BIRD_PLUS_THRESHOLD, '鸟加')
