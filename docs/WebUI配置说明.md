# QueryBot 管理 WebUI 配置说明

## 1. 功能与运行方式

WebUI 由 QueryBot 进程自动启动，默认独立监听 `127.0.0.1:8099`，无需单独运行
前端命令，也不要求 Bot 使用 FastAPI Driver。适合将该端口直接交给 Nginx/Caddy
反向代理。若确实希望与 NoneBot FastAPI Driver 共用端口，可将端口设为 `0`。

WebUI 页面不加载第三方脚本。所有 JSON API 都必须带：

```http
Authorization: Bearer <MAIMAIDX_ADMIN_WEB_TOKEN>
```

二维码、水鱼/落雪 Token 和完整街机 UID 不会返回到浏览器。

## 2. 最小可用配置

在 Bot 根目录的 `.env` 或实际使用的环境变量文件中设置：

```env
MAIMAIDX_ADMIN_WEB_ENABLED=true
MAIMAIDX_ADMIN_WEB_TOKEN=replace_with_at_least_24_random_characters
MAIMAIDX_ADMIN_WEB_HOST=127.0.0.1
MAIMAIDX_ADMIN_WEB_PORT=8099
MAIMAIDX_ADMIN_WEB_PATH=/maimaidx/admin
MAIMAIDX_ADMIN_WEB_PUBLIC_URL=https://bot.example.com
MAIMAIDX_AUDIT_RETENTION_DAYS=90
MAIMAIDX_MESSAGE_STATS_ENABLED=true
```

修改环境变量后需要重启 Bot。

### 生成管理 Token

推荐使用至少 32 字节的随机值：

```bash
openssl rand -hex 32
```

不要使用 QQ 号、群号、生日、固定英文单词或与 Bot Token 相同的值。
WebUI 拒绝长度少于 24 位的 Token，API 会返回 HTTP 503。

## 3. 配置项详解

### `MAIMAIDX_ADMIN_WEB_ENABLED`

- `false`：默认，不注册 WebUI 路由。
- `true`：启动时注册页面和 API，并按端口配置启动服务。

### `MAIMAIDX_ADMIN_WEB_TOKEN`

管理 API 的 Bearer Token，至少 24 位。页面会将管理员输入的 Token 保存在当前
浏览器的 `localStorage`，因此不要在公共电脑上使用；使用完可清理站点数据。

### `MAIMAIDX_ADMIN_WEB_PATH`

WebUI 路径，默认 `/maimaidx/admin`。必须是应用中未被占用的路径。
例如设为 `/ops/maimai`，则页面为：

```text
https://bot.example.com/ops/maimai
```

API 自动位于：

```text
https://bot.example.com/ops/maimai/api/...
```

### `MAIMAIDX_ADMIN_WEB_HOST`

独立 WebUI 的监听地址，默认 `127.0.0.1`。同机 Nginx/Caddy 反代时保持默认最安全。
只有 Docker 跨容器访问等确有需要时才使用 `0.0.0.0`，并应配合防火墙限制访问。

### `MAIMAIDX_ADMIN_WEB_PORT`

独立 WebUI 的监听端口，默认 `8099`。可换成任意未占用的 `1～65535` 端口。
设为 `0` 时不启动独立监听，而是挂载到 NoneBot FastAPI Driver 的共享应用；这种
兼容模式才要求 Bot 使用 FastAPI Driver。

### `MAIMAIDX_ADMIN_WEB_PUBLIC_URL`

仅用于“管理面板”指令向管理员展示反代后的公网完整地址，不改变实际监听地址。
不要在末尾填页面路径，也不建议保留末尾 `/`。

### `MAIMAIDX_AUDIT_RETENTION_DAYS`

默认 `90`。Bot 启动时清理超期的 REF_ID 请求链路和群消息日统计。
用户封禁记录、账号绑定和 BREAK 流水不使用这个自动清理周期。

### `MAIMAIDX_MESSAGE_STATS_ENABLED`

- `true`：统计各群成员消息数，用于活跃用户和消息排行。
- `false`：不记录新的群消息计数。

只保存用户 ID、群 ID、日期和数量，不保存聊天正文。

## 4. 访问流程

1. 重启 Bot，确认日志出现 `管理 WebUI 正在监听 http://127.0.0.1:8099`。
2. 超级管理员可在 Bot 中发送 `管理面板` 查看配置地址。
3. 浏览器打开 WebUI。
4. 输入 `MAIMAIDX_ADMIN_WEB_TOKEN` 的完整值。
5. 点击“保存并刷新”。

页面本身可以被打开，但没有正确 Token 时所有数据 API 都返回 HTTP 401。

## 5. 页面功能

### 概览

- 近 24 小时 REF 请求数和错误数；
- 封禁数、群数、BREAK 用户数和账号绑定数；
- 近 7 日命令调用量、成功量、错误量和平均耗时。

### 用户

