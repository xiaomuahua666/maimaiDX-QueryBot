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
            f'📈 最高定数：{max_ds:g}（{level_label}）· 版本：{bi.version}\n'
            f'🆔 曲 id：{data.music.id}'
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

        questions_used_up = data.question_count >= data.max_questions

        # 「我猜」前缀：任何时候都视为猜曲名尝试（猜对即胜，猜错不结束）。
        guess_text, is_guess_attempt = _strip_guess_prefix(raw)
        if is_guess_attempt:
            if is_guess(guess_text, data.answers):
                data.winner_uid = uid
                data.winner_name = name
                data.winner_billing = billing_id
                data.end = True
                return {'kind': 'win'}
            # 猜错：不结束游戏，让其他人继续猜，直到超时公布答案。
            return {'kind': 'wrong_guess', 'guess': guess_text}

        # 问完阶段：非「我猜」前缀的消息一律忽略（视为群内正常聊天）。
        if questions_used_up:
            return {'kind': 'idle'}

        # 问问题阶段：用「我问」前缀提问是非题。
        question_text, had_prefix = _strip_ask_prefix(raw)
        if not had_prefix:
            return {'kind': 'idle'}

        answer, consumed = classify_question(data.music, question_text)
        if consumed:
            # 否定反转：玩家说「不是动漫曲吧」「无白谱吗」时，把是/否回答反转
            answer = _apply_negation(question_text, answer)
            data.question_count += 1
            data.qa.append(QAEntry(
                uid=uid,
                name=name,
                question=question_text,
                answer=answer,
                at=time.time(),
            ))
            # 每 6 次提问后，追加已确认信息摘要
            if data.question_count % 6 == 0 and data.question_count < data.max_questions:
                summary = _summarize_qa(data.qa)
                if summary:
                    answer = f'{answer}\n\n{summary}'
            return {
                'kind': 'question',
                'answer': answer,
                'remaining': data.remaining(),
                'used': data.question_count,
                'last': data.question_count >= data.max_questions,
            }

        # 无法识别为问题（问问题阶段）
        return {'kind': 'unknown', 'answer': answer}


# ───────────────────── 是非题分类器 ─────────────────────

# 处理函数返回回答文本（命中）或 None（无法识别）。
QuestionHandler = Callable[[Music, str], Optional[str]]

_YES = '是喵 ✅'
_NO = '不是喵 ❌'


def _yn(flag: bool) -> str:
    return _YES if flag else _NO


_GENRE_KEYWORDS = {
    '流行&动漫': (
        '动漫', '动画', '動畫', '番曲', '番剧', '流行', 'pops', 'アニメ',
        'anime', 'jpop', 'acg',
    ),
    'niconico & VOCALOID': (
        'niconico', 'ニコニコ', 'vocaloid', 'ボーカロイド', 'ボカロ',
        '术曲', '术力口', '術曲', 'v家', 'v+', 'vc', 'nico', 'n站',
        'ボカロ曲', 'vocalo',
    ),
    '东方Project': ('东方', '東方', 'touhou', '车万'),
    '其他游戏': (
        '游戏', 'バラエティ', 'variety', '其他游戏',
        '音游', '音游曲', 'bof', 'game',
    ),
    '音击&中二节奏': (
        '音击', 'オンゲキ', 'ongeki', '中二', '中二节奏', '中二節奏',
        'チュウニズム', 'chunithm', '音击曲', '中二曲',
    ),
    '舞萌': ('舞萌', 'maimai', '原创', '原曲', '原创曲'),
    '宴会場': ('宴会', '宴会场', '宴會場', 'utage', '宴谱', '宴譜', '宴曲'),
}

