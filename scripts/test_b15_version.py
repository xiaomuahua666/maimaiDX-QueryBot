"""
B15 / B35 分类回归测试（独立运行，无需 nonebot 运行时）。

背景：b50 = B35(旧版本) + B15(新版本)。
  - B15(新曲) = 当前版本曲目（国服 PRiSM PLUS = 镜彩世代）
  - B35(旧曲) = 之前所有版本曲目
水鱼查分器 charts.dx(B15)/sd(B35) 按曲库 is_new(新曲标记) 划分；
变体 b50(ap50/fc50/拟合…) 与「上分」必须用同一口径，否则 B35 会混入 B15。

本测试验证：
  1. config 版本表：镜彩为最新世代、plate_to_dx_version 不含 CiRCLE
  2. 分类口径 _music_is_new 的「反混入」分区保证：
     - 水鱼形态(有 is_new)：新列只含新曲，旧列只含旧曲，二者不相交
     - 精简源形态(无 is_new)：回退版本名集合，结论一致
  3. 截图中误入新版本列的 BUDDiES(双/宴) 等旧曲，新口径下一定归入 B35

运行：python3 scripts/test_b15_version.py
"""
import json
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
package = types.ModuleType("nonebot_plugin_maimaidx")
package.__path__ = [str(ROOT)]
sys.modules["nonebot_plugin_maimaidx"] = package


def _install_nonebot_stub():
    nb = types.ModuleType("nonebot")

    class _Cfg:
        nickname = []

    class _Driver:
        config = _Cfg()

    _static = tempfile.mkdtemp(prefix="maimai_static_")

    class _PluginCfg:
        maimaidxtoken = None
        maimaidxpath = _static
        maimaidx_music_cache_seconds = 3600
        maimaidx_data_source = None

        def __getattr__(self, name):
            return None

    nb.get_driver = lambda: _Driver()
    nb.get_plugin_config = lambda _model: _PluginCfg()
    sys.modules["nonebot"] = nb


_install_nonebot_stub()
from nonebot_plugin_maimaidx import config as cfg  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


# 与 libraries/maimaidx_best_50.py::_music_is_new 完全一致的纯逻辑复刻
def music_is_new(is_new_flag, version, lib_has_new_flag, b15_set):
    if lib_has_new_flag:
        return bool(is_new_flag)
    return version in b15_set


print("== 1. 配置版本表 ==")
dx_values = list(cfg.plate_to_dx_version.values())
print("  末两作(应为镜彩):", dx_values[-2:])
check("末两作为 PRiSM / PRiSM PLUS",
      dx_values[-2:] == ['maimai でらっくす PRiSM', 'maimai でらっくす PRiSM PLUS'],
      f"实际={dx_values[-2:]}")
check("plate_to_dx_version 不含 CiRCLE",
      not any('CiRCLE' in v for v in dx_values),
      f"含CiRCLE项={[v for v in dx_values if 'CiRCLE' in v]}")

b15_set = set(cfg.get_b15_version_names_at_generation(0))
print("  当前 B15 版本名集合:", sorted(b15_set))
check("B15 集合 = 镜彩(PRiSM / PRiSM PLUS)",
      {'PRiSM', 'PRiSM PLUS'} <= b15_set
      and not any('BUDDiES' in v or 'CiRCLE' in v for v in b15_set))


# ---- 用真实 dxdata.json 提供「曲目 -> 版本」分布 ----
data = json.load(open(ROOT / "dxdata.json", encoding="utf-8"))
# 按 internalId 去重（真实代码 mai.total_list.by_id 每个 id 只对应一条曲目）
_seen = set()
songs = []  # (iid, title, version)
for song in data["songs"]:
    sheets = song.get("sheets") or []
    if not sheets:
        continue
    sh = sheets[0]
    iid = sh.get("internalId")
    if iid in _seen:
        continue
    _seen.add(iid)
    songs.append((iid, song.get("title"), sh.get("version")))

# 国服当前世代版本名（用于模拟水鱼 is_new 与精简源回退）
current_versions = {'PRiSM', 'PRiSM PLUS',
                    'maimai でらっくす PRiSM', 'maimai でらっくす PRiSM PLUS'}

# 截图中曾被错误塞进「新版本列」的旧曲（BUDDiES 双/宴）
screenshot_leaked = {11717: "LOSTPHANTASIA", 11696: "アイドル",
                     11693: "過去を喰らう", 11666: "アイディスマイル"}


def run_partition(label, lib_has_new):
    print(f"== 分区测试（{label}）==")
    new_col, old_col = [], []
    for iid, title, ver in songs:
        # 水鱼形态：is_new 即「版本属于当前世代」（模拟国服水鱼标记）
        is_new_flag = ver in current_versions
        if music_is_new(is_new_flag, ver, lib_has_new, b15_set):
            new_col.append((iid, ver))
        else:
            old_col.append((iid, ver))

    new_ids = {i for i, _ in new_col}
    old_ids = {i for i, _ in old_col}
    check(f"[{label}] 新/旧列不相交(无混入)", new_ids.isdisjoint(old_ids))
    check(f"[{label}] 新列全部为当前世代(镜彩)",
          all(v in current_versions for _, v in new_col),
          f"异常={[x for x in new_col if x[1] not in current_versions][:5]}")
    check(f"[{label}] 旧列不含任何镜彩曲",
          all(v not in current_versions for _, v in old_col),
          f"异常={[x for x in old_col if x[1] in current_versions][:5]}")
    check(f"[{label}] 新列非空", len(new_col) > 0, f"数量={len(new_col)}")
    # 截图泄漏的 BUDDiES 旧曲必须归入旧列(B35)
    leaked_now_old = all(iid in old_ids for iid in screenshot_leaked)
    check(f"[{label}] 截图误入新列的 BUDDiES 旧曲全部归回 B35", leaked_now_old,
          f"仍在新列={[i for i in screenshot_leaked if i in new_ids]}")
    print(f"  新列(镜彩)={len(new_col)}  旧列={len(old_col)}")


# 水鱼形态：曲库带 is_new 标记（用户实际运行的默认数据源）
run_partition("水鱼/有is_new", lib_has_new=True)
# 精简源形态：曲库无 is_new 标记，回退到版本名集合判定
run_partition("精简源/无is_new回退版本名", lib_has_new=False)

print()
print(f"结果: PASS={PASS}  FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
