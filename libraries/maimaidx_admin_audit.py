"""管理审计、REF_ID 请求链路、封禁与群消息统计。

审计库只保存脱敏摘要。二维码、Token、Authorization、Cookie、密码及长密钥
不会写入请求链路。原始响应正文同样不进入数据库。
"""

from __future__ import annotations

import contextvars
import asyncio
import json
import re
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from threading import RLock
from typing import Any, Optional

from .maimaidx_sqlite import configure_sqlite_connection
from .maimaidx_io_executor import run_io


DB_DIR = Path(__file__).resolve().parent.parent / "data" / "admin"
DB_PATH = DB_DIR / "admin.db"

_SECRET_KEYS = {
    "authorization", "cookie", "password", "secret", "client_secret", "token",
    "fish_token", "lxns_token", "access_token", "refresh_token", "qrcode",
    "qr_code", "sgid", "developer-token", "public_gateway_token",
    "mai_uid", "arcade_uid",
}
_SGID_RE = re.compile(r"SGWCMAID[^\s\]\[<>{}\"']+", re.IGNORECASE)
_WAHLAP_QR_RE = re.compile(
    r"https?://wq\.wahlap\.net/qrcode/(?:img|req)/MAID[A-Z0-9]{20,160}"
    r"(?:\.png|\.html)(?:\?[^\s\]\[<>{}\"']*)?",
    re.IGNORECASE,
)
_BARE_MAID_RE = re.compile(r"(?<![A-Z0-9])MAID[A-Z0-9]{20,160}", re.IGNORECASE)
_BEARER_RE = re.compile(r"(?i)Bearer\s+[A-Za-z0-9._~+\-/=]+")
_TOKEN_ASSIGN_RE = re.compile(
    r"(?i)(token|secret|password|qrcode|qr_text|sgid)=([^&\s]+)"
)
_ARCADE_UID_RE = re.compile(
    r'''(?ix)
    (["']?(?:mai_uid|arcade_uid|userId|UserID)["']?\s*[:=]\s*)
    ["']?[0-9]+["']?
    '''
)


