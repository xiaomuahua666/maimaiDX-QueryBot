"""
统一数据源层：根据用户的「数据源」设置，从水鱼或落雪获取成绩数据，
对外返回相同的数据结构（UserInfo / UserInfoDev），使各指令可透明切换。

关键限制：
  - 落雪按用户名查询不支持（lxns 无 username 接口），username 查询强制走水鱼。
  - 落雪全量成绩需 OAuth 授权（dev 接口按好友码仅返回简化成绩，无达成率）。
  - 拟合难度（fit_diff）、全服 rating 排行为水鱼独有，相关功能不切换。
"""

import asyncio
from typing import List, Optional, Tuple

from ..config import log
from . import maimaidx_timing as _timing
from .maimaidx_api_data import maiApi
from .maimaidx_error import LxnsDataError
from .maimaidx_lxns_client import (
    dev_get_bests,
    dev_get_player_by_qq,
    user_get_bests,
    user_get_player,
    user_get_scores,
)
from .maimaidx_lxns_db import lxns_db
from .maimaidx_model import ChartInfo, Data, PlayInfoDev, UserInfo
from .maimaidx_music import mai
from .maimaidx_player_cache import (
    clear_fetch_meta,
    get_cached_player,
    resolve_player_b50,
    resolve_player_records,
    save_cached_player,
)

_LEVEL_LABELS = ['Basic', 'Advanced', 'Expert', 'Master', 'Re:Master']


def get_user_source(qqid: int) -> str:
    """获取用户的数据源偏好；新用户默认使用 AWMCNET。"""
    try:
        return lxns_db.get_source(qqid)
    except Exception:
        return 'awmcnet'


def _records_to_userinfo(player: dict, records: List[PlayInfoDev]) -> UserInfo:
    """把 AWMCNET 的统一成绩响应还原为 QueryBot 的 B50 结构。"""
    from .maimaidx_best_50 import regroup_b50_userinfo

    return regroup_b50_userinfo(UserInfo(
        nickname=str(player.get('nickname') or 'AWMCNET 用户'),
        rating=int(player.get('rating') or 0),
        additional_rating=0,
        username=str(player.get('username') or ''),
        charts=Data(sd=list(records), dx=[]),
    ))


def _awmcnet_records(player: dict) -> List[PlayInfoDev]:
    records: List[PlayInfoDev] = []
    for raw in player.get('records') or []:
        try:
            records.append(PlayInfoDev(**raw))
        except Exception as exc:
            log.warning(f'[datasource] AWMCNET record ignored: {exc}')
    return records


def _merge_upstream_records(results: list) -> Tuple[Optional[UserInfo], List[PlayInfoDev]]:
    """合并水鱼/落雪成绩；同谱面保留达成率和 DX 分更高的一条。"""
    best: dict[tuple[int, int], PlayInfoDev] = {}
    users: list[UserInfo] = []
    for result in results:
        if isinstance(result, Exception):
            continue
        userinfo, records = result
        users.append(userinfo)
        for record in records:
            key = (int(record.song_id), int(record.level_index))
            current = best.get(key)
            candidate_rank = (float(record.achievements), int(record.dxScore or 0))
            current_rank = (
                (float(current.achievements), int(current.dxScore or 0))
                if current else (-1.0, -1)
            )
            if candidate_rank > current_rank:
                best[key] = record
    if not users:
        return None, []
    userinfo = max(users, key=lambda item: int(item.rating or 0))
    return userinfo, list(best.values())


