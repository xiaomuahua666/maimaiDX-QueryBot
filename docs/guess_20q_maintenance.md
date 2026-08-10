# 20问（你想我猜）维护指南

本文档说明「你想我猜」20问是非题判定系统的数据源、更新方法与注意事项，供后续版本更新维护参考。

## 一、数据源

### 1. 曲目数据（版本/分类/BPM/定数/谱师）
- **本地文件**：`/workspace/dxdata.json`
  - 结构：`songs[]` 数组，每首曲含 `title/artist/category/bpm/sheets[]`
  - `sheets[].version`：曲目所属版本（如 `BUDDiES PLUS`）
  - `sheets[].noteDesigner`：谱师名
  - `sheets[].internalLevelValue`：定数（如 13.6）
  - `sheets[].difficulty`：难度（basic/advanced/expert/master）
- **更新方式**：由数据维护者定期从官方/社区数据源更新 `dxdata.json`
- **加载入口**：`libraries/maimaidx_music.py` 的 `MaiMusic.get_music()`

### 2. 别名数据（曲目俗称/别名）
- **在线 API**：水鱼在线别名 API（`libraries/maimaidx_music.py` 的 `get_music_alias_list()`）
- **用途**：猜曲名时匹配玩家输入的别名（如「魔法少女」=「魔法少女とチョコレゐト」）
- **更新方式**：API 自动更新，无需本地维护

### 3. 版本发售日数据
- **来源**：SEGA 官方 arcade 页面 https://www.sega.jp/arcade/find/?q=maimai
- **记录位置**：`libraries/maimaidx_guess_20q.py` 的 `_VERSION_ORDER`（含发售日注释）

## 二、版本映射表更新指南

### 何时需要更新？
舞萌DX 新版本上线后（如 CiRCLE PLUS 之后的新版本），需同步更新以下三处。

### 1. `_VERSION_ORDER`（发售顺序表）
位置：`libraries/maimaidx_guess_20q.py` 第 441 行

新增版本时在末尾追加，格式：
```python
'maimai でらっくす 新版本名',     # YYYY-MM-DD 中文俗称
```

**注意**：
- 顺序必须按发售日正序（旧→新）
- PLUS 在基版之后
- 注释写发售日 + 中文俗称

### 2. `_VERSION_KEYWORDS`（版本俗称映射）
位置：`libraries/maimaidx_guess_20q.py` 第 364 行

新增版本时在对应位置（PLUS 在前、基版在后）追加：
```python
('maimai でらっくす 新版本名 plus', ('新版本 plus', '新版本+', '新代+', '新代')),
('maimai でらっくす 新版本名', ('新版本', '新代', '新')),
```

**注意**：
- canonical（第一字段）必须与 `_VERSION_ORDER` 中的字符串完全一致（小写）
- kws（第二字段）收录玩家所有可能的俗称，含中文简称、英文、错字
- PLUS 在基版之前录入（匹配顺序优先）

### 3. `_VERSION_GROUP_ALIASES`（合并叫法）
位置：`libraries/maimaidx_guess_20q.py`（搜索 `_VERSION_GROUP_ALIASES`）

如果新版本有国服合并叫法（如「熊华代」=熊代+华代），追加：
```python
'新合并代': ('maimai でらっくす 新版本', 'maimai でらっくす 新版本 plus'),
```

**当前已收录的合并叫法**：
| 合并叫法 | 包含版本 | 说明 |
|---|---|---|
| 舞代 | 旧框全系列（finale 及以前） | 旧框统称 |
| 真代 | maimai + maimai plus | |
| 熊华代 | でらっくす + でらっくす plus | 国服舞萌DX |
| 爽煌代 | splash + splash plus | 国服舞萌DX2021 |
| 宙星代 | universe + universe plus | 国服舞萌DX2022 |
| 祭祝代 | festival + festival plus | 国服舞萌DX2023 |
| 双宴代 | buddies + buddies plus | 国服舞萌DX2024 |
| 彩镜代 / 镜彩代 | prism + prism plus | 国服舞萌DX2025 |

