import nonebot
from nonebot.plugin import PluginMetadata, require
from pathlib import Path
import asyncio

from .config import Config, driver, log, maiconfig, plate_tabledir, rating_table_dir
from .libraries.maimaidx_divingfish_oauth import oauth_enabled as divingfish_oauth_enabled
from .libraries.maimaidx_platform import (
    cleanup_qq_public_images,
    install_qq_event_compat,
)

install_qq_event_compat()

from .command import *
nonebot.load_plugin("nonebot_plugin_maimaidx.command.mai_jacket")
from .libraries.maimaidx_music_info import get_music_tags
from .libraries import maimaidx_admin_web as _maimaidx_admin_web  # 注册可选管理 WebUI
from .libraries import maimaidx_storage_runtime as _maimaidx_storage_runtime  # 统一存储同步
from .libraries import maimaidx_pending_session as _maimaidx_pending_session  # 关机通知未完成交互

scheduler = require('nonebot_plugin_apscheduler')

from nonebot_plugin_apscheduler import scheduler

__plugin_meta__ = PluginMetadata(
    name='nonebot-plugin-maimaidx',
    description='移植自 mai-bot 开源项目，基于 nonebot2 的街机音游 舞萌DX 的查询插件',
    usage='请使用 帮助maimaiDX 指令查看使用方法',
    type='application',
    config=Config,
    homepage='https://github.com/AWMC-TEAM/maimaiDX-QueryBot',
    supported_adapters={'~onebot.v11', '~qq'}
)

sub_plugins = nonebot.load_plugins(
    str(Path(__file__).parent.joinpath('plugins').resolve())
)


@driver.on_startup
async def get_music():
    """
    bot启动时开始获取所有数据
    """
    # The QQ adapter can be imported after the plugin module during NoneBot
    # startup.  Retry the idempotent compatibility install once adapters are
    # registered so plain-text @ replies use the same path after every restart.
    install_qq_event_compat()
    cleanup_qq_public_images(force=True)
    _wmc_key = bool(getattr(maiconfig, 'wmc_api_key', None))
    log.opt(colors=True).info('谱面标签(v.wmc.pub): ' + ('<g>已配置</g>' if _wmc_key else '<y>未配置 wmc_api_key，详情图不显示 WMC 标签</y>'))
    if maiconfig.maimaidxproberproxy:
        log.info('正在使用代理服务器访问查分器')
    if maiconfig.maimaidxaliasproxy:
        log.info('正在使用代理服务器访问别名服务器')
    maiApi.load_token_proxy()
    if divingfish_oauth_enabled():
        log.info('水鱼查分器已启用 OAuth：用户可发送「绑定水鱼」授权读取本人成绩')
        if maiApi.tokens:
            log.warning('已同时配置水鱼开发者 Token；OAuth 优先，旧 Token 仅用于用户名查询过渡')
    elif maiconfig.divingfish_oauth_enabled:
        log.warning('已开启水鱼 OAuth，但 CLIENT_ID / CLIENT_SECRET 未完整配置，已回退 Import-Token 模式')
    elif maiApi.tokens:
        log.info('水鱼 OAuth 开关关闭，使用 Import-Token 绑定兼容模式')
    else:
        log.warning('水鱼 OAuth 开关关闭且未配置开发者 Token；仅保留公开 B50 与 Import-Token 上传功能')
    if maiconfig.maimaidxaliaspush:
        log.opt(colors=True).info('别名推送为「<g>开启</g>」状态')
        asyncio.ensure_future(ws_alias_server())
    else:
        log.opt(colors=True).info('别名推送为「<r>关闭</r>」状态')
    log.info('正在获取maimai所有曲目信息')
    await mai.get_music()
    log.info('正在获取maimai牌子数据')
    await mai.get_plate_json()
    log.info('正在获取maimai所有曲目别名信息')
    await mai.get_music_alias()
    mai.guess()
    log.success('maimai数据获取完成')
    if maiconfig.saveinmem:
        ScoreBaseImage._load_image()
        log.success('已将图片保存在内存中')
    
    if not rating_table_dir.exists() or not list(rating_table_dir.iterdir()):
        log.opt(colors=True).warning(
            '<y>注意！注意！</y>检测到定数表文件夹为空！'
            '可能导致「定数表」「完成表」指令无法使用，'
            '请及时私聊BOT使用指令「更新定数表」进行生成。'
        )
    else:
        from .libraries.maimaidx_update_plate import stale_rating_table_names

        stale_tables = stale_rating_table_names()
        if stale_tables:
            preview = '、'.join(stale_tables[:8])
            suffix = f' 等 {len(stale_tables)} 张' if len(stale_tables) > 8 else ''
            log.opt(colors=True).warning(
                f'<y>检测到定数表底图缺失或已过期：</y>{preview}{suffix}。'
                '请及时私聊BOT使用指令「更新定数表」重新生成。'
            )
    if not plate_tabledir.exists() or not list(plate_tabledir.iterdir()):
        log.opt(colors=True).warning(
            '<y>注意！注意！</y>检测到完成表文件夹为空！'
            '可能导致牌子「完成表」指令无法使用，'
            '请及时私聊BOT使用指令「更新完成表」进行生成。'
        )
    else:
        from .libraries.maimaidx_update_plate import stale_plate_table_names

        stale_tables = stale_plate_table_names()
        if stale_tables:
            preview = '、'.join(stale_tables[:8])
            suffix = f' 等 {len(stale_tables)} 张' if len(stale_tables) > 8 else ''
            log.opt(colors=True).warning(
                f'<y>检测到完成表底图缺失或已过期：</y>{preview}{suffix}。'
                '请及时私聊BOT使用指令「更新完成表」重新生成。'
            )
    log.opt(colors=True).success('<g>maimaiDX 插件初始化完成，等待客户端连接</g>')
    try:
        from .libraries.maimaidx_guess_chart import (
            ADAPTIVE_ENABLED,
            schedule_chart_cache_background_fill,
            schedule_chart_cache_auto_prepare,
            schedule_chart_render_recovery,
        )
        from .libraries.maimaidx_guess_audio import (
            schedule_audio_cache_auto_prepare,
            schedule_audio_render_recovery,
        )

        schedule_chart_cache_background_fill()
        # 启动时先恢复被重启打断的任务；没有待恢复任务时自动增量预制。
        schedule_chart_render_recovery()
        schedule_audio_render_recovery()
        schedule_chart_cache_auto_prepare()
        schedule_audio_cache_auto_prepare()
        if ADAPTIVE_ENABLED:
            log.info('猜铺面自适应并发 + BGM 后台补洞 + 启动自动预制已调度')
        else:
            log.info('猜铺面 BGM 后台补洞已调度（固定并发）')
    except Exception as e:
        log.warning(f'猜铺面后台补洞调度失败: {e}')
    if maiconfig.b50_assets_path:
        from .libraries.b50_analysis.context_builder import load_peer_stats
        from .command.mai_b50_analysis import set_peer_stats
        stats = load_peer_stats(maiconfig.b50_assets_path)
        set_peer_stats(stats)
        if stats:
            log.info('B50 分析 peer_stats 已加载')
        else:
            log.warning('B50 分析 peer_stats 未找到，分析b50 同段对比可能受限')
    elif maiconfig.b50_llm_key:
        log.warning('已配置 b50_llm_key 但未配置 b50_assets_path')

scheduler.add_job(update_daily, 'cron', hour=4)
scheduler.add_job(
    cleanup_qq_public_images,
    'interval',
    minutes=10,
    id='maimaidx_qq_media_cleanup',
    replace_existing=True,
    coalesce=True,
    max_instances=1,
)
