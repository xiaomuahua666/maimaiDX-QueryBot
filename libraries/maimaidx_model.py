from collections import namedtuple
from typing import Dict, List, Optional, Union

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


##### Music
class Stats(BaseModel):
    
    cnt: Optional[float] = None
    diff: Optional[str] = None
    fit_diff: Optional[float] = None
    avg: Optional[float] = None
    avg_dx: Optional[float] = None
    std_dev: Optional[float] = None
    dist: Optional[List[int]] = None
    fc_dist: Optional[List[float]] = None


Notes1 = namedtuple('Notes', ['tap', 'hold', 'slide', 'brk'])
Notes2 = namedtuple('Notes', ['tap', 'hold', 'slide', 'touch', 'brk'])


class Chart(BaseModel):
    
    notes: Union[Notes1, Notes2]
    charter: str = None


class BasicInfo(BaseModel):
    
    title: str
    artist: str
    genre: str
    bpm: int
    release_date: Optional[str] = ''
    version: str = Field(alias='from')
    is_new: bool


class Music(BaseModel):
    # type: 谱面类型，SD=标准谱面，DX=DX谱面
    id: str
    title: str
    type: str
    ds: List[float]
    level: List[str]
    cids: List[int]
    charts: List[Chart]
    basic_info: BasicInfo
    stats: Optional[List[Optional[Stats]]] = []
    diff: Optional[List[int]] = []


class RaMusic(BaseModel):
    
    id: str
    ds: float
    lv: str
    lvp: str
    type: str


##### API
class APIResult(BaseModel):
    
    code: int = 0
    content: Union[dict, list, str]


##### Aliases
class Alias(BaseModel):
    
    SongID: int
    Name: str
    Alias: List[str]


class StatusBase(BaseModel):
    
    SongID: int
    ApplyUID: int
    ApplyAlias: str


class Approved(StatusBase):
    
    Tag: str
    Name: str
    GroupID: int | None = None
    WSUUID: str | None = None


class AliasStatus(StatusBase):
    
    Tag: str
    Name: str
    Time: str
    AgreeVotes: Optional[int] = 0
    Votes: int

class Reviewed(StatusBase):

    Tag: str
    Name: str


class PushAliasStatus(BaseModel):
    
    Type: str
    Status: Union[AliasStatus, Approved, Reviewed]


##### Guess
class GuessData(BaseModel):
    
    music: Music
    img: str
    answer: List[str]
    end: bool = False
    hint_step: int = 0
    user_attempts: Dict[str, int] = Field(default_factory=dict)


class GuessDefaultData(GuessData):
    
    options: List[str]


class GuessPicData(GuessData):

    crop_cx: int
    crop_cy: int
    current_scale: float
    initial_scale: float
    max_scale: float
    full_w: int
    full_h: int
    interferences: List[str]
    interference_labels: List[str]
    difficulty: int
    expansion_count: int
    global_shown: bool = False
    interference_cleared: bool = False


class GuessAudioData(GuessData):

    stage_paths: List[str]
    stage_count: int = 4


class GuessChartData(GuessData):

    video_path: str
    video_path_bgm: str = ''
    chart_kind: str = 'dx'
    chart_diff: int = 5
    chart_diff_name: str = '紫'
    duration: int = 40
    bgm_duration: int = 30
    stage_count: int = 2
    started_at: float = 0.0
    bgm_at: float = 0.0


class Switch(BaseModel):

    enable: List[Union[int, str]] = []
    disable: List[Union[int, str]] = []


class GuessSwitch(Switch): ...


##### AliasesPush
class AliasesPush(Switch): ...


##### FeatureSwitch
class FeatureSwitch(BaseModel):
    """功能开关：每个功能对应一个 Switch"""
    query: Switch = Switch()           # 查询功能（mai什么、查询等）
    search: Switch = Switch()          # 搜索功能（搜索、查歌等）
    score: Switch = Switch()           # 成绩查询功能（b50、成绩等）
    tag_analysis: Switch = Switch()     # 底力分析功能
    random: Switch = Switch()           # 随机推荐功能
    today: Switch = Switch()            # 今日运势功能
    ranking: Switch = Switch()         # 排名功能


