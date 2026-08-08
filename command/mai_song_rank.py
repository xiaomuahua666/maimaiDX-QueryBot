import re
from typing import Optional

from nonebot import get_bot, on_regex
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageEvent
from nonebot.exception import IgnoredException
from nonebot.params import RegexMatched

from ..config import log
from ..libraries.maimaidx_group_rating import (
    build_forward_node,
    get_group_member_song_scores,
    group_song_my_rank,
    render_group_song_board,
)
from ..libraries.maimaidx_music import feature_manager
from ..libraries.maimaidx_score_formatter import (
    format_leaderboard_text,
    format_score_line_from_dict,
    get_difficulty_name,
)
from ..libraries.maimaidx_song_resolver import SongResolver
from ..libraries.image import music_picture
from ..libraries.maimaidx_platform import (
    format_forward_nodes_as_text,
    plugin_finish,
    rank_text_image,
    resolve_score_qqid,
)


_DIFF_RE = r"(?:绿|黄|红|紫|白|basic|advanced|expert|master|remaster|re:master)"
_DIFF_ALIASES = [
    "re:master", "remaster", "basic", "advanced", "expert", "master",
    "绿", "黄", "红", "紫", "白",
]

# 只用一个 matcher，根治“同一条消息被两个 matcher 处理”的问题。
# 支持：
# - 我的白潘排名 / 我的 白 潘 排名 / 我的 潘 排名 白
# - 白潘排名 / 白 潘 排名 / 潘 排名 白
# - 可选 topN：潘 排名 白 20 / 潘 排名 20 / 我的潘排名20
# 「我的排名」「查看排名」属于全局 Rating 排名命令，不能回退成歌曲名。
_SONG_RANK_PATTERN = (
    r"^\s*(?!(?:我的|查看)\s*排名(?:\s+.*)?\s*$)"
    r"(我的)?\s*(.+?)\s*排名\s*(.*?)\s*$"
)
song_rank = on_regex(
    _SONG_RANK_PATTERN,
    flags=re.IGNORECASE,
)


def _strip_prefix_diff(text: str) -> tuple[str, str]:
    """从歌曲前缀剥离难度（如 白潘 -> 白 + 潘）。返回 (song_text, diff)。"""
    s = text.strip()
    log.debug(f"[song_rank] _strip_prefix_diff input='{s}'")
    lower = s.lower()
    for alias in sorted(_DIFF_ALIASES, key=len, reverse=True):
        if lower.startswith(alias):
            remain = s[len(alias):].strip()
            if remain:
                log.debug(f"[song_rank] _strip_prefix_diff matched alias='{alias}', remain='{remain}'")
                return remain, alias
    log.debug(f"[song_rank] _strip_prefix_diff no prefix diff, song='{s}'")
    return s, ""


def _parse_suffix(suffix: str) -> tuple[str, Optional[int], Optional[str]]:
    """
    解析“排名”后面的参数，支持：
    - 空
    - 难度（白 / master）
    - 数量（20）
    - 难度+数量（白20 / 白 20 / master 20）
    返回: (diff, top_n, error)
    """
    s = suffix.strip()
    log.debug(f"[song_rank] _parse_suffix input='{s}'")
    if not s:
        return "", None, None
    m = re.fullmatch(rf"\s*(?:(?P<diff>{_DIFF_RE})\s*)?(?P<n>\d{{1,2}})?\s*", s, flags=re.IGNORECASE)
    if not m:
        log.debug(f"[song_rank] _parse_suffix failed for '{suffix}'")
        return "", None, f"无法解析排名参数：{suffix}"
    diff = (m.group("diff") or "").strip().lower()
    n_raw = (m.group("n") or "").strip()
    top_n = int(n_raw) if n_raw else None
    log.debug(f"[song_rank] _parse_suffix parsed diff='{diff}', top_n={top_n}")
    return diff, top_n, None


def _extract_rank_args(matched: dict) -> tuple[bool, str, str, Optional[int], Optional[str]]:
    """
    groups:
      1: optional '我的'
      2: 排名前正文（可能含前置难度）
      3: 排名后缀参数（可能是难度/数量）
    返回: (is_my, song_input, diff_input, top_n, error)
    """
    if not matched:
        return False, "", "", None, None

    is_my = bool((matched.group(1) or "").strip())
    prefix = (matched.group(2) or "").strip()
    suffix = (matched.group(3) or "").strip()
    log.debug(
        f"[song_rank] _extract_rank_args raw is_my={is_my}, prefix='{prefix}', suffix='{suffix}', groups={matched.groups()}"
    )

    song_text, prefix_diff = _strip_prefix_diff(prefix)
    suffix_diff, top_n, err = _parse_suffix(suffix)
    if err:
        return is_my, song_text, "", top_n, err

    diff_input = suffix_diff or prefix_diff
    log.debug(
        f"[song_rank] _extract_rank_args parsed is_my={is_my}, song='{song_text}', diff='{diff_input}', top_n={top_n}"
    )
    return is_my, song_text, diff_input, top_n, None


