#!/usr/bin/env python3
"""Level-15 anomalous users stay out of group rankings and game pools."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
os.environ.setdefault("MAIMAIDXPATH", str(ROOT / "static"))
os.environ.setdefault("MAIMAIDX_STORAGE_FAIL_FAST", "false")

import nonebot

nonebot.init()

from nonebot_plugin_maimaidx.libraries import maimaidx_friend_battle as battle  # noqa: E402
from nonebot_plugin_maimaidx.libraries import maimaidx_group_rating as group  # noqa: E402


ILLEGAL_QQ = 10001
LEGAL_QQ = 10002


def _userinfo(qqid: int):
    illegal = qqid == ILLEGAL_QQ
    chart = SimpleNamespace(
        song_id=123,
        level_index=3,
        level="15" if illegal else "14+",
        ds=15.0 if illegal else 14.9,
        achievements=101.0 if illegal else 100.5,
        fc="app" if illegal else "ap",
        dxScore=3000,
        dxScoreMax=3000,
    )
    return SimpleNamespace(
        rating=16000 if illegal else 15000,
        charts=SimpleNamespace(sd=[chart], dx=[]),
    )


async def _members(_bot, _group_id):
    return [
        {"user_id": ILLEGAL_QQ, "nickname": "Illegal"},
        {"user_id": LEGAL_QQ, "nickname": "Legal"},
    ]


async def main() -> None:
    original_group_members = group._get_group_member_list
    original_cached_rating = group.get_cached_rating_for_friend_battle
    original_get_b50 = group.get_user_b50
    original_battle_members = battle._get_group_member_list
    original_battle_users = battle.list_battle_users
    original_battle_eligibility = battle.is_cached_user_group_eligible

    async def get_b50(*, qqid: int, **_kwargs):
        return _userinfo(qqid)

    try:
        group._get_group_member_list = _members
        group.get_cached_rating_for_friend_battle = lambda _qqid: None
        group.get_user_b50 = get_b50
        ratings = await group.get_group_member_ratings(SimpleNamespace(), 1)
        assert ratings == [(LEGAL_QQ, "Legal", 15000)]

        battle._get_group_member_list = _members
        battle.list_battle_users = lambda: {
            ILLEGAL_QQ: {"tier": "S5", "cp": 50},
            LEGAL_QQ: {"tier": "A5", "cp": 40},
        }
        battle.is_cached_user_group_eligible = lambda qqid: qqid != ILLEGAL_QQ
        text, nodes = await battle.group_friend_battle_ranking(
            SimpleNamespace(), 1, 999, "Milk", LEGAL_QQ,
        )
        assert "共 1 人参战" in text
        assert len(nodes) == 1
        assert "Legal" in nodes[0]["data"]["content"]
        assert "Illegal" not in str(nodes)
    finally:
        group._get_group_member_list = original_group_members
        group.get_cached_rating_for_friend_battle = original_cached_rating
        group.get_user_b50 = original_get_b50
        battle._get_group_member_list = original_battle_members
        battle.list_battle_users = original_battle_users
        battle.is_cached_user_group_eligible = original_battle_eligibility


asyncio.run(main())
print("group score eligibility tests: ok")
