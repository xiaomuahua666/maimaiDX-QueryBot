import asyncio
import json
import random
from collections import defaultdict
from copy import deepcopy
from typing import Dict, List, Optional, Set, Tuple, Union

import numpy as np
from loguru import logger as log
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps

from ..config import *
from .image import image_to_base64, music_picture
from .maimaidx_api_data import maiApi
from .maimaidx_error import *
from .maimaidx_model import *
from .maimaidx_guess_audio import (
    STAGE_COUNT,
    ensure_audio_ready,
    is_audio_ready,
    list_stage_files,
    set_audio_prepare_status,
)
from .maimaidx_guess_chart import (
    DEFAULT_DURATION as CHART_DEFAULT_DURATION,
    PHASE2_DURATION as CHART_PHASE2_DURATION,
    CHART_DIFF_NAMES,
    bgm_video_path_for,
    ensure_chart_video_ready,
    is_chart_bgm_ready,
    is_chart_video_ready,
    list_ready_chart_rounds,
    pick_chart_diff,
    chart_kind as resolve_chart_kind,
    set_chart_prepare_status,
    video_path_for,
)
from .tool import openfile, writefile


def cross(
    checker: Union[List[str], List[float]], 
    elem: Optional[Union[str, float, List[str], List[float], Tuple[float, float]]], 
    diff: List[int]
) -> Tuple[bool, List[int]]:
    ret = False
    diff_ret = []
    if not elem or elem is Ellipsis:
        return True, diff
    if isinstance(elem, List):
        for _j in (range(len(checker)) if diff is Ellipsis else diff):
            if _j >= len(checker):
                continue
            __e = checker[_j]
            if __e in elem:
                diff_ret.append(_j)
                ret = True
    elif isinstance(elem, Tuple):
        for _j in (range(len(checker)) if diff is Ellipsis else diff):
            if _j >= len(checker):
                continue
            __e = checker[_j]
            if elem[0] <= __e <= elem[1]:
                diff_ret.append(_j)
                ret = True
    else:
        for _j in (range(len(checker)) if diff is Ellipsis else diff):
            if _j >= len(checker):
                continue
            __e = checker[_j]
            if elem == __e:
                diff_ret.append(_j)
                ret = True
    return ret, diff_ret


def in_or_equal(
    checker: Union[str, int], 
    elem: Optional[Union[str, float, List[str], List[float], Tuple[float, float]]]
) -> bool:
    if elem is Ellipsis:
        return True
    if isinstance(elem, List):
        return checker in elem
    elif isinstance(elem, Tuple):
        return elem[0] <= checker <= elem[1]
    else:
        return checker == elem


class MusicList(List[Music]):

    def _id_index(self) -> Dict[str, Music]:
        # 惰性 id→Music 索引：by_id 在全成绩循环里逐条调用（O(records×songs)
        # 的线性扫热点）。列表经 append 构建、刷新时整体替换，按长度失效即可。
        cached = getattr(self, '_id_cache', None)
        if cached is not None and cached[0] == len(self):
            return cached[1]
        index: Dict[str, Music] = {}
        for music in self:
            index.setdefault(music.id, music)  # 与线性扫一致：重复 id 取首个
        self._id_cache = (len(self), index)
        return index

    def by_id(self, music_id: Union[str, int]) -> Optional[Music]:
        return self._id_index().get(str(music_id))

    def by_title(self, music_title: str) -> Optional[Music]:
        for music in self:
            if music.title == music_title:
                return music
        return None
    
    def by_plan(
        self, 
        level: str
    ) -> Dict[str, Union[PlanInfo, RaMusic, Dict[int, Union[PlanInfo, RaMusic]]]]:
        lv = defaultdict(dict)
        
        def create_ra_music(music: Music, index: int) -> RaMusic:
            return RaMusic(
                id=music.id, 
                ds=music.ds[index], 
                lv=str(index), 
                lvp=music.level[index], 
                type=music.type
            )
        
        for music in self:
            if level not in music.level:
                continue
            if int(music.id) >= 100000:
                continue
            if music.level.count(level) > 1: # 同曲有相同等级
                lv[music.id] = { 
                    index: create_ra_music(music, index)
                    for index, _lv in enumerate(music.level) 
                    if _lv == level 
                }
            else:
                index = music.level.index(level)
                lv[music.id] = create_ra_music(music, index)
        return dict(lv)
    
    def by_level_list(self) -> Dict[str, Dict[str, List[RaMusic]]]:
        
        def level_range(lv: str) -> range:
            if lv == '15':
                return range(1)
            if lv.endswith('+'):
                return range(9, 5, -1)
            return range(9, -1, -1) if int(lv) <= 5 else range(5, -1, -1)
        
        _level = {
            lv: {f"{lv.rstrip('+')}.{i}": [] for i in level_range(lv)} for lv in levelList
        }
        for music in self:
            if int(music.id) >= 100000:
                continue
            for index, ds in enumerate(music.ds):
                if ds < 7:
                    continue
                ra = RaMusic(
                    id=music.id,
                    ds=ds,
                    lv=str(index),
                    lvp=music.level[index],
                    type=music.type
                )
                _level[music.level[index]][str(ds)].append(ra)
        return _level
    
    def by_id_list(self, music_id_list: List[int]) -> Optional[List[Music]]:
        musicList = []
        for music in self:
            if int(music.id) in music_id_list:
                musicList.append(music)
        return musicList
    
    def random(self) -> Music:
        return random.choice(self)

    def filter(
        self,
        *,
        level: Optional[Union[str, List[str]]] = ...,
        ds: Optional[Union[float, List[float], Tuple[float, float]]] = ...,
        title_search: Optional[str] = ...,
        artist_search: Optional[str] = ...,
        charter_search: Optional[str] = ...,
        genre: Optional[Union[str, List[str]]] = ...,
        bpm: Optional[Union[float, List[float], Tuple[float, float]]] = ...,
        type: Optional[Union[str, List[str]]] = ...,  # 谱面类型 SD=标准谱面 DX=DX谱面
        diff: List[int] = ...,
        version: Union[str, List[str]] = ...
    ) -> 'MusicList':
        new_list = MusicList()
        for music in self:
            diff2 = diff
            music = deepcopy(music)
            ret, diff2 = cross(music.level, level, diff2)
            if not ret:
                continue
            ret, diff2 = cross(music.ds, ds, diff2)
            if not ret:
                continue
            ret, diff2 = search_charts(music.charts, charter_search, diff2)
            if not ret:
                continue
            if not in_or_equal(music.basic_info.genre, genre):
                continue
            if not in_or_equal(music.type, type):
                continue
            if not in_or_equal(music.basic_info.bpm, bpm):
                continue
            if not in_or_equal(music.basic_info.version, version):
                continue
            if title_search is not Ellipsis and title_search.lower() not in music.title.lower():
                continue
            if artist_search is not Ellipsis and artist_search.lower() not in music.basic_info.artist.lower():
                continue
            music.diff = diff2
            new_list.append(music)
        return new_list


