"""你想我猜（20 问猜曲）：Bot 心里想一首曲，群友通过是非题缩小范围并猜出曲名。

复用猜歌热门曲目池、别名匹配与积分系统。玩家发送的每条消息会先被识别为
「是非题」（分类/BPM/定数/版本/谱面类型/艺术家/标题特征等），由 Bot 回答；
若不像题目但能匹配到某首曲的别名/标题，则视为猜答案。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from loguru import logger as log

from .maimaidx_music import Music, mai, guess
from .maimaidx_guess_match import match_guess_answer

TWENTYQ_MAX_QUESTIONS = 20
TWENTYQ_DURATION = 600
TWENTYQ_COUNTDOWN = (120, 60, 30, 10)

# 用掉的提问数 -> 猜对基础分（问得越少分越高）
def twentyq_base_points(questions_used: int) -> int:
    if questions_used <= 5:
        return 12
    if questions_used <= 10:
        return 8
    if questions_used <= 15:
        return 5
    return 2


@dataclass
class QAEntry:
    uid: str
    name: str
    question: str
    answer: str
    at: float


@dataclass
class Guess20QData:
    music: Music
    answers: List[str]
    max_questions: int
    duration: int
    started_at: float
    question_count: int = 0
    qa: List[QAEntry] = field(default_factory=list)
    winner_uid: Optional[str] = None
    winner_name: str = ''
    winner_billing: int = 0
    end: bool = False

    def time_left(self) -> float:
        return max(0.0, self.duration - (time.time() - self.started_at))

    def remaining(self) -> int:
        return max(0, self.max_questions - self.question_count)


class Guess20QManager:
    groups: Dict[int, Guess20QData] = {}
    locked: set = set()

    def is_busy(self, gid: int) -> bool:
        return gid in self.groups or gid in self.locked

    def lock(self, gid: int) -> bool:
        if self.is_busy(gid):
            return False
        self.locked.add(gid)
        return True

    def unlock(self, gid: int) -> None:
        self.locked.discard(gid)
        from .maimaidx_game_session import game_session_gate
        game_session_gate.release(gid)

    def get(self, gid: int) -> Optional[Guess20QData]:
        return self.groups.get(gid)

    def end(
        self,
        gid: int,
        *,
        expected: Optional[Guess20QData] = None,
    ) -> Optional[Guess20QData]:
        if expected is not None and self.groups.get(gid) is not expected:
            return None
        self.locked.discard(gid)
        from .maimaidx_game_session import game_session_gate
        game_session_gate.release(gid)
        return self.groups.pop(gid, None)

    def start(
        self,
        gid: int,
        *,
        duration: int = TWENTYQ_DURATION,
        max_questions: int = TWENTYQ_MAX_QUESTIONS,
    ) -> Guess20QData:
        music = guess._pick_guess_music()
        alias_list = mai.total_alias_list.by_id(music.id)
        answers = list(alias_list[0].Alias) if alias_list else []
        answers.append(music.id)
        data = Guess20QData(
            music=music,
            answers=answers,
            max_questions=max_questions,
            duration=duration,
            started_at=time.time(),
        )
        self.locked.discard(gid)
        self.groups[gid] = data
        log.info(f'[Guess20Q] 开局 gid={gid} answer={music.title} id={music.id}')
        return data

    def reveal_text(self, data: Guess20QData) -> str:
        bi = data.music.basic_info
        max_ds = max(data.music.ds) if data.music.ds else 0.0
        level_label = data.music.level[-1] if data.music.level else '?'
        return (
            f'🎵 答案是：{data.music.title}\n'
            f'🎤 艺术家：{bi.artist}\n'
            f'🎯 分类：{bi.genre} · BPM {bi.bpm}\n'
            f'📈 最高定数：{max_ds:g}（{level_label}）· 版本：{bi.version}'
        )

    def process_message(
        self,
        gid: int,
        uid: str,
        name: str,
        text: str,
        billing_id: int = 0,
    ) -> dict:
        data = self.groups.get(gid)
        if data is None or data.end:
            return {'kind': 'idle'}

        raw = (text or '').strip()
        if not raw:
            return {'kind': 'idle'}

        if is_guess(raw, data.answers):
            data.winner_uid = uid
            data.winner_name = name
            data.winner_billing = billing_id
            data.end = True
            log.info(
                f'[Guess20Q] gid={gid} winner={name}({uid}) '
                f'questions={data.question_count} answer={data.music.title}'
            )
            return {
                'kind': 'win',
                'questions_used': data.question_count,
                'base_points': twentyq_base_points(data.question_count),
            }

        answer, consumed = classify_question(data.music, raw)
        if consumed:
            if data.question_count >= data.max_questions:
                return {'kind': 'no_questions'}
            data.question_count += 1
            data.qa.append(QAEntry(
                uid=uid,
                name=name,
                question=raw,
                answer=answer,
                at=time.time(),
            ))
            return {
                'kind': 'question',
                'answer': answer,
                'remaining': data.remaining(),
                'used': data.question_count,
                'last': data.question_count >= data.max_questions,
            }

        if data.question_count >= data.max_questions:
            data.end = True
            return {'kind': 'failed'}
        return {'kind': 'unknown', 'answer': answer}


# ───────────────────── 是非题分类器 ─────────────────────

# 处理函数返回回答文本（命中）或 None（无法识别）。
QuestionHandler = Callable[[Music, str], Optional[str]]

_YES = '是喵 ✅'
_NO = '不是喵 ❌'


def _yn(flag: bool) -> str:
    return _YES if flag else _NO


_GENRE_KEYWORDS = {
    'POPSアニメ': ('动漫', '动画', '流行', 'pops', 'アニメ', 'anime'),
    'niconicoボーカロイド': (
        'niconico', 'ニコニコ', 'vocaloid', 'ボーカロイド', 'ボカロ',
        '术力口', 'v家', 'vc', 'nico', 'n站',
    ),
    '東方Project': ('东方', '東方', 'touhou'),
    'ゲームバラエティ': ('游戏', 'バラエティ', 'variety', '其他游戏'),
    'オンゲキCHUNITHM': (
        '音击', 'オンゲキ', 'ongeki', '中二', 'チュウニズム', 'chunithm',
    ),
    'maimai': ('舞萌', 'maimai', '原创'),
    '宴会場': ('宴会', '宴会场', 'utage'),
}

_VERSION_KEYWORDS = (
    ('prism', 'prism'),
    ('buddies', 'buddies'),
    ('festival', 'festival'),
    ('universe', 'universe'),
    ('splash', 'splash'),
    ('finale', 'finale'),
    ('milk', 'milk'),
    ('murasaki', 'murasaki', '紫'),
    ('pink', 'pink'),
    ('orange', 'orange'),
    ('green', 'green'),
    ('でらっくす', 'でらっくす', 'deluxe', '新框体'),
    ('plus', 'plus'),
)

_CJK_RE = re.compile(r'[\u4e00-\u9fff]')
_KANA_RE = re.compile(r'[\u3040-\u30ff]')
_LATIN_RE = re.compile(r'[a-zA-Z]')
_NUM_RE = re.compile(r'\d+(?:\.\d+)?')


def _norm(text: str) -> str:
    return text.strip().lower().replace(' ', '').replace('　', '')


def _nums(text: str) -> List[float]:
    return [float(x) for x in _NUM_RE.findall(text)]


def _cmp_bool(value: float, text: str, nums: List[float]) -> Optional[bool]:
    """根据文本中的比较词判断 value 与数字的关系。"""
    if not nums:
        return None
    n = nums[0]
    t = text
    if '以上' in t or '≥' in t or '>=' in t or '不低于' in t or '不小于' in t:
        return value >= n
    if '以下' in t or '≤' in t or '<=' in t or '不高于' in t or '不超过' in t:
        return value <= n
    if '大于' in t or '超过' in t or '高于' in t or '>' in t or '多过' in t:
        return value > n
    if '小于' in t or '低于' in t or '不到' in t or '不满' in t or '<' in t or '少于' in t:
        return value < n
    if '等于' in t or '=' in t or '为' in t or '是' in t:
        if len(nums) >= 2:
            return nums[0] <= value <= nums[1]
        return abs(value - n) < 0.01
    if len(nums) >= 2:
        return nums[0] <= value <= nums[1]
    return None


def _q_genre(music: Music, text: str) -> Optional[str]:
    genre = music.basic_info.genre
    target: Optional[str] = None
    for canonical, kws in _GENRE_KEYWORDS.items():
        if any(k in text for k in kws):
            target = canonical
            break
    if target is None:
        return None
    return _yn(genre == target or (target == '宴会場' and genre in ('宴会場', '宴会场')))


def _q_bpm(music: Music, text: str) -> Optional[str]:
    if not any(k in text for k in ('bpm', '节奏', '速度', '快', '慢')):
        # 单独出现 100 以上的数字 + 比较词时，视为问 BPM（等级不会超过 15）
        nums = _nums(text)
        if nums and nums[0] > 30 and any(k in text for k in ('以上', '以下', '大于', '小于', '超过', '低于', '>', '<', '≥', '≤')):
            bpm = music.basic_info.bpm
            res = _cmp_bool(bpm, text, nums)
            return _yn(res) if res is not None else None
        return None
    bpm = music.basic_info.bpm
    nums = _nums(text)
    if nums:
        res = _cmp_bool(bpm, text, nums)
        if res is not None:
            return _yn(res)
        return None
    if any(k in text for k in ('高', '快', '大')):
        return _yn(bpm >= 180)
    if any(k in text for k in ('低', '慢', '小')):
        return _yn(bpm <= 120)
    return None


def _q_white_chart(music: Music, text: str) -> Optional[str]:
    if any(k in text for k in ('白谱', '白譜', 're:master', 'remaster', '白re', '白master')):
        return _yn(len(music.ds) >= 5)
    return None


def _q_ds(music: Music, text: str) -> Optional[str]:
    if not any(k in text for k in ('定数', 'ds', '等级', '难度', '難度', '级别', '級別', '最高')):
        return None
    if any(k in text for k in ('白谱', 're:master', 'remaster')):
        return None
    max_ds = max(music.ds) if music.ds else 0.0
    nums = _nums(text)
    if not nums:
        if any(k in text for k in ('高', '大', '难', '難')):
            return _yn(max_ds >= 13.5)
        if any(k in text for k in ('低', '小', '简单', '簡單', '易')):
            return _yn(max_ds <= 11.0)
        return None
    n = nums[0]
    if '+' in text:
        return _yn((n + 0.5) <= max_ds < (n + 1.0))
    res = _cmp_bool(max_ds, text, nums)
    if res is not None:
        return _yn(res)
    return _yn(n <= max_ds < n + 0.5)


_BARE_LEVEL_RE = re.compile(
    r'^(?:是|为|為)?\s*(\d{1,2})(\+)?\s*(?:级|級|等级|等級|定数|星)?\s*'
    r'(?:吗|嘛|？|\?)?$'
)


def _q_level_bare(music: Music, text: str) -> Optional[str]:
    """玩家直接问「是13吗」「14+吗」时按最高定数处理。"""
    if 'bpm' in text:
        return None
    m = _BARE_LEVEL_RE.match(text)
    if not m:
        return None
    try:
        n = float(m.group(1))
    except ValueError:
        return None
    if not (1 <= n <= 15):
        return None
    max_ds = max(music.ds) if music.ds else 0.0
    if m.group(2) == '+':
        return _yn((n + 0.5) <= max_ds < (n + 1.0))
    return _yn(n <= max_ds < n + 0.5)


def _q_song_type(music: Music, text: str) -> Optional[str]:
    if not any(k in text for k in ('谱面', '譜面', '谱', '譜', 'sd', 'dx')):
        return None
    t = text
    if ('dx' in t and '谱' in t) or any(k in t for k in ('黄谱', '黃譜')):
        return _yn(music.type == 'DX')
    if any(k in t for k in ('标准谱', '標準譜', 'sd谱', '紫谱', '紫譜')):
        return _yn(music.type == 'SD')
    return None


def _q_version(music: Music, text: str) -> Optional[str]:
    version = (music.basic_info.version or '').lower()
    if any(k in text for k in ('新歌', '新曲', 'isnew', '是新', '新的')):
        return _yn(bool(music.basic_info.is_new))
    if any(k in text for k in ('旧曲', '舊曲', '老歌', '老曲', '旧的', '舊的')):
        return _yn(not music.basic_info.is_new)
    if not any(k in text for k in ('版本', '版', 'version', '哪代', '哪一作', '哪作', '出自', '来自哪')):
        matched = None
        for kws in _VERSION_KEYWORDS:
            if any(k in text for k in kws[1:]):
                matched = kws[0]
                break
        if matched is None:
            return None
        return _yn(matched in version)
    for kws in _VERSION_KEYWORDS:
        if any(k in text for k in kws[1:]):
            return _yn(kws[0] in version)
    if '初代' in text or '最早' in text or '第一作' in text:
        return _yn(version == 'maimai')
    return None


def _extract_artist_keyword(text: str) -> Optional[str]:
    m = re.search(
        r'(?:艺术家|artist|作曲|编曲|編曲|作者|曲师|曲師|师匠|師匠|是谁写的|是誰寫的)'
        r'(?:是|为|為)?\s*(.+?)(?:写的|寫的|作的|谱的|譜的|吗|嗎|？|\?|$)',
        text,
    )
    if m:
        kw = m.group(1).strip(' 的是为為')
        return kw or None
    m = re.search(r'是\s*(.+?)\s*(?:写|寫|作|谱|譜)的(?:吗|嗎)?$', text)
    if m:
        kw = m.group(1).strip()
        return kw or None
    return None


def _q_artist(music: Music, text: str) -> Optional[str]:
    kw = _extract_artist_keyword(text)
    if not kw:
        return None
    artist = (music.basic_info.artist or '').lower()
    return _yn(kw.lower() in artist)


def _q_title_script(music: Music, text: str) -> Optional[str]:
    if not any(k in text for k in ('标题', '標題', '曲名', '名字', '歌名', '题', '題')):
        return None
    title = music.title
    t = text
    if any(k in t for k in ('英文', '英语', '英語', '拉丁', '字母')):
        return _yn(bool(_LATIN_RE.search(title)) and not _CJK_RE.search(title) and not _KANA_RE.search(title))
    if any(k in t for k in ('日文', '日语', '日語', '假名', 'かな', 'カナ')):
        return _yn(bool(_KANA_RE.search(title)))
    if any(k in t for k in ('中文', '汉语', '漢語', '汉字', '漢字')):
        return _yn(bool(_CJK_RE.search(title)))
    return None


def _q_title_length(music: Music, text: str) -> Optional[str]:
    if not any(k in text for k in ('几个字', '幾個字', '多少个字', '多少字', '字长', '字長', '长度', '長度', '多长', '多長', '名字长', '名字短', '标题长', '标题短')):
        return None
    length = len(music.title)
    # 「多长/几个字/多少字」直接回答字数
    if any(k in text for k in ('多长', '多長', '几个字', '幾個字', '多少个字', '多少字')):
        return f'标题有 {length} 个字喵 📏'
    nums = _nums(text)
    if nums:
        res = _cmp_bool(length, text, nums)
        return _yn(res) if res is not None else None
    if '长' in text or '長' in text:
        return _yn(length >= 12)
    if '短' in text:
        return _yn(length <= 5)
    return None


def _q_title_contains(music: Music, text: str) -> Optional[str]:
    # 先排除「有几个字/多少字」这类长度问法
    if any(k in text for k in ('几个字', '幾個字', '多少个字', '多少字', '字长', '字長', '长度', '長度')):
        return None
    m = re.search(
        r'(?:标题|標題|曲名|名字|歌名|名)(?:里|裡|中|里面|裏面)有(.+)'
        r'|(?:标题|標題|曲名|名字|歌名|名)(?:包含|含有|含|带|帶)(.+)',
        text,
    )
    if not m:
        return None
    kw = (m.group(1) or m.group(2) or '').strip(' 的吗嘛？?')
    if not kw:
        return None
    return _yn(kw.lower() in music.title.lower())


def _q_informational(music: Music, text: str) -> Optional[str]:
    """回答特殊疑问句（多少/是谁/什么版本），直接给出客观信息但消耗一次提问。"""
    bi = music.basic_info
    # BPM 是多少
    if 'bpm' in text and any(k in text for k in ('多少', '几', '幾', '是什么', '是多少')):
        return f'BPM 是 {bi.bpm} 喵 🎵'
    # 定数是多少 / 最高定数多少
    if any(k in text for k in ('定数', 'ds', '等级', '難度', '难度')) and any(k in text for k in ('多少', '几', '幾', '是什么', '是多少')):
        max_ds = max(music.ds) if music.ds else 0.0
        levels = '/'.join(str(x) for x in music.level) if music.level else '?'
        return f'各难度定数是 {levels}（最高 {max_ds:g}）喵 📈'
    # 艺术家/作者是谁
    if any(k in text for k in ('艺术家', 'artist', '作曲', '编曲', '編曲', '作者', '曲师', '曲師', '谁写的', '誰寫的', '谁做的', '誰做的')) \
            and any(k in text for k in ('谁', '誰', '什么', '什麼', '哪位', '哪个', '哪個', '多少', '是谁', '是誰')):
        return f'艺术家是 {bi.artist} 喵 🎤'
    # 什么版本 / 哪一作
    if any(k in text for k in ('版本', 'version', '哪一作', '哪代', '哪一代', '出自哪', '来自哪')) \
            and any(k in text for k in ('什么', '什麼', '哪', '多少', '是')):
        return f'出自 {bi.version} 喵 📦'
    # 什么分类
    if any(k in text for k in ('分类', '类别', '類別', '曲风', '曲風', 'genre')) and any(k in text for k in ('什么', '什麼', '哪', '多少')):
        return f'分类是 {bi.genre} 喵 🎯'
    # 是 SD 还是 DX（选择题）
    if 'sd' in text and 'dx' in text:
        return f'是 {music.type} 谱面喵'
    return None


_QUESTION_HANDLERS: Tuple[QuestionHandler, ...] = (
    _q_informational,
    _q_white_chart,
    _q_song_type,
    _q_genre,
    _q_bpm,
    _q_ds,
    _q_level_bare,
    _q_version,
    _q_artist,
    _q_title_script,
    _q_title_length,
    _q_title_contains,
)

_UNKNOWN_HINT = (
    '唔…Milk 没听懂这个问题喵。可以问这些方向：\n'
    '· 分类：「是动画曲吗」「是东方曲吗」\n'
    '· BPM：「BPM 大于 180 吗」「这歌快吗」\n'
    '· 定数：「最高定数是 14 吗」「是 14+ 吗」「有白谱吗」「定数高吗」\n'
    '· 版本：「是新歌吗」「是 Festival 的吗」\n'
    '· 谱面：「是 DX 谱面吗」\n'
    '· 艺术家：「艺术家是 Sakuzyo 吗」\n'
    '· 标题：「标题是英文吗」「标题有几个字」「标题里有 Bad 吗」\n'
    '想好了就直接发曲名猜答案喵～'
)


def classify_question(music: Music, text: str) -> Tuple[str, bool]:
    """返回 (回答文本, 是否消耗一次提问)。"""
    norm = _norm(text)
    for handler in _QUESTION_HANDLERS:
        try:
            result = handler(music, norm)
        except Exception as e:
            log.warning(f'[Guess20Q] 题目判定异常 {handler.__name__}: {e}')
            continue
        if result is not None:
            return result, True
    return _UNKNOWN_HINT, False



def is_guess(text: str, answers: List[str]) -> bool:
    return match_guess_answer(text, answers)


twentyq_guess = Guess20QManager()