async def _refresh_awmcnet_from_upstreams(
    qqid: int,
    *,
    force_refresh: bool = False,
    base_result: Optional[Tuple[UserInfo, List[PlayInfoDev]]] = None,
) -> Tuple[Optional[UserInfo], List[PlayInfoDev]]:
    """并行拉取可用上游并写入 AWMCNET，实现首次查询自动迁移。

    ``base_result`` is the current AWMCNET snapshot during an explicit refresh.
    Keeping it in the merge prevents eventually-consistent upstreams from
    replacing a score that was uploaded to AWMCNET seconds earlier.
    """
    attempts = await asyncio.gather(
        get_user_records(
            qqid=qqid, force_source='divingfish', force_refresh=force_refresh
        ),
        get_user_records(qqid=qqid, force_source='lxns', force_refresh=force_refresh),
        return_exceptions=True,
    )
    for source, result in zip(('divingfish', 'lxns'), attempts):
        if isinstance(result, Exception):
            # 上游无用户/OAuth 未绑定是自动探测的正常结果；保留 DEBUG
            # 便于排障，避免群批量查询或定时补存刷满生产日志。
            log.debug(
                f'[datasource] AWMCNET migration skipped {source} qq={qqid}: {result}'
            )
    merge_results = list(attempts)
    if base_result is not None and base_result[1]:
        merge_results.insert(0, base_result)
    userinfo, records = _merge_upstream_records(merge_results)
    if userinfo and records:
        from .maimaidx_awmcnet_sync import sync_awmcnet
        await sync_awmcnet(qqid, userinfo, records, source='auto-migrate')
    return userinfo, records


async def _get_awmcnet_records(
    qqid: int, *, force_refresh: bool = False
) -> Tuple[UserInfo, List[PlayInfoDev]]:
    from .maimaidx_awmcnet_sync import fetch_awmcnet_player

    player = await fetch_awmcnet_player(qqid)
    current_result: Optional[Tuple[UserInfo, List[PlayInfoDev]]] = None
    if player and player.get('records'):
        records = _awmcnet_records(player)
        current_result = (_records_to_userinfo(player, records), records)
        if not force_refresh:
            return current_result

    # AWMC NET 没有成绩时才自动探测水鱼和落雪；强制刷新时也执行探测。
    upstream_user, upstream_records = await _refresh_awmcnet_from_upstreams(
        qqid,
        force_refresh=force_refresh,
        base_result=current_result,
    )
    if upstream_user and upstream_records:
        refreshed = await fetch_awmcnet_player(qqid)
        if refreshed and refreshed.get('records'):
            refreshed_records = _awmcnet_records(refreshed)
            merged_user, merged_records = _merge_upstream_records([
                (upstream_user, upstream_records),
                (_records_to_userinfo(refreshed, refreshed_records), refreshed_records),
            ])
            if merged_user and merged_records:
                return merged_user, merged_records
    if player:
        records = _awmcnet_records(player)
        if records:
            return _records_to_userinfo(player, records), records
    if upstream_user and upstream_records:
        return _records_to_userinfo({
            'nickname': upstream_user.nickname,
            'username': upstream_user.username,
            'rating': upstream_user.rating,
        }, upstream_records), upstream_records
    raise LxnsDataError('用户不存在：AWMC NET.、水鱼和落雪均未找到可用成绩。')


# 不支持落雪的功能名 -> 用于提示文案
def lxns_unsupported_notice(qqid: Optional[int], feature: str = '该功能') -> str:
    """
    若用户数据源为落雪，返回提示文案（说明该功能仅支持水鱼，已用水鱼数据）；
    否则返回空字符串。
    """
    if qqid and get_user_source(qqid) == 'lxns':
        return f'\n[提示] {feature}依赖水鱼独有数据，落雪暂不支持，已使用水鱼数据生成。'
    return ''


def _import_compute_ra():
    """延迟导入 computeRa，避免循环导入。"""
    from .maimaidx_best_50 import computeRa
    return computeRa


def _flatten_lxns_scores(raw) -> list:
    """将落雪成绩响应统一展平为 dict 列表（兼容 Score[] / SimpleScore[] / bests 结构）。"""
    if not raw:
        return []
    if isinstance(raw, dict):
        nested = raw.get('scores')
        if isinstance(nested, list):
            return _flatten_lxns_scores(nested)
        out: list = []
        for key in ('standard', 'dx'):
            part = raw.get(key)
            if isinstance(part, list):
                out.extend(part)
        if out:
            return [s for s in out if isinstance(s, dict)]
    if isinstance(raw, list):
        return [s for s in raw if isinstance(s, dict)]
    return []


def _lxns_raw_song_id(score: dict) -> Optional[int]:
    """从落雪成绩对象提取曲目 ID（兼容 id / song_id / diff_id）。"""
    for key in ('id', 'song_id', 'diff_id'):
        val = score.get(key)
        if val is None:
            continue
        try:
            return int(val)
        except (TypeError, ValueError):
            continue
    return None


