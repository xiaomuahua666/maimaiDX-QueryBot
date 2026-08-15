"""验证 ASCII 拼写错误版本题的门控放行 + LLM 调用链路。

用 mock _llm_classify 模拟 LLM 返回，确认：
1. 拼写错误版本题门控放行（不再直接 unknown）
2. 确实调用了 _llm_classify
3. 日常闲聊不被误判为版本题走 LLM（门控不误触）
"""
import sys
import types
import asyncio
from pathlib import Path
from collections import namedtuple

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

import libraries.maimaidx_guess_20q as mod  # noqa: E402
from libraries.maimaidx_guess_20q import classify_question, _looks_like_ascii_version_text  # noqa: E402
from libraries.maimaidx_model import BasicInfo, Chart, Music  # noqa: E402

PASS = 0
FAIL = 0


def _mk(ver):
    bi = BasicInfo.model_validate({
        'title': 'V', 'artist': 'A', 'genre': 'maimai', 'bpm': 180,
        'release_date': '', 'from': ver, 'is_new': False, 'version': ver,
    })
    return Music(
        id='9', title='V', type='SD', ds=[10.0, 12.0, 13.6, 14.6],
        level=['7', '10', '12+', '13+'], cids=[1, 2, 3, 4],
        charts=[Chart(notes=namedtuple('N', ['tap', 'hold', 'slide', 'brk'])(1, 1, 1, 1), charter='x') for _ in range(4)],
        basic_info=bi,
    )


# ── 门控函数单测 ──
print('=' * 70)
print('【1】门控函数 _looks_like_ascii_version_text')
print('=' * 70)

# 应放行（拼写错误版本题）
_should_pass = [
    'muilkplus', 'muilk plus', 'mlikplus', 'imlk',
    'buudies', 'buddise', 'budies', 'buddes',
    'splsh', 'salsh', 'splah',
    'unvierse', 'unverse', 'univ',
    'festval', 'festivla', 'fest',
    'pirsm', 'przm', 'prsm+',
    'cricle', 'cirle', 'circl',
    'finlae', 'fniale',
    'murasaki', 'mura',
    'dx加', 'dx+', 'dxplus', 'dx家', 'dx佳',
    'orange加', 'pink+', 'greenplus',
    'muilkplus吗', '是buudies吗', 'dx加吗',
]
# 不应放行（日常闲聊/非版本题）
_should_fail = [
    '1+1', '2+3=5', 'index', 'next', 'text', 'box', 'fox',
    'orange juice', 'pink floyd', 'green tea', 'apple',
    'dx', 'plus', '加', '是术曲吗', 'bpm大于180吗',
    '紫谱定数14吗', '谱师是沙发太吗', '标题是英文吗',
    'hello world', 'abcd', 'test',
]

for t in _should_pass:
    r = _looks_like_ascii_version_text(t)
    if r:
        PASS += 1
        print(f'  ✓ 放行: {t!r}')
    else:
        FAIL += 1
        print(f'  ✗ 应放行但未放行: {t!r}')

for t in _should_fail:
    r = _looks_like_ascii_version_text(t)
    if not r:
        PASS += 1
        print(f'  ✓ 拦截: {t!r}')
    else:
        FAIL += 1
        print(f'  ✗ 不应放行但放行了: {t!r}')

# ── classify_question 链路：门控放行后走 LLM ──
print()
print('=' * 70)
print('【2】classify_question: 门控放行 → 走 LLM（mock 验证）')
print('=' * 70)

# 规则层 classify_question 是同步的，门控放行后 _q_version 精确匹配失败返回 None，
# 最终 classify_question 返回 (default_unknown_ans, False, '')。
# consumed=False 表示规则未命中，上层 _process_message 会据此调用 _llm_classify。
# 这里验证：门控放行的拼写错误题，规则层 consumed=False（而非被某个 handler 误判）。

