# -*- coding: utf-8 -*-
"""验证猜20问语义修复：
1. HOLD 物量题不被误判为定级（"hold 大于40吗"）
2. 音符维度识别（星星=SLIDE，不是 TOUCH）
3. 谱师题全部交 LLM（规则层不抢答）
"""
import sys
import types
import importlib
from pathlib import Path
from collections import namedtuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# stub 掉 maimaidx_music，避免触发 NoneBot 配置加载
_model_mod = importlib.import_module('libraries.maimaidx_model')
_music_stub = types.ModuleType('libraries.maimaidx_music')
_music_stub.Music = _model_mod.Music


class _MaiStub:
    pass


class _GuessStub:
    pass


_music_stub.mai = _MaiStub()
_music_stub.guess = _GuessStub()
sys.modules['libraries.maimaidx_music'] = _music_stub

from libraries.maimaidx_model import BasicInfo, Chart, Music  # noqa: E402
from libraries.maimaidx_guess_20q import (  # noqa: E402
    _q_note_count,
    _QUESTION_HANDLERS,
    _YES,
    _NO,
)


def _make_music(charter='Luxizhel', notes=(100, 36, 80, 4)) -> Music:
    nt = namedtuple('Notes', ['tap', 'hold', 'slide', 'brk'])
    basic_info = BasicInfo.model_validate({
        'title': 'TEST SONG', 'artist': 'TEST', 'genre': '舞萌',
        'bpm': 180, 'release_date': '', 'from': 'maimai でらっくす', 'is_new': True,
    })
    return Music(
        id='99999', title='TEST SONG', type='DX',
        ds=[10.0, 12.0, 13.5, 14.6], level=['7', '10', '12+', '13+'],
        cids=[1, 2, 3, 4],
        charts=[Chart(notes=nt(*notes), charter=charter) for _ in range(4)],
        basic_info=basic_info,
    )


def _dispatch(music, text):
    for h in _QUESTION_HANDLERS:
        r = h(music, text)
        if r is not None:
            return h.__name__, r
    return None, None


def yn(r):
    return r[0] if isinstance(r, tuple) else None


passed = failed = 0


def check(name, cond, detail=''):
    global passed, failed
    if cond:
        passed += 1
        print(f'  [PASS] {name} {detail}')
    else:
        failed += 1
        print(f'  [FAIL] {name} {detail}')


m = _make_music(notes=(100, 36, 80, 4))

print('=== Bug2: HOLD 物量题不被误判定级 ===')
hname, r = _dispatch(m, 'hold 大于 40 个吗')
check('被 _q_note_count 接管', hname == '_q_note_count', f'handler={hname}')
check('hold(36)>40 → 否', yn(r) == _NO, f'got={r!r}')
check('reason 含 HOLD 不含定级',
      isinstance(r, tuple) and 'HOLD' in r[1] and '定级' not in r[1], f'got={r!r}')

hname, r = _dispatch(m, 'hold 36 个吗')
check('hold 36 个吗 → 是（精确）', yn(r) == _YES, f'handler={hname} got={r!r}')

hname, r = _dispatch(m, '紫谱定数是 14 吗')
check('紫谱定数题仍由 _q_ds 处理', hname == '_q_ds', f'handler={hname} got={r!r}')

print()
print('=== Bug3: 星星=SLIDE 不是 TOUCH ===')
hname, r = _dispatch(m, '星星大于50个吗')
check('星星题被 _q_note_count 接管', hname == '_q_note_count', f'handler={hname}')
check('星星(slide=80)>50 → 是', yn(r) == _YES, f'got={r!r}')
check('reason 标注 SLIDE(星星) 非 TOUCH',
      isinstance(r, tuple) and 'SLIDE' in r[1] and 'TOUCH' not in r[1], f'got={r!r}')

hname, r = _dispatch(m, '是星星歌吗')
check('"是星星歌吗"不被规则层抢答（放行LLM）', hname is None, f'handler={hname} got={r!r}')

print()
print('=== 谱师题全部交 LLM（规则层不抢答）===')
for q in ('谱师是泸溪河吗', '谱师是luxizhel吗', '谱师是沙发太吗', '谱师是翠楼屋吗'):
    hname, r = _dispatch(m, q)
    check(f'「{q}」规则层不抢答', hname is None, f'handler={hname} got={r!r}')

print()
print(f'=== 结果: {passed} passed, {failed} failed ===')
sys.exit(1 if failed else 0)
