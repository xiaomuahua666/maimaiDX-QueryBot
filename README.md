# maimaiDX QueryBot

[![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![NoneBot2](https://img.shields.io/badge/NoneBot2-2.x-EA5252)](https://nonebot.dev/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> **衍生项目声明：本项目基于 [Yuri-YuzuChaN/maimaiDX](https://github.com/Yuri-YuzuChaN/maimaiDX) 修改。**

面向 NoneBot2 的舞萌 DX 查询与账号服务插件，由 [AWMC TEAM](https://github.com/AWMC-TEAM) 维护。原项目版权与贡献归原作者及其贡献者所有，详细说明见 [NOTICE](NOTICE)。

## 功能特性

- **成绩查询**：b50 / b40、定数查询、谱面详情、个人成绩
- **难度 / 版本筛选**：按难度（紫 / 13+ 等）或版本（镜代 / 爽代等）筛选 b50
- **历代版本 b50 / b35**：使用指定版本定数重算 rating
- **定数表 / 完成表**：等级定数表、等级完成表、牌子完成表（晓极完成表等）
- **进度与推荐**：牌子进度、等级进度、吃分推荐、弱项处方单、目标 Rating 沙盘、B50 风险预警、周报 / 月报 / 日报
- **数据存储**：开启本地成绩快照，支持存档查询与进步报告
- **群功能**：我在群里有多菜、群 rating 排行、群单曲排行、友人对战（含段位 CP）、对战战绩 Head-to-Head
- **PC 数系统**：机台登录、曲目 PC 数统计、PC 数排行榜
- **AWMCNET 默认数据源**：首次查分自动合并水鱼/落雪可用成绩并迁移到 AWMCNET
- **查分器上传**：二维码始终写入 AWMCNET；已绑定水鱼/落雪时额外同步对应平台
- **统一账号**：原 maibot 的账号绑定、Token、上传、票券与状态功能已合并，无需单独运行 Koishi Bot
- **管理审计**：统一 REF_ID 请求链路、敏感信息脱敏、用户封禁与内置管理 WebUI
- **倍率票 / 道具**：获取倍率票、查询票券、添加收藏品
- **谱面标签 / 印象**：dxrating 谱面标签、谱面印象 API
- **数据源切换**：水鱼 API 或本地 `dxdata.json`
- **双 Bot 模式**：保留 OneBot 与腾讯官方 QQ Bot 两种模式；官方 QQ 的加密 openid
  通过论坛 OAuth 或管理员绑定映射到原 QQ 号，群级猜歌数据也可迁移。

## 安装

```bash
pip install nonebot-plugin-maimaidx
```

从当前仓库安装开发版：

```bash
git clone https://github.com/AWMC-TEAM/maimaiDX-QueryBot.git
cd maimaiDX-QueryBot
pip install -e .
```

开发版可将本仓库文件覆盖到 bot 的插件目录。另需安装 Playwright Chromium 与 ffmpeg（猜铺面录视频）：

```bash
playwright install --with-deps chromium
# macOS: brew install ffmpeg
```

猜铺面依赖本地谱面预览页（基于 [Maimai-Chart-Preview](https://github.com/Pimeng/Maimai-Chart-Preview)，已去掉音乐与背景）。若缺少 `static/chart_preview/`，执行：

```bash
./scripts/build_chart_preview.sh
```

## 静态资源

1. 下载并解压静态资源包，得到 `static` 文件夹（含 `mai/`、`font/` 等）。
2. 在 `.env` 中配置 `MAIMAIDXPATH` 指向该目录的**绝对路径**。

| 文件 / 目录 | 说明 |
|-------------|------|
| `static/` | 插件静态资源根目录（**必须**） |
| `static/font/` | 字体（`ResourceHanRoundedCN-Bold.ttf` 等） |
| `static/chart_preview/` | 猜铺面录制页（可由 `scripts/build_chart_preview.sh` 生成） |
| `dxdata.json` | 本地曲库数据源（可选，见下方配置） |

首次部署后，若 `rating` / `plate` 目录为空，需**私聊 Bot（超级用户）**执行：

- `更新定数表`
- `更新完成表`

否则「定数表」「完成表」类指令可能无法使用。

## 配置

配置写入 bot 根目录的 `.env` 或 `.env.prod`。NoneBot 会将 `config.py` 中的字段名转为**大写环境变量**注入（例如 `maimaidxpath` → `MAIMAIDXPATH`）。

### 必填

```env
# 静态资源目录（绝对路径）
MAIMAIDXPATH=/path/to/static
```

### 查分器 Token（水鱼开发者 Token）

用于 dev 接口、完成表、友人对战、数据存储等。**支持配置多个 Token**，用逗号或空格分隔；请求失败（token 有误 / 被禁用）时自动切换下一个，全部失败才报错。

```env
# 单个
MAIMAIDXTOKEN=your_token_here

# 多个（推荐，提高并发与容错）
MAIMAIDXTOKEN=token_a,token_b,token_c
```

### AWMC API（PC 数 / 上传 / 倍率票）

支持通过 `AWMC_API_MODE` 在 **自建 sw-api** 与 **AWMC 公共网关** 间切换。

#### team（自建）

```env
AWMC_API_MODE=team
AWMC_API_BASE_URL=http://127.0.0.1:5001
SDGBT_CLIENT_ID=your_keychip
```

路径前缀：`/awmc/api/v1/...`，请求体需带 `qrcode` + `keychip`。

#### public（公共网关）

平台：https://api.wmc.pub · 文档：https://wiki.awmc.team/dev/awmc-api

用 AWMC 通行证登录控制台，在个人中心生成 `gw_` 令牌；额度通过卡密兑换充入。

```env
AWMC_API_MODE=public
# 可留空，默认 https://api.wmc.pub
AWMC_API_BASE_URL=https://api.wmc.pub
AWMC_PUBLIC_GATEWAY_TOKEN=gw_xxx
# public 模式无需 SDGBT_CLIENT_ID（keychip 由网关注入）
```

路径前缀：`/v1/...`，请求头：`Authorization: Bearer <令牌>`，JSON Body 只传业务参数（如 `qrcode`）。健康检查成功为 `returnCode === 0`，其余业务成功为 `returnCode === 1`；兼容字段同时返回 `code === 0`（余额不足返回 403）。

| 接口 | 消耗 |
|------|------|
| `GET /v1/health` | 0 |
| `POST /v1/user/data` | 1 |
| `POST /v1/user/region` | 1 |
| `POST /v1/user/music` | 2 |
| `POST /v1/user/charge` | 1 |
| `POST /v1/charge` | 10 |
| `POST /v1/update-lx` | 5 |
| `POST /v1/update-fish` | 5 |

#### 共用调优项

```env
# 机台会话全局串行，AWMC API 成功后静默冷却 1 秒。
AWMC_MACHINE_LOCK_TIMEOUT_SECONDS=120
AWMC_API_SUCCESS_COOLDOWN_SECONDS=1
# 发票允许倍率，当前仅 2、3 倍。
AWMC_TICKET_ALLOWED_MULTIPLIERS=2,3
# AWMC v2 发票为同步接口；首次预计 80 秒，后续根据近期真实耗时动态估算。
AWMC_TICKET_TIMEOUT_SECONDS=120
AWMC_TICKET_ESTIMATE_SECONDS=80
# 同步接口成功后只查一次票券库存，确认到账后才扣 BREAK。
AWMC_TICKET_SETTLEMENT_DELAY_SECONDS=2
# mais：全局失败率分类数据（默认即此地址）
AWMC_FAILURE_RATE_URL=https://api.wmc.pub/usage/failure-rate
# 高负载计费：60 秒前 30 个真实功能请求免费，第 31 个起加收 1 BREAK。
MAIMAIDX_BUSY_SURCHARGE_ENABLED=true
MAIMAIDX_BUSY_WINDOW_SECONDS=60
MAIMAIDX_BUSY_FREE_REQUESTS=30
MAIMAIDX_BUSY_SURCHARGE_BREAK=1
```

旧变量 `SDGBTECHAPI` 仍兼容。完整模板见仓库根目录 `.env.example`。

### 落雪查分器（可选）

```env
LXNS_DEV_TOKEN=your_lxns_dev_token
LX_CLIENT_ID=your_oauth_client_id
LX_CLIENT_SECRET=your_oauth_client_secret
# 留空 = 无回调模式（用户在落雪页面直接看到授权码）
LX_REDIRECT_URI=
```

### AWMCNET（默认数据源）

AWMC NET. 默认服务地址为 `https://net.wmc.pub`（可用 `AWMCNET_SYNC_URL`
覆盖，正常部署无需写入环境文件）。未绑定水鱼或落雪的用户发送
舞萌二维码后也会自动建立 AWMCNET 档案；外部平台绑定不再是上传前置条件。

```env
AWMCNET_BOT_TOKEN=replace_with_a_shared_random_secret
```

AWMCNET 服务端配置相同密钥：

```env
AWWC_BOT_TOKENS=replace_with_a_shared_random_secret
```

### 官方 QQ Bot 与论坛绑定

官方 QQ 的用户、消息和群 ID 是加密 openid，不能直接当作水鱼查分 QQ 号使用。插件同时
支持普通 OneBot 和官方 QQ 两种模式，切换后重启 Bot：

```env
# onebot（默认）或 qq_official
MAIMAIDX_PLATFORM=qq_official
# 官方 QQ adapter 由插件依赖自动安装；NoneBot 项目还需配置 driver 与 QQ_BOTS：
# DRIVER=~fastapi+~httpx+~websockets
# QQ_BOTS='[{"id":"AppID","token":"Token","secret":"AppSecret","intent":{"c2c_group_at_messages":true,"at_messages":true},"use_websocket":true}]'

# XenForo ThemeHouse/Audentio OAuth，论坛站点固定为 bbs.wmc.pub
AWMC_XF_BASE_URL=https://bbs.wmc.pub
AWMC_XF_CLIENT_ID=your_oauth_client_id
AWMC_XF_CLIENT_SECRET=your_oauth_client_secret
# 必须与论坛 OAuth 应用登记值一致。bot 绑定需要一个能把 code 保留在地址栏的回调页；
# 不要直接使用会在服务端消费 code 的网页登录回调，除非用户能拿到原始 code。
AWMC_XF_REDIRECT_URI=https://genshin.wmc.pub/
AWMC_XF_AUTHORIZE_PATH=/audapi/oauth2/authorize
AWMC_XF_TOKEN_URL=https://bbs.wmc.pub/api/audapi-oauth2/token
```

用户在官方 QQ 中发送 `qbind` / `论坛绑定`，打开链接登录 `https://bbs.wmc.pub`，再把
完整回调链接或授权码发回（`qbind <链接>`，或已发起绑定后直接粘贴链接/授权码）。
插件会读取论坛用户 ID、昵称和邮箱；邮箱符合 `数字@qq.com` 时自动绑定对应查分 QQ。

仓库提供回调页 `static/qbind_callback.html`：把它挂到任意静态站点后，把该 URL 登记为
OAuth 应用的 redirect_uri，并设置 `AWMC_XF_REDIRECT_URI` 与之完全一致。授权成功后页面会
展示「qbind + 完整链接」，一键复制即可发给机器人。若暂不托管该页，也可继续用
`https://bbs.wmc.pub/`，从地址栏复制带 `code=` 的 URL。

管理员/群管理员命令：

- `强制绑定QQ @用户 <QQ号>`：为当前平台用户绑定查分 QQ；官方 QQ 也可直接填写加密平台用户 ID。
- `群绑定QQ <旧QQ群号>`：把当前官方 QQ 群 openid 映射到旧 QQ 群号，恢复群级猜歌积分、加倍卡等数据。
- `解绑群QQ`、`群绑定状态`：管理或查看群映射。
- `猜歌预制状态`（别名 `猜谱面预制状态`）：查看热门池音频、谱面视频的完整/部分/未预制数量及后台任务。

如果要继续使用普通 Bot，只需将 `MAIMAIDX_PLATFORM` 改回 `onebot`；普通模式下消息 QQ
号仍直接作为查分 QQ，不需要 `qbind`。

### 谱面标签（dxrating，可选）

未配置时谱面详情不显示 dxrating 标签。

```env
MAIMAIDX_DXRATING_TOKEN=your_dxrating_token
# 可选：自定义 combined-tags 接口地址
# MAIMAIDX_DXRATING_COMBINED_TAGS_URL=https://derrakuma.dxrating.net/functions/v1/combined-tags
```

### 谱面印象 API（可选）

```env
PMYX_API_BASE_URL=http://103.45.162.66:37913
```

### 数据源切换（可选）

```env
# 留空 = 使用水鱼查分器 API（默认）
# 设为 dxdata = 使用本地 dxdata.json，无需拉取曲库网络接口
MAIMAIDX_DATA_SOURCE=dxdata
MAIMAIDX_DXDATA_PATH=dxdata.json
```

### 缓存与限流（可选）

```env
# 我有多菜 / 群 rating 等缓存时长（秒），默认 900（15 分钟）
MAIMAIDX_RATING_CACHE_SECONDS=900

# 曲库 / 谱面 / 别名启动缓存（秒），默认 3600；设为 0 则每次启动都拉网络
MAIMAIDX_MUSIC_CACHE_SECONDS=3600

# 友人对战冷却（秒），默认 180（3 分钟）；设为 0 关闭
MAIMAIDX_FRIEND_BATTLE_COOLDOWN_SECONDS=180
```

### 管理 WebUI（可选）

```env
MAIMAIDX_ADMIN_WEB_ENABLED=true
MAIMAIDX_ADMIN_WEB_TOKEN=至少24位高强度随机字符串
MAIMAIDX_ADMIN_WEB_HOST=127.0.0.1
MAIMAIDX_ADMIN_WEB_PORT=8099
MAIMAIDX_ADMIN_WEB_PATH=/maimaidx/admin
MAIMAIDX_ADMIN_WEB_PUBLIC_URL=https://bot.example.com
MAIMAIDX_MESSAGE_STATS_ENABLED=true
MAIMAIDX_COMPACT_MESSAGES=true
MAIMAIDX_KOISHI_MIGRATION_DIR=data/migration
```

WebUI 默认独立监听 `127.0.0.1:8099`，可以直接用 Nginx/Caddy 反向代理；设
`MAIMAIDX_ADMIN_WEB_PORT=0` 时才挂载到 NoneBot FastAPI Driver 的共享端口。
API 强制使用 Bearer Token，页面不会返回二维码、水鱼/落雪 Token 等原文。
管理员可在 Bot 内发送 `管理面板` 查看地址。
完整部署与安全说明见 `docs/WebUI配置说明.md`。

`MAIMAIDX_COMPACT_MESSAGES=true` 默认合并猜歌开场/结算摘要、凭据撤回警告
与业务结果，并省略非必要的“处理中”消息，以降低平台消息发送频率；排行榜统一渲染为图片。
二维码补交、
猜歌阶段提示等需要用户继续交互的消息不会被省略。

### SQLite / YAML / MySQL 统一存储

默认继续使用原生 SQLite/JSON。迁移到 MySQL 时先填写
`MAIMAIDX_STORAGE_MYSQL_*`，再由超级管理员执行：

```text
存储迁移 检查 sqlite mysql
存储迁移 确认 sqlite mysql
```

成功后设置 `MAIMAIDX_STORAGE_BACKEND=mysql` 并重启。YAML 与反向迁移同样支持，
每次迁移都会校验逐文件及总体 SHA-256。详见 `docs/统一存储与迁移说明.md`；
本轮全部新增变量见 `docs/今日新增ENV配置.md`。

### 代理与其它（可选）

```env
# 查分器 / 别名库走代理（默认 false）
MAIMAIDXPROBERPROXY=false
MAIMAIDXALIASPROXY=false

# 图片页脚 Bot 名称（默认取 nonebot nickname）
BOTNAME=maimai

# 自定义背景图路径（相对 static 或绝对路径）
# MAIMAIDX_HOW_WEAK_BG=mai/pic/custom_weak_bg.png
# MAIMAIDX_TAG_ANALYSIS_BG=mai/pic/custom_tag_bg.png
```

### 页脚完整性保护

项目来源、维护团队、QQ群和固定署名保存在 RSA-SHA256 签名清单中，插件导入时会验证签名；清单被直接改动时会拒绝启动。`BOTNAME` 仍是公开配置项，可按部署实例自定义。

签名私钥不进入仓库。维护者更新固定署名时，应使用离线保存的私钥重新签名。由于本项目是开源软件，任何人都能删除校验代码并建立分支，因此该机制用于检测未经授权的原地篡改，不能从技术上禁止第三方修改其自行维护的副本。

## 命令列表

### 基础查询

| 命令 | 说明 |
|------|------|
| `b50` / `b40` | 查询 b50 / b40 |
| `id <歌曲id>` | 查询歌曲详情 |
| `<歌曲别名>是什么歌` | 通过别名查歌曲 |
| `帮助maimaiDX` | 查看帮助 |

### 难度 / 版本筛选

| 命令 | 说明 |
|------|------|
| `紫b50` / `白b50` / `三星b50` / `四星b50` / `13+b50` / `14.0b50` | 按谱面难度、DX 星数或定数筛选 b50；紫=Master，白=Re:MASTER |
| `镜代b50` / `爽代b50` | 按版本筛选 b50 |
| `l镜代b50` / `l爽代b35` | 历代版本 b50 / b35 |
| `dx2025b50` | 读取 2026-06-09 本地存档，PRiSM 定数重算，分 B35/B15 展示 2025 版 Rating |

### 定数表 / 完成表 / 进度

| 命令 | 说明 |
|------|------|
| `13+定数表` | 查看等级定数表 |
| `13+完成表` / `13+ap完成表` | 查看等级完成表 |
| `晓极完成表` | 查看牌子完成表（版本 + 极 / 将 / 舞舞等） |
| `晓极进度` | 牌子进度 |
| `13+sss进度` | 等级进度 |
| `段位表` / `段位表 真二段` | 查看段位认定列表或指定课题；详情含 LIFE、曲目难度、个人最佳与服务器近期匿名样本（自动更新） |
| `更新定数表` / `更新完成表` | 生成静态表图（超级用户，私聊） |

### 数据存储与报告

| 命令 | 说明 |
|------|------|
| `开启数据存储` | 开启本地成绩快照 |
| `立即存储数据` | 手动拉取并存档 |
| `周报` / `月报` / `日报` | 进步报告 |
| `今日吃分推荐` | 个性化推分推荐 |
| `弱项处方` | 根据 B50 底力短板标签推荐练习曲目 |
| `目标rating 16000` | 推分沙盘：估算达到目标 Rating 的改动方案 |
| `b50风险` | B50 风险预警（需开启数据存储） |

### 群功能

| 命令 | 说明 |
|------|------|
| `我有多菜` / `我在群里有多菜` | rating 对比 |
| `友人对战` | 群友随机对战（可选 `友人对战 300` 收紧 rating 差） |
| `对战战绩@某人` | Head-to-Head 重叠曲目胜率对比图 |
| `底力分析` | 谱面标签底力分析 |

### 群小游戏

| 命令 | 说明 |
|------|------|
| `猜Rating` / `猜Rating1`～`猜Rating4` | 看匿名 B50 猜总 Rating；省略难度时随机，题主不参与作答与奖励 |
| `B50找内鬼` / `找假卡` | 从 5 张 B50 卡片中找出单曲 RA 被篡改的一张 |
| `重置猜Rating` / `重置找内鬼` | 结束对应的当前对局 |
| `我的猜歌` | 查看包含找内鬼在内的各玩法个人积分统计图 |

### PC 数系统

| 命令 | 说明 |
|------|------|
| `更新pc数 <二维码>` | 机台登录并更新 PC 数 |
| `我的pc数` | 查看个人 PC 数统计 |
| `pc排行` | 全部用户 PC 数排行榜 |
| `pc50` / `pca50` | B50 内按 PC 排序 |
| `游玩排行50` | 游玩最多的 50 首谱面 |

### 查分器上传

| 命令 | 说明 |
|------|------|
| `dfbind <token>` | 绑定水鱼查分器 |
| `lxbind` | 绑定落雪查分器 |
| `上传水鱼 <二维码>` | 上传 b50 到水鱼 |
| `上传落雪 <二维码>` | 上传 b50 到落雪 |
| `数据源 落雪` | 切换个人数据源 |

### 统一账号（原 maibot）

| 命令 | 说明 |
|------|------|
| `mai账号` | 查看账号功能帮助 |
| `mai绑定` / `maibind` / `mai解绑` | 绑定、认领或解绑舞萌账号 |
| `mai状态` / `mymai` | 查看详细账号状态；SGID 缓存失效时交互刷新 |
| `mai绑定水鱼 [token]` / `maibindfish [token]` | 绑定水鱼上传 Token；无参数时提供获取链接并交互等待，最多重试 3 次 |
| `lxbind` | 绑定落雪 OAuth，上传无需导入 Token（推荐） |
| `mai绑定落雪 <导入token>` / `maibindlx <导入token>` | 绑定落雪导入 Token（兼容） |
| `maiu` / `导` | 仅上传水鱼 |
| `maiul` | 仅上传落雪 |
| `maiua` | 同时上传水鱼与落雪 |
| `发票` / `fp <2/3>` / `mai查票` | 票券操作（默认倍率 × 10 BREAK；3 倍票消耗 30 BREAK） |
| `mai地图` / `maiping` | 游玩地区 / API 健康检查 |
| `mai预览` / `预览`；`mai道具` / `道具` | 查询账号预览 / 道具列表（成功查询每次 5 BREAK） |
| `mai门状态` / `查门` / `门状态` | 查询 Kaleidx Gate 发现、钥匙与通关状态（成功 5 BREAK） |
| `mai改成绩` / `改分 [歌曲 难度 达成率 DX分 FC FS]` | 交互或一步编辑单条成绩（成功 75 BREAK） |
| `mai删成绩` / `删分 [歌曲 难度]` | 交互或一步删除单条成绩（成功 50 BREAK） |
| `mai清票` / `清票` | 交互确认后清空 Charge 票券（成功 10 BREAK） |
| `mai改道具` / `改道具 [itemKind itemId add/del]` | 高风险交互式道具修改（成功 100 BREAK；未经测试，风险自负） |
| `舞萌状态` / `mais` | AWMC 全局失败率分类图与服务器实时状态（空时段、空分类省略） |
| `迁移Koishi 检查/确认 <数据库>` | 超级管理员预检/导入 Koishi maiBot 数据 |

绑定后执行 `更新pc数` 会直接使用已保存账号，不再要求重复发送二维码。
落雪上传优先复用 `lxbind` OAuth；仅未授权 OAuth 时才需要落雪导入 Token。

### AWMCNET 自动同步与认领

配置 `AWMCNET_BOT_TOKEN` 后，用户通过 QQ 查询 B50 时会优先读取 AWMC NET.；
只有 AWMC NET. 尚无账号/成绩或用户执行「刷新b50」时，Bot 才探测水鱼和落雪，
并把可用的全量成绩镜像到 AWMC NET.。尚未注册的 QQ 会创建
不可公开的临时玩家；以后使用对应的 `QQ号@qq.com` 作为 AWMC 论坛邮箱登录时，
系统会自动认领临时玩家及其全部成绩。同步不会发送 SGWCMAID、街机 UID、水鱼
Token 或落雪 Token，AWMCNET 暂时不可用也不会影响原有 Bot 查分。

直接发送以 `SGWCMAID` 开头的凭据会自动绑定并同步 AWMC NET.。首次同步成功
只提示一次注册地址；水鱼、落雪是可选附加平台，分别通过 `maibindfish`、
`maibindlx` 绑定。发送 `成绩趋势 [天数]`（默认 30 天）可读取 AWMC NET.
按日记录的 Rating、B35/B15 和谱面数量趋势。

直接发送 `SGWCMAID...`、舞萌官方二维码链接或含二维码的图片时，Bot 会自动识别并先尝试撤回敏感消息。
没有账号记录时自动验真绑定，已有记录时更新凭据；随后同步 PC，并按用户已绑定的水鱼 Token / 落雪
OAuth（或兼容 Token）自动上传：只绑定一边就上传一边，两边都绑定则全部上传。识别成功后会立即提示
撤回状态与根据近期同类流程平均值计算的预计处理时间。普通图片和非舞萌二维码会静默忽略。账号与 BREAK 功能首次使用前需发送
`用户协议`，阅读链接并完整输入网页确认词。

Koishi 数据迁移时，将完整数据库放进 `MAIMAIDX_KOISHI_MIGRATION_DIR`，先执行
`迁移Koishi 检查 koishi.db`，核对后再执行 `迁移Koishi 确认 koishi.db`。源库只读，
迁移器只导入 maiBot 账号/Token/协议数据，并可通过 Koishi `binding` 表自动映射 QQ。

### 倍率票 / 道具

| 命令 | 说明 |
|------|------|
| `发票` / `fp <倍率>` | 获取倍率票（当前允许 2x、3x） |
| `查票 <二维码>` | 查询票券状态 |

## 开发

提交改动前建议运行：

```bash
python scripts/test_footer_integrity.py
python -m compileall -q .
```

参与方式与提交约定见 [CONTRIBUTING.md](CONTRIBUTING.md)，安全问题请参阅 [SECURITY.md](SECURITY.md)。

### 目录结构

```
nonebot_plugin_maimaidx/
├── command/              # 命令注册与处理
│   ├── mai_score.py      # b50、数据存储、友人对战等
│   ├── mai_table.py      # 定数表 / 完成表 / 进度
│   ├── mai_playcount.py  # PC 数 / SDGB / 倍率票
│   └── ...
├── libraries/            # 核心库
│   ├── maimaidx_api_data.py      # 查分器 API（含 token 池）
│   ├── maimaidx_best_50.py       # b50 绘图
│   ├── maimaidx_friend_battle.py # 友人对战
│   ├── maimaidx_sw_api.py        # sw-api 客户端
│   ├── maimaidx_sdgb_prober.py   # sw-api 上传/拿票封装
│   └── ...
├── data/                 # 运行时数据（快照、段位 CP 等）
└── config.py             # 配置定义
```

完整环境变量以 `config.py` 中 `Config` 类为准。

## 致谢

- **本项目基于修改的原项目：[Yuri-YuzuChaN/maimaiDX](https://github.com/Yuri-YuzuChaN/maimaiDX)**
- 维护：[AWMC TEAM](https://github.com/AWMC-TEAM/maimaiDX-QueryBot)
- 数据源：[Diving-Fish/maimaidx-prober](https://github.com/Diving-Fish/maimaidx-prober)
- 定数数据：dxdata.json 社区维护版本
