import copy
import json
import os
from pathlib import Path
from collections import defaultdict
from typing import Any, Dict, List, Optional

import httpx

from ..config import TAG_DISPLAY_ORDER, TAG_PILL_COLORS, footer_designed_generated, log


def _get_dxrating_token() -> Optional[str]:
    """优先用插件配置，否则读环境变量 MAIMAIDX_DXRATING_TOKEN。若未读到则尝试加载 .env.prod / .env。"""
    t = getattr(maiconfig, 'dxrating_token', None)
    if t:
        return t
    t = os.environ.get('MAIMAIDX_DXRATING_TOKEN')
    if t:
        return t
    try:
        from pathlib import Path
        from dotenv import load_dotenv
        cwd = Path.cwd()
        for name in ('.env.prod', '.env'):
            p = cwd / name
            if p.is_file():
                load_dotenv(p, override=False)
                t = os.environ.get('MAIMAIDX_DXRATING_TOKEN')
                if t:
                    return t
    except Exception:
        pass
    return None


from .image import rounded_corners, draw_centered_design_footer
from .maimaidx_best_50 import *
from .maimaidx_music import Music, mai


async def get_music_by_alias(alias: str) -> Optional[Music]:
    """
    通过别名获取歌曲信息
    
    Params:
        `alias`: 歌曲别名
    Returns:
        `Music` 对象或 None
    """
    if not alias:
        return None
    
    # 使用别名列表查找歌曲
    aliases = mai.total_alias_list.by_alias(alias)
    if not aliases:
        return None
    
    # 如果找到多个别名匹配，返回第一个匹配的歌曲
    if len(aliases) > 0:
        music_id = str(aliases[0].SongID)
        return mai.total_list.by_id(music_id)
    
    return None


_music_tags_cache: Dict[str, Dict[str, str]] = {}
_tags_file_index: Optional[Dict[str, Dict[str, str]]] = None
_tags_by_difficulty_index: Optional[Dict[tuple, list]] = None
_tags_by_difficulty_and_group: Optional[Dict[tuple, Dict[str, List[str]]]] = None

DIFF_DISPLAY_ORDER = ('BASIC', 'ADVANCED', 'EXPERT', 'MASTER', 'Re:MASTER')
LEVEL_INDEX_TO_SHEET = ('basic', 'advanced', 'expert', 'master', 'remaster')
_SHEET_DIFF_TO_DISPLAY = {'basic': 'BASIC', 'advanced': 'ADVANCED', 'expert': 'EXPERT', 'master': 'MASTER', 'remaster': 'Re:MASTER'}


def _load_tags_from_json() -> Optional[Dict[str, Dict[str, str]]]:
    global _tags_file_index, _tags_by_difficulty_index, _tags_by_difficulty_and_group
    if _tags_file_index is not None:
        return _tags_file_index
    path = getattr(maiconfig, 'dxrating_tags_json_path', None)
    if not path:
        path = Path.cwd() / 'response.json'
    else:
        path = Path(path)
    if not path.is_file():
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        log.warning(f'[maimai] 谱面标签 JSON 加载失败 path={path} err={e}')
        return None
    tags = data.get('tags') or []
    tag_groups = data.get('tagGroups') or []
    tag_songs = data.get('tagSongs') or []
    group_name_by_id = {g.get('id'): (g.get('localized_name') or {}).get('zh-Hans') or (g.get('localized_name') or {}).get('en') or '' for g in tag_groups if g.get('id') is not None}
    tag_by_id = {}
    for t in tags:
        tid = t.get('id')
        if tid is not None:
            name = (t.get('localized_name') or {}).get('zh-Hans') or (t.get('localized_name') or {}).get('en') or ''
            tag_by_id[tid] = (name, t.get('group_id'))
    from collections import defaultdict
    song_tag_ids = defaultdict(set)
    for s in tag_songs:
        title = s.get('song_id')
        if title is not None:
            song_tag_ids[str(title).strip()].add(s.get('tag_id'))
    index = {}
    for title, tag_ids in song_tag_ids.items():
        out = {}
        for tid in tag_ids:
            if tid not in tag_by_id:
                continue
            name, gid = tag_by_id[tid]
            group_name = group_name_by_id.get(gid) or ''
            if group_name and name and group_name not in out:
                out[group_name] = name
        if out:
            index[title] = out
    _tags_file_index = index
    by_diff = defaultdict(set)
    for s in tag_songs:
        title = s.get('song_id')
        diff = (s.get('sheet_difficulty') or '').strip().lower()
        tid = s.get('tag_id')
        if title is not None and diff and tid in tag_by_id:
            name = tag_by_id[tid][0]
            if name:
                by_diff[(str(title).strip(), diff)].add(name)
    _tags_by_difficulty_index = {k: sorted(v) for k, v in by_diff.items()}
    by_diff_group = {}
    for s in tag_songs:
        title = s.get('song_id')
        diff = (s.get('sheet_difficulty') or '').strip().lower()
        tid = s.get('tag_id')
        if title is None or not diff or tid not in tag_by_id:
            continue
        name, gid = tag_by_id[tid]
        group_name = group_name_by_id.get(gid) or ''
        if not name or not group_name:
            continue
        key = (str(title).strip(), diff)
        if key not in by_diff_group:
            by_diff_group[key] = defaultdict(set)
        by_diff_group[key][group_name].add(name)
    _tags_by_difficulty_and_group = {k: {gk: sorted(gv) for gk, gv in v.items()} for k, v in by_diff_group.items()}
    log.info(f'[maimai] 谱面标签已从本地 JSON 加载 共 {len(index)} 曲')
    return _tags_file_index


def _get_tags_from_file(music_title: str) -> Optional[Dict[str, str]]:
    index = _load_tags_from_json()
    if not index:
        return None
    title = (music_title or '').strip()
    return index.get(title) if title else None


