from __future__ import annotations

import io
import json

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


def get_peer_stats():
    global _peer_stats
    if _peer_stats is None and maiconfig.b50_assets_path:
        _peer_stats = load_peer_stats(maiconfig.b50_assets_path)
    return _peer_stats


def set_peer_stats(stats):
    global _peer_stats
    _peer_stats = stats


b50_analysis_cmd = on_command(
    '锐评一下',
    aliases={'分析b50', '分析B50', 'B50分析'},
    priority=4,
    block=True,
)


@b50_analysis_cmd.handle()
async def _handle(matcher: Matcher, bot: Bot, event: MessageEvent, args: Message = CommandArg()):
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
        await plugin_send(
            matcher,
            pending,
            event=event,
            mention_sender=use_qq_mode(event),
        )

    # The official QQ gateway may not support the OneBot reaction API.  Send
    # the visible acknowledgement first so a slow/failed reaction request can
    # never make a long-running roast look like a silent command.
    await react_processing(bot, event)

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
        b50_data = await fetch_for_analysis(
            legacy_qq, assets_path=maiconfig.b50_assets_path
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

    peer_stats = get_peer_stats()
    context = build_context(b50_data, peer_stats)
    context['player']['qq'] = str(legacy_qq)

    try:
        reserved = reserve_analysis_charge(billing_qq)
    except BreakInsufficientError as e:
        await plugin_finish(
            matcher, str(e), event=event, mention_sender=use_qq_mode(event)
        )
        return
    try:
        ensure_image_render_affordable(billing_qq)
    except BreakInsufficientError as e:
        refund_analysis_charge(billing_qq, reserved, reason='render:insufficient')
        await plugin_finish(
            matcher, str(e), event=event, mention_sender=use_qq_mode(event)
        )
        return

    failure_stage = '分析生成'
    try:
        analysis_text, token_usage = await generate_analysis(context, maiconfig, style)
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
        refund_analysis_charge(
            billing_qq,
            reserved,
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
    charged = settle_analysis_charge(
        billing_qq,
        cost,
        reserved=reserved,
        token_usage=token_usage,
    )

    balance = break_db.get_balance(billing_qq)
    query_footer = take_break_charge_footer()
    footer_parts = []
    if query_footer:
        footer_parts.extend(query_footer)
    render_line = settle_image_render(billing_qq)
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
                usage_available=usage_available,
            )
        )
    footer = '\n' + '\n'.join(footer_parts)
    await plugin_finish(
        matcher,
        MessageSegment.image(buf) + MessageSegment.text(footer),
        event=event,
        mention_sender=use_qq_mode(event),
        publish_qq_image=True,
    )
