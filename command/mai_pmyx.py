# 谱面印象：查看（v.wmc.pub API）/ 写入引导 / 排行榜
from typing import List, Optional, Tuple

from nonebot import on_command
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageEvent
from nonebot.exception import IgnoredException
from nonebot.matcher import Matcher
from nonebot.params import CommandArg

from ..config import diffs, maiconfig
from ..libraries.maimaidx_music import feature_manager, mai
from ..libraries.maimaidx_wmc_api import (
    WMC_DIFF_NAMES,
    WmcAPI,
    build_preview_url,
    make_chart_key,
    resolve_wmc_base_url,
)
from ..libraries.maimaidx_platform import (
    build_markdown_link_message,
    build_markdown_message,
    plugin_finish,
    plugin_send,
    rank_text_image,
    use_qq_mode,
)


def _wmc_api() -> Optional[WmcAPI]:
    """返回 WmcAPI 实例；未配置 api_key 时返回 None。"""
    key = maiconfig.wmc_api_key
    if not key:
        return None
    return WmcAPI(resolve_wmc_base_url(maiconfig), key)


def _resolve_music(args: str) -> Optional[Tuple[str, str]]:
    """解析输入 -> (music_id, title)，未找到返回 None。"""
    args = args.strip()
    if not args:
        return None
    if m := mai.total_list.by_id(args):
        return m.id, m.title
    if m := mai.total_list.by_title(args):
        return m.id, m.title
    aliases = mai.total_alias_list.by_alias(args)
    if not aliases or len(aliases) != 1:
        return None
    song_id = str(aliases[0].SongID)
    m = mai.total_list.by_id(song_id)
    return (m.id, m.title) if m else (song_id, song_id)


def _song_id_for_wmc(music) -> str:
    """音乐对象 -> v.wmc.pub 的 song_id（DX 谱去掉前导 1）。"""
    raw = music.id
    if music.type == "DX" and raw.startswith("1"):
        return raw[1:]
    return raw


def _kind_str(music) -> str:
    return "standard" if music.type == "SD" else "dx"


def _available_diffs(music) -> List[int]:
    """返回可用难度的 API diff 值列表（2-6）。"""
    return [i + 2 for i in range(len(music.ds))]


async def _finish_impression_result(
    matcher: Matcher,
    event: MessageEvent,
    text: str,
    title: str,
    links: List[tuple[str, str]],
) -> None:
    """Send the impression text and keep write actions as QQ buttons."""
    if not use_qq_mode(event):
        await matcher.finish(text, reply_message=True)
        return
    await plugin_send(
        matcher,
        build_markdown_message(text, event=event),
        event=event,
        reply_message=True,
    )
    if links:
        await plugin_send(
            matcher,
            build_markdown_link_message(title, links, event=event),
            event=event,
            reply_message=False,
            mention_sender=False,
        )
    await matcher.finish()


# ================================================================
# 查看谱面印象
# ================================================================
pmyx_get = on_command("谱面印象", aliases={"查谱面印象", "曲目印象"})


@pmyx_get.handle()
async def _(event: MessageEvent, matcher: Matcher, arg: Message = CommandArg()):
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, "score"):
        raise IgnoredException("功能已禁用")

    text = arg.extract_plain_text().strip()
    resolved = _resolve_music(text) if text else None
    if not resolved:
        await matcher.finish("用法：谱面印象 <曲目id或曲名>", reply_message=True)
        return

    music_id, title = resolved
    music = mai.total_list.by_id(music_id)
    if not music:
        await matcher.finish(f"未找到曲目：{music_id}", reply_message=True)
        return

    api = _wmc_api()
    if not api:
        await matcher.finish("谱面印象 API 未配置（wmc_api_key），请联系管理员", reply_message=True)
        return

    wmc_sid = _song_id_for_wmc(music)
    kind = _kind_str(music)
    diffs_avail = _available_diffs(music)

    # 逐谱面拉取评论
    all_comments = []
    for d in diffs_avail:
        key = make_chart_key(wmc_sid, kind, d)
        try:
            result = await api.get_comments(key, limit=15)
        except Exception as e:
            continue
        if not result or not result.get("items"):
            continue
        diff_name = WMC_DIFF_NAMES.get(d, str(d))
        for c in result["items"]:
            c["_diff_name"] = diff_name
            c["_diff"] = d
            c["_chart_key"] = key
        all_comments.extend(result["items"])

    if not all_comments:
        msg = f"「{title}」暂无谱面印象"
        links = [
            (f"写入{WMC_DIFF_NAMES.get(d, str(d))}", build_preview_url(wmc_sid, kind, d))
            for d in diffs_avail
        ]
        await _finish_impression_result(matcher, event, msg, f"{title} 谱面印象", links)
        return

    lines = [f"【{title}】谱面印象（共 {len(all_comments)} 条）"]
    for i, c in enumerate(all_comments[:20], 1):
        diff_name = c.get("_diff_name", "?")
        author = c.get("author", "?")
        rating = c.get("rating", 0)
        body = (c.get("body") or "").strip()
        like_count = c.get("likeCount", 0)
        created = c.get("createdAt", "")[:10]
        line = f"{i}. [{diff_name}] {author} {rating}★ 👍{like_count}"
        if created:
            line += f" {created}"
        if body:
            line += f"\n   {body[:80]}{'…' if len(body) > 80 else ''}"
        lines.append(line)
    if len(all_comments) > 20:
        lines.append(f"… 仅展示前 20 条，共 {len(all_comments)} 条")

    links = [
        (f"写入{WMC_DIFF_NAMES.get(d, str(d))}", build_preview_url(wmc_sid, kind, d))
        for d in diffs_avail
    ]
    await _finish_impression_result(
        matcher,
        event,
        "\n".join(lines),
        f"{title} 谱面印象",
        links,
    )