def get_music_tags_by_difficulty(music_id: str) -> Dict[str, List[str]]:
    out = {}
    try:
        music = mai.total_list.by_id(music_id)
    except Exception:
        for k in DIFF_DISPLAY_ORDER:
            out[k] = ['暂无']
        return out
    title = (getattr(music, 'title', None) or '').strip()
    has_remaster = len(getattr(music, 'level', [])) >= 5
    _load_tags_from_json()
    index = _tags_by_difficulty_index
    if not index:
        for k in DIFF_DISPLAY_ORDER:
            if k == 'Re:MASTER' and not has_remaster:
                continue
            out[k] = ['暂无']
        return out
    for display_name in DIFF_DISPLAY_ORDER:
        if display_name == 'Re:MASTER' and not has_remaster:
            continue
        sheet_key = None
        for k, v in _SHEET_DIFF_TO_DISPLAY.items():
            if v == display_name:
                sheet_key = k
                break
        if sheet_key is None:
            continue
        tags = index.get((title, sheet_key))
        out[display_name] = sorted(tags) if tags else ['暂无']
    return out


def get_chart_tags_by_group(title: str, level_index: int) -> Dict[str, List[str]]:
    """按曲名与难度索引返回分组标签（配置/难度/评价）。"""
    _load_tags_from_json()
    index = _tags_by_difficulty_and_group
    if not index or not title:
        return {}
    sheet = LEVEL_INDEX_TO_SHEET[min(max(0, level_index), 4)]
    by_group = index.get((title.strip(), sheet))
    return {gk: list(gv) for gk, gv in by_group.items()} if by_group else {}


def get_b50_tag_stats(userinfo, wmc_tags_cache: Optional[Dict[tuple, dict]] = None) -> Dict[str, Dict[str, int]]:
    """根据 B50 用户数据统计各分组标签出现次数，用于底力分析图。
    
    wmc_tags_cache: 可选，预拉取的 v.wmc.pub /tags 结果。
        key=(song_id_str, diff_val), value=API 返回的 tags dict。
        提供时优先使用，否则回退本地 JSON。
    """
    _load_tags_from_json()
    index = _tags_by_difficulty_and_group
    counts = defaultdict(lambda: defaultdict(int))
    has_any = False
    # 查分器 sd=B35、dx=B15（与谱面类型 SD/DX 无关）
    for chart_list in (getattr(userinfo.charts, 'sd', None) or [], getattr(userinfo.charts, 'dx', None) or []):
        if not chart_list:
            continue
        for chart in chart_list:
            song_id = getattr(chart, 'song_id', None)
            level_index = getattr(chart, 'level_index', 0)
            if song_id is None:
                continue
            music = mai.total_list.by_id(str(song_id))
            if not music:
                continue
            title = (getattr(music, 'title', None) or '').strip()
            if not title:
                continue
            # 尝试 v.wmc.pub 缓存
            wmc_used = False
            if wmc_tags_cache:
                wmc_sid = music.id[1:] if music.type == "DX" and music.id.startswith("1") else music.id
                kind = "standard" if music.type == "SD" else "dx"
                diff_val = min(max(level_index + 2, 2), 6)
                cache_key = (wmc_sid, kind, diff_val)
                tags_data = wmc_tags_cache.get(cache_key)
                if tags_data:
                    has_any = True
                    wmc_used = True
                    dc = tags_data.get("difficultyClassification", {})
                    dc_label = dc.get("label", "")
                    if dc_label == "正常谱":
                        counts['难度']['正常谱'] += 1
                    elif dc_label == "水":
                        counts['难度']['水'] += 1
                    elif dc_label in ("诈称谱", "虚高谱"):
                        counts['难度']['诈称谱'] += 1
                    for t in tags_data.get("radarTags", []):
                        counts['配置'][t['label']] += 1
                    for t in tags_data.get("evaluationTags", []):
                        counts['评价'][t['label']] += 1
            if not wmc_used:
                # 回退本地 JSON
                if not index:
                    continue
                sheet = LEVEL_INDEX_TO_SHEET[min(max(0, level_index), 4)]
                by_group = index.get((title, sheet))
                diff_tags = list(by_group.get('难度', [])) if by_group else []
                if by_group:
                    has_any = True
                    for group_name, tags in by_group.items():
                        for tag in tags:
                            counts[group_name][tag] += 1
                if '水' not in diff_tags and '诈称谱' not in diff_tags:
                    if has_any:
                        counts['难度']['正常谱'] += 1
    if not has_any:
        return {'配置': {}, '难度': {}, '评价': {}}
    return {g: dict(counts[g]) for g in ('配置', '难度', '评价')}


async def build_tags_forward_nodes(
    music_id: str, bot_user_id: int, bot_nickname: str
) -> List[Dict[str, Any]]:
    await get_music_tags(music_id)
    tags_by_diff = get_music_tags_by_difficulty(music_id)
    if not tags_by_diff:
        return []
    music = mai.total_list.by_id(music_id)
    title = getattr(music, 'title', None) or f'ID {music_id}'

    def node(user_id: int, nickname: str, text: str) -> Dict[str, Any]:
        return {
            'type': 'node',
            'data': {
                'user_id': str(user_id),
                'nickname': nickname,
                'content': text,
            },
        }

    # 过滤掉标签为 ['暂无'] 的难度
    has_tags_diffs = {}
    for diff in DIFF_DISPLAY_ORDER:
        if diff not in tags_by_diff:
            continue
        tags = tags_by_diff[diff]
        # 如果标签只有 '暂无'，则跳过
        if tags == ['暂无']:
            continue
        has_tags_diffs[diff] = tags

    if not has_tags_diffs:
        return []

    intro = f'这是 {title} 所有谱面相关的标签'
    nodes: List[Dict[str, Any]] = [node(bot_user_id, bot_nickname, intro)]
    for diff, tags in has_tags_diffs.items():
        nodes.append(node(bot_user_id, bot_nickname, f'{diff}：{" ".join(tags)}'))
    return nodes


