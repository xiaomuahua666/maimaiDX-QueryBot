#!/usr/bin/env python3
"""验证 BREAK 多语句事务中途异常时 rollback 且无半截事务。"""
import ast
import sys
import types
import sqlite3
import time
import json
import random
import re
from pathlib import Path
from typing import Optional, List, Dict, Tuple, Any
from pydantic import BaseModel, Field
from datetime import datetime, timedelta, timezone, date
from threading import RLock
from dataclasses import dataclass, field
from contextlib import contextmanager

class _FakeCM:
    def double_break_info(self, qqid): return (False, 0.0, 0.0)
_FAKE_CARD_MANAGER = _FakeCM()

SRC_PATH = Path(__file__).resolve().parents[1] / 'libraries' / 'maimaidx_break.py'
SRC = SRC_PATH.read_text(encoding='utf-8')
SRC = SRC.replace('from .maimaidx_card import card_manager', 'card_manager = _FAKE_CARD_MANAGER')
tree = ast.parse(SRC)
cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'BreakDatabase')
tops = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name != 'BreakDatabase']
others = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.Assign, ast.If)) and not isinstance(n, ast.ClassDef)]


mod = types.ModuleType('maimaidx_card'); mod.card_manager = _FakeCM()
libs = types.ModuleType('libraries'); libs.maimaidx_card = mod
sys.modules['libraries'] = libs; sys.modules['libraries.maimaidx_card'] = mod

ns = dict(globals())
for n in tops:
    exec(compile(ast.Module(body=[n], type_ignores=[]), '<t>', 'exec'), ns)
for n in others:
    try:
        exec(compile(ast.Module(body=[n], type_ignores=[]), '<o>', 'exec'), ns)
    except Exception:
        pass
exec(compile(ast.Module(body=[cls], type_ignores=[]), '<cls>', 'exec'), ns)
BreakDatabase = ns['BreakDatabase']

conn = sqlite3.connect(':memory:', check_same_thread=False)
conn.row_factory = sqlite3.Row
db = object.__new__(BreakDatabase)
db._conn = conn
db._lock = RLock()
for ddl in (
    'CREATE TABLE break_users (qqid INTEGER PRIMARY KEY, balance INTEGER NOT NULL DEFAULT 0, streak INTEGER NOT NULL DEFAULT 0, created_at REAL, updated_at REAL)',
    'CREATE TABLE break_daily_usage (qqid INTEGER NOT NULL, date TEXT NOT NULL, free_used INTEGER NOT NULL DEFAULT 0, query_count INTEGER NOT NULL DEFAULT 0, analysis_count INTEGER NOT NULL DEFAULT 0, break_spent INTEGER NOT NULL DEFAULT 0, break_gained INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (qqid, date))',
    'CREATE TABLE break_game_daily (qqid INTEGER, date TEXT, game TEXT, break_awarded INTEGER NOT NULL DEFAULT 0, last_at REAL, PRIMARY KEY (qqid,date,game))',
    'CREATE TABLE break_config (key TEXT PRIMARY KEY, value TEXT)',
    'CREATE TABLE break_log (id INTEGER PRIMARY KEY AUTOINCREMENT, qqid INTEGER, delta INTEGER, reason TEXT, meta TEXT, created_at REAL)',
):
    conn.execute(ddl)

db._ensure_user(100)
real_exec = conn.execute

# happy path 先行
r = db.award_game_break(100, 'song', 5, 'verify_happy')
assert r.awarded == 5 and r.capped is False, r
assert real_exec('SELECT balance FROM break_users WHERE qqid=100').fetchone()['balance'] == 5

# —— 故障注入：award_game_break 链中 INSERT break_game_daily 时炸 ——
rolled = []
class PatchedConn:
    def __init__(self, inner): self._inner = inner
    def execute(self, sql, *a, **k):
        if 'INSERT OR IGNORE INTO break_game_daily' in sql:
            raise sqlite3.OperationalError('simulated mid-chain failure')
        return self._inner.execute(sql, *a, **k)
    def commit(self): self._inner.commit()
    def rollback(self):
        rolled.append(1); self._inner.rollback()
    def __getattr__(self, k): return getattr(self._inner, k)

db._conn = PatchedConn(conn)
try:
    db.award_game_break(100, 'song', 3, 'boom')
    raise SystemExit('should have raised')
except sqlite3.OperationalError:
    pass
db._conn = conn
assert rolled == [1], f'award rollback 未被调用: {rolled}'
bal = real_exec('SELECT balance FROM break_users WHERE qqid=100').fetchone()['balance']
assert bal == 5, f'回滚后余额应仍为 5, got {bal}'
gd = real_exec("SELECT COALESCE(SUM(break_awarded),0) t FROM break_game_daily WHERE qqid=100").fetchone()['t']
assert gd == 5, f'break_game_daily 总额应仍为 5, got {gd}'

# —— transfer 链中 UPDATE break_daily_usage(break_gained) 时炸 ——
rolled.clear()
db._ensure_user(200)
class PatchedConn2(PatchedConn):
    def execute(self, sql, *a, **k):
        if 'break_gained=break_gained+?' in sql:
            raise sqlite3.OperationalError('simulated transfer failure')
        return self._inner.execute(sql, *a, **k)
db._conn = PatchedConn2(conn)
try:
    db.transfer(100, 200, 2)
    raise SystemExit('transfer should have raised')
except sqlite3.OperationalError:
    pass
db._conn = conn
assert rolled == [1], f'transfer rollback 未被调用: {rolled}'
bal100 = real_exec('SELECT balance FROM break_users WHERE qqid=100').fetchone()['balance']
bal200 = real_exec('SELECT balance FROM break_users WHERE qqid=200').fetchone()['balance']
assert bal100 == 5 and bal200 == 0, f'转账回滚后余额应 5/0, got {bal100}/{bal200}'

# —— lottery 已更新余额、写每日统计时炸，必须完整回滚且不写日志 ——
rolled.clear()
real_exec('UPDATE break_users SET balance=20 WHERE qqid=100')
conn.commit()

class PatchedConn3(PatchedConn):
    def execute(self, sql, *a, **k):
        if 'UPDATE break_daily_usage SET break_spent=break_spent+?' in sql:
            raise sqlite3.OperationalError('simulated lottery timeout')
        return self._inner.execute(sql, *a, **k)

db._conn = PatchedConn3(conn)
try:
    db.lottery(100, 2)
    raise SystemExit('lottery should have raised')
except sqlite3.OperationalError:
    pass
db._conn = conn
assert rolled == [1], f'lottery rollback 未被调用: {rolled}'
lottery_balance = real_exec(
    'SELECT balance FROM break_users WHERE qqid=100'
).fetchone()['balance']
assert lottery_balance == 20, f'抽奖回滚后余额应仍为 20, got {lottery_balance}'
lottery_logs = real_exec(
    "SELECT COUNT(*) AS total FROM break_log WHERE qqid=100 AND reason='lottery'"
).fetchone()['total']
assert lottery_logs == 0, f'抽奖失败不应留下日志, got {lottery_logs}'

print('rollback verification: OK (award+transfer+lottery 均无半截事务)')
