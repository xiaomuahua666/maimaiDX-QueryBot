import random
import re
from textwrap import dedent
from typing import List, Tuple

from nonebot import on_command, on_endswith, on_regex
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
)
from nonebot.exception import IgnoredException
from nonebot.params import CommandArg, Endswith, RegexMatched

from ..config import SONGS_PER_PAGE, diffs, log, maiconfig
from ..libraries.image import image_to_base64, text_to_bytes_io
from ..libraries.maimaidx_model import AliasStatus
from ..libraries.maimaidx_guess_letter import letter_guess
from ..libraries.maimaidx_music import feature_manager, guess, mai, maiApi
from ..libraries.maimaidx_music_info import build_tags_forward_nodes, draw_music_info
from ..libraries.maimaidx_multiver_chart import draw_multiver_chart
from ..libraries.maimaidx_pmyx_api import PmyxAPI
from ..libraries.maimaidx_wmc_api import (
    WMC_DIFF_NAMES,
    WmcAPI,
    build_preview_url,
    diff_value_for_wmc,
    kind_for_wmc,
    make_chart_key,
    resolve_wmc_base_url,
    song_id_for_wmc,
)
from ..libraries.maimaidx_timing import attach_timing, finish_timed_sync, run_timed
from ..libraries.maimaidx_platform import (
    billing_user_id,
    build_image_message,
    build_markdown_link_message,
    deliver_forward_messages,
    resolve_score_qqid,
    use_qq_mode,
)

search_music        = on_command('查歌', aliases={'search'})
search_base         = on_command('定数查歌', aliases={'search base'})
search_bpm          = on_command('bpm查歌', aliases={'search bpm'})
search_artist       = on_command('曲师查歌', aliases={'search artist'})
search_charter      = on_command('谱师查歌', aliases={'search charter'})
search_alias_song   = on_endswith(('是什么歌', '是啥歌'))
query_chart         = on_regex(r'^id\s?([0-9]+)$', re.IGNORECASE)
chart_preview       = on_regex(r'^谱面\s?([0-9]+)(绿|黄|红|紫|白)$')


def _guess_anti_cheat_active(group_id) -> bool:
    gid = str(group_id)
    return gid in guess.Group or letter_guess.is_playing(gid)


def _bot_nickname(bot: Bot) -> str:
    """从 bot 配置取昵称，保证返回可 JSON 序列化的 str（config.nickname 可能是 set）。"""
    try:
        nick = getattr(bot.config, 'nickname', None)
        if isinstance(nick, (list, tuple)) and nick:
            return str(nick[0])
        if isinstance(nick, (set, frozenset)) and nick:
            return str(next(iter(nick)))
        if nick and isinstance(nick, str):
            return nick
    except Exception:
        pass
    return 'kndbot'


def _pmyx_node(self_id: int, nickname: str, text: str) -> dict:
    return {
        'type': 'node',
        'data': {'user_id': str(self_id), 'nickname': nickname, 'content': text},
    }


def _chart_preview_links(music) -> list[tuple[str, str]]:
    """Return stable, public preview URLs for every available difficulty."""
    # QQ custom-keyboard buttons have limited width.  The message heading
    # already identifies the action (preview/impression), so keep each
    # difficulty button to the familiar one-character colour label.
    diff_names = ['绿', '黄', '红', '紫', '白']
    kind = kind_for_wmc(music)
    song_id = song_id_for_wmc(music)
    return [
        (diff_names[i], build_preview_url(song_id, kind, diff_value_for_wmc(i)))
        for i in range(min(len(music.ds), len(diff_names)))
    ]


def _chart_write_links(music) -> list[tuple[str, str]]:
    """Return the same chart pages for impression writing.

    The surrounding Markdown heading says this is the impression-writing
    action, so the keyboard can reuse the compact colour labels.
    """
    return _chart_preview_links(music)


