"""B50 掩码判定：放大 10000 后末位是 0 触发。"""

import sys
import types
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent.parent

# 用一个虚拟包承载 libraries.maimaidx_b50_warnings，避免相对导入失败
pkg = types.ModuleType("libraries")
pkg.__path__ = [str(ROOT / "libraries")]
sys.modules["libraries"] = pkg
model = types.ModuleType("libraries.maimaidx_model")
model.ChartInfo = object
model.UserInfo = object
sys.modules["libraries.maimaidx_model"] = model
sys.path.insert(0, str(ROOT))

from libraries import maimaidx_b50_warnings as bw  # noqa: E402

_chart_achievements = bw._chart_achievements
is_masked_b50 = bw.is_masked_b50


def make_userinfo(values):
    charts = []
    for v in values:
        charts.append(SimpleNamespace(achievements=v, level_index=3, ds=13.0, dxScore=0, dxScoreMax=0))
    return SimpleNamespace(charts=SimpleNamespace(sd=charts[:25], dx=charts[25:]))


# 用户的例子：50 条 100.4950 → 放大 10000 = 1004950 末位 0 → 视为掩码
assert is_masked_b50(make_userinfo([100.4950] * 50)) is True

# 100.0000 整数 50 条 → 1000000 末位 0 → 视为掩码
assert is_masked_b50(make_userinfo([100.0000] * 50)) is True

# 100 整数 50 条
assert is_masked_b50(make_userinfo([100] * 50)) is True

# 99.5000 → 995000 末位 0 → 视为掩码
assert is_masked_b50(make_userinfo([99.5000] * 50)) is True

# 正常浮点 99.5123 → 995123 末位 3 → 不报警
assert is_masked_b50(make_userinfo([99.5123] * 50)) is False

# 50 条里只要有一条末位非 0 → 不报警
mixed = [100.5000] * 49 + [100.5123]
assert is_masked_b50(make_userinfo(mixed)) is False

# 空数据 → 不报警
assert is_masked_b50(make_userinfo([])) is False

# 99.5010 末位 0 → 视为掩码
assert is_masked_b50(make_userinfo([99.5010] * 50)) is True

# 100.5001 末位 1 → 不报警
assert is_masked_b50(make_userinfo([100.5001] * 50)) is False

# 100.4951 末位 1 → 不报警
assert is_masked_b50(make_userinfo([100.4951] * 50)) is False

print("b50 masked warning tests: ok")
