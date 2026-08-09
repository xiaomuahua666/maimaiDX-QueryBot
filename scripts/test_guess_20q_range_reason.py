"""20问：判定依据(reason) + 并发锁 + 定数档位语义 的回归测试。

版本范围推理（「在X代以前/以后」）已移交 LLM，规则层不再处理；
本文件聚焦：reason 字段不泄露真值、并发锁、定数档位语义。
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
    classify_question, _YES, _NO,
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


m = _make_music()  # 双代 buddies

# ── classify_question 返回 reason，且不泄露真实值 ──
# 注：分类/艺术家/版本等需语义理解的维度已移交 LLM，规则层只处理纯数值题。
ans, consumed, reason = classify_question(m, '紫谱定数是13吗')
assert consumed and ans == _NO and reason
assert '紫谱' in reason and '13' in reason
# 不得泄露真实定数 14.6
assert '14.6' not in reason

ans, consumed, reason = classify_question(m, 'BPM大于100吗')
assert consumed and ans == _YES and 'BPM' in reason and '100' in reason
assert '180' not in reason

# 分类/艺术家/版本是非题移交 LLM：规则层不命中 → consumed=False
assert classify_question(m, '是术曲吗')[1] is False, '分类题移交 LLM'
assert classify_question(m, '艺术家是deco27吗')[1] is False, '艺术家题移交 LLM'
assert classify_question(m, '是双代吗')[1] is False, '版本题移交 LLM'

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

print('reason/lock tests passed')

# ── 定数档位语义：「是14吗」指 14.0~14.5 档，不是精确等于 14.0 ──
def _make_ds_music(purple_ds):
    bi = BasicInfo.model_validate({
        'title': 'D', 'artist': 'A', 'genre': '舞萌', 'bpm': 180,
        'release_date': '', 'from': 'maimai でらっくす', 'is_new': False,
        'version': 'maimai でらっくす',
    })
    return Music(
        id='9', title='D', type='SD', ds=[10.0, 12.0, 13.5, purple_ds],
        level=['7', '10', '12+', '13+'], cids=[1, 2, 3, 4],
        charts=[Chart(notes=namedtuple('N', ['tap', 'hold', 'slide', 'brk'])(1, 1, 1, 1), charter='C') for _ in range(4)],
        basic_info=bi,
    )

# 14.4 问「是14吗」→ 应是（14 档 = 14.0~14.5），不是精确等于
ans, _, reason = classify_question(_make_ds_music(14.4), '紫谱是14吗')
assert ans == _YES, f'14.4 应属于 14 档: {ans}'
assert '14~14.5' in reason, reason
# 14.6 问「是14吗」→ 不是
ans, _, _ = classify_question(_make_ds_music(14.6), '紫谱是14吗')
assert ans == _NO, f'14.6 不属 14 档: {ans}'
# 14.4 问「14+」→ 不是（14+ = 14.5~15.0）
ans, _, reason = classify_question(_make_ds_music(14.4), '紫谱是14+吗')
assert ans == _NO, f'14.4 不属 14+: {ans}'
assert '14.5~15' in reason, reason
# 14.5 问「14+」→ 是
ans, _, _ = classify_question(_make_ds_music(14.5), '紫谱是14+吗')
assert ans == _YES, f'14.5 属 14+: {ans}'
# 明确比较词「等于14」→ 精确相等，14.4 不等于 14.0 → 不是
ans, _, _ = classify_question(_make_ds_music(14.4), '紫谱定数等于14吗')
assert ans == _NO, f'等于应精确比较，14.4≠14: {ans}'

print('ds tier semantics tests passed')
