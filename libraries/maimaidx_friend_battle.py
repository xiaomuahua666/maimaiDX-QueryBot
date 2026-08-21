"""
友人对战：从发起者 B50 随机一首，在水平接近的群友中随机匹配对手并比该谱成绩。

- 水平过滤：避免「高水平碾压 / 碾压低水平」。以双方总 rating 均值为档给出最大 |Δrating|；
  均分 ≥16000 → ±50，≥15000 → ±100，再低按阶梯放宽。可选指令数字在不超过该档前提下进一步收紧。
- 对手池：优先同群中「已开启数据存储」且符合条件者；若无人则退化为同群其它符合条件者。
- 对手成绩：优先 SQLite 玩家缓存 / 数据存储快照（默认 7 天内有效，重启不丢）；
  本地无数据时才经 datasource 拉取并写回缓存（同局内内存缓存；网络请求小并发）。
  未游玩该谱则不计入可匹配池。
"""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Union

from nonebot.adapters.onebot.v11 import Bot

from ..config import log, maiconfig
from .maimaidx_datasource import get_user_b50, get_user_records, get_user_source
from .maimaidx_player_cache import (
    get_cached_b50_for_friend_battle,
    get_cached_player_for_friend_battle,
    is_cached_user_group_eligible,
)
from .maimaidx_data_storage import ScoreRecord, data_storage
from .maimaidx_error import (
    UserDisabledQueryError,
    UserNotFoundError,
    UserNotExistsError,
)
from .maimaidx_group_rating import (
    _display_name,
    _get_group_member_list,
    build_forward_node,
)
from .maimaidx_timing import measure
from .maimaidx_model import ChartInfo, PlayInfoDev, UserInfoDev
from .maimaidx_score_filter import is_user_group_eligible
from .maimaidx_score_formatter import get_difficulty_name
from .maimaidx_friend_battle_class import (
    TIER_INDEX,
    format_class_line,
    format_rank_brief,
    get_class_state,
    get_win_streak,
    list_battle_users,
    settle_battle_cp_with_extras,
    tier_name,
)

FRIEND_BATTLE_BATCH_MAX = 20
# 单局友人对战：群 rating 补拉上限、对手全量成绩网络探测上限（其余仅本地库）
_FRIEND_BATTLE_GROUP_B50_NET_MAX = 12
_FRIEND_BATTLE_OPP_NET_MAX = 6

_FATAL_BATTLE_ERRORS = (
    "友人对战在本地无缓存",
    "未找到你的 B50",
    "你的 B50 为空",
    "获取群成员失败",
    "群成员列表为空",
    "群内没有满足",
)


def parse_friend_battle_args(text: str) -> Tuple[int, Optional[int]]:
    """
    解析友人对战参数：1～20 为连战场数，50～800 为 rating 差收紧上限。
    例：友人对战 10 → 10 连；友人对战 300 → 单局+±300；友人对战 10 300 → 十连且每局 ±300。
    """
    rounds = 1
    rating_cap: Optional[int] = None
    for part in (text or "").split():
        try:
            n = int(part)
        except (ValueError, TypeError):
            continue
        if 50 <= n <= 800:
            rating_cap = n
        elif 1 <= n <= FRIEND_BATTLE_BATCH_MAX:
            rounds = n
    return rounds, rating_cap


def _is_fatal_friend_battle_error(msg: str) -> bool:
    return any(p in msg for p in _FATAL_BATTLE_ERRORS)

_last_friend_battle: Dict[int, float] = {}


def friend_battle_cooldown_seconds() -> int:
    return max(0, int(getattr(maiconfig, "maimaidx_friend_battle_cooldown_seconds", 180) or 0))


