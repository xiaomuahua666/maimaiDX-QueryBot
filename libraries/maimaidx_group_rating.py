"""
群内 rating 相关：获取群成员列表、批量查询 rating、排名与合并转发文案。

- 我在群里有多菜：群内排名 + 上下 5 位的合并转发
- 看看<rating>有多少人：群内 rating >= 指定值的人数与列表（合并转发）
- 看看群rating排名<参数>：群内 rating 倒序前 N 名（合并转发，默认 10 ）
群成员 rating 优先读本地玩家缓存 / 数据存储快照，仅对无本地数据的成员并发拉查分器并写回用户级缓存；
每次请求都会重新获取群成员列表，群人数变化时新成员会自动纳入、退群成员不再计入。
群成员 rating 网络补拉使用并发请求 + 信号量限流，减少总耗时。

- 我的 <歌曲> 排名：群内指定歌曲成绩排名（支持难度筛选）
- <歌曲> 排名：群内指定歌曲成绩排行榜（支持难度筛选）
支持按群号+歌曲ID+难度缓存成绩列表，时长同样由 maimaidx_rating_cache_seconds 控制。
- 群吃分榜、群寸止/锁血榜：对「本群拉全量统计后的原始行」按群号（及吃分榜的天数）缓存，TTL 与上表一致。
"""
import asyncio
import math
import random
import time
from typing import TYPE_CHECKING, List, Optional, Tuple

if TYPE_CHECKING:
    from nonebot.adapters.onebot.v11 import MessageSegment

from nonebot.adapters.onebot.v11 import Bot

from ..config import achievementList
from ..config import maiconfig
from ..config import log
from .maimaidx_data_storage import data_storage
from .maimaidx_api_data import maiApi
from .maimaidx_datasource import get_user_b50, get_user_records, get_user_source
from .maimaidx_player_cache import get_cached_rating_for_friend_battle
from .maimaidx_best_50 import filter_utage_records
from .maimaidx_error import UserDisabledQueryError, UserNotFoundError, UserNotExistsError, MusicNotPlayError
from .maimaidx_score_formatter import (
    format_leaderboard_text,
    format_my_rank_text,
    format_rank_gaps,
    format_score_line_from_dict,
    get_difficulty_name,
)
from .maimaidx_platform import is_likely_qq_group_id
from .maimaidx_qq_bind import qq_bind_db
from .maimaidx_qq_member_registry import qq_member_registry

# 群内歌曲成绩缓存：key = (group_id, music_id, level_index), value = (rows, expiry_timestamp)
_group_song_score_cache: dict = {}

# 群吃分榜：key = (group_id, days)，value = (rows, expiry)；rows 为 (uid, name, old_r, new_r, delta)，已按 delta 降序
_group_gain_rank_cache: dict = {}

# 群寸止/锁血榜：key = group_id，value = (rows, expiry)；rows 为 (uid, name, sun_c, lock_c)，未按榜别排序
_group_sun_lock_raw_cache: dict = {}


def _get_cache_ttl() -> int:
    """获取缓存时长（秒），默认 900（15 分钟）"""
    return getattr(maiconfig, 'maimaidx_rating_cache_seconds', 900) or 900


# 请求者 B1/B36 参照：key = qqid，value = (ref_dict, expiry)
_self_b1b36_cache: dict = {}


def _self_b1b36_cache_get(qqid: int):
    now = time.time()
    item = _self_b1b36_cache.get(qqid)
    if item and now < item[1]:
        return item[0]
    _self_b1b36_cache.pop(qqid, None)
    return None


def _self_b1b36_cache_set(qqid: int, ref, ttl_seconds: int):
    if ttl_seconds > 0 and ref:
        _self_b1b36_cache[qqid] = (ref, time.time() + ttl_seconds)


async def fetch_self_b1b36(qqid: int) -> Optional[dict]:
    """Best-effort 获取请求者 B50 的 B1（B35首位）/ B36（B15首位），带缓存，失败返回 None。"""
    if not qqid:
        return None
    cached = _self_b1b36_cache_get(qqid)
    if cached is not None:
        return cached
    try:
        # get_user_b50 直接返回已排序的 B35(sd)/B15(dx)，比拉全量成绩更轻
        userinfo = await asyncio.wait_for(get_user_b50(qqid=int(qqid)), timeout=8)
        ref = _b1b36_from_userinfo(userinfo)
        if not ref:
            return None
        _self_b1b36_cache_set(qqid, ref, _get_cache_ttl())
        return ref
    except Exception as e:
        log.debug(f'[group_rating] fetch b1/b36 for {qqid} failed: {e}')
        return None


