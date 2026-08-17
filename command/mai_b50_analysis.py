from __future__ import annotations

import asyncio
from weakref import WeakValueDictionary

from loguru import logger as log
from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent, MessageSegment
from nonebot.exception import FinishedException
from nonebot.matcher import Matcher
from nonebot.params import CommandArg

from ..config import maiconfig
from ..libraries.maimaidx_error import BreakInsufficientError, QBindRequiredError, format_command_error
from ..libraries.maimaidx_image_executor import run_image_cpu
from ..libraries.maimaidx_platform import (
    billing_user_id,
    plugin_finish,
    plugin_send,
    platform_user_id,
    resolve_score_qqid,
    use_qq_mode,
)
from ..libraries.maimaidx_reaction import react_processing
from ..libraries.maimaidx_break import refund_analysis_charge
from ..libraries.maimaidx_roast_v2 import (
    build_evidence_pack,
    build_report_fallback,
    fetch_snapshot,
    generate_report,
    normalize_style,
    render_report,
)
from ..libraries.maimaidx_roast_v2.billing import commit_quote, prepare_quote
from ..libraries.maimaidx_roast_v2.style_store import get_style

fetch_for_analysis = fetch_snapshot


_MAX_CONCURRENCY = max(1, int(getattr(maiconfig, "b50_analysis_max_concurrency", 8) or 8))
_ANALYSIS_SEMAPHORE = asyncio.Semaphore(_MAX_CONCURRENCY)
_SEMAPHORE = _ANALYSIS_SEMAPHORE
_USER_LOCKS: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
_LEGACY_PEER_STATS = None
_SHORTCUTS = (
    ("锐评", "锐评一下"),
    ("标准 B50", "b50"),
    ("AP50", "ap50"),
    ("FC50", "fc50"),
    ("含金量", "含金量"),
    ("含水量", "含水量"),
    ("教练总结", "锐评一下 教练"),
    ("自定义风格", "锐评风格 设置 "),
    ("查看风格", "锐评风格 查看"),
)
_ANALYSIS_SHORTCUTS = _SHORTCUTS


def set_peer_stats(stats) -> None:
    """Keep the startup hook compatible; Roast V2 computes its own evidence."""
    global _LEGACY_PEER_STATS
    _LEGACY_PEER_STATS = stats


def _user_lock(user_id: str) -> asyncio.Lock:
    lock = _USER_LOCKS.get(str(user_id))
    if lock is None:
        lock = asyncio.Lock()
        _USER_LOCKS[str(user_id)] = lock
    return lock


def _timeout(name: str, default: float) -> float:
    try:
        return max(0.5, float(getattr(maiconfig, name, default) or default))
    except (TypeError, ValueError):
        return default


def _style_text(args: Message) -> str:
    return " ".join(args.extract_plain_text().strip().split())


class AnalysisStageTimeoutError(TimeoutError):
    pass


async def _run_timed_stage(awaitable, *, stage: str, timeout: float):
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise AnalysisStageTimeoutError(f"{stage}超时") from exc


def _analysis_failure_note(reserved) -> str:
    if int(getattr(reserved, "amount", reserved) or 0) > 0:
        return "（预扣已全额退回）"
    return "（本次未扣费）"


async def _deliver_result_or_refund(
    matcher: Matcher,
    event: MessageEvent,
    image,
    billing_qq: int,
    reserved,
) -> bool:
    """Compatibility adapter for callers that still hold a legacy reservation."""
    try:
        await _run_timed_stage(
            plugin_send(
                matcher,
                MessageSegment.image(image),
                event=event,
                mention_sender=use_qq_mode(event),
                publish_qq_image=True,
            ),
            stage="图片发送",
            timeout=_timeout("b50_send_timeout_seconds", 30.0),
        )
    except Exception as exc:
        await asyncio.to_thread(
            refund_analysis_charge,
            billing_qq,
            reserved,
            reason=f"发送结果:{type(exc).__name__}",
        )
        await plugin_finish(
            matcher,
            f"锐评图片发送失败：{exc}{_analysis_failure_note(reserved)}",
            event=event,
            mention_sender=use_qq_mode(event),
        )
        return False
    return True


