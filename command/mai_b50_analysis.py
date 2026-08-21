from __future__ import annotations

import asyncio
import time
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
    adapt_reply_payload,
    billing_user_id,
    ensure_sender_mention,
    get_event_group_id,
    plugin_finish,
    plugin_send,
    platform_user_id,
    resolve_score_qqid,
    send_group_message,
    send_private_message,
    use_qq_mode,
)
from ..libraries.maimaidx_reaction import react_processing
from ..libraries.maimaidx_processing_time import processing_time_estimator
from ..libraries.maimaidx_break import (
    analysis_token_cost,
    break_db,
    ensure_image_render_affordable,
    format_analysis_cost_line,
    format_analysis_pricing_help,
    format_free_window_exemption,
    format_freedom_exemption,
    refund_analysis_charge,
    reserve_analysis_charge,
    settle_analysis_charge,
    settle_image_render,
    take_break_charge_footer,
)
from ..libraries.maimaidx_roast_v2 import (
    build_evidence_pack,
    fetch_snapshot,
    generate_report,
    normalize_style,
    render_report,
)
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
_ANALYSIS_TIMING_KEY = "b50_analysis"
_ANALYSIS_FALLBACK_SECONDS = 90


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


def _format_analysis_estimate(seconds: int, samples: int) -> str:
    if samples:
        return (
            f"根据最近 {samples} 次成功锐评的真实平均耗时，"
            f"预计约 {seconds} 秒完成。"
        )
    return (
        f"暂无真实锐评耗时样本，首次预计约 {seconds} 秒完成；"
        "完成后会自动校准。"
    )


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
    if bool(getattr(reserved, "daily_free", False)):
        return "（今日首次免费名额未消耗）"
    if bool(getattr(reserved, "freedom", False)):
        return "（FREEDOM 生效，本次未预扣）"
    if int(getattr(reserved, "amount", reserved) or 0) > 0:
        return "（预扣已全额退回）"
    return "（本次未扣费）"


def _empty_token_usage() -> dict[str, int | bool]:
    return {
        "available": False,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cached_input_tokens": 0,
    }


def _format_freedom_line(
    qqid: int, cost: int, remaining: float,
) -> str:
    return format_freedom_exemption(qqid, "锐评", cost, remaining)


async def _deliver_result_or_refund(
    matcher: Matcher,
    event: MessageEvent,
    image,
    billing_qq: int,
    reserved,
    bot: Bot | None = None,
) -> bool:
    """Compatibility adapter for callers that still hold a legacy reservation."""
    try:
        await _run_timed_stage(
            _send_analysis_followup(
                matcher,
                bot,
                event,
                MessageSegment.image(image),
                mention_sender=use_qq_mode(event),
                publish_qq_image=True,
            ),
            stage="图片发送",
            timeout=_timeout("b50_send_timeout_seconds", 30.0),
        )
    except BaseException as exc:
        await asyncio.to_thread(
            refund_analysis_charge,
            billing_qq,
            reserved,
            reason=f"发送结果:{type(exc).__name__}",
        )
        if isinstance(exc, FinishedException) or not isinstance(exc, Exception):
            raise
        await _send_analysis_followup(
            matcher,
            bot,
            event,
            f"锐评图片发送失败：{exc}{_analysis_failure_note(reserved)}",
            mention_sender=use_qq_mode(event),
            finish=True,
        )
        return False
    return True


async def _send_analysis_followup(
    matcher: Matcher,
    bot: Bot | None,
    event: MessageEvent,
    message,
    *,
    mention_sender: bool = False,
    publish_qq_image: bool = False,
    qq_buttons=None,
    finish: bool = False,
):
    """Send long-running results without reusing an expired official-QQ msgid."""
    if bot is not None and use_qq_mode(event):
        if mention_sender:
            message = ensure_sender_mention(message, event)
        payload = adapt_reply_payload(
            message,
            event=event,
            publish_qq_image=publish_qq_image,
        )
        group_id = get_event_group_id(event)
        if group_id is not None:
            await send_group_message(bot, group_id, payload)
        else:
            await send_private_message(bot, platform_user_id(event), payload)
        return None
    send_kwargs = {
        "event": event,
        "mention_sender": mention_sender,
        "publish_qq_image": publish_qq_image,
    }
    if qq_buttons is not None:
        send_kwargs["qq_buttons"] = qq_buttons
    if finish:
        return await plugin_finish(
            matcher,
            message,
            **send_kwargs,
        )
    return await plugin_send(
        matcher,
        message,
        **send_kwargs,
    )


