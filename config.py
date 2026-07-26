"""
nonebot-plugin-maimaidx 插件配置与路径。

- Config：从 nonebot 配置/环境变量加载，含查分器 token、代理、自定义背景等。
- 下方全局变量：静态路径（static、maimaidir、字体等）、业务常量。多人协作时修改配置请同步注释。
"""
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from loguru import logger as log
from nonebot import get_driver, get_plugin_config
from pydantic import BaseModel

from .libraries.maimaidx_attribution import ATTRIBUTION, project_message, short_footer

driver = get_driver()


class Config(BaseModel):
    """插件配置项；字段可通过 pyproject.toml [tool.nonebot.plugin] 或环境变量注入。"""
    
    maimaidxtoken: Optional[str] = None
    maimaidxpath: str
    maimaidxproberproxy: bool = False
    maimaidxaliasproxy: bool = False
    maimaidxaliaspush: bool = True
    saveinmem: Optional[bool] = True
    botName: str = list(driver.config.nickname)[0] if driver.config.nickname else 'maimai'
    dxrating_combined_tags_url: Optional[str] = 'https://derrakuma.dxrating.net/functions/v1/combined-tags'
    dxrating_token: Optional[str] = None
    dxrating_tags_json_path: Optional[str] = None
    # 谱面印象 API（舞萌 DX 谱面印象），默认 http://103.45.162.66:37913
    pmyx_api_base_url: Optional[str] = "http://103.45.162.66:37913"
    # sw-api 地址（PC 数 / 上传 / 倍率票），可通过环境变量 SDGBTECHAPI 设置
    sdgbtechapi: Optional[str] = None
    # 机台 keychip（sw-api 必填），可通过环境变量 SDGBT_CLIENT_ID 设置
    sdgbt_client_id: Optional[str] = None
    # ---------- AWMC 账号 / 上传服务（由原 maibot 合并） ----------
    # team：自建 sw-api（需 keychip）；public：AWMC 公共网关（Bearer gw_ 令牌）。
    awmc_api_mode: str = 'team'
    # team 留空时沿用 SDGBTECHAPI；public 留空默认 https://api.wmc.pub。
    awmc_api_base_url: Optional[str] = None
    # public 模式必填：控制台生成的 gw_ 长期令牌或登录 JWT。
    awmc_public_gateway_token: Optional[str] = None
    awmc_api_timeout_seconds: float = 120.0
    awmc_api_retry_count: int = 3
    awmc_api_retry_delay_seconds: float = 1.0
    awmc_upload_poll_interval_seconds: float = 2.0
    # 水鱼 / 落雪 B50 上传（update-fish / update-lx）单次 HTTP 超时。
    awmc_b50_upload_timeout_seconds: float = 120.0
    # 若上传响应仍带 task_id（旧网关），轮询上限；新版均为同步。
    awmc_upload_poll_timeout_seconds: float = 120.0
    # 落雪 OAuth 拉机台全量成绩保持 15s 硬超时，超时立即失败。
    # 有新鲜 PC 缓存时优先用本地成绩，不再打这条接口。
    awmc_user_music_timeout_seconds: float = 15.0
    awmc_user_music_retry_count: int = 0
    # 本地 PC 成绩可用于落雪 OAuth 直传的新鲜度（秒）；默认与 SGID 缓存一致。
    awmc_lxns_pc_cache_seconds: float = 600.0
    # 等待全局机台锁的最长时间（秒）；0=无限等待。超时返回「机台繁忙」。
    awmc_machine_lock_timeout_seconds: float = 60.0
    # AWMC 账号 API 成功返回后的静默冷却（秒）。
    awmc_api_success_cooldown_seconds: float = 1.0
    # 发票允许倍率，使用英文逗号分隔，例如 2,3,5。
    awmc_ticket_allowed_multipliers: str = '2,3,5'
    # 发票提交仅代表进入服务端队列；轮询队列与票券库存，确认到账后才结算 BREAK。
    awmc_ticket_poll_interval_seconds: float = 3.0
    awmc_ticket_poll_timeout_seconds: float = 120.0
    awmc_ticket_max_poll_timeout_seconds: float = 600.0
    awmc_ticket_seconds_per_request: float = 80.0
    # 队列 done 后等待票券数据落库，随后只查一次 /user/charge。
    awmc_ticket_settlement_delay_seconds: float = 2.0
    # 合并后的账号功能总开关；关闭时不注册外部调用，但本地查分不受影响。
    awmc_account_enabled: bool = True
    # 账号二维码本地缓存时间。0 表示永久保留，单位秒。
    awmc_qrcode_cache_seconds: int = 0
    # mymai/成绩上传复用最近一次已验证 SGID 的时长；0 表示每次重新询问。
    awmc_sgid_cache_seconds: int = 600
    # 舞萌状态附带的 Uptime Kuma 公开状态页（https://wiki.awmc.team/dev/status-api）。
    awmc_status_page_base: str = 'https://status.awmc.cc'
    awmc_status_page_slug: str = 'maimai'
    # mais 失败率统计 API；与实时 Uptime 状态页分开。
    awmc_failure_rate_url: str = 'https://api.wmc.pub/usage/failure-rate'
    awmc_status_cache_seconds: float = 30.0
    awmc_status_timeout_seconds: float = 8.0
    # 自动识别消息图片中的舞萌二维码；普通图片静默忽略。
    awmc_image_qrcode_enabled: bool = True
    # 单张待识别图片最大下载字节数，默认 8 MiB。
    awmc_image_qrcode_max_bytes: int = 8 * 1024 * 1024
    # ---------- 管理审计 / WebUI ----------
    maimaidx_admin_web_enabled: bool = False
    # 必须使用高强度随机值；WebUI 仅接受 Authorization: Bearer。
    maimaidx_admin_web_token: str = ''
    # 默认独立监听本机 8099，便于 Nginx/Caddy 反向代理。
    # port=0 时改为挂载到 NoneBot FastAPI Driver 的共享端口。
    maimaidx_admin_web_host: str = '127.0.0.1'
    maimaidx_admin_web_port: int = 8099
    maimaidx_admin_web_path: str = '/maimaidx/admin'
    maimaidx_admin_web_public_url: str = ''
    maimaidx_audit_retention_days: int = 90
    maimaidx_message_stats_enabled: bool = True
    # 60 秒内前 30 个真实功能请求免费；超出的每个请求向触发者加收 1 BREAK。
    maimaidx_busy_surcharge_enabled: bool = True
    maimaidx_busy_window_seconds: float = 60.0
    maimaidx_busy_free_requests: int = 30
    maimaidx_busy_surcharge_break: int = 1
    # 合并连续提示并省略非必要的“处理中”消息，降低平台发信频率。
    maimaidx_compact_messages: bool = True
    # 开始处理时对触发消息贴的 QQ 表情 ID（NapCat set_msg_emoji_like）；空=关闭。
    maimaidx_processing_emoji_id: str = '424'
    # 开启后，绑定、上传和发票前需先同意当前用户协议。
    maimaidx_user_agreement_required: bool = True
    # Koishi 迁移指令只允许读取此目录；相对路径以插件根目录为准。
    maimaidx_koishi_migration_dir: str = 'data/migration'
    # ---------- 统一持久化：sqlite | yaml | mysql ----------
    maimaidx_storage_backend: str = 'sqlite'
    maimaidx_storage_namespace: str = 'default'
    maimaidx_storage_yaml_path: str = 'data/storage/state.yaml'
    maimaidx_storage_mysql_host: str = ''
    maimaidx_storage_mysql_port: int = 3306
    maimaidx_storage_mysql_user: str = ''
    maimaidx_storage_mysql_password: str = ''
    maimaidx_storage_mysql_database: str = ''
    maimaidx_storage_mysql_charset: str = 'utf8mb4'
    maimaidx_storage_mysql_table_prefix: str = 'maimaidx_'
    maimaidx_storage_mysql_ssl: bool = False
    maimaidx_storage_mysql_keep_snapshots: int = 3
    # 仅在工作集发生变化时制作快照；此值是检测间隔，不再代表全量打包频率。
    maimaidx_storage_sync_interval_seconds: int = 900
    maimaidx_storage_include_user_scores: bool = True
    # 最新成绩 API 缓存通常较大且可重建；大型部署可关闭其远端快照。
    maimaidx_storage_include_player_cache: bool = True
    # auto：同一后端沿用本地工作缓存；remote：每次启动强制从后端恢复。
    maimaidx_storage_bootstrap_policy: str = 'auto'
    maimaidx_storage_allow_empty_remote_init: bool = False
    maimaidx_storage_fail_fast: bool = True
    # ---------- 曲目数据源切换（可选） ----------
    # 留空/不设置 = 使用水鱼查分器 API（默认）
    # "dxdata" = 使用本地 dxdata.json 文件，无需网络
    maimaidx_data_source: Optional[str] = None
    # dxdata.json 文件路径（相对于项目根目录或绝对路径），默认 "dxdata.json"
    maimaidx_dxdata_path: Optional[str] = None
    # ---------- 落雪查分器（Lxns）配置 ----------
    lxns_dev_token: Optional[str] = None          # 开发者 Token（查曲库/按QQ查别人b50）
    lx_client_id: Optional[str] = None            # OAuth 应用 Client ID
    lx_client_secret: Optional[str] = None        # OAuth 应用 Client Secret
    # redirect_uri 留空 = 无回调模式（用户在落雪页面直接看到授权码）
    lx_redirect_uri: Optional[str] = None

    # ---------- 猜曲子音频 CDN（Lxns） ----------
    maimaidx_audio_cdn_base: str = 'https://assets2.lxns.net/maimai/music'
    maimaidx_demucs_device: str = 'cpu'

    # 我有多菜：自定义背景图路径（相对 static 或绝对路径），未配置则使用常规 B50 背景 b50_bg.png
    maimaidx_how_weak_bg: Optional[str] = None
    # 底力分析：自定义背景图路径（相对 static 或绝对路径），未配置则使用常规 B50 背景 b50_bg.png
    maimaidx_tag_analysis_bg: Optional[str] = None
    # 我有多菜 / 我在群里有多菜：rating 数据缓存时长（秒），默认 15 分钟；可通过 .env 设置 MAIMAIDX_RATING_CACHE_SECONDS
    maimaidx_rating_cache_seconds: int = 900
    # 玩家成绩（B50 + 全量 dev）SQLite 缓存时长（秒），默认 15 分钟；0=关闭。MAIMAIDX_PLAYER_CACHE_SECONDS
    maimaidx_player_cache_seconds: int = 900
    # 是否允许用「数据存储」最近快照作兜底（默认 24h 内有效）。MAIMAIDX_PLAYER_CACHE_USE_STORAGE
    maimaidx_player_cache_use_storage: bool = True
    maimaidx_player_storage_fallback_seconds: int = 86400
    # 曲库/谱面/别名数据本地缓存时长（秒），默认 1 小时。
    # 启动时若本地缓存文件未过期则直接读取，跳过网络请求，加快启动速度。
    # 设为 0 则每次启动都从网络获取（旧行为）。可通过 .env 设置 MAIMAIDX_MUSIC_CACHE_SECONDS
    maimaidx_music_cache_seconds: int = 3600
    # 友人对战：同一 QQ 两次发起之间的冷却（秒），默认 3 分钟；设为 0 关闭冷却。可通过 .env 设置 MAIMAIDX_FRIEND_BATTLE_COOLDOWN_SECONDS
    maimaidx_friend_battle_cooldown_seconds: int = 180
    # 友人对战读取本地成绩缓存的最长有效期（秒），默认 7 天；重启后仍可用 SQLite/存档，减少重复拉取水鱼。MAIMAIDX_FRIEND_BATTLE_CACHE_SECONDS
    maimaidx_friend_battle_cache_seconds: int = 604800

    # ---------- B50 分析（LLM 锐评） ----------
    b50_llm_url: str = 'https://api.openai.com/v1'
    b50_llm_key: str = ''
    b50_llm_model: str = 'gemini-3-flash-preview'
    b50_assets_path: str = ''

    # ---------- 平台适配（OneBot / 官方 QQ） ----------
    # onebot | qq_official —— MAIMAIDX_PLATFORM
    maimaidx_platform: str = 'onebot'
    # 官方 QQ 下是否尝试卡片形态发图（暂以图片消息为主，后续扩展 Ark）
    maimaidx_use_qq_card: bool = False
    # 插件管理员 platform id（逗号/空格分隔），与 SUPERUSER 等效；官方 QQ 填 openid
    maimaidx_bot_admins: str = ''


