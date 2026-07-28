"""跟踪进行中的交互会话，供关机/重启时通知并清理。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Literal, Optional, Set, Tuple

from nonebot import get_bots, get_driver

from ..config import BOT_QQ_GROUP, log
from .maimaidx_platform import send_group_plain_text

SHUTDOWN_NOTICE = '机器人程序因更新重启'

Kind = Literal['group', 'private']


@dataclass(frozen=True)
class PendingTarget:
    kind: Kind
    target_id: str
    bot_id: Optional[str] = None


_pending: Dict[str, PendingTarget] = {}


def session_key(prefix: str, event) -> str:
    """按指令前缀 + 群/私聊会话生成登记键。"""
    uid = str(event.get_user_id())
    gid = getattr(event, 'group_id', None)
    if gid is None:
        gid = getattr(event, 'group_openid', None)
    if gid is not None:
        return f'{prefix}:g{gid}:u{uid}'
    return f'{prefix}:p{uid}'


def track(key: str, target: PendingTarget) -> None:
    _pending[key] = target


def untrack(key: str) -> None:
    _pending.pop(key, None)


def track_event(key: str, event) -> None:
    """从 MessageEvent 登记等待中的交互。"""
    bot_id = str(getattr(event, 'self_id', '') or '') or None
    gid = getattr(event, 'group_id', None)
    if gid is None:
        gid = getattr(event, 'group_openid', None)
    if gid is not None:
        track(key, PendingTarget('group', str(gid), bot_id))
        return
    track(key, PendingTarget('private', str(event.get_user_id()), bot_id))


def finish_pending(key: str) -> None:
    """交互正常结束（finish / 取消）时注销。"""
    untrack(key)


def _dedupe(targets: Iterable[PendingTarget]) -> List[PendingTarget]:
    seen: Set[Tuple[str, str]] = set()
    out: List[PendingTarget] = []
    for t in targets:
        sig = (t.kind, t.target_id)
        if sig in seen:
            continue
        seen.add(sig)
        out.append(t)
    return out


async def _send_notice(target: PendingTarget, text: str) -> None:
    bots = get_bots()
    if not bots:
        return
    bot = None
    if target.bot_id and target.bot_id in bots:
        bot = bots[target.bot_id]
    else:
        bot = next(iter(bots.values()), None)
    if bot is None:
        return
    try:
        if target.kind == 'group':
            await send_group_plain_text(bot, target.target_id, text)
            return
        # 私聊：纯数字走 OneBot；openid 走官方 QQ C2C
        if str(target.target_id).isdigit():
            await bot.send_private_msg(user_id=int(target.target_id), message=text)
            return
        await bot.send_to_c2c(openid=str(target.target_id), message=text)
    except Exception as exc:
        log.warning(
            f'[shutdown] 通知失败 {target.kind}={target.target_id}: '
            f'{type(exc).__name__}: {exc}'
        )


async def collect_cleanup_targets() -> List[PendingTarget]:
    """收集未完成会话、清理状态，返回需通知的目标（已去重）。"""
    targets: List[PendingTarget] = list(_pending.values())
    _pending.clear()

    # 猜歌 / 猜曲绘 / 猜曲子 / 猜铺面 / 猜 Rating / 找内鬼 / 开字母
    try:
        from .maimaidx_guess_audio import request_hot_batch_cancel
        from .maimaidx_guess_letter import letter_guess
        from .maimaidx_guess_rating import rating_guess
        from .maimaidx_guess_impostor import impostor_guess
        from .maimaidx_guess_score import guess_score
        from .maimaidx_music import guess

        request_hot_batch_cancel()
        gids = list(
            set(guess.Group.keys())
            | set(guess.Preparing)
            | set(letter_guess.Group.keys())
            | set(rating_guess.groups.keys())
            | set(rating_guess.locked)
            | set(impostor_guess.groups.keys())
            | set(impostor_guess.locked)
        )
        for gid in gids:
            targets.append(PendingTarget('group', str(gid)))
            guess.Preparing.discard(gid)
            if gid in letter_guess.Group:
                letter_guess.end(gid)
            if gid in guess.Group:
                guess.Group[gid].end = True
                try:
                    await guess_score.reset_all_streaks(gid)
                except Exception:
                    pass
                guess.end(gid)
            rating_guess.end(gid)
            impostor_guess.end(gid)
    except Exception as exc:
        log.warning(f'[shutdown] 结束猜歌失败: {type(exc).__name__}: {exc}')

    # 更新 PC 数等二维码等待
    try:
        from ..command.mai_playcount import drain_waiting_qrcode_sessions

        for _user_id, group_id in drain_waiting_qrcode_sessions():
            targets.append(PendingTarget('group', str(group_id)))
    except Exception as exc:
        log.warning(f'[shutdown] 清理二维码等待失败: {type(exc).__name__}: {exc}')

    return _dedupe(targets)


async def notify_shutdown(targets: Iterable[PendingTarget]) -> None:
    text = SHUTDOWN_NOTICE
    for target in targets:
        await _send_notice(target, text)


def _shutdown_notice_target() -> PendingTarget:
    """关机重启通知只发往配置的通知群（默认 BOT_QQ_GROUP）。"""
    return PendingTarget('group', str(BOT_QQ_GROUP))


driver = get_driver()


@driver.on_shutdown
async def _on_maimaidx_shutdown() -> None:
    """关机时清理未完成交互，并向通知群发送重启提示（不广播到其它会话）。"""
    cleaned = 0
    try:
        targets = await collect_cleanup_targets()
        cleaned = len(targets)
    except Exception as exc:
        log.error(f'[shutdown] 收集未完成任务失败: {type(exc).__name__}: {exc}')
    notice = _shutdown_notice_target()
    log.info(
        f'[shutdown] 已清理 {cleaned} 个会话目标；'
        f'向通知群 {notice.target_id} 发送「{SHUTDOWN_NOTICE}」'
    )
    await notify_shutdown([notice])
