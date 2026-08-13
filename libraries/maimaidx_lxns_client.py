"""
落雪查分器（Lxns / maimai.lxns.net）API 客户端。

支持两种查询方式：
  - 开发者 Token（查曲库 / 别名 / 按 QQ 或好友码查别人）
  - OAuth2 用户授权（查自己的 b50 / recent / scores 等私有数据）
"""

import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import httpx

from ..config import log, maiconfig

_BASE_URL = 'https://maimai.lxns.net'
# 落雪「无回调模式」标准 OOB 地址（授权后直接在页面显示授权码）
_DEFAULT_REDIRECT_URI = 'urn:ietf:wg:oauth:2.0:oob'

_INVALID_SCORE_RE = re.compile(
    r'invalid score \(id:\s*(\d+),\s*type:\s*([a-zA-Z_]+),\s*level_index:\s*(\d+)\)',
    re.IGNORECASE,
)


class LxnsApiError(RuntimeError):
    """落雪 API 错误；仅保留可安全展示的状态码与服务端说明。"""

    def __init__(self, message: str, *, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


def _error_message(payload: Any, fallback: str) -> str:
    if not isinstance(payload, dict):
        return fallback
    return str(
        payload.get('error_description')
        or payload.get('message')
        or payload.get('error')
        or fallback
    )


def _parse_oauth_token_response(
    response: httpx.Response, *, operation: str
) -> Dict[str, Any]:
    """兼容 OAuth 标准顶层响应与旧版 ``success/data`` 包装。"""
    try:
        payload = response.json()
    except ValueError as exc:
        raise LxnsApiError(
            f'{operation}响应不是有效 JSON', status_code=response.status_code
        ) from exc

    if response.is_error:
        raise LxnsApiError(
            _error_message(payload, f'{operation}失败'),
            status_code=response.status_code,
        )

    token_data = payload.get('data') if isinstance(payload, dict) else None
    if not isinstance(token_data, dict) or not token_data.get('access_token'):
        token_data = payload
    if not isinstance(token_data, dict) or not token_data.get('access_token'):
        raise LxnsApiError(
            _error_message(payload, f'{operation}未返回 access_token'),
            status_code=response.status_code,
        )
    return token_data


def _parse_user_api_response(
    response: httpx.Response, *, operation: str
) -> Dict[str, Any]:
    """解析落雪 OAuth 用户 API 的统一响应，并保留明确失败原因。"""
    try:
        payload = response.json()
    except ValueError as exc:
        raise LxnsApiError(
            f'{operation}响应不是有效 JSON', status_code=response.status_code
        ) from exc
    if response.is_error or not isinstance(payload, dict) or payload.get('success') is False:
        raise LxnsApiError(
            _error_message(payload, f'{operation}失败'),
            status_code=response.status_code,
        )
    return payload


def _dev_headers() -> Dict[str, str]:
    """开发者 Token 请求头。"""
    return {'Authorization': maiconfig.lxns_dev_token or ''}


def _oauth_headers(access_token: str) -> Dict[str, str]:
    """OAuth 用户请求头。"""
    return {'Authorization': f'Bearer {access_token}'}


# ─────────────────────────── OAuth ───────────────────────────


def get_authorize_url(client_id: str, scope: str = 'read_player read_user_profile write_player') -> str:
    """生成 OAuth 授权链接。无回调模式使用 OOB 地址，授权后页面直接显示授权码。"""
    redirect_uri = maiconfig.lx_redirect_uri or _DEFAULT_REDIRECT_URI
    query = urlencode({
        'response_type': 'code',
        'client_id': client_id,
        'redirect_uri': redirect_uri,
        'scope': scope,
    })
    return f'{_BASE_URL}/oauth/authorize?{query}'


async def fetch_token(code: str) -> Dict[str, Any]:
    """
    用授权码换取 access_token / refresh_token。
    返回 OAuth2Token 字典：access_token, token_type, expires_in, refresh_token, scope
    """
    redirect_uri = maiconfig.lx_redirect_uri or _DEFAULT_REDIRECT_URI
    payload = {
        'client_id': maiconfig.lx_client_id,
        'client_secret': maiconfig.lx_client_secret,
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': redirect_uri,
    }

    timeout = httpx.Timeout(connect=5.0, read=15.0, write=15.0, pool=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(f'{_BASE_URL}/api/v0/oauth/token', json=payload)
        return _parse_oauth_token_response(resp, operation='OAuth 授权码兑换')


async def refresh_token(refresh_token: str) -> Dict[str, Any]:
    """
    用 refresh_token 刷新 access_token。
    返回新的 OAuth2Token 字典。
    """
    payload = {
        'client_id': maiconfig.lx_client_id,
        'client_secret': maiconfig.lx_client_secret,
        'grant_type': 'refresh_token',
        'refresh_token': refresh_token,
    }
    timeout = httpx.Timeout(connect=5.0, read=15.0, write=15.0, pool=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(f'{_BASE_URL}/api/v0/oauth/token', json=payload)
        return _parse_oauth_token_response(resp, operation='OAuth Token 刷新')


# ─────────────────────────── 开发者 API ───────────────────────────


async def _billable_lxns_fetch(coro):
    """落雪成绩/玩家 API：在 break_billing 上下文中扣费。"""
    import asyncio
    from .maimaidx_break import ensure_query_affordable, get_billing_qqid, settle_prober_fetch
    from .maimaidx_admin_audit import admin_audit

    qqid = get_billing_qqid()
    if qqid:
        await asyncio.to_thread(ensure_query_affordable, qqid)
    started = time.time()
    try:
        result = await coro
    except Exception as exc:
        admin_audit.add_step(
            'http.lxns', 'error', {'error': str(exc)}, started_at=started,
        )
        raise
    admin_audit.add_step('http.lxns', 'success', started_at=started)
    if qqid and result is not None:
        await asyncio.to_thread(settle_prober_fetch, qqid)
    return result


async def dev_get_player_by_qq(qq: int) -> Optional[Dict[str, Any]]:
    """通过 QQ 号获取玩家信息（开发者 Token）。"""
    async def _fetch():
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f'{_BASE_URL}/api/v0/maimai/player/qq/{qq}',
                headers=_dev_headers(),
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            result = resp.json()
            if not result.get('success'):
                return None
            return result.get('data')

    return await _billable_lxns_fetch(_fetch())


async def dev_get_player_by_friend_code(friend_code: int) -> Optional[Dict[str, Any]]:
    """通过好友码获取玩家信息（开发者 Token）。"""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f'{_BASE_URL}/api/v0/maimai/player/{friend_code}',
            headers=_dev_headers(),
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        result = resp.json()
        if not result.get('success'):
            return None
        return result.get('data')


async def dev_get_bests(friend_code: int) -> Optional[Dict[str, Any]]:
    """通过好友码获取玩家 Best50（开发者 Token）。"""
    async def _fetch():
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f'{_BASE_URL}/api/v0/maimai/player/{friend_code}/bests',
                headers=_dev_headers(),
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            result = resp.json()
            if not result.get('success'):
                return None
            return result.get('data')

    return await _billable_lxns_fetch(_fetch())


# ─────────────────────────── 用户 API（OAuth） ───────────────────────────


async def user_get_bests(access_token: str) -> Optional[Dict[str, Any]]:
    """获取当前用户的 Best50（OAuth token）。"""
    async def _fetch():
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f'{_BASE_URL}/api/v0/user/maimai/player/bests',
                headers=_oauth_headers(access_token),
            )
            result = _parse_user_api_response(resp, operation='获取落雪 B50')
            return result.get('data')

    return await _billable_lxns_fetch(_fetch())


async def user_get_player(access_token: str) -> Optional[Dict[str, Any]]:
    """获取当前用户信息（OAuth token）。"""
    async def _fetch():
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f'{_BASE_URL}/api/v0/user/maimai/player',
                headers=_oauth_headers(access_token),
            )
            result = _parse_user_api_response(resp, operation='获取落雪用户信息')
            return result.get('data')

    return await _billable_lxns_fetch(_fetch())


async def user_get_scores(access_token: str) -> Optional[list]:
    """获取当前用户所有成绩（OAuth token）。返回 Score 列表。"""
    async def _fetch():
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f'{_BASE_URL}/api/v0/user/maimai/player/scores',
                headers=_oauth_headers(access_token),
            )
            result = _parse_user_api_response(resp, operation='获取落雪成绩')
            return result.get('data')

    return await _billable_lxns_fetch(_fetch())


