import asyncio

from nonebot import on_command, on_regex
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageEvent, PrivateMessageEvent
from nonebot.exception import IgnoredException
from nonebot.params import CommandArg, RegexMatched
from ..libraries.maimaidx_bot_admin import PLUGIN_ADMIN_ONLY

from ..config import project_attribution_message
from ..libraries.maimaidx_break import break_db, calculate_luck_break
from ..libraries.maimaidx_music import feature_manager
from ..libraries.maimaidx_music_info import *
from ..libraries.maimaidx_error import QBindRequiredError
from ..libraries.maimaidx_player_score import *
from ..libraries.maimaidx_platform import (
    billing_user_id,
    build_command_keyboard_message,
    event_group_data_id,
    platform_user_id,
    plugin_finish,
    resolve_score_qqid,
    use_qq_mode,
)
from ..libraries.maimaidx_qq_bind import qq_bind_db
from ..libraries.maimaidx_timing import finish_timed
from ..libraries.maimaidx_update_plate import *
from ..libraries.tool import qqhash

update_data         = on_command('更新maimai数据', permission=PLUGIN_ADMIN_ONLY)
help_cmd            = on_command('帮助', aliases={'help', '帮助maimaiDX', '帮助maimaidx'})
setattr(help_cmd, '_maimaidx_qbind_exempt', True)
maimaidxrepo        = on_command('项目地址maimaiDX', aliases={'项目地址maimaidx'})
# Documentation commands do not need a bound score account.
setattr(maimaidxrepo, '_maimaidx_qbind_exempt', True)
mai_today           = on_command('今日mai', aliases={'今日舞萌', '今日运势'})
setattr(mai_today, "_maimaidx_debt_exempt", True)
mai_what            = on_regex(r'.*mai.*什么(.+)?')
random_song         = on_regex(r'^[随来给]个((?:dx|sd|标准))?([绿黄红紫白]?)([0-9]+\+?).*')
rating_ranking      = on_command('查看排名', aliases={'查看排行'})
my_rating_ranking   = on_command('我的排名')
theme_cmd           = on_command('主题', aliases={'theme'})


_QQ_HELP_POPULAR = (
    ('标准 B50', 'b50'), ('刷新 B50', '刷新b50'), ('B50 锐评', '锐评一下'),
    ('AP50', 'ap50'), ('FC50', 'fc50'), ('吃分推荐', '吃分推荐'),
    ('含金量', '含金量'), ('含水量', '含水量'), ('MyMai', 'mymai'),
    ('签到', '签到'), ('猜歌', '猜歌'), ('猜封面', '猜封面'),
    ('今日舞萌', '今日舞萌'), ('查歌', '查歌'), ('完整文档', '帮助 文档'),
)

_TODAY_SHORTCUTS = (
    ('今日运势', '今日舞萌'), ('标准 B50', 'b50'),
    ('吃分推荐', '吃分推荐'), ('签到', '签到'),
    ('猜歌', '猜歌'), ('帮助', 'help'),
)

_SONG_RECOMMEND_SHORTCUTS = (
    ('再推荐一首', '今天mai什么'), ('吃分推荐', '吃分推荐'),
    ('查歌', '查歌'), ('随机 13+', '随个13+'),
    ('今日舞萌', '今日舞萌'), ('标准 B50', 'b50'),
)

_RANKING_SHORTCUTS = (
    ('我的排名', '我的排名'), ('Rating 排行', '查看排名'),
    ('群 Rating 榜', '群聊rating排行榜'), ('标准 B50', 'b50'),
    ('含金量', '含金量'), ('帮助', 'help'),
)


def _qq_help_message(event: MessageEvent, section: str = ''):
    """Build one qbind-first or popular-command official QQ help message."""
    if not use_qq_mode(event):
        return None
    bound = qq_bind_db.get_legacy_qq(platform_user_id(event))
    if bound is None:
        return build_command_keyboard_message(
            (('绑定 qbind', 'qbind'),),
            event=event,
            title='尚未绑定查分 QQ\n请先点击下方按钮完成论坛绑定。',
            columns=1,
            id_prefix='maimaidx-help-qbind',
        )
    key = str(section or '').strip()
    title = '🎛️ AWMC 指令菜单'
    if key == '文档':
        title += '\n完整说明：https://wiki.awmc.team/guide/bot/intro'
    else:
        title += '\n这里是最常用的功能；完整用法请查看文档。'
    return build_command_keyboard_message(
        _QQ_HELP_POPULAR,
        event=event,
        title=title,
        columns=3,
        id_prefix='maimaidx-help-home',
    )


