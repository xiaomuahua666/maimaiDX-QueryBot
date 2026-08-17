"""
AWMC BREAK 积分：签到、查分扣费、账号统计。

- 统一通过 UnifiedConnection 支持 SQLite / MySQL
- 签到倍率加算叠加；查分仅在实际 API 请求时扣费
"""

from __future__ import annotations

import base64
import asyncio
import contextvars
import json
import math
import random
import re
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from ..config import BOT_QQ_GROUP, log, maiconfig
from .maimaidx_db import create_unified_connection
from .maimaidx_error import BreakInsufficientError

DEFAULT_CONFIG: Dict[str, str] = {
    # BREAK 计费总开关：1=开启（默认）；0/false/off/关闭=停止所有功能扣费与余额拦截。
    # 关闭后签到 / 转账 / 红包 / 管理员增减仍正常，仅 Bot 不再对功能使用收费。
    'billing_enabled': '1',
    'checkin_base_min': '1',
    'checkin_base_max': '2',
    'query_cost': '1',
    'cache_query_cost': '1',
    # 每次成功出图（含读缓存）额外收取的生成图片费用；0 = 关闭。
    'image_render_cost': '1',
    'analysis_input_tokens_per_break': '4000',
    'analysis_output_tokens_per_break': '1000',
    'analysis_min_cost': '2',
    'analysis_max_cost': '20',
    'analysis_fallback_cost': '4',
    # 锐评按原 Token 价格计费；调用模型前先预扣固定额度。
    'analysis_price_multiplier': '1',
    'analysis_precharge_cost': '10',
    # 第 1～5 天按曲线递增；streak_bonus_growth 控制超过曲线后每天的增长量，0 = 封顶。
    # 曲线收敛至 3，避免连签奖励成为长期通胀来源。
    'streak_bonus': '1,1,2,2,3',
    'streak_bonus_growth': '0',
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
    # 小游戏每日 BREAK 上限：全局总闸（0=不限制）+ 每游戏节流阀（0=该游戏不限制）。
    # game_key ∈ song/cover/tune/chart/rating/impostor/duel/twentyq/letter。
    'guess_daily_break_global_cap': '40',
    'guess_daily_caps': 'song:20,cover:20,tune:20,chart:20,rating:15,impostor:15,duel:10,twentyq:20,letter:15',
    # 上传/发票仅在外部操作成功后结算；上传每日首次免费，发票每次扣费。
    'upload_fish_cost': '2',
    'upload_lx_cost': '2',
    'upload_all_cost': '3',
    'ticket_cost_per_multiplier': '10',
    'ticket_status_cost': '1',
    'awmc_read_cost': '5',
    'awmc_game_event_cost': '2',
    'awmc_status_cost': '2',
    'awmc_music_upsert_cost': '75',
    'awmc_music_delete_cost': '50',
    'awmc_item_upsert_cost': '100',
    'ticket_unused_penalty': '20',
    'transfer_fee': '0',
    'lottery_cost': '2',
    'weekly_report_cost': '1',
    'monthly_report_cost': '2',
    'annual_report_cost': '3',
    'daily_report_cost': '0',
    'coop_b50_cost': '2',
    'red_packet_expire_minutes': '10',
    'red_packet_max_total': '10000',
    'red_packet_max_count': '100',
    # 限时免费时段：free_window_enabled=1 开启；free_window_hours 如 '17,20'
    # 表示每天 17:00~20:00 全功能免费（含查分/出图/锐评/上传等所有扣费点）。
    # 格式异常或缺失时安全降级为「不免费」，绝不抛异常。
    'free_window_enabled': '0',
    'free_window_hours': '',
}

LEGACY_ECONOMY_DEFAULTS: Dict[str, str] = {
    'checkin_base_min': '1',
    'checkin_base_max': '5',
    'bonus_group_1072033605': '0.5',
    'bonus_thursday': '1.0',
    'bonus_group_first': '1.0',
}

