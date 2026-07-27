import json
from datetime import date, datetime, timedelta
from typing import Dict, List, NamedTuple, Optional, Tuple, Union

from loguru import logger as log
from pydantic import BaseModel, Field

from ..config import guess_score_events_file, guess_score_file, guess_score_history_file
from .maimaidx_group_rating import build_forward_node
from .maimaidx_platform import GroupId, UserId, format_forward_nodes_as_text, is_likely_qq_group_id, send_group_plain_text
from .tool import writefile


class PeriodSpec(NamedTuple):
    score_attr: str
    key_attr: str
    label: str
    rank_label: str
    board_title: str


class GuessMemberScore(BaseModel):
    score: int = 0
    name: str = ''
    streak: int = 0
    daily_score: int = 0
    daily_key: str = ''
    weekly_score: int = 0
    weekly_week: str = ''
    monthly_score: int = 0
    monthly_key: str = ''
    yearly_score: int = 0
    yearly_key: str = ''
    season_score: int = 0
    season_key: str = ''


class GuessGroupScores(BaseModel):
    members: Dict[str, GuessMemberScore] = Field(default_factory=dict)
    archived_periods: Dict[str, str] = Field(default_factory=dict)


class GuessScoreStore(BaseModel):
    groups: Dict[str, GuessGroupScores] = Field(default_factory=dict)


class GuessHistoryEntry(BaseModel):
    uid: str
    name: str
    score: int


class GuessHistoryRecord(BaseModel):
    period: str
    period_key: str
    archived_at: str
    ranking: List[GuessHistoryEntry] = Field(default_factory=list)


class GuessHistoryGroup(BaseModel):
    records: List[GuessHistoryRecord] = Field(default_factory=list)


class GuessScoreHistoryStore(BaseModel):
    groups: Dict[str, GuessHistoryGroup] = Field(default_factory=dict)


class GuessScoreEvent(BaseModel):
    """单次猜对结算明细（按模式+时间，供个人数据图）。"""
    uid: str
    name: str = ''
    mode: str
    points: int
    at: str = ''


class GuessScoreEventGroup(BaseModel):
    events: List[GuessScoreEvent] = Field(default_factory=list)


class GuessScoreEventStore(BaseModel):
    groups: Dict[str, GuessScoreEventGroup] = Field(default_factory=dict)