async def get_music_tags(music_id: str) -> Optional[Dict[str, str]]:
    sid = str(music_id)
    if sid in _music_tags_cache:
        return _music_tags_cache[sid]
    tags = None
    try:
        music = mai.total_list.by_id(music_id)
        if music and getattr(music, 'title', None):
            tags = _get_tags_from_file(music.title)
    except Exception:
        pass
    if not tags:
        tags = await fetch_combined_tags(music_id)
    if tags:
        _music_tags_cache[sid] = tags
    return tags


async def fetch_combined_tags(music_id: str) -> Optional[Dict[str, str]]:
    token = _get_dxrating_token()
    url = getattr(maiconfig, 'dxrating_combined_tags_url', None) or 'https://derrakuma.dxrating.net/functions/v1/combined-tags'
    if not token:
        log.opt(lazy=True).info(
            lambda: '[maimai] 谱面标签未显示: 未配置 dxrating_token（请在 .env 设置 MAIMAIDX_DXRATING_TOKEN 或插件配置 dxrating_token）'
        )
        return None
    try:
        id_num = int(music_id) if str(music_id).strip().isdigit() else music_id
    except (ValueError, TypeError):
        id_num = music_id
    payload = {'ids': [id_num]}
    headers = {
        'Authorization': f'Bearer {token}',
        'Origin': 'https://dxrating.net',
        'Content-Type': 'application/json',
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=payload, headers=headers)
    except Exception as e:
        log.warning(f'[maimai] 谱面标签请求失败 id={music_id} err={type(e).__name__}: {e}')
        return None
    if resp.status_code != 200:
        log.info(f'[maimai] 谱面标签 API 非 200 id={music_id} status={resp.status_code} body={resp.text[:200]}')
        return None
    try:
        data = resp.json()
    except Exception as e:
        log.warning(f'[maimai] 谱面标签响应非 JSON id={music_id} err={e}')
        return None
    sid = str(music_id)
    raw = None
    if isinstance(data, dict):
        raw = data.get(sid) or data.get(id_num)
        if raw is None and 'data' in data:
            raw = data['data'].get(sid) or data['data'].get(id_num)
        if raw is None:
            keys_preview = list(data.keys())[:12]
            log.info(f'[maimai] 谱面标签 响应中无本曲 id={sid} 响应顶层 keys={keys_preview}')
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            mid = item.get('id') or item.get('song_id') or item.get('music_id') or item.get('sid')
            if str(mid) == sid:
                raw = item.get('tags') or item.get('combined_tags') or item
                break
        if raw is None:
            log.info(f'[maimai] 谱面标签 响应为 list 但未找到 id={sid} 的项')
    if raw is None:
        return None
    if isinstance(raw, dict) and not any(k in raw for k in ('tag_category', 'tag_name', 'category', 'tag')):
        out = {k: str(v) for k, v in raw.items() if v}
        if out:
            log.info(f'[maimai] 谱面标签 已获取 id={sid} 标签={list(out.keys())}')
        return out if out else None
    if isinstance(raw, list):
        out = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            cat = item.get('tag_category') or item.get('category')
            name = item.get('tag_name') or item.get('name') or item.get('tag')
            if cat and name:
                out[str(cat)] = str(name)
        if out:
            log.info(f'[maimai] 谱面标签 已获取 id={sid} 标签={list(out.keys())}')
        return out if out else None
    return None


def newbestscore(song_id: str, lv: int, value: int, bestlist: List[ChartInfo]) -> int:
    for v in bestlist:
        if song_id == str(v.song_id) and lv == v.level_index:
            if value >= v.ra:
                return value - v.ra
            else:
                return 0
    return value - bestlist[-1].ra


def _resolve_play_info_cover_overlay(theme: str, genre: str) -> Path:
    """解析 play_info 曲绘框图：共享 genre 图优先，宴会场等无共享图时用主题 info_bg.png。"""
    from ..config import category, maimaidir
    from .maimaidx_theme import pic as _pic, resolve_theme_path as _rtp

    cat = category.get(genre)
    if cat:
        overlay = _pic(f'info_{cat}.png', theme)
        if overlay.exists():
            return overlay
    return _rtp(maimaidir, theme, 'info_bg.png')


