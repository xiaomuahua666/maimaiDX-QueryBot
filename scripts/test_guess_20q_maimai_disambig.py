"""20问：「舞萌」一词的歧义消解回归测试。

核心 case：分类=niconico & VOCALOID 的曲目，玩家问「是舞萌吗」必须判定为「否」，
因为「舞萌」在分类是非题里指分类=maimai（原创曲），不是指游戏归属。

双管齐下：
1. 规则层：_GENRE_KEYWORDS 的 maimai 行含「舞萌」关键词，规则层直接命中判断分类字段。
2. LLM 提示词：保留【最高优先级】舞萌歧义消解规则，万一规则层没覆盖到的变体走 LLM 也能正确判断。

不依赖 NoneBot/完整曲库；复用 test_guess_20q_prefix 的 stub 机制。
"""
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import importlib  # noqa: E402
model_mod = importlib.import_module('libraries.maimaidx_model')
music_stub = types.ModuleType('libraries.maimaidx_music')


class _AliasList(list):
    def by_alias(self, music_alias):
        return []


class _MaiStub:
    total_alias_list = _AliasList()


music_stub.mai = _MaiStub()
music_stub.guess = types.SimpleNamespace()
music_stub.Music = model_mod.Music
sys.modules['libraries.maimaidx_music'] = music_stub

import libraries.maimaidx_guess_20q as mod  # noqa: E402
from libraries.maimaidx_guess_20q import classify_question, _YES, _NO  # noqa: E402
from libraries.maimaidx_model import BasicInfo, Chart, Music  # noqa: E402
from collections import namedtuple  # noqa: E402


def _make_genre_music(genre, version='maimai でらっくす buddies'):
    notes = namedtuple('N', ['tap', 'hold', 'slide', 'brk'])(1, 1, 1, 1)
    bi = BasicInfo.model_validate({
        'title': 'T', 'artist': 'ピノキオピー', 'genre': genre, 'bpm': 150,
        'release_date': '', 'from': version, 'is_new': False,
        'version': version,
    })
    return Music(
        id='11559', title='魔法少女とチョコレゐト', type='SD',
        ds=[10.0, 12.0, 13.5, 13.5], level=['7', '10', '12+', '13'],
        cids=[1, 2, 3, 4],
        charts=[Chart(notes=notes, charter='サファ太') for _ in range(4)],
        basic_info=bi,
    )


# ── 案例曲目：分类=niconico & VOCALOID（即用户报告的 bug 曲目）──
m_nico = _make_genre_music('niconico & VOCALOID')

# 1) 玩家问「是舞萌吗」→ 规则层命中（舞萌在 _GENRE_KEYWORDS 的 maimai 行）
#    分类=niconico ≠ maimai → 必须回「否」。这是用户报告的 bug，核心断言。
ans, consumed, reason = classify_question(m_nico, '是舞萌吗')
assert consumed is True, (
    f'「是舞萌吗」应被规则层命中（舞萌关键词在 maimai 行），但 consumed={consumed}'
)
assert ans == _NO, (
    f'分类=niconico 问「是舞萌吗」必须回「否」（不是 maimai 分类），实际 {ans}'
)
assert 'maimai' in reason or '原创' in reason, (
    f'reason 应说明判定维度为 maimai 分类：{reason!r}'
)
# reason 不能泄露真实分类
assert 'niconico' not in reason and 'VOCALOID' not in reason.lower(), (
    f'reason 不应泄露真实分类：{reason!r}'
)
print(f'✓ 核心case：分类=niconico 问「是舞萌吗」→ 否（规则层命中），reason={reason!r}')

# 2) 反例：分类=maimai 的曲目，问「是舞萌吗」→ 回「是」
m_mai = _make_genre_music('maimai')
ans, consumed, reason = classify_question(m_mai, '是舞萌吗')
assert consumed is True and ans == _YES, (
    f'分类=maimai 问「是舞萌吗」应回「是」（规则层命中），实际 {ans} consumed={consumed}'
)
print(f'✓ 反例：分类=maimai 问「是舞萌吗」→ 是（规则层命中）')

