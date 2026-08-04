#!/usr/bin/env python3
"""Long official-QQ guess rounds use active group sends, not one msg_id replies."""

from __future__ import annotations

import asyncio
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

import nonebot_plugin_maimaidx  # noqa: F401, E402
from nonebot_plugin_maimaidx.command import mai_guess  # noqa: E402


class Event:
    __module__ = "nonebot.adapters.qq.event"
    group_openid = "guess-active-group"
    author = SimpleNamespace(username="答题用户")

    def get_user_id(self):
        return "guess-user-openid"


class Matcher:
    def __init__(self):
        self.sent = []

    async def send(self, message, **kwargs):
        self.sent.append((message, kwargs))


async def main() -> None:
    event = Event()
    matcher = Matcher()
    active = []

    class Bot:
        pass

    bot = Bot()
    originals = {
        "use_qq_mode": mai_guess.use_qq_mode,
        "resolve_event_bot": mai_guess.resolve_event_bot,
        "send_group_message": mai_guess.send_group_message,
    }
    mai_guess.use_qq_mode = lambda _event: True
    mai_guess.resolve_event_bot = lambda _event: bot

    async def fake_active(received_bot, gid, message):
        active.append((received_bot, gid, message))

    mai_guess.send_group_message = fake_active
    try:
        # Seven timer/hint messages must not consume the original command's
        # passive-reply allowance.
        for index in range(7):
            await mai_guess._safe_matcher_send(
                matcher, event, f"{index + 1}/7", "guess-active-group"
            )
        assert len(active) == 7
        assert not matcher.sent

        # A direct answer acknowledgement remains a passive reply and carries
        # a real official-QQ @ prefix for the user who answered.
        await mai_guess._safe_matcher_send(
            matcher, event, "猜对了！", "guess-active-group", reply=True
        )
        assert len(matcher.sent) == 1
        payload = matcher.sent[0][0]
        assert getattr(payload[0], "type", None) == "text"
        assert payload[0].data["text"] == (
            '<qqbot-at-user id="guess-user-openid" />'
        )
    finally:
        mai_guess.use_qq_mode = originals["use_qq_mode"]
        mai_guess.resolve_event_bot = originals["resolve_event_bot"]
        mai_guess.send_group_message = originals["send_group_message"]


asyncio.run(main())
print("qq guess active delivery tests: ok")