def _b1b36_from_userinfo(userinfo) -> Optional[dict]:
    """从 UserInfo.charts 抽取 B1(sd[0])/B36(dx[0]) 的精简 dict，无数据返回 None。"""
    charts = getattr(userinfo, 'charts', None)
    b1_rec = (charts.sd[0] if charts and getattr(charts, 'sd', None) else None)
    b36_rec = (charts.dx[0] if charts and getattr(charts, 'dx', None) else None)

    def _ref(rec):
        if rec is None:
            return None
        return {
            'song_id': int(rec.song_id), 'title': rec.title,
            'level': rec.level, 'level_index': int(rec.level_index),
            'ds': float(getattr(rec, 'ds', 0) or 0),
            'ra': int(rec.ra), 'achievements': float(rec.achievements),
        }

    ref = {'b1': _ref(b1_rec), 'b36': _ref(b36_rec)}
    if not ref['b1'] and not ref['b36']:
        return None
    return ref


def collect_window_b1b36(rows: List[Tuple[int, str, int]], self_qq: int,
                         half: int = 5) -> dict:
    """仅从本地 B50 缓存读取窗口内各用户的 B1/B36，不发起网络请求。"""
    try:
        self_idx = next((i for i, r in enumerate(rows) if r[0] == self_qq), None)
        if self_idx is None:
            return {}
        start = max(0, self_idx - half)
        end = min(len(rows), self_idx + half + 1)
        from .maimaidx_player_cache import get_cached_b50_for_friend_battle
        result = {}
        for uid, _, _ in rows[start:end]:
            userinfo = get_cached_b50_for_friend_battle(int(uid))
            if userinfo is None:
                continue
            ref = _b1b36_from_userinfo(userinfo)
            if ref:
                result[int(uid)] = ref
        return result
    except Exception as e:
        log.debug(f'[group_rating] collect window b1/b36 failed: {e}')
        return {}


def collect_rows_b1b36(display_rows) -> dict:
    """仅从本地 B50 缓存读取给定显示行各用户的 B1/B36，不发起网络请求。"""
    result = {}
    try:
        from .maimaidx_player_cache import get_cached_b50_for_friend_battle
        for row in display_rows:
            uid = int(row[0])
            userinfo = get_cached_b50_for_friend_battle(uid)
            if userinfo is None:
                continue
            ref = _b1b36_from_userinfo(userinfo)
            if ref:
                result[uid] = ref
    except Exception as e:
        log.debug(f'[group_rating] collect rows b1/b36 failed: {e}')
    return result


def _group_song_score_cache_get(group_id: int, music_id: str, level_index: int):
    """获取歌曲成绩缓存"""
    now = time.time()
    cache_key = (group_id, music_id, level_index)
    if cache_key in _group_song_score_cache:
        val, expiry = _group_song_score_cache[cache_key]
        if now < expiry:
            return val
        del _group_song_score_cache[cache_key]
    return None


def _group_song_score_cache_set(group_id: int, music_id: str, level_index: int, rows: List[Tuple[int, str, dict]], ttl_seconds: int):
    """设置歌曲成绩缓存"""
    if ttl_seconds <= 0:
        return
    cache_key = (group_id, music_id, level_index)
    _group_song_score_cache[cache_key] = (rows, time.time() + ttl_seconds)


def _group_gain_cache_get(key: Tuple[int, int]):
    now = time.time()
    if key in _group_gain_rank_cache:
        val, expiry = _group_gain_rank_cache[key]
        if now < expiry:
            return val
        del _group_gain_rank_cache[key]
    return None


def _group_gain_cache_set(key: Tuple[int, int], rows: List[Tuple[int, str, int, int, int]], ttl_seconds: int):
    if ttl_seconds <= 0:
        return
    _group_gain_rank_cache[key] = (rows, time.time() + ttl_seconds)


def _group_sun_lock_raw_cache_get(group_id: int):
    now = time.time()
    if group_id in _group_sun_lock_raw_cache:
        val, expiry = _group_sun_lock_raw_cache[group_id]
        if now < expiry:
            return val
        del _group_sun_lock_raw_cache[group_id]
    return None


def _group_sun_lock_raw_cache_set(group_id: int, rows: List[Tuple[int, str, int, int]], ttl_seconds: int):
    if ttl_seconds <= 0:
        return
    _group_sun_lock_raw_cache[group_id] = (rows, time.time() + ttl_seconds)


def _display_name(member: dict) -> str:
    """优先群名片，其次昵称。"""
    card = (member.get("card") or "").strip()
    if card:
        return card
    return (member.get("nickname") or str(member.get("user_id", ""))).strip() or "未知"


# 群成员 rating 并发数上限。AWMCNET 单机后端需要为正常查分保留余量。
_GROUP_RATING_CONCURRENCY = 5


def _forum_or_qbind_qq(platform_id: str) -> Optional[int]:
    """Resolve an official QQ member openid to the real query QQ number."""
    qq = qq_bind_db.get_legacy_qq(str(platform_id))
    if qq is not None:
        return qq
    binding = qq_bind_db.get_forum_binding(str(platform_id))
    if binding and binding.get('legacy_qq') is not None:
        try:
            return int(binding['legacy_qq'])
        except (TypeError, ValueError):
            pass
    return None


