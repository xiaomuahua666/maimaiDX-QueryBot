"""QueryBot 运行时审计、封禁拦截与管理员 REF_ID 指令。"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

from nonebot import get_driver, on_command, on_message
from nonebot.adapters import Bot, Event
from nonebot.adapters.onebot.v11 import Message, MessageEvent
from nonebot.exception import IgnoredException
from nonebot.matcher import Matcher
from nonebot.message import run_postprocessor, run_preprocessor
from nonebot.params import CommandArg, Depends
from nonebot.typing import T_State

from ..config import log, maiconfig
from ..libraries.maimaidx_admin_audit import admin_audit, redact
from ..libraries.maimaidx_bot_admin import PLUGIN_ADMIN_ONLY, is_plugin_admin
from ..libraries.maimaidx_platform import (
    billing_user_id,
    get_event_group_id,
    parse_at_target_id,
)
from ..libraries.maimaidx_error import QBindRequiredError
from ..libraries.maimaidx_break import (
    break_db,
    format_break_insufficient_message,
    is_superuser_exempt,
)
from ..libraries.maimaidx_request_rate import request_meter
from ..libraries.maimaidx_user_operation import (
    finish_account_operation,
    try_begin_account_operation,
)


audit_query = on_command("查询REF", aliases={"查询Ref_ID", "ref查询"}, permission=PLUGIN_ADMIN_ONLY)
ban_user_cmd = on_command("封禁用户", aliases={"封禁"}, permission=PLUGIN_ADMIN_ONLY)
unban_user_cmd = on_command("解封用户", aliases={"解封"}, permission=PLUGIN_ADMIN_ONLY)
ban_list_cmd = on_command("封禁列表", permission=PLUGIN_ADMIN_ONLY)
admin_web_cmd = on_command("管理面板", aliases={"WebUI"}, permission=PLUGIN_ADMIN_ONLY)

_message_recorder = on_message(priority=99, block=False)
setattr(_message_recorder, "_maimaidx_passive_recorder", True)
_ban_notified: dict[str, float] = {}
_debt_notified: dict[str, float] = {}
_DEBT_NOTICE_COOLDOWN_SECONDS = 300
_MESSAGE_STATS_FLUSH_SECONDS = 2.0
_message_stats_pending: dict[tuple[str, str], tuple[int, float]] = {}
_message_stats_flush_task: Optional[asyncio.Task] = None


async def _flush_message_stats(*, delay: bool = True) -> None:
    """将普通群消息计数合并为单事务，并在线程中落盘。"""
    global _message_stats_flush_task
    cancelled = False
    pending: dict[tuple[str, str], tuple[int, float]] = {}
    try:
        if delay:
            await asyncio.sleep(_MESSAGE_STATS_FLUSH_SECONDS)
        if not _message_stats_pending:
            return
        pending = _message_stats_pending.copy()
        _message_stats_pending.clear()
        rows = [
            (group_id, user_id, count, last_at)
            for (group_id, user_id), (count, last_at) in pending.items()
        ]
        await asyncio.to_thread(admin_audit.record_messages, rows)
    except asyncio.CancelledError:
        cancelled = True
        raise
    except Exception as exc:
        for key, (count, last_at) in pending.items():
            queued_count, queued_at = _message_stats_pending.get(key, (0, 0.0))
            _message_stats_pending[key] = (
                queued_count + count,
                max(queued_at, last_at),
            )
        log.warning(f"群消息统计批量落盘失败：{type(exc).__name__}: {exc}")
    finally:
        _message_stats_flush_task = None
        if _message_stats_pending and not cancelled:
            _message_stats_flush_task = asyncio.create_task(
                _flush_message_stats(), name="maimaidx-message-stats-flush"
            )


@get_driver().on_startup
async def _cleanup_admin_audit() -> None:
    retention_days = int(getattr(maiconfig, "maimaidx_audit_retention_days", 90))
    result = await asyncio.to_thread(admin_audit.cleanup, retention_days)
    if any(result.values()):
        log.info(f'管理审计过期数据已清理：{result}')


@get_driver().on_shutdown
async def _flush_admin_audit_on_shutdown() -> None:
    global _message_stats_flush_task
    task = _message_stats_flush_task
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    _message_stats_flush_task = None
    await _flush_message_stats(delay=False)


def _matcher_module_name(matcher: Matcher) -> str:
    """解析 matcher 所在模块名。

    NoneBot 2.5+ 的 ``matcher.module`` 是 ``ModuleType``，``str(module)`` 形如
    ``<module 'pkg.mai_guess' from '...'>``，不能再用 ``endswith('.mai_guess')``。
    应优先读 ``module_name`` / ``__name__``。
    """
    for obj in (matcher, type(matcher)):
        name = getattr(obj, "module_name", None)
        if name:
            return str(name)
    mod = getattr(matcher, "module", None)
    if mod is None:
        mod = getattr(type(matcher), "module", None)
    if isinstance(mod, str):
        return mod
    name = getattr(mod, "__name__", None) if mod is not None else None
    return str(name) if name else ""


def _plugin_matcher(matcher: Matcher) -> bool:
    module = _matcher_module_name(matcher)
    plugin_name = str(getattr(matcher, "plugin_name", "") or "")
    # ModuleType 的 repr 仍可能带 maimaidx；兼容旧路径。
    raw_module = str(getattr(matcher, "module", "") or "")
    blob = f"{module} {plugin_name} {raw_module}".lower()
    return "maimaidx" in blob


def _busy_surcharge_exempt(matcher: Matcher) -> bool:
    """猜歌模块是免费奖励玩法，整模块不参与高负载 BREAK 附加费。"""
    if bool(getattr(type(matcher), "_maimaidx_busy_surcharge_exempt", False)):
        return True
    module = _matcher_module_name(matcher)
    return module.endswith(".mai_guess") or module.endswith(".mai_letter")


def _serial_user_operation(matcher: Matcher) -> bool:
    return bool(
        getattr(type(matcher), "_maimaidx_serial_user_operation", False)
    )


def _debt_exempt(matcher: Matcher) -> bool:
    return bool(getattr(type(matcher), "_maimaidx_debt_exempt", False))


def _release_user_operation(state: T_State) -> None:
    key = state.pop("__maimaidx_serial_user_operation", None)
    if key is not None:
        finish_account_operation(key)


def _command_name(matcher: Matcher, event: Event, state: T_State) -> str:
    prefix = state.get("_prefix") or {}
    command = prefix.get("command") if isinstance(prefix, dict) else None
    if isinstance(command, (tuple, list)):
        command = "".join(str(item) for item in command)
    if command:
        return str(command)[:200]
    module = _matcher_module_name(matcher) or str(
        getattr(matcher, "module", "") or ""
    )
    try:
        text = event.get_plaintext().strip()
    except Exception:
        text = ""
    label = text.split(maxsplit=1)[0][:40] if text else "message"
    return f"{module.rsplit('.', 1)[-1]}:{label}"


def _event_summary(event: Event) -> dict:
    try:
        message = event.get_message()
        segment_types = [str(getattr(seg, "type", "unknown")) for seg in message]
    except Exception:
        segment_types = []
    try:
        text_len = len(event.get_plaintext())
        request_text = str(redact(event.get_plaintext()))[:500]
    except Exception:
        text_len = 0
        request_text = ""
    return {
        "request": request_text,
        "message_length": text_len,
        "segment_types": segment_types[:30],
    }


def _event_request_key(bot: Bot, event: Event) -> str:
    event_id = getattr(event, "message_id", None) or getattr(event, "id", None)
    if event_id is None:
        event_id = f"object-{id(event)}"
    return f"{getattr(bot, 'self_id', '')}:{event_id}"


@_message_recorder.handle()
async def _(event: Event):
    global _message_stats_flush_task
    if not bool(getattr(maiconfig, "maimaidx_message_stats_enabled", True)):
        return
    gid = get_event_group_id(event)
    if gid is None:
        return
    key = (str(gid), str(event.get_user_id()))
    count, _ = _message_stats_pending.get(key, (0, 0.0))
    _message_stats_pending[key] = (count + 1, time.time())
    if _message_stats_flush_task is None or _message_stats_flush_task.done():
        _message_stats_flush_task = asyncio.create_task(
            _flush_message_stats(), name="maimaidx-message-stats-flush"
        )


@run_preprocessor
async def _audit_and_ban_preprocessor(
    matcher: Matcher, bot: Bot, event: Event, state: T_State
):
    if not _plugin_matcher(matcher):
        return
    # 全量消息统计只计数，不为每条普通聊天创建 REF 链路，也不触发封禁提示。
    if type(matcher) is _message_recorder or bool(
        getattr(type(matcher), "_maimaidx_passive_recorder", False)
    ):
        return
    # 图片扫码监听会匹配所有图片；只有真正识别为二维码后才进入账号流程。
    # 普通图片不能被当成功能调用，更不能触发欠费提示。
    if bool(getattr(type(matcher), "_maimaidx_deferred_audit", False)):
        return
    uid = str(event.get_user_id())
    ban_keys = [uid]
    try:
        billing_key = str(billing_user_id(event))
        if billing_key not in ban_keys:
            ban_keys.append(billing_key)
    except Exception:
        pass
    def find_active_ban():
        return next(
            (row for key in ban_keys if (row := admin_audit.get_active_ban(key))),
            None,
        )

    ban = await asyncio.to_thread(find_active_ban)
    if ban and not is_plugin_admin(uid):
        event_key = str(getattr(event, "message_id", "") or getattr(event, "id", "") or f"{uid}:{int(time.time())}")
        now = time.time()
        if now - _ban_notified.get(event_key, 0) > 10:
            _ban_notified[event_key] = now
            reason = str(ban.get("reason") or "违反使用规则")
            expires = ban.get("expires_at")
            expire_text = (
                time.strftime("%Y-%m-%d %H:%M", time.localtime(float(expires)))
                if expires else "永久"
            )
            try:
                await bot.send(event, f"你已被禁止使用 maimai 功能。\n原因：{reason}\n到期：{expire_text}")
            except Exception:
                pass
        raise IgnoredException("maimaidx user banned")

    try:
        payer = int(billing_user_id(event))
        balance = break_db.get_balance(payer)
        if balance < 0 and not is_plugin_admin(uid) and not _debt_exempt(matcher):
            now = time.time()
            debt_key = str(payer)
            if now - _debt_notified.get(debt_key, 0) > _DEBT_NOTICE_COOLDOWN_SECONDS:
                _debt_notified[debt_key] = now
                try:
                    await bot.send(
                        event,
                        f"当前 BREAK 余额为 {balance}，已暂停其他功能。\n"
                        "请先通过 AWMC签到、今日舞萌或抢红包将余额补回非负数。",
                    )
                except Exception:
                    pass
            raise IgnoredException("negative BREAK balance")
    except IgnoredException:
        raise
    except Exception:
        log.warning('[BREAK] 欠费检查失败，跳过', exc_info=True)

    if _serial_user_operation(matcher):
        try:
            operation_key = str(billing_user_id(event))
        except QBindRequiredError:
            # Let the platform matcher guard send the common qbind prompt.
            # A missing mapping must not abort the preprocessor first.
            operation_key = None
        if operation_key is not None:
            if not try_begin_account_operation(operation_key):
                # 同一账号只允许一个账号流程。重复请求静默丢弃，
                # 避免用“等待/已受理”之类过程消息刷屏。
                raise IgnoredException("serial user operation already active")
            state["__maimaidx_serial_user_operation"] = operation_key

    busy_surcharge_exempt = _busy_surcharge_exempt(matcher)
    try:
        if (
            bool(getattr(maiconfig, "maimaidx_busy_surcharge_enabled", True))
            and not busy_surcharge_exempt
        ):
            window = max(
                1.0, float(getattr(maiconfig, "maimaidx_busy_window_seconds", 60.0))
            )
            free_requests = max(
                0, int(getattr(maiconfig, "maimaidx_busy_free_requests", 30))
            )
            surcharge = max(
                0, int(getattr(maiconfig, "maimaidx_busy_surcharge_break", 1))
            )
            request_count = request_meter.record(
                _event_request_key(bot, event), window_seconds=window
            )
            if request_count is not None and request_count > free_requests and surcharge:
                payer = int(billing_user_id(event))
                if not is_superuser_exempt(payer):
                    meta = {
                        "window_seconds": window,
                        "free_requests": free_requests,
                        "request_count": request_count,
                    }
                    from ..libraries.maimaidx_card import card_manager
                    freedom = card_manager.freedom_active(payer)
                    if not break_db.try_consume(
                        payer, surcharge, "busy_request_surcharge", meta=meta
                    ):
                        balance = break_db.get_balance(payer)
                        await bot.send(
                            event,
                            "当前使用人数较多，本次请求需额外支付 "
                            f"{surcharge} BREAK。\n"
                            + format_break_insufficient_message(payer, surcharge, balance),
                        )
                        _release_user_operation(state)
                        raise IgnoredException("maimaidx busy surcharge insufficient")
                    if freedom:
                        meta = {**meta, "charged": 0, "freedom": True}
                    state["__maimaidx_busy_charge"] = {
                        **meta,
                        "charged": 0 if freedom else surcharge,
                        "balance": break_db.get_balance(payer),
                    }
    except IgnoredException:
        raise
    except Exception:
        log.warning('[BREAK] 拥堵计费检查失败，跳过', exc_info=True)

    ref_id = await asyncio.to_thread(
        admin_audit.start_trace,
        command=_command_name(matcher, event, state),
        user_id=uid,
        group_id=str(get_event_group_id(event) or ""),
        matcher=str(getattr(matcher, "module", "") or ""),
        input_summary=_event_summary(event),
    )
    state["__maimaidx_ref_id"] = ref_id
    state["__maimaidx_ref_token"] = admin_audit.set_current_ref(ref_id)
    busy_charge = state.get("__maimaidx_busy_charge")
    if busy_charge:
        await asyncio.to_thread(
            admin_audit.add_step,
            "break.busy_surcharge",
            "success",
            busy_charge,
            ref_id=ref_id,
        )


@run_postprocessor
async def _audit_postprocessor(
    matcher: Matcher,
    exception: Optional[Exception],
    event: Event,
    state: T_State,
):
    _release_user_operation(state)
    ref_id = state.get("__maimaidx_ref_id")
    if not ref_id:
        return
    trace = await asyncio.to_thread(admin_audit.get_trace, str(ref_id))
    if trace and trace.get("status") != "running":
        token = state.get("__maimaidx_ref_token")
        if token is not None:
            try:
                admin_audit.reset_current_ref(token)
            except Exception:
                pass
        return
    normal_control = {
        "FinishedException", "PausedException", "RejectedException", "SkippedException",
        "StopPropagation",
    }
    if exception is None or type(exception).__name__ in normal_control:
        await asyncio.to_thread(admin_audit.finish_trace, str(ref_id), "success")
    elif isinstance(exception, IgnoredException):
        await asyncio.to_thread(admin_audit.finish_trace, str(ref_id), "ignored")
    else:
        await asyncio.to_thread(
            admin_audit.finish_trace, str(ref_id), "error", error=exception
        )
    token = state.get("__maimaidx_ref_token")
    if token is not None:
        try:
            admin_audit.reset_current_ref(token)
        except Exception:
            pass


def _at_user(event: MessageEvent) -> Optional[str]:
    return parse_at_target_id(event)


@audit_query.handle()
async def _(args: Message = CommandArg()):
    ref_id = args.extract_plain_text().strip().upper()
    if not ref_id:
        await audit_query.finish("用法：查询REF REF-XXXXXXXXXXXXXXXX")
    if not ref_id.startswith("REF-"):
        ref_id = "REF-" + ref_id
    trace = admin_audit.get_trace(ref_id)
    if not trace:
        await audit_query.finish("没有找到该 REF_ID。")
    lines = [
        f"REF_ID：{trace['ref_id']}",
        f"状态：{trace['status']}  耗时：{trace.get('duration_ms') or 0}ms",
        f"用户：{trace.get('user_id') or '-'}  群：{trace.get('group_id') or '私聊'}",
        f"命令：{trace.get('command')}",
    ]
    if trace.get("error_type"):
        lines.append(f"错误：{trace['error_type']} · {trace.get('error_message') or ''}")
    lines.append("处理链路：")
    for index, step in enumerate(trace.get("steps") or [], 1):
        lines.append(
            f"{index}. {step['step_name']} [{step['status']}] {step.get('duration_ms') or 0}ms"
        )
        if step.get("detail"):
            lines.append("   " + str(step["detail"])[:500])
    if not trace.get("steps"):
        lines.append("（该请求没有外部调用步骤）")
    await audit_query.finish("\n".join(lines))


@ban_user_cmd.handle()
async def _(
    event: MessageEvent,
    args: Message = CommandArg(),
    at_user: Optional[str] = Depends(_at_user),
):
    parts = args.extract_plain_text().strip().split()
    target = at_user
    if target is None and parts:
        target = parts.pop(0)
    if not target:
        await ban_user_cmd.finish("用法：封禁用户 @用户 [小时，0=永久] [原因]")
    hours = 0.0
    if parts:
        try:
            hours = max(0.0, float(parts[0]))
            parts.pop(0)
        except ValueError:
            pass
    reason = " ".join(parts).strip() or "管理员封禁"
    expires_at = time.time() + hours * 3600 if hours > 0 else None
    admin_audit.ban_user(target, reason, str(event.get_user_id()), expires_at=expires_at)
    await ban_user_cmd.finish(
        f"已封禁 {target}\n期限：{'永久' if expires_at is None else f'{hours:g} 小时'}\n原因：{redact(reason)}"
    )


@unban_user_cmd.handle()
async def _(
    args: Message = CommandArg(), at_user: Optional[str] = Depends(_at_user)
):
    target = at_user or args.extract_plain_text().strip().split(maxsplit=1)[0]
    if not target:
        await unban_user_cmd.finish("用法：解封用户 @用户")
    changed = admin_audit.unban_user(target)
    await unban_user_cmd.finish("已解除封禁。" if changed else "该用户当前没有生效中的封禁。")


@ban_list_cmd.handle()
async def _():
    rows = admin_audit.list_bans(active_only=True)
    if not rows:
        await ban_list_cmd.finish("当前没有被封禁的用户。")
    lines = [f"当前封禁 {len(rows)} 人："]
    for row in rows[:50]:
        expires = row.get("expires_at")
        end = time.strftime("%m-%d %H:%M", time.localtime(expires)) if expires else "永久"
        lines.append(f"{row['user_id']} · {end} · {row['reason']}")
    await ban_list_cmd.finish("\n".join(lines))


@admin_web_cmd.handle()
async def _():
    if not bool(getattr(maiconfig, "maimaidx_admin_web_enabled", False)):
        await admin_web_cmd.finish("管理 WebUI 尚未启用，请设置 MAIMAIDX_ADMIN_WEB_ENABLED=true。")
    public_url = str(getattr(maiconfig, "maimaidx_admin_web_public_url", "") or "").rstrip("/")
    path = str(getattr(maiconfig, "maimaidx_admin_web_path", "/maimaidx/admin") or "/maimaidx/admin")
    if public_url:
        await admin_web_cmd.finish(public_url + path)
    port = int(getattr(maiconfig, "maimaidx_admin_web_port", 8099) or 0)
    if port > 0:
        host = str(getattr(maiconfig, "maimaidx_admin_web_host", "127.0.0.1") or "127.0.0.1")
        await admin_web_cmd.finish(f"http://{host}:{port}{path}")
    await admin_web_cmd.finish(f"管理面板路径：{path}（WebUI 使用 NoneBot 共享端口）")