def _lxns_song_id_type(raw_id: int) -> Optional[tuple[int, str]]:
    """Sega musicId → 落雪 (id, type)。同一曲目标准/DX 共用基础 ID。"""
    if raw_id > 100000:
        return raw_id, 'utage'
    if raw_id >= 10000:
        return raw_id % 10000, 'dx'
    if raw_id > 0:
        return raw_id, 'standard'
    return None


def convert_sega_music_scores(detail_list: List[dict]) -> List[dict]:
    """把 ``userMusicDetail`` 转成落雪个人 API 接受的 Score 列表。"""
    combo_map = {0: None, 1: 'fc', 2: 'fcp', 3: 'ap', 4: 'app'}
    sync_map = {0: None, 1: 'fs', 2: 'fsp', 3: 'fsd', 4: 'fsdp', 5: 'sync'}
    best: Dict[tuple[int, str, int], dict] = {}

    for item in detail_list:
        try:
            raw_id = int(item.get('musicId', 0))
            level_index = int(item.get('level', 0))
            achievement = float(item.get('achievement', 0)) / 10000.0
            dx_score = int(item.get('deluxscoreMax', 0) or 0)
        except (TypeError, ValueError):
            continue
        if raw_id <= 0 or not 0 <= level_index <= 4 or achievement < 0:
            continue
        parsed = _lxns_song_id_type(raw_id)
        if not parsed:
            continue
        song_id, song_type = parsed

        score = {
            'id': song_id,
            'type': song_type,
            'level_index': level_index,
            'achievements': achievement,
            'dx_score': dx_score,
            # 仅客户端兜底用；POST 前会剥离。DX 被拒时可回退成 Sega 原始 ID（如 11407）。
            '_raw_music_id': raw_id,
        }
        fc = combo_map.get(item.get('comboStatus'))
        fs = sync_map.get(item.get('syncStatus'))
        # 落雪 Score 接口期望缺省字段省略，而不是显式 null。
        if fc:
            score['fc'] = fc
        if fs:
            score['fs'] = fs
        key = (song_id, song_type, level_index)
        previous = best.get(key)
        if previous is None or (achievement, dx_score) > (
            previous['achievements'], previous['dx_score']
        ):
            best[key] = score
    return list(best.values())


