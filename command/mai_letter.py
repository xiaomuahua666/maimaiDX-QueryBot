"""舞萌开字母命令。"""

from __future__ import annotations

import asyncio
import re

from nonebot import on_command, on_message
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent, MessageSegment
from nonebot.params import CommandArg
from nonebot.rule import Rule

from ..config import log
from ..libraries.maimaidx_guess_letter import (
    BOARD_SIZE,
    LetterBoard,
    LetterSettlement,
    _is_maskable,
    _norm_token,
    board_image_segment,
    combo_solved_count,
    format_board_text,
    format_combo_tip,
    format_elapsed,
    format_finish_elapsed_line,
    letter_guess,
    letter_triple_banner,
)
from ..libraries.maimaidx_guess_score import guess_score
from ..libraries.maimaidx_guess_rating import rating_guess
from ..libraries.maimaidx_guess_impostor import impostor_guess
from ..libraries.maimaidx_guess_duel import duel_guess
from ..libraries.maimaidx_game_session import game_session_gate
from ..libraries.maimaidx_image_executor import run_image_cpu
from ..libraries.maimaidx_guess_sync import MAIN_GROUP_REDIRECT, guess_sync
from ..libraries.maimaidx_letter_rank_draw import (
    image_b64,
    render_contrib_board,
    render_score_board,
    render_settlement_split,
    render_time_board,
)
from ..libraries.maimaidx_letter_stats import letter_stats
from ..libraries.maimaidx_music import guess
from ..libraries.maimaidx_platform import (
    adapt_guess_outbound,
    billing_user_id,
    build_command_keyboard_message,
    ensure_sender_mention,
    get_event_group_id,
    get_sender_display_name,
    is_group_message_event,
    platform_user_id,
    resolve_reply_message,
    use_qq_mode,
)

_LETTER_SHORTCUTS = (
    ('再来开字母', '开字母'),
    ('猜歌', '猜歌'),
    ('猜封面', '猜封面'),
    ('猜曲子', '猜曲子'),
    ('猜谱面', '猜谱面'),
    ('猜 Rating', '猜rating'),
    ('B50 找内鬼', '找内鬼'),
    ('极限二选一', '极限二选一'),
    ('你想我猜', '你想我猜'),
    ('开字母排行', '开字母排行'),
)

_RESERVED_PREFIXES = (
    "开字母",
    "开歌",
    "不玩了",
    "结束开字母",
    "舞萌开字母",
    "开字母看板",
    "开字母排行",
    "开字母积分榜",
    "开字母贡献榜",
    "开字母时间榜",
    "开字母文字模式",
    "开字母图片模式",
    "开字母自动模式",
    "文字模式",
    "图片模式",
    "纯文字模式",
    "文本模式",
    "图文模式",
    "切换文字模式",
    "切换图片模式",
    "自动模式",
    "自适应模式",
    "恢复自动",
    "重置猜歌",
    "猜歌",
    "猜曲绘",
    "猜曲子",
    "猜铺面",
    "猜谱面",
    "猜Rating",
    "猜rating",
    "找内鬼",
    "找假卡",
    "重置找内鬼",
)


def _is_group_message(event) -> bool:
    return is_group_message_event(event)


def _is_letter_playing(event) -> bool:
    gid = get_event_group_id(event)
    return gid is not None and letter_guess.is_playing(gid)


GROUP_MESSAGE = Rule(_is_group_message)
LETTER_PLAYING = Rule(_is_group_message) & Rule(_is_letter_playing)

