"""舞萌极限二选一：每轮展示两张谱面，玩家发送 左/右 比较单一指标。

- 5 轮逐渐变难，每轮题面唯一答案
- 出题时排除数值相同的组合
- 局内不显示选择人数；提交后不可改
- 首轮可加入，第二轮后禁止中途参赛
- 题目开局一次性生成并预渲染
- 全部通关时按最终轮正确作答时间排位
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from loguru import logger as log

from .maimaidx_music import mai
from .maimaidx_model import Music


# ─────────────────────── 常量 ───────────────────────

DUEL_ROUNDS = 5
DUEL_ROUND_DURATION = 20  # 每轮作答秒数
# 通过 1~5 轮累计积分
DUEL_ROUND_SCORES = (1, 2, 4, 7, 12)
# 全通关前三额外 BREAK 奖励
DUEL_BREAK_BONUS = (2, 1, 0)
# 提问类型
TYPE_DS = 'ds'           # 定数
TYPE_NOTES = 'notes'     # 物量
TYPE_BPM = 'bpm'         # BPM
TYPE_VERSION = 'version' # 收录版本
TYPE_BREAK = 'break'     # BREAK 数量
TYPE_TOUCH = 'touch'     # TOUCH 数量（仅 DX）
TYPE_LEVEL = 'level'     # 等级

TYPE_LABELS = {
    TYPE_DS: '定数',
    TYPE_NOTES: '物量',
    TYPE_BPM: 'BPM',
    TYPE_VERSION: '收录版本',
    TYPE_BREAK: 'BREAK 数',
    TYPE_TOUCH: 'TOUCH 数',
    TYPE_LEVEL: '等级',
}

TYPE_PROMPTS = {
    TYPE_DS: '哪张谱面定数更高？',
    TYPE_NOTES: '哪张谱面物量更多？',
    TYPE_BPM: '哪首 BPM 更快？',
    TYPE_VERSION: '哪首收录版本更早？',
    TYPE_BREAK: '哪张谱面 BREAK 数量更多？',
    TYPE_TOUCH: '哪张谱面 TOUCH 更多？',
    TYPE_LEVEL: '哪张谱面等级更高？',
}


# ─────────────────────── 等级排序 ───────────────────────

_LEVEL_KEY_CACHE: Dict[str, float] = {}


def _level_key(lv: str) -> float:
    """把等级字符串转成可比较的浮点（如 14+ > 14 > 13+）。"""
    if lv in _LEVEL_KEY_CACHE:
        return _LEVEL_KEY_CACHE[lv]
    try:
        if lv.endswith('+'):
            base = float(lv[:-1])
            v = base + 0.5
        else:
            v = float(lv)
    except (TypeError, ValueError):
        v = 0.0
    _LEVEL_KEY_CACHE[lv] = v
    return v


# ─────────────────────── 版本排序 ───────────────────────

# 收录版本从老到新，便于"更早"判断。
_VERSION_ORDER: List[str] = [
    'maimaiでらっくす',
    'maimaiでらっくす PLUS',
    'Splash',
    'Splash PLUS',
    'UNiVERSE',
    'UNiVERSE PLUS',
    'FESTiVAL',
    'FESTiVAL PLUS',
    'BUDDiES',
    'BUDDiES PLUS',
    'PRiSM',
    'PRiSM PLUS',
    'CiRCLE',
    'CiRCLE PLUS',
]


def _version_index(v: str) -> int:
    """版本在时间线上的位置，0 越老。未知版本返回一个大值。"""
    if v in _VERSION_ORDER:
        return _VERSION_ORDER.index(v)
    # 兼容某些老版本/标准谱版本名：尽量靠前
    for i, name in enumerate(_VERSION_ORDER):
        if name in v:
            return i
    return len(_VERSION_ORDER) + 100


# ─────────────────────── 数据结构 ───────────────────────

@dataclass(frozen=True)
class ChartRef:
    """指向一首歌某一个难度的引用。"""
    music_id: str
    title: str
    level_index: int     # 0=Basic..4=Remaster
    level: str           # 例如 "13+"
    ds: float            # 定数
    chart_type: str      # 'SD' / 'DX'
    bpm: int
    version: str         # 收录版本（basic_info.version）
    notes_total: int
    notes_break: int
    notes_touch: int

    @property
    def is_dx(self) -> bool:
        return self.chart_type == 'DX'


@dataclass
class DuelRound:
    question_type: str
    prompt: str
    left: ChartRef
    right: ChartRef
    answer: int  # 1=左胜, 2=右胜
    round_no: int


@dataclass
class DuelParticipant:
    uid: str
    name: str
    billing_id: int
    last_answer_at: float = 0.0
    answers: Dict[int, int] = field(default_factory=dict)  # round_no -> 1/2
    correct_rounds: int = 0
    last_correct_at: float = 0.0
    eliminated_round: int = 0  # 0=未淘汰
    final_score: int = 0
    finish_rank: int = 0  # 全通关时的排名
    finish_time: float = 0.0  # 最后一轮提交时间


@dataclass
class DuelSettlement:
    rewards: List[Tuple[str, str, int, int, int]]  # (uid, name, rank, score, break)
    survivors: List[str]  # 全通关 uid 列表
    eliminated_count: int


@dataclass
class GuessDuelData:
    rounds: List[DuelRound]
    round_durations: int
    participants: Dict[str, DuelParticipant] = field(default_factory=dict)
    start_round_at: float = 0.0
    current_round: int = 0  # 0 表示开局前
    locked_after_first: bool = False
    end: bool = False
    survivor_uids: List[str] = field(default_factory=list)
    started_at: float = 0.0

    def time_left(self) -> float:
        return max(0.0, self.round_durations - (time.time() - self.start_round_at))

    def is_over(self) -> bool:
        return self.end or self.time_left() <= 0


# ─────────────────────── 选曲工具 ───────────────────────

def _build_chart_ref(music: Music, level_index: int) -> Optional[ChartRef]:
    if not (0 <= level_index < len(music.charts)):
        return None
    chart = music.charts[level_index]
    if chart is None:
        return None
    notes = chart.notes
    tap = int(getattr(notes, 'tap', 0) or 0)
    hold = int(getattr(notes, 'hold', 0) or 0)
    slide = int(getattr(notes, 'slide', 0) or 0)
    touch = int(getattr(notes, 'touch', 0) or 0)
    brk = int(getattr(notes, 'brk', 0) or 0)
    notes_total = tap + hold + slide + touch + brk
    if notes_total <= 0:
        return None
    ds = float(music.ds[level_index] or 0)
    if ds <= 0:
        return None
    return ChartRef(
        music_id=str(music.id),
        title=str(music.title),
        level_index=level_index,
        level=str(music.level[level_index] or ''),
        ds=ds,
        chart_type=str(music.type),
        bpm=int(music.basic_info.bpm or 0),
        version=str(music.basic_info.version or ''),
        notes_total=notes_total,
        notes_break=brk,
        notes_touch=touch,
    )


def _candidate_charts() -> List[ChartRef]:
    """枚举可出题的谱面（剔除 utage/Remaster 缺数据/物量过小）。"""
    if not mai.total_list:
        return []
    out: List[ChartRef] = []
    for music in mai.total_list:
        if int(music.id) >= 100000:
            continue
        for level_index in range(min(4, len(music.charts))):  # 0..3 基础到 Master
            ref = _build_chart_ref(music, level_index)
            if ref is None:
                continue
            if ref.notes_total < 50:
                continue
            out.append(ref)
    return out


# ─────────────────────── 出题 ───────────────────────

def _type_for_round(round_no: int) -> List[str]:
    if round_no == 1:
        return [TYPE_DS]
    if round_no == 2:
        return [TYPE_BPM, TYPE_NOTES, TYPE_DS]
    if round_no == 3:
        return [TYPE_VERSION]
    if round_no == 4:
        return [TYPE_DS, TYPE_LEVEL]
    if round_no == 5:
        return [TYPE_BREAK, TYPE_NOTES, TYPE_TOUCH, TYPE_LEVEL]
    return [TYPE_DS]


def _round_filters(round_no: int) -> Dict[str, object]:
    """每轮额外的差异要求 + 题目约束。"""
    if round_no == 1:
        return {'ds_min_gap': 0.5, 'exclude_types': []}
    if round_no == 2:
        return {
            'ds_min_gap': 0.0,
            'strong_gap': True,  # BPM 或物量有明显差距
            'exclude_types': [],
        }
    if round_no == 3:
        return {'ds_min_gap': 0.0, 'version_adjacent': True, 'exclude_types': []}
    if round_no == 4:
        return {
            'ds_min_gap': 0.1, 'ds_max_gap': 0.2,
            'exclude_types': [TYPE_VERSION],
        }
    if round_no == 5:
        return {
            'same_level': True, 'same_type': True,
            'exclude_types': [TYPE_VERSION, TYPE_BPM, TYPE_DS],
        }
    return {}


def _diff_pair(
    left: ChartRef, right: ChartRef, qtype: str,
) -> Tuple[int, bool]:
    """比较两张谱面；返回 (answer 1/2, 满足差异 True)。相同为 (0, False)。"""
    if qtype == TYPE_DS:
        if abs(left.ds - right.ds) <= 0.05:
            return 0, False
        return (1, True) if left.ds > right.ds else (2, True)
    if qtype == TYPE_BPM:
        if left.bpm == right.bpm:
            return 0, False
        return (1, True) if left.bpm > right.bpm else (2, True)
    if qtype == TYPE_NOTES:
        if left.notes_total == right.notes_total:
            return 0, False
        return (1, True) if left.notes_total > right.notes_total else (2, True)
    if qtype == TYPE_BREAK:
        if left.notes_break == right.notes_break:
            return 0, False
        return (1, True) if left.notes_break > right.notes_break else (2, True)
    if qtype == TYPE_TOUCH:
        if (left.notes_touch == 0 and right.notes_touch == 0):
            return 0, False
        if left.notes_touch == right.notes_touch:
            return 0, False
        return (1, True) if left.notes_touch > right.notes_touch else (2, True)
    if qtype == TYPE_VERSION:
        li, ri = _version_index(left.version), _version_index(right.version)
        if li == ri:
            return 0, False
        return (1, True) if li < ri else (2, True)
    if qtype == TYPE_LEVEL:
        lk, rk = _level_key(left.level), _level_key(right.level)
        if lk == rk:
            return 0, False
        return (1, True) if lk > rk else (2, True)
    return 0, False


def _meets_constraints(left: ChartRef, right: ChartRef, filters: dict) -> bool:
    if filters.get('same_level') and left.level != right.level:
        return False
    if filters.get('same_type') and left.chart_type != right.chart_type:
        return False
    if 'ds_min_gap' in filters:
        gap = abs(left.ds - right.ds)
        if gap < filters['ds_min_gap']:
            return False
    if 'ds_max_gap' in filters:
        gap = abs(left.ds - right.ds)
        if gap > filters['ds_max_gap']:
            return False
    if filters.get('version_adjacent'):
        li, ri = _version_index(left.version), _version_index(right.version)
        if abs(li - ri) != 1:
            return False
    return True


def _try_make_round(
    pool: List[ChartRef],
    qtype: str,
    filters: dict,
    used_pairs: set,
    *,
    max_tries: int = 80,
) -> Optional[DuelRound]:
    exclude_types = set(filters.get('exclude_types') or [])
    if qtype in exclude_types:
        return None
    for _ in range(max_tries):
        if len(pool) < 2:
            return None
        left, right = random.sample(pool, 2)
        if not _meets_constraints(left, right, filters):
            continue
        answer, ok = _diff_pair(left, right, qtype)
        if not ok:
            continue
        if qtype == TYPE_TOUCH and (left.notes_touch == 0 or right.notes_touch == 0):
            continue
        if filters.get('strong_gap'):
            bpm_gap = abs(left.bpm - right.bpm) >= 15
            notes_gap = abs(left.notes_total - right.notes_total) >= 100
            ds_gap = abs(left.ds - right.ds) >= 0.3
            if not (bpm_gap or notes_gap or ds_gap):
                continue
        pair_key = (left.music_id, left.level_index, right.music_id, right.level_index)
        if pair_key in used_pairs or tuple(reversed(pair_key)) in used_pairs:
            continue
        used_pairs.add(pair_key)
        return DuelRound(
            question_type=qtype,
            prompt=TYPE_PROMPTS[qtype],
            left=left,
            right=right,
            answer=answer,
            round_no=0,
        )
    return None


def _make_round_with_fallback(
    pool: List[ChartRef],
    round_no: int,
    used_pairs: set,
) -> Optional[DuelRound]:
    types = _type_for_round(round_no)
    filters = _round_filters(round_no)
    random.shuffle(types)
    for qtype in types:
        round_obj = _try_make_round(pool, qtype, filters, used_pairs)
        if round_obj is not None:
            return round_obj
    # 回退：定数差 ≥0.3
    fallback = {'ds_min_gap': 0.3, 'exclude_types': [TYPE_VERSION, TYPE_TOUCH]}
    for qtype in (TYPE_DS, TYPE_NOTES, TYPE_BREAK):
        round_obj = _try_make_round(pool, qtype, fallback, used_pairs)
        if round_obj is not None:
            return round_obj
    return None


def build_duel_rounds(
    *,
    count: int = DUEL_ROUNDS,
    duration: int = DUEL_ROUND_DURATION,
) -> List[DuelRound]:
    pool = _candidate_charts()
    if len(pool) < 4:
        return []
    rounds: List[DuelRound] = []
    used_pairs: set = set()
    for r in range(1, count + 1):
        round_obj = _make_round_with_fallback(pool, r, used_pairs)
        if round_obj is None:
            log.warning(f'[Duel] 第 {r} 轮出题失败，截断到 {len(rounds)} 轮')
            break
        round_obj.round_no = len(rounds) + 1
        rounds.append(round_obj)
    return rounds


# ─────────────────────── 管理器 ───────────────────────

class GuessDuelManager:
    groups: Dict[int, GuessDuelData] = {}
    locked: set = set()

    def is_busy(self, gid) -> bool:
        return gid in self.groups or gid in self.locked

    def lock(self, gid) -> bool:
        if self.is_busy(gid):
            return False
        self.locked.add(gid)
        return True

    def unlock(self, gid) -> None:
        self.locked.discard(gid)

    def get(self, gid) -> Optional[GuessDuelData]:
        return self.groups.get(gid)

    def end(self, gid) -> Optional[GuessDuelData]:
        self.locked.discard(gid)
        return self.groups.pop(gid, None)

    def start(
        self,
        gid,
        *,
        rounds: List[DuelRound],
        duration: int = DUEL_ROUND_DURATION,
    ) -> GuessDuelData:
        data = GuessDuelData(
            rounds=rounds,
            round_durations=duration,
            start_round_at=time.time(),
            started_at=time.time(),
        )
        self.locked.discard(gid)
        self.groups[gid] = data
        log.info(
            f'[Duel] 开局 gid={gid} rounds={len(rounds)} duration={duration}s'
        )
        return data

    def join(self, gid, uid: str, name: str, billing_id: int) -> Tuple[bool, str]:
        """加入当前局（仅在第一轮生效）。"""
        data = self.groups.get(gid)
        if data is None or data.end:
            return False, '当前没有进行中的舞萌极限二选一。'
        if data.locked_after_first:
            return False, '本局第二轮已开始，无法中途加入。'
        if uid in data.participants:
            return False, ''
        data.participants[uid] = DuelParticipant(
            uid=uid, name=name, billing_id=int(billing_id or 0),
        )
        return True, ''

    def submit(
        self, gid, uid: str, choice: int,
        *,
        name: str = '',
        billing_id: int = 0,
    ) -> Tuple[bool, str, bool]:
        """提交答案。返回 (accepted, message, was_new).

        第 1 轮允许自动加入（方便看到题面直接答），之后必须先加入。
        """
        data = self.groups.get(gid)
        if data is None or data.end or data.current_round == 0:
            return False, '', False
        round_no = data.current_round
        if round_no > len(data.rounds):
            return False, '', False
        participant = data.participants.get(uid)
        is_new = participant is None
        if is_new:
            if data.locked_after_first or round_no > 1:
                return False, '本局第二轮已开始，无法中途加入。', False
            participant = DuelParticipant(
                uid=uid, name=name, billing_id=int(billing_id or 0),
            )
            data.participants[uid] = participant
        elif name and not participant.name:
            participant.name = name
        elif billing_id and not participant.billing_id:
            participant.billing_id = int(billing_id)
        if participant.eliminated_round:
            return False, f'你已在第 {participant.eliminated_round} 轮淘汰。', False
        if round_no in participant.answers:
            return False, '本轮已作答，无法修改。', False
        if choice not in (1, 2):
            return False, '', False
        participant.answers[round_no] = choice
        participant.last_answer_at = time.time()
        return True, '', is_new

    def lock_after_first_round(self, gid) -> None:
        data = self.groups.get(gid)
        if data is None:
            return
        data.locked_after_first = True
        data.survivor_uids = [
            uid for uid, p in data.participants.items()
            if p.eliminated_round == 0
        ]

    def settle_round(self, gid) -> Tuple[List[str], List[str], int]:
        """结算本轮：返回 (出局 uid, 晋级 uid, 全通关人数)。"""
        data = self.groups.get(gid)
        if data is None or data.current_round == 0:
            return [], [], 0
        round_no = data.current_round
        if round_no > len(data.rounds):
            return [], [], 0
        round_obj = data.rounds[round_no - 1]
        correct_answer = round_obj.answer
        eliminated: List[str] = []
        survivors: List[str] = []
        for uid, participant in data.participants.items():
            if participant.eliminated_round:
                continue
            ans = participant.answers.get(round_no)
            if ans is None or ans != correct_answer:
                participant.eliminated_round = round_no
                eliminated.append(uid)
            else:
                participant.correct_rounds = round_no
                participant.last_correct_at = time.time()
                survivors.append(uid)
        # 累计积分：晋级者按当前已通过轮数算；淘汰者保留晋级前的轮数
        for uid in survivors:
            participant = data.participants[uid]
            participant.final_score = sum(DUEL_ROUND_SCORES[:round_no])
        for uid in eliminated:
            participant = data.participants[uid]
            if participant.final_score == 0 and round_no > 1:
                participant.final_score = sum(DUEL_ROUND_SCORES[:round_no - 1])
        all_clear = len(survivors) if round_no == len(data.rounds) else 0
        return eliminated, survivors, all_clear

    def settle_final(self, gid) -> Optional[DuelSettlement]:
        data = self.groups.get(gid)
        if data is None:
            return None
        data.end = True
        total_rounds = len(data.rounds)
        survivors: List[DuelParticipant] = [
            p for p in data.participants.values()
            if p.eliminated_round == 0 and p.correct_rounds == total_rounds
        ]
        # 全部通关：按最后一轮提交时间升序排位；都到齐但都没答对时按加入时间
        survivors.sort(key=lambda p: p.last_correct_at if p.last_correct_at else 1e18)
        for i, p in enumerate(survivors):
            p.finish_rank = i + 1
            p.finish_time = p.last_correct_at

        rewards: List[Tuple[str, str, int, int, int]] = []
        for p in survivors:
            base_score = sum(DUEL_ROUND_SCORES[:total_rounds])
            rank_idx = min(p.finish_rank - 1, len(DUEL_BREAK_BONUS) - 1)
            break_bonus = DUEL_BREAK_BONUS[rank_idx]
            rewards.append((
                p.uid, p.name, p.finish_rank, base_score, break_bonus,
            ))
        # 已淘汰但有累计积分的（由各轮结算时填入 final_score）
        for p in data.participants.values():
            if p.eliminated_round and p.final_score == 0 and p.eliminated_round > 1:
                p.final_score = sum(DUEL_ROUND_SCORES[:p.eliminated_round - 1])
        eliminated_count = sum(
            1 for p in data.participants.values() if p.eliminated_round
        )
        return DuelSettlement(
            rewards=rewards,
            survivors=[p.uid for p in survivors],
            eliminated_count=eliminated_count,
        )


duel_guess = GuessDuelManager()
