from typing import Optional, Tuple

from nonebot import get_bots, on_command, require
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
)
from nonebot.exception import FinishedException
from nonebot.matcher import Matcher
from nonebot.params import Arg, CommandArg, Depends
from ..libraries.maimaidx_break import (
    DEFAULT_CONFIG,
    GAMBLE_ENTRY_COST,
    LOTTERY_PRIZES,
    LOTTERY_WEIGHTS,
    break_db,
    format_account_profile,
    format_account_profile_sections,
    render_account_profile_image,
    format_analysis_pricing_help,
    format_checkin_result,
    format_makeup_checkin_result,
    get_account_profile,
    normalize_billing_qqid,
)
from ..libraries.maimaidx_bot_admin import PLUGIN_ADMIN_ONLY
from ..libraries.maimaidx_error import QBindRequiredError
from ..libraries.maimaidx_platform import (
    billing_user_id,
    get_event_group_id,
    get_sender_display_name,
    parse_at_target_id,
    platform_user_id,
    plugin_finish,
    rank_text_image,
    require_account_qqid,
    resolve_group_bot,
    resolve_group_delivery_id,
    resolve_group_legacy_id,
    resolve_score_qqid,
    send_group_plain_text,
    use_qq_mode,
)
from ..libraries.maimaidx_group_rating import build_forward_node
from ..libraries.maimaidx_image_executor import run_image_cpu
from ..libraries.maimaidx_pending_session import finish_pending, session_key, track_event
from ..config import log, maiconfig
from .mai_agreement import agreement_prompt, has_user_agreed

require('nonebot_plugin_apscheduler')
from nonebot_plugin_apscheduler import scheduler  # noqa: E402

awmc_checkin = on_command('AWMC签到', aliases={'签到', 'awmc签到'})
awmc_makeup_checkin = on_command(
    'AWMC补签', aliases={'补签', 'awmc补签', '签到补签'}
)
for _checkin_matcher in (awmc_checkin, awmc_makeup_checkin):
    setattr(_checkin_matcher, '_maimaidx_serial_user_operation', True)
# 补签本身已有固定阶梯价格，不再叠加高负载附加费。
setattr(awmc_makeup_checkin, '_maimaidx_busy_surcharge_exempt', True)
my_awmc = on_command(
    '我的AWMC', aliases={'我的awmc', 'AWMC状态', 'awmc状态', '我的账号'}
)
break_economy = on_command(
    'BREAK统计', aliases={'BREAK收支', '全局BREAK统计', 'AWMC经济', 'AWMC收支'}
)
awmc_admin_set = on_command('设置BREAK', permission=PLUGIN_ADMIN_ONLY)
awmc_admin_add = on_command('增减BREAK', permission=PLUGIN_ADMIN_ONLY)
awmc_admin_config = on_command('BREAK配置', permission=PLUGIN_ADMIN_ONLY)
awmc_admin_view = on_command('查看AWMC', permission=PLUGIN_ADMIN_ONLY)
ticket_stats_admin = on_command(
    '发票统计', aliases={'ticket统计', 'returnCode统计'}, permission=PLUGIN_ADMIN_ONLY
)
awmc_help = on_command('AWMC帮助', aliases={'BREAK帮助'})
break_transfer = on_command('转账BREAK', aliases={'BREAK转账'})
break_lottery = on_command('BREAK抽奖', aliases={'抽奖BREAK'})
break_red_packet_send = on_command(
    '发红包', aliases={'发BREAK红包', 'BREAK红包', '发break红包'}
)
break_red_packet_claim = on_command(
    '抢红包', aliases={'领红包', '抢BREAK红包', '抢break红包'}
)
break_red_packet_status = on_command(
    '红包状态', aliases={'红包记录', '查看红包'}
)
break_red_packet_cancel = on_command(
    '收回红包', aliases={'重置红包'}
)
break_gamble_all = on_command(
    '倾家荡产', aliases={'梭哈'}
)
break_gamble_pool = on_command(
    '抽奖池', aliases={'今日贡献榜', '贡献榜'}
)
break_gamble_claim = on_command(
    '领取福利', aliases={'领福利'}
)
break_gamble_leaderboard = on_command(
    '贡献总榜', aliases={'首富榜', '总贡献榜'}
)

# Help is safe to use before account binding; it explains how to send qbind.
setattr(awmc_help, '_maimaidx_qbind_exempt', True)

for _debt_exempt_matcher in (
    awmc_checkin,
    my_awmc,
    break_economy,
    awmc_help,
    break_red_packet_claim,
    break_red_packet_status,
):
    setattr(_debt_exempt_matcher, "_maimaidx_debt_exempt", True)

