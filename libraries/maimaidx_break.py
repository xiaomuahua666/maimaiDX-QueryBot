"""
AWMC BREAK 积分：签到、查分扣费、账号统计。

- SQLite 持久化：data/break/break.db
- 签到倍率加算叠加；查分仅在实际 API 请求时扣费
"""

from __future__ import annotations

import contextvars
import json
import math
import random
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from threading import RLock
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from ..config import BOT_QQ_GROUP, log
from .maimaidx_error import BreakInsufficientError
from .maimaidx_sqlite import configure_sqlite_connection

DB_DIR = Path(__file__).resolve().parent.parent / 'data' / 'break'
DB_PATH = DB_DIR / 'break.db'

DEFAULT_CONFIG: Dict[str, str] = {
    'checkin_base_min': '1',
    'checkin_base_max': '2',
    'query_cost': '1',
    'analysis_input_tokens_per_break': '4000',
    'analysis_output_tokens_per_break': '1000',
    'analysis_min_cost': '2',
    'analysis_max_cost': '20',
    'analysis_fallback_cost': '4',
    # 恢复旧版第 1～5 天曲线；之后按 streak_bonus_growth 继续增长，不封顶。
    'streak_bonus': '3,5,8,12,20',
    'streak_bonus_growth': '1',
    'makeup_checkin_costs': '30,60,90',
    'bonus_group_1072033605': '0.25',
    'bonus_thursday': '0.5',
    'bonus_group_first': '0.5',
    # 开启个人数据存储后，签到基础部分加算 +50%
    'bonus_data_storage': '0.5',
    # 存储签到加成防刷：关闭后再开 / 补发冷却天数
    'storage_bonus_cooldown_days': '7',
    # 猜对每次固定奖励，不设每日上限，避免被分数倍率放大。
    'guess_break_per_correct': '1',
    # 上传/发票仅在外部操作成功后结算；上传每日首次免费，发票每次扣费。
    'upload_fish_cost': '2',
    'upload_lx_cost': '2',
    'upload_all_cost': '3',
    'ticket_cost_per_multiplier': '10',
    'ticket_unused_penalty': '20',
    'transfer_fee': '0',
    'lottery_cost': '2',
    'red_packet_expire_minutes': '10',
    'red_packet_max_total': '10000',
    'red_packet_max_count': '100',
}

LEGACY_ECONOMY_DEFAULTS: Dict[str, str] = {
    'checkin_base_min': '1',
    'checkin_base_max': '5',
    'bonus_group_1072033605': '0.5',
    'bonus_thursday': '1.0',
    'bonus_group_first': '1.0',
}

CAPPED_STREAK_DEFAULT = '0,0,1,1,1,2,2'

BONUS_GROUP_IDS = {int(BOT_QQ_GROUP), 993795066}
DOUBLE_CHECKIN_GROUP_IDS = {669800745}
LOTTERY_PRIZES = (0, 1, 2, 5, 10)
LOTTERY_WEIGHTS = (35, 30, 20, 12, 3)
# 仅这些业务享受「每日首次成功免费」；发票等不在此列，每次成功均扣费。
DAILY_FREE_SERVICES = frozenset({'upload'})

_CREATE_SQL = """\
CREATE TABLE IF NOT EXISTS break_users (
    qqid                    INTEGER PRIMARY KEY,
    balance                 INTEGER NOT NULL DEFAULT 0,
    streak                  INTEGER NOT NULL DEFAULT 0,
    last_checkin_date       TEXT,
    total_query_count       INTEGER NOT NULL DEFAULT 0,
    total_analysis_count    INTEGER NOT NULL DEFAULT 0,
    last_query_at           REAL,
    last_analysis_at        REAL,
    created_at              REAL NOT NULL,
    updated_at              REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS break_daily_usage (
    qqid            INTEGER NOT NULL,
    date            TEXT NOT NULL,
    free_used       INTEGER NOT NULL DEFAULT 0,
    query_count     INTEGER NOT NULL DEFAULT 0,
    analysis_count  INTEGER NOT NULL DEFAULT 0,
    break_spent     INTEGER NOT NULL DEFAULT 0,
    break_gained    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (qqid, date)
);
CREATE TABLE IF NOT EXISTS break_group_checkin (
    group_id    INTEGER NOT NULL,
    date        TEXT NOT NULL,
    first_qqid  INTEGER NOT NULL,
    PRIMARY KEY (group_id, date)
);
CREATE TABLE IF NOT EXISTS break_makeup_checkin (
    qqid         INTEGER NOT NULL,
    target_date  TEXT NOT NULL,
    used_month   TEXT NOT NULL,
    monthly_no   INTEGER NOT NULL,
    cost         INTEGER NOT NULL,
    streak       INTEGER NOT NULL,
    created_at   REAL NOT NULL,
    PRIMARY KEY (qqid, target_date)
);
CREATE INDEX IF NOT EXISTS idx_break_makeup_month
    ON break_makeup_checkin(qqid, used_month);
CREATE TABLE IF NOT EXISTS break_config (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS break_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    qqid        INTEGER NOT NULL,
    delta       INTEGER NOT NULL,
    reason      TEXT NOT NULL,
    meta        TEXT,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_break_log_qqid ON break_log(qqid, created_at DESC);
CREATE TABLE IF NOT EXISTS break_guess_daily (
    qqid            INTEGER NOT NULL,
    date            TEXT NOT NULL,
    guess_points    INTEGER NOT NULL DEFAULT 0,
    break_awarded   INTEGER NOT NULL DEFAULT 0,
    last_at         REAL NOT NULL,
    PRIMARY KEY (qqid, date)
);
CREATE TABLE IF NOT EXISTS break_service_daily (
    qqid          INTEGER NOT NULL,
    date          TEXT NOT NULL,
    service       TEXT NOT NULL,
    success_count INTEGER NOT NULL DEFAULT 0,
    free_used     INTEGER NOT NULL DEFAULT 0,
    break_spent   INTEGER NOT NULL DEFAULT 0,
    last_at       REAL NOT NULL,
    PRIMARY KEY (qqid, date, service)
);
CREATE INDEX IF NOT EXISTS idx_break_service_daily
    ON break_service_daily(date, service);
CREATE TABLE IF NOT EXISTS break_daily_reward (
    qqid        INTEGER NOT NULL,
    date        TEXT NOT NULL,
    reward_key  TEXT NOT NULL,
    amount      INTEGER NOT NULL,
    created_at  REAL NOT NULL,
    PRIMARY KEY (qqid, date, reward_key)
);
CREATE TABLE IF NOT EXISTS break_red_packet (
    id                TEXT PRIMARY KEY,
    group_id          INTEGER NOT NULL,
    sender_qqid       INTEGER NOT NULL,
    total_amount      INTEGER NOT NULL,
    total_count       INTEGER NOT NULL,
    remaining_amount  INTEGER NOT NULL,
    remaining_count   INTEGER NOT NULL,
    status            TEXT NOT NULL,
    created_at        REAL NOT NULL,
    expires_at        REAL NOT NULL,
    finished_at       REAL
);
CREATE INDEX IF NOT EXISTS idx_break_red_packet_group
    ON break_red_packet(group_id, status, created_at DESC);
CREATE TABLE IF NOT EXISTS break_red_packet_claim (
    packet_id  TEXT NOT NULL,
    qqid       INTEGER NOT NULL,
    amount     INTEGER NOT NULL,
    claimed_at REAL NOT NULL,
    PRIMARY KEY (packet_id, qqid),
    FOREIGN KEY (packet_id) REFERENCES break_red_packet(id)
);
"""


class BreakLogEntry(BaseModel):
    delta: int
    reason: str
    created_at: float
    meta: Optional[str] = None


class AccountProfile(BaseModel):
    qqid: int
    balance: int = 0
    streak: int = 0
    last_checkin_date: Optional[str] = None
    checked_in_today: bool = False
    today_query_count: int = 0
    today_analysis_count: int = 0
    today_break_spent: int = 0
    today_break_gained: int = 0
    free_used_today: bool = False
    total_query_count: int = 0
    total_analysis_count: int = 0
    last_query_at: Optional[float] = None
    last_analysis_at: Optional[float] = None
    data_source: str = 'divingfish'
    theme: str = 'default'
    storage_enabled: bool = False
    account_bound: bool = False
    account_today_total: int = 0
    account_today_success: int = 0
    account_today_error: int = 0
    account_total: int = 0
    account_total_success: int = 0
    account_total_error: int = 0
    account_operation_counts: Dict[str, int] = Field(default_factory=dict)
    account_ticket_stats: Dict[str, float | int] = Field(default_factory=dict)
    recent_account_logs: List[dict] = Field(default_factory=list)
    recent_logs: List[BreakLogEntry] = Field(default_factory=list)


@dataclass
class CheckinResult:
    qqid: int
    reward: int
    balance: int
    streak: int
    streak_bonus: int
    base: int
    multiplier_sum: float
    base_min: int = 1
    base_max: int = 2
    bonus_labels: List[str] = field(default_factory=list)
    already_checked: bool = False
    storage_enabled: bool = False
    prompt_enable_storage: bool = False


@dataclass
class MakeupCheckinResult:
    qqid: int
    target_date: str
    monthly_no: int
    monthly_limit: int
    cost: int
    balance: int
    streak: int
    next_cost: Optional[int] = None


@dataclass
class GuessBreakReward:
    points_added: int
    daily_points: int
    break_added: int
    daily_break: int
    daily_cap: int
    points_per_break: int
    balance: int


@dataclass
class ServiceChargeResult:
    service: str
    charged: int
    free: bool
    balance: int


@dataclass
class TransferResult:
    sender_balance: int
    recipient_balance: int
    amount: int
    fee: int


@dataclass
class LotteryResult:
    count: int
    cost: int
    prize: int
    balance: int


@dataclass
class DailyRewardResult:
    reward_key: str
    amount: int
    balance: int
    awarded: bool


