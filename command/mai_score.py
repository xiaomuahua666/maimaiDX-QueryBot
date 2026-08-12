import json
import re

from nonebot import get_bot, on_command, on_regex
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageEvent, MessageSegment
from nonebot.exception import IgnoredException
from nonebot.params import CommandArg, Depends, RegexMatched

from ..libraries.maimaidx_api_data import maiApi
from ..libraries.maimaidx_error import (
    BreakInsufficientError,
    QBindRequiredError,
    UserDisabledQueryError,
    UserNotFoundError,
    UserNotExistsError,
)
from ..libraries.maimaidx_platform import (
    adapt_reply_payload,
    billing_user_id,
    format_forward_nodes_as_text,
    parse_at_target_id,
    plugin_finish,
    rank_text_image,
    resolve_score_qqid,
)
from ..libraries.maimaidx_group_rating import (
    group_weak_rank,
    group_rating_ranking,
    group_gain_ranking,
    group_sun_lock_ranking,
    build_forward_node,
    get_group_member_ratings,
    group_song_my_rank,
    group_song_leaderboard,
    render_group_rating_board,
    render_group_weak_board,
    render_group_gain_board,
    render_group_sun_lock_board,
)
from ..libraries.maimaidx_score_formatter import (
    format_leaderboard_text,
    format_score_line_from_dict,
    get_difficulty_name,
)
from ..libraries.maimaidx_song_resolver import SongResolver
from ..libraries.maimaidx_music import feature_manager
from ..config import log, maiconfig
from ..libraries.maimaidx_music_info import get_b50_tag_stats
from ..libraries.maimaidx_music_info import *
from ..libraries.maimaidx_player_score import *
from ..libraries.maimaidx_best_50 import (
    generate,
    generate_all,
    generate_coop_b50,
    generate_coop_all_b50,
    generate_lock_b50,
    generate_lock_all_b50,
    generate_yueji_b50,
    generate_yueji_all_b50,
    generate_version_b50,
    generate_difficulty_b50,
    generate_difficulty_all_b50,
    generate_ideal_b50,
    generate_ideal_all_b50,
    generate_b50_bird_rate,
    generate_b50_bird_plus_rate,
)
from ..libraries.maimaidx_data_storage import data_storage
from ..libraries.maimaidx_data_scheduler import fetch_and_store_user_scores
from ..libraries.maimaidx_plate_count import (
    fetch_dev_records_as_score_records,
    format_plate_count_message,
    format_plate_count_message_from_records,
)
from ..libraries.maimaidx_progress_report import (
    generate_daily_report,
    generate_progress_report,
    generate_progress_report_between,
)
from ..libraries.maimaidx_reaction import react_processing
from ..libraries.maimaidx_gain_recommend import generate_today_gain_recommendation_image
from ..libraries.maimaidx_floor import generate_floor_query
from ..libraries.maimaidx_friend_battle import (
    check_friend_battle_cooldown,
    group_friend_battle_ranking,
    mark_friend_battle_used,
    parse_friend_battle_args,
    run_friend_battle,
    run_friend_battle_batch,
)
from ..libraries.maimaidx_friend_battle_draw import draw_friend_battle_image
from ..libraries.maimaidx_friend_battle_batch_draw import draw_friend_battle_batch_image
from ..libraries.maimaidx_gold_water import generate_gold_content, generate_water_content
from ..libraries.maimaidx_rating_compare import generate_how_weak
from ..libraries.maimaidx_tag_analysis import draw_analysis, image_to_message_segment
from ..libraries.maimaidx_weakness_prescription import generate_weakness_prescription
from ..libraries.maimaidx_b50_risk import generate_b50_risk_warning
from ..libraries.maimaidx_head_to_head import generate_head_to_head
from ..libraries.maimaidx_rating_sandbox import generate_rating_sandbox
from ..libraries.maimaidx_wmc_api import (
    WmcAPI,
    diff_value_for_wmc,
    kind_for_wmc,
    make_chart_key,
    resolve_wmc_base_url,
    song_id_for_wmc,
)
from ..libraries.maimaidx_update_plate import *

best50       = on_command('b50', aliases={'B50'})
best_all50   = on_command('ab50', aliases={'a50', 'allb50'})
b50_bird_rate      = on_command('b50鸟率', aliases={'B50鸟率', 'b50鳥率'})
b50_bird_plus_rate = on_command('b50鸟加率', aliases={'B50鸟加率', 'b50鳥加率', 'b50鸟+率'})
refresh_b50  = on_command('刷新b50', aliases={'刷新成绩', '更新b50', '刷新B50'})
coop_b50     = on_command('合作b50', aliases={'合作B50'})
coop_ab50    = on_command('合作a50', aliases={'合作A50'})
how_weak     = on_command('我有多菜', aliases={'我有多菜'})
gold_content = on_command('含金量', aliases={'含金量'})
water_content = on_command('含水量', aliases={'含水量'})
fcb50        = on_command('fcb50', aliases={'fc50'})
fcallb50     = on_command('fcallb50', aliases={'fcallb50', 'fca50'})
apb50        = on_command('apb50', aliases={'ap50'})
apallb50     = on_command('apallb50', aliases={'apallb50', 'apa50'})
fit_b50      = on_command('拟合b50', aliases={'拟合50'})
fit_all_b50  = on_command('拟合b50全部', aliases={'拟合b50全部', '拟合allb50', '拟合a50'})
sun_b50      = on_command('寸b50', aliases={'寸50'})
sun_all_b50  = on_command('寸ab50', aliases={'寸a50', '寸allb50'})
lock_b50     = on_command('锁血b50', aliases={'锁血50', '名刀50', '名刀b50'})
lock_ab50    = on_command('锁血ab50', aliases={'锁血a50'})
yueji_b50    = on_command('越级b50', aliases={'越级50'})
yueji_ab50   = on_command('越级ab50', aliases={'越级a50'})
# 允许首尾及中间空格/换行，避免 QQ 换行导致「双\n代b50」无法匹配
version_b50  = on_regex(r'^\s*([初真超檄橙暁晓桃櫻樱紫菫堇白雪輝辉霸舞熊华華爽煌宙星祭祝双宴镜彩丸圆])\s*代\s*b50\s*$')
legacy_b50   = on_regex(r'^\s*l\s*(.+代)\s*b50\s*$')
legacy_b35   = on_regex(r'^\s*l\s*(.+代)\s*b35\s*$')
dx2025_b50   = on_command('dx2025b50', aliases={'DX2025b50'})
dx2026_b35   = on_command('dx2026b35', aliases={'DX2026b35'})
# 难度 B50：交给 DifficultyFilter 解析（支持：紫14+、13-14、master14.0 等）
# 注意：排除纯 "b50/ab50"，避免与 on_command('b50/ab50') 冲突
# 以及排除某些 b50 别名（如 名刀b50、分析b50），避免被当作「任意筛选+b50」误解析
# priority 低于各专用 on_command，防止误匹配后返回空图
difficulty_b50 = on_regex(
    r'^\s*(?!b50\s*$)(?!名刀\s*b50\s*$)(?!l\s*.+?代\s*b50\s*$)'
    r'(?!分析\s*b50\s*$)(?!理想\s*b50\s*$)(?!拟合\s*b50\s*$)'
    r'(?!刷新\s*b50\s*$)(?!更新\s*b50\s*$)(?!合作\s*b50\s*$)'
    r'(?!fcb50\s*$)(?!fcallb50\s*$)(?!apb50\s*$)(?!apallb50\s*$)'
    r'(?!寸\s*b50\s*$)(?!锁血\s*b50\s*$)(?!越级\s*b50\s*$)'
    r'(?!dx2025b50\s*$)(?!b50\s*(?:风险(?:预警)?|预警)\s*$)'
    r'(.+?)\s*b50\s*$',
    flags=re.IGNORECASE,
    priority=15,
)
difficulty_ab50 = on_regex(r'^\s*(?!ab50\s*$)(?!理想\s*ab50\s*$)(?!拟合\s*.+ab50\s*$)(.+?)\s*ab50\s*$', flags=re.IGNORECASE, priority=15)
# 理想 B50
ideal_b50 = on_command('理想b50', aliases={'理想B50'})
ideal_ab50 = on_command('理想ab50', aliases={'理想a50', '理想allb50'})
# 数据存储
enable_data_storage = on_command(
    '开启存储数据', aliases={'开启数据存储', '开启储存数据', '开启数据储存'}
)
disable_data_storage = on_command(
    '关闭存储数据', aliases={'关闭数据存储', '关闭储存数据', '关闭数据储存'}
)
store_data_now = on_command(
    '立即存储数据', aliases={'存储数据', '立即储存数据', '储存数据'}
)
storage_history = on_command('存储历史', aliases={'查询存储历史', '存储记录'})
storage_snapshot = on_command('查看存档', aliases={'查看存储快照', '存档详情'})
awmcnet_trend = on_command(
    '成绩趋势', aliases={'rating趋势', 'mai趋势', 'awmc趋势', 'AWMC趋势'}
)
weekly_report = on_command('周报', aliases={'成绩周报', 'maimai周报'})
monthly_report = on_command('月报', aliases={'成绩月报', 'maimai月报'})
annual_report = on_command('年报', aliases={'成绩年报', 'maimai年报'})
daily_report = on_command('日报', aliases={'成绩日报', 'maimai日报'})
today_gain_recommend = on_command('今日吃分推荐', aliases={'吃分推荐', '今日推分推荐'})
floor_query = on_command('地板', aliases={'b50地板', 'rating地板'})
plate_count_stats = on_command('牌子统计', aliases={'统计牌子'})
compare_report = on_command('对比存档', aliases={'存档对比', '报告对比'})
tag_analysis = on_command('底力分析', aliases={'底力分析'})
weakness_prescription = on_command('弱项处方', aliases={'弱项处方单', '底力处方', '练习推荐'})
b50_risk_warning = on_regex(
    r'^\s*(?:b50\s*(?:风险(?:预警)?|预警)|风险预警)\s*$',
    flags=re.IGNORECASE,
    priority=0,
    block=True,
)
head_to_head = on_command('对战战绩', aliases={'headtohead', 'h2h', '对决战绩'})
rating_sandbox = on_command('目标rating', aliases={'rating沙盘', '目标分', '推分沙盘'})
minfo   = on_command('minfo', aliases={'minfo', 'Minfo', 'MINFO', 'info', 'Info', 'INFO'})
ginfo   = on_command('ginfo', aliases={'ginfo', 'Ginfo', 'GINFO'})
score   = on_command('分数线')

