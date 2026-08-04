import asyncio
import re
import time
from typing import Callable, Optional

from nonebot import on_command, on_message, on_regex
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageEvent, MessageSegment
from nonebot.exception import IgnoredException
from nonebot.matcher import Matcher
from nonebot.params import CommandArg, Depends

from ..config import log, maiconfig
from ..libraries.maimaidx_best_50 import generate_pc50, generate_pca50, generate_pc_rank50
from ..libraries.maimaidx_datasource import get_user_source
from ..libraries.maimaidx_error import QBindRequiredError
from ..libraries.maimaidx_music import feature_manager
from ..libraries.maimaidx_machine_session import (
    MachineBusyError,
    machine_session,
)
from ..libraries.maimaidx_reaction import react_processing
from ..libraries.maimaidx_processing_time import (
    auto_qrcode_workflow_key,
    processing_time_estimator,
)
from ..libraries.maimaidx_platform import (
    billing_user_id,
    foreign_recall_notice,
    get_event_group_id,
    parse_at_target_id,
    platform_user_id,
    plugin_finish,
    rank_text_image,
    recall_message,
    resolve_score_qqid,
    use_qq_mode,
)
from ..libraries.maimaidx_playcount_db import pc_db
from ..libraries.maimaidx_playcount_fetcher import playcount_fetcher
from ..libraries.maimaidx_prober_compare import (
    SYNC_WARN_FISH,
    awmc_differs_from_prober,
    sync_warning_for_source,
)
from ..libraries.maimaidx_qrcode_util import (
    DIRECT_QRCODE_PREFIX_PATTERN,
    extract_sgwcmaid_qrcode,
    extract_sgwcmaid_from_image_segments,
    qrcode_log_preview,
)
from ..libraries.maimaidx_user_operation import (
    finish_account_operation,
    try_begin_account_operation,
)

update_pc = on_command('更新pc数', aliases={'更新PC数', '同步pc数', '同步PC数', '绑定机台', '登录机台'})
my_pc = on_command('我的pc数', aliases={'我的PC数', '我的pc', '我的PC'})
pc_rank = on_command('pc排行', aliases={'PC排行', 'pc数排行', 'PC数排行', 'pc全部排行', 'PC全部排行'})
pc_detail = on_command('pc数', aliases={'PC数'})
pc50 = on_command('pc50', aliases={'PC50', '嫖娼50'})
pca50 = on_command('pca50', aliases={'PCA50', '嫖娼a50'})
pc_rank50 = on_command('游玩排行50', aliases={'游玩PC50', 'PC游玩50', 'pc游玩50'})
setattr(update_pc, '_maimaidx_serial_user_operation', True)

# user_id -> group_id；关机时可据此通知对应群
_waiting_qrcode: dict[int, object] = {}
_qrcode_auto_dedupe: dict[tuple[int, str], float] = {}
_qrcode_auto_processing: set[int] = set()
_qrcode_retry_state: dict[int, tuple[int, float]] = {}
_QRCODE_AUTO_DEDUPE_SECONDS = 60
_QRCODE_RETRY_WINDOW_SECONDS = 180


def drain_waiting_qrcode_sessions() -> list[tuple[int, int]]:
    """取出并清空所有「等待二维码」会话，供关机通知使用。"""
    items = list(_waiting_qrcode.items())
    _waiting_qrcode.clear()
    _qrcode_retry_state.clear()
    _qrcode_auto_processing.clear()
    return items


# 仅拦截“直接发送”的 SGWCMAID/官方二维码链接；显式命令仍交给命令处理器。
# 优先级 1 + block=True 确保先撤回敏感凭据，再做任何外部请求。
qrcode_auto_listener = on_regex(
    DIRECT_QRCODE_PREFIX_PATTERN,
    flags=re.IGNORECASE,
    priority=1,
    block=True,
)
setattr(qrcode_auto_listener, '_maimaidx_announcement_exempt', True)

# 普通图片静默扫码；仅识别到有效舞萌二维码后才接管并处理消息。
image_qrcode_auto_listener = on_message(priority=2, block=False)
setattr(image_qrcode_auto_listener, '_maimaidx_deferred_audit', True)
setattr(image_qrcode_auto_listener, '_maimaidx_announcement_exempt', True)

async def get_at_qq(message: MessageEvent) -> Optional[int]:
    target = parse_at_target_id(message)
    if target is None:
        return None
    return resolve_score_qqid(message, target)


