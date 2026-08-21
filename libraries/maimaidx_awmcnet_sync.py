"""AWMCNET 成绩镜像客户端。

水鱼和落雪数据在 QueryBot 的 datasource 层已经转换为统一的 PlayInfoDev，
这里仅负责脱敏后同步 QQ、展示名与成绩，不发送二维码、街机 UID 或第三方 Token。
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from enum import Enum
import time
from typing import Any, Sequence

import httpx

from ..config import log, maiconfig


# AWMCNET rejects overlapping writes for the same player with 429 while the
# previous snapshot is still being committed.  Keep writes for one QQ in order
# inside this bot process so maintenance jobs cannot race an explicit upload.
_SYNC_LOCKS: dict[int, asyncio.Lock] = {}

# AWMCNET rejects overlapping writes globally as well as per player.  The
# periodic backfill can submit hundreds of snapshots at once; without a
# process-wide write gate every coroutine retries independently and turns one
# upstream 429 into a retry storm.  Keep at most a small number of in-flight
# sync POSTs and open a short circuit when the upstream keeps failing.
_SYNC_CONDITION = asyncio.Condition()
_SYNC_ACTIVE = 0
_SYNC_FAILURE_TIMES: deque[float] = deque()
_SYNC_CIRCUIT_OPEN_UNTIL = 0.0


class AwmcnetSyncStatus(str, Enum):
    SUCCESS = 'success'
    UNCONFIGURED = 'unconfigured'
    CIRCUIT_OPEN = 'circuit_open'
    AUTH_FAILED = 'auth_failed'
    VALIDATION_FAILED = 'validation_failed'
    SERVICE_ERROR = 'service_error'
    REJECTED = 'rejected'
    AMBIGUOUS = 'ambiguous'


@dataclass(frozen=True)
class AwmcnetSyncResult:
    """AWMCNET 同步结果，区分提交后的不确定超时与确定的配置/鉴权错误。"""

    status: AwmcnetSyncStatus
    payload: dict | None = None
    detail: str = ''

    @property
    def ok(self) -> bool:
        return self.status is AwmcnetSyncStatus.SUCCESS

    @property
    def ambiguous(self) -> bool:
        return self.status is AwmcnetSyncStatus.AMBIGUOUS

    @property
    def unavailable(self) -> bool:
        return self.status is AwmcnetSyncStatus.UNCONFIGURED or self.status is AwmcnetSyncStatus.CIRCUIT_OPEN


def _ambiguous_sync_result(exc: Exception) -> AwmcnetSyncResult:
    """提交请求后未能确认响应时，不能断言服务端没有落库。"""
    return AwmcnetSyncResult(
        AwmcnetSyncStatus.AMBIGUOUS,
        detail=f'{type(exc).__name__}: {exc}',
    )


def _awmcnet_sync_max_concurrency() -> int:
    return max(
        1,
        int(getattr(maiconfig, "awmcnet_sync_max_concurrency", 4) or 0),
    )


def _awmcnet_circuit_threshold() -> int:
    return max(
        1,
        int(getattr(maiconfig, "awmcnet_sync_circuit_threshold", 12) or 0),
    )


def _awmcnet_circuit_seconds() -> float:
    return max(
        1.0,
        float(getattr(maiconfig, "awmcnet_sync_circuit_seconds", 30.0) or 0.0),
    )


def _sync_circuit_is_open() -> bool:
    return time.monotonic() < _SYNC_CIRCUIT_OPEN_UNTIL


def _sync_note_failure() -> None:
    global _SYNC_CIRCUIT_OPEN_UNTIL
    now = time.monotonic()
    _SYNC_FAILURE_TIMES.append(now)
    window = _awmcnet_circuit_seconds() * 2
    while _SYNC_FAILURE_TIMES and now - _SYNC_FAILURE_TIMES[0] > window:
        _SYNC_FAILURE_TIMES.popleft()
    if len(_SYNC_FAILURE_TIMES) >= _awmcnet_circuit_threshold():
        opened_until = now + _awmcnet_circuit_seconds()
        if opened_until > _SYNC_CIRCUIT_OPEN_UNTIL:
            _SYNC_CIRCUIT_OPEN_UNTIL = opened_until
            log.warning(
                f"[AWMCNET] 写同步连续失败 {len(_SYNC_FAILURE_TIMES)} 次，"
                f"熔断 {_awmcnet_circuit_seconds():.0f}s"
            )


def _sync_note_success() -> None:
    global _SYNC_CIRCUIT_OPEN_UNTIL
    _SYNC_FAILURE_TIMES.clear()
    _SYNC_CIRCUIT_OPEN_UNTIL = 0.0


def _sync_lock_for(qqid: int) -> asyncio.Lock:
    qqid = int(qqid)
    lock = _SYNC_LOCKS.get(qqid)
    if lock is None:
        lock = asyncio.Lock()
        _SYNC_LOCKS[qqid] = lock
    return lock


def awmcnet_configured() -> bool:
    return bool(
        str(getattr(maiconfig, "awmcnet_sync_url", "") or "").strip()
        and str(getattr(maiconfig, "awmcnet_bot_token", "") or "").strip()
    )


def _value(record: Any, key: str, default: Any = None) -> Any:
    if isinstance(record, dict):
        return record.get(key, default)
    return getattr(record, key, default)


def _record_payload(record: Any) -> dict:
    raw_id = int(_value(record, "song_id", _value(record, "id", 0)) or 0)
    raw_type = str(_value(record, "type", "") or "")
    if raw_type.lower() == "dx" and 0 < raw_id < 10000:
        raw_id += 10000
    song_type = (
        "DX" if raw_type.lower() == "dx" or raw_id > 10000 else "SD"
    )
    payload = {
        "song_id": raw_id,
        "title": str(_value(record, "title", "") or ""),
        "type": song_type,
        "level_index": int(_value(record, "level_index", 0) or 0),
        "level": str(_value(record, "level", "") or ""),
        "level_label": str(_value(record, "level_label", "") or ""),
        "ds": float(_value(record, "ds", 0) or 0),
        "achievements": float(_value(record, "achievements", 0) or 0),
        "ra": int(_value(record, "ra", 0) or 0),
        "rate": str(_value(record, "rate", "") or ""),
        "dxScore": int(
            _value(record, "dxScore", _value(record, "dx_score", 0)) or 0
        ),
        "fc": str(_value(record, "fc", "") or "").lower(),
        "fs": str(_value(record, "fs", "") or "").lower(),
    }
    return payload


def _chart_rating(userinfo: Any, key: str) -> int:
    charts = getattr(getattr(userinfo, "charts", None), key, None) or []
    return sum(int(getattr(record, "ra", 0) or 0) for record in charts)


def _connection() -> tuple[str, str, float] | None:
    base_url = str(getattr(maiconfig, "awmcnet_sync_url", "") or "").rstrip("/")
    token = str(getattr(maiconfig, "awmcnet_bot_token", "") or "").strip()
    if not base_url or not token:
        return None
    timeout = max(
        1.0,
        float(getattr(maiconfig, "awmcnet_sync_timeout_seconds", 120.0) or 120.0),
    )
    return base_url, token, timeout


def _sync_retry_count() -> int:
    return max(0, int(getattr(maiconfig, "awmcnet_sync_retry_count", 3) or 0))


def _sync_retry_delay(response: httpx.Response | None, attempt: int) -> float:
    """Return the fixed backoff for a transient AWMCNET response.

    The upstream contract is 2s for the first retry, 5s for the second and
    10s for the third; the attempt parameter is zero-based.
    """
    configured = max(
        0.0,
        float(
            getattr(maiconfig, "awmcnet_sync_retry_delay_seconds", 2.0) or 2.0
        ),
    )
    return configured * (2**attempt)


def _sync_max_records_per_batch() -> int:
    return max(
        1,
        int(
            getattr(
                maiconfig,
                "awmcnet_sync_max_records_per_batch",
                1000,
            )
            or 1000
        ),
    )


def _record_key(record: dict) -> tuple[int, int]:
    """Return the server's score uniqueness key."""
    return int(record.get("song_id") or 0), int(record.get("level_index") or 0)