# 版本匹配表：canonical 为完整版本字符串（小写），kws 为玩家可能的俗称。
# 用完整版本字符串做精确匹配，避免「でらっくす」误匹配「maimai でらっくす splash」
# 这类子串问题。PLUS 与基版必须分条录入（顺序：PLUS 在前，基版在后）。
_VERSION_KEYWORDS = (
    ('maimai でらっくす prism plus', ('prism plus', 'prism+', '镜+', '镜代+')),
    ('maimai でらっくす prism', ('prism', '镜代', '镜')),
    ('maimai でらっくす buddies plus', ('buddies plus', 'buddies+', '宴代', '宴+')),
    ('maimai でらっくす buddies', ('buddies', '双代', '双')),
    ('maimai でらっくす festival plus', ('festival plus', 'festival+', '祝代', '祝+')),
    ('maimai でらっくす festival', ('festival', '祭代', '祭')),
    ('maimai でらっくす universe plus', ('universe plus', 'universe+', '星代', '星+')),
    ('maimai でらっくす universe', ('universe', '宙代', '宙')),
    ('maimai でらっくす splash plus', ('splash plus', 'splash+', '煌代', '煌')),
    ('maimai でらっくす splash', ('splash', '爽代', '爽')),
    ('maimai finale', ('finale', '辉代', '辉')),
    ('maimai milk plus', ('milk plus', 'milk+', '雪代', '雪')),
    ('maimai milk', ('milk', '白代', '白')),
    ('maimai murasaki plus', ('murasaki plus', 'murasaki+', '堇代', '菫代', '堇', '菫')),
    ('maimai murasaki', ('murasaki', '紫代', '紫')),
    ('maimai pink plus', ('pink plus', 'pink+', '樱代', '櫻代', '樱', '櫻')),
    ('maimai pink', ('pink', '桃代', '粉代', '桃', '粉')),
    ('maimai orange plus', ('orange plus', 'orange+', '晓代', '曉代', '晓', '曉')),
    ('maimai orange', ('orange', '橙代', '橙')),
    ('maimai green plus', ('green plus', 'green+', '檄代', '檄')),
    ('maimai green', ('green', '超代', '绿代', '超', '绿')),
    ('maimai でらっくす plus', ('でらっくす plus', 'deluxe plus', 'dx+', '华代', '華代', '华')),
    ('maimai でらっくす', ('でらっくす', 'deluxe', 'dx', '熊代', '熊')),
    ('maimai plus', ('maimai plus', 'maimai+', '真代+', '无印+')),
    ('maimai', ('maimai', '初代', '真代', '无印', '最早', '第一作')),
)

# 旧框统称「舞代」——任意旧框版本都算（小写匹配）
_OLD_FRAME_VERSIONS = frozenset({
    'maimai', 'maimai plus', 'maimai green', 'maimai green plus',
    'maimai orange', 'maimai orange plus', 'maimai pink',
    'maimai pink plus', 'maimai murasaki', 'maimai murasaki plus',
    'maimai milk', 'maimai milk plus', 'maimai finale',
})

# 国服合并叫法——任一子版本都算（小写匹配）
_VERSION_GROUP_ALIASES = (
    ('舞代', _OLD_FRAME_VERSIONS),
    ('真代', frozenset({'maimai', 'maimai plus'})),
    ('熊华代', frozenset({'maimai でらっくす', 'maimai でらっくす plus'})),
    ('爽煌代', frozenset({'maimai でらっくす splash', 'maimai でらっくす splash plus'})),
    ('宙星代', frozenset({'maimai でらっくす universe', 'maimai でらっくす universe plus'})),
    ('祭祝代', frozenset({'maimai でらっくす festival', 'maimai でらっくす festival plus'})),
    ('双宴代', frozenset({'maimai でらっくす buddies', 'maimai でらっくす buddies plus'})),
)

_CJK_RE = re.compile(r'[\u4e00-\u9fff]')
_KANA_RE = re.compile(r'[\u3040-\u30ff]')
_LATIN_RE = re.compile(r'[a-zA-Z]')
_NUM_RE = re.compile(r'\d+(?:\.\d+)?')