async def _build_chart_preview_nodes(music, self_id: int, nickname: str) -> List[dict]:
    """构建谱面预览链接的合并转发节点列表"""
    nodes = []
    nodes.append(_pmyx_node(self_id, nickname, f"ID {music.id} 的谱面预览链接："))
    for diff_name, url in _chart_preview_links(music):
        nodes.append(_pmyx_node(self_id, nickname, f"{diff_name} {url}"))
    return nodes


async def _build_wmc_tags_forward_nodes(music, self_id: int, nickname: str) -> List[dict]:
    """从 v.wmc.pub /charts/:chartKey/tags 拉取谱面难度分析标签，构建合并转发节点。"""
    import asyncio
    wmc_key = maiconfig.wmc_api_key
    if not wmc_key:
        return []
    api = WmcAPI(resolve_wmc_base_url(maiconfig), wmc_key)
    wmc_sid = song_id_for_wmc(music)
    kind = kind_for_wmc(music)
    diff_labels = ['绿谱', '黄谱', '红谱', '紫谱', '白谱']
    # 并发拉取所有难度的 tags
    tasks = []
    for i in range(min(len(music.ds), len(diff_labels))):
        key = make_chart_key(wmc_sid, kind, diff_value_for_wmc(i))
        tasks.append(api.get_tags(key, radar_threshold=40, feature_threshold=0.5))
    if not tasks:
        return []
    results = await asyncio.gather(*tasks, return_exceptions=True)
    nodes = []
    for i, result in enumerate(results):
        if isinstance(result, Exception) or not result or not isinstance(result, dict) or not result.get("tags"):
            continue
        tags = result["tags"]
        lines = [f"【{diff_labels[i]}】"]
        dc = tags.get("difficultyClassification")
        if dc:
            label = dc.get("label", "")
            est = dc.get("estimatedLevel")
            dev = dc.get("deviation")
            line = f"难度分类：{label}"
            if est is not None:
                line += f"（预测 {est:.1f}"
                if dev is not None:
                    sign = "+" if dev >= 0 else ""
                    line += f"，偏差 {sign}{dev:.1f}"
                line += "）"
            lines.append(line)
        eval_tags = tags.get("evaluationTags") or []
        if eval_tags:
            parts = [f"{t['label']}({t['score']})" for t in eval_tags[:5]]
            lines.append("评估：" + " ".join(parts))
        radar_tags = tags.get("radarTags") or []
        if radar_tags:
            parts = [f"{t['label']}({t['score']})" for t in radar_tags[:5]]
            lines.append("雷达：" + " ".join(parts))
        patterns = tags.get("patterns") or []
        if patterns:
            sev_map = {"high": "★", "mid": "●", "low": "○"}
            parts = [f"{t['label']}{sev_map.get(t.get('severity', ''), '')}×{t.get('count', 0)}" for t in patterns[:6]]
            lines.append("模式：" + " ".join(parts))
        nodes.append(_pmyx_node(self_id, nickname, "\n".join(lines)))
    if not nodes:
        return []
    nodes.insert(0, _pmyx_node(self_id, nickname, f"ID {music.id} 的谱面难度分析（v.wmc.pub）"))
    return nodes