async def _get_group_member_list(bot: Bot, group_id) -> List[dict]:
    """Fetch members for OneBot, or bound/seen members for official QQ."""
    if is_likely_qq_group_id(group_id):
        members: List[dict] = []
        for row in qq_member_registry.list_group(str(group_id), limit=200):
            platform_id = str(row.get('member_id') or '').strip()
            qq = _forum_or_qbind_qq(platform_id)
            if qq is None:
                continue
            binding = qq_bind_db.get_forum_binding(platform_id) or {}
            nickname = str(binding.get('username') or f'QQ{qq}')
            members.append({'user_id': qq, 'nickname': nickname, 'card': ''})
        return members
    try:
        raw = await bot.call_api("get_group_member_list", group_id=group_id)
    except Exception:
        return []
    return raw if isinstance(raw, list) else []


async def get_group_member_ratings(
    bot: Bot,
    group_id: int,
    *,
    net_fetch_limit: Optional[int] = None,
    shuffle_net: bool = False,
    require_token_for_net: bool = False,
) -> List[Tuple[int, str, int]]:
    """
    获取群成员列表并汇总 rating（绑定查分器且可查的才计入）。
    优先读本地玩家缓存 / 数据存储快照；无本地数据的成员再并发拉查分器（写回用户级缓存）。
    返回: [(user_id, display_name, rating), ...]，按 rating 降序。

    net_fetch_limit: 网络补拉人数上限（None=不限制）；shuffle_net 为 True 时先打乱再截取。
    require_token_for_net: 为 True 时未配置开发者 TOKEN 则跳过网络补拉。
    """
    raw = await _get_group_member_list(bot, group_id)
    if not raw or not isinstance(raw, list):
        return []

    rows: List[Tuple[int, str, int]] = []
    need_net: List[dict] = []
    for m in raw:
        uid = m.get("user_id")
        if uid is None:
            continue
        name = _display_name(m)
        qq = int(uid)
        ra = get_cached_rating_for_friend_battle(qq)
        if ra is not None and ra > 0:
            rows.append((qq, name, ra))
        else:
            need_net.append(m)

    if need_net:
        if require_token_for_net and not bool(getattr(maiconfig, "maimaidxtoken", None)):
            need_net = []
        if shuffle_net:
            random.shuffle(need_net)
        if net_fetch_limit is not None and net_fetch_limit > 0:
            need_net = need_net[:net_fetch_limit]

        sem = asyncio.Semaphore(_GROUP_RATING_CONCURRENCY)

        async def _fetch_one(m: dict) -> Optional[Tuple[int, str, int]]:
            uid = m.get("user_id")
            if uid is None:
                return None
            async with sem:
                try:
                    # 群排名是批量读取，不应为每个陌生群成员触发水鱼/落雪
                    # 自动迁移。AWMCNET 用户只读取轻量摘要；个人主动查分仍
                    # 继续使用 datasource 的完整自动迁移流程。
                    if get_user_source(int(uid)) == 'awmcnet':
                        from .maimaidx_awmcnet_sync import fetch_awmcnet_summary

                        summary = await fetch_awmcnet_summary(int(uid))
                        if summary:
                            ra = int(summary.get('rating') or 0)
                            if ra > 0:
                                return (int(uid), _display_name(m), ra)
                    userinfo = await get_user_b50(
                        qqid=int(uid),
                        access_mode='shared',
                    )
                    ra = int(userinfo.rating or 0)
                    if ra <= 0:
                        return None
                    return (int(uid), _display_name(m), ra)
                except (UserNotFoundError, UserNotExistsError, UserDisabledQueryError, ValueError, TypeError):
                    return None
                except Exception:
                    return None

        gathered = await asyncio.gather(*[_fetch_one(m) for m in need_net])
        rows.extend(r for r in gathered if r is not None)

    rows.sort(key=lambda x: x[2], reverse=True)
    return rows


def build_forward_node(user_id: str, nickname: str, content: str) -> dict:
    """
    OneBot v11 合并转发单条 node。
    go-cqhttp 要求使用 name + uin。所有 value 强制为 str，避免 partial 等导致 JSON 序列化失败。
    """
    uid = str(user_id)
    name_s = str(nickname)
    content_s = str(content)
    return {
        "type": "node",
        "data": {
            "name": name_s,
            "uin": uid,
            "content": content_s,
        },
    }


