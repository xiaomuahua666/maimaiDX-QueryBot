import asyncio
import json
import random
import time
from pathlib import Path
from textwrap import dedent
from typing import Literal, Optional, Union

from loguru import logger as log
from nonebot import on_command, on_message, on_regex
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent, MessageSegment
from nonebot.matcher import Matcher
from nonebot.params import CommandArg, RegexMatched
from nonebot.rule import Rule

from ..config import maiconfig
from ..libraries.maimaidx_bot_admin import GUESS_GROUP_MANAGER, PLUGIN_ADMIN_ONLY
from ..libraries.maimaidx_guess_boost_card import (
    DEFAULT_CARD_HOURS,
    guess_boost_card,
)
from ..libraries.maimaidx_guess_match import match_guess_answer
from ..libraries.maimaidx_guess_rate_limit import consume_guess_answer_slot
from ..libraries.maimaidx_guess_score import guess_score
from ..libraries.maimaidx_guess_rank_draw import image_b64 as rank_image_b64, render_guess_rank_image
from ..libraries.maimaidx_guess_stats_draw import personal_guess_stats_image_b64
from ..libraries.maimaidx_guess_sync import (
    MAIN_GROUP_REDIRECT,
    guess_sync,
)
from ..libraries.maimaidx_pending_session import finish_pending, session_key, track_event
from ..libraries.maimaidx_guess_audio import (
    STAGE_FINAL_GRACE,
    STAGE_INTERVAL,
    STAGE_LABELS,
    build_hot_audio_cache,
    get_audio_manifest_entry,
    get_audio_prepare_status,
    summarize_pool_cache as summarize_audio_pool_cache,
    request_hot_batch_cancel,
)
from ..libraries.maimaidx_guess_chart import (
    COUNTDOWN_MARKS as CHART_COUNTDOWN_MARKS,
    PHASE2_DURATION as CHART_PHASE2_DURATION,
    STAGE_FINAL_GRACE as CHART_STAGE_FINAL_GRACE,
    STAGE_INTERVAL as CHART_STAGE_INTERVAL,
    build_hot_chart_cache,
    get_chart_prepare_status,
    summarize_pool_cache as summarize_chart_pool_cache,
    request_chart_batch_cancel,
)
from ..libraries.maimaidx_render_tasks import format_active_tasks
from ..libraries.maimaidx_game_session import game_session_gate
from ..libraries.maimaidx_image_executor import run_image_cpu
from ..libraries.maimaidx_music import guess
from ..libraries.maimaidx_reaction import REACT_EMOJI_CHECK, react_processing
from ..libraries.maimaidx_model import (
    GuessAudioData,
    GuessChartData,
    GuessData,
    GuessDefaultData,
    GuessPicData,
)
from ..libraries.maimaidx_guess_rating import (
    DEFAULT_DURATION,
    MIN_DURATION,
    RATING_DIFFICULTIES,
    format_reward_text,
    pick_random_candidate,
    rating_guess,
    select_random_charts,
)
from ..libraries.maimaidx_guess_impostor import (
    IMPOSTOR_CARD_COUNT,
    IMPOSTOR_DURATION,
    build_impostor_cards,
    format_impostor_rewards,
    impostor_guess,
)
from ..libraries.maimaidx_guess_duel import (
    DUEL_ROUND_DURATION,
    DUEL_ROUNDS,
    DUEL_ROUND_SCORES,
    build_duel_rounds,
    duel_guess,
)
from ..libraries.maimaidx_guess_duel_image import duel_image_segment
from ..libraries.maimaidx_guess_20q import (
    TWENTYQ_COUNTDOWN,
    TWENTYQ_DURATION,
    TWENTYQ_GUESS_WINDOW,
    TWENTYQ_IDLE_TIMEOUT,
    TWENTYQ_MAX_QUESTIONS,
    twentyq_base_points,
    twentyq_guess,
    _qa_display_info,
)
from ..libraries.maimaidx_music_info import *
from ..libraries.maimaidx_platform import (
    GroupId,
    adapt_guess_outbound,
    billing_user_id,
    build_command_keyboard_message,
    build_mention_message,
    ensure_sender_mention,
    format_forward_nodes_as_text,
    get_event_group_id,
    get_sender_display_name,
    is_at_all_message,
    is_group_message_event,
    local_video_segment,
    parse_at_target_id,
    platform_user_id,
    plugin_finish,
    rank_text_image,
    resolve_event_bot,
    resolve_reply_message,
    send_group_message,
    use_qq_mode,
)
from ..libraries.maimaidx_qq_member_registry import qq_member_registry
from ..libraries.maimaidx_update_plate import *


def _is_group_message(event) -> bool:
    return is_group_message_event(event)


def _has_guess_sync_pending(event) -> bool:
    gid = get_event_group_id(event)
    if gid is None:
        return False
    return guess_sync.get_pending(gid, platform_user_id(event)) is not None


GROUP_MESSAGE = Rule(_is_group_message)

_GUESS_SHORTCUTS = (
    ('猜歌', '猜歌'),
    ('猜曲绘', '猜封面'),
    ('猜曲子', '猜曲子'),
    ('猜谱面', '猜谱面'),
    ('猜 Rating', '猜rating'),
    ('B50 找内鬼', '找内鬼'),
    ('极限二选一', '极限二选一'),
    ('你想我猜', '你想我猜'),
    ('我的猜歌', '我的猜歌'),
    ('本群排行', '本群猜歌排行'),
)


async def _send_guess_shortcuts(
    matcher: Matcher,
    event: MessageEvent,
    gid: GroupId,
) -> None:
    payload = build_command_keyboard_message(
        _GUESS_SHORTCUTS,
        event=event,
        title='🎮 再来一把',
        id_prefix='maimaidx-guess',
    )
    if payload is not None:
        await _safe_matcher_send(
            matcher, event, payload, gid, fatal=False,
        )
GUESS_SYNC_PENDING = Rule(_has_guess_sync_pending)


def is_now_playing_guess_music(event) -> bool:
    gid = get_event_group_id(event)
    return gid is not None and gid in guess.Group


def is_now_playing_guess_rating(event) -> bool:
    gid = get_event_group_id(event)
    return gid is not None and rating_guess.is_busy(gid)


def is_now_playing_guess_impostor(event) -> bool:
    gid = get_event_group_id(event)
    return gid is not None and impostor_guess.is_busy(gid)


def is_now_playing_guess_duel(event) -> bool:
    gid = get_event_group_id(event)
    return gid is not None and duel_guess.is_busy(gid)


def is_now_playing_guess_20q(event) -> bool:
    gid = get_event_group_id(event)
    return gid is not None and twentyq_guess.is_busy(gid)


guess_music_start   = on_command('猜歌', rule=GROUP_MESSAGE)
guess_music_pic     = on_regex(
    r'^(?:猜曲绘|猜封面|猜歌封面|猜曲图|猜歌图|猜曲绘图)\s*([1-4])?\s*$',
    rule=GROUP_MESSAGE,
)
guess_music_audio   = on_command('猜曲子', rule=GROUP_MESSAGE)
guess_music_chart   = on_command('猜铺面', aliases={'猜谱面'}, rule=GROUP_MESSAGE)
update_guess_audio  = on_regex(r'^更新猜曲音频(?:\s+(-full))?\s*$', permission=PLUGIN_ADMIN_ONLY)
update_guess_chart  = on_regex(
    r'^(?:更新|预制)猜(?:铺|谱)面(?:\s+(-full))?(?:\s+(\d+))?\s*$',
    permission=PLUGIN_ADMIN_ONLY,
)
guess_prepare_status = on_command(
    '猜歌预制状态',
    aliases={'猜谱面预制状态', '猜歌预制', '预制状态'},
    permission=PLUGIN_ADMIN_ONLY,
)
render_status = on_command(
    '渲染状态',
    aliases={'查询渲染', '查询渲染任务', '渲染任务', '渲染进度', '预制任务状态'},
    permission=PLUGIN_ADMIN_ONLY,
)
guess_boost_grant   = on_command('发加倍卡', permission=GUESS_GROUP_MANAGER, rule=GROUP_MESSAGE)
guess_boost_query   = on_command('查加倍卡', rule=GROUP_MESSAGE)
guess_music_solve   = on_message(
    rule=is_now_playing_guess_music,
    priority=10,
    block=False,
)
guess_music_reset   = on_command('重置猜歌', priority=4, block=True, rule=GROUP_MESSAGE)
guess_music_enable  = on_command('开启mai猜歌', permission=GUESS_GROUP_MANAGER, rule=GROUP_MESSAGE)
guess_music_disable = on_command('关闭mai猜歌', permission=GUESS_GROUP_MANAGER, rule=GROUP_MESSAGE)
guess_score_rank    = on_command('猜歌积分排行', rule=GROUP_MESSAGE)
guess_score_daily   = on_command('猜歌积分日榜', rule=GROUP_MESSAGE)
guess_score_weekly  = on_command('猜歌积分周榜', rule=GROUP_MESSAGE)
guess_score_monthly = on_command('猜歌积分月榜', rule=GROUP_MESSAGE)
guess_score_yearly  = on_command('猜歌积分年榜', rule=GROUP_MESSAGE)
guess_score_season  = on_command('猜歌积分赛季榜', rule=GROUP_MESSAGE)
guess_score_hist_daily   = on_command('猜歌历史日榜', rule=GROUP_MESSAGE)
guess_score_hist_weekly  = on_command('猜歌历史周榜', rule=GROUP_MESSAGE)
guess_score_hist_monthly = on_command('猜歌历史月榜', rule=GROUP_MESSAGE)
guess_score_hist_yearly  = on_command('猜歌历史年榜', rule=GROUP_MESSAGE)
guess_score_hist_season  = on_command('猜歌历史赛季榜', rule=GROUP_MESSAGE)
guess_my_stats = on_command(
    '我的猜歌',
    aliases={'猜歌数据', '猜歌统计'},
    rule=GROUP_MESSAGE,
    priority=5,
    block=True,
)
guess_group_rank = on_command('本群猜歌排行', rule=GROUP_MESSAGE)
guess_sync_reply = on_message(
    rule=GROUP_MESSAGE & GUESS_SYNC_PENDING,
    priority=1,
    block=True,
)
guess_migrate_data = on_command(
    '迁移数据',
    aliases={'迁移猜歌数据', '同步猜歌数据'},
    rule=GROUP_MESSAGE,
    priority=5,
    block=True,
)

# ── 猜Rating ──
guess_rating_start = on_regex(
    r'^猜(?i:rating)([1-5])?(?:\s+([1-5]))?(?:\s+(\d{2,3}))?\s*$',
    rule=GROUP_MESSAGE,
    priority=5,
    block=True,
)
guess_rating_solve = on_message(
    rule=GROUP_MESSAGE & Rule(is_now_playing_guess_rating),
    priority=10,
    block=False,
)
guess_rating_reset = on_command('重置猜rating', aliases={'重置猜Rating'}, priority=4, block=True, rule=GROUP_MESSAGE)
guess_rating_volunteer = on_regex(
    r'^(?:猜猜我的|猜我的|选我)\s*$',
    rule=GROUP_MESSAGE,
    priority=5,
    block=True,
)

# ── B50 找内鬼 ──
guess_impostor_start = on_regex(
    r'^\s*(?:找内鬼|找假卡)\s*$',
    rule=GROUP_MESSAGE,
    priority=5,
    block=True,
)
guess_impostor_solve = on_message(
    rule=GROUP_MESSAGE & Rule(is_now_playing_guess_impostor),
    priority=10,
    block=False,
)
guess_impostor_reset = on_command(
    '重置找内鬼', aliases={'重置找假卡'},
    priority=4, block=True, rule=GROUP_MESSAGE,
)

# ── 舞萌极限二选一 ──
guess_duel_start = on_regex(
    r'^\s*(?:舞萌极限二选一|极限二选一|二选一)\s*$',
    rule=GROUP_MESSAGE,
    priority=5,
    block=True,
)
guess_duel_join = on_regex(
    r'^\s*(?:加入|参赛)\s*$',
    rule=GROUP_MESSAGE,
    priority=6,
    block=True,
)
guess_duel_solve = on_message(
    rule=GROUP_MESSAGE & Rule(is_now_playing_guess_duel),
    priority=10,
    block=False,
)
guess_duel_reset = on_command(
    '重置二选一', aliases={'重置舞萌极限二选一'},
    priority=4, block=True, rule=GROUP_MESSAGE,
)

# ── 你想我猜（20 问猜曲）──
guess_20q_start = on_command(
    '你想我猜',
    aliases={'你想我问', '20问猜曲', '二十问猜曲', '20问', '你想我猜吗'},
    rule=GROUP_MESSAGE,
    priority=5,
    block=True,
)
guess_20q_solve = on_message(
    rule=GROUP_MESSAGE & Rule(is_now_playing_guess_20q),
    priority=10,
    block=False,
)
guess_20q_reset = on_command(
    '重置你想我猜',
    aliases={'重置20问', '重置二十问'},
    priority=4,
    block=True,
    rule=GROUP_MESSAGE,
)
guess_20q_list = on_command(
    '查看提问列表',
    aliases={'查看已有信息', '提问列表', '20问进度', '我问已有信息'},
    priority=4,
    block=True,
    rule=GROUP_MESSAGE,
)

# 猜歌玩法不参与高峰期「额外 1 BREAK」附加费（含局内答题 on_message）。
for _guess_matcher in (
    guess_music_start,
    guess_music_pic,
    guess_music_audio,
    guess_music_chart,
    update_guess_audio,
    update_guess_chart,
    guess_prepare_status,
    guess_boost_grant,
    guess_boost_query,
    guess_music_solve,
    guess_music_reset,
    guess_music_enable,
    guess_music_disable,
    guess_score_rank,
    guess_score_daily,
    guess_score_weekly,
    guess_score_monthly,
    guess_score_yearly,
    guess_score_season,
    guess_score_hist_daily,
    guess_score_hist_weekly,
    guess_score_hist_monthly,
    guess_score_hist_yearly,
    guess_score_hist_season,
    guess_my_stats,
    guess_sync_reply,
    guess_migrate_data,
    guess_rating_start,
    guess_rating_solve,
    guess_rating_reset,
    guess_rating_volunteer,
    guess_impostor_start,
    guess_impostor_solve,
    guess_impostor_reset,
    guess_duel_start,
    guess_duel_join,
    guess_duel_solve,
    guess_duel_reset,
    guess_20q_start,
    guess_20q_solve,
    guess_20q_reset,
    guess_20q_list,
):
    setattr(_guess_matcher, '_maimaidx_busy_surcharge_exempt', True)