@update_data.handle()
async def _(event: PrivateMessageEvent):
    await mai.get_music(force=True)
    await mai.get_music_alias(force=True)
    await mai.get_plate_json()
    await update_data.finish('maimai数据更新完成')


@help_cmd.handle()
async def _(event: MessageEvent, args: Message = CommandArg()):
    payload = _qq_help_message(event, args.extract_plain_text())
    if payload is not None:
        await plugin_finish(
            help_cmd, payload, event=event,
            reply_message=False, mention_sender=False,
        )
        return
    await help_cmd.finish(
        '机器人帮助请前往\nhttps://wiki.awmc.team/guide/bot/intro',
    )


@maimaidxrepo.handle()
async def _():
    await maimaidxrepo.finish(project_attribution_message(), reply_message=True)


@mai_today.handle()
async def _(event: MessageEvent):
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event_group_data_id(event), 'today'):
        raise IgnoredException('功能已禁用')
    wm_list = [
        '拼机', 
        '推分', 
        '越级', 
        '下埋', 
        '夜勤', 
        '练底力', 
        '练手法', 
        '打旧框', 
        '干饭', 
        '抓绝赞', 
        '收歌'
    ]
    h = qqhash(billing_user_id(event))
    rp = h % 100
    rounded_rp, luck_break = calculate_luck_break(rp)
    reward = await asyncio.to_thread(
        break_db.claim_daily_reward,
        billing_user_id(event), 'today_luck', luck_break,
        reason='today_luck',
        meta={'luck': rp, 'rounded_luck': rounded_rp},
    )
    wm_value = []
    for i in range(11):
        wm_value.append(h & 3)
        h >>= 2
    msg = f'今日人品值：{rp}\n'
    if reward.awarded:
        msg += (
            f'今日 BREAK：{rp} 四舍五入为 {rounded_rp}，'
            f'÷20 获得 {reward.amount} BREAK（余额 {reward.balance}）\n'
        )
    else:
        msg += (
            f'今日 BREAK：已领取 {reward.amount} BREAK，'
            f'不会重复发放（人品 ÷20，余额 {reward.balance}）\n'
        )
    for i in range(11):
        if wm_value[i] == 3:
            msg += f'宜 {wm_list[i]}\n'
        elif wm_value[i] == 0:
            msg += f'忌 {wm_list[i]}\n'
    music = mai.total_list[h % len(mai.total_list)]
    ds = '/'.join([str(_) for _ in music.ds])
    msg += f'{maiconfig.botName} Bot提醒您：打机时不要大力拍打或滑动哦\n今日推荐歌曲：'
    msg += f'ID.{music.id} - {music.title}'
    msg += MessageSegment.image(music_picture(music.id))
    msg += ds
    await plugin_finish(
        mai_today, msg, event=event, reply_message=True,
        qq_buttons=_TODAY_SHORTCUTS,
    )


@mai_what.handle()
async def _(event: MessageEvent, match = RegexMatched()):
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event_group_data_id(event), 'query'):
        raise IgnoredException('功能已禁用')

    async def _gen():
        music = mai.total_list.random()
        user = None
        score_qqid = None
        if (point := match.group(1)) and ('推分' in point or '上分' in point or '加分' in point):
            try:
                score_qqid = resolve_score_qqid(event)
                from ..libraries.maimaidx_datasource import get_user_b50
                user = await get_user_b50(qqid=score_qqid)
                r = random.randint(0, 1)
                _ra = 0
                ignore = []
                if r == 0:
                    if b35 := user.charts.sd:
                        ignore = [m.song_id for m in b35 if m.achievements < 100.5]
                        _ra = b35[-1].ra
                else:
                    if b15 := user.charts.dx:
                        ignore = [m.song_id for m in b15 if m.achievements < 100.5]
                        _ra = b15[-1].ra
                if _ra != 0:
                    ds = round(_ra / 22.4, 1)
                    musiclist = mai.total_list.filter(ds=(ds, ds + 1))
                    musiclist = type(musiclist)(
                        m for m in musiclist if int(m.id) not in ignore
                    )
                    music = musiclist.random()
            except (UserNotFoundError, UserDisabledQueryError):
                pass
        return await draw_music_info(music, score_qqid, user)

    try:
        await finish_timed(
            mai_what, _gen(), billing_qqid=billing_user_id(event),
            feature_charge='search', event=event,
            qq_buttons=_SONG_RECOMMEND_SHORTCUTS,
        )
    except QBindRequiredError as exc:
        await mai_what.finish(str(exc), reply_message=True)


