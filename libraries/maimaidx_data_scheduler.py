"""
数据存储定时任务模块

功能：
- 每天自动存储已开启用户的成绩
- 使用 nonebot 的定时任务调度器
"""

import asyncio
from datetime import datetime, timedelta
from typing import Optional

from loguru import logger as log
from nonebot import require, get_bot
from nonebot.adapters.onebot.v11 import Bot

# 使用 require 导入定时任务调度器
require("nonebot_plugin_apscheduler")
from nonebot_plugin_apscheduler import scheduler

from ..libraries.maimaidx_data_storage import data_storage, DailySnapshot
from ..libraries.maimaidx_datasource import get_user_records
from ..libraries.maimaidx_error import LxnsDataError


async def fetch_and_store_user_scores(
    qqid: int, *, source: str = "manual", target_date: Optional[str] = None
) -> bool:
    """
    获取并存储用户成绩
    
    Args:
        qqid: 用户QQ号
    
    Returns:
        是否成功
    """
    try:
        from .maimaidx_share_snapshot import build_daily_snapshot

        # 强制刷新并写入玩家缓存，供其它指令复用
        userinfo, dev_records = await get_user_records(qqid=qqid, force_refresh=True)
        records = list(dev_records or [])
        
        if not records:
            log.warning(f"[DataScheduler] 用户 {qqid} 没有成绩数据")
            return False

        snapshot = build_daily_snapshot(
            qqid,
            userinfo,
            records,
            source=source,
            target_date=target_date,
        )
        if snapshot is None:
            # 成绩过少时仍尽量落盘（个人开启存储场景）
            from .maimaidx_share_snapshot import playinfo_to_score_records

            score_records = playinfo_to_score_records(records)
            if not score_records:
                return False
            snapshot = DailySnapshot(
                date=target_date or datetime.now().strftime("%Y-%m-%d"),
                qqid=qqid,
                nickname=userinfo.nickname or userinfo.username or str(qqid),
                rating=userinfo.rating or 0,
                records=score_records,
                record_count=len(score_records),
                source=source,
            )

        success = data_storage.save_daily_snapshot(snapshot)
        if success:
            log.info(
                f"[DataScheduler] 成功存储用户 {qqid} 的 {snapshot.date} 成绩快照，"
                f"source={source}，共 {snapshot.record_count} 首，rating: {snapshot.rating}"
            )
        return success
        
    except LxnsDataError as e:
        # 用户没有 AWMCNET/水鱼/落雪成绩属于预期状态，不应以 ERROR 刷屏。
        log.debug(f"[DataScheduler] 用户 {qqid} 暂无可存储成绩: {e}")
        return False
    except Exception as e:
        log.error(f"[DataScheduler] 获取并存储用户 {qqid} 成绩失败: {e}")
        return False


async def daily_storage_task():
    """每日存储：个人开启用户 API 落盘 + 近期缓存中的共享用户静默贡献。"""
    log.info("[DataScheduler] 开始执行每日成绩存储任务")

    from .maimaidx_data_share import data_share
    from .maimaidx_player_cache import player_cache_db
    from .maimaidx_share_snapshot import MIN_SHARE_RECORDS, maybe_save_share_snapshot

    enabled_users = set(int(x) for x in data_storage.get_enabled_users())
    # 近 7 天查过分且有厚 records 的缓存用户
    since = (datetime.now() - timedelta(days=7)).timestamp()
    try:
        recent = player_cache_db.list_recent_full_records(
            since_ts=since, min_records=MIN_SHARE_RECORDS, limit=2000
        )
    except Exception as e:
        log.warning(f"[DataScheduler] 扫描 player_cache 失败: {e}")
        recent = []

    share_from_cache = 0
    for item in recent:
        qqid = int(item["qqid"])
        if not data_share.is_sharing_enabled(qqid):
            continue
        if maybe_save_share_snapshot(
            qqid,
            item["userinfo"],
            item["records"],
            source="share_cache_daily",
        ):
            share_from_cache += 1

    if not enabled_users and share_from_cache == 0:
        log.info("[DataScheduler] 无需存储的用户，跳过")
        return

    log.info(
        f"[DataScheduler] 个人存储 {len(enabled_users)} 人；"
        f"缓存贡献写入 {share_from_cache} 人"
    )

    semaphore = asyncio.Semaphore(5)

    async def store_one(qqid: int):
        async with semaphore:
            return await fetch_and_store_user_scores(qqid, source="auto")

    results = await asyncio.gather(
        *[store_one(qqid) for qqid in enabled_users], return_exceptions=True
    )
    success_count = sum(1 for r in results if r is True)
    fail_count = len(results) - success_count
    log.info(
        f"[DataScheduler] 每日存储完成：个人成功 {success_count} / 失败 {fail_count}；"
        f"共享缓存贡献 {share_from_cache}"
    )


# 添加定时任务：每天凌晨 4:00 执行
@scheduler.scheduled_job("cron", hour=4, minute=0, id="daily_score_storage")
async def scheduled_daily_storage():
    """定时任务：每天凌晨 4:00 自动存储成绩"""
    await daily_storage_task()


# 添加定时任务：每小时检查一次（用于启动时补存）
@scheduler.scheduled_job("cron", hour="*/6", minute=0, id="periodic_storage_check")
async def periodic_storage_check():
    """定期检查：每6小时检查一次是否需要补存"""
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    
    enabled_users = data_storage.get_enabled_users()
    users_to_store = []
    
    for qqid in enabled_users:
        # 检查今天是否已经存储过
        existing = data_storage.load_daily_snapshot(qqid, today)
        if not existing:
            users_to_store.append(qqid)
    
    if users_to_store:
        log.info(f"[DataScheduler] 发现 {len(users_to_store)} 个用户今天尚未存储成绩，开始补存")
        
        semaphore = asyncio.Semaphore(5)
        
        async def store_one(qqid: int):
            async with semaphore:
                return await fetch_and_store_user_scores(
                    qqid, source="periodic_check", target_date=today
                )
        
        tasks = [store_one(qqid) for qqid in users_to_store]
        await asyncio.gather(*tasks, return_exceptions=True)


# 启动时执行一次存储（用于补存昨天的数据）
async def on_startup_storage():
    """启动时执行：检查昨天是否存储，如果没有则补存"""
    await asyncio.sleep(30)  # 等待 30 秒，确保 bot 完全启动
    
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    enabled_users = data_storage.get_enabled_users()
    
    users_to_store = []
    for qqid in enabled_users:
        existing = data_storage.load_daily_snapshot(qqid, yesterday)
        if not existing:
            users_to_store.append(qqid)
    
    if users_to_store:
        log.info(f"[DataScheduler] 启动补存：{len(users_to_store)} 个用户昨天未存储")
        
        semaphore = asyncio.Semaphore(5)
        
        async def store_one(qqid: int):
            async with semaphore:
                return await fetch_and_store_user_scores(
                    qqid, source="startup_backfill", target_date=yesterday
                )
        
        tasks = [store_one(qqid) for qqid in users_to_store]
        await asyncio.gather(*tasks, return_exceptions=True)


# 注册启动任务
from nonebot import get_driver
driver = get_driver()
_startup_storage_started = False

@driver.on_bot_connect
async def _(bot):
    """Bot 连接时触发启动补存"""
    global _startup_storage_started
    if _startup_storage_started:
        log.info("[DataScheduler] 启动补存已调度，本次重连跳过")
        return
    _startup_storage_started = True
    asyncio.create_task(on_startup_storage())