async def _gate_guess_group_entry(matcher: Matcher, event: MessageEvent) -> None:
    """仅主群导流；不在开局/开字母时自动同步，避免异常覆盖。"""
    gid = get_event_group_id(event)
    if gid is None:
        return
    action, message = await guess_sync.prepare_group_entry(
        gid, platform_user_id(event)
    )
    if action == 'redirect':
        await matcher.finish(message or MAIN_GROUP_REDIRECT, reply_message=True)


def _sender_name(event: MessageEvent) -> str:
    return get_sender_display_name(event)


def _guess_first_stage(data: GuessData) -> bool:
    """猜曲子/猜铺面：第二段发出前仍算首阶段。"""
    if isinstance(data, GuessAudioData):
        return data.hint_step < 2
    if isinstance(data, GuessChartData):
        return data.hint_step < 2
    return data.hint_step == 0


def _chart_points_now(data: GuessChartData) -> int:
    now = time.time()
    started = float(getattr(data, 'started_at', 0) or 0)
    elapsed = max(0.0, now - started) if started > 0 else 0.0
    bgm_at = float(getattr(data, 'bgm_at', 0) or 0)
    bgm_elapsed = max(0.0, now - bgm_at) if bgm_at > 0 else 0.0
    return guess_score.chart_points_for(
        data.hint_step,
        elapsed_sec=elapsed,
        bgm_elapsed_sec=bgm_elapsed,
    )


# 基础猜歌四模式 → 双层上限 game_key（与 maimaidx_break 的 guess_daily_caps 对应）
_GUESS_GAME_KEY_BY_MODE = {
    guess_score.MODE_PIC: 'cover',
    guess_score.MODE_AUDIO: 'tune',
    guess_score.MODE_CHART: 'chart',
    guess_score.MODE_SONG: 'song',
}


async def _award_guess_points(
    event: MessageEvent,
    gid: GroupId,
    data: GuessData,
    *,
    first_stage: bool,
    first_guess: bool,
) -> str:
    if isinstance(data, GuessPicData):
        raw_base = guess_score.pic_points_for(data)
        mode = guess_score.MODE_PIC
    elif isinstance(data, GuessAudioData):
        raw_base = guess_score.audio_points_for(data.hint_step)
        mode = guess_score.MODE_AUDIO
    elif isinstance(data, GuessChartData):
        raw_base = _chart_points_now(data)
        mode = guess_score.MODE_CHART
    elif isinstance(data, GuessDefaultData):
        raw_base = guess_score.song_points_for(data.hint_step)
        mode = guess_score.MODE_SONG
    else:
        raw_base = 1
        mode = guess_score.MODE_SONG
    multiplier, multiplier_tags = guess_score.get_score_multiplier(
        first_stage=first_stage,
        first_guess=first_guess,
    )
    if isinstance(data, GuessAudioData) and guess_score.audio_season_double_active():
        multiplier *= 2
        multiplier_tags.append('赛季限时双倍得分')
    if isinstance(data, GuessChartData) and guess_score.chart_season_double_active():
        multiplier *= 2
        multiplier_tags.append('猜铺面限时双倍')
    uid = platform_user_id(event)
    if await guess_boost_card.consume_one(gid, uid):
        multiplier *= 2
        multiplier_tags.append('限时加倍卡×2')
    (
        added, raw_base, combo, streak, total, rank, period_snapshot,
    ) = await guess_score.award_correct_guess(
        gid,
        uid,
        _sender_name(event),
        raw_base,
        multiplier,
        mode=mode,
    )
    settlement = guess_score.format_settlement_lines(
        added, raw_base, combo, multiplier, streak, total, rank, period_snapshot,
        multiplier_tags,
    )
    from ..libraries.maimaidx_break import break_db

    try:
        reward = await asyncio.to_thread(
            break_db.award_guess_points,
            billing_user_id(event), added, group_id=str(gid),
            game=_GUESS_GAME_KEY_BY_MODE.get(mode, 'song'),
        )
    except Exception as exc:
        log.exception(
            f'[Guess] BREAK 奖励结算失败，保留积分与答对回复 '
            f'gid={gid} mode={mode}: {type(exc).__name__}: {exc}'
        )
        return settlement
    if reward.break_added > 0:
        double_tag = ''
        if reward.doubled:
            from ..libraries.maimaidx_card import format_duration
            double_tag = (
                f'（双倍BREAK卡生效中，剩余 {format_duration(reward.double_remaining)}）'
            )
        settlement += (
            f'\n💳 猜对奖励 +{reward.break_added} BREAK'
            f'（余额 {reward.balance}）{double_tag}'
        )
    elif reward.capped:
        settlement += '\n💳 猜对奖励 +0 BREAK'
    return settlement


_GUESS_BUSY_HINT = '该群已有正在进行的猜歌、猜曲绘、猜曲子、猜铺面、猜Rating、B50找内鬼、舞萌极限二选一、你想我猜或开字母'
_GUESS_SEND_FAIL_MSG = '游戏数据获取失败，本游戏已结束。'
GUESS_SEND_TIMEOUT_TEXT = 15
GUESS_SEND_TIMEOUT_MEDIA = 60
GUESS_SEND_TIMEOUT_VIDEO = 90
GUESS_AUDIO_PREPARE_FIRST_UPDATE = 20
GUESS_AUDIO_PREPARE_UPDATE_INTERVAL = 25
GUESS_CHART_PREPARE_FIRST_UPDATE = 25
GUESS_CHART_PREPARE_UPDATE_INTERVAL = 30
GUESS_GENERIC_PREPARE_FIRST_UPDATE = 8
GUESS_GENERIC_PREPARE_UPDATE_INTERVAL = 15


def _letter_busy(gid: GroupId) -> bool:
    from ..libraries.maimaidx_guess_letter import letter_guess

    return letter_guess.is_playing(gid)


def _rating_or_impostor_busy(gid: GroupId) -> bool:
    return rating_guess.is_busy(gid) or impostor_guess.is_busy(gid)


def _duel_busy(gid: GroupId) -> bool:
    return duel_guess.is_busy(gid)


def _twentyq_busy(gid: GroupId) -> bool:
    return twentyq_guess.is_busy(gid)


def _guess_or_letter_busy(gid: GroupId) -> bool:
    return (
        guess.is_busy(gid)
        or _letter_busy(gid)
        or _rating_or_impostor_busy(gid)
        or _duel_busy(gid)
        or _twentyq_busy(gid)
    )


async def _reserve_game_session(gid: GroupId, mode: str) -> bool:
    """Atomically reserve a group before a game handler performs any await."""
    return await game_session_gate.acquire(
        gid,
        mode=mode,
        busy_check=_guess_or_letter_busy,
    )


def _release_game_session(gid: GroupId) -> None:
    game_session_gate.release(gid)


def _guess_loop_should_stop(gid: GroupId, expected: Optional[GuessData] = None) -> bool:
    """猜歌主循环是否应退出（被重置、关闭或正常结束）。"""
    current = guess.Group.get(gid)
    if current is None or (expected is not None and current is not expected):
        return True
    if not guess.is_enabled(gid):
        return True
    return bool(guess.Group[gid].end)


async def _guess_sleep(
    gid: GroupId,
    seconds: float,
    expected: Optional[GuessData] = None,
) -> None:
    """可中断的 sleep：重置猜歌后尽快退出主循环。"""
    remaining = max(0.0, float(seconds))
    while remaining > 0 and not _guess_loop_should_stop(gid, expected):
        step = min(1.0, remaining)
        await asyncio.sleep(step)
        remaining -= step


class GuessSendAborted(Exception):
    """猜歌局内消息发送失败，本局已强制结束。"""


async def _force_end_guess_round(gid: GroupId) -> None:
    """强制结束本群猜歌局（可重复调用）。"""
    guess.Preparing.discard(gid)
    data = guess.Group.get(gid)
    if data is None:
        _release_game_session(gid)
        return
    data.end = True
    await guess_score.reset_all_streaks(gid)
    guess.end(gid, expected=data)


async def _guess_notify(
    matcher: Matcher,
    event: MessageEvent,
    message,
    *,
    reply: bool = False,
    mention_sender: Optional[bool] = None,
    timeout: int = GUESS_SEND_TIMEOUT_TEXT,
) -> None:
    """尽力发送通知，不修改游戏状态。"""
    try:
        if mention_sender is None:
            mention_sender = reply
        payload = message
        if use_qq_mode(event) and mention_sender:
            payload = ensure_sender_mention(payload, event)
        payload = adapt_guess_outbound(payload, event=event)
        if use_qq_mode(event) and not reply:
            gid = get_event_group_id(event)
            if gid is None:
                return
            await asyncio.wait_for(
                send_group_message(resolve_event_bot(event), gid, payload),
                timeout=timeout,
            )
        else:
            await asyncio.wait_for(
                matcher.send(
                    payload,
                    reply_message=resolve_reply_message(
                        event, reply_message=reply
                    ),
                ),
                timeout=timeout,
            )
    except Exception as e:
        gid = get_event_group_id(event)
        log.warning(
            f'[maimai] 猜歌通知发送失败 gid={gid}: {type(e).__name__}: {e}'
        )


async def _safe_matcher_send(
    matcher: Matcher,
    event: MessageEvent,
    message,
    gid: GroupId,
    *,
    reply: bool = False,
    mention_sender: Optional[bool] = None,
    media: bool = False,
    fatal: bool = True,
    timeout: Optional[int] = None,
) -> None:
    if timeout is None:
        timeout = GUESS_SEND_TIMEOUT_MEDIA if media else GUESS_SEND_TIMEOUT_TEXT
    try:
        if mention_sender is None:
            mention_sender = reply
        payload = message
        if use_qq_mode(event) and mention_sender:
            payload = ensure_sender_mention(payload, event)
        payload = adapt_guess_outbound(payload, event=event)
        if use_qq_mode(event) and not reply:
            await asyncio.wait_for(
                send_group_message(resolve_event_bot(event), gid, payload),
                timeout=timeout,
            )
        else:
            await asyncio.wait_for(
                matcher.send(
                    payload,
                    reply_message=resolve_reply_message(
                        event, reply_message=reply
                    ),
                ),
                timeout=timeout,
            )
    except Exception as e:
        log.warning(
            f'[maimai] 猜歌消息发送失败 gid={gid}: {type(e).__name__}: {e}'
        )
        if not fatal:
            return
        await _force_end_guess_round(gid)
        await _guess_notify(matcher, event, _GUESS_SEND_FAIL_MSG)
        raise GuessSendAborted() from e


async def _wait_prepare_with_progress(
    matcher: Matcher,
    event: MessageEvent,
    task: asyncio.Task,
    *,
    intro: str,
    title: str,
    status_fn,
    first_wait: int,
    interval: int,
    tip_fn=None,
):
    """通用准备等待：定时推送已等待秒数 + 当前步骤。"""
    await _guess_notify(matcher, event, intro, reply=True)
    started = asyncio.get_running_loop().time()
    wait_seconds = first_wait
    try:
        while True:
            try:
                return await asyncio.wait_for(asyncio.shield(task), timeout=wait_seconds)
            except asyncio.TimeoutError:
                elapsed = int(asyncio.get_running_loop().time() - started)
                detail = ''
                try:
                    detail = (status_fn() or '').strip()
                except Exception:
                    detail = ''
                tip = ''
                if tip_fn is not None:
                    try:
                        tip = (tip_fn(elapsed) or '').strip()
                    except Exception:
                        tip = ''
                lines = [f'{title}（已等待 {elapsed} 秒）']
                if detail:
                    lines.append(f'当前：{detail}')
                if tip:
                    lines.append(tip)
                await _guess_notify(matcher, event, '\n'.join(lines))
                wait_seconds = interval
    except asyncio.CancelledError:
        task.cancel()
        raise


async def _prepare_guess_audio_with_progress(
    matcher: Matcher,
    event: MessageEvent,
    gid: GroupId,
) -> Optional[GuessAudioData]:
    task = asyncio.create_task(guess.prepare_audio_round())
    return await _wait_prepare_with_progress(
        matcher, event, task,
        intro=(
            '正在随机选曲并准备音频…\n'
            '命中缓存通常数秒内开始；首次生成新曲预计 1～3 分钟，'
            '期间会报告具体步骤，请稍候。'
        ),
        title='猜曲音频仍在准备中',
        status_fn=get_audio_prepare_status,
        first_wait=GUESS_AUDIO_PREPARE_FIRST_UPDATE,
        interval=GUESS_AUDIO_PREPARE_UPDATE_INTERVAL,
        tip_fn=lambda elapsed: (
            '新曲通常会在总计 1～3 分钟内完成。'
            if elapsed < 180
            else '已超过常见耗时，可能正在跳过无资源曲目并尝试下一首。'
        ),
    )


async def _prepare_guess_chart_with_progress(
    matcher: Matcher,
    event: MessageEvent,
    gid: GroupId,
) -> Optional[GuessChartData]:
    task = asyncio.create_task(guess.prepare_chart_round())
    return await _wait_prepare_with_progress(
        matcher, event, task,
        intro=(
            '正在随机选曲并渲染铺面视频…\n'
            '命中缓存通常数秒内开始；首次需录制静音谱面 + 曲末 BGM，'
            '约 1.5～3 分钟，期间会报告具体步骤，请稍候。'
        ),
        title='猜铺面视频仍在渲染中',
        status_fn=get_chart_prepare_status,
        first_wait=GUESS_CHART_PREPARE_FIRST_UPDATE,
        interval=GUESS_CHART_PREPARE_UPDATE_INTERVAL,
        tip_fn=lambda elapsed: (
            '正在用谱面预览引擎录制并混音，请再稍候。'
            if elapsed < 240
            else '已超过常见耗时，可能正在换曲重试。'
        ),
    )