async def check_feature(bot: Bot, event: GroupMessageEvent):
    if not feature_manager.is_enabled(event.group_id, 'query'):
        await bot.send(event, message=MessageSegment.reply(event.message_id) + MessageSegment.text('本群查询功能已关闭'))
        raise IgnoredException('功能未开启')


async def _send_progress(bot: Bot, event: MessageEvent, text: str):
    try:
        await bot.send(event, message=MessageSegment.text(text))
    except Exception:
        pass


async def _recall_sensitive_qrcode_message(
    bot: Bot, event: MessageEvent, *, timeout_seconds: float = 3.0
) -> bool:
    """尽快撤回二维码原消息；OneBot 无响应时不得拖住后续反馈。"""
    return await recall_message(
        bot, event, timeout_seconds=timeout_seconds, foreign=True
    )


# ============================================================================
# 更新PC数（绑定二维码）
# ============================================================================

@update_pc.handle()
async def handle_update_pc(bot: Bot, event: GroupMessageEvent):
    """处理「更新pc数」命令，引导用户发送机台二维码。"""
    await check_feature(bot, event)

    qqid = billing_user_id(event)

    if not playcount_fetcher.sdgb_available:
        await update_pc.finish(
            MessageSegment.reply(event.message_id)
            + MessageSegment.text(
                '\nPC数功能未配置。\n'
                '请在 .env 中配置 AWMC API：\n'
                '  team：AWMC_API_MODE=team + AWMC_API_BASE_URL + SDGBT_CLIENT_ID\n'
                '  public：AWMC_API_MODE=public + AWMC_PUBLIC_GATEWAY_TOKEN\n'
                '配置后重启 Bot 即可使用。'
            )
        )

    # 账号功能合并后，已执行「mai绑定」的用户无需重复发送二维码。
    from ..libraries.maimaidx_account_db import account_db
    from ..libraries.maimaidx_platform import billing_user_id

    account_qqid = billing_user_id(event)
    binding = account_db.get(str(account_qqid))
    if binding and binding.qrcode:
        from .mai_account import _sgid_cache_state

        cache_valid, _ = _sgid_cache_state(binding)
        if cache_valid:
            await _handle_sdgb_update(
                bot,
                event,
                account_qqid,
                binding.qrcode,
                matcher=update_pc,
                retry_on_cached_failure=True,
                success_builder=lambda count, total: (
                    f'\n✅ 已使用绑定账号同步 PC 数据！\n'
                    f'- 收录谱面: {count} 个\n- 总游玩次数: {total} 次'
                ),
            )
            return

    await update_pc.send(
        MessageSegment.reply(event.message_id)
        + MessageSegment.text(
            '\n请发送你的机台二维码数据。\n\n'
            '获取方式：打开微信中的「舞萌DX | 中二节奏」玩家二维码，\n'
            '长按二维码并选择「识别图中二维码」，\n'
            '复制识别出的 SGWCMAID 字符或官方网页地址，\n'
            '直接发送给 Bot 即可。\n\n'
            '⚠️ 请注意保护好你的二维码数据，不要发给他人。'
        )
    )
    _waiting_qrcode[qqid] = get_event_group_id(event)


@update_pc.receive()
async def receive_qrcode(bot: Bot, event: GroupMessageEvent):
    """接收用户发送的二维码数据。"""
    qqid = billing_user_id(event)

    if qqid not in _waiting_qrcode:
        return

    _waiting_qrcode.pop(qqid, None)

    raw_text = event.get_plaintext()
    qrcode_data = extract_sgwcmaid_qrcode(raw_text)
    if not qrcode_data:
        await update_pc.finish(
            MessageSegment.reply(event.message_id)
            + MessageSegment.text('二维码格式错误，请发送 SGWCMAID 或官方二维码链接。')
        )

    log.info(
        f'[SDGBPC] 用户 {qqid} 手动提交二维码 qrcode={qrcode_log_preview(qrcode_data)}'
    )
    await _handle_sdgb_update(bot, event, qqid, qrcode_data, matcher=update_pc)


def _default_pc_success_message(count: int, total_plays: int) -> str:
    return (
        f'\n✅ 机台成绩已同步到 AWMC！\n'
        f'- 收录谱面: {count} 个\n'
        f'- 总游玩次数: {total_plays} 次\n\n'
        f'使用「pc50」查看按次数排序的 B50\n'
        f'使用「pca50」查看 B50 内按 PC 排序\n'
        f'使用「游玩排行50」查看游玩最多的 50 首谱面\n'
        f'使用「我的pc数」查看详细数据\n'
        f'使用「pc排行」查看全部用户 PC 排行\n\n'
        f'⚠️ b50 等成绩指令以查分器数据为准，'
        f'如需更新请使用 maiu（水鱼）/ maiul（落雪）上传成绩。'
    )