# 群内 rating：我在群里有多菜、群聊 rating 排行榜
group_weak = on_command('我在群里有多菜', aliases={'我在群里有多菜'})
group_rating_leaderboard = on_command('群聊rating排行榜', aliases={'群聊rating排行榜'})
group_gain_board = on_command('群吃分榜', aliases={'群内吃分榜', '吃分榜'})
group_sun_board = on_command('群寸止榜', aliases={'群内寸止榜', '寸止榜'})
group_lock_board = on_command('群锁血榜', aliases={'群内锁血榜', '锁血榜'})
friend_battle = on_command('友人对战', aliases={'好友对战'})
friend_battle_rank = on_command('友人对战排行', aliases={'友人排行', '友人对战排名'})


_B50_SHORTCUTS = (
    ('标准 B50', 'b50'),
    ('全部 A50', 'a50'),
    ('AP50', 'ap50'),
    ('FC50', 'fc50'),
    ('寸50', '寸50'),
    ('名刀50', '名刀50'),
    ('越级50', '越级50'),
    ('拟合50', '拟合50'),
    ('含金量', '含金量'),
    ('含水量', '含水量'),
    ('B50 地板', 'b50地板'),
    ('刷新 B50', '刷新b50'),
    ('自动上传 B50', 'maiua'),
    ('PC50', 'pc50'),
    ('我的 PC', '我的pc数'),
)
_CONTENT_SHORTCUTS = (
    ('含金量', '含金量'),
    ('含水量', '含水量'),
    ('我有多菜', '我有多菜'),
    ('标准 B50', 'b50'),
)
_REPORT_SHORTCUTS = (
    ('日报', '日报'),
    ('周报', '周报'),
    ('月报', '月报'),
    ('年报', '年报'),
    ('成绩趋势', '成绩趋势'),
    ('吃分推荐', '吃分推荐'),
    ('查看存档', '查看存档'),
    ('立即存储', '立即存储数据'),
)


def _score_shortcuts(matcher) -> tuple[tuple[str, str], ...]:
    if any(
        matcher is candidate
        for candidate in (gold_content, water_content, how_weak)
    ):
        return _CONTENT_SHORTCUTS
    if any(
        matcher is candidate
        for candidate in (
            best50, best_all50, refresh_b50, fcb50, fcallb50, apb50,
            apallb50, fit_b50, fit_all_b50, sun_b50, sun_all_b50,
            lock_b50, lock_ab50, yueji_b50, yueji_ab50, ideal_b50,
            ideal_ab50, floor_query, version_b50, legacy_b50, legacy_b35,
            dx2025_b50, dx2026_b35, difficulty_b50, difficulty_ab50,
        )
    ):
        return _B50_SHORTCUTS
    if any(
        matcher is candidate
        for candidate in (
            weekly_report, monthly_report, annual_report, daily_report,
            today_gain_recommend, awmcnet_trend, storage_snapshot,
        )
    ):
        return _REPORT_SHORTCUTS
    return ()

def get_at_qq(message: MessageEvent) -> Optional[int]:
    target = parse_at_target_id(message)
    if target is None:
        return None
    return resolve_score_qqid(message, target)


def _source_label(qqid: Optional[int]) -> str:
    """返回当前用户数据源中文名。username/@他人 查询传 None，视为水鱼。"""
    if qqid is None:
        return '水鱼'
    from ..libraries.maimaidx_datasource import get_user_source
    return {'awmcnet': 'AWMCNET', 'lxns': '落雪'}.get(get_user_source(qqid), '水鱼')


def _build_footer(
    qqid: Optional[int],
    total: float,
    *,
    forced_source: Optional[str] = None,
    unsupported_feature: Optional[str] = None,
) -> str:
    """构建成绩图下方文案：数据源 + 数据更新时间 + 主题 + 耗时（+ 落雪不支持提示）。"""
    from ..libraries.maimaidx_timing import get_fetch, format_summary
    from ..libraries.maimaidx_player_cache import footer_join_sections, pop_data_freshness_footer_lines
    source = forced_source or _source_label(qqid)
    sections: list[list[str]] = [
        [f'📊 数据源：{source} | 可使用 数据源 AWMCNET/水鱼/落雪 修改'],
    ]
    freshness = pop_data_freshness_footer_lines()
    if freshness:
        sections.append(freshness)
    if qqid is not None:
        try:
            from ..libraries.maimaidx_theme import get_user_theme, get_theme_display_name, Theme
            _t = get_user_theme(qqid)
            _name = get_theme_display_name(_t)
            _all = ' / '.join(get_theme_display_name(x.value) for x in Theme)
            sections.append([f'🎨 主题：{_name} | 可使用 主题 {_all} 切换'])
        except Exception:
            pass
    if unsupported_feature and qqid is not None and source == '落雪':
        sections.append([
            f'⚠️ {unsupported_feature}依赖水鱼独有数据，落雪暂不支持，已用水鱼生成',
        ])
    from ..libraries.maimaidx_b50_warnings import pop_b50_warning_footer
    warning = pop_b50_warning_footer()
    if warning:
        sections.append([warning])
    from ..libraries.maimaidx_break import take_break_charge_footer
    charge = take_break_charge_footer()
    if charge:
        sections.append(charge)
    sections.append([format_summary(total, get_fetch())])
    return footer_join_sections(sections)


async def _finish_score(
    matcher,
    coro,
    qqid: Optional[int],
    *,
    billing_qqid: Optional[int] = None,
    billing_event: Optional[MessageEvent] = None,
    username: Optional[str] = None,
    forced_source: Optional[str] = None,
    unsupported_feature: Optional[str] = None,
    service_name: Optional[str] = None,
    service_cost: int = 0,
):
    """统一成绩图收尾：计时执行 coro，成功追加「数据源 + 耗时」文案，错误原样发送。"""
    from ..libraries.maimaidx_timing import is_valid_image_result, run_timed
    from ..libraries.maimaidx_player_cache import clear_fetch_meta
    from ..libraries.maimaidx_error import BreakInsufficientError
    from ..libraries.maimaidx_break import normalize_billing_qqid

    if billing_event is None:
        from nonebot.matcher import current_event

        billing_event = current_event.get(None)
    payer = billing_qqid
    if payer is None and billing_event is not None:
        payer = billing_user_id(billing_event)
    payer = normalize_billing_qqid(payer)
    try:
        if service_name and service_cost > 0 and payer:
            from ..libraries.maimaidx_break import break_db, image_render_cost
            from ..libraries.maimaidx_card import card_manager

            break_db.ensure_service_affordable(payer, service_name, service_cost)
            if not card_manager.freedom_active(payer):
                service_due = (
                    0 if break_db.service_is_free(payer, service_name)
                    else max(0, int(service_cost))
                )
                required = service_due + max(0, image_render_cost())
                balance = break_db.get_balance(payer)
                if balance < required:
                    raise BreakInsufficientError(required, balance, qqid=payer)
        result, total = await run_timed(
            coro,
            billing_qqid=payer,
            render_charge=True,
        )
    except BreakInsufficientError as e:
        clear_fetch_meta()
        await plugin_finish(matcher, str(e), event=billing_event)
        return
    except QBindRequiredError as e:
        clear_fetch_meta()
        await plugin_finish(matcher, str(e), event=billing_event)
        return
    if isinstance(result, str):
        clear_fetch_meta()
        await plugin_finish(matcher, result, event=billing_event)
        return
    if not is_valid_image_result(result):
        clear_fetch_meta()
        await plugin_finish(
            matcher,
            '成绩图生成失败，请稍后重试或联系管理员。',
            event=billing_event,
        )
        return
    if service_name and service_cost > 0 and payer:
        try:
            from ..libraries.maimaidx_break import (
                break_db,
                image_render_cost,
                replace_break_charge_footer,
            )

            charge_result = break_db.settle_service_success(
                payer, service_name, service_cost
            )
            if charge_result.freedom:
                from ..libraries.maimaidx_break import format_freedom_exemption

                report_labels = {
                    'weekly_report': '周报（含生成图片）',
                    'monthly_report': '月报（含生成图片）',
                    'annual_report': '年报（含生成图片）',
                    'daily_report': '日报（含生成图片）',
                }
                total_saved = break_db.get_freedom_savings_total(payer)
                replace_break_charge_footer([
                    format_freedom_exemption(
                        payer,
                        report_labels.get(service_name, service_name),
                        max(0, int(service_cost)) + max(0, image_render_cost()),
                        charge_result.freedom_remaining,
                        total_saved=total_saved,
                    )
                ])
            elif charge_result.charged > 0:
                total_charge = charge_result.charged + max(
                    0, image_render_cost()
                )
                replace_break_charge_footer([
                    f'💳 消耗 {total_charge} BREAK · '
                    f'余额 {charge_result.balance} BREAK'
                ])
        except BreakInsufficientError as e:
            clear_fetch_meta()
            await plugin_finish(matcher, str(e), event=billing_event)
            return
    footer = _build_footer(
        qqid, total,
        forced_source=forced_source,
        unsupported_feature=unsupported_feature,
    )
    await plugin_finish(
        matcher,
        result,
        footer=footer,
        event=billing_event,
        publish_qq_image=True,
        qq_buttons=_score_shortcuts(matcher),
    )


@best50.handle()
async def _(
    event: MessageEvent,
    message: Message = CommandArg(),
    user_id: Optional[int] = Depends(get_at_qq)
):
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, 'score'):
        raise IgnoredException('功能已禁用')
    try:
        qqid = resolve_score_qqid(event, user_id)
    except QBindRequiredError as e:
        await plugin_finish(best50, str(e), event=event)
        return
    username = message.extract_plain_text().strip()
    await _finish_score(
        best50, generate(qqid, username), None if username else qqid, username=username or None,
        billing_event=event,
    )


