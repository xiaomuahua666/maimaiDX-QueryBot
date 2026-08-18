"""水鱼查分器 OAuth 授权：一次授权同时用于查分与成绩上传。"""

import asyncio
import time
from textwrap import dedent

from nonebot import on_command
from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import Message, MessageEvent
from nonebot.params import CommandArg

from ..config import log
from ..libraries.maimaidx_divingfish_oauth import (
    binding_label,
    create_device_authorization,
    get_access_token,
    oauth_enabled,
    revoke_url,
)
from ..libraries.maimaidx_error import (
    DivingFishNotAuthorizedError,
    DivingFishOAuthError,
    QBindRequiredError,
)
from ..libraries.maimaidx_platform import (
    build_markdown_message,
    build_mention_message,
    get_event_group_id,
    plugin_finish,
    platform_user_id,
    resolve_score_qqid,
    send_group_message,
    use_qq_mode,
)


_df_bind_tasks: dict[int, asyncio.Task] = {}


async def _wait_for_df_bind_and_notify(
    bot: Bot,
    event: MessageEvent,
    qqid: int,
    *,
    expires_in: int,
    interval: int,
) -> None:
    """Poll the device grant and announce completion in the originating group."""
    group_id = get_event_group_id(event)
    if group_id is None:
        return
    deadline = time.monotonic() + max(int(expires_in), 1)
    delay = max(int(interval), 2)
    while time.monotonic() < deadline:
        await asyncio.sleep(min(delay, max(deadline - time.monotonic(), 0)))
        if time.monotonic() >= deadline:
            break
        try:
            await get_access_token(qqid)
        except DivingFishNotAuthorizedError:
            continue
        except (DivingFishOAuthError, RuntimeError, OSError) as exc:
            log.debug(
                f'[divingfish-oauth] bind poll pending qq={qqid}: '
                f'{type(exc).__name__}'
            )
            continue
        try:
            message = build_mention_message(
                platform_user_id(event),
                '\n✅ 水鱼 OAuth 绑定成功！现在可以使用水鱼数据源查询 B50，'
                '也可以通过 maiu/maiua 上传成绩。',
                event=event,
            )
            await send_group_message(bot, group_id, message)
        except Exception as exc:
            log.warning(
                f'[divingfish-oauth] group bind notification failed '
                f'qq={qqid}: {type(exc).__name__}'
            )
        return


def _forget_df_bind_task(qqid: int, task: asyncio.Task) -> None:
    if _df_bind_tasks.get(qqid) is task:
        _df_bind_tasks.pop(qqid, None)


def _schedule_df_bind_notification(
    bot: Bot,
    event: MessageEvent,
    qqid: int,
    *,
    expires_in: int,
    interval: int,
) -> None:
    if get_event_group_id(event) is None:
        return
    previous = _df_bind_tasks.get(qqid)
    if previous is not None and not previous.done():
        previous.cancel()
    task = asyncio.create_task(
        _wait_for_df_bind_and_notify(
            bot,
            event,
            qqid,
            expires_in=expires_in,
            interval=interval,
        )
    )
    _df_bind_tasks[qqid] = task
    task.add_done_callback(lambda done: _forget_df_bind_task(qqid, done))


def _oauth_prompt(url: str, qqid: int, expires_in: int, *, event) -> object:
    minutes = max(int(expires_in) // 60, 1)
    plain_text = dedent(
        f'''\
        水鱼查分器授权

        授权页面：{url}

        1. 打开链接并登录水鱼账号
        2. 确认页面显示的绑定身份为「{binding_label(qqid)}」后点击「同意授权」

        链接 {minutes} 分钟内有效，授权完成后无需回复授权码。
        本次授权同时用于读取资料、读取成绩和上传成绩，不再需要 Import-Token。
        发送「刷新b50」可立即读取水鱼全量成绩并同步到 AWMC NET。
        请注意：这条链接仅供您本人使用，请勿转发他人。
        管理或取消授权：{revoke_url()}
        '''
    ).strip()
    if not use_qq_mode(event):
        return plain_text

    authorize_url = url.replace(')', '\\)')
    revoke_manage_url = revoke_url().replace(')', '\\)')
    markdown = dedent(
        f'''\
        ## 水鱼查分器授权

        [打开水鱼授权页面]({authorize_url})

        1. 打开链接并登录水鱼账号
        2. 确认页面显示的绑定身份为「{binding_label(qqid)}」后点击「同意授权」

        链接 {minutes} 分钟内有效，授权完成后无需回复授权码。
        本次授权同时用于读取资料、读取成绩和上传成绩，不再需要 Import-Token。
        发送「刷新b50」可立即读取水鱼全量成绩并同步到 AWMC NET。
        请注意：这条链接仅供您本人使用，请勿转发他人。

        [管理或取消水鱼授权]({revoke_manage_url})
        '''
    ).strip()
    return build_markdown_message(markdown, event=event)


df_bind = on_command(
    'dfbind',
    aliases={'绑定水鱼', '绑定df', '水鱼授权'},
    block=True,
)
df_status = on_command(
    'dfstatus', aliases={'水鱼授权状态', '水鱼OAuth状态'}, block=True
)


@df_bind.handle()
async def _handle_df_bind(
    bot: Bot,
    event: MessageEvent,
    args: Message = CommandArg(),
):
    if not oauth_enabled():
        await plugin_finish(
            df_bind,
            '水鱼 OAuth 当前未开启。\n'
            '如需绑定 Import-Token，请发送「mai绑定水鱼 <Token>」。',
            event=event,
            reply_message=True,
        )
    if args.extract_plain_text().strip():
        await plugin_finish(
            df_bind,
            '水鱼 OAuth 绑定不需要参数，请直接发送「绑定水鱼」。',
            event=event,
            reply_message=True,
        )
    try:
        qqid = resolve_score_qqid(event)
        authorization = await create_device_authorization(qqid)
    except QBindRequiredError as exc:
        await plugin_finish(df_bind, str(exc), event=event, reply_message=True)
    except (DivingFishOAuthError, RuntimeError, OSError) as exc:
        log.warning(f'[divingfish-oauth] authorization failed: {type(exc).__name__}')
        await plugin_finish(
            df_bind,
            '发起水鱼授权失败：水鱼账号服务可能暂时不可用，请稍后再试。',
            event=event,
            reply_message=True,
        )

    _schedule_df_bind_notification(
        bot,
        event,
        qqid,
        expires_in=authorization.expires_in,
        interval=authorization.interval,
    )
    await plugin_finish(
        df_bind,
        _oauth_prompt(
            authorization.verification_uri_complete,
            qqid,
            authorization.expires_in,
            event=event,
        ),
        event=event,
        reply_message=True,
    )


@df_status.handle()
async def _handle_df_status(event: MessageEvent):
    if not oauth_enabled():
        await plugin_finish(
            df_status,
            '水鱼 OAuth 当前未开启。',
            event=event,
        )
    try:
        qqid = resolve_score_qqid(event)
        await get_access_token(qqid)
    except QBindRequiredError as exc:
        await plugin_finish(df_status, str(exc), event=event)
    except Exception:
        await plugin_finish(
            df_status,
            '尚未完成新版水鱼 OAuth 授权，或授权已失效。\n'
            '请发送「绑定水鱼」重新授权；旧 Import-Token 已停用。',
            event=event,
        )
    await plugin_finish(
        df_status,
        '水鱼 OAuth 授权有效。\n已授权：账号资料读取、成绩读取、成绩写入。\n'
        '查分和上传会使用这一次授权。',
        event=event,
    )
