"""由 Koishi maibot 移植的账号绑定与查分器上传命令。

账号功能现在与 QueryBot 共用配置、进程和 SQLite 数据目录；BREAK 仍由
原有 ``mai_break`` 模块管理。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any, Optional

import httpx
from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageEvent, MessageSegment
from nonebot.matcher import Matcher
from nonebot.params import Arg, CommandArg

from ..config import log, maiconfig
from ..libraries.maimaidx_account_db import AccountBinding, account_db
from ..libraries.maimaidx_admin_audit import admin_audit, redact
from ..libraries.maimaidx_break import break_db
from ..libraries.maimaidx_api_data import maiApi
from ..libraries.maimaidx_divingfish_oauth import (
    get_access_token as get_divingfish_access_token,
    oauth_enabled as divingfish_oauth_enabled,
)
from ..libraries.maimaidx_error import (
    BreakInsufficientError,
    DivingFishNotAuthorizedError,
    DivingFishOAuthError,
    QBindRequiredError,
)
from ..libraries.maimaidx_group_rating import build_forward_node
from ..libraries.maimaidx_lxns_client import (
    LxnsApiError,
    convert_pc_records_to_lxns_scores,
    convert_pc_records_to_divingfish_scores,
    convert_sega_music_scores_to_divingfish,
    convert_sega_music_scores,
    user_upload_scores,
)
from ..libraries.maimaidx_lxns_db import lxns_db
from ..libraries.maimaidx_machine_session import (
    MachineBusyError,
    machine_session,
)
from ..libraries.maimaidx_music import mai
from ..libraries.maimaidx_platform import (
    billing_user_id,
    plugin_finish,
    require_account_qqid,
    plugin_send,
    resolve_score_qqid,
)
from ..libraries.maimaidx_playcount_db import pc_db
from ..libraries.maimaidx_qrcode_util import (
    extract_sgwcmaid_from_image_segments,
    extract_sgwcmaid_qrcode,
)
from ..libraries.maimaidx_pending_session import finish_pending, session_key, track_event
from ..libraries.maimaidx_processing_time import (
    format_processing_estimate,
    processing_time_estimator,
    upload_fallback_seconds,
    upload_workflow_key,
)
from ..libraries.maimaidx_reaction import react_processing
from ..libraries.maimaidx_status_api import build_live_status_payload
from ..libraries.maimaidx_sw_api import (
    SwApiError,
    find_sw_api_error,
    format_sw_api_quota_error,
    format_user_region_block,
    is_sw_api_quota_error,
    sw_api,
)
from .mai_agreement import agreement_prompt, has_user_agreed

account_help = on_command("mai账号", aliases={"账号帮助", "mai账户"})
account_bind = on_command("mai绑定", aliases={"绑定舞萌", "舞萌绑定", "maibind"})
account_unbind = on_command("mai解绑", aliases={"解绑舞萌", "舞萌解绑"})
account_status = on_command("mai状态", aliases={"mymai"})
maimai_live_status = on_command("舞萌状态", aliases={"mais"})
_fish_bind_aliases = {"绑定水鱼token", "绑定水鱼上传", "maibindfish"}
fish_bind = on_command(
    "mai绑定水鱼", aliases=_fish_bind_aliases
)
fish_unbind = on_command("mai解绑水鱼", aliases={"解绑水鱼token"})
lx_upload_bind = on_command(
    "mai绑定落雪",
    aliases={"mai绑定落雪token", "绑定落雪token", "lxuploadbind", "maibindlx"},
)
lx_upload_unbind = on_command(
    "mai解绑落雪", aliases={"mai解绑落雪token", "解绑落雪token", "lxuploadunbind"}
)
upload_fish = on_command("maiu", aliases={"mai上传B50", "上传水鱼", "导"})
upload_lx = on_command("maiul", aliases={"mai上传落雪b50", "上传落雪"})
upload_all = on_command("maiua", aliases={"同时上传b50", "全部上传b50"})
account_ping = on_command("maiping", aliases={"mai连接测试"})
account_ticket = on_command("mai发票", aliases={"发票", "fp", "拿票"})
account_ticket_status = on_command("mai查票", aliases={"查票"})
account_region = on_command("mai地图", aliases={"游玩地图"})
account_preview = on_command("mai预览", aliases={"预览"})
account_items = on_command("mai道具", aliases={"道具"})
account_gate_status = on_command(
    "mai门状态", aliases={"mai查门", "查门", "门状态"}
)
account_game_event = on_command("maievent", aliases={"mai活动", "舞萌活动"})
account_music_upsert = on_command(
    "mai改成绩", aliases={"修改成绩", "改成绩", "改分"}
)
account_music_delete = on_command(
    "mai删成绩", aliases={"删除成绩", "删成绩", "删分"}
)
account_item_upsert = on_command("mai改道具", aliases={"修改道具", "改道具"})
account_opt = on_command("mai查询opt", aliases={"查询opt"})

_ACCOUNT_SHORTCUTS = (
    ('MyMai', 'mymai'),
    ('游玩地图', 'mai地图'),
    ('发票 ×2', 'mai发票 2'),
    ('查询票券', 'mai查票'),
    ('账号预览', 'mai预览'),
    ('查看道具', 'mai道具'),
    ('门状态', 'mai门状态'),
    ('活动事件', 'maievent'),
    ('修改成绩', 'mai改成绩'),
    ('修改道具', 'mai改道具'),
    ('上传水鱼', 'maiu'),
    ('上传落雪', 'maiul'),
    ('账号帮助', 'mai账号'),
    ('PC50', 'pc50'),
    ('我的 PC', '我的pc数'),
    ('更新 PC', '更新pc数'),
)

def _account_flow_shortcuts(event: MessageEvent) -> tuple[tuple[str, str], ...]:
    """Expose missing bindings once, then replace them with useful actions."""
    key = _user_key(event)
    binding = account_db.get(key)
    has_account = bool(binding and binding.qrcode)
    oauth_mode = divingfish_oauth_enabled()
    has_fish = bool(binding and binding.fish_token and not oauth_mode)
    has_lxns = bool(
        (binding and binding.lxns_token) or _has_lxns_oauth(event)
    )
    buttons: list[tuple[str, str]] = []
    if not has_account:
        buttons.append(('绑定舞萌', 'mai绑定'))
    if oauth_mode:
        buttons.append(('授权水鱼', '绑定水鱼'))
    elif not has_fish:
        buttons.append(('绑定水鱼', 'mai绑定水鱼'))
    if not has_lxns:
        buttons.append(('绑定落雪', 'lxbind'))
    if has_account and (has_fish or oauth_mode) and has_lxns:
        buttons.append(('自动上传 B50', 'maiua'))
    buttons.extend([
        ('标准 B50', 'b50'),
        ('刷新 B50', '刷新b50'), ('PC50', 'pc50'),
        ('我的 PC', '我的pc数'), ('更新 PC', '更新pc数'),
        ('MyMai', 'mymai'),
    ])
    return tuple(buttons)
# Help remains available before qbind so users can discover the binding flow.
setattr(account_help, '_maimaidx_qbind_exempt', True)
# 涉及账号状态、外部上传或机台会话的命令按用户串行执行。
# 同一账号并发提交时静默拒绝后到的请求，不发送过程确认消息。
for _serial_account_matcher in (
    account_bind,
    account_unbind,
    account_status,
    fish_bind,
    fish_unbind,
    lx_upload_bind,
    lx_upload_unbind,
    upload_fish,
    upload_lx,
    upload_all,
    account_ping,
    account_ticket,
    account_ticket_status,
    account_region,
    account_preview,
    account_items,
    account_gate_status,
    account_game_event,
    account_music_upsert,
    account_music_delete,
    account_item_upsert,
    account_opt,
):
    setattr(_serial_account_matcher, '_maimaidx_serial_user_operation', True)
# 舞萌状态只读公开 Uptime 与全局失败率 API，不占用机台串行锁。

_RECALL_FAILED_NOTICE = "⚠️ Bot 无法撤回该凭据消息，请立即手动撤回。\n"
_QRCODE_RECALL_TIMEOUT_SECONDS = 3.0
_TICKET_QRCODE_RETRY_SECONDS = 180
_TICKET_TIMING_KEY = "ticket:processing_seconds"
_LEGACY_TICKET_TIMING_KEY = "ticket_queue:seconds_per_request"
_TICKET_AUTO_RETRIES = 2
_TICKET_IRREVERSIBLE_NOTICE = (
    "⚠️ 发票不可逆：票券一经发放必须上机使用，不可以屯票。"
)
# AWMC 发票接口是全局机台资源；即使不同用户同时发起，也必须逐张提交。
_ticket_queue_lock = asyncio.Lock()
_ticket_queue_waiting = 0
_pending_ticket_retries: dict[str, tuple[int, float]] = {}
_pending_account_retries: dict[str, tuple[str, dict[str, Any], float]] = {}
_DIVING_FISH_PROBER_URL = "https://www.diving-fish.com/maimaidx/prober/"
_FISH_TOKEN_MIN_LENGTH = 127
_post_upload_tasks: set[asyncio.Task] = set()
_FISH_TOKEN_MAX_LENGTH = 132
_ACCOUNT_SETUP_GUIDE = (
    "尚未建立账号记录，请按以下步骤完成：\n"
    "1. 发送最新的 SGWCMAID 字符串，Bot 会自动建档并上传 AWMCNET；\n"
    "2. 水鱼 / 落雪 OAuth 均为可选，授权后会额外同步对应平台；\n"
    "3. 之后再次发送二维码即可更新全部已绑定平台。"
)

_AWMCNET_FIRST_SYNC_NOTICE = (
    "AWMC NET. 已同步您的信息，您无需其他操作\n\n"
    "您可以在 https://net.wmc.pub 注册查询（使用您的QQ邮箱注册）\n\n"
    "如果需要水鱼或落雪，还需要绑定 maibindfish / maibindlx"
)
_AWMCNET_SYNCED_LINE = "已同步到 AWMC NET."


def take_awmcnet_first_sync_notice(user_key: str, upload_result: str) -> str:
    """Return the onboarding notice exactly once after a successful sync."""
    if _AWMCNET_SYNCED_LINE not in str(upload_result or ""):
        return ""
    if not account_db.mark_awmcnet_notified_once(str(user_key)):
        return ""
    return _AWMCNET_FIRST_SYNC_NOTICE


def _user_key(event: MessageEvent) -> str:
    return str(billing_user_id(event))


def _arg_text(args: Message) -> str:
    return args.extract_plain_text().strip()


async def _recall_qrcode_message(bot: Bot, event: MessageEvent) -> str:
    """限时撤回二维码消息，避免 OneBot 无响应时卡住后续上传。"""
    from ..libraries.maimaidx_platform import foreign_recall_notice, recall_message

    if await recall_message(
        bot, event, timeout_seconds=_QRCODE_RECALL_TIMEOUT_SECONDS, foreign=True
    ):
        return ""
    return f'{foreign_recall_notice(event)}\n'


def _mask(value: str, head: int = 5, tail: int = 4) -> str:
    if not value:
        return "未绑定"
    if len(value) <= head + tail:
        return "*" * len(value)
    return value[:head] + "…" + value[-tail:]


def _nested_preview(payload: dict) -> dict:
    for key in ("userData", "userPreview", "userPreviewData"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return payload


def _merged_preview(payload: dict) -> dict:
    """保留 userData 内字段，同时保留 maibot 兼容的顶层状态字段。"""
    nested = _nested_preview(payload)
    if nested is payload:
        return dict(payload)
    merged = dict(payload)
    merged.update(nested)
    # 新版 sw-api 会把 banState / returnCode 放在最外层。
    for key in ("BanState", "banState", "ReturnCode", "returnCode"):
        if payload.get(key) is not None:
            merged[key] = payload[key]
    return merged


def _pick(data: dict, *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = data.get(key)
        if value is not None:
            return value
    return default


def _normalize_preview(payload: dict) -> tuple[str, str, int, dict]:
    data = _merged_preview(payload)
    uid = _pick(payload, "userId", "UserID", "userID")
    if uid is None:
        uid = _pick(data, "userId", "UserID", "userID")
    if uid in (None, "", -1, "-1"):
        raise RuntimeError("二维码未能读取到有效舞萌账号")
    name = str(_pick(data, "userName", "UserName", default="") or "")
    rating_raw = _pick(data, "playerRating", "PlayerRating", "rating", "Rating", default=0)
    try:
        rating = int(float(rating_raw or 0))
    except (TypeError, ValueError):
        rating = 0
    return str(uid), name, rating, data


def _normalize_charge_payload(payload: dict) -> tuple[bool, list[dict], list[dict]]:
    """兼容 maibot 的新版 user/charge 顶层与 userCharge 包装格式。"""
    nested = payload.get("userCharge")
    data = nested if isinstance(nested, dict) else payload
    rows = _pick(data, "userChargeList", "UserChargeList")
    if rows is None:
        rows = _pick(payload, "userChargeList", "UserChargeList")
    free_rows = _pick(data, "userFreeChargeList", "UserFreeChargeList")
    if free_rows is None:
        free_rows = _pick(payload, "userFreeChargeList", "UserFreeChargeList")

    return_code = _pick(payload, "returnCode", "ReturnCode")
    if return_code is None:
        return_code = _pick(data, "returnCode", "ReturnCode")
    charge_status = _pick(data, "chargeStatus", "ChargeStatus")
    if charge_status is None:
        charge_status = _pick(payload, "chargeStatus", "ChargeStatus")
    user_id = _pick(payload, "userId", "UserID")
    has_new_response = user_id is not None and any(
        key in payload for key in ("userChargeList", "UserChargeList", "length", "Length")
    )
    success = charge_status in (True, 1, "1") or return_code in (1, "1") or has_new_response
    return (
        success,
        rows if isinstance(rows, list) else [],
        free_rows if isinstance(free_rows, list) else [],
    )


def _ticket_stock(rows: list[dict], charge_id: int) -> int:
    total = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_id = _pick(row, "chargeId", "ChargeId", "chargeID", "ChargeID")
        try:
            matches = int(raw_id) == int(charge_id)
        except (TypeError, ValueError):
            matches = False
        if not matches:
            continue
        try:
            total += max(0, int(_pick(row, "stock", "Stock", default=0) or 0))
        except (TypeError, ValueError):
            continue
    return total


def _unused_ticket_stocks(
    rows: list[dict], *, now: Optional[float] = None
) -> dict[int, int]:
    """返回仍未使用的 2/3/5 倍票库存。"""
    current = float(time.time() if now is None else now)
    valid_rows = [
        row
        for row in rows
        if isinstance(row, dict)
        and (
            _ticket_valid_timestamp(row) is None
            or _ticket_valid_timestamp(row) > current
        )
    ]
    return {
        charge_id: stock
        for charge_id in _allowed_ticket_multipliers()
        if (stock := _ticket_stock(valid_rows, charge_id)) > 0
    }


class UnusedTicketPenaltyError(RuntimeError):
    def __init__(self, stocks: dict[int, int]):
        self.stocks = stocks
        super().__init__("账号仍有未使用的倍率票")


class TicketQrcodeError(RuntimeError):
    """发票使用的二维码不可用；调用方可登记一次限时重试。"""


class TicketRetryableError(RuntimeError):
    """上游已明确本次未发票，可以安全重新提交。"""


class QrcodeRefreshRequiredError(RuntimeError):
    """账号操作已挂起，等待用户提交新的二维码凭据。"""


async def _run_ticket_with_retries(
    operation: Callable[[int], Awaitable[int]],
    *,
    notify: Optional[Callable[[str], Awaitable[Any]]] = None,
    max_retries: int = _TICKET_AUTO_RETRIES,
) -> tuple[int, int]:
    """执行发票，明确失败时最多额外重试 ``max_retries`` 次。"""
    retries = max(0, int(max_retries))
    total_attempts = retries + 1
    for attempt in range(1, total_attempts + 1):
        try:
            return await operation(attempt), attempt
        except TicketRetryableError as exc:
            if attempt >= total_attempts:
                final_error = TicketRetryableError(
                    f"{_exception_detail(exc)}；已自动重试 {retries} 次，仍失败"
                )
                if notify is not None:
                    try:
                        await notify(f"⚠️ 发票失败：{_exception_detail(final_error)}")
                    except Exception as notify_exc:
                        log.warning(
                            "[ticket] 发送最终失败通知失败："
                            f"{_exception_detail(notify_exc)}"
                        )
                raise final_error from exc
            log.warning(
                f"[ticket] 第 {attempt} 次明确失败，准备自动重试 "
                f"({attempt}/{retries})：{_exception_detail(exc)}"
            )
            if notify is not None:
                try:
                    await notify(
                        f"⚠️ 第 {attempt} 次发票失败，正在自动重试"
                        f"（{attempt}/{retries}）……"
                    )
                except Exception as notify_exc:
                    log.warning(
                        "[ticket] 发送自动重试通知失败，继续重试："
                        f"{_exception_detail(notify_exc)}"
                    )
            await asyncio.sleep(min(2.0, float(attempt)))
    raise AssertionError("unreachable")


def remember_pending_ticket_retry(
    user_key: str,
    multiple: int,
    *,
    expires_at: Optional[float] = None,
    now: Optional[float] = None,
) -> float:
    """登记待二维码恢复的发票请求，默认 180 秒后失效。"""
    current = time.time() if now is None else float(now)
    deadline = (
        current + _TICKET_QRCODE_RETRY_SECONDS
        if expires_at is None
        else float(expires_at)
    )
    if deadline > current:
        _pending_ticket_retries[str(user_key)] = (int(multiple), deadline)
    return deadline


def take_pending_ticket_retry(
    user_key: str, *, now: Optional[float] = None
) -> Optional[tuple[int, float]]:
    """原子取出仍有效的待发票请求，避免同一二维码被重复处理。"""
    pending = _pending_ticket_retries.pop(str(user_key), None)
    if pending is None:
        return None
    current = time.time() if now is None else float(now)
    if pending[1] <= current:
        return None
    return pending


def clear_pending_ticket_retry(user_key: str) -> None:
    _pending_ticket_retries.pop(str(user_key), None)


def remember_pending_account_retry(
    user_key: str,
    operation: str,
    payload: Optional[dict[str, Any]] = None,
    *,
    expires_at: Optional[float] = None,
) -> float:
    now = time.time()
    deadline = float(expires_at or (now + _TICKET_QRCODE_RETRY_SECONDS))
    if deadline > now:
        _pending_account_retries[str(user_key)] = (
            operation,
            dict(payload or {}),
            deadline,
        )
    return deadline


def take_pending_account_retry(
    user_key: str,
) -> Optional[tuple[str, dict[str, Any], float]]:
    pending = _pending_account_retries.pop(str(user_key), None)
    if pending is None or pending[2] <= time.time():
        return None
    return pending


def clear_pending_account_retry(user_key: str) -> None:
    _pending_account_retries.pop(str(user_key), None)


def _charge_payload_user_id(payload: dict) -> str:
    """提取 /user/charge 返回的 UID，用于防止跨账号误判到账。"""
    if not isinstance(payload, dict):
        return ""
    nested = payload.get("userCharge")
    data = nested if isinstance(nested, dict) else payload
    value = _pick(data, "userId", "UserID", "userID")
    if value is None:
        value = _pick(payload, "userId", "UserID", "userID")
    return str(value) if value not in (None, "") else ""


def _ticket_estimate() -> tuple[int, int]:
    """估算同步发票耗时；新计时键无样本时复用旧队列历史。"""
    fallback_seconds = max(
        1.0,
        float(getattr(maiconfig, "awmc_ticket_estimate_seconds", 80.0) or 80.0),
    )
    estimated, samples = processing_time_estimator.estimate(
        _TICKET_TIMING_KEY, fallback_seconds=fallback_seconds
    )
    if samples:
        return estimated, samples
    return processing_time_estimator.estimate(
        _LEGACY_TICKET_TIMING_KEY, fallback_seconds=fallback_seconds
    )


def _format_wait_duration(seconds: int) -> str:
    minutes, remain = divmod(max(1, int(seconds)), 60)
    if minutes and remain:
        return f"{minutes} 分 {remain} 秒"
    if minutes:
        return f"{minutes} 分钟"
    return f"{remain} 秒"


def _ticket_wait_message(estimated: int, samples: int) -> str:
    estimate_source = (
        f"根据最近 {samples} 次真实处理时间估算"
        if samples
        else "按单个请求约 80 秒估算"
    )
    return (
        "🎫 发票请求正在处理，"
        f"预计约 {_format_wait_duration(estimated)} 完成"
        f"（{estimate_source}）。\n"
        "Bot 会在接口返回并确认票券到账后才扣 BREAK。"
    )


def _ticket_queue_wait_message(ahead: int) -> str:
    """告知排队请求前方数量；数量至少为正在处理的那一张发票。"""
    return (
        "🎫 发票请求已进入队列，"
        f"前面还有 {max(1, int(ahead))} 个请求，请耐心等待。\n"
        "轮到后 Bot 会发送预计处理时间，并在确认到账后才扣 BREAK。"
    )


async def _confirm_ticket_delivery(
    qrcode: str,
    charge_id: int,
    mai_uid: str,
    baseline_stock: int,
) -> int:
    """同步发票成功后等待落库，并只查询一次真实票券库存。"""
    settlement_delay = max(
        0.0,
        float(getattr(maiconfig, "awmc_ticket_settlement_delay_seconds", 2.0) or 0.0),
    )
    if settlement_delay:
        await asyncio.sleep(settlement_delay)
    async with machine_session():
        current = await sw_api.get_user_charge(qrcode)
    current_uid = _charge_payload_user_id(current)
    if current_uid and current_uid != str(mai_uid):
        raise RuntimeError("到账复核返回了其他账号的数据；本次不扣 BREAK")
    charge_ok, rows, free_rows = _normalize_charge_payload(current)
    stock = _ticket_stock(rows + free_rows, charge_id) if charge_ok else baseline_stock
    if stock <= baseline_stock:
        raise RuntimeError(
            "发票接口已返回成功，但到账复核未增加："
            f"{charge_id} 倍票库存 {baseline_stock}→{stock}；"
            "可能是上游落库延迟，本次不扣 BREAK"
        )
    log.info(
        f"[ticket] 同步接口成功后已确认到账 uid={mai_uid} charge={charge_id} "
        f"stock={stock} baseline={baseline_stock}"
    )
    return stock


def _ticket_valid_timestamp(row: dict) -> Optional[float]:
    raw = _pick(row, "validDate", "ValidDate", "validUntil", "ValidUntil")
    if raw in (None, ""):
        return None
    normalized = str(raw).strip().replace(" ", "T")[:19]
    try:
        return time.mktime(time.strptime(normalized, "%Y-%m-%dT%H:%M:%S"))
    except (TypeError, ValueError):
        return None


def _format_ticket_status(payload: dict, *, now: Optional[float] = None) -> str:
    """只用票种、库存和有效期生成用户回复，绝不回显上游账号字段。"""
    ok, rows, free_rows = _normalize_charge_payload(payload)
    if not ok:
        raise RuntimeError("上游服务未返回有效的票券状态")
    current = float(time.time() if now is None else now)

    def collect(source: list[dict], *, free: bool) -> tuple[list[str], int]:
        lines: list[str] = []
        total = 0
        for row in source:
            if not isinstance(row, dict):
                continue
            try:
                stock = max(0, int(_pick(row, "stock", "Stock", default=0) or 0))
            except (TypeError, ValueError):
                stock = 0
            if stock <= 0:
                continue
            valid_ts = _ticket_valid_timestamp(row)
            if valid_ts is not None and valid_ts <= current:
                continue
            total += stock
            raw_id = _pick(row, "chargeId", "ChargeId", "chargeID", "ChargeID")
            try:
                charge_id = int(raw_id)
            except (TypeError, ValueError):
                charge_id = 0
            if free:
                label = f"免费 {charge_id} 倍票" if charge_id > 1 else "免费票券"
            else:
                label = f"{charge_id} 倍票" if charge_id > 0 else "倍率票券"
            line = f"· {label} × {stock}"
            if valid_ts is not None:
                line += time.strftime("\n  有效期至：%Y-%m-%d %H:%M", time.localtime(valid_ts))
            lines.append(line)
        return lines, total

    paid_lines, paid_total = collect(rows, free=False)
    free_lines, free_total = collect(free_rows, free=True)
    if not paid_lines and not free_lines:
        return "🎫 舞萌票券状态\n当前没有有效票券。"
    output = [f"🎫 舞萌票券状态\n有效票券共 {paid_total + free_total} 张"]
    if paid_lines:
        output.append("【倍率票】\n" + "\n".join(paid_lines))
    if free_lines:
        output.append("【免费票】\n" + "\n".join(free_lines))
    return "\n\n".join(output)


def _binding_or_error(event: MessageEvent) -> tuple[str, Optional[AccountBinding], Optional[str]]:
    key = _user_key(event)
    binding = account_db.get(key)
    if not binding or not binding.qrcode:
        return key, None, "尚未绑定舞萌账号，请先使用：mai绑定 SGWCMAID..."
    cache_valid, cache_label = _sgid_cache_state(binding)
    if not cache_valid:
        return key, None, _pending_qrcode_prompt(cache_label)
    return key, binding, None


def _binding_for_write_preflight(
    event: MessageEvent,
) -> tuple[str, Optional[AccountBinding], Optional[str]]:
    """交互写操作先允许用户填完参数，二维码新鲜度在最终提交时检查。"""
    key = _user_key(event)
    binding = account_db.get(key)
    if not binding or not binding.qrcode:
        return key, None, "尚未绑定舞萌账号，请先使用：mai绑定 SGWCMAID..."
    return key, binding, None


def _sgid_cache_seconds() -> int:
    return max(0, int(getattr(maiconfig, "awmc_sgid_cache_seconds", 600) or 0))


def _sgid_cache_state(binding: AccountBinding) -> tuple[bool, str]:
    if not binding.qrcode:
        return False, "未保存"
    if binding.last_qrcode_success == 0:
        return False, "上次使用失败，需刷新"
    ttl = _sgid_cache_seconds()
    if ttl <= 0:
        return False, "已关闭，每次重新获取"
    age = max(0, time.time() - float(binding.qrcode_updated_at or 0))
    if not binding.qrcode_updated_at or age >= ttl:
        return False, "已过期，需刷新"
    remaining = max(1, int((ttl - age + 59) // 60))
    return True, f"有效（约剩 {remaining} 分钟）"


def _status_qrcode_prompt(reason: str) -> str:
    return (
        f"🔄 {reason}\n"
        "请打开微信中的「舞萌DX | 中二节奏」玩家二维码，\n"
        "长按二维码并选择「识别图中二维码」，复制识别出的字符或网页地址发送给 Bot。\n"
        "支持 SGWCMAID、wq.wahlap.net 的 img/req 链接；发送「取消」可查看缓存资料。"
    )


def _pending_qrcode_prompt(reason: str, operation_label: str = "原操作") -> str:
    return (
        f"🔄 二维码缓存{reason}\n"
        "请直接发送最新 SGWCMAID（SGID）、官方二维码链接或二维码图片（180 秒内有效）。\n"
        f"验证后，Bot 会直接继续本次{operation_label}，不会同步成绩。"
    )


def _is_sgid_expired_error(exc: BaseException) -> bool:
    """识别上游 Chime 3002 等失效 SGID 响应，避免继续使用旧凭据。"""
    current: Optional[BaseException] = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        detail = str(current or "")
        lowered = detail.lower()
        if "chime" in lowered and (
            "3002" in lowered or "获取用户失败" in detail
        ):
            return True
        cause = current.__cause__ or current.__context__
        current = cause if isinstance(cause, BaseException) else None
    return False


def _raise_sgid_refresh_required(
    key: str,
    operation: str,
    payload: Optional[dict[str, Any]],
    label: str,
) -> None:
    account_db.mark_qrcode_result(key, False)
    remember_pending_account_retry(key, operation, payload or {})
    raise QrcodeRefreshRequiredError(
        _pending_qrcode_prompt("已过期，需刷新", label)
    )


async def _read_verified_preview(
    binding: AccountBinding,
    qrcode: str,
    *,
    save_qrcode: bool,
) -> tuple[AccountBinding, dict]:
    payload = await sw_api.get_user_data(qrcode)
    mai_uid, name, rating, data = _normalize_preview(payload)
    if binding.mai_uid and str(binding.mai_uid) != str(mai_uid):
        raise RuntimeError("二维码与当前绑定的舞萌账号不一致")
    if save_qrcode:
        account_db.save_verified_qrcode(
            binding.user_key,
            qrcode,
            mai_uid=mai_uid,
            user_name=name,
            rating=rating,
            preview=data,
        )
        # Any successful explicit refresh supersedes older, unrelated pending work.
        clear_pending_account_retry(binding.user_key)
        clear_pending_ticket_retry(binding.user_key)
    else:
        account_db.refresh_preview(
            binding.user_key,
            mai_uid=mai_uid,
            user_name=name,
            rating=rating,
            preview=data,
        )
        account_db.mark_qrcode_result(binding.user_key, True)
    refreshed = account_db.get(binding.user_key)
    if refreshed is None:
        raise RuntimeError("账号状态保存失败")
    return refreshed, data


async def _bind_verified_account(
    user_key: str, qrcode: str
) -> tuple[AccountBinding, list[str]]:
    """验真并绑定/认领账号，供显式绑定和直发二维码共用。"""
    preview = await sw_api.get_user_data(qrcode)
    mai_uid, name, rating, preview_data = _normalize_preview(preview)
    binding, claimed_keys = account_db.bind_verified(
        user_key,
        qrcode,
        mai_uid=mai_uid,
        user_name=name,
        rating=rating,
        preview=preview_data,
    )
    # 认领后令旧账号保存的 PC 登录凭据失效，避免继续访问同一舞萌账号。
    for old_key in claimed_keys:
        try:
            pc_db.delete_credential(int(old_key))
        except (TypeError, ValueError):
            continue
    return binding, claimed_keys


def _preview_line(data: dict, label: str, *keys: str) -> Optional[str]:
    value = _pick(data, *keys)
    if value in (None, ""):
        return None
    return f"{label}：{value}"


async def _render_account_status(
    event: MessageEvent,
    binding: AccountBinding,
    preview: Optional[dict] = None,
) -> str:
    data = preview or binding.preview
    _, cache_label = _sgid_cache_state(binding)
    lines = [
        "✅ 已绑定舞萌账号",
        f"绑定时间：{time.strftime('%Y-%m-%d %H:%M', time.localtime(binding.bound_at))}",
        f"二维码缓存：{cache_label}",
    ]
    try:
        balance = await asyncio.to_thread(
            break_db.get_balance, int(binding.user_key)
        )
        lines.append(f"BREAK 余额：{balance}")
    except ValueError:
        pass
    lines.extend(["", "📊 账号信息" + ("（缓存）" if not preview else "")])
    name = _pick(data, "userName", "UserName", default=binding.user_name or "未知") or "未知"
    rating = _pick(
        data, "playerRating", "PlayerRating", "rating", "Rating",
        default=binding.rating or "未知",
    )
    old_rating = _pick(data, "PlayerOldRating", "playerOldRating")
    new_rating = _pick(data, "PlayerNewRating", "playerNewRating")
    if old_rating is not None and new_rating is not None:
        rating = f"{rating}（{old_rating}+{new_rating}）"
    lines.extend([f"用户名：{name}", f"Rating：{rating}"])

    class_rank = _pick(data, "ClassRank", "classRank")
    course_rank = _pick(data, "CourseRank", "courseRank")
    if class_rank is not None and course_rank is not None:
        lines.append(f"友人对战等级：{class_rank}[{course_rank}]")
    fields = (
        ("总游玩次数", ("PlayCount", "playCount")),
        ("当前版本游玩次数", ("CurrentPlayCount", "currentPlayCount")),
        ("机台版本", ("RomVersion", "romVersion")),
        ("数据版本", ("DataVersion", "dataVersion")),
        ("上次登录", ("LastLoginDate", "lastLoginDate")),
        ("上次游玩", ("LastPlayDate", "lastPlayDate")),
        ("上次拼机", ("LastPairLoginDate", "lastPairLoginDate")),
        ("上次游玩区域", ("LastRegionName", "lastRegionName")),
        ("总觉醒次数", ("TotalAwake", "totalAwake")),
    )
    for label, keys in fields:
        line = _preview_line(data, label, *keys)
        if line:
            lines.append(line)
    ban_state = _pick(data, "BanState", "banState")
    ban_labels = {0: "正常", 1: "警告", 2: "封禁", "0": "正常", "1": "警告", "2": "封禁"}
    lines.append(f"封禁状态：{ban_labels.get(ban_state, '未知' if ban_state is None else ban_state)}")

    lines.append("")
    if await _has_divingfish_oauth(event):
        lines.append("🐟 水鱼查分/上传：OAuth 已授权（推荐）")
    elif binding.fish_token and divingfish_oauth_enabled():
        lines.append("🐟 水鱼：旧 Token 已停用，请发送「绑定水鱼」重新授权")
    elif binding.fish_token:
        lines.append("🐟 水鱼上传：Import-Token 已绑定")
    else:
        lines.append("🐟 水鱼查分/上传：未授权（发送「绑定水鱼」）")
    if _has_lxns_oauth(event):
        lines.append("❄️ 落雪上传：OAuth 已绑定")
    elif binding.lxns_token:
        lines.append("❄️ 落雪上传：兼容 Token 已绑定")
    else:
        lines.append("❄️ 落雪上传：未绑定（发送 lxbind）")
    if binding.last_upload_at:
        lines.append(
            "最近上传："
            + time.strftime("%Y-%m-%d %H:%M", time.localtime(binding.last_upload_at))
        )

    if binding.qrcode and sw_api.available:
        try:
            async with machine_session():
                charge = await sw_api.get_user_charge(binding.qrcode)
            charge_ok, _, _ = _normalize_charge_payload(charge)
            if not charge_ok:
                return_code = _pick(charge, "returnCode", "ReturnCode")
                suffix = f"（returnCode={return_code}）" if return_code is not None else ""
                lines.append(f"🎫 票券情况：获取失败{suffix}")
                return "\n".join(lines)
            # 与「mai查票」共用详细解析，展开倍率、库存和有效期。
            lines.extend(["", _format_ticket_status(charge)])
        except Exception as exc:
            log.warning(f"[AccountStatus] 获取票券失败：{type(exc).__name__}: {exc}")
            if _is_sgid_expired_error(exc):
                account_db.mark_qrcode_result(binding.user_key, False)
                lines.append("🎫 票券情况：二维码已过期，请重新发送最新 SGID")
                return "\n".join(lines)
            if is_sw_api_quota_error(exc):
                lines.append(f"🎫 票券情况：{_exception_detail(exc)}")
                return "\n".join(lines)
            lines.append("🎫 票券情况：暂时无法获取")
    return "\n".join(lines)


_ITEM_KIND_LABELS = {
    1: "姓名框",
    2: "称号",
    3: "头像",
    4: "收藏品",
    5: "乐曲解锁",
    6: "MASTER 谱面解锁",
    7: "Re:MASTER 谱面解锁",
    8: "乐曲解锁(高风险类型)",
    9: "角色",
    10: "搭档",
    11: "边框",
    12: "票券",
}
_HIDDEN_ITEM_KINDS = frozenset({15})

_ITEM_UPSERT_SUCCESS_NOTE = (
    "提示：提交后请主人检查账号内是否出现一条名为「MilK」、0 分的乐曲记录；"
    "看到这条记录即代表道具写入成功。\n"
    "Rating 可能会短暂显示异常，上机游玩一局后会自动重算。"
)
_COLLECTION_UPSERT_TICKET_WARNING = (
    "⚠️ 如果账号内还有票券，收藏品可能实际不会生效，但本次道具修改仍会扣费。\n"
    "若确认未成功：请先上机游玩清除票券，再次重试。"
)


def _format_user_preview(payload: dict) -> str:
    """格式化只读 preview，避免输出 userId、二维码等账号标识。"""
    data = _merged_preview(payload)
    lines = ["👤 舞萌账号预览"]
    fields = (
        ("用户名", ("userName", "UserName")),
        ("Rating", ("playerRating", "PlayerRating", "rating", "Rating")),
        ("友人对战等级", ("classRank", "ClassRank")),
        ("段位", ("courseRank", "CourseRank")),
        ("总游玩次数", ("playCount", "PlayCount")),
        ("当前版本游玩次数", ("currentPlayCount", "CurrentPlayCount")),
        ("上次游玩", ("lastPlayDate", "LastPlayDate")),
        ("上次游玩区域", ("lastRegionName", "LastRegionName")),
    )
    for label, keys in fields:
        line = _preview_line(data, label, *keys)
        if line:
            lines.append(line)
    if len(lines) == 1:
        lines.append("API 未返回可展示的预览字段。")
    return "\n".join(lines)


def _flatten_user_items(payload: Any) -> list[dict]:
    rows: list[dict] = []

    def walk(value: Any, inherited_kind: Any = None) -> None:
        if isinstance(value, list):
            for item in value:
                walk(item, inherited_kind)
            return
        if not isinstance(value, dict):
            return
        kind = _pick(value, "itemKind", "ItemKind", default=inherited_kind)
        item_id = _pick(value, "itemId", "ItemId", "itemID", "ItemID")
        if item_id is not None:
            try:
                if int(kind) in _HIDDEN_ITEM_KINDS:
                    return
            except (TypeError, ValueError):
                pass
            row = dict(value)
            if _pick(row, "itemKind", "ItemKind") is None and kind is not None:
                row["itemKind"] = kind
            rows.append(row)
            return
        for nested in value.values():
            if isinstance(nested, (dict, list)):
                walk(nested, kind)

    walk(payload)
    return rows


def _format_user_items(payload: Any) -> str:
    rows = _flatten_user_items(payload)
    if not rows:
        return "🎒 舞萌道具\n当前没有可展示的道具记录。"

    grouped: dict[int, list[dict]] = {}
    for row in rows:
        try:
            kind = int(_pick(row, "itemKind", "ItemKind", default=-1))
        except (TypeError, ValueError):
            kind = -1
        grouped.setdefault(kind, []).append(row)

    lines = [f"🎒 舞萌道具 · 共 {len(rows)} 条"]
    for kind in sorted(grouped):
        items = grouped[kind]
        label = _ITEM_KIND_LABELS.get(kind, f"未知类型 {kind}")
        ids = []
        for row in items[:12]:
            item_id = _pick(row, "itemId", "ItemId", "itemID", "ItemID")
            stock = _pick(row, "stock", "Stock")
            ids.append(f"{item_id}×{stock}" if stock not in (None, 1, "1") else str(item_id))
        suffix = f" 等 {len(items)} 项" if len(items) > len(ids) else ""
        lines.append(f"{label}（kind={kind}）：" + "、".join(ids) + suffix)
    return "\n".join(lines)


def _flatten_gate_status(payload: Any) -> list[dict]:
    rows: list[dict] = []

    def walk(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                walk(item)
            return
        if not isinstance(value, dict):
            return
        if _pick(value, "gateId", "GateId", "gateID", "GateID") is not None:
            rows.append(value)
            return
        for nested in value.values():
            if isinstance(nested, (dict, list)):
                walk(nested)

    walk(payload)
    return rows


def _format_gate_status(payload: Any) -> str:
    rows = _flatten_gate_status(payload)
    if not rows:
        return "🚪 Kaleidx 门状态\n当前没有可展示的门状态记录。"

    def yes_no(value: Any) -> str:
        return "是" if value in (True, 1, "1", "true", "True") else "否"

    rows.sort(
        key=lambda row: int(
            _pick(row, "gateId", "GateId", "gateID", "GateID", default=0)
        )
    )
    lines = [f"🚪 Kaleidx 门状态 · 共 {len(rows)} 门"]
    for row in rows:
        gate_id = _pick(row, "gateId", "GateId", "gateID", "GateID")
        try:
            gate_number = int(gate_id)
        except (TypeError, ValueError):
            gate_number = 0
        gate_name = _GATE_NAMES.get(gate_number, f"未知之门")
        found = _pick(row, "isGateFound", "IsGateFound")
        key_found = _pick(row, "isKeyFound", "IsKeyFound")
        cleared = _pick(row, "isClear", "IsClear")
        lines.append(
            f"{gate_name}（Gate {gate_id}）：发现 {yes_no(found)} · "
            f"钥匙 {yes_no(key_found)} · 通关 {yes_no(cleared)}"
        )
    return "\n".join(lines)


_GAME_EVENT_MAPPINGS = {
    26080511: "区域介绍公告",
    26080521: "乐曲 11811、11812、11813、11814",
    26080525: "Utage 谱面 111852、121852、131852、141852、151852、161852",
    26080531: "Map 550002（龙之区域4）",
    26080532: "Kaleidx Gate/Key/Course 6（红色之门）",
    26080541: "Map 550054（联动区域）",
    26080551: "Challenge 118130（关联乐曲 11813）",
    26090191: "Title 609000（WEC2026 称号）",
    24021661: "chargeId=5（付费 5 倍票券解放）",
}
_GAME_EVENT_DISPLAY_LIMIT = 12


def _game_event_rows(payload: Any) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    rows = payload.get("gameEventList") or payload.get("GameEventList") or []
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _game_event_timestamp(value: Any) -> Optional[float]:
    raw = str(value or "").strip().replace("T", " ")[:19]
    if not raw:
        return None
    try:
        return time.mktime(time.strptime(raw, "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return None


def _game_event_is_current(row: dict, now: float) -> bool:
    enabled = _pick(row, "enable", "Enable", default=1)
    if enabled in (False, 0, "0", "false", "False"):
        return False
    start = _game_event_timestamp(_pick(row, "startDate", "StartDate"))
    end = _game_event_timestamp(_pick(row, "endDate", "EndDate"))
    return (start is None or start <= now) and (end is None or now <= end)


def _format_user_game_event(payload: Any, *, now: Optional[float] = None) -> str:
    """只展示当前有效的重点活动，避免整份 businessData 刷屏。"""
    if not isinstance(payload, dict):
        raise RuntimeError("活动事件返回格式异常")
    rows = _game_event_rows(payload)
    current = float(time.time() if now is None else now)
    active_rows = [row for row in rows if _game_event_is_current(row, current)]
    mapped_rows = []
    for row in active_rows:
        event_id = _pick(row, "id", "Id", "eventId", "EventId")
        try:
            mapped = int(event_id) in _GAME_EVENT_MAPPINGS
        except (TypeError, ValueError):
            mapped = False
        if mapped:
            mapped_rows.append(row)
    candidates = mapped_rows or active_rows
    shown = candidates[:_GAME_EVENT_DISPLAY_LIMIT]
    lines = [
        "🎪 舞萌活动事件",
        f"事件类型：{_pick(payload, 'type', 'Type', default='未返回')}",
        f"共 {len(rows)} 条 · 当前有效 {len(active_rows)} 条 · 展示 {len(shown)} 条",
    ]
    if shown:
        lines.append("")
        for row in shown:
            event_id = _pick(row, "id", "Id", "eventId", "EventId", default="未知")
            try:
                mapping = _GAME_EVENT_MAPPINGS.get(int(event_id))
            except (TypeError, ValueError):
                mapping = None
            lines.append(f"· EVENT {event_id}" + (f"｜{mapping}" if mapping else ""))
            start = str(_pick(row, "startDate", "StartDate", default="") or "")[:10]
            end = str(_pick(row, "endDate", "EndDate", default="") or "")[:10]
            if start or end:
                lines.append(f"  {start or '未知'} ～ {end or '未知'}")
            disable_area = _pick(row, "disableArea", "DisableArea")
            if disable_area not in (None, ""):
                lines.append(f"  禁用区域：{disable_area}")
    else:
        lines.append("当前没有可展示的有效活动。")
    omitted = len(candidates) - len(shown)
    if omitted > 0:
        lines.append(f"另有 {omitted} 条有效事件未展示。")
    if mapped_rows and len(active_rows) > len(mapped_rows):
        lines.append(f"已隐藏 {len(active_rows) - len(mapped_rows)} 条未识别的有效事件。")
    return "\n".join(lines)


_GATE_NAMES = {
    1: "蓝色之门",
    2: "白色之门",
    3: "紫色之门",
    4: "黑色之门",
    5: "黄色之门",
    6: "红色之门",
    7: "棱镜塔",
    8: "表门",
    9: "希望之门",
    10: "里门",
}


_DIFFICULTY_LABELS = {
    0: "BASIC",
    1: "ADVANCED",
    2: "EXPERT",
    3: "MASTER",
    4: "Re:MASTER",
    10: "宴会场",
}
_DIFFICULTY_ALIASES = {
    "0": 0, "basic": 0, "bas": 0, "绿": 0, "绿色": 0,
    "1": 1, "advanced": 1, "adv": 1, "黄": 1, "黄色": 1,
    "2": 2, "expert": 2, "exp": 2, "红": 2, "红色": 2,
    "3": 3, "master": 3, "mas": 3, "紫": 3, "紫色": 3,
    "4": 4, "remaster": 4, "re:master": 4, "remas": 4, "白": 4, "白色": 4,
    "10": 10, "utage": 10, "宴": 10, "宴会场": 10,
}
_COMBO_ALIASES = {
    "none": "none", "无fc": "none", "fc": "fc", "fcp": "fcp",
    "fc+": "fcp", "ap": "ap", "app": "app", "ap+": "app",
}
_SYNC_ALIASES = {
    "none": "none", "无fs": "none", "fs": "fs", "fsp": "fsp",
    "fs+": "fsp", "fsd": "fsd", "fdx": "fsd", "fsdp": "fsdp",
    "fdxp": "fsdp", "sync": "sync",
}


def _parse_difficulty(value: str) -> Optional[int]:
    normalized = re.sub(r"[\s_-]+", "", str(value or "").strip().lower())
    return _DIFFICULTY_ALIASES.get(normalized)


def _resolve_account_music(query: str):
    text = str(query or "").strip()
    if not text:
        raise ValueError("请输入歌曲名、别名或歌曲 ID")
    if music := mai.total_list.by_id(text):
        return music
    if music := mai.total_list.by_title(text):
        return music
    lowered = text.lower()
    exact_titles = [m for m in mai.total_list if str(m.title).lower() == lowered]
    if len(exact_titles) == 1:
        return exact_titles[0]
    aliases = mai.total_alias_list.by_alias(lowered) or mai.total_alias_list.by_alias(text)
    song_ids = sorted({str(alias.SongID) for alias in (aliases or [])})
    matches = [mai.total_list.by_id(song_id) for song_id in song_ids]
    matches = [music for music in matches if music is not None]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        choices = "、".join(f"{music.id} {music.title}" for music in matches[:8])
        raise ValueError(f"歌曲别名不唯一，请改用歌曲 ID：{choices}")
    partial = [m for m in mai.total_list if lowered in str(m.title).lower()]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        choices = "、".join(f"{music.id} {music.title}" for music in partial[:8])
        raise ValueError(f"歌曲名不唯一，请改用歌曲 ID：{choices}")
    raise ValueError(f"未找到歌曲：{text}")


def _resolve_item_music(query: str, item_kind: int):
    """Resolve a song item and enforce DX/Re:MASTER unlock constraints."""
    music = _resolve_account_music(query)
    if item_kind in {6, 7} and str(getattr(music, "type", "")).upper() != "DX":
        raise ValueError(f"《{music.title}》不是 DX 谱面，不需要解锁 {item_kind}。")
    if item_kind == 7 and len(getattr(music, "charts", ()) or ()) <= 4:
        raise ValueError(f"《{music.title}》没有 Re:MASTER 难度，无法进行 7 类解锁。")
    return music


def _validate_music_difficulty(music, level: int) -> None:
    is_utage = (
        int(music.id) >= 100000
        or str(getattr(music.basic_info, "genre", "")) in {"宴会場", "宴会场"}
    )
    if level == 10 and not is_utage:
        raise ValueError(f"《{music.title}》不是宴会场曲目")
    if level != 10 and is_utage:
        raise ValueError(f"《{music.title}》是宴会场曲目，难度请填写“宴”")
    index = 0 if level == 10 else level
    if index < 0 or index >= len(music.charts):
        available = " / ".join(
            _DIFFICULTY_LABELS.get(i, str(i)) for i in range(len(music.charts))
        )
        raise ValueError(f"《{music.title}》没有该难度；可选：{available}")


def _parse_achievement(value: str) -> float:
    text = str(value or "").strip().replace("％", "%")
    if text.endswith("%"):
        text = text[:-1].strip()
    try:
        achievement = float(text)
    except ValueError as exc:
        raise ValueError("达成率格式错误，例如：100.5%") from exc
    if 0 < achievement <= 1:
        achievement *= 100
    if not 0 <= achievement <= 101:
        raise ValueError("达成率必须在 0%～101% 之间")
    return round(achievement, 4)


def _parse_dx_score(value: str) -> int:
    text = re.sub(r"^(?:dx(?:分|score)?)[：:=]?", "", str(value).strip(), flags=re.I)
    try:
        score = int(text)
    except ValueError as exc:
        raise ValueError("DX 分必须是整数；简单模式填写星级 0～5") from exc
    if score < 0:
        raise ValueError("DX 分不能小于 0")
    return score


def _chart_max_dx_score(music, level: int) -> int:
    index = 0 if level == 10 else level
    return sum(int(value) for value in music.charts[index].notes) * 3


def _parse_score_options(tokens: list[str], music, level: int) -> dict:
    if len(tokens) < 2:
        raise ValueError("请提供达成率和 DX 分，例如：100.5% 5 FC FS")
    achievement = _parse_achievement(tokens[0])
    dx_score = _parse_dx_score(tokens[1])
    mode: Optional[str] = None
    combo, sync = "none", "none"
    for token in tokens[2:]:
        normalized = token.strip().lower()
        if normalized in {"简单", "简单模式", "simple", "模糊", "fuzzy"}:
            mode = "simple"
        elif normalized in {"专业", "专业模式", "pro", "professional", "精确", "exact"}:
            mode = "professional"
        elif normalized in _COMBO_ALIASES:
            combo = _COMBO_ALIASES[normalized]
        elif normalized in _SYNC_ALIASES:
            sync = _SYNC_ALIASES[normalized]
        else:
            raise ValueError(f"无法识别的成绩选项：{token}")
    inferred_mode = "simple" if dx_score <= 5 else "professional"
    mode = mode or inferred_mode
    if mode == "simple" and not 0 <= dx_score <= 5:
        raise ValueError("简单模式的 DX 分表示星级，只能填写 0～5")
    max_dx = _chart_max_dx_score(music, level)
    if mode == "professional" and dx_score > max_dx:
        raise ValueError(f"专业模式实际 DX 分不能超过该谱面满分 {max_dx}")
    return {
        "musicId": int(music.id),
        "level": level,
        "achievement": achievement,
        "dxScore": dx_score,
        "comboStatus": combo,
        "syncStatus": sync,
        "fuzzy": mode == "simple",
    }


def _parse_music_upsert_command(raw: str) -> tuple[Any, int, dict]:
    tokens = str(raw or "").split()
    last_error: Optional[ValueError] = None
    for index in range(len(tokens) - 2, 0, -1):
        level = _parse_difficulty(tokens[index])
        if level is None:
            continue
        try:
            music = _resolve_account_music(" ".join(tokens[:index]))
            _validate_music_difficulty(music, level)
            score = _parse_score_options(tokens[index + 1 :], music, level)
            return music, level, score
        except ValueError as exc:
            last_error = exc
            continue
    if last_error is not None:
        raise last_error
    raise ValueError(
        "格式错误：mai改成绩 <歌曲> <难度> <达成率> <DX分> [FC] [FS] [简单/专业]"
    )


def _parse_music_delete_command(raw: str) -> tuple[Any, int]:
    tokens = str(raw or "").split()
    if len(tokens) < 2:
        raise ValueError("格式错误：mai删成绩 <歌曲> <难度>")
    level = _parse_difficulty(tokens[-1])
    if level is None:
        raise ValueError("无法识别难度，请使用 BASIC/ADV/EXP/MAS/Re:MAS 或颜色")
    music = _resolve_account_music(" ".join(tokens[:-1]))
    _validate_music_difficulty(music, level)
    return music, level


_ITEM_KIND_INPUTS = {
    "姓名框": 1, "nameplate": 1,
    "称号": 2, "title": 2,
    "头像": 3, "icon": 3,
    "收藏品": 4,
    "乐曲": 5, "乐曲解锁": 5, "music": 5,
    "MASTER谱面解锁": 6, "master谱面解锁": 6,
    "Re:MASTER谱面解锁": 7, "re:master谱面解锁": 7,
    "角色": 9, "character": 9,
    "搭档": 10, "partner": 10,
    "边框": 11, "frame": 11,
    "票券": 12, "ticket": 12,
}
_SUPPORTED_ITEM_KINDS = frozenset({1, 2, 3, 4, 5, 6, 7, 9, 10, 11, 12})
_MUSIC_ITEM_KINDS = frozenset({5, 6, 7})
_INTERACTION_CANCEL_WORDS = {"取消", "cancel", "q", "退出", "00"}


def _is_interaction_cancel(value: str) -> bool:
    return str(value or "").strip().lower() in _INTERACTION_CANCEL_WORDS


def _parse_item_kind(value: str) -> int:
    text = str(value or "").strip().lower()
    if text in _ITEM_KIND_INPUTS:
        return _ITEM_KIND_INPUTS[text]
    try:
        kind = int(text)
    except ValueError as exc:
        raise ValueError("itemKind 必须是正整数或已知类型名称") from exc
    if kind not in _SUPPORTED_ITEM_KINDS:
        raise ValueError(
            "暂只支持 itemKind：1、2、3、4、5、6、7、9、10、11、12"
        )
    return kind


def _parse_item_operation(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"add", "添加", "增加", "新增"}:
        return "add"
    if normalized in {"del", "delete", "删除", "移除"}:
        return "del"
    raise ValueError("操作只能选择 add（添加）或 del（删除）")


def _parse_item_upsert_command(raw: str) -> tuple[int, int, str]:
    tokens = str(raw or "").split()
    if len(tokens) < 3:
        raise ValueError("格式错误：mai改道具 <itemKind> <歌曲或 itemId> <add/del>")
    kind = _parse_item_kind(tokens[0])
    operation = _parse_item_operation(tokens[-1])
    item_query = " ".join(tokens[1:-1]).strip()
    if kind in {5, 6, 7}:
        music = _resolve_item_music(item_query, kind)
        return kind, int(music.id), operation
    try:
        item_id = int(item_query)
    except ValueError as exc:
        raise ValueError("itemId 必须是正整数") from exc
    if item_id <= 0:
        raise ValueError("itemId 必须大于 0")
    return kind, item_id, operation


def _forward_image_node(user_id: str, nickname: str, image_b64: str, caption: str = "") -> dict:
    """合并转发节点：图片 + 可选说明。"""
    content: list[dict] = [{"type": "image", "data": {"file": image_b64}}]
    if caption.strip():
        content.append({"type": "text", "data": {"text": "\n" + caption.strip()}})
    return {
        "type": "node",
        "data": {
            "name": str(nickname),
            "uin": str(user_id),
            "content": content,
        },
    }


async def _deliver_live_status_forward(bot: Bot, event: MessageEvent, payload: dict) -> None:
    """发送「失败率图 + 服务器状态」合并转发。"""
    from ..libraries.maimaidx_platform import (
        build_image_message,
        deliver_forward_messages,
        use_qq_mode,
    )

    nickname = str(getattr(maiconfig, "botName", None) or "AWMC Bot")
    caption = str(payload.get("failure_caption") or "")
    chart_b64 = payload.get("chart_b64")
    nodes = []
    if chart_b64:
        nodes.append(_forward_image_node(str(event.self_id), nickname, chart_b64, caption))
    for section in payload.get("server_sections") or []:
        if str(section).strip():
            nodes.append(build_forward_node(str(event.self_id), nickname, section))
    try:
        send_nodes = list(nodes)
        if chart_b64 and use_qq_mode(event):
            img = (
                chart_b64
                if str(chart_b64).startswith('base64://')
                else f'base64://{chart_b64}'
            )
            await bot.send(event, build_image_message(img, event=event))
            if caption:
                await bot.send(event, caption)
            send_nodes = [
                node
                for node in nodes
                if not str((node.get('data') or {}).get('content', '')).startswith('[CQ:image')
            ]
        await deliver_forward_messages(
            bot,
            event,
            send_nodes,
            title='舞萌状态',
            reply_message=True,
        )
    except Exception as exc:
        log.warning(
            f"[LiveStatus] 合并转发失败，回退普通消息：{type(exc).__name__}: {exc}"
        )
        if chart_b64:
            await maimai_live_status.send(
                MessageSegment.image(chart_b64) + ("\n" + caption if caption else ""),
                reply_message=True,
            )
        for section in payload.get("server_sections") or []:
            await maimai_live_status.send(section, reply_message=False)


def _result_text(result: dict) -> str:
    if not result:
        return "操作已完成"
    if result.get("error"):
        return str(result["error"])
    message = result.get("msg") or result.get("message")
    if isinstance(message, dict):
        message = message.get("message") or json.dumps(message, ensure_ascii=False)
    if message:
        return str(message)
    if result.get("done") is True:
        return "异步任务已完成"
    if result.get("status") == "ok" and (
        "imported" in result or "updated" in result
    ):
        return (
            f"已同步（新增 {int(result.get('imported') or 0)}，"
            f"更新 {int(result.get('updated') or 0)}）"
        )
    task_id = result.get("task_id")
    if task_id:
        return f"任务已提交，任务 ID：{task_id}"
    count = result.get("count")
    if count is not None:
        return f"已处理 {count} 条成绩"
    return "操作已完成"


def _exception_detail(exc: BaseException) -> str:
    """保证面向用户和审计日志的异常原因永不为空。"""
    sw_error = find_sw_api_error(exc)
    if sw_error is not None and sw_error.is_quota_exceeded:
        return format_sw_api_quota_error(sw_error)
    if sw_error is not None and sw_error.is_connection_error:
        return redact(str(sw_error)).strip() or "无法连接 AWMC 网关，请稍后重试"
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException)):
        return "请求超时，上游服务未在规定时间内响应"
    if isinstance(exc, httpx.ConnectError):
        return "无法连接上游服务，请稍后重试"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"上游服务返回 HTTP {exc.response.status_code}"

    detail = redact(str(exc)).strip()
    # UID 是街机账号的内部标识，不得通过任何异常文案透出给用户。
    detail = re.sub(
        r"(?i)\bUID\b\s*[\"']?\s*[:：=]?\s*\d+",
        "账号标识[已隐藏]",
        detail,
    )
    # 截断上游 httpx 错误中附带的内部 URL 和 Server Error 细节，
    # 只保留冒号前有意义的错误前缀（如"拉取 Sega 成绩失败"）。
    url_match = re.match(
        r"^(.+?)\s*[:：]\s*.*\bfor url\b.*$",
        detail,
        re.IGNORECASE | re.DOTALL,
    )
    if url_match:
        detail = url_match.group(1).strip()
    if detail:
        return detail

    cause = exc.__cause__ or exc.__context__
    if cause is not None and cause is not exc:
        cause_detail = _exception_detail(cause)
        if cause_detail:
            return cause_detail
    if isinstance(exc, SwApiError):
        return "AWMCError"
    return f"{type(exc).__name__}（上游服务未返回错误详情）"


def _upload_failure_message(exc: BaseException) -> str:
    return f"上传失败：{_exception_detail(exc)}"


def _oauth_qqid(event: MessageEvent) -> Optional[int]:
    try:
        return resolve_score_qqid(event)
    except Exception:
        return None


def _has_lxns_oauth(event: MessageEvent) -> bool:
    qqid = _oauth_qqid(event)
    if qqid is None:
        return False
    row = lxns_db.get_user(qqid)
    return bool(row and row.get("access_token"))


async def _has_divingfish_oauth(event: MessageEvent) -> bool:
    """Probe the upstream grant; waterfish intentionally stores no local token."""
    if not divingfish_oauth_enabled():
        return False
    qqid = _oauth_qqid(event)
    if qqid is None:
        return False
    try:
        await get_divingfish_access_token(qqid)
        return True
    except (DivingFishNotAuthorizedError, DivingFishOAuthError, RuntimeError, OSError):
        return False


def _lxns_oauth_missing_write_scope(event: MessageEvent) -> bool:
    qqid = _oauth_qqid(event)
    if qqid is None:
        return False
    row = lxns_db.get_user(qqid)
    if not row or not row.get("access_token"):
        return False
    scope = str(row.get("scope") or "").replace(",", " ").split()
    return bool(scope and "write_player" not in scope)


async def _lxns_oauth_access_token(
    event: MessageEvent, *, force_refresh: bool = False
) -> Optional[str]:
    """复用现有 lxbind 授权；旧授权缺少写权限时要求重新授权。"""
    qqid = _oauth_qqid(event)
    if qqid is None:
        return None
    row = lxns_db.get_user(qqid)
    if not row:
        return None
    scope = str(row.get("scope") or "").replace(",", " ").split()
    if scope and "write_player" not in scope:
        return None
    # 延迟导入，避免命令模块初始化时形成循环依赖。
    from .mai_lxns import _get_valid_access_token

    return await _get_valid_access_token(qqid, force_refresh=force_refresh)


def _oauth_token_rejected(exc: Exception) -> bool:
    if isinstance(exc, LxnsApiError):
        return exc.status_code in {401, 403}
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {401, 403}


def _lxns_upload_failure_text(exc: Exception, *, stage: str) -> str:
    detail = _exception_detail(exc)
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return f"{stage}超时（{detail[:120]}）"
    if isinstance(exc, LxnsApiError) and exc.status_code == 403:
        return (
            "落雪拒绝写入（HTTP 403）。请确认 OAuth 应用已启用 write_player，"
            "并在落雪账号隐私设置中允许第三方写入数据"
        )
    if isinstance(exc, LxnsApiError) and exc.status_code == 401:
        return "落雪 OAuth 凭据已失效，自动刷新后仍未通过验证"
    return f"{stage}失败：{detail[:200]}"


def _pc_cache_ttl_seconds() -> float:
    return max(
        0.0,
        float(
            getattr(
                maiconfig,
                "awmc_lxns_pc_cache_seconds",
                getattr(maiconfig, "awmc_sgid_cache_seconds", 600.0),
            )
            or 0.0
        ),
    )


def _lxns_scores_from_pc_cache(qqid: int) -> Optional[list[dict]]:
    """有足够新鲜的本地 PC 成绩时，直接转成落雪 Score，跳过机台登录。"""
    records = pc_db.get_user_play_counts(qqid)
    if not records:
        return None
    newest = max(float(r.updated_at or 0) for r in records)
    ttl = _pc_cache_ttl_seconds()
    if ttl > 0 and (time.time() - newest) > ttl:
        return None
    scores = convert_pc_records_to_lxns_scores(records)
    return scores or None


async def _oauth_upload_lxns_scores(
    event: MessageEvent,
    oauth_token: str,
    scores: list[dict],
    *,
    source: str,
) -> dict:
    """OAuth 个人 API：POST /api/v0/user/maimai/player/scores，默认 120s 上限。"""
    upload_timeout = float(
        getattr(maiconfig, "awmc_b50_upload_timeout_seconds", 120.0) or 120.0
    )
    try:
        return await asyncio.wait_for(
            user_upload_scores(oauth_token, scores),
            timeout=upload_timeout,
        )
    except asyncio.TimeoutError as exc:
        raise TimeoutError(
            f"落雪写入超时（{upload_timeout:.0f}s，来源 {source}）"
        ) from exc


async def _oauth_upload_lxns_with_refresh(
    event: MessageEvent,
    oauth_token: str,
    scores: list[dict],
    *,
    source: str,
) -> dict:
    try:
        return await _oauth_upload_lxns_scores(
            event, oauth_token, scores, source=source
        )
    except Exception as exc:
        if not _oauth_token_rejected(exc):
            raise
        refreshed_token = await _lxns_oauth_access_token(event, force_refresh=True)
        if not refreshed_token:
            raise RuntimeError("落雪 OAuth Token 刷新失败") from exc
        return await _oauth_upload_lxns_scores(
            event, refreshed_token, scores, source=source
        )


def _ensure_business_success(result: dict) -> None:
    """防止外部服务以 HTTP 200 返回业务失败时被误扣 BREAK。"""
    if not isinstance(result, dict):
        return

    if _is_sgid_expired_error(
        RuntimeError(json.dumps(result, ensure_ascii=False, default=str))
    ):
        raise RuntimeError("ChimeError 3002：Chime 获取用户失败，SGID 已过期")

    def all_null(value: Any) -> bool:
        if isinstance(value, dict):
            return not value or all(all_null(item) for item in value.values())
        if isinstance(value, (list, tuple)):
            return not value or all(all_null(item) for item in value)
        return value is None

    if all_null(result):
        raise RuntimeError("外部服务返回全部 null，二维码可能已失效")
    if (
        result.get("success") is False
        or result.get("ok") is False
        or result.get("UploadStatus") is False
        or result.get("ChargeStatus") is False
    ):
        raise RuntimeError(str(result.get("error") or result.get("msg") or "外部操作失败"))
    if result.get("error"):
        raise RuntimeError(str(result["error"]))
    code = result.get("code")
    if code not in (None, 0, "0"):
        raise RuntimeError(str(result.get("msg") or f"外部操作失败（code={code}）"))
    return_code = result.get("returnCode", result.get("ReturnCode"))
    if return_code is not None and return_code not in (1, "1"):
        raise RuntimeError(
            str(
                result.get("returnMessage")
                or result.get("msg")
                or f"外部操作失败（returnCode={return_code}）"
            )
        )


async def _await_upload_success(result: dict, *, lxns: bool) -> dict:
    """上传成功后才允许 BREAK 结算。新版 public/team 均为同步；若仍返回 task_id 则轮询。"""
    _ensure_business_success(result)
    task_id = str(result.get("task_id") or "").strip()
    if not task_id or result.get("sync") is True:
        return result
    interval = max(
        1.0, float(getattr(maiconfig, "awmc_upload_poll_interval_seconds", 2.0))
    )
    # B50 生成偶尔较慢，默认允许 120s；进度消息会提前告知用户正在处理。
    timeout = max(
        interval, float(getattr(maiconfig, "awmc_upload_poll_timeout_seconds", 120.0))
    )
    deadline = time.monotonic() + timeout
    log.info(
        f"[upload] 轮询异步任务 task_id={task_id} lxns={lxns} "
        f"timeout={timeout:.0f}s interval={interval:.0f}s"
    )
    while time.monotonic() < deadline:
        await asyncio.sleep(interval)
        detail = await sw_api.get_upload_task(task_id, lxns=lxns)
        error = detail.get("error")
        if error not in (None, ""):
            raise RuntimeError(str(error))
        if detail.get("done") is True:
            return {**result, **detail, "task_id": task_id}
    raise RuntimeError(f"上传任务 {task_id} 超时（{timeout:.0f}s），未扣 BREAK")


def _log(user_key: str, operation: str, status: str, detail: str = "") -> str:
    safe_detail = str(redact(detail))[:1000]
    ref_id = admin_audit.current_ref_id()
    manual = ref_id is None
    if ref_id is None:
        ref_id = admin_audit.start_trace(
            command=operation, user_id=user_key, input_summary={"source": "account"}
        )
    admin_audit.add_step(
        f"account.{operation}", status, {"detail": safe_detail}, ref_id=ref_id
    )
    account_db.append_log(ref_id, user_key, operation, status, safe_detail)
    if manual:
        admin_audit.finish_trace(ref_id, "success" if status == "success" else "error")
    return ref_id


async def _service_cost(service: str, *, multiple: int = 1) -> int:
    if service == "ticket":
        unit = int(await asyncio.to_thread(
            break_db.get_config, "ticket_cost_per_multiplier", "10"
        ))
        return max(0, unit) * max(1, multiple)
    if service == "ticket_status":
        return max(0, int(await asyncio.to_thread(
            break_db.get_config, "ticket_status_cost", "1"
        )))
    if service == "awmc_status":
        return max(0, int(await asyncio.to_thread(
            break_db.get_config, "awmc_status_cost", "2"
        )))
    if service in {"awmc_preview", "awmc_items", "awmc_gate_status"}:
        return max(0, int(await asyncio.to_thread(
            break_db.get_config, "awmc_read_cost", "5"
        )))
    if service == "awmc_game_event":
        return max(0, int(await asyncio.to_thread(
            break_db.get_config, "awmc_game_event_cost", "2"
        )))
    if service == "awmc_music_upsert":
        return max(0, int(await asyncio.to_thread(
            break_db.get_config, "awmc_music_upsert_cost", "75"
        )))
    if service == "awmc_music_delete":
        return max(0, int(await asyncio.to_thread(
            break_db.get_config, "awmc_music_delete_cost", "50"
        )))
    if service == "awmc_item_upsert":
        return max(0, int(await asyncio.to_thread(
            break_db.get_config, "awmc_item_upsert_cost", "100"
        )))
    defaults = {"upload_fish": "2", "upload_lx": "2", "upload_all": "3"}
    return max(0, int(await asyncio.to_thread(
        break_db.get_config, f"{service}_cost", defaults[service]
    )))


async def _ensure_service_affordable(qqid: int, service: str, cost: int) -> None:
    await asyncio.to_thread(
        break_db.ensure_service_affordable, qqid, service, cost
    )


async def _settle_service_success(
    qqid: int, service: str, cost: int, *, meta: Optional[dict] = None
):
    return await asyncio.to_thread(
        break_db.settle_service_success, qqid, service, cost, meta=meta
    )


def _allowed_ticket_multipliers() -> tuple[int, ...]:
    raw = getattr(maiconfig, "awmc_ticket_allowed_multipliers", "2,3,5")
    if isinstance(raw, (list, tuple, set)):
        parts = raw
    else:
        parts = str(raw or "").replace("，", ",").split(",")
    values: set[int] = set()
    allowed = {2, 3, 5}
    for part in parts:
        try:
            value = int(str(part).strip())
        except (TypeError, ValueError):
            continue
        if value in allowed:
            values.add(value)
    return tuple(sorted(values)) or (2, 3, 5)


def _charge_text(result, qqid: Optional[int] = None) -> str:
    labels = {
        "upload": "成绩上传",
        "ticket": "发票",
        "ticket_status": "舞萌票券状态",
        "awmc_status": "账号状态查询",
        "awmc_preview": "账号预览查询",
        "awmc_items": "道具查询",
        "awmc_gate_status": "门状态查询",
        "awmc_game_event": "活动事件查询",
        "awmc_music_upsert": "成绩编辑",
        "awmc_music_delete": "成绩删除",
        "awmc_item_upsert": "道具修改",
    }
    label = labels.get(result.service, result.service)
    if getattr(result, "billing_disabled", False):
        return f"💳 {label}BREAK 计费已关闭，本次免费 · 余额 {result.balance} BREAK"
    if getattr(result, "free", False):
        return f"💳 {label}今日首次成功，免费 · 余额 {result.balance} BREAK"
    if getattr(result, "freedom", False):
        from ..libraries.maimaidx_break import format_freedom_exemption

        remaining = getattr(result, "freedom_remaining", 0.0) or 0.0
        return format_freedom_exemption(
            int(qqid),
            label,
            int(getattr(result, 'listed_cost', 0) or 0),
            remaining,
        )
    return f"💳 {label}消耗 {result.charged} BREAK · 余额 {result.balance} BREAK"


async def _require_agreement(matcher, event: MessageEvent) -> None:
    if not bool(getattr(maiconfig, "maimaidx_user_agreement_required", True)):
        return
    if not has_user_agreed(event):
        await matcher.finish(agreement_prompt())


@account_help.handle()
async def _(event: MessageEvent):
    values = await asyncio.gather(*(
        asyncio.to_thread(break_db.get_config, key, default)
        for key, default in (
            ("upload_fish_cost", "2"), ("upload_lx_cost", "2"),
            ("upload_all_cost", "3"), ("ticket_cost_per_multiplier", "10"),
            ("ticket_status_cost", "1"), ("awmc_read_cost", "5"),
            ("awmc_game_event_cost", "2"),
            ("awmc_status_cost", "2"), ("awmc_music_upsert_cost", "75"),
            ("awmc_music_delete_cost", "50"),
            ("awmc_item_upsert_cost", "100"),
        )
    ))
    (
        fish_cost, lx_cost, all_cost, ticket_unit, ticket_status_cost,
        read_cost, game_event_cost, status_cost, edit_cost, delete_cost, item_cost,
    ) = values
    ticket_multipliers = "/".join(map(str, _allowed_ticket_multipliers()))
    await plugin_finish(
        account_help,
        "AWMC 账号功能（已合并到 QueryBot）\n"
        "mai绑定 / maibind：绑定或认领舞萌账号\n"
        f"mai状态 / mymai：查看账号详细状态，每次成功查询 {status_cost} BREAK，失败不扣费\n"
        "舞萌状态 / mais：AWMC 全局失败率分类图（空分类省略）+ 实时状态\n"
        "绑定水鱼 / dfbind：一次 OAuth 同时用于水鱼查分和上传（推荐）\n"
        "mai绑定水鱼 <Token> / maibindfish <Token>：仅 OAuth 关闭时使用旧 Import-Token\n"
        "lxbind：落雪 OAuth（推荐）；maibindlx <导入Token> 为兼容方式\n"
        "发送二维码：始终上传 AWMCNET；已绑定水鱼/落雪时同时同步对应平台\n"
        "maiu / maiul / maiua：AWMCNET + 指定且已绑定的外部平台\n"
        f"发票 / fp <{ticket_multipliers}> / mai地图 / maiping\n"
        f"mai查票 / 查票：查询舞萌票券状态，每次成功查询 {ticket_status_cost} BREAK，失败不扣费\n"
        "mai预览 / 预览：查询账号预览；mai道具 / 道具：查询全部道具\n"
        "mai门状态 / 查门：查询 Kaleidx Gate\n"
        f"maievent / mai活动：查询舞萌活动事件，每次成功查询 {game_event_cost} BREAK\n"
        "mai改成绩 / 改分 [歌曲 难度 达成率 DX分 FC FS]：交互或一步编辑成绩\n"
        "mai删成绩 / 删分 [歌曲 难度]：交互或一步删除成绩\n"
        "mai改道具 / 改道具：高风险道具修改\n"
        f"当前上传价格：水鱼 {fish_cost} / 落雪 {lx_cost} / 同时 {all_cost} BREAK\n"
        f"发票价格：倍率 × {ticket_unit} BREAK（当前支持：{ticket_multipliers}）\n"
        f"AWMC 只读新功能：每次成功查询 {read_cost} BREAK，失败不扣费\n"
        f"账号状态查询（mymai）：每次成功查询 {status_cost} BREAK，失败不扣费\n"
        f"成绩编辑 {edit_cost} BREAK / 条；成绩删除 {delete_cost} BREAK / 条，失败不扣费\n"
        f"道具修改 {item_cost} BREAK / 次（未经测试，风险自负）\n"
        "已有 2/3/5 倍票未使用时重复发票，将拦截并扣除 20 BREAK。\n"
        "成绩上传每日首次成功免费；发票每次按价扣费，失败不扣费；"
        "明确失败会自动重试 2 次。\n"
        "发送“用户协议”阅读和确认服务条款。",
        event=event,
    )


@account_bind.handle()
async def _(matcher: Matcher, event: MessageEvent, args: Message = CommandArg()):
    await _require_agreement(account_bind, event)
    raw = _arg_text(args)
    if _is_interaction_cancel(raw):
        await account_bind.finish("已取消舞萌账号绑定。")
    if raw:
        matcher.set_arg("qrcode", Message(raw))
    else:
        track_event(session_key("account_bind", event), event)
        await account_bind.send(
            "请发送最新的 SGWCMAID，或舞萌二维码图片/请求链接。\n"
            "Bot 会尝试撤回凭据消息；最多可重试 3 次。\n"
            "发送“取消”可结束绑定。"
        )


@account_bind.got("qrcode")
async def _(
    matcher: Matcher,
    bot: Bot,
    event: MessageEvent,
    qrcode_message: Message = Arg("qrcode"),
):
    pending_key = session_key("account_bind", event)
    raw = qrcode_message.extract_plain_text().strip()
    if raw.lower() in {"取消", "cancel", "q", "退出"}:
        finish_pending(pending_key)
        await account_bind.finish("已取消舞萌账号绑定。")
    qrcode = extract_sgwcmaid_qrcode(raw)
    recall_notice = ""
    if qrcode:
        from ..libraries.maimaidx_platform import foreign_recall_notice, recall_message

        if not await recall_message(bot, event, foreign=True):
            recall_notice = f'{foreign_recall_notice(event)}\n'

    async def retry(reason: str) -> None:
        attempt = int(matcher.state.get("account_bind_retry", 0)) + 1
        matcher.state["account_bind_retry"] = attempt
        if attempt >= 3:
            finish_pending(pending_key)
            await account_bind.finish(
                recall_notice
                + f"二维码验证已连续失败 3 次：{reason}\n"
                "绑定流程已结束，请重新获取二维码后再发送 mai绑定。"
            )
        track_event(pending_key, event)
        await account_bind.reject(
            recall_notice
            + f"二维码无效或已过期：{reason}\n"
            f"请重新获取并发送 SGWCMAID 或官方二维码链接（{attempt}/3）。\n"
            "发送“取消”可退出。"
        )

    if not qrcode:
        await retry("内容不是完整 SGWCMAID 或受支持的官方二维码链接")
    key = _user_key(event)
    claimed_keys: list[str] = []
    try:
        binding, claimed_keys = await _bind_verified_account(key, qrcode)
        name = binding.user_name
        rating = binding.rating
    except Exception as exc:
        ref = _log(key, "bind", "error", str(exc))
        if is_sw_api_quota_error(exc):
            finish_pending(pending_key)
            await account_bind.finish(
                recall_notice + _exception_detail(exc) + f"\nRef_ID: {ref}"
            )
        await retry(f"{type(exc).__name__}（Ref_ID: {ref}）")

    # PC 凭据同步失败不回滚已经验真的绑定，避免用户重复提交敏感凭据。
    pc_status = "skipped"
    pc_note = ""
    try:
        from ..libraries.maimaidx_playcount_fetcher import playcount_fetcher

        if playcount_fetcher.sdgb_available:
            await playcount_fetcher.login_by_sdgb(qrcode, int(key))
            pc_status = "success"
    except Exception as exc:
        pc_status = f"error:{type(exc).__name__}"
        pc_note = "\nPC 凭据同步暂未完成，可稍后发送「更新pc数」。"

    # 显式 mai绑定 与直发 SGWCMAID 使用相同规则：AWMC NET. 必传，
    # 用户已绑定的水鱼/落雪作为附加目标一并上传。
    fish, lxns = auto_upload_channels(
        fish_token=binding.fish_token,
        lxns_token=binding.lxns_token,
        has_fish_oauth=await _has_divingfish_oauth(event),
        has_lxns_oauth=_has_lxns_oauth(event),
        divingfish_oauth_mode=divingfish_oauth_enabled(),
    )
    upload_note = ""
    first_notice = ""
    try:
        upload_note = await _upload(
            event,
            fish=fish,
            lxns=lxns,
            qrcode_arg=qrcode,
            _qrcode_verified=True,
        )
        first_notice = take_awmcnet_first_sync_notice(key, upload_note)
    except Exception as exc:
        log.warning(f"[bind] AWMC NET. 首次同步失败 user={key}: {type(exc).__name__}")
        upload_note = "AWMC NET. 暂未同步成功，可重新发送 SGWCMAID 重试。"
    operation = "claim" if claimed_keys else "bind"
    ref = _log(
        key, operation, "success",
        f"account_verified,claimed_records={len(claimed_keys)},pc={pc_status}",
    )
    label = name or "已识别玩家"
    action = "绑定认领成功" if claimed_keys else "绑定成功"
    claim_note = (
        "\n旧记录已安全转移，原记录在本 Bot 保存的舞萌/PC 凭据已失效。"
        if claimed_keys else ""
    )
    finish_pending(pending_key)
    await plugin_finish(
        account_bind,
        recall_notice
        + f"{action}：{label}\nRating：{rating}{claim_note}{pc_note}\n"
        + upload_note
        + (f"\n\n{first_notice}" if first_notice else "")
        + f"\nRef_ID: {ref}",
        event=event,
        reply_message=True,
        qq_buttons=_account_flow_shortcuts(event),
    )


@account_unbind.handle()
async def _(event: MessageEvent):
    key = _user_key(event)
    if not account_db.unbind_account(key):
        await account_unbind.finish("当前没有已绑定的舞萌账号。")
    try:
        pc_db.delete_credential(int(key))
    except (TypeError, ValueError):
        pass
    ref = _log(key, "unbind", "success")
    await account_unbind.finish(f"已解绑舞萌账号；水鱼/落雪 Token 已保留。\nRef_ID: {ref}")


@account_status.handle()
async def _(
    matcher: Matcher,
    event: MessageEvent,
    args: Message = CommandArg(),
):
    key = _user_key(event)
    binding = account_db.get(key)
    if not binding:
        await account_status.finish(_ACCOUNT_SETUP_GUIDE, reply_message=True)
    raw = _arg_text(args)
    if raw:
        matcher.set_arg("status_qrcode", Message(raw))
        return

    cache_valid, cache_label = _sgid_cache_state(binding)
    if cache_valid:
        cost = await _service_cost("awmc_status")
        try:
            await _ensure_service_affordable(int(key), "awmc_status", cost)
            binding, preview = await _read_verified_preview(
                binding, binding.qrcode, save_qrcode=False
            )
            text = await _render_account_status(event, binding, preview)
        except BreakInsufficientError as exc:
            ref = _log(key, "status", "error", "insufficient_break")
            await plugin_finish(
                account_status,
                f"{exc}\nRef_ID: {ref}",
                event=event,
                reply_message=True,
            )
        except Exception as exc:
            if is_sw_api_quota_error(exc):
                ref = _log(key, "status", "error", _exception_detail(exc))
                await account_status.finish(
                    _exception_detail(exc) + f"\nRef_ID: {ref}", reply_message=True
                )
            account_db.mark_qrcode_result(key, False)
            matcher.state["status_cache_error"] = type(exc).__name__
            cache_label = "缓存验证失败，需刷新"
        else:
            charge = await _settle_service_success(
                int(key), "awmc_status", cost, meta={"operation": "status", "source": "sgid_cache"}
            )
            ref = _log(
                key, "status", "success",
                f"preview_source=sgid_cache,charged={charge.charged}",
            )
            await plugin_finish(
                account_status,
                text + f"\n{_charge_text(charge, int(key))}\nRef_ID: {ref}",
                event=event,
                reply_message=True,
                qq_buttons=_account_flow_shortcuts(event),
            )
    track_event(session_key("account_status", event), event)
    await account_status.send(_status_qrcode_prompt(cache_label), reply_message=True)


@account_status.got("status_qrcode")
async def _(
    matcher: Matcher,
    bot: Bot,
    event: MessageEvent,
    qrcode_message: Message = Arg("status_qrcode"),
):
    pending_key = session_key("account_status", event)
    key = _user_key(event)
    binding = account_db.get(key)
    if not binding:
        finish_pending(pending_key)
        await account_status.finish(_ACCOUNT_SETUP_GUIDE, reply_message=True)
    raw = qrcode_message.extract_plain_text().strip()
    if raw.lower() in {"取消", "cancel", "q", "退出"}:
        text = await _render_account_status(event, binding)
        ref = _log(key, "status", "success", "preview_source=stored,cancelled_refresh")
        finish_pending(pending_key)
        await plugin_finish(
            account_status,
            text + f"\nRef_ID: {ref}",
            event=event,
            reply_message=True,
            qq_buttons=_account_flow_shortcuts(event),
        )

    recall_notice = ""
    from ..libraries.maimaidx_platform import foreign_recall_notice, recall_message

    if not await recall_message(bot, event, foreign=True):
        recall_notice = f'{foreign_recall_notice(event)}\n'
    qrcode = extract_sgwcmaid_qrcode(raw)

    async def retry(reason: str) -> None:
        attempt = int(matcher.state.get("status_qrcode_retry", 0)) + 1
        matcher.state["status_qrcode_retry"] = attempt
        if attempt >= 3:
            account_db.mark_qrcode_result(key, False)
            text = await _render_account_status(event, account_db.get(key) or binding)
            ref = _log(key, "status", "error", "refresh_failed=3_attempts")
            finish_pending(pending_key)
            await account_status.finish(
                recall_notice
                + f"二维码刷新已连续失败 3 次：{redact(reason)}\n本次展示缓存资料。\n"
                + text
                + f"\nRef_ID: {ref}",
                reply_message=True,
            )
        track_event(pending_key, event)
        await account_status.reject(
            recall_notice
            + f"二维码无效或已过期：{redact(reason)}\n"
            + f"请重新识别并发送（{attempt}/3），或发送「取消」查看缓存资料。",
            reply_message=True,
        )

    if not qrcode:
        await retry("未识别到 SGWCMAID 或受支持的官方二维码链接")
    cost = await _service_cost("awmc_status")
    try:
        await _ensure_service_affordable(int(key), "awmc_status", cost)
        binding, preview = await _read_verified_preview(
            binding, qrcode, save_qrcode=True
        )
        text = await _render_account_status(event, binding, preview)
    except BreakInsufficientError as exc:
        ref = _log(key, "status", "error", "insufficient_break")
        finish_pending(pending_key)
        await plugin_finish(
            account_status,
            recall_notice + f"{exc}\nRef_ID: {ref}",
            event=event,
            reply_message=True,
        )
    except Exception as exc:
        if is_sw_api_quota_error(exc):
            ref = _log(key, "status", "error", _exception_detail(exc))
            finish_pending(pending_key)
            await account_status.finish(
                recall_notice + _exception_detail(exc) + f"\nRef_ID: {ref}",
                reply_message=True,
            )
        await retry(type(exc).__name__)
    charge = await _settle_service_success(
        int(key), "awmc_status", cost, meta={"operation": "status", "source": "user_refresh"}
    )
    ref = _log(
        key, "status", "success",
        f"preview_source=user_refresh,charged={charge.charged}",
    )
    finish_pending(pending_key)
    await plugin_finish(
        account_status,
        recall_notice + text + f"\n{_charge_text(charge, int(key))}\nRef_ID: {ref}",
        event=event,
        reply_message=True,
        qq_buttons=_account_flow_shortcuts(event),
    )


@maimai_live_status.handle()
async def _(bot: Bot, event: MessageEvent):
    """舞萌状态 / mais：AWMC 全局失败率分类图 + 全部服务器实时状态。"""
    try:
        payload = await build_live_status_payload()
    except Exception as exc:
        log.warning(f"[LiveStatus] 拉取失败：{type(exc).__name__}: {exc}")
        await maimai_live_status.finish(
            f"暂时无法获取舞萌状态：{type(exc).__name__}\n"
            "可稍后重试，或打开 https://status.awmc.cc/status/maimai",
            reply_message=True,
        )
    await _deliver_live_status_forward(bot, event, payload)
    await maimai_live_status.finish()


def _save_upload_token(event: MessageEvent, token: str, kind: str) -> str:
    key = _user_key(event)
    account_db.set_token(key, kind, token)
    try:
        if kind == "fish":
            pc_db.save_prober_token(int(key), fish_token=token)
        else:
            pc_db.save_prober_token(int(key), lxns_code=token)
    except (TypeError, ValueError):
        pass
    return _log(key, f"bind_{kind}", "success")


@fish_bind.handle()
async def _(matcher: Matcher, event: MessageEvent, args: Message = CommandArg()):
    await _require_agreement(fish_bind, event)
    if divingfish_oauth_enabled():
        await plugin_finish(
            fish_bind,
            "水鱼 OAuth 已开启，旧 Import-Token 不再用于查分或上传。\n"
            "请发送「绑定水鱼」重新授权；一次授权即可同时查分和上传成绩。",
            event=event,
            reply_message=True,
        )
    token = _arg_text(args)
    if token:
        matcher.set_arg("fish_token", Message(token))
        return
    track_event(session_key("fish_bind", event), event)
    await fish_bind.send(
        "🐟 水鱼 Import-Token 获取方式：\n"
        f"1. 打开水鱼查分器：{_DIVING_FISH_PROBER_URL}\n"
        "2. 登录后进入「编辑个人资料」；\n"
        "3. 找到 Import-Token，生成后复制完整 Token 发给我。\n\n"
        "我会等待你的输入；格式不正确时可以重试，本轮最多 3 次。\n"
        "发送「取消」可结束绑定。",
        reply_message=True,
    )


@fish_bind.got("fish_token")
async def _(
    matcher: Matcher,
    bot: Bot,
    event: MessageEvent,
    token_message: Message = Arg("fish_token"),
):
    pending_key = session_key("fish_bind", event)
    token = token_message.extract_plain_text().strip()
    if token.lower() in {"取消", "cancel", "q", "退出"}:
        finish_pending(pending_key)
        await fish_bind.finish("已取消水鱼 Token 绑定。", reply_message=True)

    recall_notice = ""
    if token:
        from ..libraries.maimaidx_platform import foreign_recall_notice, recall_message

        if not await recall_message(bot, event, foreign=True):
            recall_notice = f'{foreign_recall_notice(event)}\n'

    if not (_FISH_TOKEN_MIN_LENGTH <= len(token) <= _FISH_TOKEN_MAX_LENGTH):
        attempt = int(matcher.state.get("fish_token_retry", 0)) + 1
        matcher.state["fish_token_retry"] = attempt
        reason = (
            f"Token 长度为 {len(token)}，应为 "
            f"{_FISH_TOKEN_MIN_LENGTH}–{_FISH_TOKEN_MAX_LENGTH} 个字符。"
        )
        if attempt >= 3:
            finish_pending(pending_key)
            await fish_bind.finish(
                recall_notice
                + f"❌ {reason}\n已连续输入失败 3 次，本轮绑定已结束。\n"
                "请重新生成完整 Import-Token 后，再发送「maibindfish」。",
                reply_message=True,
            )
        track_event(pending_key, event)
        await fish_bind.reject(
            recall_notice
            + f"❌ {reason}\n"
            f"请重新复制完整 Import-Token 发给我（{attempt}/3）。\n"
            "发送「取消」可退出。",
            reply_message=True,
        )

    ref = _save_upload_token(event, token, "fish")
    finish_pending(pending_key)
    await plugin_finish(
        fish_bind,
        f"✅ 水鱼兼容 Token 已绑定。\nToken：{_mask(token, 8, 4)}\n"
        "建议再发送「绑定水鱼」迁移到一次授权即可查分和上传的 OAuth。\n"
        f"Ref_ID: {ref}",
        event=event,
        reply_message=True,
        qq_buttons=_account_flow_shortcuts(event),
    )


@lx_upload_bind.handle()
async def _(event: MessageEvent, args: Message = CommandArg()):
    await _require_agreement(lx_upload_bind, event)
    token = _arg_text(args)
    if not token:
        await lx_upload_bind.finish(
            "推荐发送「lxbind」完成落雪 OAuth，无需提供导入 Token。\n"
            "兼容用法：mai绑定落雪 <导入Token>"
        )
    if len(token) < 8:
        await lx_upload_bind.finish("落雪导入 Token 格式过短，请检查后重试。")
    ref = _save_upload_token(event, token, "lxns")
    await plugin_finish(
        lx_upload_bind,
        f"落雪 Token 已绑定。\nRef_ID: {ref}",
        event=event,
        qq_buttons=_account_flow_shortcuts(event),
    )


async def _clear_token(matcher, event: MessageEvent, kind: str):
    key = _user_key(event)
    account_db.set_token(key, kind, "")
    try:
        if kind == "fish":
            pc_db.save_prober_token(int(key), fish_token="")
        else:
            pc_db.save_prober_token(int(key), lxns_code="")
    except (TypeError, ValueError):
        pass
    await matcher.finish(f"已解绑{'水鱼' if kind == 'fish' else '落雪'} Token。")


@fish_unbind.handle()
async def _(event: MessageEvent):
    await _clear_token(fish_unbind, event, "fish")


@lx_upload_unbind.handle()
async def _(event: MessageEvent):
    await _clear_token(lx_upload_unbind, event, "lxns")


async def _upload(
    event: MessageEvent,
    *,
    fish: bool,
    lxns: bool,
    qrcode_arg: str = "",
    _machine_locked: bool = False,
    _qrcode_verified: bool = False,
) -> str:
    if bool(getattr(maiconfig, "maimaidx_user_agreement_required", True)):
        if not _qrcode_verified and not has_user_agreed(event):
            return agreement_prompt()
    key = _user_key(event)
    binding = account_db.get(key)
    if not binding:
        return _ACCOUNT_SETUP_GUIDE

    requested_lxns = lxns
    external_warnings: list[str] = []
    oauth_token = await _lxns_oauth_access_token(event) if requested_lxns else None
    has_lxns_oauth = _has_lxns_oauth(event) if requested_lxns else False
    has_lxns_upload = bool(oauth_token or binding.lxns_token)
    if requested_lxns and has_lxns_oauth and not oauth_token and _lxns_oauth_missing_write_scope(event):
        external_warnings.append(
            "落雪：OAuth 缺少 write_player 权限，本次仅同步 AWMCNET"
        )
        if not binding.lxns_token:
            requested_lxns = False
    if requested_lxns and has_lxns_oauth and not oauth_token:
        external_warnings.append(
            "落雪 OAuth Token 已失效且自动刷新失败，本次仍会同步 AWMCNET；请重新 lxbind"
        )
        if not binding.lxns_token:
            requested_lxns = False
    # AWMCNET 永远上传。OAuth 开启后水鱼强制使用新版读写授权，旧
    # Import-Token 完全停用；关闭 OAuth 时才保留原有 Token 上传路径。
    requested_fish = fish
    fish_oauth = False
    if requested_fish and divingfish_oauth_enabled():
        try:
            await get_divingfish_access_token(int(key))
            fish_oauth = True
        except DivingFishNotAuthorizedError:
            external_warnings.append(
                "水鱼：尚未完成新版 OAuth 授权，请发送「绑定水鱼」重新绑定"
            )
        except (DivingFishOAuthError, RuntimeError, OSError) as exc:
            log.warning(f'[upload] 水鱼 OAuth 预检失败 user={key}: {type(exc).__name__}')
            external_warnings.append(
                "水鱼 OAuth 暂时不可用，本次仅同步 AWMC NET；旧 Token 不会回退使用"
            )
    fish = bool(
        requested_fish
        and (fish_oauth if divingfish_oauth_enabled() else binding.fish_token)
    )
    lxns = bool(requested_lxns and has_lxns_upload)

    # 最优路径：仅落雪 + OAuth + 新鲜 PC 缓存 → 直连个人 API，不占机台锁、不验二维码。
    # 文档：POST /api/v0/user/maimai/player/scores（Bearer OAuth）。
    if lxns and oauth_token and not fish and not _machine_locked:
        try:
            qqid = int(key)
        except ValueError:
            qqid = 0
        pc_scores = _lxns_scores_from_pc_cache(qqid) if qqid else None
        if pc_scores:
            operation = "upload_lx"
            billing_service = "upload"
            cost = await _service_cost(operation)
            awmc_result = None
            try:
                await _ensure_service_affordable(int(key), billing_service, cost)
                log.info(
                    f"[upload] 落雪 OAuth：使用 PC 缓存 {len(pc_scores)} 条，跳过机台 user={key}"
                )
                from ..libraries.maimaidx_awmcnet_sync import sync_awmcnet_pc_records
                awmc_result = await sync_awmcnet_pc_records(
                    qqid,
                    pc_db.get_user_play_counts(qqid),
                    nickname=binding.user_name,
                    rating=binding.rating,
                    play_count=pc_db.get_user_total_plays(qqid),
                )
                if awmc_result is None:
                    raise RuntimeError('AWMCNET 同步失败，请检查 Bot-Token 与服务地址')
                result = await _oauth_upload_lxns_with_refresh(
                    event, oauth_token, pc_scores, source="PC缓存"
                )
                account_db.mark_uploaded(key)
                charge = await _settle_service_success(
                    int(key), billing_service, cost,
                    meta={"operation": operation, "fish": False, "lxns": True, "source": "pc"},
                )
                _schedule_post_upload_maintenance(
                    key,
                    fish=False,
                    lxns=True,
                    archive_qqids=_archive_qqids_for_event(event, key),
                )
                ref = _log(
                    key, operation, "success",
                    f"charged={charge.charged},free={charge.free},source=pc,count={len(pc_scores)}",
                )
                return (
                    "上传完成\n"
                    f"{_AWMCNET_SYNCED_LINE}\n"
                    f"落雪（OAuth/PC缓存）：{_result_text(result)}\n"
                    f"{_charge_text(charge, int(key))}\nRef_ID: {ref}"
                )
            except Exception as exc:
                if awmc_result is not None:
                    # AWMC NET 已先写入成功时，落雪超时只能算外部平台部分失败，
                    # 不能把整次同步错误地回复成"上传失败"。
                    account_db.mark_uploaded(key)
                    charge = await _settle_service_success(
                        int(key), billing_service, cost,
                        meta={"operation": operation, "fish": False, "lxns": True, "source": "pc", "partial": True},
                    )
                    _schedule_post_upload_maintenance(
                        key,
                        fish=False,
                        lxns=True,
                        archive_qqids=_archive_qqids_for_event(event, key),
                    )
                    detail = _lxns_upload_failure_text(
                        exc, stage='向落雪写入成绩'
                    )
                    ref = _log(
                        key, "upload_awmcnet", "success",
                        f"awmcnet=success,lxns=error:{type(exc).__name__},charged={charge.charged},free={charge.free}",
                    )
                    return (
                        f"{_AWMCNET_SYNCED_LINE}\n"
                        f"⚠️ 落雪同步失败：{detail}\n"
                        "您可以稍后重新 lxbind 后再同步落雪。\n"
                        f"{_charge_text(charge, int(key))}\nRef_ID: {ref}"
                    )
                failure_message = f"上传失败：{_lxns_upload_failure_text(exc, stage='向落雪写入成绩')}"
                ref = _log(key, "upload", "error", _exception_detail(exc))
                return failure_message + f"\nRef_ID: {ref}"

    if not _machine_locked:
        try:
            async with machine_session():
                return await _upload(
                    event,
                    fish=fish,
                    lxns=lxns,
                    qrcode_arg=qrcode_arg,
                    _machine_locked=True,
                    _qrcode_verified=_qrcode_verified,
                )
        except MachineBusyError as exc:
            return _upload_failure_message(exc)

    direct_qrcode = extract_sgwcmaid_qrcode(qrcode_arg)
    qrcode = direct_qrcode or binding.qrcode
    if not qrcode:
        if lxns and oauth_token and not fish:
            return (
                "本地暂无足够新鲜的 PC 成绩，且尚未绑定舞萌二维码。\n"
                "请先发送最新 SGWCMAID / 官方二维码完成「更新pc数」或直接附在 maiul 后上传。"
            )
        return "尚未绑定舞萌账号，请使用 mai绑定，或在上传命令后附带 SGWCMAID。"

    try:
        if _qrcode_verified:
            qrcode = direct_qrcode or binding.qrcode
        elif direct_qrcode:
            binding, _ = await _read_verified_preview(
                binding, direct_qrcode, save_qrcode=True
            )
            qrcode = direct_qrcode
        else:
            cache_valid, cache_label = _sgid_cache_state(binding)
            if not cache_valid:
                return f"上传失败：二维码缓存{cache_label}"
            binding, _ = await _read_verified_preview(
                binding, binding.qrcode, save_qrcode=False
            )
            qrcode = binding.qrcode
    except Exception as exc:
        if is_sw_api_quota_error(exc):
            ref = _log(key, "upload", "error", _exception_detail(exc))
            return _upload_failure_message(exc) + f"\nRef_ID: {ref}"
        account_db.mark_qrcode_result(key, False)
        ref = _log(key, "upload", "error", f"sgid_preview={type(exc).__name__}")
        return f"上传失败：二维码验证失败（{type(exc).__name__}）\nRef_ID: {ref}"

    operation = (
        "upload_all" if fish and lxns
        else "upload_fish" if fish
        else "upload_lx" if lxns
        else "upload_awmcnet"
    )
    # AWMC NET 是 Bot 的默认成绩库，单独同步不应占用外部查分器的
    # 每日免费次数，也不收取 BREAK；只有同时上传水鱼/落雪时才计费。
    billing_service = "upload" if (fish or lxns) else "awmcnet_sync"
    cost = await _service_cost(operation) if (fish or lxns) else 0
    results: list[str] = list(external_warnings)
    awmc_result = None
    try:
        await _ensure_service_affordable(int(key), billing_service, cost)
        try:
            qqid = int(key)
        except ValueError:
            qqid = 0
        pc_records = pc_db.get_user_play_counts(qqid) if qqid else []
        fresh_seconds = float(getattr(maiconfig, 'awmc_lxns_pc_cache_seconds', 600) or 600)
        fresh_pc = bool(
            pc_records
            and time.time() - max(float(r.updated_at or 0) for r in pc_records) <= fresh_seconds
        )
        from ..libraries.maimaidx_awmcnet_sync import (
            sync_awmcnet_arcade_scores,
            sync_awmcnet_pc_records,
        )
        if fresh_pc:
            awmc_result = await sync_awmcnet_pc_records(
                qqid,
                pc_records,
                nickname=binding.user_name,
                rating=binding.rating,
                play_count=pc_db.get_user_total_plays(qqid),
            )
        elif not fish and not lxns:
            music_timeout = float(
                getattr(maiconfig, "awmc_user_music_timeout_seconds", 15.0)
            )
            raw_scores = await asyncio.wait_for(
                sw_api.get_user_music(qrcode, timeout=music_timeout, retry_count=0),
                timeout=music_timeout + 1.0,
            )
            converted = convert_sega_music_scores(raw_scores)
            awmc_result = await sync_awmcnet_arcade_scores(
                qqid, converted, nickname=binding.user_name, rating=binding.rating
            )
        else:
            # 外部上传会消耗一次性二维码；先把现有上游快照写入 AWMCNET，
            # 上传成功后的维护任务会再拉取最新结果覆盖。
            from ..libraries.maimaidx_awmcnet_sync import sync_awmcnet
            from ..libraries.maimaidx_datasource import get_user_records
            for source in ('divingfish', 'lxns'):
                if (source == 'divingfish' and not fish) or (source == 'lxns' and not lxns):
                    continue
                try:
                    upstream_user, upstream_records = await get_user_records(
                        qqid=qqid, force_source=source
                    )
                    awmc_result = await sync_awmcnet(
                        qqid, upstream_user, upstream_records, source=source
                    )
                    if awmc_result:
                        break
                except Exception as exc:
                    log.info(f'[upload] AWMCNET 上游预同步跳过 source={source}: {exc}')
        if awmc_result is None and not (fish or lxns):
            raise RuntimeError('AWMCNET 同步失败，请检查 Bot-Token 与服务地址')
        if awmc_result is not None:
            results.append(_AWMCNET_SYNCED_LINE)
        if fish:
            if fish_oauth:
                # OAuth write uses the same user grant as reads. Prefer fresh PC
                # rows when available; otherwise obtain the machine records once
                # and send the normalized update_records payload directly.
                if fresh_pc:
                    fish_records = convert_pc_records_to_divingfish_scores(pc_records)
                else:
                    music_timeout = float(
                        getattr(maiconfig, 'awmc_user_music_timeout_seconds', 15.0)
                    )
                    raw_scores = await asyncio.wait_for(
                        sw_api.get_user_music(
                            qrcode,
                            timeout=music_timeout,
                            retry_count=0,
                        ),
                        timeout=music_timeout + 1.0,
                    )
                    fish_records = convert_sega_music_scores_to_divingfish(raw_scores)
                result = await maiApi.update_records_oauth(qqid, fish_records)
            else:
                result = await sw_api.update_fish(qrcode, binding.fish_token)
            result = await _await_upload_success(result, lxns=False)
            results.append(
                ('水鱼（OAuth）：' if fish_oauth else '水鱼（Import-Token）：')
                + _result_text(result)
            )
        if lxns:
            if oauth_token:
                # 主路径：机台全量成绩 + OAuth 个人 API 直传。
                # 不再回退 update_lx（会二次占用已消耗的二维码并长时间挂起）。
                lxns_stage = "读取玩家 PC 数据"
                try:
                    # 机台路径也可能已有本轮刚写入的 PC；再试一次本地，避免重复登录。
                    try:
                        qqid = int(key)
                    except ValueError:
                        qqid = 0
                    pc_scores = _lxns_scores_from_pc_cache(qqid) if qqid else None
                    if pc_scores:
                        lxns_stage = "向落雪写入成绩"
                        log.info(
                            f"[upload] 落雪 OAuth：机台会话内改用 PC 缓存 "
                            f"{len(pc_scores)} 条 user={key}"
                        )
                        result = await _oauth_upload_lxns_with_refresh(
                            event, oauth_token, pc_scores, source="PC缓存"
                        )
                        results.append("落雪（OAuth/PC缓存）：" + _result_text(result))
                    else:
                        log.info(f"[upload] 落雪 OAuth：开始读取机台成绩 user={key}")
                        music_timeout = float(
                            getattr(maiconfig, "awmc_user_music_timeout_seconds", 15.0)
                        )
                        raw_scores = await asyncio.wait_for(
                            sw_api.get_user_music(
                                qrcode,
                                timeout=music_timeout,
                                retry_count=0,
                            ),
                            timeout=music_timeout + 1.0,
                        )
                        scores = convert_sega_music_scores(raw_scores)
                        if not scores:
                            raise RuntimeError("机台返回的成绩无法转换为落雪 Score")
                        log.info(
                            f"[upload] 落雪 OAuth：转换完成 {len(scores)} 条，开始写入落雪"
                        )
                        lxns_stage = "向落雪写入成绩"
                        result = await _oauth_upload_lxns_with_refresh(
                            event, oauth_token, scores, source="机台"
                        )
                        results.append("落雪（OAuth）：" + _result_text(result))
                except Exception as exc:
                    raise RuntimeError(
                        _lxns_upload_failure_text(exc, stage=lxns_stage)
                        + "。可先「更新pc数」再用 maiul，或重新 lxbind 后重试"
                    ) from exc
            else:
                # 备选：仅无 OAuth 时才用导入 Token。
                log.info(f"[upload] 落雪兼容 Token：开始 update_lx user={key}")
                result = await sw_api.update_lx(qrcode, binding.lxns_token)
                result = await _await_upload_success(result, lxns=True)
                results.append("落雪（兼容 Token）：" + _result_text(result))
        if awmc_result is None:
            from ..libraries.maimaidx_awmcnet_sync import sync_awmcnet
            from ..libraries.maimaidx_datasource import get_user_records
            for source in ('divingfish', 'lxns'):
                if (source == 'divingfish' and not fish) or (source == 'lxns' and not lxns):
                    continue
                try:
                    upstream_user, upstream_records = await get_user_records(
                        qqid=qqid, force_source=source, force_refresh=True
                    )
                    awmc_result = await sync_awmcnet(
                        qqid, upstream_user, upstream_records, source=source
                    )
                    if awmc_result:
                        results.insert(0, _AWMCNET_SYNCED_LINE)
                        break
                except Exception as exc:
                    log.warning(f'[upload] 外部上传后同步 AWMCNET 失败 source={source}: {exc}')
        if awmc_result is None:
            raise RuntimeError('外部平台已处理，但 AWMCNET 同步失败，请稍后重试')
        account_db.mark_uploaded(key)
        charge = await _settle_service_success(
            int(key), billing_service, cost,
            meta={"operation": operation, "fish": fish, "lxns": lxns},
        )
        _schedule_post_upload_maintenance(
            key,
            fish=fish,
            lxns=lxns,
            archive_qqids=_archive_qqids_for_event(event, key),
        )
        ref = _log(key, operation, "success", f"charged={charge.charged},free={charge.free}")
        return "上传完成\n" + "\n".join(results) + f"\n{_charge_text(charge, int(key))}\nRef_ID: {ref}"
    except Exception as exc:
        if awmc_result is not None:
            # 水鱼/落雪失败不回滚已经成功的 AWMC NET 同步。
            account_db.mark_uploaded(key)
            charge = await _settle_service_success(
                int(key), billing_service, cost,
                meta={"operation": operation, "fish": fish, "lxns": lxns, "partial": True},
            )
            _schedule_post_upload_maintenance(
                key,
                fish=fish,
                lxns=lxns,
                archive_qqids=_archive_qqids_for_event(event, key),
            )
            detail = _exception_detail(exc)
            shown = list(dict.fromkeys(results))
            if _AWMCNET_SYNCED_LINE not in shown:
                shown.insert(0, _AWMCNET_SYNCED_LINE)
            ref = _log(
                key, "upload_awmcnet", "success",
                f"awmcnet=success,external=error:{type(exc).__name__},charged={charge.charged},free={charge.free}",
            )
            return (
                "\n".join(shown)
                + f"\n⚠️ 其他查分平台同步失败 {detail}\n{_charge_text(charge, int(key))}\nRef_ID: {ref}"
            )
        failure_message = _upload_failure_message(exc)
        if _upload_retryable(failure_message):
            account_db.mark_qrcode_result(key, False)
        ref = _log(key, "upload", "error", _exception_detail(exc))
        return failure_message + f"\nRef_ID: {ref}"


_UPLOAD_MODE_STATE_KEY = "maimaidx_upload_mode"


def _upload_mode(matcher: Matcher) -> tuple[bool, bool]:
    """Resolve and persist the upload mode across ``got`` session resumes.

    NoneBot may resume a waiting matcher with a generated subclass, so exact
    ``type(matcher) is ...`` checks are not reliable on the QR-code reply.
    """
    stored = matcher.state.get(_UPLOAD_MODE_STATE_KEY)
    if stored == "fish":
        return True, False
    if stored == "lxns":
        return False, True
    if stored == "all":
        return True, True

    if isinstance(matcher, upload_fish):
        matcher.state[_UPLOAD_MODE_STATE_KEY] = "fish"
        return True, False
    if isinstance(matcher, upload_lx):
        matcher.state[_UPLOAD_MODE_STATE_KEY] = "lxns"
        return False, True
    if isinstance(matcher, upload_all):
        matcher.state[_UPLOAD_MODE_STATE_KEY] = "all"
        return True, True
    raise ValueError("未知上传指令")


def _upload_preflight_error(
    event: MessageEvent, *, fish: bool, lxns: bool
) -> Optional[str]:
    """在外部请求前完成不需要网络的基础校验。"""
    # Official QQ users must have a qbind mapping before any upload flow,
    # including the agreement/setup branches that otherwise return early.
    require_account_qqid(event)
    if bool(getattr(maiconfig, "maimaidx_user_agreement_required", True)):
        if not has_user_agreed(event):
            return agreement_prompt()

    binding = account_db.get(_user_key(event))
    if not binding:
        return _ACCOUNT_SETUP_GUIDE

    # 外部平台授权问题由 _upload 降级处理，不能阻塞 AWMCNET 建档。
    return None


def _upload_can_start_now(
    event: MessageEvent,
    *,
    fish: bool,
    lxns: bool,
    qrcode_arg: str = "",
) -> bool:
    """是否已有可立即开始上传的数据，避免把等待新 SGID 说成已受理。"""
    if extract_sgwcmaid_qrcode(qrcode_arg):
        return True

    key = _user_key(event)
    binding = account_db.get(key)
    if binding is None:
        return False

    # 仅落雪 OAuth 可以复用新鲜 PC 缓存，不依赖 SGID。
    if lxns and not fish and _has_lxns_oauth(event):
        try:
            qqid = int(key)
        except ValueError:
            qqid = 0
        if qqid and _lxns_scores_from_pc_cache(qqid):
            return True

    if not binding.qrcode:
        return False
    cache_valid, _ = _sgid_cache_state(binding)
    return cache_valid


def auto_upload_channels(
    *,
    fish_token: str = "",
    lxns_token: str = "",
    has_fish_oauth: bool = False,
    has_lxns_oauth: bool = False,
    divingfish_oauth_mode: bool = False,
) -> tuple[bool, bool]:
    """直接二维码默认按 maiua 处理，但只上传用户实际绑定的渠道。"""
    fish = has_fish_oauth if divingfish_oauth_mode else bool(fish_token)
    return bool(fish), bool(lxns_token or has_lxns_oauth)


def _upload_retryable(message: str) -> bool:
    if not message.startswith("上传失败："):
        return False
    lowered = message.lower()
    return any(marker in lowered for marker in (
        "http 500", "500 internal", "null", "none", "qrcode", "sgwcmaid",
        "二维码", "过期", "失效", "无效", "登录失败",
    ))


def _upload_retry_prompt(message: str, attempt: int) -> str:
    reason = message.split("\nRef_ID:", 1)[0].removeprefix("上传失败：").strip()
    if "二维码缓存" in reason and any(
        marker in reason for marker in ("过期", "失效", "无效", "刷新")
    ):
        return (
            "二维码缓存已过期，请重新发送最新 SGWCMAID、"
            "官方二维码链接或二维码图片。\n"
            "发送“取消”可退出。"
        )
    if not reason:
        reason = "上游服务未返回错误详情，请换新二维码后重试"
    retry_label = f"已尝试 {attempt}/3" if attempt else "尚未重试，最多可尝试 3 次"
    return (
        f"上传未完成：{redact(reason)}\n"
        f"请重新获取并发送最新 SGWCMAID 或官方二维码链接（{retry_label}）。\n"
        "Bot 会尝试撤回凭据消息；发送“取消”可退出。"
    )


async def _notify_upload_accepted(
    matcher: Matcher,
    event: MessageEvent,
    *,
    fish: bool,
    lxns: bool,
) -> None:
    """上传发起后立即回复动态预计时间，不受紧凑消息开关影响。"""
    timing_key = upload_workflow_key(fish=fish, lxns=lxns)
    seconds, samples = processing_time_estimator.estimate(
        timing_key,
        fallback_seconds=upload_fallback_seconds(fish=fish, lxns=lxns),
    )
    external = " + 水鱼 + 落雪" if fish and lxns else (" + 水鱼" if fish else " + 落雪" if lxns else "")
    targets = "AWMCNET" + external
    message = (
        f"📤 已受理，正在上传到{targets}。\n"
        f"{format_processing_estimate(seconds, samples)}\n"
        "处理完成后会另行发送最终结果。"
    )
    try:
        await matcher.send(message, reply_message=True)
    except Exception as exc:
        log.warning(f"[upload] 发送受理与预计时间失败，继续上传：{_exception_detail(exc)}")


async def _refresh_b50_cache_after_upload(
    user_key: str, *, fish: bool, lxns: bool
) -> None:
    """上传成功后静默刷新对应数据源的 B50 与全量成绩缓存，不产生费用。"""
    try:
        qqid = int(user_key)
    except (TypeError, ValueError):
        return
    from ..libraries.maimaidx_datasource import get_user_b50, get_user_records
    from ..libraries.maimaidx_player_cache import (
        clear_fetch_meta,
        invalidate_player_cache,
    )

    sources = []
    if fish:
        sources.append("divingfish")
    if lxns:
        sources.append("lxns")

    # 查分器上传接口返回成功后仍可能有短暂的最终一致性窗口。每轮都先清理
    # 本地缓存，避免第一次读到旧成绩后把旧 B50 固化 15 分钟；最后一轮结果留在缓存。
    for attempt, delay in enumerate((0.0, 8.0, 12.0), start=1):
        if delay:
            await asyncio.sleep(delay)
        invalidate_player_cache(qqid)
        for source in sources:
            try:
                userinfo, records = await get_user_records(
                    qqid=qqid, force_source=source, force_refresh=True
                )
                from ..libraries.maimaidx_awmcnet_sync import sync_awmcnet
                await sync_awmcnet(qqid, userinfo, records, source=source)
                log.info(
                    f"[upload] 上传后已静默刷新全量成绩缓存 "
                    f"user={user_key},source={source},attempt={attempt}"
                )
            except Exception as exc:
                log.warning(
                    f"[upload] 上传已成功，但静默刷新全量成绩缓存失败 "
                    f"user={user_key},source={source},attempt={attempt}: "
                    f"{_exception_detail(exc)}"
                )
            try:
                await get_user_b50(
                    qqid=qqid, force_source=source, force_refresh=True
                )
                log.info(
                    f"[upload] 上传后已静默刷新 B50 缓存 "
                    f"user={user_key},source={source},attempt={attempt}"
                )
            except Exception as exc:
                log.warning(
                    f"[upload] 上传已成功，但静默刷新 B50 缓存失败 "
                    f"user={user_key},source={source},attempt={attempt}: "
                    f"{_exception_detail(exc)}"
                )
            finally:
                clear_fetch_meta()


async def _post_upload_maintenance(
    user_key: str,
    *,
    fish: bool,
    lxns: bool,
    archive_qqids: Optional[list[int]] = None,
) -> None:
    """上传结算后的缓存刷新与存档；不阻塞用户收到最终结果。"""
    await _refresh_b50_cache_after_upload(user_key, fish=fish, lxns=lxns)
    try:
        from ..libraries.maimaidx_dataset_archive import (
            archive_user_scores_for_dataset,
            collect_archive_qqids,
        )

        qqids = collect_archive_qqids(user_key, *(archive_qqids or []))
        # 上传后稍等查分器同步，再强制落盘（含 PC 兜底），拓展数据集
        await archive_user_scores_for_dataset(
            qqids,
            fish=fish,
            lxns=lxns,
            source="share_upload",
            retries=3,
            retry_delay=5.0,
            allow_playcount_fallback=True,
        )
    except Exception as exc:
        log.warning(
            f"[DataStorage] 上传后后台自动存档失败 user={user_key}: "
            f"{_exception_detail(exc)}"
        )


def _archive_qqids_for_event(event: MessageEvent, user_key: str) -> list[int]:
    from ..libraries.maimaidx_dataset_archive import collect_archive_qqids

    score_qq: Optional[int] = None
    try:
        score_qq = int(resolve_score_qqid(event))
    except Exception:
        score_qq = None
    return collect_archive_qqids(user_key, score_qq, billing_user_id(event))


def _schedule_post_upload_maintenance(
    user_key: str,
    *,
    fish: bool,
    lxns: bool,
    archive_qqids: Optional[list[int]] = None,
) -> None:
    # Invalidate synchronously before yielding the successful upload response.
    # Otherwise an immediate ``b50``/``刷新b50`` command can win the scheduling
    # race and resurrect the pre-upload SQLite/storage snapshot.
    try:
        from ..libraries.maimaidx_player_cache import invalidate_player_cache

        invalidate_player_cache(int(user_key))
    except (TypeError, ValueError):
        pass
    task = asyncio.create_task(
        _post_upload_maintenance(
            user_key,
            fish=fish,
            lxns=lxns,
            archive_qqids=archive_qqids,
        ),
        name=f"maimaidx-post-upload-{user_key}",
    )
    _post_upload_tasks.add(task)
    task.add_done_callback(_post_upload_tasks.discard)


@upload_fish.handle()
@upload_lx.handle()
@upload_all.handle()
async def _(
    matcher: Matcher, bot: Bot, event: MessageEvent, args: Message = CommandArg()
):
    fish, lxns = _upload_mode(matcher)
    try:
        preflight_error = _upload_preflight_error(event, fish=fish, lxns=lxns)
    except QBindRequiredError as exc:
        await matcher.finish(str(exc), reply_message=True)
        return
    if preflight_error:
        await matcher.finish(preflight_error, reply_message=False)
    raw = _arg_text(args)
    if raw and not extract_sgwcmaid_qrcode(raw):
        await matcher.finish("上传失败：二维码格式无效", reply_message=True)
    # 凭据安全优先：先撤回，再贴处理表情和执行任何网络请求。缓存已过期时
    # 尚处于等待新 SGID 阶段，不能提前发送"已受理"。
    qrcode = extract_sgwcmaid_qrcode(raw)
    if not qrcode and any(seg.type == 'image' for seg in args):
        qrcode = await extract_sgwcmaid_from_image_segments(args)
    recall_notice = ""
    if qrcode:
        recall_notice = await _recall_qrcode_message(bot, event)
        from .mai_announcement import enforce_current_announcement

        if not await enforce_current_announcement(bot, event):
            await matcher.finish(recall_notice, reply_message=False)
    await react_processing(bot, event)
    if _upload_can_start_now(
        event, fish=fish, lxns=lxns, qrcode_arg=raw
    ):
        await _notify_upload_accepted(matcher, event, fish=fish, lxns=lxns)
    timing_key = upload_workflow_key(fish=fish, lxns=lxns)
    started_at = time.perf_counter()
    result = await _upload(event, fish=fish, lxns=lxns, qrcode_arg=raw)
    if result.startswith("上传完成"):
        processing_time_estimator.record(
            timing_key, time.perf_counter() - started_at
        )
    if not _upload_retryable(result):
        await plugin_finish(
            matcher,
            recall_notice + result,
            event=event,
            reply_message=False,
            qq_buttons=_account_flow_shortcuts(event),
        )
    attempt = 1 if raw else 0
    matcher.state["upload_qrcode_retry"] = attempt
    track_event(session_key("upload_qrcode", event), event)
    await matcher.send(
        recall_notice + _upload_retry_prompt(result, attempt), reply_message=True
    )


@upload_fish.got("upload_qrcode")
@upload_lx.got("upload_qrcode")
@upload_all.got("upload_qrcode")
async def _(
    matcher: Matcher,
    bot: Bot,
    event: MessageEvent,
    qrcode_message: Message = Arg("upload_qrcode"),
):
    pending_key = session_key("upload_qrcode", event)
    raw = qrcode_message.extract_plain_text().strip()
    if raw.lower() in {"取消", "cancel", "q", "退出"}:
        finish_pending(pending_key)
        await matcher.finish("已取消成绩上传。", reply_message=True)
    fish, lxns = _upload_mode(matcher)
    try:
        preflight_error = _upload_preflight_error(event, fish=fish, lxns=lxns)
    except QBindRequiredError as exc:
        finish_pending(pending_key)
        await matcher.finish(str(exc), reply_message=True)
        return
    if preflight_error:
        finish_pending(pending_key)
        await matcher.finish(preflight_error, reply_message=False)
    qrcode = extract_sgwcmaid_qrcode(raw)
    if not qrcode and any(seg.type == 'image' for seg in qrcode_message):
        qrcode = await extract_sgwcmaid_from_image_segments(qrcode_message)
    recall_notice = ""
    if qrcode:
        recall_notice = await _recall_qrcode_message(bot, event)
        from .mai_announcement import enforce_current_announcement

        if not await enforce_current_announcement(bot, event):
            finish_pending(pending_key)
            await matcher.finish(recall_notice, reply_message=False)
    await react_processing(bot, event)
    if qrcode:
        await _notify_upload_accepted(matcher, event, fish=fish, lxns=lxns)
        timing_key = upload_workflow_key(fish=fish, lxns=lxns)
        started_at = time.perf_counter()
        result = await _upload(event, fish=fish, lxns=lxns, qrcode_arg=qrcode)
        if result.startswith("上传完成"):
            processing_time_estimator.record(
                timing_key, time.perf_counter() - started_at
            )
    else:
        result = "上传失败：二维码格式无效"
    if not _upload_retryable(result):
        finish_pending(pending_key)
        await plugin_finish(
            matcher,
            recall_notice + result,
            event=event,
            reply_message=False,
            qq_buttons=_account_flow_shortcuts(event),
        )
    attempt = int(matcher.state.get("upload_qrcode_retry", 0)) + 1
    matcher.state["upload_qrcode_retry"] = attempt
    if attempt >= 3:
        finish_pending(pending_key)
        await matcher.finish(
            recall_notice
            + _upload_retry_prompt(result, 3)
            + "\n已连续失败 3 次，本次上传流程结束，且不扣 BREAK。",
            reply_message=True,
        )
    track_event(pending_key, event)
    await matcher.reject(
        recall_notice + _upload_retry_prompt(result, attempt), reply_message=True
    )


@account_ping.handle()
async def _():
    try:
        result = await sw_api.health()
    except Exception as exc:
        await account_ping.finish(f"AWMC API 连接失败：{_exception_detail(exc)}")
    await account_ping.finish("AWMC API 连接正常\n" + _result_text(result))


async def _execute_ticket_now(
    event: MessageEvent,
    multiple: int,
    *,
    qrcode_override: str = "",
    notify: Optional[Callable[[str], Awaitable[Any]]] = None,
) -> str:
    """执行并结算发票；直发新二维码时会先更新绑定凭据。"""
    allowed = _allowed_ticket_multipliers()
    if multiple not in allowed:
        allowed_text = " / ".join(map(str, allowed))
        raise ValueError(f"票券倍率仅支持：{allowed_text}。")
    key = _user_key(event)
    binding = account_db.get(key)
    if binding is None or not (qrcode_override or binding.qrcode):
        raise RuntimeError("尚未绑定舞萌账号，请先使用：mai绑定 SGWCMAID...")
    cost = await _service_cost("ticket", multiple=multiple)
    await _ensure_service_affordable(int(key), "ticket", cost)
    credential = qrcode_override or binding.qrcode
    if notify is not None:
        await notify(_TICKET_IRREVERSIBLE_NOTICE)
    async with machine_session():
        try:
            binding, _ = await _read_verified_preview(
                binding, credential, save_qrcode=bool(qrcode_override)
            )
        except Exception as exc:
            if is_sw_api_quota_error(exc):
                raise
            if not qrcode_override or credential == binding.qrcode:
                account_db.mark_qrcode_result(key, False)
            raise TicketQrcodeError(
                "二维码已过期、失效或与当前绑定账号不一致"
            ) from exc
        try:
            before_charge = await sw_api.get_user_charge(binding.qrcode)
        except Exception as exc:
            if _is_sgid_expired_error(exc):
                account_db.mark_qrcode_result(key, False)
                raise TicketQrcodeError("二维码已过期，需重新发送最新 SGID") from exc
            raise
        before_ok, before_rows, before_free_rows = _normalize_charge_payload(
            before_charge
        )
        if not before_ok:
            raise RuntimeError("发票前无法读取票券库存，已取消提交以避免错误扣费")
        unused_stocks = _unused_ticket_stocks(before_rows + before_free_rows)
        if unused_stocks:
            raise UnusedTicketPenaltyError(unused_stocks)
        baseline_stock = _ticket_stock(before_rows + before_free_rows, multiple)
    async def execute_attempt(attempt: int) -> int:
        estimated, timing_samples = _ticket_estimate()
        if notify is not None:
            try:
                await notify(_ticket_wait_message(estimated, timing_samples))
            except Exception as exc:
                log.warning(
                    f"[ticket] 发送第 {attempt} 次预计消息失败，"
                    f"继续执行：{_exception_detail(exc)}"
                )

        timing_started_at = time.perf_counter()
        async with machine_session():
            try:
                result = await sw_api.charge_ticket(binding.qrcode, multiple)
            except Exception as exc:
                if _is_sgid_expired_error(exc):
                    account_db.mark_qrcode_result(key, False)
                    raise TicketQrcodeError("二维码已过期，需重新发送最新 SGID") from exc
                # 网络超时无法判断上游是否已执行，不能自动重试。
                raise RuntimeError(
                    f"发票请求状态未知：{_exception_detail(exc)}；"
                    "为避免重复发票，本次不会自动重试或扣 BREAK"
                ) from exc
            try:
                _ensure_business_success(result)
            except Exception as exc:
                if _is_sgid_expired_error(exc):
                    account_db.mark_qrcode_result(key, False)
                    raise TicketQrcodeError("二维码已过期，需重新发送最新 SGID") from exc
                raise TicketRetryableError(
                    f"发票被上游明确拒绝：{_exception_detail(exc)}"
                ) from exc

        try:
            return await _confirm_ticket_delivery(
                binding.qrcode,
                multiple,
                binding.mai_uid,
                baseline_stock,
            )
        except Exception as exc:
            if _is_sgid_expired_error(exc):
                account_db.mark_qrcode_result(key, False)
                raise TicketQrcodeError("二维码已过期，需重新发送最新 SGID") from exc
            raise
        # Only successful, fully confirmed tickets contribute to the estimate.
        # A rejected request or failed settlement must not make future waits
        # look faster or slower than real completed deliveries.
        elapsed = max(0.001, time.perf_counter() - timing_started_at)
        processing_time_estimator.record(_TICKET_TIMING_KEY, elapsed)

    verified_stock, attempts = await _run_ticket_with_retries(
        execute_attempt,
        notify=notify,
    )
    charge = await _settle_service_success(
        int(key),
        "ticket",
        cost,
        meta={
            "multiple": multiple,
            "baseline_stock": baseline_stock,
            "verified_stock": verified_stock,
            "attempts": attempts,
        },
    )
    ref = _log(
        key,
        "ticket",
        "success",
        f"multiple={multiple},stock={verified_stock},attempts={attempts},"
        f"charged={charge.charged},free={charge.free}",
    )
    clear_pending_ticket_retry(key)
    return (
        f"{multiple} 倍票已发放并确认到账（当前库存 {verified_stock} 张）。\n"
        + (f"自动重试 {attempts - 1} 次后成功。\n" if attempts > 1 else "")
        + f"{_charge_text(charge, int(key))}\nRef_ID: {ref}"
    )


async def _execute_ticket(
    event: MessageEvent,
    multiple: int,
    *,
    qrcode_override: str = "",
    notify: Optional[Callable[[str], Awaitable[Any]]] = None,
) -> str:
    """将发票请求放入全局串行队列，再执行实际发票流程。

    发票接口背后的机台资源是全局单通道。等待中的任务只占用队列计数，
    不占用 ``machine_session``，并在取消或异常时及时清理计数。
    """
    global _ticket_queue_waiting

    # ``asyncio.Lock.locked()`` can briefly be false after the active task
    # releases the lock but before its first waiter is resumed.  Include the
    # waiting count so a new request cannot miss the queue notification in
    # that hand-off window (the lock itself still guarantees FIFO ordering).
    queued = _ticket_queue_lock.locked() or _ticket_queue_waiting > 0
    if queued:
        # ``ahead`` 包含当前正在处理的任务以及已经排在本任务前面的等待者。
        _ticket_queue_waiting += 1
        ahead = _ticket_queue_waiting

    acquired = False
    try:
        if queued and notify is not None:
            try:
                await notify(_ticket_queue_wait_message(ahead))
            except Exception as exc:
                log.warning(
                    "[ticket] 发送排队通知失败，继续等待："
                    f"{_exception_detail(exc)}"
                )

        await _ticket_queue_lock.acquire()
        acquired = True
        if queued:
            _ticket_queue_waiting -= 1
        return await _execute_ticket_now(
            event,
            multiple,
            qrcode_override=qrcode_override,
            notify=notify,
        )
    finally:
        # 取消可能发生在 acquire() 或排队通知期间；此时任务还未减少等待数。
        if queued and not acquired:
            _ticket_queue_waiting -= 1
        if acquired:
            _ticket_queue_lock.release()


async def _ticket_failure_text(key: str, multiple: int, exc: Exception) -> str:
    """格式化发票失败；保留原有未使用票券处罚语义。"""
    if isinstance(exc, UnusedTicketPenaltyError):
        penalty = max(1, int(await asyncio.to_thread(
            break_db.get_config, "ticket_unused_penalty", "20"
        ) or 20))
        meta = {"unused_stocks": exc.stocks, "requested_multiple": multiple}
        consumed = await asyncio.to_thread(
            break_db.try_consume,
            int(key), penalty, "ticket_unused_penalty", meta=meta,
        )
        balance = await asyncio.to_thread(break_db.get_balance, int(key))
        if not consumed:
            ref = _log(
                key,
                "ticket_unused_penalty",
                "error",
                f"penalty={penalty},balance={balance},stocks={exc.stocks}",
            )
            return (
                "检测到账号还有未使用的倍率票，本次发票已拦截。\n"
                f"处罚需要 {penalty} BREAK，但当前仅有 {balance}。\nRef_ID: {ref}"
            )
        ref = _log(
            key,
            "ticket_unused_penalty",
            "success",
            f"penalty={penalty},balance={balance},stocks={exc.stocks}",
        )
        return (
            f"你智商可好？发一堆票和意为？已吃掉{penalty}个绝赞。\n"
            f"当前余额：{balance} BREAK\nRef_ID: {ref}"
        )
    detail = _exception_detail(exc)
    ref = _log(key, "ticket", "error", detail)
    return f"发票失败：{detail}\nRef_ID: {ref}"


async def continue_ticket_with_qrcode(
    event: MessageEvent,
    qrcode: str,
    pending: tuple[int, float],
    *,
    notify: Optional[Callable[[str], Awaitable[Any]]] = None,
) -> str:
    """用 180 秒窗口内直发的新二维码继续原发票，而不是触发自动上传。"""
    multiple, expires_at = pending
    key = _user_key(event)
    try:
        return await _execute_ticket(
            event, multiple, qrcode_override=qrcode, notify=notify
        )
    except TicketQrcodeError as exc:
        current = time.time()
        if expires_at > current:
            remember_pending_ticket_retry(
                key, multiple, expires_at=expires_at, now=current
            )
            remaining = max(1, int(expires_at - current + 0.999))
            suffix = f"请在剩余 {remaining} 秒内重新发送最新二维码。"
        else:
            suffix = "180 秒续发窗口已结束，请重新发送发票命令。"
        return await _ticket_failure_text(key, multiple, exc) + "\n" + suffix
    except Exception as exc:
        return await _ticket_failure_text(key, multiple, exc)


@account_ticket.handle()
async def _(event: MessageEvent, args: Message = CommandArg()):
    await _require_agreement(account_ticket, event)
    key, binding, error = _binding_or_error(event)
    if error or binding is None:
        if error and "二维码缓存" in error:
            raw_multiple = _arg_text(args) or "2"
            try:
                pending_multiple = int(raw_multiple)
            except ValueError:
                pending_multiple = 2
            remember_pending_ticket_retry(key, pending_multiple)
            await plugin_finish(
                account_ticket,
                _pending_qrcode_prompt("已过期，需刷新", "发票操作"),
                event=event,
                reply_message=True,
            )
        await plugin_finish(account_ticket, error or "账号未绑定", event=event)
    raw = _arg_text(args) or "2"
    try:
        multiple = int(raw)
    except ValueError:
        await plugin_finish(account_ticket, "倍率格式错误，用法：发票 2（或 fp 2）", event=event)
    allowed = _allowed_ticket_multipliers()
    if multiple not in allowed:
        allowed_text = " / ".join(map(str, allowed))
        await plugin_finish(account_ticket, f"票券倍率仅支持：{allowed_text}。", event=event)
    clear_pending_ticket_retry(key)
    try:
        cost = await _service_cost("ticket", multiple=multiple)
        await _ensure_service_affordable(int(key), "ticket", cost)
    except Exception as exc:
        await plugin_finish(
            account_ticket,
            await _ticket_failure_text(key, multiple, exc),
            event=event,
            reply_message=True,
        )
    async def notify(message: str) -> None:
        await plugin_send(account_ticket, message, event=event, reply_message=True)

    try:
        text = await _execute_ticket(event, multiple, notify=notify)
    except TicketQrcodeError as exc:
        remember_pending_ticket_retry(key, multiple)
        text = (
            await _ticket_failure_text(key, multiple, exc)
            + "\n请在 180 秒内重新发送最新 SGWCMAID、官方链接或二维码图片；"
            "Bot 将直接继续本次发票，不会绑定或上传 B50。"
        )
    except Exception as exc:
        text = await _ticket_failure_text(key, multiple, exc)
    await plugin_finish(
        account_ticket,
        text,
        event=event,
        reply_message=True,
        qq_buttons=_account_flow_shortcuts(event),
    )


@account_ticket_status.handle()
async def _(event: MessageEvent):
    try:
        text = await _run_paid_awmc_read(
            event,
            service="ticket_status",
            fetch=sw_api.get_user_charge,
            formatter=_format_ticket_status,
        )
    except Exception as exc:
        if isinstance(exc, QrcodeRefreshRequiredError):
            await plugin_finish(
                account_ticket_status,
                str(exc),
                event=event,
                reply_message=True,
            )
        detail = _exception_detail(exc)
        key = _user_key(event)
        ref = _log(key, "ticket_status", "error", detail)
        await plugin_finish(
            account_ticket_status,
            f"票券查询失败：{detail}\n本次不扣 BREAK\nRef_ID: {ref}",
            event=event,
            reply_message=True,
        )
    await plugin_finish(
        account_ticket_status,
        text,
        event=event,
        reply_message=True,
        qq_buttons=_account_flow_shortcuts(event),
    )


async def _run_paid_awmc_read(
    event: MessageEvent,
    *,
    service: str,
    fetch: Callable[[str], Awaitable[Any]],
    formatter: Callable[[Any], str],
) -> str:
    key, binding, error = _binding_or_error(event)
    if error or binding is None:
        if error and "二维码缓存" in error:
            remember_pending_account_retry(key, service)
            label = {
                "ticket_status": "票券查询",
                "awmc_preview": "账号预览查询",
                "awmc_items": "道具查询",
                "awmc_gate_status": "门状态查询",
                "awmc_game_event": "活动事件查询",
            }.get(service, "查询")
            raise QrcodeRefreshRequiredError(_pending_qrcode_prompt("已过期，需刷新", label))
        raise RuntimeError(error or "账号未绑定")
    cost = await _service_cost(service)
    await _ensure_service_affordable(int(key), service, cost)
    try:
        async with machine_session():
            result = await fetch(binding.qrcode)
    except Exception as exc:
        if _is_sgid_expired_error(exc):
            label = {
                "ticket_status": "票券查询",
                "awmc_preview": "账号预览查询",
                "awmc_items": "道具查询",
                "awmc_gate_status": "门状态查询",
                "awmc_game_event": "活动事件查询",
            }.get(service, "查询")
            _raise_sgid_refresh_required(key, service, {}, label)
        raise
    text = formatter(result)
    charge = await _settle_service_success(
        int(key), service, cost, meta={"operation": service}
    )
    ref = _log(key, service, "success", f"charged={charge.charged}")
    return f"{text}\n\n{_charge_text(charge, int(key))}\nRef_ID: {ref}"


async def continue_pending_account_retry(
    event: MessageEvent,
    qrcode: str,
    pending: tuple[str, dict[str, Any], float],
) -> str:
    """验证新二维码后继续一个已挂起的账号 API 操作。"""
    operation, payload, expires_at = pending
    key = _user_key(event)
    if expires_at <= time.time():
        return "二维码续跑窗口已结束，请重新发送原命令。"
    binding = account_db.get(key)
    if binding is None:
        return "尚未绑定舞萌账号，请先使用 mai绑定。"
    try:
        async with machine_session():
            await _read_verified_preview(binding, qrcode, save_qrcode=True)
    except Exception as exc:
        remember_pending_account_retry(key, operation, payload, expires_at=expires_at)
        if is_sw_api_quota_error(exc):
            ref = _log(key, operation, "error", _exception_detail(exc))
            return (
                f"❌ 自动继续{operation}暂时无法完成：{_exception_detail(exc)}\n"
                f"原操作仍在续跑窗口内保留，本次不扣 BREAK。\nRef_ID: {ref}"
            )
        if _is_sgid_expired_error(exc):
            account_db.mark_qrcode_result(key, False)
            return _pending_qrcode_prompt("已过期，需刷新", "原操作")
        return _pending_qrcode_prompt("验证失败，请重新获取", "原操作")

    try:
        if operation in {"awmc_preview", "awmc_items", "awmc_gate_status", "awmc_game_event"}:
            fetchers = {
                "awmc_preview": (sw_api.get_user_preview, _format_user_preview),
                "awmc_items": (sw_api.get_user_items, _format_user_items),
                "awmc_gate_status": (sw_api.get_user_kaleidx_scope, _format_gate_status),
                "awmc_game_event": (sw_api.get_user_game_event, _format_user_game_event),
            }
            fetch, formatter = fetchers[operation]
            return await _run_paid_awmc_read(
                event, service=operation, fetch=fetch, formatter=formatter
            )
        if operation == "ticket_status":
            return await _run_paid_awmc_read(
                event,
                service=operation,
                fetch=sw_api.get_user_charge,
                formatter=_format_ticket_status,
            )
        if operation == "region":
            try:
                result = await sw_api.get_user_region(qrcode)
            except Exception as exc:
                if _is_sgid_expired_error(exc):
                    _raise_sgid_refresh_required(key, operation, payload, "地区查询")
                raise
            return format_user_region_block(result)
        if operation in {"awmc_music_upsert", "awmc_music_delete"}:
            return await _run_music_write(
                event,
                service=operation,
                music=payload.get("music"),
                level=int(payload.get("level")),
                score=payload.get("score"),
            )
        if operation == "awmc_item_upsert":
            return await _run_item_upsert(
                event,
                item_kind=payload.get("item_kind"),
                item_id=payload.get("item_id"),
                operation=str(payload.get("operation") or ""),
            )
        return "二维码已刷新，但原操作类型已失效，请重新发送原命令。"
    except QrcodeRefreshRequiredError as exc:
        return str(exc)
    except Exception as exc:
        ref = _log(key, operation, "error", _exception_detail(exc))
        return (
            f"❌ 自动继续{operation}失败：{_exception_detail(exc)}\n"
            f"本次不扣 BREAK，请重新发送原命令。\nRef_ID: {ref}"
        )


async def _run_music_write(
    event: MessageEvent,
    *,
    service: str,
    music,
    level: int,
    score: Optional[dict] = None,
) -> str:
    key, binding, error = _binding_or_error(event)
    if error or binding is None:
        if error and "二维码缓存" in error:
            remember_pending_account_retry(
                key,
                service,
                {"music": music, "level": level, "score": score},
            )
            label = "成绩编辑" if score is not None else "成绩删除"
            raise QrcodeRefreshRequiredError(
                _pending_qrcode_prompt("已过期，需刷新", label)
            )
        raise RuntimeError(error or "账号未绑定")
    cost = await _service_cost(service)
    await _ensure_service_affordable(int(key), service, cost)
    try:
        async with machine_session():
            if service == "awmc_music_upsert":
                if score is None:
                    raise RuntimeError("缺少成绩数据")
                await sw_api.upsert_music(binding.qrcode, score)
            elif service == "awmc_music_delete":
                await sw_api.delete_music(binding.qrcode, int(music.id), level)
            else:
                raise RuntimeError(f"不支持的成绩写入服务：{service}")
    except Exception as exc:
        if _is_sgid_expired_error(exc):
            label = "成绩编辑" if score is not None else "成绩删除"
            _raise_sgid_refresh_required(
                key,
                service,
                {"music": music, "level": level, "score": score},
                label,
            )
        raise

    try:
        from ..libraries.maimaidx_player_cache import invalidate_player_cache
        invalidate_player_cache(int(key))
    except (TypeError, ValueError):
        pass

    charge = await _settle_service_success(
        int(key),
        service,
        cost,
        meta={"music_id": int(music.id), "level": level},
    )
    mode = "简单模式" if score and score.get("fuzzy") else "专业模式"
    action = "已写入" if score is not None else "已删除"
    detail = f"music_id={music.id},level={level},charged={charge.charged}"
    ref = _log(key, service, "success", detail)
    lines = [
        f"✅ {action}《{music.title}》 {_DIFFICULTY_LABELS[level]} 成绩"
    ]
    if score is not None:
        dx_label = "DX 星级" if score["fuzzy"] else "实际 DX 分"
        lines.append(
            f"{mode} · {score['achievement']}% · {dx_label} {score['dxScore']} · "
            f"{str(score['comboStatus']).upper()} / {str(score['syncStatus']).upper()}"
        )
    lines.extend([_charge_text(charge, int(key)), f"Ref_ID: {ref}"])
    return "\n".join(lines)


async def _finish_music_write_error(matcher, event: MessageEvent, service: str, exc: Exception):
    if isinstance(exc, QrcodeRefreshRequiredError):
        await matcher.finish(str(exc), reply_message=True)
    detail = _exception_detail(exc)
    ref = _log(_user_key(event), service, "error", detail)
    await matcher.finish(
        f"操作失败：{detail}\n本次不扣 BREAK\nRef_ID: {ref}",
        reply_message=True,
    )


async def _run_item_upsert(
    event: MessageEvent,
    *,
    item_kind: Optional[int] = None,
    item_id: Optional[int] = None,
    operation: str = "",
) -> str:
    service = "awmc_item_upsert"
    key, binding, error = _binding_or_error(event)
    if error or binding is None:
        if error and "二维码缓存" in error:
            payload = {
                "item_kind": item_kind,
                "item_id": item_id,
                "operation": operation,
            }
            remember_pending_account_retry(key, service, payload)
            raise QrcodeRefreshRequiredError(
                _pending_qrcode_prompt("已过期，需刷新", "道具修改")
            )
        raise RuntimeError(error or "账号未绑定")
    cost = await _service_cost(service)
    await _ensure_service_affordable(int(key), service, cost)
    try:
        async with machine_session():
            if item_kind is None or item_id is None:
                raise RuntimeError("缺少道具参数")
            await sw_api.upsert_item(
                binding.qrcode, item_kind, item_id, operation
            )
            meta = {
                "item_kind": item_kind,
                "item_id": item_id,
                "operation": operation,
            }
            action = "添加" if operation == "add" else "删除"
            label = _ITEM_KIND_LABELS.get(item_kind, f"未知类型 {item_kind}")
            result_text = f"✅ 已提交{action}道具：{label} · itemId={item_id}"
            result_text += f"\n{_ITEM_UPSERT_SUCCESS_NOTE}"
            if item_kind == 4:
                result_text += f"\n{_COLLECTION_UPSERT_TICKET_WARNING}"
    except Exception as exc:
        if _is_sgid_expired_error(exc):
            _raise_sgid_refresh_required(
                key,
                service,
                {
                    "item_kind": item_kind,
                    "item_id": item_id,
                    "operation": operation,
                },
                "道具修改",
            )
        raise
    charge = await _settle_service_success(int(key), service, cost, meta=meta)
    ref = _log(
        key,
        service,
        "success",
        ",".join(f"{name}={value}" for name, value in meta.items())
        + f",charged={charge.charged}",
    )
    return f"{result_text}\n{_charge_text(charge, int(key))}\nRef_ID: {ref}"


@account_preview.handle()
async def _(event: MessageEvent):
    await _require_agreement(account_preview, event)
    try:
        text = await _run_paid_awmc_read(
            event,
            service="awmc_preview",
            fetch=sw_api.get_user_preview,
            formatter=_format_user_preview,
        )
    except Exception as exc:
        if isinstance(exc, QrcodeRefreshRequiredError):
            await plugin_finish(account_preview, str(exc), event=event, reply_message=True)
        detail = _exception_detail(exc)
        ref = _log(_user_key(event), "awmc_preview", "error", detail)
        await plugin_finish(
            account_preview,
            f"账号预览查询失败：{detail}\n本次不扣 BREAK\nRef_ID: {ref}",
            event=event,
            reply_message=True,
        )
    await plugin_finish(
        account_preview,
        text,
        event=event,
        reply_message=True,
        qq_buttons=_account_flow_shortcuts(event),
    )


@account_items.handle()
async def _(event: MessageEvent):
    await _require_agreement(account_items, event)
    try:
        text = await _run_paid_awmc_read(
            event,
            service="awmc_items",
            fetch=sw_api.get_user_items,
            formatter=_format_user_items,
        )
    except Exception as exc:
        if isinstance(exc, QrcodeRefreshRequiredError):
            await plugin_finish(account_items, str(exc), event=event, reply_message=True)
        detail = _exception_detail(exc)
        ref = _log(_user_key(event), "awmc_items", "error", detail)
        await plugin_finish(
            account_items,
            f"道具查询失败：{detail}\n本次不扣 BREAK\nRef_ID: {ref}",
            event=event,
            reply_message=True,
        )
    await plugin_finish(
        account_items,
        text,
        event=event,
        reply_message=True,
        qq_buttons=_account_flow_shortcuts(event),
    )


@account_gate_status.handle()
async def _(event: MessageEvent):
    await _require_agreement(account_gate_status, event)
    try:
        text = await _run_paid_awmc_read(
            event,
            service="awmc_gate_status",
            fetch=sw_api.get_user_kaleidx_scope,
            formatter=_format_gate_status,
        )
    except Exception as exc:
        if isinstance(exc, QrcodeRefreshRequiredError):
            await plugin_finish(account_gate_status, str(exc), event=event, reply_message=True)
        detail = _exception_detail(exc)
        ref = _log(_user_key(event), "awmc_gate_status", "error", detail)
        await plugin_finish(
            account_gate_status,
            f"门状态查询失败：{detail}\n本次不扣 BREAK\nRef_ID: {ref}",
            event=event,
            reply_message=True,
        )
    await plugin_finish(
        account_gate_status,
        text,
        event=event,
        reply_message=True,
        qq_buttons=_account_flow_shortcuts(event),
    )


@account_game_event.handle()
async def _(event: MessageEvent):
    await _require_agreement(account_game_event, event)
    try:
        text = await _run_paid_awmc_read(
            event,
            service="awmc_game_event",
            fetch=sw_api.get_user_game_event,
            formatter=_format_user_game_event,
        )
    except Exception as exc:
        if isinstance(exc, QrcodeRefreshRequiredError):
            await plugin_finish(account_game_event, str(exc), event=event, reply_message=True)
        detail = _exception_detail(exc)
        ref = _log(_user_key(event), "awmc_game_event", "error", detail)
        await plugin_finish(
            account_game_event,
            f"活动事件查询失败：{detail}\n本次不扣 BREAK\nRef_ID: {ref}",
            event=event,
            reply_message=True,
        )
    await plugin_finish(
        account_game_event,
        text,
        event=event,
        reply_message=True,
        qq_buttons=_account_flow_shortcuts(event),
    )


@account_music_upsert.handle()
async def _(matcher: Matcher, event: MessageEvent, args: Message = CommandArg()):
    await _require_agreement(account_music_upsert, event)
    raw = _arg_text(args)
    if not raw:
        try:
            key, binding, error = _binding_for_write_preflight(event)
            if error or binding is None:
                raise RuntimeError(error or "账号未绑定")
            await _ensure_service_affordable(
                int(key), "awmc_music_upsert", await _service_cost("awmc_music_upsert")
            )
        except Exception as exc:
            await _finish_music_write_error(
                account_music_upsert, event, "awmc_music_upsert", exc
            )
        await matcher.send(
            "成绩编辑为高价写操作，成功后消耗 75 BREAK。\n"
            "默认简单模式：DX 分填写星级 0～5；填写大于 5 的实际 DX 分会自动使用专业模式。",
            reply_message=True,
        )
        return
    try:
        music, level, score = _parse_music_upsert_command(raw)
    except Exception as exc:
        await _finish_music_write_error(
            account_music_upsert, event, "awmc_music_upsert", exc
        )
    matcher.state["edit_music"] = music
    matcher.state["edit_level"] = level
    matcher.state["edit_score"] = score
    matcher.set_arg("edit_song", Message(str(music.id)))
    matcher.set_arg("edit_difficulty", Message(str(level)))
    matcher.set_arg("edit_score", Message(raw))
    dx_label = "DX 星级" if score.get("fuzzy") else "实际 DX 分"
    mode = "简单模式" if score.get("fuzzy") else "专业模式"
    await matcher.send(
        f"⚠️ 即将写入《{music.title}》 {_DIFFICULTY_LABELS[level]} 成绩\n"
        f"{mode} · {score['achievement']}% · {dx_label} {score['dxScore']} · "
        f"{str(score['comboStatus']).upper()} / {str(score['syncStatus']).upper()}\n"
        "成功后消耗 75 BREAK。",
        reply_message=True,
    )


@account_music_upsert.got(
    "edit_song",
    prompt="请输入歌曲名、别名或歌曲 ID；发送“取消”退出：",
)
async def _(matcher: Matcher, message: Message = Arg("edit_song")):
    raw = _arg_text(message)
    if _is_interaction_cancel(raw):
        await account_music_upsert.finish("已取消成绩编辑，本次不扣 BREAK。")
    try:
        music = _resolve_account_music(raw)
    except ValueError as exc:
        await matcher.reject(str(exc))
    matcher.state["edit_music"] = music


@account_music_upsert.got(
    "edit_difficulty",
    prompt=(
        "请选择难度（数字 0 BASIC / 1 ADVANCED / 2 EXPERT / 3 MASTER / "
        "4 Re:MASTER；没有 Re:MASTER 的歌曲只能选 0-3；也可发送绿/黄/红/紫/白；"
        "宴谱发送宴）；发送“取消”退出："
    ),
)
async def _(matcher: Matcher, message: Message = Arg("edit_difficulty")):
    raw = _arg_text(message)
    if _is_interaction_cancel(raw):
        await account_music_upsert.finish("已取消成绩编辑，本次不扣 BREAK。")
    music = matcher.state["edit_music"]
    level = _parse_difficulty(raw)
    if level is None:
        await matcher.reject("无法识别难度，请重新发送。")
    try:
        _validate_music_difficulty(music, level)
    except ValueError as exc:
        await matcher.reject(str(exc))
    matcher.state["edit_level"] = level


@account_music_upsert.got(
    "edit_score",
    prompt=(
        "请输入：<达成率> <DX分> [FC] [FS] [简单/专业]\n"
        "例如：100.5% 5 AP FDX（自动简单模式）\n"
        "或：100.5% 2100 AP FDX 专业\n"
        "发送“取消”退出："
    ),
)
async def _(matcher: Matcher, event: MessageEvent, message: Message = Arg("edit_score")):
    raw = _arg_text(message)
    if _is_interaction_cancel(raw):
        await account_music_upsert.finish("已取消成绩编辑，本次不扣 BREAK。")
    music = matcher.state["edit_music"]
    level = matcher.state["edit_level"]
    try:
        score = _parse_score_options(raw.split(), music, level)
    except ValueError as exc:
        await matcher.reject(str(exc))
    matcher.state["edit_score"] = score
    dx_label = "DX 星级" if score.get("fuzzy") else "实际 DX 分"
    mode = "简单模式" if score.get("fuzzy") else "专业模式"
    await matcher.send(
        f"⚠️ 即将写入《{music.title}》 {_DIFFICULTY_LABELS[level]} 成绩\n"
        f"{mode} · {score['achievement']}% · {dx_label} {score['dxScore']} · "
        f"{str(score['comboStatus']).upper()} / {str(score['syncStatus']).upper()}\n"
        "成功后消耗 75 BREAK。",
        reply_message=True,
    )


@account_music_upsert.got(
    "edit_confirm",
    prompt="确认请发送“确认修改”；发送其他内容或“取消”退出：",
)
async def _(matcher: Matcher, event: MessageEvent, message: Message = Arg("edit_confirm")):
    raw = _arg_text(message)
    if _is_interaction_cancel(raw) or raw != "确认修改":
        await account_music_upsert.finish("已取消成绩编辑，本次不扣 BREAK。")
    music = matcher.state["edit_music"]
    level = matcher.state.get("edit_level")
    score = matcher.state.get("edit_score")
    if level is None or score is None:
        await account_music_upsert.finish("会话已过期，请重新发起改成绩。")
    try:
        text = await _run_music_write(
            event,
            service="awmc_music_upsert",
            music=music,
            level=int(level),
            score=score,
        )
    except Exception as exc:
        await _finish_music_write_error(
            account_music_upsert, event, "awmc_music_upsert", exc
        )
    await account_music_upsert.finish(text, reply_message=True)


@account_music_delete.handle()
async def _(matcher: Matcher, event: MessageEvent, args: Message = CommandArg()):
    await _require_agreement(account_music_delete, event)
    raw = _arg_text(args)
    if not raw:
        try:
            key, binding, error = _binding_for_write_preflight(event)
            if error or binding is None:
                raise RuntimeError(error or "账号未绑定")
            await _ensure_service_affordable(
                int(key), "awmc_music_delete", await _service_cost("awmc_music_delete")
            )
        except Exception as exc:
            await _finish_music_write_error(
                account_music_delete, event, "awmc_music_delete", exc
            )
        await matcher.send(
            "成绩删除成功后消耗 50 BREAK。删除后无法由 Bot 自动恢复。",
            reply_message=True,
        )
        return
    try:
        music, level = _parse_music_delete_command(raw)
        _validate_music_difficulty(music, level)
    except Exception as exc:
        await _finish_music_write_error(
            account_music_delete, event, "awmc_music_delete", exc
        )
    matcher.state["delete_music"] = music
    matcher.state["delete_level"] = level
    matcher.set_arg("delete_song", Message(str(music.id)))
    matcher.set_arg("delete_difficulty", Message(str(level)))
    await matcher.send(
        f"⚠️ 即将删除《{music.title}》 {_DIFFICULTY_LABELS[level]} 成绩，"
        "删除后无法由 Bot 自动恢复，成功后消耗 50 BREAK。",
        reply_message=True,
    )


@account_music_delete.got(
    "delete_song",
    prompt="请输入要删除成绩的歌曲名、别名或歌曲 ID；发送“取消”退出：",
)
async def _(matcher: Matcher, message: Message = Arg("delete_song")):
    raw = _arg_text(message)
    if _is_interaction_cancel(raw):
        await account_music_delete.finish("已取消成绩删除，本次不扣 BREAK。")
    try:
        music = _resolve_account_music(raw)
    except ValueError as exc:
        await matcher.reject(str(exc))
    matcher.state["delete_music"] = music


@account_music_delete.got(
    "delete_difficulty",
    prompt=(
        "请选择要删除的难度（数字 0 BASIC / 1 ADVANCED / 2 EXPERT / 3 MASTER / "
        "4 Re:MASTER；没有 Re:MASTER 的歌曲只能选 0-3；或绿/黄/红/紫/白；"
        "宴谱发送宴）；发送“取消”退出："
    ),
)
async def _(matcher: Matcher, message: Message = Arg("delete_difficulty")):
    raw = _arg_text(message)
    if _is_interaction_cancel(raw):
        await account_music_delete.finish("已取消成绩删除，本次不扣 BREAK。")
    music = matcher.state["delete_music"]
    level = _parse_difficulty(raw)
    if level is None:
        await matcher.reject("无法识别难度，请重新发送。")
    try:
        _validate_music_difficulty(music, level)
    except ValueError as exc:
        await matcher.reject(str(exc))
    matcher.state["delete_level"] = level
    await matcher.send(
        f"⚠️ 即将删除《{music.title}》 {_DIFFICULTY_LABELS[level]} 成绩，"
        "删除后无法由 Bot 自动恢复，成功后消耗 50 BREAK。",
        reply_message=True,
    )


@account_music_delete.got(
    "delete_confirm",
    prompt="确认请发送“确认删除”；发送其他内容或“取消”退出：",
)
async def _(matcher: Matcher, event: MessageEvent, message: Message = Arg("delete_confirm")):
    raw = _arg_text(message)
    if _is_interaction_cancel(raw) or raw != "确认删除":
        await account_music_delete.finish("已取消成绩删除，本次不扣 BREAK。")
    music = matcher.state["delete_music"]
    level = matcher.state.get("delete_level")
    if level is None:
        await account_music_delete.finish("会话已过期，请重新发起删成绩。")
    try:
        text = await _run_music_write(
            event, service="awmc_music_delete", music=music, level=int(level)
        )
    except Exception as exc:
        await _finish_music_write_error(
            account_music_delete, event, "awmc_music_delete", exc
        )
    await account_music_delete.finish(text, reply_message=True)


@account_item_upsert.handle()
async def _(matcher: Matcher, event: MessageEvent, args: Message = CommandArg()):
    await _require_agreement(account_item_upsert, event)
    try:
        key, binding, error = _binding_for_write_preflight(event)
        if error or binding is None:
            raise RuntimeError(error or "账号未绑定")
        await _ensure_service_affordable(
            int(key), "awmc_item_upsert", await _service_cost("awmc_item_upsert")
        )
    except Exception as exc:
        await _finish_music_write_error(
            account_item_upsert, event, "awmc_item_upsert", exc
        )
    raw = _arg_text(args)
    if raw:
        try:
            kind, item_id, operation = _parse_item_upsert_command(raw)
        except ValueError as exc:
            await account_item_upsert.finish(str(exc), reply_message=True)
        matcher.set_arg("item_kind", Message(str(kind)))
        matcher.set_arg("item_id", Message(str(item_id)))
        matcher.set_arg("item_operation", Message(operation))
    await matcher.send(
        "⚠️ 道具修改功能未经实际账号测试，可能造成数据异常或不可逆后果。\n"
        "继续操作即表示风险由用户自行承担；成功后消耗 100 BREAK。",
        reply_message=True,
    )


@account_item_upsert.got(
    "item_kind",
    prompt=(
        "请输入 itemKind 数字或类型名称：\n"
        "1姓名框 / 2称号 / 3头像 / 4收藏品 / 5乐曲 / "
        "6 MASTER谱面解锁 / 7 Re:MASTER谱面解锁 / 9角色 / "
        "10搭档 / 11边框 / 12票券\n"
        "发送“取消”或 00 退出"
    ),
)
async def _(matcher: Matcher, message: Message = Arg("item_kind")):
    raw = _arg_text(message)
    if _is_interaction_cancel(raw):
        await account_item_upsert.finish("已取消道具修改，本次不扣 BREAK。")
    try:
        kind = _parse_item_kind(raw)
    except ValueError as exc:
        await matcher.reject(str(exc))
    matcher.state["item_kind_value"] = kind


@account_item_upsert.got(
    "item_id",
    prompt=(
        "请输入要操作的 乐曲ID/乐曲名/别名（5/6/7 类）或 itemId（其他类型，正整数）；"
        "发送“取消”或 00 退出："
    ),
)
async def _(matcher: Matcher, message: Message = Arg("item_id")):
    raw = _arg_text(message)
    if _is_interaction_cancel(raw):
        await account_item_upsert.finish("已取消道具修改，本次不扣 BREAK。")
    kind = matcher.state["item_kind_value"]
    music = None
    if kind in _MUSIC_ITEM_KINDS:
        try:
            music = _resolve_item_music(raw, kind)
        except ValueError as exc:
            await matcher.reject(str(exc) + " 请重新输入歌曲 ID、歌曲名或别名。")
        item_id = int(music.id)
        matcher.state["item_music"] = music
    else:
        try:
            item_id = int(raw)
            if item_id <= 0:
                raise ValueError
        except ValueError:
            await matcher.reject("itemId 必须是正整数，请重新发送。")
    matcher.state["item_id_value"] = item_id


@account_item_upsert.got(
    "item_operation",
    prompt="请选择操作：add（添加）或 del（删除）；发送“取消”或 00 退出：",
)
async def _(
    matcher: Matcher,
    event: MessageEvent,
    message: Message = Arg("item_operation"),
):
    raw = _arg_text(message)
    if _is_interaction_cancel(raw):
        await account_item_upsert.finish("已取消道具修改，本次不扣 BREAK。")
    try:
        operation = _parse_item_operation(raw)
    except ValueError as exc:
        await matcher.reject(str(exc))
    matcher.state["item_operation_value"] = operation
    kind = matcher.state["item_kind_value"]
    item_id = matcher.state["item_id_value"]
    action = "添加" if operation == "add" else "删除"
    label = _ITEM_KIND_LABELS.get(kind, f"未知类型 {kind}")
    music = matcher.state.get("item_music")
    if music is not None:
        target = f"乐曲名为《{music.title}》，乐曲ID {music.id}"
    else:
        target = f"itemId={item_id}"
    await plugin_send(
        matcher,
        f"即将{action}：{label}（itemKind={kind}），{target}。\n"
        "该功能未经测试，风险由用户自行承担。",
        event=event,
        reply_message=True,
    )


@account_item_upsert.got(
    "item_risk_confirm",
    prompt=(
        "确认承担风险并继续，请发送“我已知晓风险”；"
        "发送“取消”、00 或其他内容取消："
    ),
)
async def _(matcher: Matcher, event: MessageEvent, message: Message = Arg("item_risk_confirm")):
    if _arg_text(message) != "我已知晓风险":
        await account_item_upsert.finish("已取消道具修改，本次不扣 BREAK。")
    await plugin_send(
        matcher,
        "✅ 已确认风险，正在提交道具修改，请稍候……",
        event=event,
        reply_message=True,
    )
    try:
        text = await _run_item_upsert(
            event,
            item_kind=matcher.state["item_kind_value"],
            item_id=matcher.state["item_id_value"],
            operation=matcher.state["item_operation_value"],
        )
    except Exception as exc:
        await _finish_music_write_error(
            account_item_upsert, event, "awmc_item_upsert", exc
        )
    await account_item_upsert.finish(text, reply_message=True)


@account_region.handle()
async def _(event: MessageEvent):
    key, binding, error = _binding_or_error(event)
    if error or binding is None:
        if error and "二维码缓存" in error:
            remember_pending_account_retry(key, "region")
            await plugin_finish(
                account_region,
                _pending_qrcode_prompt("已过期，需刷新", "地区查询"),
                event=event,
                reply_message=True,
            )
        await plugin_finish(account_region, error or "账号未绑定", event=event)
    try:
        result = await sw_api.get_user_region(binding.qrcode)
    except Exception as exc:
        if _is_sgid_expired_error(exc):
            account_db.mark_qrcode_result(key, False)
            remember_pending_account_retry(key, "region")
            await plugin_finish(
                account_region,
                _pending_qrcode_prompt("已过期，需刷新", "地区查询"),
                event=event,
                reply_message=True,
            )
        await plugin_finish(
            account_region,
            f"查询失败：{_exception_detail(exc)}",
            event=event,
        )
    # 与 maibot 一致：用 regionId → WAHLAP_REGIONS 映射省份名，勿依赖 regionName。
    await plugin_finish(
        account_region,
        format_user_region_block(result),
        event=event,
        qq_buttons=_account_flow_shortcuts(event),
    )


@account_opt.handle()
async def _(event: MessageEvent, args: Message = CommandArg()):
    title_ver = _arg_text(args)
    if not title_ver:
        await plugin_finish(account_opt, "用法：mai查询opt <titleVer>", event=event)
    try:
        result = await sw_api.get_opt(title_ver)
    except Exception as exc:
        await plugin_finish(
            account_opt,
            f"查询失败：{_exception_detail(exc)}",
            event=event,
        )
    await plugin_finish(
        account_opt, json.dumps(result, ensure_ascii=False, indent=2)[:3000], event=event
    )