# 历史上出现过的连签曲线默认值，启动时迁移到当前温和曲线。
LEGACY_STREAK_DEFAULTS = frozenset({'1,2,3,4,5', '0,0,1,1,1,2,2', '3,5,8,12,20'})

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
CREATE TABLE IF NOT EXISTS break_game_daily (
    qqid            INTEGER NOT NULL,
    date            TEXT NOT NULL,
    game            TEXT NOT NULL,
    break_awarded   INTEGER NOT NULL DEFAULT 0,
    last_at         REAL NOT NULL,
    PRIMARY KEY (qqid, date, game)
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
CREATE TABLE IF NOT EXISTS break_gamble_pool (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    qqid        INTEGER NOT NULL,
    date        TEXT NOT NULL,
    amount      INTEGER NOT NULL,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_break_gamble_pool_date
    ON break_gamble_pool(date, amount DESC);
CREATE TABLE IF NOT EXISTS break_gamble_pool_payout (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    qqid        INTEGER NOT NULL,
    date        TEXT NOT NULL,
    amount      INTEGER NOT NULL,
    payout_type TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_break_gamble_pool_payout_date
    ON break_gamble_pool_payout(date);
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
    doubled: bool = False
    double_remaining: float = 0.0
    capped: bool = False


@dataclass
class GameBreakAward:
    """小游戏 BREAK 发放结果（双层上限口返回）。"""
    game: str
    requested: int
    awarded: int
    capped: bool
    doubled: bool = False
    double_remaining: float = 0.0
    balance: int = 0


@dataclass
class ServiceChargeResult:
    service: str
    charged: int
    free: bool
    balance: int
    listed_cost: int = 0
    freedom: bool = False
    freedom_remaining: float = 0.0
    billing_disabled: bool = False
    free_window: bool = False


@dataclass(frozen=True)
class AnalysisChargeReservation:
    amount: int
    freedom: bool = False
    freedom_remaining: float = 0.0
    free_window: bool = False


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


# 倾家荡产模式定义
GAMBLE_MODES = ('标准', '刺激', '高风险')
GAMBLE_ENTRY_COST = {'标准': 2, '刺激': 3, '高风险': 5}

# 各模式概率：(倍率, 权重)
# 标准模式：70% 谢谢参与，~60% 期望返还率
GAMBLE_WEIGHTS_STANDARD = (
    (0, 70),    # 谢谢参与
    (1, 15),    # 返还本金
    (2, 8),     # 2 倍
    (5, 4),     # 5 倍
    (10, 2),    # 10 倍
    (30, 0.8),  # 30 倍
    (50, 0.2),  # 50 倍
)
# 刺激模式：78% 谢谢参与，~55% 期望返还率，波动更大
GAMBLE_WEIGHTS_EXCITING = (
    (0, 78),    # 谢谢参与
    (2, 10),    # 2 倍
    (5, 6),     # 5 倍
    (20, 4),    # 20 倍
    (50, 1.5),  # 50 倍
    (100, 0.5), # 100 倍
)
# 高风险模式：82% 谢谢参与，~50% 期望返还率，极不稳定
GAMBLE_WEIGHTS_RISKY = (
    (0, 82),    # 谢谢参与
    (5, 8),     # 5 倍
    (20, 6),    # 20 倍
    (50, 3),    # 50 倍
    (100, 1),   # 100 倍
)

GAMBLE_WEIGHTS_MAP = {
    '标准': GAMBLE_WEIGHTS_STANDARD,
    '刺激': GAMBLE_WEIGHTS_EXCITING,
    '高风险': GAMBLE_WEIGHTS_RISKY,
}


@dataclass
class GambleAllResult:
    mode: str
    balance_before: int
    multiplier: int
    win_amount: int
    balance_after: int


@dataclass
class GamblePoolContributor:
    qqid: int
    amount: int


@dataclass
class GamblePoolStatus:
    date: str
    total_pool: int
    distributable: int  # total_pool * 80%
    contributors: List[GamblePoolContributor]


def _parse_config_int(raw: str, default: int) -> int:
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def calculate_streak_bonus(streak: int, bonuses: list[int], growth: int) -> int:
    """按配置曲线计算连签奖励；超过曲线后按 growth 线性增长，growth=0 时封顶。"""
    if not bonuses:
        return 0
    idx = max(int(streak) - 1, 0)
    if idx < len(bonuses):
        return max(0, int(bonuses[idx]))
    extra = idx - len(bonuses) + 1
    if extra <= 0 or int(growth) <= 0:
        return max(0, int(bonuses[-1]))
    return max(0, int(bonuses[-1])) + int(growth) * extra


def calculate_luck_break(luck: int) -> tuple[int, int]:
    """人品值按普通四舍五入取整到十位，再 ÷20 换算为 BREAK（整体减半）。"""
    value = max(0, min(100, int(luck)))
    rounded = ((value + 5) // 10) * 10
    return rounded, rounded // 20


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
    """签到最终奖励。

    群倍数（如 ×2 群）只放大基础与百分比加算部分，连签奖励不被群倍数放大，
    避免指定群用户的连签奖励长期翻倍造成经济膨胀。
    """
    multiplier = max(1, int(reward_multiplier))
    base_part = int(round(int(base) * (1 + float(multiplier_sum)) * multiplier))
    return base_part + max(0, int(streak_bonus))


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
        self._conn = create_unified_connection()
        # 建表：SQLite 模式直接执行建表脚本；MySQL 模式自动补建缺失表
        if self._conn._backend == 'sqlite':
            self._conn.executescript(_CREATE_SQL)
        else:
            self._ensure_mysql_tables()
        self._ensure_config_primary_key_mysql()
        self._repair_break_usage_schema_mysql()
        self._seed_config()
        self._prune_unbound_hash_users()
        self._prune_empty_users()

    def _ensure_mysql_tables(self) -> None:
        """MySQL 模式下自动补建缺失的表，避免依赖手动迁移脚本。

        SQLite 模式通过 ``executescript(_CREATE_SQL)`` 一次性建表，
        但 MySQL 模式的 ``executescript`` 是空操作（表理应由迁移脚本创建）。
        如果迁移脚本未跑或新表是在后续版本引入的，这里按需补建，
        确保表结构始终与代码一致。
        """
        prefix = getattr(self._conn, '_prefix', '') or ''
        raw = self._conn._conn
        # _CREATE_SQL 中每条 CREATE TABLE IF NOT EXISTS 的定义
        # 解析出表名和完整 DDL，对 MySQL 逐条检查并补建
        for match in re.finditer(
            r'CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\);',
            _CREATE_SQL,
            re.DOTALL,
        ):
            table_name = match.group(1)
            col_defs = match.group(2)
            prefixed = f'{prefix}{table_name}'
            try:
                with raw.cursor() as cur:
                    cur.execute(
                        "SELECT COUNT(*) AS cnt FROM information_schema.tables "
                        "WHERE table_schema = DATABASE() AND table_name = %s",
                        (prefixed,),
                    )
                    row = cur.fetchone()
                    if row and int(row.get('cnt', 0) or 0) > 0:
                        continue
                    # 表不存在，转换 SQLite DDL 为 MySQL 兼容格式并建表
                    mysql_ddl = self._sqlite_ddl_to_mysql(table_name, col_defs)
                    create_sql = (
                        f'CREATE TABLE `{prefixed}` ({mysql_ddl}) '
                        f'ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 '
                        f'COLLATE=utf8mb4_unicode_ci'
                    )
                    cur.execute(create_sql)
                raw.commit()
                log.info(f'[BREAK] MySQL 自动建表: {prefixed}')
            except Exception as exc:
                try:
                    raw.rollback()
                except Exception:
                    pass
                log.warning(
                    f'[BREAK] MySQL 自动建表 {prefixed} 失败（已忽略）：'
                    f'{type(exc).__name__}: {exc}'
                )

    @staticmethod
    def _sqlite_ddl_to_mysql(table_name: str, col_defs: str) -> str:
        """将 SQLite 建表列定义转换为 MySQL 兼容格式。

        处理以下 SQLite DDL 特性：
        - 列级 PRIMARY KEY（``INTEGER PRIMARY KEY`` / ``TEXT PRIMARY KEY``）
        - ``INTEGER PRIMARY KEY AUTOINCREMENT`` -> ``BIGINT AUTO_INCREMENT PRIMARY KEY``
        - 表级 ``PRIMARY KEY (col1, col2)`` 作为独立行
        - ``FOREIGN KEY`` 约束跳过（前缀表名导致引用失效，且不依赖 FK 约束保证正确性）
        - ``CREATE INDEX`` 行跳过（索引在表外独立创建）
        - INTEGER -> BIGINT, TEXT -> TEXT/VARCHAR(255), REAL -> DOUBLE
        """
        lines = []
        primary_keys = []
        for definition in col_defs.strip().split('\n'):
            pk_match = re.match(
                r'\s*PRIMARY\s+KEY\s*\(([^)]+)\)',
                definition,
                re.IGNORECASE,
            )
            if pk_match:
                primary_keys.extend(
                    column.strip().strip('`"')
                    for column in pk_match.group(1).split(',')
                )
        primary_key_set = set(primary_keys)
        for line in col_defs.strip().split('\n'):
            line = line.strip().rstrip(',')
            if not line:
                continue
            upper = line.upper()
            # 跳过 CREATE INDEX 行
            if upper.startswith('CREATE INDEX'):
                continue
            # 跳过 FOREIGN KEY 约束（前缀表名导致引用失效）
            if upper.startswith('FOREIGN KEY'):
                continue
            # 表级 PRIMARY KEY 行
            if upper.startswith('PRIMARY KEY'):
                continue
            # 解析列名和类型
            parts = line.split(None, 1)
            if len(parts) < 2:
                continue
            col_name = parts[0]
            col_type_raw = parts[1]
            upper_type = col_type_raw.upper()
            # INTEGER PRIMARY KEY AUTOINCREMENT
            if 'INTEGER' in upper_type and 'AUTOINCREMENT' in upper_type:
                lines.append(
                    f'`{col_name}` BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY'
                )
            # INTEGER PRIMARY KEY（列级）
            elif 'INTEGER' in upper_type and 'PRIMARY KEY' in upper_type:
                primary_keys.append(col_name)
                lines.append(f'`{col_name}` BIGINT NOT NULL')
            # 普通 INTEGER 列
            elif 'INTEGER' in upper_type:
                extra = ''
                if 'NOT NULL' in upper_type:
                    extra += ' NOT NULL'
                if 'DEFAULT' in upper_type:
                    m = re.search(r'DEFAULT\s+(\S+)', col_type_raw, re.IGNORECASE)
                    if m:
                        extra += f' DEFAULT {m.group(1)}'
                lines.append(f'`{col_name}` BIGINT{extra}')
            # TEXT PRIMARY KEY（列级）
            elif (
                'TEXT' in upper_type
                and ('PRIMARY KEY' in upper_type or col_name in primary_key_set)
            ):
                if 'PRIMARY KEY' in upper_type:
                    primary_keys.append(col_name)
                lines.append(f'`{col_name}` VARCHAR(191) NOT NULL')
            # 普通 TEXT 列（可能允许 NULL）
            elif 'TEXT' in upper_type:
                extra = ''
                if 'NOT NULL' in upper_type:
                    extra += ' NOT NULL'
                lines.append(f'`{col_name}` TEXT{extra}')
            # REAL / FLOAT -> DOUBLE
            elif 'REAL' in upper_type or 'FLOAT' in upper_type:
                extra = ''
                if 'NOT NULL' in upper_type:
                    extra += ' NOT NULL'
                lines.append(f'`{col_name}` DOUBLE{extra}')
            # 其他类型原样保留
            else:
                lines.append(f'`{col_name}` {col_type_raw}')
        # 如果有主键（表级或列级收集的），追加 PRIMARY KEY 子句
        # 但如果列级 AUTO_INCREMENT 已包含 PRIMARY KEY，则不重复
        has_autoinc_pk = any('AUTO_INCREMENT PRIMARY KEY' in l for l in lines)
        if primary_keys and not has_autoinc_pk:
            lines.append(
                f'PRIMARY KEY ({", ".join(f"`{k}`" for k in primary_keys)})'
            )
        return ',\n    '.join(lines)

    def _ensure_config_primary_key_mysql(self) -> None:
        """修复 MySQL 旧库 break_config 缺少主键导致的重复行问题。

        早期迁移脚本建表时遗漏了 ``key`` 主键，每次启动的
        ``INSERT OR IGNORE`` 都会新增一整份配置；``get_config`` 取到的是
        最早写入的历史默认值（含已废弃的无上限连签曲线），是签到通胀的根因。
        检测到缺失唯一约束时，按最后写入胜出（最近一次设置/最新默认）去重，
        重建为带主键的表。SQLite 不受影响。
        """
        if self._conn._backend != 'mysql':
            return
        prefix = getattr(self._conn, '_prefix', '') or ''
        table = f'{prefix}break_config'
        raw = self._conn._conn
        try:
            with raw.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) AS cnt FROM information_schema.statistics "
                    "WHERE table_schema = DATABASE() AND table_name = %s "
                    "AND column_name = 'key' AND non_unique = 0",
                    (table,),
                )
                row = cur.fetchone() or {}
                if int(row.get('cnt', 0) or 0) > 0:
                    return

                new_table = f'{table}__repair'
                old_table = f'{table}__old'
                cur.execute(f"DROP TABLE IF EXISTS `{new_table}`")
                cur.execute(
                    f"CREATE TABLE `{new_table}` ("
                    f"`key` VARCHAR(191) NOT NULL, "
                    f"`value` TEXT NOT NULL, "
                    f"PRIMARY KEY (`key`)"
                    f") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
                )
                # 无主键 InnoDB 全表扫描按隐藏 rowid（≈插入顺序）返回；
                # 后插入的值覆盖前面，保留最近一次管理员设置或最新默认值。
                cur.execute(
                    f"INSERT INTO `{new_table}` (`key`, `value`) "
                    f"SELECT `key`, `value` FROM `{table}` "
                    f"ON DUPLICATE KEY UPDATE `value` = VALUES(`value`)"
                )
                cur.execute(f"SELECT COUNT(DISTINCT `key`) AS cnt FROM `{table}`")
                distinct = int((cur.fetchone() or {}).get('cnt', 0) or 0)
                cur.execute(f"SELECT COUNT(*) AS cnt FROM `{new_table}`")
                kept = int((cur.fetchone() or {}).get('cnt', 0) or 0)
                if distinct != kept:
                    raise RuntimeError(f'去重后行数异常 {kept} != {distinct}')
                cur.execute(
                    f"RENAME TABLE `{table}` TO `{old_table}`, "
                    f"`{new_table}` TO `{table}`"
                )
                cur.execute(f"DROP TABLE `{old_table}`")
            raw.commit()
            log.info(f'[BREAK] 已修复 MySQL {table} 缺失主键：去重后保留 {kept} 条配置')
        except Exception as exc:
            try:
                raw.rollback()
            except Exception:
                pass
            log.warning(
                f'[BREAK] 修复 {table} 主键失败（已忽略）：'
                f'{type(exc).__name__}: {exc}'
            )

    def _repair_break_usage_schema_mysql(self) -> None:
        """修复旧 SQLite→MySQL 迁移丢失的约束与 NULL 计数。

        旧迁移器没有识别 SQLite 的表级复合主键，导致
        ``break_daily_usage(qqid, date)`` 没有唯一约束。随后每次
        ``INSERT IGNORE`` 都会插入一条新行，而资料卡的普通 SELECT 又可能
        读到最早的 0 行。同时，早期 ``break_users`` 数值列允许 NULL，
        ``NULL + 1`` 永远仍是 NULL，累计查分/分析便一直显示 0。

        日表的每次更新会作用于同一用户同一天的所有重复行，因此最早一行
        保存了完整累计值；按列 MAX 合并可以无损恢复。用户累计计数再以合并
        后的日表求和补齐，最后补上唯一键，迁移全程用 RENAME TABLE 原子切换。
        """
        if self._conn._backend != 'mysql':
            return
        prefix = getattr(self._conn, '_prefix', '') or ''
        users = f'{prefix}break_users'
        daily = f'{prefix}break_daily_usage'
        raw = self._conn._conn
        try:
            with raw.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) AS cnt FROM ("
                    "SELECT index_name, GROUP_CONCAT(column_name "
                    "ORDER BY seq_in_index) AS cols "
                    "FROM information_schema.statistics "
                    "WHERE table_schema = DATABASE() AND table_name = %s "
                    "AND non_unique = 0 GROUP BY index_name"
                    ") AS indexes_found WHERE cols = 'qqid,date'",
                    (daily,),
                )
                has_daily_key = int((cur.fetchone() or {}).get('cnt', 0) or 0) > 0

                if not has_daily_key:
                    repaired = f'{daily}__repair'
                    old = f'{daily}__old'
                    cur.execute(f"DROP TABLE IF EXISTS `{repaired}`")
                    cur.execute(f"DROP TABLE IF EXISTS `{old}`")
                    cur.execute(
                        f"CREATE TABLE `{repaired}` ("
                        "`qqid` BIGINT NOT NULL, `date` VARCHAR(32) NOT NULL, "
                        "`free_used` BIGINT NOT NULL DEFAULT 0, "
                        "`query_count` BIGINT NOT NULL DEFAULT 0, "
                        "`analysis_count` BIGINT NOT NULL DEFAULT 0, "
                        "`break_spent` BIGINT NOT NULL DEFAULT 0, "
                        "`break_gained` BIGINT NOT NULL DEFAULT 0, "
                        "PRIMARY KEY (`qqid`, `date`)"
                        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 "
                        "COLLATE=utf8mb4_unicode_ci"
                    )
                    cur.execute(
                        f"INSERT INTO `{repaired}` "
                        "(`qqid`, `date`, `free_used`, `query_count`, "
                        "`analysis_count`, `break_spent`, `break_gained`) "
                        f"SELECT `qqid`, CAST(`date` AS CHAR(32)), "
                        "MAX(COALESCE(`free_used`, 0)), "
                        "MAX(COALESCE(`query_count`, 0)), "
                        "MAX(COALESCE(`analysis_count`, 0)), "
                        "MAX(COALESCE(`break_spent`, 0)), "
                        "MAX(COALESCE(`break_gained`, 0)) "
                        f"FROM `{daily}` WHERE `qqid` IS NOT NULL "
                        "AND `date` IS NOT NULL GROUP BY `qqid`, CAST(`date` AS CHAR(32))"
                    )
                    cur.execute(f"SELECT COUNT(*) AS cnt FROM `{daily}`")
                    before = int((cur.fetchone() or {}).get('cnt', 0) or 0)
                    cur.execute(f"SELECT COUNT(*) AS cnt FROM `{repaired}`")
                    after = int((cur.fetchone() or {}).get('cnt', 0) or 0)
                    cur.execute(
                        f"RENAME TABLE `{daily}` TO `{old}`, "
                        f"`{repaired}` TO `{daily}`"
                    )
                    cur.execute(f"DROP TABLE `{old}`")
                    log.info(
                        f'[BREAK] 已修复 MySQL 日用量复合主键：'
                        f'{before} 行合并为 {after} 行'
                    )

                # 从已经去重的每日统计恢复 NULL/偏小的历史累计。正常非 NULL
                # 计数用 GREATEST 保留，避免任何旧日表缺失时倒退。
                cur.execute(
                    f"UPDATE `{users}` AS u LEFT JOIN ("
                    "SELECT `qqid`, SUM(`query_count`) AS queries, "
                    "SUM(`analysis_count`) AS analyses "
                    f"FROM `{daily}` GROUP BY `qqid`"
                    ") AS d ON d.qqid = u.qqid SET "
                    "u.total_query_count = GREATEST("
                    "COALESCE(u.total_query_count, 0), COALESCE(d.queries, 0)), "
                    "u.total_analysis_count = GREATEST("
                    "COALESCE(u.total_analysis_count, 0), COALESCE(d.analyses, 0)), "
                    "u.balance = COALESCE(u.balance, 0), "
                    "u.streak = COALESCE(u.streak, 0), "
                    "u.created_at = COALESCE(u.created_at, u.updated_at, UNIX_TIMESTAMP()), "
                    "u.updated_at = COALESCE(u.updated_at, u.created_at, UNIX_TIMESTAMP())"
                )
                cur.execute(
                    f"ALTER TABLE `{users}` "
                    "MODIFY `qqid` BIGINT NOT NULL, "
                    "MODIFY `balance` BIGINT NOT NULL DEFAULT 0, "
                    "MODIFY `streak` BIGINT NOT NULL DEFAULT 0, "
                    "MODIFY `total_query_count` BIGINT NOT NULL DEFAULT 0, "
                    "MODIFY `total_analysis_count` BIGINT NOT NULL DEFAULT 0, "
                    "MODIFY `created_at` DOUBLE NOT NULL, "
                    "MODIFY `updated_at` DOUBLE NOT NULL"
                )
            raw.commit()
        except Exception as exc:
            try:
                raw.rollback()
            except Exception:
                pass
            log.warning(
                f'[BREAK] 修复 MySQL 使用统计失败（读取侧仍会聚合兜底）：'
                f'{type(exc).__name__}: {exc}'
            )

    def _prune_unbound_hash_users(self) -> None:
        """Remove official-QQ hash keys left by the pre-qbind fallback.

        Legacy QQ ids accepted by this plugin are at most 12 digits.  The old
        official-QQ fallback used the first 15 SHA-256 hex digits, producing
        13- to 18-digit ``qqid`` values that cannot belong to a bound account.
        Delete those keys and every BREAK ledger row referring to them.  This
        deliberately leaves ordinary numeric OneBot QQ ids untouched.
        """
        try:
            candidates = self._conn.execute(
                'SELECT qqid FROM break_users WHERE qqid >= 1000000000000'
            ).fetchall()
            if not candidates:
                return
            related_rows = (
                'DELETE FROM break_daily_usage WHERE qqid = ?',
                'DELETE FROM break_group_checkin WHERE first_qqid = ?',
                'DELETE FROM break_makeup_checkin WHERE qqid = ?',
                'DELETE FROM break_log WHERE qqid = ?',
                'DELETE FROM break_guess_daily WHERE qqid = ?',
                'DELETE FROM break_game_daily WHERE qqid = ?',
                'DELETE FROM break_service_daily WHERE qqid = ?',
                'DELETE FROM break_daily_reward WHERE qqid = ?',
                'DELETE FROM break_red_packet_claim '
                'WHERE packet_id IN '
                '(SELECT id FROM break_red_packet WHERE sender_qqid = ?)',
                'DELETE FROM break_red_packet_claim WHERE qqid = ?',
                'DELETE FROM break_red_packet WHERE sender_qqid = ?',
                'DELETE FROM break_gamble_pool WHERE qqid = ?',
                'DELETE FROM break_gamble_pool_payout WHERE qqid = ?',
                'DELETE FROM break_users WHERE qqid = ?',
            )
            removed = 0
            for candidate in candidates:
                qqid = int(candidate['qqid'])
                for sql in related_rows:
                    try:
                        self._conn.execute(sql, (qqid,))
                    except Exception as exc:
                        message = str(exc).lower()
                        if 'no such table' in message or "doesn't exist" in message:
                            continue
                        raise
                removed += 1
            self._conn.commit()
            log.info(f'[BREAK] 已清理 {removed} 条未绑定官方 QQ 哈希用户记录')
        except Exception as exc:
            try:
                self._conn._conn.rollback()
            except Exception:
                pass
            log.warning(
                f'[BREAK] 清理未绑定官方 QQ 哈希记录失败（已忽略）：'
                f'{type(exc).__name__}: {exc}'
            )

    def _prune_empty_users(self) -> None:
        """Remove rows created by read-only checks that never had activity.

        Official QQ used to derive a temporary BREAK key from an unbound
        openid.  Older read paths inserted a zeroed ``break_users`` row even
        when the request was rejected later.  Keep every row with a balance,
        check-in, counter, timestamp, or related business record; only delete
        genuinely empty shells and their zeroed daily/service placeholders.
        """
        try:
            candidates = self._conn.execute(
                """SELECT qqid FROM break_users
                   WHERE COALESCE(balance, 0) = 0
                     AND COALESCE(streak, 0) = 0
                     AND COALESCE(last_checkin_date, '') = ''
                     AND COALESCE(total_query_count, 0) = 0
                     AND COALESCE(total_analysis_count, 0) = 0
                     AND COALESCE(last_query_at, 0) = 0
                     AND COALESCE(last_analysis_at, 0) = 0"""
            ).fetchall()
            if not candidates:
                return

            # A row is meaningful if it appears in any ledger/history table,
            # even when its current balance has returned to zero.
            meaningful_checks = (
                "SELECT 1 FROM break_log WHERE qqid = ? LIMIT 1",
                "SELECT 1 FROM break_group_checkin WHERE first_qqid = ? LIMIT 1",
                "SELECT 1 FROM break_makeup_checkin WHERE qqid = ? LIMIT 1",
                "SELECT 1 FROM break_guess_daily WHERE qqid = ? LIMIT 1",
                """SELECT 1 FROM break_service_daily
                   WHERE qqid = ? AND (COALESCE(success_count, 0) > 0
                       OR COALESCE(free_used, 0) > 0
                       OR COALESCE(break_spent, 0) > 0) LIMIT 1""",
                "SELECT 1 FROM break_daily_reward WHERE qqid = ? LIMIT 1",
                "SELECT 1 FROM break_red_packet WHERE sender_qqid = ? LIMIT 1",
                "SELECT 1 FROM break_red_packet_claim WHERE qqid = ? LIMIT 1",
                "SELECT 1 FROM break_gamble_pool WHERE qqid = ? LIMIT 1",
                "SELECT 1 FROM break_gamble_pool_payout WHERE qqid = ? LIMIT 1",
                """SELECT 1 FROM break_daily_usage
                   WHERE qqid = ? AND (COALESCE(free_used, 0) > 0
                       OR COALESCE(query_count, 0) > 0
                       OR COALESCE(analysis_count, 0) > 0
                       OR COALESCE(break_spent, 0) > 0
                       OR COALESCE(break_gained, 0) > 0) LIMIT 1""",
            )
            removed = 0
            for candidate in candidates:
                qqid = int(candidate['qqid'])
                meaningful = False
                for sql in meaningful_checks:
                    try:
                        if self._conn.execute(sql, (qqid,)).fetchone():
                            meaningful = True
                            break
                    except Exception as exc:
                        # Optional history tables were introduced over time.
                        # A legacy database may not have one yet; that table
                        # cannot contain history, so continue checking the
                        # remaining tables instead of aborting all cleanup.
                        message = str(exc).lower()
                        if 'no such table' in message or "doesn't exist" in message:
                            continue
                        raise
                if meaningful:
                    continue
                self._conn.execute('DELETE FROM break_daily_usage WHERE qqid = ?', (qqid,))
                self._conn.execute('DELETE FROM break_service_daily WHERE qqid = ?', (qqid,))
                self._conn.execute('DELETE FROM break_users WHERE qqid = ?', (qqid,))
                removed += 1
            if removed:
                self._conn.commit()
                log.info(f'[BREAK] 已清理 {removed} 条无业务数据的空用户记录')
        except Exception as exc:
            # A legacy installation may not yet have every optional table;
            # cleanup must never prevent the bot from starting.
            try:
                self._conn._conn.rollback()
            except Exception:
                pass
            log.warning(f'[BREAK] 清理空用户记录失败（已忽略）：{type(exc).__name__}: {exc}')

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
        self._migrate_analysis_pricing_default()
        self._migrate_streak_curve_default()

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

    def _migrate_analysis_pricing_default(self) -> None:
        """将旧版锐评倍率 ×3/×5 迁移为 ×1，预扣额度仍保留 10 BREAK。"""
        migrations = {
            'analysis_price_multiplier': '3',
            'analysis_precharge_cost': '6',
        }
        changed = False
        for key, old_value in migrations.items():
            row = self._conn.execute(
                'SELECT value FROM break_config WHERE key = ?', (key,)
            ).fetchone()
            if row and (
                (key == 'analysis_price_multiplier' and str(row['value']) in {'3', '5'})
                or (key != 'analysis_price_multiplier' and str(row['value']) == old_value)
            ):
                self._conn.execute(
                    'UPDATE break_config SET value = ? WHERE key = ?',
                    (DEFAULT_CONFIG[key], key),
                )
                changed = True
        if changed:
            self._conn.commit()
            log.info(
                '[BREAK] 已将锐评默认计费迁移为倍率 ×1、预扣 10 BREAK'
            )

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

    def _migrate_streak_curve_default(self) -> None:
        """把仍在使用历史默认连签曲线的配置迁移到当前温和曲线，保留管理员自定义值。

        历史默认曲线配套 ``streak_bonus_growth=1``（第 5 天后无限线性增长），
        迁移曲线时一并把该默认增长值复位为 0，从源头消除连签通胀。
        """
        row = self._conn.execute(
            'SELECT value FROM break_config WHERE key = ?', ('streak_bonus',)
        ).fetchone()
        if row and str(row['value']) in LEGACY_STREAK_DEFAULTS:
            self._conn.execute(
                'UPDATE break_config SET value = ? WHERE key = ?',
                (DEFAULT_CONFIG['streak_bonus'], 'streak_bonus'),
            )
            growth_row = self._conn.execute(
                'SELECT value FROM break_config WHERE key = ?',
                ('streak_bonus_growth',),
            ).fetchone()
            if growth_row and str(growth_row['value']) == '1':
                self._conn.execute(
                    'UPDATE break_config SET value = ? WHERE key = ?',
                    (DEFAULT_CONFIG['streak_bonus_growth'], 'streak_bonus_growth'),
                )
            self._conn.commit()
            log.info('[BREAK] 已将历史连签奖励曲线迁移为封顶 3 的温和配置')

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

    def billing_enabled(self) -> bool:
        """BREAK 功能计费总开关；关闭后所有功能扣费与余额拦截一律放行。"""
        raw = str(self.get_config('billing_enabled', '1') or '1').strip().lower()
        return raw not in {'0', 'false', 'no', 'off', '关闭', '停用'}

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
        # 每日重置边界统一为 UTC+8（北京时间）零点，不依赖服务器本地时区。
        return (datetime.now(timezone(timedelta(hours=8))).date()).isoformat()

    def _ensure_daily(self, qqid: int) -> None:
        self._conn.execute(
            """INSERT OR IGNORE INTO break_daily_usage
               (qqid, date, free_used, query_count, analysis_count, break_spent, break_gained)
               VALUES (?, ?, 0, 0, 0, 0, 0)""",
            (qqid, self._today()),
        )

    def get_balance(self, qqid: int) -> int:
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
                   total_query_count = COALESCE(total_query_count, 0) + 1,
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
                   total_analysis_count = COALESCE(total_analysis_count, 0) + 1,
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
        allow_freedom: bool = True,
    ) -> bool:
        if amount <= 0:
            return True
        if not self.billing_enabled():
            return True
        from .maimaidx_card import card_manager
        with self._lock:
            if is_free_window_active():
                self._append_log(
                    qqid, 0, f'free_window_exempt:{reason}',
                    meta={**(meta or {}), 'free_window': True, 'listed_cost': amount},
                )
                self._conn.commit()
                return True
            if allow_freedom and card_manager.freedom_active(qqid):
                self._append_log(
                    qqid, 0, f'freedom_exempt:{reason}',
                    meta={**(meta or {}), 'freedom': True, 'listed_cost': amount},
                )
                self._conn.commit()
                return True
            row = self._conn.execute(
                'SELECT balance FROM break_users WHERE qqid = ?', (qqid,)
            ).fetchone()
            balance = int(row['balance']) if row else 0
            if balance < amount:
                return False
            self._ensure_daily(qqid)
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

    def try_reserve_analysis(self, qqid: int, amount: int, *, meta: Optional[dict] = None) -> bool:
        """原子预扣锐评额度；预扣暂不计入消费统计，等待成功结算或失败退款。"""
        amount = max(0, int(amount))
        if amount <= 0:
            return True
        if not self.billing_enabled():
            return True
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
            self._append_log(qqid, -amount, 'b50_analysis_precharge', meta=meta)
            self._conn.commit()
        return True

    def refund_analysis_reservation(
        self,
        qqid: int,
        reserved: int,
        *,
        meta: Optional[dict] = None,
    ) -> int:
        """锐评未完成时全额退回预扣，不产生消费/收入统计。"""
        reserved = max(0, int(reserved))
        if reserved <= 0:
            return self.get_balance(qqid)
        self._ensure_user(qqid)
        with self._lock:
            try:
                now = time.time()
                self._conn.execute(
                    'UPDATE break_users SET balance = balance + ?, updated_at = ? WHERE qqid = ?',
                    (reserved, now, qqid),
                )
                self._append_log(qqid, reserved, 'b50_analysis_refund', meta=meta)
                self._conn.commit()
                return self.get_balance(qqid)
            except BaseException:
                # 余额与退款流水必须同成同败，避免出现“日志写失败但余额
                # 已加回”或连接继续携带半截事务。
                self._conn.rollback()
                raise

    def settle_analysis_reservation(
        self,
        qqid: int,
        cost: int,
        reserved: int,
        *,
        meta: Optional[dict] = None,
    ) -> int:
        """将预扣结算为实际锐评费用，多退少补并只统计一次真实消费。"""
        cost = max(0, int(cost))
        reserved = max(0, int(reserved))
        self._ensure_user(qqid)
        self._ensure_daily(qqid)
        with self._lock:
            adjustment = reserved - cost
            now = time.time()
            self._conn.execute(
                'UPDATE break_users SET balance = balance + ?, '
                'total_analysis_count = total_analysis_count + 1, '
                'last_analysis_at = ?, updated_at = ? WHERE qqid = ?',
                (adjustment, now, now, qqid),
            )
            self._conn.execute(
                """UPDATE break_daily_usage SET
                   analysis_count = analysis_count + 1,
                   break_spent = break_spent + ?
                   WHERE qqid = ? AND date = ?""",
                (cost, qqid, self._today()),
            )
            detail = dict(meta or {})
            detail.update({'reserved': reserved, 'cost': cost, 'adjustment': adjustment})
            self._append_log(qqid, adjustment, 'b50_analysis_settlement', meta=detail)
            self._conn.commit()
            row = self._conn.execute(
                'SELECT balance FROM break_users WHERE qqid = ?', (qqid,),
            ).fetchone()
            return int(row['balance']) if row else 0

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
        if not self.billing_enabled():
            return
        if is_free_window_active():
            return
        if self.service_is_free(qqid, service):
            return
        from .maimaidx_card import card_manager
        if card_manager.freedom_active(qqid):
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
        if not self.billing_enabled():
            return ServiceChargeResult(
                service, 0, free=False, balance=self.get_balance(qqid),
                listed_cost=cost, billing_disabled=True,
            )
        today, now = self._today(), time.time()
        with self._lock:
            free_window = is_free_window_active()
            free = False
            freedom = False
            freedom_remaining = 0.0
            if not free_window:
                row = self._conn.execute(
                    """SELECT free_used FROM break_service_daily
                       WHERE qqid=? AND date=? AND service=?""",
                    (qqid, today, service),
                ).fetchone()
                free = (
                    service in DAILY_FREE_SERVICES
                    and (not row or int(row['free_used']) == 0)
                )
                if not free:
                    from .maimaidx_card import card_manager
                    f_active, f_remaining, _f_exp = card_manager.freedom_info(qqid, now=now)
                    freedom = bool(f_active)
                    freedom_remaining = f_remaining
            charged = 0 if (free or free_window) else cost
            if freedom:
                charged = 0
            balance = self.get_balance(qqid)
            if charged and balance < charged:
                raise BreakInsufficientError(charged, balance, qqid=qqid)
            self._ensure_user(qqid)
            self._ensure_daily(qqid)
            self._conn.execute(
                """INSERT OR IGNORE INTO break_service_daily
                   (qqid, date, service, success_count, free_used, break_spent, last_at)
                   VALUES (?, ?, ?, 0, 0, 0, ?)""",
                (qqid, today, service, now),
            )
            self._conn.execute(
                """UPDATE break_service_daily SET success_count=success_count+1,
                   free_used=?, break_spent=break_spent+?, last_at=?
                   WHERE qqid=? AND date=? AND service=?""",
                (1 if free else 0, charged, now, qqid, today, service),
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
            if freedom:
                detail['freedom'] = True
            if free_window:
                detail['free_window'] = True
            self._append_log(qqid, -charged, f'service:{service}', meta=detail)
            self._conn.commit()
            balance -= charged
        return ServiceChargeResult(
            service, charged, free, balance,
            listed_cost=cost,
            freedom=freedom, freedom_remaining=freedom_remaining,
            free_window=free_window,
        )

    def transfer(self, sender: int, recipient: int, amount: int) -> TransferResult:
        amount = int(amount)
        if amount <= 0 or sender == recipient:
            raise ValueError('转账数量必须大于 0，且不能转给自己')
        fee = max(0, _parse_config_int(self.get_config('transfer_fee', '0'), 0))
        with self._lock:
            try:
                return self._transfer_locked(sender, recipient, amount, fee)
            except Exception:
                # MySQL(autocommit=False) 下 SQL 链中途异常必须回滚，否则
                # 未提交事务持有 break_users 行锁直到超时，后续转账/发奖
                # 碰到同一用户即报 1205 锁等待；SQLite 同理释放库锁。
                try:
                    self._conn.rollback()
                except Exception:
                    pass
                raise

    def _transfer_locked(
        self, sender: int, recipient: int, amount: int, fee: int
    ) -> TransferResult:
        """transfer 的锁内实现；调用方须已持有 self._lock。"""
        sender_balance = self.get_balance(sender)
        total = amount + fee
        if sender_balance < total:
            raise BreakInsufficientError(total, sender_balance, qqid=sender)
        self._ensure_user(sender)
        self._ensure_user(recipient)
        self._ensure_daily(sender)
        self._ensure_daily(recipient)
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
                    created_date = datetime.fromtimestamp(
                        float(row['created_at']), timezone(timedelta(hours=8))
                    ).date().isoformat()
                    self._conn.execute(
                        """UPDATE break_daily_usage
                            SET break_spent=CASE WHEN break_spent-? < 0 THEN 0 ELSE break_spent-? END
                           WHERE qqid=? AND date=?""",
                        (refund, refund, sender, created_date),
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
            self._ensure_user(sender)
            self._ensure_daily(sender)
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
            self._ensure_user(qqid)
            self._ensure_daily(qqid)
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

    def cancel_red_packet(
        self, qqid: int, group_id: int
    ) -> RedPacketRefundResult:
        """发送者或管理员主动收回本群当前进行中的红包，未领取余额退回发送者。

        约束：
        - 仅对 status='active' 的红包生效；completed/expired 已无可退金额或已退过。
        - 发红包满 90 秒后才允许收回（防止发完立刻反悔）。
        - 触发者必须是红包发送者或插件管理员。
        - 退回逻辑与 expire_red_packets 严格一致：退余额、扣创建当日 break_spent
          （带防负 CASE）、写 red_packet_refund 日志、状态置 expired。
        """
        from .maimaidx_bot_admin import is_plugin_admin

        self.expire_red_packets()
        current = time.time()
        with self._lock:
            packet = self._conn.execute(
                """SELECT * FROM break_red_packet
                   WHERE group_id=? AND status='active'
                   ORDER BY created_at DESC LIMIT 1""",
                (int(group_id),),
            ).fetchone()
            if not packet:
                raise ValueError('本群当前没有进行中的红包')
            packet_id = str(packet['id'])
            sender = int(packet['sender_qqid'])
            # 发红包满 90 秒后才允许收回
            if current - float(packet['created_at']) < 90:
                raise ValueError('红包发送未满 90 秒，暂时无法收回')
            if sender != int(qqid) and not is_plugin_admin(int(qqid)):
                raise ValueError('无权收回此红包')
            refund = int(packet['remaining_amount'])
            try:
                if refund > 0:
                    self._ensure_user(sender)
                    self._conn.execute(
                        'UPDATE break_users SET balance=balance+?, updated_at=? WHERE qqid=?',
                        (refund, current, sender),
                    )
                    created_date = datetime.fromtimestamp(
                        float(packet['created_at']), timezone(timedelta(hours=8))
                    ).date().isoformat()
                    self._conn.execute(
                        """UPDATE break_daily_usage
                            SET break_spent=CASE WHEN break_spent-? < 0 THEN 0 ELSE break_spent-? END
                           WHERE qqid=? AND date=?""",
                        (refund, refund, sender, created_date),
                    )
                    self._append_log(
                        sender,
                        refund,
                        'red_packet_refund',
                        meta={
                            'packet_id': packet_id,
                            'group_id': int(packet['group_id']),
                        },
                    )
                self._conn.execute(
                    """UPDATE break_red_packet
                       SET status='expired', finished_at=? WHERE id=? AND status='active'""",
                    (current, packet_id),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return RedPacketRefundResult(
            packet_id, int(packet['group_id']), sender, refund
        )

    def lottery(self, qqid: int, count: int = 1) -> LotteryResult:
        count = max(1, min(int(count), 10))
        unit_cost = max(1, _parse_config_int(self.get_config('lottery_cost', '2'), 2))
        cost = unit_cost * count
        with self._lock:
            balance = self.get_balance(qqid)
            if balance < cost:
                raise BreakInsufficientError(cost, balance, qqid=qqid)
            self._ensure_user(qqid)
            self._ensure_daily(qqid)
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

    def _get_remaining_distributable(self, today: str) -> int:
        """今日剩余可分配额度 = 总池*50% - 已分配（赢+领福利）。"""
        pool_row = self._conn.execute(
            'SELECT COALESCE(SUM(amount), 0) as total FROM break_gamble_pool WHERE date=?',
            (today,),
        ).fetchone()
        total_pool = int(pool_row['total'])
        distributable = int(total_pool * 0.5)

        paid_row = self._conn.execute(
            'SELECT COALESCE(SUM(amount), 0) as paid FROM break_gamble_pool_payout WHERE date=?',
            (today,),
        ).fetchone()
        total_paid = int(paid_row['paid'])

        return max(0, distributable - total_paid)

    def gamble_all(self, qqid: int, mode: str = '标准') -> GambleAllResult:
        """倾家荡产：梭哈全部 BREAK，按模式概率获得倍率反馈。"""
        if mode not in GAMBLE_WEIGHTS_MAP:
            raise ValueError(f'未知模式：{mode}，可选：{", ".join(GAMBLE_MODES)}')

        with self._lock:
            balance = self.get_balance(qqid)
            if balance <= 0:
                raise BreakInsufficientError(1, balance, qqid=qqid)
            self._ensure_user(qqid)
            self._ensure_daily(qqid)

            # 按权重随机选择倍率
            weights_data = GAMBLE_WEIGHTS_MAP[mode]
            multipliers = [m for m, _ in weights_data]
            weights = [w for _, w in weights_data]
            multiplier = random.choices(multipliers, weights=weights, k=1)[0]

            win_amount = balance * multiplier
            net = win_amount - balance  # 正数=赢，负数=输
            today = self._today()
            now = time.time()

            # 赢钱时从可分配额度扣，不够则封顶
            if net > 0:
                remaining = self._get_remaining_distributable(today)
                if net > remaining:
                    net = remaining
                    win_amount = balance + net
                    multiplier = win_amount / balance if balance > 0 else 0
                # 记录支出
                self._conn.execute(
                    """INSERT INTO break_gamble_pool_payout (qqid, date, amount, payout_type, created_at)
                       VALUES (?, ?, ?, 'gamble_win', ?)""",
                    (qqid, today, net, now),
                )

            self._conn.execute(
                'UPDATE break_users SET balance=balance+?, updated_at=? WHERE qqid=?',
                (net, now, qqid),
            )
            self._conn.execute(
                """UPDATE break_daily_usage SET break_spent=break_spent+?,
                   break_gained=break_gained+? WHERE qqid=? AND date=?""",
                (max(0, -net), max(0, net), qqid, today),
            )
            self._append_log(
                qqid, net, 'gamble_all',
                meta={'mode': mode, 'balance': balance, 'multiplier': multiplier, 'win': win_amount},
            )

            # 记录输掉的 break 到抽奖池（只有输光时才记录）
            if multiplier == 0 and balance > 0:
                self._conn.execute(
                    """INSERT INTO break_gamble_pool (qqid, date, amount, created_at)
                       VALUES (?, ?, ?, ?)""",
                    (qqid, today, balance, now),
                )

            self._conn.commit()

            return GambleAllResult(
                mode=mode,
                balance_before=balance,
                multiplier=multiplier,
                win_amount=win_amount,
                balance_after=balance + net,
            )

    def get_gamble_pool_status(self, date: Optional[str] = None) -> GamblePoolStatus:
        """获取今日抽奖池状态和贡献榜。"""
        if date is None:
            date = self._today()

        with self._lock:
            # 查询今日总贡献
            row = self._conn.execute(
                'SELECT COALESCE(SUM(amount), 0) as total FROM break_gamble_pool WHERE date=?',
                (date,),
            ).fetchone()
            total_pool = int(row['total'])

            # 查询贡献榜前5
            rows = self._conn.execute(
                """SELECT qqid, SUM(amount) as amount FROM break_gamble_pool
                   WHERE date=? GROUP BY qqid ORDER BY amount DESC LIMIT 5""",
                (date,),
            ).fetchall()
            contributors = [
                GamblePoolContributor(qqid=int(r['qqid']), amount=int(r['amount']))
                for r in rows
            ]

            return GamblePoolStatus(
                date=date,
                total_pool=total_pool,
                distributable=self._get_remaining_distributable(date),
                contributors=contributors,
            )

    def get_gamble_pool_leaderboard(self, limit: int = 10) -> list[GamblePoolContributor]:
        """获取历史贡献总榜。"""
        with self._lock:
            rows = self._conn.execute(
                """SELECT qqid, SUM(amount) as amount FROM break_gamble_pool
                   GROUP BY qqid ORDER BY amount DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [
                GamblePoolContributor(qqid=int(r['qqid']), amount=int(r['amount']))
                for r in rows
            ]

    def claim_gamble_pool_reward(self, qqid: int) -> tuple[int, int]:
        """领取今日抽奖池福利。返回 (奖励金额, 当前余额)。"""
        today = self._today()
        now = time.time()

        with self._lock:
            # 检查今日是否有贡献
            row = self._conn.execute(
                'SELECT COALESCE(SUM(amount), 0) as contributed FROM break_gamble_pool WHERE qqid=? AND date=?',
                (qqid, today),
            ).fetchone()
            contributed = int(row['contributed'])

            if contributed <= 0:
                raise ValueError('今日没有贡献，无法领取福利')

            # 检查是否已领取
            reward_key = 'gamble_pool_reward'
            existing = self._conn.execute(
                'SELECT amount FROM break_daily_reward WHERE qqid=? AND date=? AND reward_key=?',
                (qqid, today, reward_key),
            ).fetchone()

            if existing:
                raise ValueError('今日已领取过福利')

            # 计算今日总池
            pool_row = self._conn.execute(
                'SELECT COALESCE(SUM(amount), 0) as total FROM break_gamble_pool WHERE date=?',
                (today,),
            ).fetchone()
            total_pool = int(pool_row['total'])

            if total_pool <= 0:
                raise ValueError('今日奖池为空')

            # 净贡献 = 输掉的 - 赢来的（从 break_log 查今日 gamble_all 赢的金额）
            today_start = datetime.combine(date.today(), datetime.min.time()).timestamp()
            today_end = today_start + 86400
            won_row = self._conn.execute(
                """SELECT COALESCE(SUM(delta), 0) as won FROM break_log
                   WHERE qqid=? AND reason='gamble_all' AND delta > 0
                   AND created_at >= ? AND created_at < ?""",
                (qqid, today_start, today_end),
            ).fetchone()
            today_won = int(won_row['won'])
            net_contribution = max(0, contributed - today_won)

            if net_contribution <= 0:
                raise ValueError('今日净贡献为0，无法领取福利')

            # 计算福利：按净贡献比例分配 50% 的奖池
            distributable = int(total_pool * 0.5)
            user_contribution_ratio = net_contribution / total_pool
            reward = max(1, int(distributable * user_contribution_ratio))

            # 从可分配额度扣，不够则封顶
            remaining = self._get_remaining_distributable(today)
            if remaining <= 0:
                raise ValueError('今日可分配福利已发完')
            reward = min(reward, remaining)

            # 记录支出
            self._conn.execute(
                """INSERT INTO break_gamble_pool_payout (qqid, date, amount, payout_type, created_at)
                   VALUES (?, ?, ?, 'welfare_claim', ?)""",
                (qqid, today, reward, now),
            )

            # 发放福利
            self._conn.execute(
                'UPDATE break_users SET balance=balance+?, updated_at=? WHERE qqid=?',
                (reward, now, qqid),
            )
            self._conn.execute(
                """UPDATE break_daily_usage SET break_gained=break_gained+?
                   WHERE qqid=? AND date=?""",
                (reward, qqid, today),
            )
            self._append_log(
                qqid, reward, 'gamble_pool_reward',
                meta={'contributed': contributed, 'today_won': today_won, 'net_contribution': net_contribution, 'total_pool': total_pool},
            )
            self._conn.execute(
                'INSERT INTO break_daily_reward (qqid, date, reward_key, amount, created_at) VALUES (?, ?, ?, ?, ?)',
                (qqid, today, reward_key, reward, now),
            )
            self._conn.commit()

            balance = self.get_balance(qqid)
            return reward, balance

    def award_guess_points(
        self,
        qqid: int,
        points: int,
        *,
        group_id: Optional[str] = None,
        game: str = '',
    ) -> GuessBreakReward:
        """每次猜对固定发 BREAK；分数仅作排行统计，不放大奖励。

        game 为小游戏 game_key（song/cover/tune/chart/twentyq），用于双层上限统计。
        BREAK 发放统一走 award_game_break（含双倍卡翻倍/豁免）。
        """
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
                """SELECT guess_points FROM break_guess_daily
                   WHERE qqid = ? AND date = ?""",
                (qqid, today),
            ).fetchone()
            daily_points = int(row['guess_points'])
            # BREAK 发放走统一双层上限口（含双倍卡翻倍/豁免）。
            # 此处已持有 self._lock，调用锁内实现避免重入死锁。
            award = self._award_game_break_locked(
                qqid, game or '', reward, 'guess_reward',
                meta={
                    'points_added': points,
                    'daily_points': daily_points,
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
            break_added=award.awarded,
            daily_break=award.awarded,
            daily_cap=0,
            points_per_break=0,
            balance=int(balance_row['balance']) if balance_row else 0,
            doubled=award.doubled,
            double_remaining=award.double_remaining,
            capped=award.capped,
        )

    # ---- 小游戏每日 BREAK 双层上限 ----

    def get_global_game_cap(self) -> int:
        """每日小游戏 BREAK 全局总上限（0 = 不限制）。"""
        return _parse_config_int(self.get_config('guess_daily_break_global_cap', '0'), 0)

    def get_game_cap(self, game: str) -> int:
        """某游戏每日 BREAK 上限（0 = 不限制）。game_key 不存在时返回 0。"""
        caps = self._parse_game_caps()
        return caps.get((game or '').strip(), 0)

    def get_all_game_caps(self) -> Dict[str, int]:
        """全部游戏每日 BREAK 上限（game_key → 上限，0 = 不限制）。"""
        return self._parse_game_caps()

    def get_game_break_daily_status(self, qqid: int) -> tuple[int, Dict[str, int]]:
        """读取指定用户当天小游戏 BREAK 的全局与分游戏发放量。"""
        rows = self._conn.execute(
            'SELECT game, COALESCE(break_awarded, 0) AS break_awarded '
            'FROM break_game_daily WHERE qqid=? AND date=?',
            (int(qqid), self._today()),
        ).fetchall()
        awards: Dict[str, int] = {}
        for row in rows:
            game = str(row['game'] or '').strip()
            if game:
                awards[game] = max(0, int(row['break_awarded'] or 0))
        return sum(awards.values()), awards

    def _parse_game_caps(self) -> Dict[str, int]:
        raw = self.get_config('guess_daily_caps', '') or ''
        caps: Dict[str, int] = {}
        for part in raw.split(','):
            part = part.strip()
            if not part or ':' not in part:
                continue
            key, _, val = part.partition(':')
            caps[key.strip()] = _parse_config_int(val.strip(), 0)
        return caps

    def award_game_break(
        self,
        qqid: int,
        game: str,
        amount: int,
        reason: str,
        *,
        meta: Optional[dict] = None,
    ) -> GameBreakAward:
        """小游戏 BREAK 统一发放口：双层上限（每游戏 + 全局）+ 双倍卡豁免。

        用于 猜Rating / B50找内鬼 / 极限二选一 / 来信 四类结算。
        双倍卡（CARD_TYPE_DOUBLE）生效时先翻倍再全额发放、豁免所有上限；
        FREEDOM 卡与此无关（它只免「触发指令扣费」，不影响赚 BREAK 的上限）。
        否则按 min(amount, 每游戏剩余空间, 全局剩余空间) 发放。
        """
        self._ensure_user(qqid)
        self._ensure_daily(qqid)
        with self._lock:
            try:
                award = self._award_game_break_locked(qqid, game, amount, reason, meta=meta)
                # 显式提交，避免未提交事务长期持有行锁（MySQL）或库锁（SQLite），
                # 或被后续其它操作的 commit 顺带提交。
                self._conn.commit()
                return award
            except Exception:
                # 同 transfer：SQL 链中途异常必须回滚，避免未提交事务
                # 持有 break_users / break_game_daily 行锁直到超时（1205），
                # 或被后续 _ensure_user 的 commit 顺带提交造成半截入账。
                try:
                    self._conn.rollback()
                except Exception:
                    pass
                raise

    def _award_game_break_locked(
        self,
        qqid: int,
        game: str,
        amount: int,
        reason: str,
        *,
        meta: Optional[dict] = None,
    ) -> GameBreakAward:
        """award_game_break 的锁内实现；调用方须已持有 self._lock。"""
        amount = max(0, int(amount))
        game = (game or '').strip()
        today = self._today()
        now = time.time()
        doubled = False
        double_remaining = 0.0
        if amount and game:
            from .maimaidx_card import card_manager
            active, remaining, _exp = card_manager.double_break_info(qqid)
            if active:
                doubled = True
                double_remaining = remaining
                amount *= 2  # 双倍卡：先翻倍，再豁免上限
        if not amount:
            return GameBreakAward(
                game=game, requested=0, awarded=0, capped=False,
                doubled=doubled, double_remaining=double_remaining,
                balance=self.get_balance(qqid),
            )
        if doubled:
            actual = amount
            capped = False
        else:
            global_cap = self.get_global_game_cap()
            per_cap = self.get_game_cap(game)
            if global_cap <= 0 and per_cap <= 0:
                actual = amount
                capped = False
            else:
                row = self._conn.execute(
                    'SELECT COALESCE(SUM(break_awarded),0) AS t '
                    'FROM break_game_daily WHERE qqid=? AND date=?',
                    (qqid, today),
                ).fetchone()
                total_awarded = int(row['t']) if row else 0
                game_awarded = 0
                if per_cap > 0:
                    grow = self._conn.execute(
                        'SELECT COALESCE(break_awarded,0) AS a '
                        'FROM break_game_daily WHERE qqid=? AND date=? AND game=?',
                        (qqid, today, game),
                    ).fetchone()
                    game_awarded = int(grow['a']) if grow else 0
                global_room = amount if global_cap <= 0 else max(0, global_cap - total_awarded)
                game_room = amount if per_cap <= 0 else max(0, per_cap - game_awarded)
                actual = max(0, min(amount, global_room, game_room))
                capped = actual < amount
        if actual <= 0:
            detail = dict(meta or {})
            detail.update({
                'game': game, 'requested': amount, 'capped': True,
                'double_break_card': doubled,
                'global_cap': self.get_global_game_cap(),
                'game_cap': self.get_game_cap(game) if game else 0,
                'hit_cap': True,
            })
            self._append_log(qqid, 0, reason, meta=detail)
            return GameBreakAward(
                game=game, requested=amount, awarded=0, capped=True,
                doubled=doubled, double_remaining=double_remaining,
                balance=self.get_balance(qqid),
            )
        self._conn.execute(
            'UPDATE break_users SET balance=balance+?, updated_at=? WHERE qqid=?',
            (actual, now, qqid),
        )
        self._conn.execute(
            'UPDATE break_daily_usage SET break_gained=break_gained+? WHERE qqid=? AND date=?',
            (actual, qqid, today),
        )
        self._conn.execute(
            'INSERT OR IGNORE INTO break_game_daily '
            '(qqid,date,game,break_awarded,last_at) VALUES (?,?,?,0,?)',
            (qqid, today, game, now),
        )
        self._conn.execute(
            'UPDATE break_game_daily SET break_awarded=break_awarded+?, last_at=? '
            'WHERE qqid=? AND date=? AND game=?',
            (actual, now, qqid, today, game),
        )
        detail = dict(meta or {})
        detail.update({
            'game': game, 'requested': amount, 'capped': capped,
            'double_break_card': doubled,
            'global_cap': self.get_global_game_cap(),
            'game_cap': self.get_game_cap(game) if game else 0,
        })
        self._append_log(qqid, actual, reason, meta=detail)
        return GameBreakAward(
            game=game, requested=amount, awarded=actual, capped=capped,
            doubled=doubled, double_remaining=double_remaining,
            balance=self.get_balance(qqid),
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
        row = self._conn.execute('SELECT * FROM break_users WHERE qqid = ?', (qqid,)).fetchone()
        result = dict(row) if row else {}
        if result:
            # MySQL 启动迁移若因临时 DDL 权限/锁失败，仍从每日统计恢复展示。
            # 这里始终取较大值，也覆盖旧 NULL 在首次新调用后变成 1、但历史
            # 日统计其实更大的情况。
            totals = self._conn.execute(
                """SELECT COALESCE(SUM(query_count), 0) AS total_query_count,
                          COALESCE(SUM(analysis_count), 0) AS total_analysis_count
                   FROM (
                       SELECT MAX(COALESCE(query_count, 0)) AS query_count,
                              MAX(COALESCE(analysis_count, 0)) AS analysis_count
                       FROM break_daily_usage
                       WHERE qqid = ? GROUP BY date
                   ) AS daily_totals""",
                (qqid,),
            ).fetchone()
            totals = dict(totals) if totals else {}
            for key in ('total_query_count', 'total_analysis_count'):
                result[key] = max(
                    int(result.get(key) or 0), int(totals.get(key) or 0)
                )
        return result

    def get_daily_row(self, qqid: int) -> dict:
        row = self._conn.execute(
            """SELECT qqid, date,
                      MAX(COALESCE(free_used, 0)) AS free_used,
                      MAX(COALESCE(query_count, 0)) AS query_count,
                      MAX(COALESCE(analysis_count, 0)) AS analysis_count,
                      MAX(COALESCE(break_spent, 0)) AS break_spent,
                      MAX(COALESCE(break_gained, 0)) AS break_gained
               FROM break_daily_usage WHERE qqid = ? AND date = ?
               GROUP BY qqid, date""",
            (qqid, self._today()),
        ).fetchone()
        return dict(row) if row else {}

    def get_recent_logs(self, qqid: int, limit: int = 20) -> List[BreakLogEntry]:
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

    def get_freedom_savings_total(self, qqid: int) -> int:
        """累计 FREEDOM 历史免单金额；旧版未记录 listed_cost 的流水不计。"""
        rows = self._conn.execute(
            "SELECT meta FROM break_log WHERE qqid = ? AND meta IS NOT NULL",
            (qqid,),
        ).fetchall()
        total = 0
        for row in rows:
            try:
                meta = json.loads(row['meta'] or '{}')
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(meta, dict) or not meta.get('freedom'):
                continue
            try:
                total += max(0, int(meta.get('listed_cost') or 0))
            except (TypeError, ValueError):
                continue
        return total

    def record_freedom_exemption(
        self,
        qqid: int,
        reason: str,
        listed_cost: int,
        *,
        meta: Optional[dict] = None,
    ) -> int:
        """记录一次 FREEDOM 免单并返回包含本次在内的累计节省。"""
        cost = max(0, int(listed_cost))
        with self._lock:
            self._append_log(
                qqid,
                0,
                f'freedom_exempt:{reason}',
                meta={
                    **(meta or {}),
                    'freedom': True,
                    'listed_cost': cost,
                },
            )
            self._conn.commit()
            return self.get_freedom_savings_total(qqid)

    def record_free_window_exemption(
        self,
        qqid: int,
        reason: str,
        listed_cost: int,
        *,
        meta: Optional[dict] = None,
    ) -> None:
        """记录一次限时免费时段免单（delta=0，不扣余额）。"""
        cost = max(0, int(listed_cost))
        with self._lock:
            self._append_log(
                qqid,
                0,
                f'free_window_exempt:{reason}',
                meta={
                    **(meta or {}),
                    'free_window': True,
                    'listed_cost': cost,
                },
            )
            self._conn.commit()

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

    def economy_totals(self, days: int) -> dict:
        """Return cumulative BREAK income and spending for a recent period.

        ``break_daily_usage`` is the canonical aggregate used by the admin
        economy report.  The range is inclusive of today, so ``days=7``
        covers today and the six preceding calendar days.
        """
        days = max(1, int(days))
        today = date.fromisoformat(self._today())
        since = (today - timedelta(days=days - 1)).isoformat()
        row = self._conn.execute(
            """SELECT COALESCE(SUM(break_gained), 0) AS gained,
                      COALESCE(SUM(break_spent), 0) AS spent,
                      COUNT(DISTINCT CASE
                          WHEN break_gained > 0 OR break_spent > 0
                          THEN qqid END) AS active_users
                 FROM break_daily_usage
                WHERE date >= ? AND date <= ?""",
            (since, today.isoformat()),
        ).fetchone()
        return {
            'days': days,
            'since': since,
            'until': today.isoformat(),
            'gained': int(row['gained']) if row else 0,
            'spent': int(row['spent']) if row else 0,
            'active_users': int(row['active_users']) if row else 0,
        }

    def analysis_token_report(self, days: int = 1) -> dict:
        """Aggregate recorded LLM token usage without exposing user IDs."""
        since = time.time() - max(1, int(days)) * 86400
        with self._lock:
            rows = self._conn.execute(
                """SELECT meta FROM break_log
                   WHERE reason='b50_analysis_settlement' AND created_at >= ?
                   ORDER BY id DESC LIMIT 5000""",
                (since,),
            ).fetchall()
        result = {
            'days': max(1, int(days)),
            'calls': 0,
            'usage_available_calls': 0,
            'cache_hit_calls': 0,
            'input_tokens': 0,
            'output_tokens': 0,
            'total_tokens': 0,
            'cached_input_tokens': 0,
        }
        for row in rows:
            try:
                meta = json.loads(row['meta'] or '{}')
            except (TypeError, ValueError):
                meta = {}
            result['calls'] += 1
            if meta.get('available'):
                result['usage_available_calls'] += 1
            try:
                if int(meta.get('cached_input_tokens') or 0) > 0:
                    result['cache_hit_calls'] += 1
            except (TypeError, ValueError):
                pass
            for key in ('input_tokens', 'output_tokens', 'total_tokens', 'cached_input_tokens'):
                try:
                    result[key] += max(0, int(meta.get(key) or 0))
                except (TypeError, ValueError):
                    pass
        input_tokens = int(result['input_tokens'])
        result['cached_input_rate'] = round(
            int(result['cached_input_tokens']) / input_tokens, 4
        ) if input_tokens > 0 else 0.0
        return result

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
        row = self._conn.execute(
            'SELECT last_checkin_date FROM break_users WHERE qqid = ?', (qqid,)
        ).fetchone()
        return bool(row and row['last_checkin_date'] == self._today())

    def _streak_bonus(self, streak: int) -> int:
        raw = self.get_config('streak_bonus', DEFAULT_CONFIG['streak_bonus'])
        parts = [int(x.strip()) for x in raw.split(',') if x.strip().isdigit()]
        if not parts:
            return 0
        growth = max(
            0,
            _parse_config_int(
                self.get_config(
                    'streak_bonus_growth', DEFAULT_CONFIG['streak_bonus_growth']
                ),
                0,
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

    def claim_once_reward(
        self,
        qqid: int,
        reward_key: str,
        amount: int,
        *,
        reason: str,
        meta: Optional[dict] = None,
    ) -> DailyRewardResult:
        """永久幂等奖励；同一用户和 reward_key 只发放一次。"""
        key = str(reward_key).strip()[:64]
        if not key:
            raise ValueError('reward_key 不能为空')
        value = max(0, int(amount))
        self._ensure_user(qqid)
        with self._lock:
            try:
                now = time.time()
                ledger_reason = f'once_reward:{key}'
                # A no-op update locks this user's row until commit on MySQL;
                # together with the process RLock it prevents concurrent OAuth
                # completions from both passing the ledger check.
                self._conn.execute(
                    'UPDATE break_users SET updated_at = updated_at WHERE qqid = ?',
                    (qqid,),
                )
                existing = self._conn.execute(
                    """SELECT delta FROM break_log
                       WHERE qqid = ? AND reason = ? LIMIT 1""",
                    (qqid, ledger_reason),
                ).fetchone()
                if existing:
                    self._conn.commit()
                    return DailyRewardResult(
                        reward_key=key,
                        amount=max(0, int(existing['delta'])),
                        balance=self.get_balance(qqid),
                        awarded=False,
                    )

                self._ensure_daily(qqid)
                self._conn.execute(
                    """UPDATE break_users
                       SET balance = balance + ?, updated_at = ? WHERE qqid = ?""",
                    (value, now, qqid),
                )
                self._conn.execute(
                    """UPDATE break_daily_usage
                       SET break_gained = break_gained + ?
                       WHERE qqid = ? AND date = ?""",
                    (value, qqid, self._today()),
                )
                log_meta = dict(meta or {})
                log_meta.update({'reward_key': key, 'reason': reason})
                self._append_log(qqid, value, ledger_reason, meta=log_meta)
                self._conn.commit()
                return DailyRewardResult(
                    reward_key=key,
                    amount=value,
                    balance=self.get_balance(qqid),
                    awarded=True,
                )
            except Exception:
                self._conn.rollback()
                raise

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
    extra_lines: List[str] = field(default_factory=list)


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


def normalize_billing_qqid(qqid: Optional[int | str]) -> Optional[int]:
    """Normalize a legacy QQ number or official QQ openid for BREAK billing.

    Some legacy handlers still pass ``event.user_id`` directly.  Official QQ
    user IDs are encrypted strings, so never cast them with ``int``.  Prefer a
    qbind/forum mapping; an unmapped official id is rejected instead of being
    converted to a persistent hash key.
    """
    if qqid in (None, ''):
        return None
    raw = str(qqid).strip()
    if raw.isdigit():
        return int(raw)
    try:
        from .maimaidx_qq_bind import qq_bind_db

        mapped = qq_bind_db.get_legacy_qq(raw)
    except Exception:
        mapped = None
    if mapped is not None:
        return int(mapped)
    from .maimaidx_error import QBindRequiredError

    raise QBindRequiredError(raw)


def charge_session_extra(qqid: Optional[int], cost: int, service: str) -> bool:
    """在 break_billing 上下文内额外扣除功能费；成功返回 True。"""
    if not qqid or cost <= 0 or is_superuser_exempt(qqid) or not break_db.billing_enabled():
        return True
    session = _charge_session.get()
    if is_free_window_active():
        break_db.record_usage(qqid, service, break_delta=0)
        break_db.record_free_window_exemption(qqid, service, cost, meta={'kind': service})
        if session:
            session.balance = break_db.get_balance(qqid)
        return True
    if break_db.is_daily_free_available(qqid):
        break_db.mark_daily_free_used(qqid)
        break_db.record_usage(qqid, service, break_delta=0)
        if session:
            session.used_free = True
            session.balance = break_db.get_balance(qqid)
        return True
    if not break_db.try_consume(qqid, cost, service, meta={'kind': service}):
        return False
    break_db.record_usage(qqid, service, break_delta=-cost)
    if session:
        session.spent += cost
        session.balance = break_db.get_balance(qqid)
    return True


def settle_feature_if_uncharged(qqid: Optional[int], service: str = 'search') -> None:
    """本地功能（谱面详情等）成功后：若本会话尚未因 API/缓存扣过，则按 query_cost 结算（享每日首免）。"""
    if not qqid or is_superuser_exempt(qqid) or not break_db.billing_enabled():
        return
    session = _charge_session.get()
    if session and (session.spent > 0 or session.used_free):
        return
    cost = query_cost()
    if cost <= 0:
        return
    if not charge_session_extra(qqid, cost, service):
        raise BreakInsufficientError(cost, break_db.get_balance(qqid), qqid=qqid)


def ensure_image_render_affordable(qqid: Optional[int]) -> None:
    """出图前余额检查：生成图片每次都收费（含读缓存），FREEDOM 可免。"""
    if not qqid or is_superuser_exempt(qqid) or not break_db.billing_enabled():
        return
    cost = image_render_cost()
    if cost <= 0:
        return
    if is_free_window_active():
        return
    from .maimaidx_card import card_manager
    if card_manager.freedom_active(qqid):
        return
    balance = break_db.get_balance(qqid)
    if balance < cost:
        raise BreakInsufficientError(cost, balance, qqid=qqid)


def settle_image_render(qqid: Optional[int]) -> Optional[str]:
    """成功出图后结算生成图片费用；FREEDOM 时返回可展示的 footer。"""
    if not qqid or is_superuser_exempt(qqid) or not break_db.billing_enabled():
        return None
    cost = image_render_cost()
    if cost <= 0:
        return None
    from .maimaidx_card import card_manager
    session = _charge_session.get()
    f_active, f_remaining, _f_exp = card_manager.freedom_info(qqid)
    if f_active:
        break_db.record_usage(qqid, 'image_render', break_delta=0)
        total_saved = break_db.record_freedom_exemption(
            qqid,
            'image_render',
            cost,
            meta={'kind': 'image_render'},
        )
        line = format_freedom_exemption(
            qqid,
            '生成图片',
            cost,
            f_remaining,
            total_saved=total_saved,
        )
        if session:
            session.extra_lines.append(line)
        return line
    if is_free_window_active():
        break_db.record_usage(qqid, 'image_render', break_delta=0)
        break_db.record_free_window_exemption(
            qqid, 'image_render', cost, meta={'kind': 'image_render'},
        )
        line = format_free_window_exemption(qqid, '生成图片', cost)
        if session:
            session.extra_lines.append(line)
        return line
    if not break_db.try_consume(qqid, cost, 'image_render', meta={'kind': 'image_render'}, allow_freedom=False):
        raise BreakInsufficientError(cost, break_db.get_balance(qqid), qqid=qqid)
    break_db.record_usage(qqid, 'image_render', break_delta=-cost)
    if session:
        session.spent += cost
        session.balance = break_db.get_balance(qqid)
    return None


@asynccontextmanager
async def break_billing(qqid: Optional[int]):
    """指令级扣费上下文：查分器/落雪成绩 API 成功后会在此 qq 上结算 BREAK。"""
    # Official QQ events expose an encrypted openid.  A few mature commands
    # still pass ``event.user_id`` directly into this context, so normalize it
    # here as a final boundary: use the forum/qbind QQ when available, and a
    # stable hash only for platform-local BREAK accounting when it is not.
    payer = normalize_billing_qqid(qqid)
    if payer and is_superuser_exempt(payer):
        payer = None
    t1 = _billing_qqid.set(payer)
    # A locked SQLite database may wait up to busy_timeout (5 seconds).  Keep
    # that wait off NoneBot's event loop so other messages continue dispatching.
    balance = await asyncio.to_thread(break_db.get_balance, payer) if payer else 0
    t2 = _charge_session.set(_BreakChargeSession(balance=balance))
    try:
        yield
    finally:
        session = _charge_session.get()
        if session and (session.spent > 0 or session.used_free or session.extra_lines):
            lines: List[str] = []
            if session.used_free and session.spent == 0:
                lines = [f'💳 今日首次查分免费 · 余额 {session.balance} BREAK']
            elif session.spent > 0:
                hint = '（含今日免费）' if session.used_free else ''
                lines = [f'💳 消耗 {session.spent} BREAK{hint} · 余额 {session.balance} BREAK']
            for extra in session.extra_lines:
                lines.append(extra)
            _pending_charge_footer.set(lines)
        _billing_qqid.reset(t1)
        _charge_session.reset(t2)


def take_break_charge_footer() -> List[str]:
    lines = _pending_charge_footer.get() or []
    _pending_charge_footer.set(None)
    return lines


def replace_break_charge_footer(lines: List[str]) -> None:
    """替换本次指令待展示的计费说明，用于合并多项成功扣费。"""
    _pending_charge_footer.set(list(lines) or None)


def format_break_insufficient_message(
    qqid: Optional[int],
    required: int,
    current: int,
) -> str:
    checked = break_db.is_checked_in_today(qqid) if qqid else False
    lines = [f'❌ BREAK 不足（需要 {required}，当前 {current}）']
    if checked:
        lines.append('今日已签到，也可发「今日舞萌」攒 BREAK~')
    else:
        lines.append('发送「签到」或「今日舞萌」获取 BREAK；每日首次查分免费哦~')
    store_url = (getattr(maiconfig, 'maimaidx_store_url', '') or '').strip()
    if store_url:
        lines.append(f'🛒 也可前往卡密商店购买：{store_url}')
    lines.append('来二群（993795066）玩小游戏赚BREAK喵呜~')
    return '\n'.join(lines)


def _config_int(key: str, default: int) -> int:
    try:
        return int(float(break_db.get_config(key, str(default))))
    except (TypeError, ValueError):
        return default


def is_superuser_exempt(qqid: int) -> bool:
    # 管理员不再免费使用功能；唯一的计费豁免来源是 FREEDOM 卡。
    return False


def query_cost() -> int:
    return _config_int('query_cost', 1)


def cache_query_cost() -> int:
    return _config_int('cache_query_cost', 1)


def image_render_cost() -> int:
    """成功出图（含读缓存）的生成图片费用；0 表示关闭。"""
    return _config_int('image_render_cost', 1)


def format_freedom_exemption(
    qqid: int,
    label: str,
    saved: int,
    remaining: float,
    *,
    total_saved: Optional[int] = None,
) -> str:
    """统一 FREEDOM 免单文案：本次金额、剩余时长与累计节省。"""
    from .maimaidx_card import format_duration

    if total_saved is None:
        total_saved = break_db.get_freedom_savings_total(qqid)
    return (
        f'🛡️ {label} FREEDOM 减免了 {max(0, int(saved))} BREAK'
        f'（剩余 {format_duration(max(0.0, float(remaining)))}，'
        f'一共省下了 {max(0, int(total_saved))} BREAK）'
    )


def format_free_window_exemption(qqid: int, label: str, saved: int) -> str:
    """限时免费时段免单文案。"""
    return f'🕐 {label} 限时免费时段减免了 {max(0, int(saved))} BREAK'


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


def is_free_window_active(*, now: Optional[float] = None) -> bool:
    """当前是否处于限时免费时段（UTC+8）。

    配置：break_config 的 free_window_enabled（1=开启）+ free_window_hours
    （如 '17,20' 表示每天 17:00~20:00 全功能免费）。
    任何配置缺失/格式异常均安全降级为「不免费」，绝不抛异常。
    """
    raw_en = str(break_db.get_config('free_window_enabled', '0') or '0').strip().lower()
    if raw_en not in {'1', 'true', 'yes', 'on', '开', '开启'}:
        return False
    raw_hours = str(break_db.get_config('free_window_hours', '') or '').strip()
    if not raw_hours:
        return False
    parts = [p.strip() for p in raw_hours.split(',') if p.strip()]
    if len(parts) != 2:
        return False
    try:
        start = int(parts[0])
        end = int(parts[1])
    except (TypeError, ValueError):
        return False
    if not (0 <= start <= 23 and 1 <= end <= 24):
        return False
    if start >= end:
        return False
    from datetime import timezone
    ts = now if now is not None else time.time()
    dt = datetime.fromtimestamp(ts, tz=timezone(timedelta(hours=8)))
    return start <= dt.hour < end


def analysis_base_cost() -> int:
    return _config_int('analysis_cost', 3)


def analysis_cost() -> int:
    """兼容旧调用：返回 usage 缺失时的兜底价。"""
    return analysis_token_cost(0, 0, usage_available=False)


def analysis_price_multiplier() -> int:
    return max(1, _config_int('analysis_price_multiplier', 1))


def analysis_precharge_cost() -> int:
    return max(0, _config_int('analysis_precharge_cost', 10))


def analysis_token_cost(
    input_tokens: int,
    output_tokens: int,
    *,
    usage_available: bool = True,
) -> int:
    """按模型实际 Token 用量计算基础价，再应用锐评价格倍率。"""
    minimum = max(0, _config_int('analysis_min_cost', 2))
    maximum = max(minimum, _config_int('analysis_max_cost', 20))
    multiplier = analysis_price_multiplier()
    if not usage_available:
        fallback = _config_int('analysis_fallback_cost', 4)
        return min(maximum, max(minimum, fallback)) * multiplier
    input_rate = max(1, _config_int('analysis_input_tokens_per_break', 4000))
    output_rate = max(1, _config_int('analysis_output_tokens_per_break', 1000))
    weighted = max(0, int(input_tokens)) / input_rate
    weighted += max(0, int(output_tokens)) / output_rate
    base_cost = min(maximum, max(minimum, int(math.ceil(weighted))))
    return base_cost * multiplier


def format_analysis_cost_line(
    *,
    charged: Optional[int] = None,
    balance: Optional[int] = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cached_input_tokens: int = 0,
    usage_available: bool = True,
) -> str:
    """向用户展示 Token 用量、实际收费和余额。"""
    cost = charged if charged is not None else analysis_token_cost(
        input_tokens, output_tokens, usage_available=usage_available
    )
    if usage_available:
        detail = f'输入 {max(0, input_tokens):,} / 输出 {max(0, output_tokens):,} Token'
        cached = min(max(0, cached_input_tokens), max(0, input_tokens))
        if cached > 0:
            rate = cached / max(1, input_tokens)
            detail += f'，模型缓存 {cached:,}（{rate:.1%}）'
    else:
        detail = '模型未返回 Token 用量，按兜底价计费'
    text = f'💳 锐评消耗 {cost} BREAK（{detail}）'
    if balance is not None:
        text += f' · 余额 {balance} BREAK'
    input_rate = max(1, _config_int('analysis_input_tokens_per_break', 4000))
    output_rate = max(1, _config_int('analysis_output_tokens_per_break', 1000))
    minimum = max(0, _config_int('analysis_min_cost', 2))
    maximum = max(minimum, _config_int('analysis_max_cost', 20))
    multiplier = analysis_price_multiplier()
    text += (
        f'\n计费规则：输入每 {input_rate:,} Token + 输出每 {output_rate:,} Token '
        f'各计 1 BREAK，基础价合计向上取整后 ×{multiplier}，'
        f'最低 {minimum * multiplier}、最高 {maximum * multiplier}。'
    )
    return text


# 小游戏 game_key → 展示名（顺序即消息中的展示顺序）
GAME_BREAK_CAP_LABELS: Tuple[Tuple[str, str], ...] = (
    ('song', '猜歌'),
    ('cover', '猜曲绘'),
    ('tune', '猜曲子'),
    ('chart', '猜铺面'),
    ('letter', '开字母'),
    ('rating', '猜Rating'),
    ('impostor', 'B50找内鬼'),
    ('duel', '极限二选一'),
    ('twentyq', '你想我猜'),
)


def format_game_break_caps(qqid: Optional[int] = None) -> str:
    """列出小游戏每日 BREAK 上限；传入用户时附带当天发放进度。"""
    global_cap = break_db.get_global_game_cap()
    caps = break_db.get_all_game_caps()
    total_awarded, game_awarded = (
        break_db.get_game_break_daily_status(qqid) if qqid is not None else (0, {})
    )
    lines = ['🎉 小游戏每日 BREAK 上限']
    if global_cap > 0:
        progress = f'{total_awarded}/' if qqid is not None else ''
        lines.append(
            f'· 全局总上限：{progress}{global_cap} BREAK / 天（所有小游戏合计）'
        )
    else:
        lines.append('· 全局总上限：不限制')
    lines.append('')
    lines.append('各游戏单独上限（BREAK / 天）：')
    for key, label in GAME_BREAK_CAP_LABELS:
        cap = caps.get(key, 0)
        if qqid is not None and cap > 0:
            value = f'{int(game_awarded.get(key, 0) or 0)}/{cap}'
        else:
            value = str(cap) if cap > 0 else '不限制'
        lines.append(f'· {label}：{value}')
    lines.append('')
    lines.append('说明：单个游戏达到自身上限后该游戏不再发放；')
    lines.append('全局总上限用满后所有小游戏均不再发放，次日 0 点重置。')
    lines.append('双倍BREAK卡生效期间翻倍发放并豁免上述所有上限。')
    return '\n'.join(lines)


def format_analysis_pricing_help() -> str:
    input_rate = max(1, _config_int('analysis_input_tokens_per_break', 4000))
    output_rate = max(1, _config_int('analysis_output_tokens_per_break', 1000))
    minimum = max(0, _config_int('analysis_min_cost', 2))
    maximum = max(minimum, _config_int('analysis_max_cost', 20))
    multiplier = analysis_price_multiplier()
    fallback_base = min(
        maximum,
        max(minimum, _config_int('analysis_fallback_cost', 4)),
    )
    precharge = analysis_precharge_cost()
    return (
        f'· 分析b50 / 锐评一下 — 按实际 Token 计费：每 {input_rate:,} 输入 Token '
        f'+ 每 {output_rate:,} 输出 Token 各计 1 BREAK，合计向上取整；'
        f'基础价 ×{multiplier}，最低 {minimum * multiplier}、最高 {maximum * multiplier} BREAK；'
        f'usage 缺失时 {fallback_base * multiplier} BREAK。调用前预扣 {precharge} BREAK'
        '（FREEDOM 生效时不预扣），'
        '成功后按实际用量多退少补，失败全额退回\n'
    )


def reserve_analysis_charge(qqid: int) -> AnalysisChargeReservation:
    """调用模型前预扣固定额度；FREEDOM 生效时仅保存免单快照。"""
    if is_superuser_exempt(qqid):
        return AnalysisChargeReservation(0)
    if not break_db.billing_enabled():
        return AnalysisChargeReservation(0)
    if is_free_window_active():
        return AnalysisChargeReservation(0, free_window=True)
    from .maimaidx_card import card_manager

    freedom, remaining, _expires_at = card_manager.freedom_info(qqid)
    if freedom:
        return AnalysisChargeReservation(0, freedom=True, freedom_remaining=remaining)
    reserved = analysis_precharge_cost()
    if not break_db.try_reserve_analysis(
        qqid,
        reserved,
        meta={'kind': 'llm', 'stage': 'precharge'},
    ):
        raise BreakInsufficientError(reserved, break_db.get_balance(qqid), qqid=qqid)
    return AnalysisChargeReservation(reserved)


def refund_analysis_charge(qqid: int, reserved: Any, *, reason: str) -> int:
    """模型或制图失败时退回尚未结算的预扣额度。"""
    amount = max(0, int(getattr(reserved, 'amount', reserved)))
    # 退款必须以预扣事实为准。即使管理员在处理中关闭计费，或者把用户
    # 加入免单名单，已扣走的余额也仍然必须原路退回。
    if amount <= 0:
        return break_db.get_balance(qqid)
    return break_db.refund_analysis_reservation(
        qqid,
        amount,
        meta={'kind': 'llm', 'stage': 'refund', 'reason': str(reason)[:120]},
    )


def ensure_query_affordable(qqid: Optional[int]) -> None:
    """查分器/落雪成绩 API 即将发起前：余额或免费额度检查。"""
    if not qqid or is_superuser_exempt(qqid) or not break_db.billing_enabled():
        return
    if is_free_window_active():
        return
    cost = query_cost()
    balance = break_db.get_balance(qqid)
    if break_db.is_daily_free_available(qqid):
        return
    if balance < cost:
        raise BreakInsufficientError(cost, balance, qqid=qqid)


def settle_prober_fetch(qqid: Optional[int]) -> None:
    """单次查分器/落雪成绩 API 成功后结算（免费额度或扣 BREAK）。"""
    if not qqid or is_superuser_exempt(qqid) or not break_db.billing_enabled():
        return
    session = _charge_session.get()
    cost = query_cost()
    if is_free_window_active():
        break_db.record_usage(qqid, 'query', break_delta=0)
        break_db.record_free_window_exemption(qqid, 'query', cost, meta={'kind': 'prober_api'})
        if session:
            session.balance = break_db.get_balance(qqid)
        return
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


def settle_cache_hit(qqid: Optional[int]) -> None:
    """本地缓存命中后结算：检查免费额度或扣 BREAK。"""
    if not qqid or is_superuser_exempt(qqid) or not break_db.billing_enabled():
        return
    session = _charge_session.get()
    cost = cache_query_cost()
    if cost <= 0:
        return
    if is_free_window_active():
        break_db.record_usage(qqid, 'query', break_delta=0)
        break_db.record_free_window_exemption(qqid, 'query', cost, meta={'kind': 'cache_hit'})
        if session:
            session.balance = break_db.get_balance(qqid)
        return
    if break_db.is_daily_free_available(qqid):
        break_db.mark_daily_free_used(qqid)
        break_db.record_usage(qqid, 'query', break_delta=0)
        if session:
            session.used_free = True
            session.balance = break_db.get_balance(qqid)
        return
    if not break_db.try_consume(qqid, cost, 'query', meta={'kind': 'cache_hit'}):
        log.warning(f'[BREAK] qq={qqid} cache hit consume failed')
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
    if not qqid or is_superuser_exempt(qqid) or not break_db.billing_enabled():
        return
    from .maimaidx_player_cache import peek_fetch_meta

    meta = peek_fetch_meta()
    if meta is None or meta.origin != 'api':
        return
    cost = query_cost()
    if is_free_window_active():
        break_db.record_usage(qqid, 'query', break_delta=0)
        break_db.record_free_window_exemption(qqid, 'query', cost, meta={'kind': 'prober_api'})
        return
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
    reserved: Any = 0,
    token_usage: Optional[dict] = None,
) -> int:
    cost = max(0, int(cost))
    reserved_amount = max(0, int(getattr(reserved, 'amount', reserved)))
    freedom = bool(getattr(reserved, 'freedom', False))
    free_window = bool(getattr(reserved, 'free_window', False))
    usage = dict(token_usage or {})
    if is_superuser_exempt(qqid):
        break_db.record_usage(qqid, 'analysis', break_delta=0)
        return 0
    if not break_db.billing_enabled():
        break_db.record_usage(qqid, 'analysis', break_delta=0)
        return 0
    meta = {
        'kind': 'llm',
        'pricing': 'token_x_multiplier',
        'price_multiplier': analysis_price_multiplier(),
        **usage,
    }
    if freedom:
        break_db.record_usage(qqid, 'analysis', break_delta=0)
        break_db.record_freedom_exemption(
            qqid,
            'b50_analysis',
            cost,
            meta=meta,
        )
        return 0
    if free_window:
        break_db.record_usage(qqid, 'analysis', break_delta=0)
        break_db.record_free_window_exemption(qqid, 'b50_analysis', cost, meta=meta)
        return 0
    if reserved_amount > 0:
        balance = break_db.settle_analysis_reservation(
            qqid,
            cost,
            reserved_amount,
            meta=meta,
        )
    else:
        # 兼容旧调用方：没有预扣记录时仍可直接结算。
        balance = break_db.add_balance(qqid, -cost, 'b50_analysis', meta=meta)
        break_db.record_usage(qqid, 'analysis', break_delta=-cost)
    if balance < 0:
        log.info(
            f'[BREAK] qq={qqid} 锐评多退少补 cost={cost} '
            f'reserved={reserved_amount} balance={balance}'
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

    def _i(row, key, default=0):
        """MySQL 后端的 NULL 列会返回 None，int(None) 会抛 TypeError，统一兜底。"""
        value = row.get(key, default)
        if value is None:
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    return AccountProfile(
        qqid=qqid,
        balance=_i(user, 'balance'),
        streak=_i(user, 'streak'),
        last_checkin_date=user.get('last_checkin_date'),
        checked_in_today=user.get('last_checkin_date') == today,
        today_query_count=_i(daily, 'query_count'),
        today_analysis_count=_i(daily, 'analysis_count'),
        today_break_spent=_i(daily, 'break_spent'),
        today_break_gained=_i(daily, 'break_gained'),
        free_used_today=bool(_i(daily, 'free_used')),
        total_query_count=_i(user, 'total_query_count'),
        total_analysis_count=_i(user, 'total_analysis_count'),
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
        recent_logs=break_db.get_recent_logs(qqid, 20),
    )


def format_account_profile(profile: AccountProfile, *, title: str = '我的 AWMC 账号') -> str:
    return '\n\n'.join(format_account_profile_sections(profile, title=title))


def render_account_profile_image(
    profile: AccountProfile, *, title: str = '我的 AWMC 账号', user_name: str = 'Milk'
):
    """把账号资料渲染成现代化图片 MessageSegment；失败回退 None。"""
    try:
        from nonebot.adapters.onebot.v11 import MessageSegment
        from .maimaidx_awmc_image import render_awmc_profile

        data = profile.model_dump() if hasattr(profile, 'model_dump') else dict(profile)
        bio = render_awmc_profile(data, title=title, user_name=user_name)
        # render_awmc_profile 已返回编码好的 PNG，直接取字节，避免二次 Image.open/save
        # （懒加载 + 重复编码既慢，又可能在并发下触发 "read of closed file"）。
        encoded = base64.b64encode(bio.getvalue()).decode()
        return MessageSegment.image('base64://' + encoded)
    except Exception as exc:  # pragma: no cover - 渲染失败回退文本
        log.warning(f'[BREAK] AWMC 账号图片渲染失败，回退文本：{type(exc).__name__}: {exc}')
        return None


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
        'bind': '账号绑定', 'claim': '账号认领', 'unbind': '账号解绑',
        'status': '账号状态', 'upload': '成绩上传',
        'upload_fish': '上传水鱼', 'upload_lx': '上传落雪',
        'upload_all': '同时上传', 'upload_awmcnet': 'AWMCNET同步',
        'ticket': '发票', 'ticket_status': '票券状态查询',
        'ticket_unused_penalty': '重复发票惩罚',
        'bind_fish': '绑定水鱼', 'bind_lx': '绑定落雪',
        'awmc_preview': '账号预览查询', 'awmc_items': '道具查询',
        'awmc_gate_status': '门状态查询',
        'awmc_music_upsert': '成绩编辑', 'awmc_music_delete': '成绩删除',
        'awmc_item_upsert': '道具修改',
        'auto_qrcode': '自动通信',
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
        recent_lines.append('🧾 最近账号功能记录（最多 5 条）')
        for entry in profile.recent_account_logs[:5]:
            ts = datetime.fromtimestamp(float(entry['created_at'])).strftime('%m-%d %H:%M')
            status = '成功' if entry['status'] == 'success' else '失败'
            label = operation_labels.get(str(entry['operation']), str(entry['operation']))
            recent_lines.append(f'  · {ts}  {label} · {status} · {entry["ref_id"]}')
    if profile.recent_logs:
        recent_lines.append('')
        recent_lines.append('📝 最近 BREAK 记录（最多 20 条）')
        reason_map = {
            'query': '查分',
            'checkin': '签到',
            'checkin_makeup': '补签',
            'checkin_storage_bonus': '签到·数据存储加成',
            'today_luck': '今日舞萌',
            'b50_analysis': '分析b50',
            'b50_analysis_precharge': '分析b50·预扣',
            'b50_analysis_refund': '分析b50·退款',
            'b50_analysis_settlement': '分析b50·结算调整',
            'busy_request_surcharge': '高负载请求附加费',
            'guess_reward': '猜歌奖励',
            'admin_set': '管理员设置',
            'admin_add': '管理员调整',
            'feishu_admin': '人工操作',
            'web_admin': 'Web管理',
            'image_render': '图片渲染',
            'search': '搜索',
            'gamble_all': '倾家荡产',
            'gamble_pool_reward': '抽奖池奖励',
            'lottery': '抽奖',
            'transfer_out': '转账转出',
            'transfer_in': '转账收入',
            'card_redeem': '卡密兑换',
            'rating_guess_settlement': '猜 Rating 结算',
            'b50_impostor_settlement': '抓内鬼结算',
            'duel_all_clear_bonus': '极限二选一结算',
            'letter_settlement': '开字母结算',
            'red_packet_create': '红包创建',
            'red_packet_claim': '红包领取',
            'red_packet_refund': '红包退款',
        }
        service_labels = {
            'upload': '成绩上传', 'ticket': '发票',
            'ticket_status': '票券状态查询',
            'awmc_status': '账号状态查询',
            'awmc_preview': '账号预览查询', 'awmc_items': '道具查询',
            'awmc_gate_status': '门状态查询',
            'awmc_music_upsert': '成绩编辑', 'awmc_music_delete': '成绩删除',
            'awmc_item_upsert': '道具修改',
            'upload_fish': '上传水鱼', 'upload_lx': '上传落雪',
            'upload_all': '同时上传', 'awmcnet_sync': 'AWMCNET同步',
            'coop_b50': '合作B50', 'today_gain_recommend': '今日推荐',
            'weekly_report': '周报', 'monthly_report': '月报',
            'annual_report': '年报', 'daily_report': '日报',
        }
        once_reward_labels = {
            'forum_bind_welcome': '论坛绑定欢迎',
        }
        for entry in profile.recent_logs:
            ts = datetime.fromtimestamp(entry.created_at).strftime('%m-%d %H:%M')
            sign = '+' if entry.delta >= 0 else ''
            reason = entry.reason
            if reason.startswith('free_window_exempt:'):
                base = reason.split(':', 1)[1]
                label = '免费窗口·' + (reason_map.get(base) or service_labels.get(base, base))
            elif reason.startswith('freedom_exempt:'):
                base = reason.split(':', 1)[1]
                label = '免单·' + (reason_map.get(base) or service_labels.get(base, base))
            elif reason.startswith('service:'):
                base = reason.split(':', 1)[1]
                label = service_labels.get(base, base)
            elif reason.startswith('once_reward:'):
                base = reason.split(':', 1)[1]
                label = '一次性奖励·' + once_reward_labels.get(base, base)
            else:
                label = reason_map.get(reason) or service_labels.get(reason, reason)
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
            '（保持开启至明日签到生效）'
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