_LOTTERY_PRIZE_LINES = ''.join(
    f'· {prize} BREAK：{weight}%\n'
    for prize, weight in zip(LOTTERY_PRIZES, LOTTERY_WEIGHTS)
)
LOTTERY_HELP = (
    '【BREAK 抽奖】\n'
    '这是 Bot 内部的群互动消耗玩法，BREAK 不具有现金价值。\n'
    '默认每次消耗 2 BREAK，奖池为：\n'
    + _LOTTERY_PRIZE_LINES
    + '用法：BREAK抽奖 [次数]\n'
    '例："BREAK抽奖"抽 1 次，"BREAK抽奖 5"连抽 5 次。\n'
    '非空奖概率 65%，单抽期望返还 1.6 BREAK；最多 10 连抽，长期仍为净消耗。'
)

# GAMBLE_ALL_HELP = (
#     '【倾家荡产】\n'
#     '梭哈你的全部 BREAK，一抽定生死！\n\n'
#     '用法：倾家荡产 [模式]\n'
#     '入场费（不退还）：\n'
#     '  · 标准（默认）— 2 BREAK\n'
#     '  · 刺激 — 3 BREAK\n'
#     '  · 高风险 — 5 BREAK\n\n'
#     '倍率表：\n'
#     '  标准：70% 谢谢参与 / 15% 返本 / 8% 2x / 4% 5x / 2% 10x / 0.8% 30x / 0.2% 50x\n'
#     '  刺激：78% 谢谢参与 / 10% 2x / 6% 5x / 4% 20x / 1.5% 50x / 0.5% 100x\n'
#     '  高风险：82% 谢谢参与 / 8% 5x / 6% 20x / 3% 50x / 1% 100x\n\n'
#     '例："倾家荡产" 或 "倾家荡产 刺激"\n'
#     'BREAK 是 Bot 内部积分，不具有现金价值。'
# )

def get_at_qq(message: MessageEvent) -> Optional[int]:
    target = parse_at_target_id(message)
    if target is None:
        return None
    # Official QQ targets must also have a qbind mapping; never create a hash
    # ledger row for an opaque openid supplied in an @ segment.
    return normalize_billing_qqid(target)


async def _require_break_agreement(matcher, event: MessageEvent) -> None:
    if not has_user_agreed(event):
        await matcher.finish(agreement_prompt(), reply_message=True)


@awmc_help.handle()
async def _():
    text = (
        '【AWMC BREAK 系统】\n'
        '· AWMC签到 — 每日签到获取 BREAK（基础 1~2，连签额外奖励第 5 天起封顶 3）\n'
        '· AWMC补签 — 补昨天，每月最多 3 次，依次消耗 30/60/90 BREAK\n'
        '· 今日舞萌 — 人品值四舍五入后 ÷10，每日领取一次 BREAK\n'
        '· 猜歌 — 每次猜对奖励 1 BREAK，无每日上限\n'
        '· 转账BREAK @用户 数量 — 转给其他用户\n'
        '· BREAK抽奖 [1-10] — 每次默认消耗 2 BREAK，发送"BREAK抽奖 帮助"看奖池\n'
        # '· 倾家荡产 [模式] — 梭哈全部 BREAK，发送"倾家荡产 帮助"看模式说明\n'
        '· 抽奖池 — 查看今日贡献榜和可领取福利\n'
        '· 领取福利 — 领取今日抽奖池福利（需当日有贡献）\n'
        '· 贡献总榜 — 查看历史贡献总榜（首富榜）\n'
        '· 发红包 [总额] [份数] — 群内发送 BREAK 手气红包，也可按提示逐步输入\n'
        '· 抢红包 — 领取本群当前红包；红包状态 — 查看领取明细\n'
        '· 收回红包 — 发红包满 90 秒可主动收回，未领余额退回发送者（重置红包 同义）\n'
        '· 我的AWMC — 查看账号状态、使用统计；群内自动附带猜歌数据图\n'
        '· BREAK统计 — 查看所有用户今日、近7天、近30天的 BREAK 收入与支出\n'
        '· 查分指令 — 每日首次实际请求查分器 API 免费，之后每次扣 1 BREAK（缓存命中另计）\n'
        '· 查歌/谱面详情 — 成功出图每次扣 1 BREAK（与查分共享每日首免；同次已因查分扣过则不重复）\n'
        '· 兑换卡密 <卡密> — 兑换 BREAK 卡 / 双倍BREAK卡 / FREEDOM 卡；我的卡密 — 查看生效中加成\n'
        + format_analysis_pricing_help()
        + '· BREAK 不足时请先签到，也可在卡密商店购买卡密\n\n'
        '【签到加成（加算）】\n'
        '· 指定群 1072033605、993795066 +25%\n'
        '· 指定群 669800745 签到总奖励 ×2\n'
        '· 周四 +50%\n'
        '· 群内当日首签 +50%\n'
        '· 开启数据存储 +50%（发送「开启存储数据」；需保持开启跨天，频繁开关不重复发）\n'
        '· 连续签到额外奖励（第 5 天起封顶 3，群倍数不放大连签奖励）'
    )
    await awmc_help.finish(text, reply_message=True)


