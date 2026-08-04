"""官方 QQ openid 绑定水鱼查分 QQ：qbind 走论坛 OAuth。"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

from nonebot import on_command, on_message
from nonebot.adapters.onebot.v11 import Message, MessageEvent
from nonebot.params import CommandArg

from ..libraries.maimaidx_bot_admin import PLUGIN_ADMIN_ONLY, is_plugin_admin
from ..libraries.maimaidx_forum_auth import (
    ForumOAuthError,
    begin_forum_login,
    complete_forum_login,
    forum_binding_text,
    parse_authorization_code,
)
from ..libraries.maimaidx_platform import (
    is_qq_event,
    platform_user_id,
    plugin_finish,
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
qunbind_cmd = on_command('qunbind', aliases={'解绑qq', 'QQ解绑'})
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
        '请通过论坛 OAuth 绑定查分 QQ（不要只手填 QQ 号）：\n'
        f'{url}\n'
        f'{claim}\n'
        '1. 浏览器打开链接并登录论坛\n'
        '2. 论坛邮箱请使用 你的QQ号@qq.com\n'
        '3. 授权后把回调里的 code=...（或完整回调 URL）发回：\n'
        '   qbind 授权码\n'
        '授权码 10 分钟内有效，仅一次可用。'
    )


def _oauth_success_text(profile: dict) -> str:
    qq = profile.get('legacy_qq') or ''
    lines = [
        f"论坛 OAuth 绑定成功：{profile.get('username') or profile.get('xf_user_id')}",
        f"邮箱：{profile.get('email') or '未返回'}",
        f'查分 QQ：{qq}',
        '现在可以直接使用签到、查分、B50 等账号功能。',
    ]
    return '\n'.join(lines)


@qbind_cmd.handle()
async def _(event: MessageEvent, args: Message = CommandArg()):
    if not use_qq_mode(event):
        await plugin_finish(
            qbind_cmd,
            '当前为 OneBot 模式，消息 QQ 即查分 QQ，无需 qbind / 论坛 OAuth。',
            event=event,
        )

    pid = platform_user_id(event)
    raw = args.extract_plain_text().strip()

    # 主路径：无参数 → 发起 OAuth
    if not raw:
        try:
            url = begin_forum_login(pid)
        except ForumOAuthError as exc:
            await plugin_finish(qbind_cmd, str(exc), event=event)
        await plugin_finish(qbind_cmd, _oauth_start_text(url), event=event)

    # 可选：手填 QQ，仍必须走 OAuth 校验
    claimed = _parse_qq_arg(raw)
    if claimed is not None:
        try:
            url = begin_forum_login(pid, claimed_qq=claimed)
        except ForumOAuthError as exc:
            await plugin_finish(qbind_cmd, str(exc), event=event)
        await plugin_finish(
            qbind_cmd,
            _oauth_start_text(url, claimed_qq=claimed),
            event=event,
        )

    # 授权码 / 回调 URL → 完成 OAuth
    if not _looks_like_oauth_code(raw):
        await plugin_finish(
            qbind_cmd,
            '用法：\n'
            '· qbind — 发起论坛 OAuth（推荐）\n'
            '· qbind 授权码 — 粘贴回调 code / URL 完成绑定\n'
            '· qbind QQ号 — 可选预填，仍须 OAuth 校验邮箱 QQ\n'
            '论坛邮箱请使用 数字@qq.com。',
            event=event,
        )

    try:
        profile = await complete_forum_login(pid, raw)
    except ForumOAuthError as exc:
        await plugin_finish(qbind_cmd, str(exc), event=event)
    await plugin_finish(qbind_cmd, _oauth_success_text(profile), event=event)


@qunbind_cmd.handle()
async def _(event: MessageEvent):
    if not use_qq_mode(event):
        await plugin_finish(qunbind_cmd, 'OneBot 模式无需解绑。', event=event)
    pid = platform_user_id(event)
    qq_bind_db.clear_forum_pending(pid)
    unbound = qq_bind_db.unbind(pid)
    # Keep forum profile row but clear score QQ mapping via unbind only.
    if not unbound:
        await plugin_finish(qunbind_cmd, '你尚未绑定查分 QQ。', event=event)
    await plugin_finish(qunbind_cmd, '已解绑查分 QQ。如需重新绑定请发送 qbind。', event=event)


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
    qq_bind_db.clear_forum_pending(platform_user_id(event))
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
