from __future__ import annotations

import time
from typing import Any, Dict, Iterable, List, Optional, Set, Union

import httpx

from ..config import UUID, maiconfig
from .maimaidx_error import *
from .maimaidx_model import *
from . import maimaidx_timing as _timing
from .maimaidx_admin_audit import admin_audit

# 计入「数据获取」耗时的成绩查询接口
_FETCH_ENDPOINTS = ('/query/player', '/query/plate', '/dev/player/records', '/dev/player/record')


class MaimaiAPI:
    
    MaiProxyAPI = 'https://proxy.yuzuchan.site'
    
    MaiProberAPI = 'https://www.diving-fish.com/api/maimaidxprober'
    MaiCover = 'https://www.diving-fish.com/covers'
    MaiAliasAPI = 'https://www.yuzuchan.moe/api/maimaidx'
    QQAPI = 'http://q1.qlogo.cn/g'

    def __init__(self) -> None:
        """封装Api"""
        self.headers = None
        self.token = None
        self.tokens: List[str] = []
        self._bad_tokens: Set[str] = set()
        self._rr_idx: int = 0
        self.MaiProberProxyAPI = None
        self.MaiAliasProxyAPI = None

    @staticmethod
    def _parse_tokens(raw: Optional[str]) -> List[str]:
        """支持用逗号/空白分隔配置多个 token。"""
        if not raw:
            return []
        # 兼容：逗号、换行、空格、tab
        parts = [p.strip() for p in raw.replace(",", " ").split()]
        out: List[str] = []
        for p in parts:
            if p and p not in out:
                out.append(p)
        return out

    def load_token_proxy(self) -> None:
        self.MaiProberProxyAPI = self.MaiProberAPI if not maiconfig.maimaidxproberproxy else self.MaiProxyAPI + '/maimaidxprober'
        self.MaiAliasProxyAPI = self.MaiAliasAPI if not maiconfig.maimaidxaliasproxy else self.MaiProxyAPI + '/maimaidxaliases'
        self.token = maiconfig.maimaidxtoken
        self.tokens = self._parse_tokens(self.token)
        self._bad_tokens = set()
        self._rr_idx = 0
        if self.tokens:
            # 兼容旧逻辑：非 dev 接口也会带上 token，不影响
            self.headers = {'developer-token': self.tokens[0]}
        else:
            self.headers = None

    def _mark_token_bad(self, token: str) -> None:
        if token:
            self._bad_tokens.add(token)

    def _iter_tokens_round_robin(self) -> Iterable[str]:
        """返回当前可用 token 的轮询序列（每次调用起点不同）。"""
        if not self.tokens:
            return []
        avail = [t for t in self.tokens if t not in self._bad_tokens]
        if not avail:
            return []
        # round-robin 起点
        start = self._rr_idx % len(avail)
        self._rr_idx = (self._rr_idx + 1) % len(avail)
        return avail[start:] + avail[:start]

    async def _requestalias(self, method: str, endpoint: str, **kwargs) -> APIResult:
        """
        别名库通用请求

        Params:
            `method`: 请求方式
            `endpoint`: 请求接口
            `kwargs`: 其它参数
        Returns:
            `Dict[str, Any]` 返回结果
        """
        async with httpx.AsyncClient(timeout=30) as session:
            res = await session.request(method, self.MaiAliasProxyAPI + endpoint, **kwargs)
            if res.status_code == 200:
                data = res.json()
                return APIResult.model_validate(data)
            elif res.status_code == 500:
                raise ServerError
            else:
                raise UnknownError

    async def _requestmai_once(
        self, 
        method: str, 
        endpoint: str, 
        headers: Optional[Dict[str, str]] = None,
        *,
        max_retries: int = 2,
        retry_delay: float = 2.0,
        **kwargs
    ) -> Union[Dict[str, Any], List[Dict[str, Any]]]:
        """
        查分器通用请求（带重试机制）

        Params:
            `method`: 请求方式
            `endpoint`: 请求接口
            `max_retries`: 网络错误时最大重试次数
            `retry_delay`: 重试间隔（秒），使用指数退避
            `kwargs`: 其它参数
        Returns:
            `Dict[str, Any]` 返回结果
        """
        # 查分器 /chart_stats 等接口数据量大，超时放宽至 90 秒避免启动失败
        _is_fetch = any(endpoint.startswith(e) for e in _FETCH_ENDPOINTS)
        _bill_qq = None
        if _is_fetch:
            import asyncio
            from .maimaidx_break import ensure_query_affordable, get_billing_qqid, settle_prober_fetch
            _bill_qq = get_billing_qqid()
            if _bill_qq:
                await asyncio.to_thread(ensure_query_affordable, _bill_qq)
        _ctx = _timing.measure('fetch') if _is_fetch else None
        if _ctx:
            _ctx.__enter__()
        
        last_exc: Optional[Exception] = None
        for attempt in range(max_retries + 1):
            _audit_started = time.time()
            try:
                try:
                    async with httpx.AsyncClient(timeout=httpx.Timeout(90)) as session:
                        res = await session.request(
                            method,
                            self.MaiProberProxyAPI + endpoint,
                            headers=headers,
                            **kwargs
                        )
                except Exception as exc:
                    admin_audit.add_step(
                        'http.divingfish', 'error',
                        {'method': method, 'endpoint': endpoint, 'error': str(exc)},
                        started_at=_audit_started,
                    )
                    last_exc = exc
                    if attempt < max_retries:
                        import asyncio
                        await asyncio.sleep(retry_delay * (attempt + 1))
                        continue
                    raise
            finally:
                if _ctx and attempt == max_retries:
                    _ctx.__exit__(None, None, None)
            
            admin_audit.add_step(
                'http.divingfish',
                'success' if res.status_code == 200 else 'error',
                {'method': method, 'endpoint': endpoint, 'status_code': res.status_code},
                started_at=_audit_started,
            )
            if res.status_code == 200:
                data = res.json()
                if _bill_qq:
                    await asyncio.to_thread(settle_prober_fetch, _bill_qq)
                return data
            elif res.status_code == 400:
                error: Dict = res.json()
                if 'message' in error:
                    if error['message'] == 'no such user':
                        raise UserNotFoundError
                    elif error['message'] == 'user not exists':
                        raise UserNotExistsError
                    else:
                        raise UserNotFoundError
                elif 'msg' in error:
                    if error['msg'] == '开发者token有误':
                        raise TokenError
                    elif error['msg'] == '开发者token被禁用':
                        raise TokenDisableError
                    else:
                        raise TokenNotFoundError
                else:
                    raise UserNotFoundError
            elif res.status_code == 403:
                raise UserDisabledQueryError
            else:
                raise UnknownError
        
        # 不应到达此处，但以防万一
        if last_exc is not None:
            raise last_exc
        raise UnknownError

    async def _requestmai(
        self,
        method: str,
        endpoint: str,
        **kwargs,
    ) -> Union[Dict[str, Any], List[Dict[str, Any]]]:
        """
        查分器请求：配置了 token 池时自动轮询。
        - token 相关错误 -> 标记坏并换下一个
        - 全部失败才抛最后一次 token 错误
        - 未配置 token 时不带 developer-token 头
        """
        if not self.tokens:
            return await self._requestmai_once(method, endpoint, headers=None, **kwargs)

        last_err: Optional[Exception] = None
        for t in self._iter_tokens_round_robin():
            try:
                return await self._requestmai_once(
                    method,
                    endpoint,
                    headers={"developer-token": t},
                    **kwargs,
                )
            except (TokenError, TokenDisableError, TokenNotFoundError) as e:
                self._mark_token_bad(t)
                last_err = e
                continue
        if last_err is not None:
            raise last_err
        raise TokenNotFoundError

    async def _requestmai_dev(
        self,
        method: str,
        endpoint: str,
        **kwargs,
    ) -> Union[Dict[str, Any], List[Dict[str, Any]]]:
        """dev 接口（与 _requestmai 共用 token 池）。"""
        return await self._requestmai(method, endpoint, **kwargs)

    async def music_data(self):
        """获取曲目数据"""
        return await self._requestmai('GET', '/music_data')

    async def chart_stats(self):
        """获取单曲数据"""
        return await self._requestmai('GET', '/chart_stats')

    async def query_user_b50(
        self, 
        *, 
        qqid: Optional[int] = None, 
        username: Optional[str] = None
    ) -> UserInfo:
        """
        获取玩家 B50。查分器约定 charts.sd=B35(35首)、charts.dx=B15(15首)，与谱面类型 SD/DX 无关。
        """
        json = {}
        if qqid:
            json['qq'] = qqid
        if username:
            json['username'] = username
        json['b50'] = True
        userinfo = UserInfo.model_validate(await self._requestmai('POST', '/query/player', json=json))
        from .maimaidx_best_50 import regroup_b50_userinfo
        return regroup_b50_userinfo(userinfo)

    async def query_user_plate(
        self,
        *,
        qqid: Optional[int] = None,
        username: Optional[str] = None,
        version: Optional[List[str]] = None
    ) -> List[PlayInfoDefault]:
        """
        请求用户数据

        Params:
            `qqid`: 用户QQ
            `username`: 查分器用户名
            `version`: 版本
        Returns:
            `List[PlayInfoDefault]` 数据列表
        """
        json = {}
        if qqid:
            json['qq'] = qqid
        if username:
            json['username'] = username
        if version:
            json['version'] = version
        result = await self._requestmai('POST', '/query/plate', json=json)
        return [PlayInfoDefault.model_validate(d) for d in result['verlist']]

    async def query_user_get_dev(
        self, 
        *, 
        qqid: Optional[int] = None, 
        username: Optional[str] = None
    ) -> UserInfoDev:
        """
        使用开发者接口获取用户数据，请确保拥有和输入了开发者 `token`

        Params:
            qqid: 用户QQ
            username: 查分器用户名
        Returns:
            `UserInfoDev` 开发者用户信息
        """
        params = {}
        if qqid:
            params['qq'] = qqid
        if username:
            params['username'] = username
        
        result = await self._requestmai_dev('GET', '/dev/player/records', params=params)
        return UserInfoDev.model_validate(result)

    async def query_user_post_dev(
        self,
        *,
        qqid: Optional[int] = None,
        username: Optional[str] = None,
        music_id: Union[str, int, List[Union[str, int]]]
    ) -> List[PlayInfoDev]:
        """
        使用开发者接口获取用户指定曲目数据，请确保拥有和输入了开发者 `token`

        Params:
            `qqid`: 用户QQ
            `username`: 查分器用户名
            `music_id`: 曲目id，可以为单个ID或者列表
        Returns:
            `List[PlayInfoDev]` 开发者成绩列表
        """
        json = {}
        if qqid:
            json['qq'] = qqid
        if username:
            json['username'] = username
        json['music_id'] = music_id
        
        result = await self._requestmai_dev('POST', '/dev/player/record', json=json)
        if result == {}:
            raise MusicNotPlayError
        
        if isinstance(music_id, list):
            return [PlayInfoDev.model_validate(d) for k, v in result.items() for d in v]
        return [PlayInfoDev.model_validate(d) for d in result[str(music_id)]]

    async def rating_ranking(self) -> List[UserRanking]:
        """
        获取查分器排行榜
        
        Returns:
            `List[UserRanking]` 按`ra`从高到低排序后的查分器排行模型列表
        """
        result = await self._requestmai('GET', '/rating_ranking')
        return sorted([UserRanking.model_validate(u) for u in result], key=lambda x: x.ra, reverse=True)

    async def get_plate_json(self) -> Dict[str, List[int]]:
        """获取所有版本牌子完成需求"""
        result = await self._requestalias('GET', '/maimaidxplate')
        if result.code == 0:
            return result.content
        raise UnknownError
    
    async def get_alias(self) -> Dict[str, Union[str, int, List[str]]]:
        """获取所有别名"""
        result = await self._requestalias('GET', '/maimaidxalias')
        if result.code == 0:
            return result.content
        raise UnknownError

    async def get_songs(self, name: str) -> Union[List[AliasStatus], List[Alias]]:
        """
        使用别名查询曲目。
        `code` 为 `0` 时返回值为 `List[Alias]`。
        `code` 为 `3006` 时返回值为 `List[AliasStatus]`。
        
        Params:
            `name`: 别名
        Returns:
            `Union[List[AliasStatus], List[Alias]]`
        """
        result = await self._requestalias('GET', '/getsongs', params={'name': name})
        if result.code == 3006:
            return [AliasStatus.model_validate(s) for s in result.content]
        elif result.code == 1004:
            return []
        elif result.code == 0:
            return [Alias.model_validate(s) for s in result.content]
        else:
            raise UnknownError

    async def get_songs_alias(self, song_id: int) -> Union[Alias, str]:
        """
        使用曲目 `id` 查询别名
        
        Params:
            `song_id`: 曲目 `ID`
        Returns:
            `Alias` | `str`
        """
        result = await self._requestalias('GET', '/getsongsalias', params={'song_id': song_id})
        if result.code == 0:
            return Alias.model_validate(result.content)
        elif result.code == 1004:
            return result.content
        else:
            raise UnknownError

    async def get_alias_status(self) -> List[AliasStatus]:
        """获取当前正在进行的别名投票"""
        result = await self._requestalias('GET', '/getaliasstatus')
        if result.code == 0:
            return [AliasStatus.model_validate(s) for s in result.content]
        elif result.code == 1004:
            return []
        else:
            raise UnknownError

    async def post_alias(
        self, 
        song_id: int, 
        aliasname: str, 
        user_id: int,
        group_id: int
    ) -> Union[AliasStatus, str]:
        """
        提交别名申请

        Params:
            `id`: 曲目 `id`
            `aliasname`: 别名
            `user_id`: 提交的用户
        Returns:
            `AliasStatus`
        """
        json = {
            'SongID': song_id,
            'ApplyAlias': aliasname,
            'ApplyUID': user_id,
            'GroupID': group_id,
            'WSUUID': str(UUID)
        }
        result = await self._requestalias('POST', '/applyalias', json=json)
        return result.content

    async def post_agree_user(self, tag: str, user_id: int) -> str:
        """
        提交同意投票

        Params:
            `tag`: 标签
            `user_id`: 同意投票的用户
        Returns:
            `str`
        """
        json = {
            'Tag': tag,
            'AgreeUser': user_id
        }
        result = await self._requestalias('POST', '/agreeuser', json=json)
        return result.content

    async def qqlogo(self, qqid: int = None, icon: str = None) -> Optional[bytes]:
        """获取QQ头像"""
        session = httpx.AsyncClient(timeout=30)
        if qqid:
            params = {
                'b': 'qq',
                'nk': qqid,
                's': 100
            }
            res = await session.request('GET', self.QQAPI, params=params)
        elif icon:
            res = await session.request('GET', icon)
        else:
            return None
        return res.content


maiApi = MaimaiAPI()