maiconfig = get_plugin_config(Config)

# 在其它模块创建 SQLite 连接前恢复所选持久化后端。
try:
    from .libraries.maimaidx_storage import bootstrap_storage
    log.info(f"统一存储：{bootstrap_storage(maiconfig)}")
except Exception as exc:
    message = f"统一存储初始化失败：{type(exc).__name__}: {exc}"
    if bool(getattr(maiconfig, 'maimaidx_storage_fail_fast', True)):
        raise RuntimeError(message) from exc
    log.error(message + "；已按配置允许继续使用现有本地数据")

BOT_QQ_GROUP = str(ATTRIBUTION['group'])
UPSTREAM_REPO_URL = str(ATTRIBUTION['upstream_url'])
FORK_TEAM_URL = str(ATTRIBUTION['fork_url'])
IMAGE_DESIGNER = str(ATTRIBUTION['image_designer'])


def project_attribution_message() -> str:
    """项目地址 / 关于：完整致谢文案。"""
    return project_message()


def footer_generated(bot_name: Optional[str] = None) -> str:
    """图片 / 文本回复底部短署名。"""
    name = bot_name or maiconfig.botName
    return short_footer(name)


def footer_designed_generated(designer: str = IMAGE_DESIGNER, bot_name: Optional[str] = None) -> str:
    return f'Designed by {designer}. {footer_generated(bot_name)}'


