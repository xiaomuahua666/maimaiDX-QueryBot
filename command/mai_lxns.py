"""
落雪查分器绑定与查询指令。

指令：
  lxbind          — OAuth 授权绑定落雪账号（无回调模式）
  lxunbind        — 解绑落雪账号
  lxb50           — 用落雪数据源查询 B50
"""

import asyncio
import re
import time
from textwrap import dedent
from typing import Optional

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Message, MessageSegment, MessageEvent
from nonebot.matcher import Matcher
from nonebot.params import Arg, CommandArg

from ..config import log, maiconfig
from ..libraries.image import image_to_base64
from ..libraries.maimaidx_best_50 import DrawBest
from ..libraries.maimaidx_error import QBindRequiredError
from ..libraries.maimaidx_lxns_client import (
    fetch_token,
    get_authorize_url,
    refresh_token,
    user_get_player,
)
from ..libraries.maimaidx_lxns_db import lxns_db
from ..libraries.maimaidx_platform import resolve_score_qqid
from ..libraries.maimaidx_pending_session import finish_pending, session_key, track_event

# ─────────────────────────── helpers ───────────────────────────

_oauth_refresh_locks: dict[int, asyncio.Lock] = {}


def _lxns_qqid(event) -> int:
    """落雪库与 API 统一用查分 QQ（官方 QQ 需先 qbind）。"""
    return resolve_score_qqid(event)


async def _do_token_refresh(qqid: int, db_row: dict) -> Optional[str]:
    """尝试刷新 token，成功返回新 access_token，失败返回 None。"""
    rt = db_row.get('refresh_token')
    if not rt:
        return None
    try:
        token_data = await refresh_token(rt)
        lxns_db.update_tokens(
            qqid,
            access_token=token_data['access_token'],
            # 部分 OAuth 服务刷新时不会轮换 refresh_token。
            refresh_token=token_data.get('refresh_token') or rt,
            expires_in=token_data.get('expires_in', 900),
            scope=token_data.get('scope') or db_row.get('scope', ''),
            token_type=token_data.get('token_type') or db_row.get('token_type', 'Bearer'),
        )
        return token_data['access_token']
    except Exception as e:
        log.warning(f'[lxns] refresh_token failed for qq={qqid}: {e}')
        return None


async def _get_valid_access_token(
    qqid: int, *, force_refresh: bool = False
) -> Optional[str]:
    """获取有效的 OAuth access_token（自动刷新过期 token）。"""
    lock = _oauth_refresh_locks.setdefault(qqid, asyncio.Lock())
    # 刷新锁也必须有等待上限，避免并发上传无限卡在 refresh。
    try:
        await asyncio.wait_for(lock.acquire(), timeout=25.0)
    except asyncio.TimeoutError:
        log.warning(f'[lxns] refresh lock timeout for qq={qqid}')
        return None
    try:
        # 等锁期间可能已有另一个请求刷新完成，因此必须重新读取数据库。
        db_row = lxns_db.get_user(qqid)
        if not db_row:
            return None

        access_token = db_row.get('access_token')
        expires_at = db_row.get('expires_at', 0)

        if not force_refresh and access_token and expires_at > time.time() + 60:
            return access_token

        return await _do_token_refresh(qqid, db_row)
    finally:
        lock.release()


# ─────────────────────────── lxbind ───────────────────────────

lxbind = on_command('lxbind', aliases={'绑定落雪', '绑定lx'})


@lxbind.handle()
async def _lxbind(matcher: Matcher, event: MessageEvent, message: Message = CommandArg()):
    if not maiconfig.lx_client_id or not maiconfig.lx_client_secret:
        await lxbind.finish('Bot 管理员尚未配置落雪 OAuth 信息，无法绑定。', reply_message=True)

    args = message.extract_plain_text().strip()
    if args:
        # 用户直接 lxbind XXXX-XXXX-XXXX
        matcher.set_arg('code', message)
    else:
        url = get_authorize_url(maiconfig.lx_client_id)
        prompt = dedent(f"""\
            请点击以下链接进行落雪查分器授权
            =======================
            {url}
            =======================
            授权后你将看到一个授权码（形如 XXXX-XXXX-XXXX）
            请直接发送该授权码完成绑定
            =======================
            请注意！你必须在落雪查分器的
            「账号设置 → 常规设置」中的
            「隐私设置」开启允许读取成绩，
            否则 Bot 将无法查询你的成绩
        """).strip()
        track_event(session_key('lxbind', event), event)
        await lxbind.send(prompt, reply_message=True)


