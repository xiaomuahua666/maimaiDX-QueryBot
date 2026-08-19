"""回归测试：旧框 PLUS 版曲库 from 字段省略 'maimai' 前缀时的版本是非题。

真实曲库 basic_info.from 对旧框 PLUS 版写成 'MiLK PLUS'（无 'maimai' 前缀），
而版本表规范串是 'maimai milk plus'。版本比较若不做前缀归一化，会误答「不是喵」。

验证：
1. from='MiLK PLUS' + 问「是雪代吗」 → 是喵（雪代=milk plus）
2. from='MiLK PLUS' + 问「是白代吗」 → 不是喵（白代=milk 基版，非 plus）
3. from='maimai milk plus'（旧 fixture）+ 问「是雪代吗」 → 是喵（行为不变）
4. from='maimai でらっくす buddies' + 问「是雪代吗」 → 不是喵（新框非旧框）
5. 版本顺序：from='MiLK PLUS' 问「雪代之前吗」 → 不是喵；
   from='maimai でらっくす' 问「雪代及以后吗」 → 是喵
"""
import sys
import types
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
from libraries.maimaidx_guess_20q import classify_question, _YES, _NO, _version_cn  # noqa: E402
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


def _check(desc, m, text, expect):
    global PASS, FAIL
    ans = classify_question(m, text)[0]
    ok = ans == expect
    print(f"[{'PASS' if ok else 'FAIL'}] {desc}: 答={ans!r} 期望={expect!r}")
    if ok:
        PASS += 1
    else:
        FAIL += 1


# 1. 真实数据：'MiLK PLUS'（无 maimai 前缀）→ 雪代 应 是喵
_check("from='MiLK PLUS' 问「是雪代吗」", _mk('MiLK PLUS'), '是雪代吗', _YES)
# 2. 雪代=PLUS，白代=基版 → 问白代 应 不是喵
_check("from='MiLK PLUS' 问「是白代吗」", _mk('MiLK PLUS'), '是白代吗', _NO)
# 3. 旧 fixture『maimai milk plus』行为不变
_check("from='maimai milk plus' 问「是雪代吗」", _mk('maimai milk plus'), '是雪代吗', _YES)
# 4. 新框非旧框 → 雪代 应 不是喵
_check("from='maimai でらっくす buddies' 问「是雪代吗」", _mk('maimai でらっくす buddies'), '是雪代吗', _NO)
# 5. 版本顺序题
_check("from='MiLK PLUS' 问「雪代之前吗」", _mk('MiLK PLUS'), '雪代之前吗', _NO)
_check("from='maimai でらっくす' 问「雪代及以后吗」", _mk('maimai でらっくす'), '雪代及以后吗', _YES)

# 6. _version_cn 展示映射：修复后 MiLK PLUS（无前缀）也能映射到雪代，空值不误判为初代
def _check_cn(desc, ver, exp):
    global PASS, FAIL
    ans = _version_cn(ver)
    ok = ans == exp
    print(f"[{'PASS' if ok else 'FAIL'}] {desc}: _version_cn({ver!r})={ans!r} 期望={exp!r}")
    if ok:
        PASS += 1
    else:
        FAIL += 1


_check_cn("MiLK PLUS → 雪代", 'MiLK PLUS', '雪代')
_check_cn("空值 → 未知", '', '未知')
_check_cn("maimai milk → 白代", 'maimai milk', '白代')

print(f"\n结果：PASS={PASS} FAIL={FAIL}")
raise SystemExit(1 if FAIL else 0)