# 定数关键词 + 难度形容词。_q_ds 据此识别定数问题，_q_bpm / _q_white_chart
# 据此让出（避免「紫谱高吗」被 _q_version 的「紫」单字误判为版本题）。
_DS_KEYWORDS = (
    '定数', 'ds', '等级', '难度', '難度', '级别', '級別', '最高',
    '高', '大', '难', '難', '低', '小', '简单', '簡單', '易',
)


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
    # 联动曲——跨分类：「其他游戏」与「音击&中二节奏」都算联动
    if any(k in text for k in ('联动', '联动曲', '联動', 'collab', '合作曲')):
        return _yn(genre in ('其他游戏', '音击&中二节奏'))
    target: Optional[str] = None
    for canonical, kws in _GENRE_KEYWORDS.items():
        if any(k in text for k in kws):
            target = canonical
            break
    if target is None:
        return None
    return _yn(genre == target or (target == '宴会場' and genre in ('宴会場', '宴会场')))


def _q_bpm(music: Music, text: str) -> Optional[str]:
    # 含定数关键词/难度形容词/难度颜色时让给 _q_ds——否则「紫谱定数超过50吗」
    # 会被这里用 BPM(180>50) 误答为「是」，造成数据错误。
    has_ds_signal = any(k in text for k in _DS_KEYWORDS) or _resolve_diff_index(text) is not None
    if not any(k in text for k in ('bpm', '节奏', '速度', '快', '慢')):
        if has_ds_signal:
            return None
        # 单独出现 100 以上的数字 + 比较词时，视为问 BPM（等级不会超过 15）
        nums = _nums(text)
        if nums and nums[0] > 30 and any(k in text for k in ('以上', '以下', '大于', '小于', '超过', '低于', '>', '<', '≥', '≤')):
            bpm = music.basic_info.bpm
            res = _cmp_bool(bpm, text, nums)
            return _yn(res) if res is not None else None
        return None
    # 即使含 BPM 关键词，若同时含定数关键词+颜色+大数字，仍可能是定数问题
    # （如「紫谱定数 BPM 超过 50 吗」罕见但需防御）——保守起见也让出。
    if has_ds_signal and _resolve_diff_index(text) is not None and _nums(text):
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
    if not any(k in text for k in ('白谱', '白譜', 're:master', 'remaster', '白re', '白master')):
        return None
    # 定数/难度相关问题（「白谱定数是13吗」「白谱是14+吗」「白谱难吗」「白谱简单吗」）
    # 让给 _q_ds 处理，这里只回答「有没有白谱」——
    # 否则会把定数题误判为有无白谱题，造成数据错误。
    if any(k in text for k in _DS_KEYWORDS) or _nums(text):
        return None
    return _yn(len(music.ds) >= 5)


# 难度颜色 → ds 列表索引：[BASIC, ADVANCED, EXPERT, MASTER, Re:MASTER]
# 即 [绿, 黄, 红, 紫, 白]。俗称「橙=黄」「basic=绿」「master=紫」「remaster=白」。
# 注意：白谱(Re:MASTER)的关键词必须比紫谱(MASTER)更先匹配，否则 remaster 会被
# master 吞掉。这里把白谱放在紫谱前面，并去掉过短的 'mas'/'mst' 等易冲突子串。
_DIFF_COLOR_INDEX = (
    (4, ('白谱', '白譜', '白', 're:master', 'remaster', 're master', '白re', '白master')),
    (0, ('绿谱', '綠譜', '绿', '綠', 'basic', 'bas', 'easy')),
    (1, ('黄谱', '黃譜', '黄', '黃', '橙谱', '橙譜', '橙', 'advanced', 'adv', 'normal')),
    (2, ('红谱', '紅譜', '红', '紅', 'expert', 'exp', 'hard')),
    (3, ('紫谱', '紫譜', '紫', 'master', 'mst')),
)


def _resolve_diff_index(text: str) -> Optional[int]:
    """从文本中解析难度颜色，返回 ds 列表索引（0-4）。未命中返回 None。"""
    t = text.lower()
    for idx, kws in _DIFF_COLOR_INDEX:
        for kw in kws:
            if kw in t:
                return idx
    return None