async def draw_music_info(
    music: Music, 
    qqid: Optional[int] = None, 
    user: Optional[UserInfo] = None
) -> MessageSegment:
    """
    查看谱面
    
    Params:
        `music`: 曲目模型
        `qqid`: QQID
        `user`: 用户模型
    Returns:
        `MessageSegment`
    """
    calc = True
    isfull = True
    bestlist: List[ChartInfo] = []
    try:
        if qqid:
            if user is None:
                from .maimaidx_datasource import get_user_b50
                player = await get_user_b50(qqid=qqid)
            else:
                player = user
            # bestlist: 查分器 B15(dx) 或 B35(sd) 成绩列表
            # 新曲(is_new)归 B15，否则归 B35，与水鱼 charts.dx/sd 口径一致
            if music.basic_info.is_new:
                bestlist = player.charts.dx   # B15
                isfull = bool(len(bestlist) == 15)
            else:
                bestlist = player.charts.sd   # B35
                isfull = bool(len(bestlist) == 35)
        else:
            calc = False
    except (UserNotFoundError, UserNotExistsError, UserDisabledQueryError):
        calc = False
    except Exception:
        calc = False

    from .maimaidx_theme import Theme as _Th, resolve_theme_path as _rtp
    _theme = _Th.get_default().value
    im = Image.open(_rtp(maimaidir, _theme, 'chart_info.png')).convert('RGBA')
    dr = ImageDraw.Draw(im)
    mr = DrawText(dr, SIYUAN)
    tb = DrawText(dr, TBFONT)

    default_color = (124, 130, 255, 255)

    im.alpha_composite(Image.open(_rtp(maimaidir, _theme, 'logo.png')).resize((249, 120)), (65, 25))
    if music.basic_info.is_new:
        im.alpha_composite(Image.open(pic('UI_CMN_TabTitle_NewSong.png')).resize((249, 120)), (842, 100))
    songbg = Image.open(music_picture(music.id)).resize((242, 242))
    im.alpha_composite(rounded_corners(songbg, 17, (True, False, False, True)), (133, 197))
    im.alpha_composite(Image.open(pic(f'{music.basic_info.version}.png')).resize((182, 90)), (800, 370))
    im.alpha_composite(Image.open(pic(f'{music.type}.png')).resize((80, 30)), (295, 410))

    title = music.title
    if coloumWidth(title) > 40:
        title = changeColumnWidth(title, 39) + '...'
    mr.draw(405, 220, 28, title, default_color, 'lm')
    artist = music.basic_info.artist
    if coloumWidth(artist) > 50:
        artist = changeColumnWidth(artist, 49) + '...'
    mr.draw(407, 265, 20, artist, default_color, 'lm')
    tb.draw(460, 345, 24, music.basic_info.bpm, default_color, 'lm')
    tb.draw(405, 435, 22, f'ID {music.id}', default_color, 'lm')
    mr.draw(665, 435, 24, music.basic_info.genre, default_color, 'mm')

    for num, _ in enumerate(music.level):
        if num == 4:
            color = (255, 255, 255, 255)
        else:
            color = (255, 255, 255, 255)
        spacing = 70 * num
        tb.draw(120, 590 + spacing, 22, f'{music.level[num]}({music.ds[num]:.1f})', color, 'mm')
        tb.draw(
            120, 613 + spacing, 15, 
            f'{round(music.stats[num].fit_diff, 2):.2f}' if music.stats and music.stats[num] else '-', 
            color, 'mm'
        )
        notes = list(music.charts[num].notes)
        # TOTAL 列
        tb.draw(480, 590 + spacing, 25, sum(notes), default_color, 'mm')
        if len(notes) == 4:
            notes.insert(3, '-')
        # 其余 notes 字段从第二列开始 (tap, hold, slide, touch, brk)
        for n, c in enumerate(notes):
            tb.draw(480 + 122 * (n + 1), 590 + spacing, 25, c, default_color, 'mm')
        if num > 1:
            charter = music.charts[num].charter
            if coloumWidth(charter) > 19:
                charter = changeColumnWidth(charter, 18) + '...'
            mr.draw(310, 590 + spacing, 20, charter, default_color, 'mm')
            ra = sorted(
                [computeRa(music.ds[num], r) for r in (100.5, 100.5, 100.0, 99.5, 99.0, 98.0, 97.0)],
                reverse=True,
            )  # AP / SSS+ … S 共 7 列
            for _n, value in enumerate(ra):
                size = 22
                if not calc:
                    rating = value
                elif not isfull:
                    size = 17
                    rating = f'{value}(+{value})'
                elif value > bestlist[-1].ra:
                    new = newbestscore(music.id, num, value, bestlist)
                    if new == 0:
                        rating = value
                    else:
                        size = 17
                        rating = f'{value}(+{new})'
                else:
                    rating = value
                tb.draw(295 + 125 * _n, 1017 + 46 * (num - 2), size, rating, default_color, 'mm')
    draw_centered_design_footer(
        im, mr, footer_designed_generated(),
        color=default_color,
        margin_x=48,
        start_font_size=22,
        min_font_size=9,
        bottom_gap=20,
    )
    return MessageSegment.image(image_to_base64(im))


