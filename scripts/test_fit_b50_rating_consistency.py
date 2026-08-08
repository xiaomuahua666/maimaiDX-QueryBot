"""
拟合 b50 评级一致性测试

问题背景：
  拟合 b50 原本用一张独立的 _FIT_B50_TABLE_THRESHOLDS 表，
  其评级阈值和系数与真实 b50 的 computeRa 不一致：
    - 91~94% 区间被标为 'S'，系数 20.96（比真实 S 档 20.0 还高）
    - 94~97% 区间被标为 'Sp'，应该是 'AAA'
    - 50~90% 整体评级降一档
  导致：拟合 b50 图标与真实 b50 不一致，且低完成率拿到不合理的高分。

修复方案 A：
  computeRa_fit_b50 直接复用 computeRa，仅把 ds 替换为 fit_diff。
  这样评级和系数完全对齐标准 maimai DX 规则。

测试策略：
  maimaidx_best_50.py 顶部依赖 nonebot / PIL / 项目内部模块，
  直接 import 会触发重运行时。这里用 AST 从源码中独立提取
  computeRa 与 computeRa_fit_b50 两个纯函数，在隔离命名空间中 exec，
  然后对它们做行为等价性测试。
"""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "libraries" / "maimaidx_best_50.py").read_text(encoding="utf-8")

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def _extract_funcs(source: str, names: list[str]) -> dict:
    """从源码中按名字提取顶层 FunctionDef，连带其引用的模块级常量赋值
    与辅助函数（以 _FIT_B50_ 开头的所有顶层定义）一起提取，
    在隔离命名空间 exec 后返回可调用对象。
    这样既能跑原 bug 版本（依赖常量表+辅助函数），也能跑修复后版本（只调 computeRa）。
    """
    tree = ast.parse(source)
    wanted = set(names)
    found_funcs = {}
    helpers = []  # fit_b50 相关的辅助函数，按定义顺序保留
    const_assigns = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in wanted:
                found_funcs[node.name] = node
            elif "fit_b50" in node.name.lower():
                helpers.append(node)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and "fit_b50" in tgt.id.lower():
                    const_assigns.append(node)
                    break
        elif isinstance(node, ast.AnnAssign):
            # 带类型注解的常量赋值，如 _FIT_B50_TABLE_THRESHOLDS: List[...] = [...]
            tgt = node.target
            if isinstance(tgt, ast.Name) and "fit_b50" in tgt.id.lower():
                # 转成无注解的普通赋值，避免 typing 类型未导入
                new_assign = ast.Assign(targets=[tgt], value=node.value)
                ast.fix_missing_locations(new_assign)
                const_assigns.append(new_assign)
    missing = wanted - set(found_funcs)
    if missing:
        raise RuntimeError(f"未在源码中找到函数: {missing}")
    module_src = "from typing import Tuple, Union, List\n\n"
    # 先放辅助函数（_build_* 会被 const_assigns 调用，需先定义）
    for h in helpers:
        module_src += ast.unparse(h) + "\n\n"
    # 再放常量赋值
    for ca in const_assigns:
        module_src += ast.unparse(ca) + "\n"
    module_src += "\n"
    # 最后放目标函数
    for name in names:
        module_src += ast.unparse(found_funcs[name]) + "\n\n"
    ns = {}
    exec(compile(module_src, "<fit_b50_test>", "exec"), ns)
    return {name: ns[name] for name in names}


funcs = _extract_funcs(SRC, ["computeRa", "computeRa_fit_b50"])
computeRa = funcs["computeRa"]
computeRa_fit_b50 = funcs["computeRa_fit_b50"]


