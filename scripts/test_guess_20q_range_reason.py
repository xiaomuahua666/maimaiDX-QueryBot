"""20问：版本范围比较 + 判定依据(reason) + 并发锁 的回归测试。

复用 test_guess_20q_prefix 的 stub 机制，不依赖 NoneBot/完整曲库。
"""
import asyncio
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import importlib  # noqa: E402
model_mod = importlib.import_module('libraries.maimaidx_model')
music_stub = types.ModuleType('libraries.maimaidx_music')
music_stub.Music = model_mod.Music


class _AliasList(list):
    def by_alias(self, music_alias):
        return []


class _MaiStub:
    total_alias_list = _AliasList()


music_stub.mai = _MaiStub()
music_stub.guess = types.SimpleNamespace()
sys.modules['libraries.maimaidx_music'] = music_stub

from libraries.maimaidx_guess_20q import (  # noqa: E402
    Guess20QData,
    classify_question, _YES, _NO, _detect_version_range,
    _resolve_version_refs, _compare_version_range, _version_index,
    twentyq_guess,
)
from libraries.maimaidx_model import BasicInfo, Chart, Music  # noqa: E402
from collections import namedtuple  # noqa: E402


def _make_music(version='maimai でらっくす buddies'):
    notes = namedtuple('Notes', ['tap', 'hold', 'slide', 'brk'])(1, 1, 1, 1)
    bi = BasicInfo.model_validate({
        'title': 'T', 'artist': 'DECO*27', 'genre': 'niconico & VOCALOID',
        'bpm': 180, 'release_date': '', 'from': version, 'is_new': False,
        'version': version,
    })
    return Music(
        id='1', title='T', type='SD', ds=[10.0, 12.0, 13.5, 14.6],
        level=['7', '10', '12+', '13+'], cids=[1, 2, 3, 4],
        charts=[Chart(notes=notes, charter='サファ太') for _ in range(4)],
        basic_info=bi,
    )


m = _make_music()  # 双代 buddies, idx=21

# ── 范围方向识别 ──
assert _detect_version_range('在milkplus及以后') == 'after_in'
assert _detect_version_range('在祭代以前') == 'before_ex'
assert _detect_version_range('在双代以后') == 'after_ex'
assert _detect_version_range('murasaki及以前') == 'before_in'
assert _detect_version_range('是双代吗') is None

# ── 范围比较（buddies idx=21）──
def check(q, expected):
    refs = _resolve_version_refs(q)
    d = _detect_version_range(q)
    got = _compare_version_range(_version_index('maimai でらっくす buddies'), refs, d)
    assert got is expected, f'{q}: got {got}, expect {expected}'

check('在milkplus及以后', True)   # 雪代 idx=11 <= 21
check('在祭代以前', False)        # 祭代 idx=19, before_ex => 21<19 False
check('在双代以后', False)        # after_ex => 21>21 False
check('是雪代以后', True)         # 雪代 idx=11 after_ex => 21>11
check('在镜代及以前', True)       # 镜代 idx=23 before_in => 21<=23
check('在murasaki及以前', False)  # 紫代 idx=8 before_in => 21<=8 False

# ── classify_question 返回 reason，且不泄露真实值 ──
ans, consumed, reason = classify_question(m, '是术曲吗')
assert consumed and ans == _YES and reason, f'术曲应是且有依据: {reason}'
assert 'niconico' in reason

ans, consumed, reason = classify_question(m, '紫谱定数是13吗')
assert consumed and ans == _NO and reason
assert '紫谱' in reason and '13' in reason
# 不得泄露真实定数 14.6
assert '14.6' not in reason

ans, consumed, reason = classify_question(m, 'BPM大于100吗')
assert consumed and ans == _YES and 'BPM' in reason and '100' in reason
assert '180' not in reason

ans, consumed, reason = classify_question(m, '艺术家是deco27吗')
assert consumed and ans == _YES and '艺术家' in reason

# ── 版本范围走确定性规则（不依赖 LLM）──
ans, consumed, reason = classify_question(m, '这首歌在Milk Plus及以后')
assert consumed and ans == _YES and reason and '雪代' in reason, reason
ans, consumed, reason = classify_question(m, '在murasaki及以前')
assert consumed and ans == _NO and '紫代' in reason, reason

# ── 并发锁：上一条没处理完时，第二条返回 busy ──
# 用 monkeypatch 让 _check_guess 挂起，模拟正在判定
import libraries.maimaidx_guess_20q as mod  # noqa: E402

orig = mod._check_guess
gate = asyncio.Event()


async def _slow_check(*a, **k):
    await gate.wait()
    return False

mod._check_guess = _slow_check
try:
    import time as _t
    data = Guess20QData(
        music=m, answers=['1'], max_questions=20, duration=600,
        started_at=_t.time(),
    )
    twentyq_guess.groups[999] = data

    async def driver():
        r1 = asyncio.create_task(
            twentyq_guess.process_message(999, 'u', 'n', '我猜 某曲')
        )
        await asyncio.sleep(0.05)  # 确保 r1 已拿到锁
        r2 = await twentyq_guess.process_message(999, 'u2', 'n2', '我问 是术曲吗')
        assert r2.get('kind') == 'busy', f'并发应返回 busy: {r2}'
        gate.set()
        r1v = await r1
        assert r1v.get('kind') == 'wrong_guess', r1v

    asyncio.run(driver())
finally:
    mod._check_guess = orig
    twentyq_guess.groups.pop(999, None)
    twentyq_guess._proc_locks.pop(999, None)

print('range/reason/lock tests passed')
