"""猜Rating：看B50猜总Rating游戏。"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from loguru import logger as log

from .maimaidx_datasource import get_user_b50_or_fallback
from .maimaidx_group_rating import _get_group_member_list
from .maimaidx_model import ChartInfo, UserInfo

# ─────────────────────── 常量 ───────────────────────

DEFAULT_DURATION = 60
MIN_DURATION = 30  # 最短局时长（秒），不得提前结算

# BREAK 奖励统一规则：仅前两名可获得 BREAK，分别为 2 / 1。
# 积分（score_bonus）仍按难度递增，BREAK 与难度解耦。
RATING_BREAK_BONUS: Tuple[int, int, int] = (2, 1, 0)
# 第一名误差超过该值时，本局不发放任何 BREAK
RATING_BREAK_MAX_DIFF = 200
# 参与人数（不含题主）达到该值才可产生 BREAK 奖励
RATING_BREAK_MIN_PLAYERS = 3


@dataclass(frozen=True)
class RatingDifficulty:
    """猜 Rating 难度配置。难度越高，展示卡片和辅助信息越少。"""

    level: int
    display_count: int
    show_rate: bool
    show_fc_fs: bool
    hide_cover: bool
    score_bonus: Tuple[int, int, int]
    break_bonus: Tuple[int, int, int]


RATING_DIFFICULTIES: Dict[int, RatingDifficulty] = {
    1: RatingDifficulty(1, 20, True,  True,  False, (15, 5, 3), RATING_BREAK_BONUS),
    2: RatingDifficulty(2, 16, True,  False, False, (18, 6, 4), RATING_BREAK_BONUS),
    3: RatingDifficulty(3, 12, False, False, False, (21, 7, 5), RATING_BREAK_BONUS),
    4: RatingDifficulty(4, 8,  False, False, False, (24, 8, 6), RATING_BREAK_BONUS),
    5: RatingDifficulty(5, 8,  False, False, True,  (30, 10, 7), RATING_BREAK_BONUS),
}

PARTICIPATION_SCORE = 1  # 参与奖（仅积分，无 BREAK）


# ─────────────────────── 数据结构 ───────────────────────

@dataclass
class RatingGuessEntry:
    """单个玩家的答题记录。"""
    uid: str
    name: str
    billing_id: int
    answer: int
    first_at: float
    diff: int = 0  # |answer - actual|, 结算时填


@dataclass
class RatingGuessReward:
    """单个玩家的结算奖励。"""
    uid: str
    billing_id: int
    name: str
    rank: int
    diff: int
    score: int
    break_points: int
    break_capped: bool = False


@dataclass
class RatingGuessSettlement:
    """一局结算数据。"""
    target_uid: int
    target_name: str
    target_rating: int
    elapsed: float
    rewards: List[RatingGuessReward]


@dataclass
class GuessRatingData:
    """一局猜Rating的游戏状态。"""
    target_uid: int
    target_name: str
    target_rating: int
    difficulty: int
    display_count: int
    total_chart_count: int
    duration: int
    started_at: float
    end: bool = False
    entries: Dict[str, RatingGuessEntry] = field(default_factory=dict)
    selected_charts: List[ChartInfo] = field(default_factory=list)
    b50_sd: List[ChartInfo] = field(default_factory=list)
    b50_dx: List[ChartInfo] = field(default_factory=list)

    def time_left(self) -> float:
        return max(0.0, self.duration - (time.time() - self.started_at))

    def is_over(self) -> bool:
        return self.end or self.time_left() <= 0


# ─────────────────────── 管理器 ───────────────────────

class GuessRatingManager:
    """管理各群的猜Rating会话。"""

    groups: Dict[int, GuessRatingData] = {}
    # 正在开局中的群（防止并发重复开局）
    locked: set = set()
    # 志愿者报名：{gid: {billing_id: 过期时间戳}}
    volunteers: Dict[int, Dict[int, float]] = {}
    # 上局被抽中的人：{gid: target_uid}，防连抽
    last_target: Dict[int, int] = {}

    VOLUNTEER_TTL = 600  # 报名有效期 10 分钟
    VOLUNTEER_WEIGHT = 5.0  # 志愿者权重倍率
    LAST_TARGET_WEIGHT = 0.1  # 上局目标权重衰减

    def is_busy(self, gid: int) -> bool:
        return gid in self.groups or gid in self.locked

    def lock(self, gid: int) -> bool:
        """尝试锁住本群开始开局。返回 False 表示已被占用。"""
        if gid in self.groups or gid in self.locked:
            return False
        self.locked.add(gid)
        return True

    def unlock(self, gid: int) -> None:
        """开局失败时释放锁。"""
        self.locked.discard(gid)
        from .maimaidx_game_session import game_session_gate
        game_session_gate.release(gid)

    def get(self, gid: int) -> Optional[GuessRatingData]:
        return self.groups.get(gid)

    def end(
        self,
        gid: int,
        *,
        expected: Optional[GuessRatingData] = None,
    ) -> Optional[GuessRatingData]:
        if expected is not None and self.groups.get(gid) is not expected:
            return None
        self.locked.discard(gid)
        from .maimaidx_game_session import game_session_gate
        game_session_gate.release(gid)
        return self.groups.pop(gid, None)

    def add_volunteer(self, gid: int, billing_id: int) -> None:
        """登记志愿者（下局猜Rating抽中概率提升）。"""
        self.volunteers.setdefault(gid, {})[int(billing_id)] = (
            time.time() + self.VOLUNTEER_TTL
        )

    def active_volunteers(self, gid: int) -> set:
        """本群当前有效的志愿者 billing_id 集合（顺带清理过期）。"""
        vols = self.volunteers.get(gid)
        if not vols:
            return set()
        now = time.time()
        expired = [u for u, exp in vols.items() if exp < now]
        for u in expired:
            vols.pop(u, None)
        return set(vols.keys())

    def clear_volunteers(self, gid: int) -> None:
        """开局后清空本群志愿者（下局需重新报名）。"""
        self.volunteers.pop(gid, None)

    def weighted_pick(self, gid: int, pool: List[Tuple[int, str]]) -> Tuple[int, str]:
        """按权重抽选候选人：志愿者×5，上局目标×0.1。"""
        vols = self.active_volunteers(gid)
        last = self.last_target.get(gid)
        weights: List[float] = []
        for uid, _name in pool:
            w = 1.0
            if uid in vols:
                w *= self.VOLUNTEER_WEIGHT
            if uid == last and len(pool) > 1:
                w *= self.LAST_TARGET_WEIGHT
            weights.append(w)
        return random.choices(pool, weights=weights, k=1)[0]

    def start(
        self,
        gid: int,
        *,
        target_uid: int,
        target_name: str,
        target_rating: int,
        difficulty: int,
        display_count: int,
        total_chart_count: int,
        duration: int,
        selected_charts: List[ChartInfo],
        b50_sd: List[ChartInfo],
        b50_dx: List[ChartInfo],
    ) -> GuessRatingData:
        data = GuessRatingData(
            target_uid=target_uid,
            target_name=target_name,
            target_rating=target_rating,
            difficulty=difficulty,
            display_count=display_count,
            total_chart_count=total_chart_count,
            duration=duration,
            started_at=time.time(),
            selected_charts=selected_charts,
            b50_sd=b50_sd,
            b50_dx=b50_dx,
        )
        self.locked.discard(gid)
        self.groups[gid] = data
        log.info(
            f'[GuessRating] 开局 gid={gid} target={target_name}({target_uid}) '
            f'rating={target_rating} difficulty={difficulty} '
            f'display={len(selected_charts)}/{total_chart_count} duration={duration}s'
        )
        return data

    def submit(
        self,
        gid: int,
        uid: str,
        name: str,
        billing_id: int,
        answer: int,
    ) -> str:
        """提交/修改答案。返回提示文案。

        题主同样可以作答、修改、被统计在参与人数中；结算时按 billing_id
        过滤题主，不计入排名和奖励。
        """
        data = self.groups.get(gid)
        if data is None or data.end:
            return ''
        if uid in data.entries:
            data.entries[uid].answer = answer
            return f'✅ {name} 已修改答案为 {answer}'
        data.entries[uid] = RatingGuessEntry(
            uid=uid,
            name=name,
            billing_id=billing_id,
            answer=answer,
            first_at=time.time(),
        )
        return f'✅ {name} 已作答（{len(data.entries)}人参与）'

    def settle(self, gid: int) -> Optional[RatingGuessSettlement]:
        """结算：计算排名与奖励。

        BREAK 奖励规则（与难度解耦）：
        - 仅前两名可获得 BREAK，分别为 2 / 1；
        - 有效参与人数（不含题主）不足 3 人时，本局不产生 BREAK；
        - 第一名误差超过 RATING_BREAK_MAX_DIFF 时，本局不产生 BREAK。
        积分（score）仍按难度前三发放，其余参与者 1 分参与奖。
        """
        data = self.groups.get(gid)
        if data is None:
            return None
        data.end = True
        actual = data.target_rating

        # 计算误差
        for entry in data.entries.values():
            entry.diff = abs(entry.answer - actual)

        # 按误差排序
        # 结算再过滤一次题主，防止旧状态或平台 ID 映射绕过 submit 检查。
        ranked = sorted(
            (
                entry for entry in data.entries.values()
                if int(entry.billing_id) != int(data.target_uid)
            ),
            key=lambda e: (e.diff, e.first_at),
        )

        # 判断本局是否产生 BREAK
        break_eligible = len(ranked) >= RATING_BREAK_MIN_PLAYERS
        if break_eligible and ranked:
            top_diff = ranked[0].diff
            if top_diff > RATING_BREAK_MAX_DIFF:
                break_eligible = False

        rewards: List[RatingGuessReward] = []
        difficulty = RATING_DIFFICULTIES.get(
            data.difficulty, RATING_DIFFICULTIES[1]
        )
        for i, entry in enumerate(ranked):
            rank = i + 1
            if 1 <= rank <= 3:
                score = difficulty.score_bonus[rank - 1]
            else:
                score = PARTICIPATION_SCORE
            # BREAK：仅前两名，且需满足参与人数与第一名误差门槛
            if break_eligible and 1 <= rank <= 2:
                bp = RATING_BREAK_BONUS[rank - 1]
            else:
                bp = 0
            rewards.append(RatingGuessReward(
                uid=entry.uid,
                billing_id=entry.billing_id,
                name=entry.name,
                rank=rank,
                diff=entry.diff,
                score=score,
                break_points=bp,
            ))

        return RatingGuessSettlement(
            target_uid=data.target_uid,
            target_name=data.target_name,
            target_rating=actual,
            elapsed=time.time() - data.started_at,
            rewards=rewards,
        )


rating_guess = GuessRatingManager()


# ─────────────────────── 候选人筛选 ───────────────────────

async def pick_random_candidate(
    bot,
    group_id,
    *,
    min_charts: int = 1,
    weighted: bool = True,
    exclude_uids: Optional[set] = None,
) -> Optional[Tuple[int, str, UserInfo]]:
    """从群成员中随机选一个有B50数据的人。

    Returns:
        (uid, display_name, b50) 或 None
    """
    from .maimaidx_data_storage import data_storage

    exclude = {int(x) for x in (exclude_uids or set())}

    # 官方 QQ 没有全量成员列表 API。群成绩模块会从已见成员登记表
    # 中读取 openid，并通过 qbind/论坛绑定转换为可查询的旧 QQ；OneBot
    # 则仍调用原生 get_group_member_list，避免改变既有行为。
    try:
        raw = await _get_group_member_list(bot, group_id)
    except Exception as e:
        log.warning(f'[GuessRating] 获取群成员失败 gid={group_id}: {e}')
        return None
    if not raw or not isinstance(raw, list):
        return None

    self_id = str(getattr(bot, 'self_id', ''))
    members = [
        m for m in raw
        if m.get('user_id') is not None and str(m['user_id']) != self_id
    ]
    if not members:
        return None

    all_members: List[Tuple[int, str]] = [
        (int(m['user_id']), m.get('nickname') or m.get('card') or str(m['user_id']))
        for m in members
        if int(m['user_id']) not in exclude
    ]

    # 优先从本地数据存储找有快照的用户。
    # 启用集合一次读入 + 快照索引扫描放线程，避免在事件循环里逐成员读文件
    def _scan_with_data() -> List[Tuple[int, str]]:
        enabled = data_storage.enabled_users()
        if not enabled:
            return []
        return [
            (uid, name)
            for uid, name in all_members
            if uid in enabled and data_storage.list_snapshots(uid, limit=1)
        ]

    candidates_with_data = await asyncio.to_thread(_scan_with_data)

    # 优先选有本地数据的
    pool = candidates_with_data if candidates_with_data else all_members
    if not pool:
        return None

    # 加权不放回抽取最多 5 个，并发拉取 B50，按抽取顺序取首个可用
    picks: List[Tuple[int, str]] = []
    pool_copy = list(pool)
    for _ in range(min(5, len(pool_copy))):
        if weighted:
            uid, name = rating_guess.weighted_pick(group_id, pool_copy)
        else:
            uid, name = random.choice(pool_copy)
        picks.append((uid, name))
        pool_copy = [(u, n) for u, n in pool_copy if u != uid]

    results = await asyncio.gather(
        *(get_user_b50_or_fallback(qqid=uid) for uid, _ in picks),
        return_exceptions=True,
    )
    for (uid, name), b50 in zip(picks, results):
        if isinstance(b50, Exception):
            log.debug(f'[GuessRating] 拉取B50失败 uid={uid}: {b50}')
            continue
        chart_count = 0
        if b50 and b50.charts:
            chart_count = len(b50.charts.sd or []) + len(b50.charts.dx or [])
        if b50 and b50.rating is not None and chart_count >= max(1, min_charts):
            return uid, name, b50

    return None


def select_random_charts(
    b50: UserInfo,
    count: int,
) -> Tuple[List[ChartInfo], List[ChartInfo], List[ChartInfo]]:
    """从B50中随机抽取count首。

    Returns:
        (selected, sd_best, dx_best)
    """
    sd = (b50.charts and b50.charts.sd) or []
    dx = (b50.charts and b50.charts.dx) or []
    all_charts = list(sd) + list(dx)
    if not all_charts:
        return [], sd, dx
    count = min(count, len(all_charts))
    selected = random.sample(all_charts, count)
    return selected, sd, dx


def format_countdown(seconds: float) -> str:
    """格式化倒计时。"""
    s = max(0, int(seconds))
    if s >= 60:
        return f'{s // 60}分{s % 60}秒'
    return f'{s}秒'


def format_reward_text(rewards: List[RatingGuessReward], actual: int) -> str:
    """格式化排名奖励文案。"""
    if not rewards:
        return '本局无人作答。'
    lines: List[str] = []
    for r in rewards:
        medal = {1: '🥇', 2: '🥈', 3: '🥉'}.get(r.rank, '▫️')
        diff_text = f'差值{r.diff}'
        if r.break_points > 0:
            bp_text = f' +{r.break_points}BREAK'
        elif r.break_capped:
            bp_text = ' +0 BREAK'
        else:
            bp_text = ''
        lines.append(f'{medal} #{r.rank} {r.name}  {diff_text}  +{r.score}分{bp_text}')
    return '\n'.join(lines)