def _q_ds(music: Music, text: str) -> Optional[str]:
    has_ds_kw = any(k in text for k in _DS_KEYWORDS)
    diff_idx = _resolve_diff_index(text)
    nums = _nums(text)
    # 既没定数关键词/形容词、也没难度颜色 + 数字 -> 不是定数问题
    if not has_ds_kw and not (diff_idx is not None and nums):
        return None
    # 必须指定谱面颜色（绿/黄/橙/红/紫/白）才回答定数——
    # 否则不知道玩家问哪个难度，不乱答，走 unknown 提示。
    if diff_idx is None:
        return None
    # 指定了颜色 -> 只看对应难度的 ds
    if diff_idx >= len(music.ds):
        # 该曲没有这个难度（如没有白谱）-> 前提不成立
        return _NO
    target_ds = music.ds[diff_idx]
    if not nums:
        # 问「紫谱定数高吗」「紫谱难吗」之类，按 13.5 阈值
        if any(k in text for k in ('高', '大', '难', '難')):
            return _yn(target_ds >= 13.5)
        if any(k in text for k in ('低', '小', '简单', '簡單', '易')):
            return _yn(target_ds <= 11.0)
        return None
    n = nums[0]
    if '+' in text:
        return _yn((n + 0.5) <= target_ds < (n + 1.0))
    res = _cmp_bool(target_ds, text, nums)
    if res is not None:
        return _yn(res)
    return _yn(n <= target_ds < n + 0.5)


_BARE_LEVEL_RE = re.compile(
    r'^(?:是|为|為)?\s*(\d{1,2})(\+)?\s*(?:级|級|等级|等級|定数|星)?\s*'
    r'(?:吗|嘛|？|\?)?$'
)


def _q_level_bare(music: Music, text: str) -> Optional[str]:
    """玩家直接问「是13吗」「14+吗」——没指定谱面颜色，无法判断哪个难度，不回答。"""
    # 无颜色裸数字定数问题统一不答（避免用最高定数乱猜），走 unknown 提示。
    return None


def _q_song_type(music: Music, text: str) -> Optional[str]:
    if not any(k in text for k in ('谱面', '譜面', '谱', '譜', 'sd', 'dx')):
        return None
    t = text
    # SD/DX 谱面类型判断：仅响应「标准谱/sd谱」「dx谱/dx谱面」等明确类型词。
    # 难度颜色（绿/黄/红/紫/白谱）交给 _q_ds 处理，不在这里误判为谱面类型。
    if any(k in t for k in ('dx谱', 'dx谱面', 'dx谱', 'dx譜面')):
        return _yn(music.type == 'DX')
    if any(k in t for k in ('标准谱', '標準譜', 'sd谱', 'sd譜', '标准谱面', '標準譜面')):
        return _yn(music.type == 'SD')
    return None


def _q_version(music: Music, text: str) -> Optional[str]:
    version_raw = music.basic_info.version or ''
    version = version_raw.lower()
    # 含难度颜色 + 「谱」时，是定数/谱面/谱师问题，不是版本问题，让出——
    # 否则「紫谱是谁的谱」会被「紫」单字误判为问 murasaki 版本。
    if _resolve_diff_index(text) is not None and any(k in text for k in ('谱', '譜')):
        return None
    # 注意：'为新' 不能作为新歌判断关键词——会误匹配「是新框体吗」等版本提问
    if any(k in text for k in ('新歌', '新曲', 'isnew', '新的')):
        return _yn(bool(music.basic_info.is_new))
    if any(k in text for k in ('旧曲', '舊曲', '老歌', '老曲', '旧的', '舊的')):
        return _yn(not music.basic_info.is_new)
    # 框体代际——新框体=DX 全系列（でらっくす 及其派生），旧框体=初代~finale
    if any(k in text for k in ('新框体', '新框', '老框体', '老框', '旧框体', '旧框')):
        is_dx = 'でらっくす' in version
        if any(k in text for k in ('新框体', '新框')):
            return _yn(is_dx)
        return _yn(not is_dx)
    # 国服合并叫法（双宴代/祭祝代等）——任一子版本都算
    for alias, versions in _VERSION_GROUP_ALIASES:
        if alias in text:
            return _yn(version in versions)
    # 单版本俗称——canonical 为完整版本字符串，做精确匹配
    # （PLUS 在前、基版在后，保证「华代」优先命中 PLUS 条目）
    for canonical, kws in _VERSION_KEYWORDS:
        if any(k in text for k in kws):
            return _yn(version == canonical)
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
    # 信息题（艺术家是谁/曲师是谁）-> 不回答，走 unknown
    if any(k in text for k in ('艺术家', 'artist', '作曲', '编曲', '編曲', '作者', '曲师', '曲師', '师匠', '師匠')) \
            and any(k in text for k in ('谁', '誰', '什么人', '什麼人', '哪位', '哪个', '哪個')):
        return None
    kw = _extract_artist_keyword(text)
    if not kw:
        return None
    # 单字符子串过宽（如「d」匹配「deco*27」），要求至少 2 字符才回答
    if len(kw) < 2:
        return None
    artist = (music.basic_info.artist or '').lower()
    return _yn(kw.lower() in artist)


