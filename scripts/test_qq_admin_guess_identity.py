#!/usr/bin/env python3
"""Regression coverage for official-QQ admin and migrated guess identities."""

from __future__ import annotations

import asyncio
import os
import sys
import types
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
os.environ.setdefault("B50_LLM_KEY", "identity-test-key")

import nonebot

nonebot.init()

from nonebot_plugin_maimaidx.command import mai_alias, mai_break, mai_guess  # noqa: E402
from nonebot_plugin_maimaidx.libraries import maimaidx_bot_admin  # noqa: E402
from nonebot_plugin_maimaidx.libraries.maimaidx_bot_admin import (  # noqa: E402
    GUESS_GROUP_MANAGER,
    PLUGIN_ADMIN_ONLY,
)
from nonebot_plugin_maimaidx.libraries.maimaidx_qq_bind import (  # noqa: E402
    QqBindDatabase,
)


class _Event:
    def __init__(self, user_id: str):
        self.user_id = user_id

    def get_user_id(self) -> str:
        return self.user_id


_Event.__module__ = "nonebot.adapters.qq.event"


class _OneBotEvent(_Event):
    pass


_OneBotEvent.__module__ = "nonebot.adapters.onebot.v11.event"


async def main() -> None:
    original_admin_ids = maimaidx_bot_admin.get_plugin_admin_ids
    original_get_legacy = QqBindDatabase.get_legacy_qq
    original_forum = QqBindDatabase.get_forum_binding
    maimaidx_bot_admin.get_plugin_admin_ids = lambda: {"123456789"}
    QqBindDatabase.get_legacy_qq = lambda self, pid: (
        123456789 if str(pid) == "official-admin-openid" else None
    )
    QqBindDatabase.get_forum_binding = lambda self, _pid: None
    try:
        assert maimaidx_bot_admin.is_plugin_admin("123456789")
        assert maimaidx_bot_admin.is_plugin_admin("official-admin-openid")
        assert not maimaidx_bot_admin.is_plugin_admin("ordinary-openid")
        assert not maimaidx_bot_admin.is_plugin_admin("987654321")
        assert await PLUGIN_ADMIN_ONLY(None, _Event("official-admin-openid"))
        assert not await PLUGIN_ADMIN_ONLY(None, _Event("ordinary-openid"))
        assert not await GUESS_GROUP_MANAGER(None, _Event("ordinary-openid"))

        # Native OneBot GROUP_ADMIN/GROUP_OWNER checkers assume a group sender
        # and crash on private FriendAuthor events.  Alias push must use the
        # qbind-aware group-manager permission instead.
        alias_checker_names = {
            getattr(dependent.call, "__name__", "")
            for dependent in mai_alias.alias_switch.permission.checkers
        }
        assert "_group_manager_or_plugin_admin" in alias_checker_names
        assert "_group_admin" not in alias_checker_names
        assert "_group_owner" not in alias_checker_names

        # All BREAK maintenance commands must use the qbind-aware permission.
        for name in (
            "awmc_admin_set",
            "awmc_admin_add",
            "awmc_admin_config",
            "awmc_admin_view",
            "ticket_stats_admin",
        ):
            matcher = getattr(mai_break, name)
            checker_names = {
                getattr(dependent.call, "__name__", "")
                for dependent in matcher.permission.checkers
            }
            assert "_plugin_admin_only" in checker_names, (name, checker_names)

        # Group migration alone does not identify the person.  The empty
        # result should explain that qbind is needed and data was not cleared.
        QqBindDatabase.get_group_legacy_id = lambda self, gid: (
            993795066 if str(gid) == "official-group-openid" else None
        )
        event = _Event("official-user-openid")
        hint = mai_guess._legacy_guess_identity_hint(
            event, "official-group-openid", "official-user-openid"
        )
        assert "993795066" in hint
        assert "qbind" in hint
        assert "没有因此被清空" in hint

        # A bound user and an un-migrated OneBot group must retain the normal
        # empty-state response without the migration warning.
        QqBindDatabase.get_legacy_qq = lambda self, pid: (
            123456789 if str(pid) == "official-user-openid" else None
        )
        assert mai_guess._legacy_guess_identity_hint(
            event, "official-group-openid", "official-user-openid"
        ) == ""
        assert mai_guess._legacy_guess_identity_hint(
            _OneBotEvent("987654321"), 993795066, "987654321"
        ) == ""
    finally:
        maimaidx_bot_admin.get_plugin_admin_ids = original_admin_ids
        QqBindDatabase.get_legacy_qq = original_get_legacy
        QqBindDatabase.get_forum_binding = original_forum
        QqBindDatabase.get_group_legacy_id = original_get_legacy_group


original_get_legacy_group = QqBindDatabase.get_group_legacy_id
asyncio.run(main())
print("qq admin and guess identity tests: ok")
