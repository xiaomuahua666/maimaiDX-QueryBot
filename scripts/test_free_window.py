#!/usr/bin/env python3
"""限时免费时段（free_window）回归测试。

验证：
1. is_free_window_active 配置开关 / 时间段格式 / 边界 / 异常安全降级
2. DEFAULT_CONFIG 种子写入（free_window_enabled=0 默认关闭）
3. 各计费入口在 free_window 生效时免单、不扣余额、不消耗每日首免
4. free_window 关闭时正常扣费（回归）
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path = [item for item in sys.path if item and Path(item).resolve() != ROOT]
sys.path.insert(0, str(ROOT.parent))

os.environ.setdefault("COMMAND_START", '["", "/"]')
os.environ.setdefault("MAIMAIDXPATH", str(ROOT / "static"))
os.environ.setdefault("MAIMAIDX_STORAGE_FAIL_FAST", "false")
os.environ.setdefault("MAIMAIDX_MESSAGE_STATS_ENABLED", "false")
os.environ.setdefault("MAIMAIDX_BUSY_SURCHARGE_ENABLED", "false")

import nonebot  # noqa: E402

nonebot.init()

import nonebot_plugin_maimaidx  # noqa: F401, E402
from nonebot_plugin_maimaidx.libraries import maimaidx_break  # noqa: E402

break_db = maimaidx_break.break_db
is_free_window_active = maimaidx_break.is_free_window_active

TZ8 = timezone(timedelta(hours=8))


def _ts_utc8(hour: int, minute: int = 0, day: int = 1) -> float:
    """构造 UTC+8 某日某时刻的时间戳。"""
    return datetime(2026, 1, day, hour, minute, tzinfo=TZ8).timestamp()


def _cur_utc8_hour() -> int:
    return datetime.now(TZ8).hour


def _cur_window() -> str:
    """覆盖当前 UTC+8 小时的合法时段（用于测不传 now 的计费入口）。"""
    h = _cur_utc8_hour()
    return f'{h},{h + 1}'  # h=23 时 23,24 合法


_qqid_base = (int(time.time()) % 100000) * 1000 + 300000000
_qqid_counter = [0]


def _new_qqid() -> int:
    _qqid_counter[0] += 1
    return _qqid_base + _qqid_counter[0]


def _reset_free_window() -> None:
    break_db.set_config('free_window_enabled', '0')
    break_db.set_config('free_window_hours', '')


def _enable_free_window(hours: str) -> None:
    break_db.set_config('free_window_enabled', '1')
    break_db.set_config('free_window_hours', hours)


def _logs(qqid: int, reason_prefix: str = 'free_window_exempt'):
    rows = break_db._conn.execute(
        'SELECT reason, delta, meta FROM break_log WHERE qqid = ? ORDER BY created_at',
        (qqid,),
    ).fetchall()
    import json as _json
    out = []
    for r in rows:
        try:
            meta = _json.loads(r['meta'] or '{}')
        except Exception:
            meta = {}
        out.append({'reason': r['reason'], 'delta': r['delta'], 'meta': meta})
    return [x for x in out if x['reason'].startswith(reason_prefix)]


# ───────────────────── 1. 配置种子：DEFAULT_CONFIG 含 free_window ─────────────────────
assert 'free_window_enabled' in maimaidx_break.DEFAULT_CONFIG
assert 'free_window_hours' in maimaidx_break.DEFAULT_CONFIG
assert maimaidx_break.DEFAULT_CONFIG['free_window_enabled'] == '0'
assert maimaidx_break.DEFAULT_CONFIG['free_window_hours'] == ''
# _seed_config 已写入 break_config 表
assert break_db.get_config('free_window_enabled', 'MISSING') == '0'
assert break_db.get_config('free_window_hours', 'MISSING') == ''
print('config seed tests passed')

# ───────────────────── 2. is_free_window_active 单元测试 ─────────────────────
try:
    # 开关关闭 → 永远 False
    _reset_free_window()
    assert is_free_window_active(now=_ts_utc8(18)) is False, '开关关闭应 False'

    # 开关开 + 17,20
    _enable_free_window('17,20')
    assert is_free_window_active(now=_ts_utc8(17, 0)) is True, '17:00 应在区间'
    assert is_free_window_active(now=_ts_utc8(17, 30)) is True, '17:30 应在区间'
    assert is_free_window_active(now=_ts_utc8(19, 59)) is True, '19:59 应在区间'
    assert is_free_window_active(now=_ts_utc8(20, 0)) is False, '20:00 应不在区间（开区间右端）'
    assert is_free_window_active(now=_ts_utc8(16, 59)) is False, '16:59 应不在区间'
    assert is_free_window_active(now=_ts_utc8(23, 0)) is False, '23:00 应不在区间'

    # end=24 合法：23,24 覆盖 23 点
    _enable_free_window('23,24')
    assert is_free_window_active(now=_ts_utc8(23, 0)) is True, '23:00 在 23,24 区间'
    assert is_free_window_active(now=_ts_utc8(23, 59)) is True, '23:59 在 23,24 区间'
    assert is_free_window_active(now=_ts_utc8(0, 0)) is False, '0:00 不在 23,24 区间'

    # 全天 0,24
    _enable_free_window('0,24')
    assert is_free_window_active(now=_ts_utc8(0)) is True
    assert is_free_window_active(now=_ts_utc8(23, 59)) is True
    assert is_free_window_active(now=_ts_utc8(12)) is True

    # 异常安全降级（绝不抛异常，统一 False）
    for bad in ('', '17', '17,20,23', 'a,b', '17,a', '-1,20', '17,25', '20,17', '17,17', ' ', '17,', ',20'):
        _enable_free_window(bad)
        assert is_free_window_active(now=_ts_utc8(18)) is False, f'异常配置 {bad!r} 应降级 False'
    # 带空格容错：'17, 20' 等价于 '17,20'
    _enable_free_window('17, 20')
    assert is_free_window_active(now=_ts_utc8(18)) is True, '带空格应容错识别'
    assert is_free_window_active(now=_ts_utc8(16)) is False
    # 开关值容错
    for ok in ('1', 'true', 'yes', 'on', '开', '开启', 'TRUE'):
        break_db.set_config('free_window_enabled', ok)
        break_db.set_config('free_window_hours', '0,24')
        assert is_free_window_active(now=_ts_utc8(12)) is True, f'开关值 {ok!r} 应识别为开启'
    for off in ('0', 'false', 'no', 'off', '关闭', '', '2', 'random'):
        break_db.set_config('free_window_enabled', off)
        break_db.set_config('free_window_hours', '0,24')
        assert is_free_window_active(now=_ts_utc8(12)) is False, f'开关值 {off!r} 应识别为关闭'
finally:
    _reset_free_window()
print('is_free_window_active unit tests passed')

# ───────────────────── 3. try_consume：free_window 开 → 免单不扣 ─────────────────────
try:
    # 关闭：正常扣费
    _reset_free_window()
    u1 = _new_qqid()
    break_db.add_balance(u1, 100, 'test')
    ok = break_db.try_consume(u1, 10, 'test_charge')
    assert ok is True
    assert break_db.get_balance(u1) == 90, f'关闭时应扣 10，余额={break_db.get_balance(u1)}'

    # 开启：免单不扣
    _enable_free_window(_cur_window())
    u2 = _new_qqid()
    break_db.add_balance(u2, 100, 'test')
    ok = break_db.try_consume(u2, 10, 'test_charge')
    assert ok is True
    assert break_db.get_balance(u2) == 100, f'开启时应免单不扣，余额={break_db.get_balance(u2)}'
    # 流水：free_window_exempt，delta=0
    fw_logs = _logs(u2)
    assert len(fw_logs) >= 1, f'应有 free_window 免单流水: {fw_logs}'
    assert fw_logs[-1]['delta'] == 0
    assert fw_logs[-1]['meta'].get('free_window') is True
    assert fw_logs[-1]['meta'].get('listed_cost') == 10

    # 余额不足时 free_window 开启仍返回 True（免单，无需余额）
    u3 = _new_qqid()  # 无余额
    ok = break_db.try_consume(u3, 999, 'test_no_balance')
    assert ok is True, 'free_window 开启时余额不足也应免单返回 True'
    assert break_db.get_balance(u3) == 0, '免单不应产生负余额'
finally:
    _reset_free_window()
print('try_consume free_window tests passed')

# ───────────────────── 4. settle_service_success：免单 + 不消耗每日首免 ─────────────────────
try:
    # 非 daily_free 服务（ticket）：free_window 开 → charged=0
    _enable_free_window(_cur_window())
    u = _new_qqid()
    break_db.add_balance(u, 100, 'test')
    r = break_db.settle_service_success(u, 'ticket', 5)
    assert r.charged == 0, f'free_window 开 ticket 应 charged=0: {r}'
    assert r.free_window is True, f'应标记 free_window: {r}'
    assert r.free is False, 'free_window 路径不应标记 daily free'
    assert break_db.get_balance(u) == 100, f'应不扣余额: {break_db.get_balance(u)}'

    # daily_free 服务（upload）：free_window 开 → 不消耗每日首免资格
    u2 = _new_qqid()
    break_db.add_balance(u2, 100, 'test')
    r2 = break_db.settle_service_success(u2, 'upload', 2)
    assert r2.charged == 0 and r2.free_window is True
    # 关闭 free_window 后，upload 仍应享每日首免（未被消耗）
    _reset_free_window()
    r3 = break_db.settle_service_success(u2, 'upload', 2)
    assert r3.charged == 0 and r3.free is True, f'每日首免应仍可用: {r3}'
    assert break_db.get_balance(u2) == 100, '每日首免不扣余额'

    # 关闭：ticket 正常扣费
    u4 = _new_qqid()
    break_db.add_balance(u4, 100, 'test')
    r4 = break_db.settle_service_success(u4, 'ticket', 5)
    assert r4.charged == 5 and r4.free_window is False, f'关闭时应扣 5: {r4}'
    assert break_db.get_balance(u4) == 95
finally:
    _reset_free_window()
print('settle_service_success free_window tests passed')

# ───────────────────── 5. settle_image_render：免单 + 返回文案 ─────────────────────
try:
    _enable_free_window(_cur_window())
    u = _new_qqid()
    break_db.add_balance(u, 100, 'test')
    line = maimaidx_break.settle_image_render(u)
    assert line is not None, 'free_window 开应返回免单文案'
    assert '限时免费' in line, f'文案应含限时免费: {line}'
    assert break_db.get_balance(u) == 100, f'应不扣余额: {break_db.get_balance(u)}'
    fw_logs = _logs(u)
    assert any(l['reason'] == 'free_window_exempt:image_render' for l in fw_logs), \
        f'应有 image_render 免单流水: {fw_logs}'

    # 关闭：正常扣费（image_render_cost 默认 1）
    _reset_free_window()
    u2 = _new_qqid()
    break_db.add_balance(u2, 100, 'test')
    line2 = maimaidx_break.settle_image_render(u2)
    assert line2 is None, '关闭时应返回 None（正常扣费无文案）'
    assert break_db.get_balance(u2) == 99, f'应扣 1: {break_db.get_balance(u2)}'
finally:
    _reset_free_window()
print('settle_image_render free_window tests passed')

# ───────────────────── 6. charge_session_extra：免单 + 不消耗每日首免 ─────────────────────
try:
    _enable_free_window(_cur_window())
    u = _new_qqid()
    break_db.add_balance(u, 100, 'test')
    ok = maimaidx_break.charge_session_extra(u, 3, 'search')
    assert ok is True
    assert break_db.get_balance(u) == 100, 'free_window 开应不扣'
    # 关闭后每日首免仍可用
    _reset_free_window()
    ok2 = maimaidx_break.charge_session_extra(u, 3, 'search')
    assert ok2 is True
    assert break_db.get_balance(u) == 100, '每日首免应不扣'

    # 关闭且首免已用 → 正常扣费
    u3 = _new_qqid()
    break_db.add_balance(u3, 100, 'test')
    break_db.mark_daily_free_used(u3)
    ok3 = maimaidx_break.charge_session_extra(u3, 3, 'search')
    assert ok3 is True
    assert break_db.get_balance(u3) == 97, f'应扣 3: {break_db.get_balance(u3)}'
finally:
    _reset_free_window()
print('charge_session_extra free_window tests passed')

# ───────────────────── 7. settle_prober_fetch / settle_cache_hit：免单 ─────────────────────
try:
    _enable_free_window(_cur_window())
    # 先消耗每日首免，确保走 try_consume 路径（验证 free_window 拦截）
    u = _new_qqid()
    break_db.add_balance(u, 100, 'test')
    break_db.mark_daily_free_used(u)
    maimaidx_break.settle_prober_fetch(u)
    assert break_db.get_balance(u) == 100, f'prober_fetch free_window 应不扣: {break_db.get_balance(u)}'
    maimaidx_break.settle_cache_hit(u)
    assert break_db.get_balance(u) == 100, f'cache_hit free_window 应不扣: {break_db.get_balance(u)}'

    # 关闭：prober_fetch 扣 query_cost=1
    _reset_free_window()
    u2 = _new_qqid()
    break_db.add_balance(u2, 100, 'test')
    break_db.mark_daily_free_used(u2)
    maimaidx_break.settle_prober_fetch(u2)
    assert break_db.get_balance(u2) == 99, f'关闭 prober_fetch 应扣 1: {break_db.get_balance(u2)}'
finally:
    _reset_free_window()
print('settle_prober/cache free_window tests passed')

# ───────────────────── 8. ensure_*：free_window 开 → 余额不足也放行 ─────────────────────
try:
    _enable_free_window(_cur_window())
    u = _new_qqid()  # 余额 0
    break_db.mark_daily_free_used(u)  # 确保无每日首免
    # 不应抛 BreakInsufficientError
    maimaidx_break.ensure_query_affordable(u)
    maimaidx_break.ensure_image_render_affordable(u)
    break_db.ensure_service_affordable(u, 'ticket', 5)

    # 关闭：余额不足应抛
    _reset_free_window()
    raised = False
    try:
        maimaidx_break.ensure_query_affordable(u)
    except maimaidx_break.BreakInsufficientError:
        raised = True
    assert raised, '关闭时余额不足应抛 BreakInsufficientError'
finally:
    _reset_free_window()
print('ensure_* free_window tests passed')

# ───────────────────── 9. 锐评 reserve/settle：免单 ─────────────────────
try:
    _enable_free_window(_cur_window())
    u = _new_qqid()
    break_db.add_balance(u, 100, 'test')
    reservation = maimaidx_break.reserve_analysis_charge(u)
    assert reservation.amount == 0, f'free_window 开预扣应为 0: {reservation}'
    assert reservation.free_window is True, f'应标记 free_window: {reservation}'
    assert reservation.freedom is False
    assert break_db.get_balance(u) == 100, 'free_window 开不应预扣'
    # 结算：免单
    cost = maimaidx_break.settle_analysis_charge(u, 8, reserved=reservation)
    assert cost == 0, f'free_window 开结算应返回 0: {cost}'
    assert break_db.get_balance(u) == 100, 'free_window 开结算不应扣'
    fw_logs = _logs(u)
    assert any('b50_analysis' in l['reason'] for l in fw_logs), \
        f'应有锐评免单流水: {fw_logs}'

    # 关闭：正常预扣 + 结算
    _reset_free_window()
    u2 = _new_qqid()
    break_db.add_balance(u2, 100, 'test')
    reservation2 = maimaidx_break.reserve_analysis_charge(u2)
    assert reservation2.amount > 0, f'关闭应预扣: {reservation2}'
    assert reservation2.free_window is False
    assert break_db.get_balance(u2) == 100 - reservation2.amount
    pre_balance = break_db.get_balance(u2)
    cost2 = maimaidx_break.settle_analysis_charge(u2, 5, reserved=reservation2)
    assert cost2 == 5
    # 预扣 10，实际 5，退回 5 → 净扣 5
    assert break_db.get_balance(u2) == 100 - 5, \
        f'关闭应净扣 5: {break_db.get_balance(u2)}'
finally:
    _reset_free_window()
print('analysis reserve/settle free_window tests passed')

# ───────────────────── 10. 跨时段切换不残留 ─────────────────────
try:
    u = _new_qqid()
    break_db.add_balance(u, 100, 'test')
    # 开启 → 免单
    _enable_free_window(_cur_window())
    break_db.try_consume(u, 5, 't1')
    assert break_db.get_balance(u) == 100
    # 关闭 → 扣费
    _reset_free_window()
    break_db.try_consume(u, 5, 't2')
    assert break_db.get_balance(u) == 95, f'关闭后应扣 5: {break_db.get_balance(u)}'
    # 再开启 → 免单
    _enable_free_window(_cur_window())
    break_db.try_consume(u, 5, 't3')
    assert break_db.get_balance(u) == 95
finally:
    _reset_free_window()
print('cross-toggle tests passed')

print('\n===== all free_window tests passed =====')