- 按用户 ID、玩家名或街机 UID 搜索；
- 查看脱敏绑定状态、Rating、Token 是否存在、BREAK 和签到连续天数；
- 设置/增减 BREAK；
- 封禁、定时封禁和解封。

### REF 链路

- 按 REF_ID、用户或命令搜索；
- 查看命令入口、处理状态、总耗时、外部 API 步骤和脱敏异常；
- 查看 WebUI 管理修改产生的 REF_ID。

### 群组与消息排行

- 近 30 日群消息数和活跃用户数；
- 按群开启/关闭 QueryBot 功能；
- 查看近 7 日群消息排行。

### BREAK 报表

可在线修改以下配置：

| Key | 默认 | 说明 |
|---|---:|---|
| `checkin_base_min` | 1 | 签到随机下限 |
| `checkin_base_max` | 2 | 签到随机上限 |
| `streak_bonus` | `3,5,8,12,20` | 连续签到前几天的额外值 |
| `streak_bonus_growth` | `1` | 超过上述天数后每天继续增加的值；不设总上限 |
| `bonus_group_1072033605` | 0.25 | 群 1072033605、993795066 的共同签到加成 |
| `bonus_thursday` | 0.5 | 周四加成 |
| `bonus_group_first` | 0.5 | 群首签加成 |
| `query_cost` | 1 | 查分 API 非首免价格 |
| `analysis_input_tokens_per_break` | 4000 | 锐评基础价每 1 BREAK 对应的输入 Token |
| `analysis_output_tokens_per_break` | 1000 | 锐评基础价每 1 BREAK 对应的输出 Token |
| `analysis_min_cost` | 2 | 倍率应用前的单次锐评最低基础价 |
| `analysis_max_cost` | 20 | 倍率应用前的单次锐评最高基础价 |
| `analysis_fallback_cost` | 4 | 模型未返回 Token usage 时的兜底基础价 |
| `analysis_price_multiplier` | 3 | 锐评基础价倍率；默认最终收费为基础价 ×3 |
| `analysis_precharge_cost` | 6 | 调用模型前的预扣额度；成功后多退少补，失败全退 |
| `guess_break_per_correct` | 1 | 每次猜对奖励 |
| `upload_fish_cost` | 2 | 上传水鱼价格 |
| `upload_lx_cost` | 2 | 上传落雪价格 |
| `upload_all_cost` | 3 | 同时上传价格 |
| `ticket_cost_per_multiplier` | 10 | 发票每倍率单价 |
| `transfer_fee` | 0 | BREAK 转账手续费 |
| `lottery_cost` | 2 | BREAK 抽奖每次成本 |

还可查看近 30 日产出、消耗、查分量、分析量和活跃用户，以及最近
BREAK 逐笔流水。每日首次免费的上传也会保留 `delta=0` 记录。

### 用户协议

- 修改协议 HTTP(S) 链接；
- 修改用户必须从网页完整复制的确认词；
- 修改协议版本。

如果确认词发生变化而版本没有手动修改，后端会自动生成新版本。
旧版本的同意记录不再算有效。

## 6. Nginx 反向代理示例

```nginx
location /maimaidx/admin {
    proxy_pass http://127.0.0.1:8099;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

如果修改了 `MAIMAIDX_ADMIN_WEB_PATH`，Nginx `location` 也要使用相同前缀。
如果修改了 `MAIMAIDX_ADMIN_WEB_PORT`，`proxy_pass` 端口也必须同步修改。
不要在代理层删除 `Authorization` 请求头。

## 7. 安全建议

- 必须使用 HTTPS，或只允许内网/VPN 访问。
- 通过防火墙、Nginx IP allowlist 或零信任网关限制访问源。
- 不要把 Token 放在 URL、群消息、截图或仓库中。
- Token 泄露后立即更换环境变量并重启 Bot。
- 定期备份 `data/admin/admin.db`、`data/break/break.db` 和 `data/account/account.db`。
- 不建议将 WebUI 直接暴露到互联网且不做额外访问控制。

## 8. 常见问题

### 页面 404

- 确认 `MAIMAIDX_ADMIN_WEB_ENABLED=true`；
- 确认已重启 Bot；
- 确认日志显示 WebUI 已监听，且端口没有被其它进程占用；
- 确认反向代理路径与 `MAIMAIDX_ADMIN_WEB_PATH` 一致。

### API 返回 401

Bearer Token 不正确。重新复制完整 Token，不要带引号或首尾空格。

### API 返回 503

`MAIMAIDX_ADMIN_WEB_TOKEN` 未配置或长度少于 24 位。

### “管理面板”显示内网地址

配置 `MAIMAIDX_ADMIN_WEB_PUBLIC_URL=https://你的域名`。该值只影响指令展示，
不影响实际监听或路由。

### 页面有数据但群消息排行不增长

确认 `MAIMAIDX_MESSAGE_STATS_ENABLED=true`，然后重启 Bot。
