"""AWMCNET 成绩镜像客户端。

水鱼和落雪数据在 QueryBot 的 datasource 层已经转换为统一的 PlayInfoDev，
这里仅负责脱敏后同步 QQ、展示名与成绩，不发送二维码、街机 UID 或第三方 Token。
"""

from __future__ import annotations

from typing import Any, Sequence

import httpx

from ..config import log, maiconfig


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
    return {
        "song_id": raw_id,
        "title": str(_value(record, "title", "") or ""),
        "type": song_type,
        "level_index": int(_value(record, "level_index", 0) or 0),
        "achievements": float(_value(record, "achievements", 0) or 0),
        "dxScore": int(
            _value(record, "dxScore", _value(record, "dx_score", 0)) or 0
        ),
        "fc": str(_value(record, "fc", "") or "").lower(),
        "fs": str(_value(record, "fs", "") or "").lower(),
    }


def _chart_rating(userinfo: Any, key: str) -> int:
    charts = getattr(getattr(userinfo, "charts", None), key, None) or []
    return sum(int(getattr(record, "ra", 0) or 0) for record in charts)


def _connection() -> tuple[str, str, float] | None:
    base_url = str(getattr(maiconfig, "awmcnet_sync_url", "") or "").rstrip("/")
    token = str(getattr(maiconfig, "awmcnet_bot_token", "") or "")
    if not base_url or not token:
        return None
    timeout = max(
        1.0,
        float(getattr(maiconfig, "awmcnet_sync_timeout_seconds", 8.0) or 8.0),
    )
    return base_url, token, timeout


async def _post_sync(payload: dict) -> dict | None:
    connection = _connection()
    if connection is None:
        return None
    base_url, token, timeout = connection
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{base_url}/api/bot/sync",
                headers={"Bot-Token": token},
                json=payload,
            )
        if response.status_code != 200:
            log.warning(
                f"[AWMCNET] 成绩同步失败 status={response.status_code} "
                f"body={response.text[:120]}"
            )
            return None
        result = response.json()
        sent_count = len(payload.get("records") or [])
        stored_count = result.get("stored_records")
        if sent_count and stored_count is not None and int(stored_count) <= 0:
            errors = result.get("errors") or []
            log.warning(
                f"[AWMCNET] QQ={payload['qq']} 收到 {sent_count} 条但未落库 "
                f"skipped={result.get('skipped', 0)} errors={errors[:2]}"
            )
            return None
        log.info(
            f"[AWMCNET] QQ={payload['qq']} source={payload.get('source')} "
            f"imported={result.get('imported', 0)} updated={result.get('updated', 0)} "
            f"stored={stored_count if stored_count is not None else 'unknown'}"
        )
        return result
    except Exception as exc:
        log.warning(f"[AWMCNET] 成绩同步异常: {type(exc).__name__}: {exc}")
        return None


async def sync_awmcnet(
    qqid: int,
    userinfo: Any,
    records: Sequence[Any],
    *,
    source: str,
    play_count: int | None = None,
) -> dict | None:
    payload = {
        "qq": int(qqid),
        "nickname": str(getattr(userinfo, "nickname", "") or ""),
        "source": source,
        "rating": int(getattr(userinfo, "rating", 0) or 0),
        "old_rating": _chart_rating(userinfo, "sd"),
        "new_rating": _chart_rating(userinfo, "dx"),
        "records": [_record_payload(record) for record in records],
    }
    if play_count is not None:
        payload["play_count"] = int(play_count)
    return await _post_sync(payload)


async def sync_awmcnet_pc_records(
    qqid: int,
    records: Sequence[Any],
    *,
    nickname: str = "",
    rating: int | None = None,
    source: str = "sega",
    play_count: int | None = None,
) -> dict | None:
    payload = {
        "qq": int(qqid),
        "nickname": nickname,
        "source": source,
        "rating": rating,
        "records": [_record_payload(record) for record in records],
    }
    if play_count is not None:
        payload["play_count"] = int(play_count)
    return await _post_sync(payload)


async def sync_awmcnet_arcade_scores(
    qqid: int,
    scores: Sequence[Any],
    *,
    nickname: str = "",
    rating: int | None = None,
    play_count: int | None = None,
) -> dict | None:
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