def footer_designed_pipe_generated(designer: str, bot_name: Optional[str] = None) -> str:
    return f'Designed by {designer} | {footer_generated(bot_name)}'

# 谱面标签展示配置（后续多处复用，此处统一配置，勿在业务里写死）
TAG_DISPLAY_ORDER: List[str] = ['配置', '难度', '评价']
TAG_PILL_COLORS: Dict[str, Tuple[int, int, int]] = {
    '配置': (173, 216, 230),
    '难度': (200, 162, 220),
    '评价': (255, 182, 193),
}


vote_url: str = 'https://www.yuzuchan.moe/vote'

# ws
UUID = uuid.uuid1()


# echartsjs
SNAPSHOT_JS = (
    "echarts.getInstanceByDom(document.querySelector('div[_echarts_instance_]'))."
    "getDataURL({type: 'PNG', pixelRatio: 2, excludeComponents: ['toolbox']})"
)


# 文件路径
Root: Path = Path(__file__).parent
if maiconfig.maimaidxpath:
    static: Path = Path(maiconfig.maimaidxpath)
else:
    raise ValueError(
        '`nonebot-plugin-maimaidx` 插件未检测到静态文件夹 `static`，'
        '请根据 README 配置页说明进行下载静态文件'
    )
alias_file: Path = static / 'music_alias.json'                  # 别名暂存文件
local_alias_file: Path = static / 'local_music_alias.json'      # 本地别名文件
music_file: Path = static / 'music_data.json'                   # 曲目暂存文件
chart_file: Path = static / 'music_chart.json'                  # 谱面数据暂存文件
guess_file: Path = static / 'group_guess_switch.json'           # 猜歌开关群文件
guess_score_file: Path = static / 'group_guess_score.json'     # 猜歌积分群文件
guess_score_history_file: Path = static / 'group_guess_score_history.json'  # 猜歌积分历史榜
guess_score_events_file: Path = static / 'group_guess_score_events.json'  # 猜歌猜对明细（按模式）
guess_boost_card_file: Path = static / 'group_guess_boost_cards.json'  # 猜歌限时加倍卡
guess_sync_prefs_file: Path = static / 'group_guess_sync_prefs.json'  # 主群↔猜歌群同步偏好
letter_stats_file: Path = static / 'group_letter_stats.json'    # 开字母通关历史与榜单
group_alias_file: Path = static / 'group_alias_switch.json'     # 别名推送开关群文件
group_feature_switch_file: Path = static / 'group_feature_switch.json'  # 功能开关群文件
pie_html_file: Path = static / 'temp_pie.html'                  # 饼图html文件


