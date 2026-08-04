"""官方 QQ 群回复加 @ 前缀时必须保留 OneBot 图片段。"""

import asyncio
import base64
import os
import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.parent))
os.environ.setdefault("MAIMAIDXPATH", str(ROOT / "static"))
os.environ.setdefault("MAIMAIDX_STORAGE_FAIL_FAST", "false")

import nonebot

nonebot.init()

from nonebot.adapters.onebot.v11 import MessageSegment

from nonebot_plugin_maimaidx.libraries.maimaidx_platform import plugin_finish, plugin_send


class FakeQQGroupEvent:
    group_openid = "group-openid"
    author = SimpleNamespace(username="测试用户")

    def get_user_id(self) -> str:
        return "user-openid"


# ``use_qq_mode`` deliberately identifies the platform from the event module.
FakeQQGroupEvent.__module__ = "nonebot.adapters.qq.event"


class FakeMatcher:
    payload = None
    reply_message = None

    async def finish(self, payload=None, *, reply_message=True) -> None:
        self.payload = payload
        self.reply_message = reply_message

    async def send(self, payload=None, *, reply_message=True):
        self.payload = payload
        self.reply_message = reply_message
        return {"id": "sent"}


image_bytes = b"b50-image-bytes"
onebot_image = MessageSegment.image(
    "base64://" + base64.b64encode(image_bytes).decode("ascii")
)
matcher = FakeMatcher()
asyncio.run(
    plugin_finish(
        matcher,
        onebot_image,
        footer="\n数据源与耗时 footer",
        event=FakeQQGroupEvent(),
    )
)

segments = list(matcher.payload)
text = "".join(
    str(segment.data.get("text") or "")
    for segment in segments
    if segment.type == "text"
)
images = [segment for segment in segments if segment.type == "file_image"]
mentions = [segment for segment in segments if segment.type == "mention_user"]

assert segments[0].type == "mention_user"
assert len(mentions) == 1
assert mentions[0].data.get("user_id") == "user-openid"
assert mentions[0].data.get("username") == "测试用户"
assert "数据源与耗时 footer" in text
assert "base64://" not in text
assert len(images) == 1
assert images[0].data.get("content") == image_bytes
assert matcher.reply_message is False

send_matcher = FakeMatcher()
send_result = asyncio.run(
    plugin_send(send_matcher, onebot_image, event=FakeQQGroupEvent())
)
assert send_result == {"id": "sent"}
send_segments = list(send_matcher.payload)
assert send_segments[0].type == "mention_user"
assert any(segment.type == "file_image" for segment in send_segments)
assert not any(
    "base64://" in str(segment.data.get("text") or "")
    for segment in send_segments
    if segment.type == "text"
)

print("qq image reply tests: ok")
