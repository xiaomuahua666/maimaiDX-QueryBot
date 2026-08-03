# 账号 Bot 合并说明

原 `koishi-plugin-maibot` 为 Koishi/TypeScript 插件，QueryBot 为
NoneBot/Python 插件，因此本次采用业务移植，而不是在一个进程中同时运行两套框架。

## 合并后的职责

- `config.py`：查询、账号、AWMC API、平台与缓存的统一配置模型。
- `.env.example`：部署时唯一需要参考的配置模板。
- `libraries/maimaidx_account_db.py`：舞萌账号、二维码及水鱼/落雪上传 Token。
- `libraries/maimaidx_sw_api.py`：team/public 两种 AWMC API 调用。
- `command/mai_account.py`：账号和上传指令。
- `libraries/maimaidx_break.py`：继续独立负责 BREAK，不迁入账号表。

## 用户指令

| 指令 | 说明 |
|---|---|
| `mai账号` | 查看合并后的账号帮助 |
| `mai绑定` / `maibind` | 绑定并验证舞萌账号；同 UID 旧记录会被认领 |
| `mai解绑` | 解绑街机账号，保留上传 Token |
| `mai状态` / `mymai` | 查看完整账号状态；缓存失效时交互刷新 SGID |
| `mai绑定水鱼 [Token]` / `maibindfish [Token]` | 交互获取或直接保存水鱼上传 Token；无参数时最多重试 3 次 |
| `lxbind` | 绑定落雪 OAuth，上传时无需导入 Token（推荐） |
| `mai绑定落雪 <导入Token>` / `maibindlx <导入Token>` | 保存落雪第三方导入 Token（兼容） |
| `maiu` | 仅上传水鱼 |
| `maiul` | 仅上传落雪 |
| `maiua` | 同时上传水鱼与落雪 |
| `更新pc数` | 已绑定后可直接刷新，不必再次发送二维码 |
| `发票` / `fp <2/3/5>` / `mai查票` | 发放及查询票券（倍率可由 ENV 配置；public 模式按网关 Token 计费） |
| `mai地图` | 查询游玩地区 |
| `mai预览` / `mai道具` | 查询账号预览 / 道具列表（每次成功查询 5 BREAK） |
| `mai门状态` / `mai查门` | 只读查询 Kaleidx Gate 状态（成功 5 BREAK） |
| `mai改成绩` / `mai删成绩` | 交互或一步编辑 / 删除单条成绩（75 / 50 BREAK） |
| `mai清票` / `mai改道具` | 清空 Charge（10 BREAK）/ 高风险道具修改（100 BREAK，未经测试） |
| `maiping` | AWMC API 健康检查 |

`maiul` 和 `maiua` 会优先使用 `lxbind` 的 OAuth 授权直接上传；没有 OAuth
时才使用 `mai绑定落雪` 保存的导入 Token。

绑定、PC 同步和上传命令同时接受完整 `SGWCMAID...`、舞萌官方
`/qrcode/img/MAID....png` 图片链接及 `/qrcode/req/MAID....html` 请求链接；链接路径
中的 `MAID...` 会在本地转换为 `SGWCMAID...`，Bot 不需要访问二维码网页。
直接发送含二维码的图片或截图时也会自动识别；仅当二维码内容是上述舞萌凭据时才处理。
识别后会立即尝试撤回原消息，提示动态预计耗时；未绑定账号时自动绑定，并按用户已绑定的
水鱼/落雪渠道上传（只绑定一边就上传一边，两边都绑定则全部上传）。
普通图片和其它二维码静默忽略。无账号记录会先自动验真绑定，之后同步 PC，并按已绑定的
水鱼 Token、落雪 OAuth 或兼容 Token 自动上传。

## mymai 状态与 SGID 缓存

- 最近输入且验证成功的 SGID 默认缓存 10 分钟；`mymai`、PC 更新和成绩上传共用缓存。
- 使用缓存前会重新调用 preview，并确认舞萌 UID 与当前绑定一致；不会显示该 UID。
- 状态包含用户名、Rating 及拆分、友人对战等级、游玩次数、机台/数据版本、最近登录、
  游玩、拼机、地区、觉醒次数、封禁状态、查分器绑定、最近上传和有效票券摘要。
- 缓存过期、上次操作失败、preview 全空/500 或账号不一致时，Bot 会引导用户打开微信中的
  「舞萌DX | 中二节奏」玩家二维码，长按选择「识别图中二维码」并复制字符或网页地址。
- 新二维码最多验证 3 次且优先撤回。取消刷新或连续失败 3 次时仍展示已保存的缓存资料。
- `AWMC_SGID_CACHE_SECONDS=600` 可调整缓存时间；设置为 `0` 表示每次重新询问。

## 绑定认领

`mai绑定` 成功读取最新 SGWCMAID 后，以返回的舞萌 UID 判断账号归属。如果该 UID
仍在 Koishi 或旧 Bot 迁移记录下，Bot 会把记录认领到当前平台账号，继承旧记录中
已有而当前没有的水鱼/落雪 Token，并清除旧账号保存的二维码与 PC 登录凭据。聊天
消息只提示“旧记录已安全转移”，不会显示旧平台账号标识。二维码校验失败时不会认领。
BREAK、协议状态、封禁状态和落雪 OAuth 不随舞萌账号认领；当前用户如需 OAuth，
应由本人发送 `lxbind` 授权。

## 兼容策略

- 旧 `SDGBTECHAPI` 仍有效；若同时设置 `AWMC_API_BASE_URL`，以后者为准。
- `team` 模式复用现有 `/awmc/api/v1/...` 服务，请求体带 `keychip`。
- `public` 模式对接 https://api.wmc.pub（`/v1/...`），请求头带 `Authorization: Bearer <gw_令牌>`；`keychip` 由网关注入，调用方只传 `qrcode` 等业务参数。用户数据、谱面成绩、票券、地图、上传均可用；按接口消耗账户 Token。
- 旧公共路径（如 `/v1/get_preview`、`/v1/upload_b50`）已移除，请使用新路径。
- QueryBot 现有 BREAK、查分、猜歌、友人对战数据库均保持原路径，不做破坏性迁移。

## 未照搬的 Koishi 专属功能

以下功能依赖 Koishi authority、bind 插件或 Koishi 数据库语义，不能直接复制：

- 优先授权卡密、群组换绑和 Koishi authority 自动优先
- QQ Markdown Keyboard 的 Koishi 交互实现
- 保护/锁定、手工改分、清收藏品等内部维护指令

这些功能应按 QueryBot 的管理员权限与 BREAK 经济重新设计，不建议照搬旧实现。

## 旧数据

拿到 Koishi 数据库后，可使用超级管理员指令 `迁移Koishi 检查 koishi.db` 预检，
再用 `迁移Koishi 确认 koishi.db` 导入；也可运行
`scripts/migrate_maibot_accounts.py` 导入账号、二维码、上传 Token 与用户协议记录。
可以直接传入包含多个 Koishi 插件表的完整 SQLite 数据库：迁移器以只读方式打开源库，
只读取 `maibot_bindings`、可选的 `maibot_user_terms` 和用于身份解析的 `binding`，
其它插件表不会读写或删除。`binding(aid,pid)` 可自动把 Koishi 内部 ID 映射回 QQ；
无法唯一映射的记录需提供 `identity-map.json`。