async def _get_bot_info(event: MessageEvent) -> tuple[Bot, int, str]:
    try:
        bot = get_bot()
    except Exception:
        bot = get_bot(str(event.self_id))
    self_id = int(getattr(bot, "self_id", event.self_id))
    nickname = str(getattr(bot, "nickname", None) or "Bot")
    log.debug(f"[song_rank] _get_bot_info self_id={self_id}, nickname='{nickname}'")
    return bot, self_id, nickname


async def _resolve_song_and_level(song_input: str, diff_input: str) -> tuple[Optional[object], int, Optional[str]]:
    from ..libraries.maimaidx_difficulty_filter import DifficultyFilter

    log.debug(f"[song_rank] _resolve_song_and_level song_input='{song_input}', diff_input='{diff_input}'")
    music = await SongResolver.resolve(song_input)
    if not music:
        log.debug(f"[song_rank] song not found: '{song_input}'")
        return None, 0, f"未找到歌曲「{song_input}」，请检查ID或曲名是否正确。"

    level_index = 3
    if diff_input:
        try:
            diff_filter = DifficultyFilter.parse(diff_input)
            if diff_filter.level_index is not None:
                level_index = diff_filter.level_index
        except ValueError as e:
            log.debug(f"[song_rank] difficulty parse failed: {e}")
            return None, 0, f"难度解析失败：{e}"

    if not SongResolver.has_level(music, level_index):
        diff_name = get_difficulty_name(level_index)
        music_title = SongResolver.get_title(music)
        log.debug(f"[song_rank] song has no level index={level_index}, title='{music_title}'")
        return None, 0, f"「{music_title}」没有{diff_name}难度。"

    log.debug(f"[song_rank] resolved song_id={SongResolver.get_id(music)}, level_index={level_index}")
    return music, level_index, None


@song_rank.handle()
async def _song_rank(event: MessageEvent, matched = RegexMatched()):
    plain = event.get_plaintext().strip()
    log.debug(f"[song_rank] received message='{plain}'")
    if not isinstance(event, GroupMessageEvent):
        await song_rank.finish("该功能仅在群聊中可用。", reply_message=True)
    if not feature_manager.is_enabled(event.group_id, "score"):
        raise IgnoredException("功能已禁用")

    is_my, song_input, diff_input, top_n, parse_error = _extract_rank_args(matched)
    if parse_error:
        log.debug(f"[song_rank] parse_error='{parse_error}'")
        await song_rank.finish(parse_error, reply_message=True)
    if not song_input:
        await song_rank.finish("请提供歌曲ID或曲名，格式：我的<歌曲>排名 / <歌曲>排名", reply_message=True)

    music, level_index, error = await _resolve_song_and_level(song_input, diff_input)
    if error:
        log.debug(f"[song_rank] resolve error='{error}'")
        await song_rank.finish(error, reply_message=True)

    music_id = SongResolver.get_id(music)
    music_title = SongResolver.get_title(music)
    log.debug(
        f"[song_rank] dispatch branch={'my' if is_my else 'leaderboard'}, music_id={music_id}, title='{music_title}', level_index={level_index}, top_n={top_n}"
    )

    bot, self_id, nickname = await _get_bot_info(event)
    current_qqid = resolve_score_qqid(event)

    if is_my:
        board_text = await render_group_song_board(
            bot, event.group_id, music_id, music_title, level_index,
            top_n=top_n or 11, self_qq=current_qqid,
            cover_path=str(music_picture(music_id)),
        )
        await plugin_finish(
            song_rank,
            board_text,
            event=event,
            reply_message=True,
        )

    # 群榜：只查一次群成绩，并附带当前用户排名提示
    if top_n is None:
        top_n = 10
    top_n = max(1, min(50, top_n))
    result = await render_group_song_board(
        bot, event.group_id, music_id, music_title, level_index,
        top_n=top_n, self_qq=current_qqid,
        cover_path=str(music_picture(music_id)),
    )
    await plugin_finish(
        song_rank,
        result,
        event=event,
        reply_message=False,
    )