async def group_weak_rank(
    bot: Bot,
    group_id: int,
    self_id: int,
    bot_nickname: str,
    current_qq: int,
) -> Tuple[str, List[dict]]:
    """
    我在群里有多菜：计算当前用户在群内的排名，并生成「上下 5 位」的合并转发 node 列表。
    返回: (回复文案, forward_nodes)。
    """
    rows = await get_group_member_ratings(bot, group_id)
    if not rows:
        return "群内暂无已绑定查分器的成员，无法排名。", []

    total = len(rows)
    rank = None
    for i, (uid, name, ra) in enumerate(rows):
        if uid == current_qq:
            rank = i + 1
            break
    if rank is None:
        return "你尚未绑定查分器或未同意协议，无法参与群内排名。", []

    exceeded = total - rank  # 严格比你低的人数
    others = total - 1
    if others <= 0:
        percent = 100.0
    else:
        percent = (exceeded / others) * 100.0
    text = f"你的 rating 在群里的排名为 {rank}/{total}，超过了 {percent:.1f}% 的群友。"

    # 合并转发：展示当前用户上下各 5 位，前后不足 5 位时按实际人数展示
    half = 5
    start = max(0, rank - 1 - half)
    end = min(total, rank - 1 + half + 1)
    slice_rows = rows[start:end]
    nodes = []
    for i, (uid, name, ra) in enumerate(slice_rows):
        actual_rank = start + i + 1
        if uid == current_qq:
            line = f"▶{actual_rank}. 你 {ra}"
            node_name = "你"
        else:
            line = f"{actual_rank}. {name} {ra}"
            node_name = name
        nodes.append(build_forward_node(str(self_id), str(node_name), line))
    return text, nodes


def _achievement_is_sun(a: float) -> bool:
    for x in achievementList:
        if (x - 0.1) < a <= x:
            return True
    return False


def _achievement_is_lock(a: float) -> bool:
    for x in achievementList:
        step = 0.01 if x != math.floor(x) else 0.1
        if x <= a < (x + step):
            return True
    return False


def _count_sun_lock_on_records(records) -> Tuple[int, int]:
    sun, lock = 0, 0
    for r in records:
        try:
            achv = float(getattr(r, "achievements", 0) or 0)
        except (TypeError, ValueError):
            continue
        if _achievement_is_sun(achv):
            sun += 1
        if _achievement_is_lock(achv):
            lock += 1
    return sun, lock


async def group_gain_ranking(
    bot: Bot,
    group_id: int,
    self_id: int,
    bot_nickname: str,
    days: int = 7,
    top_n: int = 15,
) -> Tuple[str, List[dict]]:
    """
    群吃分榜：仅统计本群成员中「已开启数据存储」且在最近 days 天内有至少 2 次存档的用户，
    按 rating 增量降序。
    """
    days = max(1, min(90, int(days)))
    top_n = max(1, min(50, int(top_n)))
    ttl = _get_cache_ttl()
    cache_key = (group_id, days)
    cached_rows = _group_gain_cache_get(cache_key)
    if cached_rows is not None:
        rows = list(cached_rows)
    else:
        try:
            raw = await _get_group_member_list(bot, group_id)
        except Exception as e:
            log.warning(f"[group_gain_ranking] get_group_member_list failed: {e}")
            return "获取群成员列表失败。", []
        if not raw or not isinstance(raw, list):
            return "群成员列表为空。", []

        member_by_id = {int(m.get("user_id")): m for m in raw if m.get("user_id") is not None}
        member_ids = set(member_by_id.keys())
        rows = []

        for uid in data_storage.get_enabled_users():
            if uid not in member_ids:
                continue
            delta_t = data_storage.rating_delta_in_period(uid, days)
            if delta_t is None:
                continue
            old_r, new_r, delta = delta_t
            name = _display_name(member_by_id[uid])
            rows.append((uid, name, old_r, new_r, delta))

        rows.sort(key=lambda x: x[4], reverse=True)
        _group_gain_cache_set(cache_key, rows, ttl)
    eligible = len(rows)
    if not rows:
        return (
            f"近{days}天暂无可用吃分数据。\n"
            "（需本群成员发送「开启存储数据」且该周期内至少有 2 次存档）",
            [],
        )

    take = rows[:top_n]
    text = (
        f"群吃分榜（近{days}天 · rating 增量 · 前{len(take)}名）\n"
        f"符合条件共 {eligible} 人；仅统计已开启数据存储（开启存储数据）且有足够存档的成员。"
    )
    nodes = []
    for i, (uid, name, old_r, new_r, delta) in enumerate(take):
        line = f"{i + 1}. {name}  {old_r} → {new_r}  ({delta:+d})"
        nodes.append(build_forward_node(str(self_id), str(name), line))
    return text, nodes


