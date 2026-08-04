"""官方 QQ openid 绑定水鱼查分 QQ：qbind / qunbind / qbind状态 / 我的id / 群成员记录"""

import asyncio
import time
from typing import Optional

from nonebot import on_command, on_message
from nonebot.adapters.onebot.v11 import Message, MessageEvent
from nonebot.params import CommandArg

from ..libraries.maimaidx_bot_admin import PLUGIN_ADMIN_ONLY, is_plugin_admin
from ..libraries.maimaidx_platform import is_qq_event, platform_user_id, plugin_finish, use_qq_mode
from ..libraries.maimaidx_qq_bind import qq_bind_db
from ..libraries.maimaidx_qq_member_registry import qq_member_registry, record_from_event

qbind_cmd = on_command('qbind', aliases={'绑定qq', 'QQ绑定', '/qbind', 'mai绑定qq', 'maiqbind'})
qunbind_cmd = on_command('qunbind', aliases={'解绑qq', 'QQ解绑'})
qbind_status = on_command('qbind状态', aliases={'查绑定qq', '我的qbind'})
my_platform_id = on_command('我的id', aliases={'platformid', '平台id', '我的openid'})
group_member_list = on_command('群成员记录', permission=PLUGIN_ADMIN_ONLY)

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
    if not text:
        return None
    if not text.isdigit():
        return None
    qq = int(text)
    if qq < 10000 or qq > 999999999999:
        return None
    return qq


@qbind_cmd.handle()
async def _(event: MessageEvent, args: Message = CommandArg()):
    if not use_qq_mode(event):
        await plugin_finish(
            qbind_cmd,
            '当前为 OneBot 模式，消息 QQ 即查分 QQ，无需 qbind。\n'
            '官方 QQ 群直接发送 qbind 即可绑定。',
            event=event,
        )
    qq = _parse_qq_arg(args.extract_plain_text())
    if qq is None:
        await plugin_finish(
            qbind_cmd,
            '用法：qbind 你的QQ号\n'
            '示例：qbind 123456789\n'
            '请填写水鱼查分器绑定的 QQ 号。',
            event=event,
        )
    pid = platform_user_id(event)
    existing = qq_bind_db.get_legacy_qq(pid)
    qq_bind_db.bind(pid, qq)
    if existing and existing != qq:
        await plugin_finish(
            qbind_cmd,
            f'已更新绑定：查分 QQ {existing} → {qq}',
            event=event,
        )
    await plugin_finish(qbind_cmd, f'绑定成功！查分将使用 QQ {qq}', event=event)


@qunbind_cmd.handle()
async def _(event: MessageEvent):
    if not use_qq_mode(event):
        await plugin_finish(qunbind_cmd, 'OneBot 模式无需解绑。', event=event)
    pid = platform_user_id(event)
    if not qq_bind_db.unbind(pid):
        await plugin_finish(qunbind_cmd, '你尚未绑定查分 QQ。', event=event)
    await plugin_finish(qunbind_cmd, '已解绑查分 QQ。', event=event)


@qbind_status.handle()
async def _(event: MessageEvent):
    if not use_qq_mode(event):
        await plugin_finish(
            qbind_status,
            f'OneBot 模式，当前查分 QQ：{event.get_user_id()}',
            event=event,
        )
    pid = platform_user_id(event)
    legacy = qq_bind_db.get_legacy_qq(pid)
    if legacy is None:
        await plugin_finish(
            qbind_status,
            '未绑定查分 QQ。发送 qbind 你的QQ号 进行绑定。',
            event=event,
        )
    await plugin_finish(
        qbind_status,
        f'平台 ID：{pid}\n查分 QQ：{legacy}',
        event=event,
    )


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