async def _build_auto_qrcode_success_message(event: MessageEvent, storage_qqid: int) -> str:
    """扫码自动同步成功后的提示。

    b50 以查分器数据为准：与查分器比对——
    一致：查分器最新数据已刷入玩家缓存，告知可直接使用 b50；
    不一致：提示用 maiu/maiul 上传，并清除玩家缓存（见 awmc_differs_from_prober），
    保证上传完成后 b50 拉取查分器最新数据。
    """
    base_msg = '🤔 AWMC已根据您提供的二维码同步机台成绩（可使用 pc50 / 我的pc数 等指令）。'
    try:
        score_qqid = resolve_score_qqid(event)
        source = get_user_source(score_qqid)
        if await awmc_differs_from_prober(
            score_qqid,
            storage_qqid=storage_qqid,
            source=source,
        ):
            base_msg += '\n' + sync_warning_for_source(source)
        else:
            base_msg += '\n✅ 机台成绩与查分器一致，可直接使用 b50 等指令。'
    except QBindRequiredError:
        base_msg += '\n' + SYNC_WARN_FISH
    return base_msg


async def _sync_sdgb_qrcode(
    qqid: int, qrcode_data: str, *, save_qrcode: bool = True
) -> int:
    """扫码同步机台成绩到 AWMC 本地库（供 pc 类指令使用）。

    b50 等成绩指令始终以查分器（水鱼/落雪）数据为准，机台数据不写入玩家缓存。
    同步完成后清除该用户的玩家缓存，保证之后的 b50 拉取查分器最新数据。
    """
    await _verify_or_auto_bind_account(
        qqid, qrcode_data, save_qrcode=save_qrcode
    )
    success = await playcount_fetcher.login_by_sdgb(qrcode_data, qqid)
    if not success:
        raise RuntimeError('凭据保存失败')
    count = await playcount_fetcher.fetch_via_sdgb_with_retry(qqid)
    from ..libraries.maimaidx_awmcnet_sync import sync_awmcnet_pc_records
    from ..libraries.maimaidx_account_db import account_db
    binding = account_db.get(str(qqid))
    await sync_awmcnet_pc_records(
        qqid,
        pc_db.get_user_play_counts(qqid),
        nickname=binding.user_name if binding else '',
        rating=binding.rating if binding else None,
        play_count=pc_db.get_user_total_plays(qqid),
    )
    from ..libraries.maimaidx_player_cache import invalidate_player_cache
    invalidate_player_cache(qqid)
    return count


async def _verify_or_auto_bind_account(
    qqid: int, qrcode_data: str, *, save_qrcode: bool = True
):
    """校验二维码；无完整绑定时自动绑定/认领，已有绑定时仅安全刷新。"""
    from ..libraries.maimaidx_account_db import account_db
    from .mai_account import _bind_verified_account, _read_verified_preview

    binding = account_db.get(str(qqid))
    if binding and binding.qrcode:
        refreshed, _ = await _read_verified_preview(
            binding, qrcode_data, save_qrcode=save_qrcode
        )
        return refreshed
    refreshed, _ = await _bind_verified_account(str(qqid), qrcode_data)
    return refreshed


def _qrcode_dedupe_hit(qqid: int, qrcode_data: str) -> bool:
    key = (qqid, qrcode_data[:48])
    now = time.time()
    last = _qrcode_auto_dedupe.get(key, 0)
    if now - last < _QRCODE_AUTO_DEDUPE_SECONDS:
        return True
    _qrcode_auto_dedupe[key] = now
    if len(_qrcode_auto_dedupe) > 500:
        cutoff = now - _QRCODE_AUTO_DEDUPE_SECONDS
        stale = [k for k, ts in _qrcode_auto_dedupe.items() if ts < cutoff]
        for k in stale:
            del _qrcode_auto_dedupe[k]
    return False


def _qrcode_retry_failed(qqid: int) -> tuple[int, bool]:
    now = time.time()
    previous, deadline = _qrcode_retry_state.get(qqid, (0, 0.0))
    attempt = 1 if now > deadline else previous + 1
    exhausted = attempt >= 3
    if exhausted:
        _qrcode_retry_state.pop(qqid, None)
    else:
        _qrcode_retry_state[qqid] = (attempt, now + _QRCODE_RETRY_WINDOW_SECONDS)
    return attempt, exhausted


