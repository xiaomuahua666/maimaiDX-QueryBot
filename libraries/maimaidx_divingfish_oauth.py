"""水鱼查分器 OAuth 设备授权客户端。

水鱼 OAuth 的授权关系保存在水鱼服务端。Bot 只保留应用凭据，并在进程内缓存
短期 access token；不会把用户令牌写入本地数据库、AWMCNET 或日志。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from ..config import log, maiconfig

DEVICE_AUTHORIZATION_PATH = '/oauth/device_authorization'
TOKEN_PATH = '/oauth/token'
REVOKE_PATH = '/apps'
DEVICE_GRANT = 'urn:ietf:params:oauth:grant-type:device_code'
ON_BEHALF_OF_GRANT = 'urn:diving-fish:params:oauth:grant-type:on-behalf-of'
SCOPE = 'profile prober.profile.read prober.records.read prober.records.write'
TOKEN_EXPIRY_MARGIN_SECONDS = 30
REQUEST_TIMEOUT_SECONDS = 15.0
_DB_SWITCH_KEY = 'divingfish_oauth_enabled'
_DB_SWITCH_UNSET = object()
_db_switch: object = _DB_SWITCH_UNSET


def _parse_bool(value: object) -> Optional[bool]:
    if value is None:
        return None
    raw = str(value).strip().lower()
    if not raw:
        return None
    if raw in {'1', 'true', 'yes', 'on', 'enabled', '开启', '启用'}:
        return True
    if raw in {'0', 'false', 'no', 'off', 'disabled', '关闭', '停用'}:
        return False
    return None


def _load_db_switch() -> Optional[bool]:
    """Load the optional BREAK database override once during process startup."""
    try:
        # Lazy import avoids a module cycle and keeps OAuth unit tests lightweight.
        from .maimaidx_break import break_db

        return _parse_bool(break_db.get_config(_DB_SWITCH_KEY, ''))
    except Exception as exc:
        log.debug(f'[divingfish-oauth] database switch unavailable: {type(exc).__name__}')
        return None


def reload_oauth_config() -> None:
    """Forget the startup snapshot; useful after an administrative DB change."""
    global _db_switch
    _db_switch = _DB_SWITCH_UNSET


def oauth_switch_enabled() -> bool:
    """Return the effective switch, with ``break_config`` taking precedence."""
    global _db_switch
    if _db_switch is _DB_SWITCH_UNSET:
        _db_switch = _load_db_switch()
    if _db_switch is not None:
        return bool(_db_switch)
    return bool(getattr(maiconfig, 'divingfish_oauth_enabled', False))


@dataclass(frozen=True)
class DeviceAuthorization:
    verification_uri_complete: str
    expires_in: int
    interval: int = 5


@dataclass(frozen=True)
class _CachedToken:
    access_token: str
    expires_at: float


def oauth_enabled() -> bool:
    return bool(
        oauth_switch_enabled()
        and str(getattr(maiconfig, 'divingfish_client_id', '') or '').strip()
        and str(getattr(maiconfig, 'divingfish_client_secret', '') or '').strip()
    )


def subject_ref(qqid: int) -> str:
    """生成按应用隔离的 QQ 摘要；QQ 原号不会发给水鱼 OAuth 端点。"""
    client_id = str(getattr(maiconfig, 'divingfish_client_id', '') or '')
    return hashlib.sha256(f'{client_id}:{int(qqid)}'.encode('utf-8')).hexdigest()


def binding_label(qqid: int) -> str:
    value = str(int(qqid))
    if len(value) <= 4:
        return f'QQ {value}'
    return f'QQ {value[:2]}{"*" * (len(value) - 4)}{value[-2:]}'


def revoke_url() -> str:
    base = str(getattr(maiconfig, 'divingfish_auth_url', '') or '').rstrip('/')
    return f'{base}{REVOKE_PATH}'


class _TokenCache:
    def __init__(self) -> None:
        self._tokens: dict[str, _CachedToken] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def get(self, ref: str) -> Optional[str]:
        cached = self._tokens.get(ref)
        if cached is None:
            return None
        if cached.expires_at <= time.monotonic():
            self._tokens.pop(ref, None)
            return None
        return cached.access_token

    def discard(self, ref: str, access_token: Optional[str] = None) -> None:
        cached = self._tokens.get(ref)
        if access_token is not None and cached is not None:
            if cached.access_token != access_token:
                return
        self._tokens.pop(ref, None)

    def put(self, ref: str, access_token: str, expires_in: int) -> None:
        # 偶尔清理过期条目，避免长时间运行的 Bot 为每个历史用户无限保留缓存。
        now = time.monotonic()
        if len(self._tokens) > 2048:
            self._tokens = {
                key: value for key, value in self._tokens.items()
                if value.expires_at > now
            }
        self._tokens[ref] = _CachedToken(
            access_token=access_token,
            expires_at=now + max(int(expires_in) - TOKEN_EXPIRY_MARGIN_SECONDS, 0),
        )

    def lock_for(self, ref: str) -> asyncio.Lock:
        lock = self._locks.get(ref)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[ref] = lock
        return lock


_token_cache = _TokenCache()


def _base_url() -> str:
    return str(
        getattr(maiconfig, 'divingfish_auth_url', 'https://auth.diving-fish.com')
        or 'https://auth.diving-fish.com'
    ).rstrip('/')


def _json_or_empty(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except (ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


async def _post(path: str, data: dict[str, Any]) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            response = await client.post(f'{_base_url()}{path}', data=data)
    except httpx.HTTPError as exc:
        log.warning(f'[divingfish-oauth] request failed path={path}: {type(exc).__name__}')
        from .maimaidx_error import DivingFishOAuthError
        raise DivingFishOAuthError() from exc

    payload = _json_or_empty(response)
    if response.status_code != 200:
        error = str(payload.get('error') or payload.get('message') or '').strip()
        if error == 'consent_required':
            from .maimaidx_error import DivingFishNotAuthorizedError
            raise DivingFishNotAuthorizedError
        from .maimaidx_error import DivingFishOAuthError
        raise DivingFishOAuthError()
    return payload


async def create_device_authorization(qqid: int) -> DeviceAuthorization:
    if not oauth_enabled():
        raise RuntimeError('Bot 管理员尚未配置水鱼 OAuth 应用，无法进行绑定授权。')
    ref = subject_ref(qqid)
    _token_cache.discard(ref)
    payload = await _post(
        DEVICE_AUTHORIZATION_PATH,
        {
            'client_id': str(maiconfig.divingfish_client_id),
            'client_secret': str(maiconfig.divingfish_client_secret),
            'scope': SCOPE,
            'subject_ref': ref,
            'binding_label': binding_label(qqid),
        },
    )
    try:
        url = str(payload['verification_uri_complete'])
        expires_in = int(payload.get('expires_in', 600))
        interval = int(payload.get('interval', 5))
    except (KeyError, TypeError, ValueError) as exc:
        from .maimaidx_error import DivingFishOAuthError
        raise DivingFishOAuthError('水鱼账号服务返回了无法识别的授权信息。') from exc
    return DeviceAuthorization(url, max(expires_in, 1), max(interval, 1))


async def _fetch_access_token(qqid: int) -> tuple[str, int]:
    payload = await _post(
        TOKEN_PATH,
        {
            'grant_type': ON_BEHALF_OF_GRANT,
            'client_id': str(maiconfig.divingfish_client_id),
            'client_secret': str(maiconfig.divingfish_client_secret),
            'subject': f'ref:{subject_ref(qqid)}',
            'scope': SCOPE,
        },
    )
    try:
        return str(payload['access_token']), max(int(payload.get('expires_in', 300)), 1)
    except (KeyError, TypeError, ValueError) as exc:
        from .maimaidx_error import DivingFishOAuthError
        raise DivingFishOAuthError('水鱼账号服务返回了无法识别的访问令牌。') from exc


async def get_access_token(qqid: int, *, refresh: bool = False) -> str:
    if not oauth_enabled():
        raise RuntimeError('水鱼 OAuth 尚未配置。')
    ref = subject_ref(qqid)
    if not refresh:
        cached = _token_cache.get(ref)
        if cached:
            return cached
    else:
        _token_cache.discard(ref)

    lock = _token_cache.lock_for(ref)
    async with lock:
        if not refresh:
            cached = _token_cache.get(ref)
            if cached:
                return cached
        access_token, expires_in = await _fetch_access_token(qqid)
        _token_cache.put(ref, access_token, expires_in)
        return access_token


def invalidate_access_token(qqid: int, access_token: Optional[str] = None) -> None:
    _token_cache.discard(subject_ref(qqid), access_token)


def clear_token_cache() -> None:
    _token_cache._tokens.clear()