letter_start = on_command(
    "舞萌开字母", aliases={"开字母看板"}, rule=GROUP_MESSAGE, priority=5, block=True
)
letter_open = on_command("开字母", rule=GROUP_MESSAGE, priority=5, block=True)
letter_song = on_command("开歌", rule=LETTER_PLAYING, priority=5, block=True)
letter_quit = on_command(
    "不玩了", aliases={"结束开字母"}, rule=LETTER_PLAYING, priority=5, block=True
)
# priority 高于「开字母」，避免被拆成 开字母+参数
letter_score_cmd = on_command(
    "开字母排行", aliases={"开字母积分榜"}, rule=GROUP_MESSAGE, priority=4, block=True
)
letter_contrib_cmd = on_command(
    "开字母贡献榜", rule=GROUP_MESSAGE, priority=4, block=True
)
letter_time_cmd = on_command(
    "开字母时间榜", rule=GROUP_MESSAGE, priority=4, block=True
)
# priority 高于「开字母」，避免被拆成 开字母+参数；rule 用 GROUP_MESSAGE 而非 LETTER_PLAYING，
# 确保无游戏时也能给出提示，而不是静默无响应。
letter_text_mode_cmd = on_command(
    "开字母文字模式", aliases={"文字模式", "纯文字模式", "文本模式", "切换文字模式"}, rule=GROUP_MESSAGE, priority=4, block=True
)
letter_image_mode_cmd = on_command(
    "开字母图片模式", aliases={"图片模式", "图文模式", "切换图片模式"}, rule=GROUP_MESSAGE, priority=4, block=True
)
letter_auto_mode_cmd = on_command(
    "开字母自动模式", aliases={"自动模式", "自适应模式", "恢复自动"}, rule=GROUP_MESSAGE, priority=4, block=True
)
# 对局中可直接发字母 / 别名，无需命令前缀
letter_quick = on_message(rule=LETTER_PLAYING, priority=9, block=False)

# 开字母玩法不参与高峰期「额外 1 BREAK」附加费（含局内开字母/开歌/答题）。
for _letter_matcher in (
    letter_start,
    letter_open,
    letter_song,
    letter_quit,
    letter_score_cmd,
    letter_contrib_cmd,
    letter_time_cmd,
    letter_text_mode_cmd,
    letter_image_mode_cmd,
    letter_auto_mode_cmd,
    letter_quick,
):
    setattr(_letter_matcher, "_maimaidx_busy_surcharge_exempt", True)

_HELP = (
    "【舞萌开字母】\n"
    f"· 发送「舞萌开字母」或「开字母」开局（{BOARD_SIZE} 首歌）\n"
    "· 对局中直接发字母（如 m）即可开字符\n"
    "· 直接发曲名/别名即可猜歌（也可「开歌 xxx」）\n"
    "· 局内不计分；全部解开后按用时星级 + 贡献结算积分/BREAK\n"
    "· 星级阈值自适应：默认 ≤30/45/60/90/180 秒；群通关变快后五星上限可降至最低 15 秒\n"
    "· 贡献：有效开字母×1、补齐曲×3、开歌×4；无贡献不得分\n"
    "· 开字母排行 / 开字母贡献榜 / 开字母时间榜 — 查看本群榜单图\n"
    "· 开字母图片模式 / 开字母文字模式 / 开字母自动模式 — 设置本群看板样式(持久生效)\n"
    "· 不玩了 — 结束并揭晓剩余（不发奖）\n"
    "与猜歌等模式同群互斥；需先「开启mai猜歌」。"
)


async def _payout_settlement(event: MessageEvent, gid, settlement: LetterSettlement) -> None:
    """通关结算发奖（文案已由短结算句展示，此处只落库）。"""
    from ..libraries.maimaidx_break import break_db

    for reward in settlement.rewards:
        if reward.score > 0:
            await guess_score.award_fixed_points(
                gid,
                reward.uid,
                reward.name,
                reward.score,
                mode=guess_score.MODE_LETTER,
            )
        if reward.break_points > 0:
            award = await asyncio.to_thread(
                break_db.award_game_break,
                reward.billing_id, "letter", reward.break_points, "letter_settlement",
                meta={
                    "group_id": str(gid),
                    "elapsed": settlement.elapsed,
                    "stars": settlement.stars,
                    "weight": reward.weight,
                },
            )
            # 写回实际到账额：结算图/分成图按真实发放显示；封顶时记 capped。
            reward.break_points = award.awarded
            reward.break_capped = award.capped