**关于 PRiSM PLUS 的「彩代」俗称**：
PRiSM=镜代（prism 棱镜），PRiSM PLUS=彩代（因 KALEIDXSCOPE 万花筒彩色元素，社群俗称）。
合并叫「彩镜代」（国服舞萌DX2025 = PRiSM + PRiSM PLUS）。
此俗称未见于公开 wiki（B站入坑指南、萌娘百科、THBWiki 均只标 PRiSM=镜代），
但社群实际使用，已收录。后续若 CiRCLE/CiRCLE PLUS 出现合并叫法（如「圈彩代」），
按同规律补到 `_VERSION_GROUP_ALIASES`。

### 4. LLM 提示词同步
位置：`libraries/maimaidx_guess_20q.py` 的 `_GUESS_20Q_LLM_SYSTEM`（第 1546 行）

新增版本后需同步更新提示词里的两处：
- **【版本俗称对照】**：追加 `新代=maimai でらっくす 新版本名`
- **【版本发售顺序】**：在顺序链末尾追加 `→ 新代(新版本)`

## 三、分类映射更新指南

### 1. `_GENRE_KEYWORDS`（分类俗称表）
位置：`libraries/maimaidx_guess_20q.py` 第 1137 行

新增分类俗称时追加到对应行：
```python
('maimai', 'maimai分类（原创曲）', ('原创曲', ..., '新俗称')),
```

**当前分类映射**：
| 分类字段值 | 玩家俗称 |
|---|---|
| `maimai` | 原创曲/本家曲/委约曲/舞萌/舞萌曲/舞萌原创 |
| `niconico＆ボーカロイド` | 术曲/V家曲/术力口/初音曲 |
| `東方Project` | 东方曲/东方同人/touhou |
| `POPS＆アニメ` | 动漫曲/动画曲/J-POP/流行曲/pops |
| `ゲーム＆バラエティ` | 游戏曲 |
| `オンゲキ＆CHUNITHM` | 音击曲/中二节奏曲/chunithm |
| `宴会場` | 宴会曲/utage |

### 2. LLM 提示词分类映射
位置：`_GUESS_20Q_LLM_SYSTEM` 规则11

需与 `_GENRE_KEYWORDS` 保持同步，追加新俗称时两处都要改。

## 四、谱师别名更新指南

### 1. `_CHARTER_ALIASES`（谱师别名表）
位置：`libraries/maimaidx_guess_20q.py`（搜索 `_CHARTER_ALIASES`）

谱师常有别名/笔名/社团名，玩家用别名提问时需映射到官方名：
```python
('官方名', ('别名1', '别名2', '罗马音', '中文译名')),
```

**更新方法**：
- 从 `dxdata.json` 的 `sheets[].noteDesigner` 获取官方谱师名
- 玩家社区收集别名/俗称
- 双向匹配：玩家用别名 → 匹配官方名；玩家用官方名 → 直接匹配

### 2. 注意事项
- 谱师别名题走规则层，不走 LLM（LLM 对小众谱师信息不可靠）
- 合作谱师（多人共同作谱）需双向匹配：任一名字命中即算「是」
- 谱师总数 >3 时，只列前 3 位，第 4 位及以后走 LLM 回「无法回答」

## 五、定数判定规则

### 1. 档位 vs 精确定数
- **整数定数**（如 13）→ 档位判断：`[13.0, 13.5] 闭区间`（含 13.0 和 13.5）
- **+档**（如 13+）→ `[13.6, 13.9] 闭区间`
- **小数定数**（如 13.6）→ 精确等于：`abs(ds - 13.6) < 0.01`

### 2. 舞萌定数小数位
舞萌定数小数位只有 `.0/.5/.6/.7/.8/.9`，因此用闭区间（左右同符号）表示档位范围。

### 3. 比较词语义
| 比较词 | 语义 | 备注 |
|---|---|---|
| 以上/大于/超过/高于 | `>` 严格大于 | **「以上」不含本数** |
| 以下/小于/低于 | `<` 严格小于 | **「以下」不含本数** |
| 不低于/不高于/至少/至多/大于等于/小于等于 | `≥/≤` | 含本数 |
| 等于/= | `=` 精确等于 | |

