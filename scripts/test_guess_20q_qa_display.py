"""你想我猜「已有信息」展示与选择语义规则测试。

验证：
1. _qa_display_info 优先用 AI 判定维度（reason），去掉「判定维度：」前缀；
   reason 缺失时回退到精简后的玩家原话
2. _summarize_qa 用 reason 展示，不复述玩家原话
3. QAEntry 新增 reason 字段，_respond 存 reason
4. LLM 提示词含选择语义规则（「或/还是」连接多对象 → 无法回答）
5. 选择问句不被规则命中（走 LLM 兜底）
6. 端到端：选择问句走 LLM 回「无法回答」不消耗次数；
   规则命中题走 _respond 后 qa.reason 被正确存入
"""

import asyncio
import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# ── 注入 libraries.maimaidx_music 的轻量 stub，避免触发 NoneBot 配置 ──
import importlib
model_mod = importlib.import_module('libraries.maimaidx_model')

music_stub = types.ModuleType('libraries.maimaidx_music')
music_stub.Music = model_mod.Music


class _MaiStub:
    pass


class _GuessStub:
    pass


music_stub.mai = _MaiStub()
music_stub.guess = _GuessStub()
sys.modules['libraries.maimaidx_music'] = music_stub

from libraries.maimaidx_guess_20q import (  # noqa: E402
    QAEntry,
    Guess20QData,
    Guess20QManager,
    _GUESS_20Q_LLM_SYSTEM,
    _qa_display_info,
    _summarize_qa,
    classify_question,
    _is_choice_question,
    _YES,
    _NO,
)
from libraries.maimaidx_model import BasicInfo, Chart, Music  # noqa: E402
from collections import namedtuple


def _make_music(title: str = 'PANDORA PARADOX', genre: str = '舞萌') -> Music:
    notes = namedtuple('Notes', ['tap', 'hold', 'slide', 'brk'])(100, 10, 10, 5)
    basic_info = BasicInfo.model_validate({
        'title': title,
        'artist': 'DECO*27',
        'genre': genre,
        'bpm': 180,
        'release_date': '',
        'from': 'maimai でらっくす',
        'is_new': True,
    })
    return Music(
        id='10044',
        title=title,
        type='SD',
        ds=[10.0, 12.0, 13.5, 14.6],
        level=['7', '10', '12+', '13+'],
        cids=[1, 2, 3, 4],
        charts=[
            Chart(notes=notes, charter='谱面-100号'),
            Chart(notes=notes, charter='谱面-100号'),
            Chart(notes=notes, charter='谱面-100号'),
            Chart(notes=notes, charter='谱面-100号'),
        ],
        basic_info=basic_info,
    )


def _make_data() -> Guess20QData:
    return Guess20QData(
        music=_make_music(),
        answers=['PANDORA PARADOX', 'pandora', '10044'],
        max_questions=20,
        duration=600,
        started_at=time.time(),
        question_count=0,
    )


# ═══════════════════ 1. _qa_display_info 单元测试 ═══════════════════

# reason 存在 → 去掉「判定维度：」前缀
e1 = QAEntry(uid='u1', name='A', question='是檄代吗', answer='否喵 ❌',
             at=time.time(), reason='判定维度：版本是否为檄代')
assert _qa_display_info(e1) == '版本是否为檄代', \
    f'reason 应去前缀: {_qa_display_info(e1)!r}'

# 全角冒号前缀也去掉
e1b = QAEntry(uid='u1', name='A', question='x', answer='是',
              at=time.time(), reason='判定维度:谱面类型是否为 DX 谱面')
assert _qa_display_info(e1b) == '谱面类型是否为 DX 谱面', \
    f'半角冒号前缀也应去掉: {_qa_display_info(e1b)!r}'

# reason 缺失 → 回退到精简后的玩家原话
e2 = QAEntry(uid='u1', name='A', question='是超代或檄代吗吗？',
             answer='无法回答', at=time.time(), reason='')
assert _qa_display_info(e2) == '是超代或檄代', \
    f'reason 缺失应回退到精简原话: {_qa_display_info(e2)!r}'

# reason 只有「判定维度：」前缀无内容 → 回退到精简原话（去掉「吗」「？」）
e3 = QAEntry(uid='u1', name='A', question='是熊代吗？', answer='是喵 ✅',
             at=time.time(), reason='判定维度：')
