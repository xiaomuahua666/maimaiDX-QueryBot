#!/usr/bin/env python3
"""Regression test for preserving media while adding an official QQ reply prefix."""

from __future__ import annotations

import base64
import importlib.util
import sys
import types
from pathlib import Path

from nonebot.adapters.onebot.v11 import Message as OneBotMessage
from nonebot.adapters.onebot.v11 import MessageSegment as OneBotSegment
from nonebot.adapters.qq.bot import Bot as QQBot
from nonebot.adapters.qq.message import Message as QQMessage
from nonebot.adapters.qq.message import MessageSegment as QQSegment


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "maimaidx_platform_media_test"


class _Log:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


def _load_platform_module():
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT)]
    sys.modules[PACKAGE] = package

    libraries = types.ModuleType(f"{PACKAGE}.libraries")
    libraries.__path__ = [str(ROOT / "libraries")]
    sys.modules[libraries.__name__] = libraries

    config = types.ModuleType(f"{PACKAGE}.config")
    config.log = _Log()
    config.maiconfig = types.SimpleNamespace(maimaidx_platform="qq")
    sys.modules[config.__name__] = config

    qq_bind = types.ModuleType(f"{PACKAGE}.libraries.maimaidx_qq_bind")
    qq_bind.qq_bind_db = types.SimpleNamespace(
        get_platform_id=lambda qq: "bound-target-openid" if int(qq) == 123456 else None,
        get_legacy_qq=lambda _uid: None,
        get_group_legacy_id=lambda _gid: None,
    )
    sys.modules[qq_bind.__name__] = qq_bind

    module_name = f"{PACKAGE}.libraries.maimaidx_platform"
    spec = importlib.util.spec_from_file_location(
        module_name, ROOT / "libraries" / "maimaidx_platform.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


QQEvent = type("QQEvent", (), {"__module__": "nonebot.adapters.qq.event"})


def _event():
    event = QQEvent()
    event.group_openid = "group-openid"
    event.author = types.SimpleNamespace(username="Tester")
    event.get_user_id = lambda: "user-openid"
    return event


def _assert_no_serialized_media(message) -> None:
    for segment in message:
        if segment.type != "text":
            continue
        text = str(segment.data.get("text") or "")
        assert "base64://" not in text
        assert "[CQ:image" not in text


def main() -> None:
    platform = _load_platform_module()
    event = _event()
    image_bytes = b"test-image-bytes"
    image = OneBotSegment.image(
        "base64://" + base64.b64encode(image_bytes).decode("ascii")
    )

    image_reply = platform.ensure_sender_mention(image, event)
    image_parts = list(image_reply)
    assert image_parts[0].type == "mention_user"
    assert image_parts[0].data["user_id"] == "user-openid"
    assert image_parts[0].data["username"] == "Tester"
    assert str(image_parts[0]) == '<qqbot-at-user id="user-openid" />'
    extracted = QQBot._extract_send_message(image_reply, escape_text=False)
    assert extracted["content"].startswith(
        '<qqbot-at-user id="user-openid" />\n'
    )
    assert "<@user-openid>" not in extracted["content"]
    assert any(part.type == "file_image" for part in image_parts)
    image_part = next(part for part in image_parts if part.type == "file_image")
    assert image_part.data["content"] == image_bytes
    _assert_no_serialized_media(image_reply)

    mixed = OneBotMessage([OneBotSegment.text("result"), image])
    mixed_reply = platform.ensure_sender_mention(mixed, event)
    mixed_parts = list(mixed_reply)
    assert any(
        part.type == "text" and part.data.get("text") == "result"
        for part in mixed_parts
    )
    assert any(part.type == "file_image" for part in mixed_parts)
    _assert_no_serialized_media(mixed_reply)

    final_reply = platform.adapt_reply_payload(
        image_reply, footer="\nfooter", event=event
    )
    assert list(final_reply)[-1].data.get("text") == "\nfooter"
    _assert_no_serialized_media(final_reply)

    target_message = platform.build_mention_message(
        "target-openid", "\nhello", event=event
    )
    target_parts = list(target_message)
    assert target_parts[0].type == "mention_user"
    assert target_parts[0].data["user_id"] == "target-openid"
    assert str(target_parts[0]) == '<qqbot-at-user id="target-openid" />'

    legacy_target = platform.build_mention_message(123456, event=event)
    assert list(legacy_target)[0].data["user_id"] == "bound-target-openid"
    assert str(list(legacy_target)[0]) == (
        '<qqbot-at-user id="bound-target-openid" />'
    )

    incoming = _event()
    incoming.message = QQMessage(
        [QQSegment.mention_user("target-openid", username="Target")]
    )
    assert platform.parse_at_target_id(incoming) == "target-openid"

    # The event remains ``to_me`` after the adapter has removed @bot.  An
    # unmarked first segment is now the real target and must not be skipped.
    incoming.is_tome = lambda: True
    incoming.message = QQMessage(
        [QQSegment.mention_user("unmarked-target", username="Target")]
    )
    assert platform.parse_at_target_id(incoming) == "unmarked-target"

    everyone = platform.adapt_guess_outbound(
        OneBotSegment.at("all"), event=event
    )
    assert list(everyone)[0].type == "mention_everyone"
    assert platform.foreign_recall_notice(event) == (
        "⚠️ Bot 无法撤回该消息，请立即手动撤回。"
    )

    print("qq media mention tests: ok")


if __name__ == "__main__":
    main()
