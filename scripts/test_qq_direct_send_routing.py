#!/usr/bin/env python3
"""Official QQ direct sends must convert media and select the mapped Bot."""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import sys
import types
from pathlib import Path

from nonebot.adapters.onebot.v11 import MessageSegment as OneBotSegment
from nonebot.adapters.qq.bot import Bot as QQBot


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "maimaidx_platform_direct_send_test"
LEGACY_GROUP = 993795066
GROUP_OPENID = "mapped-group-openid"


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
    # This reproduces a dual-adapter deployment whose historical default is
    # still OneBot.  A QQ Bot call must override this global preference.
    config.maiconfig = types.SimpleNamespace(maimaidx_platform="onebot")
    sys.modules[config.__name__] = config

    qq_bind = types.ModuleType(f"{PACKAGE}.libraries.maimaidx_qq_bind")
    qq_bind.qq_bind_db = types.SimpleNamespace(
        get_platform_group_id=lambda gid: (
            GROUP_OPENID if int(gid) == LEGACY_GROUP else None
        ),
        get_group_legacy_id=lambda gid: (
            LEGACY_GROUP if str(gid) == GROUP_OPENID else None
        ),
        get_platform_id=lambda uid: "mapped-user-openid" if int(uid) == 123456 else None,
        get_legacy_qq=lambda _uid: None,
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


class FakeOneBot:
    async def send_group_msg(self, **data):
        self.sent = data


FakeOneBot.__module__ = "nonebot.adapters.onebot.v11.bot"


class FakeQQBot(QQBot):
    def __init__(self):
        self.sent: list[dict] = []

    async def send_to_group(self, **data):
        self.sent.append(data)
        return {"id": f"sent-{len(self.sent)}"}

    async def send_to_c2c(self, **data):
        self.sent.append(data)
        return {"id": f"sent-{len(self.sent)}"}


FakeQQBot.__module__ = "nonebot.adapters.qq.bot"


async def main() -> None:
    platform = _load_platform_module()
    platform.install_qq_event_compat()

    onebot = FakeOneBot()
    qq = FakeQQBot()
    bots = {"onebot": onebot, "qq": qq}
    assert platform.resolve_group_bot(LEGACY_GROUP, bots) is qq
    assert platform.resolve_group_bot(GROUP_OPENID, bots) is qq
    assert platform.resolve_group_bot(12345, bots) is onebot

    image_bytes = b"direct-send-image"
    image = OneBotSegment.image(
        "base64://" + base64.b64encode(image_bytes).decode("ascii")
    )

    # Exercise the patched QQBot.call_api path, not only the converter helper.
    await qq.call_api("send_group_msg", group_id=LEGACY_GROUP, message=image)
    sent = qq.sent[-1]
    assert sent["group_openid"] == GROUP_OPENID
    parts = list(sent["message"])
    assert any(part.type == "file_image" for part in parts)
    assert next(part for part in parts if part.type == "file_image").data["content"] == image_bytes
    assert all("base64://" not in str(part.data.get("text") or "") for part in parts)

    # The legacy convenience shim must force the same conversion.
    await qq.send_group_msg(group_id=LEGACY_GROUP, message=image)
    assert any(part.type == "file_image" for part in qq.sent[-1]["message"])

    await qq.send_private_msg(user_id=123456, message=image)
    assert qq.sent[-1]["openid"] == "mapped-user-openid"
    assert any(part.type == "file_image" for part in qq.sent[-1]["message"])


asyncio.run(main())
print("qq direct send routing tests: ok")
