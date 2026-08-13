"""你想我猜 · WMC 谱面标签确定性规则层测试（_q_wmc_tag）。

不依赖任何网络/API：直接用 mock 的 wmc_tags 字典喂给 classify_question，
验证「涉及标签的是非题」已由确定性规则层拦截并据标签回 是/否/无法回答，
不再交给 LLM 判断「这算不算标签题」；并验证含「大」等词的标签题不会被
定数层误判成「定数偏高」。

覆盖：
1. 各真实标签命中 → 是（星星谱/体力谱/键盘谱/错位/交互/扫键/跳拍/触摸/转圈/
   大位移/爆发/散打/定拍/反手/诈称谱/水谱/正常谱）
2. 标签不存在 → 否
3. 难度选择（默认紫谱 / 指定颜色 / 最高）
4. 无 WMC 数据或该难度标签缺失 → 放行给 LLM（不消耗次数）
5. 物量题（星星多吗）不被标签规则误伤
6. 「大位移」等含「大」的标签题绝不会被 _q_ds 误判为「定数偏高」
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

from libraries.maimaidx_guess_20q import classify_question, _q_ds, _q_wmc_tag  # noqa: E402
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


# 紫谱标签（依据真实 API 数据构造）：
# 难度分类=正常谱；评价=星星谱/体力谱；雷达=交互/错位/触摸/跳拍/大位移；
# 模式=扫键/爆发/散打/定拍/反手/转圈
_WMC_MASTER = {
    'difficultyClassification': {'label': '正常谱', 'estimatedLevel': 14.6, 'deviation': 0.0},
    'evaluationTags': [
        {'label': '星星谱', 'score': 0.92},
        {'label': '体力谱', 'score': 0.81},
    ],
    'radarTags': [
        {'label': '交互', 'score': 0.74},
        {'label': '错位', 'score': 0.63},
        {'label': '触摸', 'score': 0.69},
        {'label': '跳拍', 'score': 0.77},
        {'label': '大位移', 'score': 0.55},
        {'label': '纵连', 'score': 0.60},
        {'label': '一笔画', 'score': 0.50},
    ],
    'patterns': [
        {'label': '扫键', 'severity': 'mid', 'count': 2},
        {'label': '爆发', 'severity': 'high', 'count': 6},
        {'label': '散打', 'severity': 'mid', 'count': 4},
        {'label': '定拍', 'severity': 'low', 'count': 1},
        {'label': '反手', 'severity': 'mid', 'count': 3},
        {'label': '转圈', 'severity': 'low', 'count': 1},
        {'label': '绝赞段', 'severity': 'high', 'count': 3},
        {'label': '拆弹', 'severity': 'mid', 'count': 2},
    ],
}

# 红谱标签：难度分类=诈称谱；评价=键盘谱；雷达=跳拍；模式=触摸组
_WMC_EXPERT = {
    'difficultyClassification': {'label': '诈称谱', 'estimatedLevel': 14.0, 'deviation': -0.5},
    'evaluationTags': [
        {'label': '键盘谱', 'score': 0.88},
    ],
    'radarTags': [
        {'label': '跳拍', 'score': 0.70},
    ],
    'patterns': [
        {'label': '触摸组', 'severity': 'low', 'count': 18},
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
_check('星星歌-reason含标签维度', '大位移' not in reason and '标签' in reason, True)
ans, consumed, _ = classify_question(_m, '体力谱吗', _wmc(_WMC_MASTER))
_check('体力谱(命中)', (ans, consumed), ('是喵 ✅', True))
ans, consumed, _ = classify_question(_m, '有错位吗', _wmc(_WMC_MASTER))
_check('错位(配置标签命中)', (ans, consumed), ('是喵 ✅', True))
ans, consumed, _ = classify_question(_m, '交互谱吗', _wmc(_WMC_MASTER))
_check('交互(配置标签命中)', (ans, consumed), ('是喵 ✅', True))
ans, consumed, _ = classify_question(_m, '扫键谱吗', _wmc(_WMC_MASTER))
_check('扫键(模式标签命中)', (ans, consumed), ('是喵 ✅', True))
ans, consumed, _ = classify_question(_m, '触摸吗', _wmc(_WMC_MASTER))
_check('触摸(雷达标签命中)', (ans, consumed), ('是喵 ✅', True))
ans, consumed, _ = classify_question(_m, '转圈吗', _wmc(_WMC_MASTER))
_check('转圈(模式标签命中)', (ans, consumed), ('是喵 ✅', True))
# 本次修复重点：大位移/爆发/散打/定拍/反手（真实 API 标签）
ans, consumed, _ = classify_question(_m, '是大位移吗', _wmc(_WMC_MASTER))
_check('大位移(真实标签命中)', (ans, consumed), ('是喵 ✅', True))
ans, consumed, _ = classify_question(_m, '是爆发谱吗', _wmc(_WMC_MASTER))
_check('爆发(真实标签命中)', (ans, consumed), ('是喵 ✅', True))
ans, consumed, _ = classify_question(_m, '散打吗', _wmc(_WMC_MASTER))
_check('散打(真实标签命中)', (ans, consumed), ('是喵 ✅', True))
ans, consumed, _ = classify_question(_m, '定拍吗', _wmc(_WMC_MASTER))
_check('定拍(真实标签命中)', (ans, consumed), ('是喵 ✅', True))
ans, consumed, _ = classify_question(_m, '反手谱吗', _wmc(_WMC_MASTER))
_check('反手(真实标签命中)', (ans, consumed), ('是喵 ✅', True))
# 本次补回/新增的标签：纵连/一笔画/绝赞段/拆弹
ans, consumed, _ = classify_question(_m, '纵连吗', _wmc(_WMC_MASTER))
_check('纵连(真实标签命中)', (ans, consumed), ('是喵 ✅', True))
ans, consumed, _ = classify_question(_m, '一笔画吗', _wmc(_WMC_MASTER))
_check('一笔画(真实标签命中)', (ans, consumed), ('是喵 ✅', True))
ans, consumed, _ = classify_question(_m, '绝赞段吗', _wmc(_WMC_MASTER))
_check('绝赞段(真实标签命中)', (ans, consumed), ('是喵 ✅', True))
ans, consumed, _ = classify_question(_m, '拆弹吗', _wmc(_WMC_MASTER))
_check('拆弹(真实标签命中)', (ans, consumed), ('是喵 ✅', True))

# ── 2. 标签不存在 → 否 ──
print('【2】标签不存在 → 否')
ans, consumed, _ = classify_question(_m, '键盘歌吗', _wmc(_WMC_MASTER))
_check('键盘歌(紫谱无→否)', (ans, consumed), ('不是喵 ❌', True))
ans, consumed, _ = classify_question(_m, '底力谱吗', _wmc(_WMC_MASTER))
_check('底力谱(无→否)', (ans, consumed), ('不是喵 ❌', True))
ans, consumed, _ = classify_question(_m, '高物量吗', _wmc(_WMC_MASTER))
_check('高物量(无→否)', (ans, consumed), ('不是喵 ❌', True))
ans, consumed, _ = classify_question(_m, '诈称谱吗', _wmc(_WMC_MASTER))
_check('诈称谱(难度=正常谱→否)', (ans, consumed), ('不是喵 ❌', True))
ans, consumed, _ = classify_question(_m, '水谱吗', _wmc(_WMC_MASTER))
_check('水谱(难度=正常谱→否)', (ans, consumed), ('不是喵 ❌', True))
# 已移出词表（未实采确认）的标签 → 不再由规则层判定，放行 LLM
ans, consumed, _ = classify_question(_m, '认知系吗', _wmc(_WMC_MASTER))
_check('认知系(已移出词表→放行)', consumed, False)
ans, consumed, _ = classify_question(_m, '同押吗', _wmc(_WMC_MASTER))
_check('同押(已移出词表→放行)', consumed, False)

# ── 3. 难度选择 ──
print('【3】难度选择（默认紫谱 / 指定颜色）')
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
ans, consumed, _ = classify_question(_m, '最高是大位移吗', wmc_both)
_check('最高=紫谱大位移(命中)', (ans, consumed), ('是喵 ✅', True))

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

# ── 6. 「大位移」等含「大」的词绝不被 _q_ds 误判为「定数偏高」──
print('【6】含「大」的标签题不得被定数层误判')
# 6a) _q_ds 直接对「是大位移吗」必须放行（护栏），绝不能回「定数偏高」
qds = _q_ds(_m, '是大位移吗')
_check('_q_ds(是大位移吗) 放行(不误判定数)', qds, None)
qds = _q_ds(_m, '大位移谱吗')
_check('_q_ds(大位移谱吗) 放行', qds, None)
# 6b) 有数据时：整条链路回「是」，理由必须是「大位移（WMC标签）」而非「定数偏高」
ans, consumed, reason = classify_question(_m, '是大位移吗', _wmc(_WMC_MASTER))
_check('是大位移吗 → 是', ans, '是喵 ✅')
_check('是大位移吗 → 理由不出现「定数」', '定数' not in reason, True)
_check('是大位移吗 → 理由含「大位移」', '大位移' in reason, True)
# 6c) 无数据时：也绝不能回「定数偏高」的假「是」，应放行给 LLM
ans, consumed, reason = classify_question(_m, '是大位移吗', None)
_check('是大位移吗(无数据) → 不消耗次数', consumed, False)
_check('是大位移吗(无数据) → 理由非「定数偏高」', '定数' not in reason, True)
# 6d) 「高物量」含「高」，同样是定数层宽松关键词，不得误判
check_qds_gao = _q_ds(_m, '高物量吗')
_check('_q_ds(高物量吗) 放行(不误判定数)', check_qds_gao, None)
ans, consumed, reason = classify_question(_m, '高物量吗', _wmc(_WMC_MASTER))
_check('高物量吗(紫谱无→否)', ans, '不是喵 ❌')
_check('高物量吗 → 理由不出现「定数」', '定数' not in reason, True)

print(f'\n结果：通过 {_passed} 项，失败 {_failed} 项')
sys.exit(1 if _failed else 0)