def _format_break_economy_period(label: str, stats: dict) -> str:
    gained = int(stats.get('gained', 0))
    spent = int(stats.get('spent', 0))
    net = gained - spent
    net_text = f'+{net}' if net > 0 else str(net)
    return (
        f'{label}（{stats["since"]} 至 {stats["until"]}）\n'
        f'  收入：{gained} BREAK　支出：{spent} BREAK　净变化：{net_text} BREAK'
    )


@break_economy.handle()
async def _():
    periods = (
        ('今日', break_db.economy_totals(1)),
        ('近 7 天累计', break_db.economy_totals(7)),
        ('近 30 天累计', break_db.economy_totals(30)),
    )
    lines = [
        '【BREAK 全体收支】',
        '统计范围：所有用户；按自然日累计，不含当前余额。',
        '',
    ]
    lines.extend(_format_break_economy_period(label, stats) for label, stats in periods)
    await break_economy.finish('\n'.join(lines), reply_message=True)


@break_transfer.handle()
async def _(
    event: MessageEvent,
    message: Message = CommandArg(),
    user_id: Optional[int] = Depends(get_at_qq),
):
    await _require_break_agreement(break_transfer, event)
    parts = message.extract_plain_text().strip().split()
    target = user_id
    if target is None and parts and parts[0].isdigit():
        target = int(parts.pop(0))
    amount = int(parts[-1]) if parts and parts[-1].isdigit() else 0
    if target is None or amount <= 0:
        await break_transfer.finish('用法：转账BREAK @用户 数量', reply_message=True)
    try:
        result = break_db.transfer(int(billing_user_id(event)), target, amount)
    except Exception as exc:
        await break_transfer.finish(f'转账失败：{exc}', reply_message=True)
    fee_text = f'（手续费 {result.fee}）' if result.fee else ''
    await break_transfer.finish(
        f'转账成功：{result.amount} BREAK {fee_text}\n当前余额：{result.sender_balance}',
        reply_message=True,
    )


@break_lottery.handle()
async def _(event: MessageEvent, message: Message = CommandArg()):
    raw = message.extract_plain_text().strip() or '1'
    if raw.lower() in {'帮助', '说明', 'help', '?'}:
        await break_lottery.finish(LOTTERY_HELP, reply_message=True)
    await _require_break_agreement(break_lottery, event)
    if not raw.isdigit() or not 1 <= int(raw) <= 10:
        await break_lottery.finish('用法：BREAK抽奖 [1-10]', reply_message=True)
    try:
        result = break_db.lottery(int(billing_user_id(event)), int(raw))
    except Exception as exc:
        await break_lottery.finish(f'抽奖失败：{exc}', reply_message=True)
    await break_lottery.finish(
        f'抽奖 {result.count} 次\n消耗：{result.cost} BREAK\n'
        f'获得：{result.prize} BREAK\n当前余额：{result.balance}',
        reply_message=True,
    )


def _red_packet_group_id(event: MessageEvent) -> Optional[int]:
    if isinstance(event, GroupMessageEvent):
        gid = get_event_group_id(event)
        if gid is None:
            return None
        if isinstance(gid, str) and not gid.isdigit():
            return resolve_group_legacy_id(gid)
        return int(gid)
    return None


@break_red_packet_send.handle()
async def _(
    matcher: Matcher,
    event: MessageEvent,
    message: Message = CommandArg(),
):
    await _require_break_agreement(break_red_packet_send, event)
    if _red_packet_group_id(event) is None:
        await break_red_packet_send.finish('BREAK 红包只能在群聊中发送。')
    raw = message.extract_plain_text().strip()
    if raw.lower() in {'帮助', '说明', 'help', '?'}:
        await break_red_packet_send.finish(
            '【BREAK 手气红包】\n'
            '用法：发红包 总额 份数\n'
            '例：发红包 20 5\n'
            '也可以只发送“发红包”，Bot 会依次询问总额和份数。\n'
            '群友发送“抢红包”领取，每人限领一次；10 分钟后未领金额自动退回。\n'
            'BREAK 是 Bot 内部积分，不具有现金价值。',
            reply_message=True,
        )
    parts = raw.split()
    if len(parts) > 2:
        await break_red_packet_send.finish(
            '参数太多啦。用法：发红包 总额 份数', reply_message=True
        )
    pending_key = session_key('break_red_packet', event)
    track_event(pending_key, event)
    if parts:
        matcher.set_arg('red_packet_total', Message(parts[0]))
    if len(parts) == 2:
        matcher.set_arg('red_packet_count', Message(parts[1]))


