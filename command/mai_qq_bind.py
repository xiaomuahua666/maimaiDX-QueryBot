"""官方 QQ openid 绑定水鱼查分 QQ：qbind 走论坛 OAuth。"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

from nonebot import on_command, on_message
from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import Message, MessageEvent
from nonebot.adapters.qq.message import Message as QQMessage
from nonebot.adapters.qq.message import MessageSegment as QQSeg
from nonebot.adapters.qq.models import (
    Action,
    Button,
    InlineKeyboard,
    InlineKeyboardRow,
    MessageKeyboard,
    Permission,
    RenderData,
)
from nonebot.exception import FinishedException
from nonebot.params import CommandArg
from nonebot.rule import Rule

from ..libraries.maimaidx_break import break_db
from ..libraries.maimaidx_bot_admin import PLUGIN_ADMIN_ONLY, is_plugin_admin
from ..libraries.maimaidx_forum_auth import (
    ForumOAuthError,
    begin_forum_login,
    complete_forum_login,
    forum_binding_text,
    parse_authorization_code,
)
from ..libraries.maimaidx_platform import (
    extract_sent_message_id,
    foreign_recall_notice,
    is_qq_event,
    platform_user_id,
    plugin_finish,
    plugin_send,
    build_markdown_message,
    recall_message,
    use_qq_mode,
)
from ..libraries.maimaidx_qq_bind import qq_bind_db
from ..libraries.maimaidx_qq_member_registry import qq_member_registry, record_from_event

qbind_cmd = on_command(
    'qbind',
    aliases={
        '绑定qq',
        'QQ绑定',
        'mai绑定qq',
        'maiqbind',
        '论坛绑定',
        '论坛登录',
        'AWMC论坛绑定',
        'awmc论坛绑定',
    },
)
qunbind_cmd = on_command(
    'qunbind',
    aliases={'解绑qq', 'QQ解绑', '解绑qbind', '解绑论坛', '论坛解绑'},
)
qbind_status = on_command(
    'qbind状态',
    aliases={'查绑定qq', '我的qbind', '论坛绑定状态', '论坛账号状态', '论坛状态'},
)
my_platform_id = on_command('我的id', aliases={'platformid', '平台id', '我的openid'})
group_member_list = on_command('群成员记录', permission=PLUGIN_ADMIN_ONLY)
forum_bind_cancel = on_command('取消论坛绑定', aliases={'论坛绑定取消', '取消qbind'})

for _bind_matcher in (
    qbind_cmd,
    qunbind_cmd,
    qbind_status,
    my_platform_id,
    forum_bind_cancel,
):
    setattr(_bind_matcher, '_maimaidx_announcement_exempt', True)
    setattr(_bind_matcher, '_maimaidx_debt_exempt', True)
    setattr(_bind_matcher, '_maimaidx_busy_surcharge_exempt', True)

# 官方 QQ：群消息时登记 member_openid（无全量拉群 API，仅能积累见过的成员）
_qq_member_recorder = on_message(priority=99, block=False)
setattr(_qq_member_recorder, '_maimaidx_passive_recorder', True)

# platform_id -> 绑定流程中待撤回的消息 id（用户 qbind / bot 引导文）
_oauth_recall_ids: dict[str, list[str]] = {}

_RECALL_USER_WARN = foreign_recall_notice  # 按平台返回合适文案
_RECALL_BOT_WARN = '⚠️ Bot 无法撤回绑定引导消息，请手动删除含授权链接的消息。'


def _event_group_id(event) -> Optional[str]:
    gid = getattr(event, 'group_openid', None) or getattr(event, 'group_id', None)
    return str(gid) if gid is not None else None


@_qq_member_recorder.handle()
async def _record_qq_group_member(event: MessageEvent):
    if not is_qq_event(event):
        return
    if _event_group_id(event) is None:
        return
    await asyncio.to_thread(record_from_event, event)


def _parse_qq_arg(text: str) -> Optional[int]:
    text = text.strip()
    if not text or not text.isdigit():
        return None
    qq = int(text)
    if qq < 10000 or qq > 999999999999:
        return None
    return qq


def _looks_like_oauth_code(raw: str) -> bool:
    value = (raw or '').strip()
    if not value:
        return False
    if _parse_qq_arg(value) is not None:
        return False
    return bool(parse_authorization_code(value))


def _oauth_start_text(url: str, *, claimed_qq: Optional[int] = None) -> str:
    claim = ''
    if claimed_qq is not None:
        claim = (
            f'\n已记录待校验 QQ：{claimed_qq}\n'
            '授权完成后，论坛邮箱必须是该号码对应的 数字@qq.com。\n'
        )
    return (
        '请通过AWMC论坛绑定查分 QQ：\n'
        f'{url}\n'
        f'{claim}\n'
        '1. 浏览器打开链接并登录论坛～\n'
        '2. 论坛邮箱请使用 你的QQ号@qq.com！\n'
        '3. 授权后把授权链接直接发给我哟！\n'
        '\n'
        '授权码 10 分钟内有效，仅一次可用，请尽快操作哟！'
    )


def _normalize_oauth_paste(raw: str) -> str:
    """Strip accidental command prefixes from a pasted callback URL/code."""
    value = (raw or '').strip()
    for prefix in (
        'qbind',
        '论坛绑定',
        '论坛登录',
        '绑定qq',
        'QQ绑定',
        'mai绑定qq',
        'maiqbind',
    ):
        if value.lower().startswith(prefix.lower()):
            value = value[len(prefix):].strip()
            break
    return value


def _oauth_success_text(profile: dict, *, reward_awarded: bool = False) -> str:
    qq = profile.get('legacy_qq') or ''
    lines = [
        f"论坛 OAuth 绑定成功：{profile.get('username') or profile.get('xf_user_id')}",
        f"邮箱：{profile.get('email') or '未返回'}",
        f'查分 QQ：{qq}',
        '现在可以直接使用签到、查分、B50 等账号功能。',
    ]
    if reward_awarded:
        lines.append('首次绑定奖励：已发放 3 BREAK。')
    return '\n'.join(lines)


def _escape_markdown_value(value: object) -> str:
    """Keep profile fields from changing the surrounding QQ Markdown."""
    text = str(value or '未返回')
    for char in ('\\', '`', '*', '_', '[', ']', '(', ')', '#'):
        text = text.replace(char, f'\\{char}')
    return text


def _oauth_success_payload(
    profile: dict,
    event: MessageEvent,
    *,
    prefix: str = '',
    reward_awarded: bool = False,
):
    """Render the OAuth receipt as native QQ Markdown when available."""
    if not use_qq_mode(event):
        return prefix + _oauth_success_text(
            profile, reward_awarded=reward_awarded
        )

    name = _escape_markdown_value(profile.get('username') or profile.get('xf_user_id'))
    email = _escape_markdown_value(profile.get('email'))
    qq = _escape_markdown_value(profile.get('legacy_qq'))
    lines = ['## 论坛绑定成功', '']
    if prefix.strip():
        lines.extend(f'> {line}' for line in prefix.splitlines() if line.strip())
        lines.append('')
    lines.extend(
        [
            f'- **论坛**：{name}',
            f'- **邮箱**：{email}',
            f'- **查分 QQ**：{qq}',
            '',
            '已可使用：签到、查分、B50 等账号功能。',
        ]
    )
    if reward_awarded:
        lines.append('🎁 首次绑定奖励：**+3 BREAK**')
    keyboard = _build_welcome_keyboard(event)
    return QQMessage(
        [
            QQSeg.markdown('\n'.join(lines)),
            QQSeg.keyboard(keyboard),
        ]
    )




def _track_oauth_message(platform_id: str, *message_ids: object) -> None:
    bucket = _oauth_recall_ids.setdefault(str(platform_id), [])
    for mid in message_ids:
        value = str(mid or '').strip()
        if value and value not in bucket:
            bucket.append(value)


def _pop_oauth_messages(platform_id: str) -> list[str]:
    return _oauth_recall_ids.pop(str(platform_id), [])


def _event_message_id(event) -> str:
    mid = getattr(event, 'message_id', None) or getattr(event, 'id', None) or ''
    return str(mid).strip()


async def _send_oauth_start(
    matcher, bot: Bot, event: MessageEvent, text: str
) -> None:
    pid = platform_user_id(event)
    _track_oauth_message(pid, _event_message_id(event))
    payload = text
    if use_qq_mode(event):
        # Bare URLs in a QQ text payload are not consistently tappable.  Keep
        # the instructions readable while making the authorization entry a
        # native Markdown link; the platform layer adds the blue sender @ to
        # this same Markdown message.
        lines = text.splitlines()
        if len(lines) >= 2:
            url = lines[1].strip()
            if url.startswith(('http://', 'https://')):
                safe_url = url.replace(')', '\\)')
                lines[1] = f'[打开授权页面]({safe_url})'
        payload = build_markdown_message(
            '## AWMC 论坛绑定\n\n' + '\n'.join(lines),
            event=event,
        )
    result = await plugin_send(matcher, payload, event=event, reply_message=True)
    _track_oauth_message(pid, extract_sent_message_id(result))
    raise FinishedException


async def _complete_oauth_paste(
    matcher, bot: Bot, event: MessageEvent, raw: str
) -> None:
    pid = platform_user_id(event)
    payload = _normalize_oauth_paste(raw)

    # 官方 QQ 无法撤回用户消息；OneBot 仍尝试撤回授权码原消息
    user_recalled = await recall_message(bot, event, foreign=True)

    try:
        profile = await complete_forum_login(pid, payload)
        legacy_qq = profile.get('legacy_qq')
        reward_awarded = False
        if legacy_qq:
            reward_awarded = break_db.claim_once_reward(
                int(legacy_qq),
                'forum_bind_welcome',
                3,
                reason='forum_bind_welcome',
                meta={'platform_id': pid},
            ).awarded
    except ForumOAuthError as exc:
        warn = '' if user_recalled else f'{_RECALL_USER_WARN(event)}\n'
        await plugin_finish(matcher, warn + str(exc), event=event)

    bot_failed = 0
    for mid in _pop_oauth_messages(pid):
        if mid == _event_message_id(event):
            continue
        if not await recall_message(bot, event, message_id=mid):
            bot_failed += 1

    warnings: list[str] = []
    if not user_recalled:
        warnings.append(_RECALL_USER_WARN(event))
    if bot_failed:
        warnings.append(_RECALL_BOT_WARN)
    prefix = ('\n'.join(warnings) + '\n') if warnings else ''
    await plugin_finish(
        matcher,
        _oauth_success_payload(
            profile,
            event,
            prefix=prefix,
            reward_awarded=reward_awarded,
        ),
        event=event,
        mention_sender=False,
    )


# 已发起 qbind、等待授权时：可直接粘贴回调链接 / 授权码（无需再写 qbind）
async def _oauth_paste_rule(event: MessageEvent) -> bool:
    if not use_qq_mode(event):
        return False
    raw = (event.get_plaintext() or '').strip()
    if not raw:
        return False
    pending = qq_bind_db.get_forum_pending(platform_user_id(event))
    if pending is None:
        return False
    return _looks_like_oauth_code(_normalize_oauth_paste(raw))


_oauth_paste = on_message(rule=Rule(_oauth_paste_rule), priority=5, block=True)
setattr(_oauth_paste, '_maimaidx_announcement_exempt', True)
setattr(_oauth_paste, '_maimaidx_debt_exempt', True)
setattr(_oauth_paste, '_maimaidx_busy_surcharge_exempt', True)


@_oauth_paste.handle()
async def _(bot: Bot, event: MessageEvent):
    await _complete_oauth_paste(_oauth_paste, bot, event, event.get_plaintext() or '')


@qbind_cmd.handle()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    if not use_qq_mode(event):
        await plugin_finish(
            qbind_cmd,
            '当前为 OneBot 模式，消息 QQ 即查分 QQ，无需 qbind / 论坛 OAuth。',
            event=event,
        )

    pid = platform_user_id(event)
    raw = _normalize_oauth_paste(args.extract_plain_text())

    # 主路径：无参数 → 发起 OAuth
    if not raw:
        try:
            url = begin_forum_login(pid)
        except ForumOAuthError as exc:
            await plugin_finish(qbind_cmd, str(exc), event=event)
        await _send_oauth_start(qbind_cmd, bot, event, _oauth_start_text(url))

    # 可选：手填 QQ，仍必须走 OAuth 校验
    claimed = _parse_qq_arg(raw)
    if claimed is not None:
        try:
            url = begin_forum_login(pid, claimed_qq=claimed)
        except ForumOAuthError as exc:
            await plugin_finish(qbind_cmd, str(exc), event=event)
        await _send_oauth_start(
            qbind_cmd, bot, event, _oauth_start_text(url, claimed_qq=claimed)
        )

    # 授权码 / 回调 URL → 完成 OAuth
    if not _looks_like_oauth_code(raw):
        await plugin_finish(
            qbind_cmd,
            '用法：\n'
            '· qbind — 发起论坛 OAuth（推荐）\n'
            '· qbind 授权码或完整回调链接 — 完成绑定\n'
            '· 已发起 qbind 后也可直接发链接/授权码（不用前缀）\n'
            '· qbind QQ号 — 可选预填，仍须 OAuth 校验邮箱 QQ\n'
            '论坛邮箱请使用 数字@qq.com。',
            event=event,
        )

    await _complete_oauth_paste(qbind_cmd, bot, event, raw)


@qunbind_cmd.handle()
async def _(event: MessageEvent):
    if not use_qq_mode(event):
        await plugin_finish(qunbind_cmd, 'OneBot 模式无需解绑。', event=event)
    pid = platform_user_id(event)
    unbound = qq_bind_db.unbind(pid)
    _pop_oauth_messages(pid)
    if not unbound:
        await plugin_finish(
            qunbind_cmd,
            '你尚未绑定查分 QQ / 论坛账号。需要绑定请发送 qbind。',
            event=event,
        )
    await plugin_finish(
        qunbind_cmd,
        '已解绑查分 QQ 与论坛身份。重新绑定请发送 qbind。',
        event=event,
    )


@qbind_status.handle()
async def _(event: MessageEvent):
    if not use_qq_mode(event):
        await plugin_finish(
            qbind_status,
            f'OneBot 模式，当前查分 QQ：{event.get_user_id()}',
            event=event,
        )
    pid = platform_user_id(event)
    forum_text = forum_binding_text(pid)
    legacy = qq_bind_db.get_legacy_qq(pid)
    if legacy is None and '尚未绑定论坛' in forum_text:
        await plugin_finish(
            qbind_status,
            '未绑定查分 QQ。发送 qbind 通过论坛 OAuth 绑定。',
            event=event,
        )
    extra = f'\n当前查分 QQ 映射：{legacy}' if legacy else '\n当前查分 QQ 映射：无'
    await plugin_finish(qbind_status, forum_text + extra, event=event)


@forum_bind_cancel.handle()
async def _(event: MessageEvent):
    pid = platform_user_id(event)
    qq_bind_db.clear_forum_pending(pid)
    _pop_oauth_messages(pid)
    await plugin_finish(forum_bind_cancel, '已取消本次论坛授权。', event=event)


@my_platform_id.handle()
async def _(event: MessageEvent):
    pid = platform_user_id(event)
    gid = _event_group_id(event)
    role = ''
    if use_qq_mode(event) and gid:
        from ..libraries.maimaidx_bot_admin import _qq_group_role
        r = _qq_group_role(event)
        if r:
            role = f'\n群内身份：{r}'
    admin_hint = ''
    if is_plugin_admin(pid):
        admin_hint = '\n（你已在插件管理员列表中）'
    else:
        admin_hint = (
            '\n\n管理员可在 .env 配置：\n'
            f'MAIMAIDX_BOT_ADMINS={pid}'
        )
    await plugin_finish(
        my_platform_id,
        f'你的平台 ID：{pid}{role}{admin_hint}',
        event=event,
    )


@group_member_list.handle()
async def _(event: MessageEvent):
    if not use_qq_mode(event):
        await plugin_finish(
            group_member_list,
            '群成员记录仅用于官方 QQ 模式（无全量拉群成员 API，仅统计机器人见过的成员）。',
            event=event,
        )
    gid = _event_group_id(event)
    if not gid:
        await plugin_finish(group_member_list, '请在群内使用本命令。', event=event)
    total = qq_member_registry.count_group(gid)
    rows = qq_member_registry.list_group(gid, limit=30)
    if not rows:
        await plugin_finish(
            group_member_list,
            '本群尚无记录。成员发言后机器人会自动登记 member_openid。',
            event=event,
        )
    lines = [f'本群已记录 {total} 人（展示最近 30）：']
    for r in rows:
        ts = time.strftime('%m-%d %H:%M', time.localtime(r['last_seen']))
        lines.append(f"{r['member_id']} | {r['member_role']} | 最近 {ts} | ×{r['seen_count']}")
    lines.append('\n说明：官方 QQ 公域群无法一次性拉取全员，只能积累事件中的 openid。')
    await plugin_finish(group_member_list, '\n'.join(lines), event=event)


def _build_welcome_keyboard(event: Optional[MessageEvent] = None) -> MessageKeyboard:
    """论坛绑定完成后的下一步账号流程。"""
    binding = None
    has_lxns = False
    try:
        from ..libraries.maimaidx_account_db import account_db
        from ..libraries.maimaidx_lxns_db import lxns_db
        from ..libraries.maimaidx_platform import resolve_score_qqid

        qqid = resolve_score_qqid(event) if event is not None else None
        if qqid is not None:
            binding = account_db.get(str(qqid))
            lxns_row = lxns_db.get_user(int(qqid))
            has_lxns = bool(
                (binding and binding.lxns_token)
                or (lxns_row and lxns_row.get('access_token'))
            )
    except Exception:
        pass
    action_buttons = []
    has_account = bool(binding and binding.qrcode)
    if not has_account:
        action_buttons.append(('绑定舞萌', 'mai绑定'))
    if not (binding and binding.fish_token):
        action_buttons.append(('绑定水鱼', 'mai绑定水鱼'))
    if not has_lxns:
        action_buttons.append(('绑定落雪', 'lxbind'))
    if has_account and binding.fish_token and has_lxns:
        action_buttons.append(('自动上传 B50', 'maiua'))
    action_buttons.extend([
        ('标准 B50', 'b50'),
        ('PC50', 'pc50'),
        ('我的 PC', '我的pc数'),
        ('更新 PC', '更新pc数'),
        ('MyMai', 'mymai'),
        ('签到', '签到'),
    ])
    buttons = [
        Button(
            id=f'welcome-action-{idx}',
            render_data=RenderData(label=label, visited_label=label, style=1),
            action=Action(
                type=2,
                permission=Permission(type=2),
                data=cmd,
                enter=True,
                reply=False,
            ),
        )
        for idx, (label, cmd) in enumerate(action_buttons, 1)
    ]
    buttons.append(
        Button(
            id='welcome-help-link',
            render_data=RenderData(label='帮助', visited_label='帮助', style=1),
            action=Action(
                type=0,
                permission=Permission(type=2),
                data='https://wiki.awmc.team/guide/bot/intro',
            ),
        )
    )
    rows = [
        InlineKeyboardRow(buttons=buttons[start : start + 3])
        for start in range(0, len(buttons), 3)
    ]
    return MessageKeyboard(content=InlineKeyboard(rows=rows))
