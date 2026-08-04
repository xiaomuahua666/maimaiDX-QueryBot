#!/usr/bin/env python3
"""Official QQ guess-rating candidates use seen/bound members, not OneBot APIs."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
os.environ.setdefault("MAIMAIDXPATH", str(ROOT / "static"))
os.environ.setdefault("MAIMAIDX_STORAGE_FAIL_FAST", "false")

import nonebot

nonebot.init()

from nonebot_plugin_maimaidx.libraries import maimaidx_guess_rating as rating  # noqa: E402
from nonebot_plugin_maimaidx.libraries.maimaidx_model import (  # noqa: E402
    ChartInfo,
    Data,
    UserInfo,
)
from nonebot_plugin_maimaidx.libraries.maimaidx_qq_bind import QqBindDatabase  # noqa: E402
from nonebot_plugin_maimaidx.libraries.maimaidx_qq_member_registry import (  # noqa: E402
    QqMemberRegistry,
)


GROUP_OPENID = "guess-rating-group-openid"
MEMBER_OPENID = "guess-rating-member-openid"
LEGACY_QQ = 123456789


class QQBot:
    self_id = "bot-openid"

    async def call_api(self, *_args, **_kwargs):
        raise AssertionError("official QQ must not call get_group_member_list")


class OneBot:
    self_id = "10000"

    def __init__(self):
        self.calls = []

    async def call_api(self, api, **data):
        self.calls.append((api, data))
        return [{"user_id": LEGACY_QQ, "nickname": "OneBotMember"}]


async def _b50(*, qqid: int):
    chart = ChartInfo(
        achievements=100.0,
        fc="",
        fs="",
        level="14",
        levelIndex=3,
        level_label="Master",
        title="Test",
        type="DX",
        ds=14.0,
        dxScore=0,
        rating=300,
        rate="sss",
        song_id=1,
    )
    return UserInfo(
        additional_rating=0,
        nickname=f"QQ{qqid}",
        rating=15000,
        username=f"QQ{qqid}",
        charts=Data(sd=[chart], dx=[]),
    )


async def main() -> None:
    original_list_group = QqMemberRegistry.list_group
    original_legacy = QqBindDatabase.get_legacy_qq
    original_forum = QqBindDatabase.get_forum_binding
    original_fetch = rating.get_user_b50_or_fallback
    try:
        QqMemberRegistry.list_group = lambda _self, gid, limit=200: (
            [{"member_id": MEMBER_OPENID}] if gid == GROUP_OPENID else []
        )
        QqBindDatabase.get_legacy_qq = (
            lambda _self, pid: LEGACY_QQ if str(pid) == MEMBER_OPENID else None
        )
        QqBindDatabase.get_forum_binding = lambda _self, pid: (
            {"username": "ForumName", "legacy_qq": LEGACY_QQ}
            if str(pid) == MEMBER_OPENID else None
        )
        rating.get_user_b50_or_fallback = _b50

        candidate = await rating.pick_random_candidate(
            QQBot(), GROUP_OPENID, min_charts=1, weighted=False,
        )
        assert candidate is not None
        assert candidate[:2] == (LEGACY_QQ, "ForumName")

        onebot = OneBot()
        candidate = await rating.pick_random_candidate(
            onebot, 10001, min_charts=1, weighted=False,
        )
        assert candidate is not None
        assert candidate[:2] == (LEGACY_QQ, "OneBotMember")
        assert onebot.calls == [("get_group_member_list", {"group_id": 10001})]
    finally:
        QqMemberRegistry.list_group = original_list_group
        QqBindDatabase.get_legacy_qq = original_legacy
        QqBindDatabase.get_forum_binding = original_forum
        rating.get_user_b50_or_fallback = original_fetch


asyncio.run(main())
print("qq guess rating candidate tests: ok")
