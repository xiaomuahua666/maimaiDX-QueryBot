#!/usr/bin/env python3
"""等级牌子指令后缀不能被误解析为查分器用户名。"""

from __future__ import annotations

import re
import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
tree = ast.parse((ROOT / 'command' / 'mai_table.py').read_text(encoding='utf-8'))
pattern_value = None
for node in tree.body:
    if isinstance(node, ast.Assign) and any(
        isinstance(target, ast.Name) and target.id == 'LEVEL_PLATE_PROGRESS_PATTERN'
        for target in node.targets
    ):
        pattern_value = ast.literal_eval(node.value)
        break
assert pattern_value is not None
PATTERN = re.compile(pattern_value)


def groups(command: str):
    match = PATTERN.fullmatch(command)
    assert match is not None, command
    return match.groups()


assert groups('10将完成表') == ('10', '将', None, None, None)
assert groups('13将') == ('13', '将', None, None, None)
assert groups('14+极进度') == ('14+', '极', None, None, None)
assert groups('13舞舞完成表 未完成 2') == ('13', '舞舞', '未完成', '2', None)
assert groups('13将进度 玩家名') == ('13', '将', None, None, '玩家名')

# 用户名仍然可用，但必须与指令主体有空白边界。
assert PATTERN.fullmatch('13将玩家名') is None

print('level plate command matching tests: ok')
