"""B50 找内鬼：在五张 B50 卡片中找出单曲 RA 被篡改的一张。"""

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


@dataclass
class ImpostorSettlement:
    target_uid: int
    target_name: str
    answer: int
    actual_ra: int
    fake_ra: int
    rewards: List[ImpostorReward]
    wrong_names: List[str]


@dataclass
class GuessImpostorData:
    target_uid: int
    target_name: str
    answer: int
    actual_ra: int
    fake_ra: int
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

    def get(self, gid: int) -> Optional[GuessImpostorData]:
        return self.groups.get(gid)

    def end(self, gid: int) -> Optional[GuessImpostorData]:
        self.locked.discard(gid)
        return self.groups.pop(gid, None)

    def start(
        self,
        gid: int,
        *,
        target_uid: int,
        target_name: str,
        answer: int,
        actual_ra: int,
        fake_ra: int,
        charts: List[ChartInfo],
        duration: int = IMPOSTOR_DURATION,
    ) -> GuessImpostorData:
        data = GuessImpostorData(
            target_uid=target_uid,
            target_name=target_name,
            answer=answer,
            actual_ra=actual_ra,
            fake_ra=fake_ra,
            charts=charts,
            duration=duration,
            started_at=time.time(),
        )
        self.locked.discard(gid)
        self.groups[gid] = data
        log.info(
            f'[GuessImpostor] 开局 gid={gid} target={target_name}({target_uid}) '
            f'answer={answer} ra={actual_ra}->{fake_ra}'
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
        valid_entries = [
            entry for entry in data.entries.values()
            if int(entry.billing_id) != int(data.target_uid)
        ]
        correct = sorted(
            (entry for entry in valid_entries if entry.answer == data.answer),
            key=lambda entry: entry.first_at,
        )
        rewards: List[ImpostorReward] = []
        for index, entry in enumerate(correct):
            reward_index = min(index, len(IMPOSTOR_SCORE_REWARDS) - 1)
            rewards.append(ImpostorReward(
                uid=entry.uid,
                name=entry.name,
                billing_id=entry.billing_id,
                rank=index + 1,
                score=IMPOSTOR_SCORE_REWARDS[reward_index],
                break_points=IMPOSTOR_BREAK_REWARDS[reward_index],
            ))
        wrong_names = [
            entry.name for entry in valid_entries if entry.answer != data.answer
        ]
        return ImpostorSettlement(
            target_uid=data.target_uid,
            target_name=data.target_name,
            answer=data.answer,
            actual_ra=data.actual_ra,
            fake_ra=data.fake_ra,
            rewards=rewards,
            wrong_names=wrong_names,
        )


impostor_guess = GuessImpostorManager()


def build_impostor_cards(
    b50: UserInfo,
    count: int = IMPOSTOR_CARD_COUNT,
) -> Tuple[List[ChartInfo], int, int, int]:
    """抽取卡片并篡改其中一张 RA。

    返回 ``(展示卡片, 内鬼序号, 真实RA, 假RA)``。所有正常卡片先按当前
    公式校正 RA，避免上游历史定数导致一局出现多个“内鬼”。
    """
    sd = (b50.charts and b50.charts.sd) or []
    dx = (b50.charts and b50.charts.dx) or []
    source = list(sd) + list(dx)
    if len(source) < count:
        raise ValueError(f'B50 卡片不足：需要 {count}，实际 {len(source)}')

    charts = [chart.model_copy(deep=True) for chart in random.sample(source, count)]
    for chart in charts:
        chart.ra = int(computeRa(float(chart.ds), float(chart.achievements)))

    impostor_index = random.randrange(len(charts))
    actual_ra = int(charts[impostor_index].ra)
    delta = random.randint(6, 12) * random.choice((-1, 1))
    fake_ra = max(1, actual_ra + delta)
    if fake_ra == actual_ra:
        fake_ra = actual_ra + 6
    charts[impostor_index].ra = fake_ra
    return charts, impostor_index + 1, actual_ra, fake_ra


def format_impostor_rewards(rewards: List[ImpostorReward]) -> str:
    if not rewards:
        return '本局无人找出内鬼。'
    lines: List[str] = []
    for reward in rewards:
        medal = {1: '🥇', 2: '🥈', 3: '🥉'}.get(reward.rank, '▫️')
        bp = f' +{reward.break_points}BREAK' if reward.break_points else ''
        lines.append(
            f'{medal} #{reward.rank} {reward.name}  +{reward.score}分{bp}'
        )
    return '\n'.join(lines)
