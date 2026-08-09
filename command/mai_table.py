import re

from nonebot import on_fullmatch, on_regex
from nonebot.adapters.onebot.v11 import Message, MessageEvent, PrivateMessageEvent
from nonebot.params import Depends, RegexMatched
from ..libraries.maimaidx_bot_admin import PLUGIN_ADMIN_ONLY

from ..libraries.maimaidx_music_info import *
from ..libraries.maimaidx_player_score import *
from ..libraries.maimaidx_error import BreakInsufficientError, UserDisabledQueryError, UserNotExistsError, UserNotFoundError
from ..libraries.maimaidx_break import take_break_charge_footer
from ..libraries.maimaidx_timing import attach_timing, finish_timed, run_timed, run_timed_call
from ..libraries.maimaidx_update_plate import *
from ..libraries.maimaidx_platform import (
    billing_user_id,
    get_sender_display_name,
    parse_at_target_id,
    resolve_score_qqid,
)

_RISE_SCORE_TIP = "您可以通过开启数据存储 使用「今日吃分推荐」获取更有参考价值的个性化推荐上分曲目。"

update_table            = on_fullmatch('更新定数表', permission=PLUGIN_ADMIN_ONLY)
update_plate            = on_fullmatch('更新完成表', permission=PLUGIN_ADMIN_ONLY)
rating_table            = on_regex(r'([0-9]+\+?)定数表')
rating_table_pfm        = on_regex(r'^([0-9]+\+?)(([apfcp]+|\+)+)?完成表$', re.IGNORECASE)
plate_table_pfm         = on_regex(r'^([真超檄橙暁晓桃櫻樱紫菫堇白雪輝辉舞霸熊華华爽煌星宙祭祝双宴镜彩丸圆])([極极将舞神者]舞?)完成表\s?([0-9]+)?$')
rise_score              = on_regex(r'^我要在?([0-9]+\+?)?[上加\+]([0-9]+)?分\s?(.+)?')
plate_process           = on_regex(r'^([真超檄橙暁晓桃櫻樱紫菫堇白雪輝辉舞霸熊華华爽煌星宙祭祝双宴镜彩丸圆])([極极将舞神者]舞?)进度\s?(.+)?')
level_process           = on_regex(r'^([0-9]+\+?)\s?([abcdsfxp\+]+)\s?([\u4e00-\u9fa5]+)?\s?进度\s?([0-9]+)?(.+)?', re.IGNORECASE)
# 等级牌子进度：13将 / 14+极进度 / 13舞舞 等（等价于 13sss进度、13fc进度、13fdx进度）
level_plate_progress    = on_regex(r'^([0-9]+\+?)(舞舞|将|極|极|神|者)(?:进度)?(?:\s+(已完成|未完成|未开始|未游玩))?(?:\s+(\d+))?\s*(.*)?$')
level_achievement_list  = on_regex(r'^([0-9]+\.?[0-9]?\+?)\s?分数列表\s?([0-9]+)?\s?(.+)?')


def get_at_qq(message: MessageEvent) -> Optional[int]:
    target = parse_at_target_id(message)
    if target is None:
        return None
    return resolve_score_qqid(message, target)


@update_table.handle()
async def _(event: PrivateMessageEvent):
    await update_table.finish(await update_rating_table())
    

@update_plate.handle()
async def _(event: PrivateMessageEvent):
    await update_plate.finish(await update_plate_table())


@rating_table.handle()
async def _(match = RegexMatched()):
    args = match.group(1).strip()
    if args in levelList[:6]:
        await rating_table.finish('只支持查询lv7-15的定数表', reply_message=True)
    elif args in levelList[6:]:
        from ..libraries.maimaidx_table_image import rating_table_path
        from ..libraries.maimaidx_update_plate import rating_table_is_current

        if not rating_table_is_current(args):
            await rating_table.finish(
                f'Level {args} 定数表底图缺失或已过期。\n'
                '请联系管理员私聊机器人执行「更新定数表」后再试。',
                reply_message=True,
            )
        path = rating_table_path(args)
        pic, total = run_timed_call(draw_rating, args, path)
        await rating_table.finish(attach_timing(pic, total), reply_message=True)
    else:
        await rating_table.finish('无法识别的定数', reply_message=True)


@rating_table_pfm.handle()
async def _(event: MessageEvent, match = RegexMatched()):
    ra = match.group(1)
    plan = match.group(2)
    if ra in levelList[:6]:
        await rating_table_pfm.finish('只支持查询lv7-15的完成表', reply_message=True)
    elif ra in levelList[6:]:
        await finish_timed(
            rating_table_pfm,
            draw_rating_table(resolve_score_qqid(event), ra, True if plan and plan.lower() in comboRank else False),
            billing_qqid=billing_user_id(event),
        )
    else:
        await rating_table_pfm.finish('无法识别的定数', reply_message=True)


@plate_table_pfm.handle()
async def _(event: MessageEvent, match = RegexMatched()):
    ver = match.group(1)
    plan = match.group(2)
    if ver in platecn:
        ver = platecn[ver]
    if f'{ver}{plan}' == '真将':
        await plate_table_pfm.finish('真系没有真将哦', reply_message=True)
    page = int(match.group(3)) if match.group(3) else 1
    qqid = resolve_score_qqid(event)
    await finish_timed(
        plate_table_pfm,
        draw_plate_table(qqid, ver, plan, page),
        billing_qqid=billing_user_id(event), event=event,
    )