async def _send_guess_answer_bundle(
    matcher: Matcher,
    event: MessageEvent,
    data: GuessData,
    gid: GroupId,
    *,
    header: str,
    settlement: str = '',
    reply: bool = False,
) -> None:
    lines = [line for line in (header, settlement) if line]
    music_info = await draw_music_info(data.music)
    reveal = None
    if isinstance(data, GuessPicData):
        reveal = MessageSegment.image(
            await run_image_cpu(guess.render_pic_reveal, data)
        )
    final_audio = None
    if (
        isinstance(data, GuessAudioData)
        and data.hint_step < data.stage_count
        and data.stage_paths
    ):
        final_idx = data.stage_count - 1
        stage_path = Path(data.stage_paths[final_idx]).resolve()
        label = (
            STAGE_LABELS[final_idx]
            if final_idx < len(STAGE_LABELS)
            else '完整混音'
        )
        final_audio = (
            MessageSegment.text(f'\n[{label}]\n')
            + MessageSegment.record(str(stage_path))
        )

    chart_bgm = None
    if isinstance(data, GuessChartData) and data.video_path_bgm:
        bgm_path = Path(data.video_path_bgm).resolve()
        if bgm_path.is_file():
            chart_bgm = (
                MessageSegment.text('\n[曲末带 BGM 谱面]\n')
                + local_video_segment(bgm_path)
            )
        else:
            log.warning(
                f'[GuessChart] 揭晓 BGM 视频缺失 path={bgm_path}'
            )

    if bool(getattr(maiconfig, 'maimaidx_compact_messages', True)):
        bundle = Message()
        if lines:
            bundle += MessageSegment.text('\n'.join(lines) + '\n')
        bundle += music_info
        if reveal is not None:
            bundle += reveal
        await _safe_matcher_send(
            matcher, event, bundle, gid,
            reply=reply,
            media=reveal is not None,
            fatal=False,
        )
        # 部分 OneBot/QQ 实现不接受图片与语音混在同一条消息中。
        if final_audio is not None:
            await _safe_matcher_send(
                matcher, event, final_audio, gid, media=True, fatal=False,
            )
        if chart_bgm is not None:
            await _safe_matcher_send(
                matcher, event, chart_bgm, gid,
                media=True, fatal=False, timeout=GUESS_SEND_TIMEOUT_VIDEO,
            )
        await _send_guess_shortcuts(matcher, event, gid)
        return

    if lines:
        await _safe_matcher_send(
            matcher, event,
            MessageSegment.text('\n'.join(lines)),
            gid,
            reply=reply,
            fatal=False,
        )
    await _safe_matcher_send(matcher, event, music_info, gid, fatal=False)
    if reveal is not None:
        await _safe_matcher_send(
            matcher, event, reveal, gid,
            media=True,
            fatal=False,
        )
    if final_audio is not None:
        await _safe_matcher_send(
            matcher, event, final_audio, gid,
            media=True,
            fatal=False,
        )
    if chart_bgm is not None:
        await _safe_matcher_send(
            matcher, event, chart_bgm, gid,
            media=True, fatal=False, timeout=GUESS_SEND_TIMEOUT_VIDEO,
        )
    await _send_guess_shortcuts(matcher, event, gid)


async def _send_guess_score_forward(
    matcher: Matcher,
    bot: Bot,
    event: MessageEvent,
    title: str,
    nodes: list,
) -> None:
    if not nodes:
        await plugin_finish(matcher, title, event=event, reply_message=True)
    await plugin_finish(
        matcher,
        rank_text_image(format_forward_nodes_as_text(title, nodes)),
        event=event,
        reply_message=True,
    )


def _parse_grant_target(
    event: MessageEvent, args: Message,
) -> Optional[Union[str, Literal['all']]]:
    if is_at_all_message(event):
        return 'all'
    target = parse_at_target_id(event)
    if target is not None:
        return target
    text = args.extract_plain_text().strip()
    if text == '全体' or text.startswith('全体 '):
        return 'all'
    return None


def _parse_grant_args(text: str, *, for_all: bool = False) -> tuple[int, float]:
    """解析 数量 [有效小时]，默认 1 张 / 24 小时。"""
    parts = text.strip().split()
    if for_all and parts and parts[0] == '全体':
        parts = parts[1:]
    count = 1
    hours = float(DEFAULT_CARD_HOURS)
    if parts:
        try:
            count = int(parts[0])
        except ValueError:
            return count, hours
    if len(parts) >= 2:
        try:
            hours = float(parts[1])
        except ValueError:
            pass
    return count, hours


@guess_boost_grant.handle()
async def _(event: MessageEvent, args: Message = CommandArg()):
    gid = get_event_group_id(event)
    if gid is None:
        await guess_boost_grant.finish('请在群内使用。', reply_message=True)
    if not guess.is_enabled(gid):
        await guess_boost_grant.finish(
            '该群已关闭猜歌功能，开启请输入 开启mai猜歌', reply_message=True,
        )
    target = _parse_grant_target(event, args)
    if target is None:
        await guess_boost_grant.finish(
            '用法：发加倍卡 @用户 [数量] [有效小时]\n'
            '      发加倍卡 @全体成员 [数量] [有效小时]\n'
            '      发加倍卡 全体 [数量] [有效小时]\n'
            f'示例：发加倍卡 @某人 1 {DEFAULT_CARD_HOURS}；发加倍卡 全体 1 {DEFAULT_CARD_HOURS}',
            reply_message=True,
        )
    extra = args.extract_plain_text().strip()
    count, hours = _parse_grant_args(extra, for_all=(target == 'all'))
    issuer_uid = platform_user_id(event)

    if target == 'all':
        if use_qq_mode(event):
            self_id = str(event.self_id)
            uids = [
                uid for uid in qq_member_registry.list_member_ids(str(gid))
                if uid != self_id
            ]
            if not uids:
                await guess_boost_grant.finish(
                    '本群尚无足够的成员记录。请让成员先发言后再试，'
                    '或改用 @用户 单独发放。',
                    reply_message=True,
                )
        else:
            bot = resolve_event_bot(event)
            try:
                raw = await bot.call_api('get_group_member_list', group_id=int(gid))
            except Exception as e:
                log.warning(f'[GuessBoost] 获取群成员失败 gid={gid}: {e}')
                await guess_boost_grant.finish(f'获取群成员失败：{e}', reply_message=True)
            if not raw or not isinstance(raw, list):
                await guess_boost_grant.finish('群成员列表为空。', reply_message=True)
            self_id = int(bot.self_id)
            uids = [
                str(m['user_id']) for m in raw
                if m.get('user_id') is not None and int(m['user_id']) != self_id
            ]
            if not uids:
                await guess_boost_grant.finish('群成员列表为空。', reply_message=True)
        member_count, hours = await guess_boost_card.grant_many(
            gid,
            uids,
            count=count,
            hours=hours,
            issuer_uid=issuer_uid,
        )
        await guess_boost_grant.finish(
            f'已向本群 {member_count} 人各发放 {count} 张限时加倍卡'
            f'（{hours:g} 小时内有效，猜对消耗 1 张积分 ×2）。',
            reply_message=True,
        )
        return

    granted, hours = await guess_boost_card.grant(
        gid,
        target,
        count=count,
        hours=hours,
        issuer_uid=issuer_uid,
    )
    remain = guess_boost_card.active_count(gid, target)
    await guess_boost_grant.finish(
        build_mention_message(
            target,
            f'\n已发放 {granted} 张限时加倍卡（{hours:g} 小时内有效，猜对消耗 1 张积分 ×2）。'
            f'当前剩余 {remain} 张。',
            event=event,
        ),
        reply_message=True,
    )


@guess_boost_query.handle()
async def _(event: MessageEvent, args: Message = CommandArg()):
    gid = get_event_group_id(event)
    if gid is None:
        await guess_boost_query.finish('请在群内使用。', reply_message=True)
    self_uid = platform_user_id(event)
    target = parse_at_target_id(event) or self_uid
    count = guess_boost_card.active_count(gid, target)
    if count <= 0:
        if target == self_uid:
            await guess_boost_query.finish('你当前没有可用的限时加倍卡。', reply_message=True)
        else:
            await guess_boost_query.finish(
                build_mention_message(target, ' 当前没有可用的限时加倍卡。', event=event),
                reply_message=True,
            )
    nearest = guess_boost_card.nearest_expiry_hours(gid, target)
    hint = f'最近一张约 {nearest:.1f} 小时后过期' if nearest is not None else ''
    prefix = '你' if target == self_uid else ''
    msg = f'{prefix}当前有 {count} 张限时加倍卡（猜对消耗，积分 ×2）'
    if hint:
        msg += f'，{hint}'
    msg += '。'
    if target != self_uid:
        await guess_boost_query.finish(
            build_mention_message(target, f'\n{msg}', event=event),
            reply_message=True,
        )
    await guess_boost_query.finish(msg, reply_message=True)


@guess_migrate_data.handle()
async def _(event: MessageEvent):
    gid = get_event_group_id(event)
    if gid is None:
        await guess_migrate_data.finish('请在群内使用。', reply_message=True)
    uid = platform_user_id(event)
    pending_key = session_key('guess_sync', event)
    status, message = await guess_sync.start_manual_migrate(gid, uid)
    if status in {'need_prompt', 'pending'}:
        track_event(pending_key, event)
        await guess_migrate_data.finish(message or '', reply_message=True)
    finish_pending(pending_key)
    await guess_migrate_data.finish(message or '处理完成。', reply_message=True)


@guess_sync_reply.handle()
async def _(event: MessageEvent):
    gid = get_event_group_id(event)
    if gid is None:
        await guess_sync_reply.finish()
    uid = platform_user_id(event)
    pending_key = session_key('guess_sync', event)
    text = event.get_plaintext().strip()
    handled, reply = await guess_sync.handle_reply(gid, uid, text)
    if not handled:
        await guess_sync_reply.finish()
    if guess_sync.get_pending(gid, uid):
        track_event(pending_key, event)
    else:
        finish_pending(pending_key)
    await guess_sync_reply.finish(reply, reply_message=True)


@guess_music_start.handle()
async def _(event: MessageEvent):
    await _gate_guess_group_entry(guess_music_start, event)
    gid = get_event_group_id(event)
    if not guess.is_enabled(gid):
        await guess_music_start.finish('该群已关闭猜歌功能，开启请输入 开启mai猜歌')
    if not await _reserve_game_session(gid, 'song'):
        await guess_music_start.finish(_GUESS_BUSY_HINT)
    await _guess_notify(guess_music_start, event, '正在准备猜歌（选曲与提示）…', reply=True)
    try:
        guess.start(gid)
    except Exception:
        _release_game_session(gid)
        raise
    data = guess.Group[gid]
    try:
        await _safe_matcher_send(
            guess_music_start, event,
            dedent('''\
                我将从热门乐曲中选择一首歌，每隔8秒描述它的特征，
                请输入歌曲的 id 标题 或 别名（需bot支持，无需大小写）进行猜歌（DX乐谱和标准乐谱视为两首歌）。
                猜歌时查歌等其他命令依然可用。
                积分：越早猜中越高（基础最高7分）；首条提示前猜中可叠加首阶段×2、首答×2，理论最高4倍。
            '''),
            gid,
        )
        await _guess_sleep(gid, 4, data)
        for cycle in range(7):
            if _guess_loop_should_stop(gid, data):
                break
            if cycle < 6:
                await _safe_matcher_send(
                    guess_music_start, event,
                    f'{cycle + 1}/7 这首歌{data.options[cycle]}',
                    gid,
                )
                data.hint_step = cycle + 1
                await _guess_sleep(gid, 8, data)
            else:
                await _safe_matcher_send(
                    guess_music_start, event,
                    MessageSegment.text('7/7 这首歌封面的一部分是：\n')
                    + MessageSegment.image(data.img)
                    + MessageSegment.text('答案将在30秒后揭晓'),
                    gid,
                    media=True,
                )
                data.hint_step = 7
                for _ in range(30):
                    await _guess_sleep(gid, 1, data)
                    if _guess_loop_should_stop(gid, data):
                        await guess_music_start.finish()
                if _guess_loop_should_stop(gid, data):
                    await guess_music_start.finish()
                guess.Group[gid].end = True
                await guess_score.reset_all_streaks(gid)
                answer = (
                    MessageSegment.text('答案是：\n')
                    + await draw_music_info(data.music)
                )
                guess.end(gid, expected=data)
                await _safe_matcher_send(
                    guess_music_start,
                    event,
                    answer,
                    gid,
                    media=True,
                    fatal=False,
                )
                await guess_music_start.finish()
    except GuessSendAborted:
        await guess_music_start.finish()
    except BaseException:
        guess.end(gid, expected=data)
        raise


@guess_music_pic.handle()
async def _(event: MessageEvent, matched=RegexMatched()):
    await _gate_guess_group_entry(guess_music_pic, event)
    gid = get_event_group_id(event)
    if not guess.is_enabled(gid):
        await guess_music_pic.finish('该群已关闭猜歌功能，开启请输入 开启mai猜歌', reply_message=True)
    if not await _reserve_game_session(gid, 'pic'):
        await guess_music_pic.finish(_GUESS_BUSY_HINT, reply_message=True)
    diff_raw = matched.group(1)
    difficulty = int(diff_raw) if diff_raw else None
    await _guess_notify(
        guess_music_pic, event,
        '正在生成猜曲绘（裁剪封面与干扰）…',
        reply=True,
    )
    try:
        data = await run_image_cpu(guess.guesspicdata, difficulty)
        guess.Group[gid] = data
        guess._log_guess_start('猜曲绘', gid)
    except Exception:
        _release_game_session(gid)
        raise
    try:
        intro = dedent(f'''\
            开始猜曲绘！可以直接发送答案！
            每隔10秒会给出进一步提示。发送 重置猜歌 可结束游戏。
            当前难度：{data.difficulty}，当前干扰类型：{'、'.join(data.interference_labels)}
            积分：难度越高基础分越高（1～4分）；首次扩增前猜中可叠加首阶段×2、首答×2，理论最高4倍。
            指定难度可发送：猜曲绘1～猜曲绘4。
        ''')
        first_pic = MessageSegment.image(
            await run_image_cpu(guess.render_pic_crop, data)
        )
        compact = bool(getattr(maiconfig, 'maimaidx_compact_messages', True))
        await _safe_matcher_send(
            guess_music_pic, event,
            MessageSegment.text(intro + '\n') + first_pic if compact else intro,
            gid,
            media=compact,
        )
        if not compact:
            await _safe_matcher_send(
                guess_music_pic, event, first_pic, gid, media=True,
            )

        hint_interval = 10
        timeout_after_clear = 30
        clear_at = (data.expansion_count + 2) * hint_interval
        total_duration = clear_at + timeout_after_clear

        for elapsed in range(1, total_duration + 1):
            await _guess_sleep(gid, 1, data)
            if _guess_loop_should_stop(gid, data):
                await guess_music_pic.finish()
            if gid not in guess.Group:
                await guess_music_pic.finish()

            if elapsed % hint_interval != 0:
                continue

            step = elapsed // hint_interval
            if step <= data.expansion_count:
                guess.expand_pic_crop(data)
                crop = await run_image_cpu(guess.render_pic_crop, data)
                await _safe_matcher_send(
                    guess_music_pic, event,
                    MessageSegment.text('[区域扩增!]\n')
                    + MessageSegment.image(crop),
                    gid,
                    media=True,
                )
                data.hint_step += 1
            elif step == data.expansion_count + 1 and not data.global_shown:
                data.global_shown = True
                global_pic = await run_image_cpu(guess.render_pic_global, data)
                await _safe_matcher_send(
                    guess_music_pic, event,
                    MessageSegment.text('[全局视野!]\n')
                    + MessageSegment.image(global_pic),
                    gid,
                    media=True,
                )
                data.hint_step += 1
            elif step == data.expansion_count + 2 and not data.interference_cleared:
                data.interference_cleared = True
                clear_pic = await run_image_cpu(guess.render_pic_clear, data)
                await _safe_matcher_send(
                    guess_music_pic, event,
                    MessageSegment.text('[干扰消除!]\n')
                    + MessageSegment.image(clear_pic),
                    gid,
                    media=True,
                )
                data.hint_step += 1

        if _guess_loop_should_stop(gid, data):
            await guess_music_pic.finish()
        data.end = True
        await guess_score.reset_all_streaks(gid)
        guess.end(gid, expected=data)
        await _send_guess_answer_bundle(
            guess_music_pic, event, data, gid, header='答案是：',
        )
        await guess_music_pic.finish()
    except GuessSendAborted:
        await guess_music_pic.finish()
    except BaseException:
        guess.end(gid, expected=data)
        raise


