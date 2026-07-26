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
from ..libraries.maimaidx_group_rating import build_forward_node
from ..libraries.maimaidx_lxns_client import (
    LxnsApiError,
    convert_pc_records_to_lxns_scores,
    convert_sega_music_scores,
    user_upload_scores,
)
from ..libraries.maimaidx_lxns_db import lxns_db
from ..libraries.maimaidx_machine_session import (
    MachineBusyError,
    machine_session,
)
from ..libraries.maimaidx_platform import billing_user_id, resolve_score_qqid
from ..libraries.maimaidx_playcount_db import pc_db
from ..libraries.maimaidx_qrcode_util import extract_sgwcmaid_qrcode
from ..libraries.maimaidx_pending_session import finish_pending, session_key, track_event
from ..libraries.maimaidx_processing_time import (
    format_processing_estimate,
    processing_time_estimator,
    upload_fallback_seconds,
    upload_workflow_key,
)
from ..libraries.maimaidx_reaction import react_processing
from ..libraries.maimaidx_status_api import build_live_status_payload
from ..libraries.maimaidx_sw_api import format_user_region_block, sw_api
from .mai_agreement import agreement_prompt, has_user_agreed

account_help = on_command("mai账号", aliases={"账号帮助", "mai账户"})
account_bind = on_command("mai绑定", aliases={"绑定舞萌", "舞萌绑定", "maibind"})
account_unbind = on_command("mai解绑", aliases={"解绑舞萌", "舞萌解绑"})
account_status = on_command("mai状态", aliases={"mymai"})
maimai_live_status = on_command("舞萌状态", aliases={"mais"})
fish_bind = on_command(
    "mai绑定水鱼", aliases={"dfbind", "绑定水鱼token", "maibindfish"}
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
account_opt = on_command("mai查询opt", aliases={"查询opt"})
account_queue = on_command("maiqueue", aliases={"mai队列"})

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
    account_opt,
    account_queue,
):
    setattr(_serial_account_matcher, '_maimaidx_serial_user_operation', True)
# 舞萌状态只读公开 Uptime 与全局失败率 API，不占用机台串行锁。

_RECALL_FAILED_NOTICE = "⚠️ Bot 无法撤回该凭据消息，请立即手动撤回。\n"
_QRCODE_RECALL_TIMEOUT_SECONDS = 3.0
_TICKET_QRCODE_RETRY_SECONDS = 180
_TICKET_QUEUE_UNIT_TIMING_KEY = "ticket_queue:seconds_per_request"
_pending_ticket_retries: dict[str, tuple[int, float]] = {}
_DIVING_FISH_PROBER_URL = "https://www.diving-fish.com/maimaidx/prober/"
_FISH_TOKEN_MIN_LENGTH = 127
_post_upload_tasks: set[asyncio.Task] = set()
_FISH_TOKEN_MAX_LENGTH = 132
_ACCOUNT_SETUP_GUIDE = (
    "尚未建立账号记录，请按以下步骤完成：\n"
    "1. 发送「mai绑定」，再提交最新的 SGWCMAID 字符串；\n"
    "2. 按需发送「mai绑定水鱼 <Token>」或「mai绑定落雪 <导入Token>」；\n"
    "3. 使用 maiu / maiul / maiua 上传水鱼 / 落雪 / 两边。"
)


def _user_key(event: MessageEvent) -> str:
    return str(billing_user_id(event))


def _arg_text(args: Message) -> str:
    return args.extract_plain_text().strip()


async def _recall_qrcode_message(bot: Bot, event: MessageEvent) -> str:
    """限时撤回二维码消息，避免 OneBot 无响应时卡住后续上传。"""
    started_at = time.perf_counter()
    try:
        await asyncio.wait_for(
            bot.delete_msg(message_id=event.message_id),
            timeout=_QRCODE_RECALL_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        log.warning(
            f"[upload] 二维码消息撤回失败：{type(exc).__name__} "
            f"({time.perf_counter() - started_at:.2f}s)"
        )
        return _RECALL_FAILED_NOTICE
    log.info(
        f"[upload] 二维码消息已撤回 "
        f"({time.perf_counter() - started_at:.2f}s)"
    )
    return ""


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
        for charge_id in (2, 3, 5)
        if (stock := _ticket_stock(valid_rows, charge_id)) > 0
    }


class UnusedTicketPenaltyError(RuntimeError):
    def __init__(self, stocks: dict[int, int]):
        self.stocks = stocks
        super().__init__("账号仍有未使用的倍率票")


class TicketQrcodeError(RuntimeError):
    """发票使用的二维码不可用；调用方可登记一次限时重试。"""


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