@refresh_b50.handle()
async def _refresh_b50(
    bot: Bot,
    event: MessageEvent,
    message: Message = CommandArg(),
    user_id: Optional[int] = Depends(get_at_qq),
):
    """强制从查分器拉取最新成绩后生成常规 b50（绕过一切本地缓存）。"""
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, 'score'):
        raise IgnoredException('功能已禁用')
    try:
        qqid = resolve_score_qqid(event, user_id)
    except QBindRequiredError as e:
        await plugin_finish(refresh_b50, str(e), event=event)
        return
    username = message.extract_plain_text().strip()
    from ..libraries.maimaidx_datasource import get_user_records
    from ..libraries.maimaidx_break import break_billing
    from ..libraries.maimaidx_error import LxnsDataError

    await react_processing(bot, event)
    if not bool(getattr(maiconfig, 'maimaidx_compact_messages', True)):
        await refresh_b50.send('正在从查分器同步最新成绩并生成 b50，请稍候…', reply_message=True)
    try:
        async with break_billing(event.user_id):
            await get_user_records(
                qqid=None if username else qqid,
                username=username or None,
                force_refresh=True,
            )
    except BreakInsufficientError as e:
        await plugin_finish(refresh_b50, str(e), event=event, reply_message=True)
        return
    except LxnsDataError as e:
        await refresh_b50.finish(str(e), reply_message=True)
        return
    except (UserNotFoundError, UserNotExistsError, UserDisabledQueryError) as e:
        await refresh_b50.finish(str(e), reply_message=True)
        return
    except Exception as e:
        log.exception(f'[refresh_b50] qq={qqid} user={username!r}')
        await refresh_b50.finish(f'刷新失败：{type(e).__name__}', reply_message=True)
        return
    await _finish_score(
        refresh_b50,
        # 上面已经强制拉取并写入 AWMC NET；生成阶段直接读取刚同步的结果，
        # 避免同一次“刷新 b50”重复请求水鱼和落雪。
        generate(qqid, username, force_refresh=False),
        None if username else qqid,
        username=username or None,
        billing_qqid=event.user_id,
    )


@best_all50.handle()
async def _best_all50(
    event: MessageEvent,
    message: Message = CommandArg(),
    user_id: Optional[int] = Depends(get_at_qq)
):
    """常规 ab50：无视 B35/B15 分组，直接取 rating 最高的 50 首。"""
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, 'score'):
        raise IgnoredException('功能已禁用')
    qqid = resolve_score_qqid(event, user_id)
    username = message.extract_plain_text().strip()
    await _finish_score(
        best_all50, generate_all(qqid, username), None if username else qqid, username=username or None,
        billing_qqid=event.user_id,
    )


async def _handle_bird_rate(
    matcher,
    event: MessageEvent,
    message: Message,
    user_id: Optional[int],
    generator,
):
    """b50鸟率 / b50鸟加率 共用处理：拉 B50 统计文本，附带数据源与耗时 footer。"""
    from ..libraries.maimaidx_timing import run_timed
    from ..libraries.maimaidx_player_cache import clear_fetch_meta
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, 'score'):
        raise IgnoredException('功能已禁用')
    try:
        qqid = resolve_score_qqid(event, user_id)
    except QBindRequiredError as e:
        await plugin_finish(matcher, str(e), event=event)
        return
    username = message.extract_plain_text().strip() or None
    target_qq = None if username else qqid
    try:
        result, total = await run_timed(
            generator(qqid=target_qq, username=username),
            billing_qqid=event.user_id,
        )
    except (QBindRequiredError, BreakInsufficientError) as e:
        clear_fetch_meta()
        await plugin_finish(matcher, str(e), event=event)
        return
    clear_fetch_meta()
    footer = _build_footer(target_qq, total)
    text = str(result).rstrip()
    await plugin_finish(matcher, text, footer=footer, event=event)


@b50_bird_rate.handle()
async def _b50_bird_rate(
    event: MessageEvent,
    message: Message = CommandArg(),
    user_id: Optional[int] = Depends(get_at_qq),
):
    await _handle_bird_rate(b50_bird_rate, event, message, user_id, generate_b50_bird_rate)


@b50_bird_plus_rate.handle()
async def _b50_bird_plus_rate(
    event: MessageEvent,
    message: Message = CommandArg(),
    user_id: Optional[int] = Depends(get_at_qq),
):
    await _handle_bird_rate(b50_bird_plus_rate, event, message, user_id, generate_b50_bird_plus_rate)


def _display_name_from_sender(sender) -> str:
    """优先群名片，其次昵称。"""
    card = (getattr(sender, 'card', None) or '').strip()
    if card:
        return card
    return (getattr(sender, 'nickname', None) or '').strip() or '未知'


async def _coop_resolve_nicks_and_finish(event, at_qq, cmd, generator):
    """合作 b50/ab50 共用：解析两人昵称并调用对应生成函数。"""
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, 'score'):
        raise IgnoredException('功能已禁用')
    if not at_qq:
        await cmd.finish('请使用「合作b50@某人」或「合作ab50@某人」并@一位好友。', reply_message=True)
    qqid = resolve_score_qqid(event)
    if at_qq == qqid:
        await cmd.finish('请@除自己以外的另一位好友。', reply_message=True)
    from ..libraries.maimaidx_break import break_db, charge_session_extra
    cost = int(break_db.get_config('coop_b50_cost', '2'))
    if cost > 0:
        break_db.ensure_service_affordable(billing_user_id(event), 'coop_b50', cost)
    nick_a = _display_name_from_sender(event.sender) or str(qqid)
    nick_b = nick_a
    if isinstance(event, GroupMessageEvent):
        try:
            try:
                bot = get_bot()
            except Exception:
                bot = get_bot(str(event.self_id))
            member = await bot.call_api('get_group_member_info', group_id=event.group_id, user_id=at_qq)
            card = (member.get('card') or '').strip()
            nick_b = card or (member.get('nickname') or str(at_qq)).strip() or '未知'
        except Exception:
            nick_b = str(at_qq)
    else:
        nick_b = str(at_qq)
    from ..libraries.maimaidx_timing import finish_timed
    if cost > 0:
        charge_session_extra(billing_user_id(event), cost, 'coop_b50')
    await finish_timed(
        cmd,
        generator(qqid, at_qq, nick_a, nick_b),
        billing_qqid=billing_user_id(event),
        event=event,
    )


@coop_b50.handle()
async def _coop_b50(
    event: MessageEvent,
    message: Message = CommandArg(),
    at_qq: Optional[int] = Depends(get_at_qq),
):
    await _coop_resolve_nicks_and_finish(event, at_qq, coop_b50, generate_coop_b50)


@coop_ab50.handle()
async def _coop_ab50(
    event: MessageEvent,
    message: Message = CommandArg(),
    at_qq: Optional[int] = Depends(get_at_qq),
):
    await _coop_resolve_nicks_and_finish(event, at_qq, coop_ab50, generate_coop_all_b50)


@how_weak.handle()
async def _how_weak(
    event: MessageEvent,
    message: Message = CommandArg(),
    user_id: Optional[int] = Depends(get_at_qq),
):
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, 'score'):
        raise IgnoredException('功能已禁用')
    qqid = resolve_score_qqid(event, user_id)
    username = message.extract_plain_text().strip() or None
    await _finish_score(how_weak, generate_how_weak(qqid=qqid, username=username), None if username else qqid, username=username or None,
        billing_qqid=event.user_id,
    )


@group_weak.handle()
async def _group_weak(event: MessageEvent):
    if not isinstance(event, GroupMessageEvent):
        await group_weak.finish('该功能仅在群聊中可用。', reply_message=True)
    if not feature_manager.is_enabled(event.group_id, 'score'):
        raise IgnoredException('功能已禁用')
    try:
        bot = get_bot()
    except Exception:
        bot = get_bot(str(event.self_id))
    self_id = int(getattr(bot, 'self_id', event.self_id))
    nickname = str(getattr(bot, 'nickname', None) or 'Bot')
    board_text = await render_group_weak_board(
        bot, event.group_id, self_qq=resolve_score_qqid(event),
        user_name=_display_name_from_sender(event.sender),
    )
    await plugin_finish(
        group_weak,
        board_text,
        event=event,
        reply_message=True,
    )


@group_rating_leaderboard.handle()
async def _group_rating_leaderboard(event: MessageEvent, message: Message = CommandArg()):
    """群聊 rating 排行榜：获取群内成员 rating 倒序前 N 名，合并转发展示，不引用用户消息。默认前 10 名，可传参如 群聊rating排行榜 20。"""
    if not isinstance(event, GroupMessageEvent):
        await group_rating_leaderboard.finish('该功能仅在群聊中可用。', reply_message=False)
    if not feature_manager.is_enabled(event.group_id, 'score'):
        raise IgnoredException('功能已禁用')
    args = message.extract_plain_text().strip().split()
    top_n = 10
    if args:
        try:
            n = int(args[0])
            if n < 1:
                n = 1
            elif n > 50:
                n = 50
            top_n = n
        except (ValueError, TypeError):
            top_n = 10
    try:
        bot = get_bot()
    except Exception:
        bot = get_bot(str(event.self_id))
    self_id = int(getattr(bot, 'self_id', event.self_id))
    raw_nick = getattr(bot, 'nickname', None)
    nickname = raw_nick if isinstance(raw_nick, str) else 'Bot'
    if not nickname:
        nickname = 'Bot'
    board_text = await render_group_rating_board(
        bot, event.group_id, top_n=top_n, self_qq=resolve_score_qqid(event),
        user_name=_display_name_from_sender(event.sender),
    )
    await plugin_finish(
        group_rating_leaderboard,
        board_text,
        event=event,
        reply_message=False,
    )


async def _send_group_forward_or_finish(
    matcher,
    bot: Bot,
    group_id: int,
    self_id: int,
    nickname: str,
    text: str,
    nodes: list,
    *,
    empty_fallback: str = '暂无数据。',
    event: MessageEvent | None = None,
):
    """将群榜内容统一渲染成图片，避免发送合并转发聊天记录。"""
    board_text = format_forward_nodes_as_text(text, nodes) if nodes else (text or empty_fallback)
    await plugin_finish(
        matcher,
        rank_text_image(board_text),
        event=event,
        reply_message=False,
    )


@group_gain_board.handle()
async def _group_gain_board(event: MessageEvent, message: Message = CommandArg()):
    """群吃分榜 [近N天] [前M名]，默认 7 天、前 15 名。"""
    if not isinstance(event, GroupMessageEvent):
        await group_gain_board.finish('该功能仅在群聊中可用。', reply_message=False)
    if not feature_manager.is_enabled(event.group_id, 'score'):
        raise IgnoredException('功能已禁用')
    parts = message.extract_plain_text().strip().split()
    days, top_n = 7, 15
    if len(parts) >= 1:
        try:
            days = int(parts[0])
        except (ValueError, TypeError):
            pass
    if len(parts) >= 2:
        try:
            top_n = int(parts[1])
        except (ValueError, TypeError):
            pass
    try:
        bot = get_bot()
    except Exception:
        bot = get_bot(str(event.self_id))
    self_id = int(getattr(bot, 'self_id', event.self_id))
    result = await render_group_gain_board(
        bot, event.group_id, days=days, top_n=top_n, self_qq=resolve_score_qqid(event),
        user_name=_display_name_from_sender(event.sender),
    )
    await plugin_finish(group_gain_board, result, event=event, reply_message=False)