async def draw_music_play_data(qqid: int, music_id: str) -> Union[str, MessageSegment]:
    """
    谱面游玩
    
    Params:
        `qqid`: QQID
        `music_id`: 曲目ID
    Returns:
        `Union[str, MessageSegment]`
    """
    try:
        from .maimaidx_datasource import get_user_records

        _userinfo, records = await get_user_records(qqid=qqid)
        data = [r for r in records if str(r.song_id) == str(music_id)]
        if not data:
            raise MusicNotPlayError
        music = mai.total_list.by_id(music_id)
        diff: List[Union[None, PlayInfoDev, PlayInfoDefault]] = [
            None for _ in music.ds
        ]
        for record in data:
            if 0 <= int(record.level_index) < len(diff):
                diff[record.level_index] = record
        dev = True

        from .maimaidx_theme import Theme as _Th, resolve_theme_path as _rtp
        _theme = _Th.get_default().value
        im = Image.open(_rtp(maimaidir, _theme, 'play_info.png')).convert('RGBA')
    
        dr = ImageDraw.Draw(im)
        tb = DrawText(dr, TBFONT)
        mr = DrawText(dr, SIYUAN)

        im.alpha_composite(Image.open(_rtp(maimaidir, _theme, 'logo.png')).resize((249, 120)), (42, 34))
        cover = Image.open(music_picture(music_id))
        im.alpha_composite(cover.resize((300, 300)), (100, 260))
        im.alpha_composite(
            Image.open(_resolve_play_info_cover_overlay(_theme, music.basic_info.genre)),
            (100, 260),
        )
        im.alpha_composite(Image.open(pic(f'{music.basic_info.version}.png')).resize((183, 90)), (295, 205))
        im.alpha_composite(Image.open(pic(f'{music.type}.png')).resize((55, 20)), (350, 560))
        
        color = (124, 129, 255, 255)
        artist = music.basic_info.artist
        if coloumWidth(artist) > 58:
            artist = changeColumnWidth(artist, 57) + '...'
        mr.draw(255, 595, 12, artist, color, 'mm')
        title = music.title
        if coloumWidth(title) > 38:
            title = changeColumnWidth(title, 37) + '...'
        mr.draw(255, 622, 18, title, color, 'mm')
        tb.draw(160, 720, 22, music.id, color, 'mm')
        tb.draw(380, 720, 22, music.basic_info.bpm, color, 'mm')

        y = 100
        for num, info in enumerate(diff):
            im.alpha_composite(Image.open(pic(f'd_{num}.png')), (650, 235 + y * num))
            if info:
                im.alpha_composite(Image.open(_rtp(maimaidir, _theme, 'ra_dx.png')).resize((102, 44)), (850, 272 + y * num))
                if dev:
                    dxscore = info.dxScore
                    _dxscore = sum(music.charts[num].notes) * 3
                    dx_pct = (dxscore / _dxscore * 100) if _dxscore else 0
                    dxnum = dxScore(min(100, max(0, dx_pct)))
                    rating, rate = info.ra, score_Rank_l[info.rate]
                    if dxnum != 0:
                        im.alpha_composite(
                            Image.open(pic(f'UI_GAM_Gauge_DXScoreIcon_0{dxnum}.png')).resize((32, 19)), 
                            (851, 296 + y * num)
                        )
                    tb.draw(916, 304 + y * num, 13, f'{dxscore}/{_dxscore}', color, 'mm')
                else:
                    rating, rate = computeRa(music.ds[num], info.achievements, israte=True)
                
                im.alpha_composite(Image.open(pic('fcfs.png')), (965, 265 + y * num))
                if info.fc:
                    im.alpha_composite(
                        Image.open(pic(f'UI_CHR_PlayBonus_{fcl[info.fc]}.png')).resize((65, 65)), 
                        (960, 261 + y * num)
                    )
                if info.fs:
                    im.alpha_composite(
                        Image.open(pic(f'UI_CHR_PlayBonus_{fsl[info.fs]}.png')).resize((65, 65)), 
                        (1025, 261 + y * num)
                    )
                im.alpha_composite(
                    Image.open(_rtp(maimaidir, _theme, f'UI_TTR_Rank_{rate}.png')).resize((100, 45)), 
                    (737, 272 + y * num)
                )

                tb.draw(500, 295 + y * num, 30, f'{info.achievements:.4f}%', color, 'lm')
                tb.draw(685, 248 + y * num, 20, music.ds[num], anchor='mm')
                tb.draw(915, 283 + y * num, 18, rating, color, 'mm')
            else:
                tb.draw(685, 248 + y * num, 25, music.ds[num], anchor='mm')
                mr.draw(800, 302 + y * num, 30, '未游玩', color, 'mm')
        if len(diff) == 4:
            mr.draw(800, 302 + y * 4, 30, '没有该难度', color, 'mm')

        draw_centered_design_footer(
            im, mr, footer_designed_generated(),
            color=color,
            margin_x=48,
            start_font_size=18,
            min_font_size=9,
            bottom_gap=18,
        )
        msg = MessageSegment.image(image_to_base64(im))
        
    except (UserNotFoundError, UserNotExistsError, UserDisabledQueryError, MusicNotPlayError) as e:
        msg = str(e)
    except Exception as e:
        log.error(traceback.format_exc())
        msg = format_command_error(e)
    return msg


def calc_achievements_fc(scorelist: Union[List[float], List[str]], lvlist_num: int, isfc: bool = False) -> int:
    r = -1
    obj = range(4) if isfc else achievementList[-6:]
    for __f in obj:
        if len(list(filter(lambda x: x >= __f, scorelist))) == lvlist_num:
            r += 1
        else:
            break
    return r


def draw_rating(rating: str, path: Path) -> MessageSegment:
    """绘制只有等级文本的定数表（与 beta 一致）"""
    from .maimaidx_table_image import TableImageAssets

    TableImageAssets.ensure_loaded()
    im = Image.open(path).convert('RGBA')
    dr = ImageDraw.Draw(im)
    fot = DrawText(dr, TBFONT)
    fot.draw(495, 220, 70, 'Level.', TableImageAssets.font_color, 'ld', 8, (255, 255, 255, 255))
    fot.draw(750, 220, 100, rating, TableImageAssets.font_color, 'ld', 8, (255, 255, 255, 255))
    return MessageSegment.image(image_to_base64(im))