def _matching_charge_task(
    payload: dict, charge_id: int, mai_uid: str, task_id: str = ""
) -> Optional[dict]:
    """从服务端发票队列中找当前账号、当前倍率最新的一笔任务。"""
    tasks = payload.get("tasks") if isinstance(payload, dict) else None
    if not isinstance(tasks, list):
        return None
    matches = []
    id_matches = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        current_task_id = str(
            _pick(task, "taskId", "TaskId", "task_id", "id", default="") or ""
        )
        if task_id and current_task_id == str(task_id):
            id_matches.append(task)
        try:
            same_charge = int(_pick(task, "chargeId", "ChargeId")) == int(charge_id)
        except (TypeError, ValueError):
            same_charge = False
        task_uid = str(_pick(task, "userId", "UserID", default="") or "")
        if same_charge and task_uid == str(mai_uid):
            matches.append(task)
    candidates = id_matches or matches
    return max(candidates, key=lambda task: str(task.get("ts") or ""), default=None)


def _ticket_submission_task_id(payload: dict) -> str:
    """从发票 200 响应中提取队列任务 ID。"""
    if not isinstance(payload, dict):
        return ""
    containers = [payload]
    for key in ("data", "result", "task", "msg"):
        value = payload.get(key)
        if isinstance(value, dict):
            containers.append(value)
        elif isinstance(value, str):
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError:
                continue
            if isinstance(decoded, dict):
                containers.append(decoded)
    for row in containers:
        value = _pick(row, "taskId", "TaskId", "task_id", "id", default="")
        if value not in (None, ""):
            return str(value)
    return ""


def _ticket_queue_ahead(payload: dict) -> Optional[int]:
    """从发票 200 响应中提取前方排队数，兼容常见字段与文本。"""
    if not isinstance(payload, dict):
        return None
    containers = [payload]
    for key in ("data", "result", "task", "msg"):
        value = payload.get(key)
        if isinstance(value, dict):
            containers.append(value)
        elif isinstance(value, str):
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError:
                continue
            if isinstance(decoded, dict):
                containers.append(decoded)
    direct_keys = (
        "ahead", "aheadCount", "ahead_count", "waitingAhead", "waiting_ahead",
        "queueAhead", "queue_ahead", "waitCount", "wait_count", "waiting",
        "queueSize", "queue_size", "queueLength", "queue_length",
    )
    for row in containers:
        for key in direct_keys:
            if key not in row:
                continue
            try:
                return max(0, int(row[key]))
            except (TypeError, ValueError):
                continue
        for key in ("position", "queuePosition", "queue_position"):
            if key not in row:
                continue
            try:
                return max(0, int(row[key]) - 1)
            except (TypeError, ValueError):
                continue
    texts = [str(payload.get(key) or "") for key in ("msg", "message")]
    for text in texts:
        for pattern in (
            r"(?:前方|ahead)\D{0,8}(\d+)",
            r"(?:排队|队列|等待人数)(?:人数|数量|长度|中有|已有|剩余)?\D{0,8}(\d+)",
        ):
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return max(0, int(match.group(1)))
    return None


def _ticket_task_result_code(task: dict) -> Optional[int]:
    """解析队列 msg 中嵌套的 ``result={...returnCode...}``。"""
    containers = [task]
    for key in ("result", "data"):
        value = task.get(key)
        if isinstance(value, dict):
            containers.append(value)
    message = task.get("msg") or task.get("message")
    if isinstance(message, dict):
        containers.append(message)
    elif isinstance(message, str):
        match = re.search(r"result\s*=\s*(\{.*\})\s*$", message, re.DOTALL)
        if match:
            try:
                decoded = json.loads(match.group(1))
            except json.JSONDecodeError:
                decoded = None
            if isinstance(decoded, dict):
                containers.append(decoded)
    for row in containers:
        value = _pick(row, "returnCode", "ReturnCode")
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


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


def _ticket_queue_units(queue_ahead: Optional[int]) -> int:
    """API 的排队数已包含当前待处理量；0/缺失时至少算1笔。"""
    return max(1, int(queue_ahead or 0))


def _ticket_wait_plan(queue_ahead: Optional[int]) -> tuple[int, float, int]:
    """根据前方排队数计算预计时间和动态超时。"""
    fallback_per_request = max(
        1.0,
        float(getattr(maiconfig, "awmc_ticket_seconds_per_request", 80.0) or 80.0),
    )
    per_request, samples = processing_time_estimator.estimate(
        _TICKET_QUEUE_UNIT_TIMING_KEY,
        fallback_seconds=fallback_per_request,
    )
    base_timeout = min(
        600.0,
        max(
            1.0,
            float(getattr(maiconfig, "awmc_ticket_poll_timeout_seconds", 120.0) or 120.0),
        ),
    )
    max_timeout = min(
        600.0,
        max(
            base_timeout,
            float(getattr(maiconfig, "awmc_ticket_max_poll_timeout_seconds", 600.0) or 600.0),
        ),
    )
    requests = _ticket_queue_units(queue_ahead)
    estimated = max(1, int(round(requests * per_request)))
    timeout = min(max_timeout, max(base_timeout, estimated + 40.0))
    return estimated, timeout, samples