async def _handle_impl(matcher: Matcher, bot: Bot, event: MessageEvent, args: Message) -> None:
    billing_qq = billing_user_id(event)
    legacy_qq = resolve_score_qqid(event)
    style_text = _style_text(args)
    if style_text:
        style = normalize_style(
            style_text,
            max_length=int(getattr(maiconfig, "roast_v2_style_max_length", 240) or 240),
        )
    else:
        style = await asyncio.to_thread(get_style, platform_user_id(event))

    if use_qq_mode(event) or not bool(getattr(maiconfig, "maimaidx_compact_messages", True)):
        await plugin_send(
            matcher,
            "正在处理 B50 锐评，请稍候喵…",
            event=event,
            mention_sender=use_qq_mode(event),
        )
    if not use_qq_mode(event):
        try:
            await asyncio.wait_for(
                react_processing(bot, event),
                timeout=_timeout("b50_reaction_timeout_seconds", 2.0),
            )
        except Exception:
            pass

    try:
        snapshot = await _run_timed_stage(
            fetch_for_analysis(legacy_qq),
            stage="成绩拉取",
            timeout=_timeout("b50_fetch_timeout_seconds", 45.0),
        )
    except Exception as exc:
        await plugin_finish(
            matcher,
            str(exc),
            event=event,
            mention_sender=use_qq_mode(event),
            qq_buttons=_SHORTCUTS,
        )
        return
    pack = await asyncio.to_thread(build_evidence_pack, snapshot)
    quote = await asyncio.to_thread(
        prepare_quote,
        billing_qq,
        int(getattr(maiconfig, "roast_v2_cost", 4) or 4),
    )
    try:
        report = await asyncio.wait_for(
            generate_report(pack, style),
            timeout=_timeout("b50_llm_timeout_seconds", 180.0),
        )
    except Exception as exc:
        log.warning(
            f"[roast_v2] model failed, using deterministic fallback: "
            f"{type(exc).__name__}: {exc}"
        )
        report = build_report_fallback(pack, style)
    image = await run_image_cpu(render_report, pack, report)
    await asyncio.wait_for(
        plugin_send(
            matcher,
            MessageSegment.image(image),
            event=event,
            mention_sender=use_qq_mode(event),
            publish_qq_image=True,
        ),
        timeout=_timeout("b50_send_timeout_seconds", 30.0),
    )
    settlement = await asyncio.to_thread(commit_quote, billing_qq, quote)
    summary = report.summary
    charge = int(settlement.get("charged", 0) or 0)
    balance = int(settlement.get("balance", 0) or 0)
    if settlement.get("free"):
        footer = f"🎁 今日首次锐评免费 · 余额 {balance} BREAK\n{summary}"
    elif settlement.get("free_window"):
        footer = f"🕒 免费时段 · 余额 {balance} BREAK\n{summary}"
    elif settlement.get("freedom"):
        footer = f"🪽 FREEDOM 生效，本次免扣 · 余额 {balance} BREAK\n{summary}"
    else:
        footer = f"💳 锐评 V2 消耗 {charge} BREAK · 余额 {balance} BREAK\n{summary}"
    await plugin_finish(
        matcher,
        footer,
        event=event,
        mention_sender=use_qq_mode(event),
        qq_buttons=_SHORTCUTS,
    )


roast_v2_cmd = on_command(
    "锐评一下",
    aliases={"分析b50", "分析B50", "B50分析"},
    priority=4,
    block=True,
)


@roast_v2_cmd.handle()
async def _handle(matcher: Matcher, bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    try:
        queue_timeout = _timeout("b50_analysis_queue_timeout_seconds", 2.0)
        try:
            lock_key = platform_user_id(event)
        except AttributeError:
            lock_key = f"event:{id(event)}"
        async with _user_lock(lock_key):
            try:
                await asyncio.wait_for(_ANALYSIS_SEMAPHORE.acquire(), timeout=queue_timeout)
            except asyncio.TimeoutError:
                await plugin_finish(
                    matcher,
                "当前锐评任务较多，为避免卡住已拒绝本次请求，请稍后再试。",
                    event=event,
                    mention_sender=use_qq_mode(event),
                    qq_buttons=_SHORTCUTS,
                )
                return
            try:
                await _handle_impl(matcher, bot, event, args)
            finally:
                _ANALYSIS_SEMAPHORE.release()
    except (BreakInsufficientError, QBindRequiredError, ValueError) as exc:
        await plugin_finish(
            matcher,
            str(exc),
            event=event,
            mention_sender=use_qq_mode(event),
            qq_buttons=_SHORTCUTS,
        )
    except FinishedException:
        # matcher.finish() uses this exception as a normal control-flow signal.
        # Never turn it into the user-facing "未知错误：FinishedException".
        raise
    except Exception as exc:
        log.exception(f"[roast_v2] failed: {exc}")
        await plugin_finish(
            matcher,
            format_command_error(exc),
            event=event,
            mention_sender=use_qq_mode(event),
            qq_buttons=_SHORTCUTS,
        )