def _lxns_achievements_from_rate(rate: str) -> float:
    """SimpleScore 无 achievements 时，按评级取下限达成率估算。"""
    table = {
        'd': 0.0, 'c': 50.0, 'b': 60.01, 'bb': 70.01, 'bbb': 75.01,
        'a': 80.01, 'aa': 90.01, 'aaa': 94.01, 's': 97.01,
        'sp': 98.01, 'ss': 99.01, 'ssp': 99.51, 'sss': 100.0, 'sssp': 100.5,
    }
    return table.get((rate or '').lower(), 0.0)


def _resolve_local_music(lxns_id: int, lxns_type: str):
    """
    把落雪的 (id, type) 映射到本地（水鱼）曲目。

    落雪约定：标准/DX 谱面共用同一基础 ID（不带 +10000 偏移）。
    水鱼约定：DX 谱面 ID = 基础 ID + 10000。

    返回 (music, local_id_str)；找不到返回 (None, None)。
    """
    is_dx = lxns_type == 'dx'
    candidates = []
    if is_dx:
        # 水鱼 DX 谱面 ID 通常为 基础ID + 10000
        candidates.append(str(lxns_id + 10000))
        candidates.append(str(lxns_id))
    else:
        candidates.append(str(lxns_id))
        candidates.append(str(lxns_id + 10000))

    want_type = 'DX' if is_dx else 'SD'
    # 优先匹配 id 且 type 一致
    for cid in candidates:
        m = mai.total_list.by_id(cid)
        if m and getattr(m, 'type', '').upper() == want_type:
            return m, cid
    # 退而求其次：id 命中即可（type 可能缺失）
    for cid in candidates:
        m = mai.total_list.by_id(cid)
        if m:
            return m, cid
    return None, None


def _lxns_score_to_chartinfo(score: dict):
    """将 lxns score dict 转换为 ChartInfo（也兼容 PlayInfoDev，字段一致）。"""
    computeRa = _import_compute_ra()
    raw_id = _lxns_raw_song_id(score)
    if raw_id is None:
        log.warning(f'[datasource] lxns score missing id/song_id: keys={list(score.keys())}')
        return None
    lxns_type = score.get('type', 'standard')
    if lxns_type == 'utage' or raw_id > 100000:
        lxns_id = raw_id
    else:
        lxns_id = raw_id % 10000 if raw_id > 10000 else raw_id
    level_index = score.get('level_index', 0)
    achievements = score.get('achievements')
    if achievements is None:
        achievements = _lxns_achievements_from_rate(score.get('rate', ''))
    song_type = 'DX' if lxns_type == 'dx' else 'SD'

    music, local_id = _resolve_local_music(lxns_id, lxns_type)
    if music is None:
        return None

    song_id = int(local_id)

    if level_index < len(music.ds):
        ds = round(float(music.ds[level_index]), 1)
        level_str = music.level[level_index] if level_index < len(music.level) else score.get('level', '')
        title = music.title
        level_label = _LEVEL_LABELS[min(level_index, 4)]
    else:
        ds = float(str(score.get('level', '0')).replace('+', '.5'))
        level_str = score.get('level', '')
        title = score.get('song_name', '')
        level_label = score.get('level', '')

    fc = (score.get('fc') or '').lower()
    fs = (score.get('fs') or '').lower()

    # rate 统一由 computeRa 计算，保证与渲染所需格式一致（如 Sp/SSp/SSSp）
    ra, rate = computeRa(ds, achievements, israte=True)

    return dict(
        song_id=song_id,
        level=level_str,
        level_index=level_index,
        ds=ds,
        ra=ra,
        rate=rate,
        achievements=achievements,
        fc=fc,
        fs=fs,
        dxScore=score.get('dx_score', 0),
        title=title,
        type=song_type,
        level_label=level_label,
    )