class GuessScoreManager:

    PIC_POINTS = {4: 4, 3: 3, 2: 2, 1: 1}
    PIC_CLEAR_POINTS = 1
    SONG_MAX_POINTS = 7
    AUDIO_MIN_POINTS = 5
    AUDIO_MAX_POINTS = 10
    AUDIO_STAGE_POINTS = (10, 9, 7, 5)
    # 猜铺面：按时扣分（可叠加首答/首阶段/加倍卡）
    CHART_POINTS = 10
    # 猜曲子赛季限时双倍（含 2026-06-30 当天）
    AUDIO_SEASON_DOUBLE_END = date(2026, 6, 30)
    # 猜铺面限时双倍（含 2026-07-26 当天）
    CHART_SEASON_DOUBLE_END = date(2026, 7, 26)
    MAX_HISTORY_PER_PERIOD = 30
    MAX_EVENTS_PER_GROUP = 8000

    # 个人数据图六模式（含开字母、猜Rating结算分）
    MODE_SONG = 'song'
    MODE_PIC = 'pic'
    MODE_AUDIO = 'audio'
    MODE_CHART = 'chart'
    MODE_LETTER = 'letter'
    MODE_RATING = 'rating'
    GUESS_MODES = (MODE_SONG, MODE_PIC, MODE_AUDIO, MODE_CHART, MODE_LETTER, MODE_RATING)
    MODE_LABELS = {
        MODE_SONG: '猜歌',
        MODE_PIC: '猜曲绘',
        MODE_AUDIO: '猜曲子',
        MODE_CHART: '猜铺面',
        MODE_LETTER: '开字母',
        MODE_RATING: '猜Rating',
    }

    PERIODS: Dict[str, PeriodSpec] = {
        'daily': PeriodSpec('daily_score', 'daily_key', '今日', '日榜', '日榜'),
        'weekly': PeriodSpec('weekly_score', 'weekly_week', '本周', '周榜', '周榜'),
        'monthly': PeriodSpec('monthly_score', 'monthly_key', '本月', '月榜', '月榜'),
        'yearly': PeriodSpec('yearly_score', 'yearly_key', '今年', '年榜', '年榜'),
        'season': PeriodSpec('season_score', 'season_key', '赛季', '赛季榜', '赛季榜'),
    }

    def pic_points_for(self, data) -> int:
        if data.interference_cleared:
            return self.PIC_CLEAR_POINTS
        return self.PIC_POINTS.get(data.difficulty, 1)

    def song_points_for(self, hint_step: int) -> int:
        # 开局与 1/7 提示均为最高分，随后每多一条提示减 1 分，封面 7/7 为 1 分
        return max(1, self.SONG_MAX_POINTS + 1 - max(hint_step, 1))

    def audio_points_for(self, hint_step: int) -> int:
        if hint_step <= 0:
            return self.AUDIO_MAX_POINTS
        idx = min(int(hint_step), len(self.AUDIO_STAGE_POINTS)) - 1
        return self.AUDIO_STAGE_POINTS[idx]

    def chart_points_for(
        self,
        hint_step: int = 1,
        *,
        elapsed_sec: float = 0.0,
        bgm_elapsed_sec: float = 0.0,
    ) -> int:
        """BGM 前：10 起每 7 秒 -1 最低 4；BGM 后：3 起每 20 秒 -1 最低 1。"""
        if int(hint_step) >= 2:
            return max(1, 3 - int(max(0.0, bgm_elapsed_sec)) // 20)
        return max(4, 10 - int(max(0.0, elapsed_sec)) // 7)

    @classmethod
    def audio_season_double_active(cls) -> bool:
        return date.today() <= cls.AUDIO_SEASON_DOUBLE_END

    @classmethod
    def chart_season_double_active(cls) -> bool:
        return date.today() <= cls.CHART_SEASON_DOUBLE_END

    def __init__(self) -> None:
        if guess_score_file.exists():
            with open(guess_score_file, 'r', encoding='utf-8') as f:
                self.store = GuessScoreStore.model_validate(json.load(f))
        else:
            self.store = GuessScoreStore()
        if guess_score_history_file.exists():
            with open(guess_score_history_file, 'r', encoding='utf-8') as f:
                self.history_store = GuessScoreHistoryStore.model_validate(json.load(f))
        else:
            self.history_store = GuessScoreHistoryStore()
        if guess_score_events_file.exists():
            try:
                with open(guess_score_events_file, 'r', encoding='utf-8') as f:
                    self.event_store = GuessScoreEventStore.model_validate(json.load(f))
            except Exception as e:
                log.warning(
                    f'[maimai] 读取猜歌明细失败，使用空库: {type(e).__name__}: {e}'
                )
                self.event_store = GuessScoreEventStore()
        else:
            self.event_store = GuessScoreEventStore()

    @staticmethod
    def _gid_key(gid: GroupId) -> str:
        return str(gid)

    @staticmethod
    def _uid_key(uid: UserId) -> str:
        return str(uid)

    @staticmethod
    def current_day_key() -> str:
        return datetime.now().strftime('%Y-%m-%d')

    @staticmethod
    def current_week_key() -> str:
        year, week, _ = datetime.now().isocalendar()
        return f'{year}-W{week:02d}'

    @staticmethod
    def current_month_key() -> str:
        return datetime.now().strftime('%Y-%m')

    @staticmethod
    def current_year_key() -> str:
        return datetime.now().strftime('%Y')

    @staticmethod
    def current_season_key() -> str:
        now = datetime.now()
        season = (now.month - 1) // 3 + 1
        return f'{now.year}-S{season}'

    @classmethod
    def period_key(cls, period: str) -> str:
        return {
            'daily': cls.current_day_key(),
            'weekly': cls.current_week_key(),
            'monthly': cls.current_month_key(),
            'yearly': cls.current_year_key(),
            'season': cls.current_season_key(),
        }[period]

    async def _save(self) -> None:
        await writefile(guess_score_file, self.store.model_dump())

    async def _save_history(self) -> None:
        await writefile(guess_score_history_file, self.history_store.model_dump())

    async def _save_events(self) -> None:
        await writefile(guess_score_events_file, self.event_store.model_dump())

    def _get_event_group(self, gid: GroupId) -> GuessScoreEventGroup:
        gk = self._gid_key(gid)
        if gk not in self.event_store.groups:
            self.event_store.groups[gk] = GuessScoreEventGroup()
        return self.event_store.groups[gk]

    def _append_event(
        self,
        gid: GroupId,
        uid: UserId,
        name: str,
        mode: str,
        points: int,
    ) -> None:
        if mode not in self.MODE_LABELS:
            return
        added = max(0, int(points))
        if added <= 0:
            return
        group = self._get_event_group(gid)
        group.events.append(
            GuessScoreEvent(
                uid=self._uid_key(uid),
                name=name or '',
                mode=mode,
                points=added,
                at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            )
        )
        if len(group.events) > self.MAX_EVENTS_PER_GROUP:
            group.events = group.events[-self.MAX_EVENTS_PER_GROUP :]

    def list_user_events(
        self,
        gid: GroupId,
        uid: UserId,
        *,
        days: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> List[GuessScoreEvent]:
        group = self.event_store.groups.get(self._gid_key(gid))
        if not group:
            return []
        uk = self._uid_key(uid)
        items = [e for e in group.events if e.uid == uk and e.mode in self.MODE_LABELS]
        if days is not None and days > 0:
            cutoff = datetime.now() - timedelta(days=days)
            filtered: List[GuessScoreEvent] = []
            for e in items:
                try:
                    ts = datetime.strptime(e.at, '%Y-%m-%d %H:%M:%S')
                except ValueError:
                    continue
                if ts >= cutoff:
                    filtered.append(e)
            items = filtered
        if limit is not None and limit > 0:
            return items[-limit:]
        return items

    def build_user_guess_stats(
        self,
        gid: GroupId,
        uid: UserId,
        *,
        days: int = 30,
        recent_n: int = 12,
    ) -> Dict:
        """个人猜歌数据图用快照：五模式趋势 / 雷达 / 近期明细（含开字母结算）。"""
        uk = self._uid_key(uid)
        member = self.store.groups.get(self._gid_key(gid), GuessGroupScores()).members.get(uk)
        name = (member.name if member else '') or uk
        total_score = member.score if member else 0
        events = self.list_user_events(gid, uid)
        window = self.list_user_events(gid, uid, days=days)

        modes: Dict[str, Dict[str, Union[int, str, None]]] = {}
        for mode in self.GUESS_MODES:
            mode_events = [e for e in events if e.mode == mode]
            last_at = mode_events[-1].at if mode_events else None
            if last_at and len(last_at) >= 16:
                last_short = last_at[5:16]  # MM-DD HH:MM
            else:
                last_short = last_at
            modes[mode] = {
                'count': len(mode_events),
                'points': sum(e.points for e in mode_events),
                'last_at': last_short,
            }

        day_keys: List[str] = []
        day_index: Dict[str, int] = {}
        today = datetime.now().date()
        for i in range(days - 1, -1, -1):
            d = today - timedelta(days=i)
            key = d.strftime('%Y-%m-%d')
            day_index[key] = len(day_keys)
            day_keys.append(key)
        series: Dict[str, List[int]] = {m: [0] * days for m in self.GUESS_MODES}
        for e in window:
            try:
                key = datetime.strptime(e.at, '%Y-%m-%d %H:%M:%S').strftime('%Y-%m-%d')
            except ValueError:
                continue
            idx = day_index.get(key)
            if idx is None or e.mode not in series:
                continue
            series[e.mode][idx] += e.points

        recent_raw = events[-recent_n:] if recent_n > 0 else []
        recent = []
        for e in reversed(recent_raw):
            at = e.at[5:16] if len(e.at) >= 16 else e.at
            recent.append({'mode': e.mode, 'points': e.points, 'at': at})

        radar_labels = [self.MODE_LABELS[m] for m in self.GUESS_MODES]
        radar_points = [int(modes[m]['points'] or 0) for m in self.GUESS_MODES]
        radar_counts = [int(modes[m]['count'] or 0) for m in self.GUESS_MODES]
        max_pts = max(radar_points) if radar_points else 0
        max_cnt = max(radar_counts) if radar_counts else 0
        points_norm = [
            (p / max_pts) if max_pts > 0 else 0.0 for p in radar_points
        ]
        counts_norm = [
            (c / max_cnt) if max_cnt > 0 else 0.0 for c in radar_counts
        ]

        return {
            'uid': uk,
            'name': name,
            'total_score': total_score,
            'total_rank': self.get_rank(gid, uid),
            'period_snapshot': self.get_period_snapshot(gid, uid),
            'modes': modes,
            'daily_series': {
                'labels': [k[5:] for k in day_keys],  # MM-DD
                **series,
            },
            'recent': recent,
            'radar': {
                'labels': radar_labels,
                'modes': list(self.GUESS_MODES),
                'points': radar_points,
                'counts': radar_counts,
                'points_norm': points_norm,
                'counts_norm': counts_norm,
            },
            'note': (
                f'趋势/明细自明细落库后实时更新；'
                f'近 {days} 日五模式共 {len(window)} 次。'
            ),
        }

    @classmethod
    def previous_period_key(cls, period: str) -> str:
        now = datetime.now()
        if period == 'daily':
            return (now - timedelta(days=1)).strftime('%Y-%m-%d')
        if period == 'weekly':
            prev = now - timedelta(days=7)
            year, week, _ = prev.isocalendar()
            return f'{year}-W{week:02d}'
        if period == 'monthly':
            first = now.replace(day=1)
            prev = first - timedelta(days=1)
            return prev.strftime('%Y-%m')
        if period == 'yearly':
            return str(now.year - 1)
        if period == 'season':
            month = now.month
            year = now.year
            season = (month - 1) // 3 + 1
            if season == 1:
                return f'{year - 1}-S4'
            return f'{year}-S{season - 1}'
        raise ValueError(f'unknown period: {period}')

    def periods_to_archive_today(self) -> List[Tuple[str, str]]:
        now = datetime.now()
        tomorrow = now + timedelta(days=1)
        result: List[Tuple[str, str]] = [('daily', self.current_day_key())]
        if tomorrow.isocalendar()[:2] != now.isocalendar()[:2]:
            result.append(('weekly', self.current_week_key()))
        if tomorrow.month != now.month:
            result.append(('monthly', self.current_month_key()))
            if now.month in (3, 6, 9, 12):
                result.append(('season', self.current_season_key()))
        if now.month == 12 and now.day == 31:
            result.append(('yearly', self.current_year_key()))
        return result

    def _is_period_archived(self, gid: GroupId, period: str, period_key: str) -> bool:
        group = self.store.groups.get(self._gid_key(gid))
        if not group:
            return False
        return group.archived_periods.get(period) == period_key

    def _mark_period_archived(self, gid: GroupId, period: str, period_key: str) -> None:
        group = self._get_group(gid)
        group.archived_periods[period] = period_key

    def _append_history(
        self,
        gid: GroupId,
        period: str,
        period_key: str,
        ranking: List[Tuple[str, str, int]],
    ) -> None:
        gk = self._gid_key(gid)
        if gk not in self.history_store.groups:
            self.history_store.groups[gk] = GuessHistoryGroup()
        record = GuessHistoryRecord(
            period=period,
            period_key=period_key,
            archived_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            ranking=[
                GuessHistoryEntry(uid=str(uid), name=name, score=score)
                for uid, name, score in ranking
            ],
        )
        group_hist = self.history_store.groups[gk]
        group_hist.records.append(record)
        same_period = [r for r in group_hist.records if r.period == period]
        if len(same_period) > self.MAX_HISTORY_PER_PERIOD:
            drop_keys = {
                r.period_key
                for r in sorted(same_period, key=lambda item: item.archived_at)[:-self.MAX_HISTORY_PER_PERIOD]
            }
            group_hist.records = [
                r for r in group_hist.records
                if not (r.period == period and r.period_key in drop_keys)
            ]

    def get_history_ranking(
        self,
        gid: GroupId,
        period: str,
        period_key: str,
        top_n: Optional[int] = None,
    ) -> List[Tuple[str, str, int]]:
        group = self.history_store.groups.get(self._gid_key(gid))
        if not group:
            return []
        for record in reversed(group.records):
            if record.period == period and record.period_key == period_key:
                items = [
                    (entry.uid, entry.name or entry.uid, entry.score)
                    for entry in record.ranking
                    if entry.score > 0
                ]
                items.sort(key=lambda item: (-item[2], item[0]))
                if top_n is not None:
                    return items[:top_n]
                return items
        return []

    def list_history_keys(self, gid: GroupId, period: str, limit: int = 8) -> List[str]:
        group = self.history_store.groups.get(self._gid_key(gid))
        if not group:
            return []
        keys = sorted(
            {record.period_key for record in group.records if record.period == period},
            reverse=True,
        )
        return keys[:limit]

    def _build_forward_from_ranking(
        self,
        ranking: List[Tuple[str, str, int]],
        self_id: int,
        title: str,
        top_n: int = 20,
    ) -> Tuple[str, List[dict]]:
        if not ranking:
            return title, []
        shown = ranking[:top_n]
        nodes = [
            build_forward_node(
                str(self_id),
                name,
                f'{index}. {name} — {score} 分',
            )
            for index, (_, name, score) in enumerate(shown, start=1)
        ]
        return title, nodes

    async def archive_and_broadcast_period(
        self,
        bot,
        group_ids: List[GroupId],
        period: str,
        period_key: str,
        top_n: int = 20,
    ) -> None:
        from nonebot import get_bots

        spec = self.PERIODS[period]
        score_changed = False
        history_changed = False
        bots = get_bots()
        qq_bot = None
        onebot_bot = None
        for candidate in bots.values():
            mod = type(candidate.adapter).__module__
            if 'adapters.qq' in mod:
                qq_bot = candidate
            elif 'adapters.onebot' in mod:
                onebot_bot = candidate
        if bot is not None and onebot_bot is None and qq_bot is None:
            mod = type(getattr(bot, 'adapter', bot)).__module__
            if 'adapters.qq' in mod:
                qq_bot = bot
            else:
                onebot_bot = bot

        for gid in group_ids:
            if self._is_period_archived(gid, period, period_key):
                continue
            ranking = self.get_period_ranking(gid, period)
            self._append_history(gid, period, period_key, ranking)
            history_changed = True
            self._mark_period_archived(gid, period, period_key)
            score_changed = True
            if ranking:
                title = (
                    f'猜歌积分{spec.board_title}结算'
                    f'（{period_key}，前 {min(top_n, len(ranking))} 名）'
                )
                self_id = int((onebot_bot or qq_bot or bot).self_id)
                _, nodes = self._build_forward_from_ranking(
                    ranking, self_id, title, top_n=top_n,
                )
                try:
                    if is_likely_qq_group_id(gid):
                        if qq_bot is None:
                            raise RuntimeError('未找到官方 QQ Bot')
                        await send_group_plain_text(
                            qq_bot,
                            gid,
                            format_forward_nodes_as_text(title, nodes),
                        )
                    else:
                        target_bot = onebot_bot or bot
                        title_node = build_forward_node(str(self_id), '猜歌榜结算', title)
                        messages = json.loads(
                            json.dumps([title_node] + nodes, ensure_ascii=False),
                        )
                        await target_bot.call_api(
                            'send_group_forward_msg',
                            group_id=int(gid),
                            messages=messages,
                        )
                except Exception as e:
                    log.warning(
                        f'[maimai] 猜歌{spec.board_title}结算推送失败 '
                        f'({gid}, {period_key}): {type(e).__name__}: {e}'
                    )
            else:
                empty_msg = (
                    f'猜歌积分{spec.board_title}结算（{period_key}）\n'
                    f'本群该周期无人上榜。'
                )
                try:
                    if is_likely_qq_group_id(gid):
                        if qq_bot is None:
                            raise RuntimeError('未找到官方 QQ Bot')
                        await send_group_plain_text(qq_bot, gid, empty_msg)
                    else:
                        target_bot = onebot_bot or bot
                        await target_bot.send_group_msg(group_id=int(gid), message=empty_msg)
                except Exception as e:
                    log.warning(
                        f'[maimai] 猜歌{spec.board_title}空榜推送失败 '
                        f'({gid}, {period_key}): {type(e).__name__}: {e}'
                    )
        if score_changed:
            await self._save()
        if history_changed:
            await self._save_history()

    def _ensure_period(self, member: GuessMemberScore, period: str) -> None:
        spec = self.PERIODS[period]
        current_key = self.period_key(period)
        if getattr(member, spec.key_attr) != current_key:
            setattr(member, spec.key_attr, current_key)
            setattr(member, spec.score_attr, 0)

    def _ensure_all_periods(self, member: GuessMemberScore) -> None:
        for period in self.PERIODS:
            self._ensure_period(member, period)

    def get_ranking(
        self,
        gid: GroupId,
        top_n: Optional[int] = None,
    ) -> List[Tuple[str, str, int]]:
        group = self.store.groups.get(self._gid_key(gid))
        if not group:
            return []
        items = [
            (uid, member.name or uid, member.score)
            for uid, member in group.members.items()
            if member.score > 0
        ]
        items.sort(key=lambda item: (-item[2], item[0]))
        if top_n is not None:
            return items[:top_n]
        return items

    def get_period_ranking(
        self,
        gid: GroupId,
        period: str,
        top_n: Optional[int] = None,
    ) -> List[Tuple[str, str, int]]:
        spec = self.PERIODS[period]
        group = self.store.groups.get(self._gid_key(gid))
        if not group:
            return []
        current_key = self.period_key(period)
        items = [
            (uid, member.name or uid, getattr(member, spec.score_attr))
            for uid, member in group.members.items()
            if getattr(member, spec.key_attr) == current_key
            and getattr(member, spec.score_attr) > 0
        ]
        items.sort(key=lambda item: (-item[2], item[0]))
        if top_n is not None:
            return items[:top_n]
        return items

    def get_rank(self, gid: GroupId, uid: UserId) -> int:
        ranking = self.get_ranking(gid)
        uk = self._uid_key(uid)
        for index, (member_uid, _, _) in enumerate(ranking, start=1):
            if member_uid == uk:
                return index
        return len(ranking) + 1

    def get_period_rank(self, gid: GroupId, uid: UserId, period: str) -> int:
        ranking = self.get_period_ranking(gid, period)
        uk = self._uid_key(uid)
        for index, (member_uid, _, _) in enumerate(ranking, start=1):
            if member_uid == uk:
                return index
        return len(ranking) + 1

    def get_period_snapshot(self, gid: GroupId, uid: UserId) -> Dict[str, Tuple[int, int]]:
        member = self.store.groups.get(self._gid_key(gid), GuessGroupScores()).members.get(self._uid_key(uid))
        snapshot: Dict[str, Tuple[int, int]] = {}
        for period, spec in self.PERIODS.items():
            score = getattr(member, spec.score_attr, 0) if member else 0
            snapshot[period] = (score, self.get_period_rank(gid, uid, period))
        return snapshot

    def streak_bonus(self, streak: int) -> int:
        if streak < 2:
            return 0
        return streak - 1

    def get_score_multiplier(
        self,
        *,
        first_stage: bool,
        first_guess: bool,
    ) -> Tuple[int, List[str]]:
        multiplier = 1
        tags: List[str] = []
        if first_stage:
            multiplier *= 2
            tags.append('首阶段×2')
        if first_guess:
            multiplier *= 2
            tags.append('首答×2')
        return multiplier, tags

    def _get_group(self, gid: GroupId) -> GuessGroupScores:
        gk = self._gid_key(gid)
        if gk not in self.store.groups:
            self.store.groups[gk] = GuessGroupScores()
        return self.store.groups[gk]

    def _get_member(self, gid: GroupId, uid: UserId) -> GuessMemberScore:
        group = self._get_group(gid)
        uk = self._uid_key(uid)
        if uk not in group.members:
            group.members[uk] = GuessMemberScore()
        return group.members[uk]

    def get_member_or_none(
        self, gid: GroupId, uid: UserId
    ) -> Optional[GuessMemberScore]:
        group = self.store.groups.get(self._gid_key(gid))
        if not group:
            return None
        return group.members.get(self._uid_key(uid))

    def user_has_guess_data(self, gid: GroupId, uid: UserId) -> bool:
        """是否有可展示的猜歌数据（积分或明细）。"""
        member = self.get_member_or_none(gid, uid)
        if member is not None:
            if int(member.score or 0) > 0:
                return True
            if any(
                int(getattr(member, spec.score_attr) or 0) > 0
                for spec in self.PERIODS.values()
            ):
                return True
        return bool(self.list_user_events(gid, uid))

    def user_data_fingerprint(self, gid: GroupId, uid: UserId) -> str:
        """用于判断两群数据是否一致。"""
        member = self.get_member_or_none(gid, uid)
        events = [
            e.model_dump() for e in self.list_user_events(gid, uid)
        ]
        payload = {
            'member': member.model_dump() if member else None,
            'events': events,
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        return raw

    async def copy_user_guess_data(
        self, src_gid: GroupId, dst_gid: GroupId, uid: UserId
    ) -> bool:
        """将用户猜歌积分与明细从 src 覆盖到 dst。"""
        uk = self._uid_key(uid)
        src_member = self.get_member_or_none(src_gid, uid)
        src_events = self.list_user_events(src_gid, uid)
        if src_member is None and not src_events:
            return False

        dst_group = self._get_group(dst_gid)
        if src_member is not None:
            dst_group.members[uk] = GuessMemberScore(**src_member.model_dump())
        else:
            dst_group.members.pop(uk, None)

        dst_eg = self._get_event_group(dst_gid)
        kept = [e for e in dst_eg.events if e.uid != uk]
        kept.extend(GuessScoreEvent(**e.model_dump()) for e in src_events)
        if len(kept) > self.MAX_EVENTS_PER_GROUP:
            kept = kept[-self.MAX_EVENTS_PER_GROUP :]
        dst_eg.events = kept

        await self._save()
        await self._save_events()
        return True

    async def reset_all_streaks(self, gid: GroupId) -> None:
        group = self.store.groups.get(self._gid_key(gid))
        if not group:
            return
        for member in group.members.values():
            member.streak = 0
        await self._save()

    async def award_correct_guess(
        self,
        gid: GroupId,
        uid: UserId,
        name: str,
        raw_base: int,
        multiplier: int,
        *,
        mode: Optional[str] = None,
    ) -> Tuple[int, int, int, int, int, int, Dict[str, Tuple[int, int]]]:
        group = self._get_group(gid)
        uk = self._uid_key(uid)
        for member_uid, member in group.members.items():
            if member_uid != uk:
                member.streak = 0
        winner = self._get_member(gid, uid)
        self._ensure_all_periods(winner)
        winner.streak += 1
        combo = self.streak_bonus(winner.streak)
        total_added = (raw_base + combo) * multiplier
        winner.score += total_added
        for period, spec in self.PERIODS.items():
            setattr(winner, spec.score_attr, getattr(winner, spec.score_attr) + total_added)
        if name:
            winner.name = name
        if mode:
            self._append_event(gid, uid, name or winner.name, mode, total_added)
        await self._save()
        if mode:
            await self._save_events()
        period_snapshot = self.get_period_snapshot(gid, uid)
        return (
            total_added,
            raw_base,
            combo,
            winner.streak,
            winner.score,
            self.get_rank(gid, uid),
            period_snapshot,
        )

    async def award_fixed_points(
        self,
        gid: GroupId,
        uid: UserId,
        name: str,
        points: int,
        *,
        mode: Optional[str] = None,
    ) -> Tuple[int, int, int]:
        """发放固定积分（不计连击/倍率，不改 streak）；可选写入模式明细。"""
        added = max(0, int(points))
        if added <= 0:
            member = self._get_member(gid, uid)
            if name:
                member.name = name
            return 0, member.score, self.get_rank(gid, uid)
        winner = self._get_member(gid, uid)
        self._ensure_all_periods(winner)
        winner.score += added
        for period, spec in self.PERIODS.items():
            setattr(winner, spec.score_attr, getattr(winner, spec.score_attr) + added)
        if name:
            winner.name = name
        if mode:
            self._append_event(gid, uid, name or winner.name, mode, added)
        await self._save()
        if mode:
            await self._save_events()
        return added, winner.score, self.get_rank(gid, uid)

    @staticmethod
    def format_settlement_lines(
        added: int,
        raw_base: int,
        combo: int,
        multiplier: int,
        streak: int,
        total: int,
        total_rank: int,
        period_snapshot: Dict[str, Tuple[int, int]],
        multiplier_tags: Optional[List[str]] = None,
    ) -> str:
        detail_parts: List[str] = []
        if multiplier_tags:
            detail_parts.extend(multiplier_tags)
        if combo > 0:
            detail_parts.append(f'连击 +{combo}（{streak} 连击）')
        if multiplier > 1:
            detail_parts.append(f'({raw_base}+{combo})×{multiplier}')
        bonus_part = f'（{"，".join(detail_parts)}）' if detail_parts else ''
        streak_part = ''

        period_lines: List[str] = []
        for period in ('daily', 'weekly', 'monthly', 'season', 'yearly'):
            spec = GuessScoreManager.PERIODS[period]
            score, rank = period_snapshot[period]
            extra = ''
            if period == 'season':
                extra = f'（{GuessScoreManager.current_season_key()}）'
            period_lines.append(
                f'{spec.label} {score} 分{extra}，群内{spec.rank_label}第 {rank} 名'
            )

        return (
            f'本次 +{added} 分{bonus_part}，总分 {total} 分，总榜第 {total_rank} 名'
            f'{streak_part}\n'
            + '\n'.join(period_lines)
        )

    def build_ranking_forward(
        self,
        gid: GroupId,
        self_id: int,
        *,
        period: str = 'total',
        period_key: Optional[str] = None,
        top_n: int = 20,
    ) -> Tuple[str, List[dict]]:
        if period == 'total':
            ranking = self.get_ranking(gid, top_n=top_n)
            if not ranking:
                return '本群暂无猜歌积分记录。', []
            title = f'猜歌积分总榜（前 {min(top_n, len(ranking))} 名）'
        elif period_key is not None:
            spec = self.PERIODS[period]
            ranking = self.get_history_ranking(gid, period, period_key, top_n=top_n)
            if not ranking:
                keys = self.list_history_keys(gid, period)
                hint = f'可查询：{", ".join(keys)}' if keys else '暂无历史记录'
                return f'未找到 {period_key} 的猜歌积分{spec.board_title}。{hint}', []
            title = (
                f'猜歌积分历史{spec.board_title}'
                f'（{period_key}，前 {min(top_n, len(ranking))} 名）'
            )
        else:
            spec = self.PERIODS[period]
            ranking = self.get_period_ranking(gid, period, top_n=top_n)
            if not ranking:
                return f'本群当前{spec.board_title}暂无积分记录。', []
            key = self.period_key(period)
            title = f'猜歌积分{spec.board_title}（{key}，前 {min(top_n, len(ranking))} 名）'
        _, nodes = self._build_forward_from_ranking(ranking, self_id, title, top_n=top_n)
        return title, nodes


guess_score = GuessScoreManager()