@dataclass
class RedPacketCreateResult:
    packet_id: str
    total_amount: int
    total_count: int
    expires_at: float
    sender_balance: int


@dataclass
class RedPacketClaimResult:
    packet_id: str
    amount: int
    remaining_amount: int
    remaining_count: int
    recipient_balance: int
    completed: bool


@dataclass
class RedPacketRefundResult:
    packet_id: str
    group_id: int
    sender_qqid: int
    refund: int


@dataclass
class RedPacketStatus:
    packet_id: str
    sender_qqid: int
    total_amount: int
    total_count: int
    remaining_amount: int
    remaining_count: int
    status: str
    expires_at: float
    claims: List[tuple[int, int]] = field(default_factory=list)


def _parse_config_int(raw: str, default: int) -> int:
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def calculate_streak_bonus(streak: int, bonuses: list[int], growth: int) -> int:
    """按配置曲线计算连签奖励；超过曲线后线性增长，不封顶。"""
    if not bonuses:
        return 0
    idx = max(int(streak) - 1, 0)
    if idx < len(bonuses):
        return max(0, int(bonuses[idx]))
    return max(0, int(bonuses[-1])) + max(1, int(growth)) * (
        idx - len(bonuses) + 1
    )


def calculate_luck_break(luck: int) -> tuple[int, int]:
    """人品值按普通四舍五入取整到十位，再换算为 BREAK。"""
    value = max(0, min(100, int(luck)))
    rounded = ((value + 5) // 10) * 10
    return rounded, rounded // 10


def calculate_red_packet_claim(remaining_amount: int, remaining_count: int) -> int:
    """生成手气红包金额，并保证其余每份至少 1 BREAK。"""
    remaining_amount = int(remaining_amount)
    remaining_count = int(remaining_count)
    if remaining_count <= 1:
        return remaining_amount
    max_available = remaining_amount - (remaining_count - 1)
    average_twice = max(1, remaining_amount * 2 // remaining_count)
    return random.randint(1, min(max_available, average_twice))


def calculate_checkin_reward(
    base: int,
    multiplier_sum: float,
    streak_bonus: int,
    reward_multiplier: int = 1,
) -> int:
    """签到最终奖励：加算百分比与连签奖励计算完成后，再应用群倍数。"""
    return int(
        round(
            (int(base) * (1 + float(multiplier_sum)) + int(streak_bonus))
            * max(1, int(reward_multiplier))
        )
    )


def parse_makeup_checkin_costs(raw: str) -> tuple[int, ...]:
    """补签阶梯价格；无效配置回退为每月三次 30/60/90。"""
    values: list[int] = []
    for part in str(raw or '').replace('，', ',').split(','):
        try:
            value = int(part.strip())
        except (TypeError, ValueError):
            continue
        if value > 0:
            values.append(value)
    return tuple(values) or (30, 60, 90)


def calculate_makeup_streak(
    last_checkin_date: Optional[str],
    current_streak: int,
    target_date: date,
    today: date,
    *,
    previous_checkin_date: Optional[date] = None,
    previous_streak: int = 0,
) -> tuple[str, int]:
    """补昨天后的 last_checkin_date 与连续天数，兼容今天已先签到的情况。"""
    last = date.fromisoformat(last_checkin_date) if last_checkin_date else None
    if last == target_date:
        raise ValueError('昨天已经签到过，无需补签。')
    if last == today:
        streak = (
            max(0, int(previous_streak)) + 2
            if previous_checkin_date == date.fromordinal(target_date.toordinal() - 1)
            else 2
        )
        return today.isoformat(), streak
    if last is None or last < target_date:
        streak = (
            max(0, int(current_streak)) + 1
            if last == date.fromordinal(target_date.toordinal() - 1)
            else 1
        )
        return target_date.isoformat(), streak
    raise ValueError('签到日期状态异常，请联系管理员处理。')


class BreakDatabase:
    _instance = None
    _lock = RLock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        DB_DIR.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        configure_sqlite_connection(self._conn)
        self._conn.executescript(_CREATE_SQL)
        self._conn.commit()
        self._seed_config()

    def _seed_config(self):
        for key, value in DEFAULT_CONFIG.items():
            self._conn.execute(
                'INSERT OR IGNORE INTO break_config (key, value) VALUES (?, ?)',
                (key, value),
            )
        self._conn.commit()
        self._migrate_legacy_economy_defaults()
        self._migrate_ticket_cost_default()
        self._migrate_analysis_max_cost_default()
        self._migrate_analysis_token_rates_default()
        self._restore_uncapped_streak_default()

    def _migrate_ticket_cost_default(self) -> None:
        """将旧版发票默认价（倍率 ×2/×3）迁移为倍率 ×10。"""
        row = self._conn.execute(
            'SELECT value FROM break_config WHERE key = ?',
            ('ticket_cost_per_multiplier',),
        ).fetchone()
        if row and str(row['value']) in {'2', '3'}:
            self._conn.execute(
                'UPDATE break_config SET value = ? WHERE key = ?',
                (DEFAULT_CONFIG['ticket_cost_per_multiplier'], 'ticket_cost_per_multiplier'),
            )
            self._conn.commit()
            log.info('[BREAK] 已将发票价格迁移为倍率 ×10')

    def _migrate_analysis_max_cost_default(self) -> None:
        """将首版 Token 计费封顶从 6 BREAK 迁移为 20 BREAK。"""
        row = self._conn.execute(
            'SELECT value FROM break_config WHERE key = ?',
            ('analysis_max_cost',),
        ).fetchone()
        if row and str(row['value']) == '6':
            self._conn.execute(
                'UPDATE break_config SET value = ? WHERE key = ?',
                (DEFAULT_CONFIG['analysis_max_cost'], 'analysis_max_cost'),
            )
            self._conn.commit()
            log.info('[BREAK] 已将锐评 Token 计费封顶迁移为 20 BREAK')

    def _migrate_analysis_token_rates_default(self) -> None:
        """将旧版锐评默认费率迁移为新标准，保留管理员自定义值。"""
        previous_defaults = {
            'analysis_input_tokens_per_break': '8000',
            'analysis_output_tokens_per_break': '2000',
            'analysis_fallback_cost': '3',
        }
        changed = False
        for key, old_value in previous_defaults.items():
            row = self._conn.execute(
                'SELECT value FROM break_config WHERE key = ?', (key,)
            ).fetchone()
            if row and str(row['value']) == old_value:
                self._conn.execute(
                    'UPDATE break_config SET value = ? WHERE key = ?',
                    (DEFAULT_CONFIG[key], key),
                )
                changed = True
        if changed:
            self._conn.commit()
            log.info(
                '[BREAK] 已将锐评默认费率迁移为输入 4000 / 输出 1000 Token '
                '各计 1 BREAK，usage 缺失时收取 4 BREAK'
            )

    def _migrate_legacy_economy_defaults(self) -> None:
        """仅替换仍等于旧默认值的配置，保留管理员自定义数据。"""
        changed = False
        for key, old_value in LEGACY_ECONOMY_DEFAULTS.items():
            row = self._conn.execute(
                'SELECT value FROM break_config WHERE key = ?', (key,)
            ).fetchone()
            if row and str(row['value']) == old_value:
                self._conn.execute(
                    'UPDATE break_config SET value = ? WHERE key = ?',
                    (DEFAULT_CONFIG[key], key),
                )
                changed = True
        if changed:
            self._conn.commit()
            log.info('[BREAK] 已将旧版高通胀签到默认值迁移为温和配置')

    def _restore_uncapped_streak_default(self) -> None:
        """仅恢复上一版的封顶默认值，保留管理员自定义签到曲线。"""
        row = self._conn.execute(
            'SELECT value FROM break_config WHERE key = ?', ('streak_bonus',)
        ).fetchone()
        if row and str(row['value']) == CAPPED_STREAK_DEFAULT:
            self._conn.execute(
                'UPDATE break_config SET value = ? WHERE key = ?',
                (DEFAULT_CONFIG['streak_bonus'], 'streak_bonus'),
            )
            self._conn.commit()
            log.info('[BREAK] 已恢复连续签到奖励曲线，并启用无上限增长')

    def get_config(self, key: str, default: str = '') -> str:
        row = self._conn.execute(
            'SELECT value FROM break_config WHERE key = ?', (key,)
        ).fetchone()
        return row['value'] if row else default

    def set_config(self, key: str, value: str) -> None:
        self._conn.execute(
            'INSERT INTO break_config (key, value) VALUES (?, ?) '
            'ON CONFLICT(key) DO UPDATE SET value = excluded.value',
            (key, value),
        )
        self._conn.commit()

    def _ensure_user(self, qqid: int) -> None:
        with self._lock:
            exists = self._conn.execute(
                'SELECT 1 FROM break_users WHERE qqid = ?', (qqid,)
            ).fetchone()
            if exists:
                return
            now = time.time()
            self._conn.execute(
                """INSERT OR IGNORE INTO break_users
                   (qqid, balance, streak, created_at, updated_at)
                   VALUES (?, 0, 0, ?, ?)""",
                (qqid, now, now),
            )
            self._conn.commit()

    def _today(self) -> str:
        return date.today().isoformat()

    def _ensure_daily(self, qqid: int) -> None:
        self._conn.execute(
            """INSERT OR IGNORE INTO break_daily_usage
               (qqid, date, free_used, query_count, analysis_count, break_spent, break_gained)
               VALUES (?, ?, 0, 0, 0, 0, 0)""",
            (qqid, self._today()),
        )

    def get_balance(self, qqid: int) -> int:
        self._ensure_user(qqid)
        row = self._conn.execute(
            'SELECT balance FROM break_users WHERE qqid = ?', (qqid,)
        ).fetchone()
        return int(row['balance']) if row else 0

    def _append_log(
        self,
        qqid: int,
        delta: int,
        reason: str,
        *,
        meta: Optional[dict] = None,
    ) -> None:
        self._conn.execute(
            'INSERT INTO break_log (qqid, delta, reason, meta, created_at) VALUES (?, ?, ?, ?, ?)',
            (qqid, delta, reason, json.dumps(meta, ensure_ascii=False) if meta else None, time.time()),
        )

    def is_daily_free_available(self, qqid: int) -> bool:
        self._ensure_user(qqid)
        self._ensure_daily(qqid)
        row = self._conn.execute(
            'SELECT free_used FROM break_daily_usage WHERE qqid = ? AND date = ?',
            (qqid, self._today()),
        ).fetchone()
        return not row or int(row['free_used']) == 0

    def mark_daily_free_used(self, qqid: int) -> None:
        self._ensure_user(qqid)
        self._ensure_daily(qqid)
        self._conn.execute(
            """UPDATE break_daily_usage SET free_used = 1
               WHERE qqid = ? AND date = ?""",
            (qqid, self._today()),
        )
        self._conn.commit()

    def record_usage(
        self,
        qqid: int,
        kind: str,
        *,
        break_delta: int = 0,
    ) -> None:
        """kind: query | analysis"""
        self._ensure_user(qqid)
        self._ensure_daily(qqid)
        now = time.time()
        if kind == 'query':
            self._conn.execute(
                """UPDATE break_users SET
                   total_query_count = total_query_count + 1,
                   last_query_at = ?,
                   updated_at = ?
                   WHERE qqid = ?""",
                (now, now, qqid),
            )
            self._conn.execute(
                """UPDATE break_daily_usage SET
                   query_count = query_count + 1,
                   break_spent = break_spent + ?
                   WHERE qqid = ? AND date = ?""",
                (max(0, -break_delta), qqid, self._today()),
            )
        elif kind == 'analysis':
            self._conn.execute(
                """UPDATE break_users SET
                   total_analysis_count = total_analysis_count + 1,
                   last_analysis_at = ?,
                   updated_at = ?
                   WHERE qqid = ?""",
                (now, now, qqid),
            )
            self._conn.execute(
                """UPDATE break_daily_usage SET
                   analysis_count = analysis_count + 1,
                   break_spent = break_spent + ?
                   WHERE qqid = ? AND date = ?""",
                (max(0, -break_delta), qqid, self._today()),
            )
        self._conn.commit()

    def try_consume(
        self,
        qqid: int,
        amount: int,
        reason: str,
        *,
        meta: Optional[dict] = None,
    ) -> bool:
        if amount <= 0:
            return True
        self._ensure_user(qqid)
        self._ensure_daily(qqid)
        with self._lock:
            row = self._conn.execute(
                'SELECT balance FROM break_users WHERE qqid = ?', (qqid,)
            ).fetchone()
            balance = int(row['balance']) if row else 0
            if balance < amount:
                return False
            now = time.time()
            self._conn.execute(
                'UPDATE break_users SET balance = balance - ?, updated_at = ? WHERE qqid = ?',
                (amount, now, qqid),
            )
            self._conn.execute(
                """UPDATE break_daily_usage SET break_spent = break_spent + ?
                   WHERE qqid = ? AND date = ?""",
                (amount, qqid, self._today()),
            )
            self._append_log(qqid, -amount, reason, meta=meta)
            self._conn.commit()
        return True

    def add_balance(
        self,
        qqid: int,
        delta: int,
        reason: str,
        *,
        meta: Optional[dict] = None,
    ) -> int:
        self._ensure_user(qqid)
        self._ensure_daily(qqid)
        now = time.time()
        self._conn.execute(
            'UPDATE break_users SET balance = balance + ?, updated_at = ? WHERE qqid = ?',
            (delta, now, qqid),
        )
        if delta > 0:
            self._conn.execute(
                """UPDATE break_daily_usage SET break_gained = break_gained + ?
                   WHERE qqid = ? AND date = ?""",
                (delta, qqid, self._today()),
            )
        self._append_log(qqid, delta, reason, meta=meta)
        self._conn.commit()
        return self.get_balance(qqid)

    def service_is_free(self, qqid: int, service: str) -> bool:
        if service not in DAILY_FREE_SERVICES:
            return False
        row = self._conn.execute(
            """SELECT free_used FROM break_service_daily
               WHERE qqid=? AND date=? AND service=?""",
            (qqid, self._today(), service),
        ).fetchone()
        return not row or int(row['free_used']) == 0

    def ensure_service_affordable(self, qqid: int, service: str, cost: int) -> None:
        """外部业务请求前检查；真正扣费必须在成功后调用 settle_service_success。"""
        if self.service_is_free(qqid, service):
            return
        balance = self.get_balance(qqid)
        if balance < max(0, int(cost)):
            raise BreakInsufficientError(max(0, int(cost)), balance, qqid=qqid)

    def settle_service_success(
        self,
        qqid: int,
        service: str,
        cost: int,
        *,
        meta: Optional[dict] = None,
    ) -> ServiceChargeResult:
        """成功业务原子结算：DAILY_FREE_SERVICES 每日首次免费，其余每次按配置扣费。"""
        cost = max(0, int(cost))
        self._ensure_user(qqid)
        self._ensure_daily(qqid)
        today, now = self._today(), time.time()
        with self._lock:
            self._conn.execute(
                """INSERT OR IGNORE INTO break_service_daily
                   (qqid, date, service, success_count, free_used, break_spent, last_at)
                   VALUES (?, ?, ?, 0, 0, 0, ?)""",
                (qqid, today, service, now),
            )
            row = self._conn.execute(
                """SELECT free_used FROM break_service_daily
                   WHERE qqid=? AND date=? AND service=?""",
                (qqid, today, service),
            ).fetchone()
            free = (
                service in DAILY_FREE_SERVICES
                and (not row or int(row['free_used']) == 0)
            )
            charged = 0 if free else cost
            balance = self.get_balance(qqid)
            if charged and balance < charged:
                raise BreakInsufficientError(charged, balance, qqid=qqid)
            self._conn.execute(
                """UPDATE break_service_daily SET success_count=success_count+1,
                   free_used=1, break_spent=break_spent+?, last_at=?
                   WHERE qqid=? AND date=? AND service=?""",
                (charged, now, qqid, today, service),
            )
            if charged:
                self._conn.execute(
                    'UPDATE break_users SET balance=balance-?, updated_at=? WHERE qqid=?',
                    (charged, now, qqid),
                )
                self._conn.execute(
                    """UPDATE break_daily_usage SET break_spent=break_spent+?
                       WHERE qqid=? AND date=?""",
                    (charged, qqid, today),
                )
            detail = dict(meta or {})
            detail.update({'service': service, 'free': free, 'listed_cost': cost})
            self._append_log(qqid, -charged, f'service:{service}', meta=detail)
            self._conn.commit()
            balance -= charged
        return ServiceChargeResult(service, charged, free, balance)

    def transfer(self, sender: int, recipient: int, amount: int) -> TransferResult:
        amount = int(amount)
        if amount <= 0 or sender == recipient:
            raise ValueError('转账数量必须大于 0，且不能转给自己')
        fee = max(0, _parse_config_int(self.get_config('transfer_fee', '0'), 0))
        self._ensure_user(sender)
        self._ensure_user(recipient)
        self._ensure_daily(sender)
        self._ensure_daily(recipient)
        with self._lock:
            sender_balance = self.get_balance(sender)
            total = amount + fee
            if sender_balance < total:
                raise BreakInsufficientError(total, sender_balance, qqid=sender)
            now = time.time()
            self._conn.execute(
                'UPDATE break_users SET balance=balance-?, updated_at=? WHERE qqid=?',
                (total, now, sender),
            )
            self._conn.execute(
                'UPDATE break_users SET balance=balance+?, updated_at=? WHERE qqid=?',
                (amount, now, recipient),
            )
            self._conn.execute(
                'UPDATE break_daily_usage SET break_spent=break_spent+? WHERE qqid=? AND date=?',
                (total, sender, self._today()),
            )
            self._conn.execute(
                'UPDATE break_daily_usage SET break_gained=break_gained+? WHERE qqid=? AND date=?',
                (amount, recipient, self._today()),
            )
            self._append_log(sender, -total, 'transfer_out', meta={'to': recipient, 'amount': amount, 'fee': fee})
            self._append_log(recipient, amount, 'transfer_in', meta={'from': sender, 'amount': amount})
            self._conn.commit()
            return TransferResult(sender_balance-total, self.get_balance(recipient), amount, fee)

    def expire_red_packets(self, now: Optional[float] = None) -> List[RedPacketRefundResult]:
        """关闭已过期红包并将未领取余额原路退回。"""
        current = float(now if now is not None else time.time())
        refunds: List[RedPacketRefundResult] = []
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM break_red_packet
                   WHERE status='active' AND expires_at<=?""",
                (current,),
            ).fetchall()
            for row in rows:
                refund = int(row['remaining_amount'])
                sender = int(row['sender_qqid'])
                packet_id = str(row['id'])
                if refund > 0:
                    self._ensure_user(sender)
                    self._conn.execute(
                        'UPDATE break_users SET balance=balance+?, updated_at=? WHERE qqid=?',
                        (refund, current, sender),
                    )
                    created_date = datetime.fromtimestamp(float(row['created_at'])).date().isoformat()
                    self._conn.execute(
                        """UPDATE break_daily_usage
                           SET break_spent=MAX(0, break_spent-?)
                           WHERE qqid=? AND date=?""",
                        (refund, sender, created_date),
                    )
                    self._append_log(
                        sender,
                        refund,
                        'red_packet_refund',
                        meta={'packet_id': packet_id, 'group_id': int(row['group_id'])},
                    )
                self._conn.execute(
                    """UPDATE break_red_packet
                       SET status='expired', finished_at=? WHERE id=? AND status='active'""",
                    (current, packet_id),
                )
                refunds.append(
                    RedPacketRefundResult(
                        packet_id, int(row['group_id']), sender, refund
                    )
                )
            self._conn.commit()
        return refunds

    def create_red_packet(
        self,
        sender: int,
        group_id: int,
        total_amount: int,
        total_count: int,
    ) -> RedPacketCreateResult:
        total_amount, total_count = int(total_amount), int(total_count)
        if total_amount <= 0 or total_count <= 0:
            raise ValueError('红包总额和份数必须大于 0')
        if total_amount < total_count:
            raise ValueError('红包总额不能小于份数（每份至少 1 BREAK）')
        max_total = max(
            1, _parse_config_int(self.get_config('red_packet_max_total', '10000'), 10000)
        )
        max_count = max(
            1, _parse_config_int(self.get_config('red_packet_max_count', '100'), 100)
        )
        if total_amount > max_total:
            raise ValueError(f'单个红包最多 {max_total} BREAK')
        if total_count > max_count:
            raise ValueError(f'单个红包最多 {max_count} 份')

        self.expire_red_packets()
        self._ensure_user(sender)
        self._ensure_daily(sender)
        now = time.time()
        expire_minutes = max(
            1,
            _parse_config_int(
                self.get_config('red_packet_expire_minutes', '10'), 10
            ),
        )
        expires_at = now + expire_minutes * 60
        packet_id = uuid.uuid4().hex[:8].upper()
        with self._lock:
            active = self._conn.execute(
                """SELECT id FROM break_red_packet
                   WHERE group_id=? AND status='active' LIMIT 1""",
                (int(group_id),),
            ).fetchone()
            if active:
                raise ValueError('本群还有一个未结束的红包，请抢完或等待过期后再发')
            row = self._conn.execute(
                'SELECT balance FROM break_users WHERE qqid=?', (sender,)
            ).fetchone()
            balance = int(row['balance']) if row else 0
            if balance < total_amount:
                raise BreakInsufficientError(total_amount, balance, qqid=sender)
            try:
                self._conn.execute(
                    'UPDATE break_users SET balance=balance-?, updated_at=? WHERE qqid=?',
                    (total_amount, now, sender),
                )
                self._conn.execute(
                    """UPDATE break_daily_usage SET break_spent=break_spent+?
                       WHERE qqid=? AND date=?""",
                    (total_amount, sender, self._today()),
                )
                self._conn.execute(
                    """INSERT INTO break_red_packet
                       (id, group_id, sender_qqid, total_amount, total_count,
                        remaining_amount, remaining_count, status, created_at, expires_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
                    (
                        packet_id,
                        int(group_id),
                        sender,
                        total_amount,
                        total_count,
                        total_amount,
                        total_count,
                        now,
                        expires_at,
                    ),
                )
                self._append_log(
                    sender,
                    -total_amount,
                    'red_packet_create',
                    meta={
                        'packet_id': packet_id,
                        'group_id': int(group_id),
                        'count': total_count,
                    },
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return RedPacketCreateResult(
            packet_id, total_amount, total_count, expires_at, balance - total_amount
        )

    def claim_red_packet(self, qqid: int, group_id: int) -> RedPacketClaimResult:
        self.expire_red_packets()
        self._ensure_user(qqid)
        self._ensure_daily(qqid)
        now = time.time()
        with self._lock:
            packet = self._conn.execute(
                """SELECT * FROM break_red_packet
                   WHERE group_id=? AND status='active'
                   ORDER BY created_at DESC LIMIT 1""",
                (int(group_id),),
            ).fetchone()
            if not packet:
                raise ValueError('本群当前没有可以领取的红包')
            packet_id = str(packet['id'])
            if int(packet['sender_qqid']) == int(qqid):
                raise ValueError('不能领取自己发出的红包')
            claimed = self._conn.execute(
                'SELECT 1 FROM break_red_packet_claim WHERE packet_id=? AND qqid=?',
                (packet_id, qqid),
            ).fetchone()
            if claimed:
                raise ValueError('你已经领取过这个红包了')
            remaining_amount = int(packet['remaining_amount'])
            remaining_count = int(packet['remaining_count'])
            amount = calculate_red_packet_claim(remaining_amount, remaining_count)
            after_amount = remaining_amount - amount
            after_count = remaining_count - 1
            completed = after_count == 0
            status = 'completed' if completed else 'active'
            try:
                self._conn.execute(
                    """INSERT INTO break_red_packet_claim
                       (packet_id, qqid, amount, claimed_at) VALUES (?, ?, ?, ?)""",
                    (packet_id, qqid, amount, now),
                )
                self._conn.execute(
                    """UPDATE break_red_packet SET remaining_amount=?, remaining_count=?,
                       status=?, finished_at=? WHERE id=? AND status='active'""",
                    (
                        after_amount,
                        after_count,
                        status,
                        now if completed else None,
                        packet_id,
                    ),
                )
                self._conn.execute(
                    'UPDATE break_users SET balance=balance+?, updated_at=? WHERE qqid=?',
                    (amount, now, qqid),
                )
                self._conn.execute(
                    """UPDATE break_daily_usage SET break_gained=break_gained+?
                       WHERE qqid=? AND date=?""",
                    (amount, qqid, self._today()),
                )
                self._append_log(
                    qqid,
                    amount,
                    'red_packet_claim',
                    meta={
                        'packet_id': packet_id,
                        'group_id': int(group_id),
                        'sender': int(packet['sender_qqid']),
                    },
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            balance_row = self._conn.execute(
                'SELECT balance FROM break_users WHERE qqid=?', (qqid,)
            ).fetchone()
        return RedPacketClaimResult(
            packet_id,
            amount,
            after_amount,
            after_count,
            int(balance_row['balance']) if balance_row else amount,
            completed,
        )

    def get_red_packet_status(self, group_id: int) -> Optional[RedPacketStatus]:
        self.expire_red_packets()
        with self._lock:
            packet = self._conn.execute(
                """SELECT * FROM break_red_packet WHERE group_id=?
                   ORDER BY created_at DESC LIMIT 1""",
                (int(group_id),),
            ).fetchone()
            if not packet:
                return None
            claims = self._conn.execute(
                """SELECT qqid, amount FROM break_red_packet_claim
                   WHERE packet_id=? ORDER BY claimed_at""",
                (str(packet['id']),),
            ).fetchall()
        return RedPacketStatus(
            packet_id=str(packet['id']),
            sender_qqid=int(packet['sender_qqid']),
            total_amount=int(packet['total_amount']),
            total_count=int(packet['total_count']),
            remaining_amount=int(packet['remaining_amount']),
            remaining_count=int(packet['remaining_count']),
            status=str(packet['status']),
            expires_at=float(packet['expires_at']),
            claims=[(int(row['qqid']), int(row['amount'])) for row in claims],
        )

    def lottery(self, qqid: int, count: int = 1) -> LotteryResult:
        count = max(1, min(int(count), 10))
        unit_cost = max(1, _parse_config_int(self.get_config('lottery_cost', '2'), 2))
        cost = unit_cost * count
        self._ensure_user(qqid)
        self._ensure_daily(qqid)
        with self._lock:
            balance = self.get_balance(qqid)
            if balance < cost:
                raise BreakInsufficientError(cost, balance, qqid=qqid)
            prizes = random.choices(
                LOTTERY_PRIZES,
                weights=LOTTERY_WEIGHTS,
                k=count,
            )
            prize = sum(prizes)
            net = prize - cost
            now = time.time()
            self._conn.execute(
                'UPDATE break_users SET balance=balance+?, updated_at=? WHERE qqid=?',
                (net, now, qqid),
            )
            self._conn.execute(
                """UPDATE break_daily_usage SET break_spent=break_spent+?,
                   break_gained=break_gained+? WHERE qqid=? AND date=?""",
                (cost, prize, qqid, self._today()),
            )
            self._append_log(
                qqid, net, 'lottery',
                meta={'count': count, 'cost': cost, 'prizes': prizes, 'prize': prize},
            )
            self._conn.commit()
            return LotteryResult(count, cost, prize, balance + net)

    def award_guess_points(
        self,
        qqid: int,
        points: int,
        *,
        group_id: Optional[str] = None,
    ) -> GuessBreakReward:
        """每次猜对固定发 BREAK；分数仅作排行统计，不放大奖励。"""
        points = max(0, int(points))
        reward = max(0, _parse_config_int(
            self.get_config('guess_break_per_correct', '1'), 1
        ))
        self._ensure_user(qqid)
        self._ensure_daily(qqid)
        today = self._today()
        now = time.time()
        with self._lock:
            self._conn.execute(
                """INSERT OR IGNORE INTO break_guess_daily
                   (qqid, date, guess_points, break_awarded, last_at)
                   VALUES (?, ?, 0, 0, ?)""",
                (qqid, today, now),
            )
            self._conn.execute(
                """UPDATE break_guess_daily
                   SET guess_points = guess_points + ?, last_at = ?
                   WHERE qqid = ? AND date = ?""",
                (points, now, qqid, today),
            )
            row = self._conn.execute(
                """SELECT guess_points, break_awarded FROM break_guess_daily
                   WHERE qqid = ? AND date = ?""",
                (qqid, today),
            ).fetchone()
            daily_points = int(row['guess_points'])
            already = int(row['break_awarded'])
            if reward:
                self._conn.execute(
                    """UPDATE break_guess_daily SET break_awarded = break_awarded + ?
                       WHERE qqid = ? AND date = ?""",
                    (reward, qqid, today),
                )
                self._conn.execute(
                    """UPDATE break_users SET balance = balance + ?, updated_at = ?
                       WHERE qqid = ?""",
                    (reward, now, qqid),
                )
                self._conn.execute(
                    """UPDATE break_daily_usage SET break_gained = break_gained + ?
                       WHERE qqid = ? AND date = ?""",
                    (reward, qqid, today),
                )
                self._append_log(
                    qqid, reward, 'guess_reward',
                    meta={
                        'points_added': points,
                        'daily_points': daily_points,
                        'daily_break': already + reward,
                        'daily_cap': 0,
                        'group_id': group_id,
                    },
                )
            self._conn.commit()
            balance_row = self._conn.execute(
                'SELECT balance FROM break_users WHERE qqid = ?', (qqid,)
            ).fetchone()
        return GuessBreakReward(
            points_added=points,
            daily_points=daily_points,
            break_added=reward,
            daily_break=already + reward,
            daily_cap=0,
            points_per_break=0,
            balance=int(balance_row['balance']) if balance_row else 0,
        )

    def admin_set_balance(self, qqid: int, balance: int) -> int:
        self._ensure_user(qqid)
        row = self._conn.execute(
            'SELECT balance FROM break_users WHERE qqid = ?', (qqid,)
        ).fetchone()
        old = int(row['balance']) if row else 0
        delta = balance - old
        self._conn.execute(
            'UPDATE break_users SET balance = ?, updated_at = ? WHERE qqid = ?',
            (balance, time.time(), qqid),
        )
        self._append_log(qqid, delta, 'admin_set', meta={'old': old, 'new': balance})
        self._conn.commit()
        return balance

    def get_user_row(self, qqid: int) -> dict:
        self._ensure_user(qqid)
        row = self._conn.execute('SELECT * FROM break_users WHERE qqid = ?', (qqid,)).fetchone()
        return dict(row) if row else {}

    def get_daily_row(self, qqid: int) -> dict:
        self._ensure_daily(qqid)
        row = self._conn.execute(
            'SELECT * FROM break_daily_usage WHERE qqid = ? AND date = ?',
            (qqid, self._today()),
        ).fetchone()
        return dict(row) if row else {}

    def get_recent_logs(self, qqid: int, limit: int = 5) -> List[BreakLogEntry]:
        rows = self._conn.execute(
            'SELECT delta, reason, meta, created_at FROM break_log WHERE qqid = ? '
            'ORDER BY created_at DESC LIMIT ?',
            (qqid, limit),
        ).fetchall()
        return [
            BreakLogEntry(
                delta=int(r['delta']),
                reason=str(r['reason']),
                created_at=float(r['created_at']),
                meta=r['meta'],
            )
            for r in rows
        ]

    def list_users(self, *, limit: int = 100, offset: int = 0, search: str = '') -> List[dict]:
        clauses, params = [], []
        if search:
            clauses.append('CAST(qqid AS TEXT) LIKE ?')
            params.append(f'%{search}%')
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        params.extend([max(1, min(limit, 500)), max(0, offset)])
        rows = self._conn.execute(
            'SELECT * FROM break_users' + where + ' ORDER BY updated_at DESC LIMIT ? OFFSET ?',
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def count_users(self) -> int:
        row = self._conn.execute('SELECT COUNT(*) AS c FROM break_users').fetchone()
        return int(row['c']) if row else 0

    def economy_report(self, days: int = 30) -> List[dict]:
        from datetime import timedelta

        since = (date.today() - timedelta(days=max(1, days) - 1)).isoformat()
        rows = self._conn.execute(
            """SELECT date, SUM(break_gained) AS gained, SUM(break_spent) AS spent,
                      SUM(query_count) AS queries, SUM(analysis_count) AS analyses,
                      COUNT(DISTINCT qqid) AS active_users
               FROM break_daily_usage WHERE date >= ?
               GROUP BY date ORDER BY date""",
            (since,),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_break_calls(
        self, *, limit: int = 200, offset: int = 0, user_id: str = '', reason: str = ''
    ) -> List[dict]:
        clauses, params = [], []
        if user_id:
            clauses.append('CAST(qqid AS TEXT) LIKE ?')
            params.append(f'%{user_id}%')
        if reason:
            clauses.append('reason LIKE ?')
            params.append(f'%{reason}%')
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        params.extend([max(1, min(limit, 500)), max(0, offset)])
        rows = self._conn.execute(
            """SELECT id, qqid AS user_id, delta, reason, meta, created_at
               FROM break_log""" + where + ' ORDER BY id DESC LIMIT ? OFFSET ?',
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def is_checked_in_today(self, qqid: int) -> bool:
        row = self.get_user_row(qqid)
        return row.get('last_checkin_date') == self._today()

    def _streak_bonus(self, streak: int) -> int:
        raw = self.get_config('streak_bonus', DEFAULT_CONFIG['streak_bonus'])
        parts = [int(x.strip()) for x in raw.split(',') if x.strip().isdigit()]
        if not parts:
            return 0
        growth = max(
            1,
            _parse_config_int(
                self.get_config(
                    'streak_bonus_growth', DEFAULT_CONFIG['streak_bonus_growth']
                ),
                1,
            ),
        )
        return calculate_streak_bonus(streak, parts, growth)

    def claim_daily_reward(
        self,
        qqid: int,
        reward_key: str,
        amount: int,
        *,
        reason: str,
        meta: Optional[dict] = None,
    ) -> DailyRewardResult:
        """每日幂等奖励；同一用户、日期和 reward_key 只发放一次。"""
        key = str(reward_key).strip()[:64]
        if not key:
            raise ValueError('reward_key 不能为空')
        value = max(0, int(amount))
        self._ensure_user(qqid)
        today = self._today()
        with self._lock:
            existing = self._conn.execute(
                """SELECT amount FROM break_daily_reward
                   WHERE qqid = ? AND date = ? AND reward_key = ?""",
                (qqid, today, key),
            ).fetchone()
            if existing:
                return DailyRewardResult(
                    reward_key=key,
                    amount=int(existing['amount']),
                    balance=self.get_balance(qqid),
                    awarded=False,
                )

            now = time.time()
            self._ensure_daily(qqid)
            self._conn.execute(
                """INSERT INTO break_daily_reward
                   (qqid, date, reward_key, amount, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (qqid, today, key, value, now),
            )
            self._conn.execute(
                """UPDATE break_users
                   SET balance = balance + ?, updated_at = ? WHERE qqid = ?""",
                (value, now, qqid),
            )
            self._conn.execute(
                """UPDATE break_daily_usage
                   SET break_gained = break_gained + ?
                   WHERE qqid = ? AND date = ?""",
                (value, qqid, today),
            )
            log_meta = dict(meta or {})
            log_meta['reward_key'] = key
            self._append_log(qqid, value, reason, meta=log_meta)
            self._conn.commit()
            row = self._conn.execute(
                'SELECT balance FROM break_users WHERE qqid = ?', (qqid,)
            ).fetchone()
            return DailyRewardResult(
                reward_key=key,
                amount=value,
                balance=int(row['balance']) if row else value,
                awarded=True,
            )

    def _checkin_base_range(self) -> tuple[int, int]:
        lo = _parse_config_int(self.get_config('checkin_base_min', DEFAULT_CONFIG['checkin_base_min']), 1)
        hi = _parse_config_int(self.get_config('checkin_base_max', DEFAULT_CONFIG['checkin_base_max']), 2)
        if lo > hi:
            lo, hi = hi, lo
        return lo, hi

    def _roll_checkin_base(self) -> tuple[int, int, int]:
        lo, hi = self._checkin_base_range()
        if lo == hi:
            return lo, lo, hi
        return random.randint(lo, hi), lo, hi

    def _is_group_first_today(self, group_id: Optional[int]) -> bool:
        if not group_id:
            return False
        row = self._conn.execute(
            'SELECT 1 FROM break_group_checkin WHERE group_id = ? AND date = ?',
            (group_id, self._today()),
        ).fetchone()
        return row is None

    def _storage_bonus_rate(self) -> float:
        return max(
            0.0,
            float(
                self.get_config(
                    'bonus_data_storage', DEFAULT_CONFIG['bonus_data_storage']
                )
            ),
        )

    def checkin(
        self,
        qqid: int,
        group_id: Optional[int] = None,
        *,
        storage_enabled: bool = False,
        storage_bonus_eligible: Optional[bool] = None,
    ) -> CheckinResult:
        """storage_enabled：当前是否开启；storage_bonus_eligible：是否可拿 +50%（需跨天保持）。"""
        self._ensure_user(qqid)
        today = self._today()
        user = self.get_user_row(qqid)
        bonus_ok = (
            bool(storage_enabled)
            if storage_bonus_eligible is None
            else bool(storage_bonus_eligible)
        )
        if user.get('last_checkin_date') == today:
            return CheckinResult(
                qqid=qqid,
                reward=0,
                balance=int(user.get('balance', 0)),
                streak=int(user.get('streak', 0)),
                streak_bonus=0,
                base=0,
                multiplier_sum=0,
                already_checked=True,
                storage_enabled=bool(storage_enabled),
                prompt_enable_storage=not bool(storage_enabled),
            )

        base, base_min, base_max = self._roll_checkin_base()
        bonus_labels: List[str] = []
        multiplier_sum = 0.0

        if group_id in BONUS_GROUP_IDS:
            bonus = float(
                self.get_config(
                    'bonus_group_1072033605',
                    DEFAULT_CONFIG['bonus_group_1072033605'],
                )
            )
            multiplier_sum += bonus
            bonus_labels.append(f'指定群 {group_id} +{int(bonus * 100)}%')

        if date.today().weekday() == 3:
            bonus = float(self.get_config('bonus_thursday', '1.0'))
            multiplier_sum += bonus
            bonus_labels.append(f'周四 +{int(bonus * 100)}%')

        group_first = self._is_group_first_today(group_id)
        if group_first and group_id:
            bonus = float(self.get_config('bonus_group_first', '1.0'))
            multiplier_sum += bonus
            bonus_labels.append(f'群内首签 +{int(bonus * 100)}%')

        last = user.get('last_checkin_date')
        streak = int(user.get('streak', 0))
        if last:
            yesterday = (date.today().fromordinal(date.today().toordinal() - 1)).isoformat()
            streak = streak + 1 if last == yesterday else 1
        else:
            streak = 1

        streak_bonus = self._streak_bonus(streak)
        reward_multiplier = 2 if group_id in DOUBLE_CHECKIN_GROUP_IDS else 1
        if reward_multiplier > 1:
            bonus_labels.append(f'指定群 {group_id} ×{reward_multiplier}')
        reward = calculate_checkin_reward(
            base, multiplier_sum, streak_bonus, reward_multiplier
        )

        # 数据存储加成单独加算到基础：extra = round(base × rate × 群倍数)
        # （避免并入 multiplier 后因四舍五入出现「+50% 却多 0」）
        storage_bonus_applied = False
        storage_rate = self._storage_bonus_rate()
        storage_extra = 0
        if bonus_ok and storage_rate > 0 and base > 0:
            storage_extra = int(round(base * storage_rate * reward_multiplier))
            if storage_extra > 0:
                reward += storage_extra
                bonus_labels.append(
                    f'数据存储 +{int(round(storage_rate * 100))}%（+{storage_extra}）'
                )
                storage_bonus_applied = True
                multiplier_sum += storage_rate

        now = time.time()
        self._conn.execute(
            """UPDATE break_users SET
               balance = balance + ?,
               streak = ?,
               last_checkin_date = ?,
               updated_at = ?
               WHERE qqid = ?""",
            (reward, streak, today, now, qqid),
        )
        self._ensure_daily(qqid)
        self._conn.execute(
            """UPDATE break_daily_usage SET break_gained = break_gained + ?
               WHERE qqid = ? AND date = ?""",
            (reward, qqid, today),
        )
        if group_first and group_id:
            self._conn.execute(
                'INSERT OR IGNORE INTO break_group_checkin (group_id, date, first_qqid) VALUES (?, ?, ?)',
                (group_id, today, qqid),
            )
        self._append_log(
            qqid,
            reward,
            'checkin',
            meta={
                'streak': streak,
                'labels': bonus_labels,
                'group_id': group_id,
                'base': base,
                'base_range': [base_min, base_max],
                'reward_multiplier': reward_multiplier,
                'storage_bonus': storage_bonus_applied,
                'storage_bonus_rate': storage_rate if storage_bonus_applied else 0,
            },
        )
        # 已在签到时计入存储加成时，占住当日幂等键，避免「开启存储」再补发
        if storage_bonus_applied and storage_extra > 0:
            try:
                self._conn.execute(
                    """INSERT OR IGNORE INTO break_daily_reward
                       (qqid, date, reward_key, amount, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (qqid, today, 'checkin_storage_bonus', storage_extra, now),
                )
            except Exception:
                pass
        self._conn.commit()

        return CheckinResult(
            qqid=qqid,
            reward=reward,
            balance=self.get_balance(qqid),
            streak=streak,
            streak_bonus=streak_bonus,
            base=base,
            multiplier_sum=multiplier_sum,
            base_min=base_min,
            base_max=base_max,
            bonus_labels=bonus_labels,
            storage_enabled=bool(storage_enabled),
            prompt_enable_storage=not bool(storage_enabled),
        )

    def format_storage_pending_tip(self) -> str:
        return (
            '\n⏳ 数据存储已开启：保持开启至明日签到即可享受基础 +50% BREAK'
            '（当天开关不计入，防刷）'
        )

    def _storage_bonus_cooldown_days(self) -> int:
        return max(
            1,
            _parse_config_int(
                self.get_config(
                    'storage_bonus_cooldown_days',
                    DEFAULT_CONFIG['storage_bonus_cooldown_days'],
                ),
                7,
            ),
        )

    def has_recent_storage_checkin_bonus(self, qqid: int, *, days: Optional[int] = None) -> bool:
        """冷却期内是否已领过签到·数据存储加成（含签到当场计入）。"""
        window = self._storage_bonus_cooldown_days() if days is None else max(1, int(days))
        cutoff = date.today().toordinal() - (window - 1)
        cutoff_text = date.fromordinal(cutoff).isoformat()
        row = self._conn.execute(
            """SELECT 1 FROM break_daily_reward
               WHERE qqid = ? AND reward_key = 'checkin_storage_bonus'
                 AND date >= ? AND amount > 0
               LIMIT 1""",
            (int(qqid), cutoff_text),
        ).fetchone()
        return row is not None

    def try_grant_checkin_storage_bonus(
        self,
        qqid: int,
        *,
        allow_retroactive: bool = True,
        deny_reason: Optional[str] = None,
    ) -> Optional[DailyRewardResult]:
        """今日已签到且签到时未含数据存储加成时，补发 base × 存储加成 × 群倍数。

        防刷：冷却期内已领过 / 外部判定不允许补发时直接拒绝。
        """
        if deny_reason:
            return None
        if not allow_retroactive:
            return None
        self._ensure_user(qqid)
        today = self._today()
        user = self.get_user_row(qqid)
        if user.get('last_checkin_date') != today:
            return None

        if self.has_recent_storage_checkin_bonus(qqid):
            return None

        row = self._conn.execute(
            """SELECT meta FROM break_log
               WHERE qqid = ? AND reason = 'checkin'
               ORDER BY id DESC LIMIT 1""",
            (qqid,),
        ).fetchone()
        if not row:
            return None
        try:
            meta = json.loads(row['meta'] or '{}')
        except Exception:
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        if meta.get('storage_bonus'):
            return None

        base = int(meta.get('base') or 0)
        reward_multiplier = max(1, int(meta.get('reward_multiplier') or 1))
        rate = self._storage_bonus_rate()
        extra = int(round(base * rate * reward_multiplier))
        if extra <= 0:
            return None

        result = self.claim_daily_reward(
            qqid,
            'checkin_storage_bonus',
            extra,
            reason='checkin_storage_bonus',
            meta={
                'base': base,
                'reward_multiplier': reward_multiplier,
                'storage_bonus_rate': rate,
                'from_checkin_meta': True,
            },
        )
        return result

    def _makeup_checkin_costs(self) -> tuple[int, ...]:
        return parse_makeup_checkin_costs(
            self.get_config(
                'makeup_checkin_costs', DEFAULT_CONFIG['makeup_checkin_costs']
            )
        )

    def _checkin_exists_on(self, qqid: int, target: date) -> bool:
        target_text = target.isoformat()
        ordinary = self._conn.execute(
            """SELECT 1 FROM break_log
               WHERE qqid = ? AND reason = 'checkin'
                 AND date(created_at, 'unixepoch', 'localtime') = ?
               LIMIT 1""",
            (qqid, target_text),
        ).fetchone()
        if ordinary:
            return True
        makeup = self._conn.execute(
            'SELECT 1 FROM break_makeup_checkin WHERE qqid = ? AND target_date = ?',
            (qqid, target_text),
        ).fetchone()
        return makeup is not None

    def _latest_checkin_before(self, qqid: int, target: date) -> tuple[Optional[date], int]:
        target_text = target.isoformat()
        ordinary = self._conn.execute(
            """SELECT date(created_at, 'unixepoch', 'localtime') AS checkin_date, meta
               FROM break_log
               WHERE qqid = ? AND reason = 'checkin'
                 AND date(created_at, 'unixepoch', 'localtime') < ?
               ORDER BY created_at DESC LIMIT 1""",
            (qqid, target_text),
        ).fetchone()
        candidates: list[tuple[date, int]] = []
        if ordinary and ordinary['checkin_date']:
            try:
                meta = json.loads(ordinary['meta'] or '{}')
            except (TypeError, ValueError, json.JSONDecodeError):
                meta = {}
            candidates.append(
                (date.fromisoformat(str(ordinary['checkin_date'])), int(meta.get('streak') or 0))
            )
        makeup = self._conn.execute(
            """SELECT target_date, streak FROM break_makeup_checkin
               WHERE qqid = ? AND target_date < ?
               ORDER BY target_date DESC LIMIT 1""",
            (qqid, target_text),
        ).fetchone()
        if makeup:
            candidates.append(
                (date.fromisoformat(str(makeup['target_date'])), int(makeup['streak']))
            )
        return max(candidates, default=(None, 0), key=lambda item: item[0] or date.min)

    def makeup_yesterday(self, qqid: int) -> MakeupCheckinResult:
        """消耗 BREAK 补昨天，仅修复连续签到，不补发昨天奖励。"""
        self._ensure_user(qqid)
        today = date.today()
        target = date.fromordinal(today.toordinal() - 1)
        used_month = today.strftime('%Y-%m')
        costs = self._makeup_checkin_costs()
        with self._lock:
            if self._checkin_exists_on(qqid, target):
                raise ValueError('昨天已经签到过，无需补签。')
            used = int(
                self._conn.execute(
                    """SELECT COUNT(*) AS count FROM break_makeup_checkin
                       WHERE qqid = ? AND used_month = ?""",
                    (qqid, used_month),
                ).fetchone()['count']
            )
            if used >= len(costs):
                raise ValueError(f'本月补签次数已用完（{len(costs)}/{len(costs)}）。')
            cost = costs[used]
            user = self.get_user_row(qqid)
            balance = int(user.get('balance', 0))
            if balance < cost:
                raise BreakInsufficientError(cost, balance, qqid=qqid)

            previous_date, previous_streak = self._latest_checkin_before(qqid, target)
            last_date, streak = calculate_makeup_streak(
                user.get('last_checkin_date'),
                int(user.get('streak', 0)),
                target,
                today,
                previous_checkin_date=previous_date,
                previous_streak=previous_streak,
            )
            now = time.time()
            monthly_no = used + 1
            try:
                self._conn.execute(
                    """INSERT INTO break_makeup_checkin
                       (qqid, target_date, used_month, monthly_no, cost, streak, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (qqid, target.isoformat(), used_month, monthly_no, cost, streak, now),
                )
                self._conn.execute(
                    """UPDATE break_users SET balance = balance - ?, streak = ?,
                       last_checkin_date = ?, updated_at = ? WHERE qqid = ?""",
                    (cost, streak, last_date, now, qqid),
                )
                self._ensure_daily(qqid)
                self._conn.execute(
                    """UPDATE break_daily_usage SET break_spent = break_spent + ?
                       WHERE qqid = ? AND date = ?""",
                    (cost, qqid, self._today()),
                )
                self._append_log(
                    qqid,
                    -cost,
                    'checkin_makeup',
                    meta={
                        'target_date': target.isoformat(),
                        'monthly_no': monthly_no,
                        'streak': streak,
                        'reward': 0,
                    },
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            return MakeupCheckinResult(
                qqid=qqid,
                target_date=target.isoformat(),
                monthly_no=monthly_no,
                monthly_limit=len(costs),
                cost=cost,
                balance=balance - cost,
                streak=streak,
                next_cost=costs[monthly_no] if monthly_no < len(costs) else None,
            )


break_db = BreakDatabase()


@dataclass
class _BreakChargeSession:
    spent: int = 0
    used_free: bool = False
    balance: int = 0


_billing_qqid: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
    'break_billing_qqid', default=None
)
_charge_session: contextvars.ContextVar[Optional[_BreakChargeSession]] = contextvars.ContextVar(
    'break_charge_session', default=None
)
_pending_charge_footer: contextvars.ContextVar[Optional[List[str]]] = contextvars.ContextVar(
    'break_pending_footer', default=None
)


def get_billing_qqid() -> Optional[int]:
    return _billing_qqid.get()


@asynccontextmanager
async def break_billing(qqid: Optional[int]):
    """指令级扣费上下文：查分器/落雪成绩 API 成功后会在此 qq 上结算 BREAK。"""
    payer = int(qqid) if qqid else None
    if payer and is_superuser_exempt(payer):
        payer = None
    t1 = _billing_qqid.set(payer)
    t2 = _charge_session.set(
        _BreakChargeSession(balance=break_db.get_balance(payer) if payer else 0)
    )
    try:
        yield
    finally:
        session = _charge_session.get()
        if session and (session.spent > 0 or session.used_free):
            if session.spent == 0:
                lines = [f'💳 今日首次查分免费 · 余额 {session.balance} BREAK']
            else:
                hint = '（含今日免费）' if session.used_free else ''
                lines = [f'💳 消耗 {session.spent} BREAK{hint} · 余额 {session.balance} BREAK']
            _pending_charge_footer.set(lines)
        _billing_qqid.reset(t1)
        _charge_session.reset(t2)


def take_break_charge_footer() -> List[str]:
    lines = _pending_charge_footer.get() or []
    _pending_charge_footer.set(None)
    return lines


def format_break_insufficient_message(
    qqid: Optional[int],
    required: int,
    current: int,
) -> str:
    checked = break_db.is_checked_in_today(qqid) if qqid else False
    lines = [f'❌ BREAK 不足（需要 {required}，当前 {current}）']
    if checked:
        lines.append('今日已签到，请明天再试。')
    else:
        lines.append('发送「AWMC签到」获取 BREAK；每日首次查分免费哦~')
    return '\n'.join(lines)


def _config_int(key: str, default: int) -> int:
    try:
        return int(float(break_db.get_config(key, str(default))))
    except (TypeError, ValueError):
        return default


def is_superuser_exempt(qqid: int) -> bool:
    from .maimaidx_bot_admin import is_plugin_admin
    return is_plugin_admin(qqid)


def query_cost() -> int:
    return _config_int('query_cost', 1)


_ANALYSIS_PEAK_WINDOWS_UTC8 = (
    (9, 0, 12, 0),   # 09:00–12:00
    (14, 0, 18, 0),  # 14:00–18:00
)


def is_analysis_peak_hour() -> bool:
    """锐评峰时（UTC+8）：09:00–12:00、14:00–18:00，与 DeepSeek 峰谷策略对齐。"""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone(timedelta(hours=8)))
    minutes = now.hour * 60 + now.minute
    for h1, m1, h2, m2 in _ANALYSIS_PEAK_WINDOWS_UTC8:
        start = h1 * 60 + m1
        end = h2 * 60 + m2
        if start <= minutes < end:
            return True
    return False


def analysis_base_cost() -> int:
    return _config_int('analysis_cost', 3)


def analysis_cost() -> int:
    """兼容旧调用：返回 usage 缺失时的兜底价。"""
    return _config_int('analysis_fallback_cost', analysis_base_cost())


def analysis_token_cost(
    input_tokens: int,
    output_tokens: int,
    *,
    usage_available: bool = True,
) -> int:
    """按模型实际 Token 用量计算锐评价格，并应用最低/最高保护。"""
    minimum = max(0, _config_int('analysis_min_cost', 2))
    maximum = max(minimum, _config_int('analysis_max_cost', 20))
    if not usage_available:
        fallback = _config_int('analysis_fallback_cost', 4)
        return min(maximum, max(minimum, fallback))
    input_rate = max(1, _config_int('analysis_input_tokens_per_break', 4000))
    output_rate = max(1, _config_int('analysis_output_tokens_per_break', 1000))
    weighted = max(0, int(input_tokens)) / input_rate
    weighted += max(0, int(output_tokens)) / output_rate
    return min(maximum, max(minimum, int(math.ceil(weighted))))


def format_analysis_cost_line(
    *,
    charged: Optional[int] = None,
    balance: Optional[int] = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    usage_available: bool = True,
) -> str:
    """向用户展示 Token 用量、实际收费和余额。"""
    cost = charged if charged is not None else analysis_token_cost(
        input_tokens, output_tokens, usage_available=usage_available
    )
    if usage_available:
        detail = f'输入 {max(0, input_tokens):,} / 输出 {max(0, output_tokens):,} Token'
    else:
        detail = '模型未返回 Token 用量，按兜底价计费'
    text = f'💳 锐评消耗 {cost} BREAK（{detail}）'
    if balance is not None:
        text += f' · 余额 {balance} BREAK'
    input_rate = max(1, _config_int('analysis_input_tokens_per_break', 4000))
    output_rate = max(1, _config_int('analysis_output_tokens_per_break', 1000))
    minimum = max(0, _config_int('analysis_min_cost', 2))
    maximum = max(minimum, _config_int('analysis_max_cost', 20))
    text += (
        f'\n计费规则：输入每 {input_rate:,} Token + 输出每 {output_rate:,} Token '
        f'各计 1 BREAK，合计向上取整，最低 {minimum}、最高 {maximum}。'
    )
    return text


def format_analysis_pricing_help() -> str:
    input_rate = max(1, _config_int('analysis_input_tokens_per_break', 4000))
    output_rate = max(1, _config_int('analysis_output_tokens_per_break', 1000))
    minimum = max(0, _config_int('analysis_min_cost', 2))
    maximum = max(minimum, _config_int('analysis_max_cost', 20))
    fallback = min(
        maximum,
        max(minimum, _config_int('analysis_fallback_cost', 4)),
    )
    return (
        f'· 分析b50 / 锐评一下 — 按实际 Token 计费：每 {input_rate:,} 输入 Token '
        f'+ 每 {output_rate:,} 输出 Token 各计 1 BREAK，合计向上取整；'
        f'最低 {minimum}、最高 {maximum} BREAK，不设峰时加价；usage 缺失时 {fallback} BREAK。'
        '先用后付，可扣至负数；负余额期间暂停其他功能\n'
    )


def ensure_query_affordable(qqid: Optional[int]) -> None:
    """查分器/落雪成绩 API 即将发起前：余额或免费额度检查。"""
    if not qqid or is_superuser_exempt(qqid):
        return
    cost = query_cost()
    balance = break_db.get_balance(qqid)
    if break_db.is_daily_free_available(qqid):
        return
    if balance < cost:
        raise BreakInsufficientError(cost, balance, qqid=qqid)


def settle_prober_fetch(qqid: Optional[int]) -> None:
    """单次查分器/落雪成绩 API 成功后结算（免费额度或扣 BREAK）。"""
    if not qqid or is_superuser_exempt(qqid):
        return
    session = _charge_session.get()
    cost = query_cost()
    if break_db.is_daily_free_available(qqid):
        break_db.mark_daily_free_used(qqid)
        break_db.record_usage(qqid, 'query', break_delta=0)
        if session:
            session.used_free = True
            session.balance = break_db.get_balance(qqid)
        log.debug(f'[BREAK] qq={qqid} daily free query')
        return
    if not break_db.try_consume(qqid, cost, 'query', meta={'kind': 'prober_api'}):
        log.warning(f'[BREAK] qq={qqid} query consume failed after fetch')
        return
    break_db.record_usage(qqid, 'query', break_delta=-cost)
    if session:
        session.spent += cost
        session.balance = break_db.get_balance(qqid)


def settle_query_api_charge(qqid: Optional[int]) -> None:
    """兼容旧调用：在扣费上下文中等价于 settle_prober_fetch。"""
    if get_billing_qqid() is not None:
        settle_prober_fetch(qqid)
        return
    if not qqid or is_superuser_exempt(qqid):
        return
    from .maimaidx_player_cache import peek_fetch_meta

    meta = peek_fetch_meta()
    if meta is None or meta.origin != 'api':
        return
    cost = query_cost()
    if break_db.is_daily_free_available(qqid):
        break_db.mark_daily_free_used(qqid)
        break_db.record_usage(qqid, 'query', break_delta=0)
        return
    if not break_db.try_consume(qqid, cost, 'query', meta={'kind': 'prober_api'}):
        return
    break_db.record_usage(qqid, 'query', break_delta=-cost)


def settle_analysis_charge(
    qqid: int,
    cost: int,
    *,
    token_usage: Optional[dict] = None,
) -> int:
    cost = max(0, int(cost))
    usage = dict(token_usage or {})
    if is_superuser_exempt(qqid):
        break_db.record_usage(qqid, 'analysis', break_delta=0)
        return 0
    meta = {'kind': 'llm', 'pricing': 'token', **usage}
    balance = break_db.add_balance(qqid, -cost, 'b50_analysis', meta=meta)
    break_db.record_usage(qqid, 'analysis', break_delta=-cost)
    if balance < 0:
        log.info(
            f'[BREAK] qq={qqid} 锐评先用后付 cost={cost} balance={balance}'
        )
    return cost


def get_account_profile(qqid: int) -> AccountProfile:
    from .maimaidx_account_db import account_db
    from .maimaidx_data_storage import data_storage
    from .maimaidx_lxns_db import lxns_db

    user = break_db.get_user_row(qqid)
    daily = break_db.get_daily_row(qqid)
    account = account_db.get(str(qqid))
    account_usage = account_db.get_usage_stats(str(qqid))
    today = break_db._today()
    return AccountProfile(
        qqid=qqid,
        balance=int(user.get('balance', 0)),
        streak=int(user.get('streak', 0)),
        last_checkin_date=user.get('last_checkin_date'),
        checked_in_today=user.get('last_checkin_date') == today,
        today_query_count=int(daily.get('query_count', 0)),
        today_analysis_count=int(daily.get('analysis_count', 0)),
        today_break_spent=int(daily.get('break_spent', 0)),
        today_break_gained=int(daily.get('break_gained', 0)),
        free_used_today=bool(int(daily.get('free_used', 0))),
        total_query_count=int(user.get('total_query_count', 0)),
        total_analysis_count=int(user.get('total_analysis_count', 0)),
        last_query_at=user.get('last_query_at'),
        last_analysis_at=user.get('last_analysis_at'),
        data_source=lxns_db.get_source(qqid),
        theme=lxns_db.get_theme(qqid),
        storage_enabled=data_storage.is_enabled(qqid),
        account_bound=bool(account and account.is_bound),
        account_today_total=account_usage['today_total'],
        account_today_success=account_usage['today_success'],
        account_today_error=account_usage['today_error'],
        account_total=account_usage['total'],
        account_total_success=account_usage['success'],
        account_total_error=account_usage['error'],
        account_operation_counts=account_usage['operations'],
        account_ticket_stats=account_usage.get('ticket') or {},
        recent_account_logs=account_usage['recent'],
        recent_logs=break_db.get_recent_logs(qqid, 5),
    )


def format_account_profile(profile: AccountProfile, *, title: str = '我的 AWMC 账号') -> str:
    return '\n\n'.join(format_account_profile_sections(profile, title=title))


def format_account_profile_sections(
    profile: AccountProfile, *, title: str = '我的 AWMC 账号'
) -> List[str]:
    def _ts(val: Optional[float]) -> str:
        if not val:
            return '暂无'
        return datetime.fromtimestamp(val).strftime('%m-%d %H:%M')

    src = '落雪' if profile.data_source == 'lxns' else '水鱼'
    storage = '已开启' if profile.storage_enabled else '未开启'
    checkin = '已完成' if profile.checked_in_today else '未签到'
    free = '已用' if profile.free_used_today else '可用'

    account_state = '已绑定' if profile.account_bound else '未绑定'
    overview = [
        f'📋 {title}',
        '━━━━━━━━━━━━━━',
        f'🆔 QQ：{profile.qqid}',
        f'💳 BREAK 余额：{profile.balance}',
        f'📅 连续签到：{profile.streak} 天 · 上次签到：{profile.last_checkin_date or "暂无"}',
        f'🎁 今日签到：{checkin}',
        f'🔗 舞萌账号：{account_state}',
    ]
    today_lines = [
        '📊 今日使用',
        f'  · 查分器 API：{profile.today_query_count} 次（消耗 {profile.today_break_spent} BREAK 合计含分析）',
        f'  · 分析 b50：{profile.today_analysis_count} 次',
        f'  · 今日 BREAK 获得：+{profile.today_break_gained}',
        f'  · 每日免费查分：{free}',
        f'  · 账号功能：{profile.account_today_total} 次'
        f'（成功 {profile.account_today_success} / 失败 {profile.account_today_error}）',
    ]
    total_lines = [
        '📈 累计统计',
        f'  · 查分 API 总计：{profile.total_query_count} 次',
        f'  · 分析 b50 总计：{profile.total_analysis_count} 次',
        f'  · 上次查分：{_ts(profile.last_query_at)}',
        f'  · 上次分析：{_ts(profile.last_analysis_at)}',
        f'  · 账号功能总计：{profile.account_total} 次'
        f'（成功 {profile.account_total_success} / 失败 {profile.account_total_error}）',
    ]
    operation_labels = {
        'bind': '账号绑定', 'unbind': '账号解绑', 'status': '账号状态',
        'upload': '成绩上传', 'ticket': '发票', 'bind_fish': '绑定水鱼',
        'bind_lx': '绑定落雪',
    }
    if profile.account_operation_counts:
        detail = ' / '.join(
            f'{operation_labels.get(name, name)} {count}'
            for name, count in profile.account_operation_counts.items()
        )
        total_lines.append(f'  · 功能分布：{detail}')
    ticket = profile.account_ticket_stats or {}
    ticket_total = int(ticket.get('total') or 0)
    if ticket_total > 0:
        total_lines.append(
            f'  · 发票：成功 {int(ticket.get("success") or 0)}'
            f'（{ticket.get("success_rate", 0)}%）'
            f' / 失败 {int(ticket.get("error") or 0)}'
            f'（{ticket.get("error_rate", 0)}%）'
        )
        total_lines.append(
            f'  · 发票 returnCode=0：{int(ticket.get("return_code_0") or 0)} 次'
            f'（占全部 {ticket.get("return_code_0_rate", 0)}%）；'
            f'null/未返回 {int(ticket.get("return_code_null") or 0)} 次'
        )
    preference_lines = [
        '⚙️ 插件偏好',
        f'  · 查分数据源：{src}',
        f'  · B50 主题：{profile.theme}',
        f'  · 数据存储：{storage}',
    ]
    recent_lines: List[str] = []
    if profile.recent_account_logs:
        recent_lines.append('🧾 最近账号功能记录（最多 10 条）')
        for entry in profile.recent_account_logs:
            ts = datetime.fromtimestamp(float(entry['created_at'])).strftime('%m-%d %H:%M')
            status = '成功' if entry['status'] == 'success' else '失败'
            label = operation_labels.get(str(entry['operation']), str(entry['operation']))
            recent_lines.append(f'  · {ts}  {label} · {status} · {entry["ref_id"]}')
    if profile.recent_logs:
        recent_lines.append('')
        recent_lines.append('📝 最近 BREAK 记录（最多 5 条）')
        reason_map = {
            'query': '查分',
            'checkin': '签到',
            'checkin_makeup': '补签',
            'checkin_storage_bonus': '签到·数据存储加成',
            'today_luck': '今日舞萌',
            'b50_analysis': '分析b50',
            'busy_request_surcharge': '高负载请求附加费',
            'guess_reward': '猜歌奖励',
            'admin_set': '管理员设置',
            'admin_add': '管理员调整',
        }
        for entry in profile.recent_logs:
            ts = datetime.fromtimestamp(entry.created_at).strftime('%m-%d %H:%M')
            sign = '+' if entry.delta >= 0 else ''
            label = reason_map.get(entry.reason, entry.reason)
            recent_lines.append(f'  · {ts}  {sign}{entry.delta}  {label}')
    sections = [overview, today_lines, total_lines, preference_lines]
    if recent_lines:
        sections.append(recent_lines)
    return ['\n'.join(lines) for lines in sections]


def format_checkin_result(result: CheckinResult) -> str:
    if result.already_checked:
        text = f'今天已经签到过啦~ 当前 BREAK：{result.balance}'
        if result.prompt_enable_storage:
            text += (
                '\n💡 还未开启数据存储：发送「开启存储数据」'
                '并保持开启，明日签到可享基础 +50% BREAK'
            )
        return text
    bonus = ' · '.join(result.bonus_labels) if result.bonus_labels else '无额外加成'
    streak_extra = f'（+{result.streak_bonus} BREAK）' if result.streak_bonus else ''
    range_hint = (
        f'{result.base} BREAK（随机 {result.base_min}~{result.base_max}）'
        if result.base_min != result.base_max
        else f'{result.base} BREAK'
    )
    text = (
        '✅ AWMC 签到成功！\n'
        '━━━━━━━━━━━━━━\n'
        f'📅 连续签到：{result.streak} 天{streak_extra}\n'
        f'🎲 随机基础：{range_hint}\n'
        f'✨ 今日加成：{bonus}\n'
        f'💰 获得：{result.reward} BREAK\n'
        f'💳 当前余额：{result.balance} BREAK'
    )
    if result.prompt_enable_storage:
        text += (
            '\n━━━━━━━━━━━━━━\n'
            '💡 发送「开启存储数据」可享签到基础 +50% BREAK\n'
            '（保持开启至明日签到生效；频繁开关不重复发放，防刷）'
        )
    return text


def format_makeup_checkin_result(result: MakeupCheckinResult) -> str:
    next_line = (
        f'🎟 本月已用 {result.monthly_no}/{result.monthly_limit} 次；'
        f'下次需 {result.next_cost} BREAK'
        if result.next_cost is not None
        else f'🎟 本月已用 {result.monthly_no}/{result.monthly_limit} 次，次数已用完'
    )
    return (
        '✅ AWMC 补签成功！\n'
        '━━━━━━━━━━━━━━\n'
        f'📅 已补日期：{result.target_date}（昨天）\n'
        f'🔥 连续签到：{result.streak} 天\n'
        f'💳 消耗：{result.cost} BREAK · 余额 {result.balance} BREAK\n'
        f'{next_line}\n'
        '补签仅修复连续签到，不补发昨天的签到奖励。'
    )
