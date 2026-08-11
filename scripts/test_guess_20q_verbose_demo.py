"""20问：回归测试「过程可见」版——打印前几首曲的每个 case 执行详情，证明真实跑测。"""
import json
import random
import sys
import types
from pathlib import Path
from collections import namedtuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import importlib
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

from libraries.maimaidx_guess_20q import classify_question, _YES, _NO  # noqa: E402
from libraries.maimaidx_model import BasicInfo, Chart, Music  # noqa: E402

# 复用 regression_real 的加载逻辑
import scripts.test_guess_20q_regression_real as reg  # noqa: E402

# 取前 5 首 + 随机 15 首，详细打印
random.seed(2024)
sample = reg._musics[:5] + random.sample(reg._musics[5:], 15)

print(f'本次抽测 {len(sample)} 首曲，每首打印所有 case 执行详情')
print('=' * 80)

total = 0
passed = 0
failed = 0

for idx, m in enumerate(sample):
    cases = reg._gen_cases(m)
    gi = reg._genre_key(m)
    vi = reg._ver_idx_of(m)
    purple = m.ds[3] if len(m.ds) >= 4 else 0
    print(f'\n[{idx+1}/{len(sample)}] {m.title[:30]}')
    print(f'    真实特征: genre={m.basic_info.genre}({gi}) version={m.basic_info.version}(idx={vi}) bpm={m.basic_info.bpm} purple_ds={purple:g}')
    for q, expected in cases:
        ans, consumed, reason = classify_question(m, q)
        if not consumed:
            # 规则层未命中（含「舞萌」等歧义词走 LLM），不计入错误，单独标记
            print(f'    → LLM Q:{q!r:30} (规则未命中，交 AI) | {reason}')
            continue
        total += 1
        expected_ans = _YES if expected else _NO
        ok = (ans == expected_ans)
        if ok:
            passed += 1
            mark = '✓'
        else:
            failed += 1
            mark = '✗ WRONG'
        exp_str = '是' if expected else '否'
        print(f'    {mark} Q:{q!r:30} 期望={exp_str} 实际={ans} | {reason}')

print()
print('=' * 80)
print(f'过程可见回归: {passed}/{total} 通过, {failed} 错误')
if failed == 0:
    print('✓ 全部正确')
else:
    print(f'✗ 有 {failed} 条错误')
    sys.exit(1)