async def _send_board(matcher, event: MessageEvent, board, *, text: str = "") -> None:
    # 人多/突发：纯文字看板，跳过 PIL；不挂 reply 链以加快发送
    if board.prefer_text():
        body = format_board_text(board)
        msg = f"{text}{body}" if text else body
        payload = ensure_sender_mention(msg, event) if use_qq_mode(event) else msg
        await matcher.send(
            adapt_guess_outbound(payload, event=event),
            reply_message=False,
        )
        return
    msg = await run_image_cpu(board_image_segment, board)
    if text:
        msg = text + msg
    payload = ensure_sender_mention(msg, event) if use_qq_mode(event) else msg
    await matcher.send(
        adapt_guess_outbound(payload, event=event),
        reply_message=resolve_reply_message(event, reply_message=True),
    )


async def _send_plain(
    matcher, event: MessageEvent, text: str, *, fast: bool = False
) -> None:
    payload = ensure_sender_mention(text, event) if use_qq_mode(event) else text
    await matcher.send(
        adapt_guess_outbound(payload, event=event),
        reply_message=False
        if fast
        else resolve_reply_message(event, reply_message=True),
    )


async def _send_image(matcher, event: MessageEvent, im) -> None:
    message = MessageSegment.image(image_b64(im))
    if use_qq_mode(event):
        message = ensure_sender_mention(message, event)
    await matcher.send(
        adapt_guess_outbound(message, event=event),
        reply_message=False,
    )


def _ensure_enabled(gid) -> str | None:
    if not guess.is_enabled(gid):
        return "本群未开启猜歌功能，请管理员发送「开启mai猜歌」"
    return None


def _plain_guess_text(event: MessageEvent) -> str:
    text = event.get_plaintext().strip()
    text = re.sub(r"^@\S+\s*", "", text).strip()
    return text


def _is_reserved_command(text: str) -> bool:
    if not text:
        return True
    for prefix in _RESERVED_PREFIXES:
        if text == prefix or text.startswith(prefix + " ") or text.startswith(prefix):
            if text.startswith(prefix):
                return True
    return False


async def _maybe_finish_board(
    matcher, event: MessageEvent, gid, board: LetterBoard, parts: list[str]
) -> None:
    """
    通关结算：结束文案、达标/破纪录提示、分成榜图和终局看板合并发送。
    人多文字模式只影响局中看板更新；通关结算始终强制出图。
    「不玩了」中途结束不走本路径。
    """
    if board.finished:
        # 通关判定成功后立刻停表，不含后续落库/渲染耗时
        board.freeze_end()
    board.note_process()
    if not board.finished:
        await _send_board(matcher, event, board, text="\n".join(parts) + "\n")
        await matcher.finish()
        return

    # 保留相对上一局用时 diff，稍后与结算图、达标和纪录一起发送。
    prev_elapsed = letter_stats.last_clear_elapsed(gid)
    settlement_lines = [
        *parts,
        format_finish_elapsed_line(board.elapsed(), prev_elapsed),
    ]

    th = letter_stats.thresholds_for(gid)
    settlement = board.settle(
        limits=th.limits,
        adaptive=th.adaptive,
        sample_count=th.sample_count,
    )
    letter_guess.end(gid)
    feedback = await letter_stats.record_clear(
        gid,
        elapsed=settlement.elapsed,
        stars=settlement.stars,
        score_pool=settlement.score_pool,
        break_pool=settlement.break_pool,
        rewards=[
            (r.uid, r.billing_id, r.name, r.score, r.weight)
            for r in settlement.rewards
        ],
    )
    await _payout_settlement(event, gid, settlement)

    settlement_lines.extend(feedback.goal_tips)
    settlement_lines.extend(feedback.record_tips)

    # 通关结算始终出图（bypass prefer_text），全部内容只占一条群消息。
    msg = Message("\n".join(settlement_lines) + "\n")
    try:
        split_im = await render_settlement_split(settlement)
        msg += MessageSegment.image(await run_image_cpu(image_b64, split_im))
    except Exception as exc:
        log.warning(f"[LetterGuess] 结算分成图失败：{type(exc).__name__}: {exc}")
    msg += await run_image_cpu(board_image_segment, board)
    await matcher.send(
        adapt_guess_outbound(msg, event=event),
        reply_message=resolve_reply_message(event, reply_message=True),
    )
    shortcuts = build_command_keyboard_message(
        _LETTER_SHORTCUTS,
        event=event,
        title='🎮 再来一把',
        id_prefix='maimaidx-letter',
    )
    if shortcuts is not None:
        await matcher.send(
            shortcuts,
            reply_message=False,
        )
    await matcher.finish()


