#!/usr/bin/env python3
"""help：官方 QQ 未绑定只给 qbind，绑定后单条展示热门功能。"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path = [item for item in sys.path if item and Path(item).resolve() != ROOT]
sys.path.insert(0, str(ROOT.parent))
os.environ.setdefault("COMMAND_START", '["", "/"]')
os.environ.setdefault("MAIMAIDXPATH", str(ROOT / "static"))
os.environ.setdefault("MAIMAIDX_STORAGE_FAIL_FAST", "false")
os.environ.setdefault("MAIMAIDX_MESSAGE_STATS_ENABLED", "false")
os.environ.setdefault("MAIMAIDX_BUSY_SURCHARGE_ENABLED", "false")

import nonebot

nonebot.init()

from nonebot_plugin_maimaidx.command import mai_base  # noqa: E402


qq_event = SimpleNamespace(
    group_openid="group-openid",
    get_user_id=lambda: "help-openid",
)
onebot_event = SimpleNamespace(
    user_id=123456789,
    get_user_id=lambda: "123456789",
)


def keyboard_buttons(message) -> list[dict]:
    assert [segment.type for segment in message] == ["markdown", "keyboard"]
    keyboard = message[1].data["keyboard"].model_dump(exclude_none=True)
    rows = keyboard["content"]["rows"]
    assert len(rows) <= 5
    assert all(len(row["buttons"]) <= 3 for row in rows)
    return [button for row in rows for button in row["buttons"]]


original_get_legacy_qq = mai_base.qq_bind_db.get_legacy_qq
try:
    mai_base.qq_bind_db.get_legacy_qq = lambda _pid: None
    unbound = mai_base._qq_help_message(qq_event)
    unbound_buttons = keyboard_buttons(unbound)
    assert len(unbound_buttons) == 1
    assert unbound_buttons[0]["render_data"]["label"] == "绑定 qbind"
    assert unbound_buttons[0]["action"]["data"] == "qbind"
    assert "尚未绑定" in unbound[0].data["markdown"].content

    mai_base.qq_bind_db.get_legacy_qq = lambda _pid: 123456789
    bound = mai_base._qq_help_message(qq_event)
    bound_buttons = keyboard_buttons(bound)
    # Exactly one full keyboard: popular commands only, no multi-message menu.
    assert len(bound_buttons) == 15
    commands = {
        button["render_data"]["label"]: button["action"]["data"]
        for button in bound_buttons
    }
    assert commands == {
        "标准 B50": "b50",
        "刷新 B50": "刷新b50",
        "B50 锐评": "锐评一下",
        "AP50": "ap50",
        "FC50": "fc50",
        "吃分推荐": "吃分推荐",
        "含金量": "含金量",
        "含水量": "含水量",
        "MyMai": "mymai",
        "签到": "签到",
        "猜歌": "猜歌",
        "猜封面": "猜封面",
        "今日舞萌": "今日舞萌",
        "查歌": "查歌",
        "完整文档": "帮助 文档",
    }
    assert "最常用" in bound[0].data["markdown"].content

    docs = mai_base._qq_help_message(qq_event, "文档")
    assert "https://wiki.awmc.team/guide/bot/intro" in (
        docs[0].data["markdown"].content
    )
finally:
    mai_base.qq_bind_db.get_legacy_qq = original_get_legacy_qq

# OneBot keeps the existing concise wiki response and receives no QQ menu.
assert mai_base._qq_help_message(onebot_event) is None

source = (ROOT / "command" / "mai_base.py").read_text(encoding="utf-8")
assert "on_command('帮助', aliases={'help'" in source
assert "_maimaidx_qbind_exempt" in source
assert "机器人帮助请前往" in source

print("help qbind/popular-menu tests: ok")
