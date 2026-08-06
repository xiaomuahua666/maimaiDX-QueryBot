import random
import time
import traceback
from collections import defaultdict
from typing import Callable, DefaultDict, List, Optional, Tuple, Union

import pyecharts.options as opts
from nonebot.adapters.onebot.v11 import MessageSegment
from pyecharts.charts import Pie

from ..config import *
from .maimaidx_theme import pic
from .image import *
from .maimaidx_api_data import *
from .maimaidx_best_50 import ScoreBaseImage, changeColumnWidth, coloumWidth, computeRa, _music_is_new
from .maimaidx_model import PlanInfo, PlayInfoDefault, PlayInfoDev, RaMusic
from .maimaidx_music import Music, mai
from .tool import run_chrome_to_base64

Filter = Tuple[
    List[PlayInfoDefault],
    List[PlayInfoDefault],
    List[PlayInfoDefault],
    List[PlayInfoDefault],
    List[PlayInfoDefault]
]
Condition = Callable[[PlayInfoDefault], bool]

# 等级牌子简称 → level_process 使用的 plan 码（与 13sss进度 / 13+fc进度 等价）
LEVEL_PLATE_PLAN: dict[str, str] = {
    '将': 'sss',
    '者': 'bbb',
    '极': 'fc',
    '極': 'fc',
    '神': 'ap',
    '舞舞': 'fdx',  # 与 level_process 的 syncRank 一致（≥ FSD/FDX）
}

LEVEL_PLATE_DESC: dict[str, str] = {
    '将': '达成率 ≥100%',
    '者': '达成率 ≥80%',
    '极': 'Full Combo 及以上',
    '極': 'Full Combo 及以上',
    '神': 'All Perfect 及以上',
    '舞舞': 'Full Sync DX 及以上',
}


def resolve_level_plate_plan(plan_cn: str) -> str:
    key = plan_cn.strip()
    if key not in LEVEL_PLATE_PLAN:
        raise ValueError(f'不支持的等级牌子类型：{plan_cn}')
    return LEVEL_PLATE_PLAN[key]


def _fc_plan_index(fc: Optional[str]) -> int:
    if not fc:
        return -1
    fc = fc.lower()
    if fc in combo_rank:
        return combo_rank.index(fc)
    return -1


def _fs_plan_index(fs: Optional[str]) -> int:
    """查分器 fs 档位索引；sync 低于 fs，未知值视为未达标。"""
    if not fs:
        return -1
    fs = fs.lower()
    if fs == 'sync':
        return -1
    if fs in sync_rank:
        return sync_rank.index(fs)
    if fs in sync_rank_p:
        return sync_rank_p.index(fs)
    return -1


def _fc_meets_plan(fc: Optional[str], plan_value: int) -> bool:
    return _fc_plan_index(fc) >= plan_value


def _fs_meets_plan(fs: Optional[str], plan_value: int) -> bool:
    return _fs_plan_index(fs) >= plan_value


def _level_plan_completed(plannum: int, plan_value, rec) -> bool:
    if plannum == 0:
        return float(rec.achievements) >= float(plan_value)
    if plannum == 1:
        return _fc_meets_plan(rec.fc, plan_value)
    if plannum == 2:
        return _fs_meets_plan(rec.fs, plan_value)
    return False


def _parse_level_plan(plan: str) -> tuple[int, float | int]:
    p = plan.lower()
    if p in scoreRank:
        return 0, achievementList[scoreRank.index(p) - 1]
    if p in comboRank:
        return 1, comboRank.index(p)
    if p in combo_rank:
        return 1, combo_rank.index(p)
    if p in syncRank:
        return 2, syncRank.index(p)
    if p in sync_rank:
        return 2, sync_rank.index(p)
    if p in sync_rank_p:
        return 2, sync_rank_p.index(p)
    raise ValueError(f'无法识别的评价条件：{plan}')


async def _fetch_level_plate_records(qqid: int, username: Optional[str]):
    from .maimaidx_datasource import get_user_records

    _userinfo, records = await get_user_records(qqid=qqid, username=username)
    return records