##### Best50
class PlayInfo(BaseModel):
    """查分器返回的成绩项；支持 camelCase / snake_case（dx_score）等 API 字段名。"""
    model_config = ConfigDict(populate_by_name=True)
    # type: 谱面类型，SD=标准谱面，DX=DX谱面
    achievements: float = 0.0
    fc: str = ''
    fs: str = ''
    level: str = ''
    level_index: int = Field(0, alias='levelIndex')
    title: str = ''
    type: str = 'SD'
    ds: float = 0
    dxScore: int = Field(0, validation_alias=AliasChoices('dx_score', 'dxScore'))
    ra: int = Field(0, alias='rating')  # 单曲 rating，部分 API 返回 rating
    rate: str = ''


class ChartInfo(PlayInfo):
    
    level_label: str
    song_id: int


class Data(BaseModel):
    # 查分器字段：sd = B35（35 首）, dx = B15（15 首），与谱面类型 SD/DX 无关
    sd: Optional[List[ChartInfo]] = None  # B35
    dx: Optional[List[ChartInfo]] = None  # B15


class _UserInfo(BaseModel):
    
    additional_rating: Optional[int]
    nickname: Optional[str]
    plate: Optional[str] = None
    rating: Optional[int]
    username: Optional[str]


class UserInfo(_UserInfo):
    
    charts: Optional[Data]

class PlayInfoDefault(PlayInfo):
    
    song_id: int = Field(alias='id')
    table_level: list[int] = []


class PlayInfoDev(ChartInfo): ...


class PlanInfo(BaseModel):
    
    completed: Union[PlayInfoDefault, PlayInfoDev] = None
    unfinished: Union[PlayInfoDefault, PlayInfoDev] = None


class RiseScore(BaseModel):
    
    song_id: int
    title: str
    type: str
    level_index: int
    ds: float
    ra: int
    rate: str
    achievements: float
    oldra: Optional[int] = 0
    oldrate: Optional[str] = 'D'
    oldachievements: Optional[float] = 0


##### Dev
class UserInfoDev(_UserInfo):
    
    records: Optional[List[PlayInfoDev]] = None


##### Rank
class UserRanking(BaseModel):
    
    username: str
    ra: int


##### Source
class Source(str):
    """数据源标识。"""
    DIVINGFISH = 'divingfish'
    LXNS = 'lxns'
    AWMCNET = 'awmcnet'

    @classmethod
    def get_by_name(cls, name: str) -> str:
        _map = {
            '水鱼': cls.DIVINGFISH, 'divingfish': cls.DIVINGFISH, 'df': cls.DIVINGFISH,
            '落雪': cls.LXNS, 'lxns': cls.LXNS, 'lx': cls.LXNS,
            'awmcnet': cls.AWMCNET, 'awmc': cls.AWMCNET,
        }
        return _map.get(name.lower(), cls.DIVINGFISH)


# 各数据源的能力标记
SOURCE_CAPABILITIES = {
    Source.DIVINGFISH: {
        'b50': True,
        'records': True,
        'fit_diff': True,       # 拟合定数（水鱼独有）
        'rating_ranking': True, # 全服 rating 排行（水鱼独有）
        'gold_water': True,     # 含金量/含水量（依赖 fit_diff）
        'pc_data': True,        # PC 数据（水鱼独有）
        'minfo': True,          # 单曲详细游玩数据
        'friend_battle': True,  # 友人对战
    },
    Source.LXNS: {
        'b50': True,
        'records': True,        # 需 OAuth 授权
        'fit_diff': False,
        'rating_ranking': False,
        'gold_water': False,
        'pc_data': False,
        'minfo': False,
        'friend_battle': False,
    },
    Source.AWMCNET: {
        'b50': True,
        'records': True,
        'fit_diff': False,
        'rating_ranking': False,
        'gold_water': False,
        'pc_data': True,
        'minfo': True,
        'friend_battle': False,
    },
}


def source_supports(source: str, feature: str) -> bool:
    """检查指定数据源是否支持某功能。"""
    return SOURCE_CAPABILITIES.get(source, {}).get(feature, False)
