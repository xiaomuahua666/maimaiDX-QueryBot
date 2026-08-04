#!/usr/bin/env python3
"""Official QQ @bot messages must reach command handlers and keep @targets."""

from __future__ import annotations

import os
from types import SimpleNamespace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("COMMAND_START", '["", "/"]')
os.environ.setdefault("MAIMAIDXPATH", str(ROOT / "static"))
os.environ.setdefault("MAIMAIDX_STORAGE_FAIL_FAST", "false")

import nonebot

nonebot.init()

from nonebot.adapters.qq.event import GroupAtMessageCreateEvent
from nonebot.adapters.qq.models import GroupMemberAuthor, GroupMentionUser
from nonebot.consts import PREFIX_KEY
from nonebot.rule import TrieRule, command

from nonebot_plugin_maimaidx.libraries.maimaidx_platform import (
    install_qq_event_compat,
    parse_at_target_id,
)


install_qq_event_compat()
# Installation is intentionally idempotent because editable/dev loaders may
# import the compatibility module before the plugin root.
install_qq_event_compat()

command("锐评一下")
command("我的AWMC")


def event_for(content: str, *, target: bool = False) -> GroupAtMessageCreateEvent:
    author = GroupMemberAuthor(
        id="author-id",
        bot=False,
        member_openid="sender-openid",
        username="测试用户",
    )
    mentions = [
        GroupMentionUser(
            scope="single",
            bot=True,
            id="bot-id",
            is_you=True,
            member_openid="bot-openid",
            username="AWMC BOT",
        )
    ]
    if target:
        mentions.append(
            GroupMentionUser(
                scope="single",
                bot=False,
                id="target-openid",
                is_you=False,
                member_openid="target-openid",
                username="目标用户",
            )
        )
    return GroupAtMessageCreateEvent(
        id="message-id",
        content=f" {content}",
        timestamp="2026-08-04T00:00:00+08:00",
        mentions=mentions,
        author=author,
        group_id="legacy-group-token",
        group_openid="group-openid",
    )


roast_event = event_for("锐评一下", target=True)
assert roast_event.self_id == "bot-id"
segments = list(roast_event.get_message())
assert segments[0].type == "text"
assert segments[0].data.get("text") == "锐评一下"
assert segments[1].type == "mention_user"
assert segments[1].data.get("user_id") == "target-openid"
assert roast_event.get_plaintext() == "锐评一下"

state: dict = {}
TrieRule.get_value(None, roast_event, state)
prefix = state[PREFIX_KEY]
assert prefix["command"] == ("锐评一下",)
assert parse_at_target_id(roast_event) == "target-openid"

awmc_event = event_for("我的AWMC")
state = {}
TrieRule.get_value(None, awmc_event, state)
assert state[PREFIX_KEY]["command"] == ("我的AWMC",)
assert parse_at_target_id(awmc_event) is None

# Tencent's current wire form can leave an empty text segment before the bot
# mention.  It must not hide the command from NoneBot's trie.
modern_roast_event = event_for(
    '<qqbot-at-user id="bot-openid" /> 锐评一下'
)
modern_segments = list(modern_roast_event.get_message())
assert modern_segments[0].type == "text"
assert modern_segments[0].data.get("text") == "锐评一下"
state = {}
TrieRule.get_value(None, modern_roast_event, state)
assert state[PREFIX_KEY]["command"] == ("锐评一下",)

# Older official-QQ adapter builds expose the same target as member_openid/id
# instead of user_id.  The command dependency must accept all three forms.
for field in ("member_openid", "id"):
    legacy_target_event = SimpleNamespace(
        message=[
            SimpleNamespace(
                type="mention_user",
                data={field: "legacy-target-openid"},
            )
        ]
    )
    assert parse_at_target_id(legacy_target_event) == "legacy-target-openid"

print("qq command matching tests: ok")