# ================================================================
# 写谱面印象 → 引导用户到网页
# ================================================================
pmyx_write = on_command("写谱面印象", aliases={"上传谱面印象", "添加谱面印象"})


@pmyx_write.handle()
async def _(event: MessageEvent, matcher: Matcher, arg: Message = CommandArg()):
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, "score"):
        raise IgnoredException("功能已禁用")

    text = arg.extract_plain_text().strip()
    resolved = _resolve_music(text) if text else None
    if not resolved:
        await matcher.finish(
            "用法：写谱面印象 <曲目id或曲名>\n"
            "将生成对应谱面的网页链接，在浏览器中打开即可写入。",
            reply_message=True,
        )
        return

    music_id, title = resolved
    music = mai.total_list.by_id(music_id)
    if not music:
        await matcher.finish(f"未找到曲目：{music_id}", reply_message=True)
        return

    wmc_sid = _song_id_for_wmc(music)
    kind = _kind_str(music)
    diffs_avail = _available_diffs(music)

    links = [
        (f"写入{WMC_DIFF_NAMES.get(d, str(d))}", build_preview_url(wmc_sid, kind, d))
        for d in diffs_avail
    ]
    await _finish_impression_result(
        matcher,
        event,
        f"「{title}」谱面印象写入",
        f"{title} 谱面印象",
        links,
    )


# ================================================================
# 谱面排行榜
# ================================================================
pmyx_ranking = on_command("谱面排行", aliases={"谱面热度"})


@pmyx_ranking.handle()
async def _(event: MessageEvent, matcher: Matcher, arg: Message = CommandArg()):
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, "score"):
        raise IgnoredException("功能已禁用")

    api = _wmc_api()
    if not api:
        await matcher.finish("谱面印象 API 未配置（wmc_api_key），请联系管理员", reply_message=True)
        return

    text = arg.extract_plain_text().strip().lower()
    sort = "rating" if "评" in text or "分" in text else "views"

    try:
        result = await api.get_rankings(sort=sort, limit=10)
    except Exception as e:
        await matcher.finish(f"请求失败：{e}", reply_message=True)
        return

    if not result or not result.get("items"):
        await matcher.finish("暂无排行数据", reply_message=True)
        return

    sort_label = "评分" if sort == "rating" else "浏览"
    lines = [f"谱面排行 TOP10（按{sort_label}）"]
    for i, item in enumerate(result["items"], 1):
        title = item.get("title", "?")
        artist = item.get("artist", "")
        kind = item.get("kind", "standard")
        diff = item.get("difficulty", 0)
        views = item.get("views", 0)
        avg = item.get("ratingAverage", 0)
        count = item.get("ratingCount", 0)
        diff_name = WMC_DIFF_NAMES.get(diff, str(diff))
        kind_label = "DX" if kind == "dx" else "SD"
        line = f"{i}. {title}（{artist}）[{kind_label} {diff_name}]"
        line += f"\n   浏览 {views} | 评分 {avg:.1f}★（{count}人）"
        lines.append(line)

    await plugin_finish(
        matcher,
        rank_text_image("\n".join(lines)),
        event=event,
        reply_message=True,
    )