def _format_wait_duration(seconds: int) -> str:
    minutes, remain = divmod(max(1, int(seconds)), 60)
    if minutes and remain:
        return f"{minutes} 分 {remain} 秒"
    if minutes:
        return f"{minutes} 分钟"
    return f"{remain} 秒"


def _ticket_wait_message(
    queue_ahead: Optional[int], estimated: int, timeout: float, samples: int
) -> str:
    queue_text = (
        f"队列预计有 {queue_ahead} 个请求待处理"
        if queue_ahead is not None
        else "API 未返回明确的排队数"
    )
    estimate_source = (
        f"根据最近 {samples} 次真实处理时间估算"
        if samples
        else "按单个请求约 80 秒估算"
    )
    timeout_note = (
        "\n当前队列较长，Bot 最多等待 10 分钟。"
        if timeout >= 600
        else ""
    )
    return (
        f"🎫 发票请求已进入队列，{queue_text}，"
        f"预计约 {_format_wait_duration(estimated)} 完成"
        f"（{estimate_source}）。\n"
        "Bot 会等待队列处理，并在确认票券到账后才扣 BREAK。"
        + timeout_note
    )


def _ticket_task_state(task: dict) -> str:
    if task.get("success") is False or task.get("error"):
        return "failed"
    status = str(_pick(task, "status", "Status", "state", "State", default="") or "").lower()
    if status in {"failed", "failure", "error", "cancelled", "canceled", "失败", "已取消"}:
        return "failed"
    if status in {"pending", "queued", "waiting", "processing", "running", "排队中", "处理中"}:
        return "active"
    terminal = (
        task.get("done") is True
        or task.get("success") is True
        or status in {
            "done", "success", "succeeded", "completed", "finished",
            "成功", "已完成", "完成",
        }
    )
    if terminal:
        result_code = _ticket_task_result_code(task)
        # UpsertUserChargelogApi 的返回码语义与 /user/charge 不同：
        # 队列内层 returnCode=1 成功，其他已知返回码均为失败。
        if result_code is not None and result_code != 1:
            return "failed"
        return "success"
    return status or "unknown"