async def level_plate_summary_text(
    qqid: int,
    username: Optional[str],
    level: str,
    plan_cn: str,
) -> str:
    """等级牌子进度文字摘要（如 13将）。"""
    plan = resolve_level_plate_plan(plan_cn)
    plannum, plan_value = _parse_level_plan(plan)
    records = await _fetch_level_plate_records(qqid, username)
    music = mai.total_list.by_plan(level)

    completed_n = 0
    unfinished_n = 0
    notplayed_n = 0

    played_map: dict[tuple[int, int], PlayInfoDev | PlayInfoDefault] = {}
    for rec in records:
        if str(rec.song_id) in music and rec.level == level:
            played_map[(int(rec.song_id), int(rec.level_index))] = rec

    def iter_slots():
        for sid, slot in music.items():
            if isinstance(slot, dict):
                for idx in slot:
                    yield int(sid), int(idx), slot[idx]
            else:
                yield int(sid), int(slot.lv), slot

    for song_id, level_index, _slot in iter_slots():
        key = (song_id, level_index)
        rec = played_map.get(key)
        if rec is None:
            notplayed_n += 1
        elif _level_plan_completed(plannum, plan_value, rec):
            completed_n += 1
        else:
            unfinished_n += 1

    total = completed_n + unfinished_n + notplayed_n
    pct = (completed_n / total * 100) if total else 0.0
    desc = LEVEL_PLATE_DESC.get(plan_cn, plan)
    lines = [
        f'【{level}{plan_cn} 进度】',
        f'目标：{desc}',
        f'已完成 {completed_n} / {total} 首（{pct:.1f}%）',
        f'未完成 {unfinished_n} 首 · 未游玩 {notplayed_n} 首',
    ]
    if completed_n >= total and total > 0:
        lines.append(f'恭喜，{level}{plan_cn} 已全部完成！')
    elif unfinished_n + notplayed_n <= 5 and (unfinished_n + notplayed_n) > 0:
        lines.append('剩余较少，加油清完最后几首～')
    else:
        lines.append('下方为完成表图片，可查看已完成 / 未完成 / 未开始详情。')
    return '\n'.join(lines)


async def music_global_data(music: Music, level_index: int) -> MessageSegment:
    """
    绘制曲目游玩详情
    
    Params:
        `music`: :class:Music
        `level_index`: 难度
    Returns:
        `MessageSegment`
    """
    stats = music.stats[level_index]
    fc_data_pair = [list(z) for z in zip([c.upper() if c else 'Not FC' for c in [''] + comboRank], stats.fc_dist)]
    acc_data_pair = [list(z) for z in zip([s.upper() for s in scoreRank], stats.dist)]

    initopts = opts.InitOpts(width='1000px', height='800px', bg_color='#fff', js_host='./')
    labelopts = opts.LabelOpts(
        position='outside',
        formatter='{a|{a}}{abg|}\n{hr|}\n {b|{b}: }{c}  {per|{d}%}  ',
        background_color='#eee',
        border_color='#aaa',
        border_width=1,
        border_radius=4,
        rich={
            'a': {'color': '#999', 'lineHeight': 22, 'align': 'center'},
            'abg': {
                'backgroundColor': '#e3e3e3',
                'width': '100%',
                'align': 'right',
                'height': 22,
                'borderRadius': [4, 4, 0, 0],
            },
            'hr': {
                'borderColor': '#aaa',
                'width': '100%',
                'borderWidth': 0.5,
                'height': 0,
            },
            'b': {'fontSize': 16, 'lineHeight': 33},
            'per': {
                'color': '#eee',
                'backgroundColor': '#334455',
                'padding': [2, 4],
                'borderRadius': 2,
            },
        },
    )
    titleopts = opts.TitleOpts(
        title=f'{music.id} {music.title} 「{diffs[level_index]}」',
        pos_left='center',
        pos_top='20',
        title_textstyle_opts=opts.TextStyleOpts(color='#2c343c'),
    )
    legendopts = opts.LegendOpts(pos_left=15, pos_top=10, orient='vertical')

    pie = Pie(initopts)
    pie.add('全连等级', fc_data_pair, radius=[0, '30%'], label_opts=labelopts)
    pie.add('达成率等级', acc_data_pair, radius=['50%', '70%'], is_clockwise=True, label_opts=labelopts)
    pie.set_global_opts(title_opts=titleopts, legend_opts=legendopts)
    pie.set_series_opts(tooltip_opts=opts.TooltipOpts(trigger='item', formatter='{a} <br/>{b}: {c} ({d}%)'))
    pie.render(str(pie_html_file))
    base64 = await run_chrome_to_base64()

    return MessageSegment.image(base64)