# 静态资源路径
maimaidir: Path = static / 'mai' / 'pic'
coverdir: Path = static / 'mai' / 'cover'
ratingdir: Path = static / 'mai' / 'rating'
rating_table_dir: Path = static / 'mai' / 'rating_table'
platedir: Path = static / 'mai' / 'plate'
plate_versiondir: Path = static / 'mai' / 'plate_version'
plate_tabledir: Path = static / 'mai' / 'plate_table'


# 字体路径
fontdir: Path = static / 'font'
SIYUAN: Path = fontdir / 'ResourceHanRoundedCN-Bold.ttf'
SHANGGUMONO: Path = fontdir / 'ShangguMonoSC-Regular.otf'
TBFONT: Path = fontdir / 'Torus SemiBold.otf'


# 定义（全插件统一，以下四者互不影响）:
# - 谱面类型: SD = 标准谱面, DX = DX谱面（曲目/成绩的 type 字段）
# - B35 = 前 35 首槽位, B15 = 后 15 首槽位（游戏 B50 排版）
# - 查分器约定: charts.sd 表示 B35，charts.dx 表示 B15（与谱面类型 SD/DX 无关）

# 常用变量
SONGS_PER_PAGE: int = 25
scoreRank: List[str] = ['d', 'c', 'b', 'bb', 'bbb', 'a', 'aa', 'aaa', 's', 's+', 'ss', 'ss+', 'sss', 'sss+']
score_Rank: List[str] = ['d', 'c', 'b', 'bb', 'bbb', 'a', 'aa', 'aaa', 's', 'sp', 'ss', 'ssp', 'sss', 'sssp']
STATISTICS_KEYS: List[str] = [
    'clear', 's', 'sp', 'ss', 'ssp', 'sss', 'sssp',
    'sync', 'fc', 'fcp', 'ap', 'app', 'fs', 'fsp', 'fsd', 'fsdp',
]
COMBO_SP: List[str] = ['fc', 'fcp', 'ap', 'app']
SYNC_D_SP: List[str] = ['fs', 'fsp', 'fsd', 'fsdp']
score_Rank_l: Dict[str, str] = {
    'd': 'D', 
    'c': 'C', 
    'b': 'B', 
    'bb': 'BB', 
    'bbb': 'BBB', 
    'a': 'A', 
    'aa': 'AA', 
    'aaa': 'AAA', 
    's': 'S', 
    'sp': 'Sp', 
    'ss': 'SS', 
    'ssp': 'SSp', 
    'sss': 'SSS', 
    'sssp': 'SSSp'
}
comboRank: List[str] = ['fc', 'fc+', 'ap', 'ap+']
combo_rank: List[str] = ['fc', 'fcp', 'ap', 'app']
syncRank: List[str] = ['fs', 'fs+', 'fdx', 'fdx+']
sync_rank: List[str] = ['fs', 'fsp', 'fsd', 'fsdp']
sync_rank_p: List[str] = ['fs', 'fsp', 'fdx', 'fdxp']
diffs: List[str] = ['Basic', 'Advanced', 'Expert', 'Master', 'Re:Master']
levelList: List[str] = [
    '1', 
    '2', 
    '3', 
    '4', 
    '5', 
    '6', 
    '7', 
    '7+', 
    '8', 
    '8+', 
    '9', 
    '9+', 
    '10', 
    '10+', 
    '11', 
    '11+', 
    '12', 
    '12+', 
    '13', 
    '13+', 
    '14', 
    '14+', 
    '15'
]
achievementList: List[float] = [50.0, 60.0, 70.0, 75.0, 80.0, 90.0, 94.0, 97.0, 98.0, 99.0, 99.5, 100.0, 100.5]
BaseRaSpp: List[float] = [7.0, 8.0, 9.6, 11.2, 12.0, 13.6, 15.2, 16.8, 20.0, 20.3, 20.8, 21.1, 21.6, 22.4]
fcl: Dict[str, str] = {'fc': 'FC', 'fcp': 'FCp', 'ap': 'AP', 'app': 'APp'}
fsl: Dict[str, str] = {'fs': 'FS', 'fsp': 'FSp', 'fsd': 'FSD', 'fdx': 'FSD', 'fsdp': 'FSDp', 'fdxp': 'FSDp', 'sync': 'Sync'}
plate_to_sd_version: Dict[str, str] = {
    '初': 'maimai',
    '真': 'maimai PLUS',
    '超': 'maimai GreeN',
    '檄': 'maimai GreeN PLUS',
    '橙': 'maimai ORANGE',
    '暁': 'maimai ORANGE PLUS',
    '晓': 'maimai ORANGE PLUS',
    '桃': 'maimai PiNK',
    '櫻': 'maimai PiNK PLUS',
    '樱': 'maimai PiNK PLUS',
    '紫': 'maimai MURASAKi',
    '菫': 'maimai MURASAKi PLUS',
    '堇': 'maimai MURASAKi PLUS',
    '白': 'maimai MiLK',
    '雪': 'MiLK PLUS',
    '輝': 'maimai FiNALE',
    '辉': 'maimai FiNALE'
}
plate_to_dx_version: Dict[str, str] = {
    **plate_to_sd_version,
    '熊': 'maimai でらっくす',
    '華': 'maimai でらっくす PLUS',
    '华': 'maimai でらっくす PLUS',
    '爽': 'maimai でらっくす Splash',
    '煌': 'maimai でらっくす Splash PLUS',
    '宙': 'maimai でらっくす UNiVERSE',
    '星': 'maimai でらっくす UNiVERSE PLUS',
    '祭': 'maimai でらっくす FESTiVAL',
    '祝': 'maimai でらっくす FESTiVAL PLUS',
    '双': 'maimai でらっくす BUDDiES',
    '宴': 'maimai でらっくす BUDDiES PLUS',
    '镜': 'maimai でらっくす PRiSM',
    '彩': 'maimai でらっくす PRiSM PLUS',
}