async def _await_ticket_delivery(
    qrcode: str,
    charge_id: int,
    mai_uid: str,
    baseline_stock: int,
    previous_task_ts: Optional[str] = "",
    *,
    task_id: str = "",
    timeout: float = 120.0,
    timing_started_at: Optional[float] = None,
    timing_units: int = 1,
) -> int:
    """先轮询队列；队列成功后只查一次真实票券库存。"""
    interval = max(
        1.0, float(getattr(maiconfig, "awmc_ticket_poll_interval_seconds", 3.0))
    )
    timeout = max(interval, min(600.0, float(timeout)))
    deadline = time.monotonic() + timeout
    saw_current_task = False
    last_task_status = ""
    last_query_error = ""
    completed_result_code: Optional[int] = None
    timing_recorded = False

    def record_terminal_timing() -> None:
        nonlocal timing_recorded
        if timing_recorded or timing_started_at is None:
            return
        elapsed = max(0.001, time.perf_counter() - timing_started_at)
        processing_time_estimator.record(
            _TICKET_QUEUE_UNIT_TIMING_KEY,
            elapsed / max(1, int(timing_units)),
        )
        timing_recorded = True

    log.info(
        f"[ticket] 开始确认到账 uid={mai_uid} charge={charge_id} "
        f"baseline={baseline_stock} timeout={timeout:.0f}s interval={interval:.0f}s"
    )
    while time.monotonic() < deadline:
        await asyncio.sleep(interval)
        try:
            queue = await sw_api.get_charge_queue()
            _ensure_business_success(queue)
            task = _matching_charge_task(queue, charge_id, mai_uid, task_id)
            if (
                task is not None
                and bool(previous_task_ts)
                and str(task.get("ts") or "") == previous_task_ts
            ):
                # 提交前已经存在的同账号同倍率历史任务，不属于本次请求。
                task = None
            if task is not None:
                saw_current_task = True
                last_task_status = _ticket_task_state(task)
                if last_task_status == "failed":
                    record_terminal_timing()
                    result_code = _ticket_task_result_code(task)
                    detail = (
                        f"上游 returnCode={result_code}，票券未发放"
                        if result_code is not None
                        else str(task.get("msg") or "队列任务失败")
                    )
                    raise RuntimeError(f"发票队列任务执行失败（{detail}）；本次不扣 BREAK")
                if last_task_status == "success":
                    record_terminal_timing()
                    completed_result_code = _ticket_task_result_code(task)
                    break
        except RuntimeError:
            raise
        except Exception as exc:
            last_query_error = _exception_detail(exc)
            log.warning(f"[ticket] 查询发票队列失败，继续等待：{last_query_error}")
    else:
        queue_hint = f"，队列末次状态 {last_task_status}" if last_task_status else ""
        active_hint = "，已识别本次队列任务" if saw_current_task else ""
        error_hint = f"，末次查询错误：{last_query_error}" if last_query_error else ""
        raise RuntimeError(
            f"发票队列超时（{timeout:.0f} 秒），未收到任务成功状态"
            f"{queue_hint}{active_hint}{error_hint}；本次不扣 BREAK"
        )

    # 队列明确成功后，留出短暂落库时间，再只查询一次库存。
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
        result_note = (
            f"returnCode={completed_result_code}"
            if completed_result_code is not None
            else "未返回 returnCode"
        )
        raise RuntimeError(
            f"发票队列任务已完成（{result_note}），但到账复核未增加："
            f"{charge_id} 倍票库存 {baseline_stock}→{stock}；"
            "可能是上游落库延迟，本次不扣 BREAK"
        )
    log.info(
        f"[ticket] 队列成功后已确认到账 uid={mai_uid} charge={charge_id} "
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
    ttl = max(0, int(getattr(maiconfig, "awmc_qrcode_cache_seconds", 0) or 0))
    if ttl and time.time() - binding.qrcode_updated_at > ttl:
        return key, None, "已保存的二维码凭据过期，请重新使用 mai绑定 提交最新二维码。"
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


async def _read_verified_preview(
    binding: AccountBinding,
    qrcode: str,
    *,
    save_qrcode: bool,
) -> tuple[AccountBinding, dict]:
    payload = await sw_api.get_user_preview(qrcode)
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
    preview = await sw_api.get_user_preview(qrcode)
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
        lines.append(f"BREAK 余额：{break_db.get_balance(int(binding.user_key))}")
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
    lines.append(f"🐟 水鱼上传：{'已绑定' if binding.fish_token else '未绑定'}")
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
            lines.append("🎫 票券情况：暂时无法获取")
    return "\n".join(lines)


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
        messages = json.loads(json.dumps(nodes, ensure_ascii=False))
        if isinstance(event, GroupMessageEvent):
            await bot.call_api(
                "send_group_forward_msg", group_id=event.group_id, messages=messages
            )
        else:
            await bot.call_api(
                "send_private_forward_msg", user_id=event.user_id, messages=messages
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
    task_id = result.get("task_id")
    if task_id:
        return f"任务已提交，任务 ID：{task_id}"
    count = result.get("count")
    if count is not None:
        skipped = result.get("skipped")
        if skipped:
            return f"已处理 {count} 条成绩（跳过 {skipped} 条无效谱面）"
        return f"已处理 {count} 条成绩"
    return "操作已完成"


def _exception_detail(exc: BaseException) -> str:
    """保证面向用户和审计日志的异常原因永不为空。"""
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
    if detail:
        return detail

    cause = exc.__cause__ or exc.__context__
    if cause is not None and cause is not exc:
        cause_detail = _exception_detail(cause)
        if cause_detail:
            return cause_detail
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


def _service_cost(service: str, *, multiple: int = 1) -> int:
    if service == "ticket":
        unit = int(break_db.get_config("ticket_cost_per_multiplier", "10"))
        return max(0, unit) * max(1, multiple)
    defaults = {"upload_fish": "2", "upload_lx": "2", "upload_all": "3"}
    return max(0, int(break_db.get_config(f"{service}_cost", defaults[service])))


def _allowed_ticket_multipliers() -> tuple[int, ...]:
    raw = getattr(maiconfig, "awmc_ticket_allowed_multipliers", "2,3,5")
    if isinstance(raw, (list, tuple, set)):
        parts = raw
    else:
        parts = str(raw or "").replace("，", ",").split(",")
    values: set[int] = set()
    for part in parts:
        try:
            value = int(str(part).strip())
        except (TypeError, ValueError):
            continue
        if value > 0:
            values.add(value)
    return tuple(sorted(values)) or (2, 3, 5)


def _charge_text(result) -> str:
    labels = {"upload": "成绩上传", "ticket": "发票"}
    label = labels.get(result.service, result.service)
    if result.free:
        return f"💳 {label}今日首次成功，免费 · 余额 {result.balance} BREAK"
    return f"💳 {label}消耗 {result.charged} BREAK · 余额 {result.balance} BREAK"


async def _require_agreement(matcher, event: MessageEvent) -> None:
    if not bool(getattr(maiconfig, "maimaidx_user_agreement_required", True)):
        return
    if not has_user_agreed(event):
        await matcher.finish(agreement_prompt())


@account_help.handle()
async def _():
    fish_cost = break_db.get_config("upload_fish_cost", "2")
    lx_cost = break_db.get_config("upload_lx_cost", "2")
    all_cost = break_db.get_config("upload_all_cost", "3")
    ticket_unit = break_db.get_config("ticket_cost_per_multiplier", "10")
    ticket_multipliers = "/".join(map(str, _allowed_ticket_multipliers()))
    await account_help.finish(
        "AWMC 账号功能（已合并到 QueryBot）\n"
        "mai绑定 / maibind：绑定或认领舞萌账号\n"
        "mai状态 / mymai：查看账号详细状态，缓存失效时引导刷新二维码\n"
        "舞萌状态 / mais：AWMC 全局失败率分类图（空分类省略）+ 实时状态\n"
        "mai绑定水鱼 [Token] / maibindfish：无参数时交互引导，最多重试 3 次\n"
        "lxbind：落雪 OAuth（推荐）；maibindlx <导入Token> 为兼容方式\n"
        "maiu：仅水鱼；maiul：仅落雪；maiua：水鱼和落雪全部上传\n"
        f"发票 / fp <{ticket_multipliers}> / mai查票 / mai地图 / maiping\n"
        f"当前上传价格：水鱼 {fish_cost} / 落雪 {lx_cost} / 同时 {all_cost} BREAK\n"
        f"发票价格：倍率 × {ticket_unit} BREAK（例：2倍=20，3倍=30，5倍=50）\n"
        "已有 2/3/5 倍票未使用时重复发票，将拦截并扣除 20 BREAK。\n"
        "成绩上传每日首次成功免费；发票每次按价扣费，失败不扣费。\n"
        "发送“用户协议”阅读和确认服务条款。"
    )


@account_bind.handle()
async def _(matcher: Matcher, event: MessageEvent, args: Message = CommandArg()):
    await _require_agreement(account_bind, event)
    raw = _arg_text(args)
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
        try:
            await bot.delete_msg(message_id=event.message_id)
        except Exception:
            recall_notice = _RECALL_FAILED_NOTICE

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
    await account_bind.finish(
        recall_notice
        + f"{action}：{label}\nRating：{rating}{claim_note}{pc_note}\nRef_ID: {ref}"
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
        try:
            binding, preview = await _read_verified_preview(
                binding, binding.qrcode, save_qrcode=False
            )
            text = await _render_account_status(event, binding, preview)
            ref = _log(key, "status", "success", "preview_source=sgid_cache")
        except Exception as exc:
            account_db.mark_qrcode_result(key, False)
            matcher.state["status_cache_error"] = type(exc).__name__
            cache_label = "缓存验证失败，需刷新"
        else:
            await account_status.finish(text + f"\nRef_ID: {ref}", reply_message=True)
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
        await account_status.finish(text + f"\nRef_ID: {ref}", reply_message=True)

    recall_notice = ""
    try:
        await bot.delete_msg(message_id=event.message_id)
    except Exception:
        recall_notice = _RECALL_FAILED_NOTICE
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
    try:
        binding, preview = await _read_verified_preview(
            binding, qrcode, save_qrcode=True
        )
    except Exception as exc:
        await retry(type(exc).__name__)
    text = await _render_account_status(event, binding, preview)
    ref = _log(key, "status", "success", "preview_source=user_refresh")
    finish_pending(pending_key)
    await account_status.finish(
        recall_notice + text + f"\nRef_ID: {ref}", reply_message=True
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
        try:
            await bot.delete_msg(message_id=event.message_id)
        except Exception:
            recall_notice = _RECALL_FAILED_NOTICE

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
    await fish_bind.finish(
        f"✅ 水鱼 Token 已绑定。\nToken：{_mask(token, 8, 4)}\nRef_ID: {ref}",
        reply_message=True,
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
    await lx_upload_bind.finish(f"落雪 Token 已绑定。\nRef_ID: {ref}")


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
        if not has_user_agreed(event):
            return agreement_prompt()
    key = _user_key(event)
    binding = account_db.get(key)
    if not binding:
        return _ACCOUNT_SETUP_GUIDE

    oauth_token = await _lxns_oauth_access_token(event) if lxns else None
    has_lxns_oauth = _has_lxns_oauth(event) if lxns else False
    has_lxns_upload = bool(oauth_token or binding.lxns_token)
    if lxns and not oauth_token and _lxns_oauth_missing_write_scope(event):
        return (
            "落雪 OAuth 授权缺少 write_player 写入权限。"
            "请让管理员在落雪 OAuth 应用中启用该权限，然后重新发送 lxbind 授权。"
        )
    if lxns and has_lxns_oauth and not oauth_token:
        return (
            "落雪 OAuth Token 已失效且自动刷新失败。"
            "请重新发送「lxbind」完成授权后再上传。"
        )
    if fish and lxns and not binding.fish_token and not has_lxns_upload:
        return (
            "水鱼和落雪上传均未绑定。\n"
            "请使用「mai绑定水鱼 <Token>」，并发送「lxbind」完成落雪 OAuth。"
        )
    if fish and not binding.fish_token:
        return "未绑定水鱼 Token，请使用「mai绑定水鱼 <Token>」。"
    if lxns and not has_lxns_upload:
        return "未绑定落雪上传，请先发送「lxbind」完成 OAuth。"

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
            cost = _service_cost(operation)
            try:
                break_db.ensure_service_affordable(int(key), billing_service, cost)
                log.info(
                    f"[upload] 落雪 OAuth：使用 PC 缓存 {len(pc_scores)} 条，跳过机台 user={key}"
                )
                result = await _oauth_upload_lxns_with_refresh(
                    event, oauth_token, pc_scores, source="PC缓存"
                )
                account_db.mark_uploaded(key)
                charge = break_db.settle_service_success(
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
                    f"落雪（OAuth/PC缓存）：{_result_text(result)}\n"
                    f"{_charge_text(charge)}\nRef_ID: {ref}"
                )
            except Exception as exc:
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
        account_db.mark_qrcode_result(key, False)
        ref = _log(key, "upload", "error", f"sgid_preview={type(exc).__name__}")
        return f"上传失败：二维码验证失败（{type(exc).__name__}）\nRef_ID: {ref}"

    operation = "upload_all" if fish and lxns else "upload_fish" if fish else "upload_lx"
    billing_service = "upload"
    cost = _service_cost(operation)
    results: list[str] = []
    try:
        break_db.ensure_service_affordable(int(key), billing_service, cost)
        if fish:
            result = await sw_api.update_fish(qrcode, binding.fish_token)
            result = await _await_upload_success(result, lxns=False)
            results.append("水鱼：" + _result_text(result))
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
        account_db.mark_uploaded(key)
        charge = break_db.settle_service_success(
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
        return "上传完成\n" + "\n".join(results) + f"\n{_charge_text(charge)}\nRef_ID: {ref}"
    except Exception as exc:
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
    if bool(getattr(maiconfig, "maimaidx_user_agreement_required", True)):
        if not has_user_agreed(event):
            return agreement_prompt()

    binding = account_db.get(_user_key(event))
    if not binding:
        return _ACCOUNT_SETUP_GUIDE

    has_lxns_upload = bool(binding.lxns_token or _has_lxns_oauth(event))
    if lxns and _lxns_oauth_missing_write_scope(event):
        return (
            "落雪 OAuth 授权缺少 write_player 写入权限。"
            "请让管理员在落雪 OAuth 应用中启用该权限，然后重新发送 lxbind 授权。"
        )
    if fish and lxns and not binding.fish_token and not has_lxns_upload:
        return (
            "水鱼和落雪上传均未绑定。\n"
            "请使用「mai绑定水鱼 <Token>」，并发送「lxbind」完成落雪 OAuth。"
        )
    if fish and not binding.fish_token:
        return "未绑定水鱼 Token，请使用「mai绑定水鱼 <Token>」。"
    if lxns and not has_lxns_upload:
        return "未绑定落雪上传，请先发送「lxbind」完成 OAuth。"
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
    *, fish_token: str = "", lxns_token: str = "", has_lxns_oauth: bool = False
) -> tuple[bool, bool]:
    """直接二维码默认按 maiua 处理，但只上传用户实际绑定的渠道。"""
    return bool(fish_token), bool(lxns_token or has_lxns_oauth)


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
    targets = "水鱼 + 落雪" if fish and lxns else ("水鱼" if fish else "落雪")
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

    invalidate_player_cache(qqid)
    for source in sources:
        # 变种 B50 读取全量 records，普通 B50 读取 charts；两者必须同时更新。
        # 即使全量成绩暂时拉取失败，也继续刷新 charts，使新的 SQLite 缓存行
        # 阻止后续查询回退到统一存储里的旧快照。
        try:
            await get_user_records(
                qqid=qqid, force_source=source, force_refresh=True
            )
            log.info(
                f"[upload] 上传后已静默刷新全量成绩缓存 "
                f"user={user_key},source={source}"
            )
        except Exception as exc:
            log.warning(
                f"[upload] 上传已成功，但静默刷新全量成绩缓存失败 "
                f"user={user_key},source={source}: {_exception_detail(exc)}"
            )
        try:
            await get_user_b50(
                qqid=qqid, force_source=source, force_refresh=True
            )
            log.info(
                f"[upload] 上传后已静默刷新 B50 缓存 "
                f"user={user_key},source={source}"
            )
        except Exception as exc:
            log.warning(
                f"[upload] 上传已成功，但静默刷新 B50 缓存失败 "
                f"user={user_key},source={source}: {_exception_detail(exc)}"
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
    preflight_error = _upload_preflight_error(event, fish=fish, lxns=lxns)
    if preflight_error:
        await matcher.finish(preflight_error, reply_message=False)
    raw = _arg_text(args)
    if raw and not extract_sgwcmaid_qrcode(raw):
        await matcher.finish("上传失败：二维码格式无效", reply_message=True)
    # 凭据安全优先：先撤回，再贴处理表情和执行任何网络请求。缓存已过期时
    # 尚处于等待新 SGID 阶段，不能提前发送“已受理”。
    qrcode = extract_sgwcmaid_qrcode(raw)
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
        await matcher.finish(recall_notice + result, reply_message=False)
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
    preflight_error = _upload_preflight_error(event, fish=fish, lxns=lxns)
    if preflight_error:
        finish_pending(pending_key)
        await matcher.finish(preflight_error, reply_message=False)
    qrcode = extract_sgwcmaid_qrcode(raw)
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
        await matcher.finish(recall_notice + result, reply_message=False)
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
        await account_ping.finish(f"AWMC API 连接失败：{exc}")
    await account_ping.finish("AWMC API 连接正常\n" + _result_text(result))


async def _execute_ticket(
    event: MessageEvent,
    multiple: int,
    *,
    qrcode_override: str = "",
    notify: Optional[Callable[[str], Awaitable[Any]]] = None,
) -> str:
    """执行并结算发票；直发新二维码时会先更新绑定凭据。"""
    key = _user_key(event)
    binding = account_db.get(key)
    if binding is None or not (qrcode_override or binding.qrcode):
        raise RuntimeError("尚未绑定舞萌账号，请先使用：mai绑定 SGWCMAID...")
    cost = _service_cost("ticket", multiple=multiple)
    break_db.ensure_service_affordable(int(key), "ticket", cost)
    credential = qrcode_override or binding.qrcode
    async with machine_session():
        try:
            binding, _ = await _read_verified_preview(
                binding, credential, save_qrcode=bool(qrcode_override)
            )
        except Exception as exc:
            if not qrcode_override or credential == binding.qrcode:
                account_db.mark_qrcode_result(key, False)
            raise TicketQrcodeError(
                "二维码已过期、失效或与当前绑定账号不一致"
            ) from exc
        before_charge = await sw_api.get_user_charge(binding.qrcode)
        before_ok, before_rows, before_free_rows = _normalize_charge_payload(
            before_charge
        )
        if not before_ok:
            raise RuntimeError("发票前无法读取票券库存，已取消提交以避免错误扣费")
        unused_stocks = _unused_ticket_stocks(before_rows + before_free_rows)
        if unused_stocks:
            raise UnusedTicketPenaltyError(unused_stocks)
        baseline_stock = _ticket_stock(before_rows + before_free_rows, multiple)
        previous_task_ts: Optional[str] = ""
        try:
            before_queue = await sw_api.get_charge_queue()
            previous_task = _matching_charge_task(
                before_queue, multiple, binding.mai_uid
            )
            if previous_task is not None:
                previous_task_ts = str(previous_task.get("ts") or "")
        except Exception as exc:
            previous_task_ts = None
            log.warning(
                f"[ticket] 提交前队列快照失败，将仅依赖库存确认："
                f"{_exception_detail(exc)}"
            )
        result = await sw_api.charge_ticket(binding.qrcode, multiple)
        _ensure_business_success(result)
        task_id = _ticket_submission_task_id(result)
        queue_ahead = _ticket_queue_ahead(result)
    estimated, poll_timeout, timing_samples = _ticket_wait_plan(queue_ahead)
    queue_started_at = time.perf_counter()
    if notify is not None:
        try:
            await notify(
                _ticket_wait_message(
                    queue_ahead, estimated, poll_timeout, timing_samples
                )
            )
        except Exception as exc:
            log.warning(f"[ticket] 发送排队预计消息失败，继续确认任务：{_exception_detail(exc)}")
    verified_stock = await _await_ticket_delivery(
        binding.qrcode,
        multiple,
        binding.mai_uid,
        baseline_stock,
        previous_task_ts,
        task_id=task_id,
        timeout=poll_timeout,
        timing_started_at=queue_started_at,
        timing_units=_ticket_queue_units(queue_ahead),
    )
    charge = break_db.settle_service_success(
        int(key),
        "ticket",
        cost,
        meta={
            "multiple": multiple,
            "baseline_stock": baseline_stock,
            "verified_stock": verified_stock,
        },
    )
    ref = _log(
        key,
        "ticket",
        "success",
        f"multiple={multiple},stock={verified_stock},"
        f"charged={charge.charged},free={charge.free}",
    )
    clear_pending_ticket_retry(key)
    return (
        f"{multiple} 倍票已发放并确认到账（当前库存 {verified_stock} 张）。\n"
        f"{_charge_text(charge)}\nRef_ID: {ref}"
    )


def _ticket_failure_text(key: str, multiple: int, exc: Exception) -> str:
    """格式化发票失败；保留原有未使用票券处罚语义。"""
    if isinstance(exc, UnusedTicketPenaltyError):
        penalty = max(
            1, int(break_db.get_config("ticket_unused_penalty", "20") or 20)
        )
        meta = {"unused_stocks": exc.stocks, "requested_multiple": multiple}
        if not break_db.try_consume(
            int(key), penalty, "ticket_unused_penalty", meta=meta
        ):
            balance = break_db.get_balance(int(key))
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
        balance = break_db.get_balance(int(key))
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
        return _ticket_failure_text(key, multiple, exc) + "\n" + suffix
    except Exception as exc:
        return _ticket_failure_text(key, multiple, exc)


@account_ticket.handle()
async def _(event: MessageEvent, args: Message = CommandArg()):
    await _require_agreement(account_ticket, event)
    key, binding, error = _binding_or_error(event)
    if error or binding is None:
        await account_ticket.finish(error or "账号未绑定")
    raw = _arg_text(args) or "2"
    try:
        multiple = int(raw)
    except ValueError:
        await account_ticket.finish("倍率格式错误，用法：发票 2（或 fp 2）")
    allowed = _allowed_ticket_multipliers()
    if multiple not in allowed:
        allowed_text = " / ".join(map(str, allowed))
        await account_ticket.finish(f"票券倍率仅支持：{allowed_text}。")
    clear_pending_ticket_retry(key)
    try:
        cost = _service_cost("ticket", multiple=multiple)
        break_db.ensure_service_affordable(int(key), "ticket", cost)
    except Exception as exc:
        await account_ticket.finish(
            _ticket_failure_text(key, multiple, exc), reply_message=True
        )
    async def notify(message: str) -> None:
        await account_ticket.send(message, reply_message=True)

    try:
        text = await _execute_ticket(event, multiple, notify=notify)
    except TicketQrcodeError as exc:
        remember_pending_ticket_retry(key, multiple)
        text = (
            _ticket_failure_text(key, multiple, exc)
            + "\n请在 180 秒内重新发送最新 SGWCMAID、官方链接或二维码图片；"
            "Bot 将直接继续本次发票，不会绑定或上传 B50。"
        )
    except Exception as exc:
        text = _ticket_failure_text(key, multiple, exc)
    await account_ticket.finish(text, reply_message=True)


@account_ticket_status.handle()
async def _(event: MessageEvent):
    key, binding, error = _binding_or_error(event)
    if error or binding is None:
        await account_ticket_status.finish(error or "账号未绑定")
    try:
        async with machine_session():
            result = await sw_api.get_user_charge(binding.qrcode)
        text = _format_ticket_status(result)
    except Exception as exc:
        detail = _exception_detail(exc)
        ref = _log(key, "ticket_status", "error", detail)
        await account_ticket_status.finish(
            f"票券查询失败：{detail}\nRef_ID: {ref}", reply_message=True
        )
    await account_ticket_status.finish(text, reply_message=True)


@account_region.handle()
async def _(event: MessageEvent):
    _, binding, error = _binding_or_error(event)
    if error or binding is None:
        await account_region.finish(error or "账号未绑定")
    try:
        result = await sw_api.get_user_region(binding.qrcode)
    except Exception as exc:
        await account_region.finish(f"查询失败：{exc}")
    # 与 maibot 一致：用 regionId → WAHLAP_REGIONS 映射省份名，勿依赖 regionName。
    await account_region.finish(format_user_region_block(result))


@account_opt.handle()
async def _(args: Message = CommandArg()):
    title_ver = _arg_text(args)
    if not title_ver:
        await account_opt.finish("用法：mai查询opt <titleVer>")
    try:
        result = await sw_api.get_opt(title_ver)
    except Exception as exc:
        await account_opt.finish(f"查询失败：{exc}")
    await account_opt.finish(json.dumps(result, ensure_ascii=False, indent=2)[:3000])


@account_queue.handle()
async def _():
    try:
        result = await sw_api.get_charge_queue()
    except Exception as exc:
        await account_queue.finish(f"查询失败：{exc}")
    await account_queue.finish(json.dumps(result, ensure_ascii=False, indent=2)[:3000])