@group_sun_board.handle()
async def _group_sun_board(event: MessageEvent, message: Message = CommandArg()):
    """群寸止榜 [前N名]，默认 15。"""
    if not isinstance(event, GroupMessageEvent):
        await group_sun_board.finish('该功能仅在群聊中可用。', reply_message=False)
    if not feature_manager.is_enabled(event.group_id, 'score'):
        raise IgnoredException('功能已禁用')
    parts = message.extract_plain_text().strip().split()
    top_n = 15
    if parts:
        try:
            top_n = int(parts[0])
        except (ValueError, TypeError):
            pass
    try:
        bot = get_bot()
    except Exception:
        bot = get_bot(str(event.self_id))
    self_id = int(getattr(bot, 'self_id', event.self_id))
    result = await render_group_sun_lock_board(
        bot, event.group_id, mode='sun', top_n=top_n, self_qq=resolve_score_qqid(event),
        user_name=_display_name_from_sender(event.sender),
    )
    await plugin_finish(group_sun_board, result, event=event, reply_message=False)


@group_lock_board.handle()
async def _group_lock_board(event: MessageEvent, message: Message = CommandArg()):
    """群锁血榜 [前N名]，默认 15。"""
    if not isinstance(event, GroupMessageEvent):
        await group_lock_board.finish('该功能仅在群聊中可用。', reply_message=False)
    if not feature_manager.is_enabled(event.group_id, 'score'):
        raise IgnoredException('功能已禁用')
    parts = message.extract_plain_text().strip().split()
    top_n = 15
    if parts:
        try:
            top_n = int(parts[0])
        except (ValueError, TypeError):
            pass
    try:
        bot = get_bot()
    except Exception:
        bot = get_bot(str(event.self_id))
    self_id = int(getattr(bot, 'self_id', event.self_id))
    result = await render_group_sun_lock_board(
        bot, event.group_id, mode='lock', top_n=top_n, self_qq=resolve_score_qqid(event),
        user_name=_display_name_from_sender(event.sender),
    )
    await plugin_finish(group_lock_board, result, event=event, reply_message=False)


@friend_battle.handle()
async def _friend_battle(event: MessageEvent, message: Message = CommandArg()):
    """
    友人对战 [可选参数]：从本人 B50 随机一首，与同群友比该谱成绩。
    - 1～20：连战场数（如 友人对战 10），使用连战专用结果图，最高 20 局。
    - 50～800：rating 差收紧（如 友人对战 300）；可与连战组合，如 友人对战 10 300。
    """
    if not isinstance(event, GroupMessageEvent):
        await friend_battle.finish('该功能仅在群聊中可用。', reply_message=True)
    if not feature_manager.is_enabled(event.group_id, 'score'):
        raise IgnoredException('功能已禁用')
    qqid = resolve_score_qqid(event)
    cd_msg = check_friend_battle_cooldown(qqid)
    if cd_msg:
        await friend_battle.finish(cd_msg, reply_message=True)
    arg = message.extract_plain_text().strip()
    rounds, user_rating_cap = parse_friend_battle_args(arg)

    async def _gen():
        mark_friend_battle_used(qqid)
        try:
            bot = get_bot()
        except Exception:
            bot = get_bot(str(event.self_id))
        if rounds > 1:
            batch = await run_friend_battle_batch(
                bot, event.group_id, qqid, rounds, user_rating_cap=user_rating_cap
            )
            if isinstance(batch, str):
                return batch
            return await draw_friend_battle_batch_image(batch)
        result = await run_friend_battle(
            bot, event.group_id, qqid, user_rating_cap=user_rating_cap
        )
        if isinstance(result, str):
            return result
        return await draw_friend_battle_image(result)

    from ..libraries.maimaidx_timing import finish_timed
    await finish_timed(
        friend_battle, _gen(), billing_qqid=billing_user_id(event), event=event
    )


@friend_battle_rank.handle()
async def _friend_battle_rank(event: MessageEvent, message: Message = CommandArg()):
    """本群友人对战段位排行 [前N名]，默认前 15 名；标题含 LEGEND 人数与你的排名。"""
    if not isinstance(event, GroupMessageEvent):
        await friend_battle_rank.finish('该功能仅在群聊中可用。', reply_message=False)
    if not feature_manager.is_enabled(event.group_id, 'score'):
        raise IgnoredException('功能已禁用')
    parts = message.extract_plain_text().strip().split()
    top_n = 15
    if parts:
        try:
            top_n = max(1, min(50, int(parts[0])))
        except (ValueError, TypeError):
            pass
    try:
        bot = get_bot()
    except Exception:
        bot = get_bot(str(event.self_id))
    self_id = int(getattr(bot, 'self_id', event.self_id))
    nickname = str(getattr(bot, 'nickname', None) or 'Bot')
    text, nodes = await group_friend_battle_ranking(
        bot, event.group_id, self_id, nickname, resolve_score_qqid(event), top_n=top_n
    )
    await _send_group_forward_or_finish(
        friend_battle_rank,
        bot,
        event.group_id,
        self_id,
        nickname,
        text,
        nodes,
        empty_fallback='暂无友人对战段位数据。',
        event=event,
    )


async def _get_bot_info(event: MessageEvent) -> tuple[Bot, int, str]:
    """获取 bot 实例和基本信息。"""
    try:
        bot = get_bot()
    except Exception:
        bot = get_bot(str(event.self_id))
    self_id = int(getattr(bot, "self_id", event.self_id))
    nickname = str(getattr(bot, "nickname", None) or "Bot")
    return bot, self_id, nickname




@fcb50.handle()
async def _fcb50(
    event: MessageEvent,
    message: Message = CommandArg(),
    user_id: Optional[int] = Depends(get_at_qq),
):
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, 'score'):
        raise IgnoredException('功能已禁用')
    qqid = resolve_score_qqid(event, user_id)
    username = message.extract_plain_text().strip()
    await _finish_score(fcb50, generate_fc_b50(qqid, username), None if username else qqid, username=username or None,
        billing_qqid=event.user_id,
    )


@fcallb50.handle()
async def _fcallb50(
    event: MessageEvent,
    message: Message = CommandArg(),
    user_id: Optional[int] = Depends(get_at_qq),
):
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, 'score'):
        raise IgnoredException('功能已禁用')
    qqid = resolve_score_qqid(event, user_id)
    username = message.extract_plain_text().strip()
    await _finish_score(fcallb50, generate_fc_all_b50(qqid, username), None if username else qqid, username=username or None,
        billing_qqid=event.user_id,
    )


@apb50.handle()
async def _apb50(
    event: MessageEvent,
    message: Message = CommandArg(),
    user_id: Optional[int] = Depends(get_at_qq),
):
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, 'score'):
        raise IgnoredException('功能已禁用')
    qqid = resolve_score_qqid(event, user_id)
    username = message.extract_plain_text().strip()
    await _finish_score(apb50, generate_ap_b50(qqid, username), None if username else qqid, username=username or None,
        billing_qqid=event.user_id,
    )


@apallb50.handle()
async def _apallb50(
    event: MessageEvent,
    message: Message = CommandArg(),
    user_id: Optional[int] = Depends(get_at_qq),
):
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, 'score'):
        raise IgnoredException('功能已禁用')
    qqid = resolve_score_qqid(event, user_id)
    username = message.extract_plain_text().strip()
    await _finish_score(apallb50, generate_ap_all_b50(qqid, username), None if username else qqid, username=username or None,
        billing_qqid=event.user_id,
    )


@fit_b50.handle()
async def _fit_b50(
    event: MessageEvent,
    message: Message = CommandArg(),
    user_id: Optional[int] = Depends(get_at_qq),
):
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, 'score'):
        raise IgnoredException('功能已禁用')
    qqid = resolve_score_qqid(event, user_id)
    username = message.extract_plain_text().strip()
    await _finish_score(fit_b50, generate_fit_b50(qqid, username), None if username else qqid, username=username or None,
        billing_qqid=event.user_id,
    )


@fit_all_b50.handle()
async def _fit_all_b50(
    event: MessageEvent,
    message: Message = CommandArg(),
    user_id: Optional[int] = Depends(get_at_qq),
):
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, 'score'):
        raise IgnoredException('功能已禁用')
    qqid = resolve_score_qqid(event, user_id)
    username = message.extract_plain_text().strip()
    await _finish_score(fit_all_b50, generate_fit_all_b50(qqid, username), None if username else qqid, username=username or None,
        billing_qqid=event.user_id,
    )


def _parse_threshold_and_username(args: str) -> tuple:
    """解析可选档位门槛（数字）+ 可选用户名。返回 (threshold 或 None, username)。用于寸b50/锁血b50。"""
    args = args.strip()
    if not args:
        return None, ''
    parts = args.split(maxsplit=1)
    try:
        t = float(parts[0])
        if 50 <= t <= 100.5:
            return t, (parts[1].strip() if len(parts) > 1 else '')
    except (ValueError, TypeError):
        pass
    return None, args


@sun_b50.handle()
async def _sun_b50(
    event: MessageEvent,
    message: Message = CommandArg(),
    user_id: Optional[int] = Depends(get_at_qq),
):
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, 'score'):
        raise IgnoredException('功能已禁用')
    qqid = resolve_score_qqid(event, user_id)
    threshold, username = _parse_threshold_and_username(message.extract_plain_text())
    await _finish_score(sun_b50, generate_sun_b50(qqid, username, threshold), None if username else qqid, username=username or None,
        billing_qqid=event.user_id,
    )


@sun_all_b50.handle()
async def _sun_all_b50(
    event: MessageEvent,
    message: Message = CommandArg(),
    user_id: Optional[int] = Depends(get_at_qq),
):
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, 'score'):
        raise IgnoredException('功能已禁用')
    qqid = resolve_score_qqid(event, user_id)
    threshold, username = _parse_threshold_and_username(message.extract_plain_text())
    await _finish_score(sun_all_b50, generate_sun_all_b50(qqid, username, threshold), None if username else qqid, username=username or None,
        billing_qqid=event.user_id,
    )