_rule_should_miss = [
    ('muilkplus吗', 'maimai milk plus'),
    ('buudies吗', 'maimai でらっくす buddies'),
    ('splsh吗', 'maimai でらっくす splash'),
    ('prsm+吗', 'maimai でらっくす prism plus'),
    ('circl吗', 'maimai でらっくす circle'),
    ('muilk plus吗', 'maimai milk plus'),
]
for q, ver in _rule_should_miss:
    m = _mk(ver)
    a, c, r = classify_question(m, q)
    # consumed=False 表示规则未命中，上层会走 LLM
    if not c:
        PASS += 1
        print(f'  ✓ 规则未命中(→走LLM): 曲={ver[:25]:25s} 问={q}')
    else:
        FAIL += 1
        print(f'  ✗ 规则不应命中(应走LLM): 曲={ver} 问={q} consumed={c} ans={a!r}')

# 门控拦截的日常闲聊题：规则层也应 consumed=False（走 unknown/LLM，但非版本 LLM）
print()
print('  --- 日常闲聊题不应被版本门控误触 ---')
_chat_should_miss = [
    '1+1吗', 'hello world吗', 'orange juice吗', 'pink floyd吗',
]
for q in _chat_should_miss:
    m = _mk('maimai でらっくす')
    a, c, r = classify_question(m, q)
    # 这些既不是版本题也不是其他规则题，应 consumed=False
    if not c:
        PASS += 1
        print(f'  ✓ 闲聊未被误判: 问={q}')
    else:
        FAIL += 1
        print(f'  ✗ 闲聊被误判为规则题: 问={q} consumed={c} ans={a!r}')

# ── 异步链路：mock _llm_classify 验证确实被调用 ──
print()
print('=' * 70)
print('【3】异步链路: mock _llm_classify 验证调用')
print('=' * 70)

orig_llm = mod._llm_classify
llm_calls = []


async def _mock_llm(music, text, config, **kwargs):
    llm_calls.append(text)
    # 模拟 LLM 按 prompt 容错理解：muilkplus → milk plus → 雪代
    return ('是喵 ✅', '判定维度：版本是否为 milk plus（雪代）')


mod._llm_classify = _mock_llm
try:
    from libraries.maimaidx_guess_20q import Guess20QData, twentyq_guess
    import time as _t

    async def _driver():
        m = _mk('maimai milk plus')  # 曲目就是 milk plus（雪代）
        data = Guess20QData(
            music=m, answers=['9'], max_questions=20, duration=600,
            started_at=_t.time(),
        )
        twentyq_guess.groups[888] = data
        # 玩家用错字提问
        r = await twentyq_guess.process_message(888, 'u', 'n', '我问 muilkplus吗')
        return r

    r = asyncio.run(_driver())
    if llm_calls and 'muilkplus' in llm_calls[0]:
        PASS += 1
        print(f'  ✓ LLM 被调用，入参: {llm_calls[0]!r}')
        print(f'    返回 kind={r.get("kind")} answer={r.get("answer", "")[:40]!r}')
    else:
        FAIL += 1
        print(f'  ✗ LLM 未被调用或入参错误: calls={llm_calls} result={r}')

    # 闲聊不应触发 LLM
    llm_calls.clear()
    async def _driver2():
        data = twentyq_guess.groups[888]
        r = await twentyq_guess.process_message(888, 'u', 'n', '今天天气不错')
        return r
    r2 = asyncio.run(_driver2())
    if not llm_calls:
        PASS += 1
        print(f'  ✓ 闲聊未触发 LLM: kind={r2.get("kind")}')
    else:
        FAIL += 1
        print(f'  ✗ 闲聊不应触发 LLM: calls={llm_calls}')
finally:
    mod._llm_classify = orig_llm
    twentyq_guess.groups.pop(888, None)
    twentyq_guess._proc_locks.pop(888, None)

print()
print('=' * 70)
print(f'总计: {PASS} 通过, {FAIL} 失败')
print('=' * 70)
sys.exit(1 if FAIL else 0)