def redact(value: Any, *, depth: int = 0) -> Any:
    """递归生成可安全持久化的摘要。"""
    if depth > 5:
        return "[MAX_DEPTH]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            k = str(key)
            low = k.lower().replace("-", "_")
            if low in {s.replace("-", "_") for s in _SECRET_KEYS} or any(
                marker in low for marker in ("token", "secret", "password", "cookie", "qrcode")
            ):
                out[k] = "[REDACTED]"
            else:
                out[k] = redact(item, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        return [redact(item, depth=depth + 1) for item in items[:100]]
    text = str(value)
    text = _WAHLAP_QR_RE.sub("WAHLAP_QR_URL[REDACTED]", text)
    text = _SGID_RE.sub("SGWCMAID[REDACTED]", text)
    text = _BARE_MAID_RE.sub("MAID[REDACTED]", text)
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _TOKEN_ASSIGN_RE.sub(lambda m: f"{m.group(1)}=[REDACTED]", text)
    text = _ARCADE_UID_RE.sub(lambda m: f"{m.group(1)}[REDACTED]", text)
    return text[:4000]


def safe_json(value: Any) -> str:
    return json.dumps(redact(value), ensure_ascii=False, separators=(",", ":"))


_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_traces (
    ref_id          TEXT PRIMARY KEY,
    parent_ref_id   TEXT,
    user_id         TEXT,
    group_id        TEXT,
    command         TEXT NOT NULL,
    matcher         TEXT,
    status          TEXT NOT NULL,
    input_summary   TEXT,
    error_type      TEXT,
    error_message   TEXT,
    started_at      REAL NOT NULL,
    finished_at     REAL,
    duration_ms     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_audit_started ON audit_traces(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_traces(user_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_command ON audit_traces(command, started_at DESC);

CREATE TABLE IF NOT EXISTS audit_steps (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ref_id        TEXT NOT NULL,
    step_name     TEXT NOT NULL,
    status        TEXT NOT NULL,
    detail        TEXT,
    started_at    REAL NOT NULL,
    finished_at   REAL,
    duration_ms   INTEGER,
    FOREIGN KEY(ref_id) REFERENCES audit_traces(ref_id)
);
CREATE INDEX IF NOT EXISTS idx_audit_step_ref ON audit_steps(ref_id, id);

CREATE TABLE IF NOT EXISTS user_bans (
    user_id       TEXT PRIMARY KEY,
    reason        TEXT NOT NULL,
    actor         TEXT NOT NULL,
    created_at    REAL NOT NULL,
    expires_at    REAL,
    active        INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS group_message_daily (
    group_id      TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    date          TEXT NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0,
    last_at       REAL NOT NULL,
    PRIMARY KEY(group_id, user_id, date)
);
CREATE INDEX IF NOT EXISTS idx_message_daily_group
    ON group_message_daily(group_id, date, message_count DESC);

CREATE TABLE IF NOT EXISTS user_agreements (
    user_id       TEXT PRIMARY KEY,
    version       TEXT NOT NULL,
    accepted_at   REAL NOT NULL,
    revoked_at    REAL
);

CREATE TABLE IF NOT EXISTS admin_settings (
    key           TEXT PRIMARY KEY,
    value         TEXT NOT NULL,
    updated_at    REAL NOT NULL
);
"""


_current_ref: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "maimaidx_audit_ref", default=None
)


class AdminAuditDatabase:
    def __init__(self, path: Path = DB_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        configure_sqlite_connection(self._conn)
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    @staticmethod
    def new_ref_id() -> str:
        return "REF-" + uuid.uuid4().hex[:16].upper()

    def current_ref_id(self) -> Optional[str]:
        return _current_ref.get()

    def set_current_ref(self, ref_id: str):
        return _current_ref.set(ref_id)

    def reset_current_ref(self, token) -> None:
        _current_ref.reset(token)

    def start_trace(
        self,
        *,
        command: str,
        user_id: str = "",
        group_id: str = "",
        matcher: str = "",
        input_summary: Any = None,
        parent_ref_id: Optional[str] = None,
        ref_id: Optional[str] = None,
    ) -> str:
        return run_io(
            self._start_trace_sync,
            command=command,
            user_id=user_id,
            group_id=group_id,
            matcher=matcher,
            input_summary=input_summary,
            parent_ref_id=parent_ref_id,
            ref_id=ref_id,
        )

    def _start_trace_sync(
        self,
        *,
        command: str,
        user_id: str = "",
        group_id: str = "",
        matcher: str = "",
        input_summary: Any = None,
        parent_ref_id: Optional[str] = None,
        ref_id: Optional[str] = None,
    ) -> str:
        ref = ref_id or self.new_ref_id()
        now = time.time()
        with self._lock:
            self._conn.execute(
                """INSERT OR IGNORE INTO audit_traces
                   (ref_id, parent_ref_id, user_id, group_id, command, matcher,
                    status, input_summary, started_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?)""",
                (
                    ref, parent_ref_id, str(user_id), str(group_id), command[:200],
                    matcher[:300], safe_json(input_summary) if input_summary is not None else None,
                    now,
                ),
            )
            self._conn.commit()
        return ref

    def finish_trace(
        self,
        ref_id: str,
        status: str,
        *,
        error: Optional[BaseException] = None,
    ) -> None:
        return run_io(
            self._finish_trace_sync, ref_id, status, error=error
        )

    def _finish_trace_sync(
        self,
        ref_id: str,
        status: str,
        *,
        error: Optional[BaseException] = None,
    ) -> None:
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT started_at FROM audit_traces WHERE ref_id = ?", (ref_id,)
            ).fetchone()
            if not row:
                return
            duration = max(0, int((now - float(row["started_at"])) * 1000))
            self._conn.execute(
                """UPDATE audit_traces SET status = ?, error_type = ?, error_message = ?,
                   finished_at = ?, duration_ms = ? WHERE ref_id = ?""",
                (
                    status,
                    type(error).__name__ if error else None,
                    str(redact(str(error)))[:1000] if error else None,
                    now,
                    duration,
                    ref_id,
                ),
            )
            self._conn.commit()

    def add_step(
        self,
        step_name: str,
        status: str,
        detail: Any = None,
        *,
        ref_id: Optional[str] = None,
        started_at: Optional[float] = None,
    ) -> None:
        return run_io(
            self._add_step_sync,
            step_name,
            status,
            detail,
            ref_id=ref_id,
            started_at=started_at,
        )

    def _add_step_sync(
        self,
        step_name: str,
        status: str,
        detail: Any = None,
        *,
        ref_id: Optional[str] = None,
        started_at: Optional[float] = None,
    ) -> None:
        ref = ref_id or self.current_ref_id()
        if not ref:
            return
        end = time.time()
        start = started_at or end
        with self._lock:
            self._conn.execute(
                """INSERT INTO audit_steps
                   (ref_id, step_name, status, detail, started_at, finished_at, duration_ms)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    ref, step_name[:200], status,
                    safe_json(detail) if detail is not None else None,
                    start, end, max(0, int((end - start) * 1000)),
                ),
            )
            self._conn.commit()

    @asynccontextmanager
    async def step(self, step_name: str, detail: Any = None):
        started = time.time()
        try:
            yield
        except Exception as exc:
            await asyncio.to_thread(
                self._add_step_sync,
                step_name,
                "error",
                {"request": detail, "error": str(exc)},
                started_at=started,
            )
            raise
        else:
            await asyncio.to_thread(
                self._add_step_sync,
                step_name,
                "success",
                detail,
                started_at=started,
            )

    def list_traces(
        self, *, limit: int = 100, offset: int = 0, status: str = "", search: str = ""
    ) -> list[dict]:
        clauses, params = [], []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if search:
            clauses.append("(ref_id LIKE ? OR user_id LIKE ? OR command LIKE ?)")
            q = f"%{search}%"
            params.extend([q, q, q])
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.extend([max(1, min(limit, 500)), max(0, offset)])
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM audit_traces" + where + " ORDER BY started_at DESC LIMIT ? OFFSET ?",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def get_trace(self, ref_id: str) -> Optional[dict]:
        return run_io(self._get_trace_sync, ref_id)

    def _get_trace_sync(self, ref_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM audit_traces WHERE ref_id = ?", (ref_id,)
            ).fetchone()
            if not row:
                return None
            steps = self._conn.execute(
                "SELECT * FROM audit_steps WHERE ref_id = ? ORDER BY id", (ref_id,)
            ).fetchall()
        result = dict(row)
        result["steps"] = [dict(item) for item in steps]
        return result

    def command_ranking(self, days: int = 7, limit: int = 30) -> list[dict]:
        since = time.time() - max(1, days) * 86400
        with self._lock:
            rows = self._conn.execute(
                """SELECT command, COUNT(*) AS calls,
                          SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success,
                          SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errors,
                          CAST(AVG(COALESCE(duration_ms, 0)) AS INTEGER) AS avg_ms
                   FROM audit_traces WHERE started_at >= ?
                   GROUP BY command ORDER BY calls DESC LIMIT ?""",
                (since, max(1, min(limit, 200))),
            ).fetchall()
        return [dict(row) for row in rows]

    def api_report(self, days: int = 1, limit: int = 20) -> dict:
        """Summarize AWMC API calls already captured in audit steps."""
        since = time.time() - max(1, int(days)) * 86400
        with self._lock:
            rows = self._conn.execute(
                """SELECT status, detail FROM audit_steps
                   WHERE step_name='http.awmc' AND started_at >= ?
                   ORDER BY id DESC LIMIT 5000""",
                (since,),
            ).fetchall()
        total = success = errors = not_found = 0
        quota_exceeded = quota_observed = 0
        latest_quota: Optional[dict[str, Any]] = None
        paths: dict[str, dict[str, int]] = {}
        for row in rows:
            total += 1
            status = str(row["status"] or "")
            if status == "success":
                success += 1
            else:
                errors += 1
            try:
                detail = json.loads(row["detail"] or "{}")
            except (TypeError, ValueError):
                detail = {}
            if not isinstance(detail, dict):
                detail = {}
            if str(detail.get("error_code") or "").lower() == "quota_exceeded":
                quota_exceeded += 1
            raw_quota = detail.get("quota")
            if isinstance(raw_quota, dict) and raw_quota:
                quota_observed += 1
                if latest_quota is None:
                    latest_quota = {
                        key: raw_quota[key]
                        for key in (
                            "scope", "category", "window", "windowLabel",
                            "used", "limit", "requested", "retryAt", "resetAt",
                            "retryAfterSeconds",
                        )
                        if key in raw_quota
                    }
            path = str(detail.get("path") or "unknown")[:160]
            item = paths.setdefault(path, {"calls": 0, "errors": 0, "404": 0})
            item["calls"] += 1
            if status != "success":
                item["errors"] += 1
            if str(detail.get("status_code")) == "404":
                not_found += 1
                item["404"] += 1
        top_paths = [
            {"path": path, **values}
            for path, values in sorted(
                paths.items(), key=lambda item: item[1]["calls"], reverse=True
            )[: max(1, min(int(limit), 50))]
        ]
        return {
            "days": max(1, int(days)),
            "calls": total,
            "success": success,
            "errors": errors,
            "not_found_404": not_found,
            "quota": {
                "exceeded": quota_exceeded,
                "observed": quota_observed,
                "latest": latest_quota,
            },
            "paths": top_paths,
        }

    def ban_user(
        self, user_id: str, reason: str, actor: str, *, expires_at: Optional[float] = None
    ) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO user_bans
                   (user_id, reason, actor, created_at, expires_at, active)
                   VALUES (?, ?, ?, ?, ?, 1)
                   ON CONFLICT(user_id) DO UPDATE SET reason=excluded.reason,
                   actor=excluded.actor, created_at=excluded.created_at,
                   expires_at=excluded.expires_at, active=1""",
                (str(user_id), str(redact(reason))[:500], str(actor), time.time(), expires_at),
            )
            self._conn.commit()

    def unban_user(self, user_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE user_bans SET active=0 WHERE user_id=? AND active=1", (str(user_id),)
            )
            self._conn.commit()
            return cur.rowcount > 0

    def get_active_ban(self, user_id: str) -> Optional[dict]:
        return run_io(self._get_active_ban_sync, user_id)

    def _get_active_ban_sync(self, user_id: str) -> Optional[dict]:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "UPDATE user_bans SET active=0 WHERE active=1 AND expires_at IS NOT NULL AND expires_at <= ?",
                (now,),
            )
            row = self._conn.execute(
                "SELECT * FROM user_bans WHERE user_id=? AND active=1", (str(user_id),)
            ).fetchone()
            self._conn.commit()
        return dict(row) if row else None

    def list_bans(self, *, active_only: bool = True) -> list[dict]:
        where = " WHERE active=1" if active_only else ""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM user_bans" + where + " ORDER BY created_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def accept_agreement(self, user_id: str, version: str) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO user_agreements (user_id, version, accepted_at, revoked_at)
                   VALUES (?, ?, ?, NULL)
                   ON CONFLICT(user_id) DO UPDATE SET version=excluded.version,
                   accepted_at=excluded.accepted_at, revoked_at=NULL""",
                (str(user_id), version, time.time()),
            )
            self._conn.commit()

    def revoke_agreement(self, user_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE user_agreements SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                (time.time(), str(user_id)),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def has_agreed(self, user_id: str, version: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                """SELECT 1 FROM user_agreements
                   WHERE user_id=? AND version=? AND revoked_at IS NULL""",
                (str(user_id), version),
            ).fetchone()
        return bool(row)

    def get_setting(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM admin_settings WHERE key=?", (str(key),)
            ).fetchone()
        return str(row["value"]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO admin_settings (key, value, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                   updated_at=excluded.updated_at""",
                (str(key), str(value), time.time()),
            )
            self._conn.commit()

    def record_message(self, group_id: str, user_id: str) -> None:
        self.record_messages([(group_id, user_id, 1, time.time())])

    def record_messages(
        self, entries: list[tuple[str, str, int, float]]
    ) -> None:
        """批量落盘群消息计数，避免每条普通聊天都单独 fsync。"""
        return run_io(self._record_messages_sync, entries)

    def _record_messages_sync(
        self, entries: list[tuple[str, str, int, float]]
    ) -> None:
        if not entries:
            return
        day = time.strftime("%Y-%m-%d", time.localtime())
        rows = [
            (str(group_id), str(user_id), day, max(1, int(count)), float(last_at))
            for group_id, user_id, count, last_at in entries
        ]
        with self._lock:
            self._conn.executemany(
                """INSERT INTO group_message_daily
                   (group_id, user_id, date, message_count, last_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(group_id, user_id, date) DO UPDATE SET
                       message_count=message_count+excluded.message_count,
                       last_at=MAX(last_at, excluded.last_at)""",
                rows,
            )
            self._conn.commit()

    def message_ranking(
        self, *, group_id: str = "", days: int = 7, limit: int = 50
    ) -> list[dict]:
        from datetime import date, timedelta

        since = (date.today() - timedelta(days=max(1, days) - 1)).isoformat()
        clauses = ["date >= ?"]
        params: list[Any] = [since]
        if group_id:
            clauses.append("group_id = ?")
            params.append(str(group_id))
        params.append(max(1, min(limit, 500)))
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT group_id, user_id, SUM(message_count) AS messages,
                           MAX(last_at) AS last_at
                    FROM group_message_daily WHERE {' AND '.join(clauses)}
                    GROUP BY group_id, user_id ORDER BY messages DESC LIMIT ?""",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def list_groups(self, days: int = 30) -> list[dict]:
        from datetime import date, timedelta

        since = (date.today() - timedelta(days=max(1, days) - 1)).isoformat()
        with self._lock:
            rows = self._conn.execute(
                """SELECT group_id, SUM(message_count) AS messages,
                          COUNT(DISTINCT user_id) AS active_users, MAX(last_at) AS last_at
                   FROM group_message_daily WHERE date >= ?
                   GROUP BY group_id ORDER BY messages DESC""",
                (since,),
            ).fetchall()
        return [dict(row) for row in rows]

    def cleanup(self, retention_days: int = 90) -> dict[str, int]:
        """清理过期审计链路和消息日统计；封禁记录不会自动删除。"""
        from datetime import date, timedelta

        days = max(1, int(retention_days))
        trace_before = time.time() - days * 86400
        message_before = (date.today() - timedelta(days=days)).isoformat()
        with self._lock:
            step_cur = self._conn.execute(
                """DELETE FROM audit_steps WHERE ref_id IN
                   (SELECT ref_id FROM audit_traces WHERE started_at < ?)""",
                (trace_before,),
            )
            trace_cur = self._conn.execute(
                "DELETE FROM audit_traces WHERE started_at < ?", (trace_before,)
            )
            message_cur = self._conn.execute(
                "DELETE FROM group_message_daily WHERE date < ?", (message_before,)
            )
            self._conn.commit()
        return {
            "steps": max(0, step_cur.rowcount),
            "traces": max(0, trace_cur.rowcount),
            "messages": max(0, message_cur.rowcount),
        }

    def summary(self) -> dict:
        since = time.time() - 86400
        with self._lock:
            traces = self._conn.execute(
                "SELECT COUNT(*) c FROM audit_traces WHERE started_at >= ?", (since,)
            ).fetchone()["c"]
            errors = self._conn.execute(
                "SELECT COUNT(*) c FROM audit_traces WHERE started_at >= ? AND status='error'", (since,)
            ).fetchone()["c"]
            bans = self._conn.execute(
                "SELECT COUNT(*) c FROM user_bans WHERE active=1"
            ).fetchone()["c"]
            groups = self._conn.execute(
                "SELECT COUNT(DISTINCT group_id) c FROM group_message_daily"
            ).fetchone()["c"]
        return {"traces_24h": traces, "errors_24h": errors, "active_bans": bans, "groups": groups}


admin_audit = AdminAuditDatabase()
