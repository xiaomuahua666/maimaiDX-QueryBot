#!/usr/bin/env python3
"""call_api 发送重试：QQ / OneBot 断连(NetworkError)自动重发，ActionFailed 不重试。

对应 maimaidx_platform 中 _install_call_api_retry 给 nonebot Bot.call_api
加的指数退避重试——这是「突然断联导致消息没发出去」的修复核心。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path = [item for item in sys.path if item and Path(item).resolve() != ROOT]
sys.path.insert(0, str(ROOT.parent))

os.environ.setdefault("MAIMAIDXPATH", str(ROOT / "static"))
os.environ.setdefault("MAIMAIDX_STORAGE_FAIL_FAST", "false")
os.environ.setdefault("MAIMAIDX_MESSAGE_STATS_ENABLED", "false")

import nonebot

# 用 none 驱动避免依赖 fastapi/aiohttp，仅为单元测试初始化 nonebot
nonebot.init(driver="nonebot.drivers.none")

import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 预置「假包」顶模块，使 Python 跳过真实 __init__.py（它会顺带 import command
# 链，依赖 httpx_ws/playwright 等重型依赖）。我们只测 maimaidx_platform 本身，
# 它经 config / maimaidx_qq_bind 这些轻量模块即可独立导入。
_pkg = types.ModuleType("nonebot_plugin_maimaidx")
_pkg.__path__ = [str(ROOT)]
_pkg.__package__ = "nonebot_plugin_maimaidx"
sys.modules["nonebot_plugin_maimaidx"] = _pkg

from nonebot.adapters import Bot as RealBot
from nonebot.adapters.onebot.v11.exception import ActionFailed, NetworkError
from nonebot_plugin_maimaidx.libraries import maimaidx_platform as platform


class _FakeAdapter:
    """最小 adapter：把底层 _call_api 交给全局 state 驱动，便于注入故障。"""

    async def _call_api(self, bot, api, **data):
        state["calls"] += 1
        if state["calls"] <= state["fail_first"]:
            raise state["exc"]()
        return {"api": api, "calls": state["calls"]}


class _TestBot(RealBot):
    """最小 Bot：仅需满足抽象方法 send，adapter 用上面的假对象。"""

    async def send(self, *args, **kwargs):
        raise NotImplementedError


state = {"calls": 0, "fail_first": 0, "exc": None}


def _reset(fail_first, exc):
    global state
    state = {"calls": 0, "fail_first": fail_first, "exc": exc}


async def main():
    # 加速：固定重试 2 次、退避 0s
    platform._send_retry_count = lambda: 2
    platform._send_retry_delay = lambda attempt: 0.0

    # 1) 包装确实已安装到 nonebot 基类 call_api 上
    assert getattr(RealBot.call_api, "_maimaidx_retry", False) is True, \
        "Bot.call_api 未安装重试包装"

    # 2) 断连(NetworkError)前 2 次失败、第 3 次成功 -> 重试生效、消息送达
    _reset(fail_first=2, exc=NetworkError)
    bot = _TestBot(adapter=_FakeAdapter(), self_id="1")
    result = await bot.call_api("send_msg", message="hi")
    assert result["calls"] == 3 and state["calls"] == 3, \
        f"NetworkError 应重试至成功，calls={state['calls']}"

    # 3) ActionFailed（服务端已拒绝）不重试，立即抛出，仅 1 次调用
    _reset(fail_first=99, exc=ActionFailed)
    bot = _TestBot(adapter=_FakeAdapter(), self_id="1")
    raised = False
    try:
        await bot.call_api("send_msg", message="hi")
    except ActionFailed:
        raised = True
    assert raised is True, "ActionFailed 应当抛出"
    assert state["calls"] == 1, f"ActionFailed 不应重试，calls={state['calls']}"

    # 4) 持续 NetworkError -> 重试 2 次后放弃（共 3 次调用）并抛出
    _reset(fail_first=99, exc=NetworkError)
    bot = _TestBot(adapter=_FakeAdapter(), self_id="1")
    raised = False
    try:
        await bot.call_api("send_msg", message="hi")
    except NetworkError:
        raised = True
    assert raised is True, "持续 NetworkError 最终应抛出"
    assert state["calls"] == 3, f"重试 2 次后应放弃，calls={state['calls']}"

    print("send retry checks: OK (NetworkError 重试送达 / ActionFailed 不重试 / 超限放弃)")


if __name__ == "__main__":
    asyncio.run(main())
