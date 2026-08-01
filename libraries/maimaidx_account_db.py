"""合并自 maibot 的舞萌账号绑定存储。

QueryBot 只保存调用 AWMC/sw-api 所需的最小状态：二维码、街机 UID、
水鱼 Token、落雪导入 Token及最近一次账号预览。BREAK 仍由
``maimaidx_break`` 独立管理。
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from threading import RLock
from typing import Optional

from .maimaidx_sqlite import configure_sqlite_connection

# 发票失败详情里标记上游返回码：returnCode=0 / "returnCode": null 等。
_RETURN_CODE_ZERO_RE = re.compile(
    r"""returnCode\s*[=:]\s*0\b|"returnCode"\s*:\s*0\b""",
    re.IGNORECASE,
)
_RETURN_CODE_NULL_RE = re.compile(
    r"""returnCode\s*[=:]\s*(?:null|none)\b|"returnCode"\s*:\s*null\b|未返回\s*returnCode""",
    re.IGNORECASE,
)


DB_DIR = Path(__file__).resolve().parent.parent / "data" / "account"
DB_PATH = DB_DIR / "account.db"


@dataclass
class AccountBinding:
    user_key: str
    mai_uid: str = ""
    qrcode: str = ""
    user_name: str = ""
    rating: int = 0
    fish_token: str = ""
    lxns_token: str = ""
    bound_at: float = 0.0
    updated_at: float = 0.0
    last_upload_at: Optional[float] = None
    qrcode_updated_at: float = 0.0
    last_qrcode_success: Optional[int] = None
    preview_json: str = ""
    preview_updated_at: Optional[float] = None

    @property
    def is_bound(self) -> bool:
        return bool(self.qrcode)

    @property
    def preview(self) -> dict:
        if not self.preview_json:
            return {}
        try:
            value = json.loads(self.preview_json)
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}


_SCHEMA = """
CREATE TABLE IF NOT EXISTS account_bindings (
    user_key       TEXT PRIMARY KEY,
    mai_uid        TEXT NOT NULL DEFAULT '',
    qrcode         TEXT NOT NULL DEFAULT '',
    user_name      TEXT NOT NULL DEFAULT '',
    rating         INTEGER NOT NULL DEFAULT 0,
    fish_token     TEXT NOT NULL DEFAULT '',
    lxns_token     TEXT NOT NULL DEFAULT '',
    bound_at       REAL NOT NULL,
    updated_at     REAL NOT NULL,
    last_upload_at REAL,
    qrcode_updated_at REAL NOT NULL DEFAULT 0,
    last_qrcode_success INTEGER,
    preview_json TEXT NOT NULL DEFAULT '',
    preview_updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_account_mai_uid ON account_bindings(mai_uid);

CREATE TABLE IF NOT EXISTS account_operation_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ref_id      TEXT NOT NULL UNIQUE,
    user_key    TEXT NOT NULL,
    operation   TEXT NOT NULL,
    status      TEXT NOT NULL,
    detail      TEXT,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_account_log_user
    ON account_operation_log(user_key, created_at DESC);

CREATE TABLE IF NOT EXISTS awmcnet_first_notice (
    user_key    TEXT PRIMARY KEY,
    notified_at REAL NOT NULL
);
"""


class AccountDatabase:
    def __init__(self, path: Path = DB_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        configure_sqlite_connection(self._conn)
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        columns = {
            str(row[1])
            for row in self._conn.execute("PRAGMA table_info(account_bindings)")
        }
        additions = {
            "qrcode_updated_at": "REAL NOT NULL DEFAULT 0",
            "last_qrcode_success": "INTEGER",
            "preview_json": "TEXT NOT NULL DEFAULT ''",
            "preview_updated_at": "REAL",
        }
        for name, declaration in additions.items():
            if name not in columns:
                self._conn.execute(
                    f"ALTER TABLE account_bindings ADD COLUMN {name} {declaration}"
                )
        self._conn.execute(
            """UPDATE account_bindings
               SET qrcode_updated_at = CASE
                   WHEN bound_at > 0 THEN bound_at ELSE updated_at END
               WHERE qrcode != '' AND qrcode_updated_at = 0"""
        )

    @staticmethod
    def _from_row(row: sqlite3.Row | None) -> Optional[AccountBinding]:
        if row is None:
            return None
        return AccountBinding(**dict(row))

    def get(self, user_key: str) -> Optional[AccountBinding]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM account_bindings WHERE user_key = ?", (str(user_key),)
            ).fetchone()
        return self._from_row(row)

    def bind(
        self,
        user_key: str,
        qrcode: str,
        *,
        mai_uid: str = "",
        user_name: str = "",
        rating: int = 0,
        preview: Optional[dict] = None,
    ) -> AccountBinding:
        now = time.time()
        key = str(user_key)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO account_bindings
                    (user_key, mai_uid, qrcode, user_name, rating, bound_at, updated_at,
                     qrcode_updated_at, last_qrcode_success, preview_json,
                     preview_updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(user_key) DO UPDATE SET
                    mai_uid = excluded.mai_uid,
                    qrcode = excluded.qrcode,
                    user_name = excluded.user_name,
                    rating = excluded.rating,
                    bound_at = excluded.bound_at,
                    updated_at = excluded.updated_at,
                    qrcode_updated_at = excluded.qrcode_updated_at,
                    last_qrcode_success = 1,
                    preview_json = excluded.preview_json,
                    preview_updated_at = excluded.preview_updated_at
                """,
                (
                    key, str(mai_uid), qrcode, user_name, int(rating or 0), now, now,
                    now, json.dumps(preview or {}, ensure_ascii=False), now,
                ),
            )
            self._conn.commit()
        return self.get(key)  # type: ignore[return-value]

    def bind_verified(
        self,
        user_key: str,
        qrcode: str,
        *,
        mai_uid: str,
        user_name: str = "",
        rating: int = 0,
        preview: Optional[dict] = None,
    ) -> tuple[AccountBinding, list[str]]:
        """绑定已验真的街机账号，并认领同一 ``mai_uid`` 的旧记录。

        能读出账号预览的最新 SGWCMAID 被视为本次认领凭证。认领时旧记录
        不再保留二维码，并把其中已有、而当前记录尚未设置的上传 Token 一并
        转给当前用户，避免 Koishi/旧 Bot 迁移后要求用户重新配置。
        """
        now = time.time()
        key = str(user_key)
        uid = str(mai_uid)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                current = self._conn.execute(
                    "SELECT * FROM account_bindings WHERE user_key = ?", (key,)
                ).fetchone()
                previous = self._conn.execute(
                    """SELECT * FROM account_bindings
                       WHERE mai_uid = ? AND mai_uid != '' AND user_key != ?
                       ORDER BY updated_at DESC""",
                    (uid, key),
                ).fetchall()

                fish_token = str(current["fish_token"] or "") if current else ""
                lxns_token = str(current["lxns_token"] or "") if current else ""
                last_upload_at = current["last_upload_at"] if current else None
                for row in previous:
                    fish_token = fish_token or str(row["fish_token"] or "")
                    lxns_token = lxns_token or str(row["lxns_token"] or "")
                    candidate = row["last_upload_at"]
                    if candidate is not None and (
                        last_upload_at is None or candidate > last_upload_at
                    ):
                        last_upload_at = candidate

                claimed_keys = [str(row["user_key"]) for row in previous]
                if claimed_keys:
                    placeholders = ",".join("?" for _ in claimed_keys)
                    self._conn.execute(
                        f"DELETE FROM account_bindings WHERE user_key IN ({placeholders})",
                        claimed_keys,
                    )

                self._conn.execute(
                    """
                    INSERT INTO account_bindings
                        (user_key, mai_uid, qrcode, user_name, rating, fish_token,
                         lxns_token, bound_at, updated_at, last_upload_at,
                         qrcode_updated_at, last_qrcode_success, preview_json,
                         preview_updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    ON CONFLICT(user_key) DO UPDATE SET
                        mai_uid = excluded.mai_uid,
                        qrcode = excluded.qrcode,
                        user_name = excluded.user_name,
                        rating = excluded.rating,
                        fish_token = excluded.fish_token,
                        lxns_token = excluded.lxns_token,
                        bound_at = excluded.bound_at,
                        updated_at = excluded.updated_at,
                        last_upload_at = excluded.last_upload_at,
                        qrcode_updated_at = excluded.qrcode_updated_at,
                        last_qrcode_success = 1,
                        preview_json = excluded.preview_json,
                        preview_updated_at = excluded.preview_updated_at
                    """,
                    (
                        key, uid, qrcode, user_name, int(rating or 0), fish_token,
                        lxns_token, now, now, last_upload_at, now,
                        json.dumps(preview or {}, ensure_ascii=False), now,
                    ),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        binding = self.get(key)
        if binding is None:  # pragma: no cover - transaction guarantees this row
            raise RuntimeError("verified account binding was not persisted")
        return binding, claimed_keys

    def refresh_preview(
        self, user_key: str, *, mai_uid: str, user_name: str, rating: int,
        preview: Optional[dict] = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """UPDATE account_bindings
                   SET mai_uid = ?, user_name = ?, rating = ?, preview_json = ?,
                       preview_updated_at = ?, updated_at = ?
                   WHERE user_key = ?""",
                (
                    str(mai_uid), user_name, int(rating or 0),
                    json.dumps(preview or {}, ensure_ascii=False), time.time(),
                    time.time(), str(user_key),
                ),
            )
            self._conn.commit()

    def save_verified_qrcode(
        self,
        user_key: str,
        qrcode: str,
        *,
        mai_uid: str,
        user_name: str,
        rating: int,
        preview: Optional[dict] = None,
    ) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                """UPDATE account_bindings
                   SET qrcode = ?, qrcode_updated_at = ?, last_qrcode_success = 1,
                       mai_uid = ?, user_name = ?, rating = ?, preview_json = ?,
                       preview_updated_at = ?, updated_at = ?
                   WHERE user_key = ?""",
                (
                    qrcode, now, str(mai_uid), user_name, int(rating or 0),
                    json.dumps(preview or {}, ensure_ascii=False), now, now,
                    str(user_key),
                ),
            )
            self._conn.commit()

    def mark_qrcode_result(self, user_key: str, success: bool) -> None:
        with self._lock:
            self._conn.execute(
                """UPDATE account_bindings SET last_qrcode_success = ?, updated_at = ?
                   WHERE user_key = ?""",
                (1 if success else 0, time.time(), str(user_key)),
            )
            self._conn.commit()

    def unbind_account(self, user_key: str) -> bool:
        """仅清除街机账号，保留水鱼/落雪 Token，方便重新绑定。"""
        with self._lock:
            cur = self._conn.execute(
                """UPDATE account_bindings
                   SET mai_uid = '', qrcode = '', user_name = '', rating = 0,
                       qrcode_updated_at = 0, last_qrcode_success = NULL,
                       preview_json = '', preview_updated_at = NULL,
                       updated_at = ? WHERE user_key = ? AND qrcode != ''""",
                (time.time(), str(user_key)),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def set_token(self, user_key: str, kind: str, token: str) -> None:
        column = {"fish": "fish_token", "lxns": "lxns_token"}.get(kind)
        if not column:
            raise ValueError(f"unknown token kind: {kind}")
        now = time.time()
        with self._lock:
            self._conn.execute(
                """INSERT OR IGNORE INTO account_bindings
                   (user_key, bound_at, updated_at) VALUES (?, ?, ?)""",
                (str(user_key), now, now),
            )
            self._conn.execute(
                f"UPDATE account_bindings SET {column} = ?, updated_at = ? WHERE user_key = ?",
                (token.strip(), now, str(user_key)),
            )
            self._conn.commit()

    def mark_uploaded(self, user_key: str) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "UPDATE account_bindings SET last_upload_at = ?, updated_at = ? WHERE user_key = ?",
                (now, now, str(user_key)),
            )
            self._conn.commit()

    def mark_awmcnet_notified_once(self, user_key: str) -> bool:
        """Atomically remember the first successful AWMC NET sync per QQ."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO awmcnet_first_notice (user_key, notified_at) "
                "VALUES (?, ?)",
                (str(user_key), time.time()),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def list_accounts(
        self, *, limit: int = 100, offset: int = 0, search: str = ""
    ) -> list[AccountBinding]:
        clauses, params = [], []
        if search:
            clauses.append("(user_key LIKE ? OR user_name LIKE ? OR mai_uid LIKE ?)")
            q = f"%{search}%"
            params.extend([q, q, q])
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.extend([max(1, min(limit, 500)), max(0, offset)])
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM account_bindings" + where
                + " ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                params,
            ).fetchall()
        return [self._from_row(row) for row in rows if row is not None]  # type: ignore[misc]

    def count_accounts(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM account_bindings"
            ).fetchone()
        return int(row["c"]) if row else 0

    def append_log(
        self, ref_id: str, user_key: str, operation: str, status: str, detail: str = ""
    ) -> bool:
        """写入幂等操作日志；ref_id 已存在时返回 False。"""
        with self._lock:
            try:
                self._conn.execute(
                    """INSERT INTO account_operation_log
                       (ref_id, user_key, operation, status, detail, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (ref_id, str(user_key), operation, status, detail, time.time()),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def get_usage_stats(self, user_key: str, *, recent_limit: int = 10) -> dict:
        """汇总账号功能的今日/累计调用，并返回最近操作记录。"""
        key = str(user_key)
        today_start = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
        with self._lock:
            summary = self._conn.execute(
                """SELECT
                       COUNT(*) AS total,
                       SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success,
                       SUM(CASE WHEN status != 'success' THEN 1 ELSE 0 END) AS error,
                       SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END) AS today_total,
                       SUM(CASE WHEN created_at >= ? AND status = 'success' THEN 1 ELSE 0 END) AS today_success,
                       SUM(CASE WHEN created_at >= ? AND status != 'success' THEN 1 ELSE 0 END) AS today_error
                   FROM account_operation_log WHERE user_key = ?""",
                (today_start, today_start, today_start, key),
            ).fetchone()
            operations = self._conn.execute(
                """SELECT operation, COUNT(*) AS count
                   FROM account_operation_log
                   WHERE user_key = ?
                   GROUP BY operation ORDER BY count DESC, operation""",
                (key,),
            ).fetchall()
            recent = self._conn.execute(
                """SELECT ref_id, operation, status, detail, created_at
                   FROM account_operation_log WHERE user_key = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (key, max(1, min(int(recent_limit), 50))),
            ).fetchall()
        row = dict(summary) if summary else {}
        return {
            **{name: int(row.get(name) or 0) for name in (
                'total', 'success', 'error', 'today_total', 'today_success', 'today_error'
            )},
            'operations': {str(item['operation']): int(item['count']) for item in operations},
            'recent': [dict(item) for item in recent],
            'ticket': self.get_ticket_stats(user_key=key),
        }

    @staticmethod
    def _ticket_detail_flags(detail: str) -> tuple[bool, bool]:
        text = str(detail or "")
        return (
            bool(_RETURN_CODE_ZERO_RE.search(text)),
            bool(_RETURN_CODE_NULL_RE.search(text)),
        )

    def get_ticket_stats(
        self, *, user_key: Optional[str] = None, days: Optional[int] = None
    ) -> dict:
        """汇总发票成功/失败率，并单独统计 returnCode=0 与 null/未返回。

        判定规则：操作日志 ``operation='ticket'``；详情中一旦出现
        ``returnCode=0`` / ``"returnCode": 0``，或 ``null/None/未返回 returnCode``，
        分别计入对应计数（同一条可同时命中）。
        """
        clauses = ["operation = 'ticket'"]
        params: list = []
        if user_key is not None:
            clauses.append("user_key = ?")
            params.append(str(user_key))
        if days is not None and int(days) > 0:
            clauses.append("created_at >= ?")
            params.append(time.time() - int(days) * 86400)
        where = " AND ".join(clauses)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT status, detail FROM account_operation_log WHERE {where}",
                params,
            ).fetchall()
        total = len(rows)
        return_code_0 = 0
        return_code_null = 0
        error = 0
        for row in rows:
            detail = str(row["detail"] or "")
            status = str(row["status"] or "")
            is_zero, is_null = self._ticket_detail_flags(detail)
            if is_zero:
                return_code_0 += 1
            if is_null:
                return_code_null += 1
            if self.ticket_is_failure(status, detail):
                error += 1
        success = max(0, total - error)
        success_rate = round(100.0 * success / total, 1) if total else 0.0
        error_rate = round(100.0 * error / total, 1) if total else 0.0
        return_code_0_rate = (
            round(100.0 * return_code_0 / total, 1) if total else 0.0
        )
        return {
            "total": total,
            "success": success,
            "error": error,
            "success_rate": success_rate,
            "error_rate": error_rate,
            "return_code_0": return_code_0,
            "return_code_null": return_code_null,
            "return_code_0_rate": return_code_0_rate,
        }

    @staticmethod
    def format_ticket_stats(stats: dict, *, title: str = "发票统计") -> str:
        total = int(stats.get("total") or 0)
        success = int(stats.get("success") or 0)
        error = int(stats.get("error") or 0)
        if total <= 0:
            return f"🎫 {title}\n暂无发票记录"
        return (
            f"🎫 {title}\n"
            f"总次数：{total}\n"
            f"成功：{success}（{stats.get('success_rate', 0)}%）\n"
            f"失败：{error}（{stats.get('error_rate', 0)}%）\n"
            f"returnCode=0：{int(stats.get('return_code_0') or 0)}"
            f"（占全部 {stats.get('return_code_0_rate', 0)}%）\n"
            f"returnCode 为 null/未返回：{int(stats.get('return_code_null') or 0)}"
        )

    @staticmethod
    def _bucket_floor(dt: datetime, minutes: int = 30) -> datetime:
        minutes = max(1, int(minutes))
        discard = dt.minute % minutes
        return dt.replace(minute=dt.minute - discard, second=0, microsecond=0)

    def ticket_is_failure(self, status: str, detail: str = "") -> bool:
        """兼容旧名：同 ``account_op_is_failure``。"""
        return self.account_op_is_failure(status, detail)

    def account_op_is_failure(self, status: str, detail: str = "") -> bool:
        """账号相关操作是否计为失败：非 success，或详情含 returnCode=0 / null。"""
        if str(status or "") != "success":
            return True
        is_zero, is_null = self._ticket_detail_flags(detail)
        return is_zero or is_null

    def get_account_failure_buckets(
        self,
        *,
        hours: int = 48,
        minutes: int = 30,
        now: Optional[datetime] = None,
    ) -> list[tuple[datetime, float, int, int]]:
        """按半小时聚合近 ``hours`` 小时账号操作失败率（全量，无条数上限）。

        统计 ``account_operation_log`` 全部操作（发票 / maiu 上传 / 绑定等）。
        **仅返回有数据的时间桶**（无操作的时段省略）。
        返回 [(bucket_start, failure_rate_pct, fail_count, total), ...]。
        """
        hours = max(1, int(hours))
        minutes = max(1, int(minutes))
        end = self._bucket_floor(now or datetime.now(), minutes)
        start = end - timedelta(hours=hours) + timedelta(minutes=minutes)
        since_ts = start.timestamp()
        with self._lock:
            rows = self._conn.execute(
                """SELECT status, detail, created_at
                   FROM account_operation_log
                   WHERE created_at >= ?
                   ORDER BY created_at ASC""",
                (since_ts,),
            ).fetchall()

        buckets: dict[datetime, list[int]] = defaultdict(lambda: [0, 0])
        for row in rows:
            try:
                created = float(row["created_at"])
            except (TypeError, ValueError, KeyError):
                continue
            dt = datetime.fromtimestamp(created)
            key = self._bucket_floor(dt, minutes)
            if key < start or key > end:
                continue
            buckets[key][1] += 1
            if self.account_op_is_failure(str(row["status"] or ""), str(row["detail"] or "")):
                buckets[key][0] += 1

        result: list[tuple[datetime, float, int, int]] = []
        for key in sorted(buckets):
            fail, total = buckets[key]
            if total <= 0:
                continue
            result.append((key, 100.0 * fail / total, fail, total))
        return result

    def get_ticket_failure_buckets(
        self,
        *,
        hours: int = 48,
        minutes: int = 30,
        now: Optional[datetime] = None,
    ) -> list[tuple[datetime, float, int, int]]:
        """兼容旧名：现为全量账号操作失败率（有数据桶）。"""
        return self.get_account_failure_buckets(hours=hours, minutes=minutes, now=now)


account_db = AccountDatabase()
