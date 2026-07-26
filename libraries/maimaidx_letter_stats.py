"""舞萌开字母：按群持久化通关用时、自适应星级阈值、每日目标、纪录与排行榜。"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

from pydantic import BaseModel, Field

from ..config import Root, letter_stats_file, log
from .tool import writefile

GroupId = Union[int, str]

# 默认阈值（秒），比例 30:45:60:90:180 = 1:1.5:2:3:6
DEFAULT_FIVE_STAR = 30.0
MIN_FIVE_STAR = 15.0
MAX_FIVE_STAR = 30.0
STAR_RATIO: Dict[int, float] = {5: 1.0, 4: 1.5, 3: 2.0, 2: 3.0, 1: 6.0}
HISTORY_WINDOW = 40
MIN_SAMPLES_FOR_ADAPTIVE = 8

# 每日目标：按近 N 日活跃度动态缩放
DAILY_LOOKBACK_DAYS = 7
MIN_GOAL_CLEARS = 1
MAX_GOAL_CLEARS = 12
DEFAULT_GOAL_CLEARS = 2
MIN_GOAL_WEIGHT = 8
MAX_GOAL_WEIGHT = 120
DEFAULT_GOAL_WEIGHT = 16
MIN_SAMPLES_FOR_FASTEST_GOAL = 5

# data/ 侧镜像（与 static 下 letter_stats 同内容；优先读 data）
LETTER_DATA_DIR = Root / "data" / "letter"
letter_stats_data_file: Path = LETTER_DATA_DIR / "group_letter_stats.json"


@dataclass(frozen=True)
class StarThresholds:
    """五档上限：elapsed <= limits[stars] 得对应星；超出一星上限为 0 星。"""

    limits: Dict[int, float]  # keys 5..1
    adaptive: bool = False
    sample_count: int = 0

    @property
    def five_star(self) -> float:
        return float(self.limits[5])

    def star_for(self, elapsed: float) -> int:
        t = max(0.0, float(elapsed))
        for stars in (5, 4, 3, 2, 1):
            if t <= self.limits[stars]:
                return stars
        return 0

    def format_lines(self) -> str:
        mode = "自适应" if self.adaptive else "默认"
        parts = [
            f"⭐️×{s}≤{self.limits[s]:.3f}秒" for s in (5, 4, 3, 2, 1)
        ]
        return f"本局阈值（{mode}，样本 {self.sample_count}）：" + " / ".join(parts)


@dataclass(frozen=True)
class DailyGoalsSpec:
    """当日动态目标快照。"""

    clears: int
    weight: int
    fastest: Optional[float] = None  # 需不慢于此用时；None 表示不设速度目标

    def format_line(self) -> str:
        parts = [f"通关≥{self.clears}", f"贡献≥{self.weight}"]
        if self.fastest is not None:
            parts.append(f"最快≤{self.fastest:.3f}秒")
        return "今日目标：" + " · ".join(parts)


@dataclass
class ClearFeedback:
    """通关后需提示的达标 / 破纪录文案（各条目一天或每次破纪录只提示一次）。"""

    goal_tips: List[str] = field(default_factory=list)
    record_tips: List[str] = field(default_factory=list)

    @property
    def all_tips(self) -> List[str]:
        return [*self.goal_tips, *self.record_tips]


def default_thresholds(*, sample_count: int = 0) -> StarThresholds:
    five = DEFAULT_FIVE_STAR
    limits = {s: five * STAR_RATIO[s] for s in STAR_RATIO}
    return StarThresholds(limits=limits, adaptive=False, sample_count=sample_count)


def _percentile(sorted_vals: List[float], p: float) -> float:
    if not sorted_vals:
        return DEFAULT_FIVE_STAR
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    p = min(1.0, max(0.0, float(p)))
    idx = (len(sorted_vals) - 1) * p
    lo = int(idx)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = idx - lo
    return float(sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac)


def compute_thresholds(history_elapsed: List[float]) -> StarThresholds:
    """
    自适应五星阈值：
    - 样本不足时用默认 30/45/60/90/180
    - 否则取最近 HISTORY_WINDOW 局的 P35，夹紧到 [15, 30]
    - 其余星级按 1:1.5:2:3:6 相对五星缩放
    群打得越快，P35 越低，阈值越严（但五星不低于 15s）。
    """
    samples = [max(0.0, float(x)) for x in history_elapsed if x is not None]
    n = len(samples)
    if n < MIN_SAMPLES_FOR_ADAPTIVE:
        return default_thresholds(sample_count=n)
    window = sorted(samples[-HISTORY_WINDOW:])
    five = min(MAX_FIVE_STAR, max(MIN_FIVE_STAR, _percentile(window, 0.35)))
    limits = {s: five * STAR_RATIO[s] for s in STAR_RATIO}
    return StarThresholds(limits=limits, adaptive=True, sample_count=n)


def day_key(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(float(ts)))


def compute_daily_goals(
    clear_rows: List[Tuple[float, float, int]],
    *,
    now: Optional[float] = None,
) -> DailyGoalsSpec:
    """
    按近 DAILY_LOOKBACK_DAYS 日（不含今日）活跃度动态定目标。

    clear_rows: (at, elapsed, total_weight)
    - 通关局数：日均 ×1.2，夹紧 [1, 12]；无历史默认 2
    - 贡献权重：近几日日均权重 ×1.15，夹紧；无历史默认 16
    - 最快：近 HISTORY_WINDOW 局 P40；样本不足则不设
    """
    t = time.time() if now is None else float(now)
    today = day_key(t)
    # 近 lookback 日（不含今日）的日桶
    day_clears: Dict[str, int] = {}
    day_weight: Dict[str, int] = {}
    recent_elapsed: List[float] = []
    for at, elapsed, weight in clear_rows:
        d = day_key(at)
        if d == today:
            continue
        day_clears[d] = day_clears.get(d, 0) + 1
        day_weight[d] = day_weight.get(d, 0) + max(0, int(weight))
        recent_elapsed.append(float(elapsed))

    # 只看最近 lookback 个自然日窗口内的天数
    cutoff = t - DAILY_LOOKBACK_DAYS * 86400
    active_days = [
        d
        for d, _ in day_clears.items()
        if _day_start_ts(d) >= cutoff - 1
    ]
    if active_days:
        avg_clears = sum(day_clears[d] for d in active_days) / max(1, len(active_days))
        avg_weight = sum(day_weight.get(d, 0) for d in active_days) / max(
            1, len(active_days)
        )
        goal_clears = int(
            min(MAX_GOAL_CLEARS, max(MIN_GOAL_CLEARS, math.ceil(avg_clears * 1.2)))
        )
        goal_weight = int(
            min(MAX_GOAL_WEIGHT, max(MIN_GOAL_WEIGHT, math.ceil(avg_weight * 1.15)))
        )
    else:
        goal_clears = DEFAULT_GOAL_CLEARS
        goal_weight = DEFAULT_GOAL_WEIGHT

    fastest: Optional[float] = None
    samples = recent_elapsed[-HISTORY_WINDOW:]
    if len(samples) >= MIN_SAMPLES_FOR_FASTEST_GOAL:
        fastest = round(_percentile(sorted(samples), 0.40), 3)

    return DailyGoalsSpec(clears=goal_clears, weight=goal_weight, fastest=fastest)


def _day_start_ts(day: str) -> float:
    try:
        return time.mktime(time.strptime(day, "%Y-%m-%d"))
    except Exception:
        return 0.0


def is_better_record(
    *,
    kind: str,
    new_value: float,
    old_value: Optional[float],
) -> bool:
    """
    破纪录比较：
    - fastest：更小更好（需严格更小）
    - max：更大更好（需严格更大）
    old 为 None / 0（max 类）视为尚无纪录，首次可破。
    """
    if kind == "fastest":
        if old_value is None:
            return True
        return float(new_value) < float(old_value)
    # max
    if old_value is None:
        return float(new_value) > 0
    return float(new_value) > float(old_value)


class LetterClearRecord(BaseModel):
    elapsed: float
    stars: int = 0
    at: float = 0.0
    score_pool: int = 0
    break_pool: int = 0
    players: List[str] = Field(default_factory=list)
    total_weight: int = 0


class LetterMemberStats(BaseModel):
    uid: str = ""
    name: str = ""
    billing_id: int = 0
    score: int = 0
    weight: int = 0
    games: int = 0
    best_elapsed: Optional[float] = None


class LetterGroupRecords(BaseModel):
    """本群常驻纪录（破纪录时更新）。"""

    fastest_elapsed: Optional[float] = None
    fastest_by: str = ""
    max_single_weight: int = 0
    max_single_weight_by: str = ""
    max_daily_clears: int = 0
    max_daily_clears_day: str = ""
    max_member_weight: int = 0
    max_member_weight_by: str = ""


class LetterDayState(BaseModel):
    """当日计数与目标快照；换日重置。"""

    day: str = ""
    clears: int = 0
    total_weight: int = 0
    fastest: Optional[float] = None
    goal_clears: int = DEFAULT_GOAL_CLEARS
    goal_weight: int = DEFAULT_GOAL_WEIGHT
    goal_fastest: Optional[float] = None
    achieved: List[str] = Field(default_factory=list)


class LetterGroupStats(BaseModel):
    clears: List[LetterClearRecord] = Field(default_factory=list)
    members: Dict[str, LetterMemberStats] = Field(default_factory=dict)
    records: LetterGroupRecords = Field(default_factory=LetterGroupRecords)
    daily: LetterDayState = Field(default_factory=LetterDayState)
    display_mode: str = "auto"  # 群级持久展示模式：auto / text / image


class LetterStatsStore(BaseModel):
    groups: Dict[str, LetterGroupStats] = Field(default_factory=dict)


class LetterStatsManager:
    MAX_CLEARS_KEPT = 200

    def __init__(self) -> None:
        self.store = LetterStatsStore()
        self._load()

    def _primary_path(self) -> Path:
        """优先 data/letter，其次 static 下旧文件。"""
        if letter_stats_data_file.exists():
            return letter_stats_data_file
        return letter_stats_file

    def _load(self) -> None:
        path = self._primary_path()
        # 若 data 不存在但 static 有，从 static 读并后续写入 data
        if not path.exists() and letter_stats_file.exists():
            path = letter_stats_file
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self.store = LetterStatsStore.model_validate(json.load(f))
            except Exception as exc:
                log.warning(f"[LetterStats] 读取失败，使用空库：{type(exc).__name__}: {exc}")
                self.store = LetterStatsStore()
        else:
            self.store = LetterStatsStore()

    def _gid(self, gid: GroupId) -> str:
        return str(gid)

    def _group(self, gid: GroupId) -> LetterGroupStats:
        key = self._gid(gid)
        if key not in self.store.groups:
            self.store.groups[key] = LetterGroupStats()
        return self.store.groups[key]

    async def _save(self) -> None:
        LETTER_DATA_DIR.mkdir(parents=True, exist_ok=True)
        payload = self.store.model_dump()
        await writefile(letter_stats_data_file, payload)
        # 兼容：同步写 static 旧路径（若可写）
        try:
            await writefile(letter_stats_file, payload)
        except Exception as exc:
            log.warning(f"[LetterStats] 同步 static 失败：{type(exc).__name__}: {exc}")

    def history_elapsed(self, gid: GroupId) -> List[float]:
        group = self.store.groups.get(self._gid(gid))
        if not group:
            return []
        return [float(c.elapsed) for c in group.clears]

    def last_clear_elapsed(self, gid: GroupId) -> Optional[float]:
        """本群上一把通关用时；无记录返回 None。"""
        group = self.store.groups.get(self._gid(gid))
        if not group or not group.clears:
            return None
        return float(group.clears[-1].elapsed)

    def thresholds_for(self, gid: GroupId) -> StarThresholds:
        return compute_thresholds(self.history_elapsed(gid))

    def _ensure_daily(self, group: LetterGroupStats, *, now: float) -> LetterDayState:
        today = day_key(now)
        if group.daily.day != today:
            rows = [
                (float(c.at), float(c.elapsed), int(c.total_weight))
                for c in group.clears
            ]
            goals = compute_daily_goals(rows, now=now)
            # 从通关历史重建今日计数（进程重启不丢；已达标标记避免重复提示）
            clears_today = 0
            weight_today = 0
            fastest_today: Optional[float] = None
            for c in group.clears:
                if day_key(float(c.at)) != today:
                    continue
                clears_today += 1
                weight_today += max(0, int(c.total_weight))
                el = float(c.elapsed)
                if fastest_today is None or el < fastest_today:
                    fastest_today = el
            achieved: List[str] = []
            if clears_today >= goals.clears:
                achieved.append("clears")
            if weight_today >= goals.weight:
                achieved.append("weight")
            if (
                goals.fastest is not None
                and fastest_today is not None
                and fastest_today <= float(goals.fastest)
            ):
                achieved.append("fastest")
            group.daily = LetterDayState(
                day=today,
                clears=clears_today,
                total_weight=weight_today,
                fastest=fastest_today,
                goal_clears=goals.clears,
                goal_weight=goals.weight,
                goal_fastest=goals.fastest,
                achieved=achieved,
            )
        return group.daily

    def daily_goals_line(self, gid: GroupId, *, now: Optional[float] = None) -> str:
        """开局展示用：今日目标一行（会按需刷新日桶）。"""
        t = time.time() if now is None else float(now)
        group = self._group(gid)
        daily = self._ensure_daily(group, now=t)
        spec = DailyGoalsSpec(
            clears=daily.goal_clears,
            weight=daily.goal_weight,
            fastest=daily.goal_fastest,
        )
        return spec.format_line()

    async def record_clear(
        self,
        gid: GroupId,
        *,
        elapsed: float,
        stars: int,
        score_pool: int,
        break_pool: int,
        rewards: List[Tuple[str, int, str, int, int]],
        now: Optional[float] = None,
    ) -> ClearFeedback:
        """
        rewards: (uid, billing_id, name, score, weight)
        通关后写入；返回达标 / 破纪录提示（各只提示一次）。
        """
        t = time.time() if now is None else float(now)
        group = self._group(gid)
        daily = self._ensure_daily(group, now=t)
        feedback = ClearFeedback()

        players: List[str] = []
        total_weight = 0
        top_weight = 0
        top_weight_name = ""
        for uid, billing_id, name, score, weight in rewards:
            players.append(str(uid))
            w = max(0, int(weight))
            total_weight += w
            if w > top_weight:
                top_weight = w
                top_weight_name = name or str(uid)
            member = group.members.get(str(uid))
            if member is None:
                member = LetterMemberStats(uid=str(uid))
                group.members[str(uid)] = member
            member.uid = str(uid)
            member.name = name or member.name or str(uid)
            member.billing_id = int(billing_id)
            member.score += max(0, int(score))
            member.weight += w
            member.games += 1
            el = float(elapsed)
            if member.best_elapsed is None or el < member.best_elapsed:
                member.best_elapsed = el

            # 个人累计贡献破纪录（有旧纪录才提示，避免首局刷屏）
            rec = group.records
            if is_better_record(
                kind="max",
                new_value=member.weight,
                old_value=float(rec.max_member_weight) if rec.max_member_weight else None,
            ):
                old = rec.max_member_weight
                rec.max_member_weight = int(member.weight)
                rec.max_member_weight_by = member.name
                if old > 0:
                    feedback.record_tips.append(
                        f"🏆 破纪录！{member.name} 累计贡献权重 {member.weight}"
                        f"（原纪录 {old}）"
                    )

        group.clears.append(
            LetterClearRecord(
                elapsed=float(elapsed),
                stars=int(stars),
                at=t,
                score_pool=int(score_pool),
                break_pool=int(break_pool),
                players=players,
                total_weight=total_weight,
            )
        )
        if len(group.clears) > self.MAX_CLEARS_KEPT:
            group.clears = group.clears[-self.MAX_CLEARS_KEPT :]

        # —— 每日计数 ——
        daily.clears += 1
        daily.total_weight += total_weight
        if daily.fastest is None or float(elapsed) < float(daily.fastest):
            daily.fastest = float(elapsed)

        # —— 破纪录：本群最快（有旧纪录才提示）——
        rec = group.records
        if is_better_record(
            kind="fastest",
            new_value=float(elapsed),
            old_value=rec.fastest_elapsed,
        ):
            old = rec.fastest_elapsed
            rec.fastest_elapsed = float(elapsed)
            rec.fastest_by = top_weight_name or (players[0] if players else "")
            if old is not None:
                feedback.record_tips.append(
                    f"🏆 破纪录！本群最快通关 {elapsed:.3f}秒"
                    f"（原纪录 {old:.3f}秒）"
                )

        # —— 破纪录：单局最多贡献 ——
        if top_weight > 0 and is_better_record(
            kind="max",
            new_value=float(top_weight),
            old_value=float(rec.max_single_weight) if rec.max_single_weight else None,
        ):
            old = rec.max_single_weight
            rec.max_single_weight = int(top_weight)
            rec.max_single_weight_by = top_weight_name
            if old > 0:
                feedback.record_tips.append(
                    f"🏆 破纪录！{top_weight_name} 单局贡献 {top_weight}"
                    f"（原纪录 {old}）"
                )

        # —— 破纪录：单日最多通关 ——
        if is_better_record(
            kind="max",
            new_value=float(daily.clears),
            old_value=float(rec.max_daily_clears) if rec.max_daily_clears else None,
        ):
            old = rec.max_daily_clears
            old_day = rec.max_daily_clears_day
            rec.max_daily_clears = int(daily.clears)
            rec.max_daily_clears_day = daily.day
            if old > 0:
                feedback.record_tips.append(
                    f"🏆 破纪录！今日通关 {daily.clears} 局"
                    f"（原纪录 {old} 局，{old_day or '—'}）"
                )

        # —— 每日目标达成（各提示一次） ——
        achieved = set(daily.achieved)
        if (
            "clears" not in achieved
            and daily.clears >= daily.goal_clears
        ):
            achieved.add("clears")
            feedback.goal_tips.append(
                f"🎯 今日目标达成：通关 {daily.clears}/{daily.goal_clears} 局！"
            )
        if (
            "weight" not in achieved
            and daily.total_weight >= daily.goal_weight
        ):
            achieved.add("weight")
            feedback.goal_tips.append(
                f"🎯 今日目标达成：本群贡献 {daily.total_weight}/{daily.goal_weight}！"
            )
        if (
            "fastest" not in achieved
            and daily.goal_fastest is not None
            and daily.fastest is not None
            and float(daily.fastest) <= float(daily.goal_fastest)
        ):
            achieved.add("fastest")
            feedback.goal_tips.append(
                f"🎯 今日目标达成：最快通关 {daily.fastest:.3f}秒"
                f"（目标 ≤{daily.goal_fastest:.3f}秒）！"
            )
        daily.achieved = list(achieved)

        await self._save()
        return feedback

    def _members_with_uid(self, gid: GroupId) -> List[LetterMemberStats]:
        group = self.store.groups.get(self._gid(gid))
        if not group:
            return []
        rows: List[LetterMemberStats] = []
        for uid, m in group.members.items():
            if not m.uid:
                m.uid = str(uid)
            rows.append(m)
        return rows

    def score_ranking(self, gid: GroupId, *, top_n: int = 20) -> List[LetterMemberStats]:
        rows = [m for m in self._members_with_uid(gid) if m.score > 0]
        rows.sort(key=lambda m: (-m.score, -m.weight, m.name))
        return rows[:top_n]

    def contrib_ranking(self, gid: GroupId, *, top_n: int = 20) -> List[LetterMemberStats]:
        rows = [m for m in self._members_with_uid(gid) if m.weight > 0]
        rows.sort(key=lambda m: (-m.weight, -m.score, m.name))
        return rows[:top_n]

    def time_ranking(self, gid: GroupId, *, top_n: int = 20) -> List[LetterMemberStats]:
        rows = [m for m in self._members_with_uid(gid) if m.best_elapsed is not None]
        rows.sort(key=lambda m: (float(m.best_elapsed), -m.games, m.name))  # type: ignore[arg-type]
        return rows[:top_n]

    # ---- 群级展示模式设置（持久化） ----

    def get_display_mode(self, gid: GroupId) -> str:
        """Returns the persisted display mode for the group: 'auto', 'text', or 'image'."""
        group = self.store.groups.get(self._gid(gid))
        if group is None:
            return "auto"
        return group.display_mode

    async def set_display_mode(self, gid: GroupId, mode: str) -> None:
        """Persist the display mode for the group and save to disk."""
        self._group(gid).display_mode = mode
        await self._save()


letter_stats = LetterStatsManager()