def _qrcode_retry_succeeded(qqid: int, qrcode_data: str) -> None:
    _qrcode_retry_state.pop(qqid, None)
    # 失败时去掉去重标记，成功后才保留 60 秒防重复请求。
    _qrcode_auto_dedupe[(qqid, qrcode_data[:48])] = time.time()


def _qrcode_retry_release_dedupe(qqid: int, qrcode_data: str) -> None:
    _qrcode_auto_dedupe.pop((qqid, qrcode_data[:48]), None)


async def _handle_sdgb_update(
    bot: Bot,
    event: GroupMessageEvent,
    qqid: int,
    qrcode_data: str,
    *,
    matcher: Matcher = update_pc,
    retry_on_cached_failure: bool = False,
    success_builder: Optional[Callable[[int, int], str]] = None,
):
    """通过 sw-api 更新 PC 数据"""
    try:
        async with machine_session():
            count = await _sync_sdgb_qrcode(
                qqid, qrcode_data, save_qrcode=not retry_on_cached_failure
            )
    except Exception as e:
        log.error(
            f'[SDGBPC] 用户 {qqid} 拉取失败 qrcode={qrcode_log_preview(qrcode_data)}: {e}'
        )
        from ..libraries.maimaidx_account_db import account_db

        account_db.mark_qrcode_result(str(qqid), False)
        if retry_on_cached_failure:
            _waiting_qrcode[qqid] = get_event_group_id(event)
            await matcher.send(
                MessageSegment.reply(event.message_id)
                + MessageSegment.text(
                    '\n🔄 缓存二维码已失效，请重新获取。\n'
                    '打开微信中的「舞萌DX | 中二节奏」玩家二维码，长按选择'
                    '「识别图中二维码」，复制字符或网页地址发送给 Bot。'
                )
            )
            return
        await matcher.finish(
            MessageSegment.reply(event.message_id)
            + MessageSegment.text(f'数据拉取失败: {e}。请检查 sw-api 服务或稍后重试。')
        )
        return

    total_plays = pc_db.get_user_total_plays(qqid)
    log.info(
        f'[SDGBPC] 用户 {qqid} 更新成功 records={count} total_plays={total_plays} '
        f'qrcode={qrcode_log_preview(qrcode_data)}'
    )
    if success_builder is None:
        msg = _default_pc_success_message(count, total_plays)
    else:
        msg = success_builder(count, total_plays)

    await matcher.finish(
        MessageSegment.reply(event.message_id) + MessageSegment.text(msg)
    )


@qrcode_auto_listener.handle()
async def _auto_qrcode_update(bot: Bot, event: MessageEvent):
    """直发 SGWCMAID/官方链接自动处理。"""
    qrcode_data = extract_sgwcmaid_qrcode(event.get_plaintext())
    if not qrcode_data:
        return
    await _process_auto_qrcode(bot, event, qrcode_data, source='text')


@image_qrcode_auto_listener.handle()
async def _auto_image_qrcode_update(
    matcher: Matcher, bot: Bot, event: MessageEvent
):
    """静默识别图片二维码；仅有效舞萌二维码会进入账号更新流程。"""
    if not bool(getattr(maiconfig, 'awmc_image_qrcode_enabled', True)):
        return
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(
        event.group_id, 'query'
    ):
        return
    if not any(segment.type == 'image' for segment in event.message):
        return
    try:
        qrcode_data = await extract_sgwcmaid_from_image_segments(
            event.message,
            max_bytes=max(
                1024,
                int(
                    getattr(
                        maiconfig, 'awmc_image_qrcode_max_bytes', 8 * 1024 * 1024
                    )
                ),
            ),
        )
    except Exception as exc:
        log.warning(f'[QrcodeImage] 图片二维码识别失败：{type(exc).__name__}')
        return
    if not qrcode_data:
        return
    from ..libraries.maimaidx_admin_audit import admin_audit

    ref = admin_audit.start_trace(
        command='image_qrcode',
        user_id=str(event.get_user_id()),
        group_id=str(getattr(event, 'group_id', '') or ''),
        matcher='mai_playcount.image_qrcode_auto_listener',
        input_summary={'source': 'image', 'segment_types': ['image']},
    )
    token = admin_audit.set_current_ref(ref)
    try:
        try:
            await _process_auto_qrcode(bot, event, qrcode_data, source='image')
        except Exception as exc:
            admin_audit.finish_trace(ref, 'error', error=exc)
            raise
        else:
            trace = admin_audit.get_trace(ref)
            if trace and trace.get('status') == 'running':
                admin_audit.finish_trace(ref, 'success')
    finally:
        admin_audit.reset_current_ref(token)
        matcher.stop_propagation()


