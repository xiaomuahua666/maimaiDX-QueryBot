from __future__ import annotations

import asyncio
import time
from typing import Any
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
    build_markdown_message,
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
_ROAST_FIRST_CHUNK_TIMEOUT_DEFAULT = 30.0


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


_ACTIVE_PROGRESS: dict[str, int] = {}
_ACTIVE_REASONING: dict[str, bool] = {}


def _active_progress_key(event: MessageEvent) -> str:
    try:
        return str(platform_user_id(event))
    except AttributeError:
        return f"event:{id(event)}"


def _analysis_progress_text(progress_key: str | None = None) -> str:
    chars = int(_ACTIVE_PROGRESS.get(progress_key or "") or 0)
    if _ACTIVE_REASONING.get(progress_key or ""):
        return "模型正在思考中，已收到推理内容，请稍等喵。"
    if chars > 0:
        return f"已生成约 {chars} 字，仍在继续，请稍等喵。"
    return "正在等待模型输出，请稍等喵。"


def _mark_roast_progress(
    progress_key: str,
    first_chunk_event,
    progress,
) -> None:
    if isinstance(progress, dict):
        chars = int(progress.get("chars") or 0)
        reasoning = bool(progress.get("reasoning"))
    else:
        chars = len(str(progress or ""))
        reasoning = False
    if reasoning:
        _ACTIVE_REASONING[progress_key] = True
        first_chunk_event.set()
        return
    if chars:
        _ACTIVE_PROGRESS[progress_key] = chars
        _ACTIVE_REASONING.pop(progress_key, None)
        first_chunk_event.set()


