"""AWMC v2 单条成绩编辑/删除解析与计费契约测试。"""

import ast
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional


ROOT = Path(__file__).resolve().parent.parent
PATH = ROOT / "command" / "mai_account.py"
tree = ast.parse(PATH.read_text(encoding="utf-8"))
names = {
    "_parse_difficulty",
    "_resolve_account_music",
    "_validate_music_difficulty",
    "_parse_achievement",
    "_parse_dx_score",
    "_chart_max_dx_score",
    "_parse_score_options",
    "_parse_music_upsert_command",
    "_parse_music_delete_command",
    "_parse_item_kind",
    "_parse_item_operation",
    "_parse_item_upsert_command",
    "_is_interaction_cancel",
}
selected = [
    node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names
]
assert {node.name for node in selected} == names


class MusicList(list):
    def by_id(self, value):
        return next((music for music in self if music.id == str(value)), None)

    def by_title(self, value):
        return next((music for music in self if music.title == value), None)


class AliasList:
    def by_alias(self, value):
        return [SimpleNamespace(SongID=11479)] if value == "测试别名" else []


charts = [SimpleNamespace(notes=(100, 50, 50, 10, 10)) for _ in range(5)]
music = SimpleNamespace(
    id="11479",
    title="Test Song With Spaces",
    charts=charts,
    basic_info=SimpleNamespace(genre="POPSアニメ"),
)
mai = SimpleNamespace(total_list=MusicList([music]), total_alias_list=AliasList())
namespace = {
    "Any": Any,
    "Optional": Optional,
    "re": re,
    "mai": mai,
    "_DIFFICULTY_ALIASES": {
        "0": 0, "basic": 0, "绿": 0,
        "1": 1, "adv": 1, "黄": 1,
        "2": 2, "exp": 2, "红": 2,
        "3": 3, "mas": 3, "紫": 3,
        "4": 4, "re:master": 4, "白": 4,
        "10": 10, "宴": 10,
    },
    "_DIFFICULTY_LABELS": {0: "BASIC", 1: "ADV", 2: "EXP", 3: "MAS", 4: "Re:MAS", 10: "宴"},
    "_COMBO_ALIASES": {"none": "none", "fc": "fc", "ap": "ap"},
    "_SYNC_ALIASES": {"none": "none", "fs": "fs", "fdx": "fsd"},
    "_ITEM_KIND_INPUTS": {"称号": 2, "角色": 9, "钥匙": 15},
    "_INTERACTION_CANCEL_WORDS": {"取消", "cancel", "q", "退出", "00"},
}
exec(compile(ast.Module(body=selected, type_ignores=[]), str(PATH), "exec"), namespace)

assert namespace["_parse_difficulty"]("Re:MASTER") == 4
assert namespace["_parse_difficulty"]("紫") == 3
assert namespace["_parse_achievement"]("100.5%") == 100.5
assert namespace["_parse_achievement"]("0.995") == 99.5
assert namespace["_resolve_account_music"]("测试别名") is music

resolved, level, simple = namespace["_parse_music_upsert_command"](
    "Test Song With Spaces MAS 100.5% 5 AP FDX"
)
assert resolved is music and level == 3
assert simple == {
    "musicId": 11479,
    "level": 3,
    "achievement": 100.5,
    "dxScore": 5,
    "comboStatus": "ap",
    "syncStatus": "fsd",
    "fuzzy": True,
}

_, _, professional = namespace["_parse_music_upsert_command"](
    "测试别名 紫 99.5 600 FC FS"
)
assert professional["fuzzy"] is False
assert professional["dxScore"] == 600

try:
    namespace["_parse_score_options"](["100%", "9999", "专业"], music, 3)
except ValueError as exc:
    assert "满分" in str(exc)
else:
    raise AssertionError("专业模式必须校验谱面 DX 满分")

deleted_music, deleted_level = namespace["_parse_music_delete_command"](
    "Test Song With Spaces Re:MASTER"
)
assert deleted_music is music and deleted_level == 4
assert namespace["_parse_item_upsert_command"]("称号 123 add") == (2, 123, "add")
assert namespace["_parse_item_upsert_command"]("15 456 删除") == (15, 456, "del")
for cancel_word in ("取消", "cancel", "Q", "退出", "00"):
    assert namespace["_is_interaction_cancel"](cancel_word)

source = PATH.read_text(encoding="utf-8")
assert 'account_music_upsert = on_command(' in source and '"mai改成绩", aliases=' in source
assert 'account_music_delete = on_command(' in source and '"mai删成绩", aliases=' in source
assert 'break_db.get_config("awmc_music_upsert_cost", "75")' in source
assert 'break_db.get_config("awmc_music_delete_cost", "50")' in source
assert 'account_ticket_clear = on_command("mai清票"' in source
assert 'account_item_upsert = on_command("mai改道具"' in source
for alias in ('"改成绩"', '"改分"', '"删成绩"', '"删分"', '"清票"', '"改道具"'):
    assert alias in source
assert 'break_db.get_config("awmc_ticket_clear_cost", "10")' in source
assert 'break_db.get_config("awmc_item_upsert_cost", "100")' in source
assert "我已知晓风险" in source
assert source.count("已取消道具修改，本次不扣 BREAK") >= 4
assert "未经实际账号测试" in source
assert 'on_command("mai批量' not in source
write_block = source[
    source.index("async def _run_music_write("):
    source.index("async def _finish_music_write_error(")
]
assert write_block.index("await sw_api.upsert_music(") < write_block.index(
    "charge = break_db.settle_service_success("
)
assert write_block.index("await sw_api.delete_music(") < write_block.index(
    "charge = break_db.settle_service_success("
)

client_source = (ROOT / "libraries" / "maimaidx_sw_api.py").read_text(encoding="utf-8")
assert 'self._api_path("music/upsert")' in client_source
assert 'self._api_path("music/delete")' in client_source
assert 'self._api_path("ticket/clear")' in client_source
assert 'self._api_path("item/upsert")' in client_source
assert client_source.count("retry_count=0") >= 7

break_source = (ROOT / "libraries" / "maimaidx_break.py").read_text(encoding="utf-8")
assert "'awmc_music_upsert_cost': '75'" in break_source
assert "'awmc_music_delete_cost': '50'" in break_source
assert "'awmc_ticket_clear_cost': '10'" in break_source
assert "'awmc_item_upsert_cost': '100'" in break_source

print("awmc music edit tests: ok")