def search_charts(checker: List[Chart], elem: str, diff: List[int]) -> Tuple[bool, List[int]]:
    ret = False
    diff_ret = []
    if not elem or elem is Ellipsis:
        return True, diff
    for _j in (range(len(checker)) if diff is Ellipsis else diff):
        if elem.lower() in checker[_j].charter.lower():
            diff_ret.append(_j)
            ret = True
    return ret, diff_ret


class AliasList(List[Alias]):

    def by_id(self, music_id: Union[str, int]) -> Optional[List[Alias]]:
        alias_music = []
        for music in self:
            if music.SongID == int(music_id):
                alias_music.append(music)
        return alias_music
    
    def by_alias(self, music_alias: str) -> Optional[List[Alias]]:
        alias_list = []
        for music in self:
            if music_alias in music.Alias:
                alias_list.append(music)
        return alias_list


dataerror = dedent(f'''
    未找到文件，请自行使用浏览器访问 "https://www.diving-fish.com/api/maimaidxprober/music_data" 
    将内容保存为 "music_data.json" 存放在 "static" 目录下并重启bot
''').strip()
charterror = dedent(f'''
    未找到文件，请自行使用浏览器访问 "https://www.diving-fish.com/api/maimaidxprober/chart_stats"
    将内容保存为 "music_chart.json" 存放在 "static" 目录下并重启bot
''').strip()
aliaserror = dedent('''
    本地暂存别名文件为空，请自行使用浏览器访问 "https://www.yuzuchan.moe/api/maimaidx/maimaidxalias" 
    获取别名数据并保存在 "static/music_alias.json" 文件中并重启bot
''').strip()


async def get_music_list(force: bool = False) -> MusicList:
    """获取所有数据。force=True 时忽略本地缓存，强制从网络刷新。"""
    from .maimaidx_data_source import get_data_source
    from .tool import is_cache_fresh
    datasource = get_data_source()
    cache_ttl = 0 if force else getattr(maiconfig, 'maimaidx_music_cache_seconds', 3600)

    # MusicData
    try:
        if is_cache_fresh(music_file, cache_ttl):
            log.opt(colors=True).info('曲库数据使用<g>本地缓存</g>（未过期，跳过网络请求）')
            music_data = await openfile(music_file)
        else:
            try:
                music_data = await datasource.get_music_data()
                await writefile(music_file, music_data)
            except asyncio.exceptions.TimeoutError:
                log.error('maimaiDX曲库数据获取失败，请检查网络环境。已切换至本地暂存文件')
                music_data = await openfile(music_file)
    except FileNotFoundError:
        log.error(dataerror)
        raise FileNotFoundError

    # ChartStats
    try:
        if is_cache_fresh(chart_file, cache_ttl):
            chart_stats = await openfile(chart_file)
        else:
            try:
                chart_stats = await datasource.get_chart_stats()
                await writefile(chart_file, chart_stats)
            except asyncio.exceptions.TimeoutError:
                log.error('maimaiDX数据获取错误，请检查网络环境，已切换至本地暂存文件')
                chart_stats = await openfile(chart_file)
    except FileNotFoundError:
        log.error(charterror)
        raise FileNotFoundError

    total_list = MusicList()
    for music in music_data:
        if music['id'] in chart_stats['charts']:
            _stats = [
                _data if _data else None
                for _data in chart_stats['charts'][music['id']]
            ] if {} in chart_stats['charts'][music['id']] else \
            chart_stats['charts'][music['id']]
        else:
            _stats = None
        total_list.append(Music(stats=_stats, **music))

    return total_list


async def get_music_alias_list(force: bool = False) -> AliasList:
    """获取所有别名。force=True 时忽略本地缓存，强制从网络刷新。"""
    from .tool import is_cache_fresh
    cache_ttl = 0 if force else getattr(maiconfig, 'maimaidx_music_cache_seconds', 3600)

    if local_alias_file.exists():
        local_alias_data = await openfile(local_alias_file)
    else:
        local_alias_data = {}
    alias_data: List[Dict[str, Union[int, str, List[str]]]] = []

    # 本地别名缓存未过期则直接使用，跳过网络请求
    if is_cache_fresh(alias_file, cache_ttl):
        try:
            alias_data = await openfile(alias_file)
        except FileNotFoundError:
            alias_data = []

    if not alias_data:
        try:
            alias_data = await maiApi.get_alias()
            await writefile(alias_file, alias_data)
        except asyncio.exceptions.TimeoutError:
            log.error('获取别名超时，已切换至本地暂存文件')
            alias_data = await openfile(alias_file)
            if not alias_data:
                log.error(aliaserror)
                raise ValueError
        except ServerError as e:
            log.error(str(e) + '。已切换至本地暂存文件')
            alias_data = await openfile(alias_file)
        except UnknownError:
            log.error('获取所有曲目别名信息错误，请检查网络环境。已切换至本地暂存文件')
            alias_data = await openfile(alias_file)
            if not alias_data:
                log.error(aliaserror)
                raise ValueError

    total_alias_list = AliasList()
    for _a in filter(lambda x: mai.total_list.by_id(x['SongID']), alias_data):
        if (song_id := str(_a['SongID'])) in local_alias_data:
            _a['Alias'].extend(local_alias_data[song_id])
        total_alias_list.append(Alias.model_validate(_a))

    return total_alias_list


async def update_local_alias(id: str, alias_name: str) -> bool:
    try:
        if local_alias_file.exists():
            local_alias_data: Dict[str, List[str]] = await openfile(local_alias_file)
        else:
            local_alias_data: Dict[str, List[str]] = {}
        if id not in local_alias_data:
            local_alias_data[id] = []
        
        local_alias_data[id].append(alias_name.lower())
        mai.total_alias_list.by_id(id)[0].Alias.append(alias_name.lower())
        await writefile(local_alias_file, local_alias_data)
        return True
    except Exception as e:
        log.error(f'添加本地别名失败: {e}')
        return False