def _dedupe_record_payloads(records: Sequence[Any]) -> list[dict]:
    """Collapse duplicate charts before sending a full snapshot.

    AWMCNET stores one score per ``(qq, song_id, level_index)``.  Merged
    upstream snapshots and repeated arcade rows can contain the same chart
    more than once; sending those rows makes the server transaction fail on
    its unique constraint instead of importing the rest of the snapshot.
    Keep the strongest row deterministically.
    """
    best: dict[tuple[int, int], dict] = {}
    for record in records:
        payload = _record_payload(record)
        key = _record_key(payload)
        current = best.get(key)
        rank = (
            float(payload.get("achievements") or 0),
            int(payload.get("dxScore") or 0),
        )
        current_rank = (
            (
                float(current.get("achievements") or 0),
                int(current.get("dxScore") or 0),
            )
            if current is not None
            else (-1.0, -1)
        )
        if current is None or rank > current_rank:
            best[key] = payload
    return list(best.values())


async def _post_sync(payload: dict) -> AwmcnetSyncResult:
    global _SYNC_ACTIVE

    connection = _connection()
    if connection is None:
        return AwmcnetSyncResult(AwmcnetSyncStatus.UNCONFIGURED)
    if _sync_circuit_is_open():
        log.warning(
            "[AWMCNET] 写同步熔断中，本次上传暂缓；恢复后会随后续查询/补存自动重试"
        )
        return AwmcnetSyncResult(AwmcnetSyncStatus.CIRCUIT_OPEN)
    base_url, token, timeout = connection
    qqid = int(payload.get("qq") or 0)
    lock = _sync_lock_for(qqid)
    async with lock:
        async with _SYNC_CONDITION:
            while _sync_circuit_is_open() or _SYNC_ACTIVE >= _awmcnet_sync_max_concurrency():
                if _sync_circuit_is_open():
                    log.warning("[AWMCNET] 写同步熔断中，上传被放弃")
                    return AwmcnetSyncResult(AwmcnetSyncStatus.CIRCUIT_OPEN)
                await _SYNC_CONDITION.wait()
            _SYNC_ACTIVE += 1
        try:
            return await _post_sync_once(base_url, token, timeout, payload)
        finally:
            async with _SYNC_CONDITION:
                _SYNC_ACTIVE -= 1
                _SYNC_CONDITION.notify_all()


