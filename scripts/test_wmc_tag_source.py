"""谱面标签必须只使用 v.wmc.pub，禁止恢复本地 JSON 兜底。"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MUSIC_INFO = (ROOT / "libraries" / "maimaidx_music_info.py").read_text(
    encoding="utf-8"
)
SEARCH = (ROOT / "command" / "mai_search.py").read_text(encoding="utf-8")
WEAKNESS = (ROOT / "libraries" / "maimaidx_weakness_prescription.py").read_text(
    encoding="utf-8"
)
HEAD_TO_HEAD = (ROOT / "libraries" / "maimaidx_head_to_head.py").read_text(
    encoding="utf-8"
)

for legacy_symbol in (
    "_load_tags_from_json",
    "_get_tags_from_file",
    "get_music_tags_by_difficulty",
    "get_chart_tags_by_group",
):
    assert legacy_symbol not in MUSIC_INFO, f"仍存在本地标签入口: {legacy_symbol}"

assert "build_tags_forward_nodes" not in SEARCH, "歌曲详情仍重复展示旧标签模块"
assert "fetch_b50_wmc_tags" in WEAKNESS, "弱项处方未使用 WMC B50 标签"
assert "fetch_b50_wmc_tags" in HEAD_TO_HEAD, "对战战绩未使用 WMC B50 标签"
assert "cfg_tags = _wmc_config_tags(tags_data) if tags_data else []" in WEAKNESS

print("wmc tag source tests: ok")