async def group_sun_lock_ranking(
    bot: Bot,
    group_id: int,
    self_id: int,
    bot_nickname: str,
    mode: str,
    top_n: int = 15,
) -> Tuple[str, List[dict]]:
    """
    群寸止榜 / 群锁血榜：对本群已绑定查分器的成员拉取全量成绩，统计达成落在寸止/锁血区间的谱面条数，降序排列。
    mode: 'sun' | 'lock'
    """
    mode = (mode or "sun").lower()
    if mode not in ("sun", "lock"):
        mode = "sun"
    top_n = max(1, min(50, int(top_n)))
    ttl = _get_cache_ttl()

    rows = _group_sun_lock_raw_cache_get(group_id)
    if rows is None:
        try:
            raw = await _get_group_member_list(bot, group_id)
        except Exception as e:
            log.warning(f"[group_sun_lock_ranking] get_group_member_list failed: {e}")
            return "获取群成员列表失败。", []
        if not raw or not isinstance(raw, list):
            return "群成员列表为空。", []

        sem = asyncio.Semaphore(_GROUP_RATING_CONCURRENCY)

        async def _fetch_one(m: dict) -> Optional[Tuple[int, str, int, int]]:
            uid = m.get("user_id")
            if uid is None:
                return None
            async with sem:
                try:
                    _ui, recs = await get_user_records(
                        qqid=int(uid),
                        access_mode='shared',
                    )
                    recs = list(recs or [])
                    from .maimaidx_best_50 import filter_utage_records
                    recs = filter_utage_records(recs)
                except (UserNotFoundError, UserNotExistsError, UserDisabledQueryError, ValueError, TypeError):
                    return None
                except Exception:
                    return None
                if not recs:
                    return None
                sun_c, lock_c = _count_sun_lock_on_records(recs)
                name = _display_name(m)
                return (int(uid), name, sun_c, lock_c)

        tasks = [_fetch_one(m) for m in raw]
        gathered = await asyncio.gather(*tasks)
        rows = [r for r in gathered if r is not None]
        _group_sun_lock_raw_cache_set(group_id, rows, ttl)

    if not rows:
        return "群内暂无可用全量成绩（需绑定查分器且 Bot 有开发者 Token）。", []

    if mode == "sun":
        rows.sort(key=lambda x: (x[2], x[3]), reverse=True)
        label = "寸止"
    else:
        rows.sort(key=lambda x: (x[3], x[2]), reverse=True)
        label = "锁血"

    take = rows[:top_n]
    text = (
        f"群{label}榜（全量成绩中落在{label}区间的谱面条数 · 前{len(take)}名）\n"
        "与单曲「寸b50/锁血b50」筛选规则一致（按评级门槛区间统计）。"
    )
    nodes = []
    for i, (uid, name, sun_c, lock_c) in enumerate(take):
        cnt = sun_c if mode == "sun" else lock_c
        other = lock_c if mode == "sun" else sun_c
        other_label = "锁血" if mode == "sun" else "寸止"
        line = f"{i + 1}. {name}  {label}{cnt}条  ({other_label}{other}条)"
        nodes.append(build_forward_node(str(self_id), str(name), line))
    return text, nodes


# 群内歌曲成绩相关：获取群成员列表、批量查询指定歌曲成绩、排名与合并转发文案。
_GROUP_SONG_SCORE_CONCURRENCY = 15


def _song_score_info(record, level_index: int) -> dict:
    """Build the renderer payload without dropping the score rank badge."""
    return {
        'achievements': record.achievements,
        'fc': record.fc,
        'fs': record.fs,
        'dxScore': getattr(record, 'dxScore', 0),
        'rate': getattr(record, 'rate', '') or '',
        'level': record.level,
        'level_index': getattr(record, 'level_index', level_index),
    }


async def get_group_member_song_scores(
    bot: Bot,
    group_id: int,
    music_id: str,
    level_index: int = 3,
) -> List[Tuple[int, str, dict]]:
    """
    获取群成员列表并并发查询指定歌曲的成绩（绑定查分器且可查的才计入），信号量限流。
    返回: [(user_id, display_name, score_info), ...]，按达成率降序。
    score_info: {'achievements': float, 'fc': str, 'fs': str, 'dxScore': int, 'rate': str, 'level': str, 'level_index': int}

    Params:
        level_index: 难度索引 0=Basic, 1=Advanced, 2=Expert, 3=Master, 4=Re:Master，默认为 3
    """
    log.debug(f"[get_group_member_song_scores] 开始: group_id={group_id}, music_id={music_id}, level_index={level_index}")
    ttl = _get_cache_ttl()

    # 尝试从缓存获取
    cached = _group_song_score_cache_get(group_id, music_id, level_index)
    if cached is not None:
        log.debug(f"[get_group_member_song_scores] 命中缓存，返回 {len(cached)} 条记录")
        return cached

    try:
        raw = await _get_group_member_list(bot, group_id)
        log.debug(f"[get_group_member_song_scores] 获取群成员列表: {len(raw) if raw else 0} 人")
    except Exception as e:
        log.warning(f"[get_group_member_song_scores] 获取群成员列表失败: {e}")
        return []
    if not raw or not isinstance(raw, list):
        log.debug("[get_group_member_song_scores] 群成员列表为空")
        return []

    sem = asyncio.Semaphore(_GROUP_SONG_SCORE_CONCURRENCY)

    async def _fetch_one(m: dict) -> Optional[Tuple[int, str, dict]]:
        uid = m.get("user_id")
        if uid is None:
            return None
        async with sem:
            try:
                _userinfo, records = await get_user_records(
                    qqid=int(uid),
                    access_mode='shared',
                )
                song_records = [
                    r for r in records
                    if str(r.song_id) == str(music_id)
                    and getattr(r, 'level_index', 3) == level_index
                ]
                if not song_records:
                    return None
                best = max(song_records, key=lambda x: x.achievements)

                name = _display_name(m)
                score_info = _song_score_info(best, level_index)
                log.debug(f"[get_group_member_song_scores] 用户 {uid} ({name}) 成绩: {score_info}")
                return (int(uid), name, score_info)
            except (UserNotFoundError, UserNotExistsError, UserDisabledQueryError, ValueError, TypeError, MusicNotPlayError):
                return None
            except Exception as e:
                log.debug(f"[get_group_member_song_scores] 用户 {uid} 查询失败: {type(e).__name__}: {e}")
                return None

    tasks = [_fetch_one(m) for m in raw]
    gathered = await asyncio.gather(*tasks)
    result = [r for r in gathered if r is not None]
    # 按达成率降序
    result.sort(key=lambda x: x[2]['achievements'], reverse=True)
    log.debug(f"[get_group_member_song_scores] 查询完成: {len(result)}/{len(raw)} 人有成绩")

    # 设置缓存
    _group_song_score_cache_set(group_id, music_id, level_index, result, ttl)

    return result


