"""星极完成表 ValueError 回归测试。

用户发送「星极完成表」时报「未知错误：ValueError 请联系Bot管理员」。

根因：draw_plate_table 在拼接 plate_version 徽标图片路径时，误用了短名 `version`
（如 '星'），但 plate_version 资源包实际用的是组合键命名（如 '宙&星極.png'，
由 maimaidx_rating_compare.py:88 plate_version_path(userinfo.plate) 证实）。
'星' 不在 platecn 中，version_map['星'] = (['maimai でらっくす UNiVERSE'], '宙&星')，
所以 _ver='宙&星' 才是正确的文件名前缀。

修复：line 1002 用 _ver 替代 version 拼接徽标路径。

本测试用源码静态断言，避免依赖 Pillow 资源文件。
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
src = (ROOT / "libraries" / "maimaidx_music_info.py").read_text(encoding="utf-8")

# 1. 定位 draw_plate_table 内 Image.open(plate_versiondir / ...) 那一行
#    该行用 f-string 拼接徽标文件名
anchor = "Image.open(plate_versiondir / f'"
assert anchor in src, "未找到 plate_versiondir 徽标路径拼接行"
start = src.index(anchor)
# 取该行完整内容（到行尾）
line_end = src.index("\n", start)
badge_line = src[start:line_end]

# 2. 文件名前缀必须是 _ver，不能是 version
#    断言 f-string 内引用的是 {_ver} 而非 {version}
assert "{_ver}" in badge_line, (
    f"plate_version 徽标路径应使用 _ver（组合键，如 '宙&星'），"
    f"实际拼接行：{badge_line.strip()}"
)
assert "{version}" not in badge_line, (
    f"plate_version 徽标路径不应使用 version（短名，如 '星'），"
    f"会导致文件名错误（星極.png 而非 宙&星極.png），"
    f"实际拼接行：{badge_line.strip()}"
)

# 3. plan -> 極 的映射逻辑保留
assert '"極" if plan == "极" else plan' in badge_line, (
    "plan='极' → '極' 的映射逻辑应保留"
)

# 4. 底图路径（plate_tabledir）必须仍用短名 version，不能用 _ver
#    原因：update_plate_table 生成底图时用 self.version_list（短名），
#    文件名是 {短名}.png（如 星.png），与徽标资源（组合键+plan）命名规则不同。
#    验证 plate_file 赋值行仍用 version。
plate_file_line_start = src.index("plate_file = ")
plate_file_line_end = src.index("\n", plate_file_line_start)
plate_file_line = src[plate_file_line_start:plate_file_line_end]
assert "{version}" in plate_file_line or "version" in plate_file_line, (
    f"底图路径 plate_file 应使用短名 version，实际：{plate_file_line.strip()}"
)
assert "_ver" not in plate_file_line, (
    f"底图路径 plate_file 不应使用 _ver（组合键），否则会找不到底图文件。"
    f"实际：{plate_file_line.strip()}"
)

print("star plate badge path tests: ok")