@break_red_packet_send.got(
    'red_packet_total', prompt='请输入红包总额（正整数 BREAK），发送“取消”可退出。'
)
async def _(
    matcher: Matcher,
    event: MessageEvent,
    total_message: Message = Arg('red_packet_total'),
):
    pending_key = session_key('break_red_packet', event)
    raw = total_message.extract_plain_text().strip()
    if raw.lower() in {'取消', 'cancel', 'q', '退出'}:
        finish_pending(pending_key)
        await break_red_packet_send.finish('已取消发送红包。')
    if not raw.isdigit() or int(raw) <= 0:
        track_event(pending_key, event)
        await break_red_packet_send.reject(
            '红包总额必须是正整数，请重新输入；发送“取消”可退出。'
        )
    matcher.state['red_packet_total_value'] = int(raw)


@break_red_packet_send.got(
    'red_packet_count', prompt='请输入红包份数（正整数），发送“取消”可退出。'
)
async def _(
    matcher: Matcher,
    event: MessageEvent,
    count_message: Message = Arg('red_packet_count'),
):
    pending_key = session_key('break_red_packet', event)
    raw = count_message.extract_plain_text().strip()
    if raw.lower() in {'取消', 'cancel', 'q', '退出'}:
        finish_pending(pending_key)
        await break_red_packet_send.finish('已取消发送红包。')
    if not raw.isdigit() or int(raw) <= 0:
        track_event(pending_key, event)
        await break_red_packet_send.reject(
            '红包份数必须是正整数，请重新输入；发送“取消”可退出。'
        )
    group_id = _red_packet_group_id(event)
    if group_id is None:
        finish_pending(pending_key)
        await break_red_packet_send.finish('BREAK 红包只能在群聊中发送。')
    try:
        result = break_db.create_red_packet(
            int(billing_user_id(event)),
            group_id,
            int(matcher.state['red_packet_total_value']),
            int(raw),
        )
    except Exception as exc:
        finish_pending(pending_key)
        await break_red_packet_send.finish(f'红包发送失败：{exc}', reply_message=True)
    finish_pending(pending_key)
    expire_minutes = max(
        1,
        int(float(break_db.get_config('red_packet_expire_minutes', '10'))),
    )
    await break_red_packet_send.finish(
        '🧧 BREAK 手气红包来啦！\n'
        f'总额：{result.total_amount} BREAK · 共 {result.total_count} 份\n'
        f'红包编号：{result.packet_id}\n'
        '发送“抢红包”即可领取，每人限领一次。\n'
        f'{expire_minutes} 分钟后未领取的余额会自动退回。',
        reply_message=True,
    )


@break_red_packet_claim.handle()
async def _(event: MessageEvent):
    await _require_break_agreement(break_red_packet_claim, event)
    group_id = _red_packet_group_id(event)
    if group_id is None:
        await break_red_packet_claim.finish('BREAK 红包只能在群聊中领取。')
    try:
        result = break_db.claim_red_packet(int(billing_user_id(event)), group_id)
    except Exception as exc:
        await break_red_packet_claim.finish(f'领取失败：{exc}', reply_message=True)
    tail = '\n🎉 红包已经被领完啦！' if result.completed else (
        f'\n剩余 {result.remaining_count} 份，共 {result.remaining_amount} BREAK。'
    )
    await break_red_packet_claim.finish(
        f'🧧 领取成功：{result.amount} BREAK\n'
        f'当前余额：{result.recipient_balance} BREAK{tail}',
        reply_message=True,
    )


@break_red_packet_status.handle()
async def _(event: MessageEvent):
    group_id = _red_packet_group_id(event)
    if group_id is None:
        await break_red_packet_status.finish('红包状态只能在群聊中查看。')
    status = break_db.get_red_packet_status(group_id)
    if status is None:
        await break_red_packet_status.finish('本群还没有红包记录。', reply_message=True)
    labels = {'active': '领取中', 'completed': '已领完', 'expired': '已过期'}
    claim_lines = [f'· {qqid}：{amount} BREAK' for qqid, amount in status.claims]
    detail = '\n'.join(claim_lines) if claim_lines else '· 暂无人领取'
    await break_red_packet_status.finish(
        f'【红包 {status.packet_id} · {labels.get(status.status, status.status)}】\n'
        f'发送者：{status.sender_qqid}\n'
        f'总额 {status.total_amount} BREAK · {status.total_count} 份\n'
        f'剩余 {status.remaining_amount} BREAK · {status.remaining_count} 份\n'
        f'领取明细：\n{detail}',
        reply_message=True,
    )


