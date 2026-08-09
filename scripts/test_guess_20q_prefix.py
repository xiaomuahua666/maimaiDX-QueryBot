"""你想我猜（20 问）「我猜」前缀与两阶段猜曲名逻辑回归测试。

不依赖 NoneBot / 完整曲库：通过 sys.modules 注入轻量 stub 绕过重依赖，
直接构造 Guess20QData 实例驱动 process_message。
"""

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
    return mgr.process_message(12345, 'u1', '玩家A', text)


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

print('guess 20q prefix & two-phase tests passed')