@guess_music_audio.handle()
async def _(event: MessageEvent):
    await _gate_guess_group_entry(guess_music_audio, event)
    gid = get_event_group_id(event)
    if not guess.is_enabled(gid):
        await guess_music_audio.finish('该群已关闭猜歌功能，开启请输入 开启mai猜歌', reply_message=True)
    if not await _reserve_game_session(gid, 'audio'):
        await guess_music_audio.finish(_GUESS_BUSY_HINT, reply_message=True)
    if not await guess.try_begin_prepare(gid):
        _release_game_session(gid)
        await guess_music_audio.finish(_GUESS_BUSY_HINT, reply_message=True)

    data = None
    compact = bool(getattr(maiconfig, 'maimaidx_compact_messages', True))
    try:
        try:
            log.info(f'[GuessAudio] 猜曲子开局 gid={gid}')
            data = await _prepare_guess_audio_with_progress(
                guess_music_audio, event, gid,
            )
            if data is None:
                log.warning(f'[GuessAudio] 猜曲子无可用音频 gid={gid}')
                await guess_music_audio.finish(
                    '暂无可用猜曲音频（CDN 无资源或分轨失败）。'
                    '管理员可运行 scripts/build_guess_audio_cache.py 预烘焙，或安装 demucs 后重试。',
                    reply_message=True,
                )

            guess.startaudio(gid, data)
        finally:
            guess.end_prepare(gid)
            if gid not in guess.Group:
                _release_game_session(gid)

        stage_count = data.stage_count
        audio_meta = get_audio_manifest_entry(data.music.id)
        log.info(
            f'[GuessAudio] 猜曲子开始 gid={gid} music_id={data.music.id} '
            f'title={data.music.title} stages={stage_count} mode={audio_meta.get("mode", "?")}'
        )
        season_line = (
            '\n【赛季限时双倍得分】猜曲子积分 ×2（截至 6/30；'
            '第二段前猜中可叠加首阶段×2、首答×2，理论最高 8 倍）'
            if guess_score.audio_season_double_active()
            else ''
        )
        intro = dedent(f'''\
            猜曲子开始！共 {stage_count} 个阶段，每段约 30 秒，
            每隔 {STAGE_INTERVAL} 秒会放出更完整的混音。
            第四阶段结束后仍有 {STAGE_FINAL_GRACE} 秒作答时间。{season_line}
            请输入歌曲 id、标题或别名猜歌（DX 与标准视为不同曲目）。
            发送 重置猜歌 可结束本局。
        ''')
        if not compact:
            await _safe_matcher_send(
                guess_music_audio, event, intro, gid,
            )

        for stage_idx in range(stage_count):
            if _guess_loop_should_stop(gid, data):
                await guess_music_audio.finish()
            cur = data

            label = STAGE_LABELS[stage_idx] if stage_idx < len(STAGE_LABELS) else '更多乐器'
            stage_path = Path(cur.stage_paths[stage_idx]).resolve()
            log.info(
                f'[GuessAudio] 发送阶段 {stage_idx + 1}/{stage_count} gid={gid} '
                f'file={stage_path.name} size={stage_path.stat().st_size}'
            )
            stage_text = f'{stage_idx + 1}/{stage_count} [{label}]'
            if compact and stage_idx == 0:
                stage_text = intro + '\n' + stage_text
            if compact and stage_idx == stage_count - 1:
                stage_text += f'\n最后 {STAGE_FINAL_GRACE} 秒作答时间！'
            await _safe_matcher_send(
                guess_music_audio, event,
                MessageSegment.text(stage_text + '\n')
                + MessageSegment.record(str(stage_path)),
                gid,
                media=True,
            )
            cur.hint_step = stage_idx + 1

            if stage_idx == stage_count - 1 and not compact:
                await _safe_matcher_send(
                    guess_music_audio, event,
                    f'第四阶段已放出，最后 {STAGE_FINAL_GRACE} 秒作答时间！',
                    gid,
                )

            if stage_idx < stage_count - 1:
                for _ in range(STAGE_INTERVAL):
                    await _guess_sleep(gid, 1, data)
                    if _guess_loop_should_stop(gid, data):
                        await guess_music_audio.finish()

        for _ in range(STAGE_FINAL_GRACE):
            await _guess_sleep(gid, 1, data)
            if _guess_loop_should_stop(gid, data):
                await guess_music_audio.finish()

        if _guess_loop_should_stop(gid, data):
            await guess_music_audio.finish()
        cur = data
        cur.end = True
        await guess_score.reset_all_streaks(gid)
        guess.end(gid, expected=data)
        await _send_guess_answer_bundle(
            guess_music_audio, event, data, gid, header='答案是：',
        )
        await guess_music_audio.finish()
    except GuessSendAborted:
        await guess_music_audio.finish()
    except BaseException:
        guess.end_prepare(gid)
        if data is not None:
            guess.end(gid, expected=data)
        else:
            _release_game_session(gid)
        raise


@guess_music_chart.handle()
async def _(event: MessageEvent):
    await _gate_guess_group_entry(guess_music_chart, event)
    gid = get_event_group_id(event)
    if not guess.is_enabled(gid):
        await guess_music_chart.finish('该群已关闭猜歌功能，开启请输入 开启mai猜歌', reply_message=True)
    if not await _reserve_game_session(gid, 'chart'):
        await guess_music_chart.finish(_GUESS_BUSY_HINT, reply_message=True)
    if not await guess.try_begin_prepare(gid):
        _release_game_session(gid)
        await guess_music_chart.finish(_GUESS_BUSY_HINT, reply_message=True)

    data = None
    compact = bool(getattr(maiconfig, 'maimaidx_compact_messages', True))
    try:
        try:
            log.info(f'[GuessChart] 猜铺面开局 gid={gid}')
            data = await _prepare_guess_chart_with_progress(
                guess_music_chart, event, gid,
            )
            if data is None:
                log.warning(f'[GuessChart] 猜铺面无可用视频 gid={gid}')
                await guess_music_chart.finish(
                    '暂无可用猜铺面视频（谱面 CDN 无资源或渲染失败）。\n'
                    '请确认已构建 chart_preview（npm run build），'
                    '并已安装 Chromium（playwright install chromium）与 ffmpeg。',
                    reply_message=True,
                )

            guess.startchart(gid, data)
        finally:
            guess.end_prepare(gid)
            if gid not in guess.Group:
                _release_game_session(gid)

        video_path = Path(data.video_path).resolve()
        has_bgm = bool(data.video_path_bgm and Path(data.video_path_bgm).is_file())
        if data.video_path_bgm and not has_bgm:
            log.warning(
                f'[GuessChart] 开局声明有 BGM 但文件不存在 '
                f'path={data.video_path_bgm}'
            )
        if not has_bgm:
            log.warning(
                f'[GuessChart] 本局无 BGM 视频，将只发静音谱面 '
                f'music_id={data.music.id} kind={data.chart_kind}'
            )
        log.info(
            f'[GuessChart] 猜铺面开始 gid={gid} music_id={data.music.id} '
            f'title={data.music.title} kind={data.chart_kind} '
            f'diff={data.chart_diff_name} bgm={has_bgm} file={video_path.name}'
        )
        season_line = ''
        if guess_score.chart_season_double_active():
            end = guess_score.CHART_SEASON_DOUBLE_END.strftime('%Y-%m-%d')
            season_line = f'\n【限时双倍】猜铺面积分 ×2（截至 {end}）'
        total_sec = CHART_STAGE_INTERVAL + CHART_STAGE_FINAL_GRACE
        if has_bgm:
            intro = dedent(f'''\
                猜铺面开始！整局约 {total_sec} 秒，共 2 个阶段：
                ① 前 {CHART_STAGE_INTERVAL} 秒：静音谱面约 {data.duration} 秒（带正解音，难度倾向 {data.chart_diff_name}）
                ② 最后 {CHART_STAGE_FINAL_GRACE} 秒：放出曲末约 {data.bgm_duration or CHART_PHASE2_DURATION} 秒带 BGM 谱面
                越早答分越高；BGM 放出后继续扣分，最低 1 分。
                请输入歌曲 id、标题或别名作答。发送 重置猜歌 可结束本局。{season_line}
            ''')
        else:
            intro = dedent(f'''\
                猜铺面开始！将发送一段约 {data.duration} 秒的静音谱面视频
                （无 BGM；难度倾向 {data.chart_diff_name} 谱），作答约 {total_sec} 秒。
                请根据铺面输入歌曲 id、标题或别名作答。
                发送 重置猜歌 可结束本局。{season_line}
            ''')
        stage_text = intro if compact else '【阶段1】静音谱面：'
        if not compact:
            await _safe_matcher_send(guess_music_chart, event, intro, gid)

        await _safe_matcher_send(
            guess_music_chart, event,
            MessageSegment.text(stage_text + '\n')
            + local_video_segment(video_path),
            gid,
            media=True,
            timeout=GUESS_SEND_TIMEOUT_VIDEO,
        )
        data.started_at = time.time()
        data.hint_step = 1

        if has_bgm:
            for _ in range(CHART_STAGE_INTERVAL):
                await _guess_sleep(gid, 1, data)
                if _guess_loop_should_stop(gid, data):
                    await guess_music_chart.finish()

            if _guess_loop_should_stop(gid, data):
                await guess_music_chart.finish()

            bgm_path = Path(data.video_path_bgm).resolve()
            if not bgm_path.is_file():
                log.error(
                    f'[GuessChart] 阶段2 BGM 文件丢失，跳过发送 path={bgm_path}'
                )
            else:
                stage2 = (
                    f'【阶段2】曲末约 {data.bgm_duration or CHART_PHASE2_DURATION} 秒带 BGM 谱面：\n'
                    if not compact else
                    f'【阶段2】曲末带 BGM（约 {data.bgm_duration or CHART_PHASE2_DURATION}s）\n'
                )
                log.info(
                    f'[GuessChart] 发送阶段2 BGM gid={gid} '
                    f'size={bgm_path.stat().st_size} file={bgm_path.name}'
                )
                await _safe_matcher_send(
                    guess_music_chart, event,
                    MessageSegment.text(stage2)
                    + local_video_segment(bgm_path),
                    gid,
                    media=True,
                    timeout=GUESS_SEND_TIMEOUT_VIDEO,
                )
            cur = guess.Group.get(gid)
            if cur is not data or cur.end:
                await guess_music_chart.finish()
            cur.hint_step = 2
            cur.bgm_at = time.time()
            remaining = CHART_STAGE_FINAL_GRACE
            await _guess_notify(
                guess_music_chart, event,
                f'曲末 BGM 已放出！⏳ 还剩 {remaining}秒 作答时间哟！',
            )
            for _ in range(CHART_STAGE_FINAL_GRACE):
                await _guess_sleep(gid, 1, data)
                if _guess_loop_should_stop(gid, data):
                    await guess_music_chart.finish()
                remaining -= 1
                if remaining in CHART_COUNTDOWN_MARKS:
                    await _guess_notify(
                        guess_music_chart, event,
                        f'⏳ 还剩 {remaining}秒 作答时间哟！',
                    )
        else:
            # 无 BGM 时仍给满整局时长（90+30=120）
            remaining = total_sec
            await _guess_notify(
                guess_music_chart, event,
                f'⏳ 还剩 {remaining}秒 作答时间哟！',
            )
            for _ in range(total_sec):
                await _guess_sleep(gid, 1, data)
                if _guess_loop_should_stop(gid, data):
                    await guess_music_chart.finish()
                remaining -= 1
                if remaining in CHART_COUNTDOWN_MARKS:
                    await _guess_notify(
                        guess_music_chart, event,
                        f'⏳ 还剩 {remaining}秒 作答时间哟！',
                    )

        if _guess_loop_should_stop(gid, data):
            await guess_music_chart.finish()
        cur = data
        cur.end = True
        await guess_score.reset_all_streaks(gid)
        guess.end(gid, expected=data)
        await _send_guess_answer_bundle(
            guess_music_chart, event, data, gid, header='答案是：',
        )
        await guess_music_chart.finish()
    except GuessSendAborted:
        await guess_music_chart.finish()
    except BaseException:
        guess.end_prepare(gid)
        if data is not None:
            guess.end(gid, expected=data)
        else:
            _release_game_session(gid)
        raise