class MaiMusic:

    total_list: MusicList
    """曲目数据"""
    total_alias_list: AliasList
    """别名数据"""
    total_plate_id_list: Dict[str, List[int]]
    """牌子ID列表数据"""
    total_level_data: Dict[str, Dict[str, List[RaMusic]]]
    """等级列表数据"""
    hot_music_ids: List = []
    """游玩次数超过1w次的曲目数据"""
    guess_data: List[Music]
    """猜歌数据"""

    def __init__(self) -> None:
        """封装所有曲目信息以及猜歌数据，便于更新"""

    async def get_music(self, force: bool = False) -> None:
        """获取所有曲目数据。force=True 时强制从网络刷新，忽略本地缓存。"""
        self.total_list = await get_music_list(force=force)
        self.total_level_data = self.total_list.by_level_list()
        from .maimaidx_best_50 import invalidate_b15_version_cache
        invalidate_b15_version_cache()

    async def get_music_alias(self, force: bool = False) -> None:
        """获取所有曲目别名。force=True 时强制从网络刷新，忽略本地缓存。"""
        self.total_alias_list = await get_music_alias_list(force=force)
        
    async def get_plate_json(self) -> None:
        """获取所有牌子数据"""
        self.total_plate_id_list = await maiApi.get_plate_json()

    def guess(self):
        """初始化猜歌数据"""
        for music in self.total_list:
            if music.stats:
                count = 0
                for stats in music.stats:
                    if stats:
                        count += stats.cnt if stats.cnt else 0
                if count > 10000:
                    self.hot_music_ids.append(music.id)
        self.guess_data = list(filter(lambda x: x.id in self.hot_music_ids, self.total_list))


mai = MaiMusic()