def convert_pc_records_to_lxns_scores(records: List[Any]) -> List[dict]:
    """把本地 PC 库记录转成落雪 Score（无需再登录机台）。"""
    best: Dict[tuple[int, str, int], dict] = {}
    for item in records:
        try:
            raw_id = int(getattr(item, 'song_id', 0) or 0)
            level_index = int(getattr(item, 'level_index', -1))
            achievement = float(getattr(item, 'achievements', 0) or 0)
            dx_score = int(getattr(item, 'dx_score', 0) or 0)
        except (TypeError, ValueError):
            continue
        if raw_id <= 0 or not 0 <= level_index <= 4 or achievement < 0:
            continue
        parsed = _lxns_song_id_type(raw_id)
        if not parsed:
            continue
        song_id, song_type = parsed
        score = {
            'id': song_id,
            'type': song_type,
            'level_index': level_index,
            'achievements': achievement,
            'dx_score': dx_score,
            # 仅客户端兜底用；POST 前会剥离。DX 被拒时可回退成 Sega 原始 ID（如 11407）。
            '_raw_music_id': raw_id,
        }
        fc = str(getattr(item, 'fc', '') or '').strip() or None
        fs = str(getattr(item, 'fs', '') or '').strip() or None
        if fc:
            score['fc'] = fc
        if fs:
            score['fs'] = fs
        key = (song_id, song_type, level_index)
        previous = best.get(key)
        if previous is None or (achievement, dx_score) > (
            previous['achievements'], previous['dx_score']
        ):
            best[key] = score
    return list(best.values())


def _public_score_payload(score: dict) -> dict:
    """去掉客户端私有字段，只提交落雪 API 接受的键。"""
    return {k: v for k, v in score.items() if not str(k).startswith('_')}


def _parse_invalid_score(message: str) -> Optional[Tuple[int, str, int]]:
    match = _INVALID_SCORE_RE.search(str(message or ''))
    if not match:
        return None
    return int(match.group(1)), str(match.group(2)).lower(), int(match.group(3))


def _score_key(score: dict) -> Tuple[int, str, int]:
    return (
        int(score.get('id') or 0),
        str(score.get('type') or '').lower(),
        int(score.get('level_index') or 0),
    )


def _dx_raw_id_fallback(score: dict) -> Optional[dict]:
    """DX 取余 ID 不被落雪识别时，回退为 Sega 原始 ID（如 1407 → 11407）。"""
    song_type = str(score.get('type') or '').lower()
    song_id = int(score.get('id') or 0)
    if song_type != 'dx' or song_id <= 0:
        return None
    raw = score.get('_raw_music_id')
    try:
        raw_id = int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        raw_id = 0
    if raw_id >= 10000:
        alt_id = raw_id
    elif song_id < 10000:
        alt_id = song_id + 10000
    else:
        return None
    if alt_id == song_id:
        return None
    updated = dict(score)
    updated['id'] = alt_id
    return updated