@update_guess_audio.handle()
async def _(event: MessageEvent, match=RegexMatched()):
    force = match.group(1) is not None
    log.info(f'[GuessAudio] 收到「更新猜曲音频」qq={event.user_id} force={force}')
    hint = '强制重建' if force else '增量烘焙'
    await update_guess_audio.send(
        f'开始{hint}猜曲音频（热门池）。单首通常需要 1～3 分钟，'
        '完整热门池可能耗时数小时；已有缓存会自动跳过。'
        '进度请看服务器日志，完成后私聊汇总。'
    )
    try:
        report = await build_hot_audio_cache(force=force)
    except asyncio.CancelledError:
        request_hot_batch_cancel()
        log.warning(f'[GuessAudio] 「更新猜曲音频」被取消 qq={event.user_id}')
        raise
    log.info(f'[GuessAudio] 「更新猜曲音频」完成 qq={event.user_id} force={force}')
    await update_guess_audio.finish(report)


@update_guess_chart.handle()
async def _(event: MessageEvent, match=RegexMatched()):
    force = match.group(1) is not None
    limit_raw = match.group(2)
    limit = int(limit_raw) if limit_raw else None
    log.info(
        f'[GuessChart] 收到「更新猜铺面」qq={event.user_id} '
        f'force={force} limit={limit}'
    )
    hint = '强制重建' if force else '增量预制'
    limit_hint = (
        f'本次最多处理 {limit} 首。'
        if limit is not None
        else ('将尽量扫完整热门池。' if force else '默认每次最多新建约 20 首。')
    )
    await update_guess_chart.send(
        f'开始{hint}猜铺面视频（热门池）。\n'
        f'单首含静音段 + 曲末 BGM，通常 1.5～3 分钟；{limit_hint}\n'
        '会优先补齐「有静音缺 BGM」的空洞；已有完整缓存会跳过。\n'
        '渲染低优先级限并发，不打满 CPU。进度看服务器日志，完成后私聊汇总。'
    )
    try:
        report = await build_hot_chart_cache(force=force, limit=limit)
    except asyncio.CancelledError:
        request_chart_batch_cancel()
        log.warning(f'[GuessChart] 「更新猜铺面」被取消 qq={event.user_id}')
        raise
    log.info(f'[GuessChart] 「更新猜铺面」完成 qq={event.user_id}')
    await update_guess_chart.finish(report)


@guess_prepare_status.handle()
async def _(event: MessageEvent):
    """管理员查看音频/谱面热门池的当前预制进度与缓存分布。"""
    try:
        pool = await asyncio.to_thread(guess._guess_music_pool)
    except Exception as exc:
        await plugin_finish(
            guess_prepare_status,
            f'读取猜歌热门池失败：{type(exc).__name__}: {exc}',
            event=event,
        )
    if not pool:
        await plugin_finish(
            guess_prepare_status,
            '猜歌热门池为空，可能仍在等待曲库初始化。',
            event=event,
        )
    try:
        audio = await asyncio.to_thread(summarize_audio_pool_cache, pool)
        chart = await asyncio.to_thread(summarize_chart_pool_cache, pool)
    except Exception as exc:
        await plugin_finish(
            guess_prepare_status,
            f'读取缓存状态失败：{type(exc).__name__}: {exc}',
            event=event,
        )

    def _line(label: str, stats: dict[str, int], *, chart_mode: bool = False) -> str:
        ready = int(stats.get('ready', 0))
        partial = int(stats.get('partial', 0))
        empty = int(stats.get('empty', 0))
        if chart_mode:
            middle = f"静音已好/BGM未好 {stats.get('mute_only', 0)}"
        else:
            middle = f"旧版本缓存 {stats.get('stale', 0)}"
        return (
            f'{label}：完整 {ready}，{middle}，部分 {partial}，未预制 {empty}'
        )

    audio_task = get_audio_prepare_status().strip() or '空闲'
    chart_task = get_chart_prepare_status().strip() or '空闲'
    text = (
        f'猜歌预制状态（热门池 {len(pool)} 首）\n'
        f'{_line("猜曲音频", audio)}\n'
        f'{_line("猜谱面视频", chart, chart_mode=True)}\n\n'
        f'音频任务：{audio_task}\n'
        f'谱面任务：{chart_task}'
    )
    await plugin_finish(guess_prepare_status, text, event=event)


@render_status.handle()
async def _(event: MessageEvent):
    """一次查看音频和谱面所有活动任务，包含可持久化 ETA。"""
    await plugin_finish(render_status, format_active_tasks(), event=event)


@guess_music_solve.handle()
async def _(event: MessageEvent):
    gid = get_event_group_id(event)
    if gid not in guess.Group:
        await guess_music_solve.finish()
    data = guess.Group[gid]
    ans = event.get_plaintext().strip()
    if not ans:
        await guess_music_solve.finish()
    uid_key = platform_user_id(event)
    rate_limit_msg = consume_guess_answer_slot(uid_key)
    if rate_limit_msg:
        await guess_music_solve.finish(
            adapt_guess_outbound(rate_limit_msg, event=event),
            reply_message=resolve_reply_message(event, reply_message=True),
        )
    data.user_attempts[uid_key] = data.user_attempts.get(uid_key, 0) + 1
    first_guess = data.user_attempts[uid_key] == 1
    pic_difficulty = data.difficulty if isinstance(data, GuessPicData) else None
    if match_guess_answer(ans, data.answer, pic_difficulty=pic_difficulty):
        if data.end:
            await guess_music_solve.finish()
        data.end = True
        settlement = await _award_guess_points(
            event,
            gid,
            data,
            first_stage=_guess_first_stage(data),
            first_guess=first_guess,
        )
        guess.end(gid, expected=data)
        try:
            await _send_guess_answer_bundle(
                guess_music_solve, event, data, gid,
                header='猜对了！',
                settlement=settlement,
                reply=True,
            )
        except GuessSendAborted:
            pass
        await guess_music_solve.finish()


@guess_music_reset.handle()
async def _(event: MessageEvent):
    gid = get_event_group_id(event)
    from ..libraries.maimaidx_guess_letter import letter_guess

    if letter_guess.is_playing(gid):
        board = letter_guess.get(gid)
        if board is not None:
            letter_guess.reveal_all(board)
            letter_guess.end(gid)
            titles = ' / '.join(s.title for s in board.songs)
            await guess_music_reset.finish(
                f'已结束开字母。\n本局曲目：{titles}',
                reply_message=True,
            )
    if gid in guess.Preparing:
        guess.end_prepare(gid)
        await guess_music_reset.finish('已取消猜歌准备，本局未开始。', reply_message=True)
        return
    if gid not in guess.Group:
        await guess_music_reset.finish('该群未处在猜歌状态', reply_message=True)
        return
    data = guess.Group[gid]
    music = data.music
    await _force_end_guess_round(gid)
    await _guess_notify(
        guess_music_reset, event,
        f'已重置该群猜歌，本游戏已结束。\n答案是：{music.title}（ID: {music.id}）',
        reply=True,
    )
    await guess_music_reset.finish()


async def _handle_guess_score_board(
    event: MessageEvent,
    matcher: Matcher,
    *,
    period: str,
) -> None:
    gid = get_event_group_id(event)
    if gid is None:
        await matcher.finish('请在群内使用。', reply_message=True)
    if not guess.is_enabled(gid):
        await matcher.finish('该群已关闭猜歌功能，开启请输入 开启mai猜歌', reply_message=True)
    bot = resolve_event_bot(event)
    title, nodes = guess_score.build_ranking_forward(
        gid,
        int(event.self_id),
        period=period,
    )
    await _send_guess_score_forward(matcher, bot, event, title, nodes)


@guess_group_rank.handle()
async def _(event: MessageEvent):
    gid = get_event_group_id(event)
    if gid is None:
        await guess_group_rank.finish('请在群内使用。', reply_message=True)
    if not guess.is_enabled(gid):
        await guess_group_rank.finish(
            '该群已关闭猜歌功能，开启请输入 开启mai猜歌', reply_message=True,
        )
    ranking = guess_score.get_ranking(gid, top_n=20)
    if not ranking:
        await guess_group_rank.finish('本群暂无猜歌积分记录。', reply_message=True)
    b64 = await asyncio.to_thread(
        rank_image_b64,
        await render_guess_rank_image(ranking),
    )
    await guess_group_rank.finish(
        adapt_guess_outbound(MessageSegment.image(b64), event=event),
        reply_message=True,
    )


async def _resolve_guess_stats_target(event: MessageEvent) -> tuple[str, str, bool]:
    """解析「我的猜歌」目标用户：(uid, display_name, is_other)。无 at 查自己。"""
    at_uid = parse_at_target_id(event)
    self_uid = str(platform_user_id(event))
    if not at_uid or at_uid == self_uid:
        return self_uid, (_sender_name(event) or self_uid), False

    name = ''
    gid = get_event_group_id(event)
    try:
        bot = resolve_event_bot(event)
        if gid is not None and str(gid).isdigit() and str(at_uid).isdigit():
            member = await bot.call_api(
                'get_group_member_info',
                group_id=int(gid),
                user_id=int(at_uid),
            )
            card = (member.get('card') or '').strip()
            nick = (member.get('nickname') or '').strip()
            name = card or nick
    except Exception:
        name = ''
    if not name and gid is not None:
        member = guess_score.get_member_or_none(gid, at_uid)
        if member:
            name = member.name or ''
    return at_uid, (name or at_uid), True


def _legacy_guess_identity_hint(event: MessageEvent, gid: GroupId, uid: str) -> str:
    """Explain an empty migrated-group result when the user is not qbound.

    Group migration and user migration are independent.  The former lets us
    read the old group key, but the latter is still required to match a
    person's old score row.  Keep this diagnostic at the empty-result boundary
    so normal OneBot users and users with no migrated group never see it.
    """
    if not use_qq_mode(event) or str(uid).strip().isdigit():
        return ''
    try:
        from ..libraries.maimaidx_qq_bind import qq_bind_db

        legacy_group = qq_bind_db.get_group_legacy_id(str(gid))
        if legacy_group is None:
            return ''
        if qq_bind_db.get_legacy_qq(str(uid)) is not None:
            return ''
    except Exception:
        return ''
    return (
        f'\n检测到本群已绑定旧 QQ 群号 {int(legacy_group)}，但你的官方 QQ 身份尚未 qbind；'
        '旧个人猜歌记录暂时无法匹配，数据没有因此被清空。请发送「qbind」完成绑定后再试。'
    )


@guess_my_stats.handle()
async def _(event: MessageEvent):
    gid = get_event_group_id(event)
    if gid is None:
        await guess_my_stats.finish('请在群内使用。', reply_message=True)
    # 查自己时做主群导流 / 数据同步；查别人不打断
    at_uid = parse_at_target_id(event)
    self_uid = str(platform_user_id(event))
    if not at_uid or at_uid == self_uid:
        await _gate_guess_group_entry(guess_my_stats, event)
    if not guess.is_enabled(gid):
        await guess_my_stats.finish(
            '该群已关闭猜歌功能，开启请输入 开启mai猜歌', reply_message=True,
        )
    uid, display_name, is_other = await _resolve_guess_stats_target(event)
    stats = guess_score.build_user_guess_stats(gid, uid)
    stats['name'] = display_name or stats.get('name') or str(uid)
    if (
        int(stats.get('total_score') or 0) <= 0
        and not any(int((stats.get('modes') or {}).get(m, {}).get('count') or 0)
                    for m in guess_score.GUESS_MODES)
    ):
        empty = (
            f'{display_name} 在本群还没有猜歌积分记录。'
            if is_other
            else (
                '你在本群还没有猜歌积分记录。猜对 / 开字母结算后会累计到「我的猜歌」。'
                + _legacy_guess_identity_hint(event, gid, self_uid)
            )
        )
        await guess_my_stats.finish(empty, reply_message=True)
    b64 = await asyncio.to_thread(personal_guess_stats_image_b64, stats)
    await guess_my_stats.finish(
        adapt_guess_outbound(MessageSegment.image(b64), event=event),
        reply_message=True,
    )


@guess_score_rank.handle()
async def _(event: MessageEvent):
    await _handle_guess_score_board(event, guess_score_rank, period='total')


@guess_score_daily.handle()
async def _(event: MessageEvent):
    await _handle_guess_score_board(event, guess_score_daily, period='daily')


@guess_score_weekly.handle()
async def _(event: MessageEvent):
    await _handle_guess_score_board(event, guess_score_weekly, period='weekly')


@guess_score_monthly.handle()
async def _(event: MessageEvent):
    await _handle_guess_score_board(event, guess_score_monthly, period='monthly')


@guess_score_yearly.handle()
async def _(event: MessageEvent):
    await _handle_guess_score_board(event, guess_score_yearly, period='yearly')


@guess_score_season.handle()
async def _(event: MessageEvent):
    await _handle_guess_score_board(event, guess_score_season, period='season')


async def _handle_guess_history_board(
    event: MessageEvent,
    matcher: Matcher,
    *,
    period: str,
    period_key: Optional[str] = None,
) -> None:
    gid = get_event_group_id(event)
    if gid is None:
        await matcher.finish('请在群内使用。', reply_message=True)
    if not guess.is_enabled(gid):
        await matcher.finish('该群已关闭猜歌功能，开启请输入 开启mai猜歌', reply_message=True)
    if not period_key:
        period_key = guess_score.previous_period_key(period)
    bot = resolve_event_bot(event)
    title, nodes = guess_score.build_ranking_forward(
        gid,
        int(event.self_id),
        period=period,
        period_key=period_key,
    )
    await _send_guess_score_forward(matcher, bot, event, title, nodes)


@guess_score_hist_daily.handle()
async def _(event: MessageEvent, args: Message = CommandArg()):
    key = args.extract_plain_text().strip()
    await _handle_guess_history_board(
        event, guess_score_hist_daily, period='daily', period_key=key or None,
    )


@guess_score_hist_weekly.handle()
async def _(event: MessageEvent, args: Message = CommandArg()):
    key = args.extract_plain_text().strip()
    await _handle_guess_history_board(
        event, guess_score_hist_weekly, period='weekly', period_key=key or None,
    )


@guess_score_hist_monthly.handle()
async def _(event: MessageEvent, args: Message = CommandArg()):
    key = args.extract_plain_text().strip()
    await _handle_guess_history_board(
        event, guess_score_hist_monthly, period='monthly', period_key=key or None,
    )


@guess_score_hist_yearly.handle()
async def _(event: MessageEvent, args: Message = CommandArg()):
    key = args.extract_plain_text().strip()
    await _handle_guess_history_board(
        event, guess_score_hist_yearly, period='yearly', period_key=key or None,
    )


@guess_score_hist_season.handle()
async def _(event: MessageEvent, args: Message = CommandArg()):
    key = args.extract_plain_text().strip()
    await _handle_guess_history_board(
        event, guess_score_hist_season, period='season', period_key=key or None,
    )