class Guess:
    
    Group: Dict[
        Union[int, str],
        Union[GuessDefaultData, GuessPicData, GuessAudioData, GuessChartData],
    ] = {}
    Preparing: Set[Union[int, str]] = set()
    _gid_locks: Dict[Union[int, str], asyncio.Lock] = {}
    switch: GuessSwitch

    def __init__(self) -> None:
        """猜歌类"""
        if not guess_file.exists():
            self.switch = GuessSwitch()
        else:
            self.switch = GuessSwitch.model_validate(
                json.load(open(guess_file, 'r', encoding='utf-8'))
            )

    @classmethod
    def _lock(cls, gid: Union[int, str]) -> asyncio.Lock:
        if gid not in cls._gid_locks:
            cls._gid_locks[gid] = asyncio.Lock()
        return cls._gid_locks[gid]

    def is_busy(self, gid: Union[int, str]) -> bool:
        return gid in self.Group or gid in self.Preparing

    async def try_begin_prepare(self, gid: Union[int, str]) -> bool:
        async with self._lock(gid):
            if self.is_busy(gid):
                return False
            self.Preparing.add(gid)
            return True

    def end_prepare(self, gid: Union[int, str]) -> None:
        self.Preparing.discard(gid)

    def _log_guess_start(self, mode: str, gid: Union[int, str]) -> None:
        data = self.Group[gid]
        music = data.music
        log.info(
            f'[Guess] 开始{mode}！本次答案： {music.title} 。ID: {music.id} 。'
        )

    def start(self, gid: Union[int, str]):
        """开始猜歌"""
        self.Group[gid] = self.guessData()
        self._log_guess_start('猜歌', gid)

    def startpic(self, gid: Union[int, str], difficulty: Optional[int] = None):
        """开始猜曲绘；difficulty 为 1～4，省略则随机。"""
        self.Group[gid] = self.guesspicdata(difficulty)
        self._log_guess_start('猜曲绘', gid)
        
    def calculate_frequency_weights(self, image: Image.Image) -> np.ndarray:
        """
        计算图像的频率权重，用于在图像中选择裁剪区域
        
        Params:
            `image`: PIL.Image.Image, 输入图像
        Returns:
            `np.ndarray` 频率权重矩阵
        """
        gray_image = np.array(image.convert('L'))
        freq = np.fft.fft2(gray_image)
        freq_shift = np.fft.fftshift(freq)
        magnitude = np.abs(freq_shift)
        normalized_magnitude = magnitude / magnitude.max()
        weights = normalized_magnitude ** 2
        return weights

    def select_crop_region(
        self, 
        weights: np.ndarray, 
        crop_width: int, 
        crop_height: int, 
        top_p: int
    ) -> Tuple[int, int]:
        h, w = weights.shape
        valid_regions = weights[:h - crop_height + 1, :w - crop_width + 1]
        flattened_weights = valid_regions.flatten()
        threshold = np.percentile(flattened_weights, top_p)
        valid_indices = np.where(flattened_weights >= threshold)[0]
        probabilities = flattened_weights[valid_indices]
        probabilities /= probabilities.sum()
        chosen_index = np.random.choice(valid_indices, p=probabilities)
        top_left_y = chosen_index // valid_regions.shape[1]
        top_left_x = chosen_index % valid_regions.shape[1]
        return top_left_x, top_left_y
    
    PIC_INTERFERENCE = [
        ('hue', '色相'),
        ('invert', '反转'),
        ('blur', '模糊'),
        ('desaturate', '低饱和'),
        ('saturate', '高饱和'),
        ('mirror', '水平镜像'),
        ('flip', '垂直翻转'),
        ('pixelate', '像素化'),
        ('noise', '噪点'),
        ('low_contrast', '低对比'),
        ('overexpose', '过曝'),
        ('underexpose', '欠曝'),
        ('rotate', '旋转'),
        ('emboss', '浮雕'),
        ('solarize', '曝光'),
        ('posterize', '色阶'),
        ('edge', '边缘'),
        ('contour', '轮廓'),
        ('channel_shift', '色差'),
        ('glitch', '故障'),
        ('scanlines', '扫描线'),
        ('vignette', '暗角'),
        ('sepia', '复古'),
        ('tiles', '分块'),
        ('wave', '波浪'),
        ('oil', '油画'),
        ('rainbow', '彩虹'),
        ('cold', '冷色'),
        ('warm', '暖色'),
        ('sharpen', '锐化'),
        ('equalize', '均衡'),
        ('halftone', '网点'),
    ]

    PIC_SOLO_INTERFERENCE = {'pixelate', 'emboss', 'edge', 'contour', 'tiles', 'halftone'}

    PIC_INTERFERENCE_COUNT = {
        1: (1, 2),
        2: (2, 3),
        3: (2, 4),
        4: (3, 5),
    }

    PIC_DIFFICULTY = {
        1: {'initial': 0.12, 'max': 0.50, 'expansions': 3},
        2: {'initial': 0.09, 'max': 0.42, 'expansions': 4},
        3: {'initial': 0.07, 'max': 0.35, 'expansions': 4},
        4: {'initial': 0.05, 'max': 0.28, 'expansions': 5},
    }

    def _pick_pic_interferences(self, difficulty: int) -> Tuple[List[str], List[str]]:
        count_range = self.PIC_INTERFERENCE_COUNT[difficulty]
        pick_count = random.randint(*count_range)
        selected = random.sample(self.PIC_INTERFERENCE, pick_count)
        keys = [key for key, _ in selected]
        label_map = dict(self.PIC_INTERFERENCE)

        solo_hits = [key for key in keys if key in self.PIC_SOLO_INTERFERENCE]
        if solo_hits:
            solo = random.choice(solo_hits) if len(solo_hits) > 1 else solo_hits[0]
            return [solo], [label_map[solo]]

        return keys, [label_map[key] for key in keys]

    def _get_pic_crop_box(
        self,
        cx: int,
        cy: int,
        scale: float,
        full_w: int,
        full_h: int,
    ) -> Tuple[int, int, int, int]:
        w2, h2 = int(full_w * scale), int(full_h * scale)
        x = max(0, min(cx - w2 // 2, full_w - w2))
        y = max(0, min(cy - h2 // 2, full_h - h2))
        return x, y, w2, h2

    def _apply_single_pic_interference(self, im: Image.Image, kind: str) -> Image.Image:
        if kind == 'hue':
            hsv = im.convert('HSV')
            h, s, v = hsv.split()
            s = s.point(lambda x: int(x * 0.12))
            h = h.point(lambda x: (x + 48) % 256)
            return Image.merge('HSV', (h, s, v)).convert('RGB')
        if kind == 'invert':
            return ImageOps.invert(im)
        if kind == 'blur':
            return im.filter(ImageFilter.GaussianBlur(radius=4))
        if kind == 'desaturate':
            return ImageEnhance.Color(im).enhance(0.0)
        if kind == 'saturate':
            return ImageEnhance.Color(im).enhance(2.5)
        if kind == 'mirror':
            return ImageOps.mirror(im)
        if kind == 'flip':
            return ImageOps.flip(im)
        if kind == 'pixelate':
            w, h = im.size
            small = im.resize((max(1, w // 12), max(1, h // 12)), Image.NEAREST)
            return small.resize((w, h), Image.NEAREST)
        if kind == 'noise':
            arr = np.array(im, dtype=np.int16)
            noise = np.random.randint(-40, 41, arr.shape, dtype=np.int16)
            arr = np.clip(arr + noise, 0, 255).astype(np.uint8)
            return Image.fromarray(arr)
        if kind == 'low_contrast':
            return ImageEnhance.Contrast(im).enhance(0.35)
        if kind == 'overexpose':
            return ImageEnhance.Brightness(im).enhance(1.8)
        if kind == 'underexpose':
            return ImageEnhance.Brightness(im).enhance(0.35)
        if kind == 'rotate':
            return im.rotate(random.choice([-90, 90, 180]), expand=False)
        if kind == 'emboss':
            return im.filter(ImageFilter.EMBOSS)
        if kind == 'solarize':
            return ImageOps.solarize(im, threshold=128)
        if kind == 'posterize':
            return ImageOps.posterize(im, bits=3)
        if kind == 'edge':
            return im.filter(ImageFilter.FIND_EDGES)
        if kind == 'contour':
            return im.filter(ImageFilter.CONTOUR)
        if kind == 'channel_shift':
            r, g, b = im.split()
            shift = max(2, min(im.size) // 30)
            r = r.transform(im.size, Image.AFFINE, (1, 0, shift, 0, 1, 0))
            b = b.transform(im.size, Image.AFFINE, (1, 0, -shift, 0, 1, 0))
            return Image.merge('RGB', (r, g, b))
        if kind == 'glitch':
            arr = np.array(im)
            h, w = arr.shape[:2]
            band = max(4, h // 12)
            for y in range(0, h, band):
                offset = random.randint(-w // 6, w // 6)
                arr[y:y + band] = np.roll(arr[y:y + band], offset, axis=1)
            return Image.fromarray(arr)
        if kind == 'scanlines':
            arr = np.array(im, dtype=np.int16)
            arr[::3] = (arr[::3] * 0.45).astype(np.int16)
            return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
        if kind == 'vignette':
            arr = np.array(im, dtype=np.float32)
            h, w = arr.shape[:2]
            yy, xx = np.ogrid[:h, :w]
            cy, cx = (h - 1) / 2.0, (w - 1) / 2.0
            dist = np.sqrt((yy - cy) ** 2 + (xx - cx) ** 2)
            max_dist = np.sqrt(cy ** 2 + cx ** 2) or 1.0
            mask = np.clip(1.0 - (dist / max_dist) ** 1.6 * 1.15, 0.12, 1.0)
            arr *= mask[..., None]
            return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
        if kind == 'sepia':
            arr = np.array(im, dtype=np.float32)
            r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
            tr = np.clip(0.393 * r + 0.769 * g + 0.189 * b, 0, 255)
            tg = np.clip(0.349 * r + 0.686 * g + 0.168 * b, 0, 255)
            tb = np.clip(0.272 * r + 0.534 * g + 0.131 * b, 0, 255)
            return Image.fromarray(np.stack([tr, tg, tb], axis=-1).astype(np.uint8))
        if kind == 'tiles':
            w, h = im.size
            cols = rows = 4
            tw, th = w // cols, h // rows
            if tw < 2 or th < 2:
                return im
            tiles = []
            for row in range(rows):
                for col in range(cols):
                    box = (col * tw, row * th, (col + 1) * tw, (row + 1) * th)
                    tiles.append(im.crop(box))
            random.shuffle(tiles)
            out = Image.new('RGB', (cols * tw, rows * th))
            for idx, tile in enumerate(tiles):
                out.paste(tile, ((idx % cols) * tw, (idx // cols) * th))
            if out.size != im.size:
                canvas = Image.new('RGB', im.size)
                canvas.paste(out, (0, 0))
                return canvas
            return out
        if kind == 'wave':
            arr = np.array(im)
            h, w = arr.shape[:2]
            yy, xx = np.mgrid[:h, :w]
            amp = max(3, min(w, h) // 18)
            src_x = np.clip(xx + (amp * np.sin(2 * np.pi * yy / max(12, h // 6))).astype(int), 0, w - 1)
            src_y = np.clip(yy + (amp * np.sin(2 * np.pi * xx / max(12, w // 6))).astype(int), 0, h - 1)
            return Image.fromarray(arr[src_y, src_x])
        if kind == 'oil':
            return im.filter(ImageFilter.ModeFilter(size=7))
        if kind == 'rainbow':
            hsv = im.convert('HSV')
            h, s, v = hsv.split()
            w, _ = im.size
            lut = [int((i / max(w - 1, 1)) * 255) for i in range(w)]
            h_arr = np.array(h)
            for x in range(w):
                h_arr[:, x] = lut[x]
            s = s.point(lambda x: max(x, 160))
            return Image.merge('HSV', (Image.fromarray(h_arr), s, v)).convert('RGB')
        if kind == 'cold':
            arr = np.array(im, dtype=np.int16)
            arr[..., 0] = arr[..., 0] * 0.65
            arr[..., 2] = np.clip(arr[..., 2] * 1.35 + 20, 0, 255)
            return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
        if kind == 'warm':
            arr = np.array(im, dtype=np.int16)
            arr[..., 0] = np.clip(arr[..., 0] * 1.35 + 15, 0, 255)
            arr[..., 2] = arr[..., 2] * 0.7
            return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
        if kind == 'sharpen':
            out = im
            for _ in range(3):
                out = out.filter(ImageFilter.SHARPEN)
            return out
        if kind == 'equalize':
            return ImageOps.equalize(im)
        if kind == 'halftone':
            gray = im.convert('L')
            dithered = gray.convert('1')
            return dithered.convert('RGB')
        return im

    def _apply_pic_interference(self, im: Image.Image, kinds: List[str]) -> Image.Image:
        im = im.convert('RGB')
        for kind in kinds:
            im = self._apply_single_pic_interference(im, kind)
        return im

    def _load_pic_source(self, data: GuessPicData) -> Image.Image:
        return Image.open(music_picture(data.music.id)).convert('RGB')

    def _render_pic_masked_region(
        self,
        data: GuessPicData,
        *,
        apply_interference: bool,
        output_size: int = 400,
        draw_border: bool = False,
    ) -> str:
        im = self._load_pic_source(data)
        x, y, w, h = self._get_pic_crop_box(
            data.crop_cx, data.crop_cy, data.current_scale, data.full_w, data.full_h
        )
        crop = im.crop((x, y, x + w, y + h))
        if apply_interference:
            crop = self._apply_pic_interference(crop, data.interferences)
        crop = crop.resize((output_size, output_size), Image.LANCZOS)
        if draw_border:
            draw = ImageDraw.Draw(crop)
            draw.rectangle(
                [1, 1, output_size - 2, output_size - 2],
                outline=(255, 255, 255),
                width=3,
            )
        return image_to_base64(crop)

    def render_pic_crop(self, data: GuessPicData, output_size: int = 400) -> str:
        return self._render_pic_masked_region(
            data, apply_interference=True, output_size=output_size
        )

    def render_pic_global(self, data: GuessPicData, max_width: int = 560) -> str:
        return self._render_pic_canvas_view(
            data, apply_interference=True, max_width=max_width
        )

    def render_pic_clear(self, data: GuessPicData, max_width: int = 560) -> str:
        return self._render_pic_canvas_view(
            data, apply_interference=False, max_width=max_width
        )

    def _render_pic_canvas_view(
        self,
        data: GuessPicData,
        *,
        apply_interference: bool,
        max_width: int = 560,
    ) -> str:
        im = self._load_pic_source(data)
        scale = max_width / im.width
        canvas_h = int(im.height * scale)
        canvas = Image.new('RGB', (max_width, canvas_h), (255, 255, 255))

        x, y, w, h = self._get_pic_crop_box(
            data.crop_cx, data.crop_cy, data.current_scale, data.full_w, data.full_h
        )
        region = im.crop((x, y, x + w, y + h))
        if apply_interference:
            region = self._apply_pic_interference(region, data.interferences)

        sx, sy = int(x * scale), int(y * scale)
        sw, sh = max(1, int(w * scale)), max(1, int(h * scale))
        region = region.resize((sw, sh), Image.LANCZOS)
        canvas.paste(region, (sx, sy))
        return image_to_base64(canvas)

    def render_pic_reveal(self, data: GuessPicData, max_width: int = 600) -> str:
        im = self._load_pic_source(data)
        x, y, w, h = self._get_pic_crop_box(
            data.crop_cx, data.crop_cy, data.initial_scale, data.full_w, data.full_h
        )
        region = im.crop((x, y, x + w, y + h))
        bright = ImageEnhance.Brightness(region).enhance(1.35)
        bright = ImageEnhance.Contrast(bright).enhance(1.15)
        im.paste(bright, (x, y))

        scale = max_width / im.width
        full_h = int(im.height * scale)
        result = im.resize((max_width, full_h), Image.LANCZOS)

        sx, sy = int(x * scale), int(y * scale)
        sw, sh = int(w * scale), int(h * scale)
        draw = ImageDraw.Draw(result)
        for offset in (0, 2):
            draw.rectangle(
                [sx - offset, sy - offset, sx + sw + offset, sy + sh + offset],
                outline=(255, 220, 50) if offset == 0 else (255, 255, 255),
                width=3,
            )
        return image_to_base64(result)

    def expand_pic_crop(self, data: GuessPicData) -> None:
        step = (data.max_scale - data.initial_scale) / data.expansion_count
        data.current_scale = min(data.current_scale + step, data.max_scale)

    def pic(self, music: Music) -> Image.Image:
        """裁切曲绘"""
        im = Image.open(music_picture(music.id))
        w, h = im.size
        weights = self.calculate_frequency_weights(im)
        scale = random.uniform(0.15, 0.4)  # 裁剪尺寸范围 可在此修改
        w2, h2 = int(w * scale), int(h * scale)
        top_p = min(1.3 - np.power(scale, 0.4), 0.95) * 100
        x, y = self.select_crop_region(weights, w2, h2, top_p)
        im = im.crop((x, y, x + w2, y + h2))
        return im

    UTAGE_GENRES = {'宴会場', '宴会场'}

    @staticmethod
    def _is_utage_music(music: Music) -> bool:
        try:
            if int(music.id) >= 100000:
                return True
        except (TypeError, ValueError):
            pass
        return music.basic_info.genre in Guess.UTAGE_GENRES

    def _guess_music_pool(self) -> List[Music]:
        pool = [m for m in mai.guess_data if not self._is_utage_music(m)]
        if pool:
            return pool
        log.warning('[Guess] 热门曲目过滤宴会场后为空，扩大至全库非宴会场曲目')
        return [m for m in mai.total_list if not self._is_utage_music(m)]

    def _pick_guess_music(self) -> Music:
        pool = self._guess_music_pool()
        if not pool:
            log.error('[Guess] 无可用非宴会场曲目，回退随机热门曲')
            return random.choice(mai.guess_data)
        return random.choice(pool)

    def _guess_audio_pool(self) -> List[Music]:
        ready = [m for m in self._guess_music_pool() if is_audio_ready(m.id)]
        return ready

    def guessaudiodata(self, music: Music) -> GuessAudioData:
        answer = mai.total_alias_list.by_id(music.id)[0].Alias
        answer.append(music.id)
        paths = [str(p.resolve()) for p in list_stage_files(music.id)]
        return GuessAudioData(
            music=music,
            img='',
            answer=answer,
            end=False,
            stage_paths=paths,
            stage_count=len(paths) or STAGE_COUNT,
        )

    async def prepare_audio_round(self) -> Optional[GuessAudioData]:
        """随机选曲并确保音频缓存可用。

        每局从完整曲池重新随机抽取，让缓存池能够在实际游玩中逐步扩充；如果
        抽中的新曲全部生成失败，再回退到已有缓存。否则只要缓存池中存在一首歌，
        后续每局都会只从这个固定池中抽取，缓存也永远没有机会增长。
        """
        ready_pool = self._guess_audio_pool()
        candidates = self._guess_music_pool()[:]
        random.shuffle(candidates)
        set_audio_prepare_status('随机选曲中…')
        for music in candidates[:12]:
            set_audio_prepare_status('准备音频资源…')
            ok, _msg = await ensure_audio_ready(music.id, title=music.title)
            if ok:
                set_audio_prepare_status('音频已就绪')
                return self.guessaudiodata(music)
        if ready_pool:
            log.warning('[GuessAudio] 新曲音频生成失败，回退到已有缓存')
            picked = random.choice(ready_pool)
            set_audio_prepare_status('回退已有缓存…')
            return self.guessaudiodata(picked)
        set_audio_prepare_status('无可用音频')
        return None

    def startaudio(self, gid: Union[int, str], data: GuessAudioData) -> None:
        self.Group[gid] = data
        self._log_guess_start('猜曲子', gid)

    def guesschartdata(
        self,
        music: Music,
        *,
        video_path: str,
        chart_kind: str,
        chart_diff: int,
        duration: int = CHART_DEFAULT_DURATION,
        video_path_bgm: str = '',
        bgm_duration: int = CHART_PHASE2_DURATION,
    ) -> GuessChartData:
        answer = mai.total_alias_list.by_id(music.id)[0].Alias
        answer.append(music.id)
        return GuessChartData(
            music=music,
            img='',
            answer=answer,
            end=False,
            video_path=video_path,
            video_path_bgm=video_path_bgm,
            chart_kind=chart_kind,
            chart_diff=chart_diff,
            chart_diff_name=CHART_DIFF_NAMES.get(chart_diff, str(chart_diff)),
            duration=duration,
            bgm_duration=bgm_duration,
            stage_count=2 if video_path_bgm else 1,
        )

    async def prepare_chart_round(self) -> Optional[GuessChartData]:
        """优先使用已预制的静音+BGM 缓存，避免开局现场渲染拖垮主功能。"""
        candidates = self._guess_music_pool()[:]
        random.shuffle(candidates)
        by_id = {str(m.id): m for m in candidates}

        # 1) 完整缓存（静音+BGM）
        ready = [
            item for item in list_ready_chart_rounds()
            if item[0] in by_id
        ]
        random.shuffle(ready)
        if ready:
            mid, kind, diff = ready[0]
            music = by_id[mid]
            path = video_path_for(mid, kind, diff)
            bgm = str(bgm_video_path_for(mid, kind, diff).resolve())
            set_chart_prepare_status('命中预制完整缓存（含 BGM）')
            return self.guesschartdata(
                music,
                video_path=str(path.resolve()),
                chart_kind=kind,
                chart_diff=diff,
                video_path_bgm=bgm,
            )

        # 2) 热门池内尝试补齐/渲染（限少量候选，且走限流录制槽）
        set_chart_prepare_status('完整缓存不足，尝试补渲染…')
        for music in candidates[:6]:
            ok, _msg, path, entry = await ensure_chart_video_ready(
                music.id,
                music_type=music.type,
                title=music.title,
                level_count=len(music.ds),
                render_bgm=True,
            )
            if ok and path is not None and entry.get('has_bgm'):
                kind = str(entry.get('kind') or resolve_chart_kind(music.type))
                diff = int(entry.get('diff') or pick_chart_diff(len(music.ds)))
                bgm = str(entry.get('path_bgm') or '')
                if not bgm and is_chart_bgm_ready(music.id, kind, diff):
                    bgm = str(bgm_video_path_for(music.id, kind, diff).resolve())
                set_chart_prepare_status('铺面视频已就绪（含 BGM）')
                return self.guesschartdata(
                    music,
                    video_path=str(path.resolve()),
                    chart_kind=kind,
                    chart_diff=diff,
                    duration=int(entry.get('duration') or CHART_DEFAULT_DURATION),
                    video_path_bgm=bgm,
                    bgm_duration=int(entry.get('bgm_duration') or CHART_PHASE2_DURATION),
                )

        # 3) 回退仅静音缓存（仍可开局；后台会继续补 BGM）
        set_chart_prepare_status('回退已有静音缓存…')
        for music in candidates:
            primary = resolve_chart_kind(music.type)
            diff = pick_chart_diff(len(music.ds))
            for kind in (primary, 'standard' if primary == 'dx' else 'dx'):
                if is_chart_video_ready(music.id, kind, diff):
                    path = video_path_for(music.id, kind, diff)
                    bgm = ''
                    if is_chart_bgm_ready(music.id, kind, diff):
                        bgm = str(bgm_video_path_for(music.id, kind, diff).resolve())
                    return self.guesschartdata(
                        music,
                        video_path=str(path.resolve()),
                        chart_kind=kind,
                        chart_diff=diff,
                        video_path_bgm=bgm,
                    )
        set_chart_prepare_status('无可用铺面视频')
        return None

    def startchart(self, gid: Union[int, str], data: GuessChartData) -> None:
        self.Group[gid] = data
        self._log_guess_start('猜铺面', gid)

    def guesspicdata(self, difficulty: Optional[int] = None) -> GuessPicData:
        """猜曲绘数据；difficulty 为 1～4，省略则随机。"""
        music = self._pick_guess_music()
        im = Image.open(music_picture(music.id))
        w, h = im.size
        weights = self.calculate_frequency_weights(im)

        if difficulty is None:
            difficulty = random.randint(1, 4)
        elif difficulty not in self.PIC_DIFFICULTY:
            raise ValueError(f'无效猜曲绘难度：{difficulty}')
        cfg = self.PIC_DIFFICULTY[difficulty]
        initial_scale = cfg['initial']
        max_scale = cfg['max']
        expansion_count = cfg['expansions']

        temp_w, temp_h = int(w * initial_scale), int(h * initial_scale)
        top_p = min(1.3 - np.power(initial_scale, 0.4), 0.95) * 100
        x, y = self.select_crop_region(weights, temp_w, temp_h, top_p)
        cx, cy = x + temp_w // 2, y + temp_h // 2

        interferences, interference_labels = self._pick_pic_interferences(difficulty)
        answer = mai.total_alias_list.by_id(music.id)[0].Alias
        answer.append(music.id)
        return GuessPicData(
            music=music,
            img='',
            answer=answer,
            end=False,
            crop_cx=cx,
            crop_cy=cy,
            current_scale=initial_scale,
            initial_scale=initial_scale,
            max_scale=max_scale,
            full_w=w,
            full_h=h,
            interferences=interferences,
            interference_labels=interference_labels,
            difficulty=difficulty,
            expansion_count=expansion_count,
        )

    def guessData(self) -> GuessDefaultData:
        """猜歌数据"""
        music = self._pick_guess_music()
        guess_options = random.sample([
            f'的 Expert 难度是 {music.level[2]}',
            f'的 Master 难度是 {music.level[3]}',
            f'的分类是 {music.basic_info.genre}',
            f'的版本是 {music.basic_info.version}',
            f'的艺术家是 {music.basic_info.artist}',
            f'{"不" if music.type == "SD" else ""}是 DX 谱面',  # 谱面类型 SD=标准 DX=DX谱面
            f'{"没" if len(music.ds) == 4 else ""}有白谱',
            f'的 BPM 是 {music.basic_info.bpm}'
        ], 6)
        answer = mai.total_alias_list.by_id(music.id)[0].Alias
        answer.append(music.id)
        pic = self.pic(music)
        return GuessDefaultData(
            music=music, 
            img=image_to_base64(pic), 
            answer=answer, 
            end=False, 
            options=guess_options
        )

    def end(
        self,
        gid: Union[int, str],
        *,
        expected: Optional[
            Union[GuessDefaultData, GuessPicData, GuessAudioData, GuessChartData]
        ] = None,
    ):
        """结束猜歌"""
        if expected is not None and self.Group.get(gid) is not expected:
            return None
        data = self.Group.pop(gid, None)
        # Release the cross-mode admission reservation even when a game exits
        # through a timeout or an outbound-send failure.
        from .maimaidx_game_session import game_session_gate
        game_session_gate.release(gid)
        return data

    @staticmethod
    def _switch_key(gid: Union[int, str]) -> Union[int, str]:
        """Use the qgroupbind key for persisted official-QQ switches."""
        try:
            from .maimaidx_qq_bind import qq_bind_db

            mapped = qq_bind_db.get_group_legacy_id(str(gid))
            if mapped is not None:
                return mapped
        except Exception:
            pass
        return gid

    def is_enabled(self, gid: Union[int, str]) -> bool:
        """Check the persisted switch without confusing openid and old QQ ids."""
        aliases = {str(gid), str(self._switch_key(gid))}
        return bool(aliases & {str(item) for item in self.switch.enable})

    async def on(self, gid: Union[int, str]) -> str:
        """开启猜歌"""
        key = self._switch_key(gid)
        aliases = {str(gid), str(key)}
        if not aliases & {str(item) for item in self.switch.enable}:
            self.switch.enable.append(key)
        self.switch.disable = [
            item for item in self.switch.disable if str(item) not in aliases
        ]
        await writefile(guess_file, self.switch.model_dump())
        return '群猜歌功能已开启'

    async def off(self, gid: Union[int, str]) -> str:
        """关闭猜歌"""
        key = self._switch_key(gid)
        aliases = {str(gid), str(key)}
        if not aliases & {str(item) for item in self.switch.disable}:
            self.switch.disable.append(key)
        self.switch.enable = [
            item for item in self.switch.enable if str(item) not in aliases
        ]
        if gid in self.Group:
            self.end(gid)
        self.Preparing.discard(gid)
        await writefile(guess_file, self.switch.model_dump())
        return '群猜歌功能已关闭'


guess = Guess()


class GroupAlias:

    push: AliasesPush

    def __init__(self) -> None:
        """别名推送类"""
        if not group_alias_file.exists():
            self.push = AliasesPush()
        else:
            self.push = AliasesPush.model_validate(
                json.load(open(group_alias_file, 'r', encoding='utf-8'))
            )

    @staticmethod
    def _switch_key(gid):
        """Use the former numeric QQ group as the persisted alias key."""
        try:
            from .maimaidx_qq_bind import qq_bind_db

            mapped = qq_bind_db.get_group_legacy_id(str(gid))
            if mapped is not None:
                return mapped
        except Exception:
            pass
        return gid

    @classmethod
    def _aliases(cls, gid) -> set[str]:
        return {str(gid), str(cls._switch_key(gid))}

    def is_disabled(self, gid) -> bool:
        aliases = self._aliases(gid)
        return bool(aliases & {str(item) for item in self.push.disable})

    async def on(self, gid) -> str:
        """开启推送"""
        key = self._switch_key(gid)
        aliases = self._aliases(gid)
        if not aliases & {str(item) for item in self.push.enable}:
            self.push.enable.append(key)
        self.push.disable = [
            item for item in self.push.disable if str(item) not in aliases
        ]
        await writefile(group_alias_file, self.push.model_dump())
        return '群别名推送功能已开启'

    async def off(self, gid) -> str:
        """关闭推送"""
        key = self._switch_key(gid)
        aliases = self._aliases(gid)
        if not aliases & {str(item) for item in self.push.disable}:
            self.push.disable.append(key)
        self.push.enable = [
            item for item in self.push.enable if str(item) not in aliases
        ]
        await writefile(group_alias_file, self.push.model_dump())
        return '群别名推送功能已关闭'

    async def alias_global_change(self, switch: bool, group_list: List[int]):
        """修改全局开关"""
        if switch:
            self.push.disable.clear()
            self.push.enable.clear()
            self.push.enable.extend(group_list)
        else:
            self.push.enable.clear()
            self.push.disable.clear()
            self.push.disable.extend(group_list)
        await writefile(group_alias_file, self.push.model_dump())


alias = GroupAlias()


def _get_all_feature_names_from_model() -> list:
    """从 FeatureSwitch 模型获取所有功能名（便于新功能加入时自动纳入）。"""
    return list(FeatureSwitch.model_fields.keys())


class FeatureManager:
    """功能开关管理类"""
    
    switch: FeatureSwitch
    
    def __init__(self) -> None:
        """初始化功能开关；若有新功能（模型有而 JSON 无），按当前群总开关为其设置 disable。"""
        from ..config import group_feature_switch_file
        if not group_feature_switch_file.exists():
            self.switch = FeatureSwitch()
            return
        with open(group_feature_switch_file, 'r', encoding='utf-8') as f:
            raw = json.load(f)
        known_keys = set(raw.keys())
        self.switch = FeatureSwitch.model_validate(raw)
        all_feature_names = _get_all_feature_names_from_model()
        new_features = [f for f in all_feature_names if f not in known_keys]
        if not new_features:
            return
        # 群总开关关闭 = 该群在所有已有功能的 disable 列表中 → 新功能对该群也禁用
        old_features = [f for f in all_feature_names if f in known_keys]
        if not old_features:
            return
        groups_all_disabled = None
        for f in old_features:
            s = set(getattr(self.switch, f).disable)
            if groups_all_disabled is None:
                groups_all_disabled = s
            else:
                groups_all_disabled &= s
        if groups_all_disabled is None:
            groups_all_disabled = set()
        for f in new_features:
            sw = getattr(self.switch, f)
            sw.disable = list(groups_all_disabled)
        try:
            with open(group_feature_switch_file, 'w', encoding='utf-8') as f:
                json.dump(self.switch.model_dump(), f, ensure_ascii=False, indent=4)
        except Exception:
            pass
    
    def _get_feature_switch(self, feature_name: str) -> Switch:
        """获取指定功能的开关"""
        if not hasattr(self.switch, feature_name):
            raise ValueError(f'未知功能: {feature_name}')
        return getattr(self.switch, feature_name)

    @staticmethod
    def _group_key(gid):
        """Use an administrator's legacy QQ group mapping for persisted switches."""
        try:
            from .maimaidx_qq_bind import qq_bind_db

            mapped = qq_bind_db.get_group_legacy_id(str(gid))
            if mapped is not None:
                return mapped
        except Exception:
            pass
        return gid

    @classmethod
    def _group_aliases(cls, gid) -> set[str]:
        return {str(gid), str(cls._group_key(gid))}
    
    def is_enabled(self, gid: int, feature_name: str) -> bool:
        """检查功能是否在群组中启用；默认启用，显式 disable 优先。"""
        switch = self._get_feature_switch(feature_name)
        aliases = self._group_aliases(gid)
        return not bool(aliases & {str(item) for item in switch.disable})
    
    async def enable(self, gid: int, feature_name: str) -> str:
        """在群组中启用功能"""
        from ..config import group_feature_switch_file
        switch = self._get_feature_switch(feature_name)
        key = self._group_key(gid)
        aliases = self._group_aliases(gid)
        if not aliases & {str(item) for item in switch.enable}:
            switch.enable.append(key)
        switch.disable = [
            item for item in switch.disable if str(item) not in aliases
        ]
        await writefile(group_feature_switch_file, self.switch.model_dump())
        feature_names = self._feature_display_names()
        return f'群组 {feature_names.get(feature_name, feature_name)} 已启用'
    
    async def disable(self, gid: int, feature_name: str) -> str:
        """在群组中禁用功能"""
        from ..config import group_feature_switch_file
        switch = self._get_feature_switch(feature_name)
        key = self._group_key(gid)
        aliases = self._group_aliases(gid)
        if not aliases & {str(item) for item in switch.disable}:
            switch.disable.append(key)
        switch.enable = [
            item for item in switch.enable if str(item) not in aliases
        ]
        await writefile(group_feature_switch_file, self.switch.model_dump())
        feature_names = self._feature_display_names()
        return f'群组 {feature_names.get(feature_name, feature_name)} 已禁用'
    
    def _feature_display_names(self) -> dict:
        """功能名 -> 展示名（新功能未配置时用 key 作为展示名）"""
        return {
            'query': '查询功能',
            'search': '搜索功能',
            'score': '成绩查询功能',
            'tag_analysis': '底力分析功能',
            'random': '随机推荐功能',
            'today': '今日运势功能',
            'ranking': '排名功能',
        }

    def get_status(self, gid: int) -> str:
        """获取群组所有功能状态（含模型中新功能，展示名未配置时用功能名）"""
        display_names = self._feature_display_names()
        status_list = []
        for feature_name in self._get_all_feature_names():
            display_name = display_names.get(feature_name, feature_name)
            enabled = self.is_enabled(gid, feature_name)
            status_list.append(f'{display_name}: {"启用" if enabled else "禁用"}')
        return '\n'.join(status_list)
    
    def _get_all_feature_names(self) -> list:
        """获取所有功能名称列表（与模型一致，新功能自动纳入）"""
        return _get_all_feature_names_from_model()
    
    async def enable_all(self, gid: int) -> str:
        """在群组中启用所有功能"""
        from ..config import group_feature_switch_file
        key = self._group_key(gid)
        aliases = self._group_aliases(gid)
        feature_names = self._get_all_feature_names()
        for feature_name in feature_names:
            switch = self._get_feature_switch(feature_name)
            if not aliases & {str(item) for item in switch.enable}:
                switch.enable.append(key)
            switch.disable = [
                item for item in switch.disable if str(item) not in aliases
            ]
        await writefile(group_feature_switch_file, self.switch.model_dump())
        return '群组所有 maimai 功能已启用'
    
    async def disable_all(self, gid: int) -> str:
        """在群组中禁用所有功能"""
        from ..config import group_feature_switch_file
        key = self._group_key(gid)
        aliases = self._group_aliases(gid)
        feature_names = self._get_all_feature_names()
        for feature_name in feature_names:
            switch = self._get_feature_switch(feature_name)
            if not aliases & {str(item) for item in switch.disable}:
                switch.disable.append(key)
            switch.enable = [
                item for item in switch.enable if str(item) not in aliases
            ]
        await writefile(group_feature_switch_file, self.switch.model_dump())
        return '群组所有 maimai 功能已禁用'


feature_manager = FeatureManager()