def lxns_bests_to_userinfo(bests: dict, nickname: str = '', rating: int = 0) -> UserInfo:
    """将 lxns Best50 响应转换为本地 UserInfo。"""
    sd_raw = [_lxns_score_to_chartinfo(s) for s in (bests.get('standard') or [])]
    dx_raw = [_lxns_score_to_chartinfo(s) for s in (bests.get('dx') or [])]
    sd_list = [ChartInfo(**c) for c in sd_raw if c is not None]
    dx_list = [ChartInfo(**c) for c in dx_raw if c is not None]

    sd_list.sort(key=lambda x: -x.ra)
    dx_list.sort(key=lambda x: -x.ra)

    from .maimaidx_best_50 import regroup_b50_userinfo

    return regroup_b50_userinfo(UserInfo(
        nickname=nickname or '落雪用户',
        rating=rating,
        additional_rating=0,
        username=nickname or '',
        charts=Data(sd=sd_list, dx=dx_list),
    ))


def lxns_scores_to_records(scores: list) -> List[PlayInfoDev]:
    """将 lxns 全量成绩列表转换为 PlayInfoDev 列表。"""
    out: List[PlayInfoDev] = []
    for s in _flatten_lxns_scores(scores):
        c = _lxns_score_to_chartinfo(s)
        if c is not None:
            out.append(PlayInfoDev(**c))
    return out


# ─────────────────────────── 落雪取数（OAuth 优先，dev 兜底） ───────────────────────────


async def _lxns_get_bests_and_player(qqid: int) -> Tuple[Optional[dict], str, int, bool]:
    """
    返回 (bests, nickname, rating, via_oauth)。
    via_oauth 标记是否走了 OAuth（决定能否拿全量成绩）。
    """
    from ..command.mai_lxns import _get_valid_access_token  # 延迟导入避免循环

    nickname, rating = '', 0
    access_token = await _get_valid_access_token(qqid)
    if access_token:
        try:
            with _timing.measure('fetch'):
                bests = await user_get_bests(access_token)
                player = await user_get_player(access_token)
            if player:
                nickname = player.get('name', '')
                rating = player.get('rating', 0)
            return bests, nickname, rating, True
        except Exception as e:
            log.warning(f'[datasource] lxns OAuth bests failed qq={qqid}: {e}')

    # dev 兜底（按 QQ）
    from ..config import maiconfig
    if not maiconfig.lxns_dev_token:
        return None, nickname, rating, False
    with _timing.measure('fetch'):
        player_info = await dev_get_player_by_qq(qqid)
        if not player_info:
            return None, nickname, rating, False
        fc = player_info.get('friend_code')
        nickname = player_info.get('name', '')
        rating = player_info.get('rating', 0)
        bests = await dev_get_bests(fc) if fc else None
    return bests, nickname, rating, False


# ─────────────────────────── 统一对外接口 ───────────────────────────


async def get_user_b50(
    qqid: Optional[int] = None,
    username: Optional[str] = None,
    *,
    force_source: Optional[str] = None,
    force_refresh: bool = False,
) -> UserInfo:
    """
    获取用户 b50（UserInfo）。根据数据源偏好选择水鱼/落雪。
    username 查询强制走水鱼（落雪无此接口）。

    Raises:
        LxnsDataError: 落雪数据获取失败
        以及水鱼的 UserNotFoundError 等
    """
    source = force_source or (get_user_source(qqid) if qqid and not username else 'divingfish')
    clear_fetch_meta()

    if source == 'awmcnet' and qqid and not username:
        async def _fetch_awmcnet_b50():
            userinfo, _ = await _get_awmcnet_records(
                qqid, force_refresh=force_refresh
            )
            return userinfo

        return await resolve_player_b50(
            qqid, username, source, _fetch_awmcnet_b50,
            force_refresh=force_refresh,
        )

    if source == 'lxns' and qqid and not username:

        async def _fetch_lxns_b50():
            bests, nickname, rating, _ = await _lxns_get_bests_and_player(qqid)
            if not bests:
                raise LxnsDataError(
                    '落雪数据获取失败，请先绑定落雪查分器：发送 lxbind\n'
                    '或切换回水鱼数据源：数据源 水鱼'
                )
            return lxns_bests_to_userinfo(bests, nickname=nickname, rating=rating)

        result = await resolve_player_b50(
            qqid, username, source, _fetch_lxns_b50, force_refresh=force_refresh
        )
        return result

    async def _fetch_df_b50():
        return await maiApi.query_user_b50(qqid=qqid, username=username)

    result = await resolve_player_b50(
        qqid, username, source, _fetch_df_b50, force_refresh=force_refresh
    )
    return result