# 中国玩家对谱师的俗称 -> 谱师字段里能唯一匹配的子串（小写比较）
# 来源：虎扑评分 / 音游论坛约定俗成的叫法
# 注意：key 用小写（中文无影响，英文部分需小写）；value 为谱师原名的子串
# 简繁归一化表（简体 -> 繁体，谱师原名多为繁体）
_SIMP_TO_TRAD = str.maketrans({
    '谱': '譜', '师': '師', '号': '號', '职': '職', '门': '門',
    '东': '東', '灯': '燈', '发': '發', '变': '變', '乐': '樂',
    '习': '習', '华': '華', '梦': '夢', '艺': '藝', '术': '術',
    '樱': '櫻', '晓': '曉', '堇': '菫', '绿': '綠',
})


def _norm_charter(s: str) -> str:
    """谱师名归一化：小写 + 简转繁 + 各种减号/长音符统一为半角 '-'。"""
    s = s.lower().translate(_SIMP_TO_TRAD)
    # 片假名长音符 ー、全角减号 −、en-dash –、em-dash — 都统一成半角 -
    return s.replace('ー', '-').replace('−', '-').replace('–', '-').replace('—', '-')


# 中国玩家对谱师的俗称 -> 谱师原名子串（归一化后比较，无需列简繁/符号变体）
# 来源：虎扑评分 / 音游论坛约定俗成的叫法
_CHARTER_ALIASES = {
    '川哥': ('隅田川星人',),
    '7.3': ('シチミヘルツ',),
    '7.3ghz': ('シチミヘルツ',),
    '抽象大师': ('譜面ー100号',),
    '麦斯达': ('mai-star',),
    '哈皮': ('はっぴー',),
    '甜口姜': ('あまくちジンジャー',),
    '红箭': ('redarrow',),
    '企鹅': ('ロシェ',),                # ロシェ＠ペンギン
    '鱼板君': ('カマボコ',),             # カマボコ君
    '鸽子': ('鳩',),                    # 鳩ホルダー
    '群青': ('群青',),                  # 群青リコリス
    '小鸟游': ('小鳥遊',),              # 小鳥遊さん
    '沙发太': ('サファ太',),
    '翠楼': ('翠楼屋',),
    '翠': ('サファ太', '翠楼屋'),       # 传闻同人，两者都算
    '太': ('翠楼屋', 'サファ太'),
    'withu': ('luxizhel',),             # 以代表作著称（_norm 去空格）
    '玉子豆腐': ('玉子豆腐',),
    '科技厨房': ('techno kitchen',),
    '帕奇猫': ('ぴちネコ',),
    '物黑': ('ものくろっく',),
    '寿喜烧奉行': ('すきやき奉行',),
    '烟花职人': ('華火職人',),
    '兔子洗衣店': ('うさぎランドリー',),
    '孤挺花': ('アマリリス',),
    '王道谱谱师': ('jack',),
}

