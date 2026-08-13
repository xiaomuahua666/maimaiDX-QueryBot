"""你想我猜：「无法回答」三类分流测试。

验证用户规则：
1. 主观题（好听吗/难吗/燃吗/适合新手吗…）→ 回「没听懂」，绝不回「无数据」、
   不调 LLM、不消耗次数。
2. 客观无数据（发行销量/获奖…）→ 回「无已知数据比对，尝试换种问法」。
3. LLM 兜底调用失败（限流/超时/网络）→ 回「LLM出错，稍后重试」，绝不回「没听懂」。
4. 主观题判定 _is_subjective_question 不得误伤客观题（难度高吗/定数高吗/诈称谱吗…）。
"""

import asyncio
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import importlib
model_mod = importlib.import_module('libraries.maimaidx_model')

music_stub = types.ModuleType('libraries.maimaidx_music')
music_stub.Music = model_mod.Music


class _MaiStub:
    pass


music_stub.mai = _MaiStub()
music_stub.guess = object()
sys.modules['libraries.maimaidx_music'] = music_stub

from libraries.maimaidx_guess_20q import (  # noqa: E402
    Guess20QData,
    Guess20QManager,
    _YES,
    _NO,
    _LLM_ERROR,
    _LLM_ERROR_HINT,
    _SUBJECTIVE_HINT,
    _CANNOT_ANSWER,
    _is_subjective_question,
    classify_question,
)
from libraries.maimaidx_model import BasicInfo, Chart, Music  # noqa: E402
from collections import namedtuple

_passed = 0
_failed = 0


def _check(name, cond, extra=''):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f'  ✓ {name}')
    else:
        _failed += 1
        print(f'  ✗ {name}  {extra}')


def _make_music(title='PANDORA PARADOX', genre='舞萌'):
    notes = namedtuple('Notes', ['tap', 'hold', 'slide', 'brk'])(100, 10, 10, 5)
    bi = BasicInfo.model_validate({
        'title': title, 'artist': 'DECO*27', 'genre': genre, 'bpm': 180,
        'release_date': '', 'from': 'maimai でらっくす', 'is_new': True,
    })
    return Music(
        id='10044', title=title, type='SD', ds=[10.0, 12.0, 13.5, 14.6],
        level=['7', '10', '12+', '13+'], cids=[1, 2, 3, 4],
        charts=[Chart(notes=notes, charter='谱面-100号') for _ in range(4)],
        basic_info=bi,
    )


def _make_data():
    return Guess20QData(
        music=_make_music(), answers=['PANDORA PARADOX', 'pandora', '10044'],
        max_questions=20, duration=600, started_at=__import__('time').time(),
        question_count=0,
    )


def _run(data, text, mock_llm):
    """patch _llm_classify 与 config 后跑 process_message（带「我问」前缀）。"""
    import libraries.maimaidx_guess_20q as mod
    orig = mod._llm_classify
    mod._llm_classify = mock_llm

    class _Cfg:
        guess_20q_llm_enable = True
        b50_llm_key = 'fake'
        b50_llm_url = 'http://fake'
        b50_llm_model = 'fake'
    orig_cfg = mod._get_config
    mod._get_config = lambda: _Cfg()
    try:
        mgr = Guess20QManager()
        mgr.groups[12345] = data
        return asyncio.run(mgr.process_message(12345, 'u1', '玩家A', f'我问{text}'))
    finally:
        mod._llm_classify = orig
        mod._get_config = orig_cfg


def _mock_llm_fixed(resp):
    async def _m(music, text, config, **kwargs):
        if resp == '是':
            return _YES, 'mock'
        if resp == '否':
            return _NO, 'mock'
        if resp == '无法回答':
            # 真实 _llm_classify 在 LLM 回「无法回答」时返回 (标记, 文案) 元组，
            # 而非 None（None 表示 LLM 未启用/未调用）。
            return _CANNOT_ANSWER, '无已知数据比对，尝试换种问法'
        if isinstance(resp, tuple):
            return resp
        return None
    return _m


