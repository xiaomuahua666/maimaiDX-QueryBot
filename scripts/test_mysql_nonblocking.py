"""MySQL 在线连接必须按工作线程隔离，慢请求不能串死所有指令。"""

from __future__ import annotations

import importlib.util
import sys
import threading
import time
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent

# maimaidx_db 只需要配置对象；用最小包桩避免导入完整 NoneBot 插件。
package = types.ModuleType("nonebot_plugin_maimaidx")
package.__path__ = [str(ROOT)]
libraries = types.ModuleType("nonebot_plugin_maimaidx.libraries")
libraries.__path__ = [str(ROOT / "libraries")]
config = types.ModuleType("nonebot_plugin_maimaidx.config")
config.maiconfig = types.SimpleNamespace()
sys.modules[package.__name__] = package
sys.modules[libraries.__name__] = libraries
sys.modules[config.__name__] = config

name = "nonebot_plugin_maimaidx.libraries.maimaidx_db"
spec = importlib.util.spec_from_file_location(name, ROOT / "libraries" / "maimaidx_db.py")
assert spec and spec.loader
db = importlib.util.module_from_spec(spec)
sys.modules[name] = db
spec.loader.exec_module(db)


class FakeRawCursor:
    def __init__(self, conn):
        self.conn = conn
        self.lastrowid = None
        self.rowcount = 1
        self.rows = []

    def execute(self, sql, params=()):
        if "SLOW" in sql:
            self.conn.release_slow.wait(timeout=2)
        self.rows = [{"value": threading.get_ident()}]

    def executemany(self, sql, params):
        self.rowcount = len(list(params))

    def fetchall(self):
        return list(self.rows)

    def close(self):
        pass


class FakeConnection:
    def __init__(self):
        self.release_slow = threading.Event()

    def cursor(self):
        return FakeRawCursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


created: list[FakeConnection] = []


def fake_new_connection(self):
    conn = FakeConnection()
    created.append(conn)
    return conn


db.UnifiedConnection._new_mysql_connection = fake_new_connection
conn = db.UnifiedConnection(backend="mysql")
slow_started = threading.Event()
slow_done = threading.Event()


def slow_query():
    slow_started.set()
    conn.execute("SELECT SLOW")
    slow_done.set()


worker = threading.Thread(target=slow_query)
worker.start()
assert slow_started.wait(timeout=1)
time.sleep(0.05)

# 主线程必须取得自己的连接并立刻完成；旧实现会卡在全局连接锁上。
started = time.monotonic()
row = conn.execute("SELECT FAST").fetchone()
elapsed = time.monotonic() - started
assert row and elapsed < 0.25, elapsed
assert len(created) >= 2, "MySQL connections were not isolated per worker thread"

for raw in created:
    raw.release_slow.set()
worker.join(timeout=1)
assert slow_done.is_set()

assert not db._read_only_sql("UPDATE break_users SET balance=0")
assert db._read_only_sql(" SELECT balance FROM break_users")

source = (ROOT / "libraries" / "maimaidx_db.py").read_text(encoding="utf-8")
assert "innodb_lock_wait_timeout" in source
assert "TRANSACTION ISOLATION LEVEL READ COMMITTED" in source
assert "and _read_only_sql(sql)" in source

print("mysql nonblocking connection tests: ok")