async def draw_rating_table(qqid: int, rating: str, isfc: bool = False) -> Union[MessageSegment, str]:
    """绘制定数表（布局与 beta 分支一致）"""
    from .maimaidx_table_image import RatingGridConfig, TableImageAssets, rating_table_path

    try:
        from .maimaidx_update_plate import rating_table_is_current

        if not rating_table_is_current(rating):
            return (
                f'Level {rating} 完成表底图缺失或已过期。\n'
                '请联系管理员私聊机器人执行「更新定数表」后再试。'
            )
        TableImageAssets.ensure_loaded()
        assets = TableImageAssets
        from .maimaidx_datasource import get_user_records
        _userinfo, obj = await get_user_records(qqid=qqid)

        from ..config import COMBO_SP, STATISTICS_KEYS, SYNC_D_SP, score_Rank_l

        statistics = {k: 0 for k in STATISTICS_KEYS}
        played_map: Dict[int, Dict[int, dict]] = {}
        rank_sp = score_Rank[-6:]

        for _d in obj:
            if _d.level != rating:
                continue
            played_map.setdefault(_d.song_id, {})[_d.level_index] = {
                'achievements': _d.achievements,
                'fc': _d.fc,
            }
            rate = computeRa(_d.ds, _d.achievements, onlyrate=True).lower()
            if _d.achievements >= 80:
                statistics['clear'] += 1
            if rate in rank_sp:
                for r in rank_sp[: rank_sp.index(rate) + 1]:
                    statistics[r] += 1
            if _d.fc and _d.fc in COMBO_SP:
                for f in COMBO_SP[: COMBO_SP.index(_d.fc) + 1]:
                    statistics[f] += 1
            if _d.fs:
                if _d.fs == 'sync':
                    statistics['sync'] += 1
                elif _d.fs in SYNC_D_SP:
                    for s in SYNC_D_SP[: SYNC_D_SP.index(_d.fs) + 1]:
                        statistics[s] += 1

        lv_data = mai.total_level_data[rating]
        total_songs_count = sum(len(v) for v in lv_data.values())
        achievements_or_fc_list: List[Union[float, int]] = []

        im = Image.open(rating_table_path(rating)).convert('RGBA')
        dr = ImageDraw.Draw(im)
        tb = DrawText(dr, TBFONT)
        fot = DrawText(dr, TBFONT)

        fot.draw(495, 160, 70, 'Level.', assets.font_color, 'ld', 8, (255, 255, 255, 255))
        fot.draw(750, 160, 100, rating, assets.font_color, 'ld', 8, (255, 255, 255, 255))
        im.alpha_composite(assets.table_complete_bg, (251, 190))
        tb.draw(
            394, RatingGridConfig.stats_first_line_y, 30,
            f"{statistics['clear']}/{total_songs_count}",
            assets.default_text_color, 'mm', 5, (255, 255, 255, 255),
        )
        for n, key in enumerate(STATISTICS_KEYS[1:]):
            if n < 6:
                x = RatingGridConfig.stats_first_line_x + (n % 6) * 102
                y = RatingGridConfig.stats_first_line_y
            else:
                x = RatingGridConfig.stats_second_line_x + ((n - 6) % 9) * 102
                y = RatingGridConfig.stats_second_line_y
            tb.draw(x, y, 30, statistics[key], assets.default_text_color, 'mm', 2, (255, 255, 255, 255))

        current_y = RatingGridConfig.start_y
        from .maimaidx_theme import Theme
        theme = Theme.get_default().value
        for ra, songs in lv_data.items():
            # 与底图生成 (update_rating_table) 一致：跳过空定数分组，避免覆盖层逐段下移错位
            if not songs:
                continue
            for num, music in enumerate(songs):
                row, col = divmod(num, RatingGridConfig.row_count)
                x = RatingGridConfig.start_x + col * RatingGridConfig.gap
                y = current_y + row * RatingGridConfig.gap
                record_map = played_map.get(int(music.id))
                if record_map is None:
                    continue
                record = record_map.get(int(music.lv))
                if record is None:
                    continue
                if not isfc:
                    achievements_or_fc_list.append(record['achievements'])
                    bg = assets.rating_complete_bg if record['achievements'] >= 100 else assets.rating_unfinished_bg
                    im.alpha_composite(bg, (x + 1, y + 1))
                    rate = computeRa(music.ds, record['achievements'], onlyrate=True)
                    rank_icon = assets.get_rank_icon(rate, theme)
                    if rank_icon:
                        im.alpha_composite(rank_icon.resize((78, 35)), (x, y + 20))
                    continue
                if record['fc']:
                    achievements_or_fc_list.append(combo_rank.index(record['fc']))
                    im.alpha_composite(assets.rating_complete_bg, (x + 1, y + 1))
                    fc_icon = assets.get_fc_icon(record['fc'])
                    if fc_icon:
                        im.alpha_composite(fc_icon, (x + 15, y + 13))
            current_y = RatingGridConfig.advance_group_y(current_y, len(songs))

        if len(achievements_or_fc_list) == total_songs_count:
            r = calc_achievements_fc(achievements_or_fc_list, total_songs_count, isfc)
            if r != -1:
                pic_name = fcl[combo_rank[r]] if isfc else score_Rank_l[score_Rank[-6:][r]]
                im.alpha_composite(Image.open(pic(f'UI_MSS_Allclear_Icon_{pic_name}.png')), (40, 40))

        final_im = im.resize((int(im.size[0] * 0.8), int(im.size[1] * 0.8)), Image.Resampling.LANCZOS)
        msg = MessageSegment.image(image_to_base64(final_im))
    except (UserNotFoundError, UserNotExistsError, UserDisabledQueryError) as e:
        msg = str(e)
    except Exception as e:
        log.error(traceback.format_exc())
        msg = format_command_error(e)
    return msg


_PLATE_GRID_START_X = 180
_PLATE_GRID_START_Y = 490
_PLATE_GRID_GAP = 96
_PLATE_GRID_ROW_COUNT = 12
_PLATE_PLAN_FS = ['fsd', 'fdx', 'fsdp', 'fdxp']


def _plate_is_remaster(song_id: int, wu_remaster: List[int]) -> bool:
    return song_id in wu_remaster


def _plate_display_level(music: Music, is_wu: bool, wu_remaster: List[int]) -> str:
    if is_wu and _plate_is_remaster(int(music.id), wu_remaster) and len(music.level) > 4:
        return music.level[4]
    return music.level[3]


def _plate_sort_ds(music: Music, is_wu: bool, wu_remaster: List[int]) -> float:
    if is_wu and _plate_is_remaster(int(music.id), wu_remaster) and len(music.ds) > 4:
        return music.ds[4]
    return music.ds[3]


def _plate_sort_key(music: Music, is_wu: bool, wu_remaster: List[int]) -> tuple[float, int]:
    """与 update_plate 底图一致：定数降序，同定数按曲目 ID 升序（避免覆盖层与底图错位）。"""
    return (-_plate_sort_ds(music, is_wu, wu_remaster), int(music.id))


