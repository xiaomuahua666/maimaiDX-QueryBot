#!/usr/bin/env python3
"""Regression test for querying another user's AWMC profile via @mention."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path = [
    item
    for item in sys.path
    if item and Path(item).resolve() != ROOT
]
sys.path.insert(0, str(ROOT.parent))

os.environ.setdefault("COMMAND_START", '["", "/"]')
os.environ.setdefault("MAIMAIDXPATH", str(ROOT / "static"))
os.environ.setdefault("MAIMAIDX_STORAGE_FAIL_FAST", "false")
os.environ.setdefault("MAIMAIDX_MESSAGE_STATS_ENABLED", "false")
os.environ.setdefault("MAIMAIDX_BUSY_SURCHARGE_ENABLED", "false")

import nonebot

nonebot.init()

import nonebot_plugin_maimaidx  # noqa: F401, E402
from nonebot.adapters.qq.event import GroupAtMessageCreateEvent  # noqa: E402
from nonebot.adapters.qq.models import GroupMemberAuthor, GroupMentionUser  # noqa: E402
from nonebot_plugin_maimaidx.command import mai_break  # noqa: E402


def make_target_event() -> GroupAtMessageCreateEvent:
    return GroupAtMessageCreateEvent(
        id="target-profile-message",
        content=" 我的AWMC",
        timestamp="2026-08-10T14:00:00+08:00",
        mentions=[
            GroupMentionUser(
                scope="single",
                bot=True,
                id="bot-openid",
                is_you=True,
                member_openid="bot-openid",
                username="AWMC BOT",
            ),
            GroupMentionUser(
                scope="single",
                bot=False,
                id="target-openid",
                is_you=False,
                member_openid="target-openid",
                username="目标用户",
            ),
        ],
        author=GroupMemberAuthor(
            id="sender-id",
            bot=False,
            member_openid="sender-openid",
            username="发送者",
        ),
        group_id="legacy-group-token",
        group_openid="group-openid",
    )


async def main() -> None:
    required_targets: list[str | None] = []
    profile_requests: list[int] = []
    render_requests: list[tuple[str, str]] = []
    sent: list[tuple[object, dict]] = []

    original_require_account_qqid = mai_break.require_account_qqid
    original_get_account_profile = mai_break.get_account_profile
    original_render_awmc_overview = mai_break._render_awmc_overview
    original_run_image_cpu = mai_break.run_image_cpu
    original_plugin_finish = mai_break.plugin_finish

    def require_target(_event, target=None):
        required_targets.append(target)
        return 987654321

    def get_profile(qqid: int):
        profile_requests.append(qqid)
        return {"qqid": qqid}

    def render_profile(_profile, *, display_name: str, title: str):
        render_requests.append((display_name, title))
        return "target-profile-image"

    async def run_now(func, *args, **kwargs):
        return func(*args, **kwargs)

    async def finish(_matcher, message, **kwargs):
        sent.append((message, kwargs))

    mai_break.require_account_qqid = require_target
    mai_break.get_account_profile = get_profile
    mai_break._render_awmc_overview = render_profile
    mai_break.run_image_cpu = run_now
    mai_break.plugin_finish = finish
    try:
        handler = mai_break.my_awmc.handlers[0].call
        await handler(object(), make_target_event())
    finally:
        mai_break.require_account_qqid = original_require_account_qqid
        mai_break.get_account_profile = original_get_account_profile
        mai_break._render_awmc_overview = original_render_awmc_overview
        mai_break.run_image_cpu = original_run_image_cpu
        mai_break.plugin_finish = original_plugin_finish

    assert required_targets == ["target-openid"]
    assert profile_requests == [987654321]
    assert render_requests == [("目标用户", "目标用户 的 AWMC 账号")]
    assert len(sent) == 1
    message, kwargs = sent[0]
    assert message == "target-profile-image"
    assert kwargs["reply_message"] is True
    assert kwargs["publish_qq_image"] is True
    print("my AWMC target tests: ok")


asyncio.run(main())
