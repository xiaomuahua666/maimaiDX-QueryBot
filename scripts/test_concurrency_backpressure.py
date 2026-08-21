"""重任务与官方 QQ 出站链路必须在满载时快速退避。"""

from __future__ import annotations

import asyncio
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

from nonebot_plugin_maimaidx.command import mai_b50_analysis as analysis
from nonebot_plugin_maimaidx.libraries import maimaidx_platform as platform


class FakeArgs:
    def extract_plain_text(self) -> str:
        return ""


class FakeMatcher:
    pass


class FakeEvent:
    pass


async def test_qq_send_backpressure() -> None:
    old_semaphore = platform._QQ_SEND_SEMAPHORE
    old_media_semaphore = platform._QQ_MEDIA_SEND_SEMAPHORE
    old_config = platform.maiconfig
    platform._QQ_SEND_SEMAPHORE = asyncio.Semaphore(1)
    platform._QQ_MEDIA_SEND_SEMAPHORE = asyncio.Semaphore(1)
    platform.maiconfig = SimpleNamespace(qq_send_queue_timeout_seconds=0.02)
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_send(value: str) -> str:
        started.set()
        await release.wait()
        return value

    first = asyncio.create_task(platform._bounded_qq_send(slow_send, "first"))
    await asyncio.wait_for(started.wait(), timeout=0.2)
    try:
        await platform._bounded_qq_send(slow_send, "second")
    except TimeoutError as exc:
        assert "出站消息队列繁忙" in str(exc)
    else:
        raise AssertionError("满载的 QQ 出站队列没有快速失败")

    release.set()
    assert await asyncio.wait_for(first, timeout=0.2) == "first"
    assert not platform._QQ_SEND_SEMAPHORE.locked(), "发送完成后未释放槽位"
    platform._QQ_SEND_SEMAPHORE = old_semaphore
    platform._QQ_MEDIA_SEND_SEMAPHORE = old_media_semaphore
    platform.maiconfig = old_config


async def test_qq_media_keeps_text_lane_free() -> None:
    old_send = platform._QQ_SEND_SEMAPHORE
    old_media = platform._QQ_MEDIA_SEND_SEMAPHORE
    old_config = platform.maiconfig
    platform._QQ_SEND_SEMAPHORE = asyncio.Semaphore(2)
    platform._QQ_MEDIA_SEND_SEMAPHORE = asyncio.Semaphore(1)
    platform.maiconfig = SimpleNamespace(qq_send_queue_timeout_seconds=0.02)
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_send(*, message):
        started.set()
        await release.wait()
        return message

    async def fast_send(*, message):
        return message

    media = [SimpleNamespace(type="image")]
    first = asyncio.create_task(
        platform._bounded_qq_send(slow_send, message=media)
    )
    await asyncio.wait_for(started.wait(), timeout=0.2)
    text = await platform._bounded_qq_send(fast_send, message="签到成功")
    assert text == "签到成功", "媒体上传不应堵住文本发送槽位"
    try:
        await platform._bounded_qq_send(fast_send, message=media)
    except TimeoutError as exc:
        assert "媒体发送队列繁忙" in str(exc)
    else:
        raise AssertionError("媒体并发上限没有生效")
    release.set()
    await asyncio.wait_for(first, timeout=0.2)
    platform._QQ_SEND_SEMAPHORE = old_send
    platform._QQ_MEDIA_SEND_SEMAPHORE = old_media
    platform.maiconfig = old_config


async def test_analysis_backpressure() -> None:
    old_semaphore = analysis._ANALYSIS_SEMAPHORE
    old_config = analysis.maiconfig
    old_handle_impl = analysis._handle_impl
    old_plugin_finish = analysis.plugin_finish
    old_use_qq_mode = analysis.use_qq_mode
    analysis._ANALYSIS_SEMAPHORE = asyncio.Semaphore(1)
    analysis.maiconfig = SimpleNamespace(
        b50_analysis_queue_timeout_seconds=0.02,
    )
    started = asyncio.Event()
    release = asyncio.Event()
    rejected: list[str] = []

    async def slow_handle(*args, **kwargs) -> None:
        started.set()
        await release.wait()

    async def fake_finish(matcher, message, **kwargs) -> None:
        rejected.append(str(message))

    analysis._handle_impl = slow_handle
    analysis.plugin_finish = fake_finish
    analysis.use_qq_mode = lambda event: True
    args = FakeArgs()
    first = asyncio.create_task(
        analysis._handle(FakeMatcher(), object(), FakeEvent(), args)
    )
    await asyncio.wait_for(started.wait(), timeout=0.2)
    await analysis._handle(FakeMatcher(), object(), FakeEvent(), args)
    assert rejected == [
        "当前锐评任务较多，为避免卡住已拒绝本次请求，请稍后再试。"
    ]

    release.set()
    await asyncio.wait_for(first, timeout=0.2)
    assert not analysis._ANALYSIS_SEMAPHORE.locked(), "锐评完成后未释放槽位"
    analysis._ANALYSIS_SEMAPHORE = old_semaphore
    analysis.maiconfig = old_config
    analysis._handle_impl = old_handle_impl
    analysis.plugin_finish = old_plugin_finish
    analysis.use_qq_mode = old_use_qq_mode


