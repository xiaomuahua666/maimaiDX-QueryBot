"""你想我猜 · WMC 谱面标签确定性规则层测试（_q_wmc_tag）。

不依赖任何网络/API：直接用 mock 的 wmc_tags 字典喂给 classify_question，
验证「涉及标签的是非题」已由确定性规则层拦截并据标签回 是/否/无法回答，
不再交给 LLM 判断「这算不算标签题」。

覆盖：
1. 各标签命中 → 是
2. 标签不存在 → 否
3. 难度选择（默认紫谱 / 指定颜色 / 最高）
4. 无 WMC 数据或该难度标签缺失 → 放行给 LLM（不消耗次数）
5. 物量题（星星多吗）不被标签规则误伤
"""

import sys
import types
import importlib
from pathlib import Path
from collections import namedtuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# ── 注入 libraries.maimaidx_music 的轻量 stub，避免触发 NoneBot 配置 ──
model_mod = importlib.import_module('libraries.maimaidx_model')
music_stub = types.ModuleType('libraries.maimaidx_music')
music_stub.Music = model_mod.Music
music_stub.mai = types.SimpleNamespace()
music_stub.guess = types.SimpleNamespace()
sys.modules['libraries.maimaidx_music'] = music_stub

from libraries.maimaidx_guess_20q import classify_question  # noqa: E402
from libraries.maimaidx_model import BasicInfo, Chart, Music  # noqa: E402