async def _process_auto_qrcode(
    bot: Bot,
    event: MessageEvent,
    qrcode_data: str,
    *,
    source: str,
):
    """统一处理文字、链接及图片识别出的舞萌凭据。"""
    # 图片/文本一旦识别为敏感凭据就优先撤回；OneBot 偶发不返回时最多等 3 秒，
    # 随后立刻告知用户手动撤回，不能让撤回动作卡住整条同步链路。
    recalled = await _recall_sensitive_qrcode_message(bot, event)
    if not recalled and not use_qq_mode(event):
        try:
            await asyncio.wait_for(react_processing(bot, event), timeout=2.0)
        except asyncio.TimeoutError:
            log.warning('[QrcodeAuto] 处理表情响应超时（2.00s）')
        try:
            await bot.send(event, foreign_recall_notice(event))
        except Exception:
            pass

    recall_warning = (
        f'{foreign_recall_notice(event)}\n'
        if not recalled and use_qq_mode(event)
        else (
            '⚠️ Bot 无法撤回原凭据消息，请立即手动撤回。\n'
            if not recalled
            else ''
        )
    )
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, 'query'):
        if recall_warning:
            await bot.send(event, recall_warning.strip())
        return

    qqid = int(billing_user_id(event))
    if not try_begin_account_operation(qqid):
        await bot.send(
            event,
            message=MessageSegment.text(
                recall_warning
                + '⏳ 账号操作仍在处理中，请稍候查看上一条结果后再发送二维码。'
            ),
        )
        return
    await _send_progress(
        bot, event, recall_warning + '✅ 已收到二维码，正在验证并处理，请稍候。'
    )
    try:
        await _process_auto_qrcode_for_account(
            bot,
            event,
            qrcode_data,
            source=source,
            qqid=qqid,
            recalled=recalled,
            recall_warning=recall_warning,
        )
    finally:
        finish_account_operation(qqid)