async def test_analysis_rejects_duplicate_user() -> None:
    old_semaphore = analysis._ANALYSIS_SEMAPHORE
    old_handle_impl = analysis._handle_impl
    old_plugin_finish = analysis.plugin_finish
    old_platform_user_id = analysis.platform_user_id
    old_use_qq_mode = analysis.use_qq_mode
    analysis._ANALYSIS_SEMAPHORE = asyncio.Semaphore(2)
    analysis._USER_LOCKS.clear()
    started = asyncio.Event()
    release = asyncio.Event()
    rejected: list[str] = []

    async def slow_handle(*args, **kwargs) -> None:
        started.set()
        await release.wait()

    async def fake_finish(matcher, message, **kwargs) -> None:
        rejected.append(str(message))

    analysis._handle_impl = slow_handle
    analysis.plugin_finish = fake_finish
    analysis.platform_user_id = lambda event: "same-user"
    analysis.use_qq_mode = lambda event: True
    first = asyncio.create_task(
        analysis._handle(FakeMatcher(), object(), FakeEvent(), FakeArgs())
    )
    await asyncio.wait_for(started.wait(), timeout=0.2)
    await asyncio.wait_for(
        analysis._handle(FakeMatcher(), object(), FakeEvent(), FakeArgs()),
        timeout=0.2,
    )
    assert rejected == [
        "你已有锐评正在生成，请等待结果，勿重复发送。"
    ]

    release.set()
    await asyncio.wait_for(first, timeout=0.2)
    analysis._USER_LOCKS.clear()
    analysis._ANALYSIS_SEMAPHORE = old_semaphore
    analysis._handle_impl = old_handle_impl
    analysis.plugin_finish = old_plugin_finish
    analysis.platform_user_id = old_platform_user_id
    analysis.use_qq_mode = old_use_qq_mode


async def test_official_qq_skips_onebot_reaction() -> None:
    old_config = analysis.maiconfig
    old_functions = {
        name: getattr(analysis, name)
        for name in (
            "platform_user_id",
            "billing_user_id",
            "resolve_score_qqid",
            "use_qq_mode",
            "plugin_send",
            "plugin_finish",
            "react_processing",
            "fetch_for_analysis",
            "get_event_group_id",
            "ensure_sender_mention",
            "adapt_reply_payload",
            "send_group_message",
        )
    }
    analysis.maiconfig = SimpleNamespace(
        b50_llm_key="configured",
        b50_assets_path="configured",
        maimaidx_compact_messages=True,
        b50_send_timeout_seconds=1.0,
    )
    reaction_calls = 0
    finished: list[str] = []
    active_messages: list[tuple[str, str]] = []

    async def fake_send(*args, **kwargs):
        return {"id": "sent"}

    async def fake_finish(matcher, message, **kwargs) -> None:
        finished.append(str(message))

    async def fake_reaction(*args, **kwargs) -> None:
        nonlocal reaction_calls
        reaction_calls += 1

    async def fake_fetch(*args, **kwargs):
        raise ValueError("stop-after-ack")

    async def fake_group_send(_bot, group_id, message) -> None:
        active_messages.append((str(group_id), str(message)))

    analysis.platform_user_id = lambda event: "openid"
    analysis.billing_user_id = lambda event: 1
    analysis.resolve_score_qqid = lambda event: 1
    analysis.use_qq_mode = lambda event: True
    analysis.plugin_send = fake_send
    analysis.plugin_finish = fake_finish
    analysis.react_processing = fake_reaction
    analysis.fetch_for_analysis = fake_fetch
    analysis.get_event_group_id = lambda event: "group-openid"
    analysis.ensure_sender_mention = lambda message, event: message
    analysis.adapt_reply_payload = lambda message, **kwargs: message
    analysis.send_group_message = fake_group_send

    await analysis._handle_impl(FakeMatcher(), object(), FakeEvent(), FakeArgs())
    assert reaction_calls == 0, "官方 QQ 不应调用 OneBot 表情 API"
    assert finished == []
    assert active_messages == [("group-openid", "stop-after-ack")]

    analysis.maiconfig = old_config
    for name, value in old_functions.items():
        setattr(analysis, name, value)


async def main() -> None:
    await test_qq_send_backpressure()
    await test_qq_media_keeps_text_lane_free()
    await test_analysis_backpressure()
    await test_analysis_rejects_duplicate_user()
    await test_official_qq_skips_onebot_reaction()


asyncio.run(main())
print("concurrency backpressure tests: ok")