async def _handle_impl(matcher: Matcher, bot: Bot, event: MessageEvent, args: Message) -> None:
    started_at = time.perf_counter()
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

    pricing_help, estimate = await asyncio.gather(
        asyncio.to_thread(format_analysis_pricing_help),
        asyncio.to_thread(
            processing_time_estimator.estimate,
            _ANALYSIS_TIMING_KEY,
            fallback_seconds=_ANALYSIS_FALLBACK_SECONDS,
        ),
    )
    estimated, samples = estimate
    await plugin_send(
        matcher,
        f"正在处理 B50 锐评，请稍候喵…\n"
        f"{_format_analysis_estimate(estimated, samples)}\n"
        f"{pricing_help.strip().removeprefix('· ')}",
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
        await _send_analysis_followup(
            matcher,
            bot,
            event,
            str(exc),
            mention_sender=use_qq_mode(event),
            qq_buttons=_SHORTCUTS,
            finish=True,
        )
        return
    pack = await asyncio.to_thread(build_evidence_pack, snapshot, _LEGACY_PEER_STATS)
    reserved = None
    try:
        reserved = await asyncio.to_thread(reserve_analysis_charge, billing_qq)
        if not reserved.daily_free:
            await asyncio.to_thread(ensure_image_render_affordable, billing_qq)
    except BreakInsufficientError:
        if reserved is not None:
            await asyncio.to_thread(
                refund_analysis_charge,
                billing_qq,
                reserved,
                reason="制图费用预检不足",
            )
        raise
    try:
        token_usage = _empty_token_usage()
        report, token_usage = await _run_timed_stage(
            generate_report(pack, style),
            stage="模型生成",
            timeout=_timeout("b50_llm_timeout_seconds", 360.0),
        )

        if not isinstance(token_usage, dict):
            token_usage = _empty_token_usage()
        input_tokens = int(token_usage.get("input_tokens") or 0)
        output_tokens = int(token_usage.get("output_tokens") or 0)
        usage_available = bool(token_usage.get("available"))
        cost = await asyncio.to_thread(
            analysis_token_cost,
            input_tokens,
            output_tokens,
            usage_available=usage_available,
        )
        image = await run_image_cpu(render_report, pack, report)
    except BaseException as exc:
        if reserved is not None:
            await asyncio.to_thread(
                refund_analysis_charge,
                billing_qq,
                reserved,
                reason=f"分析流程:{type(exc).__name__}",
            )
        if isinstance(exc, FinishedException) or not isinstance(exc, Exception):
            raise
        if isinstance(exc, (BreakInsufficientError, QBindRequiredError, ValueError)):
            raise
        await _send_analysis_followup(
            matcher,
            bot,
            event,
            f"锐评生成失败：{exc}{_analysis_failure_note(reserved or 0)}",
            mention_sender=use_qq_mode(event),
            qq_buttons=_SHORTCUTS,
            finish=True,
        )
        return

    if not await _deliver_result_or_refund(
        matcher, event, image, billing_qq, reserved, bot,
    ):
        return

    try:
        def _settle_result():
            render_line = None
            if not reserved.daily_free:
                render_line = settle_image_render(billing_qq)
            charged = settle_analysis_charge(
                billing_qq,
                cost,
                reserved=reserved,
                token_usage=token_usage,
            )
            return render_line, charged, break_db.get_balance(billing_qq)

        render_line, charged, balance = await asyncio.to_thread(_settle_result)
    except BaseException as exc:
        await asyncio.to_thread(
            refund_analysis_charge,
            billing_qq,
            reserved,
            reason=f"结算异常:{type(exc).__name__}",
        )
        if isinstance(exc, FinishedException) or not isinstance(exc, Exception):
            raise
        log.exception(f"[roast_v2] settlement failed after delivery: {exc}")
        await _send_analysis_followup(
            matcher,
            bot,
            event,
            "锐评图片已发送，但结算异常；本次预扣已退回。",
            mention_sender=use_qq_mode(event),
            qq_buttons=_SHORTCUTS,
            finish=True,
        )
        return

    footer_parts = take_break_charge_footer()
    if render_line:
        footer_parts.append(render_line)
    if reserved.daily_free:
        footer_parts.append(
            f"🎁 今日首次锐评免费（含图片生成） · 余额 {balance} BREAK"
        )
    elif reserved.freedom:
        freedom_line = await asyncio.to_thread(
            _format_freedom_line,
            billing_qq,
            cost,
            reserved.freedom_remaining,
        )
        footer_parts.append(
            "🪽 FREEDOM 生效，本次未预扣\n"
            + freedom_line
        )
    elif reserved.free_window:
        footer_parts.append(format_free_window_exemption(billing_qq, "锐评", cost))
    cost_line = await asyncio.to_thread(
        format_analysis_cost_line,
        charged=0 if (reserved.daily_free or reserved.freedom or reserved.free_window) else charged,
        balance=balance,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=int(token_usage.get("cached_input_tokens") or 0),
        usage_available=usage_available,
        compact=True,
    )
    footer_parts.append(cost_line)
    elapsed = time.perf_counter() - started_at
    await asyncio.to_thread(
        processing_time_estimator.record,
        _ANALYSIS_TIMING_KEY,
        elapsed,
    )
    footer_parts.append(f"⏱️ 本次锐评用时 {elapsed:.1f} 秒")
    footer_parts.append(report.summary)
    footer_parts.append("更多详情请前往吃分推荐喵")
    await _send_analysis_followup(
        matcher,
        bot,
        event,
        "\n".join(footer_parts),
        mention_sender=use_qq_mode(event),
        qq_buttons=_SHORTCUTS,
        finish=True,
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
        user_lock = _user_lock(lock_key)
        if user_lock.locked():
            await plugin_finish(
                matcher,
                "你已有锐评正在生成，请等待结果，勿重复发送。",
                event=event,
                mention_sender=use_qq_mode(event),
                qq_buttons=_SHORTCUTS,
            )
            return
        async with user_lock:
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
        await _send_analysis_followup(
            matcher,
            bot,
            event,
            str(exc),
            mention_sender=use_qq_mode(event),
            qq_buttons=_SHORTCUTS,
            finish=True,
        )
    except FinishedException:
        # matcher.finish() uses this exception as a normal control-flow signal.
        # Never turn it into the user-facing "未知错误：FinishedException".
        raise
    except Exception as exc:
        log.exception(f"[roast_v2] failed: {exc}")
        await _send_analysis_followup(
            matcher,
            bot,
            event,
            format_command_error(exc),
            mention_sender=use_qq_mode(event),
            qq_buttons=_SHORTCUTS,
            finish=True,
        )