@lock_b50.handle()
async def _lock_b50(
    event: MessageEvent,
    message: Message = CommandArg(),
    user_id: Optional[int] = Depends(get_at_qq),
):
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, 'score'):
        raise IgnoredException('功能已禁用')
    qqid = resolve_score_qqid(event, user_id)
    _, username = _parse_threshold_and_username(message.extract_plain_text())
    await _finish_score(lock_b50, generate_lock_b50(qqid, username), None if username else qqid, username=username or None,
        billing_qqid=event.user_id,
    )


@lock_ab50.handle()
async def _lock_ab50(
    event: MessageEvent,
    message: Message = CommandArg(),
    user_id: Optional[int] = Depends(get_at_qq),
):
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, 'score'):
        raise IgnoredException('功能已禁用')
    qqid = resolve_score_qqid(event, user_id)
    _, username = _parse_threshold_and_username(message.extract_plain_text())
    await _finish_score(lock_ab50, generate_lock_all_b50(qqid, username), None if username else qqid, username=username or None,
        billing_qqid=event.user_id,
    )


@yueji_b50.handle()
async def _yueji_b50(
    event: MessageEvent,
    message: Message = CommandArg(),
    user_id: Optional[int] = Depends(get_at_qq),
):
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, 'score'):
        raise IgnoredException('功能已禁用')
    qqid = resolve_score_qqid(event, user_id)
    th, username = _parse_threshold_and_username(message.extract_plain_text())
    threshold = th if th is not None else 97.0
    await _finish_score(yueji_b50, generate_yueji_b50(qqid, username, threshold), None if username else qqid, username=username or None,
        billing_qqid=event.user_id,
    )


@yueji_ab50.handle()
async def _yueji_ab50(
    event: MessageEvent,
    message: Message = CommandArg(),
    user_id: Optional[int] = Depends(get_at_qq),
):
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, 'score'):
        raise IgnoredException('功能已禁用')
    qqid = resolve_score_qqid(event, user_id)
    th, username = _parse_threshold_and_username(message.extract_plain_text())
    threshold = th if th is not None else 97.0
    await _finish_score(yueji_ab50, generate_yueji_all_b50(qqid, username, threshold), None if username else qqid, username=username or None,
        billing_qqid=event.user_id,
    )


@difficulty_b50.handle()
async def _difficulty_b50(event: MessageEvent, matched = RegexMatched()):
    """<难度>b50：筛选指定难度的成绩，新版本取前15，旧版本取前35，按 B35/B15 分组显示。"""
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, 'score'):
        raise IgnoredException('功能已禁用')

    difficulty = matched.group(1).strip() if matched and matched.group(1) else ''
    log.debug(f"[difficulty_b50] raw='{event.get_plaintext().strip()}', difficulty='{difficulty}'")

    # 无法解析为难度（如「这怎么更新b50」）时静默忽略，避免刷屏
    from ..libraries.maimaidx_difficulty_filter import DifficultyFilter
    try:
        DifficultyFilter.parse(difficulty)
    except ValueError:
        log.debug(f"[difficulty_b50] 忽略无效难度: '{difficulty}'")
        return

    qqid = resolve_score_qqid(event)
    await _finish_score(
        difficulty_b50,
        generate_difficulty_b50(qqid=qqid, difficulty=difficulty),
        qqid,
        billing_qqid=event.user_id,
    )


@difficulty_ab50.handle()
async def _difficulty_ab50(event: MessageEvent, matched = RegexMatched()):
    """<难度>ab50：筛选指定难度的成绩，无视分组直接取前50首。"""
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, 'score'):
        raise IgnoredException('功能已禁用')

    difficulty = matched.group(1).strip() if matched and matched.group(1) else ''
    log.debug(f"[difficulty_ab50] raw='{event.get_plaintext().strip()}', difficulty='{difficulty}'")

    # 无法解析为难度时静默忽略，避免刷屏
    from ..libraries.maimaidx_difficulty_filter import DifficultyFilter
    try:
        DifficultyFilter.parse(difficulty)
    except ValueError:
        log.debug(f"[difficulty_ab50] 忽略无效难度: '{difficulty}'")
        return

    qqid = resolve_score_qqid(event)
    await _finish_score(
        difficulty_ab50,
        generate_difficulty_all_b50(qqid=qqid, difficulty=difficulty),
        qqid,
        billing_qqid=event.user_id,
    )


@ideal_b50.handle()
async def _ideal_b50(
    event: MessageEvent,
    message: Message = CommandArg(),
    user_id: Optional[int] = Depends(get_at_qq),
):
    """理想b50：将每个成绩的评级提高一个档次，新版本取前15，旧版本取前35，按 B35/B15 分组显示。"""
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, 'score'):
        raise IgnoredException('功能已禁用')
    qqid = resolve_score_qqid(event, user_id)
    username = message.extract_plain_text().strip()
    await _finish_score(ideal_b50, generate_ideal_b50(qqid, username), None if username else qqid, username=username or None,
        billing_qqid=event.user_id,
    )


@ideal_ab50.handle()
async def _ideal_ab50(
    event: MessageEvent,
    message: Message = CommandArg(),
    user_id: Optional[int] = Depends(get_at_qq),
):
    """理想ab50：将每个成绩的评级提高一个档次，无视分组直接取前50首。"""
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, 'score'):
        raise IgnoredException('功能已禁用')
    qqid = resolve_score_qqid(event, user_id)
    username = message.extract_plain_text().strip()
    await _finish_score(ideal_ab50, generate_ideal_all_b50(qqid, username), None if username else qqid, username=username or None,
        billing_qqid=event.user_id,
    )


@enable_data_storage.handle()
async def _enable_data_storage(event: MessageEvent):
    """开启数据存储：每天自动存储成绩；首次开启立即拉取一次全量存档"""
    qqid = resolve_score_qqid(event)
    was_enabled = data_storage.is_enabled(qqid)
    success = data_storage.enable_user(qqid)
    if success:
        await enable_data_storage.send(
            '已开启数据存储，正在首次同步全量成绩，请稍候…',
            reply_message=True,
        )
        store_ok = await fetch_and_store_user_scores(qqid, source="enable")
        sync_tip = (
            '\n首次同步已完成，「牌子统计」等将优先使用本地快照。'
            if store_ok
            else '\n首次同步未成功（查分器 Token、绑定或网络问题），可稍后发送「立即存储数据」重试。'
        )
        bonus_tip = ''
        if not was_enabled:
            # 今日已签到可补发差额；防刷：7 天内关过/领过则不补，需保持开启跨天再享受
            from ..libraries.maimaidx_break import break_db

            cooldown = break_db._storage_bonus_cooldown_days()
            ok, reason = data_storage.storage_bonus_eligible_for_retroactive(
                qqid, cooldown_days=cooldown
            )
            bonus_lines = []
            if ok and not break_db.has_recent_storage_checkin_bonus(
                int(billing_user_id(event))
            ) and not break_db.has_recent_storage_checkin_bonus(int(qqid)):
                seen: set[int] = set()
                for candidate in (
                    int(qqid),
                    int(billing_user_id(event)),
                ):
                    if not candidate or candidate in seen:
                        continue
                    seen.add(candidate)
                    try:
                        granted = break_db.try_grant_checkin_storage_bonus(candidate)
                    except Exception as exc:
                        log.warning(
                            f'[storage] 签到存储加成补发失败 qqid={candidate}: '
                            f'{type(exc).__name__}: {exc}'
                        )
                        continue
                    if granted and granted.awarded and granted.amount > 0:
                        bonus_lines.append(
                            f'🎁 签到数据存储加成 +{granted.amount} BREAK'
                            f'（余额 {granted.balance}）'
                        )
                        break
            if bonus_lines:
                bonus_tip = '\n' + '\n'.join(bonus_lines)
            elif reason == 'toggle_spam' or not ok:
                bonus_tip = (
                    '\n⏳ 开启补发仅限首次开启；关了再开不再补发。'
                    '\n请保持开启至明日签到，即可享受基础 +50% BREAK。'
                )
            elif reason == 'cooldown_after_disable':
                bonus_tip = (
                    f'\n⏳ 近期关闭过存储：{cooldown} 天内不发放开启补发。'
                    '\n保持开启至明日签到即可正常享受 +50%。'
                )
            else:
                bonus_tip = (
                    '\n✨ 已解锁签到加成：请保持开启，明日起签到基础 +50% BREAK'
                    '（当天开关不计入；关了再开不重复发，防刷）'
                )
        await enable_data_storage.finish(
            '已开启数据存储功能！\n'
            '每天凌晨 4:00 会自动存储你的全量成绩与 Rating。\n'
            '你也可以使用「立即存储数据」手动触发存储。'
            + sync_tip
            + bonus_tip,
            reply_message=True,
        )
    else:
        await enable_data_storage.finish('开启数据存储失败，请稍后重试。', reply_message=True)


@disable_data_storage.handle()
async def _disable_data_storage(event: MessageEvent):
    """关闭数据存储：停止自动存储成绩"""
    qqid = resolve_score_qqid(event)
    success = data_storage.disable_user(qqid)
    if success:
        await disable_data_storage.finish('已关闭数据存储功能。', reply_message=True)
    else:
        await disable_data_storage.finish('关闭数据存储失败，请稍后重试。', reply_message=True)


@store_data_now.handle()
async def _store_data_now(event: MessageEvent):
    """立即存储数据：手动触发成绩存储"""
    qqid = resolve_score_qqid(event)

    # 检查是否已开启存储
    if not data_storage.is_enabled(qqid):
        await store_data_now.finish(
            '你尚未开启数据存储功能。\n'
            '请先发送「开启存储数据」开启自动存储。',
            reply_message=True
        )
        return

    if not bool(getattr(maiconfig, 'maimaidx_compact_messages', True)):
        await store_data_now.send('正在获取并存储你的成绩数据，请稍候...', reply_message=True)
    
    success = await fetch_and_store_user_scores(qqid)
    if success:
        # 获取今天的存储信息
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        snapshot = data_storage.load_daily_snapshot(qqid, today)
        if snapshot:
            await store_data_now.finish(
                f'成绩存储成功！\n'
                f'日期：{snapshot.date}\n'
                f'Rating：{snapshot.rating}\n'
                f'记录数：{snapshot.record_count} 首',
                reply_message=True
            )
        else:
            await store_data_now.finish('成绩存储成功！', reply_message=True)
    else:
        await store_data_now.finish(
            '成绩存储失败。\n'
            '可能原因：未绑定查分器、查分器Token过期、网络问题。\n'
            '请检查你的查分器绑定状态。',
            reply_message=True
        )