@break_red_packet_cancel.handle()
async def _(event: MessageEvent):
    await _require_break_agreement(break_red_packet_cancel, event)
    group_id = _red_packet_group_id(event)
    if group_id is None:
        await break_red_packet_cancel.finish('BREAK 红包只能在群聊中收回。')
    try:
        result = break_db.cancel_red_packet(int(billing_user_id(event)), group_id)
    except Exception as exc:
        await break_red_packet_cancel.finish(f'收回失败：{exc}', reply_message=True)
    await break_red_packet_cancel.finish(
        f'🧧 红包 {result.packet_id} 已收回，'
        f'剩余 {result.refund} BREAK 已退回发送者。',
        reply_message=True,
    )


@scheduler.scheduled_job(
    'interval', minutes=1, id='break_red_packet_expiry'
)
async def _expire_break_red_packets() -> None:
    refunds = break_db.expire_red_packets()
    if not refunds:
        return
    bots = get_bots()
    for item in refunds:
        log.info(
            f'[BREAK红包] {item.packet_id} 已过期，退回 {item.refund} BREAK '
            f'to={item.sender_qqid}'
        )
        delivery_id = resolve_group_delivery_id(item.group_id)
        bot = resolve_group_bot(item.group_id, bots)
        if bot is None:
            continue
        try:
            await send_group_plain_text(
                bot,
                delivery_id,
                f'🧧 红包 {item.packet_id} 已过期，'
                f'剩余 {item.refund} BREAK 已退回发送者。',
            )
        except Exception as exc:
            log.warning(
                f'[BREAK红包] 过期通知发送失败：{type(exc).__name__}: {exc}'
            )


async def _try_guess_stats_for_awmc(
    event: MessageEvent,
) -> Optional[Tuple[str, str]]:
    """已弃用：我的AWMC 不再附带猜歌数据图。保留桩函数兼容旧测试 monkeypatch。"""
    return None


def _render_awmc_overview(profile, *, display_name: str):
    """同步生成账号概览图片；任何渲染失败都回退到文本图片，绝不抛异常。"""
    try:
        image_seg = render_account_profile_image(
            profile, user_name=display_name
        )
        if image_seg is not None:
            return image_seg
    except Exception as exc:
        log.warning(
            f'[BREAK] 我的AWMC卡片渲染失败，回退文本：{type(exc).__name__}: {exc}'
        )
    try:
        return rank_text_image(format_account_profile(profile))
    except Exception as exc:
        log.warning(
            f'[BREAK] 我的AWMC文本图渲染失败：{type(exc).__name__}: {exc}'
        )
        return None

def _account_qqid(event: MessageEvent) -> int:
    """签到/账号类：官方 QQ 必须 qbind；OneBot 直接用消息 QQ。"""
    if use_qq_mode(event):
        return int(require_account_qqid(event))
    return int(billing_user_id(event))


def _storage_qqids_for_event(event: MessageEvent, account_qqid: int) -> list[int]:
    seen: set[int] = set()
    out: list[int] = []
    candidates = [account_qqid, billing_user_id(event)]
    try:
        candidates.append(resolve_score_qqid(event))
    except Exception:
        pass
    for raw in candidates:
        try:
            qid = int(raw)
        except (TypeError, ValueError):
            continue
        if not qid or qid in seen:
            continue
        seen.add(qid)
        out.append(qid)
    return out


def _storage_status_for_event(
    event: MessageEvent, account_qqid: int
) -> tuple[bool, bool]:
    """返回 (当前已开启, 签到是否可享存储加成)。

    加成需「开启并保持到跨天」，防止当天开关刷奖励。
    """
    from ..libraries.maimaidx_data_storage import data_storage

    enabled = False
    eligible = False
    for qid in _storage_qqids_for_event(event, account_qqid):
        if data_storage.is_enabled(qid):
            enabled = True
        if data_storage.storage_bonus_eligible_for_checkin(qid):
            eligible = True
    return enabled, eligible


@awmc_checkin.handle()
async def _(event: MessageEvent):
    try:
        group_id = _red_packet_group_id(event)
        qqid = _account_qqid(event)
    except QBindRequiredError as exc:
        await awmc_checkin.finish(str(exc), reply_message=True)
        return
    storage_on, storage_eligible = _storage_status_for_event(event, qqid)
    result = break_db.checkin(
        qqid,
        group_id,
        storage_enabled=storage_on,
        storage_bonus_eligible=storage_eligible,
    )
    text = format_checkin_result(result)
    if storage_on and not storage_eligible:
        text += break_db.format_storage_pending_tip()
    await awmc_checkin.finish(text, reply_message=True)