async def _build_pmyx_forward_nodes(
    music_id: str,
    self_id: int,
    nickname: str,
    *,
    include_write_links: bool = True,
) -> List[dict]:
    """拉取谱面印象并构建转发节点。

    QQ 将写入链接作为独立键盘发送，因此其转发正文只保留印象内容；
    OneBot 继续把 URL 放在正文里，保持原有可复制、可打开的行为。
    """
    # ---- v2: v.wmc.pub API ----
    wmc_key = maiconfig.wmc_api_key
    if wmc_key:
        music = mai.total_list.by_id(music_id)
        if music:
            import asyncio
            api = WmcAPI(resolve_wmc_base_url(maiconfig), wmc_key)
            wmc_sid = song_id_for_wmc(music)
            kind = kind_for_wmc(music)
            # 并发拉取所有难度的评论
            tasks = []
            for d in range(len(music.ds)):
                key = make_chart_key(wmc_sid, kind, diff_value_for_wmc(d))
                tasks.append(api.get_comments(key, limit=15))
            results = await asyncio.gather(*tasks, return_exceptions=True)
            all_comments = []
            for d, result in enumerate(results):
                if isinstance(result, Exception) or not result or not isinstance(result, dict):
                    continue
                items = result.get("items") or []
                if items:
                    diff_name = WMC_DIFF_NAMES.get(d + 2, str(d + 2))
                    for c in items:
                        c["_diff_name"] = diff_name
                    all_comments.extend(items)
            if not all_comments:
                if include_write_links:
                    preview_urls = [
                        build_preview_url(wmc_sid, kind, diff_value_for_wmc(d))
                        for d in range(len(music.ds))
                    ]
                    url_text = "\n".join(preview_urls)
                    text = f"ID {music_id} 暂无谱面印象\n\n前往写入：\n{url_text}"
                else:
                    text = f"ID {music_id} 暂无谱面印象"
                return [_pmyx_node(self_id, nickname, text)]
            random.shuffle(all_comments)
            nodes = [_pmyx_node(self_id, nickname, f"ID {music_id} 的谱面印象（共 {len(all_comments)} 条）")]
            for x in all_comments[:10]:
                diff_name = x.get("_diff_name", "?")
                author = x.get("author", "?")
                rating = x.get("rating", 0)
                like_count = x.get("likeCount", 0)
                body = (x.get("body") or "").strip()
                created = x.get("createdAt", "")[:10]
                line = f"[{diff_name}] {author} {rating}★ 👍{like_count} {created}"
                if body:
                    line += f"\n{body[:200]}{'…' if len(body) > 200 else ''}"
                nodes.append(_pmyx_node(self_id, nickname, line))
            if len(all_comments) > 10:
                nodes.append(_pmyx_node(self_id, nickname, f"… 随机展示 10 条，共 {len(all_comments)} 条"))
            if include_write_links:
                # OneBot 下保留可复制的写入 URL；QQ 由调用方发送键盘。
                preview_urls = [
                    build_preview_url(wmc_sid, kind, diff_value_for_wmc(d))
                    for d in range(len(music.ds))
                ]
                nodes.append(_pmyx_node(self_id, nickname, "写入谱面印象：\n" + "\n".join(preview_urls)))
            return nodes

    # ---- 回退：旧 PMYX API ----
    base = maiconfig.pmyx_api_base_url or "https://mai.mai2dx.shop"
    api = PmyxAPI(base)
    try:
        items = await api.get_impressions(music_id)
        log.debug(f"Pmyx API response for music_id={music_id}: {items}")
    except Exception as e:
        log.warning(f'[maimai] 谱面印象请求失败 id={music_id} err={type(e).__name__}: {e}')
        return [_pmyx_node(self_id, nickname, f"ID {music_id} 谱面印象：请求失败（{type(e).__name__}）")]
    if not items:
        return [_pmyx_node(self_id, nickname, f"ID {music_id} 暂无谱面印象")]
    
    random.shuffle(items)

    nodes = [_pmyx_node(self_id, nickname, f"ID {music_id} 的谱面印象（共 {len(items)} 条）")]
    for x in items[:10]:
        diff_idx = x.get("difficulty", 3)
        diff_name = diffs[diff_idx] if 0 <= diff_idx < len(diffs) else str(diff_idx)
        nick = x.get("nickname", "?")
        rating = x.get("rating", 0)
        adm = x.get("admiration", 0)
        date = x.get("date", "")
        imp = (x.get("impression") or "").strip()
        line = f"[{diff_name}] {nick} 评分{rating} 👍{adm} {date}"
        if imp:
            line += f"\n{imp[:200]}{'…' if len(imp) > 200 else ''}"
        replies = x.get("replies") or []
        if replies:
            line += f"\n（{len(replies)} 条回复）"
        nodes.append(_pmyx_node(self_id, nickname, line))
    if len(items) > 10:
        nodes.append(_pmyx_node(self_id, nickname, f"… 随机展示 10 条，共 {len(items)} 条"))
    return nodes