assert _qa_display_info(e3) == '是熊代', \
    f'reason 仅有前缀应回退: {_qa_display_info(e3)!r}'

# LLM understand（无「判定维度：」前缀）原样使用
e4 = QAEntry(uid='u1', name='A', question='不是慢歌吗', answer='是喵 ✅',
             at=time.time(), reason='判断 BPM 是否为慢歌')
assert _qa_display_info(e4) == '判断 BPM 是否为慢歌'

print('_qa_display_info unit tests passed')


# ═══════════════════ 2. _summarize_qa 用 reason 展示 ═══════════════════

qa_list = [
    QAEntry(uid='u1', name='玩家A', question='是檄代吗', answer='否喵 ❌',
            at=time.time(), reason='判定维度：版本是否为檄代'),
    QAEntry(uid='u2', name='玩家B', question='不是动漫曲吗', answer='是喵 ✅',
            at=time.time(), reason='检测到否定提问，已按语义反转（判定维度：分类是否为 POPS&ANIME）'),
    QAEntry(uid='u3', name='玩家C', question='有白谱吗？', answer='否喵 ❌',
            at=time.time(), reason='判定维度：是否有白谱（Re:MASTER 难度）'),
]
summary = _summarize_qa(qa_list)
assert '版本是否为檄代' in summary, f'summary 应含 reason: {summary}'
assert '是否有白谱' in summary
# 不应直接复述玩家原话「是檄代吗」「有白谱吗？」
assert '· 是檄代吗' not in summary, f'summary 不应用玩家原话: {summary}'
assert '· 有白谱吗' not in summary
assert '已确认信息（3 次）' in summary
# 空列表
assert _summarize_qa([]) == ''
print('_summarize_qa tests passed')


# ═══════════════════ 3. LLM 提示词含选择语义规则 ═══════════════════

assert '选择问句' in _GUESS_20Q_LLM_SYSTEM, '提示词应含选择问句规则'
assert '或' in _GUESS_20Q_LLM_SYSTEM and '还是' in _GUESS_20Q_LLM_SYSTEM
assert '一次只问一个' in _GUESS_20Q_LLM_SYSTEM or '分开提问' in _GUESS_20Q_LLM_SYSTEM
# 「或更晚/或更早」等版本顺序方向词不算选择问句
assert '或更晚' in _GUESS_20Q_LLM_SYSTEM
print('LLM prompt choice-semantics rule tests passed')


# ═══════════════════ 4. 选择问句不被规则命中（走 LLM） ═══════════════════

# _is_choice_question 单元测试
assert _is_choice_question('是超代或檄代吗') is True
assert _is_choice_question('是动漫曲还是游戏曲吗') is True
assert _is_choice_question('是 SD 还是 DX 谱') is True
assert _is_choice_question('是檄代吗') is False
assert _is_choice_question('是檄代或更晚吗') is False, '「或更晚」是顺序方向词，不算选择问句'
assert _is_choice_question('是檄代或更早吗') is False, '「或更早」是顺序方向词，不算选择问句'
assert _is_choice_question('有白谱吗') is False
assert _is_choice_question('是熊代吗') is False
print('_is_choice_question unit tests passed')

music = _make_music()
# 选择问句 → 规则不命中 → 走 LLM
for choice_q in ('是超代或檄代吗', '是动漫曲还是游戏曲吗', '是 SD 还是 DX 谱'):
    _, consumed, _ = classify_question(music, choice_q)
    assert consumed is False, f'选择问句 {choice_q!r} 不应被规则命中'

# 对照：非选择问句「是檄代吗」应被规则命中
_, consumed_geki, reason_geki = classify_question(music, '是檄代吗')
assert consumed_geki is True, '非选择问句「是檄代吗」应被规则命中'
assert '檄代' in reason_geki
# 「或更晚」是版本顺序方向词，应被 _q_version_order 命中（不算选择问句）
_, consumed_order, _ = classify_question(music, '是檄代或更晚吗')
assert consumed_order is True, '「或更晚」是顺序方向词，应被规则命中'
# 「或更早」同理
_, consumed_order2, _ = classify_question(music, '是檄代或更早吗')
assert consumed_order2 is True, '「或更早」是顺序方向词，应被规则命中'
print('choice-question rule-bypass tests passed')