async def user_upload_scores(access_token: str, scores: List[dict]) -> Dict[str, Any]:
    """使用 OAuth ``write_player`` 权限上传当前用户成绩。

    遇到 ``song not found`` 时：
    1. DX 曲目先把 id 回退成 Sega 原始 ID（1407→11407）再试一次；
    2. 仍失败则 skip 该谱面，继续上传其余成绩。
    """
    if not scores:
        raise ValueError('没有可上传到落雪的有效成绩')

    from .maimaidx_admin_audit import admin_audit

    uploaded = 0
    skipped: List[str] = []
    started = time.time()
    timeout = httpx.Timeout(connect=5.0, read=15.0, write=15.0, pool=5.0)
    overall_deadline = time.monotonic() + 45.0
    # 单批内对 invalid score 的修复/跳过次数上限，避免一张坏谱面反复打爆超时。
    max_repairs_per_batch = 40
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            for offset in range(0, len(scores), 500):
                remaining = overall_deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f'落雪成绩上传总超时（已写入 {uploaded}/{len(scores)} 条，'
                        f'跳过 {len(skipped)} 条）'
                    )
                pending = [dict(item) for item in scores[offset:offset + 500]]
                tried_alt: set[Tuple[int, str, int]] = set()
                repairs = 0
                while pending:
                    remaining = overall_deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(
                            f'落雪成绩上传总超时（已写入 {uploaded} 条，跳过 {len(skipped)} 条）'
                        )
                    payload = [_public_score_payload(item) for item in pending]
                    resp = await client.post(
                        f'{_BASE_URL}/api/v0/user/maimai/player/scores',
                        headers=_oauth_headers(access_token),
                        json={'scores': payload},
                    )
                    try:
                        _parse_user_api_response(resp, operation='OAuth 成绩上传')
                        uploaded += len(pending)
                        break
                    except LxnsApiError as exc:
                        invalid = _parse_invalid_score(str(exc))
                        if not invalid or repairs >= max_repairs_per_batch:
                            raise
                        bad_id, bad_type, bad_level = invalid
                        target_idx = next(
                            (
                                i
                                for i, item in enumerate(pending)
                                if _score_key(item) == (bad_id, bad_type, bad_level)
                            ),
                            None,
                        )
                        if target_idx is None:
                            raise
                        bad = pending[target_idx]
                        alt = _dx_raw_id_fallback(bad)
                        alt_key = _score_key(alt) if alt else None
                        if (
                            alt is not None
                            and alt_key is not None
                            and _score_key(bad) not in tried_alt
                            and alt_key not in tried_alt
                        ):
                            tried_alt.add(_score_key(bad))
                            tried_alt.add(alt_key)
                            pending[target_idx] = alt
                            repairs += 1
                            log.warning(
                                f'[lxns.upload] DX id 回退 {_score_key(bad)} → {alt_key}'
                            )
                            continue
                        label = f'{bad_type}:{bad_id}@{bad_level}'
                        skipped.append(label)
                        pending.pop(target_idx)
                        repairs += 1
                        log.warning(f'[lxns.upload] 跳过无效成绩 {label}: {exc}')
                        if not pending:
                            break
            if uploaded <= 0:
                detail = '、'.join(skipped[:5])
                more = f' 等 {len(skipped)} 条' if len(skipped) > 5 else ''
                raise LxnsApiError(
                    f'落雪未接受任何成绩'
                    + (f'（已跳过 {detail}{more}）' if skipped else ''),
                    status_code=400,
                )
    except Exception as exc:
        admin_audit.add_step(
            'http.lxns.upload', 'error',
            {
                'error': str(exc),
                'uploaded': uploaded,
                'skipped': len(skipped),
            },
            started_at=started,
        )
        raise
    admin_audit.add_step(
        'http.lxns.upload',
        'success',
        {'count': uploaded, 'skipped': len(skipped)},
        started_at=started,
    )
    return {
        'success': True,
        'count': uploaded,
        'skipped': len(skipped),
        'skipped_scores': skipped[:20],
        'oauth': True,
    }



async def dev_get_scores(friend_code: int) -> Optional[list]:
    """通过好友码获取玩家所有成绩（开发者 Token）。返回 Score 列表。"""
    async def _fetch():
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f'{_BASE_URL}/api/v0/maimai/player/{friend_code}/scores',
                headers=_dev_headers(),
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            result = resp.json()
            if not result.get('success'):
                return None
            return result.get('data')

    return await _billable_lxns_fetch(_fetch())