def _plate_slot_count(music: Music, is_wu: bool, wu_remaster: List[int]) -> int:
    if not is_wu:
        return 4
    return 5 if _plate_is_remaster(int(music.id), wu_remaster) else 4


def _plate_is_qualified(play: Optional[PlayInfoDefault], plan: str) -> bool:
    from ..config import COMBO_SP

    if play is None:
        return False
    if plan in ('极', '極'):
        return play.fc in COMBO_SP
    if plan == '将':
        return play.achievements >= 100
    if plan == '者':
        return play.achievements >= 80
    if plan == '神':
        return play.fc in ('ap', 'app')
    if plan == '舞舞':
        return play.fs in _PLATE_PLAN_FS
    return False


def _plate_get_icon(play: PlayInfoDefault, plan: str, theme: str) -> Image.Image:
    from .maimaidx_theme import resolve_theme_path as _rtp

    if plan == '将':
        rate = computeRa(play.ds, play.achievements, onlyrate=True)
        return Image.open(_rtp(maimaidir, theme, f'UI_TTR_Rank_{rate}.png')).convert('RGBA').resize((80, 36))
    if plan in ('极', '極', '神'):
        return Image.open(pic(f'UI_CHR_PlayBonus_{fcl[play.fc]}.png')).convert('RGBA').resize((60, 60))
    return Image.open(pic(f'UI_CHR_PlayBonus_{fsl[play.fs]}.png')).convert('RGBA').resize((60, 60))


def _plate_badge_paths(version: str, plate_key: str, plan: str) -> List[Path]:
    """Return badge candidates for split-name and combined-name asset packs."""
    suffix = '極' if plan == '极' else plan
    prefixes = dict.fromkeys((version, plate_key))
    return [plate_versiondir / f'{prefix}{suffix}.png' for prefix in prefixes if prefix]


def _load_plate_badge(version: str, plate_key: str, plan: str) -> Optional[Image.Image]:
    candidates = _plate_badge_paths(version, plate_key, plan)
    for path in candidates:
        try:
            with Image.open(path) as image:
                image.load()
                return image.convert('RGBA').resize((1000, 161))
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as exc:
            log.warning(f'完成表徽标资源损坏: {path.name}: {type(exc).__name__}: {exc}')
    log.error(f'完成表徽标资源不可用: {[path.name for path in candidates]}')
    return None