## 六、版本顺序判断规则

### 1. 方向词四类
| 方向词 | 语义 | 含本数 |
|---|---|---|
| 及以后/不早于/以来 | `>=` | 是 |
| 及以前/及之前/不晚于 | `<=` | 是 |
| 之前/以前/前面/更早/早于/旧于/往前/前一代 | `<` | 否 |
| 之后/以后/后面/更晚/晚于/新于/往后/后一代 | `>` | 否 |

### 2. 合并叫法区间
合并叫法（如双宴代=[21,22]）按区间判断：
- `及以后` = `>= lo`（区间左端）
- `及以前` = `<= hi`（区间右端）
- `之前` = `< lo`
- `之后` = `> hi`

### 3. 「比X早/晚/新/旧」句式
用正则 `_VER_BI_CMP_RE` 匹配 `比.{0,30}?(早|晚|新|旧)`，早/旧=`<`，晚/新=`>`。

## 七、注意事项

### 1. 「舞萌」一词的歧义
- **作为分类**：「是舞萌吗」= 分类是否为 maimai（原创曲），不是游戏归属
- **作为版本**：「是舞代吗」= 是否为旧框版本（maimai~finale）
- 规则层已处理：`_GENRE_KEYWORDS` 的 maimai 行含「舞萌」关键词
- LLM 提示词有【最高优先级】舞萌歧义消解规则

### 2. reason 不泄露真值
判定依据（reason）只描述「Milk 把题意理解成什么维度」，绝不包含曲目实际数值（定数/版本/分类/BPM）。

### 3. ASCII 版本名拼写容错
玩家用 ASCII 版本名（milk/buddies/dx 等）时常拼错，`_looks_like_ascii_version_text` 门控函数放宽疑似版本题交 LLM 兜底。

### 4. 测试期提示
每次回复末尾加「测试版本，如果出现数据错误，语义理解错误，请您联系管理员反馈」。提问每 5 次带一次，结算/猜对/失败始终带。

## 八、测试

### 测试文件
| 文件 | 覆盖 |
|---|---|
| `scripts/test_guess_20q_range_reason.py` | 定数区间/比较词/版本顺序/分类/谱师 |
| `scripts/test_guess_20q_tricky.py` | 合并叫法边界/比X句式/单字陷阱/方向词歧义 |
| `scripts/test_guess_20q_ascii_typo.py` | ASCII 拼写容错 + 门控防误伤 |
| `scripts/test_guess_20q_maimai_disambig.py` | 「舞萌」歧义消解 |
| `scripts/test_guess_20q_regression_real.py` | 真实曲库大规模回归（4340 case） |

### 运行测试
```bash
python scripts/test_guess_20q_range_reason.py
python scripts/test_guess_20q_tricky.py
python scripts/test_guess_20q_ascii_typo.py
python scripts/test_guess_20q_maimai_disambig.py
python scripts/test_guess_20q_regression_real.py
```

### 新增版本后的回归验证
更新版本映射表后，务必运行 `test_guess_20q_regression_real.py`，它会基于真实曲库自动验证所有规则层判定。

## 九、常见问题排查

### Q: 玩家问「是舞萌吗」回答「是」但分类不是 maimai？
检查 `_GENRE_KEYWORDS` 的 maimai 行是否含「舞萌」关键词。规则层应直接命中，不走 LLM。

### Q: 版本顺序题回答错误？
1. 检查 `_VERSION_ORDER` 是否包含该版本
2. 检查 `_VERSION_INDEX` 查询是否用了 `_norm` 归一化（去空格+小写）
3. 用 `_music_version_index(music)` 验证索引返回值

### Q: 新版本曲目的版本字段无法识别？
`dxdata.json` 里的 `sheets[].version` 格式（如 `BUDDiES PLUS`）需通过 `_normalize_version` 映射到 `_VERSION_ORDER` 里的 canonical（如 `maimai でらっくす buddies plus`）。

### Q: 定数档位判断错误？
检查定数小数位：舞萌定数只有 `.0/.5/.6/.7/.8/.9`，用闭区间 `[N.0, N.5]` / `[N.6, N.9]` 表示。
