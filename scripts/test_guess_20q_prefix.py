"""你想我猜（20 问）「我猜」前缀与两阶段猜曲名逻辑回归测试。

不依赖 NoneBot / 完整曲库：通过 sys.modules 注入轻量 stub 绕过重依赖，
直接构造 Guess20QData 实例驱动 process_message。
"""

import asyncio
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# ── 注入 libraries.maimaidx_music 的轻量 stub，避免触发 NoneBot 配置 ──
import importlib
model_mod = importlib.import_module('libraries.maimaidx_model')  # 仅依赖 pydantic，轻量

music_stub = types.ModuleType('libraries.maimaidx_music')
music_stub.Music = model_mod.Music


class _MaiStub:
    pass


class _GuessStub:
    pass


music_stub.mai = _MaiStub()
music_stub.guess = _GuessStub()
sys.modules['libraries.maimaidx_music'] = music_stub

# 现在可以安全导入被测模块
from libraries.maimaidx_guess_20q import (  # noqa: E402
    Guess20QData,
    Guess20QManager,
    TWENTYQ_MAX_QUESTIONS,
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


def _make_data(*, questions_used: int = 0, max_questions: int = 20) -> Guess20QData:
    music = _make_music()
    return Guess20QData(
        music=music,
        answers=['PANDORA PARADOX', 'pandora', '10044'],
        max_questions=max_questions,
        duration=600,
        started_at=__import__('time').time(),
        question_count=questions_used,
    )


def _process(data: Guess20QData, text: str) -> dict:
    mgr = Guess20QManager()
    # 直接把 data 塞进 manager 的 groups，绕过 start()
    mgr.groups[12345] = data
    return asyncio.run(mgr.process_message(12345, 'u1', '玩家A', text))


# ───────────────────── 测试用例 ─────────────────────

# 1. 没加前缀的消息一律忽略（视为群内正常聊天）
r = _process(_make_data(), '今天天气真好')
assert r['kind'] == 'idle', f'无前缀应忽略: {r}'

r = _process(_make_data(), '是不是动漫曲')
assert r['kind'] == 'idle', f'无前缀的问题也应忽略: {r}'

# 2. 只发「我问」/「我猜」没跟内容 -> 不理会（idle）
r = _process(_make_data(), '我问')
assert r['kind'] == 'idle', f'只有前缀无内容应忽略: {r}'

r = _process(_make_data(), '我问 ')
assert r['kind'] == 'idle', f'只有前缀加空格应忽略: {r}'

r = _process(_make_data(questions_used=TWENTYQ_MAX_QUESTIONS), '我猜')
assert r['kind'] == 'idle', f'猜曲名阶段只有前缀无内容应忽略: {r}'

# 3. 问问题阶段：用「我猜」前缀也能猜曲名（中途允许猜）
data = _make_data(questions_used=0)
r = _process(data, '我猜 PANDORA PARADOX')
assert r['kind'] == 'win', f'问问题阶段应允许猜曲名: {r}'
assert data.winner_uid == 'u1', f'应记录赢家: {r}'
assert data.end is True, f'猜对应结束游戏: {r}'

data = _make_data(questions_used=0)
r = _process(data, '我猜pandora')
assert r['kind'] == 'win', f'别名猜对应 win: {r}'

# 3b. 问问题阶段：用「我猜」猜错 -> wrong_guess，不结束游戏
data = _make_data(questions_used=0)
r = _process(data, '我猜 某不存在的曲名')
assert r['kind'] == 'wrong_guess', f'问问题阶段猜错应 wrong_guess: {r}'
assert data.end is False, f'猜错不应结束游戏: {r}'
assert data.question_count == 0, f'猜错不应消耗提问次数: {data.question_count}'

# 4. 问问题阶段：带「我问」前缀的正常问题 -> question
r = _process(_make_data(), '我问是不是动漫曲')
assert r['kind'] == 'question', f'正常问题应回答: {r}'

r = _process(_make_data(), '我问 谱师是谁')
# 谱师查询是信息题，可能返回 question 或 unknown，但不应是 idle
assert r['kind'] in ('question', 'unknown'), f'谱师查询应被处理: {r}'

# 5. 「我问」变体前缀：「我问问」「我问一下」都算
r = _process(_make_data(), '我问问是不是动漫曲')
assert r['kind'] == 'question', f'「我问问」前缀应识别: {r}'

r = _process(_make_data(), '我问一下是不是动漫曲')
assert r['kind'] == 'question', f'「我问一下」前缀应识别: {r}'

# 6. 问完阶段（question_count >= max_questions）：猜对曲名 -> win
data = _make_data(questions_used=TWENTYQ_MAX_QUESTIONS)
r = _process(data, '我猜 PANDORA PARADOX')
assert r['kind'] == 'win', f'问完阶段猜对应 win: {r}'
assert data.winner_uid == 'u1', f'应记录赢家: {r}'
assert data.end is True, f'猜对应结束游戏: {r}'

# 别名也算猜对
data = _make_data(questions_used=TWENTYQ_MAX_QUESTIONS)
r = _process(data, '我猜pandora')
assert r['kind'] == 'win', f'别名猜对应 win: {r}'

# 6b. 问完阶段：用「我问」前缀的消息忽略（提问机会已用完）
data = _make_data(questions_used=TWENTYQ_MAX_QUESTIONS)
r = _process(data, '我问 PANDORA PARADOX')
assert r['kind'] == 'idle', f'问完阶段「我问」前缀应忽略: {r}'

# 7. 问完阶段：猜错曲名 -> wrong_guess，且不结束游戏
data = _make_data(questions_used=TWENTYQ_MAX_QUESTIONS)
r = _process(data, '我猜 某不存在的曲名')
assert r['kind'] == 'wrong_guess', f'猜错应 wrong_guess: {r}'
assert r.get('guess') == '某不存在的曲名', f'应回显猜测内容: {r}'
assert data.end is False, f'猜错不应结束游戏: {r}'
assert data.winner_uid is None, f'猜错不应有赢家: {r}'

# 8. 问完阶段：猜错后再猜对 -> win
data = _make_data(questions_used=TWENTYQ_MAX_QUESTIONS)
r1 = _process(data, '我猜 错的曲名')
assert r1['kind'] == 'wrong_guess'
r2 = _process(data, '我猜 PANDORA PARADOX')
assert r2['kind'] == 'win', f'猜错后应允许继续猜对: {r2}'
assert data.end is True

# 9. 问完阶段：发非曲名内容（带「我猜」前缀）也视为猜错
data = _make_data(questions_used=TWENTYQ_MAX_QUESTIONS)
r = _process(data, '我猜 是不是动漫曲')
assert r['kind'] == 'wrong_guess', f'问完阶段「我猜」后非曲名应视为猜错: {r}'
assert data.end is False

# 10. 问完阶段：没加前缀的消息仍忽略
data = _make_data(questions_used=TWENTYQ_MAX_QUESTIONS)
r = _process(data, 'PANDORA PARADOX')
assert r['kind'] == 'idle', f'问完阶段无前缀仍忽略: {r}'

# 11. 提问计数推进：带「我问」前缀的问题应消耗次数
data = _make_data(questions_used=0)
before = data.question_count
_process(data, '我问是不是动漫曲')
assert data.question_count == before + 1, f'提问应消耗次数: {data.question_count}'

# 11b. 问问题阶段用「我猜」前缀不消耗提问次数（走猜曲名通道）
data = _make_data(questions_used=0)
before = data.question_count
_process(data, '我猜 某不存在的曲名')
assert data.question_count == before, f'「我猜」前缀不应消耗提问次数: {data.question_count}'

# 12. 不带前缀的消息不消耗次数
data = _make_data(questions_used=0)
before = data.question_count
_process(data, '是不是动漫曲')
assert data.question_count == before, f'无前缀不应消耗次数: {data.question_count}'

# 13. last 标记：刚好问完最后一题时 last=True
data = _make_data(questions_used=TWENTYQ_MAX_QUESTIONS - 1)
r = _process(data, '我问是不是动漫曲')
assert r['kind'] == 'question', r
assert r.get('last') is True, f'最后一题应标记 last: {r}'
assert data.question_count == TWENTYQ_MAX_QUESTIONS

# 14. reveal_text 末尾应包含曲 id
from libraries.maimaidx_guess_20q import Guess20QManager as _Mgr  # noqa: E402
_mgr = _Mgr()
_d = _make_data()
_text = _mgr.reveal_text(_d)
assert '🆔 曲 id' in _text, f'reveal_text 应包含曲 id: {_text}'
assert _d.music.id in _text, f'reveal_text 应包含具体 id 值: {_text}'

# ───────────────────── 难度颜色定数测试 ─────────────────────
# 测试数据 ds=[10.0, 12.0, 13.5, 14.6]（绿10/黄12/红13.5/紫14.6，无白谱）
from libraries.maimaidx_guess_20q import _q_ds, _resolve_diff_index, _YES, _NO, _UNKNOWN_HINT  # noqa: E402

# 15. 难度颜色解析：绿=0, 黄/橙=1, 红=2, 紫=3, 白=4
assert _resolve_diff_index('紫谱') == 3, '紫应映射到 MASTER(idx=3)'
assert _resolve_diff_index('红谱') == 2, '红应映射到 EXPERT(idx=2)'
assert _resolve_diff_index('黄谱') == 1, '黄应映射到 ADVANCED(idx=1)'
assert _resolve_diff_index('橙谱') == 1, '橙应映射到 ADVANCED(idx=1)'
assert _resolve_diff_index('绿谱') == 0, '绿应映射到 BASIC(idx=0)'
assert _resolve_diff_index('白谱') == 4, '白应映射到 Re:MASTER(idx=4)'
assert _resolve_diff_index('master') == 3, 'master 俗称应映射到紫'
assert _resolve_diff_index('remaster') == 4, 'remaster 俗称应映射到白'
assert _resolve_diff_index('basic') == 0, 'basic 俗称应映射到绿'
assert _resolve_diff_index('expert') == 2, 'expert 俗称应映射到红'
assert _resolve_diff_index('定数是13吗') is None, '无颜色应返回 None'

# 16. 指定颜色的定数判断：只看对应难度的 ds
_m = _make_music()
# 紫=14.6
assert _q_ds(_m, '紫谱定数是13吗') == _NO, '紫=14.6 不在[13,14) -> 不是'
assert _q_ds(_m, '紫谱是14+吗') == _YES, '紫=14.6 在[14.5,15) -> 是'
assert _q_ds(_m, '紫谱是14吗') == _NO, '紫=14.6 不在[14,15) -> 不是'
# 红=13.5
assert _q_ds(_m, '红谱定数是13吗') == _NO, '红=13.5 不在[13,14) -> 不是'
assert _q_ds(_m, '红谱是13+吗') == _YES, '红=13.5 在[13.5,14) -> 是'
# 黄=12
assert _q_ds(_m, '黄谱是12吗') == _YES, '黄=12 在[12,13) -> 是'
assert _q_ds(_m, '橙谱定数是13吗') == _NO, '橙(=黄)=12 不在[13,14) -> 不是'
# 绿=10
assert _q_ds(_m, '绿谱是10吗') == _YES, '绿=10 在[10,11) -> 是'
assert _q_ds(_m, '绿谱定数是13吗') == _NO, '绿=10 不是13 -> 不是'

# 17. 白谱不存在（ds 长度=4，无 Re:MASTER）-> 回「不是喵」
assert _q_ds(_m, '白谱定数是13吗') == _NO, '无白谱 -> 不是'
assert _q_ds(_m, '白谱是15吗') == _NO, '无白谱 -> 不是'

# 18. 不指定颜色时不回答定数（避免乱猜 max_ds），返回 None 走 unknown
assert _q_ds(_m, '定数是13吗') is None, '无颜色定数不应回答: 返回 None'
assert _q_ds(_m, '定数是14吗') is None, '无颜色定数不应回答: 返回 None'
assert _q_ds(_m, '定数是14+吗') is None, '无颜色定数不应回答: 返回 None'
assert _q_ds(_m, '最高定数是15吗') is None, '无颜色（含最高）也不应回答: 返回 None'

# 18b. _q_level_bare 裸数字（无颜色）也不回答
from libraries.maimaidx_guess_20q import _q_level_bare  # noqa: E402
assert _q_level_bare(_m, '是13吗') is None, '裸数字无颜色不回答'
assert _q_level_bare(_m, '14+吗') is None, '裸数字无颜色不回答'

# 19. classify_question 能正确路由紫谱定数问题到 _q_ds（不被 _q_song_type 拦截）
from libraries.maimaidx_guess_20q import classify_question  # noqa: E402
_ans, _consumed = classify_question(_m, '紫谱定数是13吗')
assert _consumed and _ans == _NO, f'紫谱13应回答不是（不应被谱面类型拦截）: {_ans}'
_ans2, _c2 = classify_question(_m, '紫谱是14+吗')
assert _c2 and _ans2 == _YES, f'紫谱14+应回答是: {_ans2}'
# 红谱=13.5，问13应回不是
_ans3, _c3 = classify_question(_m, '红谱是13吗')
assert _c3 and _ans3 == _NO, f'红谱13.5 不在[13,14) 应回不是: {_ans3}'
# 无颜色定数问题走 unknown（consumed=False，不消耗次数）
_ans4, _c4 = classify_question(_m, '定数是13吗')
assert _c4 is False, f'无颜色定数应走 unknown 不消耗次数: consumed={_c4}'

# 20. 谱面类型判断不受难度颜色干扰
from libraries.maimaidx_guess_20q import _q_song_type  # noqa: E402
assert _q_song_type(_m, '是dx谱吗') == _NO, 'SD 曲应回不是 DX'
assert _q_song_type(_m, '是标准谱吗') == _YES, 'SD 曲应回是 SD'
assert _q_song_type(_m, '紫谱定数是13吗') is None, '紫谱定数问题不应被谱面类型拦截'
assert _q_song_type(_m, '黄谱定数') is None, '黄谱定数问题不应被谱面类型拦截'

# 21. 信息题（直接问答案）不再回答——走 unknown，不消耗次数
from libraries.maimaidx_guess_20q import _q_charter  # noqa: E402
# 谱师是谁 -> 不报名字
assert _q_charter(_m, '谱师是谁') is None, '谱师是谁不应报名字（开户籍）'
assert _q_charter(_m, '谱师是沙发太吗') is not None, '谱师是非题应回答'
# classify_question 对信息题走 unknown
_a_info, _c_info = classify_question(_m, '谱师是谁')
assert _c_info is False, f'信息题应走 unknown 不消耗次数: consumed={_c_info}'
_a_info2, _c_info2 = classify_question(_m, 'bpm是多少')
assert _c_info2 is False, f'BPM 数值信息题应走 unknown: consumed={_c_info2}'
_a_info3, _c_info3 = classify_question(_m, '艺术家是谁')
assert _c_info3 is False, f'艺术家是谁信息题应走 unknown: consumed={_c_info3}'
_a_info4, _c_info4 = classify_question(_m, '什么版本')
assert _c_info4 is False, f'版本信息题应走 unknown: consumed={_c_info4}'

# ───────────────────── 白谱定数 handler 顺序回归 ─────────────────────
# 防止 _q_white_chart 拦截「白谱定数是X吗」类问题（历史 bug：
# 有白谱的曲问「白谱定数是13吗」会被误答为「是喵」，因为 _q_white_chart
# 只看有无白谱不看定数）。_q_white_chart 必须让给定数相关问题给 _q_ds。
from libraries.maimaidx_guess_20q import _q_white_chart  # noqa: E402


def _make_music_with_white(white_ds: float = 15.0) -> Music:
    """带白谱的曲（ds 5 个），白谱定数可调。"""
    notes = namedtuple('Notes', ['tap', 'hold', 'slide', 'brk'])(100, 10, 10, 5)
    bi = BasicInfo.model_validate({
        'title': 'TestWhite',
        'artist': 'X',
        'genre': '舞萌',
        'bpm': 180,
        'release_date': '',
        'from': 'maimai でらっくす',
        'is_new': True,
    })
    return Music(
        id='1',
        title='TestWhite',
        type='SD',
        ds=[10.0, 12.0, 13.5, 14.6, white_ds],
        level=['7', '10', '12+', '13+', '14+'],
        cids=[1, 2, 3, 4, 5],
        charts=[Chart(notes=notes, charter='C') for _ in range(5)],
        basic_info=bi,
    )


# 22. _q_white_chart 不应拦截「白谱定数是X吗」
_mw = _make_music_with_white(white_ds=15.0)  # 白谱=15
assert _q_white_chart(_mw, '白谱定数是13吗') is None, '白谱定数问题应让给 _q_ds'
assert _q_white_chart(_mw, '白谱是14+吗') is None, '白谱+数字问题应让给 _q_ds'
assert _q_white_chart(_mw, '白谱是13吗') is None, '白谱+数字问题应让给 _q_ds'
assert _q_white_chart(_mw, '白谱难吗') is None, '白谱难度形容词应让给 _q_ds'
assert _q_white_chart(_mw, '白谱高吗') is None, '白谱难度形容词应让给 _q_ds'
# 但纯「有无白谱」问题仍由 _q_white_chart 回答
assert _q_white_chart(_mw, '有白谱吗') == _YES, '有白谱应回是'
assert _q_white_chart(_mw, '是白谱吗') == _YES, '有白谱应回是'
assert _q_white_chart(_m, '有白谱吗') == _NO, '无白谱(_m ds=4)应回不是'

# 23. classify_question 实际路由：有白谱曲问「白谱定数是13吗」应走 _q_ds 回 _NO
#     （白谱=15 不在 [13,14)），不能被 _q_white_chart 误答为 _YES
_a_w, _c_w = classify_question(_mw, '白谱定数是13吗')
assert _c_w and _a_w == _NO, f'白谱=15 问13应回不是: {_a_w}（历史 bug 会回是）'
# 白谱=15.0 问「14+吗」-> 14+ 区间 [14.5,15.0)，15.0 不在区间 -> 不是
_a_w2, _c_w2 = classify_question(_mw, '白谱是14+吗')
assert _c_w2 and _a_w2 == _NO, f'白谱=15.0 不属14+应回不是: {_a_w2}'
_a_w3, _c_w3 = classify_question(_mw, '白谱定数是15吗')
assert _c_w3 and _a_w3 == _YES, f'白谱=15 问15应回是: {_a_w3}'
# 白谱=15.0，问「白谱定数大于14吗」应回是
_a_w4, _c_w4 = classify_question(_mw, '白谱定数大于14吗')
assert _c_w4 and _a_w4 == _YES, f'白谱=15 大于14应回是: {_a_w4}'

# 23b. 白谱=14.6 的曲问「14+吗」-> 14.6 在 [14.5,15.0) -> 是
_mw2 = _make_music_with_white(white_ds=14.6)
_a_w5, _c_w5 = classify_question(_mw2, '白谱是14+吗')
assert _c_w5 and _a_w5 == _YES, f'白谱=14.6 属14+应回是: {_a_w5}'

# 24. 无白谱曲（_m, ds=4）问「白谱定数是13吗」-> _q_ds 返回 _NO（前提不成立）
_a_nw, _c_nw = classify_question(_m, '白谱定数是13吗')
assert _c_nw and _a_nw == _NO, f'无白谱问白谱定数应回不是: {_a_nw}'

# 25. 有无白谱问题仍正常工作（不被 _q_ds 抢答）
_a_has, _c_has = classify_question(_mw, '有白谱吗')
assert _c_has and _a_has == _YES, f'有白谱应回是: {_a_has}'
_a_no, _c_no = classify_question(_m, '有白谱吗')
assert _c_no and _a_no == _NO, f'无白谱应回不是: {_a_no}'

# ───────────────────── _q_bpm 不抢答定数问题回归 ─────────────────────
# 防止 _q_bpm 的 fallback（大数字+比较词视为 BPM）抢答含颜色的定数问题。
# 历史 bug：BPM=180 紫谱=14.6，问「紫谱定数超过50吗」被 _q_bpm 用
# 180>50=True 误答「是」，正确应是定数 14.6>50=False 回「不是」。
from libraries.maimaidx_guess_20q import _q_bpm  # noqa: E402

# BPM=180，紫谱=14.6
_m_bpm = _make_music()  # _make_music 默认 ds=[10,12,13.5,14.6], bpm=180
# 含颜色+定数关键词+大数字+比较词，_q_bpm 应让给 _q_ds
assert _q_bpm(_m_bpm, '紫谱定数超过50吗') is None, '_q_bpm 不应抢答紫谱定数问题'
assert _q_bpm(_m_bpm, '紫谱定数大于100吗') is None, '_q_bpm 不应抢答紫谱定数问题'
assert _q_bpm(_m_bpm, '绿谱定数超过50吗') is None, '_q_bpm 不应抢答绿谱定数问题'
# 但纯 BPM 问题（无颜色无定数关键词）_q_bpm 仍回答
assert _q_bpm(_m_bpm, '超过50吗') == _YES, '纯 BPM 大数字问题应回答'
# 含 BPM 关键词的问题 _q_bpm 仍回答
assert _q_bpm(_m_bpm, 'bpm超过50吗') == _YES, '含 BPM 关键词应回答'

# classify_question 实际路由：定数问题走 _q_ds 不被 _q_bpm 拦截
_a_b1, _c_b1 = classify_question(_m_bpm, '紫谱定数超过50吗')
assert _c_b1 and _a_b1 == _NO, f'紫谱14.6 超过50应回不是: {_a_b1}（历史 bug 会回是）'
_a_b2, _c_b2 = classify_question(_m_bpm, '紫谱定数小于50吗')
assert _c_b2 and _a_b2 == _YES, f'紫谱14.6 小于50应回是: {_a_b2}'
_a_b3, _c_b3 = classify_question(_m_bpm, '紫谱定数大于100吗')
assert _c_b3 and _a_b3 == _NO, f'紫谱14.6 大于100应回不是: {_a_b3}'

# ───────────────────── 难度形容词 + _q_version 让出回归 ─────────────────────
# 防止 _q_version 的单字版本俗称（紫/白/桃/橙等）误匹配含颜色的定数/谱面问题。
# 历史 bug：「紫谱高吗」被 _q_version 的「紫」误判为问 murasaki 版本回「不是」。
from libraries.maimaidx_guess_20q import _q_version, _q_artist  # noqa: E402

# 26. 难度形容词定数问题：_q_ds 应识别「紫谱高吗」「紫谱难吗」
assert _q_ds(_m, '紫谱高吗') == _YES, '紫谱14.6>=13.5 应回是'
assert _q_ds(_m, '紫谱难吗') == _YES, '紫谱14.6>=13.5 应回是'
assert _q_ds(_m, '紫谱低吗') == _NO, '紫谱14.6>11 应回不是'
assert _q_ds(_m, '紫谱简单吗') == _NO, '紫谱14.6>11 应回不是'
assert _q_ds(_mw, '白谱难吗') == _YES, '白谱15>=13.5 应回是'
assert _q_ds(_mw, '白谱简单吗') == _NO, '白谱15>11 应回不是'

# 27. _q_version 含颜色+「谱」时让出（不误判为版本题）
assert _q_version(_m, '紫谱高吗') is None, '紫谱+谱 不应被版本题拦截'
assert _q_version(_m, '紫谱定数是13吗') is None, '紫谱+谱 不应被版本题拦截'
assert _q_version(_m, '白谱难吗') is None, '白谱+谱 不应被版本题拦截'
# 但纯版本问题（无「谱」字）_q_version 仍回答
assert _q_version(_m, '是紫代吗') is not None, '紫代版本问题应回答'
assert _q_version(_m, '是新框体吗') is not None, '新框体版本问题应回答'

# 28. classify_question 实际路由：「紫谱高吗」走 _q_ds 不被 _q_version 拦截
_a_v1, _c_v1 = classify_question(_m, '紫谱高吗')
assert _c_v1 and _a_v1 == _YES, f'紫谱高应回是（不应被版本题拦截）: {_a_v1}'
_a_v2, _c_v2 = classify_question(_mw, '白谱难吗')
assert _c_v2 and _a_v2 == _YES, f'白谱难应回是: {_a_v2}'

# ───────────────────── _q_charter 信息题 + _q_artist 单字符 回归 ─────────────────────
# 29. _q_charter 信息题检测扩展：含「谁」+「谱」也走 unknown（不消耗次数）
from libraries.maimaidx_guess_20q import _q_charter  # noqa: E402
assert _q_charter(_m, '紫谱是谁的谱') is None, '「紫谱是谁的谱」是信息题应走 unknown'
assert _q_charter(_m, '谁写的谱') is None, '「谁写的谱」是信息题应走 unknown'
assert _q_charter(_m, '谱师是谁') is None, '「谱师是谁」是信息题应走 unknown'
# 但是非题「谱师是X吗」仍正常回答
assert _q_charter(_m, '谱师是沙发太吗') is not None, '谱师是非题应回答'
# classify_question：「紫谱是谁的谱」不消耗次数（不被 _q_version 拦截回 _NO）
_a_ch, _c_ch = classify_question(_m, '紫谱是谁的谱')
assert _c_ch is False, f'「紫谱是谁的谱」应走 unknown 不消耗次数: consumed={_c_ch}'

# 30. _q_artist 单字符子串过宽防护
assert _q_artist(_m, '艺术家是d吗') is None, '单字符艺术家查询不应回答（过宽）'
assert _q_artist(_m, '艺术家是de吗') is not None, '2字符艺术家查询应回答'
# classify_question：单字符艺术家查询走 unknown
_a_a, _c_a = classify_question(_m, '艺术家是d吗')
assert _c_a is False, f'单字符艺术家查询应走 unknown: consumed={_c_a}'

# ───────────────────── _q_title_length 不开户籍回归 ─────────────────────
# 防止 _q_title_length 直接报字数（开户籍）。历史 bug：玩家问「标题几个字」
# bot 直接回「标题有 15 个字喵」，违反只回答是/否原则。
# _m.title = 'PANDORA PARADOX'（含空格共 15 字符）
from libraries.maimaidx_guess_20q import _q_title_length  # noqa: E402

# 31. 「几个字/多少字/多长」要求报字数 -> 走 unknown，不消耗次数，不报字数
assert _q_title_length(_m, '标题几个字') is None, '问字数应走 unknown 不报字数'
assert _q_title_length(_m, '标题多少字') is None, '问字数应走 unknown 不报字数'
assert _q_title_length(_m, '标题多长') is None, '问字数应走 unknown 不报字数'
_a_tl, _c_tl = classify_question(_m, '标题几个字')
assert _c_tl is False, f'问字数应走 unknown 不消耗次数: consumed={_c_tl}'
assert '15' not in _a_tl, f'不应在回答里报字数 15: {_a_tl[:40]}'

# 32. 是/否形式问字数应正常回答（_m.title=15 字符）
assert _q_title_length(_m, '标题是15个字吗') == _YES, '15字应回是'
assert _q_title_length(_m, '标题是16个字吗') == _NO, '15字问16应回不是'
assert _q_title_length(_m, '标题是10个字吗') == _NO, '15字问10应回不是'
_a_tl2, _c_tl2 = classify_question(_m, '标题是15个字吗')
assert _c_tl2 and _a_tl2 == _YES, f'15字应回是: {_a_tl2}'

# 33. 形容词问法仍回答是/否
assert _q_title_length(_m, '标题长吗') == _YES, '15字>=12 应回是'
assert _q_title_length(_m, '标题短吗') == _NO, '15字>5 应回不是'

# ───────────────────── 信息题不开户籍回归 ─────────────────────
# 所有「只能回答答案」的信息题应走 unknown，不消耗次数、不报答案值。
# 玩家应通过是非题形式获取信息，而非直接问「X 是谁/多少/什么」。
# _m: artist=DECO*27, bpm=180, version=でらっくす, genre=舞萌, charter=谱面-100号
_info_cases = [
    # (提问, 不应泄漏的答案值)
    ('谱师是谁', '谱面-100'),
    ('谱师是谁写的', '谱面-100'),
    ('是哪位谱师', '谱面-100'),
    ('曲师是谁', 'deco'),
    ('艺术家是谁', 'deco'),
    ('艺术家是哪位', 'deco'),
    ('artist是谁', 'deco'),
    ('作曲是谁', 'deco'),
    ('bpm是多少', None),
    ('bpm多大', None),  # 历史 bug: 被误答为「是喵」（把「大」当形容词）
    ('节奏是多少', None),
    ('速度是多少', None),
    ('是什么版本', 'でらっくす'),
    ('是什么代的', None),
    ('是哪一代', None),
    ('是什么分类', '舞萌'),
    ('是什么类型', '舞萌'),
    ('什么genre', '舞萌'),
    ('标题是什么', 'pandora'),
    ('曲名是什么', 'pandora'),
    ('歌名叫什么', 'pandora'),
    ('定数是多少', None),  # 无颜色也应走 unknown
    ('最高定数是多少', None),
    ('紫谱定数是多少', '14.6'),
]
for _q, _forbidden in _info_cases:
    _ans_i, _c_i = classify_question(_m, _q)
    assert _c_i is False, f'信息题「{_q}」应走 unknown 不消耗次数: consumed={_c_i}, ans={_ans_i[:30]}'
    if _forbidden:
        assert _forbidden.lower() not in _ans_i.lower() or _ans_i == _UNKNOWN_HINT, \
            f'信息题「{_q}」不应泄漏答案「{_forbidden}」: {_ans_i[:40]}'

# 34. 对照：是非题形式应正常回答（不被信息题过滤误伤）
assert classify_question(_m, 'bpm大于100吗')[1] is True, 'BPM 是非题应回答'
assert classify_question(_m, '艺术家是deco27吗')[1] is True, '艺术家是非题应回答'
assert classify_question(_m, '是舞代吗')[1] is True, '版本是非题应回答'
assert classify_question(_m, '有白谱吗')[1] is True, '有无白谱是非题应回答'

# ───────────────────── 「多X」问数值 vs 「X」是非题 回归 ─────────────────────
# 防止「BPM 多高」「紫谱多难」被当作「BPM 高吗」「紫谱 难吗」误答。
# 历史 bug：_q_bpm 把「多高」的「高」当形容词，_q_ds 同理。
from libraries.maimaidx_guess_20q import _q_bpm, _q_white_chart  # noqa: E402

# 35. BPM「多X」问数值走 unknown，「X吗」是非题正常回答
assert _q_bpm(_m, 'bpm多高') is None, '「bpm多高」问数值应走 unknown'
assert _q_bpm(_m, 'bpm多低') is None, '「bpm多低」问数值应走 unknown'
assert _q_bpm(_m, 'bpm多快') is None, '「bpm多快」问数值应走 unknown'
assert _q_bpm(_m, 'bpm多慢') is None, '「bpm多慢」问数值应走 unknown'
assert _q_bpm(_m, 'bpm高吗') == _YES, '「bpm高吗」是非题应回答'
assert _q_bpm(_m, 'bpm快吗') == _YES, '「bpm快吗」是非题应回答'
assert _q_bpm(_m, 'bpm低吗') == _NO, '「bpm低吗」是非题应回答'
assert _q_bpm(_m, 'bpm慢吗') == _NO, '「bpm慢吗」是非题应回答'

# 36. 定数「多X」问数值走 unknown，「X吗」是非题正常回答
assert _q_ds(_m, '紫谱多高') is None, '「紫谱多高」问数值应走 unknown'
assert _q_ds(_m, '紫谱多难') is None, '「紫谱多难」问数值应走 unknown'
assert _q_ds(_m, '紫谱多低') is None, '「紫谱多低」问数值应走 unknown'
assert _q_ds(_m, '紫谱高吗') == _YES, '「紫谱高吗」是非题应回答'
assert _q_ds(_m, '紫谱难吗') == _YES, '「紫谱难吗」是非题应回答'
assert _q_ds(_m, '紫谱低吗') == _NO, '「紫谱低吗」是非题应回答'

# 37. classify_question 实际路由：「多X」走 unknown 不消耗次数
_a_bmh, _c_bmh = classify_question(_m, 'bpm多高')
assert _c_bmh is False, f'「bpm多高」应走 unknown: consumed={_c_bmh}'
_a_dsh, _c_dsh = classify_question(_m, '紫谱多难')
assert _c_dsh is False, f'「紫谱多难」应走 unknown: consumed={_c_dsh}'

# ───────────────────── 艺术家符号归一化 + 只发实体名 回归 ─────────────────────
# 38. 艺术家名含符号（DECO*27）时，「deco27」应能匹配
# 历史 bug：子串匹配「deco27」不在「deco*27」里，错回「不是」
_a_art, _c_art = classify_question(_m, '艺术家是deco27吗')
assert _c_art and _a_art == _YES, f'艺术家是deco27应回是（符号归一化）: {_a_art}'

# 39. 只发实体名（无问句意图）走 unknown，不误答
# 历史 bug：「白谱」无问句被 _q_white_chart 误答为有无白谱
assert _q_white_chart(_m, '白谱') is None, '「白谱」无问句应走 unknown'
assert _q_white_chart(_m, '有白谱吗') == _NO, '「有白谱吗」应回答'
assert _q_white_chart(_m, '白谱吗') == _NO, '「白谱吗」应回答'
_a_wb, _c_wb = classify_question(_m, '白谱')
assert _c_wb is False, f'「白谱」无问句应走 unknown: consumed={_c_wb}'

# ───────────────────── 「大于等于」比较词 + 无效颜色定数 回归 ─────────────────────
# 40. 「大于等于」必须走 >= 而非 >（历史 bug：被「大于」先命中走严格 >）
# BPM=180：大于等于180 -> 是；大于180 -> 不是
from libraries.maimaidx_guess_20q import _q_bpm  # noqa: E402
assert _q_bpm(_m, 'bpm大于等于180吗') == _YES, 'BPM>=180 应回是'
assert _q_bpm(_m, 'bpm大于180吗') == _NO, 'BPM>180(严格) 应回不是'
assert _q_bpm(_m, 'bpm小于等于180吗') == _YES, 'BPM<=180 应回是'
# 紫谱=14.6：大于等于14.6 -> 是；大于14.6 -> 不是
assert _q_ds(_m, '紫谱定数大于等于14.6吗') == _YES, '紫谱>=14.6 应回是'
assert _q_ds(_m, '紫谱定数大于14.6吗') == _NO, '紫谱>14.6(严格) 应回不是'

# 41. 无效难度颜色（粉谱）的定数问题走 unknown，不被 _q_version 抢答
# 历史 bug：「粉谱定数是10吗」被 _q_version 的「粉」(pink版本) 误判为版本题
_a_pink, _c_pink = classify_question(_m, '粉谱定数是10吗')
assert _c_pink is False, f'「粉谱定数」无效颜色应走 unknown: consumed={_c_pink}'
# 对照：有效颜色定数仍正常回答
assert classify_question(_m, '紫谱定数是14吗')[1] is True

# 42. 否定反转正常工作（在 process_message 经 _apply_negation 调用）
from libraries.maimaidx_guess_20q import _apply_negation  # noqa: E402
# 不是动漫曲：是舞萌不是动漫 -> 原回「不是」，反转后「是」
_a_neg1, _ = classify_question(_m, '不是动漫曲吗')
assert _apply_negation('不是动漫曲吗', _a_neg1) == _YES, f'否定反转应回是: {_a_neg1}'
# 不是舞萌：是舞萌 -> 原回「是」，反转后「不是」
_a_neg2, _ = classify_question(_m, '不是舞萌吗')
assert _apply_negation('不是舞萌吗', _a_neg2) == _NO, f'否定反转应回不是: {_a_neg2}'
# 无白谱：无白谱 -> 原回「不是」，反转后「是」
_a_neg3, _ = classify_question(_m, '无白谱吗')
assert _apply_negation('无白谱吗', _a_neg3) == _YES, f'否定反转应回是: {_a_neg3}'

print('guess 20q prefix & two-phase tests passed')