def _build_nested_forward_node(self_id: int, title: str, sub_nodes: List[dict]) -> dict:
    """构建嵌套合并转发节点，nickname 为标题"""
    return {
        'type': 'node',
        'data': {
            'user_id': str(self_id),
            'nickname': title,
            'content': sub_nodes
        }
    }


async def _send_forward(bot: Bot, event: MessageEvent, nodes: List[dict]) -> None:
    await deliver_forward_messages(bot, event, nodes, title='查歌补充信息')


async def _send_song_info_then_pmyx_forward(
    bot: Bot,
    event: MessageEvent,
    music,
    matcher,
    prefix: str = '',
    reply: bool = True,
):
    """歌曲信息直接回复用户，再发一个合并转发（包含谱面印象、谱面标签、谱面预览链接三个子合并转发）。"""
    async def _gen():
        pic = await draw_music_info(music, resolve_score_qqid(event))
        return (Message(prefix) + pic) if prefix else pic

    from ..libraries.maimaidx_break import take_break_charge_footer
    from ..libraries.maimaidx_error import BreakInsufficientError, QBindRequiredError
    try:
        msg, total = await run_timed(
            _gen(), billing_qqid=billing_user_id(event), feature_charge='search'
        )
    except BreakInsufficientError as e:
        await matcher.finish(str(e), reply_message=reply)
        return
    except QBindRequiredError as e:
        await matcher.finish(str(e), reply_message=reply)
        return
    charge = take_break_charge_footer()
    charge_extra = '\n'.join(charge) if charge else ''
    await matcher.send(attach_timing(msg, total, extra=charge_extra), reply_message=reply)
    nickname = _bot_nickname(bot)
    all_nodes = []
    # 并发拉取三个数据源
    import asyncio
    qq_mode = use_qq_mode(event)
    pmyx_task = _build_pmyx_forward_nodes(
        music.id,
        event.self_id,
        nickname,
        include_write_links=not qq_mode,
    )
    tag_task = build_tags_forward_nodes(music.id, event.self_id, nickname)
    wmc_task = _build_wmc_tags_forward_nodes(music, event.self_id, nickname)
    pmyx_nodes, tag_nodes, wmc_tag_nodes = await asyncio.gather(pmyx_task, tag_task, wmc_task)
    if pmyx_nodes:
        all_nodes.append(_build_nested_forward_node(event.self_id, "谱面印象", pmyx_nodes))
    if tag_nodes:
        all_nodes.append(_build_nested_forward_node(event.self_id, "谱面标签", tag_nodes))
    if wmc_tag_nodes:
        all_nodes.append(_build_nested_forward_node(event.self_id, "难度分析", wmc_tag_nodes))
    chart_img = draw_multiver_chart(music.id)
    if chart_img:
        b64 = image_to_base64(chart_img)
        if qq_mode:
            # ``draw_multiver_chart`` returns a PIL image.  Convert it to a
            # real QQ local attachment instead of passing the object through
            # the OneBot image converter (which produced an empty/"附图" node).
            from nonebot.adapters.qq.message import Message as QQMessage
            from nonebot.adapters.qq.message import MessageSegment as QQSeg

            chart_media = build_image_message(b64, event=event)
            await bot.send(
                event,
                QQMessage(
                    [
                        QQSeg.text(f'定数变化图：{music.title}\n'),
                        chart_media,
                    ]
                ),
                _maimaidx_skip_sender_mention=True,
            )
        else:
            all_nodes.append(_build_nested_forward_node(
                event.self_id, "定数变化",
                [
                    _pmyx_node(event.self_id, nickname, f"这是{music.title}的各谱面难度变化"),
                    _pmyx_node(event.self_id, nickname, f"[CQ:image,file={b64}]"),
                ]
            ))
    chart_preview_nodes = await _build_chart_preview_nodes(music, event.self_id, nickname)
    if chart_preview_nodes and not use_qq_mode(event):
        all_nodes.append(_build_nested_forward_node(event.self_id, "谱面预览", chart_preview_nodes))
    if qq_mode and _chart_preview_links(music):
        # URLs inside a rasterized forward summary are not clickable.  Send a
        # native Markdown block with URL buttons instead.
        await bot.send(
            event,
            build_markdown_link_message(
                f'ID {music.id} 谱面预览',
                _chart_preview_links(music),
                event=event,
            ),
            _maimaidx_skip_sender_mention=True,
        )
    await _send_forward(bot, event, all_nodes)
    if qq_mode and _chart_write_links(music):
        # Keep impression actions directly below the impression summary and
        # avoid exposing bare URLs in the Markdown/forward text.
        await bot.send(
            event,
            build_markdown_link_message(
                f'ID {music.id} 谱面印象',
                _chart_write_links(music),
                event=event,
            ),
            _maimaidx_skip_sender_mention=True,
        )
    await matcher.finish()