@rise_score.handle()
async def _(event: MessageEvent, match = RegexMatched(), user_id: Optional[int] = Depends(get_at_qq)):
    qqid = user_id or resolve_score_qqid(event)
    username = None
    
    rating = match.group(1)
    score = match.group(2)
    
    if rating and rating not in levelList:
        await rise_score.finish('无此等级', reply_message=True)
    elif match.group(3):
        username = match.group(3).strip()
    if username:
        qqid = None

    try:
        result, total = await run_timed(
            rise_score_data(qqid, username, rating, score),
            billing_qqid=billing_user_id(event),
        )
    except BreakInsufficientError as e:
        await rise_score.finish(str(e), reply_message=True)
        return
    charge = take_break_charge_footer()
    charge_text = ('\n' + '\n'.join(charge)) if charge else ''
    if isinstance(result, str):
        await rise_score.finish(result + charge_text, reply_message=True)
    else:
        msg = Message(result) + Message(f"\n{_RISE_SCORE_TIP}{charge_text}")
        await rise_score.finish(attach_timing(msg, total), reply_message=True)


@plate_process.handle()
async def _(event: MessageEvent, match = RegexMatched(), user_id: Optional[int] = Depends(get_at_qq)):
    qqid = user_id or resolve_score_qqid(event)
    ver = match.group(1)
    plan = match.group(2)
    
    if f'{ver}{plan}' == '真将':
        await plate_process.finish('真系没有真将哦', reply_message=True)

    display_name = get_sender_display_name(event) or 'Milk'
    await finish_timed(
        plate_process, player_plate_data(qqid, '', ver, plan, user_name=display_name),
        billing_qqid=billing_user_id(event), event=event,
    )


@level_process.handle()
async def _(event: MessageEvent, match = RegexMatched(), user_id: Optional[int] = Depends(get_at_qq)):
    qqid = user_id or resolve_score_qqid(event)
    
    level = match.group(1)
    plan = match.group(2)
    category = match.group(3)
    page = match.group(4)
    username = match.group(5)
    
    if level not in levelList:
        await level_process.finish('无此等级', reply_message=True)
    if plan.lower() not in scoreRank + comboRank + syncRank:
        await level_process.finish('无此评价等级', reply_message=True)
    if levelList.index(level) < 11 or (plan.lower() in scoreRank and scoreRank.index(plan.lower()) < 8):
        await level_process.finish('兄啊，有点志向好不好', reply_message=True)
    if category:
        if category in ['已完成', '未完成', '未开始']:
            _c = {
                '已完成': 'completed',
                '未完成': 'unfinished',
                '未开始': 'notstarted',
                '未游玩': 'notstarted'
            }
            category = _c[category]
        else:
            await level_process.finish(f'无法指定查询「{category}」', reply_message=True)
    else:
        category = 'default'

    await finish_timed(
        level_process,
        level_process_data(qqid, username, level, plan, category, int(page) if page else 1),
        billing_qqid=billing_user_id(event),
        event=event,
    )


@level_plate_progress.handle()
async def _level_plate_progress(event: MessageEvent, match=RegexMatched(), user_id: Optional[int] = Depends(get_at_qq)):
    qqid = user_id or resolve_score_qqid(event)
    level = match.group(1)
    plan_cn = match.group(2)
    category_cn = match.group(3)
    page = match.group(4)
    username = (match.group(5) or '').strip() or None

    if level not in levelList:
        await level_plate_progress.finish('无此等级', reply_message=True)
    if levelList.index(level) < 11:
        await level_plate_progress.finish('只支持查询 lv12 及以上等级牌子', reply_message=True)
    try:
        plan = resolve_level_plate_plan(plan_cn)
    except ValueError as e:
        await level_plate_progress.finish(str(e), reply_message=True)

    if plan_cn != '者' and plan in scoreRank and scoreRank.index(plan) < 8:
        await level_plate_progress.finish('兄啊，有点志向好不好', reply_message=True)

    category = 'default'
    if category_cn:
        if category_cn in ['已完成', '未完成', '未开始', '未游玩']:
            category = {'已完成': 'completed', '未完成': 'unfinished', '未开始': 'notstarted', '未游玩': 'notstarted'}[category_cn]
        else:
            await level_plate_progress.finish(f'无法指定查询「{category_cn}」', reply_message=True)

    page_n = int(page) if page else 1

    async def _generate():
        summary = await level_plate_summary_text(qqid, username, level, plan_cn)
        pic = await level_process_data(qqid, username, level, plan, category, page_n)
        return summary, pic

    try:
        result, total = await run_timed(_generate(), billing_qqid=billing_user_id(event))
    except BreakInsufficientError as e:
        await level_plate_progress.finish(str(e), reply_message=True)
        return
    except (UserNotFoundError, UserNotExistsError, UserDisabledQueryError) as e:
        await level_plate_progress.finish(str(e), reply_message=True)
        return

    summary, pic = result
    charge = take_break_charge_footer()
    charge_text = ('\n' + '\n'.join(charge)) if charge else ''
    if isinstance(pic, str):
        await level_plate_progress.finish(pic + charge_text, reply_message=True)
    from nonebot.adapters.onebot.v11 import Message
    await level_plate_progress.finish(
        attach_timing(Message(summary) + Message(pic), total, extra=charge_text.strip()),
        reply_message=True,
    )


@level_achievement_list.handle()
async def _(event: MessageEvent, match = RegexMatched(), user_id: Optional[int] = Depends(get_at_qq)):
    qqid = user_id or resolve_score_qqid(event)

    rating = match.group(1)
    page = match.group(2)
    username = match.group(3)
    
    try:
        if '.' in rating:
            rating = round(float(rating), 1)
        elif rating not in levelList:
            await level_achievement_list.finish('无此等级', reply_message=True)
    except ValueError:
        if rating not in levelList:
            await level_achievement_list.finish('无此等级', reply_message=True)

    await finish_timed(
        level_achievement_list,
        level_achievement_list_data(qqid, username, rating, int(page) if page else 1),
        billing_qqid=billing_user_id(event),
        event=event,
    )