def _is_rule_hit(music, text):
    _, consumed, _ = classify_question(music, text)
    return consumed


# ══════════════════════════════════════════════════════════════
print('测试 A: 主观题判定 _is_subjective_question 不误伤客观题')

SUBJECTIVE = ['这歌好听吗', '紫谱难吗', '这歌燃吗', '适合新手吗', '这歌推荐吗',
              '觉得这谱难吗', '难不难', '这歌带感吗', '喜欢这首歌吗']
for q in SUBJECTIVE:
    _check(f'主观命中: {q}', _is_subjective_question(q))

OBJECTIVE_SAFE = ['难度高吗', '定数高吗', '是诈称谱吗', '体力谱吗', 'BPM高吗',
                  '发行销量高吗', '是动漫曲吗', '这歌时长超过2分钟吗', '紫谱定数高吗']
for q in OBJECTIVE_SAFE:
    _check(f'客观不误伤: {q}', not _is_subjective_question(q))

# ═══════════════════════════════════════════════════════════════
print('测试 B: 主观题 → 回「没听懂」，不调 LLM、不消耗次数')

for q in ['这歌好听吗', '紫谱难吗', '这歌燃吗', '适合新手吗']:
    data = _make_data()
    # mock 故意回「是」，若闸门失效会漏成「是」；闸门生效应回 _SUBJECTIVE_HINT
    r = _run(data, q, _mock_llm_fixed('是'))
    _check(f'主观回没听懂: {q}',
           r['kind'] == 'unknown' and r['answer'] == _SUBJECTIVE_HINT,
           f'got kind={r.get("kind")} answer={r.get("answer")!r}')
    _check(f'主观不消耗次数: {q}', data.question_count == 0, f'count={data.question_count}')

# ═══════════════════════════════════════════════════════════════
print('测试 C: 客观无数据 → 回「无已知数据比对，尝试换种问法」')

data = _make_data()
q = '这歌有间奏吗'
assert not _is_rule_hit(data.music, q), f'需规则不命中: {q}'
r = _run(data, q, _mock_llm_fixed('无法回答'))  # LLM 回无法回答
_subj = '没听懂' in r['answer']
_data = '无已知数据比对，尝试换种问法' in r['answer']
_check('客观无数据回「无已知数据比对」', r['kind'] == 'unknown' and _data and not _subj,
       f'got answer={r.get("answer")!r}')
_check('客观无数据不消耗次数', data.question_count == 0, f'count={data.question_count}')

# ═══════════════════════════════════════════════════════════════
print('测试 D: LLM 兜底调用失败 → 回「LLM出错，稍后重试」，绝不回「没听懂」')

data = _make_data()
q = '这歌有间奏吗'
assert not _is_rule_hit(data.music, q), f'需规则不命中: {q}'
r = _run(data, q, _mock_llm_fixed((_LLM_ERROR, _LLM_ERROR_HINT)))
_llm = r['answer'] == _LLM_ERROR_HINT
_no_subj = '没听懂' not in r['answer']
_no_data = '无已知数据比对' not in r['answer']
_check('LLM失败回「LLM出错，稍后重试」',
       r['kind'] == 'unknown' and _llm and _no_subj and _no_data,
       f'got answer={r.get("answer")!r}')
_check('LLM失败不消耗次数', data.question_count == 0, f'count={data.question_count}')

# ═══════════════════════════════════════════════════════════════
print('测试 E: 客观题仍正常走 LLM 作答（确认闸门不过度拦截）')

data = _make_data()
q = '这歌有间奏吗'
r = _run(data, q, _mock_llm_fixed('否'))
_check('客观题走 LLM 回「否」', r['kind'] == 'question' and r['answer'].startswith('不是'),
       f'got kind={r.get("kind")} answer={r.get("answer")!r}')
_check('客观题消耗次数', data.question_count == 1, f'count={data.question_count}')

# ═══════════════════════════════════════════════════════════════
print(f'\n结果：通过 {_passed} 项，失败 {_failed} 项')
sys.exit(1 if _failed else 0)