async def _letter_cooldown_block(matcher, event: MessageEvent, board: LetterBoard) -> bool:
    """
    开字母非高峰 2.5s/人冷却。高峰文字模式跳过。
    返回 True 表示已拦截（调用方应停止后续处理）。
    不调用猜歌的 consume_guess_answer_slot。
    """
    tip = board.try_consume_answer(platform_user_id(event))
    if tip is None:
        return False
    if tip:
        await matcher.send(
            adapt_guess_outbound(tip, event=event),
            reply_message=False,
        )
    return True


async def _apply_open_letter(matcher, event: MessageEvent, gid, raw: str) -> None:
    # 非高峰：本局 2.5s/人冷却；高峰文字模式不检查。不用猜歌全局限频。
    board = letter_guess.get(gid)
    assert board is not None
    key = raw.strip()[0]
    if not _is_maskable(key):
        await matcher.finish("只能开字母、数字或日文/汉字字符哦", reply_message=True)
    already = _norm_token(key) in board.revealed
    # 已开过的字母不走冷却提示，避免刷屏；仍调用 open_letter 以处理历史补齐
    if not already:
        if await _letter_cooldown_block(matcher, event, board):
            await matcher.finish()
            return
    solver = get_sender_display_name(event)
    msg, board, completed, _hidden_before = letter_guess.open_letter(
        gid,
        raw,
        solver=solver,
        uid=platform_user_id(event),
        billing_id=billing_user_id(event),
    )
    if already and not msg and not completed:
        # 字母已开过且无补齐：静默忽略，不发看板/文字
        await matcher.finish()
        return
    parts = [msg] if msg else []
    combo = format_combo_tip(combo_solved_count(completed=len(completed)))
    if combo:
        parts.append(combo)
    await _maybe_finish_board(matcher, event, gid, board, parts)


async def _apply_open_song(matcher, event: MessageEvent, gid, text: str) -> bool:
    """尝试开歌；猜中或冷却拦截返回 True，未中返回 False（由调用方决定是否提示）。"""
    # 开字母路径不用 consume_guess_answer_slot（猜歌等模式仍走全局限频）。
    board = letter_guess.get(gid)
    if board is None:
        return False
    if await _letter_cooldown_block(matcher, event, board):
        return True
    msg, board, song, completed, _hidden_before = letter_guess.open_song(
        gid,
        text,
        solver=get_sender_display_name(event),
        uid=platform_user_id(event),
        billing_id=billing_user_id(event),
    )
    if song is None:
        # 未中也记一次处理，便于突发负载及时切文字
        board.note_process()
        return False
    parts = [msg] if msg else []
    combo = format_combo_tip(
        combo_solved_count(completed=len(completed), song_opened=True)
    )
    if combo:
        parts.append(combo)
    await _maybe_finish_board(matcher, event, gid, board, parts)
    return True