async def get_user_b50_or_fallback(
    qqid: Optional[int] = None,
    username: Optional[str] = None,
) -> UserInfo:
    """
    获取用户 b50，落雪失败时自动降级到水鱼（不抛异常）。
    用于合作 b50 等需要"尽量不要因为一方失败而整体失败"的场景。
    """
    try:
        return await get_user_b50(qqid=qqid, username=username)
    except LxnsDataError:
        log.warning(f'[datasource] lxns fallback to divingfish for qq={qqid}')
        return await maiApi.query_user_b50(qqid=qqid, username=username)


async def get_user_records(
    qqid: Optional[int] = None,
    username: Optional[str] = None,
    *,
    force_source: Optional[str] = None,
    force_refresh: bool = False,
) -> Tuple[UserInfo, List[PlayInfoDev]]:
    """
    获取用户基础信息 + 全量成绩。根据数据源偏好选择水鱼/落雪。
    落雪全量成绩需 OAuth 授权。username 查询强制走水鱼。

    Returns:
        (userinfo, records)
    Raises:
        LxnsDataError: 落雪数据获取失败 / 未授权
        以及水鱼的 UserNotFoundError 等
    """
    source = force_source or (get_user_source(qqid) if qqid and not username else 'divingfish')
    clear_fetch_meta()

    if source == 'awmcnet' and qqid and not username:
        async def _fetch_awmcnet_records():
            return await _get_awmcnet_records(qqid, force_refresh=force_refresh)

        return await resolve_player_records(
            qqid, username, source, _fetch_awmcnet_records,
            force_refresh=force_refresh,
        )

    if source == 'lxns' and qqid and not username:

        async def _fetch_lxns_records():
            from ..command.mai_lxns import _get_valid_access_token
            access_token = await _get_valid_access_token(qqid)
            if not access_token:
                raise LxnsDataError(
                    '落雪全量成绩需要 OAuth 授权，请先发送 lxbind 绑定落雪查分器\n'
                    '或切换回水鱼数据源：数据源 水鱼'
                )
            try:
                with _timing.measure('fetch'):
                    scores = await user_get_scores(access_token)
                    player = await user_get_player(access_token)
            except Exception as e:
                log.warning(f'[datasource] lxns OAuth scores failed qq={qqid}: {e}')
                raise LxnsDataError(f'落雪成绩获取失败：{e}')
            nickname = player.get('name', '') if player else ''
            rating = player.get('rating', 0) if player else 0
            userinfo = UserInfo(
                nickname=nickname or '落雪用户',
                rating=rating,
                additional_rating=0,
                username=nickname or '',
                charts=None,
            )
            records = lxns_scores_to_records(scores)
            return userinfo, records

        result = await resolve_player_records(
            qqid, username, source, _fetch_lxns_records, force_refresh=force_refresh
        )
        return result

    # 水鱼
    if username:
        qqid = None

    async def _fetch_df_records():
        partial = get_cached_player(qqid, username, source, force_refresh=force_refresh)
        if partial and partial.records and partial.userinfo.charts is not None:
            return partial.userinfo, partial.records
        if partial and partial.records:
            userinfo = await maiApi.query_user_b50(qqid=qqid, username=username)
            return userinfo, partial.records
        if partial and partial.userinfo.charts is not None:
            dev = await maiApi.query_user_get_dev(qqid=qqid, username=username)
            records = list(dev.records or [])
            save_cached_player(qqid, username, source, partial.userinfo, records)
            return partial.userinfo, records
        userinfo = await maiApi.query_user_b50(qqid=qqid, username=username)
        dev = await maiApi.query_user_get_dev(qqid=qqid, username=username)
        records = list(dev.records or [])
        return userinfo, records

    result = await resolve_player_records(
        qqid, username, source, _fetch_df_records, force_refresh=force_refresh
    )
    return result
