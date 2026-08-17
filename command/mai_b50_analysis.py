from __future__ import annotations

import asyncio
import io
import json
import time

from loguru import logger as log
from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent, MessageSegment
from nonebot.matcher import Matcher
from nonebot.params import CommandArg

from ..config import maiconfig
from ..libraries.b50_analysis import (
    build_context,
    check_llm_output,
    check_user_input,
    generate_analysis,
    load_peer_stats,
    prepare_render_cache,
    render_image,
)
from ..libraries.b50_analysis.adapter import fetch_for_analysis
from ..libraries.maimaidx_break import (
    analysis_token_cost,
    break_db,
    ensure_image_render_affordable,
    format_analysis_cost_line,
    format_analysis_pricing_help,
    format_freedom_exemption,
    refund_analysis_charge,
    reserve_analysis_charge,
    settle_analysis_charge,
    settle_image_render,
    take_break_charge_footer,
)
from ..libraries.maimaidx_error import BreakInsufficientError, format_command_error, QBindRequiredError
from ..libraries.maimaidx_image_executor import run_image_cpu
from ..libraries.maimaidx_platform import (
    billing_user_id,
    platform_user_id,
    plugin_finish,
    plugin_send,
    resolve_score_qqid,
    use_qq_mode,
)
from ..libraries.maimaidx_reaction import react_processing

_peer_stats = None

try:
    _ANALYSIS_MAX_CONCURRENCY = max(
        1, int(getattr(maiconfig, 'b50_analysis_max_concurrency', 12) or 12)
    )
except (TypeError, ValueError):
    _ANALYSIS_MAX_CONCURRENCY = 12
_ANALYSIS_SEMAPHORE = asyncio.Semaphore(_ANALYSIS_MAX_CONCURRENCY)

_ANALYSIS_SHORTCUTS = (
    ('锐评', '锐评一下'),
    ('标准 B50', 'b50'),
    ('AP50', 'ap50'),
    ('FC50', 'fc50'),
    ('含金量', '含金量'),
    ('含水量', '含水量'),
)


class AnalysisStageTimeoutError(TimeoutError):
    pass


def _timeout_seconds(name: str, default: float) -> float:
    try:
        return max(0.5, float(getattr(maiconfig, name, default) or default))
    except (TypeError, ValueError):
        return default


async def _run_timed_stage(awaitable, *, stage: str, timeout: float, qq: int):
    started = time.monotonic()
    log.info(f'[b50_analysis] {stage}开始 qq={qq} timeout={timeout:.1f}s')
    try:
        result = await asyncio.wait_for(awaitable, timeout=timeout)
    except asyncio.TimeoutError as exc:
        elapsed = time.monotonic() - started
        log.warning(f'[b50_analysis] {stage}超时 qq={qq} elapsed={elapsed:.1f}s')
        raise AnalysisStageTimeoutError(
            f'{stage}超时（超过 {timeout:g} 秒），请稍后重试'
        ) from exc
    elapsed = time.monotonic() - started
    log.info(f'[b50_analysis] {stage}完成 qq={qq} elapsed={elapsed:.1f}s')
    return result


def get_peer_stats():
    global _peer_stats
    if _peer_stats is None and maiconfig.b50_assets_path:
        _peer_stats = load_peer_stats(maiconfig.b50_assets_path)
    return _peer_stats


def set_peer_stats(stats):
    global _peer_stats
    _peer_stats = stats


async def _deliver_result_or_refund(
    matcher: Matcher,
    event: MessageEvent,
    image: io.BytesIO,
    billing_qq: int,
    reserved,
) -> bool:
    """Only let the caller settle a reservation after the image was sent."""
    try:
        await _run_timed_stage(
            plugin_send(
                matcher,
                MessageSegment.image(image),
                event=event,
                mention_sender=use_qq_mode(event),
                publish_qq_image=True,
            ),
            stage='图片发送',
            timeout=_timeout_seconds('b50_send_timeout_seconds', 30.0),
            qq=billing_qq,
        )
    except BaseException as exc:
        await asyncio.to_thread(
            refund_analysis_charge, billing_qq, reserved,
            reason=f'发送结果:{type(exc).__name__}',
        )
        if not isinstance(exc, Exception):
            raise
        await plugin_finish(
            matcher,
            f'锐评图片发送失败：{exc}（预扣已全额退回）',
            event=event,
            mention_sender=use_qq_mode(event),
        )
        return False
    return True


b50_analysis_cmd = on_command(
    '锐评一下',
    aliases={'分析b50', '分析B50', 'B50分析'},
    priority=4,
    block=True,
)