async def draw_plate_table(
    qqid: int,
    version: str,
    plan: str,
    page: int = 1,
) -> Union[MessageSegment, str]:
    """
    绘制完成表（布局与 beta 分支 nonebot-plugin-maimaidx 一致）

    Params:
        `qqid`: QQID
        `version`: 版本
        `plan`: 计划
        `page`: 页数（舞/霸者牌子分页，默认 1）
    Returns:
        `Union[MessageSegment, str]`
    """
    try:
        if version in platecn:
            version = platecn[version]

        is_wu = version in ['舞', '霸']
        plate_file = f'舞-{page}.png' if is_wu else f'{version}.png'

        if version in version_map:
            ver, _ver = version_map[version]
        elif version in plate_to_dx_version:
            ver, _ver = [plate_to_dx_version[version]], version
        else:
            return f'未找到版本 {version} 的牌子数据'

        from ..config import resolve_plate_id_list

        music_id_list = resolve_plate_id_list(mai.total_plate_id_list, _ver)
        if not music_id_list:
            return (
                f'未找到版本 {version}（{_ver}）的牌子曲目列表。\n'
                '别名库可能尚未同步该版本，请稍后重试或联系管理员更新牌子数据。'
            )
        music = mai.total_list.by_id_list(music_id_list)
        plate_total_num = len(music_id_list)
        wu_remaster = mai.total_plate_id_list.get('舞ReMASTER', []) if is_wu else []
        remaster_count = len(wu_remaster)
        slot_num = 5 if is_wu else 4

        playerdata: List[PlayInfoDefault] = []
        from .maimaidx_datasource import get_user_records
        _userinfo, obj = await get_user_records(qqid=qqid)
        for _d in obj:
            if _d.song_id not in music_id_list:
                continue
            _music = mai.total_list.by_id(_d.song_id)
            if _music is None or not 0 <= _d.level_index < len(_music.ds):
                continue
            playerdata.append(
                _d.model_copy(update={'ds': round(float(_music.ds[_d.level_index]), 1)})
            )

        # 按 reversed(levelList) 分组，组内按定数降序（与 update_plate 底图一致）
        level_songs: Dict[str, Dict[str, List[Optional[PlayInfoDefault]]]] = {
            lv: {} for lv in reversed(levelList)
        }
        for _m in music:
            _key = _plate_display_level(_m, is_wu, wu_remaster)
            slots = _plate_slot_count(_m, is_wu, wu_remaster)
            level_songs[_key][_m.id] = [None for _ in range(slots)]

        for _d in playerdata:
            if not is_wu and _d.level_index == 4:
                continue
            sid = str(_d.song_id)
            _music = mai.total_list.by_id(_d.song_id)
            if _music is None or len(_music.level) <= 3:
                continue
            if is_wu and _d.song_id in wu_remaster and len(_music.level) > 4:
                _key = _music.level[4]
            else:
                _key = _music.level[3]
            if _key not in level_songs or sid not in level_songs[_key]:
                continue
            if _d.level_index < len(level_songs[_key][sid]):
                level_songs[_key][sid][_d.level_index] = _d

        ordered_levels: List[tuple[str, List[tuple[str, List[Optional[PlayInfoDefault]]]]]] = []
        for lv in reversed(levelList):
            songs = level_songs.get(lv)
            if not songs:
                continue
            sorted_items = sorted(
                songs.items(),
                key=lambda item: _plate_sort_key(mai.total_list.by_id(item[0]), is_wu, wu_remaster),
            )
            ordered_levels.append((lv, sorted_items))

        level_keys = [lv for lv, _ in ordered_levels]
        if is_wu:
            idx = level_keys.index('13') if '13' in level_keys else len(level_keys)
            display_levels = set(level_keys[:idx] if page == 1 else level_keys[idx:])
        else:
            display_levels = set(level_keys)

        from .maimaidx_theme import Theme as _Th
        from .maimaidx_table_image import PlateGridConfig, TableImageAssets
        from .maimaidx_update_plate import plate_table_is_current

        image_key = plate_file.removesuffix('.png')
        if not plate_table_is_current(image_key, _ver, is_wu=is_wu):
            return (
                f'{version}代完成表底图缺失或已过期。\n'
                '请联系管理员私聊机器人执行「更新完成表」后再试。'
            )

        TableImageAssets.ensure_loaded()
        assets = TableImageAssets
        _theme = _Th.get_default().value
        progress_width = 176 if is_wu else 230

        im = Image.open(plate_tabledir / plate_file).convert('RGBA')
        draw = ImageDraw.Draw(im)
        fot = DrawText(draw, TBFONT)

        progress_bg = assets.plate_progress_wu_bg if is_wu else assets.plate_progress_bg
        im.alpha_composite(progress_bg, (175, 20))
        badge = _load_plate_badge(version, _ver, plan)
        if badge is None:
            return (
                f'{version}{plan}完成表徽标资源缺失或损坏。\n'
                '请联系管理员更新静态资源后再试。'
            )
        im.alpha_composite(badge, (200, 45))

        slot_counts = [0 for _ in range(slot_num)]
        finished_songs: set[int] = set()
        current_y = PlateGridConfig.start_y

        for level, song_list in ordered_levels:
            is_current_page = level in display_levels
            rows = (len(song_list) - 1) // PlateGridConfig.row_count + 1

            for idx, (song_id, results) in enumerate(song_list):
                qualified_slots = [
                    n for n, play in enumerate(results) if _plate_is_qualified(play, plan)
                ]
                if len(qualified_slots) == len(results):
                    finished_songs.add(int(song_id))
                for n in qualified_slots:
                    slot_counts[n] += 1

                if not is_current_page:
                    continue

                row, col = divmod(idx, PlateGridConfig.row_count)
                x = PlateGridConfig.start_x + col * PlateGridConfig.gap
                y = current_y + row * PlateGridConfig.gap

                last_idx = len(results) - 1
                if last_idx in qualified_slots:
                    play = results[last_idx]
                    im.alpha_composite(assets.plate_complete_bg, (x + 1, y + 1))
                    icon = _plate_get_icon(play, plan, _theme)
                    dest = (x, y + 22) if plan == '将' else (x + 10, y + 12)
                    im.alpha_composite(icon, dest)

                for n in qualified_slots:
                    if is_wu and len(results) == 5:
                        im.alpha_composite(
                            assets.plate_finished_bg[n].resize((14, 14)),
                            (x + 1 + 16 * n, y + 64),
                        )
                    else:
                        im.alpha_composite(
                            assets.plate_finished_bg[n],
                            (x + 4 + 19 * n, y + 63),
                        )

            if is_current_page:
                current_y += rows * PlateGridConfig.gap + 30

        default_text_color = assets.default_text_color
        id_text_color = assets.id_text_color

        completed_count = len(finished_songs)
        if completed_count == plate_total_num:
            text = 'COMPLETED!!!'
        else:
            text = f'{completed_count}/{plate_total_num}'
        progress = completed_count / plate_total_num if plate_total_num else 0

        if progress:
            bar = assets.plate_progress_big.crop((0, 0, int(993 * progress), 92))
            im.alpha_composite(bar, (204, 219))

        fot.draw(700, 240, 30, text, default_text_color, 'mm', 3, (255, 255, 255, 255))
        fot.draw(1190, 240, 30, f'{round(progress * 100, 2)}%', default_text_color, 'rm', 3, (255, 255, 255, 255))

        if is_wu:
            stats_start_x = 292
            stats_gap_x = 204
            progress_text_x = 89
            progress_bar_x = 88
        else:
            stats_start_x = 320
            stats_gap_x = 253
            progress_text_x = 115
            progress_bar_x = 115

        stats_start_y = 300
        for _l in range(slot_num):
            x = stats_start_x + _l * stats_gap_x
            complete_sum_group = slot_counts[_l]
            plate_count = remaster_count if is_wu and _l == 4 else plate_total_num
            progress_group = complete_sum_group / plate_count if plate_count else 0
            progress_small = assets.plate_progress_small_wu if is_wu else assets.plate_progress_small

            if progress_group:
                bar_group = progress_small.crop((0, 0, int(progress_width * progress_group), 46))
                im.alpha_composite(bar_group, (x - progress_bar_x, 326))

            if complete_sum_group == plate_count and plate_count > 0:
                fot.draw(x, stats_start_y, 24, 'COMPLETED!!!', id_text_color[_l], 'mm', 4, (255, 255, 255, 255))
            else:
                fot.draw(x, stats_start_y, 40, complete_sum_group, id_text_color[_l], 'mm', 4, (255, 255, 255, 255))

            fot.draw(x + progress_text_x, stats_start_y + 20, 14, f'/{plate_count}', id_text_color[_l], 'rd', 3, (255, 255, 255, 255))
            fot.draw(x + progress_text_x, 343, 20, f'{round(progress_group * 100, 2)}%', default_text_color, 'rm', 2, (255, 255, 255, 255))

        msg = MessageSegment.image(image_to_base64(im))
    except (UserNotFoundError, UserNotExistsError, UserDisabledQueryError) as e:
        msg = str(e)
    except Exception as e:
        log.error(traceback.format_exc())
        msg = format_command_error(e)
    return msg