# 预计算归一化后的别名表，加速查表
_CHARTER_ALIASES_NORM = {
    _norm_charter(k): tuple(_norm_charter(a) for a in v)
    for k, v in _CHARTER_ALIASES.items()
}


def _match_charter(kw: str, charters: List[str]) -> bool:
    """判断关键词是否匹配谱师。先查中国玩家俗称映射（归一化），再回退到子串匹配。"""
    kw_n = _norm_charter(kw)
    aliases = _CHARTER_ALIASES_NORM.get(kw_n)
    if aliases is not None:
        return any(any(a in _norm_charter(c) for a in aliases) for c in charters)
    return any(kw_n in _norm_charter(c) for c in charters)


def _extract_charter_keyword(text: str) -> Optional[str]:
    """从玩家提问提取谱师关键词。支持多种句式：
    - 谱师是X吗 / X写的谱吗 / X作的谱吗
    - 是X写的谱吗
    - 是不是X的谱 / X的谱吗（玩家直接把谱师名嵌进句子里）
    """
    # 句式1：谱师/写谱/... + X + 写的/作的/吗
    m = re.search(
        r'(?:谱师|譜師|写谱|寫譜|作谱|作譜|谱面作者|譜面作者|charter)'
        r'(?:是|为|為)?\s*(.+?)(?:写的|寫的|作的|谱的|譜的|吗|嗎|？|\?|$)',
        text,
    )
    if m:
        kw = m.group(1).strip(' 的是为為')
        return kw or None
    # 句式2：是X写的谱吗
    m = re.search(r'是\s*(.+?)\s*(?:写|寫|作)的(?:谱|譜)(?:吗|嗎)?$', text)
    if m:
        kw = m.group(1).strip()
        return kw or None
    # 句式3：是不是X的谱 / X的谱吗 —— 玩家直接把谱师名嵌入
    # 仅当文本以「的谱/的譜」结尾（可带 吗/？）时触发，提取中间的 X
    m = re.search(
        r'(?:是不是|是|为|為)?\s*(.+?)的(?:谱|譜)(?:吗|嗎|？|\?)?$',
        text,
    )
    if m:
        kw = m.group(1).strip(' 是不是为為')
        # 至少 2 字才认，避免「是 的谱」这类空提取
        if kw and len(kw) >= 2:
            return kw
    return None


def _q_charter(music: Music, text: str) -> Optional[str]:
    """谱师题——只回答是非题「谱师是X吗」，不直接报名字（避免开户籍）。"""
    # 信息题（谱师是谁/哪位谱师/谁的谱/谁写的谱）-> 不回答，走 unknown
    has_question = any(k in text for k in ('谁', '誰', '什么人', '什麼人', '哪位', '哪个', '哪個'))
    if has_question and any(k in text for k in (
        '谱师', '譜師', '写谱', '寫譜', '作谱', '作譜',
        '谱面作者', '譜面作者', 'charter', '谱', '譜',
    )):
        return None
    # 是非题：「谱师是XXX吗」
    kw = _extract_charter_keyword(text)
    if not kw:
        return None
    charters = _get_master_charters(music)
    if not charters:
        return _NO
    return _yn(_match_charter(kw, charters))


def _get_master_charters(music: Music) -> List[str]:
    """取 MASTER（及 Re:MASTER）难度的谱师，去重去空。"""
    result: List[str] = []
    seen: set = set()
    charts = getattr(music, 'charts', None) or []
    # charts 顺序：BASIC, ADVANCED, EXPERT, MASTER, Re:MASTER
    # 玩家最关心 MASTER（index 3），有 Re:MASTER（index 4）也一并取
    for idx in (3, 4):
        if idx < len(charts):
            charter = getattr(charts[idx], 'charter', None) or ''
            charter = charter.strip()
            if charter and charter != '-' and charter not in seen:
                seen.add(charter)
                result.append(charter)
    return result


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


# 注意：本玩法只回答「是/否」是非题，不直接给出谱师/曲师/BPM 数值/版本/分类
# 等客观信息（那样等于开户籍）。玩家想问这些，请用猜测形式：「谱师是X吗」「BPM 大于180吗」。