# ═══════════════════ 5. 端到端：选择问句走 LLM 回「无法回答」不消耗次数 ═══════════════════
# ═══════════════════    规则命中题 _respond 存 reason ═══════════════════

def _make_mock_llm(response_map: dict, default: str = '无法回答'):
    """response_map: {问题文本(归一化): '是'|'否'|'无法回答'}"""
    async def _mock(music, text, config):
        key = text.strip().lower().replace(' ', '')
        resp = response_map.get(key, default)
        if resp == '是':
            return _YES, 'mock 判定维度'
        if resp == '否':
            return _NO, 'mock 判定维度'
        return None  # 无法回答
    return _mock


def _run(data, text, mock_llm):
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


# 场景 A：选择问句「是超代或檄代吗」→ LLM 回无法回答 → 不消耗次数
data_a = _make_data()
mock_llm = _make_mock_llm({}, default='无法回答')
result_a = _run(data_a, '是超代或檄代吗', mock_llm)
assert result_a['kind'] == 'unknown', f'选择问句应回 unknown: {result_a}'
assert data_a.question_count == 0, f'选择问句不应消耗次数: {data_a.question_count}'
assert len(data_a.qa) == 0, '选择问句不应记录 QA'
print('choice-question unanswerable end-to-end tests passed')

# 场景 B：规则命中题「是檄代吗」→ _respond 存 reason → 已有信息用 reason 展示
data_b = _make_data()
mock_llm_b = _make_mock_llm({}, default='无法回答')
result_b = _run(data_b, '是檄代吗', mock_llm_b)
assert result_b['kind'] == 'question', f'规则题应回 question: {result_b}'
assert data_b.question_count == 1
assert len(data_b.qa) == 1
qa_entry = data_b.qa[0]
assert qa_entry.reason, f'规则命中题应存 reason: {qa_entry}'
assert '檄代' in qa_entry.reason
# 已有信息展示用 reason（去掉前缀），不复述玩家原话
info = _qa_display_info(qa_entry)
assert '檄代' in info
assert '判定维度：' not in info, f'展示应去掉前缀: {info}'
# 玩家原话不应出现在展示中
assert info != '是檄代吗', f'展示不应是玩家原话: {info}'
print('rule-hit reason storage & display tests passed')

# 场景 C：LLM 兜底命中题 → _respond 存 LLM understand 作为 reason
# 「不是中文歌吗」是标题语种题，规则不命中 → 走 LLM
data_c = _make_data()
# 先确认规则不命中
_, consumed_c0, _ = classify_question(_make_music(), '不是中文歌吗')
assert consumed_c0 is False, '「不是中文歌吗」应不被规则命中（走 LLM）'
mock_llm_c = _make_mock_llm({'不是中文歌吗': '是'}, default='无法回答')
result_c = _run(data_c, '不是中文歌吗', mock_llm_c)
assert result_c['kind'] == 'question', f'LLM 命中应回 question: {result_c}'
assert data_c.question_count == 1
qa_entry_c = data_c.qa[0]
assert qa_entry_c.reason == 'mock 判定维度', \
    f'LLM 题应存 understand 作为 reason: {qa_entry_c}'
assert _qa_display_info(qa_entry_c) == 'mock 判定维度'
print('LLM-hit reason storage & display tests passed')

# 场景 D：混合场景 — 选择问句（不消耗）+ 规则题（消耗）→ 已有信息只含规则题
data_d = _make_data()
mock_llm_d = _make_mock_llm({}, default='无法回答')
# 选择问句：无法回答，不消耗
_run(data_d, '是超代或檄代吗', mock_llm_d)
assert data_d.question_count == 0
# 规则题：消耗 + 存 reason
_run(data_d, '是檄代吗', mock_llm_d)
assert data_d.question_count == 1
# 再来一个规则题
_run(data_d, '有白谱吗', mock_llm_d)
assert data_d.question_count == 2
assert len(data_d.qa) == 2
summary_d = _summarize_qa(data_d.qa)
assert '檄代' in summary_d
assert '白谱' in summary_d
# 选择问句未被记录
assert '超代或檄代' not in summary_d, f'选择问句不应出现在已有信息: {summary_d}'
print('mixed scenario tests passed')

print('\n===== all qa_display & choice-semantics tests passed =====')