@lxbind.got('code')
async def _lxbind_got(matcher: Matcher, event: MessageEvent, code_msg: Message = Arg('code')):
    pending_key = session_key('lxbind', event)
    try:
        qqid = _lxns_qqid(event)
    except QBindRequiredError as e:
        finish_pending(pending_key)
        await lxbind.finish(str(e), reply_message=True)
    code = code_msg.extract_plain_text().strip()

    # 取消机制
    if code.lower() in ('取消', 'cancel', 'q', '退出'):
        finish_pending(pending_key)
        await lxbind.finish('已取消落雪绑定。', reply_message=True)

    # 格式校验 + 限制重试次数（最多 3 次）
    if not re.match(r'^[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}$', code, re.IGNORECASE):
        retry = matcher.state.get('lxbind_retry', 0) + 1
        matcher.state['lxbind_retry'] = retry
        if retry >= 3:
            finish_pending(pending_key)
            await lxbind.finish(
                '授权码格式错误次数过多，已退出绑定。\n请重新发送 lxbind 再试。',
                reply_message=True,
            )
        track_event(pending_key, event)
        await lxbind.reject(
            f'授权码格式错误，应为 XXXX-XXXX-XXXX，请重新发送。（{retry}/3）\n'
            f'若你发的是 QQ 号，请先发送：qbind 你的QQ号\n'
            f'落雪 OAuth 码需先发送 lxbind 获取授权链接。\n'
            f'发送「取消」可退出绑定。',
            reply_message=True,
        )

    try:
        token_data = await fetch_token(code)
    except Exception as e:
        log.error(f'[lxbind] token exchange failed: {e}')
        finish_pending(pending_key)
        await lxbind.finish(f'授权码兑换失败：{e}', reply_message=True)

    access_token = token_data['access_token']
    refresh_token_val = token_data['refresh_token']
    expires_in = token_data.get('expires_in', 900)
    scope = token_data.get('scope', '')
    scope_items = str(scope or '').replace(',', ' ').split()

    friend_code = None
    nickname = ''
    rating = 0
    try:
        player = await user_get_player(access_token)
        if player:
            friend_code = player.get('friend_code')
            nickname = player.get('name', '')
            rating = player.get('rating', 0)
    except Exception as e:
        log.warning(f'[lxbind] get player info failed: {e}')

    lxns_db.upsert_user(
        qqid,
        friend_code=friend_code,
        access_token=access_token,
        refresh_token=refresh_token_val,
        token_type=token_data.get('token_type', 'Bearer'),
        expires_at=time.time() + expires_in,
        scope=scope,
    )

    fc_msg = f'好友码：{friend_code}' if friend_code else '（未获取到好友码）'
    write_note = (
        ''
        if not scope_items or 'write_player' in scope_items
        else '\n⚠️ 本次授权缺少 write_player，无法上传成绩；请联系管理员检查 OAuth 应用权限。'
    )
    finish_pending(pending_key)
    await lxbind.finish(
        f'落雪绑定成功！\n'
        f'昵称：{nickname}\n'
        f'{fc_msg}\n'
        f'Rating：{rating}{write_note}',
        reply_message=True,
    )


# ─────────────────────────── lxunbind ───────────────────────────

lxunbind = on_command('lxunbind', aliases={'解绑落雪', '解绑lx'})


@lxunbind.handle()
async def _lxunbind(event: MessageEvent):
    try:
        qqid = _lxns_qqid(event)
    except QBindRequiredError as e:
        await lxunbind.finish(str(e), reply_message=True)
    user = lxns_db.get_user(qqid)
    if not user:
        await lxunbind.finish('你尚未绑定落雪查分器。', reply_message=True)
    lxns_db.clear_user(qqid)
    await lxunbind.finish('已解绑落雪查分器。', reply_message=True)


# ─────────────────────────── 数据源切换 ───────────────────────────

source_cmd = on_command('数据源', aliases={'切换数据源', 'datasource'})


