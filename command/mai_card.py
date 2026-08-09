"""卡密相关指令：兑换、我的卡密、管理员发卡 / 查询 / 作废 / 统计。"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone, timedelta
from typing import List, Optional

from nonebot import on_command, on_message
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageEvent
from nonebot.matcher import Matcher
from nonebot.params import Arg, CommandArg

from ..config import log
from ..libraries.maimaidx_bot_admin import PLUGIN_ADMIN_ONLY
from ..libraries.maimaidx_card import (
    CARD_TYPE_BREAK,
    CARD_TYPE_DOUBLE,
    CARD_TYPE_FREEDOM,
    CARD_TYPE_LABELS,
    CARD_TYPES,
    CardError,
    card_manager,
    format_duration,
    format_expires,
    normalize_code,
    parse_duration,
    store_hint,
)
from ..libraries.maimaidx_platform import (
    billing_user_id,
    get_event_group_id,
    require_account_qqid,
    use_qq_mode,
)
from ..libraries.maimaidx_pending_session import finish_pending, session_key, track_event

_CST = timezone(timedelta(hours=8))
_AUTO_CODE_RE = re.compile(r'^([A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4})$')
_AUTO_REDEEM_COOLDOWN: dict[int, float] = {}
_AUTO_REDEEM_COOLDOWN_SECONDS = 3.0

_CARD_TYPE_ALIASES = {
    '1': CARD_TYPE_BREAK, 'break': CARD_TYPE_BREAK, 'b': CARD_TYPE_BREAK,
    'break卡': CARD_TYPE_BREAK, '余额': CARD_TYPE_BREAK,
    '2': CARD_TYPE_DOUBLE, 'double': CARD_TYPE_DOUBLE, 'double_break': CARD_TYPE_DOUBLE,
    '双倍': CARD_TYPE_DOUBLE, '双倍break': CARD_TYPE_DOUBLE, '双倍break卡': CARD_TYPE_DOUBLE,
    '3': CARD_TYPE_FREEDOM, 'freedom': CARD_TYPE_FREEDOM, 'f': CARD_TYPE_FREEDOM,
    '自由': CARD_TYPE_FREEDOM, 'freedom卡': CARD_TYPE_FREEDOM,
}


def _resolve_card_type(text: str) -> Optional[str]:
    key = str(text or '').strip().lower().replace(' ', '').replace('_', '')
    return _CARD_TYPE_ALIASES.get(key)


def _account_qqid(event: MessageEvent) -> int:
    if use_qq_mode(event):
        return int(require_account_qqid(event))
    return int(billing_user_id(event))


def _fmt_time(ts: float) -> str:
    if not ts:
        return '-'
    return datetime.fromtimestamp(ts, _CST).strftime('%Y-%m-%d %H:%M:%S')


def _format_redeem_result(result) -> str:
    if result.card_type == CARD_TYPE_BREAK:
        return (
            f'🎉 兑换成功！{result.label}\n'
            f'💰 获得 {result.granted} BREAK，当前余额 {result.balance} BREAK'
        )
    text = (
        f'🎉 兑换成功！{result.label}已生效\n'
        f'⏱️ 生效时长 {format_duration(result.granted)}\n'
        f'⏰ 到期时间 {_fmt_time(result.expires_at)}（剩余 {format_expires(result.expires_at)}）'
    )
    if result.card_type == CARD_TYPE_DOUBLE:
        text += '\n✨ 期间猜歌系列游戏猜对获得的 BREAK 翻倍'
    else:
        text += '\n🛡️ 期间触发指令不扣除 BREAK'
    return text


# ---------------------------------------------------------------------------
# 用户：兑换卡密
# ---------------------------------------------------------------------------

card_redeem = on_command(
    '兑换卡密',
    aliases={'卡密兑换', 'AWMC兑换', '兑换码', 'redeem'},
)
# 兑换/查询卡密不应被负债拦截或收取高负载附加费，否则用户无法自救。
setattr(card_redeem, '_maimaidx_debt_exempt', True)
setattr(card_redeem, '_maimaidx_busy_surcharge_exempt', True)


@card_redeem.handle()
async def _(matcher: Matcher, event: MessageEvent, message: Message = CommandArg()):
    code = message.extract_plain_text().strip()
    if code:
        matcher.set_arg('card_code', Message(code))
    else:
        track_event(session_key('card_redeem', event), event)


@card_redeem.got(
    'card_code',
    prompt='请输入要兑换的卡密（格式 XXXX-XXXX-XXXX），发送“取消”可退出。',
)
async def _(matcher: Matcher, event: MessageEvent, raw: Message = Arg('card_code')):
    pending_key = session_key('card_redeem', event)
    code = raw.extract_plain_text().strip()
    if code.lower() in {'取消', 'cancel', 'q', '退出'}:
        finish_pending(pending_key)
        await card_redeem.finish('已取消兑换。')
    normalized = normalize_code(code)
    if len(normalized.replace('-', '')) < 8:
        track_event(pending_key, event)
        await card_redeem.reject('卡密格式不正确，请重新输入（发送“取消”可退出）。')
    try:
        result = card_manager.redeem(
            _account_qqid(event),
            normalized,
            group_id=str(get_event_group_id(event) or ''),
            actor=event.get_user_id(),
        )
    except CardError as exc:
        hint = store_hint()
        text = f'❌ 兑换失败：{exc}'
        if hint:
            text += f'\n{hint}'
        finish_pending(pending_key)
        await card_redeem.finish(text, reply_message=True)
        return
    finish_pending(pending_key)
    await card_redeem.finish(_format_redeem_result(result), reply_message=True)


# ---------------------------------------------------------------------------
# 用户：我的卡密（当前生效加成）
# ---------------------------------------------------------------------------

my_card = on_command('我的卡密', aliases={'卡密状态', '我的加成', '卡密'})
setattr(my_card, '_maimaidx_debt_exempt', True)
setattr(my_card, '_maimaidx_busy_surcharge_exempt', True)


@my_card.handle()
async def _(event: MessageEvent):
    qqid = _account_qqid(event)
    d_active, d_remaining, d_expires = card_manager.double_break_info(qqid)
    f_active, f_remaining, f_expires = card_manager.freedom_info(qqid)
    lines = ['🎫 我的卡密状态']
    if d_active:
        lines.append(
            f'  · 双倍BREAK卡生效中，剩余 {format_duration(d_remaining)}'
            f'（到期 {_fmt_time(d_expires)}）'
        )
    else:
        lines.append('  · 双倍BREAK卡：未生效')
    if f_active:
        lines.append(
            f'  · FREEDOM卡生效中，剩余 {format_duration(f_remaining)}'
            f'（到期 {_fmt_time(f_expires)}）'
        )
    else:
        lines.append('  · FREEDOM卡：未生效')
    hint = store_hint()
    if hint:
        lines.append(hint)
    await my_card.finish('\n'.join(lines), reply_message=True)


# ---------------------------------------------------------------------------
# 管理员：创建卡密（交互式）
# ---------------------------------------------------------------------------

card_create = on_command('创建卡密', permission=PLUGIN_ADMIN_ONLY)


def _parse_create_args(parts: List[str]):
    if len(parts) < 2:
        return None
    card_type = _resolve_card_type(parts[0])
    if card_type is None:
        return None
    try:
        if card_type == CARD_TYPE_BREAK:
            value = int(parts[1])
        else:
            value = parse_duration(parts[1])
    except ValueError:
        return None
    quantity = 1
    note = ''
    rest = parts[2:]
    if rest and rest[0].isdigit():
        quantity = int(rest[0])
        rest = rest[1:]
    if rest:
        note = ' '.join(rest)
    return card_type, value, quantity, note


@card_create.handle()
async def _(matcher: Matcher, event: MessageEvent, message: Message = CommandArg()):
    parts = message.extract_plain_text().strip().split()
    parsed = _parse_create_args(parts) if parts else None
    if parsed:
        card_type, value, quantity, note = parsed
        try:
            result = card_manager.create_cards(
                card_type, value, quantity,
                created_by=event.get_user_id(), note=note,
            )
        except (CardError, ValueError) as exc:
            await card_create.finish(f'❌ 创建失败：{exc}', reply_message=True)
            return
        await card_create.finish(_format_create_result(result), reply_message=True)
    pending_key = session_key('card_create', event)
    track_event(pending_key, event)


@card_create.got(
    'card_type',
    prompt=(
        '请选择要创建的卡密类型（发送“取消”可退出）：\n'
        '1. BREAK 卡（直接增加 BREAK）\n'
        '2. 双倍 BREAK 卡（限时猜歌 BREAK 翻倍）\n'
        '3. FREEDOM 卡（限时免 BREAK 触发指令）'
    ),
)
async def _(matcher: Matcher, event: MessageEvent, raw: Message = Arg('card_type')):
    pending_key = session_key('card_create', event)
    text = raw.extract_plain_text().strip()
    if text.lower() in {'取消', 'cancel', 'q', '退出'}:
        finish_pending(pending_key)
        await card_create.finish('已取消创建卡密。')
    card_type = _resolve_card_type(text)
    if card_type is None:
        track_event(pending_key, event)
        await card_create.reject('类型无效，请输入 1 / 2 / 3，或发送“取消”退出。')
    matcher.state['card_type'] = card_type
    matcher.state['card_type_label'] = CARD_TYPE_LABELS[card_type]


@card_create.got(
    'card_value',
    prompt='请输入卡密面值：BREAK 卡填正整数数量；双倍/FREEDOM 卡填时长（如 7d、24h、30m，或纯秒数）。发送“取消”可退出。',
)
async def _(matcher: Matcher, event: MessageEvent, raw: Message = Arg('card_value')):
    pending_key = session_key('card_create', event)
    text = raw.extract_plain_text().strip()
    if text.lower() in {'取消', 'cancel', 'q', '退出'}:
        finish_pending(pending_key)
        await card_create.finish('已取消创建卡密。')
    card_type = matcher.state['card_type']
    try:
        if card_type == CARD_TYPE_BREAK:
            value = int(text)
            if value <= 0:
                raise ValueError
        else:
            value = parse_duration(text)
    except ValueError:
        track_event(pending_key, event)
        hint = '正整数' if card_type == CARD_TYPE_BREAK else '时长（如 7d、24h、30m）'
        await card_create.reject(f'请输入合法的{hint}，或发送“取消”退出。')
    matcher.state['card_value'] = value


@card_create.got(
    'card_quantity',
    prompt='请输入创建数量（1-500，默认 1），发送“取消”可退出。',
)
async def _(matcher: Matcher, event: MessageEvent, raw: Message = Arg('card_quantity')):
    pending_key = session_key('card_create', event)
    text = raw.extract_plain_text().strip()
    if text.lower() in {'取消', 'cancel', 'q', '退出'}:
        finish_pending(pending_key)
        await card_create.finish('已取消创建卡密。')
    quantity = 1
    if text:
        if not text.isdigit() or not 1 <= int(text) <= 500:
            track_event(pending_key, event)
            await card_create.reject('数量需为 1-500 的整数，或发送“取消”退出。')
        quantity = int(text)
    card_type = matcher.state['card_type']
    value = matcher.state['card_value']
    note = f"由 {event.get_user_id()} 交互式创建"
    try:
        result = card_manager.create_cards(
            card_type, value, quantity,
            created_by=event.get_user_id(), note=note,
        )
    except (CardError, ValueError) as exc:
        finish_pending(pending_key)
        await card_create.finish(f'❌ 创建失败：{exc}', reply_message=True)
        return
    finish_pending(pending_key)
    await card_create.finish(_format_create_result(result), reply_message=True)


def _format_create_result(result: dict) -> str:
    value = result['value']
    if result['card_type'] == CARD_TYPE_BREAK:
        value_text = f'{value} BREAK'
    else:
        value_text = format_duration(value)
    codes = result['codes']
    lines = [
        f"✅ 已创建 {result['quantity']} 张「{result['label']}」（面值 {value_text}）",
        f"批次号：{result['batch_id']}",
        '卡密列表：',
    ]
    lines.extend(codes)
    lines.append('（在群内发送「兑换卡密 XXXX-XXXX-XXXX」即可兑换）')
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# 管理员：查询卡密
# ---------------------------------------------------------------------------

card_query = on_command('查询卡密', permission=PLUGIN_ADMIN_ONLY, aliases={'卡密查询'})


@card_query.handle()
async def _(event: MessageEvent, message: Message = CommandArg()):
    key = message.extract_plain_text().strip()
    if not key:
        await card_query.finish(
            '用法：查询卡密 <卡密/批次号>\n示例：查询卡密 ABCD-EFGH-HIJK',
            reply_message=True,
        )
    normalized = normalize_code(key)
    card = card_manager.get_card(normalized)
    if card:
        await card_query.finish(_format_card_detail(card), reply_message=True)
    cards = card_manager.list_cards(batch_id=key, limit=50)
    if cards:
        lines = [f'批次 {key} 共 {len(cards)} 张（最多展示 50 张）：']
        for c in cards:
            status = {
                'unused': '未使用', 'redeemed': '已兑换', 'disabled': '已作废',
            }.get(c['status'], c['status'])
            lines.append(
                f"  · {c['code']} [{CARD_TYPE_LABELS.get(c['card_type'], c['card_type'])}]"
                f" {status}"
                + (f" 兑换者 {c['redeemed_by']} @ {_fmt_time(c['redeemed_at'])}"
                   if c['status'] == 'redeemed' else '')
            )
        await card_query.finish('\n'.join(lines), reply_message=True)
    await card_query.finish('❌ 未找到该卡密或批次。', reply_message=True)


def _format_card_detail(card: dict) -> str:
    status = {
        'unused': '未使用', 'redeemed': '已兑换', 'disabled': '已作废',
    }.get(card['status'], card['status'])
    if card['card_type'] == CARD_TYPE_BREAK:
        value_text = f"{card['value']} BREAK"
    else:
        value_text = format_duration(int(card['value']))
    lines = [
        f"卡密：{card['code']}",
        f"类型：{CARD_TYPE_LABELS.get(card['card_type'], card['card_type'])}",
        f"面值：{value_text}",
        f"状态：{status}",
        f"批次：{card['batch_id']}",
        f"备注：{card['note'] or '-'}",
        f"创建者：{card['created_by'] or '-'}",
        f"创建时间：{_fmt_time(card['created_at'])}",
    ]
    if card['status'] == 'redeemed':
        lines.append(f"兑换者：{card['redeemed_by']}")
        lines.append(f"兑换群：{card['redeemed_group'] or '-'}")
        lines.append(f"兑换时间：{_fmt_time(card['redeemed_at'])}")
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# 管理员：作废卡密
# ---------------------------------------------------------------------------

card_disable = on_command('作废卡密', permission=PLUGIN_ADMIN_ONLY, aliases={'禁用卡密'})


@card_disable.handle()
async def _(event: MessageEvent, message: Message = CommandArg()):
    code = normalize_code(message.extract_plain_text().strip())
    if not code:
        await card_disable.finish('用法：作废卡密 <卡密>', reply_message=True)
    try:
        card = card_manager.disable_card(code, actor=event.get_user_id())
    except CardError as exc:
        await card_disable.finish(f'❌ {exc}', reply_message=True)
    await card_disable.finish(
        f"✅ 已作废卡密 {card['code']}（{CARD_TYPE_LABELS.get(card['card_type'], card['card_type'])}）",
        reply_message=True,
    )


# ---------------------------------------------------------------------------
# 管理员：卡密列表 / 统计
# ---------------------------------------------------------------------------

card_list = on_command('卡密列表', permission=PLUGIN_ADMIN_ONLY)


@card_list.handle()
async def _(message: Message = CommandArg()):
    parts = message.extract_plain_text().strip().split()
    card_type = None
    status = None
    for p in parts:
        t = _resolve_card_type(p)
        if t:
            card_type = t
        elif p in {'unused', 'redeemed', 'disabled', '未使用', '已兑换', '已作废'}:
            status = {
                '未使用': 'unused', '已兑换': 'redeemed', '已作废': 'disabled',
            }.get(p, p)
    cards = card_manager.list_cards(card_type=card_type, status=status, limit=20)
    if not cards:
        await card_list.finish('没有符合条件的卡密。', reply_message=True)
    lines = [f'卡密列表（最近 {len(cards)} 张）：']
    for c in cards:
        st = {
            'unused': '未使用', 'redeemed': '已兑换', 'disabled': '已作废',
        }.get(c['status'], c['status'])
        lines.append(f"  · {c['code']} [{CARD_TYPE_LABELS.get(c['card_type'], c['card_type'])}] {st}")
    await card_list.finish('\n'.join(lines), reply_message=True)


card_stats = on_command('卡密统计', permission=PLUGIN_ADMIN_ONLY)


@card_stats.handle()
async def _():
    stats = card_manager.stats()
    lines = [f"🎫 卡密统计（共 {stats['total']} 张，生效中加成 {stats['active_effects']} 人）"]
    for card_type in CARD_TYPES:
        info = stats['by_type'].get(card_type, {'unused': 0, 'redeemed': 0, 'disabled': 0})
        lines.append(
            f"  · {CARD_TYPE_LABELS[card_type]}："
            f"未使用 {info['unused']} / 已兑换 {info['redeemed']} / 已作废 {info['disabled']}"
        )
    recent = card_manager.list_recent_redemptions(limit=5)
    if recent:
        lines.append('最近兑换：')
        for c in recent:
            lines.append(
                f"  · {c['code']} -> {c['redeemed_by']} @ {_fmt_time(c['redeemed_at'])}"
            )
    await card_stats.finish('\n'.join(lines), reply_message=True)


# ---------------------------------------------------------------------------
# 群内自动识别卡密：直接发送 XXXX-XXXX-XXXX 即自动兑换
# ---------------------------------------------------------------------------

auto_card_redeem = on_message(priority=99, block=False)
setattr(auto_card_redeem, '_maimaidx_debt_exempt', True)
setattr(auto_card_redeem, '_maimaidx_busy_surcharge_exempt', True)


@auto_card_redeem.handle()
async def _(event: GroupMessageEvent):
    text = event.get_plaintext().strip().upper()
    match = _AUTO_CODE_RE.match(text)
    if not match:
        return
    code = match.group(1)

    card = card_manager.get_card(code)
    if card is None or card.get('status') != 'unused':
        return

    try:
        qqid = _account_qqid(event)
    except Exception:
        return
    now = time.time()
    last = _AUTO_REDEEM_COOLDOWN.get(qqid, 0.0)
    if now - last < _AUTO_REDEEM_COOLDOWN_SECONDS:
        return
    _AUTO_REDEEM_COOLDOWN[qqid] = now

    try:
        result = card_manager.redeem(
            qqid,
            code,
            group_id=str(get_event_group_id(event) or ''),
            actor=event.get_user_id(),
        )
    except CardError as exc:
        hint = store_hint()
        text = f'❌ 兑换失败：{exc}'
        if hint:
            text += f'\n{hint}'
        await auto_card_redeem.finish(text, reply_message=True)
        return
    await auto_card_redeem.finish(_format_redeem_result(result), reply_message=True)