@awmc_makeup_checkin.handle()
async def _(event: MessageEvent):
    await _require_break_agreement(awmc_makeup_checkin, event)
    try:
        qqid = _account_qqid(event)
    except QBindRequiredError as exc:
        await awmc_makeup_checkin.finish(str(exc), reply_message=True)
        return
    try:
        result = break_db.makeup_yesterday(qqid)
    except Exception as exc:
        await awmc_makeup_checkin.finish(f'补签失败：{exc}', reply_message=True)
    await awmc_makeup_checkin.finish(
        format_makeup_checkin_result(result), reply_message=True
    )


@my_awmc.handle()
async def _(bot: Bot, event: MessageEvent):
    try:
        qqid = _account_qqid(event)
    except QBindRequiredError as exc:
        await plugin_finish(my_awmc, str(exc), event=event, reply_message=True)
        return
    profile = None
    try:
        # 官方 QQ 与 OneBot 统一直接发送账号卡片图片，不再使用合并转发
        # （build_forward_node 会把 MessageSegment 强转成字符串，无法发出图片）。
        display_name = get_sender_display_name(event) or 'Milk'
        # 资料读取与 Pillow 绘图都放到图片线程池，避免阻塞事件循环导致“无响应”。
        profile = await run_image_cpu(get_account_profile, qqid)
        image_seg = await run_image_cpu(
            _render_awmc_overview, profile, display_name=display_name
        )
        # 官方 QQ 群需 publish_qq_image 才会把图片发布并以 Markdown 图显示，
        # 与 B50 / 排行榜成绩图走同一发送路径。
        if image_seg is None:
            await plugin_finish(
                my_awmc,
                format_account_profile(profile),
                event=event,
                reply_message=True,
            )
            return
        await plugin_finish(
            my_awmc,
            image_seg,
            event=event,
            reply_message=True,
            publish_qq_image=True,
        )
    except FinishedException:
        return
    except Exception as exc:
        log.warning(
            f'[BREAK] 我的AWMC概览生成/发送失败，回退文本：'
            f'{type(exc).__name__}: {exc}'
        )
        try:
            fallback = format_account_profile(profile)
        except Exception:
            fallback = '账号概览暂时无法生成，请稍后再试。'
        try:
            await plugin_finish(
                my_awmc,
                fallback,
                event=event,
                reply_message=True,
            )
        except FinishedException:
            return


@awmc_admin_view.handle()
async def _(
    event: MessageEvent,
    message: Message = CommandArg(),
    user_id: Optional[int] = Depends(get_at_qq),
):
    target = user_id
    if target is None:
        text = message.extract_plain_text().strip()
        if text.isdigit():
            target = int(text)
    if target is None:
        await awmc_admin_view.finish('请 @用户 或提供 QQ 号', reply_message=True)
        return
    profile = get_account_profile(target)
    image_seg = render_account_profile_image(
        profile, title=f'AWMC 账号 {target}', user_name=str(target)
    )
    await awmc_admin_view.finish(
        image_seg if image_seg is not None
        else format_account_profile(profile, title=f'AWMC 账号 {target}'),
        reply_message=True,
    )


@ticket_stats_admin.handle()
async def _(message: Message = CommandArg()):
    from ..libraries.maimaidx_account_db import account_db

    raw = message.extract_plain_text().strip()
    days: Optional[int] = None
    if raw:
        try:
            days = max(1, int(raw))
        except ValueError:
            await ticket_stats_admin.finish(
                '用法：发票统计 [天数]，例如「发票统计 7」；不填为全部历史',
                reply_message=True,
            )
            return
    stats = account_db.get_ticket_stats(days=days)
    title = f'全局发票统计（近 {days} 天）' if days else '全局发票统计（全部）'
    await ticket_stats_admin.finish(
        account_db.format_ticket_stats(stats, title=title),
        reply_message=True,
    )


@awmc_admin_set.handle()
async def _(
    message: Message = CommandArg(),
    user_id: Optional[int] = Depends(get_at_qq),
):
    parts = message.extract_plain_text().strip().split()
    target = user_id
    amount: Optional[int] = None
    if target is None and parts and parts[0].isdigit():
        target = int(parts[0])
        parts = parts[1:]
    if parts and parts[-1].lstrip('-').isdigit():
        amount = int(parts[-1])
    if target is None or amount is None:
        await awmc_admin_set.finish(
            '用法：设置BREAK @用户 数量\n示例：设置BREAK @某人 100',
            reply_message=True,
        )
        return
    balance = break_db.admin_set_balance(target, amount)
    await awmc_admin_set.finish(f'已将 {target} 的 BREAK 设为 {balance}', reply_message=True)


