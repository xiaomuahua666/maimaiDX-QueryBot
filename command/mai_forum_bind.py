"""论坛 OAuth 管理员映射命令；用户绑定入口已合并到 qbind。"""

from __future__ import annotations

import re
from typing import Optional

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Message, MessageEvent
from nonebot.params import CommandArg

from ..libraries.maimaidx_bot_admin import GUESS_GROUP_MANAGER
from ..libraries.maimaidx_platform import (
    get_event_group_id,
    parse_at_target_id,
    platform_user_id,
    plugin_finish,
)
from ..libraries.maimaidx_qq_bind import qq_bind_db


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

for _admin_matcher in (
    group_bind_qq,
    group_unbind_qq,
    group_bind_status,
    force_user_bind,
):
    setattr(_admin_matcher, '_maimaidx_announcement_exempt', True)
    setattr(_admin_matcher, '_maimaidx_debt_exempt', True)
    setattr(_admin_matcher, '_maimaidx_busy_surcharge_exempt', True)


def _parse_qq(raw: str) -> Optional[int]:
    match = re.search(r'(?<!\d)(\d{5,12})(?!\d)', str(raw or ''))
    if not match:
        return None
    value = int(match.group(1))
    return value if 10000 <= value <= 999999999999 else None


def _tokens(args: Message) -> list[str]:
    return [x for x in args.extract_plain_text().strip().split() if x]


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
            '官方 QQ 可用「强制绑定QQ 平台用户ID QQ号」。\n'
            '普通用户请使用 qbind 通过论坛 OAuth 绑定。',
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