@guess_music_enable.handle()
@guess_music_disable.handle()
async def _(matcher: Matcher, event: MessageEvent):
    gid = get_event_group_id(event)
    if type(matcher) is guess_music_enable:
        msg = await guess.on(gid)
    elif type(matcher) is guess_music_disable:
        msg = await guess.off(gid)
    else:
        raise ValueError('matcher type error')
    await guess_music_enable.finish(msg, reply_message=True)


# ─────────────────────── 猜Rating ───────────────────────


def _parse_rating_match(matched) -> tuple[int, int]:
    """解析 ``猜Rating[1-5] [时长]``；不指定难度时与猜曲绘一致随机 1-4。"""
    attached = matched.group(1)
    spaced = matched.group(2)
    duration_raw = matched.group(3)
    difficulty = int(attached or spaced) if (attached or spaced) else random.randint(1, 4)
    duration = int(duration_raw) if duration_raw else DEFAULT_DURATION
    return difficulty, max(MIN_DURATION, min(300, duration))


async def _prerender_reveal_segment(sd_best, dx_best, target_name, target_rating):
    """后台预渲染揭晓B50图；失败返回 None（结算时降级为纯文本）。"""
    from ..libraries.maimaidx_guess_rating_image import reveal_b50_image_segment
    try:
        return await asyncio.to_thread(
            reveal_b50_image_segment, sd_best, dx_best, target_name, target_rating
        )
    except Exception as e:
        log.warning(f'[GuessRating] 预渲染揭晓图失败: {e}')
        return None


@guess_rating_start.handle()
async def _(event: MessageEvent, matched=RegexMatched()):
    await _gate_guess_group_entry(guess_rating_start, event)
    gid = get_event_group_id(event)
    if gid is None:
        await guess_rating_start.finish('请在群内使用。', reply_message=True)
    if not guess.is_enabled(gid):
        await guess_rating_start.finish('该群已关闭猜歌功能，开启请输入 开启mai猜歌', reply_message=True)
    if not await _reserve_game_session(gid, 'rating'):
        await guess_rating_start.finish(_GUESS_BUSY_HINT, reply_message=True)

    difficulty, duration = _parse_rating_match(matched)
    difficulty_cfg = RATING_DIFFICULTIES[difficulty]
    display_count = difficulty_cfg.display_count

    bot = resolve_event_bot(event)
    await _guess_notify(guess_rating_start, event, '🔍 正在选取群友并加载B50…', reply=True)

    # 立即加锁，防止并发重复开局
    if not rating_guess.lock(gid):
        _release_game_session(gid)
        await guess_rating_start.finish(_GUESS_BUSY_HINT, reply_message=True)

    try:
        # 选取候选人
        candidate = await pick_random_candidate(
            bot, gid, min_charts=display_count,
        )
        if candidate is None:
            await guess_rating_start.finish(
                '未找到可用的群友数据。请确保群内有成员已开启数据存储并上传过成绩。',
                reply_message=True,
            )

        target_uid, target_name, b50 = candidate
        if b50.rating is None:
            await guess_rating_start.finish('该群友的Rating数据不可用。', reply_message=True)

        # 记录上局目标（防连抽）+ 清空本局志愿者（下局需重新报名）
        rating_guess.last_target[gid] = target_uid
        rating_guess.clear_volunteers(gid)

        # 抽取曲目
        selected, sd_best, dx_best = select_random_charts(b50, display_count)
        if not selected:
            await guess_rating_start.finish('该群友的B50数据为空。', reply_message=True)
    except Exception:
        rating_guess.unlock(gid)
        raise

    # 开局（start 会覆盖 locked 状态）
    data = rating_guess.start(
        gid,
        target_uid=target_uid,
        target_name=target_name,
        target_rating=b50.rating,
        difficulty=difficulty,
        display_count=display_count,
        total_chart_count=len(sd_best) + len(dx_best),
        duration=duration,
        selected_charts=selected,
        b50_sd=sd_best,
        b50_dx=dx_best,
    )

    # 数据开局时已确定，倒计时期间后台预渲染揭晓图，结算时直接发送
    reveal_task = asyncio.create_task(
        _prerender_reveal_segment(sd_best, dx_best, target_name, b50.rating)
    )

    # 发送隐藏B50图
    from ..libraries.maimaidx_guess_rating_image import hidden_b50_image_segment

    intro = dedent(f'''\
        🎮 猜Rating开始！
        难度 {difficulty} · 展示 {len(selected)} 首 / 共 {data.total_chart_count} 首
        ⏱ {duration}秒作答时间，发送数字猜Rating（可修改）
        最接近者获胜 🏆
        题主不能作答或参与奖励；满3人开局才发BREAK，前二名分别+2/+1，第一名差>200本局无BREAK。
    ''')

    compact = bool(getattr(maiconfig, 'maimaidx_compact_messages', True))
    try:
        img_seg = await asyncio.to_thread(
            hidden_b50_image_segment,
            selected,
            display_count,
            total_chart_count=data.total_chart_count,
            difficulty=difficulty,
            show_rate=difficulty_cfg.show_rate,
            show_fc_fs=difficulty_cfg.show_fc_fs,
            hide_cover=difficulty_cfg.hide_cover,
        )
        if compact:
            bundle = MessageSegment.text(intro + '\n') + img_seg
            await _safe_matcher_send(guess_rating_start, event, bundle, gid, media=True)
        else:
            await _safe_matcher_send(guess_rating_start, event, intro, gid)
            await _safe_matcher_send(guess_rating_start, event, img_seg, gid, media=True)
    except Exception as e:
        log.warning(f'[GuessRating] 发送隐藏B50图失败 gid={gid}: {e}')
        await _safe_matcher_send(guess_rating_start, event, intro, gid)

    # 倒计时
    remaining = duration
    while remaining > 0:
        await asyncio.sleep(1)
        if rating_guess.get(gid) is not data:
            reveal_task.cancel()
            return
        if data and data.end:
            break
        remaining -= 1
        if remaining in (30, 10):
            await _guess_notify(
                guess_rating_start, event,
                f'⏳ 还剩 {remaining}秒！',
            )

    # 结算
    if rating_guess.get(gid) is not data:
        reveal_task.cancel()
        return

    settlement = rating_guess.settle(gid)
    if settlement is None:
        reveal_task.cancel()
        return
    # settle() 已经冻结了本局数据；先释放群状态，避免奖励或渲染异常把游戏卡成 busy。
    rating_guess.end(gid, expected=data)

    # 发放奖励
    from ..libraries.maimaidx_break import break_db

    for reward in settlement.rewards:
        if reward.score > 0:
            try:
                await guess_score.award_fixed_points(
                    gid,
                    reward.uid,
                    reward.name,
                    reward.score,
                    mode=guess_score.MODE_RATING,
                )
            except Exception as exc:
                log.exception(
                    f'[GuessRating] 积分结算失败，继续发送结果 '
                    f'gid={gid} uid={reward.uid}: {type(exc).__name__}: {exc}'
                )
        if reward.break_points > 0:
            try:
                award = await asyncio.to_thread(
                    break_db.award_game_break,
                    reward.billing_id, 'rating', reward.break_points,
                    'rating_guess_settlement',
                    meta={
                        'group_id': str(gid),
                        'target_uid': settlement.target_uid,
                        'target_name': settlement.target_name,
                        'target_rating': settlement.target_rating,
                        'difficulty': difficulty,
                        'rank': reward.rank,
                        'diff': reward.diff,
                    },
                )
                # 写回实际到账额，供结算文案/画图按真实发放显示；封顶时记 capped。
                reward.break_points = award.awarded
                reward.break_capped = award.capped
            except Exception as exc:
                log.exception(
                    f'[GuessRating] BREAK 结算失败，继续发送结果 '
                    f'gid={gid} uid={reward.uid}: {type(exc).__name__}: {exc}'
                )

    # 构建结算消息
    result_lines = [
        '🎉 猜Rating结束！',
        '',
        f'🎵 TA是 {settlement.target_name}！',
        f'🎯 真实Rating：{settlement.target_rating}',
        f'🎚 本局难度：{difficulty}',
        '',
        '📊 排名：',
        format_reward_text(settlement.rewards, settlement.target_rating),
    ]
    result_text = '\n'.join(result_lines)

    # 发送揭晓B50图（开局后已后台预渲染，此处直接取结果）
    try:
        reveal_img = await reveal_task
        if reveal_img is None:
            raise RuntimeError('reveal prerender failed')
        bundle = MessageSegment.text(result_text + '\n') + reveal_img
        await _safe_matcher_send(guess_rating_start, event, bundle, gid, media=True, fatal=False)
    except Exception as e:
        log.warning(f'[GuessRating] 发送揭晓图失败 gid={gid}: {e}')
        await _safe_matcher_send(guess_rating_start, event, result_text, gid, fatal=False)

    await _send_guess_shortcuts(guess_rating_start, event, gid)
    await guess_rating_start.finish()


@guess_rating_solve.handle()
async def _(event: MessageEvent):
    gid = get_event_group_id(event)
    if gid is None:
        return
    data = rating_guess.get(gid)
    if data is None or data.end:
        return

    text = event.get_plaintext().strip()
    if not text:
        return

    # 只接受纯数字
    if not text.isdigit():
        return

    answer = int(text)
    if answer < 0 or answer > 99999:
        return

    # 频率限制
    uid_key = platform_user_id(event)
    rate_limit_msg = consume_guess_answer_slot(uid_key)
    if rate_limit_msg:
        await guess_rating_solve.finish(
            adapt_guess_outbound(rate_limit_msg, event=event),
            reply_message=resolve_reply_message(event, reply_message=True),
        )

    name = get_sender_display_name(event)
    billing = billing_user_id(event)
    msg = rating_guess.submit(gid, uid_key, name, billing, answer)
    if msg:
        bot = resolve_event_bot(event)
        reacted = await react_processing(bot, event, emoji_id=REACT_EMOJI_CHECK)
        if not reacted:
            await _guess_notify(
                guess_rating_start, event, msg, mention_sender=True
            )


@guess_rating_reset.handle()
async def _(event: MessageEvent):
    gid = get_event_group_id(event)
    if gid is None:
        return
    if not rating_guess.is_busy(gid):
        await guess_rating_reset.finish('当前没有进行中的猜Rating。', reply_message=True)
    rating_guess.end(gid)
    await guess_rating_reset.finish('猜Rating已重置。', reply_message=True)


@guess_rating_volunteer.handle()
async def _(event: MessageEvent):
    gid = get_event_group_id(event)
    if gid is None:
        return
    if not guess.is_enabled(gid):
        return
    billing = billing_user_id(event)
    rating_guess.add_volunteer(gid, billing)
    bot = resolve_event_bot(event)
    reacted = await react_processing(bot, event, emoji_id=REACT_EMOJI_CHECK)
    if reacted:
        await guess_rating_volunteer.finish()
    name = get_sender_display_name(event)
    await guess_rating_volunteer.finish(
        f'✅ {name} 已报名！下局猜Rating你被抽中的概率 ×5（10分钟内有效）',
        reply_message=True,
    )


# ─────────────────────── B50 找内鬼 ───────────────────────


@guess_impostor_start.handle()
async def _(event: MessageEvent):
    await _gate_guess_group_entry(guess_impostor_start, event)
    gid = get_event_group_id(event)
    if gid is None:
        await guess_impostor_start.finish('请在群内使用。', reply_message=True)
    if not guess.is_enabled(gid):
        await guess_impostor_start.finish(
            '该群已关闭猜歌功能，开启请输入 开启mai猜歌', reply_message=True,
        )
    if not await _reserve_game_session(gid, 'impostor'):
        await guess_impostor_start.finish(_GUESS_BUSY_HINT, reply_message=True)

    await _guess_notify(
        guess_impostor_start, event,
        '🕵️ 正在抽取B50并制作内鬼卡…', reply=True,
    )
    if not impostor_guess.lock(gid):
        _release_game_session(gid)
        await guess_impostor_start.finish(_GUESS_BUSY_HINT, reply_message=True)

    bot = resolve_event_bot(event)
    try:
        target = await pick_random_candidate(
            bot, gid, min_charts=IMPOSTOR_CARD_COUNT - 1, weighted=False,
        )
        if target is None:
            await guess_impostor_start.finish(
                f'未找到至少有 {IMPOSTOR_CARD_COUNT - 1} 张B50卡片的群友数据。',
                reply_message=True,
            )
        target_uid, target_name, target_b50 = target
        alien = await pick_random_candidate(
            bot, gid, min_charts=1, weighted=False,
            exclude_uids={target_uid},
        )
        if alien is None:
            await guess_impostor_start.finish(
                '未找到另一位群友作为“内鬼”成绩来源，暂无法开局。',
                reply_message=True,
            )
        alien_uid, alien_name, alien_b50 = alien
        try:
            charts, answer = build_impostor_cards(target_b50, alien_b50)
        except ValueError as e:
            log.warning(f'[GuessImpostor] 构建内鬼卡失败 gid={gid}: {e}')
            await guess_impostor_start.finish(
                f'内鬼卡构建失败：{e}', reply_message=True,
            )
    except Exception:
        impostor_guess.unlock(gid)
        raise

    data = impostor_guess.start(
        gid,
        target_uid=target_uid,
        target_name=target_name,
        alien_uid=alien_uid,
        alien_name=alien_name,
        answer=answer,
        charts=charts,
        duration=IMPOSTOR_DURATION,
    )

    from ..libraries.maimaidx_guess_impostor_image import impostor_image_segment

    intro = dedent(f'''\
        🕵️ B50找内鬼开始！
        5张卡片中有1张不属于题主，是别人的成绩混进来的。
        ⏱ {IMPOSTOR_DURATION}秒内发送 1～5 作答，可修改。
        答对按速度获得积分与BREAK；题主和内鬼本人不参与奖励。
    ''')
    try:
        image_seg = await run_image_cpu(impostor_image_segment, charts)
        await _safe_matcher_send(
            guess_impostor_start,
            event,
            MessageSegment.text(intro + '\n') + image_seg,
            gid,
            media=True,
            fatal=False,
        )
    except Exception as e:
        impostor_guess.end(gid)
        log.warning(f'[GuessImpostor] 生成开局图失败 gid={gid}: {e}')
        await guess_impostor_start.finish(
            'B50找内鬼图片生成失败，本局已结束。', reply_message=True,
        )

    remaining = data.duration
    while remaining > 0:
        await asyncio.sleep(1)
        current = impostor_guess.get(gid)
        if current is not data or current.end:
            return
        remaining -= 1
        if remaining in (30, 10):
            await _guess_notify(
                guess_impostor_start, event, f'⏳ 找内鬼还剩 {remaining}秒！',
            )

    if impostor_guess.get(gid) is not data:
        return
    settlement = impostor_guess.settle(gid)
    if settlement is None:
        return
    # settle() 已经冻结了本局数据；先释放群状态，避免奖励或渲染异常把游戏卡成 busy。
    impostor_guess.end(gid, expected=data)

    from ..libraries.maimaidx_break import break_db

    for reward in settlement.rewards:
        try:
            await guess_score.award_fixed_points(
                gid,
                reward.uid,
                reward.name,
                reward.score,
                mode=guess_score.MODE_IMPOSTOR,
            )
        except Exception as exc:
            log.exception(
                f'[GuessImpostor] 积分结算失败，继续发送结果 '
                f'gid={gid} uid={reward.uid}: {type(exc).__name__}: {exc}'
            )
        if reward.break_points > 0:
            try:
                award = await asyncio.to_thread(
                    break_db.award_game_break,
                    reward.billing_id, 'impostor', reward.break_points,
                    'b50_impostor_settlement',
                    meta={
                        'group_id': str(gid),
                        'target_uid': settlement.target_uid,
                        'answer': settlement.answer,
                        'rank': reward.rank,
                    },
                )
                reward.break_points = award.awarded
                reward.break_capped = award.capped
            except Exception as exc:
                log.exception(
                    f'[GuessImpostor] BREAK 结算失败，继续发送结果 '
                    f'gid={gid} uid={reward.uid}: {type(exc).__name__}: {exc}'
                )

    result_lines = [
        '🎉 B50找内鬼结束！',
        f'🕵️ 内鬼是第 {settlement.answer} 张：该成绩来自 {settlement.alien_name} 的B50',
        f'📚 本局数据来自 {settlement.target_name} 的B50',
        '',
        '🏆 找对排名：',
        format_impostor_rewards(settlement.rewards),
    ]
    if settlement.wrong_names:
        result_lines.append(f'未找对：{len(settlement.wrong_names)}人')
    result_text = '\n'.join(result_lines)

    try:
        reveal_seg = await run_image_cpu(
            impostor_image_segment,
            charts,
            reveal_index=settlement.answer,
        )
        await _safe_matcher_send(
            guess_impostor_start,
            event,
            MessageSegment.text(result_text + '\n') + reveal_seg,
            gid,
            media=True,
            fatal=False,
        )
    except Exception as e:
        log.warning(f'[GuessImpostor] 生成揭晓图失败 gid={gid}: {e}')
        await _safe_matcher_send(
            guess_impostor_start, event, result_text, gid, fatal=False,
        )

    await _send_guess_shortcuts(guess_impostor_start, event, gid)
    await guess_impostor_start.finish()


