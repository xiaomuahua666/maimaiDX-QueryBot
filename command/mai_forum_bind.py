"""论坛 OAuth 绑定，以及官方 QQ 的管理员映射命令。"""

from __future__ import annotations

import re
from typing import Optional

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Message, MessageEvent
from nonebot.params import CommandArg

from ..libraries.maimaidx_bot_admin import GUESS_GROUP_MANAGER
from ..libraries.maimaidx_forum_auth import (
    ForumOAuthError,
    begin_forum_login,
    complete_forum_login,
    forum_binding_text,
)
from ..libraries.maimaidx_platform import (
    get_event_group_id,
    parse_at_target_id,
    platform_user_id,
    plugin_finish,
)
from ..libraries.maimaidx_qq_bind import qq_bind_db


forum_bind = on_command(
    '论坛绑定', aliases={'论坛登录', 'AWMC论坛绑定', 'awmc论坛绑定'}
)
forum_bind_status = on_command(
    '论坛绑定状态', aliases={'论坛账号状态', '论坛状态'}
)
forum_bind_cancel = on_command('取消论坛绑定', aliases={'论坛绑定取消'})

group_bind_qq = on_command(
    '群绑定QQ', aliases={'绑定群QQ', '强制绑定群', '群绑定'},
    permission=GUESS_GROUP_MANAGER,
)
group_unbind_qq = on_command(
    '解绑群QQ', aliases={'群解绑QQ', '取消群绑定'},
    permission=GUESS_GROUP_MANAGER,
)
group_bind_status = on_command(
    '群绑定状态', aliases={'查群绑定', '群QQ绑定状态'},
    permission=GUESS_GROUP_MANAGER,
)
force_user_bind = on_command(
    '强制绑定QQ', aliases={'管理员绑定QQ', '帮忙绑定QQ'},
    permission=GUESS_GROUP_MANAGER,
)


def _parse_qq(raw: str) -> Optional[int]:
    match = re.search(r'(?<!\d)(\d{5,12})(?!\d)', str(raw or ''))
    if not match:
        return None
    value = int(match.group(1))
    return value if 10000 <= value <= 999999999999 else None


def _tokens(args: Message) -> list[str]:
    return [x for x in args.extract_plain_text().strip().split() if x]


@forum_bind.handle()
async def _(event: MessageEvent, args: Message = CommandArg()):
    pid = platform_user_id(event)
    raw = args.extract_plain_text().strip()
    if not raw:
        try:
            url = begin_forum_login(pid)
        except ForumOAuthError as exc:
            await plugin_finish(forum_bind, str(exc), event=event)
        await plugin_finish(
            forum_bind,
            '请在浏览器打开下面的论坛授权链接并登录：\n'
            f'{url}\n\n'
            '授权完成后，把回调地址中的 code=...（或完整回调 URL）发送：\n'
            '论坛绑定 授权码\n'
            '授权码仅一次有效，10 分钟内有效。',
            event=event,
        )

    try:
        profile = await complete_forum_login(pid, raw)
    except ForumOAuthError as exc:
        await plugin_finish(forum_bind, str(exc), event=event)
    qq = profile.get('legacy_qq')
    lines = [
        f"论坛绑定成功：{profile.get('username') or profile.get('xf_user_id')}",
        f"邮箱：{profile.get('email') or '未返回'}",
    ]
    if qq:
        lines.append(f'已关联查分 QQ：{qq}')
        lines.append('现在可以直接使用查分、B50 和成绩同步功能。')
    else:
        lines.append(
            '论坛邮箱不是数字@qq.com，暂未关联查分 QQ。请修改论坛邮箱后重新绑定，'
            '或请群管理员使用「强制绑定QQ」协助绑定。'
        )
    await plugin_finish(forum_bind, '\n'.join(lines), event=event)


@forum_bind_status.handle()
async def _(event: MessageEvent):
    await plugin_finish(
        forum_bind_status,
        forum_binding_text(platform_user_id(event)),
        event=event,
    )


@forum_bind_cancel.handle()
async def _(event: MessageEvent):
    qq_bind_db.clear_forum_pending(platform_user_id(event))
    await plugin_finish(forum_bind_cancel, '已取消本次论坛授权。', event=event)


@group_bind_qq.handle()
async def _(event: MessageEvent, args: Message = CommandArg()):
    gid = get_event_group_id(event)
    if gid is None:
        await plugin_finish(group_bind_qq, '请在群内使用。', event=event)
    qq = _parse_qq(args.extract_plain_text())
    if qq is None:
        old = qq_bind_db.get_group_legacy_id(str(gid))
        hint = f'当前绑定：{old}' if old else '当前尚未绑定'
        await plugin_finish(
            group_bind_qq,
            f'用法：群绑定QQ <旧QQ群号>\n{hint}',
            event=event,
        )
    qq_bind_db.bind_group(str(gid), qq)
    await plugin_finish(
        group_bind_qq,
        f'本群已绑定到旧 QQ 群号 {qq}。群级猜歌/排行榜数据将继续使用该数据键。',
        event=event,
    )


@group_unbind_qq.handle()
async def _(event: MessageEvent):
    gid = get_event_group_id(event)
    if gid is None:
        await plugin_finish(group_unbind_qq, '请在群内使用。', event=event)
    if not qq_bind_db.unbind_group(str(gid)):
        await plugin_finish(group_unbind_qq, '本群尚未设置旧 QQ 群号绑定。', event=event)
    await plugin_finish(group_unbind_qq, '本群旧 QQ 群号绑定已解除。', event=event)


@group_bind_status.handle()
async def _(event: MessageEvent):
    gid = get_event_group_id(event)
    if gid is None:
        await plugin_finish(group_bind_status, '请在群内使用。', event=event)
    qq = qq_bind_db.get_group_legacy_id(str(gid))
    await plugin_finish(
        group_bind_status,
        f'平台群 ID：{gid}\n旧 QQ 群号：{qq or "未绑定"}',
        event=event,
    )


@force_user_bind.handle()
async def _(event: MessageEvent, args: Message = CommandArg()):
    target = parse_at_target_id(event)
    tokens = _tokens(args)
    if target is None and len(tokens) >= 2:
        target = tokens[0]
    qq = _parse_qq(args.extract_plain_text())
    if qq is None:
        await plugin_finish(
            force_user_bind,
            '用法：强制绑定QQ @用户 <QQ号>\n'
            '官方 QQ 可用「强制绑定QQ 平台用户ID QQ号」。',
            event=event,
        )
    target = target or platform_user_id(event)
    qq_bind_db.bind(str(target), qq)
    qq_bind_db.set_forum_legacy_qq(str(target), qq)
    await plugin_finish(
        force_user_bind,
        f'已将平台用户 {target} 绑定到查分 QQ {qq}。',
        event=event,
    )