def check_friend_battle_cooldown(qq: int) -> Optional[str]:
    """若仍在冷却内则返回提示文案，否则返回 None。"""
    cd = friend_battle_cooldown_seconds()
    if cd <= 0:
        return None
    last = _last_friend_battle.get(qq)
    if last is None:
        return None
    remain = cd - (time.time() - last)
    if remain <= 0:
        return None
    if remain >= 60:
        m, s = int(remain // 60), int(remain % 60)
        hint = f"{m} 分 {s} 秒" if s else f"{m} 分钟"
    else:
        hint = f"{max(1, int(remain))} 秒"
    return f"友人对战冷却中，请 {hint} 后再试。"


def mark_friend_battle_used(qq: int) -> None:
    if friend_battle_cooldown_seconds() <= 0:
        return
    _last_friend_battle[qq] = time.time()


@dataclass
class FriendBattleGroupContext:
    """一局/连战共享的群成员与 rating 快照，避免重复拉群与全群 B50。"""

    members_by_id: Dict[int, dict]
    rating_rows: List[Tuple[int, str, int]]
    ra_by_uid: Dict[int, int]
    name_by_uid: Dict[int, str]


@dataclass
class FriendBattleOutcome:
    """友人对战结算数据（供图片渲染）。"""

    verdict: str
    winner_side: str  # me | opp | tie
    used_pool: str
    rating_delta: int
    rating_limit: int
    my_rating: int
    opp_rating: int
    opp_name: str
    my_qq: int
    opp_qq: int
    my_name: str
    rel_zh: str
    title: str
    level: str
    diff_name: str
    music_id: int
    level_index: int
    my_achv: float
    my_dx: int
    o_achv: float
    o_dx: int
    cp_lines: List[str] = field(default_factory=list)


@dataclass
class FriendBattleRoundSummary:
    """连战中单局摘要（供连战结果图）。"""

    round_no: int
    title: str
    diff_name: str
    level: str
    opp_name: str
    winner_side: str  # me | opp | tie
    my_achv: float
    o_achv: float


@dataclass
class FriendBattleBatchOutcome:
    """友人对战连战汇总（供连战结果图）。"""

    my_qq: int
    my_name: str
    rounds: List[FriendBattleRoundSummary]
    tier_start_idx: int
    tier_start_cp: int
    tier_end_idx: int
    tier_end_cp: int
    wins: int
    losses: int
    ties: int
    requested: int
    completed: int
    skipped: int
    rating_cap: Optional[int] = None
    end_streak: int = 0


def _outcome_to_round_summary(outcome: FriendBattleOutcome, round_no: int) -> FriendBattleRoundSummary:
    return FriendBattleRoundSummary(
        round_no=round_no,
        title=outcome.title,
        diff_name=outcome.diff_name,
        level=outcome.level,
        opp_name=outcome.opp_name,
        winner_side=outcome.winner_side,
        my_achv=outcome.my_achv,
        o_achv=outcome.o_achv,
    )


async def run_friend_battle_batch(
    bot: Bot,
    group_id: int,
    challenger_qq: int,
    rounds: int,
    user_rating_cap: Optional[int] = None,
) -> Union[str, FriendBattleBatchOutcome]:
    """连续进行 rounds 场友人对战（2～FRIEND_BATTLE_BATCH_MAX），跳过可重试的失败局。"""
    rounds = max(2, min(FRIEND_BATTLE_BATCH_MAX, int(rounds)))
    tier_start_idx, tier_start_cp = get_class_state(challenger_qq)
    summaries: List[FriendBattleRoundSummary] = []
    wins = losses = ties = skipped = 0
    max_attempts = rounds * 3 + 5
    attempts = 0
    my_name = ""

    ctx, ctx_err = await _build_friend_battle_group_context(bot, group_id)
    if ctx_err:
        return ctx_err
    while len(summaries) < rounds and attempts < max_attempts:
        attempts += 1
        result = await run_friend_battle(
            bot,
            group_id,
            challenger_qq,
            user_rating_cap=user_rating_cap,
            group_ctx=ctx,
        )
        if isinstance(result, str):
            if _is_fatal_friend_battle_error(result):
                if not summaries:
                    return result
                break
            skipped += 1
            continue
        if not my_name:
            my_name = result.my_name or "你"
        if result.winner_side == "me":
            wins += 1
        elif result.winner_side == "opp":
            losses += 1
        else:
            ties += 1
        summaries.append(_outcome_to_round_summary(result, len(summaries) + 1))

    if not summaries:
        return "连战未能完成任何一局，请稍后重试或换群友/曲目条件。"

    tier_end_idx, tier_end_cp = get_class_state(challenger_qq)
    return FriendBattleBatchOutcome(
        my_qq=challenger_qq,
        my_name=my_name or "你",
        rounds=summaries,
        tier_start_idx=tier_start_idx,
        tier_start_cp=tier_start_cp,
        tier_end_idx=tier_end_idx,
        tier_end_cp=tier_end_cp,
        wins=wins,
        losses=losses,
        ties=ties,
        requested=rounds,
        completed=len(summaries),
        skipped=skipped,
        rating_cap=user_rating_cap,
        end_streak=get_win_streak(challenger_qq),
    )


def _b50_pool(user_charts) -> List[ChartInfo]:
    if not user_charts:
        return []
    b35 = list(user_charts.sd or [])
    b15 = list(user_charts.dx or [])
    return b35 + b15


def _tier_limit_for_avg(avg_rating: int) -> int:
    """
    按双方总 rating 均值分档给出本局允许的最大 |Δrating|（对称，防互相碾压）。

    高分段单独收紧：均分 ≥16000 → ±50，≥15000 → ±100；其下仍按原阶梯略宽。
    """
    a = int(avg_rating)
    if a >= 16000:
        return 50
    if a >= 15000:
        return 100
    if a < 10000:
        return 520
    if a < 11500:
        return 480
    if a < 13000:
        return 420
    if a < 14500:
        return 360
    return 300


def _pair_rating_limit(my_ra: int, opp_ra: int, user_cap: Optional[int]) -> int:
    """单对组合下的有效差限：分档上限，并可被指令数字额外收紧。"""
    tier = _tier_limit_for_avg((int(my_ra) + int(opp_ra)) // 2)
    if user_cap is None:
        return tier
    cap = max(50, min(800, int(user_cap)))
    return min(tier, cap)


def _in_rating_band(ra: int, my_ra: int, user_cap: Optional[int]) -> bool:
    lim = _pair_rating_limit(my_ra, ra, user_cap)
    return abs(int(ra) - int(my_ra)) <= lim


def _weighted_pick_opponent(
    cands: List[Tuple[int, str, PlayInfoDev]],
    my_rating: int,
    ra_by_uid: dict,
    user_cap: Optional[int],
) -> Tuple[int, str, PlayInfoDev]:
    """在已通过谱面校验的候选中，按与发起者 rating 接近度加权随机。"""
    if len(cands) == 1:
        return cands[0]
    ref = _tier_limit_for_avg(int(my_rating))
    scale = max(40.0, float(ref) / 2.5)
    weights: List[float] = []
    for uid, _name, _rec in cands:
        o_ra = int(ra_by_uid.get(uid, 0))
        d = abs(o_ra - int(my_rating))
        w = 1.0 / (1.0 + (d / scale) ** 2)
        weights.append(w)
    total = sum(weights)
    if total <= 0:
        return random.choice(cands)
    r = random.random() * total
    acc = 0.0
    for i, w in enumerate(weights):
        acc += w
        if r <= acc:
            return cands[i]
    return cands[-1]


def _merge_snapshot_records(records: List[ScoreRecord]) -> dict[Tuple[int, int], ScoreRecord]:
    """同一 (song_id, level_index) 保留一条（更高达成）。"""
    best: dict[Tuple[int, int], ScoreRecord] = {}
    for r in records:
        k = (int(r.song_id), int(r.level_index))
        o = best.get(k)
        if o is None or float(r.achievements) > float(o.achievements):
            best[k] = r
    return best


def _score_record_to_playinfo_dev(rec: ScoreRecord, music_id: int) -> PlayInfoDev:
    lv = rec.level or ""
    return PlayInfoDev(
        song_id=int(music_id),
        title=rec.title or "",
        level=lv,
        level_label=lv,
        level_index=int(rec.level_index),
        achievements=float(rec.achievements),
        fc=rec.fc or "",
        fs=rec.fs or "",
        type="SD",
        ds=round(float(rec.ds), 1),
        dxScore=int(rec.dxScore or 0),
        ra=int(rec.ra),
        rate=rec.rate or "",
    )


def _play_from_record_list(
    records: List[PlayInfoDev], music_id: int, level_index: int
) -> Optional[PlayInfoDev]:
    mid = int(music_id)
    li = int(level_index)
    for r in records:
        if int(getattr(r, "song_id", 0)) == mid and int(getattr(r, "level_index", -1)) == li:
            return r
    return None


def _try_local_records_play(qqid: int, music_id: int, level_index: int) -> Optional[PlayInfoDev]:
    """SQLite 玩家缓存 / 数据存储快照（长 TTL，不占网络）。"""
    if not is_cached_user_group_eligible(qqid):
        return None
    bundle = get_cached_player_for_friend_battle(qqid)
    if bundle and bundle.records:
        hit = _play_from_record_list(bundle.records, music_id, level_index)
        if hit is not None:
            return hit
    mid = int(music_id)
    li = int(level_index)
    metas = data_storage.list_snapshots(qqid, limit=1)
    if not metas:
        return None
    sid = metas[0].get("snapshot_id", "")
    if not sid:
        return None
    snap = data_storage.load_snapshot_by_id(qqid, sid)
    if not snap or not snap.records:
        return None
    merged = _merge_snapshot_records(list(snap.records))
    rec = merged.get((mid, li))
    if rec is None:
        return None
    return _score_record_to_playinfo_dev(rec, mid)


def _play_from_dev_cache(
    qqid: int, music_id: int, level_index: int, dev_cache: dict[int, Optional[UserInfoDev]]
) -> Optional[PlayInfoDev]:
    """假定 dev_cache 已加载该 qq（或已标记为 None 失败）。"""
    mid = int(music_id)
    li = int(level_index)
    dev = dev_cache.get(qqid)
    if dev is None or not dev.records:
        return None
    for r in dev.records:
        if int(getattr(r, "song_id", 0)) == mid and int(getattr(r, "level_index", -1)) == li:
            return r
    return None


def _bundle_to_userinfo_dev(bundle) -> UserInfoDev:
    return UserInfoDev(
        nickname=bundle.userinfo.nickname,
        rating=bundle.userinfo.rating,
        additional_rating=bundle.userinfo.additional_rating or 0,
        username=bundle.userinfo.username,
        records=bundle.records,
    )


async def _ensure_dev_cached(qqid: int, dev_cache: dict[int, Optional[UserInfoDev]], sem: asyncio.Semaphore) -> None:
    """加载对手全量成绩：先长 TTL 本地库，没有再经 datasource 拉取并写入 SQLite。"""
    if qqid in dev_cache:
        return
    bundle = get_cached_player_for_friend_battle(qqid)
    if bundle and bundle.records:
        dev_cache[qqid] = (
            _bundle_to_userinfo_dev(bundle)
            if is_user_group_eligible(bundle.userinfo, bundle.records)
            else None
        )
        return
    async with sem:
        if qqid in dev_cache:
            return
        bundle = get_cached_player_for_friend_battle(qqid)
        if bundle and bundle.records:
            dev_cache[qqid] = (
                _bundle_to_userinfo_dev(bundle)
                if is_user_group_eligible(bundle.userinfo, bundle.records)
                else None
            )
            return
        try:
            source = get_user_source(qqid)
            _ui, records = await get_user_records(qqid=qqid, force_source=source)
            if records and is_user_group_eligible(_ui, records):
                dev_cache[qqid] = UserInfoDev(
                    nickname=_ui.nickname,
                    rating=_ui.rating,
                    additional_rating=_ui.additional_rating or 0,
                    username=_ui.username,
                    records=records,
                )
            else:
                dev_cache[qqid] = None
        except (UserNotFoundError, UserNotExistsError, UserDisabledQueryError, ValueError, TypeError):
            dev_cache[qqid] = None
        except Exception as e:
            log.warning(f"[friend_battle] fetch records qq={qqid}: {e!r}")
            dev_cache[qqid] = None


async def _fetch_song_on_level(
    qqid: int,
    music_id: int,
    level_index: int,
    dev_cache: dict[int, Optional[UserInfoDev]],
    sem: asyncio.Semaphore,
    *,
    allow_network: bool,
) -> Optional[PlayInfoDev]:
    """对手该谱成绩：本地库优先；allow_network=False 时不拉全量。"""
    local = _try_local_records_play(qqid, music_id, level_index)
    if local is not None:
        return local
    if not allow_network:
        return None
    await _ensure_dev_cached(qqid, dev_cache, sem)
    return _play_from_dev_cache(qqid, music_id, level_index, dev_cache)


async def _get_group_ratings_for_friend_battle(
    bot: Bot,
    group_id: int,
    members: List[dict],
) -> List[Tuple[int, str, int]]:
    """群友 rating：复用群内 rating 汇总（本地优先，网络补拉有上限）。"""
    from .maimaidx_group_rating import get_group_member_ratings

    return await get_group_member_ratings(
        bot,
        group_id,
        net_fetch_limit=_FRIEND_BATTLE_GROUP_B50_NET_MAX,
        shuffle_net=True,
        require_token_for_net=True,
    )


async def _build_friend_battle_group_context(
    bot: Bot,
    group_id: int,
) -> Tuple[Optional[FriendBattleGroupContext], Optional[str]]:
    try:
        with measure('fetch'):
            raw = await _get_group_member_list(bot, group_id)
    except Exception as e:
        return None, f"获取群成员失败：{e}"
    if not raw or not isinstance(raw, list):
        return None, "群成员列表为空。"

    members_by_id = {int(m.get("user_id")): m for m in raw if m.get("user_id") is not None}
    rating_rows = await _get_group_ratings_for_friend_battle(bot, group_id, raw)
    ctx = FriendBattleGroupContext(
        members_by_id=members_by_id,
        rating_rows=rating_rows,
        ra_by_uid={uid: ra for uid, _name, ra in rating_rows},
        name_by_uid={uid: n for uid, n, _ in rating_rows},
    )
    return ctx, None


async def _valid_from_uids_for_song(
    uids: List[int],
    music_id: int,
    level_index: int,
    name_of,
    *,
    allow_network: bool,
) -> List[Tuple[int, str, PlayInfoDev]]:
    """先同步扫本地库，仅对未命中者做小批量网络补拉。"""
    if not uids:
        return []
    shuffled = list(uids)
    random.shuffle(shuffled)

    cands: List[Tuple[int, str, PlayInfoDev]] = []
    need_net: List[int] = []
    for uid in shuffled:
        hit = _try_local_records_play(uid, music_id, level_index)
        if hit is not None:
            cands.append((uid, name_of(uid), hit))
        else:
            need_net.append(uid)

    if not need_net or not allow_network:
        return cands

    dev_cache: dict[int, Optional[UserInfoDev]] = {}
    dev_sem = asyncio.Semaphore(3)
    net_budget = _FRIEND_BATTLE_OPP_NET_MAX

    async def _try_net(uid: int) -> Optional[Tuple[int, str, PlayInfoDev]]:
        r = await _fetch_song_on_level(
            uid, music_id, level_index, dev_cache, dev_sem, allow_network=True
        )
        if r is None:
            return None
        return (uid, name_of(uid), r)

    for uid in need_net[:net_budget]:
        one = await _try_net(uid)
        if one is not None:
            cands.append(one)
    return cands


async def run_friend_battle(
    bot: Bot,
    group_id: int,
    challenger_qq: int,
    user_rating_cap: Optional[int] = None,
    *,
    group_ctx: Optional[FriendBattleGroupContext] = None,
) -> Union[str, FriendBattleOutcome]:
    has_token = bool(getattr(maiconfig, "maimaidxtoken", None))

    me = get_cached_b50_for_friend_battle(challenger_qq)
    if me is None:
        if not has_token:
            return (
                "未找到你的本地 B50 缓存。\n"
                "请先发送 b50 生成缓存，或开启「存储数据」；"
                "管理员也可配置 maimaidxtoken 以在无缓存时从查分器拉取。"
            )
        try:
            me = await get_user_b50(qqid=challenger_qq)
        except (UserNotFoundError, UserNotExistsError, UserDisabledQueryError) as e:
            return str(e)

    if not me.charts:
        return "未找到你的 B50 数据，请先绑定查分器并发送 b50。"

    pool = _b50_pool(me.charts)
    if not pool:
        return "你的 B50 为空，无法随机曲目。"

    pick = random.choice(pool)
    music_id = int(pick.song_id)
    level_index = int(pick.level_index)
    title = (pick.title or "").strip() or f"ID{music_id}"
    level = pick.level or ""
    my_achv = float(pick.achievements)
    my_dx = int(getattr(pick, "dxScore", 0) or 0)
    my_rating = int(me.rating or 0)
    diff_name = get_difficulty_name(level_index)
    ref_tier = _tier_limit_for_avg(my_rating)

    if group_ctx is None:
        group_ctx, ctx_err = await _build_friend_battle_group_context(bot, group_id)
        if ctx_err:
            return ctx_err
    assert group_ctx is not None

    members_by_id = group_ctx.members_by_id
    member_ids = set(members_by_id.keys())
    rating_rows = group_ctx.rating_rows
    ra_by_uid = group_ctx.ra_by_uid
    name_by_uid = group_ctx.name_by_uid

    def name_of(uid: int) -> str:
        if uid in name_by_uid:
            return name_by_uid[uid]
        if uid in members_by_id:
            return _display_name(members_by_id[uid])
        return str(uid)

    band_uids: List[int] = []
    for uid, _n, ra in rating_rows:
        if uid == challenger_qq:
            continue
        if uid not in member_ids:
            continue
        if not _in_rating_band(ra, my_rating, user_rating_cap):
            continue
        band_uids.append(uid)

    if not band_uids:
        cap_desc = (
            f"且额外收紧至 ±{user_rating_cap}"
            if user_rating_cap is not None
            else "（按分档动态差限）"
        )
        return (
            "群内没有满足「总 rating 水平接近」且本地/可查 rating 的群友，无法匹配。\n"
            f"你当前总 rating：{my_rating}；单人档位参考允许约 ±{ref_tier}{cap_desc}。\n"
            "可让群友先发 b50 或「开启存储数据」积累本地库，或发「友人对战 300」放宽差限。"
        )

    storage_in_group = {u for u in data_storage.get_enabled_users() if u in member_ids and u != challenger_qq}
    prefer = [u for u in band_uids if u in storage_in_group]
    others = [u for u in band_uids if u not in storage_in_group]

    allow_opp_net = has_token

    used_pool = "同群同水平(仅本地库)" if not allow_opp_net else "同群同水平(优先本地库)"
    cands = await _valid_from_uids_for_song(
        prefer, music_id, level_index, name_of, allow_network=allow_opp_net
    )
    if not cands and others:
        cands = await _valid_from_uids_for_song(
            others, music_id, level_index, name_of, allow_network=allow_opp_net
        )

    if not cands:
        return (
            f"已随机到：「{title}」{diff_name}（{level}）\n"
            "在水平接近的群友中，没有人有该谱成绩。"
            "请重发「友人对战」换一首随机的歌。"
        )

    ouid, oname, orec = _weighted_pick_opponent(cands, my_rating, ra_by_uid, user_rating_cap)
    o_achv = float(orec.achievements)
    o_dx = int(getattr(orec, "dxScore", 0) or 0)
    o_rating = int(ra_by_uid.get(ouid, 0))

    ci0, _ = get_class_state(challenger_qq)
    oi0, _ = get_class_state(ouid)
    if oi0 > ci0:
        rel_zh = "对手段位更高（相对你为格上）"
    elif oi0 < ci0:
        rel_zh = "对手段位更低（相对你为格下）"
    else:
        rel_zh = "双方同段"

    if my_achv > o_achv:
        verdict = "你赢了"
    elif my_achv < o_achv:
        verdict = f"{oname} 赢了"
    else:
        if my_dx > o_dx:
            verdict = "你赢了"
        elif my_dx < o_dx:
            verdict = f"{oname} 赢了"
        else:
            verdict = "平手"

    lim_used = _pair_rating_limit(my_rating, o_rating, user_rating_cap)
    if verdict == "你赢了":
        winner_side = "me"
    elif verdict == "平手":
        winner_side = "tie"
    else:
        winner_side = "opp"

    if verdict == "平手":
        cp_lines = ["── 段位·CP ──", "本局平手，双方段位 CP 不变。"]
    else:
        cp_lines = settle_battle_cp_with_extras(
            challenger_qq,
            ouid,
            verdict == "你赢了",
            my_rating,
            o_rating,
            my_achv,
            o_achv,
            my_dx,
            o_dx,
        ).split("\n")
    cp_lines.append(format_class_line(challenger_qq))

    return FriendBattleOutcome(
        verdict=verdict,
        winner_side=winner_side,
        used_pool=used_pool,
        rating_delta=abs(o_rating - my_rating),
        rating_limit=lim_used,
        my_rating=my_rating,
        opp_rating=o_rating,
        opp_name=oname,
        my_qq=challenger_qq,
        opp_qq=ouid,
        my_name=name_of(challenger_qq),
        rel_zh=rel_zh,
        title=title,
        level=level,
        diff_name=diff_name,
        music_id=music_id,
        level_index=level_index,
        my_achv=my_achv,
        my_dx=my_dx,
        o_achv=o_achv,
        o_dx=o_dx,
        cp_lines=cp_lines,
    )


_LEGEND_TIER_IDX = TIER_INDEX.get("LEGEND", len(TIER_INDEX) - 1)


async def group_friend_battle_ranking(
    bot: Bot,
    group_id: int,
    self_id: int,
    bot_nickname: str,
    current_qq: int,
    top_n: int = 15,
) -> Tuple[str, List[dict]]:
    """
    本群友人对战段位排行：仅统计已参与过友人对战的群成员，按段位→CP→连胜降序。
    返回: (标题文案, 合并转发 nodes)。
    """
    top_n = max(1, min(50, int(top_n)))
    battle_users = list_battle_users()
    if not battle_users:
        return "暂无友人对战段位数据，先发「友人对战」打几局吧。", []

    try:
        raw = await _get_group_member_list(bot, group_id)
    except Exception as e:
        log.warning(f"[group_friend_battle_ranking] get_group_member_list failed: {e}")
        return "获取群成员列表失败。", []
    if not raw or not isinstance(raw, list):
        return "群成员列表为空。", []

    member_by_id = {int(m.get("user_id")): m for m in raw if m.get("user_id") is not None}
    rows: List[Tuple[int, str, int, int, int]] = []
    for uid, data in battle_users.items():
        if uid not in member_by_id:
            continue
        if not is_cached_user_group_eligible(uid):
            continue
        tier_name_s = data.get("tier", "B5")
        tier_idx = TIER_INDEX.get(tier_name_s, 0)
        cp = int(data.get("cp", 0))
        streak = max(0, int(data.get("fb_win_streak", 0)))
        name = _display_name(member_by_id[uid])
        rows.append((uid, name, tier_idx, cp, streak))

    if not rows:
        return "本群暂无友人对战记录，先发「友人对战」打几局吧。", []

    rows.sort(key=lambda x: (x[2], x[3], x[4]), reverse=True)
    total = len(rows)
    legend_count = sum(1 for _uid, _n, ti, _cp, _s in rows if ti >= _LEGEND_TIER_IDX)

    user_rank = None
    user_brief = ""
    for i, (uid, _name, tier_idx, cp, streak) in enumerate(rows):
        if uid == current_qq:
            user_rank = i + 1
            user_brief = format_rank_brief(tier_idx, cp, streak)
            break

    take = rows[:top_n]
    rank_lines: List[str] = []
    if user_rank is not None:
        rank_lines.append(f"你在本群排名第 {user_rank}/{total} 名（{user_brief}）")
    else:
        rank_lines.append("你尚未参与友人对战，暂无排名")

    text = (
        f"本群友人对战段位排行（前 {len(take)} 名）\n"
        f"共 {total} 人参战，其中 LEGEND {legend_count} 人\n"
        + "\n".join(rank_lines)
    )

    nodes = []
    for i, (uid, name, tier_idx, cp, streak) in enumerate(take):
        mark = "▶" if uid == current_qq else ""
        brief = format_rank_brief(tier_idx, cp, streak)
        line = f"{mark}{i + 1}. {name}  {brief}"
        node_name = "你" if uid == current_qq else name
        nodes.append(build_forward_node(str(self_id), str(node_name), line))
    return text, nodes
