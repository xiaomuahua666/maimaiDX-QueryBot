"""卡密系统：BREAK 卡 / 双倍 BREAK 卡 / FREEDOM 卡。

- BREAK 卡：兑换后直接增加对应数量 BREAK。
- 双倍 BREAK 卡：在指定时间内，猜歌系列游戏猜对获得的 BREAK 翻倍。
- FREEDOM 卡：在指定时间内，触发指令不扣除 BREAK。

卡密主表 ``break_card_keys`` 记录每张卡密的类型、面值、状态与兑换者，
可全程追踪；``break_card_log`` 记录发卡 / 兑换 / 作废流水；
``break_user_effects`` 记录用户当前生效中的限时加成。
"""

from __future__ import annotations

import re
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .maimaidx_break import break_db

CARD_TYPE_BREAK = 'break'
CARD_TYPE_DOUBLE = 'double_break'
CARD_TYPE_FREEDOM = 'freedom'

CARD_TYPES = (CARD_TYPE_BREAK, CARD_TYPE_DOUBLE, CARD_TYPE_FREEDOM)

CARD_TYPE_LABELS = {
    CARD_TYPE_BREAK: 'BREAK 卡',
    CARD_TYPE_DOUBLE: '双倍 BREAK 卡',
    CARD_TYPE_FREEDOM: 'FREEDOM 卡',
}

_CODE_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
_CODE_RE = re.compile(r'^[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}$')

_DURATION_RE = re.compile(r'(\d+)\s*(天|小时|时|分|秒|[dhms])?', re.IGNORECASE)
_DURATION_UNIT = {
    '': 1, 's': 1, '秒': 1,
    'm': 60, '分': 60,
    'h': 3600, '时': 3600, '小时': 3600,
    'd': 86400, '天': 86400,
}