@storage_history.handle()
async def _storage_history(event: MessageEvent, message: Message = CommandArg()):
    """查询存储历史：展示最近 N 次快照摘要（默认 10 次）"""
    qqid = resolve_score_qqid(event)
    if not data_storage.is_enabled(qqid):
        await storage_history.finish(
            '你尚未开启数据存储功能，请先发送「开启存储数据」。',
            reply_message=True,
        )
        return

    args = message.extract_plain_text().strip()
    limit = 10
    if args:
        try:
            limit = max(1, min(50, int(args)))
        except ValueError:
            limit = 10

    rows = data_storage.get_rating_history(qqid, days=limit)
    if not rows:
        await storage_history.finish('暂无存储记录，可先发送「立即存储数据」。', reply_message=True)
        return

    lines = [f'最近 {len(rows)} 次存档记录：']
    for i, r in enumerate(rows, 1):
        lines.append(
            f'{i}. {r.get("stored_at", r.get("date", ""))} | '
            f'Rating {r.get("rating", 0)} | '
            f'{r.get("record_count", 0)} 首 | '
            f'{r.get("source", "")} | '
            f'ID={r.get("snapshot_id", "")}'
        )
    lines.append('使用「查看存档 <ID>」查看某次快照详情。')
    await storage_history.finish('\n'.join(lines), reply_message=True)


@storage_snapshot.handle()
async def _storage_snapshot(event: MessageEvent, message: Message = CommandArg()):
    """查看某次存档详情：查看存档 <snapshot_id>"""
    qqid = resolve_score_qqid(event)
    if not data_storage.is_enabled(qqid):
        await storage_snapshot.finish(
            '你尚未开启数据存储功能，请先发送「开启存储数据」。',
            reply_message=True,
        )
        return

    snapshot_id = message.extract_plain_text().strip()
    if not snapshot_id:
        latest = data_storage.list_snapshots(qqid, limit=1)
        if not latest:
            await storage_snapshot.finish('暂无存档记录，可先发送「立即存储数据」。', reply_message=True)
            return
        snapshot_id = latest[0].get("snapshot_id", "")

    snap = data_storage.load_snapshot_by_id(qqid, snapshot_id)
    if not snap:
        await storage_snapshot.finish(f'未找到存档：{snapshot_id}', reply_message=True)
        return

    top_records = sorted(snap.records, key=lambda x: x.ra, reverse=True)[:5]
    lines = [
        f'存档ID: {snap.snapshot_id}',
        f'时间: {snap.stored_at or snap.date}',
        f'来源: {snap.source}',
        f'昵称: {snap.nickname}',
        f'Rating: {snap.rating}',
        f'记录数: {snap.record_count}',
        'Top5 单曲：',
    ]
    for i, r in enumerate(top_records, 1):
        lines.append(f'{i}. {r.title} [{r.level}] {r.achievements:.4f}% {r.ds:.1f}->{r.ra} {r.rate}')
    await storage_snapshot.finish('\n'.join(lines), reply_message=True)


@awmcnet_trend.handle()
async def _awmcnet_trend(event: MessageEvent, message: Message = CommandArg()):
    """Read the server-side daily trend; no local storage opt-in is required."""
    qqid = resolve_score_qqid(event)
    raw = message.extract_plain_text().strip()
    try:
        days = max(1, min(365, int(raw))) if raw else 30
    except ValueError:
        await awmcnet_trend.finish(
            '用法：成绩趋势 [天数]，例如「成绩趋势 30」。',
            reply_message=True,
        )
        return
    from ..libraries.maimaidx_awmcnet_sync import (
        fetch_awmcnet_trend,
        format_awmcnet_trend,
    )

    payload = await fetch_awmcnet_trend(qqid, days)
    if payload is None:
        await awmcnet_trend.finish(
            '暂时无法读取 AWMC NET. 趋势。请确认已发送 SGWCMAID 完成同步，或稍后重试。',
            reply_message=True,
        )
        return
    await awmcnet_trend.finish(format_awmcnet_trend(payload), reply_message=True)


@weekly_report.handle()
async def _weekly_report(event: MessageEvent):
    qqid = resolve_score_qqid(event)
    if not data_storage.is_enabled(qqid):
        await weekly_report.finish(
            '你尚未开启数据存储功能，请先发送「开启存储数据」。',
            reply_message=True,
        )
        return
    from ..libraries.maimaidx_break import break_db
    cost = int(break_db.get_config('weekly_report_cost', '1'))
    await _finish_score(weekly_report, generate_progress_report(qqid, 7), qqid,
        billing_qqid=billing_user_id(event),
        billing_event=event,
        service_name='weekly_report' if cost > 0 else None,
        service_cost=cost,
    )


@monthly_report.handle()
async def _monthly_report(event: MessageEvent):
    qqid = resolve_score_qqid(event)
    if not data_storage.is_enabled(qqid):
        await monthly_report.finish(
            '你尚未开启数据存储功能，请先发送「开启存储数据」。',
            reply_message=True,
        )
        return
    from ..libraries.maimaidx_break import break_db
    cost = int(break_db.get_config('monthly_report_cost', '2'))
    await _finish_score(monthly_report, generate_progress_report(qqid, 30), qqid,
        billing_qqid=billing_user_id(event),
        billing_event=event,
        service_name='monthly_report' if cost > 0 else None,
        service_cost=cost,
    )


@annual_report.handle()
async def _annual_report(event: MessageEvent):
    qqid = resolve_score_qqid(event)
    if not data_storage.is_enabled(qqid):
        await annual_report.finish(
            '你尚未开启数据存储功能，请先发送「开启存储数据」。',
            reply_message=True,
        )
        return
    from ..libraries.maimaidx_break import break_db
    cost = int(break_db.get_config('annual_report_cost', '3'))
    await _finish_score(annual_report, generate_progress_report(qqid, 365), qqid,
        billing_qqid=billing_user_id(event),
        billing_event=event,
        service_name='annual_report' if cost > 0 else None,
        service_cost=cost,
    )


@daily_report.handle()
async def _daily_report(event: MessageEvent):
    qqid = resolve_score_qqid(event)
    if not data_storage.is_enabled(qqid):
        await daily_report.finish(
            '你尚未开启数据存储功能，请先发送「开启存储数据」。',
            reply_message=True,
        )
        return
    from ..libraries.maimaidx_break import break_db
    cost = int(break_db.get_config('daily_report_cost', '0'))
    await _finish_score(daily_report, generate_daily_report(qqid), qqid,
        billing_qqid=billing_user_id(event),
        billing_event=event,
        service_name='daily_report' if cost > 0 else None,
        service_cost=cost,
    )


@today_gain_recommend.handle()
async def _today_gain_recommend(event: MessageEvent):
    qqid = resolve_score_qqid(event)
    if not data_storage.is_enabled(qqid):
        await today_gain_recommend.finish(
            '你尚未开启数据存储功能，请先发送「开启存储数据」。',
            reply_message=True,
        )
        return
    result = await generate_today_gain_recommendation_image(qqid)
    if isinstance(result, str):
        await today_gain_recommend.finish(result, reply_message=True)
        return

    async def _image_coro():
        from ..libraries.maimaidx_break import settle_feature_if_uncharged

        settle_feature_if_uncharged(
            billing_user_id(event), 'today_gain_recommend'
        )
        return result

    await _finish_score(
        today_gain_recommend,
        _image_coro(),
        qqid,
        billing_qqid=billing_user_id(event),
        billing_event=event,
    )


@floor_query.handle()
async def _floor_query(
    event: MessageEvent,
    message: Message = CommandArg(),
    user_id: Optional[int] = Depends(get_at_qq),
):
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, 'score'):
        raise IgnoredException('功能已禁用')
    qqid = resolve_score_qqid(event, user_id)
    arg = message.extract_plain_text().strip()
    username = ''
    filter_text = arg
    if arg:
        from ..libraries.maimaidx_difficulty_filter import DifficultyFilter
        try:
            DifficultyFilter.parse(arg)
        except ValueError:
            filter_text = ''
            username = arg
    await _finish_score(
        floor_query,
        generate_floor_query(qqid, username or None, filter_text=filter_text),
        None if username else qqid,
        username=username or None,
        billing_qqid=event.user_id,
    )


@plate_count_stats.handle()
async def _plate_count_stats(event: MessageEvent, user_id: Optional[int] = Depends(get_at_qq)):
    """牌子统计：优先本地最近快照；否则本指令内拉取 dev 全量一次再统计（不强制开启存储）。"""
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, "score"):
        raise IgnoredException("功能已禁用")
    qqid = resolve_score_qqid(event, user_id)

    metas = data_storage.list_snapshots(qqid, limit=1)
    if metas:
        sid = metas[0].get("snapshot_id", "")
        snap = data_storage.load_snapshot_by_id(qqid, sid) if sid else None
        if snap and snap.records:
            await plate_count_stats.finish(format_plate_count_message(snap), reply_message=True)
            return

    if not bool(getattr(maiconfig, 'maimaidx_compact_messages', True)):
        await plate_count_stats.send("无本地快照或快照为空，正在拉取全量成绩并统计…", reply_message=True)
    from ..libraries.maimaidx_break import break_billing, take_break_charge_footer
    try:
        async with break_billing(event.user_id):
            records = await fetch_dev_records_as_score_records(qqid)
    except BreakInsufficientError as e:
        await plugin_finish(
            plate_count_stats, str(e), event=event, reply_message=True,
        )
        return
    except (UserNotFoundError, UserNotExistsError, UserDisabledQueryError) as e:
        await plate_count_stats.finish(str(e), reply_message=True)
        return
    except Exception as e:
        log.exception(f"[plate_count_stats] qq={qqid}")
        await plate_count_stats.finish(
            f"拉取全量成绩失败：{e}\n请检查查分器绑定与 Token。",
            reply_message=True,
        )
        return

    if not records:
        await plate_count_stats.finish(
            "未获取到任何成绩记录。\n"
            "开启数据存储并「立即存储数据」后，可优先使用本地快照以减少查分请求。",
            reply_message=True,
        )
        return

    note = f"数据来源：本次指令按偏好实时拉取全量成绩（{_source_label(qqid)}，未写入本地）"
    tip = ""
    if data_storage.is_enabled(qqid):
        tip = "\n提示：你已开启数据存储，发送「立即存储数据」后下次将优先用本地快照。"
    charge = take_break_charge_footer()
    charge_text = ('\n' + '\n'.join(charge)) if charge else ''
    await plate_count_stats.finish(
        format_plate_count_message_from_records(records, note) + tip + charge_text,
        reply_message=True,
    )


