#!/usr/bin/env python3
"""End-to-end official QQ dispatch smoke for roast and AWMC commands."""

from __future__ import annotations

import asyncio
import base64
import os
import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
# The repository contains an unrelated legacy namespace directory with the
# package name.  Let the editable-install finder load the real package root.
sys.path = [
    item
    for item in sys.path
    if item and Path(item).resolve() != ROOT
]
sys.path.insert(0, str(ROOT.parent))

os.environ.setdefault("COMMAND_START", '["", "/"]')
os.environ.setdefault("MAIMAIDXPATH", str(ROOT / "static"))
os.environ.setdefault("MAIMAIDX_STORAGE_FAIL_FAST", "false")
os.environ.setdefault("B50_LLM_KEY", "dispatch-test-key")
os.environ.setdefault("B50_ASSETS_PATH", str(ROOT / "static"))
os.environ.setdefault("MAIMAIDX_MESSAGE_STATS_ENABLED", "false")
os.environ.setdefault("MAIMAIDX_BUSY_SURCHARGE_ENABLED", "false")

import nonebot

nonebot.init()

import nonebot_plugin_maimaidx  # noqa: F401, E402
from nonebot_plugin_maimaidx.command import mai_break, mai_guess  # noqa: E402
from nonebot.adapters import Bot as BaseBot  # noqa: E402
from nonebot.adapters.qq.event import GroupAtMessageCreateEvent  # noqa: E402
from nonebot.adapters.qq.models import GroupMemberAuthor, GroupMentionUser  # noqa: E402
from nonebot.message import handle_event  # noqa: E402
from nonebot_plugin_maimaidx.libraries.maimaidx_qq_bind import (  # noqa: E402
    QqBindDatabase,
)


class DummyQQAdapter:
    def __init__(self):
        self.config = nonebot.get_driver().config

    @classmethod
    def get_name(cls) -> str:
        return "QQ"

    async def _call_api(self, bot, api: str, **data):
        raise RuntimeError(f"unsupported test API: {api}")


class FakeQQBot(BaseBot):
    def __init__(self):
        super().__init__(DummyQQAdapter(), "bot-openid")
        self.sent: list[tuple[object, dict]] = []

    async def send(self, event, message, **kwargs):
        self.sent.append((message, kwargs))
        return {"id": f"sent-{len(self.sent)}"}


# Keep dependency-injection behavior identical to the real QQ adapter class.
FakeQQBot.__module__ = "nonebot.adapters.qq.bot"


def make_event(command_text: str, index: int) -> GroupAtMessageCreateEvent:
    author = GroupMemberAuthor(
        id=f"author-{index}",
        bot=False,
        member_openid=f"dispatch-unbound-openid-{index}",
        username="测试用户",
    )
    bot_mention = GroupMentionUser(
        scope="single",
        bot=True,
        id="bot-openid",
        is_you=True,
        member_openid="bot-openid",
        username="AWMC BOT",
    )
    return GroupAtMessageCreateEvent(
        id=f"message-{index}",
        content=f" {command_text}",
        timestamp="2026-08-04T00:00:00+08:00",
        mentions=[bot_mention],
        author=author,
        group_id="legacy-group-token",
        group_openid="dispatch-group-openid",
    )


def message_parts(record: tuple[object, dict]):
    message, kwargs = record
    return list(message), kwargs