async def group_song_my_rank(
    bot: Bot,
    group_id: int,
    self_id: int,
    bot_nickname: str,
    current_qq: int,
    music_id: str,
    music_title: str,
    level_index: int = 3,
) -> Tuple[str, List[dict]]:
    """
    我在本群这首歌的排名：计算当前用户在群内指定歌曲的排名，并生成「上下 5 位」的合并转发 node 列表。
    返回: (回复文案, forward_nodes)。

    Params:
        level_index: 难度索引 0=Basic, 1=Advanced, 2=Expert, 3=Master, 4=Re:Master，默认为 3
    """
    log.debug(f"[group_song_my_rank] 开始: group_id={group_id}, music_id={music_id}, level_index={level_index}, current_qq={current_qq}")
    rows = await get_group_member_song_scores(bot, group_id, music_id, level_index)
    diff_name = get_difficulty_name(level_index)

    if not rows:
        log.debug("[group_song_my_rank] 无成绩数据")
        return f"群内暂无已绑定查分器的成员游玩过「{music_title}」的{diff_name}难度。", []

    total = len(rows)
    rank = None
    for i, (uid, _, _) in enumerate(rows):
        if uid == current_qq:
            rank = i + 1
            break

    log.debug(f"[group_song_my_rank] 总记录数={total}, 当前用户排名={rank}")

    if rank is None:
        return f"你尚未游玩过「{music_title}」的{diff_name}难度或未绑定查分器。", []

    # 使用 score_formatter 格式化文本和差距
    gaps = format_rank_gaps(rows, rank)
    text = format_my_rank_text(music_title, diff_name, rank, total, gaps)

    # 合并转发：展示当前用户上下各 5 位
    half = 5
    start = max(0, rank - 1 - half)
    end = min(total, rank - 1 + half + 1)
    slice_rows = rows[start:end]
    nodes = []
    for i, (uid, name, score_info) in enumerate(slice_rows):
        actual_rank = start + i + 1
        is_self = (uid == current_qq)
        line = format_score_line_from_dict(actual_rank, name, score_info, is_self)
        node_name = "你" if is_self else name
        nodes.append(build_forward_node(str(self_id), str(node_name), line))

    log.debug(f"[group_song_my_rank] 返回: text='{text[:50]}...', nodes={len(nodes)}")
    return text, nodes


async def group_song_leaderboard(
    bot: Bot,
    group_id: int,
    self_id: int,
    bot_nickname: str,
    music_id: str,
    music_title: str,
    top_n: int = 10,
    level_index: int = 3,
) -> Tuple[str, List[dict]]:
    """
    本群指定歌曲成绩排行榜：群内指定歌曲成绩倒序前 top_n 名，合并转发展示。
    返回: (回复文案, forward_nodes)。

    Params:
        level_index: 难度索引 0=Basic, 1=Advanced, 2=Expert, 3=Master, 4=Re:Master，默认为 3
    """
    log.debug(f"[group_song_leaderboard] 开始: group_id={group_id}, music_id={music_id}, level_index={level_index}, top_n={top_n}")
    rows = await get_group_member_song_scores(bot, group_id, music_id, level_index)
    diff_name = get_difficulty_name(level_index)

    if not rows:
        log.debug("[group_song_leaderboard] 无成绩数据")
        return f"群内暂无已绑定查分器的成员游玩过「{music_title}」的{diff_name}难度。", []

    take = rows[:top_n]
    text = f"本群「{music_title}」{diff_name}难度成绩排名（前 {len(take)} 名）："
    nodes = []
    for i, (uid, name, score_info) in enumerate(take):
        line = format_score_line_from_dict(i + 1, name, score_info)
        nodes.append(build_forward_node(str(self_id), str(name), line))

    log.debug(f"[group_song_leaderboard] 返回: text='{text[:50]}...', nodes={len(nodes)}")
    return text, nodes