@random_song.handle()
async def _(event: MessageEvent, match = RegexMatched()):
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event_group_data_id(event), 'random'):
        raise IgnoredException('功能已禁用')
    try:
        diff = match.group(1)
        if diff == 'dx':
            tp = ['DX']
        elif diff == 'sd' or diff == '标准':
            tp = ['SD']
        else:
            tp = ['SD', 'DX']
        level = match.group(3)
        if match.group(2) == '':
            music_data = mai.total_list.filter(level=level, type=tp)
        else:
            music_data = mai.total_list.filter(
                level=level, 
                diff=['绿黄红紫白'.index(match.group(2))], 
                type=tp
            )
    except Exception:
        await random_song.finish('随机命令错误，请检查语法', reply_message=True)
    if len(music_data) == 0:
        await random_song.finish('没有这样的乐曲哦。', reply_message=True)
    await finish_timed(
        random_song,
        draw_music_info(music_data.random()),
        billing_qqid=billing_user_id(event),
        feature_charge='search',
        event=event,
        qq_buttons=_SONG_RECOMMEND_SHORTCUTS,
    )


@rating_ranking.handle()
async def _(event: MessageEvent, message: Message = CommandArg()):
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event_group_data_id(event), 'ranking'):
        raise IgnoredException('功能已禁用')
    args = message.extract_plain_text().strip()
    name = ''
    page = 1
    if args.isdigit():
        page = int(args)
    else:
        name = args.lower()
    
    await finish_timed(
        rating_ranking,
        rating_ranking_data(name, page),
        event=event,
        qq_buttons=_RANKING_SHORTCUTS,
    )


@my_rating_ranking.handle()
async def _(event: MessageEvent):
    if isinstance(event, GroupMessageEvent) and not feature_manager.is_enabled(event_group_data_id(event), 'ranking'):
        raise IgnoredException('功能已禁用')
    try:
        from ..libraries.maimaidx_datasource import get_user_b50
        user = await get_user_b50(qqid=resolve_score_qqid(event))
        rank_data = await maiApi.rating_ranking()
        for num, rank in enumerate(rank_data):
            if rank.username == user.username:
                result = f'您的Rating为「{rank.ra}」，排名第「{num + 1}」名'
                await plugin_finish(
                    my_rating_ranking,
                    result,
                    event=event,
                    reply_message=True,
                    qq_buttons=_RANKING_SHORTCUTS,
                )
    except (UserNotFoundError, UserNotExistsError, UserDisabledQueryError) as e:
        await my_rating_ranking.finish(str(e), reply_message=True)


@theme_cmd.handle()
async def _(event: MessageEvent, message: Message = CommandArg()):
    from ..libraries.maimaidx_theme import Theme, get_theme_display_name, get_user_theme, set_user_theme
    args = message.extract_plain_text().strip()
    qqid = billing_user_id(event)

    if not args:
        current = get_user_theme(qqid)
        display = get_theme_display_name(current)
        await theme_cmd.finish(
            f'当前主题：{display}\n{Theme.get_help()}',
            reply_message=True,
        )

    t = Theme.get_by_name(args)
    if t is None:
        await theme_cmd.finish(f'未知主题「{args}」\n{Theme.get_help()}', reply_message=True)

    set_user_theme(qqid, t.value)
    await theme_cmd.finish(f'主题已切换为：{get_theme_display_name(t.value)}', reply_message=True)


async def update_daily():
    await mai.get_music()
    mai.guess()
    log.info('maimaiDX数据更新完毕')
