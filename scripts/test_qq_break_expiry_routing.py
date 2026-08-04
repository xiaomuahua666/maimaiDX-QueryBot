#!/usr/bin/env python3
"""A legacy-group BREAK expiry notice must use the mapped official QQ Bot."""

from __future__ import annotations

import asyncio
import os
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path = [
    item for item in sys.path if item and Path(item).resolve() != ROOT
]
sys.path.insert(0, str(ROOT.parent))

os.environ.setdefault("MAIMAIDXPATH", str(ROOT / "static"))
os.environ.setdefault("MAIMAIDX_STORAGE_FAIL_FAST", "false")
os.environ.setdefault("MAIMAIDX_MESSAGE_STATS_ENABLED", "false")

import nonebot

nonebot.init()

from nonebot_plugin_maimaidx.command import mai_break  # noqa: E402
from nonebot_plugin_maimaidx.libraries import maimaidx_platform as platform  # noqa: E402


LEGACY_GROUP = 993795066
GROUP_OPENID = "break-expiry-group-openid"


class FakeOneBot:
    def __init__(self):
        self.sent = []

    async def send_group_msg(self, **data):
        self.sent.append(data)


FakeOneBot.__module__ = "nonebot.adapters.onebot.v11.bot"


class FakeQQBot:
    def __init__(self):
        self.sent = []

    async def send_to_group(self, **data):
        self.sent.append(data)


FakeQQBot.__module__ = "nonebot.adapters.qq.bot"


async def main() -> None:
    onebot = FakeOneBot()
    qq = FakeQQBot()

    # Preserve insertion order to reproduce the old bug: OneBot was first and
    # ``next(iter(get_bots().values()))`` silently targeted the former group.
    mai_break.get_bots = lambda: {"onebot": onebot, "qq": qq}
    mai_break.break_db.expire_red_packets = lambda: [
        types.SimpleNamespace(
            packet_id="RP-TEST",
            refund=12,
            sender_qqid=123456,
            group_id=LEGACY_GROUP,
        )
    ]
    platform.qq_bind_db.get_platform_group_id = lambda gid: (
        GROUP_OPENID if int(gid) == LEGACY_GROUP else None
    )

    await mai_break._expire_break_red_packets()

    assert not onebot.sent
    assert len(qq.sent) == 1
    assert qq.sent[0]["group_openid"] == GROUP_OPENID
    assert "RP-TEST" in str(qq.sent[0]["message"])


asyncio.run(main())
print("qq BREAK expiry routing tests: ok")
