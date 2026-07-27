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
)


def _wmc_api() -> Optional[WmcAPI]:
    """返回 WmcAPI 实例；未配置 api_key 时返回 None。"""
    key = maiconfig.wmc_api_key
    if not key:
        return None
    return WmcAPI(maiconfig.wmc_api_base_url, key)


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
        # 无评论时仍给出写入引导
        preview_urls = [build_preview_url(wmc_sid, kind, d) for d in diffs_avail]
        msg = f"「{title}」暂无谱面印象\n\n前往写入：\n" + "\n".join(preview_urls)
        await matcher.finish(msg, reply_message=True)
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

    # 附带写入引导
    preview_urls = [build_preview_url(wmc_sid, kind, d) for d in diffs_avail]
    lines.append("\n写入谱面印象：")
    for d, url in zip(diffs_avail, preview_urls):
        lines.append(f"  {WMC_DIFF_NAMES.get(d, str(d))} → {url}")

    await matcher.finish("\n".join(lines), reply_message=True)


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

    lines = [f"「{title}」谱面印象写入链接："]
    for d in diffs_avail:
        url = build_preview_url(wmc_sid, kind, d)
        lines.append(f"{WMC_DIFF_NAMES.get(d, str(d))} → {url}")
    lines.append("\n点击链接在浏览器中打开，即可评分和发表印象。")

    await matcher.finish("\n".join(lines), reply_message=True)


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

    await matcher.finish("\n".join(lines), reply_message=True)
