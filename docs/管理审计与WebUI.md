# 管理审计与 WebUI

## REF_ID 请求链路

插件命令进入处理器时会创建 `REF-XXXXXXXXXXXXXXXX`，并记录：

- 用户、群组、命令与 Matcher；
- 开始时间、完成时间、总耗时和状态；
- 水鱼、落雪、AWMC API 等外部调用步骤及耗时；
- 脱敏后的异常类型和错误摘要。

不会保存消息正文、二维码、Authorization、Cookie、密码或 Token 原文。管理员可使用：

```text
查询REF REF-XXXXXXXXXXXXXXXX
```

## BREAK 经济

默认签到调整为：

- 基础随机 `1～2` BREAK；
- 连续第 1～5 天额外 `+3/+5/+8/+12/+20`，之后默认每天继续 `+1`，不设上限；
- 指定群 `+25%`、周四 `+50%`、群首签 `+50%`；
- 旧数据库中仍等于旧默认值的配置会自动迁移，管理员自定义值不覆盖。

`今日舞萌` 会把人品值四舍五入至十位后除以 10，每天首次调用发放一次
`0～10` BREAK；例如人品值 69 会获得 7 BREAK。重复调用不会重复发放。

猜歌每次猜对固定奖励 `1` BREAK，不设每日上限。猜歌分数倍率仅影响排行榜，
不会放大 BREAK 奖励。

默认业务计费（只在操作成功后结算）：

| 业务 | 默认价格 | 免费规则 |
|---|---:|---|
| 查分器 API | 1 BREAK | 每日首次实际 API 请求免费 |
| 分析 b50 | 输入每 4000 Token + 输出每 1000 Token 各计 1 BREAK，基础价 ×5 | 最终最低 10、最高 100；调用前预扣 10，成功后多退少补、失败全退；usage 缺失时 20 |
| 上传水鱼 | 2 BREAK | 所有上传方式共享每日首次成功免费 |
| 上传落雪 | 2 BREAK | 所有上传方式共享每日首次成功免费 |
| 同时上传 | 3 BREAK | 所有上传方式共享每日首次成功免费 |
| 发票 | 倍率 × 10 BREAK | 每次成功按价扣费，无每日首免 |
| AWMC 只读查询 | 5 BREAK | `mai预览`、`mai道具` 每次成功查询扣费 |
| 成绩编辑 / 删除 | 75 / 50 BREAK | 每条成功操作扣费，失败不扣费 |
| 清票 / 道具修改 | 10 / 100 BREAK | 道具修改未经测试，用户确认风险后才执行 |
| BREAK 转账 | 手续费 0 | 无免费额度 |
| BREAK 抽奖 | 2 BREAK/次 | 无免费额度 |

因此默认开放的发票价格为：2 倍票 `20`、3 倍票 `30`、5 倍票 `50` BREAK。
允许倍率由 `AWMC_TICKET_ALLOWED_MULTIPLIERS=2,3,5` 配置。水鱼、落雪、
同时上传三种方式共享一次“上传首免”，防止轮流调用刷免费额；发票不享受首免。

管理员仍可使用 `BREAK配置` 修改：

```text
guess_break_per_correct
checkin_base_min
checkin_base_max
streak_bonus
streak_bonus_growth
analysis_input_tokens_per_break
analysis_output_tokens_per_break
analysis_min_cost
analysis_max_cost
analysis_fallback_cost
analysis_price_multiplier
analysis_precharge_cost
upload_fish_cost
upload_lx_cost
upload_all_cost
ticket_cost_per_multiplier
transfer_fee
lottery_cost
cache_query_cost
weekly_report_cost
monthly_report_cost
annual_report_cost
daily_report_cost
coop_b50_cost
```

## 用户封禁

```text
封禁用户 @用户 24 滥用接口
封禁用户 @用户 0 永久封禁
解封用户 @用户
封禁列表
```

封禁只拦截本插件功能，不影响同一 NoneBot 实例中的其它插件。官方 QQ 会同时检查
平台 openid 和 QueryBot/BREAK 使用的结算 ID。

## 用户协议

默认使用 maiBot v4 协议链接 `https://wiki.awmc.team/guide/bot/terms` 与网页确认词。
用户发送“用户协议”获取链接，
阅读后必须完整发送网页中的确认词；Bot 不会把确认词直接显示在群里。
协议链接、确认词和版本可在 WebUI 修改。修改确认词时若未手动改版本，
系统会自动生成新版本，使旧的同意记录失效。

## WebUI

配置：

```env
MAIMAIDX_ADMIN_WEB_ENABLED=true
MAIMAIDX_ADMIN_WEB_TOKEN=至少24位高强度随机字符串
MAIMAIDX_ADMIN_WEB_HOST=127.0.0.1
MAIMAIDX_ADMIN_WEB_PORT=8099
MAIMAIDX_ADMIN_WEB_PATH=/maimaidx/admin
MAIMAIDX_ADMIN_WEB_PUBLIC_URL=https://bot.example.com
MAIMAIDX_AUDIT_RETENTION_DAYS=90
MAIMAIDX_MESSAGE_STATS_ENABLED=true
```

WebUI 默认独立监听 `127.0.0.1:8099`，不要求 Bot 使用 FastAPI Driver；设置
`MAIMAIDX_ADMIN_WEB_PORT=0` 后才会使用 NoneBot FastAPI Driver 的共享端口。WebUI 提供：

- 用户绑定摘要、BREAK、签到连续天数和封禁状态；
- BREAK 设置/增减、封禁/解封；
- REF_ID 列表、错误、步骤和耗时；
- 命令调用量、成功率与平均耗时；
- 群活跃用户、消息排行；
- 群功能启用/禁用；
- BREAK 奖励参数管理，以及每日产出、消耗、查询量与分析量报表。
- 包含免费调用在内的 BREAK 流水，可查用户、原因、变动和时间。
- 用户协议链接、网页确认词和版本。

请求链路和群消息日统计会在 Bot 启动时按保留天数清理；封禁记录不会被自动删除。

WebUI 不提供任意命令执行接口。管理动作使用独立、可审计的 API，避免伪造聊天事件或
绕过 Matcher 权限。建议只通过 HTTPS 或反向代理内网访问，不要把管理 Token 放入 URL。