async def _send_chart_and_tags_forward(bot: Bot, event: MessageEvent, music, prefix: str = '', reply: bool = True):
    """歌曲信息直接回复，合并转发里发该 id 的谱面印象。"""
    await _send_song_info_then_pmyx_forward(bot, event, music, search_alias_song, prefix=prefix, reply=reply)


def song_level(ds1: float, ds2: float) -> List[Tuple[str, str, float, str]]:
    """
    查询定数范围内的乐曲
    
    Params:
        `ds1`: 定数下限
        `ds2`: 定数上限
    Return:
        `result`: 查询结果
    """
    result: List[Tuple[str, str, float, str]] = []
    music_data = mai.total_list.filter(ds=(ds1, ds2))
    for music in sorted(music_data, key=lambda x: int(x.id)):
        if int(music.id) >= 100000:
            continue
        for i in music.diff:
            result.append((music.id, music.title, music.ds[i], diffs[i]))
    return result


@search_music.handle()
async def _(bot: Bot, event: GroupMessageEvent, message: Message = CommandArg()):
    if not feature_manager.is_enabled(event.group_id, 'search'):
        raise IgnoredException('功能已禁用')
    name = message.extract_plain_text().strip()
    page = 1
    if not name:
        await search_music.finish('请输入关键词', reply_message=True)
    result = mai.total_list.filter(title_search=name)
    if len(result) == 0:
        await search_music.finish(
            '没有找到这样的乐曲。\n※ 如果是别名请使用「xxx是什么歌」指令进行查询哦。', 
            reply_message=True
        )
    if len(result) == 1:
        music = result[0]
        await _send_song_info_then_pmyx_forward(bot, event, music, search_music, reply=True)
        return
    
    search_result = ''
    result.sort(key=lambda i: int(i.id))
    for i, music in enumerate(result):
        if (page - 1) * SONGS_PER_PAGE <= i < page * SONGS_PER_PAGE:
            search_result += f'{f"「{music.id}」":<7} {music.title}\n'
    search_result += (
        f'第「{page}」页，'
        f'共「{len(result) // SONGS_PER_PAGE + 1}」页。'
        '请使用「id xxxxx」查询指定曲目。'
    )
    await finish_timed_sync(
        search_music,
        lambda: MessageSegment.image(text_to_bytes_io(search_result)),
    )