@compare_report.handle()
async def _compare_report(event: MessageEvent, message: Message = CommandArg()):
    qqid = resolve_score_qqid(event)
    if not data_storage.is_enabled(qqid):
        await compare_report.finish(
            '你尚未开启数据存储功能，请先发送「开启存储数据」。',
            reply_message=True,
        )
        return
    args = message.extract_plain_text().strip().split()
    if len(args) != 2:
        await compare_report.finish(
            '用法：对比存档 <旧ID> <新ID>\n可先用「存储历史」查看ID。',
            reply_message=True,
        )
        return
    await _finish_score(compare_report, generate_progress_report_between(qqid, args[0], args[1]), qqid,
        billing_qqid=event.user_id,
    )


@gold_content.handle()
async def _gold_content(
    event: MessageEvent,
    message: Message = CommandArg(),
    user_id: Optional[int] = Depends(get_at_qq),
):
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, 'score'):
        raise IgnoredException('功能已禁用')
    qqid = resolve_score_qqid(event, user_id)
    username = message.extract_plain_text().strip()
    await _finish_score(gold_content, generate_gold_content(qqid, username), None if username else qqid, username=username or None,
        billing_qqid=event.user_id,
    )


@water_content.handle()
async def _water_content(
    event: MessageEvent,
    message: Message = CommandArg(),
    user_id: Optional[int] = Depends(get_at_qq),
):
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, 'score'):
        raise IgnoredException('功能已禁用')
    qqid = resolve_score_qqid(event, user_id)
    username = message.extract_plain_text().strip()
    await _finish_score(water_content, generate_water_content(qqid, username), None if username else qqid, username=username or None,
        billing_qqid=event.user_id,
    )


@version_b50.handle()
async def _(
    event: MessageEvent,
    match = RegexMatched(),
    user_id: Optional[int] = Depends(get_at_qq),
):
    """版本 B50：根据版本代号筛选歌曲，按 RA 降序排序，无视类型分组"""
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, 'score'):
        raise IgnoredException('功能已禁用')
    qqid = resolve_score_qqid(event, user_id)
    version_name = match.group(1) if match else ''
    if not version_name:
        await version_b50.finish('版本名称不能为空', reply_message=True)
        return
    await _finish_score(version_b50, generate_version_b50(qqid, None, version_name), None if user_id else qqid,
        billing_qqid=event.user_id,
    )


@legacy_b50.handle()
async def _(
    event: MessageEvent,
    match = RegexMatched(),
    user_id: Optional[int] = Depends(get_at_qq),
):
    """历代版本 b50：使用指定版本的定数重算 rating。格式：l镜代b50 / l祭代b50"""
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, 'score'):
        raise IgnoredException('功能已禁用')
    qqid = resolve_score_qqid(event, user_id)
    version_alias = match.group(1) if match else ''
    if not version_alias:
        await legacy_b50.finish('请输入版本代号，例如：l镜代b50', reply_message=True)
        return
    from ..libraries.maimaidx_version_alias import resolve_version_alias, build_legacy_ds_map, VERSION_ALIAS
    from ..libraries.maimaidx_b50_pipeline import b50_pipeline
    version_name = resolve_version_alias(version_alias)
    if not version_name:
        known = "、".join(list(VERSION_ALIAS.keys())[:12]) + "..."
        await legacy_b50.finish(
            f"未知版本代号「{version_alias}」，支持的代号：{known}\n格式：l镜代b50 / l祭代b50",
            reply_message=True,
        )
        return
    try:
        ds_map = build_legacy_ds_map(version_name)
    except FileNotFoundError:
        await legacy_b50.finish("未找到 dxdata.json 文件，无法计算历代版本 B50", reply_message=True)
        return
    except Exception as e:
        log.error(f"[legacy_b50] 构建 ds_map 失败: {e}")
        await legacy_b50.finish(f"加载定数数据失败：{e}", reply_message=True)
        return
    if not ds_map:
        await legacy_b50.finish(f"「{version_name}」版本无定数变化数据", reply_message=True)
        return
    from ..libraries.maimaidx_timing import run_timed
    from ..libraries.maimaidx_error import BreakInsufficientError
    try:
        result, _total = await run_timed(
            b50_pipeline(
                qqid=qqid,
                recalculate=True,
                ds_map=ds_map,
                by_group=False,
                compact_layout=True,
                hide_logo=False,
            ),
            billing_qqid=event.user_id,
        )
    except BreakInsufficientError as e:
        await plugin_finish(legacy_b50, str(e), event=event, reply_message=True)
        return
    if isinstance(result, str):
        await legacy_b50.finish(result, reply_message=True)
    else:
        await legacy_b50.finish(result + MessageSegment.text(_build_footer(qqid, _total)), reply_message=True)


@legacy_b35.handle()
async def _(
    event: MessageEvent,
    match = RegexMatched(),
    user_id: Optional[int] = Depends(get_at_qq),
):
    """历代版本 b35：使用指定版本的定数重算 rating，仅取前 35 首。格式：l镜代b35"""
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, 'score'):
        raise IgnoredException('功能已禁用')
    qqid = resolve_score_qqid(event, user_id)
    version_alias = match.group(1) if match else ''
    if not version_alias:
        await legacy_b35.finish('请输入版本代号，例如：l镜代b35', reply_message=True)
        return
    from ..libraries.maimaidx_version_alias import resolve_version_alias, build_legacy_ds_map, VERSION_ALIAS
    from ..libraries.maimaidx_b50_pipeline import b50_pipeline
    version_name = resolve_version_alias(version_alias)
    if not version_name:
        known = "、".join(list(VERSION_ALIAS.keys())[:12]) + "..."
        await legacy_b35.finish(
            f"未知版本代号「{version_alias}」，支持的代号：{known}\n格式：l镜代b35",
            reply_message=True,
        )
        return
    try:
        ds_map = build_legacy_ds_map(version_name)
    except FileNotFoundError:
        await legacy_b35.finish("未找到 dxdata.json 文件，无法计算历代版本 B35", reply_message=True)
        return
    except Exception as e:
        log.error(f"[legacy_b35] 构建 ds_map 失败: {e}")
        await legacy_b35.finish(f"加载定数数据失败：{e}", reply_message=True)
        return
    if not ds_map:
        await legacy_b35.finish(f"「{version_name}」版本无定数变化数据", reply_message=True)
        return
    from ..libraries.maimaidx_timing import run_timed
    from ..libraries.maimaidx_error import BreakInsufficientError
    try:
        result, _total = await run_timed(
            b50_pipeline(
                qqid=qqid,
                recalculate=True,
                ds_map=ds_map,
                by_group=False,
                max_display=35,
                compact_layout=True,
                hide_logo=False,
            ),
            billing_qqid=event.user_id,
        )
    except BreakInsufficientError as e:
        await plugin_finish(legacy_b35, str(e), event=event, reply_message=True)
        return
    if isinstance(result, str):
        await legacy_b35.finish(result, reply_message=True)
    else:
        await legacy_b35.finish(result + MessageSegment.text(_build_footer(qqid, _total)), reply_message=True)


@dx2025_b50.handle()
async def _(event: MessageEvent, user_id: Optional[int] = Depends(get_at_qq)):
    """dx2025b50：读取 2026-06-09 本地存档 + PRiSM 定数，按 2025 规则分 B35/B15 出图"""
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, 'score'):
        raise IgnoredException('功能已禁用')
    qqid = resolve_score_qqid(event, user_id)
    from ..libraries.maimaidx_version_alias import resolve_version_alias, build_legacy_ds_map
    from ..libraries.maimaidx_b50_pipeline import DX2025_SNAPSHOT_DATE, dx2025_b50_pipeline
    version_name = resolve_version_alias("dx2025")
    try:
        ds_map = build_legacy_ds_map(version_name)
    except FileNotFoundError:
        await dx2025_b50.finish("未找到 dxdata.json 文件", reply_message=True)
        return
    except Exception as e:
        await dx2025_b50.finish(f"加载定数数据失败：{e}", reply_message=True)
        return
    if not ds_map:
        await dx2025_b50.finish(f"「{version_name}」版本无定数变化数据", reply_message=True)
        return
    from ..libraries.maimaidx_timing import run_timed
    from ..libraries.maimaidx_error import BreakInsufficientError
    try:
        result, _total = await run_timed(
            dx2025_b50_pipeline(qqid=qqid, ds_map=ds_map),
            billing_qqid=event.user_id,
        )
    except BreakInsufficientError as e:
        await plugin_finish(dx2025_b50, str(e), event=event, reply_message=True)
        return
    if isinstance(result, str):
        await dx2025_b50.finish(result, reply_message=True)
    else:
        footer = _build_footer(qqid, _total)
        footer += f"\n基于本地存档 {DX2025_SNAPSHOT_DATE} · {version_name} 定数"
        await dx2025_b50.finish(result + MessageSegment.text(footer), reply_message=True)


@dx2026_b35.handle()
async def _(event: MessageEvent):
    """dx2026b35 已废弃：2026 更新后 B35 即常规 b50"""
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, 'score'):
        raise IgnoredException('功能已禁用')
    await dx2026_b35.finish(
        "喂喂喂？舞萌已经更新DX2026啦！请使用'b50'获得成绩图！",
        reply_message=True,
    )