async def group_rating_count_above(
    bot: Bot,
    group_id: int,
    self_id: int,
    bot_nickname: str,
    min_rating: int,
) -> Tuple[str, List[dict]]:
    """
    看看<rating>有多少人：群内 rating >= min_rating 的人数与列表（合并转发，倒序）。
    返回: (回复文案, forward_nodes)。
    """
    rows = await get_group_member_ratings(bot, group_id)
    above = [(uid, name, ra) for uid, name, ra in rows if ra >= min_rating]
    above.sort(key=lambda x: x[2], reverse=True)
    count = len(above)
    text = f"群内 rating ≥ {min_rating} 的有 {count} 人。"
    nodes = []
    for i, (uid, name, ra) in enumerate(above):
        line = f"{i + 1}. {name} {ra}"
        nodes.append(build_forward_node(str(self_id), str(name), line))
    return text, nodes


async def group_rating_ranking(
    bot: Bot,
    group_id: int,
    self_id: int,
    bot_nickname: str,
    top_n: int = 10,
) -> Tuple[str, List[dict]]:
    """
    看看群rating排名<参数>：群内 rating 倒序前 top_n 名，合并转发展示。
    返回: (回复文案, forward_nodes)。
    """
    rows = await get_group_member_ratings(bot, group_id)
    if not rows:
        return "群内暂无已绑定查分器的成员。", []
    take = rows[:top_n]
    text = f"群内 rating 排名（前 {len(take)} 名）："
    nodes = []
    for i, (uid, name, ra) in enumerate(take):
        line = f"{i + 1}. {name} {ra}"
        nodes.append(build_forward_node(str(self_id), str(name), line))
    return text, nodes


# ---------------------------------------------------------------------------
# 现代化图片渲染入口：返回 MessageSegment 图片，失败/空数据时返回文字提示。
# ---------------------------------------------------------------------------
def _image_segment(bio) -> "MessageSegment":
    from nonebot.adapters.onebot.v11 import MessageSegment
    from .image import image_to_base64
    data = bio.getvalue()
    return MessageSegment.image('base64://' + __import__('base64').b64encode(data).decode())


async def render_group_rating_board(bot, group_id: int, top_n: int = 10,
                                    self_qq: Optional[int] = None,
                                    user_name: str = 'Milk'):
    b1b36_task = (
        asyncio.create_task(fetch_self_b1b36(self_qq))
        if self_qq is not None else None
    )
    rows = await get_group_member_ratings(bot, group_id)
    if not rows:
        if b1b36_task:
            b1b36_task.cancel()
        return '群内暂无已绑定查分器的成员。'
    take = rows[:max(1, min(50, int(top_n or 10)))]
    self_rank = None
    if self_qq is not None:
        for i, (uid, _, _) in enumerate(rows):
            if uid == self_qq:
                self_rank = i + 1
                break
    b1b36 = await b1b36_task if b1b36_task else None
    row_b1b36 = collect_rows_b1b36(take)
    if b1b36 and self_qq is not None:
        row_b1b36[int(self_qq)] = b1b36
    from .maimaidx_leaderboard_image import render_rating_ranking
    bio = await render_rating_ranking(
        take, title='群 Rating 排行',
        subtitle=f'共 {len(rows)} 人 · 显示前 {len(take)} 名',
        self_qq=self_qq, self_rank=self_rank, all_rows=rows, user_name=user_name,
        b1b36=b1b36, row_b1b36=row_b1b36,
    )
    return _image_segment(bio)


async def render_group_weak_board(bot, group_id: int, self_qq: int,
                                 half: int = 5, user_name: str = 'Milk'):
    """我在群里有多菜：以请求用户为中心，渲染其前后各 half 位的排名上下文图。"""
    rows = await get_group_member_ratings(bot, group_id)
    if not rows:
        return '群内暂无已绑定查分器的成员，无法排名。'
    if self_qq is None or not any(uid == self_qq for uid, _, _ in rows):
        return '你尚未绑定查分器或未同意协议，无法参与群内排名。'
    b1b36 = await fetch_self_b1b36(self_qq)
    row_b1b36 = collect_window_b1b36(rows, self_qq, half=half)
    if b1b36:
        row_b1b36[int(self_qq)] = b1b36
    from .maimaidx_leaderboard_image import render_my_rank_context
    bio = await render_my_rank_context(rows, self_qq=self_qq, half=half,
                                      user_name=user_name, b1b36=b1b36,
                                      row_b1b36=row_b1b36)
    if bio is None:
        return '未在群内排名中找到你，请先绑定查分器。'
    return _image_segment(bio)