@search_base.handle()
async def _(event: MessageEvent, message: Message = CommandArg()):
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, 'search'):
        raise IgnoredException('功能已禁用')
    args = message.extract_plain_text().strip().split()
    if len(args) > 2 or len(args) == 0:
        await search_base.finish(
            dedent('''
                命令格式：
                定数查歌 「定数」「页数」
                定数查歌 「定数下限」「定数上限」「页数」
            ''')
        )
    page = 1
    if len(args) == 1:
        ds1, ds2 = args[0], args[0]
    elif len(args) == 2:
        if '.' in args[1]:
            ds1, ds2 = args
        else:
            ds1, ds2 = args[0], args[0]
            page = args[1]
    else:
        ds1, ds2, page = args
    page = int(page)
    result = song_level(float(ds1), float(ds2))
    if not result:
        await search_base.finish('没有找到这样的乐曲。', reply_message=True)
    
    search_result = ''
    for i, _result in enumerate(result):
        id, title, ds, diff = _result
        if (page - 1) * SONGS_PER_PAGE <= i < page * SONGS_PER_PAGE:
            search_result += f'{f"「{id}」":<7}{f"「{diff}」":<11}{f"「{ds}」"} {title}\n'
    search_result += (
        f'第「{page}」页，'
        f'共「{len(result) // SONGS_PER_PAGE + 1}」页。'
        '请使用「id xxxxx」查询指定曲目。'
    )
    await finish_timed_sync(
        search_base,
        lambda: MessageSegment.image(text_to_bytes_io(search_result)),
    )