async def _process_auto_qrcode_for_account(
    bot: Bot,
    event: MessageEvent,
    qrcode_data: str,
    *,
    source: str,
    qqid: int,
    recalled: bool,
    recall_warning: str,
):
    """Process an identified QR code while its account guard is held."""
    from ..libraries.maimaidx_account_db import account_db
    from .mai_account import (
        _has_lxns_oauth,
        auto_upload_channels,
        continue_pending_account_retry,
        continue_ticket_with_qrcode,
        remember_pending_account_retry,
        remember_pending_ticket_retry,
        take_awmcnet_first_sync_notice,
        take_pending_account_retry,
        take_pending_ticket_retry,
    )

    prefix = (
        MessageSegment.at(event.user_id) + MessageSegment.text('\n')
        if isinstance(event, GroupMessageEvent)
        else Message()
    )
    recall_status = (
        '🔒 原凭据消息已自动撤回。'
        if recalled
        else '⚠️ Bot 无法撤回原凭据消息，请立即手动撤回。'
    )
    pending_ticket = take_pending_ticket_retry(str(qqid))
    pending_account = take_pending_account_retry(str(qqid))
    if qqid in _waiting_qrcode:
        _waiting_qrcode.pop(qqid, None)
    if pending_ticket is None and pending_account is None and _qrcode_dedupe_hit(qqid, qrcode_data):
        log.info(
            f'[QrcodeAuto] 跳过重复请求 group={getattr(event, "group_id", "private")} qq={qqid} '
            f'qrcode={qrcode_log_preview(qrcode_data)}'
        )
        await bot.send(
            event,
            message=prefix + MessageSegment.text(
                '✅ 已识别舞萌二维码。\n'
                + recall_status
                + '\n检测到一分钟内重复提交，本次不再重复处理。'
            ),
        )
        return
    previous = account_db.get(str(qqid))
    pc_enabled = bool(playcount_fetcher.sdgb_available)
    _qrcode_auto_processing.add(qqid)
    t0 = time.perf_counter()
    log.info(
        f'[QrcodeAuto] 开始同步 source={source} group={getattr(event, "group_id", "private")} qq={qqid} '
        f'qrcode={qrcode_log_preview(qrcode_data)}'
    )
    had_binding = bool(previous and previous.qrcode)
    count: Optional[int] = None
    try:
        try:
            async with machine_session():
                if playcount_fetcher.sdgb_available:
                    count = await _sync_sdgb_qrcode(qqid, qrcode_data)
                else:
                    await _verify_or_auto_bind_account(qqid, qrcode_data)

                from .mai_account import _upload

                binding = account_db.get(str(qqid))
                fish, lxns = auto_upload_channels(
                    fish_token=binding.fish_token if binding else '',
                    lxns_token=binding.lxns_token if binding else '',
                    has_lxns_oauth=_has_lxns_oauth(event),
                )
                actual_workflow_key = auto_qrcode_workflow_key(
                    pc=pc_enabled, fish=fish, lxns=lxns
                )
                upload_result: Optional[str] = await _upload(
                    event,
                    fish=fish,
                    lxns=lxns,
                    qrcode_arg=qrcode_data,
                    _machine_locked=True,
                    _qrcode_verified=True,
                )
        except MachineBusyError as exc:
            if pending_ticket is not None:
                remember_pending_ticket_retry(str(qqid), pending_ticket[0], expires_at=pending_ticket[1])
            if pending_account is not None:
                remember_pending_account_retry(
                    str(qqid), pending_account[0], pending_account[1], expires_at=pending_account[2]
                )
            from .mai_account import _log

            ref = _log(
                str(qqid),
                'auto_qrcode',
                'error',
                f'source={source},error=MachineBusyError',
            )
            from ..libraries.maimaidx_admin_audit import admin_audit

            admin_audit.finish_trace(ref, 'error', error=exc)
            log.warning(
                f'[QrcodeAuto] 机台繁忙 source={source} qq={qqid} '
                f'({time.perf_counter() - t0:.2f}s): {exc}'
            )
            await bot.send(
                event,
                recall_warning + f'{exc}\n请稍后再发送最新二维码。\nRef_ID: {ref}',
            )
            return
        except Exception as exc:
            if pending_ticket is not None:
                remember_pending_ticket_retry(str(qqid), pending_ticket[0], expires_at=pending_ticket[1])
            if pending_account is not None:
                remember_pending_account_retry(
                    str(qqid), pending_account[0], pending_account[1], expires_at=pending_account[2]
                )
            _qrcode_retry_release_dedupe(qqid, qrcode_data)
            account_db.mark_qrcode_result(str(qqid), False)
            attempt, exhausted = _qrcode_retry_failed(qqid)
            from .mai_account import _log

            ref = _log(
                str(qqid),
                'auto_qrcode',
                'error',
                f'source={source},error={type(exc).__name__}',
            )
            from ..libraries.maimaidx_admin_audit import admin_audit

            admin_audit.finish_trace(ref, 'error', error=exc)
            log.error(
                f'[QrcodeAuto] 同步失败 source={source} '
                f'group={getattr(event, "group_id", "private")} qq={qqid} '
                f'qrcode={qrcode_log_preview(qrcode_data)} '
                f'({time.perf_counter() - t0:.2f}s): {exc}'
            )
            if exhausted:
                retry_text = (
                    f'二维码验证已连续失败 3 次（{type(exc).__name__}），流程已结束。\n'
                    '请返回舞萌页面重新获取最新二维码，稍后再直接发送。\n'
                    f'Ref_ID: {ref}'
                )
            else:
                retry_text = (
                    f'二维码无效、已过期或服务暂时不可用（{type(exc).__name__}）。\n'
                    '请在 3 分钟内重新发送 SGWCMAID、官方链接或二维码图片'
                    f'（{attempt}/3）。\nRef_ID: {ref}'
                )
            await bot.send(event, recall_warning + retry_text)
            return

        _qrcode_retry_succeeded(qqid, qrcode_data)

        from .mai_account import _log

        lines: list[str] = []
        if recall_warning:
            lines.append(recall_warning.strip())
        if not had_binding:
            player = f'：{binding.user_name}' if binding and binding.user_name else ''
            lines.append(f'✅ 已自动绑定舞萌账号{player}')
        else:
            lines.append('✅ 已更新舞萌账号二维码凭据')
        if count is not None:
            total_plays = pc_db.get_user_total_plays(qqid)
            lines.extend([
                '✅ 已同步 PC 数据',
                f'收录谱面：{count} 个 · 总游玩：{total_plays} PC',
            ])
        else:
            lines.append('AWMC PC 服务尚未配置，本次已跳过 PC 同步。')
        if upload_result is not None:
            lines.append(upload_result)
            first_notice = take_awmcnet_first_sync_notice(
                str(qqid), upload_result
            )
            if first_notice:
                lines.append(first_notice)
        else:
            lines.append('AWMCNET 上传未完成，请检查服务配置后重试。')
        if pending_ticket is not None:
            async def notify(message: str) -> None:
                await bot.send(event, message=prefix + MessageSegment.text(message))

            ticket_result = await continue_ticket_with_qrcode(
                event, qrcode_data, pending_ticket, notify=notify
            )
            lines.append('📨 自动继续发票：\n' + ticket_result)
        if pending_account is not None:
            account_result = await continue_pending_account_retry(
                event, qrcode_data, pending_account
            )
            lines.append('🔁 自动继续原操作：\n' + account_result)
        ref = _log(
            str(qqid),
            'auto_qrcode',
            'success',
            f'source={source},auto_bound={not had_binding},pc={count is not None}',
        )
        # _upload 已带自己的审计 Ref_ID 时不重复展示；扫码流程仍保留独立审计记录。
        if not (upload_result and 'Ref_ID:' in str(upload_result)):
            lines.append(f'Ref_ID: {ref}')
        msg = '\n'.join(lines)
        record_label = str(count) if count is not None else 'skipped'
        log.info(
            f'[QrcodeAuto] 同步成功 source={source} '
            f'group={getattr(event, "group_id", "private")} qq={qqid} records={record_label} '
            f'({time.perf_counter() - t0:.2f}s) qrcode={qrcode_log_preview(qrcode_data)}'
        )
        processing_time_estimator.record(
            actual_workflow_key, time.perf_counter() - t0
        )
        # 拓展数据集：扫码后后台落盘（已上传则 maintenance 也会写；此处覆盖仅 PC/未上传）
        try:
            from ..libraries.maimaidx_dataset_archive import (
                collect_archive_qqids,
                schedule_dataset_archive,
            )
            from ..libraries.maimaidx_platform import resolve_score_qqid

            score_qq = None
            try:
                score_qq = int(resolve_score_qqid(event))
            except Exception:
                score_qq = None
            archive_ids = collect_archive_qqids(qqid, score_qq)
            uploaded_ok = bool(
                upload_result and str(upload_result).startswith('上传完成')
            )
            # 上传成功由 _post_upload_maintenance 落盘；此处覆盖仅扫码/PC 同步
            if archive_ids and not uploaded_ok:
                schedule_dataset_archive(
                    archive_ids,
                    fish=bool(fish),
                    lxns=bool(lxns),
                    source='share_qrcode',
                    delay=1.0,
                )
        except Exception as exc:
            log.warning(
                f'[QrcodeAuto] 调度数据集落盘失败 qq={qqid}: '
                f'{type(exc).__name__}: {exc}'
            )
        prefix = (
            MessageSegment.at(event.user_id) + MessageSegment.text('\n')
            if isinstance(event, GroupMessageEvent)
            else Message()
        )
        await bot.send(event, message=prefix + MessageSegment.text(msg))
    finally:
        _qrcode_auto_processing.discard(qqid)


