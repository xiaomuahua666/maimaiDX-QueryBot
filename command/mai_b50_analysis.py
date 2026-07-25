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
    format_analysis_cost_line,
    settle_analysis_charge,
    take_break_charge_footer,
)
from ..libraries.maimaidx_error import BreakInsufficientError, format_command_error, QBindRequiredError
from ..libraries.maimaidx_platform import platform_user_id, resolve_query_qqid
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
    qq = int(event.get_user_id())
    billing_qq = int(platform_user_id(event))

    if not maiconfig.b50_llm_key:
        await matcher.finish('未配置 b50_llm_key，请在 .env 中填写 API Key', reply_message=True)
        return
    if not maiconfig.b50_assets_path:
        await matcher.finish('未配置 b50_assets_path，请在 .env 中填写分析素材目录', reply_message=True)
        return

    await react_processing(bot, event)

    pending = (
        '正在查询 B50，请稍候…\n'
        '锐评按模型实际 Token 计费：输入每 4,000 Token、输出每 1,000 Token '
        '各计 1 BREAK，合计向上取整；最低 2、最高 20 BREAK。'
        '采用先用后付，完成后才按实际用量扣费。'
    )
    if not bool(getattr(maiconfig, 'maimaidx_compact_messages', True)):
        await matcher.send(pending, reply_message=True)

    if style:
        mod_result = check_user_input(style)
        if not mod_result.get('allowed', True):
            await matcher.finish(
                mod_result.get('reason', '请求包含不适合处理的内容，本次分析已驳回'),
                reply_message=True,
            )
            return

    try:
        legacy_qq = resolve_query_qqid(billing_qq)
        b50_data = await fetch_for_analysis(
            legacy_qq, assets_path=maiconfig.b50_assets_path
        )
    except BreakInsufficientError as e:
        await matcher.finish(str(e), reply_message=True)
        return
    except QBindRequiredError as e:
        await matcher.finish(str(e), reply_message=True)
        return
    except ValueError as e:
        await matcher.finish(str(e), reply_message=True)
        return
    except Exception as e:
        log.warning(f'[b50_analysis] 拉取 B50 失败 qq={qq}: {type(e).__name__}: {e}')
        await matcher.finish(format_command_error(e), reply_message=True)
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
        analysis_text, token_usage = await generate_analysis(context, maiconfig, style)
    except Exception as e:
        await matcher.finish(f'分析生成失败：{e}', reply_message=True)
        return

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

    try:
        await prepare_render_cache(context, maiconfig.b50_assets_path)
        img = render_image(context, analysis_text, maiconfig.b50_assets_path)
    except Exception as e:
        await matcher.finish(f'制图失败：{e}', reply_message=True)
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
        token_usage=token_usage,
    )

    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    balance = break_db.get_balance(billing_qq)
    query_footer = take_break_charge_footer()
    footer_parts = []
    if query_footer:
        footer_parts.extend(query_footer)
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
    await matcher.finish(
        MessageSegment.image(buf) + MessageSegment.text(footer),
        reply_message=True,
    )