# 未进曲库 / 未上线的 DX 版本（不参与 B15 判定链；仅命令 alias 预留）
_future_dx_versions: Dict[str, str] = {
    '丸': 'maimai でらっくす CiRCLE',
    '圆': 'maimai でらっくす CiRCLE PLUS',
}

_DX_VERSION_PREFIX = 'maimai でらっくす '


def get_latest_plate_versions() -> List[str]:
    """当前 B15 两作完整版本名（plate_to_dx_version 末两作，国服现为镜彩）。"""
    return list(plate_to_dx_version.values())[-2:]


def expand_version_aliases(versions: List[str]) -> List[str]:
    """补充 dxdata 短版本名，供曲库 filter / B15 判定使用。"""
    out: List[str] = []
    seen: set[str] = set()
    for v in versions:
        candidates = [v]
        if _DX_VERSION_PREFIX in v:
            candidates.append(v.replace(_DX_VERSION_PREFIX, ''))
        for name in candidates:
            if name and name not in seen:
                out.append(name)
                seen.add(name)
    return out


def get_b15_version_names() -> List[str]:
    """当前 B15 版本名（plate_to_dx_version 最新一代）。"""
    return get_b15_version_names_at_generation(0)


def get_b15_version_names_at_generation(generation: int = 0) -> List[str]:
    """按世代取 B15 版本名；0=最新一代，1=上一代…"""
    values = list(plate_to_dx_version.values())
    idx = len(values) - 2 - generation * 2
    if idx < 0:
        idx = 0
    return expand_version_aliases(values[idx:idx + 2])