class DrawScore(ScoreBaseImage):
    
    def __init__(self, image: Image.Image = None) -> None:
        super().__init__(image)
        self._im.alpha_composite(self.aurora_bg)
        self._im.alpha_composite(self.shines_bg, (34, 0))
        self._im.alpha_composite(self.rainbow_bg, (319, self._im.size[1] - 643))
        self._im.alpha_composite(self.rainbow_bottom_bg, (100, self._im.size[1] - 343))
        for h in range((self._im.size[1] // 358) + 1):
            self._im.alpha_composite(self.pattern_bg, (0, (358 + 7) * h))

    def whilepic(self, data: List[RaMusic], y: int = 200):
        """
        循环绘制谱面
        
        Params:
            `data`: `谱面数据`
            `y`: `Y轴偏移`
        """
        dy = 65
        x = 0
        for n, v in enumerate(data):
            if n % 20 == 0:
                x = 55
                y += dy if n != 0 else 0
            else:
                x += 65
            cover = Image.open(music_picture(v.id)).resize((55, 55))
            self._im.alpha_composite(cover, (x, y))
            self._im.alpha_composite(self.id_diff[int(v.lv)], (x, y + 45))
            self._tb.draw(x + 27, y + 50, 10, v.id, self.t_color[int(v.lv)], 'mm')
    
    def whilerisepic(self, data: List[RiseScore], low_score: int, isdx: bool):
        """
        循环绘制上分推荐数据
        
        Params:
            `data`: `上分数据`
            `low_score`: `最低分`
            `isdx`: `是否DX版本`
        """
        y = 120
        for index, _d in enumerate(data):
            x = 200 if isdx else 700
            y += 140 if index != 0 else 0
            
            from .maimaidx_theme import resolve_theme_path as _rtp
            _t = getattr(self, '_theme', None) or 'prism_plus'
            rate = Image.open(_rtp(maimaidir, _t, f'UI_TTR_Rank_{_d.rate}.png')).resize((63, 28))
            
            self._im.alpha_composite(self._rise[_d.level_index], (x + 30, y))
            self._im.alpha_composite(Image.open(music_picture(_d.song_id)).resize((80, 80)), (x + 55, y + 40))
            self._im.alpha_composite(Image.open(pic(f'{_d.type.upper()}.png')).resize((60, 22)), (x + 240, y + 114))
            if _d.oldrate:
                oldrate = Image.open(_rtp(maimaidir, _t, f'UI_TTR_Rank_{_d.oldrate}.png')).resize((63, 28))
                self._im.alpha_composite(oldrate, (x + 145, y + 82))
            self._im.alpha_composite(rate, (x + 305, y + 82))
            
            title = _d.title
            if coloumWidth(title) > 26:
                title = changeColumnWidth(title, 25) + '...'
            self._sy.draw(x + 142, y + 44, 17, title, self.t_color[_d.level_index], 'lm')
            self._tb.draw(x + 145, y + 124, 18, f'ID: {_d.song_id}', self.id_color[_d.level_index], 'lm')
            self._tb.draw(x + 210, y + 71, 25, f'{_d.oldachievements:.4f}%', self.t_color[_d.level_index], anchor='mm')
            self._tb.draw(x + 245, y + 96, 17, f'Ra: {_d.oldra}', self.t_color[_d.level_index], anchor='mm')
            self._tb.draw(x + 370, y + 71, 25, f'{_d.achievements:.4f}%', self.t_color[_d.level_index], anchor='mm')
            self._tb.draw(x + 415, y + 96, 17, f'Ra: {_d.ra}', self.t_color[_d.level_index], anchor='mm')
            self._tb.draw(x + 315, y + 124, 18, f'ds:{_d.ds:.1f}', self.id_color[_d.level_index], anchor='lm')
            if _d.oldra > low_score:
                new_ra = _d.ra - _d.oldra
            else:
                new_ra = _d.ra - low_score
            self._tb.draw(x + 390, y + 124, 18, f'Ra +{new_ra}', self.id_color[_d.level_index], 'lm')
         
    def draw_rise(self, b35_scores: List[RiseScore], b35_score: int, b15_scores: List[RiseScore], b15_score: int) -> Image.Image:
        """
        绘制上分数据表（B35=旧版本谱面成绩，B15=新版本谱面成绩，与谱面类型 SD/DX 无关联）

        Params:
            b35_scores: B35 旧版本推荐
            b35_score: 旧版本最低分
            b15_scores: B15 新版本推荐
            b15_score: 新版本最低分
        """
        title_bg = self.title_bg.copy().resize((273, 80)) if self.title_bg else None
        if title_bg:
            self._im.alpha_composite(title_bg, (314, 30))
        self._sy.draw(450, 68, 18, '旧版本谱面推荐', self.text_color, 'mm')
        self.whilerisepic(b35_scores, b35_score, True)
        if title_bg:
            self._im.alpha_composite(title_bg, (814, 30))
        self._sy.draw(950, 68, 18, '新版本谱面推荐', self.text_color, 'mm')
        self.whilerisepic(b15_scores, b15_score, False)

        draw_centered_design_footer(
            self._im,
            self._sy,
            footer_generated(),
            design_bg=self.design_bg,
            color=self.text_color,
            margin_x=120,
            bar_height=48,
            start_font_size=13,
            min_font_size=9,
            bottom_gap=28,
        )
        return self._im

    def draw_plan(
        self,
        completed: Union[List[PlayInfoDefault], List[PlayInfoDev]],
        completed_y: int,
        unfinished: Union[List[PlayInfoDefault], List[PlayInfoDev]],
        unfinished_y: int,
        notstarted: List[RaMusic],
        plan: str,
        completed_len: int,
    ) -> Image.Image:
        """
        绘制进度表
        
        Params:
            `completed`: `已完成谱面`
            `completed_y`: `已完成谱面高度`
            `unfinished`: `未完成谱面`
            `unfinished_y`: `未完成谱面高度`
            `notstarted`: `未游玩谱面`
            `plan`: `目标`
            `completed_len`: `已完成谱面数量`
        Returns:
            `Image.Image`
        """
        max = len(completed + unfinished + notstarted)

        if self.title_lengthen_bg:
            self._im.alpha_composite(self.title_lengthen_bg, (475, 30))
            self._im.alpha_composite(self.title_lengthen_bg, (475, 30 + completed_y))
            self._im.alpha_composite(self.title_lengthen_bg, (475, 30 + completed_y + unfinished_y))
        
        self._sy.draw(700, 77, 22, f'已完成谱面「{len(completed)}」个', self.text_color, 'mm')
        self._sy.draw(700, 77 + completed_y, 22, f'未完成谱面「{len(unfinished)}」个', self.text_color, 'mm')
        self._sy.draw(700, 77 + completed_y + unfinished_y, 22, f'未游玩谱面「{len(notstarted)}」个', self.text_color, 'mm')
        
        self.whiledraw(completed[:completed_len], True, 140)
        self.whiledraw(unfinished[:30], True, 140 + completed_y)
        self.whilepic(notstarted[:100], 140 + completed_y + unfinished_y)

        if self.design_bg:
            self._im.alpha_composite(self.design_bg, (200, self._im.size[1] - 113))
        pagemsg = f'共计「{max}」个谱面，剩余「{len(unfinished + notstarted)}」个谱面未完成「{plan.upper()}」'
        self._sy.draw(700, self._im.size[1] - 70, 25, pagemsg, self.text_color, 'mm')
        return self._im

    def draw_category(
        self, 
        category: str, 
        data: Union[List[PlayInfoDefault], List[PlayInfoDev], List[RaMusic]],
        page: int = 1, 
        end_page: int = 1
    ) -> Image.Image:
        """
        绘制指定进度表
        
        Params:
            `category`: `类别`
            `data`: `数据`
            `page`: `页数`
            `end_page`: `总页数`
        Returns:
            `Image.Image`
        """
        lendata = len(data)
        newdata = data[(page - 1) * 80: page * 80]
        if self.title_lengthen_bg:
            self._im.alpha_composite(self.title_lengthen_bg, (475, 30))
        if category == 'completed' or category == 'unfinished':
            txt = '已完成' if category == 'completed' else '未完成'
            self._sy.draw(700, 77, 28, f'{txt}谱面', self.text_color, 'mm')
            self.whiledraw(newdata, True, 140)
            if self.design_bg:
                self._im.alpha_composite(self.design_bg, (200, self._im.size[1] - 113))
            
            pagemsg = f'{txt}谱面共计「{lendata}」个，'
            pagemsg += f'展示第「{(page - 1) * 80 + 1}-{80 * (page - 1) + len(newdata)}」个，'
            pagemsg += f'当前第「{page} / {end_page}」页'
            self._sy.draw(700, self._im.size[1] - 70, 25, pagemsg, self.text_color, 'mm')
        else:
            self._sy.draw(700, 105, 28, '未游玩谱面', self.text_color, 'mm')
            self.whilepic(data)
            if self.design_bg:
                self._im.alpha_composite(self.design_bg, (200, self._im.size[1] - 113))
            self._sy.draw(700, self._im.size[1] - 70, 25, f'未游玩谱面共计「{len(data)}」个', self.text_color, 'mm')
        return self._im
    
    def draw_scorelist(
        self, 
        rating: Union[str, float], 
        data: Union[List[PlayInfoDefault], List[PlayInfoDev]], 
        page: int = 1, 
        end_page: int = 1
    ) -> Image.Image:
        """
        绘制分数列表
        
        Params:
            `rating`: `定数`
            `data`: `数据`
            `page`: `页数`
            `end_page`: `总页数`
        Returns:
            `Image.Image`
        """
        lendata = len(data)
        newdata = data[(page - 1) * 80: page * 80]
        r = len(newdata) // 20 + (0 if len(newdata) % 20 == 0 else 1)
        for n in range(r):
            y = (109 * 4 + 140) * n
            if self.title_lengthen_bg:
                self._im.alpha_composite(self.title_lengthen_bg, (475, 30 + y))
            start = (20 * n + 1) + 80 * (page - 1)
            self._sy.draw(700, 77 + y, 28, f'No.{start}- No.{start + len(newdata[n * 20: (n + 1) * 20]) - 1}', self.text_color, 'mm')
            self.whiledraw(newdata[n * 20: (n + 1) * 20], True, 140 + y)
        if self.design_bg:
            self._im.alpha_composite(self.design_bg, (200, self._im.size[1] - 113))
        
        pagemsg = f'「{rating}」共计「{lendata}」个成绩，'
        pagemsg += f'展示第「{(page - 1) * 80 + 1}-{80 * (page - 1) + len(newdata)}」个，'
        pagemsg += f'当前第「{page} / {end_page}」页'
        self._sy.draw(700, self._im.size[1] - 70, 25, pagemsg, self.text_color, 'mm')
        return self._im


def _pick_rise_scores(
    old_records: DefaultDict[int, Dict[int, float]],
    candidate,
    *,
    ignore: set[tuple[int, int]],
    ra: int,
    score: Optional[int],
) -> List[RiseScore]:
    music: List[RiseScore] = []
    for _m in candidate:
        if (song_id := int(_m.id)) >= 100000:
            continue
        for index in _m.diff:
            if (song_id, index) in ignore:
                continue
            for r in achievementList[-4:]:
                basera, rate = computeRa(_m.ds[index], r, israte=True)
                if basera <= ra:
                    continue
                if score and basera - int(score) < ra:
                    continue
                if song_id in old_records and index in old_records[song_id]:
                    oldra, oldrate = computeRa(_m.ds[index], old_records[song_id][index], israte=True)
                    if oldra >= basera:
                        continue
                    ss = RiseScore(
                        song_id=song_id,
                        title=_m.title,
                        type=_m.type,
                        level_index=index,
                        ds=_m.ds[index],
                        ra=basera,
                        rate=rate,
                        achievements=r,
                        oldra=oldra,
                        oldrate=oldrate,
                        oldachievements=old_records[song_id][index],
                    )
                else:
                    ss = RiseScore(
                        song_id=song_id,
                        title=_m.title,
                        type=_m.type,
                        level_index=index,
                        ds=_m.ds[index],
                        ra=basera,
                        rate=rate,
                        achievements=r,
                    )
                music.append(ss)
                break
    return music


def get_rise_score_list(
    old_records: DefaultDict[int, Dict[int, float]],
    chart_type: str,
    b50_list: List[ChartInfo],
    level: Optional[str] = None,
    score: Optional[int] = None,
    *,
    fallback_ra: Optional[int] = None,
) -> Tuple[List[RiseScore], int]:
    """
    随机获取加分曲目。

    Params:
        chart_type: 'SD'=旧版本列(B35) / 'DX'=新版本列(B15)
        b50_list: 对应区已上分的成绩列表（用于取底分、去重）
        level: 等级
        score: 目标提升分
        fallback_ra: B15 为空时用于估算定数区间的 B35 底分
    Returns:
        `Tuple[List[RiseScore], int]`
    """
    ignore = {
        (m.song_id, m.level_index)
        for m in b50_list
        if m.achievements >= 100.5
    }
    if not b50_list:
        if not fallback_ra:
            return [], 0
        ra = int(fallback_ra)
    else:
        ra = min(int(c.ra) for c in b50_list)

    if score is None:
        ss_ds = round(ra / 20.8, 1)
    else:
        ss_ds = round((ra + int(score)) / 20.8, 1)
    sssp_ds = round(ra / 22.4, 1)
    ds = (round(sssp_ds + 0.1, 1), round(ss_ds + 0.1, 1))

    # 新旧版本判定与 b50 完全一致：按曲库 is_new(新曲) 标记（_music_is_new 带版本名兜底）。
    # chart_type=='DX' 对应「新版本列」(B15)，=='SD' 对应「旧版本列」(B35)。
    want_new = chart_type == 'DX'
    candidate = [
        m for m in mai.total_list.filter(level=level, ds=ds)
        if _music_is_new(m) == want_new
    ]
    music = _pick_rise_scores(
        old_records, candidate, ignore=ignore, ra=ra, score=score,
    )

    if not music:
        return music, ra if (b50_list or fallback_ra) else 0
    new = random.sample(music, min(len(music), 5))
    new.sort(key=lambda x: x.song_id, reverse=True)
    return new, ra


async def rise_score_data(
    qqid: int, 
    username: Optional[str] = None, 
    level: Optional[str] = None, 
    score: Optional[int] = None
) -> Union[MessageSegment, str]:
    """
    上分数据
    
    Params:
        `qqid`: 用户QQ
        `username`: 查分器用户名
        `level`: 定数
        `score`: 分数
    Returns:
        `Union[Image.Image, str]`
    """
    try:
        from .maimaidx_datasource import get_user_records
        user, records = await get_user_records(qqid=qqid, username=username)
        old_records: DefaultDict[int, Dict[int, float]] = defaultdict(dict)
        for m in records:
            old_records[m.song_id][m.level_index] = m.achievements
        
        # chart_type 谱面类型；charts.sd/dx 对应 B35/B15（旧版本/新版本成绩）
        sd_list = (user.charts and user.charts.sd) or []
        dx_list = (user.charts and user.charts.dx) or []
        b35_scores, b35_low = get_rise_score_list(
            old_records, 'SD', sd_list, level, score,
        )
        b15_scores, b15_low = get_rise_score_list(
            old_records, 'DX', dx_list, level, score,
            fallback_ra=b35_low or None,
        )
        if not b35_scores and not b15_scores:
            return '没有推荐的铺面'
        h = max(len(b35_scores), len(b15_scores))
        height = h * 140 + 110 + 150
        image = tricolor_gradient(1400, height)
        ds = DrawScore(image)
        im = ds.draw_rise(b35_scores, b35_low, b15_scores, b15_low)
        
        msg = MessageSegment.image(image_to_base64(im.crop((200, 0, 1200, height))))
    except (UserNotFoundError, UserNotExistsError, UserDisabledQueryError) as e:
        msg = str(e)
    except Exception as e:
        log.error(traceback.format_exc())
        msg = format_command_error(e)
        
    return msg


def plate_message(
    result: str, 
    plan: str, 
    music_list: List[PlayInfoDefault], 
    played: List[Tuple[int, int]]
) -> Union[MessageSegment, str]:
    """
    Params:
        `result`: 结果
        `plan`: 目标
        `music_list`: 谱面列表
        `played`: 已游玩谱面
    Returns:
        `Union[MessageSegment, str]`
    """
    for n, m in enumerate(music_list):
        self_record = ''
        if (m.song_id, m.level_index) in played:
            if plan in ['将', '者']:
                self_record = f'{m.achievements}%'
            if plan in ['極', '极', '神']:
                self_record = m.fc
            if plan in '舞舞':
                self_record = m.fs
        result += f'No.{n + 1:02d} {f"「{m.song_id}」":>7} {f"「{diffs[m.level_index]}」":>11} 「{m.ds:.1f}」 {m.title}  {self_record}\n'
    if len(music_list) > 10:
        result = MessageSegment.image(text_to_bytes_io((result.strip())))
    return result


async def player_plate_data(
    qqid: int, 
    username: str, 
    version: str, 
    plan: str
) -> Union[MessageSegment, str]:
    """
    查看牌子进度
    
    Params:
        `qqid`: 用户QQ
        `username`: 查分器用户名
        `ver`: 版本
        `plan`: 目标
    Returns:
        `Union[MessageSegment, str]`
    """
    if version in platecn:
        version = platecn[version]
    ver, _ver = version_map.get(version, ([plate_to_dx_version.get(version)], version))
    
    try:
        from .maimaidx_datasource import get_user_records
        _userinfo, verlist = await get_user_records(
            qqid=qqid, username=username
        )
    except (UserNotFoundError, UserNotExistsError, UserDisabledQueryError) as e:
        return str(e)
    
    if plan in ['将', '者']:
        achievement = 100 if plan == '将' else 80
        callable_: Condition = lambda x: x.achievements < achievement
    elif plan in ['極', '极']:
        callable_: Condition = lambda x: not x.fc
    elif plan == '舞舞':
        callable_: Condition = lambda x: x.fs not in ['fsd', 'fsdp']
    elif plan  == '神':
        callable_: Condition = lambda x: x.fc not in ['ap', 'app']
    else:
        raise ValueError
    
    unfinished_model_list: Filter = ([], [], [], [], [])
    unfinished: List[Tuple[int, int]] = []
    played: List[Tuple[int, int]] = []
    remaster: List[int] = []
    
    # 已游玩未完成曲目
    plate_id_list = resolve_plate_id_list(mai.total_plate_id_list, _ver)
    if not plate_id_list:
        return f'未找到版本 {version}（{_ver}）的牌子曲目列表，请稍后重试或联系管理员更新牌子数据。'
    if _ver in ['舞', '霸']:
        remaster = mai.total_plate_id_list['舞ReMASTER']
        for music in verlist:
            if music.song_id not in plate_id_list:
                continue
            if music.level_index == 4 and music.song_id not in remaster:
                continue
            if callable_(music):
                unfinished.append((music.song_id, music.level_index))
            played.append((music.song_id, music.level_index))
    else:
        for music in verlist:
            if music.song_id not in plate_id_list:
                continue
            if callable_(music):
                unfinished.append((music.song_id, music.level_index))
            played.append((music.song_id, music.level_index))
    
    # 未游玩未完成曲目
    for music in mai.total_list:
        if int(music.id) not in plate_id_list:
            continue
        info = PlayInfoDefault(
            achievements=0,
            level='',
            level_index=0,
            title=music.title,
            type=music.type,
            id=int(music.id)
        )
        range_ = range(5 if version in ['舞', '霸'] and int(music.id) in remaster else 4)
        for level_index in range_:
            if (m := (info.song_id, level_index)) not in played or m in unfinished:
                _info = info.model_copy()
                _info.level = music.level[level_index]
                _info.ds = round(float(music.ds[level_index]), 1)
                _info.level_index = level_index
                unfinished_model_list[level_index].append(_info)

    basic, advanced, expert, master, re_master = unfinished_model_list
    
    ramain = basic + advanced + expert + master + re_master
    ramain.sort(key=lambda x: x.ds, reverse=True)
    difficult = [_m for _m in ramain if _m.ds > 13.6]

    appellation = username if username else '您'
    result = dedent(f'''\
        {appellation}的「{version}{plan}」剩余进度如下：
        Basic剩余「{len(basic)}」首
        Advanced剩余「{len(advanced)}」首
        Expert剩余「{len(expert)}」首
        Master剩余「{len(master)}」首
    ''')
    if version in ['舞', '霸']:
        result += f'Re:Master剩余「{len(re_master)}」首\n'
    
    if len(difficult) > 0:
        if len(difficult) < 60:
            result += '剩余定数大于13.6的曲目：\n'
            result = plate_message(result, plan, difficult, played)
        else:
            result += f'还有{len(difficult)}首大于13.6定数的曲目，加油推分捏！\n'
    elif len(ramain) > 0:
        if len(ramain) < 60:
            result += '剩余曲目：\n'
            result = plate_message(result, plan, ramain, played)
        else:
            result += '已经没有定数大于13.6的曲目了，加油清谱捏！\n'
    else:
        result = f'已经没有剩余的的曲目了，恭喜{appellation}完成「{version}{plan}」！'
    return result


async def level_process_data(
    qqid: int, 
    username: Optional[str], 
    level: str, 
    plan: str, 
    category: str = 'default', 
    page: int = 1
) -> Union[MessageSegment, str]:
    """
    查看谱面等级进度

    Params:
        `qqid`: 用户QQ
        `username`: 查分器用户名
        `level`: 定数
        `plan`: 评价等级
    Returns:
        `Union[MessageSegment, str]`
    """
    try:
        from .maimaidx_datasource import get_user_records
        _userinfo, obj = await get_user_records(qqid=qqid, username=username)
        music = mai.total_list.by_plan(level)

        planlist = [0, 0, 0]
        plannum = 0
        if plan.lower() in scoreRank:
            plannum = 0
            planlist[0] = achievementList[scoreRank.index(plan.lower()) - 1]
        elif plan.lower() in comboRank:
            plannum = 1
            planlist[1] = comboRank.index(plan.lower())
        elif plan.lower() in syncRank:
            plannum = 2
            planlist[2] = syncRank.index(plan.lower())
        else:
            raise
        
        plan_value = planlist[plannum]
        
        def is_completed(plannum: int, _d: Union[PlayInfoDefault, PlayInfoDev]) -> bool:
            if plannum == 0:
                return _d.achievements >= plan_value
            if plannum == 1:
                return _fc_meets_plan(_d.fc, plan_value)
            if plannum == 2:
                return _fs_meets_plan(_d.fs, plan_value)
            return False
        
        for _d in obj:
            if isinstance(_d, PlayInfoDefault):
                _m = mai.total_list.by_id(_d.song_id)
                ds: float = _m.ds[_d.level_index]
                a: float = _d.achievements
                ra, rate = computeRa(ds, a, israte=True)
                _d.ra = ra
                _d.rate = rate
            if (song_id := str(_d.song_id)) in music and _d.level == level:
                if isinstance(music[song_id], Dict):
                    music[song_id][_d.level_index] = PlanInfo()
                    _p = music[song_id][_d.level_index]
                else:
                    music[song_id] = PlanInfo()
                    _p = music[song_id]
                
                if is_completed(plannum, _d):
                    _p.completed = _d
                else:
                    _p.unfinished = _d

        notplayed: List[RaMusic] = []
        completed: Union[List[PlayInfoDefault], List[PlayInfoDev]] = []
        unfinished: Union[List[PlayInfoDefault], List[PlayInfoDev]] = []
        for m in music:
            play = music[m]
            if isinstance(play, Dict):
                for index, p in play.items():
                    if isinstance(p, RaMusic):
                        notplayed.append(p)
                    elif p.completed:
                        completed.append(p.completed)
                    elif p.unfinished:
                        unfinished.append(p.unfinished)
            elif isinstance(play, PlanInfo):
                if play.completed:
                    completed.append(play.completed)
                if play.unfinished:
                    unfinished.append(play.unfinished)
            else:
                notplayed.append(play)
        completed.sort(key=lambda x: x.achievements if plannum == 0 else x.fc if plannum == 1 else x.fs, reverse=True)
        unfinished.sort(key=lambda x: x.achievements if plannum == 0 else x.fc if plannum == 1 else x.fs, reverse=True)
        notplayed.sort(key=lambda x: x.ds, reverse=True)

        if category == 'default':
            completed_len = 60 if len(unfinished) == 0 and len(notplayed) == 0 else 30
            clen = len(completed[:completed_len])
            completed_y = (clen // 5 + (0 if clen % 5 == 0 else 1)) * 109 + 140
            ulen = len(unfinished[:30])
            unfinished_y = (ulen // 5 + (0 if ulen % 5 == 0 else 1)) * 109 + 140
            nlen = len(notplayed[:100])
            notstarted_y = (nlen // 20 + (0 if nlen % 20 == 0 else 1)) * 65 + 140
            image = tricolor_gradient(1400, 150 + completed_y + unfinished_y + notstarted_y)
            dp = DrawScore(image)
            im = dp.draw_plan(completed, completed_y, unfinished, unfinished_y, notplayed, plan, completed_len)
        elif category == 'completed' or category == 'unfinished':
            data = completed if category == 'completed' else unfinished
            lendata = len(data)
            end_page_num = lendata // 80 + 1
            if page > end_page_num:
                return f'超出页数，您的成绩共计「{end_page_num}」页，请重新输入'
            topage = len(data[(page - 1) * 80: page * 80])
            plc = (topage // 5 + (0 if topage % 5 == 0 else 1)) * 109
            image = tricolor_gradient(1400, 240 + plc + 120)
            dp = DrawScore(image)
            im = dp.draw_category(category, data, page, end_page_num)
        else:
            lennotstarted = len(notplayed)
            pln = (lennotstarted // 20 + (0 if lennotstarted % 20 == 0 else 1)) * 65
            image = tricolor_gradient(1400, 240 + pln + 120)
            dp = DrawScore(image)
            im = dp.draw_category(category, notplayed)
        
        msg = MessageSegment.image(image_to_base64(im))
    except (UserNotFoundError, UserNotExistsError, UserDisabledQueryError) as e:
        msg = str(e)
    except Exception as e:
        log.error(traceback.format_exc())
        msg = format_command_error(e)
    return msg


async def level_achievement_list_data(
    qqid: int, 
    username: Optional[str], 
    rating: Union[str, float], 
    page: int = 1
) -> Union[MessageSegment, str]:
    """
    查看分数列表

    Params:
        `qqid` : 用户QQ
        `username` : 查分器用户名
        `rating` : 定数
        `page` : 页数
        `nickname` : 用户昵称
    Returns:
        `Union[MessageSegment, str]
    """
    try:
        from .maimaidx_datasource import get_user_records
        _userinfo, records = await get_user_records(
            qqid=qqid, username=username
        )
        data: Union[List[PlayInfoDefault], List[PlayInfoDev]] = list(records)

        if isinstance(rating, str):
            newdata = sorted(list(filter(lambda x: x.level == rating, data)), key=lambda z: z.achievements, reverse=True)
        else:
            newdata = sorted(list(filter(lambda x: x.ds == rating, data)), key=lambda z: z.achievements, reverse=True)
        
        lendata = len(newdata)
        end_page_num = lendata // 80 + 1
        if page > end_page_num:
            return f'超出页数，您的成绩共计「{end_page_num}」页，请重新输入'
        
        topage = len(newdata[(page - 1) * 80: page * 80])
        line = topage // 5 + (0 if topage % 5 == 0 else 1)
        if page < end_page_num:
            plc = line * 109 + 140 * 4
        elif topage <= 20:
            plc = 4 * 109 + 140
        elif topage <= 40:
            plc = line * 109 + 140 * 2
        elif topage <= 60:
            plc = line * 109 + 140 * 3
        else:
            plc = line * 109 + 140 * 4
        
        image = tricolor_gradient(1400, 150 + plc)

        sc = DrawScore(image)
        im = sc.draw_scorelist(rating, newdata, page, end_page_num)
        msg = MessageSegment.image(image_to_base64(im))
    except (UserNotFoundError, UserNotExistsError, UserDisabledQueryError) as e:
        msg = str(e)
    except Exception as e:
        log.error(traceback.format_exc())
        msg = format_command_error(e)
    return msg


async def rating_ranking_data(name: Optional[str], page: Optional[int]) -> Union[MessageSegment, str]:
    """
    查看查分器排行榜
    
    Params:
        `name`: 指定用户名
        `page`: 页数
    Returns:
        `Union[MessageSegment, str]`
    """
    try:
        rank_data = await maiApi.rating_ranking()

        _time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        if name != '':
            if name in [r.username.lower() for r in rank_data]:
                rank_index = [r.username.lower() for r in rank_data].index(name) + 1
                nickname = rank_data[rank_index - 1].username
                data = f'截止至 {_time}\n玩家 {nickname} 在查分器已注册用户ra排行第{rank_index}'
            else:
                data = '未找到该玩家'
        else:
            user_num = len(rank_data)
            msg = f'截止至 {_time}，查分器已注册用户ra排行：\n'
            if page * 50 > user_num:
                page = user_num // 50 + 1
            end = page * 50 if page * 50 < user_num else user_num
            for i, ranker in enumerate(rank_data[(page - 1) * 50:end]):
                msg += f'No.{i + 1 + (page - 1) * 50:02d}.「{ranker.ra}」 {ranker.username} \n'
            msg += f'第「{page}」页，共「{user_num // 50 + 1}」页'
            data = MessageSegment.image(text_to_bytes_io((msg.strip())))
    except Exception as e:
        log.error(traceback.format_exc())
        data = format_command_error(e)
    return data