@letter_start.handle()
@letter_open.handle()
async def _(matcher, event: MessageEvent, args: Message = CommandArg()):
    gid = get_event_group_id(event)
    if gid is None:
        return
    raw = args.extract_plain_text().strip()
    # 「开字母排行」等可能被拆成 开字母 + 参数，交给专用指令
    if raw in {"排行", "积分榜", "贡献榜", "时间榜"} or raw.startswith(
        ("排行", "积分榜", "贡献榜", "时间榜")
    ):
        matcher.skip()
    action, sync_msg = await guess_sync.prepare_group_entry(
        gid, platform_user_id(event)
    )
    if action == 'redirect':
        await matcher.finish(sync_msg or MAIN_GROUP_REDIRECT, reply_message=True)
    enabled_err = _ensure_enabled(gid)
    if enabled_err:
        await letter_open.finish(enabled_err, reply_message=True)

    if letter_guess.is_playing(gid):
        if not raw:
            board = letter_guess.get(gid)
            await _send_board(
                letter_open,
                event,
                board,
                text="当前开字母进行中。直接发字母或别名即可。\n",
            )
            await letter_open.finish()
        if len(raw) == 1 and _is_maskable(raw):
            await _apply_open_letter(letter_open, event, gid, raw)
        if await _apply_open_song(letter_open, event, gid, raw):
            return
        await letter_open.finish(
            "一次开一个字符（如：m / 开字母 m），或直接发曲名/别名猜歌。",
            reply_message=True,
        )

    if raw:
        if len(raw) == 1 and _is_maskable(raw):
            await letter_open.finish(
                "还没有开局。请先发送「舞萌开字母」或「开字母」。",
                reply_message=True,
            )
        await letter_open.finish(_HELP, reply_message=True)

    def _game_busy(group_id) -> bool:
        return (
            guess.is_busy(group_id)
            or letter_guess.is_playing(group_id)
            or rating_guess.is_busy(group_id)
            or impostor_guess.is_busy(group_id)
            or duel_guess.is_busy(group_id)
        )

    if not await game_session_gate.acquire(
        gid,
        mode='letter',
        busy_check=_game_busy,
    ):
        await letter_open.finish(
            "该群已有正在进行的猜歌/猜曲绘/猜曲子/猜铺面/猜Rating/B50找内鬼/极限二选一或开字母，请先结束当前游戏。",
            reply_message=True,
        )
    try:
        board = letter_guess.start(
            gid,
            starter=get_sender_display_name(event),
            count=BOARD_SIZE,
            display_mode=letter_stats.get_display_mode(gid),
        )
    except Exception as exc:
        game_session_gate.release(gid)
        log.warning(f"[LetterGuess] 开局失败：{type(exc).__name__}: {exc}")
        await letter_open.finish(f"开局失败：{exc}", reply_message=True)
    th = letter_stats.thresholds_for(gid)
    banner = letter_triple_banner()
    banner_line = f"{banner}\n" if banner else ""
    goals_line = letter_stats.daily_goals_line(gid)
    try:
        await _send_board(
            letter_open,
            event,
            board,
            text=(
                f"🎮 舞萌开字母开始！共 {len(board.songs)} 首歌。\n"
                f"{banner_line}"
                "直接发字母开字符，直接发别名/曲名猜歌。\n"
                "全部解开后按用时与贡献结算；局内不计分。\n"
                f"{th.format_lines()}\n"
                f"{goals_line}\n"
            ),
        )
    except BaseException:
        letter_guess.end(gid)
        raise
    await letter_open.finish()


@letter_song.handle()
async def _(event: MessageEvent, args: Message = CommandArg()):
    gid = get_event_group_id(event)
    if gid is None:
        return
    if not letter_guess.is_playing(gid):
        await letter_song.finish(
            "当前没有开字母对局。发送「舞萌开字母」开始。", reply_message=True
        )
    enabled_err = _ensure_enabled(gid)
    if enabled_err:
        await letter_song.finish(enabled_err, reply_message=True)

    text = args.extract_plain_text().strip()
    if not text:
        await letter_song.finish("请发送歌名或别名，或对局中直接发别名。", reply_message=True)
    if await _apply_open_song(letter_song, event, gid, text):
        return
    board = letter_guess.get(gid)
    await _send_board(
        letter_song,
        event,
        board,
        text="没有对上未解开的歌，再想想？\n",
    )
    await letter_song.finish()


@letter_quit.handle()
async def _(event: MessageEvent):
    gid = get_event_group_id(event)
    if gid is None:
        return
    board = letter_guess.get(gid)
    if board is None:
        await letter_quit.finish()
    elapsed_text = format_elapsed(board.elapsed())
    letter_guess.reveal_all(board)
    letter_guess.end(gid)
    await _send_board(
        letter_quit,
        event,
        board,
        text=(
            f"🔚 本局结束（用时 {elapsed_text}），剩余歌曲已揭晓。\n"
            "中途结束不发放速度奖与贡献奖。\n"
        ),
    )
    await letter_quit.finish()


