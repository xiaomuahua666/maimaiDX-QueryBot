"""猜Rating：看B50猜总Rating游戏。"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from loguru import logger as log

from .maimaidx_datasource import get_user_b50_or_fallback
from .maimaidx_model import ChartInfo, UserInfo

# ─────────────────────── 常量 ───────────────────────

DEFAULT_DISPLAY_COUNT = 20
DEFAULT_DURATION = 60
MIN_DISPLAY_COUNT = 10
MAX_DISPLAY_COUNT = 50

# 固定收益表: (score, break_points)
REWARD_TABLE: Dict[int, Tuple[int, int]] = {
    1: (15, 3),   # 🥇 最接近
    2: (5, 1),    # 🥈 第二
    3: (3, 0),    # 🥉 第三
}
PARTICIPATION_SCORE = 1  # 参与奖


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
    display_count: int
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
    # 志愿者报名：{gid: {billing_id: 过期时间戳}}
    volunteers: Dict[int, Dict[int, float]] = {}
    # 上局被抽中的人：{gid: target_uid}，防连抽
    last_target: Dict[int, int] = {}

    VOLUNTEER_TTL = 600  # 报名有效期 10 分钟
    VOLUNTEER_WEIGHT = 5.0  # 志愿者权重倍率
    LAST_TARGET_WEIGHT = 0.1  # 上局目标权重衰减

    def is_busy(self, gid: int) -> bool:
        return gid in self.groups

    def get(self, gid: int) -> Optional[GuessRatingData]:
        return self.groups.get(gid)

    def end(self, gid: int) -> Optional[GuessRatingData]:
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
        display_count: int,
        duration: int,
        selected_charts: List[ChartInfo],
        b50_sd: List[ChartInfo],
        b50_dx: List[ChartInfo],
    ) -> GuessRatingData:
        data = GuessRatingData(
            target_uid=target_uid,
            target_name=target_name,
            target_rating=target_rating,
            display_count=display_count,
            duration=duration,
            started_at=time.time(),
            selected_charts=selected_charts,
            b50_sd=b50_sd,
            b50_dx=b50_dx,
        )
        self.groups[gid] = data
        log.info(
            f'[GuessRating] 开局 gid={gid} target={target_name}({target_uid}) '
            f'rating={target_rating} display={len(selected_charts)} duration={duration}s'
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
        """提交/修改答案。返回提示文案。"""
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
        count = len(data.entries)
        return f'✅ {name} 已作答（{count}人参与）'

    def settle(self, gid: int) -> Optional[RatingGuessSettlement]:
        """结算：计算排名与奖励。"""
        data = self.groups.get(gid)
        if data is None:
            return None
        data.end = True
        actual = data.target_rating

        # 计算误差
        for entry in data.entries.values():
            entry.diff = abs(entry.answer - actual)

        # 按误差排序
        ranked = sorted(data.entries.values(), key=lambda e: (e.diff, e.first_at))

        rewards: List[RatingGuessReward] = []
        for i, entry in enumerate(ranked):
            rank = i + 1
            if rank in REWARD_TABLE:
                score, bp = REWARD_TABLE[rank]
            else:
                score, bp = PARTICIPATION_SCORE, 0
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
    group_id: int,
) -> Optional[Tuple[int, str, UserInfo]]:
    """从群成员中随机选一个有B50数据的人。

    Returns:
        (uid, display_name, b50) 或 None
    """
    from .maimaidx_data_storage import data_storage

    # 获取群成员列表
    try:
        raw = await bot.call_api('get_group_member_list', group_id=group_id)
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

    # 优先从本地数据存储找有快照的用户
    candidates_with_data: List[Tuple[int, str]] = []
    all_members: List[Tuple[int, str]] = []
    for m in members:
        uid = int(m['user_id'])
        name = m.get('nickname') or m.get('card') or str(uid)
        all_members.append((uid, name))
        if data_storage.is_enabled(uid):
            snapshots = data_storage.list_snapshots(uid, limit=1)
            if snapshots:
                candidates_with_data.append((uid, name))

    # 优先选有本地数据的
    pool = candidates_with_data if candidates_with_data else all_members
    if not pool:
        return None

    # 加权不放回抽样，最多尝试 5 个
    pool_copy = list(pool)
    for _ in range(min(5, len(pool_copy))):
        uid, name = rating_guess.weighted_pick(group_id, pool_copy)
        pool_copy = [(u, n) for u, n in pool_copy if u != uid]
        try:
            b50 = await get_user_b50_or_fallback(qqid=uid)
            if b50 and b50.rating is not None and b50.charts:
                return uid, name, b50
        except Exception as e:
            log.debug(f'[GuessRating] 拉取B50失败 uid={uid}: {e}')
            continue

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
        bp_text = f' +{r.break_points}BREAK' if r.break_points > 0 else ''
        lines.append(f'{medal} #{r.rank} {r.name}  {diff_text}  +{r.score}分{bp_text}')
    return '\n'.join(lines)
