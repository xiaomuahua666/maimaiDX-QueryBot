"""ThemeHouse/Audentio XenForo OAuth for Bot-side QQ identity binding.

The web application already performs the browser-cookie part of OAuth.  The
bot only needs the public PKCE half: issue an authorization URL, accept the
one-time code the user pastes back, exchange it, and read the forum profile.
No forum access token is persisted after the identity has been linked.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import time
from typing import Any, Iterable, Optional
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from ..config import log, maiconfig
from .maimaidx_qq_bind import qq_bind_db


class ForumOAuthError(RuntimeError):
    """A user-facing OAuth configuration or provider error."""


QQ_EMAIL_RE = re.compile(r"^(?P<qq>[1-9][0-9]{4,11})@qq\.com$", re.I)


def _setting(name: str, *env_names: str, default: str = "") -> str:
    """Read explicit environment overrides, then plugin config.

    ``AWWC_XF_*`` is intentionally accepted because the web application used
    that spelling in its first deployment and it is present in existing env
    files.
    """
    for env_name in env_names:
        raw = os.getenv(env_name)
        if raw not in (None, ""):
            return str(raw).strip()
    value = getattr(maiconfig, name, None)
    if value not in (None, ""):
        return str(value).strip()
    return default


def forum_base_url() -> str:
    return _setting(
        "awmc_xf_base_url",
        "AWMC_XF_BASE_URL",
        "AWWC_XF_BASE_URL",
        default="https://bbs.wmc.pub",
    ).rstrip("/")


def forum_client_id() -> str:
    return _setting(
        "awmc_xf_client_id",
        "AWMC_XF_CLIENT_ID",
        "AWWC_XF_CLIENT_ID",
    )


def forum_client_secret() -> str:
    return _setting(
        "awmc_xf_client_secret",
        "AWMC_XF_CLIENT_SECRET",
        "AWWC_XF_CLIENT_SECRET",
    )


def forum_redirect_uri() -> str:
    return _setting(
        "awmc_xf_redirect_uri",
        "AWMC_XF_REDIRECT_URI",
        "AWWC_XF_REDIRECT_URI",
        # A bot-specific redirect should leave ``code`` visible so the user can
        # paste it back. Deployments using the existing web client can override
        # this with its registered net.wmc.pub callback.
        default="https://genshin.wmc.pub/",
    )


def _path_setting(name: str, *env_names: str, default: str) -> str:
    return _setting(name, *env_names, default=default).strip()


def _absolute_url(base: str, path_or_url: str) -> str:
    value = str(path_or_url or "").strip()
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return f"{base.rstrip('/')}/{value.lstrip('/')}"


def forum_authorize_url(*, state: str, challenge: str, redirect_uri: Optional[str] = None) -> str:
    base = forum_base_url()
    path = _path_setting(
        "awmc_xf_authorize_path",
        "AWMC_XF_AUTHORIZE_PATH",
        "AWWC_XF_AUTHORIZE_PATH",
        # ThemeHouse/Audentio's authorize endpoint is mounted without the
        # ``/api`` prefix.  Keep the old variants in the token/user fallbacks
        # below because some XenForo installs expose both route layouts.
        default="/audapi/oauth2/authorize",
    )
    params = {
        "client_id": forum_client_id(),
        "redirect_uri": redirect_uri or forum_redirect_uri(),
        "response_type": "code",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    scope = _setting(
        "awmc_xf_scope", "AWMC_XF_SCOPE", "AWWC_XF_SCOPE", default=""
    )
    if scope:
        params["scope"] = scope
    return f"{_absolute_url(base, path)}?{urlencode(params)}"


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _jwt_payload(token: str) -> dict[str, Any]:
    try:
        parts = str(token).split(".")
        if len(parts) != 3:
            return {}
        payload = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _walk_dicts(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _first_value(payloads: Iterable[dict[str, Any]], keys: tuple[str, ...]) -> str:
    for payload in payloads:
        for key in keys:
            value = payload.get(key)
            if value not in (None, "") and not isinstance(value, (dict, list)):
                return str(value).strip()
    return ""


def _parse_profile(token: str, responses: Iterable[Any]) -> dict[str, str]:
    payloads: list[dict[str, Any]] = []
    jwt = _jwt_payload(token)
    if jwt:
        payloads.append(jwt)
    for response in responses:
        payloads.extend(_walk_dicts(response))
    user_id = _first_value(
        payloads,
        ("sub", "xf_user_id", "user_id", "userId", "userid", "id"),
    )
    username = _first_value(
        payloads, ("username", "user_name", "userName", "display_name", "name")
    )
    email = _first_value(payloads, ("email", "mail", "user_email", "userEmail"))
    qq_match = QQ_EMAIL_RE.fullmatch(email.lower())
    qq = qq_match.group("qq") if qq_match else ""
    return {
        "xf_user_id": user_id,
        "username": username,
        "email": email,
        "legacy_qq": qq,
    }


def parse_authorization_code(raw: str) -> str:
    """Accept a plain code, callback URL, or ``code=...`` pasted by a user."""
    value = (raw or "").strip()
    if not value:
        return ""
    if "?" in value or "&" in value:
        query = parse_qs(urlparse(value).query or value.lstrip("?"))
        code = (query.get("code") or [""])[0]
        if code:
            return str(code).strip()
    match = re.search(r"(?:^|[?&\s])code=([^&\s]+)", value, re.I)
    if match:
        return match.group(1).strip()
    return value


def _callback_state(raw: str) -> str:
    value = (raw or '').strip()
    if '?' not in value and '&' not in value:
        return ''
    query = parse_qs(urlparse(value).query or value.lstrip('?'))
    return str((query.get('state') or [''])[0]).strip()


def begin_forum_login(
    platform_id: str, *, claimed_qq: Optional[int] = None
) -> str:
    """Create a short-lived PKCE transaction and return the provider URL.

    ``claimed_qq`` is optional: when set, OAuth completion must return the same
    QQ from the forum ``数字@qq.com`` email before the bind is accepted.
    """
    if not forum_client_id():
        raise ForumOAuthError(
            "论坛 OAuth 尚未配置 client_id，请联系管理员设置 AWMC_XF_CLIENT_ID。"
        )
    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    state = secrets.token_urlsafe(24)
    redirect_uri = forum_redirect_uri()
    qq_bind_db.save_forum_pending(
        str(platform_id),
        state=state,
        verifier=verifier,
        redirect_uri=redirect_uri,
        claimed_qq=claimed_qq,
    )
    return forum_authorize_url(
        state=state, challenge=challenge, redirect_uri=redirect_uri
    )


def _token_urls() -> list[str]:
    override = _path_setting(
        "awmc_xf_token_url",
        "AWMC_XF_TOKEN_URL",
        "AWWC_XF_TOKEN_URL",
        default="",
    )
    # Prefer the Audentio API route that allows unauthenticated token exchange
    # (Oauth2::allowUnauthenticatedRequest). Keep older path spellings as
    # fallbacks; when an override is set, still try the defaults afterwards.
    defaults = [
        _absolute_url(forum_base_url(), "/api/audapi-oauth2/token"),
        _absolute_url(forum_base_url(), "/api/audapi/oauth2/token"),
        _absolute_url(forum_base_url(), "/audapi/oauth2/token"),
        _absolute_url(forum_base_url(), "/api/oauth2/token"),
    ]
    if override:
        primary = _absolute_url(forum_base_url(), override)
        return [primary] + [u for u in defaults if u != primary]
    return defaults


def _userinfo_urls() -> list[str]:
    override = _path_setting(
        "awmc_xf_userinfo_url",
        "AWMC_XF_USERINFO_URL",
        "AWWC_XF_USERINFO_URL",
        default="",
    )
    if override:
        return [_absolute_url(forum_base_url(), override)]
    return [
        _absolute_url(forum_base_url(), "/api/audapi/oauth2/user"),
        _absolute_url(forum_base_url(), "/api/me"),
        _absolute_url(forum_base_url(), "/api/me/"),
    ]


def _token_from_payload(payload: Any) -> str:
    for obj in _walk_dicts(payload):
        for key in ("access_token", "accessToken", "token"):
            value = obj.get(key)
            if value not in (None, "") and not isinstance(value, (dict, list)):
                return str(value).strip()
    return ""


async def complete_forum_login(platform_id: str, raw_code: str) -> dict[str, str]:
    """Exchange a pending code and persist only the forum -> QQ identity link."""
    code = parse_authorization_code(raw_code)
    if not code:
        raise ForumOAuthError("没有读到授权码，请发送论坛回调 URL 或 code 参数。")
    pending = qq_bind_db.get_forum_pending(str(platform_id))
    if pending is None:
        raise ForumOAuthError("授权码已过期或不存在，请重新发送「qbind」获取链接。")
    callback_state = _callback_state(raw_code)
    if callback_state and callback_state != str(pending.get('state') or ''):
        raise ForumOAuthError('授权回调 state 校验失败，请重新发送「qbind」获取链接。')
    if not forum_client_id():
        qq_bind_db.clear_forum_pending(platform_id)
        raise ForumOAuthError("论坛 OAuth 未配置 client_id。")

    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": str(pending["redirect_uri"]),
        "client_id": forum_client_id(),
        "code_verifier": str(pending["verifier"]),
    }
    secret = forum_client_secret()
    if secret:
        # ThemeHouse/Audentio expects the confidential client secret in the
        # form body.  Do NOT send Authorization: Basic — that is interpreted
        # as Audentio BasicCredential API auth and returns api_key_not_found.
        form["client_secret"] = secret

    token_payload: Any = None
    last_error = ""
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
        for url in _token_urls():
            try:
                request_headers = {
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                }
                response = await client.post(url, data=form, headers=request_headers)
                if response.status_code in (404, 405):
                    continue
                try:
                    body = response.json()
                except ValueError:
                    body = {"raw": response.text[:500]}
                if response.status_code >= 400:
                    last_error = f"HTTP {response.status_code}: {str(body)[:180]}"
                    continue
                token_payload = body
                break
            except Exception as exc:  # network errors: try the next compatible path
                last_error = f"{type(exc).__name__}: {exc}"
        token = _token_from_payload(token_payload)
        if not token:
            qq_bind_db.clear_forum_pending(platform_id)
            raise ForumOAuthError(f"论坛授权码兑换失败：{last_error or '响应中没有 access_token'}")

        responses: list[Any] = []
        # JWT claims are useful even when the provider's user endpoint is not
        # enabled; userinfo remains the authoritative fallback.
        for url in _userinfo_urls():
            try:
                response = await client.get(
                    url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json",
                    },
                )
                if response.status_code in (404, 405):
                    continue
                if response.status_code >= 400:
                    last_error = f"userinfo HTTP {response.status_code}"
                    continue
                try:
                    responses.append(response.json())
                except ValueError:
                    continue
            except Exception as exc:
                last_error = f"userinfo {type(exc).__name__}: {exc}"

    profile = _parse_profile(token, [token_payload, *responses])
    if not profile["xf_user_id"]:
        qq_bind_db.clear_forum_pending(platform_id)
        raise ForumOAuthError(
            "论坛没有返回用户 ID，无法完成绑定。请检查 OAuth 应用的 user scope。"
        )
    qq = int(profile["legacy_qq"]) if profile["legacy_qq"] else None
    claimed_raw = pending.get("claimed_qq")
    claimed = int(claimed_raw) if claimed_raw not in (None, "") else None
    if claimed is not None:
        if qq is None:
            qq_bind_db.clear_forum_pending(platform_id)
            raise ForumOAuthError(
                f"你填写了 QQ {claimed}，但论坛邮箱不是数字@qq.com，无法完成 OAuth 校验。\n"
                "请把论坛邮箱改成 你的QQ号@qq.com 后重新发送 qbind。"
            )
        if qq != claimed:
            qq_bind_db.clear_forum_pending(platform_id)
            raise ForumOAuthError(
                f"OAuth 校验失败：论坛邮箱对应 QQ {qq}，与你填写的 {claimed} 不一致。\n"
                "请确认水鱼查分 QQ、论坛邮箱一致后重试。"
            )
    if qq is None:
        qq_bind_db.bind_forum(
            str(platform_id),
            xf_user_id=profile["xf_user_id"],
            username=profile["username"],
            email=profile["email"],
            legacy_qq=None,
        )
        qq_bind_db.clear_forum_pending(platform_id)
        raise ForumOAuthError(
            "论坛授权成功，但邮箱不是数字@qq.com，无法自动关联查分 QQ。\n"
            "请把论坛邮箱改成 你的QQ号@qq.com 后重新发送 qbind；\n"
            "或联系管理员使用「强制绑定QQ」。"
        )
    qq_bind_db.bind_forum(
        str(platform_id),
        xf_user_id=profile["xf_user_id"],
        username=profile["username"],
        email=profile["email"],
        legacy_qq=qq,
    )
    # OneBot continues to use event.user_id directly; keeping this mapping
    # is harmless there and lets a deployment switch modes without losing
    # the forum identity link.
    qq_bind_db.bind(str(platform_id), qq)
    qq_bind_db.clear_forum_pending(platform_id)
    profile["legacy_qq"] = str(qq)
    return profile


def forum_binding_text(platform_id: str) -> str:
    row = qq_bind_db.get_forum_binding(platform_id)
    if not row:
        return "尚未绑定论坛账号。发送「qbind」获取授权链接。"
    name = row.get("username") or row.get("xf_user_id") or "未知"
    email = row.get("email") or "未返回"
    qq = row.get("legacy_qq")
    lines = [f"论坛账号：{name}", f"论坛用户 ID：{row.get('xf_user_id') or '未知'}", f"邮箱：{email}"]
    if qq:
        lines.append(f"查分 QQ：{qq}")
    else:
        lines.append("查分 QQ：未从邮箱识别（请改为 数字@qq.com 后重新 qbind，或联系管理员强制绑定）")
    return "\n".join(lines)
