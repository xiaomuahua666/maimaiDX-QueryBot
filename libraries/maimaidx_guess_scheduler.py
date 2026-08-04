"""猜歌积分周期榜结算：刷新前归档并推送到已开启猜歌的群。"""

from loguru import logger as log
from nonebot import get_bot, require

require('nonebot_plugin_apscheduler')
from nonebot_plugin_apscheduler import scheduler

from .maimaidx_guess_score import guess_score
from .maimaidx_music import guess
from .maimaidx_platform import resolve_group_delivery_id


def _broadcast_group_ids() -> list:
    """Keep persisted legacy data keys separate from current delivery addresses."""
    targets = []
    seen = set()
    for stored_gid in guess.switch.enable:
        target = resolve_group_delivery_id(stored_gid)
        key = str(target)
        if key in seen:
            continue
        seen.add(key)
        targets.append(target)
    return targets


@scheduler.scheduled_job('cron', hour=23, minute=55, id='guess_score_period_archive')
async def archive_guess_score_periods() -> None:
    periods = guess_score.periods_to_archive_today()
    if not periods:
        return
    try:
        bot = get_bot()
    except Exception as e:
        log.warning(f'[maimai] 猜歌榜结算跳过：无法获取 Bot ({e})')
        return
    group_ids = _broadcast_group_ids()
    if not group_ids:
        return
    for period, period_key in periods:
        try:
            await guess_score.archive_and_broadcast_period(
                bot, group_ids, period, period_key,
            )
        except Exception as e:
            log.warning(
                f'[maimai] 猜歌{period}榜结算失败 ({period_key}): {type(e).__name__}: {e}'
            )