@tag_analysis.handle()
async def _(event: MessageEvent, user_id: Optional[int] = Depends(get_at_qq)):
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, 'tag_analysis'):
        raise IgnoredException('功能已禁用')
    qqid = resolve_score_qqid(event, user_id)

    async def _gen():
        from ..libraries.maimaidx_datasource import get_user_b50
        import asyncio
        from ..libraries.maimaidx_image_executor import run_image_cpu
        try:
            userinfo = await get_user_b50(qqid=qqid)
        except (UserNotFoundError, UserNotExistsError, UserDisabledQueryError) as e:
            return str(e)
        # 预拉取 v.wmc.pub 标签
        wmc_cache = {}
        wmc_key = maiconfig.wmc_api_key
        if wmc_key:
            api = WmcAPI(resolve_wmc_base_url(maiconfig), wmc_key)
            wmc_items = []
            for chart_list in (getattr(userinfo.charts, 'sd', None) or [], getattr(userinfo.charts, 'dx', None) or []):
                if not chart_list:
                    continue
                for chart in chart_list:
                    sid = getattr(chart, 'song_id', None)
                    li = getattr(chart, 'level_index', 0)
                    if sid is None:
                        continue
                    music = mai.total_list.by_id(str(sid))
                    if not music:
                        continue
                    wmc_sid = song_id_for_wmc(music)
                    kind = kind_for_wmc(music)
                    diff_val = diff_value_for_wmc(li)
                    wmc_items.append(((wmc_sid, kind, diff_val), make_chart_key(wmc_sid, kind, diff_val)))
            if wmc_items:
                sem = asyncio.Semaphore(12)

                async def _fetch(cache_key, chart_key):
                    async with sem:
                        try:
                            return cache_key, await asyncio.wait_for(
                                api.get_tags(chart_key, radar_threshold=0, feature_threshold=0.3),
                                timeout=10.0,
                            )
                        except Exception:
                            return cache_key, None

                results = await asyncio.gather(*(_fetch(k, ck) for k, ck in wmc_items))
                for k, r in results:
                    if isinstance(r, dict) and isinstance(r.get("tags"), dict):
                        wmc_cache[k] = r["tags"]
        stats = get_b50_tag_stats(userinfo, wmc_tags_cache=wmc_cache or None)
        im = await run_image_cpu(draw_analysis, stats)
        return await run_image_cpu(lambda: MessageSegment.image(image_to_message_segment(im)))

    from ..libraries.maimaidx_timing import finish_timed
    await finish_timed(tag_analysis, _gen(), billing_qqid=event.user_id)


@weakness_prescription.handle()
async def _weakness_prescription(event: MessageEvent, user_id: Optional[int] = Depends(get_at_qq)):
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, 'tag_analysis'):
        raise IgnoredException('功能已禁用')
    qqid = resolve_score_qqid(event, user_id)

    async def _gen():
        return await generate_weakness_prescription(qqid)

    from ..libraries.maimaidx_timing import finish_timed
    await finish_timed(weakness_prescription, _gen(), billing_qqid=event.user_id)


@b50_risk_warning.handle()
async def _b50_risk_warning(event: MessageEvent, user_id: Optional[int] = Depends(get_at_qq)):
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, 'score'):
        raise IgnoredException('功能已禁用')
    qqid = resolve_score_qqid(event, user_id)

    async def _gen():
        return await generate_b50_risk_warning(qqid)

    from ..libraries.maimaidx_timing import finish_timed
    await finish_timed(b50_risk_warning, _gen(), billing_qqid=event.user_id)


@head_to_head.handle()
async def _head_to_head(
    event: MessageEvent,
    message: Message = CommandArg(),
    at_qq: Optional[int] = Depends(get_at_qq),
):
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, 'score'):
        raise IgnoredException('功能已禁用')
    if not at_qq:
        await head_to_head.finish('请使用「对战战绩@某人」并 @ 一位群友。', reply_message=True)
    qqid = resolve_score_qqid(event)
    if at_qq == qqid:
        await head_to_head.finish('请 @ 除自己以外的另一位群友。', reply_message=True)

    nick_a = _display_name_from_sender(event.sender) or str(qqid)
    nick_b = str(at_qq)
    if isinstance(event, GroupMessageEvent):
        try:
            try:
                bot = get_bot()
            except Exception:
                bot = get_bot(str(event.self_id))
            member = await bot.call_api('get_group_member_info', group_id=event.group_id, user_id=at_qq)
            card = (member.get('card') or '').strip()
            nick_b = card or (member.get('nickname') or str(at_qq)).strip() or '未知'
        except Exception:
            pass

    async def _gen():
        return await generate_head_to_head(qqid, at_qq, nick_a, nick_b)

    from ..libraries.maimaidx_timing import finish_timed
    await finish_timed(head_to_head, _gen(), billing_qqid=event.user_id)


@rating_sandbox.handle()
async def _rating_sandbox(
    event: MessageEvent,
    message: Message = CommandArg(),
    user_id: Optional[int] = Depends(get_at_qq),
):
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, 'score'):
        raise IgnoredException('功能已禁用')
    qqid = resolve_score_qqid(event, user_id)
    arg = message.extract_plain_text().strip()
    if not arg:
        await rating_sandbox.finish('请指定目标 Rating，例如：目标rating 16000', reply_message=True)
    try:
        target = int(arg.split()[0])
    except (ValueError, IndexError):
        await rating_sandbox.finish('目标 Rating 格式不正确，例如：目标rating 16000', reply_message=True)

    username = ''
    parts = arg.split()
    if len(parts) > 1:
        username = ' '.join(parts[1:]).strip()

    await _finish_score(
        rating_sandbox,
        generate_rating_sandbox(None if username else qqid, target, username or None),
        None if username else qqid,
        username=username or None,
        billing_qqid=event.user_id,
    )


@minfo.handle()
async def _(
    event: MessageEvent, 
    message: Message = CommandArg(), 
    user_id: Optional[int] = Depends(get_at_qq)
):
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, 'score'):
        raise IgnoredException('功能已禁用')
    qqid = resolve_score_qqid(event, user_id)
    args = message.extract_plain_text().strip()
    if not args:
        await minfo.finish('请输入曲目id或曲名', reply_message=True)

    if mai.total_list.by_id(args):
        music_id = args
    elif by_t := mai.total_list.by_title(args):
        music_id = by_t.id
    else:
        aliases = mai.total_alias_list.by_alias(args)
        if not aliases:
            await minfo.finish('未找到曲目')
        elif len(aliases) != 1:
            msg = '找到相同别名的曲目，请使用以下ID查询：\n'
            for music_id in aliases:
                msg += f'{music_id.SongID}：{music_id.Name}\n'
            await minfo.finish(msg.strip())
        else:
            music_id = str(aliases[0].SongID)
    
    from ..libraries.maimaidx_timing import finish_timed
    await finish_timed(minfo, draw_music_play_data(qqid, music_id), billing_qqid=event.user_id)


@ginfo.handle()
async def _(event: MessageEvent, message: Message = CommandArg()):
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, 'score'):
        raise IgnoredException('功能已禁用')
    args = message.extract_plain_text().strip()
    if not args:
        await ginfo.finish('请输入曲目id或曲名', reply_message=True)
    if args[0] not in '绿黄红紫白':
        level_index = 3
    else:
        level_index = '绿黄红紫白'.index(args[0])
        args = args[1:].strip()
        if not args:
            await ginfo.finish('请输入曲目id或曲名', reply_message=True)
    if mai.total_list.by_id(args):
        id = args
    elif by_t := mai.total_list.by_title(args):
        id = by_t.id
    else:
        alias = mai.total_alias_list.by_alias(args)
        if not alias:
            await ginfo.finish('未找到曲目', reply_message=True)
        elif len(alias) != 1:
            msg = '找到相同别名的曲目，请使用以下ID查询：\n'
            for songs in alias:
                msg += f'{songs.SongID}：{songs.Name}\n'
            await ginfo.finish(msg.strip(), reply_message=True)
        else:
            id = str(alias[0].SongID)
    
    music = mai.total_list.by_id(id)
    if not music.stats:
        await ginfo.finish('该乐曲还没有统计信息', reply_message=True)
    if len(music.ds) == 4 and level_index == 4:
        await ginfo.finish('该乐曲没有这个等级', reply_message=True)
    if not music.stats[level_index]:
        await ginfo.finish('该等级没有统计信息', reply_message=True)
    stats = music.stats[level_index]
    data = await music_global_data(music, level_index) + dedent(f'''\
        游玩次数：{round(stats.cnt)}
        拟合难度：{stats.fit_diff:.2f}
        平均达成率：{stats.avg:.2f}%
        平均 DX 分数：{stats.avg_dx:.1f}
        谱面成绩标准差：{stats.std_dev:.2f}''')
    await ginfo.finish(data, reply_message=True)


@score.handle()
async def _(event: MessageEvent, message: Message = CommandArg()):
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event.group_id, 'score'):
        raise IgnoredException('功能已禁用')
    _args = message.extract_plain_text().strip()
    args = _args.split()
    if args and args[0] == '帮助':
        msg = dedent('''\
            此功能为查找某首歌分数线设计。
            命令格式：分数线「难度+歌曲id」「分数线」
            例如：分数线 紫799 100
            命令将返回分数线允许的「TAP」「GREAT」容错，
            以及「BREAK」50落等价的「TAP」「GREAT」数。
            以下为「TAP」「GREAT」的对应表：
                    GREAT / GOOD / MISS
            TAP         1 / 2.5  / 5
            HOLD        2 / 5    / 10
            SLIDE       3 / 7.5  / 15
            TOUCH       1 / 2.5  / 5
            BREAK       5 / 12.5 / 25 (外加200落)
        ''').strip()
        from ..libraries.maimaidx_timing import finish_timed_sync
        await finish_timed_sync(score, lambda: MessageSegment.image(text_to_bytes_io(msg)))
    else:
        try:
            result = re.search(r'([绿黄红紫白])\s?([0-9]+)', _args)
            level_labels = ['绿', '黄', '红', '紫', '白']
            level_labels2 = ['Basic', 'Advanced', 'Expert', 'Master', 'Re:MASTER']
            level_index = level_labels.index(result.group(1))
            chart_id = result.group(2)
            line = float(args[-1])
            music = mai.total_list.by_id(chart_id)
            chart = music.charts[level_index]
            tap = int(chart.notes.tap)
            slide = int(chart.notes.slide)
            hold = int(chart.notes.hold)
            touch = int(chart.notes.touch) if len(chart.notes) == 5 else 0
            brk = int(chart.notes.brk)
            if brk <= 0:
                await score.finish('该谱面音符数据异常，无法计算分数线', reply_message=True)
            total_score = tap * 500 + slide * 1500 + hold * 1000 + touch * 500 + brk * 2500
            break_bonus = 0.01 / brk
            break_50_reduce = total_score * break_bonus / 4
            reduce = 101 - line
            if reduce <= 0 or reduce >= 101:
                raise ValueError
            msg = dedent(f'''\
                {music.title}「{level_labels2[level_index]}」
                分数线「{line}%」
                允许的最多「TAP」「GREAT」数量为 
                「{(total_score * reduce / 10000):.2f}」(每个-{10000 / total_score:.4f}%),
                「BREAK」50落(一共「{brk}」个)
                等价于「{(break_50_reduce / 100):.3f}」个「TAP」「GREAT」(-{break_50_reduce / total_score * 100:.4f}%)
            ''').strip()
            await score.finish(msg, reply_message=True)
        except (AttributeError, ValueError) as e:
            log.exception(e)
            await score.finish('格式错误，输入“分数线 帮助”以查看帮助信息', reply_message=True)
