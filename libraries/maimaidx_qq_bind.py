"""官方 QQ 机器人 openid 与水鱼查分 QQ 号绑定。"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from threading import Lock
from typing import Any, Optional

from ..config import log
from .maimaidx_sqlite import configure_sqlite_connection

DB_DIR = Path(__file__).parent.parent / 'data' / 'qq_bind'
DB_FILE = DB_DIR / 'qq_bind.db'


class QqBindDatabase:
    def __init__(self) -> None:
        DB_DIR.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        configure_sqlite_connection(self._conn)
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                '''
                CREATE TABLE IF NOT EXISTS qq_bind (
                    platform_id   TEXT PRIMARY KEY,
                    legacy_qq     INTEGER NOT NULL,
                    created_at    REAL NOT NULL,
                    updated_at    REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_qq_bind_legacy ON qq_bind(legacy_qq);

                CREATE TABLE IF NOT EXISTS forum_bind (
                    platform_id   TEXT PRIMARY KEY,
                    xf_user_id    TEXT NOT NULL DEFAULT '',
                    username      TEXT NOT NULL DEFAULT '',
                    email         TEXT NOT NULL DEFAULT '',
                    legacy_qq     INTEGER,
                    created_at    REAL NOT NULL,
                    updated_at    REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_forum_bind_xf_user ON forum_bind(xf_user_id);
                CREATE INDEX IF NOT EXISTS idx_forum_bind_qq ON forum_bind(legacy_qq);

                CREATE TABLE IF NOT EXISTS forum_oauth_pending (
                    platform_id   TEXT PRIMARY KEY,
                    state         TEXT NOT NULL,
                    verifier      TEXT NOT NULL,
                    redirect_uri  TEXT NOT NULL,
                    created_at    REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS qq_group_bind (
                    platform_group_id TEXT PRIMARY KEY,
                    legacy_group_id   INTEGER NOT NULL,
                    created_at        REAL NOT NULL,
                    updated_at        REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_qq_group_bind_legacy
                    ON qq_group_bind(legacy_group_id);
                '''
            )
            self._conn.commit()

    def bind(self, platform_id: str, legacy_qq: int) -> None:
        now = time.time()
        pid = str(platform_id).strip()
        with self._lock:
            self._conn.execute(
                '''
                INSERT INTO qq_bind (platform_id, legacy_qq, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(platform_id) DO UPDATE SET
                    legacy_qq = excluded.legacy_qq,
                    updated_at = excluded.updated_at
                ''',
                (pid, int(legacy_qq), now, now),
            )
            self._conn.commit()
        log.info(f'[QBind] platform={pid} -> qq={legacy_qq}')

    def unbind(self, platform_id: str) -> bool:
        pid = str(platform_id).strip()
        with self._lock:
            cur = self._conn.execute(
                'DELETE FROM qq_bind WHERE platform_id = ?', (pid,)
            )
            self._conn.commit()
            return cur.rowcount > 0

    def get_legacy_qq(self, platform_id: str) -> Optional[int]:
        pid = str(platform_id).strip()
        with self._lock:
            row = self._conn.execute(
                'SELECT legacy_qq FROM qq_bind WHERE platform_id = ?', (pid,)
            ).fetchone()
        return int(row['legacy_qq']) if row else None

    def get_platform_id(self, legacy_qq: int) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                'SELECT platform_id FROM qq_bind WHERE legacy_qq = ?', (int(legacy_qq),)
            ).fetchone()
        return str(row['platform_id']) if row else None

    # ---------- 论坛身份 / OAuth 一次性状态 ----------

    def save_forum_pending(
        self,
        platform_id: str,
        *,
        state: str,
        verifier: str,
        redirect_uri: str,
        created_at: Optional[float] = None,
    ) -> None:
        now = float(created_at or time.time())
        with self._lock:
            self._conn.execute(
                '''
                INSERT INTO forum_oauth_pending
                    (platform_id, state, verifier, redirect_uri, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(platform_id) DO UPDATE SET
                    state = excluded.state,
                    verifier = excluded.verifier,
                    redirect_uri = excluded.redirect_uri,
                    created_at = excluded.created_at
                ''',
                (str(platform_id).strip(), state, verifier, redirect_uri, now),
            )
            self._conn.commit()

    def get_forum_pending(self, platform_id: str, *, max_age: float = 600.0) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                'SELECT * FROM forum_oauth_pending WHERE platform_id = ?',
                (str(platform_id).strip(),),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        if max_age > 0 and time.time() - float(result.get('created_at') or 0) > max_age:
            self.clear_forum_pending(platform_id)
            return None
        return result

    def clear_forum_pending(self, platform_id: str) -> None:
        with self._lock:
            self._conn.execute(
                'DELETE FROM forum_oauth_pending WHERE platform_id = ?',
                (str(platform_id).strip(),),
            )
            self._conn.commit()

    def bind_forum(
        self,
        platform_id: str,
        *,
        xf_user_id: str = '',
        username: str = '',
        email: str = '',
        legacy_qq: Optional[int] = None,
    ) -> None:
        now = time.time()
        qq = int(legacy_qq) if legacy_qq is not None else None
        with self._lock:
            self._conn.execute(
                '''
                INSERT INTO forum_bind
                    (platform_id, xf_user_id, username, email, legacy_qq,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform_id) DO UPDATE SET
                    xf_user_id = excluded.xf_user_id,
                    username = excluded.username,
                    email = excluded.email,
                    legacy_qq = COALESCE(excluded.legacy_qq, forum_bind.legacy_qq),
                    updated_at = excluded.updated_at
                ''',
                (str(platform_id).strip(), str(xf_user_id or ''), str(username or ''),
                 str(email or ''), qq, now, now),
            )
            self._conn.commit()

    def get_forum_binding(self, platform_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                'SELECT * FROM forum_bind WHERE platform_id = ?',
                (str(platform_id).strip(),),
            ).fetchone()
        return dict(row) if row else None

    def set_forum_legacy_qq(self, platform_id: str, legacy_qq: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                'UPDATE forum_bind SET legacy_qq = ?, updated_at = ? WHERE platform_id = ?',
                (int(legacy_qq), time.time(), str(platform_id).strip()),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def list_forum_bindings(self, *, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                'SELECT * FROM forum_bind ORDER BY updated_at DESC LIMIT ?',
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [dict(row) for row in rows]

    # ---------- 官方 QQ 群 openid -> 旧 QQ 群号 ----------

    def bind_group(self, platform_group_id: str, legacy_group_id: int) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                '''
                INSERT INTO qq_group_bind
                    (platform_group_id, legacy_group_id, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(platform_group_id) DO UPDATE SET
                    legacy_group_id = excluded.legacy_group_id,
                    updated_at = excluded.updated_at
                ''',
                (str(platform_group_id).strip(), int(legacy_group_id), now, now),
            )
            self._conn.commit()

    def unbind_group(self, platform_group_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                'DELETE FROM qq_group_bind WHERE platform_group_id = ?',
                (str(platform_group_id).strip(),),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def get_group_legacy_id(self, platform_group_id: str) -> Optional[int]:
        with self._lock:
            row = self._conn.execute(
                'SELECT legacy_group_id FROM qq_group_bind WHERE platform_group_id = ?',
                (str(platform_group_id).strip(),),
            ).fetchone()
        return int(row['legacy_group_id']) if row else None

    def list_group_bindings(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                'SELECT * FROM qq_group_bind ORDER BY updated_at DESC LIMIT ?',
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [dict(row) for row in rows]


qq_bind_db = QqBindDatabase()