@search_bpm.handle()
async def _(event: MessageEvent, message: Message = CommandArg()):
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, 'search'):
        raise IgnoredException('功能已禁用')
    if isinstance(event, GroupMessageEvent) and _guess_anti_cheat_active(event.group_id):
        await search_bpm.finish('本群正在猜歌，不要作弊哦~', reply_message=True)
    args = message.extract_plain_text().strip().split()
    page = 1
    if len(args) == 1:
        result = mai.total_list.filter(bpm=int(args[0]))
    elif len(args) == 2:
        if (bpm := int(args[0])) > int(args[1]):
            page = int(args[1])
            result = mai.total_list.filter(bpm=bpm)
        else:
            result = mai.total_list.filter(bpm=(bpm, int(args[1])))
    elif len(args) == 3:
        result = mai.total_list.filter(bpm=(int(args[0]), int(args[1])))
        page = int(args[2])
    else:
        await search_bpm.finish(
            '命令格式：\nbpm查歌 「bpm」\nbpm查歌 「bpm下限」「bpm上限」「页数」', 
            reply_message=True
        )
    if not result:
        await search_bpm.finish('没有找到这样的乐曲。', reply_message=True)
    
    search_result = ''
    page = max(min(page, len(result) // SONGS_PER_PAGE + 1), 1)
    result.sort(key=lambda x: int(x.basic_info.bpm))
    
    for i, m in enumerate(result):
        if (page - 1) * SONGS_PER_PAGE <= i < page * SONGS_PER_PAGE:
            search_result += f'{f"「{m.id}」":<7}{f"「BPM {m.basic_info.bpm}」":<9} {m.title} \n'
    search_result += (
        f'第「{page}」页，'
        f'共「{len(result) // SONGS_PER_PAGE + 1}」页。'
        '请使用「id xxxxx」查询指定曲目。'
    )
    await finish_timed_sync(
        search_bpm,
        lambda: MessageSegment.image(text_to_bytes_io(search_result)),
    )


@search_artist.handle()
async def _(event: MessageEvent, message: Message = CommandArg()):
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, 'search'):
        raise IgnoredException('功能已禁用')
    if isinstance(event, GroupMessageEvent) and _guess_anti_cheat_active(event.group_id):
        await search_artist.finish('本群正在猜歌，不要作弊哦~', reply_message=True)
    args = message.extract_plain_text().strip().split()
    page = 1
    if len(args) == 1:
        name = args[0]
    elif len(args) == 2:
        name = args[0]
        if args[1].isdigit():
            page = int(args[1])
        else:
            await search_artist.finish('命令格式：\n曲师查歌「曲师名称」「页数」', reply_message=True)
    else:
        await search_artist.finish('命令格式：\n曲师查歌「曲师名称」「页数」', reply_message=True)

    result = mai.total_list.filter(artist_search=name)
    if not result:
        await search_artist.finish('没有找到这样的乐曲。', reply_message=True)

    search_result = ''
    page = max(min(page, len(result) // SONGS_PER_PAGE + 1), 1)
    for i, m in enumerate(result):
        if (page - 1) * SONGS_PER_PAGE <= i < page * SONGS_PER_PAGE:
            search_result += f'{f"「{m.id}」":<7}{f"「{m.basic_info.artist}」"} - {m.title}\n'
    search_result += (
        f'第「{page}」页，'
        f'共「{len(result) // SONGS_PER_PAGE + 1}」页。'
        '请使用「id xxxxx」查询指定曲目。'
    )
    await finish_timed_sync(
        search_artist,
        lambda: MessageSegment.image(text_to_bytes_io(search_result)),
    )


@search_charter.handle()
async def _(event: MessageEvent, message: Message = CommandArg()):
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, 'search'):
        raise IgnoredException('功能已禁用')
    if isinstance(event, GroupMessageEvent) and _guess_anti_cheat_active(event.group_id):
        await search_bpm.finish('本群正在猜歌，不要作弊哦~', reply_message=True)
    args = message.extract_plain_text().strip().split()
    page = 1
    if len(args) == 1:
        name = args[0]
    elif len(args) == 2:
        name = args[0]
        if args[1].isdigit():
            page = int(args[1])
        else:
            await search_charter.finish('命令格式：\n谱师查歌「谱师名称」「页数」', reply_message=True)
    else:
        await search_charter.finish('命令格式：\n谱师查歌「谱师名称」「页数」', reply_message=True)
    
    result = mai.total_list.filter(charter_search=name)
    if not result:
        await search_charter.finish('没有找到这样的乐曲。', reply_message=True)
    
    search_result = ''
    page = max(min(page, len(result) // SONGS_PER_PAGE + 1), 1)
    for i, m in enumerate(result):
        if (page - 1) * SONGS_PER_PAGE <= i < page * SONGS_PER_PAGE:
            diff_charter = zip([diffs[d] for d in m.diff], [m.charts[d].charter for d in m.diff])
            diff_parts = [
                f"{f'「{d}」':<9}{f'「{c}」'}"
                for d, c in diff_charter
            ]
            diff_str = " ".join(diff_parts)
            line = f"{f'「{m.id}」':<7}{diff_str} {m.title}\n"
            search_result += line
    search_result += (
        f'第「{page}」页，'
        f'共「{len(result) // SONGS_PER_PAGE + 1}」页。'
        '请使用「id xxxxx」查询指定曲目。'
    )
    await finish_timed_sync(
        search_charter,
        lambda: MessageSegment.image(text_to_bytes_io(search_result)),
    )


@search_alias_song.handle()
async def _(bot: Bot, event: MessageEvent, end: str = Endswith()):
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, 'search'):
        raise IgnoredException('功能已禁用')
    name = event.get_plaintext().lower()[0:-len(end)].strip()
    error_msg = (
        f'未找到别名为「{name}」的歌曲\n'
        '※ 可以使用「添加别名」指令给该乐曲添加别名\n'
        '※ 如果是歌名的一部分，请使用「查歌」指令查询哦。'
    )
    # 别名
    alias_data = mai.total_alias_list.by_alias(name)
    if not alias_data:
        obj = await maiApi.get_songs(name)
        if obj:
            if type(obj[0]) == AliasStatus:
                msg = f'未找到别名为「{name}」的歌曲，但找到与此相同别名的投票：\n'
                for _s in obj:
                    msg += f'- {_s.Tag}\n    ID {_s.SongID}: {name}\n'
                msg += f'※ 可以使用指令「同意别名 XXXXX」进行投票'
                await search_alias_song.finish(msg.strip(), reply_message=True)
            else:
                alias_data = obj
    if alias_data:
        if len(alias_data) != 1:
            msg = f'找到{len(alias_data)}个相同别名的曲目：\n'
            for songs in alias_data:
                msg += f'{songs.SongID}：{songs.Name}\n'
            msg += '※ 请使用「id xxxxx」查询指定曲目'
            await search_alias_song.finish(msg.strip(), reply_message=True)
        else:
            music = mai.total_list.by_id(str(alias_data[0].SongID))
            if music:
                await _send_chart_and_tags_forward(bot, event, music, '您要找的是不是：', reply=True)
            else:
                await search_alias_song.finish(error_msg, reply_message=True)
    
    # id
    if name.isdigit() and (music := mai.total_list.by_id(name)):
        await _send_chart_and_tags_forward(bot, event, music, '您要找的是不是：', reply=True)
    if search_id := re.search(r'^id([0-9]*)$', name, re.IGNORECASE):
        music = mai.total_list.by_id(search_id.group(1))
        if music:
            await _send_chart_and_tags_forward(bot, event, music, '您要找的是不是：', reply=True)
    
    # 标题
    result = mai.total_list.filter(title_search=name)
    if len(result) == 0:
        await search_alias_song.finish(error_msg, reply_message=True)
    elif len(result) == 1:
        music = result.random()
        await _send_chart_and_tags_forward(bot, event, music, '您要找的是不是：', reply=True)
    elif len(result) < 50:
        msg = f'未找到别名为「{name}」的歌曲，但找到「{len(result)}」个相似标题的曲目：\n'
        for music in sorted(result, key=lambda x: int(x.id)):
            msg += f'{f"「{music.id}」":<7} {music.title}\n'
        msg += '请使用「id xxxxx」查询指定曲目。'
        await search_alias_song.finish(msg.strip(), reply_message=True)
    else:
        await search_alias_song.finish(
            f'结果过多「{len(result)}」条，请缩小查询范围。', 
            reply_message=True
        )


@query_chart.handle()
async def _(bot: Bot, event: MessageEvent, match=RegexMatched()):
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, 'query'):
        raise IgnoredException('功能已禁用')
    id = match.group(1)
    music = mai.total_list.by_id(id)
    if not music:
        await query_chart.finish(f'未找到ID「{id}」的乐曲')
        return
    await _send_song_info_then_pmyx_forward(bot, event, music, query_chart, reply=True)


@chart_preview.handle()
async def _(event: MessageEvent, match=RegexMatched()):
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, 'query'):
        raise IgnoredException('功能已禁用')
    if isinstance(event, GroupMessageEvent) and _guess_anti_cheat_active(event.group_id):
        await chart_preview.finish('本群正在猜歌，不要作弊哦~', reply_message=True)
    song_id = match.group(1)
    diff_name = match.group(2)
    diff_map = {'绿': 2, '黄': 3, '红': 4, '紫': 5, '白': 6}
    diff_index = diff_map[diff_name] - 2
    music = mai.total_list.by_id(song_id)
    if not music:
        await chart_preview.finish(f'未找到ID「{song_id}」的乐曲', reply_message=True)
        return
    if diff_index >= len(music.ds):
        await chart_preview.finish(f'ID「{song_id}」没有{diff_name}谱', reply_message=True)
        return
    preview_links = dict(_chart_preview_links(music))
    url = preview_links.get(
        diff_name,
        build_preview_url(
            song_id_for_wmc(music),
            kind_for_wmc(music),
            diff_map[diff_name],
        ),
    )
    if use_qq_mode(event):
        # Keep the preview URL in QQ Markdown.  A URL embedded in a rendered
        # text image (or sent as a bare line in a forward summary) cannot be
        # opened from the client.
        await chart_preview.finish(
            build_markdown_link_message(
                f'{music.title} {diff_name}谱预览',
                [(diff_name, url)],
                event=event,
            ),
            reply_message=True,
        )
        return
    await chart_preview.finish(f"{music.title} {diff_name}谱预览：\n{url}", reply_message=True)