# 3) 「是舞萌曲吗」「是舞萌原创吗」「是舞萌分类吗」同样走规则层
for q in ('是舞萌曲吗', '是舞萌原创吗', '是舞萌分类吗'):
    _a, _c, _r = classify_question(m_nico, q)
    assert _c is True, f'「{q}」应被规则层命中，consumed={_c}'
    assert _a == _NO, f'分类=niconico 问「{q}」应回否，实际 {_a}'
print(f'✓ 「舞萌曲/舞萌原创/舞萌分类」均规则层命中且正确回否')

# 4) 「是原创曲吗」「是本家曲吗」「是委约曲吗」同样走规则层
for q in ('是原创曲吗', '是本家曲吗', '是委约曲吗'):
    _a, _c, _r = classify_question(m_nico, q)
    assert _c is True and _a == _NO, f'分类=niconico 问「{q}」应回否：{_a} consumed={_c}'
print(f'✓ 「原创曲/本家曲/委约曲」均规则层命中且正确回否')

# 5) 「是舞代吗」是版本题（舞代=旧框），不是分类题，走版本规则
#    m_nico=buddies（新框），舞代=旧框 → 不是
_a, _c, _r = classify_question(m_nico, '是舞代吗')
assert _c is True, f'「是舞代吗」应走版本规则'
assert _a == _NO, f'buddies 不是舞代（旧框），应回否：{_a}'
print(f'✓ 「是舞代吗」走版本规则，buddies 不是舞代')

# 6) 旧框曲问「是舞代吗」→ 是
m_finale = _make_genre_music('niconico & VOCALOID', version='maimai finale')
_a, _c, _r = classify_question(m_finale, '是舞代吗')
assert _c is True and _a == _YES, f'finale 是舞代（旧框）：{_a}'
print(f'✓ finale 问「是舞代吗」→ 是')

# 7) 否定句：「不是舞萌吗」分类=niconico → 不是 maimai → 「不是舞萌」为真 → 反转回是
#    注：_apply_negation 在 classify_question 之外，这里只测原始判定
_a, _c, _r = classify_question(m_nico, '不是舞萌吗')
assert _c is True and _a == _NO, f'分类=niconico 「不是舞萌吗」原始判定应回否（不是maimai）：{_a}'
print(f'✓ 否定句原始判定：分类=niconico 「不是舞萌吗」→ 否（_apply_negation 会反转）')

# 8) LLM 提示词仍保留「舞萌」歧义消解规则（双管齐下，防规则层遗漏的变体）
assert '最高优先级' in mod._GUESS_20Q_LLM_SYSTEM, '系统提示词应含「最高优先级」舞萌歧义消解'
assert '舞萌' in mod._GUESS_20Q_LLM_SYSTEM, '系统提示词应含「舞萌」关键词'
assert '所有曲都是舞萌DX' in mod._GUESS_20Q_LLM_SYSTEM, '系统提示词应禁止回答游戏归属'
print(f'✓ LLM 提示词保留「舞萌」歧义消解规则（双管齐下）')

# 9) 其他分类曲目问「是舞萌吗」→ 否（非 maimai 分类）
for genre in ('東方Project', 'POPS&ANIME', 'GAME&VARIETY', 'オンゲキ＆CHUNITHM', '宴会場'):
    m_other = _make_genre_music(genre)
    _a, _c, _r = classify_question(m_other, '是舞萌吗')
    assert _c is True and _a == _NO, f'分类={genre} 问「是舞萌吗」应回否：{_a} consumed={_c}'
print(f'✓ 东方/pops/game/音击/宴会 分类问「是舞萌吗」→ 否')

print()
print('all maimai disambiguation tests passed')