@my_pc.handle()
async def handle_my_pc(bot: Bot, event: GroupMessageEvent):
    """处理「我的pc数」命令，展示用户PC数统计。"""
    await check_feature(bot, event)

    qqid = billing_user_id(event)

    records = pc_db.get_user_play_counts(qqid)
    if not records:
        await my_pc.finish(
            MessageSegment.reply(event.message_id)
            + MessageSegment.text('你还没有PC数据，请先使用「更新pc数」命令登录机台并同步数据。')
        )

    total_plays = pc_db.get_user_total_plays(qqid)
    top_15 = sorted(records, key=lambda r: r.play_count, reverse=True)[:15]

    lines = [
        f'总谱面数: {len(records)} 个',
        f'总游玩次数: {total_plays} 次',
        '',
        '游玩最多的15个谱面:',
    ]
    for i, r in enumerate(top_15, 1):
        title = r.title if r.title else f'#{r.song_id}'
        lines.append(f'{i:2}. {title} [{r.level}] - {r.play_count} 次')

    msg = '\n'.join(lines)
    await my_pc.finish(MessageSegment.reply(event.message_id) + MessageSegment.text(msg))


@pc_rank.handle()
async def handle_pc_rank(bot: Bot, event: GroupMessageEvent):
    """处理「pc排行」命令，展示全部已同步 PC 数据的用户排行。"""
    await check_feature(bot, event)

    all_users = pc_db.get_all_users_with_data()
    if not all_users:
        await plugin_finish(
            pc_rank,
            '暂无PC排行数据，请先使用「更新pc数」同步数据。',
            event=event,
            reply_message=True,
        )

    user_stats = []
    for uid in all_users:
        total = pc_db.get_user_total_plays(uid)
        records = pc_db.get_user_play_counts(uid)
        user_stats.append((uid, total, len(records)))

    user_stats.sort(key=lambda x: x[1], reverse=True)

    lines = [f'PC全部排行（共 {len(user_stats)} 人）:']
    for i, (uid, total, count) in enumerate(user_stats, 1):
        lines.append(f'{i:2}. QQ:{uid} - PC: {total} 次 ({count} 谱面)')

    msg = '\n'.join(lines)
    await plugin_finish(
        pc_rank,
        rank_text_image(msg),
        event=event,
        reply_message=True,
    )