async def render_group_gain_board(bot, group_id: int, days: int = 7,
                                  top_n: int = 15, self_qq: Optional[int] = None,
                                  user_name: str = 'Milk'):
    days = max(1, min(90, int(days)))
    top_n = max(1, min(50, int(top_n)))
    cache_key = (group_id, days)
    rows = _group_gain_cache_get(cache_key)
    if rows is None:
        try:
            raw = await _get_group_member_list(bot, group_id)
        except Exception as e:
            log.warning(f"[group_gain] get_group_member_list failed: {e}")
            return '获取群成员列表失败。'
        if not raw:
            return '群成员列表为空。'
        member_by_id = {int(m.get("user_id")): m for m in raw if m.get("user_id") is not None}
        rows = []
        for uid in data_storage.get_enabled_users():
            if uid not in member_by_id:
                continue
            delta_t = data_storage.rating_delta_in_period(uid, days)
            if delta_t is None:
                continue
            old_r, new_r, delta = delta_t
            rows.append((uid, _display_name(member_by_id[uid]), old_r, new_r, delta))
        rows.sort(key=lambda x: x[4], reverse=True)
        _group_gain_cache_set(cache_key, rows, _get_cache_ttl())
    if not rows:
        return (f'近{days}天暂无可用吃分数据。\n'
                '（需本群成员发送「开启存储数据」且该周期内至少有 2 次存档）')
    from .maimaidx_leaderboard_image import render_gain_ranking
    bio = await render_gain_ranking(
        rows[:top_n], title='群吃分榜',
        subtitle=f'近 {days} 天 · rating 增量 · 共 {len(rows)} 人',
        self_qq=self_qq, user_name=user_name,
    )
    return _image_segment(bio)


async def render_group_sun_lock_board(bot, group_id: int, mode: str = 'sun',
                                      top_n: int = 15, self_qq: Optional[int] = None,
                                      user_name: str = 'Milk'):
    mode = (mode or 'sun').lower()
    if mode not in ('sun', 'lock'):
        mode = 'sun'
    top_n = max(1, min(50, int(top_n)))
    rows = _group_sun_lock_raw_cache_get(group_id)
    if rows is None:
        try:
            raw = await _get_group_member_list(bot, group_id)
        except Exception as e:
            log.warning(f"[group_sun_lock] get_group_member_list failed: {e}")
            return '获取群成员列表失败。'
        if not raw:
            return '群成员列表为空。'
        sem = asyncio.Semaphore(_GROUP_RATING_CONCURRENCY)

        async def _fetch_one(m: dict):
            uid = m.get("user_id")
            if uid is None:
                return None
            async with sem:
                try:
                    _ui, recs = await get_user_records(
                        qqid=int(uid),
                        access_mode='shared',
                    )
                    recs = list(recs or [])
                    from .maimaidx_best_50 import filter_utage_records
                    recs = filter_utage_records(recs)
                except Exception:
                    return None
                if not recs:
                    return None
                sun_c, lock_c = _count_sun_lock_on_records(recs)
                return (int(uid), _display_name(m), sun_c, lock_c)

        gathered = await asyncio.gather(*[_fetch_one(m) for m in raw])
        rows = [r for r in gathered if r is not None]
        _group_sun_lock_raw_cache_set(group_id, rows, _get_cache_ttl())
    if not rows:
        return '群内暂无可用全量成绩（需绑定查分器且 Bot 有开发者 Token）。'
    if mode == 'sun':
        rows = sorted(rows, key=lambda x: (x[2], x[3]), reverse=True)
        label = '寸止'
    else:
        rows = sorted(rows, key=lambda x: (x[3], x[2]), reverse=True)
        label = '锁血'
    from .maimaidx_leaderboard_image import render_sun_lock_ranking
    bio = await render_sun_lock_ranking(
        rows[:top_n], title=f'群{label}榜',
        subtitle=f'全量成绩中落在{label}区间的谱面条数 · 共 {len(rows)} 人',
        mode=mode, self_qq=self_qq, user_name=user_name,
    )
    return _image_segment(bio)


async def render_group_song_board(bot, group_id: int, music_id: str,
                                  music_title: str, level_index: int = 3,
                                  top_n: int = 10, self_qq: Optional[int] = None,
                                  cover_path: Optional[str] = None,
                                  user_name: str = 'Milk'):
    rows = await get_group_member_song_scores(bot, group_id, music_id, level_index)
    if not rows:
        diff_name = get_difficulty_name(level_index)
        return f'群内暂无已绑定查分器的成员游玩过「{music_title}」的{diff_name}难度。'
    from .maimaidx_leaderboard_image import render_song_leaderboard
    bio = await render_song_leaderboard(
        rows[:max(1, min(50, int(top_n or 10)))],
        music_title=music_title,
        diff_name=get_difficulty_name(level_index),
        level_index=level_index,
        total_players=len(rows),
        self_qq=self_qq,
        cover_path=cover_path, user_name=user_name,
    )
    return _image_segment(bio)