def _is_transient_sync_exception(exc: Exception) -> bool:
    return isinstance(exc, httpx.TimeoutException) or isinstance(exc, httpx.TransportError)


async def _post_sync_once(
    base_url: str,
    token: str,
    timeout: float,
    payload: dict,
) -> AwmcnetSyncResult:
    """Upload one exact snapshot payload with the upstream retry contract.

    Retry 429/5xx/connection timeouts with fixed 2s/5s/10s backoff at most
    three times, and never retry 400/401/403/422.  Each retry resends the
    identical payload.
    """
    retry_count = _sync_retry_count()
    result: AwmcnetSyncResult | None = None
    response: httpx.Response | None = None
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            for attempt in range(retry_count + 1):
                if _sync_circuit_is_open():
                    log.warning("[AWMCNET] 写同步因熔断中止，本次上传未完成")
                    return AwmcnetSyncResult(AwmcnetSyncStatus.CIRCUIT_OPEN)
                try:
                    response = await client.post(
                        f"{base_url}/api/bot/sync",
                        headers={"Bot-Token": token},
                        json=payload,
                    )
                except Exception as exc:
                    if not _is_transient_sync_exception(exc) or attempt >= retry_count:
                        raise
                    delay = _sync_retry_delay(None, attempt)
                    log.warning(
                        f"[AWMCNET] 成绩同步连接异常 {type(exc).__name__}，"
                        f"{delay:.1f}s 后重试 ({attempt + 1}/{retry_count})"
                    )
                    await asyncio.sleep(delay)
                    continue

                status = response.status_code
                if status == 200:
                    result = await _handle_sync_ok(payload, response)
                    return result
                if status in (429, 500, 502, 503, 504):
                    if attempt >= retry_count:
                        break
                    delay = _sync_retry_delay(response, attempt)
                    log.warning(
                        f"[AWMCNET] 成绩同步暂不可用 status={status}，"
                        f"{delay:.1f}s 后重试 ({attempt + 1}/{retry_count})"
                    )
                    await asyncio.sleep(delay)
                    continue
                break

        if result is None:
            _sync_note_failure()
            if response is None:
                return AwmcnetSyncResult(
                    AwmcnetSyncStatus.SERVICE_ERROR,
                    detail='no response after retries',
                )
            body = response.text[:240]
            if response.status_code in (401, 403):
                log.error(
                    f"[AWMCNET] 成绩同步鉴权失败 status={response.status_code}; "
                    "请检查 Bot-Token 是否与 AWMCNET 服务端一致"
                )
                return AwmcnetSyncResult(
                    AwmcnetSyncStatus.AUTH_FAILED,
                    detail=body,
                )
            if response.status_code == 400:
                log.error(
                    f"[AWMCNET] 成绩同步参数错误 status=400 body={body}"
                )
                return AwmcnetSyncResult(
                    AwmcnetSyncStatus.VALIDATION_FAILED,
                    detail=body,
                )
            if response.status_code == 422:
                log.error(
                    f"[AWMCNET] 成绩同步参数校验失败 status=422 body={body}"
                )
                return AwmcnetSyncResult(
                    AwmcnetSyncStatus.VALIDATION_FAILED,
                    detail=body,
                )
            if response.status_code >= 500:
                log.error(
                    f"[AWMCNET] 成绩同步服务端错误 status={response.status_code} body={body}"
                )
            else:
                log.warning(
                    f"[AWMCNET] 成绩同步失败 status={response.status_code} body={body}"
                )
            return AwmcnetSyncResult(
                AwmcnetSyncStatus.SERVICE_ERROR,
                detail=f'status={response.status_code} body={body}',
            )
        return result
    except Exception as exc:
        _sync_note_failure()
        log.warning(f"[AWMCNET] 成绩同步异常: {type(exc).__name__}: {exc}")
        return _ambiguous_sync_result(exc)