@pc_detail.handle()
async def handle_pc_detail(bot: Bot, event: GroupMessageEvent, arg: Message = CommandArg()):
    """处理「pc数 歌曲名/ID」命令，展示指定歌曲的PC数。"""
    await check_feature(bot, event)

    keyword = arg.extract_plain_text().strip()
    if not keyword:
        await pc_detail.finish(
            MessageSegment.reply(event.message_id)
            + MessageSegment.text('请输入歌曲名或ID，例如: pc数 1231')
        )

    target_qqid = resolve_score_qqid(event)
    at_qq = await get_at_qq(event)
    if at_qq:
        target_qqid = at_qq

    records = pc_db.get_user_play_counts(target_qqid)
    if not records:
        await pc_detail.finish(
            MessageSegment.reply(event.message_id)
            + MessageSegment.text(f'QQ:{target_qqid} 还没有PC数据。')
        )

    matched = []
    for r in records:
        title_lower = (r.title or '').lower()
        keyword_lower = keyword.lower()
        try:
            if int(keyword) == r.song_id:
                matched.append(r)
                continue
        except ValueError:
            pass
        if keyword_lower in title_lower:
            matched.append(r)

    if not matched:
        await pc_detail.finish(
            MessageSegment.reply(event.message_id)
            + MessageSegment.text(f'未找到与「{keyword}」匹配的PC记录。')
        )

    matched.sort(key=lambda r: r.level_index)

    title_display = matched[0].title if matched[0].title else f'#{matched[0].song_id}'
    lines = [f'{title_display} 的PC数:']
    for r in matched:
        lines.append(f'  {r.level} - {r.play_count} 次')

    msg = '\n'.join(lines)
    await pc_detail.finish(MessageSegment.reply(event.message_id) + MessageSegment.text(msg))


@pc50.handle()
async def handle_pc50(
    event: MessageEvent,
    user_id: Optional[int] = Depends(get_at_qq)
):
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, 'score'):
        raise IgnoredException('功能已禁用')
    qqid = user_id or resolve_score_qqid(event)
    from ..libraries.maimaidx_break import break_billing, take_break_charge_footer
    from ..libraries.maimaidx_error import BreakInsufficientError
    try:
        async with break_billing(event.user_id):
            result = await generate_pc50(qqid)
    except BreakInsufficientError as e:
        await pc50.finish(str(e), reply_message=True)
        return
    charge = take_break_charge_footer()
    if charge and not isinstance(result, str):
        result = result + MessageSegment.text('\n' + '\n'.join(charge))
    await pc50.finish(result, reply_message=True)


@pca50.handle()
async def handle_pca50(
    event: MessageEvent,
    user_id: Optional[int] = Depends(get_at_qq)
):
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, 'score'):
        raise IgnoredException('功能已禁用')
    qqid = user_id or resolve_score_qqid(event)
    from ..libraries.maimaidx_break import break_billing, take_break_charge_footer
    from ..libraries.maimaidx_error import BreakInsufficientError
    try:
        async with break_billing(event.user_id):
            result = await generate_pca50(qqid)
    except BreakInsufficientError as e:
        await pca50.finish(str(e), reply_message=True)
        return
    charge = take_break_charge_footer()
    if charge and not isinstance(result, str):
        result = result + MessageSegment.text('\n' + '\n'.join(charge))
    await pca50.finish(result, reply_message=True)


@pc_rank50.handle()
async def handle_pc_rank50(
    event: MessageEvent,
    user_id: Optional[int] = Depends(get_at_qq)
):
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, 'score'):
        raise IgnoredException('功能已禁用')
    qqid = user_id or resolve_score_qqid(event)
    from ..libraries.maimaidx_break import break_billing, take_break_charge_footer
    from ..libraries.maimaidx_error import BreakInsufficientError
    try:
        async with break_billing(event.user_id):
            result = await generate_pc_rank50(qqid)
    except BreakInsufficientError as e:
        await pc_rank50.finish(str(e), reply_message=True)
        return
    charge = take_break_charge_footer()
    if charge and not isinstance(result, str):
        result = result + MessageSegment.text('\n' + '\n'.join(charge))
    await pc_rank50.finish(result, reply_message=True)