async def _handle_impl(matcher: Matcher, bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    style = args.extract_plain_text().strip()
    qq = platform_user_id(event)
    billing_qq = billing_user_id(event)

    if not maiconfig.b50_llm_key:
        await plugin_finish(
            matcher,
            '未配置 b50_llm_key，请在 .env 中填写 API Key',
            event=event,
            mention_sender=use_qq_mode(event),
        )
        return
    if not maiconfig.b50_assets_path:
        await plugin_finish(
            matcher,
            '未配置 b50_assets_path，请在 .env 中填写分析素材目录',
            event=event,
            mention_sender=use_qq_mode(event),
        )
        return

    pricing_help = format_analysis_pricing_help().strip().removeprefix('· ')
    pending = f'正在处理 B50 锐评，请稍候…\n{pricing_help}'
    if use_qq_mode(event) or not bool(
        getattr(maiconfig, 'maimaidx_compact_messages', True)
    ):
        try:
            await asyncio.wait_for(
                plugin_send(
                    matcher,
                    pending,
                    event=event,
                    mention_sender=use_qq_mode(event),
                ),
                timeout=_timeout_seconds('b50_send_timeout_seconds', 30.0),
            )
        except Exception as exc:
            log.warning(
                f'[b50_analysis] 处理中提示发送失败 qq={billing_qq}: '
                f'{type(exc).__name__}: {exc}'
            )

    # 官方 QQ 没有 OneBot 的 set_msg_emoji_like；跳过该调用，避免每个锐评
    # 触发 1+2+4 秒的无效重试。OneBot 仍保留处理中表情。
    if not use_qq_mode(event):
        try:
            await asyncio.wait_for(
                react_processing(bot, event),
                timeout=_timeout_seconds('b50_reaction_timeout_seconds', 2.0),
            )
        except asyncio.TimeoutError:
            log.warning(f'[b50_analysis] 处理表情超时 qq={billing_qq}，继续执行')

    if style:
        mod_result = check_user_input(style)
        if not mod_result.get('allowed', True):
            await plugin_finish(
                matcher,
                mod_result.get('reason', '请求包含不适合处理的内容，本次分析已驳回'),
                event=event,
                mention_sender=use_qq_mode(event),
            )
            return

    try:
        legacy_qq = resolve_score_qqid(event)
        b50_data = await _run_timed_stage(
            fetch_for_analysis(
                legacy_qq, assets_path=maiconfig.b50_assets_path
            ),
            stage='成绩拉取',
            timeout=_timeout_seconds('b50_fetch_timeout_seconds', 45.0),
            qq=billing_qq,
        )
    except BreakInsufficientError as e:
        await plugin_finish(
            matcher, str(e), event=event, mention_sender=use_qq_mode(event)
        )
        return
    except QBindRequiredError as e:
        await plugin_finish(
            matcher, str(e), event=event, mention_sender=use_qq_mode(event)
        )
        return
    except ValueError as e:
        await plugin_finish(
            matcher, str(e), event=event, mention_sender=use_qq_mode(event)
        )
        return
    except AnalysisStageTimeoutError as e:
        await plugin_finish(
            matcher, str(e), event=event, mention_sender=use_qq_mode(event)
        )
        return
    except Exception as e:
        log.warning(f'[b50_analysis] 拉取 B50 失败 qq={qq}: {type(e).__name__}: {e}')
        await plugin_finish(
            matcher,
            format_command_error(e),
            event=event,
            mention_sender=use_qq_mode(event),
        )
        return

    # 有本地存档时附带真实 Rating 趋势，供推分可行性判断
    try:
        from ..libraries.maimaidx_data_storage import data_storage

        if data_storage.is_enabled(int(legacy_qq)):
            hist = data_storage.get_rating_history(int(legacy_qq), days=90)
            if hist:
                points = [
                    {
                        'date': m.get('date'),
                        'rating': int(m.get('rating') or 0),
                    }
                    for m in reversed(hist)
                    if m.get('date') is not None
                ]
                if len(points) >= 2:
                    b50_data['rating_trend'] = {
                        'points': points,
                        'delta': int(points[-1]['rating']) - int(points[0]['rating']),
                    }
    except Exception as e:
        log.debug(f'[b50_analysis] 读取推分趋势失败 qq={legacy_qq}: {e}')

    # peer_stats 解压和 B50 证据聚合都属于 CPU/文件任务，放到工作线程，
    # 让 NoneBot 事件循环继续接收消息和处理轻量命令。
    peer_stats = await asyncio.to_thread(get_peer_stats)
    context = await asyncio.to_thread(build_context, b50_data, peer_stats)
    context['player']['qq'] = str(legacy_qq)

    try:
        reserved = await asyncio.to_thread(reserve_analysis_charge, billing_qq)
    except BreakInsufficientError as e:
        await plugin_finish(
            matcher, str(e), event=event, mention_sender=use_qq_mode(event)
        )
        return
    try:
        await asyncio.to_thread(ensure_image_render_affordable, billing_qq)
    except BreakInsufficientError as e:
        await asyncio.to_thread(
            refund_analysis_charge,
            billing_qq,
            reserved,
            reason='render:insufficient',
        )
        await plugin_finish(
            matcher, str(e), event=event, mention_sender=use_qq_mode(event)
        )
        return

    failure_stage = '分析生成'
    try:
        analysis_text, token_usage = await _run_timed_stage(
            generate_analysis(context, maiconfig, style),
            stage='模型分析',
            timeout=_timeout_seconds('b50_llm_timeout_seconds', 180.0) + 5.0,
            qq=billing_qq,
        )
        try:
            _parsed = json.loads(analysis_text)
            for field in ('overall_roast', 'impression_roast', 'title'):
                original = str(_parsed.get(field) or '')
                if not original:
                    continue
                checked = check_llm_output(original)
                if checked.get('safe', True):
                    continue
                _parsed[field] = checked.get('redacted', original)
            if isinstance(_parsed.get('push_recommendations'), list):
                context.setdefault('evidence', {})['push_recommendations'] = (
                    _parsed.get('push_recommendations') or []
                )
            analysis_text = json.dumps(_parsed, ensure_ascii=False)
        except Exception:
            pass

        failure_stage = '制图'
        await prepare_render_cache(context, maiconfig.b50_assets_path)
        def _render_and_encode():
            image = render_image(context, analysis_text, maiconfig.b50_assets_path)
            buffer = io.BytesIO()
            image.save(buffer, format='PNG')
            buffer.seek(0)
            return buffer
        buf = await run_image_cpu(_render_and_encode)
    except BaseException as e:
        await asyncio.to_thread(
            refund_analysis_charge, billing_qq, reserved,
            reason=f'{failure_stage}:{type(e).__name__}',
        )
        if not isinstance(e, Exception):
            raise
        failure_note = (
            '（FREEDOM 生效，本次未预扣）'
            if reserved.freedom
            else '（预扣已全额退回）'
        )
        await plugin_finish(
            matcher,
            f'{failure_stage}失败：{e}{failure_note}',
            event=event,
            mention_sender=use_qq_mode(event),
        )
        return

    input_tokens = int(token_usage.get('input_tokens') or 0)
    output_tokens = int(token_usage.get('output_tokens') or 0)
    usage_available = bool(token_usage.get('available'))
    cost = analysis_token_cost(
        input_tokens,
        output_tokens,
        usage_available=usage_available,
    )
    # 先确认成品图片已成功交付，再结算预扣。过去在这里先扣费，QQ 图片
    # 上传失败时就会出现“没收到结果、退款也没执行”的资金安全问题。
    if not await _deliver_result_or_refund(
        matcher, event, buf, billing_qq, reserved,
    ):
        return

    # 图片已经送达。按真实 Token 多退少补；余额允许为负数。
    try:
        def _settle_result():
            render = settle_image_render(billing_qq)
            charge = settle_analysis_charge(
                billing_qq,
                cost,
                reserved=reserved,
                token_usage=token_usage,
            )
            return render, charge, break_db.get_balance(billing_qq)

        render_line, charged, balance = await asyncio.to_thread(_settle_result)
    except Exception as e:
        await asyncio.to_thread(
            refund_analysis_charge, billing_qq, reserved,
            reason=f'结算异常:{type(e).__name__}',
        )
        log.exception(f'[b50_analysis] 结算失败 qq={billing_qq}: {e}')
        await plugin_finish(
            matcher,
            '锐评图片已发送，但结算异常；本次预扣已退回。',
            event=event,
            mention_sender=use_qq_mode(event),
            qq_buttons=_ANALYSIS_SHORTCUTS,
        )
        return

    query_footer = take_break_charge_footer()
    footer_parts = []
    if query_footer:
        footer_parts.extend(query_footer)
    if render_line:
        footer_parts.append(render_line)
    if reserved.freedom:
        footer_parts.append(
            format_freedom_exemption(
                billing_qq,
                '锐评',
                cost,
                reserved.freedom_remaining,
            )
        )
    else:
        footer_parts.append(
            format_analysis_cost_line(
                charged=charged,
                balance=balance,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_input_tokens=int(token_usage.get('cached_input_tokens') or 0),
                usage_available=usage_available,
            )
        )
    footer = '\n'.join(footer_parts)
    await plugin_finish(
        matcher,
        footer,
        event=event,
        mention_sender=use_qq_mode(event),
        qq_buttons=_ANALYSIS_SHORTCUTS,
    )


@b50_analysis_cmd.handle()
async def _handle(
    matcher: Matcher,
    bot: Bot,
    event: MessageEvent,
    args: Message = CommandArg(),
):
    """Admit only a small number of model/render/send pipelines at once."""
    try:
        queue_timeout = max(
            0.0,
            float(
                getattr(
                    maiconfig,
                    'b50_analysis_queue_timeout_seconds',
                    2.0,
                )
                or 0.0
            ),
        )
    except (TypeError, ValueError):
        queue_timeout = 2.0
    try:
        await asyncio.wait_for(_ANALYSIS_SEMAPHORE.acquire(), timeout=queue_timeout)
    except asyncio.TimeoutError:
        await plugin_finish(
            matcher,
            '当前锐评任务较多，为避免卡住已拒绝本次请求，请稍后再试。',
            event=event,
            mention_sender=use_qq_mode(event),
        )
        return
    try:
        await _handle_impl(matcher, bot, event, args)
    finally:
        _ANALYSIS_SEMAPHORE.release()