@awmc_admin_add.handle()
async def _(
    event: MessageEvent,
    message: Message = CommandArg(),
    user_id: Optional[int] = Depends(get_at_qq),
):
    parts = message.extract_plain_text().strip().split()
    target = user_id
    delta: Optional[int] = None
    if target is None and parts and parts[0].lstrip('-').isdigit():
        target = int(parts[0])
        parts = parts[1:]
    if parts:
        raw = parts[-1]
        if raw.lstrip('-+').isdigit() or (raw.startswith(('+', '-')) and raw[1:].isdigit()):
            delta = int(raw)
    if target is None or delta is None:
        await awmc_admin_add.finish(
            '用法：增减BREAK @用户 ±N\n示例：增减BREAK @某人 +10',
            reply_message=True,
        )
        return
    balance = break_db.add_balance(target, delta, 'admin_add', meta={'by': event.get_user_id()})
    await awmc_admin_add.finish(
        f'已为 {target} {"增加" if delta >= 0 else "减少"} {abs(delta)} BREAK，当前 {balance}',
        reply_message=True,
    )


@awmc_admin_config.handle()
async def _(message: Message = CommandArg()):
    parts = message.extract_plain_text().strip().split(maxsplit=1)
    if len(parts) < 2:
        lines = ['当前 BREAK 配置：'] + [
            f'  · {k} = {break_db.get_config(k, v)}' for k, v in DEFAULT_CONFIG.items()
        ]
        await awmc_admin_config.finish('\n'.join(lines), reply_message=True)
        return
    key, value = parts[0].strip(), parts[1].strip()
    break_db.set_config(key, value)
    await awmc_admin_config.finish(f'已设置 {key} = {value}', reply_message=True)


# @break_gamble_all.handle()
# async def _(matcher: Matcher, event: MessageEvent, message: Message = CommandArg()):
#     await _require_break_agreement(break_gamble_all, event)
#
#     raw = message.extract_plain_text().strip()
#     if raw.lower() in {'帮助', '说明', 'help', '?'}:
#         await break_gamble_all.finish(GAMBLE_ALL_HELP, reply_message=True)
#
#     # 解析模式
#     mode = '标准'
#     if '刺激' in raw:
#         mode = '刺激'
#     elif '高风险' in raw:
#         mode = '高风险'
#
#     entry_cost = GAMBLE_ENTRY_COST[mode]
#
#     # 获取用户余额
#     qqid = int(billing_user_id(event))
#     balance = break_db.get_balance(qqid)
#     if balance < entry_cost:
#         await break_gamble_all.finish(
#             f'{mode}模式入场费 {entry_cost} BREAK，余额不足！\n'
#             f'当前余额：{balance} BREAK', reply_message=True)
#
#     # 二次确认
#     pending_key = session_key('break_gamble_all', event)
#     track_event(pending_key, event)
#     matcher.state['gamble_mode'] = mode
#     matcher.state['gamble_balance'] = balance
#
#     prompt = (
#         f'🎰 倾家荡产 - {mode}模式\n'
#         f'入场费：{entry_cost} BREAK\n'
#         f'当前余额：{balance} BREAK\n\n'
#         f'入场后梭哈剩余 {balance - entry_cost} BREAK\n'
#         f'确定要入场吗？\n'
#         f'发送"确认"开始，"取消"退出。'
#     )
#     await break_gamble_all.send(prompt, reply_message=True)
#
#
# @break_gamble_all.got('confirm')
# async def _(matcher: Matcher, event: MessageEvent):
#     pending_key = session_key('break_gamble_all', event)
#     raw = str(matcher.state['confirm']).strip()
#
#     if raw.lower() in {'取消', 'cancel', 'q', '退出', 'no', 'n'}:
#         finish_pending(pending_key)
#         await break_gamble_all.finish('已取消倾家荡产。', reply_message=True)
#
#     if raw.lower() not in {'确认', 'confirm', 'y', 'yes', '是', '冲', '梭哈'}:
#         finish_pending(pending_key)
#         await break_gamble_all.finish('已取消倾家荡产。', reply_message=True)
#
#     mode = matcher.state['gamble_mode']
#     qqid = int(billing_user_id(event))
#     entry_cost = GAMBLE_ENTRY_COST[mode]
#
#     # 扣入场费
#     break_db.try_consume(qqid, entry_cost, 'gamble_entry_fee')
#
#     try:
#         result = break_db.gamble_all(qqid, mode)
#     except Exception as exc:
#         finish_pending(pending_key)
#         await break_gamble_all.finish(f'抽奖失败：{exc}', reply_message=True)
#
#     finish_pending(pending_key)
#
#     # 构建结果文本
#     if result.multiplier == 0:
#         text = (
#             f'🎰 倾家荡产 - {result.mode}模式\n'
#             f'入场费：-{entry_cost} BREAK\n'
#             f'💫 谢谢参与！-{result.balance_before} BREAK\n'
#             f'当前余额：{result.balance_after} BREAK\n'
#             f'呜呜呜~下次再接再厉！'
#         )
#     else:
#         profit = result.win_amount - result.balance_before
#         text = (
#             f'🎰 倾家荡产 - {result.mode}模式\n'
#             f'入场费：-{entry_cost} BREAK\n'
#             f'🎉 恭喜！中了 {result.multiplier}x 倍率！\n'
#             f'赢得：{result.win_amount} BREAK（+{profit}）\n'
#             f'当前余额：{result.balance_after} BREAK'
#         )
#
#         # 大奖建议发红包
#         if profit >= 100:
#             text += (
#                 '\n\n喵呜~！恭喜您中大奖啦！\n'
#                 '要不要发个红包庆祝一下呢~'
#             )
#
#     await break_gamble_all.finish(text, reply_message=True)
#
#
@break_gamble_pool.handle()
async def _(event: MessageEvent):
    await _require_break_agreement(break_gamble_pool, event)
    status = break_db.get_gamble_pool_status()

    if status.total_pool == 0:
        await plugin_finish(
            break_gamble_pool,
            rank_text_image(
                '🎰 今日抽奖池\n'
                '━━━━━━━━━━━━━━\n'
                '还没有人贡献呢~\n'
                '参与 BREAK 抽奖即可贡献！'
            ),
            event=event,
            reply_message=True,
        )
        return

    # 构建贡献榜
    lines = [
        '🎰 今日抽奖池',
        '━━━━━━━━━━━━━━',
        f'💰 今日池总额：{status.total_pool} BREAK',
        f'🎁 剩余可分配福利：{status.distributable} BREAK',
        '',
        '📊 今日贡献榜（前5）',
    ]

    for i, contributor in enumerate(status.contributors, 1):
        medal = ['🥇', '🥈', '🥉', '4️⃣', '5️⃣'][i - 1]
        lines.append(f'{medal} 用户 {contributor.qqid} — {contributor.amount} BREAK')

    lines.extend([
        '',
        '感谢他们为 AWMC 做出的贡献~',
        '',
        '💡 发送"领取福利"可领取今日福利！',
    ])

    await plugin_finish(
        break_gamble_pool,
        rank_text_image('\n'.join(lines)),
        event=event,
        reply_message=True,
    )