async def _wait_for_roast_first_chunk(
    task,
    first_chunk_event,
    stream_unavailable,
) -> None:
    """模型 30 秒未开始输出则超时；已开始后不再受总时长限制。"""
    first_waiter = asyncio.ensure_future(first_chunk_event.wait())
    non_stream_waiter = asyncio.ensure_future(stream_unavailable.wait())
    try:
        done, _pending = await asyncio.wait(
            {task, first_waiter, non_stream_waiter},
            timeout=_timeout(
                "b50_llm_first_chunk_timeout_seconds",
                _ROAST_FIRST_CHUNK_TIMEOUT_DEFAULT,
            ),
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        first_waiter.cancel()
        non_stream_waiter.cancel()
    if task in done:
        return
    if first_waiter in done:
        return
    if non_stream_waiter in done:
        return
    raise AnalysisStageTimeoutError("模型 30 秒内未开始输出，已超时")


async def _run_roast_generation(
    progress_key: str,
    *,
    pack,
    style,
) -> Any:
    """流式首块 30 秒超时；流式被网关拒绝并降级为普通请求时按请求超时执行。"""
    first_chunk_event = asyncio.Event()
    stream_unavailable = asyncio.Event()
    task = asyncio.ensure_future(
        generate_report(
            pack,
            style,
            on_progress=lambda content: _mark_roast_progress(
                progress_key,
                first_chunk_event,
                content,
            ),
            on_stream_unavailable=stream_unavailable.set,
        )
    )

    try:
        if stream_unavailable.is_set():
            return await _run_timed_stage(
                task,
                stage="模型生成",
                timeout=_timeout("b50_llm_timeout_seconds", 300.0),
            )
        await _wait_for_roast_first_chunk(task, first_chunk_event, stream_unavailable)
        if stream_unavailable.is_set():
            return await _run_timed_stage(
                task,
                stage="模型生成",
                timeout=_timeout("b50_llm_timeout_seconds", 300.0),
            )
        return await task
    except (asyncio.CancelledError, Exception) as exc:
        # 首块超时必须无条件取消任务：首个 chunk 可能恰在 wait 超时与本检查
        # 之间到达并置位 first_chunk_event，若据此跳过 cancel，超时已上报而
        # 任务仍在后台流式消费（token 白烧 + Task exception never retrieved）。
        if (
            isinstance(exc, (asyncio.CancelledError, AnalysisStageTimeoutError))
            or not first_chunk_event.is_set()
        ):
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        raise
    finally:
        _ACTIVE_PROGRESS.pop(progress_key, None)
        _ACTIVE_REASONING.pop(progress_key, None)


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


def _escape_markdown(value: object) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ")
    for char in ("\\", "`", "*", "_", "[", "]", "(", ")", "#", ">"):
        text = text.replace(char, f"\\{char}")
    return text


def _build_analysis_summary_markdown(report, footer_parts: list[str], elapsed: float) -> str:
    lines = [
        "## 锐评完成",
        "",
        "### 一句话总结",
        f"> {_escape_markdown(report.summary)}",
        "",
        "### 本次结算",
    ]
    for part in footer_parts:
        for line in str(part or "").splitlines():
            if line.strip():
                lines.append(f"- {_escape_markdown(line.strip())}")
    lines.append(f"- ⏱️ 本次锐评用时 **{elapsed:.1f} 秒**")
    actions = [str(item).strip() for item in (report.actions or []) if str(item).strip()]
    if actions:
        lines.extend(["", "### 接下来怎么练"])
        for index, action in enumerate(actions[:2], 1):
            lines.append(f"{index}. {_escape_markdown(action)}")
    lines.extend(["", "更多详情请前往 **吃分推荐** 喵"])
    return "\n".join(lines)


def _build_analysis_notice_markdown(
    title: str,
    body: object,
    *,
    section: str | None = None,
) -> str:
    lines = [f"## {_escape_markdown(title)}", ""]
    if section:
        lines.extend([f"### {_escape_markdown(section)}", ""])
    body_lines = [line.strip() for line in str(body or "").splitlines() if line.strip()]
    lines.extend(f"> {_escape_markdown(line)}" for line in body_lines)
    return "\n".join(lines)


def _analysis_notice(
    event: MessageEvent,
    title: str,
    body: object,
    *,
    section: str | None = None,
):
    return build_markdown_message(
        _build_analysis_notice_markdown(title, body, section=section),
        event=event,
    )


async def _refund_reserved_safely(
    billing_qq: int,
    reserved,
    *,
    reason: str,
) -> bool:
    """Refund without replacing the original user-facing failure."""
    if reserved is None or int(getattr(reserved, "amount", reserved) or 0) <= 0:
        return True
    for attempt in range(2):
        try:
            await asyncio.to_thread(
                refund_analysis_charge,
                billing_qq,
                reserved,
                reason=reason,
            )
            return True
        except Exception as exc:
            code = exc.args[0] if exc.args else None
            if attempt == 0 and code in {1205, 1213}:
                await asyncio.sleep(0.2)
                continue
            log.exception(f"[roast_v2] refund failed reason={reason}: {exc}")
            return False
    return False


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
        refunded = await _refund_reserved_safely(
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
            _analysis_notice(
                event,
                "锐评图片发送失败",
                (
                    f"{exc}{_analysis_failure_note(reserved)}"
                    if refunded
                    else f"{exc}（退款写入失败，异常已记录）"
                ),
                section="处理结果",
            ),
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
    start_markdown = "\n".join(
        [
            "## B50 锐评已受理",
            "",
            "> 正在读取成绩并生成点评，请稍候喵。",
            "",
            "### 预计用时",
            f"- {_escape_markdown(_format_analysis_estimate(estimated, samples))}",
            "",
            "### 费用说明",
            f"- {_escape_markdown(pricing_help.strip().removeprefix('· '))}",
        ]
    )
    await plugin_send(
        matcher,
        build_markdown_message(start_markdown, event=event),
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
            _analysis_notice(event, "成绩读取失败", exc, section="原因"),
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
            await _refund_reserved_safely(
                billing_qq,
                reserved,
                reason="制图费用预检不足",
            )
        raise
    try:
        token_usage = _empty_token_usage()
        report, token_usage = await _run_roast_generation(
            _active_progress_key(event),
            pack=pack,
            style=style,
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
            refunded = await _refund_reserved_safely(
                billing_qq,
                reserved,
                reason=f"分析流程:{type(exc).__name__}",
            )
        else:
            refunded = True
        if isinstance(exc, FinishedException) or not isinstance(exc, Exception):
            raise
        if isinstance(exc, (BreakInsufficientError, QBindRequiredError, ValueError)):
            raise
        await _send_analysis_followup(
            matcher,
            bot,
            event,
            _analysis_notice(
                event,
                "锐评生成失败",
                (
                    f"{exc}{_analysis_failure_note(reserved or 0)}"
                    if refunded
                    else f"{exc}（退款写入失败，异常已记录）"
                ),
                section="处理结果",
            ),
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
            # 余额读取不能留在退款保护区内：结算此时已提交，纯读失败若触发
            # 全额退款，用户会同时拿到结果和退款（净多退）。读失败时退化为
            # 展示 0 并记日志，不影响账目。
            try:
                balance = break_db.get_balance(billing_qq)
            except Exception as balance_exc:
                log.warning(
                    f"[roast_v2] 结算成功但读取余额失败，页脚余额可能不准："
                    f"{type(balance_exc).__name__}: {balance_exc}"
                )
                balance = 0
            return render_line, charged, balance

        render_line, charged, balance = await asyncio.to_thread(_settle_result)
    except BaseException as exc:
        refunded = await _refund_reserved_safely(
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
            _analysis_notice(
                event,
                "锐评图片已发送",
                (
                    "结算发生异常，本次预扣已退回。"
                    if refunded
                    else "结算及退款写入失败，异常已记录，请联系 Bot 管理员处理。"
                ),
                section="结算状态",
            ),
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
    summary_message = build_markdown_message(
        _build_analysis_summary_markdown(report, footer_parts, elapsed),
        event=event,
    )
    await _send_analysis_followup(
        matcher,
        bot,
        event,
        summary_message,
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
                _analysis_notice(
                    event,
                    "锐评正在生成",
                    (
                        "你已有一份锐评任务在处理中，请等待结果，勿重复发送。\n"
                        + _analysis_progress_text(_active_progress_key(event))
                    ),
                ),
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
                    _analysis_notice(
                        event,
                        "锐评队列繁忙",
                        "当前任务较多，为避免长时间卡住，本次请求未进入队列，请稍后再试。",
                    ),
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
            _analysis_notice(event, "锐评暂时无法开始", exc, section="原因"),
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
            _analysis_notice(
                event,
                "锐评处理异常",
                format_command_error(exc),
                section="错误信息",
            ),
            mention_sender=use_qq_mode(event),
            qq_buttons=_SHORTCUTS,
            finish=True,
        )
