"""B50 找内鬼：在五张卡片中找出不属于题主 B50 的那一张。"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from loguru import logger as log

from .maimaidx_best_50 import computeRa
from .maimaidx_model import ChartInfo, UserInfo

IMPOSTOR_CARD_COUNT = 5
IMPOSTOR_DURATION = 45
IMPOSTOR_SCORE_REWARDS = (10, 6, 3)
IMPOSTOR_BREAK_REWARDS = (2, 1, 0)
# 有效参与人数（排除题主与内鬼）达到该值才可产生 BREAK
IMPOSTOR_BREAK_MIN_PLAYERS = 3


@dataclass
class ImpostorGuessEntry:
    uid: str
    name: str
    billing_id: int
    answer: int
    first_at: float


@dataclass
class ImpostorReward:
    uid: str
    name: str
    billing_id: int
    rank: int
    score: int
    break_points: int
    break_capped: bool = False


@dataclass
class ImpostorSettlement:
    target_uid: int
    target_name: str
    alien_uid: int
    alien_name: str
    answer: int
    rewards: List[ImpostorReward]
    wrong_names: List[str]


@dataclass
class GuessImpostorData:
    target_uid: int
    target_name: str
    alien_uid: int
    alien_name: str
    answer: int
    charts: List[ChartInfo]
    duration: int
    started_at: float
    end: bool = False
    entries: Dict[str, ImpostorGuessEntry] = field(default_factory=dict)

    def time_left(self) -> float:
        return max(0.0, self.duration - (time.time() - self.started_at))


class GuessImpostorManager:
    groups: Dict[int, GuessImpostorData] = {}
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

    def get(self, gid: int) -> Optional[GuessImpostorData]:
        return self.groups.get(gid)

    def end(
        self,
        gid: int,
        *,
        expected: Optional[GuessImpostorData] = None,
    ) -> Optional[GuessImpostorData]:
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
        target_uid: int,
        target_name: str,
        alien_uid: int,
        alien_name: str,
        answer: int,
        charts: List[ChartInfo],
        duration: int = IMPOSTOR_DURATION,
    ) -> GuessImpostorData:
        data = GuessImpostorData(
            target_uid=target_uid,
            target_name=target_name,
            alien_uid=alien_uid,
            alien_name=alien_name,
            answer=answer,
            charts=charts,
            duration=duration,
            started_at=time.time(),
        )
        self.locked.discard(gid)
        self.groups[gid] = data
        log.info(
            f'[GuessImpostor] 开局 gid={gid} target={target_name}({target_uid}) '
            f'alien={alien_name}({alien_uid}) answer={answer}'
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
        data = self.groups.get(gid)
        if data is None or data.end or not 1 <= answer <= len(data.charts):
            return ''
        if uid in data.entries:
            data.entries[uid].answer = answer
            # 速度榜按最终答案提交时间计算，防止先随便抢答再末秒改对。
            data.entries[uid].first_at = time.time()
            return f'✅ {name} 已修改答案'
        data.entries[uid] = ImpostorGuessEntry(
            uid=uid,
            name=name,
            billing_id=billing_id,
            answer=answer,
            first_at=time.time(),
        )
        return f'✅ {name} 已作答（{len(data.entries)}人参与）'

    def settle(self, gid: int) -> Optional[ImpostorSettlement]:
        data = self.groups.get(gid)
        if data is None:
            return None
        data.end = True
        # 排除题主与内鬼本人（他们知道答案）
        excluded = {int(data.target_uid), int(data.alien_uid)}
        valid_entries = [
            entry for entry in data.entries.values()
            if int(entry.billing_id) not in excluded
        ]
        correct = sorted(
            (entry for entry in valid_entries if entry.answer == data.answer),
            key=lambda entry: entry.first_at,
        )
        # 有效作答人数不足时不发 BREAK（积分照常）
        break_eligible = len(valid_entries) >= IMPOSTOR_BREAK_MIN_PLAYERS
        rewards: List[ImpostorReward] = []
        for index, entry in enumerate(correct):
            reward_index = min(index, len(IMPOSTOR_SCORE_REWARDS) - 1)
            rewards.append(ImpostorReward(
                uid=entry.uid,
                name=entry.name,
                billing_id=entry.billing_id,
                rank=index + 1,
                score=IMPOSTOR_SCORE_REWARDS[reward_index],
                break_points=(
                    IMPOSTOR_BREAK_REWARDS[reward_index]
                    if break_eligible and index < len(IMPOSTOR_BREAK_REWARDS)
                    else 0
                ),
            ))
        wrong_names = [
            entry.name for entry in valid_entries if entry.answer != data.answer
        ]
        return ImpostorSettlement(
            target_uid=data.target_uid,
            target_name=data.target_name,
            alien_uid=data.alien_uid,
            alien_name=data.alien_name,
            answer=data.answer,
            rewards=rewards,
            wrong_names=wrong_names,
        )


impostor_guess = GuessImpostorManager()


def _b50_charts(b50: UserInfo) -> List[ChartInfo]:
    sd = (b50.charts and b50.charts.sd) or []
    dx = (b50.charts and b50.charts.dx) or []
    return list(sd) + list(dx)


def build_impostor_cards(
    target_b50: UserInfo,
    alien_b50: UserInfo,
    count: int = IMPOSTOR_CARD_COUNT,
) -> Tuple[List[ChartInfo], int]:
    """从题主 B50 抽 ``count-1`` 张 + 内鬼来源 B50 抽 1 张。

    - 内鬼曲目要求不在题主 B50 里，避免撞歌。
    - 所有卡片 RA 按当前公式重新计算，避免历史定数造成额外线索。

    Returns:
        ``(展示卡片, 内鬼在展示列表中的 1-based 序号)``
    """
    target_charts = _b50_charts(target_b50)
    alien_charts = _b50_charts(alien_b50)
    if len(target_charts) < count - 1:
        raise ValueError(
            f'题主 B50 卡片不足：需要 {count - 1}，实际 {len(target_charts)}'
        )

    target_song_ids = {int(chart.song_id) for chart in target_charts}
    alien_candidates = [
        chart for chart in alien_charts
        if int(chart.song_id) not in target_song_ids
    ]
    if not alien_candidates:
        raise ValueError('内鬼来源与题主 B50 曲目完全重合，无法构造内鬼卡')

    normal_cards = [
        chart.model_copy(deep=True)
        for chart in random.sample(target_charts, count - 1)
    ]
    alien_card = random.choice(alien_candidates).model_copy(deep=True)

    cards = normal_cards + [alien_card]
    random.shuffle(cards)
    for chart in cards:
        chart.ra = int(computeRa(float(chart.ds), float(chart.achievements)))
    answer_index = cards.index(alien_card) + 1
    return cards, answer_index


def format_impostor_rewards(rewards: List[ImpostorReward]) -> str:
    if not rewards:
        return '本局无人找出内鬼。'
    lines: List[str] = []
    for reward in rewards:
        medal = {1: '🥇', 2: '🥈', 3: '🥉'}.get(reward.rank, '▫️')
        if reward.break_points > 0:
            bp = f' +{reward.break_points}BREAK'
        elif reward.break_capped:
            bp = ' +0 BREAK'
        else:
            bp = ''
        lines.append(
            f'{medal} #{reward.rank} {reward.name}  +{reward.score}分{bp}'
        )
    return '\n'.join(lines)