@guess_impostor_solve.handle()
async def _(event: MessageEvent):
    gid = get_event_group_id(event)
    if gid is None:
        return
    data = impostor_guess.get(gid)
    if data is None or data.end:
        return
    text = event.get_plaintext().strip()
    if text not in {'1', '2', '3', '4', '5'}:
        return

    uid_key = platform_user_id(event)
    rate_limit_msg = consume_guess_answer_slot(uid_key)
    if rate_limit_msg:
        await guess_impostor_solve.finish(
            adapt_guess_outbound(rate_limit_msg, event=event),
            reply_message=resolve_reply_message(event, reply_message=True),
        )

    msg = impostor_guess.submit(
        gid,
        uid_key,
        get_sender_display_name(event),
        billing_user_id(event),
        int(text),
    )
    if msg:
        bot = resolve_event_bot(event)
        reacted = await react_processing(bot, event, emoji_id=REACT_EMOJI_CHECK)
        if not reacted:
            await _guess_notify(
                guess_impostor_start, event, msg, mention_sender=True
            )


@guess_impostor_reset.handle()
async def _(event: MessageEvent):
    gid = get_event_group_id(event)
    if gid is None:
        return
    if not impostor_guess.is_busy(gid):
        await guess_impostor_reset.finish(
            '当前没有进行中的B50找内鬼。', reply_message=True,
        )
    impostor_guess.end(gid)
    await guess_impostor_reset.finish('B50找内鬼已重置。', reply_message=True)


# ─────────────────────── 舞萌极限二选一 ───────────────────────


def _duel_choice_from_text(text: str) -> Optional[int]:
    s = text.strip()
    if s in {'左', 'l', 'L', '1'}:
        return 1
    if s in {'右', 'r', 'R', '2'}:
        return 2
    return None


def _format_duel_round_summary(round_obj) -> str:
    return (
        f'📊 第 {round_obj.round_no} 轮 · {round_obj.prompt}\n'
        f'左：{round_obj.left.title} [{round_obj.left.level}] · '
        f'右：{round_obj.right.title} [{round_obj.right.level}]'
    )


async def _duel_intro_text() -> str:
    return dedent(f'''\
        ⚔️ 舞萌极限二选一开始！
        每轮投放一张对比图和一个问题，发送 左/右 作答。
        答错或超时即淘汰，坚持到最后者获胜。
        规则要点：
        · 5 轮累计积分：{"/".join(str(s) for s in DUEL_ROUND_SCORES)} = {sum(DUEL_ROUND_SCORES)} 分
        · 全通关前三额外 BREAK 奖励：2 / 1 / 0
        · 首轮开始后中途参赛需发送「加入」，第二轮后禁止加入
        · 作答不可修改；每轮仅可答一次
    ''')


async def _duel_send_round_prompt(
    event: MessageEvent,
    gid: int,
    round_obj,
    *,
    r_idx: int,
    total: int,
    fatal: bool,
) -> None:
    """按轮投放：题图 + 提问一起发，避免开局甩多图还要往上翻。"""
    text = (
        f'⏱ 第 {r_idx}/{total} 轮开始！\n'
        f'{round_obj.prompt}\n'
        f'请在 {DUEL_ROUND_DURATION} 秒内发送「左」或「右」。'
    )
    try:
        seg = await run_image_cpu(duel_image_segment, round_obj, reveal=False)
        await _safe_matcher_send(
            guess_duel_start,
            event,
            MessageSegment.text(text + '\n') + seg,
            gid,
            media=True,
            fatal=fatal,
        )
    except Exception:
        if fatal:
            raise
        await _safe_matcher_send(
            guess_duel_start, event, text, gid, fatal=False,
        )


@guess_duel_start.handle()
async def _(event: MessageEvent):
    await _gate_guess_group_entry(guess_duel_start, event)
    gid = get_event_group_id(event)
    if gid is None:
        await guess_duel_start.finish('请在群内使用。', reply_message=True)
    if not guess.is_enabled(gid):
        await guess_duel_start.finish(
            '该群已关闭猜歌功能，开启请输入 开启mai猜歌', reply_message=True,
        )
    if not await _reserve_game_session(gid, 'duel'):
        await guess_duel_start.finish(_GUESS_BUSY_HINT, reply_message=True)

    if not duel_guess.lock(gid):
        _release_game_session(gid)
        await guess_duel_start.finish(_GUESS_BUSY_HINT, reply_message=True)

    try:
        rounds = await asyncio.to_thread(build_duel_rounds)
    except Exception as e:
        duel_guess.unlock(gid)
        log.warning(f'[Duel] 出题异常 gid={gid}: {type(e).__name__}: {e}')
        await guess_duel_start.finish(
            '出题失败，本局未开始。', reply_message=True,
        )
    if not rounds or len(rounds) < DUEL_ROUNDS:
        duel_guess.unlock(gid)
        await guess_duel_start.finish(
            '出题失败：可用谱面不足，请稍后再试。', reply_message=True,
        )

    data = duel_guess.start(
        gid,
        rounds=rounds,
        duration=DUEL_ROUND_DURATION,
    )

    try:
        await _guess_notify(
            guess_duel_start, event, await _duel_intro_text(), reply=True,
        )
        data.current_round = 1
        data.start_round_at = time.time()
        await _duel_send_round_prompt(
            event, gid, data.rounds[0],
            r_idx=1, total=len(data.rounds), fatal=True,
        )
    except Exception as e:
        log.warning(f'[Duel] 首轮发题失败 gid={gid}: {type(e).__name__}: {e}')
        duel_guess.end(gid)
        await guess_duel_start.finish(
            '题目图片生成失败，本局已结束。', reply_message=True,
        )

    try:
        for r_idx in range(1, len(data.rounds) + 1):
            remaining = data.round_durations
            while remaining > 0:
                current = duel_guess.get(gid)
                if current is not data or current.end:
                    return
                await asyncio.sleep(1)
                remaining -= 1
                if remaining in (10,) and remaining > 0:
                    await _guess_notify(
                        guess_duel_start, event,
                        f'⏳ 第 {r_idx} 轮还剩 {remaining} 秒',
                    )

            current = duel_guess.get(gid)
            if current is not data or current.end:
                return
            # 结算本轮
            eliminated, survivors, all_clear = duel_guess.settle_round(gid)
            round_obj = data.rounds[r_idx - 1]
            correct_side = '左' if round_obj.answer == 1 else '右'
            head = f'🏁 第 {r_idx} 轮结束！正确答案是「{correct_side}」'
            lines = [head]
            if survivors:
                lines.append(
                    f'✅ 晋级 {len(survivors)} 人（累计 {sum(DUEL_ROUND_SCORES[:r_idx])} 分）'
                )
            if eliminated:
                lines.append(
                    f'❌ 出局 {len(eliminated)} 人'
                    f'（保留前 {r_idx - 1} 轮积分）'
                )
            try:
                reveal_seg = await run_image_cpu(
                    duel_image_segment, round_obj, reveal=True,
                )
                await _safe_matcher_send(
                    guess_duel_start, event,
                    MessageSegment.text('\n'.join(lines) + '\n') + reveal_seg,
                    gid, media=True, fatal=False,
                )
            except Exception as e:
                log.warning(f'[Duel] 揭晓图生成失败 gid={gid}: {e}')
                await _safe_matcher_send(
                    guess_duel_start, event, '\n'.join(lines), gid, fatal=False,
                )

            # 第二轮起锁定中途参赛
            if r_idx == 1:
                duel_guess.lock_after_first_round(gid)

            if r_idx >= len(data.rounds) or not survivors:
                break

            # 进入下一轮：再发当轮题图
            data.current_round = r_idx + 1
            data.start_round_at = time.time()
            await _duel_send_round_prompt(
                event, gid, data.rounds[r_idx],
                r_idx=r_idx + 1, total=len(data.rounds), fatal=False,
            )

        # 最终结算
        settlement = duel_guess.settle_final(gid)
        if settlement is None:
            return

        from ..libraries.maimaidx_break import break_db

        survivors_uids = set(settlement.survivors)
        actual_bp = {}
        capped_uids = set()
        for p in data.participants.values():
            if p.final_score <= 0:
                continue
            added = p.final_score
            await guess_score.award_fixed_points(
                gid,
                p.uid,
                p.name,
                added,
                mode=guess_score.MODE_DUEL,
            )
            if p.uid in survivors_uids and p.finish_rank >= 1:
                bp = 0
                if p.finish_rank == 1:
                    bp = 2
                elif p.finish_rank == 2:
                    bp = 1
                if bp > 0:
                    award = await asyncio.to_thread(
                        break_db.award_game_break,
                        p.billing_id, 'duel', bp, 'duel_all_clear_bonus',
                        meta={
                            'group_id': str(gid),
                            'rank': p.finish_rank,
                            'rounds': len(data.rounds),
                        },
                    )
                    actual_bp[p.uid] = award.awarded
                    if award.capped:
                        capped_uids.add(p.uid)

        # 结算文案
        result_lines = [
            '🎉 舞萌极限二选一结束！',
            f'参与人数 {len(data.participants)}；'
            f'晋级到最后 {len(settlement.survivors)} 人；'
            f'中途淘汰 {settlement.eliminated_count} 人',
            '',
        ]
        # 全部通关排名
        if settlement.rewards:
            result_lines.append('🏆 全通关排名：')
            for uid, name, rank, score, bp in settlement.rewards:
                medal = {1: '🥇', 2: '🥈', 3: '🥉'}.get(rank, '▫️')
                actual = actual_bp.get(uid, bp)
                if actual > 0:
                    bp_part = f' +{actual}BREAK'
                elif uid in capped_uids:
                    bp_part = ' +0 BREAK'
                else:
                    bp_part = ''
                result_lines.append(
                    f'{medal} #{rank} {name}  +{score}分{bp_part}'
                )
        else:
            result_lines.append('本局无人全通关 😶')

        # 中途淘汰者积分
        eliminated_lines: List[str] = []
        for p in sorted(
            (p for p in data.participants.values() if p.eliminated_round),
            key=lambda p: (-p.final_score, p.eliminated_round),
        ):
            eliminated_lines.append(
                f'· {p.name}：通过 {p.eliminated_round - 1} 轮，'
                f'保留 {p.final_score} 分'
            )
        if eliminated_lines:
            result_lines.append('')
            result_lines.append('🛡 参与奖：')
            result_lines.extend(eliminated_lines[:8])
            if len(eliminated_lines) > 8:
                result_lines.append(f'… 及其他 {len(eliminated_lines) - 8} 人')

        await _safe_matcher_send(
            guess_duel_start, event,
            '\n'.join(result_lines), gid, fatal=False,
        )
    finally:
        duel_guess.end(gid, expected=data)
    await _send_guess_shortcuts(guess_duel_start, event, gid)
    await guess_duel_start.finish()


@guess_duel_join.handle()
async def _(event: MessageEvent):
    gid = get_event_group_id(event)
    if gid is None:
        return
    if not duel_guess.is_busy(gid):
        return
    uid = platform_user_id(event)
    ok, msg = duel_guess.join(
        gid, uid,
        get_sender_display_name(event),
        billing_user_id(event),
    )
    if ok:
        data = duel_guess.get(gid)
        count = len(data.participants) if data else 0
        await _guess_notify(
            guess_duel_join, event,
            f'✅ 已加入本局（共 {count} 人）。本轮结束前均可加入。',
        )
    elif msg:
        await _guess_notify(
            guess_duel_join, event, msg,
        )


