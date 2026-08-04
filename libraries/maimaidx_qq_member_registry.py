"""官方 QQ 群：从消息/进退群事件积累见过的 member_openid（无全量拉群成员 API）。"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from threading import Lock
from typing import List, Optional

from ..config import log
from .maimaidx_sqlite import configure_sqlite_connection

DB_DIR = Path(__file__).parent.parent / 'data' / 'qq_member'
DB_FILE = DB_DIR / 'members.db'


class QqMemberRegistry:
    def __init__(self) -> None:
        DB_DIR.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        configure_sqlite_connection(self._conn)
        with self._lock:
            self._conn.executescript(
                '''
                CREATE TABLE IF NOT EXISTS qq_group_member (
                    group_id      TEXT NOT NULL,
                    member_id     TEXT NOT NULL,
                    member_role   TEXT DEFAULT 'member',
                    last_seen     REAL NOT NULL,
                    seen_count    INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY (group_id, member_id)
                );
                '''
            )
            self._conn.commit()

    def touch(self, group_id: str, member_id: str, *, role: str = 'member') -> None:
        gid, mid = str(group_id).strip(), str(member_id).strip()
        if not gid or not mid:
            return
        now = time.time()
        with self._lock:
            self._conn.execute(
                '''
                INSERT INTO qq_group_member (group_id, member_id, member_role, last_seen, seen_count)
                VALUES (?, ?, ?, ?, 1)
                ON CONFLICT(group_id, member_id) DO UPDATE SET
                    member_role = excluded.member_role,
                    last_seen = excluded.last_seen,
                    seen_count = seen_count + 1
                ''',
                (gid, mid, role or 'member', now),
            )
            self._conn.commit()

    def list_group(self, group_id: str, *, limit: int = 50) -> List[dict]:
        gid = str(group_id).strip()
        with self._lock:
            rows = self._conn.execute(
                '''
                SELECT member_id, member_role, last_seen, seen_count
                FROM qq_group_member WHERE group_id = ?
                ORDER BY last_seen DESC LIMIT ?
                ''',
                (gid, max(1, min(limit, 200))),
            ).fetchall()
        return [dict(r) for r in rows]

    def count_group(self, group_id: str) -> int:
        gid = str(group_id).strip()
        with self._lock:
            row = self._conn.execute(
                'SELECT COUNT(*) AS c FROM qq_group_member WHERE group_id = ?', (gid,)
            ).fetchone()
        return int(row['c']) if row else 0


    def list_member_ids(self, group_id: str) -> List[str]:
        gid = str(group_id).strip()
        with self._lock:
            rows = self._conn.execute(
                'SELECT member_id FROM qq_group_member WHERE group_id = ?',
                (gid,),
            ).fetchall()
        return [str(r['member_id']) for r in rows]


qq_member_registry = QqMemberRegistry()


def record_from_event(event) -> None:
    """从群消息事件记录 member_openid（官方 QQ）。"""
    from .maimaidx_platform import is_qq_event, use_qq_mode
    from .maimaidx_bot_admin import _qq_group_role

    if not is_qq_event(event) and not use_qq_mode(event):
        return
    gid = getattr(event, 'group_openid', None) or getattr(event, 'group_id', None)
    if gid is None:
        return
    mid = str(event.get_user_id())
    role = _qq_group_role(event) or 'member'
    try:
        qq_member_registry.touch(str(gid), mid, role=role)
    except Exception as e:
        log.debug(f'[QQMember] touch failed: {e}')