async def _handle_sync_ok(payload: dict, response: httpx.Response) -> AwmcnetSyncResult:
    """Validate a 200 response and clear the upstream circuit breaker."""
    result = response.json()
    sent_count = len(payload.get("records") or [])
    stored_count = result.get("stored_records")
    if sent_count and stored_count is not None and int(stored_count) <= 0:
        errors = result.get("errors") or []
        log.warning(
            f"[AWMCNET] QQ={payload['qq']} 收到 {sent_count} 条但未落库 "
            f"skipped={result.get('skipped', 0)} errors={errors[:2]}"
        )
        _sync_note_failure()
        return AwmcnetSyncResult(
            AwmcnetSyncStatus.REJECTED,
            payload=result,
            detail=f'stored_records={stored_count}',
        )
    _sync_note_success()
    log.info(
        f"[AWMCNET] QQ={payload['qq']} source={payload.get('source')} "
        f"imported={result.get('imported', 0)} updated={result.get('updated', 0)} "
        f"stored={stored_count if stored_count is not None else 'unknown'}"
    )
    return AwmcnetSyncResult(
        AwmcnetSyncStatus.SUCCESS,
        payload=result,
    )


async def _post_sync_chunks(payload: dict, records: Sequence[dict]) -> AwmcnetSyncResult:
    """Upload a snapshot in bounded batches using the exact same retry policy."""
    batch_size = _sync_max_records_per_batch()
    failures: list[AwmcnetSyncResult] = []
    merged: dict = {}
    for start in range(0, len(records), batch_size):
        chunk = records[start:start + batch_size]
        batch_payload = dict(payload)
        batch_payload["records"] = chunk
        result = await _post_sync(batch_payload)
        if result.ok:
            for key, value in (result.payload or {}).items():
                if isinstance(value, int):
                    merged[key] = merged.get(key, 0) + value
            continue
        failures.append(result)
        if result.status is AwmcnetSyncStatus.AUTH_FAILED:
            # 鉴权失败时后续批次必然同样失败，停止拆包重试。
            break
    if failures:
        first = failures[0]
        return AwmcnetSyncResult(
            first.status,
            payload=merged or first.payload,
            detail=first.detail,
        )
    return AwmcnetSyncResult(
        AwmcnetSyncStatus.SUCCESS,
        payload=merged,
    )


async def sync_awmcnet(
    qqid: int,
    userinfo: Any,
    records: Sequence[Any],
    *,
    source: str,
    play_count: int | None = None,
) -> AwmcnetSyncResult:
    records = _dedupe_record_payloads(records)
    payload = {
        "qq": int(qqid),
        "nickname": str(getattr(userinfo, "nickname", "") or ""),
        "source": source,
        "rating": int(getattr(userinfo, "rating", 0) or 0),
        "old_rating": _chart_rating(userinfo, "sd"),
        "new_rating": _chart_rating(userinfo, "dx"),
        "records": records,
    }
    if play_count is not None:
        payload["play_count"] = int(play_count)
    if len(records) <= _sync_max_records_per_batch():
        return await _post_sync(payload)
    return await _post_sync_chunks(payload, records)


async def sync_awmcnet_pc_records(
    qqid: int,
    records: Sequence[Any],
    *,
    nickname: str = "",
    rating: int | None = None,
    source: str = "sega",
    play_count: int | None = None,
) -> AwmcnetSyncResult:
    records = _dedupe_record_payloads(records)
    payload = {
        "qq": int(qqid),
        "nickname": nickname,
        "source": source,
        "rating": rating,
        "records": records,
    }
    if play_count is not None:
        payload["play_count"] = int(play_count)
    if len(records) <= _sync_max_records_per_batch():
        return await _post_sync(payload)
    return await _post_sync_chunks(payload, records)