def get_b35_version_names_for_generation(generation: int = 0) -> List[str]:
    """与 B15 同世代对应的 B35 版本名（排除当前 B15 两代）。"""
    values = list(plate_to_dx_version.values())
    cutoff = len(values) - 2 - generation * 2
    if cutoff < 0:
        cutoff = 0
    return expand_version_aliases(values[:cutoff])


def resolve_b15_generation(
    library_versions: set[str],
    *,
    chart_versions: Optional[set[str]] = None,
) -> int:
    """曲库 / 玩家 B15 曲目版本推断 B15 世代；无匹配时回退 gen=0（镜彩）。"""
    values = list(plate_to_dx_version.values())
    max_gen = max((len(values) - 2) // 2, 0)

    if chart_versions:
        for gen in range(max_gen + 1):
            if chart_versions.intersection(get_b15_version_names_at_generation(gen)):
                return gen

    for gen in range(max_gen + 1):
        if library_versions.intersection(get_b15_version_names_at_generation(gen)):
            return gen

    return 0
version_map = {
    '真': ([plate_to_dx_version['真'], plate_to_dx_version['初']], '真'),
    '超': ([plate_to_sd_version['超']], '超'),
    '檄': ([plate_to_sd_version['檄']], '檄'),
    '橙': ([plate_to_sd_version['橙']], '橙'),
    '暁': ([plate_to_sd_version['暁']], '暁'),
    '桃': ([plate_to_sd_version['桃']], '桃'),
    '櫻': ([plate_to_sd_version['櫻']], '櫻'),
    '紫': ([plate_to_sd_version['紫']], '紫'),
    '菫': ([plate_to_sd_version['菫']], '菫'),
    '白': ([plate_to_sd_version['白']], '白'),
    '雪': ([plate_to_sd_version['雪']], '雪'),
    '輝': ([plate_to_sd_version['輝']], '輝'),
    '霸': (list(set(plate_to_sd_version.values())), '舞'),
    '舞': (list(set(plate_to_sd_version.values())), '舞'),
    '熊': ([plate_to_dx_version['熊']], '熊&华'),
    '华': ([plate_to_dx_version['熊']], '熊&华'),
    '華': ([plate_to_dx_version['熊']], '熊&华'),
    '爽': ([plate_to_dx_version['爽']], '爽&煌'),
    '煌': ([plate_to_dx_version['爽']], '爽&煌'),
    '宙': ([plate_to_dx_version['宙']], '宙&星'),
    '星': ([plate_to_dx_version['宙']], '宙&星'),
    '祭': ([plate_to_dx_version['祭']], '祭&祝'),
    '祝': ([plate_to_dx_version['祭']], '祭&祝'),
    '双': ([plate_to_dx_version['双']], '双&宴'),
    '宴': ([plate_to_dx_version['双']], '双&宴'),
    '镜': ([plate_to_dx_version['镜']], '镜'),
    '彩': ([plate_to_dx_version['彩']], '彩'),
    '丸': ([_future_dx_versions['丸']], '丸'),
    '圆': ([_future_dx_versions['圆']], '圆')
}


def resolve_plate_id_list(
    plate_data: Optional[Dict[str, List[int]]],
    plate_key: str,
) -> Optional[List[int]]:
    """
    解析牌子曲目 ID 列表。优先用直接键（如 镜、彩、熊&华）；组合键缺失时回退合并分键。
    """
    if not plate_data:
        return None
    direct = plate_data.get(plate_key)
    if direct:
        return list(direct)
    if '&' not in plate_key:
        return None
    merged: List[int] = []
    seen: set[int] = set()
    for part in plate_key.split('&'):
        part = part.strip()
        if not part:
            continue
        for sid in plate_data.get(part) or []:
            i = int(sid)
            if i in seen:
                continue
            seen.add(i)
            merged.append(i)
    return merged or None


platecn = {
    '晓': '暁',
    '樱': '櫻',
    '堇': '菫',
    '辉': '輝',
    '华': '華'
}
category: Dict[str, str] = {
    '流行&动漫': 'anime',
    '舞萌': 'maimai',
    'niconico & VOCALOID': 'niconico',
    '东方Project': 'touhou',
    '其他游戏': 'game',
    '音击&中二节奏': 'ongeki',
    'POPSアニメ': 'anime',
    'maimai': 'maimai',
    'niconicoボーカロイド': 'niconico',
    '東方Project': 'touhou',
    'ゲームバラエティ': 'game',
    'オンゲキCHUNITHM': 'ongeki',
    '宴会場': '宴会场',
    '宴会场': '宴会场',
}
