"""
统一数据库连接层：SQLite / MySQL 兼容。

UnifiedCursor 自动将 SQLite 风格 SQL 转换为 MySQL 兼容格式：
- ? → %s
- INSERT OR IGNORE → INSERT IGNORE
- ON CONFLICT DO UPDATE → ON DUPLICATE KEY UPDATE
- 表名自动加前缀

上层代码无需任何修改。
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Optional

from ..config import maiconfig
from .maimaidx_sqlite import configure_sqlite_connection

# 需要加前缀的表名（不含前缀的原始名）
_TABLE_NAMES = (
    'break_users', 'break_daily_usage', 'break_group_checkin',
    'break_config', 'break_log', 'break_guess_daily',
    'break_service_daily', 'break_daily_reward', 'break_red_packet',
    'break_red_packet_claim', 'break_makeup_checkin', 'break_gamble_pool',
    'break_gamble_pool_payout',
    'break_card_keys', 'break_card_log', 'break_user_effects',
    'account_bindings', 'account_operation_log',
    'play_count_records', 'user_credentials', 'user_prober_tokens',
    'qq_bind', 'lxns_users',
)


class UnifiedCursor:
    """兼容 sqlite3.Row 行为的游标，自动转换 SQL 方言。"""

    def __init__(self, raw_cursor, backend: str, prefix: str):
        self._cur = raw_cursor
        self._backend = backend
        self._prefix = prefix

    def _convert(self, sql: str) -> str:
        if self._backend != 'mysql':
            return sql
        # CAST(x AS TEXT) → CAST(x AS CHAR)  (MySQL 不支持 TEXT 作为 CAST 目标类型)
        sql = re.sub(
            r'CAST\(([^)]+)\s+AS\s+TEXT\)', r'CAST(\1 AS CHAR)', sql, flags=re.IGNORECASE,
        )
        # INSERT OR IGNORE → INSERT IGNORE
        sql = sql.replace('INSERT OR IGNORE INTO', 'INSERT IGNORE INTO')
        # ON CONFLICT(col) DO UPDATE SET x = excluded.x → ON DUPLICATE KEY UPDATE x = VALUES(x)
        sql = re.sub(
            r'ON CONFLICT\([^)]+\)\s+DO UPDATE SET\s+(\w+)\s*=\s*excluded\.(\w+)',
            r'ON DUPLICATE KEY UPDATE \1 = VALUES(\2)',
            sql,
        )
        # 表名加前缀（仅匹配独立词，避免子串误匹配）
        if self._prefix:
            for t in _TABLE_NAMES:
                sql = re.sub(rf'\b{t}\b', f'{self._prefix}{t}', sql)
        # `key` 列是 MySQL 保留字，需要加反引号（仅匹配小写 key，避免误匹配 DUPLICATE KEY）
        sql = re.sub(r'(?<!\w)key(?=[,\s)=])', '`key`', sql)
        # SQLite date(<col>, 'unixepoch', 'localtime') → MySQL DATE(FROM_UNIXTIME(<col>))
        # 同时兼容 datetime() 变体
        sql = re.sub(
            r"\bdate\(\s*([^,)]+?)\s*,\s*'unixepoch'(?:\s*,\s*'localtime')?\s*\)",
            r"DATE(FROM_UNIXTIME(\1))",
            sql,
            flags=re.IGNORECASE,
        )
        sql = re.sub(
            r"\bdatetime\(\s*([^,)]+?)\s*,\s*'unixepoch'(?:\s*,\s*'localtime')?\s*\)",
            r"FROM_UNIXTIME(\1)",
            sql,
            flags=re.IGNORECASE,
        )
        # ? → %s
        sql = sql.replace('?', '%s')
        return sql

    def execute(self, sql: str, params: tuple = ()):
        self._cur.execute(self._convert(sql), params)
        return self

    def executemany(self, sql: str, params_list):
        self._cur.executemany(self._convert(sql), params_list)
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def close(self):
        self._cur.close()


class _BufferedCursor:
    """在锁内一次性拉完结果的游标，避免多线程共享 MySQL 连接时串结果。

    pymysql 的同一连接上多个游标并发 execute/fetch 会互相覆盖未读结果，
    偶发返回缺列的字典（如 KeyError: 'value'）。这里在持锁期间把所有行
    缓冲到内存，调用方随后的 fetchone/fetchall 不再触碰底层连接。
    """

    def __init__(self, rows, lastrowid, rowcount):
        self._rows = list(rows or [])
        self._idx = 0
        self.lastrowid = lastrowid
        self.rowcount = rowcount

    def fetchone(self):
        if self._idx >= len(self._rows):
            return None
        row = self._rows[self._idx]
        self._idx += 1
        return row

    def fetchall(self):
        rows = self._rows[self._idx:]
        self._idx = len(self._rows)
        return rows

    def close(self):
        self._rows = []

    def __iter__(self):
        return self

    def __next__(self):
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row


def _is_connection_error(exc: Exception) -> bool:
    """判断是否为连接断开类错误。"""
    try:
        import pymysql.err
        if isinstance(exc, pymysql.err.InterfaceError):
            return True
        if isinstance(exc, pymysql.err.OperationalError):
            # 1205/1213 是锁等待/死锁，连接仍可用，绝不能通过重连后
            # 自动重放写语句；否则可能重复扣款。
            code = exc.args[0] if exc.args else None
            return code in {2006, 2013, 2014, 2045, 2055}
    except ImportError:
        pass
    return False


def _read_only_sql(sql: str) -> bool:
    head = str(sql or '').lstrip().split(None, 1)[0].upper()
    return head in {'SELECT', 'SHOW', 'DESCRIBE', 'EXPLAIN'}


class UnifiedConnection:
    """兼容 sqlite3.Connection 的统一连接，透明支持 SQLite 和 MySQL。"""

    def __init__(self, backend: str = 'sqlite', prefix: str = '', **kwargs):
        self._backend = backend
        self._prefix = prefix
        self._kwargs = kwargs
        self._thread_state = threading.local()
        self._sqlite_conn = None
        self._last_active = 0.0
        # SQLite 仍共享一个本地连接；MySQL 则每个工作线程独享连接，避免
        # 一次慢网络请求把所有指令串在同一把连接锁后面。
        self._lock = threading.RLock()
        self._connect()

    def __getstate__(self):
        """Connection wrappers are not transferable across worker contexts."""
        raise TypeError('UnifiedConnection cannot be copied or pickled')

    @property
    def _conn(self):
        if self._backend == 'mysql':
            conn = getattr(self._thread_state, 'conn', None)
            if conn is None:
                conn = self._new_mysql_connection()
                self._thread_state.conn = conn
            return conn
        return self._sqlite_conn

    @_conn.setter
    def _conn(self, value):
        if self._backend == 'mysql':
            self._thread_state.conn = value
        else:
            self._sqlite_conn = value

    def _new_mysql_connection(self):
        import pymysql
        import pymysql.cursors

        kwargs = self._kwargs
        conn = pymysql.connect(
            host=kwargs.get('host', '127.0.0.1'),
            port=kwargs.get('port', 3306),
            user=kwargs.get('user', ''),
            password=kwargs.get('password', ''),
            database=kwargs.get('database', ''),
            charset=kwargs.get('charset', 'utf8mb4'),
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=False,
            connect_timeout=max(1, int(kwargs.get('connect_timeout', 3))),
            read_timeout=max(1, int(kwargs.get('read_timeout', 8))),
            write_timeout=max(1, int(kwargs.get('write_timeout', 8))),
            ssl={} if kwargs.get('ssl') else None,
        )
        with conn.cursor() as cur:
            cur.execute("SET SESSION time_zone = '+08:00'")
            cur.execute(
                'SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED'
            )
            cur.execute(
                'SET SESSION innodb_lock_wait_timeout = %s',
                (max(1, int(kwargs.get('lock_wait_timeout', 3))),),
            )
        conn.commit()
        return conn

    def _connect(self):
        """建立连接。"""
        if self._backend == 'mysql':
            previous = getattr(self._thread_state, 'conn', None)
        else:
            previous = self._sqlite_conn
        if previous is not None:
            try:
                previous.close()
            except Exception:
                pass
        if self._backend == 'mysql':
            self._conn = self._new_mysql_connection()
        else:
            kwargs = self._kwargs
            db_path = kwargs.get('db_path', ':memory:')
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            configure_sqlite_connection(self._conn)
        self._last_active = time.time()

    def _ping(self):
        """MySQL 模式下检查连接是否存活，断开则重连。"""
        if self._backend != 'mysql':
            return
        try:
            self._conn.ping(reconnect=False)
        except Exception:
            self._connect()

    def execute(self, sql: str, params: tuple = ()):
        """返回兼容游标，仅连接断开时自动重连。"""
        guard = self._lock if self._backend == 'sqlite' else nullcontext()
        with guard:
            try:
                cur = self.cursor()
                cur.execute(sql, params)
                rows = cur.fetchall()
                lastrowid = getattr(cur._cur, 'lastrowid', None)
                rowcount = getattr(cur._cur, 'rowcount', -1)
                self._last_active = time.time()
                return _BufferedCursor(rows, lastrowid, rowcount)
            except Exception as exc:
                if (
                    self._backend == 'mysql'
                    and _is_connection_error(exc)
                    and _read_only_sql(sql)
                ):
                    self._connect()
                    cur = self.cursor()
                    cur.execute(sql, params)
                    rows = cur.fetchall()
                    lastrowid = getattr(cur._cur, 'lastrowid', None)
                    rowcount = getattr(cur._cur, 'rowcount', -1)
                    self._last_active = time.time()
                    return _BufferedCursor(rows, lastrowid, rowcount)
                raise

    def executemany(self, sql: str, params_list):
        guard = self._lock if self._backend == 'sqlite' else nullcontext()
        with guard:
            cur = self.cursor()
            cur.executemany(sql, params_list)
            self._last_active = time.time()
            return cur

    def executescript(self, sql: str):
        """SQLite 建表语句；MySQL 下跳过（表已由迁移脚本创建）。"""
        if self._backend == 'sqlite':
            self._conn.executescript(sql)

    def cursor(self):
        return UnifiedCursor(self._conn.cursor(), self._backend, self._prefix)

    def commit(self):
        guard = self._lock if self._backend == 'sqlite' else nullcontext()
        with guard:
            try:
                self._conn.commit()
                self._last_active = time.time()
            except Exception as exc:
                if self._backend == 'mysql' and _is_connection_error(exc):
                    self._connect()
                raise

    def rollback(self):
        guard = self._lock if self._backend == 'sqlite' else nullcontext()
        with guard:
            self._conn.rollback()

    @property
    def row_factory(self):
        return getattr(self._conn, 'row_factory', None)

    @row_factory.setter
    def row_factory(self, value):
        if self._backend == 'sqlite':
            self._conn.row_factory = value


def create_unified_connection() -> UnifiedConnection:
    """根据环境变量配置创建连接。"""
    backend = maiconfig.maimaidx_storage_backend
    prefix = maiconfig.maimaidx_storage_mysql_table_prefix if backend == 'mysql' else ''

    if backend == 'mysql':
        return UnifiedConnection(
            backend='mysql',
            prefix=prefix,
            host=maiconfig.maimaidx_storage_mysql_host,
            port=maiconfig.maimaidx_storage_mysql_port,
            user=maiconfig.maimaidx_storage_mysql_user,
            password=maiconfig.maimaidx_storage_mysql_password,
            database=maiconfig.maimaidx_storage_mysql_database,
            charset=maiconfig.maimaidx_storage_mysql_charset,
            ssl=maiconfig.maimaidx_storage_mysql_ssl,
            connect_timeout=maiconfig.maimaidx_storage_mysql_connect_timeout_seconds,
            read_timeout=maiconfig.maimaidx_storage_mysql_read_timeout_seconds,
            write_timeout=maiconfig.maimaidx_storage_mysql_write_timeout_seconds,
            lock_wait_timeout=maiconfig.maimaidx_storage_mysql_lock_wait_timeout_seconds,
        )
    else:
        DB_DIR = Path(__file__).resolve().parent.parent / 'data' / 'break'
        DB_DIR.mkdir(parents=True, exist_ok=True)
        return UnifiedConnection(
            backend='sqlite',
            db_path=str(DB_DIR / 'break.db'),
        )