# ===== 1. computeRa_fit_b50 与 computeRa(israte=True) 完全等价 =====
print("== 1. computeRa_fit_b50 与标准 computeRa 等价性 ==")
equiv_cases = [
    (13.0, 92.0),   # 原 bug 区：被标为 S
    (13.0, 95.0),   # 原 bug 区：被标为 Sp
    (13.0, 96.5),   # 原 bug 区：被标为 Sp
    (13.0, 97.0),   # 原标 SS，应为 S
    (13.0, 97.5),   # 原标 SS，应为 S
    (13.0, 98.0),   # SS
    (13.0, 99.5),   # SSp
    (13.0, 100.5),  # SSSp
    (7.0, 55.0),    # 原标 D，应为 C
    (10.0, 75.0),   # 原 BB，应为 BBB
    (12.0, 85.0),   # 原 BBB，应为 A
    (15.0, 50.0),   # D
    (1.0, 49.99),   # <50
    (3.5, 99.999),  # 边界 99.999
    (13.0, 100.0),  # SSS 边界
]
for ds, ach in equiv_cases:
    exp_ra, exp_rate = computeRa(ds, ach, israte=True)
    act_ra, act_rate = computeRa_fit_b50(ds, ach)
    check(
        f"等价 ds={ds} ach={ach}",
        (act_ra, act_rate) == (exp_ra, exp_rate),
        f"fit_b50=({act_ra},{act_rate}) 标准=({exp_ra},{exp_rate})",
    )


# ===== 2. 关键回归点：原 bug 档位必须有正确评级 =====
print("\n== 2. 原 bug 档位评级回归 ==")

ra, rate = computeRa_fit_b50(13.0, 92.0)
check("92% 应为 AA（原 bug 为 S）", rate == 'AA', f"实际 {rate}")
# ra = int(13 * 0.92 * 15.2) = int(181.792) = 181
check("92%+ds13 的 ra=181（原 bug 给 251）", ra == 181, f"实际 {ra}")

for ach in (94.5, 95.0, 96.5):
    ra, rate = computeRa_fit_b50(13.0, ach)
    check(f"{ach}% 应为 AAA（原 bug 为 Sp）", rate == 'AAA', f"实际 {rate}")

ra, rate = computeRa_fit_b50(13.0, 97.5)
check("97.5% 应为 S（原 bug 为 SS）", rate == 'S', f"实际 {rate}")

_, rate = computeRa_fit_b50(13.0, 85.0)
check("85% 应为 A（原 bug 为 BBB）", rate == 'A', f"实际 {rate}")

_, rate = computeRa_fit_b50(13.0, 55.0)
check("55% 应为 C（原 bug 为 D）", rate == 'C', f"实际 {rate}")


# ===== 3. 低完成率不应拿到高分（核心存疑点） =====
print("\n== 3. 低完成率不应拿到高分 ==")
ra_92, _ = computeRa_fit_b50(13.0, 92.0)   # AA 档
ra_97, _ = computeRa_fit_b50(13.0, 97.0)   # S 档
# 92% 的 ra 必须明显小于 97% 的 ra（原 bug 下两者几乎相等：181 vs 252）
check(
    "92% 的 ra 明显小于 97%（原 bug 下两者差值过小）",
    ra_92 < ra_97 * 0.85,
    f"ra_92={ra_92} ra_97={ra_97}",
)


# ===== 4. 源码结构检查：不应残留独立的拟合评级表 =====
print("\n== 4. 源码结构：拟合 b50 不应残留独立评级表 ==")
check(
    "_FIT_B50_TABLE_THRESHOLDS 应被删除",
    "_FIT_B50_TABLE_THRESHOLDS" not in SRC,
    "残留 _FIT_B50_TABLE_THRESHOLDS（独立评级表未清理）",
)
check(
    "_FIT_B50_RATING_TABLE 应被删除",
    "_FIT_B50_RATING_TABLE" not in SRC,
    "残留 _FIT_B50_RATING_TABLE",
)
check(
    "_get_fit_b50_table_column 应被删除",
    "_get_fit_b50_table_column" not in SRC,
    "残留 _get_fit_b50_table_column",
)
# computeRa_fit_b50 体内应直接调用 computeRa（复用标准逻辑）
import re
fit_body = re.search(
    r"def computeRa_fit_b50\([^)]*\)[^:]*:\s*(.*?)(?=\n(?:def |async def |class |\Z))",
    SRC,
    re.DOTALL,
)
fit_body_text = fit_body.group(1) if fit_body else ""
check(
    "computeRa_fit_b50 应直接调用 computeRa",
    "computeRa(" in fit_body_text,
    f"函数体未调用 computeRa: {fit_body_text.strip()[:120]}",
)


print(f"\n总计: {PASS} 通过 / {FAIL} 失败")
sys.exit(0 if FAIL == 0 else 1)