@break_gamble_claim.handle()
async def _(event: MessageEvent):
    await _require_break_agreement(break_gamble_claim, event)
    qqid = int(billing_user_id(event))

    try:
        reward, balance = break_db.claim_gamble_pool_reward(qqid)
    except Exception as exc:
        await break_gamble_claim.finish(f'领取失败：{exc}', reply_message=True)

    await break_gamble_claim.finish(
        '🎁 今日福利领取成功！\n'
        f'━━━━━━━━━━━━━━\n'
        f'💰 获得：{reward} BREAK\n'
        f'💳 当前余额：{balance} BREAK\n'
        '━━━━━━━━━━━━━━\n'
        '感谢今日贡献榜大佬们的赞助喵呜~',
        reply_message=True,
    )


@break_gamble_leaderboard.handle()
async def _(event: MessageEvent):
    await _require_break_agreement(break_gamble_leaderboard, event)
    leaderboard = break_db.get_gamble_pool_leaderboard(limit=10)

    if not leaderboard:
        await plugin_finish(
            break_gamble_leaderboard,
            rank_text_image(
                '🏆 贡献总榜（首富榜）\n'
                '━━━━━━━━━━━━━━\n'
                '还没有人贡献过呢~\n'
                '参与 BREAK 抽奖即可贡献！'
            ),
            event=event,
            reply_message=True,
        )
        return

    # 构建贡献榜
    lines = [
        '🏆 贡献总榜（首富榜）',
        '━━━━━━━━━━━━━━',
        '',
    ]

    medals = ['🥇', '🥈', '🥉'] + [f'{i}️⃣' for i in range(4, 11)]

    for i, contributor in enumerate(leaderboard):
        medal = medals[i] if i < len(medals) else f'{i+1}️⃣'
        lines.append(f'{medal} 用户 {contributor.qqid} — {contributor.amount} BREAK')

    lines.extend([
        '',
        '感谢他们为 AWMC 做出的贡献~',
        '',
        '💡 发送"BREAK抽奖"来贡献吧！',
    ])

    await plugin_finish(
        break_gamble_leaderboard,
        rank_text_image('\n'.join(lines)),
        event=event,
        reply_message=True,
    )