@source_cmd.handle()
async def _source_cmd(event: MessageEvent, message: Message = CommandArg()):
    try:
        qqid = _lxns_qqid(event)
    except QBindRequiredError as e:
        await source_cmd.finish(str(e), reply_message=True)
    args = message.extract_plain_text().strip().lower()

    source_map = {
        'awmcnet': 'awmcnet', 'awmc': 'awmcnet',
        '水鱼': 'divingfish', 'divingfish': 'divingfish', 'df': 'divingfish',
        '落雪': 'lxns', 'lxns': 'lxns', 'lx': 'lxns',
    }

    if not args:
        current = lxns_db.get_source(qqid)
        label = {'awmcnet': 'AWMCNET', 'lxns': '落雪'}.get(current, '水鱼')
        await source_cmd.finish(
            f'当前数据源：{label}\n'
            f'可选：AWMCNET / 水鱼 / 落雪\n'
            f'用法：数据源 AWMCNET',
            reply_message=True,
        )

    if args not in source_map:
        await source_cmd.finish('未知数据源，可选：AWMCNET / 水鱼 / 落雪', reply_message=True)

    target = source_map[args]

    if target == 'lxns' and not maiconfig.lxns_dev_token:
        has_oauth = lxns_db.get_user(qqid) and lxns_db.get_user(qqid).get('access_token')
        if not has_oauth:
            await source_cmd.finish(
                '切换到落雪数据源需要满足以下条件之一：\n'
                '1. Bot 配置了开发者 Token（LXNS_DEV_TOKEN）\n'
                '2. 你已通过 lxbind 授权绑定落雪账号\n'
                '当前均不满足，请先完成绑定或联系管理员。',
                reply_message=True,
            )

    lxns_db.set_source(qqid, target)
    label = {'awmcnet': 'AWMCNET', 'lxns': '落雪'}.get(target, '水鱼')
    await source_cmd.finish(f'数据源已切换为：{label}', reply_message=True)


# ─────────────────────────── lxb50（供外部调用） ───────────────────────────

async def generate_lxns_b50(qqid: int) -> Optional[MessageSegment]:
    """
    用落雪数据源生成 b50 图片（强制走 lxns）。
    成功返回 MessageSegment（纯图片）；绑定/授权失败返回 None。
    """
    from ..libraries.maimaidx_b50_warnings import prepare_b50_warnings
    from ..libraries.maimaidx_datasource import get_user_b50
    from ..libraries.maimaidx_error import LxnsDataError

    try:
        userinfo = await get_user_b50(qqid=qqid, force_source='lxns')
    except LxnsDataError as e:
        log.warning(f'[lxb50] qq={qqid}: {e}')
        return None
    except Exception as e:
        log.warning(f'[lxb50] qq={qqid} unexpected: {e}')
        return None

    prepare_b50_warnings(userinfo, 'lxns')
    draw_best = DrawBest(userinfo, qqid)
    return MessageSegment.image(image_to_base64(await draw_best.draw()))


# ─────────────────────────── lxb50 指令 ───────────────────────────

lxb50 = on_command('lxb50', aliases={'落雪b50', '落雪B50', 'lx50'})


@lxb50.handle()
async def _lxb50(event: MessageEvent):
    try:
        qqid = _lxns_qqid(event)
    except QBindRequiredError as e:
        await lxb50.finish(str(e), reply_message=True)

    try:
        from ..libraries.maimaidx_timing import run_timed, timing_text
        result, total = await run_timed(generate_lxns_b50(qqid))
        from ..libraries.maimaidx_player_cache import footer_join_sections, pop_data_freshness_footer_lines
        from ..libraries.maimaidx_b50_warnings import pop_b50_warning_footer
    except Exception as e:
        log.error(f'[lxb50] error: {e}', exc_info=True)
        await lxb50.finish(f'查询失败：{type(e).__name__}: {e}', reply_message=True)
    if result is None:
        await lxb50.finish(
            '落雪数据获取失败，请检查：\n'
            '1. 是否已发送 lxbind 绑定落雪\n'
            '2. 或在落雪查分器绑定了 QQ\n'
            '3. Bot 是否配置了开发者 Token',
            reply_message=True,
        )
    sections = [['📊 数据源：落雪 | 可使用 数据源 AWMCNET/水鱼/落雪 修改']]
    freshness = pop_data_freshness_footer_lines()
    if freshness:
        sections.append(freshness)
    warning = pop_b50_warning_footer()
    if warning:
        sections.append([warning])
    sections.append([timing_text(total)])
    footer = footer_join_sections(sections)
    await lxb50.finish(result + MessageSegment.text(footer), reply_message=True)