@letter_score_cmd.handle()
async def _(bot: Bot, event: MessageEvent):
    gid = get_event_group_id(event)
    if gid is None:
        return
    err = _ensure_enabled(gid)
    if err:
        await letter_score_cmd.finish(err, reply_message=True)
    rows = letter_stats.score_ranking(gid)
    if not rows:
        await letter_score_cmd.finish("本群暂无开字母积分记录。", reply_message=True)
    th = letter_stats.thresholds_for(gid)
    im = await render_score_board(rows, subtitle=th.format_lines())
    await letter_score_cmd.finish(
        adapt_guess_outbound(MessageSegment.image(image_b64(im)), event=event),
        reply_message=resolve_reply_message(event, reply_message=True),
    )


@letter_contrib_cmd.handle()
async def _(bot: Bot, event: MessageEvent):
    gid = get_event_group_id(event)
    if gid is None:
        return
    err = _ensure_enabled(gid)
    if err:
        await letter_contrib_cmd.finish(err, reply_message=True)
    rows = letter_stats.contrib_ranking(gid)
    if not rows:
        await letter_contrib_cmd.finish("本群暂无开字母贡献记录。", reply_message=True)
    im = await render_contrib_board(rows)
    await letter_contrib_cmd.finish(
        adapt_guess_outbound(MessageSegment.image(image_b64(im)), event=event),
        reply_message=resolve_reply_message(event, reply_message=True),
    )


@letter_time_cmd.handle()
async def _(bot: Bot, event: MessageEvent):
    gid = get_event_group_id(event)
    if gid is None:
        return
    err = _ensure_enabled(gid)
    if err:
        await letter_time_cmd.finish(err, reply_message=True)
    rows = letter_stats.time_ranking(gid)
    if not rows:
        await letter_time_cmd.finish("本群暂无开字母通关用时记录。", reply_message=True)
    im = await render_time_board(rows)
    await letter_time_cmd.finish(
        adapt_guess_outbound(MessageSegment.image(image_b64(im)), event=event),
        reply_message=resolve_reply_message(event, reply_message=True),
    )


@letter_text_mode_cmd.handle()
async def _(event: MessageEvent):
    gid = get_event_group_id(event)
    if gid is None:
        return
    await letter_stats.set_display_mode(gid, "text")
    board = letter_guess.get(gid)
    if board is not None:
        board.display_mode = "text"
        await _send_board(letter_text_mode_cmd, event, board,
                          text="已将本群开字母设为「纯文字模式」（持久生效）。\n")
    else:
        await _send_plain(letter_text_mode_cmd, event,
                          "已将本群开字母设为「纯文字模式」，下局游戏生效。")
    await letter_text_mode_cmd.finish()


@letter_image_mode_cmd.handle()
async def _(event: MessageEvent):
    gid = get_event_group_id(event)
    if gid is None:
        return
    await letter_stats.set_display_mode(gid, "image")
    board = letter_guess.get(gid)
    if board is not None:
        board.display_mode = "image"
        await _send_board(letter_image_mode_cmd, event, board,
                          text="已将本群开字母设为「图片模式」（持久生效）。\n")
    else:
        await _send_plain(letter_image_mode_cmd, event,
                          "已将本群开字母设为「图片模式」，下局游戏生效。")
    await letter_image_mode_cmd.finish()


@letter_auto_mode_cmd.handle()
async def _(event: MessageEvent):
    gid = get_event_group_id(event)
    if gid is None:
        return
    await letter_stats.set_display_mode(gid, "auto")
    board = letter_guess.get(gid)
    if board is not None:
        board.display_mode = "auto"
        await _send_board(letter_auto_mode_cmd, event, board,
                          text="已将本群开字母设为「自动模式」（人多/突发时自动切文字，持久生效）。\n")
    else:
        await _send_plain(letter_auto_mode_cmd, event,
                          "已将本群开字母设为「自动模式」，下局游戏生效。")
    await letter_auto_mode_cmd.finish()


@letter_quick.handle()
async def _(event: MessageEvent):
    """对局中：单字开字母，其它短文本尝试开歌。"""
    gid = get_event_group_id(event)
    if gid is None or not letter_guess.is_playing(gid):
        return
    text = _plain_guess_text(event)
    if not text or _is_reserved_command(text):
        return

    if len(text) == 1 and _is_maskable(text):
        await _apply_open_letter(letter_quick, event, gid, text)
        return

    if 2 <= len(text) <= 48:
        await _apply_open_song(letter_quick, event, gid, text)
