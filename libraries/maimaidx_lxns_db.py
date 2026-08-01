"""
落雪查分器用户绑定数据库（独立 sqlite，不与 playcount.db 混用）。

表结构:
  lxns_users
    qqid            INTEGER PRIMARY KEY
    friend_code     INTEGER        -- 落雪好友码
    access_token    TEXT           -- OAuth access_token
    refresh_token   TEXT           -- OAuth refresh_token
    token_type      TEXT DEFAULT 'Bearer'
    expires_at      REAL           -- access_token 过期时间戳（秒）
    scope           TEXT
    source          TEXT DEFAULT 'awmcnet'  -- b50 数据源：awmcnet / divingfish / lxns
    created_at      REAL
    updated_at      REAL
"""

import sqlite3
import time
from pathlib import Path
from threading import Lock
from typing import Optional

from .maimaidx_sqlite import configure_sqlite_connection

DB_DIR = Path(__file__).resolve().parent.parent / 'data' / 'lxns'
DB_PATH = DB_DIR / 'lxns.db'

_CREATE_SQL = """\
CREATE TABLE IF NOT EXISTS lxns_users (
    qqid            INTEGER PRIMARY KEY,
    friend_code     INTEGER,
    access_token    TEXT,
    refresh_token   TEXT,
    token_type      TEXT DEFAULT 'Bearer',
    expires_at      REAL,
    scope           TEXT,
    source          TEXT DEFAULT 'awmcnet',
    theme           TEXT DEFAULT 'default',
    created_at      REAL,
    updated_at      REAL
);
"""

_UPSERT_SQL = """\
INSERT INTO lxns_users (qqid, friend_code, access_token, refresh_token,
                        token_type, expires_at, scope, source, created_at, updated_at)
VALUES (:qqid, :friend_code, :access_token, :refresh_token,
        :token_type, :expires_at, :scope, :source, :now, :now)
ON CONFLICT(qqid) DO UPDATE SET
    friend_code   = COALESCE(excluded.friend_code, lxns_users.friend_code),
    access_token  = COALESCE(excluded.access_token, lxns_users.access_token),
    refresh_token = COALESCE(excluded.refresh_token, lxns_users.refresh_token),
    token_type    = COALESCE(excluded.token_type, lxns_users.token_type),
    expires_at    = COALESCE(excluded.expires_at, lxns_users.expires_at),
    scope         = COALESCE(excluded.scope, lxns_users.scope),
    source        = COALESCE(excluded.source, lxns_users.source),
    updated_at    = excluded.updated_at;
"""


class LxnsDatabase:
    _instance = None
    _lock = Lock()

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
        self._conn.execute(_CREATE_SQL)
        self._conn.commit()
        self._migrate()

    def _migrate(self):
        """处理旧版 DB 缺少 source/theme 列的情况。"""
        cols = {row[1] for row in self._conn.execute('PRAGMA table_info(lxns_users)')}
        if 'source' not in cols:
            self._conn.execute("ALTER TABLE lxns_users ADD COLUMN source TEXT DEFAULT 'awmcnet'")
            self._conn.commit()
        if 'theme' not in cols:
            self._conn.execute("ALTER TABLE lxns_users ADD COLUMN theme TEXT DEFAULT 'default'")
            self._conn.commit()
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS lxns_migrations "
            "(name TEXT PRIMARY KEY, applied_at REAL NOT NULL)"
        )
        migration = 'awmcnet_default_v1'
        applied = self._conn.execute(
            'SELECT 1 FROM lxns_migrations WHERE name = ?', (migration,)
        ).fetchone()
        if not applied:
            # AWMCNET 上线时将既有用户统一迁移到新默认；之后仍可手动切回上游。
            self._conn.execute(
                "UPDATE lxns_users SET source = 'awmcnet', updated_at = ?",
                (time.time(),),
            )
            self._conn.execute(
                'INSERT INTO lxns_migrations (name, applied_at) VALUES (?, ?)',
                (migration, time.time()),
            )
            self._conn.commit()

    def upsert_user(
        self,
        qqid: int,
        *,
        friend_code: Optional[int] = None,
        access_token: Optional[str] = None,
        refresh_token: Optional[str] = None,
        token_type: str = 'Bearer',
        expires_at: Optional[float] = None,
        scope: Optional[str] = None,
        source: Optional[str] = None,
    ):
        now = time.time()
        self._conn.execute(
            _UPSERT_SQL,
            {
                'qqid': qqid,
                'friend_code': friend_code,
                'access_token': access_token,
                'refresh_token': refresh_token,
                'token_type': token_type,
                'expires_at': expires_at,
                'scope': scope,
                'source': source,
                'now': now,
            },
        )
        self._conn.commit()

    def get_user(self, qqid: int) -> Optional[dict]:
        """返回用户行 dict，不存在返回 None。"""
        row = self._conn.execute(
            'SELECT * FROM lxns_users WHERE qqid = ?', (qqid,)
        ).fetchone()
        return dict(row) if row else None

    def get_source(self, qqid: int) -> str:
        """获取用户的 b50 数据源，默认 'awmcnet'。"""
        row = self._conn.execute(
            'SELECT source FROM lxns_users WHERE qqid = ?', (qqid,)
        ).fetchone()
        if row and row['source']:
            return row['source']
        return 'awmcnet'

    def set_source(self, qqid: int, source: str):
        """设置用户的 b50 数据源。如果用户不存在则先创建记录。"""
        existing = self.get_user(qqid)
        if existing:
            self._conn.execute(
                'UPDATE lxns_users SET source = ?, updated_at = ? WHERE qqid = ?',
                (source, time.time(), qqid),
            )
        else:
            self._conn.execute(
                'INSERT INTO lxns_users (qqid, source, created_at, updated_at) VALUES (?, ?, ?, ?)',
                (qqid, source, time.time(), time.time()),
            )
        self._conn.commit()

    def get_theme(self, qqid: int) -> str:
        """获取用户的主题偏好，默认 'default'。"""
        row = self._conn.execute(
            'SELECT theme FROM lxns_users WHERE qqid = ?', (qqid,)
        ).fetchone()
        if row and row['theme']:
            return row['theme']
        return 'default'

    def set_theme(self, qqid: int, theme: str):
        """设置用户的主题偏好。如果用户不存在则先创建记录。"""
        existing = self.get_user(qqid)
        if existing:
            self._conn.execute(
                'UPDATE lxns_users SET theme = ?, updated_at = ? WHERE qqid = ?',
                (theme, time.time(), qqid),
            )
        else:
            self._conn.execute(
                'INSERT INTO lxns_users (qqid, theme, created_at, updated_at) VALUES (?, ?, ?, ?)',
                (qqid, theme, time.time(), time.time()),
            )
        self._conn.commit()

    def update_tokens(
        self,
        qqid: int,
        access_token: str,
        refresh_token: str,
        expires_in: int,
        scope: str,
        token_type: str = 'Bearer',
    ):
        """OAuth 刷新后更新 token 信息。"""
        now = time.time()
        self._conn.execute(
            """\
            UPDATE lxns_users
            SET access_token  = ?,
                refresh_token = ?,
                token_type    = ?,
                expires_at    = ?,
                scope         = ?,
                updated_at    = ?
            WHERE qqid = ?;
            """,
            (access_token, refresh_token, token_type, now + expires_in, scope, now, qqid),
        )
        self._conn.commit()

    def clear_user(self, qqid: int):
        """解绑：删除用户记录。"""
        self._conn.execute('DELETE FROM lxns_users WHERE qqid = ?', (qqid,))
        self._conn.commit()


# 全局单例
lxns_db = LxnsDatabase()