def _make_music() -> Music:
    notes = namedtuple('Notes', ['tap', 'hold', 'slide', 'brk'])(100, 10, 60, 5)
    basic_info = BasicInfo.model_validate({
        'title': 'TEST SONG',
        'artist': 'DECO*27',
        'genre': '流行&动漫',
        'bpm': 180,
        'release_date': '',
        'from': 'maimai でらっくす',
        'is_new': True,
    })
    return Music(
        id='10044',
        title='TEST SONG',
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


def _wmc(diff_tags: dict) -> dict:
    """构造 {3: tags} 的 wmc_tags（默认只给紫谱 MASTER 数据）。"""
    return {3: diff_tags}


# 紫谱标签：星星谱 / 体力谱（评价）；交互 / 错位（配置）；纵连 / 扫键（模式）；难度=正常谱
_WMC_MASTER = {
    'difficultyClassification': {'label': '正常谱', 'estimatedLevel': 14.6, 'deviation': 0.0},
    'evaluationTags': [
        {'label': '星星谱', 'score': 0.92},
        {'label': '体力谱', 'score': 0.81},
    ],
    'radarTags': [
        {'label': '交互', 'score': 0.74},
        {'label': '错位', 'score': 0.63},
    ],
    'patterns': [
        {'label': '纵连', 'severity': 'high', 'count': 3},
        {'label': '扫键', 'severity': 'mid', 'count': 2},
    ],
}

# 红谱标签：键盘谱（评价）；跳拍（配置）；一笔画（模式）；难度=诈称谱
_WMC_EXPERT = {
    'difficultyClassification': {'label': '诈称谱', 'estimatedLevel': 14.0, 'deviation': -0.5},
    'evaluationTags': [
        {'label': '键盘谱', 'score': 0.88},
    ],
    'radarTags': [
        {'label': '跳拍', 'score': 0.70},
    ],
    'patterns': [
        {'label': '一笔画', 'severity': 'low', 'count': 1},
    ],
}

_m = _make_music()
_passed = 0
_failed = 0


def _check(name: str, got, expect):
    global _passed, _failed
    ok = got == expect
    if ok:
        _passed += 1
        print(f'  ✓ {name}')
    else:
        _failed += 1
        print(f'  ✗ {name} — 期望 {expect!r}，实际 {got!r}')


print('═══ WMC 标签确定性规则层测试 ═══')

# ── 1. 命中 → 是 ──
print('【1】标签命中 → 是')
ans, consumed, reason = classify_question(_m, '是星星歌吗', _wmc(_WMC_MASTER))
_check('星星歌(命中)', (ans, consumed), ('是喵 ✅', True))
ans, consumed, _ = classify_question(_m, '体力谱吗', _wmc(_WMC_MASTER))
_check('体力谱(命中)', (ans, consumed), ('是喵 ✅', True))
ans, consumed, _ = classify_question(_m, '有错位吗', _wmc(_WMC_MASTER))
_check('错位(配置标签命中)', (ans, consumed), ('是喵 ✅', True))
ans, consumed, _ = classify_question(_m, '交互谱吗', _wmc(_WMC_MASTER))
_check('交互(配置标签命中)', (ans, consumed), ('是喵 ✅', True))
ans, consumed, _ = classify_question(_m, '纵连吗', _wmc(_WMC_MASTER))
_check('纵连(模式标签命中)', (ans, consumed), ('是喵 ✅', True))
ans, consumed, _ = classify_question(_m, '扫键谱吗', _wmc(_WMC_MASTER))
_check('扫键(模式标签命中)', (ans, consumed), ('是喵 ✅', True))

# ── 2. 标签不存在 → 否 ──
print('【2】标签不存在 → 否')
ans, consumed, _ = classify_question(_m, '键盘歌吗', _wmc(_WMC_MASTER))
_check('键盘歌(紫谱无→否)', (ans, consumed), ('不是喵 ❌', True))
ans, consumed, _ = classify_question(_m, '底力谱吗', _wmc(_WMC_MASTER))
_check('底力谱(无→否)', (ans, consumed), ('不是喵 ❌', True))
ans, consumed, _ = classify_question(_m, '转圈吗', _wmc(_WMC_MASTER))
_check('转圈(无→否)', (ans, consumed), ('不是喵 ❌', True))
ans, consumed, _ = classify_question(_m, '一笔画吗', _wmc(_WMC_MASTER))
_check('一笔画(无→否)', (ans, consumed), ('不是喵 ❌', True))
ans, consumed, _ = classify_question(_m, '跳拍吗', _wmc(_WMC_MASTER))
_check('跳拍(无→否)', (ans, consumed), ('不是喵 ❌', True))
ans, consumed, _ = classify_question(_m, '同押吗', _wmc(_WMC_MASTER))
_check('同押(无→否)', (ans, consumed), ('不是喵 ❌', True))
ans, consumed, _ = classify_question(_m, '高物量吗', _wmc(_WMC_MASTER))
_check('高物量(无→否)', (ans, consumed), ('不是喵 ❌', True))
ans, consumed, _ = classify_question(_m, '诈称谱吗', _wmc(_WMC_MASTER))
_check('诈称谱(难度=正常谱→否)', (ans, consumed), ('不是喵 ❌', True))
ans, consumed, _ = classify_question(_m, '水谱吗', _wmc(_WMC_MASTER))
_check('水谱(难度=正常谱→否)', (ans, consumed), ('不是喵 ❌', True))
ans, consumed, _ = classify_question(_m, '认知系吗', _wmc(_WMC_MASTER))
_check('认知系(无→否)', (ans, consumed), ('不是喵 ❌', True))

# ── 3. 难度选择 ──
print('【3】难度选择（默认紫谱 / 指定颜色）')
# 红谱有键盘谱/跳拍/一笔画/诈称谱，紫谱没有
wmc_both = {2: _WMC_EXPERT, 3: _WMC_MASTER}
ans, consumed, _ = classify_question(_m, '键盘歌吗', wmc_both)
_check('键盘歌(默认紫谱无→否)', (ans, consumed), ('不是喵 ❌', True))
ans, consumed, _ = classify_question(_m, '红谱是键盘歌吗', wmc_both)
_check('红谱键盘歌(命中)', (ans, consumed), ('是喵 ✅', True))
ans, consumed, _ = classify_question(_m, '红谱是诈称谱吗', wmc_both)
_check('红谱诈称谱(难度分类命中)', (ans, consumed), ('是喵 ✅', True))
ans, consumed, _ = classify_question(_m, '紫谱是星星歌吗', wmc_both)
_check('紫谱星星歌(命中)', (ans, consumed), ('是喵 ✅', True))
# 最高：ds 最高是紫谱(14.6) → 看紫谱
ans, consumed, _ = classify_question(_m, '最高是星星歌吗', wmc_both)
_check('最高=紫谱星星歌(命中)', (ans, consumed), ('是喵 ✅', True))

# ── 4. 无数据 / 该难度缺失 → 放行给 LLM（不消耗次数） ──
print('【4】无 WMC 数据 / 该难度缺失 → 放行 LLM（不消耗次数）')
ans, consumed, _ = classify_question(_m, '是星星歌吗', None)
_check('wmc_tags=None → 放行', consumed, False)
ans, consumed, _ = classify_question(_m, '是星星歌吗', {})
_check('wmc_tags={} → 放行', consumed, False)
ans, consumed, _ = classify_question(_m, '是星星歌吗', {3: None})
_check('该难度标签缺失(None) → 放行', consumed, False)
ans, consumed, _ = classify_question(_m, '红谱是键盘歌吗', {3: _WMC_MASTER})
_check('指定红谱但红谱无数据 → 放行', consumed, False)

# ── 5. 物量题不被标签规则误伤 ──
print('【5】物量题（星星多吗）不被标签规则误伤')
ans, consumed, _ = classify_question(_m, '星星多吗', _wmc(_WMC_MASTER))
_check('「星星多吗」→ 不命中标签规则(交LLM)', consumed, False)
ans, consumed, _ = classify_question(_m, 'slide有几个', _wmc(_WMC_MASTER))
_check('「slide有几个」→ 不命中标签规则', consumed, False)

# ── 6. 否定词问法仍正确（如「不是星星歌吗」）──
print('【6】否定问法语义正确')
# 注：否定反转在 process_message 层(_apply_negation)处理，classify_question 返回原始是/否；
# 这里只验证标签层正确判定「是星星歌」，反转由上层完成。
ans, consumed, _ = classify_question(_m, '是星星歌吗', _wmc(_WMC_MASTER))
_check('星星歌原始判定=是(供上层反转)', ans, '是喵 ✅')

print(f'\n结果：通过 {_passed} 项，失败 {_failed} 项')
sys.exit(1 if _failed else 0)
