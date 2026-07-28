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
import time
from pathlib import Path
from typing import Optional

from ..config import maiconfig

# 需要加前缀的表名（不含前缀的原始名）
_TABLE_NAMES = (
    'break_users', 'break_daily_usage', 'break_group_checkin',
    'break_config', 'break_log', 'break_guess_daily',
    'break_service_daily', 'break_daily_reward', 'break_red_packet',
    'break_red_packet_claim', 'break_makeup_checkin', 'break_gamble_pool',
    'break_gamble_pool_payout',
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


def _is_connection_error(exc: Exception) -> bool:
    """判断是否为连接断开类错误。"""
    try:
        import pymysql.err
        if isinstance(exc, (pymysql.err.InterfaceError, pymysql.err.OperationalError)):
            return True
    except ImportError:
        pass
    return False


class UnifiedConnection:
    """兼容 sqlite3.Connection 的统一连接，透明支持 SQLite 和 MySQL。"""

    def __init__(self, backend: str = 'sqlite', prefix: str = '', **kwargs):
        self._backend = backend
        self._prefix = prefix
        self._kwargs = kwargs
        self._conn = None
        self._last_active = 0.0
        self._connect()

    def _connect(self):
        """建立连接。"""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
        kwargs = self._kwargs
        if self._backend == 'mysql':
            import pymysql
            import pymysql.cursors
            self._conn = pymysql.connect(
                host=kwargs.get('host', '127.0.0.1'),
                port=kwargs.get('port', 3306),
                user=kwargs.get('user', ''),
                password=kwargs.get('password', ''),
                database=kwargs.get('database', ''),
                charset=kwargs.get('charset', 'utf8mb4'),
                cursorclass=pymysql.cursors.DictCursor,
                autocommit=False,
                connect_timeout=5,
                read_timeout=10,
                write_timeout=10,
            )
        else:
            db_path = kwargs.get('db_path', ':memory:')
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA wal_autocheckpoint=1000")
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
        try:
            cur = self.cursor()
            cur.execute(sql, params)
            self._last_active = time.time()
            return cur
        except Exception as exc:
            if self._backend == 'mysql' and _is_connection_error(exc):
                self._connect()
                cur = self.cursor()
                cur.execute(sql, params)
                self._last_active = time.time()
                return cur
            raise

    def executescript(self, sql: str):
        """SQLite 建表语句；MySQL 下跳过（表已由迁移脚本创建）。"""
        if self._backend == 'sqlite':
            self._conn.executescript(sql)

    def cursor(self):
        return UnifiedCursor(self._conn.cursor(), self._backend, self._prefix)

    def commit(self):
        try:
            self._conn.commit()
            self._last_active = time.time()
        except Exception as exc:
            if self._backend == 'mysql' and _is_connection_error(exc):
                self._connect()
            raise

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
        )
    else:
        DB_DIR = Path(__file__).resolve().parent.parent / 'data' / 'break'
        DB_DIR.mkdir(parents=True, exist_ok=True)
        return UnifiedConnection(
            backend='sqlite',
            db_path=str(DB_DIR / 'break.db'),
        )
