"""20问「为难题」实测：版本顺序二分法 + 定数边界 + 语义陷阱。

覆盖：合并叫法区间边界、比X早/晚/新/旧、不早于/不晚于、单字俗称、
     负面/反转提问、与定数/分类混淆、复合方向词、ASCII 俗称顺序。
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

from libraries.maimaidx_guess_20q import (  # noqa: E402
    classify_question, _YES, _NO,
)
from libraries.maimaidx_model import BasicInfo, Chart, Music  # noqa: E402

PASS = 0
FAIL = 0


def _make_ver_music(version):
    bi = BasicInfo.model_validate({
        'title': 'V', 'artist': 'A', 'genre': 'maimai', 'bpm': 180,
        'release_date': '', 'from': version, 'is_new': False, 'version': version,
    })
    return Music(
        id='9', title='V', type='SD', ds=[10.0, 12.0, 13.6, 14.6],
        level=['7', '10', '12+', '13+'], cids=[1, 2, 3, 4],
        charts=[Chart(notes=namedtuple('N', ['tap', 'hold', 'slide', 'brk'])(1, 1, 1, 1), charter='x') for _ in range(4)],
        basic_info=bi,
    )


def check(desc, music, question, expected_ans, expected_consumed=True, reason_must=None, reason_must_not=None):
    """断言单条问法。expected_ans: _YES/_NO/None(=不关心)"""
    global PASS, FAIL
    try:
        a, c, r = classify_question(music, question)
        ok = True
        msg = ''
        if expected_consumed and not c:
            ok = False
            msg = f'应规则命中(consumed=True)，实际 consumed={c}'
        elif not expected_consumed and c:
            ok = False
            msg = f'应走LLM(consumed=False)，实际 consumed={c}'
        elif expected_ans is not None and a != expected_ans:
            ok = False
            msg = f'期望回答 {expected_ans}，实际 {a}'
        if ok and reason_must and reason_must not in r:
            ok = False
            msg = f'reason 应含「{reason_must}」，实际: {r}'
        if ok and reason_must_not and reason_must_not in r:
            ok = False
            msg = f'reason 不应含「{reason_must_not}」，实际: {r}'
        if ok:
            PASS += 1
            print(f'  ✓ {desc}: {a}  [{r}]')
        else:
            FAIL += 1
            print(f'  ✗ {desc}: {msg}')
            print(f'      question={question!r} ans={a} consumed={c} reason={r!r}')
    except Exception as e:
        FAIL += 1
        print(f'  ✗ {desc}: 异常 {type(e).__name__}: {e}')


print('=' * 70)
print('【1】合并叫法区间边界——双宴代=[21,22]')
print('=' * 70)
_v_buddies = _make_ver_music('maimai でらっくす buddies')        # idx21 双代
_v_buddies_p = _make_ver_music('maimai でらっくす buddies plus')  # idx22 宴代
_v_white = _make_ver_music('maimai milk')                         # idx10 白代
_v_circle = _make_ver_music('maimai でらっくす circle')           # idx25 圈代

# 双代(21) 在区间内：及以后=是(>=21)，及以前=是(<=22)，之前=否(<21)，之后=否(>22)
check('双代 问 双宴代及以后', _v_buddies, '双宴代及以后吗', _YES, reason_must='≥')
check('双代 问 双宴代及以前', _v_buddies, '双宴代及以前吗', _YES, reason_must='≤')
check('双代 问 双宴代之前', _v_buddies, '双宴代之前吗', _NO, reason_must='<')
check('双代 问 双宴代之后', _v_buddies, '双宴代之后吗', _NO, reason_must='>')
# 宴代(22) 同样在区间内
check('宴代 问 双宴代及以后', _v_buddies_p, '双宴代及以后吗', _YES)
check('宴代 问 双宴代及以前', _v_buddies_p, '双宴代及以前吗', _YES)
check('宴代 问 双宴代之前', _v_buddies_p, '双宴代之前吗', _NO)
check('宴代 问 双宴代之后', _v_buddies_p, '双宴代之后吗', _NO)
# 白代(10) 在区间前
check('白代 问 双宴代及以后', _v_white, '双宴代及以后吗', _NO)
check('白代 问 双宴代之前', _v_white, '双宴代之前吗', _YES)
# 圈代(25) 在区间后
check('圈代 问 双宴代及以前', _v_circle, '双宴代及以前吗', _NO)
check('圈代 问 双宴代之后', _v_circle, '双宴代之后吗', _YES)

print()
print('=' * 70)
print('【2】舞代（旧框统称）顺序——舞代=[0,12]')
print('=' * 70)
_v_finale = _make_ver_music('maimai finale')       # idx12 辉代
_v_snow = _make_ver_music('maimai milk plus')      # idx11 雪代
_v_dx = _make_ver_music('maimai でらっくす')        # idx13 熊代(新框)

# 辉代(12) 在舞代区间内：及以后=是(>=0)
check('辉代 问 舞代及以后', _v_finale, '舞代及以后吗', _YES, reason_must='舞代')
check('辉代 问 舞代及以前', _v_finale, '舞代及以前吗', _YES)
# 熊代(13) 新框，不在舞代区间：及以前=是(<=12)？13<=12 否
check('熊代 问 舞代及以前', _v_dx, '舞代及以前吗', _NO)
check('熊代 问 舞代之前', _v_dx, '舞代之前吗', _NO)  # 13<0 否
check('熊代 问 舞代之后', _v_dx, '舞代之后吗', _YES)  # 13>12 是

print()
print('=' * 70)
print('【3】「比X早/晚/新/旧」句式')
print('=' * 70)
# 辉代(12) 比雪代(11) 晚/新 → 是
check('辉代 比雪代晚', _v_finale, '比雪代晚吗', _YES)
check('辉代 比雪代新', _v_finale, '比雪代新吗', _YES)
# 白代(10) 比雪代(11) 早/旧 → 是
check('白代 比雪代早', _v_white, '比雪代早吗', _YES)
check('白代 比雪代旧', _v_white, '比雪代旧吗', _YES)
# 雪代(11) 比自己早 → 否（严格<）
check('雪代 比雪代早', _v_snow, '比雪代早吗', _NO)
check('雪代 比雪代晚', _v_snow, '比雪代晚吗', _NO)
# 复合句：这曲子比雪代要晚吗
check('辉代 比雪代要晚（口语）', _v_finale, '这曲子比雪代要晚吗', _YES)
# 合并叫法 + 比X句式
check('圈代 比双宴代新', _v_circle, '比双宴代新吗', _YES)  # 25>22
check('白代 比双宴代旧', _v_white, '比双宴代旧吗', _YES)   # 10<21

print()
print('=' * 70)
print('【4】「不早于/不晚于」（含等号，对称定数不高于/不低于）')
print('=' * 70)
check('雪代 不晚于雪代', _v_snow, '不晚于雪代吗', _YES, reason_must='≤')
check('辉代 不晚于雪代', _v_finale, '不晚于雪代吗', _NO)  # 12>11
check('雪代 不早于雪代', _v_snow, '不早于雪代吗', _YES, reason_must='≥')
check('白代 不早于雪代', _v_white, '不早于雪代吗', _NO)   # 10<11

print()
print('=' * 70)
print('【5】单字俗称 + 顺序方向词（易和难度颜色冲突）')
print('=' * 70)
# 单字「雪」单独不命中版本（必须「雪代/雪版」），避免误判
check('雪代及以后（雪代两字）', _v_finale, '雪代及以后吗', _YES)
check('雪版之前（雪版两字）', _v_white, '雪版之前吗', _YES)
# 单字「雪」+及以后 → 不应命中版本（走LLM），避免「紫雪」类误判
check('单字雪+及以后 应走LLM', _v_finale, '雪及以后吗', None, expected_consumed=False)

print()
print('=' * 70)
print('【6】否定/反转提问（不是X吗 / 无X吗）')
print('=' * 70)
# 「不是雪代之前吗」= 不是(版本<雪代) → 辉代(12) 不<11 → 不是 → 反转=是
check('辉代 不是雪代之前吗（反转）', _v_finale, '不是雪代之前吗', None)  # 反转逻辑由上层处理，这里只看规则命中
# 「不是雪代及以后吗」= 辉代(12)>=11 是 → 反转=否
check('辉代 不是雪代及以后吗（规则命中）', _v_finale, '不是雪代及以后吗', None, expected_consumed=True)

print()
print('=' * 70)
print('【7】ASCII 俗称 + 顺序（dx/milk/buddies 等）')
print('=' * 70)
# dx=熊代(13), buddies=双代(21)
check('buddies 问 dx及以后', _v_buddies, 'dx及以后吗', _YES)        # 21>=13
check('dx 问 buddies之前', _v_dx, 'buddies之前吗', _YES)            # 13<21
check('buddies 问 milk及以后', _v_buddies, 'milk及以后吗', _YES)    # 21>=10(白代)
check('buddies 问 milk+及以后', _v_buddies, 'milk+及以后吗', _YES)  # 21>=11(雪代)
check('白代 问 milkplus之后', _v_white, 'milkplus之后吗', _NO)      # 10>11 否

print()
print('=' * 70)
print('【8】方向词歧义/复合词陷阱')
print('=' * 70)
# 「之前」vs「及之前」：含等号差异
check('雪代 之前（不含）', _v_snow, '雪代之前吗', _NO)        # 11<11 否
check('雪代 及之前（含）', _v_snow, '雪代及之前吗', _YES)     # 11<=11 是
check('雪代 以后（不含）', _v_snow, '雪代以后吗', _NO)        # 11>11 否
check('雪代 及以后（含）', _v_snow, '雪代及以后吗', _YES)     # 11>=11 是
# 「不晚于」vs「晚于」：含等号差异
check('雪代 晚于（不含）', _v_snow, '雪代晚于吗', _NO)        # 走LT? 实际「晚于」在GT
check('雪代 不晚于（含）', _v_snow, '不晚于雪代吗', _YES)

print()
print('=' * 70)
print('【9】无版本俗称的顺序题 → 走 LLM')
print('=' * 70)
check('是前面吗（无俗称）', _v_buddies, '是前面吗', None, expected_consumed=False)
check('比早吗（无俗称）', _v_buddies, '比早吗', None, expected_consumed=False)
check('之后吗（无俗称）', _v_buddies, '之后吗', None, expected_consumed=False)

print()
print('=' * 70)
print('【10】定数边界——为难 case 复测')
print('=' * 70)


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


_v = _make_ds_music(13.6)
# 13.6 问「是13吗」→ 否（13档=[13,13.6)，13.6属13+）
check('13.6 问 是13', _v, '紫谱是13吗', _NO, reason_must='[13.0, 13.5] 闭区间')
# 13.6 问「是13+吗」→ 是（13+档=[13.6,14)）
check('13.6 问 是13+', _v, '紫谱是13+吗', _YES, reason_must='[13.6, 13.9] 闭区间')
# 13.6 问「定数13.6吗」→ 是（精确等于）
check('13.6 问 定数13.6', _v, '紫谱定数是13.6吗', _YES)
# 13.6 问「定数13.7吗」→ 否（精确不等于）
check('13.6 问 定数13.7', _v, '紫谱定数是13.7吗', _NO)
# 13.6 问「13以上吗」→ 是（严格>13）
check('13.6 问 13以上', _v, '紫谱定数13以上吗', _YES, reason_must='>')
# 13.6 问「大于等于13吗」→ 是（≥13）
check('13.6 问 大于等于13', _v, '紫谱定数大于等于13吗', _YES, reason_must='≥')
# 13.0 问「13以上吗」→ 否（严格>13，不含本数）
_v0 = _make_ds_music(13.0)
check('13.0 问 13以上（不含本数）', _v0, '紫谱定数13以上吗', _NO)
# 13.0 问「不低于13吗」→ 是（≥13）
check('13.0 问 不低于13（含本数）', _v0, '紫谱定数不低于13吗', _YES)
# 13.5 问「是13吗」→ 是（13档=[13,13.6)含13.5）
_v5 = _make_ds_music(13.5)
check('13.5 问 是13', _v5, '紫谱是13吗', _YES)
# 13.5 问「是13+吗」→ 否（13+档=[13.6,14)不含13.5）
check('13.5 问 是13+', _v5, '紫谱是13+吗', _NO)

print()
print('=' * 70)
print(f'总计: {PASS} 通过, {FAIL} 失败')
print('=' * 70)
sys.exit(1 if FAIL else 0)
