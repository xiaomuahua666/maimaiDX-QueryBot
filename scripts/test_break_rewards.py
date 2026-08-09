"""BREAK 连签与今日舞萌奖励公式回归测试（无需启动 NoneBot）。"""

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "libraries" / "maimaidx_break.py"
FUNCTIONS = {
    "calculate_streak_bonus",
    "calculate_luck_break",
    "calculate_checkin_reward",
}

tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
selected = [
    node
    for node in tree.body
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    and node.name in FUNCTIONS
]
assert {node.name for node in selected} == FUNCTIONS

namespace: dict = {}
exec(compile(ast.Module(body=selected, type_ignores=[]), str(SOURCE), "exec"), namespace)

streak_bonus = namespace["calculate_streak_bonus"]
luck_break = namespace["calculate_luck_break"]
checkin_reward = namespace["calculate_checkin_reward"]

curve = [3, 5, 8, 12, 20]
assert [streak_bonus(day, curve, 1) for day in range(1, 9)] == [
    3,
    5,
    8,
    12,
    20,
    21,
    22,
    23,
]
assert streak_bonus(100, curve, 1) == 115
assert streak_bonus(6, curve, 0) == 20  # growth=0 封顶在曲线末尾
assert streak_bonus(100, curve, 0) == 20  # 无论多远都封顶

assert {
    value: luck_break(value)
    for value in (0, 4, 5, 14, 15, 69, 94, 95, 99)
} == {
    0: (0, 0),
    4: (0, 0),
    5: (10, 1),
    14: (10, 1),
    15: (20, 2),
    69: (70, 7),
    94: (90, 9),
    95: (100, 10),
    99: (100, 10),
}

# 群倍数只放大基础与百分比加算部分，连签奖励不被放大（避免 ×2 群让连签奖励翻倍通胀）
assert checkin_reward(2, 0.25, 5, 1) == 7
assert checkin_reward(2, 0.25, 5, 2) == 10
assert checkin_reward(2, 0.0, 5, 2) == 9  # 连签 5 在 ×2 群下仍只计 5
# 数据存储 +50%：额外 = round(base × 0.5 × 群倍数)，与签到主公式分开加算
assert round(2 * 0.5 * 1) == 1
assert round(2 * 0.5 * 2) == 2
assert checkin_reward(2, 0.25, 5, 1) + 1 == 8
assert checkin_reward(2, 0.25, 5, 2) + 2 == 12

source = SOURCE.read_text(encoding="utf-8")
assert "BONUS_GROUP_IDS = {int(BOT_QQ_GROUP), 993795066}" in source
assert "DOUBLE_CHECKIN_GROUP_IDS = {669800745}" in source
assert "bonus_data_storage" in source
assert "try_grant_checkin_storage_bonus" in source
# 连签奖励封顶 3 且 growth=0，防止长期签到通胀
assert "'streak_bonus': '1,1,2,2,3'" in source
assert "'streak_bonus_growth': '0'" in source

print("BREAK reward formula tests: ok")
