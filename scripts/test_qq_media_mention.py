#!/usr/bin/env python3
"""Regression test for preserving media while adding an official QQ reply prefix."""

from __future__ import annotations

import base64
import asyncio
import importlib.util
import sys
import tempfile
import types
from io import BytesIO
from pathlib import Path

from PIL import Image
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
    image_buffer = BytesIO()
    Image.new("RGB", (8, 6), "white").save(image_buffer, format="PNG")
    image_bytes = image_buffer.getvalue()
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

    # A few older commands pass a plain local path instead of base64.  It must
    # follow the same native upload path rather than being dropped as text.
    with tempfile.TemporaryDirectory() as image_directory:
        local_path = Path(image_directory) / "jacket.png"
        local_path.write_bytes(image_bytes)
        local_reply = platform.ensure_sender_mention(
            OneBotSegment.image(str(local_path)), event
        )
        local_part = next(
            part for part in local_reply if part.type == "file_image"
        )
        assert local_part.data["content"] == image_bytes
        _assert_no_serialized_media(local_reply)

    # Ordinary replies use the same native Markdown @ path as OAuth and links.
    checkin_reply = platform.ensure_sender_mention(
        "✅ AWMC 签到成功！\n💰 获得：23 BREAK", event
    )
    assert [part.type for part in checkin_reply] == ["markdown"]
    checkin_content = checkin_reply[0].data["markdown"].content
    assert checkin_content.startswith(
        '<qqbot-at-user id="user-openid" />\n'
    )
    extracted_checkin = QQBot._extract_send_message(
        checkin_reply, escape_text=False
    )
    assert extracted_checkin["content"] is None
    assert extracted_checkin["markdown"].content == checkin_content
    assert checkin_content.startswith('<qqbot-at-user id="user-openid" />\n')
    assert "✅ AWMC 签到成功！" in checkin_content
    # Matcher.send and Bot.send call the compatibility helper more than once;
    # an existing Markdown @ must not be duplicated.
    repeated_checkin = platform.ensure_sender_mention(checkin_reply, event)
    assert repeated_checkin[0].data["markdown"].content == checkin_content

    explicit_reply = platform.build_mention_message(
        "user-openid", " 正在处理", event=event
    )
    assert [part.type for part in explicit_reply] == ["markdown"]
    assert explicit_reply[0].data["markdown"].content == (
        '<qqbot-at-user id="user-openid" /> 正在处理'
    )

    # Without temporary hosting, use a plain-text @ prefix before the media.
    # Text-chain tags are parsed from ``content``, not a media/Markdown field.
    mention_message, media_messages = platform._split_qq_media_message(image_reply)
    assert [part.type for part in mention_message] == ["markdown"]
    fallback_content = mention_message[0].data["markdown"].content
    assert '<qqbot-at-user id="user-openid" />' in fallback_content
    assert "查询结果" in fallback_content
    extracted_fallback = QQBot._extract_send_message(
        mention_message, escape_text=False
    )
    assert extracted_fallback["content"] is None
    assert extracted_fallback["markdown"].content == fallback_content
    assert len(media_messages) == 1
    media_parts = list(media_messages[0])
    assert not any(part.type == "mention_user" for part in media_parts)
    assert any(part.type == "file_image" for part in media_parts)

    remote_media = QQMessage(
        [image_parts[0], QQSegment.image("https://example.com/result.png")]
    )
    remote_mention, remote_messages = platform._split_qq_media_message(remote_media)
    assert [part.type for part in remote_mention] == ["markdown"]
    assert [part.type for part in remote_messages[0]] == ["text", "image"]

    # Score images must stay on QQ's native upload path. External Markdown
    # image URLs frequently render as "加载失败" in QQ clients, while the
    # media request remains reliable and can still carry its caption.
    with tempfile.TemporaryDirectory() as directory:
        platform.maiconfig.maimaidx_qq_media_public_url = (
            "https://assets.example.com/qqbot-media"
        )
        platform.maiconfig.maimaidx_qq_media_dir = directory
        platform.maiconfig.maimaidx_qq_media_ttl_seconds = 3600
        platform.maiconfig.maimaidx_qq_media_max_bytes = 1024 * 1024
        marked_reply = platform.adapt_reply_payload(
            image_reply,
            footer="\nfooter | text 😀",
            event=event,
            publish_qq_image=True,
        )
        mention_message, followups = platform._split_qq_media_message(marked_reply)
        assert [part.type for part in mention_message] == ["markdown"]
        fallback_content = mention_message[0].data["markdown"].content
        assert fallback_content.startswith('<qqbot-at-user id="user-openid" />')
        assert len(followups) == 1
        media_message = followups[0]
        assert [part.type for part in media_message] == ["text", "file_image"]
        assert media_message[0].data["text"] == "footer | text 😀"
        assert media_message[1].data["content"] == image_bytes
        published = list(Path(directory).glob("*.png"))
        assert published == []
        extracted_media = QQBot._extract_send_message(
            media_message, escape_text=False
        )
        assert extracted_media["content"] == "footer | text 😀"
        cached_message, cached_followups = platform._split_qq_media_message(
            marked_reply
        )
        assert cached_message[0].data["markdown"].content == fallback_content
        assert [part.type for part in cached_followups[0]] == [
            "text", "file_image"
        ]
        assert cached_followups[0][1].data["content"] == image_bytes
    platform.maiconfig.maimaidx_qq_media_public_url = ""
    platform.maiconfig.maimaidx_qq_media_dir = ""

    mixed = OneBotMessage([OneBotSegment.text("result"), image])
    mixed_reply = platform.ensure_sender_mention(mixed, event)
    mixed_parts = list(mixed_reply)
    assert any(
        part.type == "text" and part.data.get("text") == "result"
        for part in mixed_parts
    )
    assert any(part.type == "file_image" for part in mixed_parts)
    _assert_no_serialized_media(mixed_reply)

    # An unmarked image is never published and keeps its normal media payload.
    single_caption = QQMessage(
        [QQSegment.text("说明 😀"), QQSegment.file_image(b"single-image")]
    )
    assert platform._split_qq_media_message(single_caption) is None

    # Multiple local attachments must use separate requests because the
    # official API has only one ``media`` field; the first remains beside the
    # caption.
    two_images = QQMessage(
        [
            QQSegment.text("贡献图"),
            QQSegment.file_image(b"first-image"),
            QQSegment.file_image(b"second-image"),
        ]
    )
    split = platform._split_qq_media_message(two_images)
    assert split is not None
    first_payload, followups = split
    assert [part.type for part in first_payload].count("file_image") == 1
    assert next(part for part in first_payload if part.type == "file_image").data[
        "content"
    ] == b"first-image"
    assert len(followups) == 1
    assert next(part for part in followups[0] if part.type == "file_image").data[
        "content"
    ] == b"second-image"

    markdown = QQMessage([QQSegment.markdown("**😀 可复制文字**")])
    markdown_reply = platform.ensure_sender_mention(markdown, event)
    assert [part.type for part in markdown_reply] == ["markdown"]
    assert platform._split_qq_media_message(markdown_reply) is None
    markdown_content = markdown_reply[0].data["markdown"].content
    assert markdown_content.startswith('<qqbot-at-user id="user-openid" />')
    assert "😀" in markdown_content

    forward = platform.qq_forward_markdown(
        [
            {
                "type": "node",
                "data": {
                    "user_id": "1",
                    "nickname": "bot",
                    "content": "绿谱 https://v.wmc.pub/?song=1 🎵",
                },
            },
        ],
        title="查歌补充信息",
    )
    assert "[绿谱](https://v.wmc.pub/?song=1)" in forward
    assert "🎵" in forward

    link_message = platform.build_markdown_link_message(
        "谱面预览",
        [("绿谱", "https://v.wmc.pub/?song=1")],
        event=event,
    )
    assert [part.type for part in link_message] == ["markdown", "keyboard"]
    assert "[绿谱](https://v.wmc.pub/?song=1)" in (
        link_message[0].data["markdown"].content
    )
    button = link_message[1].data["keyboard"].content.rows[0].buttons[0]
    assert button.render_data.label == "绿谱"
    assert button.action.permission.type == 2

    class _FakeBot:
        def __init__(self):
            self.sent = []

        async def send(self, _event, payload):
            self.sent.append(payload)

    fake_bot = _FakeBot()
    asyncio.run(
        platform.deliver_forward_messages(
            fake_bot,
            event,
            [
                {
                    "type": "node",
                    "data": {
                        "user_id": "1",
                        "nickname": "bot",
                        "content": "表情 😀",
                    },
                }
            ],
            title="补充",
        )
    )
    assert len(fake_bot.sent) == 1
    assert fake_bot.sent[0][0].type == "markdown"
    assert "😀" in fake_bot.sent[0][0].data["markdown"].content

    final_reply = platform.adapt_reply_payload(
        image_reply, footer="\nfooter", event=event
    )
    assert list(final_reply)[-1].data.get("text") == "\nfooter"
    _assert_no_serialized_media(final_reply)

    target_message = platform.build_mention_message(
        "target-openid", "\nhello", event=event
    )
    target_parts = list(target_message)
    assert target_parts[0].type == "markdown"
    assert target_parts[0].data["markdown"].content == (
        '<qqbot-at-user id="target-openid" />\nhello'
    )

    legacy_target = platform.build_mention_message(123456, event=event)
    assert list(legacy_target)[0].type == "markdown"
    assert list(legacy_target)[0].data["markdown"].content == (
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

    # Exercise the installed adapter wrapper, not only the splitter helper.
    # A structured score reply uses a standalone Markdown @ followed by the
    # native media request; the adapter wrapper must preserve both receipts.
    calls = []

    async def fake_send_to_group(_self, **kwargs):
        calls.append(kwargs)
        return {"id": f"receipt-{len(calls)}"}

    QQBot.send_to_group = fake_send_to_group
    platform.install_qq_event_compat()
    remote_score = QQMessage(
        [
            platform._qq_mention_segment("user-openid"),
            QQSegment.image("https://example.com/result.png"),
        ]
    )
    remote_score = QQMessage(platform._mark_qq_public_image(list(remote_score)))
    receipt = asyncio.run(
        QQBot.send_to_group(
            object(),
            group_openid="group-openid",
            message=remote_score,
            msg_id="message-id",
            msg_seq=1,
        )
    )
    assert receipt == {"id": "receipt-2"}, (receipt, calls)
    assert len(calls) == 2
    assert [part.type for part in calls[0]["message"]] == ["markdown"]
    assert [part.type for part in calls[1]["message"]] == ["text", "image"]

    # A fallback media request consumes exactly one additional passive-reply
    # sequence after the standalone Markdown mention.
    calls.clear()
    counter_token = platform._QQ_REPLY_FOLLOWUPS_SENT.set(0)
    try:
        async def send_fallback():
            result = await QQBot.send_to_group(
                object(),
                group_openid="group-openid",
                message=image_reply,
                msg_id="message-id",
                msg_seq=1,
            )
            return result, platform._QQ_REPLY_FOLLOWUPS_SENT.get()

        fallback_receipt, sent_count = asyncio.run(send_fallback())
        assert fallback_receipt == {"id": "receipt-2"}
        assert len(calls) == 2
        assert sent_count == 1
    finally:
        platform._QQ_REPLY_FOLLOWUPS_SENT.reset(counter_token)

    print("qq media mention tests: ok")


if __name__ == "__main__":
    main()