async def main() -> None:
    roast_bot = FakeQQBot()
    await handle_event(roast_bot, make_event("锐评一下", 1))
    assert len(roast_bot.sent) >= 2
    parts, kwargs = message_parts(roast_bot.sent[0])
    assert kwargs.get("reply_message") is False
    assert parts[0].type == "text"
    assert parts[0].data.get("text", "").startswith(
        '<qqbot-at-user id="dispatch-unbound-openid-1" />\n'
    )
    assert any(
        segment.type == "text"
        and "正在处理 B50 锐评" in str(segment.data.get("text") or "")
        for segment in parts
    )

    awmc_bot = FakeQQBot()
    await handle_event(awmc_bot, make_event("我的AWMC", 2))
    assert awmc_bot.sent
    parts, kwargs = message_parts(awmc_bot.sent[0])
    assert kwargs.get("reply_message") is False
    assert parts[0].type == "text"
    assert parts[0].data.get("text", "").startswith(
        '<qqbot-at-user id="dispatch-unbound-openid-2" />\n'
    )
    assert any(
        segment.type == "text"
        and "qbind" in str(segment.data.get("text") or "")
        for segment in parts
    )

    original_get_legacy_qq = QqBindDatabase.get_legacy_qq
    original_get_account_profile = mai_break.get_account_profile
    original_format_account_profile = mai_break.format_account_profile
    original_guess_stats = mai_break._try_guess_stats_for_awmc
    QqBindDatabase.get_legacy_qq = lambda self, uid: (
        123456789 if str(uid) == "dispatch-unbound-openid-3" else None
    )
    mai_break.get_account_profile = lambda _qqid: {}
    mai_break.format_account_profile = lambda _profile: "AWMC account overview"

    async def _no_guess_stats(_event):
        return None

    mai_break._try_guess_stats_for_awmc = _no_guess_stats
    try:
        bound_awmc_bot = FakeQQBot()
        await handle_event(bound_awmc_bot, make_event("我的AWMC", 3))
        assert bound_awmc_bot.sent
        parts, kwargs = message_parts(bound_awmc_bot.sent[0])
        assert kwargs.get("reply_message") is False
        assert parts[0].type == "mention_user"
        assert parts[0].data.get("user_id") == "dispatch-unbound-openid-3"
        assert any(segment.type == "file_image" for segment in parts)
        assert all(
            "base64://" not in str(segment.data.get("text") or "")
            for segment in parts
            if segment.type == "text"
        )
    finally:
        QqBindDatabase.get_legacy_qq = original_get_legacy_qq
        mai_break.get_account_profile = original_get_account_profile
        mai_break.format_account_profile = original_format_account_profile
        mai_break._try_guess_stats_for_awmc = original_guess_stats

    # Legacy handlers that call matcher.finish directly must receive the same
    # real mention prefix from the platform boundary.
    legacy_finish_bot = FakeQQBot()
    await handle_event(legacy_finish_bot, make_event("AWMC帮助", 4))
    assert legacy_finish_bot.sent
    parts, kwargs = message_parts(legacy_finish_bot.sent[0])
    assert kwargs.get("reply_message") is False
    assert parts[0].type == "text"
    assert parts[0].data.get("text", "").startswith(
        '<qqbot-at-user id="dispatch-unbound-openid-4" />\n'
    )

    # Sign-in is a plain-text handler and must use the same single text-chain
    # @ payload as the query-result fallback.
    original_checkin = mai_break.break_db.checkin
    original_checkin_formatter = mai_break.format_checkin_result
    original_checkin_qqid = mai_break._account_qqid
    original_checkin_storage = mai_break._storage_status_for_event
    mai_break.break_db.checkin = lambda *args, **kwargs: SimpleNamespace()
    mai_break.format_checkin_result = lambda _result: (
        "✅ AWMC 签到成功！\n💰 获得：23 BREAK"
    )
    mai_break._account_qqid = lambda _event: 123456789
    mai_break._storage_status_for_event = lambda _event, _qqid: (False, False)
    try:
        checkin_bot = FakeQQBot()
        await handle_event(checkin_bot, make_event("签到", 6))
        assert checkin_bot.sent
        parts, kwargs = message_parts(checkin_bot.sent[0])
        assert kwargs.get("reply_message") is False
        assert len(parts) == 1
        assert parts[0].type == "text"
        checkin_text = parts[0].data.get("text", "")
        assert checkin_text.startswith(
            '<qqbot-at-user id="dispatch-unbound-openid-6" />\n'
        )
        assert "✅ AWMC 签到成功！" in checkin_text
    finally:
        mai_break.break_db.checkin = original_checkin
        mai_break.format_checkin_result = original_checkin_formatter
        mai_break._account_qqid = original_checkin_qqid
        mai_break._storage_status_for_event = original_checkin_storage

    original_guess_enabled = mai_guess.guess.is_enabled
    original_build_guess_stats = mai_guess.guess_score.build_user_guess_stats
    original_guess_stats_image = mai_guess.personal_guess_stats_image_b64
    mai_guess.guess.is_enabled = lambda _gid: True
    mai_guess.guess_score.build_user_guess_stats = lambda gid, uid: {
        "uid": str(uid),
        "name": "测试用户",
        "total_score": 42,
        "modes": {
            mode: {"count": 0, "points": 0, "last_at": None}
            for mode in mai_guess.guess_score.GUESS_MODES
        },
    }
    mai_guess.personal_guess_stats_image_b64 = lambda _stats: (
        "base64://" + base64.b64encode(b"guess-stats-image").decode("ascii")
    )
    try:
        guess_stats_bot = FakeQQBot()
        await handle_event(guess_stats_bot, make_event("我的猜歌", 7))
        assert guess_stats_bot.sent
        parts, kwargs = message_parts(guess_stats_bot.sent[0])
        assert kwargs.get("reply_message") is False
        assert parts[0].type == "mention_user"
        assert parts[0].data.get("user_id") == "dispatch-unbound-openid-7"
        assert any(segment.type == "file_image" for segment in parts)
    finally:
        mai_guess.guess.is_enabled = original_guess_enabled
        mai_guess.guess_score.build_user_guess_stats = original_build_guess_stats
        mai_guess.personal_guess_stats_image_b64 = original_guess_stats_image


asyncio.run(main())
print("qq command dispatch tests: ok")