_QUESTION_HANDLERS: Tuple[QuestionHandler, ...] = (
    _q_white_chart,
    _q_song_type,
    _q_genre,
    _q_bpm,
    _q_ds,
    _q_level_bare,
    _q_version,
    _q_artist,
    _q_charter,
    _q_title_script,
    _q_title_length,
    _q_title_contains,
)

_UNKNOWN_HINT = (
    '唔…Milk 没听懂这个问题喵。提问请加「我问」前缀，只回答是/否：\n'
    '· 分类：「我问是术曲吗」「我问是东方曲吗」「我问是联动曲吗」\n'
    '· BPM：「我问 BPM 大于 180 吗」「我问这歌快吗」\n'
    '· 定数：必须指定颜色——「我问紫谱定数是 14 吗」「我问红谱是 13+ 吗」「我问有白谱吗」\n'
    '· 版本：「我问是双代吗」「我问是舞代吗」\n'
    '· 谱面：「我问是 DX 谱面吗」\n'
    '· 艺术家/谱师：「我问艺术家是 deco27 吗」「我问谱师是沙发太吗」（只回答是/否，不报名字）\n'
    '· 标题：「我问标题是英文吗」「我问标题里有 Bad 吗」\n'
    '注：定数问题请指明绿/黄/红/紫/白谱，否则无法回答。猜曲名用「我猜 曲名」。'
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


# 否定前缀——玩家说「不是X吗」「无X吗」时，把是/否回答反转。
# 注意：只处理明确的否定词开头，不处理「没/没有」（歧义太大，可能是疑问语气）。
_NEGATION_PREFIXES = ('不是', '无', '非', '没白', '没紫', '没黄')


def _apply_negation(raw_text: str, answer: str) -> str:
    """若玩家提问以否定词开头且回答是「是/否」，则反转回答。"""
    if answer not in (_YES, _NO):
        return answer
    stripped = raw_text.strip().lower().replace(' ', '')
    for prefix in _NEGATION_PREFIXES:
        if stripped.startswith(prefix):
            return _NO if answer == _YES else _YES
    return answer


def _summarize_qa(qa_list: List['QAEntry']) -> str:
    """每 6 次提问后，把已确认的信息拼成摘要。"""
    if not qa_list:
        return ''
    lines: List[str] = []
    used = len(qa_list)
    for entry in qa_list:
        # 原始问题精简（去掉「吗」「？」等）
        q = entry.question.strip().rstrip('吗嘛？?')
        a = entry.answer
        lines.append(f'· {q} → {a}')
    header = f'📋 已确认信息（{used} 次）：'
    return header + '\n' + '\n'.join(lines)



# 前缀区分两个阶段：
# - 「我问」用于问问题阶段（提问是非题）。
# - 「我猜」用于猜曲名阶段（问完 20 题后抢猜曲名）。
# 两者都支持「我问问」「我猜猜」「我问一下」「我猜一下」等变体。
# 不接受单独的「问」「猜」字（太宽泛，可能是语气词）。
_ASK_PREFIX_RE = re.compile(r'^我问(?:问|一下)?\s*[：:、，,\s]*')
_GUESS_PREFIX_RE = re.compile(r'^我猜(?:猜|一下)?\s*[：:、，,\s]*')


def _strip_prefix(text: str, regex) -> Tuple[str, bool]:
    """去掉指定前缀，返回 (剩余文本, 是否命中前缀)。"""
    m = regex.match(text)
    if not m:
        return text, False
    rest = text[m.end():].strip()
    if not rest:
        # 只有前缀没有内容，不算命中
        return text, False
    return rest, True


def _strip_ask_prefix(text: str) -> Tuple[str, bool]:
    return _strip_prefix(text, _ASK_PREFIX_RE)


def _strip_guess_prefix(text: str) -> Tuple[str, bool]:
    return _strip_prefix(text, _GUESS_PREFIX_RE)


def is_guess(text: str, answers: List[str]) -> bool:
    return match_guess_answer(text, answers)


twentyq_guess = Guess20QManager()
