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

# 1) 玩家问「是舞萌吗」→ 含「舞萌」字样，规则层不抢答，交给 AI 判断
#    （「舞萌」有歧义：分类? 版本? 游戏归属? 规则层无法消歧）
ans, consumed, reason = classify_question(m_nico, '是舞萌吗')
assert consumed is False, (
    f'「是舞萌吗」含舞萌字样，规则层不应抢答，但 consumed={consumed}'
)
print(f'✓ 核心case：分类=niconico 问「是舞萌吗」→ 规则层不抢答（交 AI 判断）')

# 2) 反例：分类=maimai（中文"舞萌"）的曲目，问「是舞萌吗」→ 同样交 AI
m_mai_cn = _make_genre_music('舞萌')
ans, consumed, reason = classify_question(m_mai_cn, '是舞萌吗')
assert consumed is False, (
    f'分类=舞萌(中文) 问「是舞萌吗」含舞萌字样，规则层不应抢答，实际 consumed={consumed}'
)
print(f'✓ 反例：分类=舞萌(中文) 问「是舞萌吗」→ 规则层不抢答（交 AI 判断）')

# 2b) 反例：分类=maimai（英文）的曲目，问「是舞萌吗」→ 同样交 AI
m_mai = _make_genre_music('maimai')
ans, consumed, reason = classify_question(m_mai, '是舞萌吗')
assert consumed is False, (
    f'分类=maimai 问「是舞萌吗」含舞萌字样，规则层不应抢答，实际 consumed={consumed}'
)
print(f'✓ 反例：分类=maimai 问「是舞萌吗」→ 规则层不抢答（交 AI 判断）')

# 3) 「是舞萌曲吗」「是舞萌原创吗」「是舞萌分类吗」「是舞萌DX某年代吗」均含舞萌，交 AI
for q in ('是舞萌曲吗', '是舞萌原创吗', '是舞萌分类吗', '是舞萌DX BUDDiES代吗', '是舞萌游戏的曲吗'):
    _a, _c, _r = classify_question(m_nico, q)
    assert _c is False, f'「{q}」含舞萌字样，规则层不应抢答，consumed={_c}'
print(f'✓ 「舞萌曲/舞萌原创/舞萌分类/舞萌DX年代/舞萌游戏」均交 AI 判断')

# 4) 「是原创曲吗」「是本家曲吗」「是委约曲吗」不含「舞萌」，仍走规则层
for q in ('是原创曲吗', '是本家曲吗', '是委约曲吗'):
    _a, _c, _r = classify_question(m_nico, q)
    assert _c is True and _a == _NO, f'分类=niconico 问「{q}」应走规则层并回否：{_a} consumed={_c}'
print(f'✓ 「原创曲/本家曲/委约曲」不含舞萌，仍走规则层且正确回否')

# 5) 「是舞代吗」是版本题（舞代=旧框），不含「舞萌」，走版本规则
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

# 7) 否定句：「不是舞萌吗」含舞萌，同样交 AI（规则层不抢答）
_a, _c, _r = classify_question(m_nico, '不是舞萌吗')
assert _c is False, f'「不是舞萌吗」含舞萌字样，规则层不应抢答：{_c}'
print(f'✓ 否定句「不是舞萌吗」含舞萌 → 交 AI 判断')

# 8) LLM 提示词保留「舞萌」歧义消解规则（三种含义：分类/版本/游戏归属）
assert '最高优先级' in mod._GUESS_20Q_LLM_SYSTEM, '系统提示词应含「最高优先级」舞萌歧义消解'
assert '舞萌' in mod._GUESS_20Q_LLM_SYSTEM, '系统提示词应含「舞萌」关键词'
assert '所有曲都是舞萌DX' in mod._GUESS_20Q_LLM_SYSTEM, '系统提示词应禁止回答游戏归属'
# 新增：版本/年份消歧 + 消歧原则
assert '版本/年份是非题' in mod._GUESS_20Q_LLM_SYSTEM, '应含版本/年份消歧规则'
assert '消歧原则' in mod._GUESS_20Q_LLM_SYSTEM, '应含消歧原则'
# 年份↔版本速查表（覆盖「舞萌DX 2024 年」类问题）
assert '年份↔版本速查' in mod._GUESS_20Q_LLM_SYSTEM, '应含年份↔版本速查表'
assert '2024=宴代' in mod._GUESS_20Q_LLM_SYSTEM, '年份速查应含 2024=宴代'
assert '2023=祝代' in mod._GUESS_20Q_LLM_SYSTEM, '年份速查应含 2023=祝代'
assert '2026=圈+' in mod._GUESS_20Q_LLM_SYSTEM, '年份速查应含 2026=圈+'
# 修正：华代是 2020 年（2020-01 发售），不是 2021 年
assert '2020=华代' in mod._GUESS_20Q_LLM_SYSTEM, '华代应归 2020 年（2020-01 发售）'
assert '2021=煌代' in mod._GUESS_20Q_LLM_SYSTEM, '2021 年应是煌代/宙代（不含华代）'
# 修正：prism plus 主俗称是「彩代」，不是「镜+」
assert '2025=彩代' in mod._GUESS_20Q_LLM_SYSTEM, 'prism plus 主俗称是彩代'
# 简写形式说明（「dx2026」「舞萌2026」等无「年/代」字的写法）
assert 'dx2026' in mod._GUESS_20Q_LLM_SYSTEM, '应含简写形式 dx2026 说明'
# 年份指版本发售年，不是 release_date（防「鲁迅不是周树人」式误判）
assert '版本发售年' in mod._GUESS_20Q_LLM_SYSTEM, '应说明年份指版本发售年非 release_date'
# 通用版本规则（第 7 条）也含年份速查，覆盖无「舞萌」字样的年份题
assert mod._GUESS_20Q_LLM_SYSTEM.count('年份↔版本速查') >= 2, '年份速查应在舞萌消歧和通用版本规则各出现一次'
# 「其他游戏」分类映射
assert '其他游戏' in mod._GUESS_20Q_LLM_SYSTEM, '应含「其他游戏」→ GAME&VARIETY 映射'
print(f'✓ LLM 提示词含「舞萌」三义消解规则（分类/版本年份/游戏归属）+ 年份速查 + 其他游戏分类')

# 9) 其他分类曲目问「是舞萌吗」→ 含舞萌，均交 AI（不再走规则层）
for genre in ('東方Project', 'POPS&ANIME', 'GAME&VARIETY', 'オンゲキ＆CHUNITHM', '宴会場', '舞萌'):
    m_other = _make_genre_music(genre)
    _a, _c, _r = classify_question(m_other, '是舞萌吗')
    assert _c is False, f'分类={genre} 问「是舞萌吗」含舞萌应交 AI：consumed={_c}'
print(f'✓ 所有分类问「是舞萌吗」→ 交 AI 判断')

# 10) _genre_key 中文匹配修复：genre=舞萌(中文) 应映射到 maimai
assert mod._genre_key(m_mai_cn) == 'maimai', 'genre=舞萌(中文) 应映射到 maimai'
assert mod._genre_key(m_mai) == 'maimai', 'genre=maimai(英文) 应映射到 maimai'
print(f'✓ _genre_key 中英文 maimai 匹配均正确')

print()
print('all maimai disambiguation tests passed')