@guess_duel_solve.handle()
async def _(event: MessageEvent):
    gid = get_event_group_id(event)
    if gid is None:
        return
    data = duel_guess.get(gid)
    if data is None or data.end or data.current_round == 0:
        return
    text = event.get_plaintext().strip()
    choice = _duel_choice_from_text(text)
    if choice is None:
        return

    uid_key = platform_user_id(event)
    rate_limit_msg = consume_guess_answer_slot(uid_key)
    if rate_limit_msg:
        await guess_duel_solve.finish(
            adapt_guess_outbound(rate_limit_msg, event=event),
            reply_message=resolve_reply_message(event, reply_message=True),
        )

    accepted, msg, _ = duel_guess.submit(
        gid, uid_key, choice,
        name=get_sender_display_name(event),
        billing_id=billing_user_id(event),
    )
    if accepted:
        bot = resolve_event_bot(event)
        reacted = await react_processing(bot, event, emoji_id=REACT_EMOJI_CHECK)
        if not reacted:
            label = '左' if choice == 1 else '右'
            await _guess_notify(
                guess_duel_solve, event, f'✅ 已选择「{label}」',
                mention_sender=True,
            )
    elif msg:
        await _guess_notify(
            guess_duel_solve, event, msg, mention_sender=True
        )


@guess_duel_reset.handle()
async def _(event: MessageEvent):
    gid = get_event_group_id(event)
    if gid is None:
        return
    if not duel_guess.is_busy(gid):
        await guess_duel_reset.finish(
            '当前没有进行中的舞萌极限二选一。', reply_message=True,
        )
    duel_guess.end(gid)
    await guess_duel_reset.finish('舞萌极限二选一已重置。', reply_message=True)


from ..libraries import maimaidx_guess_scheduler  # noqa: F401


# ───────────────────── 你想我猜（20 问猜曲） ─────────────────────


def _twentyq_intro() -> str:
    return dedent(f'''\
        🐱 你想我猜开始！Milk 心里想了一首舞萌曲目～
        提问用「我问」前缀，猜曲名用「我猜」前缀，共 {TWENTYQ_MAX_QUESTIONS} 次提问。
        问问题阶段不限时；问完后进入猜曲阶段，限时 {TWENTYQ_GUESS_WINDOW} 秒。
        可问：分类 / BPM / 定数 / 版本 / 是否 DX 谱面 / 谱师 / 艺术家 / 标题特征。
        猜错不结束，超时公布答案。输入「查看已有信息」可看本局问答记录，输入「重置你想我猜」可结束本局。
    ''')


@guess_20q_start.handle()
async def _(event: MessageEvent):
    await _gate_guess_group_entry(guess_20q_start, event)
    gid = get_event_group_id(event)
    if gid is None:
        await guess_20q_start.finish('请在群内使用。', reply_message=True)
    if not guess.is_enabled(gid):
        await guess_20q_start.finish(
            '该群已关闭猜歌功能，开启请输入 开启mai猜歌', reply_message=True,
        )
    if not await _reserve_game_session(gid, '20q'):
        await guess_20q_start.finish(_GUESS_BUSY_HINT, reply_message=True)

    if not twentyq_guess.lock(gid):
        _release_game_session(gid)
        await guess_20q_start.finish(_GUESS_BUSY_HINT, reply_message=True)

    try:
        data = twentyq_guess.start(gid, duration=TWENTYQ_DURATION)
    except Exception:
        twentyq_guess.unlock(gid)
        raise

    try:
        await _guess_notify(guess_20q_start, event, _twentyq_intro(), reply=True)

        # ── 阶段1：问问题阶段，不限总时长；空闲超过 TWENTYQ_IDLE_TIMEOUT 才兜底结束 ──
        idle_timeout_hit = False
        while data.question_count < data.max_questions:
            await asyncio.sleep(1)
            current = twentyq_guess.get(gid)
            # 游戏被重置/替换：无状态可结算，直接退出。
            if current is not data:
                return
            # 有人猜对（data.end=True）：跳出循环走后续结算，不能 return。
            if current.end:
                break
            if data.idle_seconds() >= TWENTYQ_IDLE_TIMEOUT:
                idle_timeout_hit = True
                break

        current = twentyq_guess.get(gid)
        # 仅当游戏被重置/替换时才退出；猜对导致的 end 要继续走结算。
        if current is not data:
            return

        # ── 阶段2：猜测阶段（正常问完进入；空闲超时/猜对跳过直接收尾）──
        if not idle_timeout_hit and not data.end and data.question_count >= data.max_questions:
            await _guess_notify(
                guess_20q_start, event,
                f'📝 提问次数用完啦！进入猜曲阶段，限时 {TWENTYQ_GUESS_WINDOW} 秒，'
                f'用「我猜 曲名」抢猜～',
            )
            remaining = TWENTYQ_GUESS_WINDOW
            while remaining > 0:
                await asyncio.sleep(1)
                current = twentyq_guess.get(gid)
                # 游戏被重置/替换：无状态可结算，直接退出。
                if current is not data:
                    return
                # 有人猜对：跳出循环走后续结算，不能 return。
                if current.end:
                    break
                remaining -= 1
                if remaining in TWENTYQ_COUNTDOWN:
                    await _guess_notify(
                        guess_20q_start, event,
                        f'⏳ 猜曲阶段还剩 {remaining} 秒！',
                    )

        current = twentyq_guess.get(gid)
        if current is not data:
            return

        if data.winner_uid:
            uid = data.winner_uid
            name = data.winner_name or '群友'
            # 先提示「正在结算」再做积分计算，确保该提示在结算结果消息之前发出。
            await _guess_notify(
                guess_20q_start, event,
                f'⏳ 正在结算 {name} 的本局贡献…',
            )
            raw_base = twentyq_base_points(data.question_count)
            multiplier = 1
            multiplier_tags: List[str] = []
            if await guess_boost_card.consume_one(gid, uid):
                multiplier *= 2
                multiplier_tags.append('限时加倍卡×2')
            (
                added, _raw, combo, _streak, total, rank, period_snapshot,
            ) = await guess_score.award_correct_guess(
                gid, uid, name, raw_base, multiplier,
                mode=guess_score.MODE_20Q,
            )
            settlement = guess_score.format_settlement_lines(
                added, raw_base, combo, multiplier, _streak, total, rank,
                period_snapshot, multiplier_tags,
            )
            from ..libraries.maimaidx_break import break_db

            reward = await asyncio.to_thread(
                break_db.award_guess_points,
                data.winner_billing, added, group_id=str(gid),
                game='twentyq',
            )
            break_part = ''
            if reward.break_added > 0:
                double_tag = ''
                if reward.doubled:
                    from ..libraries.maimaidx_card import format_duration
                    double_tag = (
                        f'（双倍BREAK卡生效中，剩余 {format_duration(reward.double_remaining)}）'
                    )
                break_part = (
                    f'\n💳 猜对奖励 +{reward.break_added} BREAK'
                    f'（余额 {reward.balance}）{double_tag}'
                )
            elif reward.capped:
                break_part = '\n💳 猜对奖励 +0 BREAK'
            log.info(
                f'[Guess20Q] 猜对结束 gid={gid} answer={data.music.title} '
                f'id={data.music.id} winner={name}({uid}) '
                f'questions={data.question_count}/{data.max_questions}'
            )
            result = (
                f'🎉 恭喜 {name} 猜对啦！全场共提问 {data.question_count} 次。\n'
                f'{twentyq_guess.reveal_text(data)}\n\n{settlement}{break_part}'
            )
            await _safe_matcher_send(
                guess_20q_start, event, result, gid, fatal=False,
            )
        else:
            await guess_score.reset_all_streaks(gid)
            log.info(
                f'[Guess20Q] 超时结束 gid={gid} answer={data.music.title} '
                f'id={data.music.id} questions={data.question_count}/{data.max_questions} '
                f'无人猜对'
            )
            result = (
                '⏰ 时间到（或提问机会已用完），没有人猜出来喵～\n'
                f'{twentyq_guess.reveal_text(data)}'
            )
            await _safe_matcher_send(
                guess_20q_start, event, result, gid, fatal=False,
            )
    finally:
        # 确保结算阶段异常时释放群状态；end 内部有 expected 身份校验，重复调用安全。
        twentyq_guess.end(gid, expected=data)
    await _send_guess_shortcuts(guess_20q_start, event, gid)
    await guess_20q_start.finish()


# 你想我猜（20 问）测试期提示：每隔几次提问追加一次，结算/猜对/失败等关键节点始终追加。
# 避免每条回复都带提示造成刷屏，同时保证玩家有反馈通道。
_TWENTYQ_TEST_NOTICE = '\n\n— 测试版本，如遇数据/语义错误请联系管理员反馈。'
_TWENTYQ_NOTICE_EVERY_N = 5  # 提问回复每 N 条带一次提示


def _twentyq_notice_for_question(used: int) -> str:
    """提问回复的提示：每 N 次提问带一次。used 为已用提问次数（从 1 起）。"""
    return _TWENTYQ_TEST_NOTICE if used % _TWENTYQ_NOTICE_EVERY_N == 0 else ''


@guess_20q_solve.handle()
async def _(event: MessageEvent):
    gid = get_event_group_id(event)
    if gid is None:
        return
    data = twentyq_guess.get(gid)
    if data is None or data.end:
        return

    text = event.get_plaintext().strip()
    if not text:
        return

    uid_key = platform_user_id(event)

    result = await twentyq_guess.process_message(
        gid, uid_key, get_sender_display_name(event), text,
        billing_id=billing_user_id(event),
    )
    kind = result.get('kind')

    if kind == 'win':
        # 即时反馈：给原消息加 ✅；react 不可用时回退到简短文本。
        # 「正在结算…」提示统一改由 guess_20q_start 结算流程开头发出，
        # 确保它在结算结果之前——此前由这里发送，但与结算循环并发，
        # react_processing 耗时会让「正在结算」反而排在结算结果之后。
        bot = resolve_event_bot(event)
        reacted = await react_processing(bot, event, emoji_id=REACT_EMOJI_CHECK)
        if not reacted:
            await _guess_notify(
                guess_20q_solve, event,
                f'✅ {get_sender_display_name(event)} 猜对了！{_TWENTYQ_TEST_NOTICE}',
                mention_sender=True,
            )
        return

    if kind == 'wrong_guess':
        # 限流只作用于真正的猜答案尝试（猜错）。
        # 「我问 是非题」和普通聊天完全不限流；
        # 自然聊天里「我猜今天会下雨」也会走到这里，但只占一次名额，可接受。
        rate_limit_msg = consume_guess_answer_slot(uid_key)
        if rate_limit_msg:
            await guess_20q_solve.finish(
                adapt_guess_outbound(rate_limit_msg, event=event),
                reply_message=resolve_reply_message(event, reply_message=True),
            )
        # 猜错不结束游戏，让其他人继续猜，直到超时公布答案。
        guess_text = result.get('guess', '')
        hint = f'「{guess_text}」' if guess_text else ''
        await _guess_notify(
            guess_20q_solve, event,
            f'❌ {hint}不对哦，继续猜～{_TWENTYQ_TEST_NOTICE}',
            mention_sender=True,
        )
        return

    if kind == 'busy':
        await _guess_notify(
            guess_20q_solve, event,
            f'喵～上一个问题还在确认中（可能正在问 AI），请稍等一下再提问哦～{_TWENTYQ_TEST_NOTICE}',
            mention_sender=True,
        )
        return

    if kind == 'question':
        suffix = f'\n（还可提问 {result["remaining"]} 次）'
        if result.get('last'):
            suffix = '\n⚠️ 提问次数用完啦！接下来只能用「我猜 曲名」抢猜。'
        # 提问回复：每 N 次带一次测试提示，避免刷屏
        notice = _twentyq_notice_for_question(result.get('used', 0))
        await _guess_notify(
            guess_20q_solve, event,
            f'{result["answer"]}{suffix}{notice}',
            mention_sender=True,
        )
        return

    if kind == 'no_questions':
        await _guess_notify(
            guess_20q_solve, event,
            f'提问次数用完啦，用「我猜 曲名」抢猜吧～{_TWENTYQ_TEST_NOTICE}',
            mention_sender=True,
        )
        return

    if kind == 'failed':
        await _guess_notify(
            guess_20q_solve, event,
            f'唔…这不是 Milk 心里想的那首喵，很遗憾本局结束。{_TWENTYQ_TEST_NOTICE}',
            mention_sender=True,
        )
        return

    if kind == 'unknown':
        await _guess_notify(
            guess_20q_solve, event,
            f'{result["answer"]}{_TWENTYQ_TEST_NOTICE}',
            mention_sender=True,
        )


@guess_20q_reset.handle()
async def _(event: MessageEvent):
    gid = get_event_group_id(event)
    if gid is None:
        return
    if not twentyq_guess.is_busy(gid):
        await guess_20q_reset.finish(
            '当前没有进行中的你想我猜。', reply_message=True,
        )
    data = twentyq_guess.get(gid)
    answer = twentyq_guess.reveal_text(data) if data else ''
    if data:
        log.info(
            f'[Guess20Q] 管理员重置 gid={gid} answer={data.music.title} '
            f'id={data.music.id} questions={data.question_count}/{data.max_questions}'
        )
    twentyq_guess.end(gid)
    msg = '你想我猜已重置。'
    if answer:
        msg += f'\n{answer}'
    await guess_20q_reset.finish(msg, reply_message=True)


@guess_20q_list.handle()
async def _(event: MessageEvent):
    gid = get_event_group_id(event)
    if gid is None:
        return
    data = twentyq_guess.get(gid)
    if data is None or data.end:
        await guess_20q_list.finish(
            '当前没有进行中的你想我猜。', reply_message=True,
        )
    lines = [f'📋 本局已确认信息（{data.question_count}/{data.max_questions} 次提问）：']
    if not data.qa:
        lines.append('（还没有提问记录，用「我问 是非题」开始提问吧～）')
    else:
        for i, qa in enumerate(data.qa, 1):
            name = qa.name or '群友'
            info = _qa_display_info(qa)
            lines.append(f'{i}. [{name}] {info}\n   → {qa.answer}')
    remaining_q = data.remaining()
    if remaining_q > 0:
        lines.append(f'\n还剩 {remaining_q} 次提问机会，猜曲名用「我猜 曲名」。')
    else:
        lines.append('\n提问次数已用完，只能用「我猜 曲名」抢猜。')
    await guess_20q_list.finish('\n'.join(lines), reply_message=True)