async def sync_awmcnet_arcade_scores(
    qqid: int,
    scores: Sequence[Any],
    *,
    nickname: str = "",
    rating: int | None = None,
    play_count: int | None = None,
) -> AwmcnetSyncResult:
    """上传由机台接口转换出的成绩；字段兼容落雪 Score 格式。"""
    return await sync_awmcnet_pc_records(
        qqid,
        scores,
        nickname=nickname,
        rating=rating,
        source="sega",
        play_count=play_count,
    )


async def fetch_awmcnet_player(qqid: int) -> dict | None:
    connection = _connection()
    if connection is None:
        return None
    base_url, token, timeout = connection
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(
                f"{base_url}/api/bot/player/{int(qqid)}",
                headers={"Bot-Token": token},
            )
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            log.warning(
                f"[AWMCNET] 成绩读取失败 status={response.status_code} "
                f"body={response.text[:120]}"
            )
            return None
        return response.json()
    except Exception as exc:
        log.warning(f"[AWMCNET] 成绩读取异常: {type(exc).__name__}: {exc}")
        return None


async def fetch_awmcnet_summary(qqid: int) -> dict | None:
    """读取轻量玩家摘要，供群排行批量查询使用。"""
    connection = _connection()
    if connection is None:
        return None
    base_url, token, timeout = connection
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(
                f"{base_url}/api/bot/player/{int(qqid)}/summary",
                headers={"Bot-Token": token},
            )
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            log.warning(
                f"[AWMCNET] 摘要读取失败 status={response.status_code} "
                f"body={response.text[:120]}"
            )
            return None
        return response.json()
    except Exception as exc:
        log.warning(f"[AWMCNET] 摘要读取异常: {type(exc).__name__}: {exc}")
        return None


async def fetch_awmcnet_trend(qqid: int, days: int = 30) -> dict | None:
    connection = _connection()
    if connection is None:
        return None
    base_url, token, timeout = connection
    days = max(1, min(365, int(days)))
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(
                f"{base_url}/api/bot/player/{int(qqid)}/trend",
                params={"days": days},
                headers={"Bot-Token": token},
            )
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            log.warning(
                f"[AWMCNET] 趋势读取失败 status={response.status_code} "
                f"body={response.text[:120]}"
            )
            return None
        return response.json()
    except Exception as exc:
        log.warning(f"[AWMCNET] 趋势读取异常: {type(exc).__name__}: {exc}")
        return None


def format_awmcnet_trend(payload: dict) -> str:
    points = list(payload.get("points") or [])
    nickname = str(payload.get("nickname") or "AWMC NET. 用户")
    days = int(payload.get("days") or 30)
    if not points:
        return (
            f"{nickname} 暂无 AWMC NET. 趋势数据。\n"
            "发送 SGWCMAID 开头的二维码凭据完成首次同步后即可开始记录。"
        )

    ratings = [int(point.get("rating") or 0) for point in points]
    low, high = min(ratings), max(ratings)
    blocks = "▁▂▃▄▅▆▇█"
    if high == low:
        sparkline = blocks[3] * len(ratings)
    else:
        sparkline = "".join(
            blocks[round((rating - low) * (len(blocks) - 1) / (high - low))]
            for rating in ratings
        )
    summary = payload.get("summary") or {}
    delta = int(summary.get("rating_delta") or 0)
    sign = "+" if delta > 0 else ""
    latest = points[-1]
    lines = [
        f"📈 {nickname} · AWMC NET. {days} 天趋势",
        sparkline,
        (
            f"Rating {summary.get('start_rating')} → {summary.get('end_rating')} "
            f"（{sign}{delta}）"
        ),
        (
            f"当前 B35 / B15：{int(latest.get('old_rating') or 0)} / "
            f"{int(latest.get('new_rating') or 0)}"
        ),
        (
            f"收录 {int(latest.get('record_count') or 0)} 张谱面 · "
            f"记录 {int(summary.get('days_with_data') or 0)} 天"
        ),
    ]
    recent = points[-7:]
    lines.append("最近记录：")
    for point in recent:
        point_delta = int(point.get("delta") or 0)
        point_sign = "+" if point_delta > 0 else ""
        lines.append(
            f"{str(point.get('date') or '')[5:]}  "
            f"{int(point.get('rating') or 0)}  "
            f"{point_sign}{point_delta}"
        )
    return "\n".join(lines)
