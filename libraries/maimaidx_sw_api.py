import asyncio
import json
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx
from loguru import logger as log

from ..config import maiconfig

# 与 maibot WAHLAP_REGIONS 对齐：API 只返回 regionId，需本地映射省份名。
WAHLAP_REGIONS: Dict[int, str] = {
    1: "北京",
    2: "重庆",
    3: "上海",
    4: "天津",
    5: "安徽",
    6: "福建",
    7: "甘肃",
    8: "广东",
    9: "贵州",
    10: "海南",
    11: "河北",
    12: "黑龙江",
    13: "河南",
    14: "湖北",
    15: "湖南",
    16: "江苏",
    17: "江西",
    18: "吉林",
    19: "辽宁",
    20: "青海",
    21: "陕西",
    22: "山东",
    23: "山西",
    24: "四川",
    25: "未知25",
    26: "云南",
    27: "浙江",
    28: "广西",
    29: "内蒙古",
    30: "宁夏",
    31: "新疆",
    32: "西藏",
}


def format_wahlap_region_name(region_id: int) -> str:
    return WAHLAP_REGIONS.get(region_id, f"未知({region_id})")


def format_user_region_block(result: dict) -> str:
    """将 user/region 响应格式化为与 maibot 一致的游玩地图文本。"""
    rows = result.get("userRegionList") or result.get("UserRegionList") or []
    entries: List[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        region_id = row.get("regionId", row.get("RegionId"))
        play_count = row.get("playCount", row.get("PlayCount"))
        created = row.get("created") or row.get("Created") or ""
        try:
            region_id_int = int(region_id)
        except (TypeError, ValueError):
            continue
        try:
            play_count_int = int(play_count or 0)
        except (TypeError, ValueError):
            play_count_int = 0
        entries.append(
            {
                "regionId": region_id_int,
                "playCount": play_count_int,
                "created": str(created).strip(),
            }
        )

    if not entries:
        return "暂无游玩地区记录。"

    entries.sort(key=lambda item: item["playCount"], reverse=True)
    length = result.get("length", result.get("Length"))
    try:
        length_int = int(length) if length is not None else len(entries)
    except (TypeError, ValueError):
        length_int = len(entries)
    total_play_count = sum(item["playCount"] for item in entries)

    lines = [
        f"记录地区数: {length_int}",
        f"总游玩次数: {total_play_count}",
        "",
        "🗺️ 游玩地区：",
    ]
    for item in entries:
        created = item["created"]
        created_part = f" · 首次 {created}" if created else ""
        lines.append(
            f"  {format_wahlap_region_name(item['regionId'])} · "
            f"{item['playCount']} 次{created_part}"
        )
    return "\n".join(lines)


class SwApiError(RuntimeError):
    """AWMC API failure with optional structured quota metadata."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "",
        retry_at: str = "",
        retry_after_seconds: Optional[int] = None,
        quota: Optional[dict] = None,
        connection_failed: bool = False,
    ) -> None:
        super().__init__(message)
        self.error_code = str(error_code or "")
        self.retry_at = str(retry_at or "")
        self.retry_after_seconds = retry_after_seconds
        self.quota = dict(quota or {})
        self.connection_failed = bool(connection_failed)

    @property
    def is_quota_exceeded(self) -> bool:
        return self.error_code.lower() == "quota_exceeded"

    @property
    def is_connection_error(self) -> bool:
        return self.connection_failed or self.error_code.lower() in (
            "connection_failed",
            "network_unreachable",
        )

    @classmethod
    def from_payload(
        cls,
        payload: Any,
        *,
        fallback: str = "AWMC API 请求失败",
    ) -> "SwApiError":
        """Build an error without discarding gateway quota metadata."""
        if not isinstance(payload, dict):
            return cls(str(payload or fallback))

        quota = payload.get("quota")
        if not isinstance(quota, dict):
            quota = {}
        error_value = payload.get("error")
        error_code = str(
            error_value
            if isinstance(error_value, str)
            else payload.get("errorCode") or payload.get("code") or ""
        )
        message = str(
            payload.get("msg")
            or payload.get("message")
            or (error_value if isinstance(error_value, str) else "")
            or fallback
        )
        retry_at = str(
            payload.get("retryAt")
            or payload.get("resetAt")
            or quota.get("retryAt")
            or quota.get("resetAt")
            or ""
        )
        retry_after_raw = payload.get(
            "retryAfterSeconds", quota.get("retryAfterSeconds")
        )
        try:
            retry_after = (
                max(0, int(retry_after_raw))
                if retry_after_raw is not None
                else None
            )
        except (TypeError, ValueError):
            retry_after = None
        return cls(
            message,
            error_code=error_code,
            retry_at=retry_at,
            retry_after_seconds=retry_after,
            quota=quota,
        )


def _audit_response_summary(value: Any) -> dict[str, Any]:
    """Keep useful response metadata in REF traces without storing full payloads."""
    if isinstance(value, dict):
        summary: dict[str, Any] = {
            "type": "object",
            "keys": [str(key)[:80] for key in list(value)[:40]],
        }
        for key in (
            "code", "returnCode", "status", "message", "msg", "error",
            "length", "count", "taskId", "done",
        ):
            if key in value and isinstance(value[key], (str, int, float, bool, type(None))):
                summary[key] = value[key]
        return summary
    if isinstance(value, list):
        first = value[0] if value and isinstance(value[0], dict) else None
        return {
            "type": "array",
            "length": len(value),
            "first_keys": [str(key)[:80] for key in list(first)[:40]] if first else [],
        }
    return {"type": type(value).__name__}


def find_sw_api_error(exc: BaseException) -> Optional[SwApiError]:
    """Find a wrapped ``SwApiError`` without following cyclic causes."""
    current: Optional[BaseException] = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, SwApiError):
            return current
        current = current.__cause__ or current.__context__
    return None


def is_sw_api_quota_error(exc: BaseException) -> bool:
    error = find_sw_api_error(exc)
    return bool(error and error.is_quota_exceeded)


def is_sw_api_connection_error(exc: BaseException) -> bool:
    error = find_sw_api_error(exc)
    return bool(error and error.is_connection_error)


def format_sw_api_quota_error(
    error: SwApiError,
    *,
    now: Optional[datetime] = None,
) -> str:
    """Return a concise user-facing quota message in China Standard Time."""
    quota = error.quota
    scope = str(quota.get("scope") or "").lower()
    category = str(quota.get("category") or "").lower()
    window = str(quota.get("window") or "").lower()
    scope_label = "个人" if scope == "personal" else ""
    category_label = {
        "read": "读取",
        "write": "写入",
    }.get(category, "请求")
    window_label = {
        "1h": "1 小时",
        "1d": "1 天",
        "24h": "24 小时",
    }.get(window, str(quota.get("windowLabel") or "当前周期"))

    usage = ""
    try:
        used = int(quota.get("used"))
        limit = int(quota.get("limit"))
        requested = int(quota.get("requested"))
        usage = f"（当前 {used}/{limit}，本次需要 {requested}）"
    except (TypeError, ValueError):
        pass

    china_tz = timezone(timedelta(hours=8))
    retry_dt: Optional[datetime] = None
    if error.retry_at:
        try:
            parsed = datetime.fromisoformat(error.retry_at.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            retry_dt = parsed.astimezone(china_tz)
        except ValueError:
            retry_dt = None
    if retry_dt is None and error.retry_after_seconds is not None:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        retry_dt = (current + timedelta(seconds=error.retry_after_seconds)).astimezone(
            china_tz
        )

    prefix = f"AWMC API {scope_label}{category_label}配额已达到 {window_label}上限{usage}。"
    if retry_dt is not None:
        return (
            f"{prefix}\n请在 {retry_dt.strftime('%Y-%m-%d %H:%M:%S')}"
            "（北京时间）后继续使用。"
        )
    return f"{prefix}\n请稍后再试。"


# AWMC 公共网关默认根地址；可用 AWMC_API_BASE_URL 覆盖。
AWMC_PUBLIC_GATEWAY_DEFAULT = "https://api.wmc.pub"


class SwApiClient:
    """AWMC HTTP 客户端：team=自建 sw-api，public=公共网关 api.wmc.pub。"""

    def __init__(self):
        self.api_mode = str(
            getattr(maiconfig, "awmc_api_mode", "team") or "team"
        ).lower()
        configured = (
            getattr(maiconfig, "awmc_api_base_url", None)
            or maiconfig.sdgbtechapi
            or ""
        ).rstrip("/")
        if self.api_mode == "public":
            self.base_url = configured or AWMC_PUBLIC_GATEWAY_DEFAULT
        else:
            self.base_url = configured
        log.info(f"[SwApi] mode={self.api_mode} base_url={self.base_url or '(未配置)'}")

    @property
    def is_public(self) -> bool:
        return self.api_mode == "public"

    @property
    def available(self) -> bool:
        if not bool(getattr(maiconfig, "awmc_account_enabled", True)):
            return False
        if self.is_public:
            return bool(self.base_url) and bool(
                getattr(maiconfig, "awmc_public_gateway_token", None)
            )
        return bool(self.base_url) and bool(maiconfig.sdgbt_client_id)

    def _check_available(self):
        if self.available:
            return
        if self.is_public:
            raise SwApiError(
                "AWMC 公共网关未配置。请在 .env 中设置:\n"
                "  AWMC_API_MODE=public\n"
                "  AWMC_PUBLIC_GATEWAY_TOKEN=gw_xxx\n"
                "可选：AWMC_API_BASE_URL=https://api.wmc.pub"
            )
        raise SwApiError(
            "sw-api 未配置。请在 .env 中设置:\n"
            "  AWMC_API_MODE=team\n"
            "  AWMC_API_BASE_URL=http://127.0.0.1:5001\n"
            "  SDGBT_CLIENT_ID=your_keychip"
        )

    def _api_path(self, suffix: str) -> str:
        """业务路径：public 用 /v1/...，team 用 /awmc/api/v1/...。"""
        suffix = "/" + str(suffix or "").lstrip("/")
        if self.is_public:
            return f"/v1{suffix}"
        return f"/awmc/api/v1{suffix}"

    def _machine_body(self, qrcode: str, **extra: Any) -> dict:
        # public：keychip 由网关注入，调用方只传业务参数。
        # team：自建 sw-api 仍需 keychip + qrcode。
        body: dict = {"qrcode": qrcode}
        if not self.is_public:
            body["keychip"] = maiconfig.sdgbt_client_id
        body.update(extra)
        return body

    @staticmethod
    def _parse_msg_payload(msg: Any) -> Any:
        if isinstance(msg, dict):
            return msg
        if isinstance(msg, str):
            if not msg:
                return {}
            try:
                return json.loads(msg)
            except json.JSONDecodeError:
                return {"raw": msg}
        return msg

    @staticmethod
    def _is_business_success(payload: Any) -> bool:
        """UpsertUserAllApi 成功契约：returnCode == 1。

        道具写入网关偶发在非 200 HTTP 状态下仍返回业务成功包，需要单独识别。
        """
        if not isinstance(payload, dict):
            return False

        def code_is_one(value: Any) -> bool:
            try:
                return int(value) == 1
            except (TypeError, ValueError):
                return False

        if code_is_one(payload.get("returnCode", payload.get("ReturnCode"))):
            return True
        business_data = payload.get("businessData")
        if isinstance(business_data, dict) and code_is_one(
            business_data.get("returnCode", business_data.get("ReturnCode"))
        ):
            return True
        for key in ("returnMessage", "msg"):
            nested = SwApiClient._parse_msg_payload(payload.get(key))
            if isinstance(nested, dict) and code_is_one(
                nested.get("returnCode", nested.get("ReturnCode"))
            ):
                return True
        return False

    @staticmethod
    def _parse_envelope(data: dict) -> Any:
        # Chime 3002 means the SGID is no longer usable.  Some gateways put
        # this failure inside an otherwise successful outer envelope, so
        # inspect the nested payload before returning businessData.
        def has_expired_sgid(value: Any) -> bool:
            if isinstance(value, dict):
                error_type = str(value.get("errorType") or value.get("ErrorType") or "").lower()
                error_code = str(value.get("errorCode") or value.get("ErrorCode") or "")
                message = str(
                    value.get("errorMessage")
                    or value.get("ErrorMessage")
                    or value.get("message")
                    or value.get("msg")
                    or ""
                ).lower()
                if (
                    ("chime" in error_type and "3002" in error_code)
                    or ("chime" in message and ("3002" in message or "获取用户失败" in message))
                ):
                    return True
                return any(has_expired_sgid(item) for item in value.values())
            if isinstance(value, (list, tuple)):
                return any(has_expired_sgid(item) for item in value)
            if isinstance(value, str):
                lowered = value.lower()
                return "chime" in lowered and (
                    "3002" in lowered or "获取用户失败" in value
                )
            return False

        if has_expired_sgid(data):
            raise SwApiError("ChimeError 3002：Chime 获取用户失败，SGID 已过期")
        # Some successful gateway responses include an empty ``error`` field.
        # Only a non-empty value represents an error; raising on ``error: ""``
        # made a completed item write look like ``SwApiError()`` to callers.
        error_value = data.get("error")
        if error_value not in (None, "", {}, []):
            raise SwApiError.from_payload(data, fallback=str(error_value))

        return_code = data.get("returnCode", data.get("ReturnCode"))
        if return_code is not None:
            try:
                business_ok = int(return_code) == 1
            except (TypeError, ValueError):
                business_ok = False
            if not business_ok:
                raise SwApiError(
                    str(
                        data.get("returnMessage")
                        or data.get("msg")
                        or f"AWMC 业务失败（returnCode={return_code}）"
                    )
                )

        business_data = data.get("businessData")
        if business_data is not None:
            # The public gateway may wrap the upstream Sega envelope twice.
            # Validate the nested returnCode before returning it so a genuine
            # nested failure is not silently treated as a successful request,
            # while ``returnCode=1`` (including item upsert) is accepted.
            if isinstance(business_data, dict):
                nested_code = business_data.get(
                    "returnCode", business_data.get("ReturnCode")
                )
                if nested_code is not None:
                    try:
                        nested_ok = int(nested_code) == 1
                    except (TypeError, ValueError):
                        nested_ok = False
                    if not nested_ok:
                        nested_error = (
                            business_data.get("returnMessage")
                            or business_data.get("msg")
                            or business_data.get("error")
                            or f"AWMC 业务失败（returnCode={nested_code}）"
                        )
                        raise SwApiError.from_payload(
                            business_data, fallback=str(nested_error)
                        )
            return business_data

        if "returnMessage" in data:
            return SwApiClient._parse_msg_payload(data.get("returnMessage"))

        code = data.get("code")
        if code == -1:
            raise SwApiError.from_payload(data, fallback="未知错误")

        # user/music 等接口：成功时 code=0，msg 为 JSON 字符串
        if code in (0, 1) and "msg" in data:
            return SwApiClient._parse_msg_payload(data.get("msg"))

        if "userId" in data or "userData" in data or "userPreview" in data:
            return data

        if data.get("Status"):
            return data

        if code == 0:
            return data

        raise SwApiError.from_payload(data, fallback="未知错误")

    @staticmethod
    def flatten_user_music(payload: Any) -> List[dict]:
        # public 网关 msg 可能直接是成绩数组；team 多为含 userMusicList 的对象。
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        if not isinstance(payload, dict):
            return []

        direct = payload.get("userMusicDetailList")
        if isinstance(direct, list):
            if (
                not direct
                or (isinstance(direct[0], dict) and "musicId" in direct[0])
            ):
                return [row for row in direct if isinstance(row, dict)]

        detail_list: List[dict] = []
        for music in payload.get("userMusicList") or []:
            if not isinstance(music, dict):
                continue
            for detail in music.get("userMusicDetailList") or []:
                if isinstance(detail, dict):
                    detail_list.append(detail)
        return detail_list

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict] = None,
        params: Optional[dict] = None,
        timeout: Optional[float] = None,
        retry_count: Optional[int] = None,
        accept_business_success: bool = False,
    ) -> dict:
        self._check_available()
        url = f"{self.base_url}{path}"
        from .maimaidx_admin_audit import admin_audit

        audit_started = time.time()
        actual_timeout = float(
            timeout
            if timeout is not None
            else getattr(maiconfig, "awmc_api_timeout_seconds", 120.0)
        )
        headers: Dict[str, str] = {}
        if self.is_public:
            token = str(getattr(maiconfig, "awmc_public_gateway_token", "") or "")
            headers["Authorization"] = f"Bearer {token}"
        if retry_count is None:
            retry_count = int(getattr(maiconfig, "awmc_api_retry_count", 3))
        retry_count = max(0, int(retry_count))
        retry_delay = max(
            0.0, float(getattr(maiconfig, "awmc_api_retry_delay_seconds", 1.0))
        )
        res: Optional[httpx.Response] = None
        last_error: Optional[Exception] = None
        for attempt in range(retry_count + 1):
            this_attempt_network_error = False
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(actual_timeout), headers=headers
                ) as client:
                    res = await client.request(
                        method, url, json=json_body, params=params
                    )
                if res.status_code == 429:
                    try:
                        quota_error = SwApiError.from_payload(res.json())
                    except (json.JSONDecodeError, ValueError, TypeError):
                        quota_error = None
                    if quota_error is not None and quota_error.is_quota_exceeded:
                        break
                if res.status_code not in (408, 429) and res.status_code < 500:
                    break
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                this_attempt_network_error = True
            if attempt < retry_count:
                # 网络/超时错误用指数退避 + 抖动，扛住网关瞬时抖动；
                # 其余（5xx/429）走线性退避。
                if this_attempt_network_error:
                    backoff = min(retry_delay * (2 ** attempt), 15.0)
                    backoff += random.uniform(0, 0.5)
                    await asyncio.sleep(max(0.2, backoff))
                else:
                    await asyncio.sleep(retry_delay * (attempt + 1))
        if res is None:
            admin_audit.add_step(
                "http.awmc",
                "error",
                {"method": method, "path": path, "error": str(last_error or "request failed")},
                started_at=audit_started,
            )
            raise SwApiError(
                "无法连接 AWMC 网关，可能是网络抖动，请稍后重试。"
                f"（{type(last_error).__name__ if last_error else 'request failed'}）",
                error_code="connection_failed",
                connection_failed=True,
            ) from last_error
        if res.status_code != 200:
            text = res.text[:200]
            err_msg = ""
            try:
                err_data = res.json()
                if isinstance(err_data, dict):
                    if accept_business_success and self._is_business_success(
                        err_data
                    ):
                        admin_audit.add_step(
                            "http.awmc",
                            "success",
                            {
                                "method": method,
                                "path": path,
                                "status_code": res.status_code,
                                "response": _audit_response_summary(err_data),
                                "note": "business returnCode=1",
                            },
                            started_at=audit_started,
                        )
                        cooldown = max(
                            0.0,
                            float(
                                getattr(
                                    maiconfig,
                                    "awmc_api_success_cooldown_seconds",
                                    1.0,
                                )
                                or 0.0
                            ),
                        )
                        if cooldown:
                            await asyncio.sleep(cooldown)
                        return err_data
                    structured_error = SwApiError.from_payload(
                        err_data,
                        fallback=f"HTTP {res.status_code}",
                    )
                    err_msg = str(structured_error)
                    raise structured_error
            except json.JSONDecodeError:
                pass
            except SwApiError:
                detail = {
                    "method": method,
                    "path": path,
                    "status_code": res.status_code,
                    "error": err_msg,
                    "error_code": structured_error.error_code,
                    "response": {
                        "error_code": structured_error.error_code,
                        "message": err_msg,
                    },
                }
                if structured_error.quota:
                    detail["quota"] = structured_error.quota
                if structured_error.retry_at:
                    detail["retry_at"] = structured_error.retry_at
                if structured_error.retry_after_seconds is not None:
                    detail["retry_after_seconds"] = structured_error.retry_after_seconds
                admin_audit.add_step(
                    "http.awmc", "error", detail, started_at=audit_started
                )
                raise
            admin_audit.add_step(
                "http.awmc",
                "error",
                {"method": method, "path": path, "status_code": res.status_code},
                started_at=audit_started,
            )
            if res.status_code == 401:
                raise SwApiError("鉴权失败：令牌缺失或无效（HTTP 401）")
            if res.status_code == 403:
                raise SwApiError(
                    f"拒绝访问（HTTP 403）：{text or '余额不足或无权限'}"
                )
            raise SwApiError(f"HTTP {res.status_code}: {text}")
        data = res.json()
        admin_audit.add_step(
            "http.awmc",
            "success",
            {
                "method": method,
                "path": path,
                "status_code": res.status_code,
                "response": _audit_response_summary(data),
            },
            started_at=audit_started,
        )
        # AWMC 账号 API 成功后静默留出短暂间隔，避免同一账号
        # 连续登录/传分导致会话异常；不向用户发送等待提示。
        cooldown = max(
            0.0,
            float(getattr(maiconfig, "awmc_api_success_cooldown_seconds", 1.0) or 0.0),
        )
        if cooldown:
            await asyncio.sleep(cooldown)
        return data

    def _b50_upload_timeout(self) -> float:
        return max(
            1.0,
            float(getattr(maiconfig, "awmc_b50_upload_timeout_seconds", 120.0)),
        )

    async def get_user_music(
        self,
        qrcode: str,
        *,
        timeout: Optional[float] = None,
        retry_count: Optional[int] = None,
    ) -> List[dict]:
        # 全量成绩默认 15s 硬超时；禁止长重试把 OAuth 上传拖成「一直卡住」。
        # 有新鲜 PC 缓存时上传路径会跳过本接口。public 消耗 2 Token。
        music_timeout = float(
            timeout
            if timeout is not None
            else getattr(maiconfig, "awmc_user_music_timeout_seconds", 15.0)
        )
        music_retries = (
            retry_count
            if retry_count is not None
            else int(getattr(maiconfig, "awmc_user_music_retry_count", 0))
        )
        log.info(
            f"[SwApi] 开始拉取谱面成绩 mode={self.api_mode} "
            f"timeout={music_timeout:.0f}s retry={music_retries}"
        )
        data = await self._request(
            "POST",
            self._api_path("user/music"),
            json_body=self._machine_body(qrcode),
            timeout=music_timeout,
            retry_count=music_retries,
        )
        payload = self._parse_envelope(data)
        detail_list = self.flatten_user_music(payload)
        log.info(f"[SwApi] 拉取谱面成绩完成，共 {len(detail_list)} 条")
        return detail_list

    async def update_fish(self, qrcode: str, token: str) -> dict:
        # B50 生成偶尔较慢，允许 120s，但仍禁止自动重试造成重复提交。
        # public / team 均为同步 JSON：{qrcode, token}；public 消耗 5 Token。
        upload_timeout = self._b50_upload_timeout()
        data = await self._request(
            "POST",
            self._api_path("update-fish"),
            json_body=self._machine_body(qrcode, token=token),
            timeout=upload_timeout,
            retry_count=0,
        )
        return self._parse_envelope(data)

    async def update_lx(self, qrcode: str, import_token: str) -> dict:
        # 兼容 Token 备选路径；允许 120s，但保持零重试避免重复提交。
        # public / team 均为同步 JSON：{qrcode, key, type}；public 消耗 5 Token。
        upload_timeout = self._b50_upload_timeout()
        data = await self._request(
            "POST",
            self._api_path("update-lx"),
            json_body=self._machine_body(
                qrcode, key=import_token, type="maimai"
            ),
            timeout=upload_timeout,
            retry_count=0,
        )
        return self._parse_envelope(data)

    async def get_upload_task(self, task_id: str, *, lxns: bool = False) -> dict:
        """旧公共网关异步任务查询已移除；新版 upload 为同步，无需轮询。"""
        raise SwApiError(
            "当前 AWMC API 上传为同步接口，不再提供异步任务查询"
            f"（task_id={task_id}, lxns={lxns}）"
        )

    async def charge_ticket(self, qrcode: str, charge_id: int) -> dict:
        # AWMC v2 同步执行发票，上游通常需要约 60 秒。写接口禁止网络层重试，
        # 避免客户端超时后重复提交。
        timeout = max(
            1.0,
            float(getattr(maiconfig, "awmc_ticket_timeout_seconds", 120.0)),
        )
        return await self._request(
            "POST",
            self._api_path("charge"),
            json_body=self._machine_body(qrcode, charge=charge_id),
            timeout=timeout,
            retry_count=0,
        )

    async def get_user_charge(self, qrcode: str) -> dict:
        data = await self._request(
            "POST",
            self._api_path("user/charge"),
            json_body=self._machine_body(qrcode),
            timeout=30,
        )
        return self._parse_envelope(data)

    async def health(self) -> dict:
        return await self._request("GET", self._api_path("health"), timeout=10)

    async def get_user_data(self, qrcode: str) -> dict:
        """读取账号基础数据；绑定验码依赖完整的 user data。"""
        # 上传前验码也会走这里；显式短超时，避免沿用默认 120s×重试。
        # public 消耗 1 Token；msg 常为 JSON 字符串，由 _parse_envelope 二次解析。
        data = await self._request(
            "POST",
            self._api_path("user/data"),
            json_body=self._machine_body(qrcode),
            timeout=15,
            retry_count=0,
        )
        return self._parse_envelope(data)

    async def get_user_preview(self, qrcode: str) -> dict:
        """读取用户预览（POST /user/preview）。"""
        data = await self._request(
            "POST",
            self._api_path("user/preview"),
            json_body=self._machine_body(qrcode),
            timeout=15,
            retry_count=0,
        )
        return self._parse_envelope(data)

    async def get_user_items(
        self, qrcode: str, *, timeout: Optional[float] = None
    ) -> dict:
        """读取用户道具列表（POST /user/item-list）。"""
        items_timeout = max(
            1.0,
            float(
                timeout
                if timeout is not None
                else getattr(maiconfig, "awmc_user_items_timeout_seconds", 120.0)
            ),
        )
        data = await self._request(
            "POST",
            self._api_path("user/item-list"),
            json_body=self._machine_body(qrcode),
            timeout=items_timeout,
            retry_count=0,
        )
        return self._parse_envelope(data)

    async def get_user_kaleidx_scope(self, qrcode: str) -> dict:
        """读取 Kaleidx Gate 状态（POST /user/kaleidx-scope）。"""
        data = await self._request(
            "POST",
            self._api_path("user/kaleidx-scope"),
            json_body=self._machine_body(qrcode),
            timeout=30,
            retry_count=0,
        )
        return self._parse_envelope(data)

    async def upsert_music(self, qrcode: str, music: dict) -> Any:
        write_timeout = float(
            getattr(maiconfig, "awmc_music_write_timeout_seconds", 120.0)
            or 120.0
        )
        data = await self._request(
            "POST",
            self._api_path("music/upsert"),
            json_body=self._machine_body(qrcode, musicList=[music]),
            timeout=write_timeout,
            retry_count=0,
        )
        return self._parse_envelope(data)

    async def delete_music(self, qrcode: str, music_id: int, level: int) -> Any:
        write_timeout = float(
            getattr(maiconfig, "awmc_music_write_timeout_seconds", 120.0)
            or 120.0
        )
        data = await self._request(
            "POST",
            self._api_path("music/delete"),
            json_body=self._machine_body(
                qrcode, musicList=[{"musicId": music_id, "level": level}]
            ),
            timeout=write_timeout,
            retry_count=0,
        )
        return self._parse_envelope(data)

    async def upsert_item(
        self, qrcode: str, item_kind: int, item_id: int, operation: str
    ) -> Any:
        # UpsertUserAllApi 服务端处理约需 90 秒；网关偶发在非 200 状态下
        # 返回 returnCode=1 的业务成功包，按业务码识别成功，避免误报 AWMCError。
        item_timeout = float(
            getattr(maiconfig, "awmc_item_upsert_timeout_seconds", 120.0)
            or 120.0
        )
        data = await self._request(
            "POST",
            self._api_path("item/upsert"),
            json_body=self._machine_body(
                qrcode,
                itemKind=item_kind,
                itemId=item_id,
                operation=operation,
            ),
            timeout=item_timeout,
            retry_count=0,
            accept_business_success=True,
        )
        return data

    async def get_user_region(self, qrcode: str) -> dict:
        data = await self._request(
            "POST",
            self._api_path("user/region"),
            json_body=self._machine_body(qrcode),
        )
        return self._parse_envelope(data)

    async def get_opt(self, title_ver: str) -> dict:
        if self.is_public:
            raise SwApiError("AWMC 公共网关不提供 get_opt 接口")
        return await self._request(
            "GET",
            "/api/private/get_opt",
            params={"title_ver": title_ver, "client_id": maiconfig.sdgbt_client_id},
            timeout=30,
        )


sw_api = SwApiClient()