_TABLE_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS break_card_keys (
        code            TEXT PRIMARY KEY,
        card_type       TEXT NOT NULL,
        value           INTEGER NOT NULL,
        status          TEXT NOT NULL DEFAULT 'unused',
        batch_id        TEXT NOT NULL,
        note            TEXT NOT NULL DEFAULT '',
        created_by      TEXT NOT NULL DEFAULT '',
        created_at      REAL NOT NULL,
        redeemed_by     INTEGER,
        redeemed_at     REAL,
        redeemed_group  TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS break_card_log (
        id          TEXT PRIMARY KEY,
        code        TEXT NOT NULL,
        action      TEXT NOT NULL,
        actor       TEXT NOT NULL DEFAULT '',
        detail      TEXT NOT NULL DEFAULT '',
        created_at  REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS break_user_effects (
        qqid                INTEGER PRIMARY KEY,
        double_break_until  REAL NOT NULL DEFAULT 0,
        freedom_until       REAL NOT NULL DEFAULT 0,
        updated_at          REAL NOT NULL
    )""",
)

_INDEX_STATEMENTS = (
    "CREATE INDEX idx_break_card_keys_type ON break_card_keys(card_type, status, created_at DESC)",
    "CREATE INDEX idx_break_card_keys_batch ON break_card_keys(batch_id)",
    "CREATE INDEX idx_break_card_keys_redeemed ON break_card_keys(redeemed_by, redeemed_at DESC)",
    "CREATE INDEX idx_break_card_log_code ON break_card_log(code, created_at DESC)",
)


class CardError(Exception):
    """卡密兑换失败。"""


@dataclass
class RedeemResult:
    card_type: str
    label: str
    balance: int = 0
    expires_at: float = 0.0
    granted: int = 0


def parse_duration(text: str) -> int:
    """解析时长文本，返回秒数。

    支持 ``7d`` / ``24h`` / ``30m`` / ``60s`` / ``1天2小时`` 与纯秒数。
    """
    raw = str(text or '').strip().lower()
    if not raw:
        raise ValueError('时长不能为空')
    if raw.isdigit():
        seconds = int(raw)
        if seconds <= 0:
            raise ValueError('时长必须大于 0')
        return seconds
    total = 0
    matched = False
    for number, unit in _DURATION_RE.findall(raw):
        if not number:
            continue
        matched = True
        factor = _DURATION_UNIT.get(unit.lower() if unit else '', 0)
        if not factor:
            raise ValueError(f'无法识别的时间单位：{unit}')
        total += int(number) * factor
    if not matched or total <= 0:
        raise ValueError('时长格式不正确，例如 7d、24h、30m')
    return total


def format_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts: List[str] = []
    if days:
        parts.append(f'{days}天')
    if hours:
        parts.append(f'{hours}小时')
    if minutes:
        parts.append(f'{minutes}分')
    if secs or not parts:
        parts.append(f'{secs}秒')
    return ''.join(parts)


def format_expires(expires_at: float, *, now: Optional[float] = None) -> str:
    remaining = (expires_at or 0) - (now if now is not None else time.time())
    if remaining <= 0:
        return '已过期'
    return format_duration(remaining)


def normalize_code(code: str) -> str:
    """归一化卡密：大写、去空格/分隔符后按 4 位分组。"""
    cleaned = re.sub(r'[^A-Za-z0-9]', '', str(code or '')).upper()
    if len(cleaned) == 12:
        return '-'.join(cleaned[i:i + 4] for i in range(0, 12, 4))
    return cleaned


def _generate_code() -> str:
    return '-'.join(
        ''.join(secrets.choice(_CODE_ALPHABET) for _ in range(4))
        for _ in range(3)
    )


class CardKeyManager:

    def __init__(self) -> None:
        self._conn = break_db._conn
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with break_db._lock:
            if self._conn._backend == 'sqlite':
                self._conn.executescript(';\n'.join(_TABLE_STATEMENTS) + ';')
            else:
                for stmt in _TABLE_STATEMENTS:
                    self._conn.execute(stmt)
            for stmt in _INDEX_STATEMENTS:
                try:
                    self._conn.execute(stmt)
                except Exception:
                    pass
            self._conn.commit()

    def _log(self, code: str, action: str, *, actor: str = '', detail: str = '') -> None:
        self._conn.execute(
            """INSERT INTO break_card_log (id, code, action, actor, detail, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (uuid.uuid4().hex, code, action, str(actor), detail, time.time()),
        )

    @staticmethod
    def _row_to_dict(row: Any) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        return dict(row)

    def generate_codes(self, count: int) -> List[str]:
        codes: List[str] = []
        existing = True
        with break_db._lock:
            for _ in range(count):
                for _retry in range(20):
                    code = _generate_code()
                    row = self._conn.execute(
                        'SELECT 1 FROM break_card_keys WHERE code = ?', (code,)
                    ).fetchone()
                    if not row:
                        codes.append(code)
                        existing = False
                        break
                else:
                    existing = True
                if existing:
                    raise RuntimeError('卡密生成冲突，请重试')
        return codes

    def create_cards(
        self,
        card_type: str,
        value: int,
        quantity: int,
        *,
        created_by: str = '',
        note: str = '',
    ) -> Dict[str, Any]:
        if card_type not in CARD_TYPES:
            raise ValueError('未知卡密类型')
        value = int(value)
        quantity = int(quantity)
        if value <= 0:
            raise ValueError('卡密面值必须大于 0')
        if not 1 <= quantity <= 500:
            raise ValueError('单次发卡数量需在 1-500 之间')
        codes = self.generate_codes(quantity)
        batch_id = 'BATCH-' + uuid.uuid4().hex[:12].upper()
        now = time.time()
        with break_db._lock:
            for code in codes:
                self._conn.execute(
                    """INSERT INTO break_card_keys
                       (code, card_type, value, status, batch_id, note, created_by, created_at)
                       VALUES (?, ?, ?, 'unused', ?, ?, ?, ?)""",
                    (code, card_type, value, batch_id, note, str(created_by), now),
                )
                self._log(
                    code, 'create', actor=str(created_by),
                    detail=f'type={card_type},value={value},batch={batch_id},note={note}',
                )
            self._conn.commit()
        return {
            'batch_id': batch_id,
            'card_type': card_type,
            'label': CARD_TYPE_LABELS[card_type],
            'value': value,
            'quantity': quantity,
            'codes': codes,
        }

    def get_card(self, code: str) -> Optional[Dict[str, Any]]:
        normalized = normalize_code(code)
        with break_db._lock:
            row = self._conn.execute(
                'SELECT * FROM break_card_keys WHERE code = ?', (normalized,)
            ).fetchone()
        return self._row_to_dict(row)

    def _get_effects(self, qqid: int, *, now: float) -> Dict[str, float]:
        row = self._conn.execute(
            """SELECT double_break_until, freedom_until FROM break_user_effects
               WHERE qqid = ?""",
            (qqid,),
        ).fetchone()
        if not row:
            return {'double_break_until': 0.0, 'freedom_until': 0.0}
        return {
            'double_break_until': float(row['double_break_until'] or 0),
            'freedom_until': float(row['freedom_until'] or 0),
        }

    def _extend_effect(self, qqid: int, column: str, seconds: int, *, now: float) -> float:
        self._conn.execute(
            """INSERT INTO break_user_effects (qqid, double_break_until, freedom_until, updated_at)
               VALUES (?, 0, 0, ?)
               ON CONFLICT(qqid) DO NOTHING""",
            (qqid, now),
        ) if self._conn._backend == 'sqlite' else self._conn.execute(
            """INSERT IGNORE INTO break_user_effects (qqid, double_break_until, freedom_until, updated_at)
               VALUES (?, 0, 0, ?)""",
            (qqid, now),
        )
        current = float(
            self._conn.execute(
                f'SELECT {column} AS v FROM break_user_effects WHERE qqid = ?', (qqid,)
            ).fetchone()['v'] or 0
        )
        base = max(now, current)
        expires_at = base + int(seconds)
        self._conn.execute(
            f'UPDATE break_user_effects SET {column} = ?, updated_at = ? WHERE qqid = ?',
            (expires_at, now, qqid),
        )
        return expires_at

    def double_break_active(self, qqid: int, *, now: Optional[float] = None) -> bool:
        return self.double_break_info(qqid, now=now)[0]

    def double_break_info(self, qqid: int, *, now: Optional[float] = None) -> tuple[bool, float, float]:
        ts = now if now is not None else time.time()
        with break_db._lock:
            eff = self._get_effects(qqid, now=ts)
        expires_at = eff['double_break_until']
        active = expires_at > ts
        return active, (expires_at - ts if active else 0.0), expires_at

    def freedom_active(self, qqid: int, *, now: Optional[float] = None) -> bool:
        return self.freedom_info(qqid, now=now)[0]

    def freedom_info(self, qqid: int, *, now: Optional[float] = None) -> tuple[bool, float, float]:
        ts = now if now is not None else time.time()
        with break_db._lock:
            eff = self._get_effects(qqid, now=ts)
        expires_at = eff['freedom_until']
        active = expires_at > ts
        return active, (expires_at - ts if active else 0.0), expires_at

    def redeem(
        self,
        qqid: int,
        code: str,
        *,
        group_id: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> RedeemResult:
        normalized = normalize_code(code)
        if not normalized:
            raise CardError('卡密不能为空')
        now = time.time()
        with break_db._lock:
            row = self._conn.execute(
                'SELECT * FROM break_card_keys WHERE code = ?', (normalized,)
            ).fetchone()
            if not row:
                raise CardError('卡密不存在或已失效')
            card = dict(row)
            if card['status'] == 'redeemed':
                raise CardError('该卡密已被兑换')
            if card['status'] != 'unused':
                raise CardError('该卡密已被作废，无法兑换')

            card_type = card['card_type']
            value = int(card['value'])
            label = CARD_TYPE_LABELS[card_type]
            expires_at = 0.0
            balance = 0
            granted = 0

            self._conn.execute(
                """UPDATE break_card_keys
                   SET status='redeemed', redeemed_by=?, redeemed_at=?, redeemed_group=?
                   WHERE code=? AND status='unused'""",
                (qqid, now, str(group_id or ''), normalized),
            )
            verify = self._conn.execute(
                'SELECT status, redeemed_by FROM break_card_keys WHERE code=?', (normalized,)
            ).fetchone()
            if not verify or dict(verify)['redeemed_by'] != qqid or dict(verify)['status'] != 'redeemed':
                raise CardError('兑换失败，该卡密可能已被他人抢先兑换')

            if card_type == CARD_TYPE_BREAK:
                balance = break_db.add_balance(
                    qqid, value, 'card_redeem',
                    meta={'code': normalized, 'card_type': card_type},
                )
                granted = value
            elif card_type == CARD_TYPE_DOUBLE:
                expires_at = self._extend_effect(qqid, 'double_break_until', value, now=now)
                granted = value
            elif card_type == CARD_TYPE_FREEDOM:
                expires_at = self._extend_effect(qqid, 'freedom_until', value, now=now)
                granted = value

            self._log(
                normalized, 'redeem', actor=str(actor or qqid),
                detail=f'type={card_type},value={value},qqid={qqid},group={group_id or ""}',
            )
            self._conn.commit()
        return RedeemResult(
            card_type=card_type, label=label,
            balance=balance, expires_at=expires_at, granted=granted,
        )

    def disable_card(self, code: str, *, actor: str = '') -> Dict[str, Any]:
        normalized = normalize_code(code)
        with break_db._lock:
            row = self._conn.execute(
                'SELECT * FROM break_card_keys WHERE code = ?', (normalized,)
            ).fetchone()
            if not row:
                raise CardError('卡密不存在')
            card = dict(row)
            if card['status'] == 'redeemed':
                raise CardError('该卡密已被兑换，无法作废')
            if card['status'] == 'disabled':
                return card
            self._conn.execute(
                "UPDATE break_card_keys SET status='disabled' WHERE code=?", (normalized,)
            )
            self._log(normalized, 'disable', actor=str(actor), detail='')
            self._conn.commit()
            card['status'] = 'disabled'
        return card

    def list_cards(
        self,
        *,
        card_type: Optional[str] = None,
        status: Optional[str] = None,
        batch_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        clauses: List[str] = []
        params: List[Any] = []
        if card_type:
            clauses.append('card_type = ?')
            params.append(card_type)
        if status:
            clauses.append('status = ?')
            params.append(status)
        if batch_id:
            clauses.append('batch_id = ?')
            params.append(batch_id)
        where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
        params.append(min(max(int(limit), 1), 500))
        with break_db._lock:
            rows = self._conn.execute(
                f'SELECT * FROM break_card_keys{where} ORDER BY created_at DESC LIMIT ?',
                tuple(params),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_recent_redemptions(self, limit: int = 20) -> List[Dict[str, Any]]:
        with break_db._lock:
            rows = self._conn.execute(
                """SELECT * FROM break_card_keys
                   WHERE status='redeemed'
                   ORDER BY redeemed_at DESC LIMIT ?""",
                (min(max(int(limit), 1), 200),),
            ).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {'total': 0, 'by_type': {}, 'active_effects': 0}
        with break_db._lock:
            rows = self._conn.execute(
                'SELECT card_type, status, COUNT(*) AS c FROM break_card_keys GROUP BY card_type, status'
            ).fetchall()
            for r in rows:
                r = dict(r)
                result['total'] += int(r['c'])
                by_type = result['by_type'].setdefault(
                    r['card_type'], {'unused': 0, 'redeemed': 0, 'disabled': 0}
                )
                by_type[r['status']] = int(r['c'])
            now = time.time()
            active = self._conn.execute(
                """SELECT COUNT(*) AS c FROM break_user_effects
                   WHERE double_break_until > ? OR freedom_until > ?""",
                (now, now),
            ).fetchone()
            if active:
                result['active_effects'] = int(active['c'])
        return result


card_manager = CardKeyManager()


def store_url() -> str:
    """卡密商店链接，可通过环境变量 MAIMAIDX_STORE_URL 配置。"""
    from ..config import maiconfig

    return (getattr(maiconfig, 'maimaidx_store_url', '') or '').strip()


def store_hint() -> str:
    url = store_url()
    return f'🛒 可前往卡密商店购买：{url}' if url else ''
