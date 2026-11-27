# 调参手册（使用者版）

> **一句话**：所有可调参数都在 `core/tuning.py`；你现在跑的这一套，就是默认预设。
> 想改行为 → 改 `.env`（推荐）或改 `core/tuning.py`；改完重启。
> 全量清单（377 个键）见 [tuning.md](tuning.md)，本文只讲**怎么调、调哪个、别碰什么**。

---

## 1. 三个入口，各管一件事

| 入口 | 干什么 | 什么时候用 |
|---|---|---|
| `core/tuning.py` | **默认值**（唯一来源） | 想改「出厂设置」 |
| `.env` | **覆盖**（同名键优先） | 临时调试 / 单机差异（推荐） |
| `docs/tuning.md` | **全量表**（自动生成） | 查某个键现在是多少、谁在用 |

> `.env` 里写 `CTX_EVIDENCE=800` 就会覆盖默认的 500。
> 删掉这一行 → 回到默认值。**不需要改代码。**
>
> **量纲**：文档里的公式（步长/饱和/锚点）都在归一化 `-1~1` 空间算；
> `tuning.md` 说的「内部 -100~100」只是落盘/展示，**不要拿它套公式**。

> ⚠️ **先看「生效位置」**：`tuning.md` 里标 **（设计，未接线）** 的键，是设计已定、代码还没读——
> **改了不生效**。这批键在 `core/tuning.py` 的 `UNWIRED` 名单里；接线后标记会自动消失。
> `python3 scripts/check_tuning.py --strict` 会拦住「文档说在用、代码没读」的假接线。

---

## 2. 分组速查

| 前缀 | 管什么 | 例子 |
|---|---|---|
| `MOOD_` | 心情：半衰期、步长、饱和、快速通道、锚点 | `MOOD_HALF_LIFE_GROUP_MIN` |
| `REL_` | 关系：四量基线/半衰期、防刷、信任/尊重步长 | `REL_DAILY_CAP` |
| `CTX_` | 上下文预算：每块多少 token | `CTX_EVIDENCE` |
| `RET_` | 检索：候选池、证据条数、工具条数、延迟目标 | `RET_EVIDENCE_MAX` |
| `ADDR_` | 收发件人：8 级信号置信度、断言阈值 | `ADDR_ASSERT_MIN` |
| `OUT_` | 输出：气泡字数/条数 | `OUT_BUBBLE_CHARS` |

---

## 3. 常用配方（按症状查）

### 3.1 她「太冷淡 / 太容易上头」

```text
单次幅度小一点：MOOD_STEP_1 0.10 → 0.08，MOOD_STEP_2 0.22 → 0.18
人设整体敏感度：MOOD_SENS_MIN / MOOD_SENS_MAX（默认 0.6 / 1.4）
```

> 只改 `MOOD_STEP_*` 是**全局**变钝/变灵；改 `SENS_*` 是改**人设的取值范围**。

### 3.2 情绪「掉得太快 / 一直不散」

```text
群聊：MOOD_HALF_LIFE_GROUP_MIN   默认 30（分钟）
私聊：MOOD_HALF_LIFE_PRIVATE_MIN 默认 120
全局：MOOD_HALF_LIFE_GLOBAL_MIN  默认 360
```

> 半衰期翻倍 = 情绪停留时间翻倍。建议一次只动一档（30 → 60，不要 30 → 180）。

### 3.3 被骂了还在笑

```text
MOOD_FAST_FLOOR        默认 −0.45（强负面）
MOOD_FAST_FLOOR_LIGHT  默认 −0.25（轻负面）
```

> 这是「硬钳」：命中强负面词就 `v = min(v, −0.45)`，只降不升。
> 想更狠 → −0.55；但别低于 −0.7，否则一句脏话直接见底，后面没法演。

### 3.4 情绪老是「漂」到不该在的位置

```text
MOOD_ANCHOR_EVERY      默认 20（每多少窗口纠一次）
MOOD_ANCHOR_GAIN       默认 0.25（一次最多挪 25%）
MOOD_ANCHOR_DEADBAND   默认 0.15（差得少就不动）
```

> 漂得厉害 → `EVERY` 降到 10；太容易被锚点拽走 → `GAIN` 降到 0.15。
> `MOOD_ANCHOR_MAX_MOVE`（0.20）是单次硬上限，**不建议超过 0.3**。

### 3.5 好感「一天就拉满」

```text
REL_DAILY_CAP        默认 0.15（每天最多变多少）
REL_NOVELTY_COEF     默认 0.6（同一人重复刷，收益递减）
REL_AFFINITY_HALF_LIFE_DAYS 默认 7（不联系多久掉一半）
```

> 想「慢热」：`DAILY_CAP` 0.10 + `HALF_LIFE` 5。
> 想「不容易散」：`HALF_LIFE` 10~14，但别超过 21（否则等于不衰减）。

### 3.6 记忆塞太多 / 太少

```text
CTX_EVIDENCE        默认 500（证据块 token）
RET_EVIDENCE_MAX    默认 5（最终注入几条）
RET_MEMORY_TOP      默认 4（记忆块注入条数；prompt_builder.build_packet 的默认 limit）
RET_MAX_PER_PERSON  默认 3（同一个人最多几条）
```

> 「记不住事」→ `CTX_EVIDENCE` 700 + `RET_EVIDENCE_MAX` 7（但 >7 条通常更差）。
> 「老是同一个人刷屏」→ `RET_MAX_PER_PERSON` 2。

### 3.7 老是「引用错人 / 不知道在跟谁说」

```text
ADDR_ASSERT_MIN     默认 0.80（≥此值才敢断言「→ X」）
ADDR_MAYBE_MIN      默认 0.50（0.5~0.8 写「像是在回 X」）
ADDR_RECENT_MENTION 默认 0.50（短句冲人「找打了」→ 指向最近被点名的人）
```

> 认错人频繁 → `ASSERT_MIN` 提到 0.85~0.90（更保守，更多「像是在回」）。
> 短句老认错「冲谁」→ 调 `ADDR_RECENT_MENTION`（<0.5 会降成「不确定」）。

### 3.8 回复太长 / 太碎

```text
OUT_BUBBLE_CHARS   默认 40（单泡汉字上限）
OUT_BUBBLES_MAX    默认 2（单轮气泡上限）
GROUP_MAX_HANZI    默认 20（群聊单条目标长度，显示宽度）
PRIVATE_MAX_HANZI  默认 20（私聊同上）
LONG_FORWARD_MIN   默认 120（系统长文转合并转发卡片的下限）
```

> 太碎 → `OUT_BUBBLE_CHARS` 60；太啰嗦 → 40 保持不变、靠人设收紧。
> **注意**：这些是「超过就转合并转发」的阈值，不是截断。写成 1000 不会让话更长，
> 只会让一条几千字直接刷屏 —— 启动日志会 `[ENV] ⚠️` 提醒。

### 3.8b 回完一句后下一句不理

```text
GROUP_REPLY_COOLDOWN  默认 45（秒）
REPLY_INTERVAL        同一会话两次回复的最小间隔
FOCUS_FOLLOW_COUNT    被 @ 后关注几条（默认 3）
FOCUS_TIMEOUT         关注窗口有效期（默认 120s）
```

> 45s 冷却只拦「没点她」的消息；**@ / 喊名字 / 关注窗口内的人**不受影响。
> 觉得太粘 → 调小 `FOCUS_FOLLOW_COUNT`；觉得「只理艾特、后面不接」→ 先确认日志里
> 不是 `[COOLDOWN]` 而是模型自己 `stay_silent`。

### 3.9 长文档答不全

```text
CTX_LONG_DOC_EVIDENCE  默认 6000（long_doc 时证据上限）
CTX_LONG_DOC_LIBRARY   默认 3000（long_doc 时图书馆上限）
```

> 只在「详细讲讲 / 分析一下 / 这本书讲了什么」这类请求时才启用；
> `persona / 协议 / 状态卡 / 历史 / anchor` **永远不随 long_doc 膨胀**。

### 3.10 工具挂太多、回复变慢

```text
RET_TOOLS_TOP  默认 6（按描述检索前 6 个工具）
```

> 工具越多，prompt 越长、越容易乱调。建议保持 6，不要全挂（26 个）。

---

## 4. 别碰的地方（碰了会出事）

| 键 | 为什么 |
|---|---|
| `ENUMS`（`core/design_constants.py`） | `target` 5 值、`route` 4 值是**协议**，改了历史数据全废 |
| `MOOD_ANCHOR_LEVEL1/LEVEL2` | 必须 `LEVEL1 < LEVEL2` 且都 > 0，否则锚点方向反了 |
| `MOOD_ANCHOR_WEIGHT` | 必须 `0 < W ≤ 1`；调到 1.0 等于「心情 = 模型说的绝对档位」，相对步进失效。注意它是 `v` 的**主项**，`MOOD_P0/Q/R` 只决定残差步进 |
| `AFF_V1_CAP` | 必须 ≥ 尾部单次跳变（约 0.12~0.14），且 **≤ `AFFINITY_DAILY_CAP`**。可达上限 = `AFF_V1_DV_K × AFF_W_MAX`（0.30×0.42=0.126）+ 事件项；写成 0.36 就是永不触发的死护栏 |
| `MOOD_HARD_CAP` | 必须 ≥ `MOOD_STEP_2 × MOOD_SENS_MAX`（0.22×1.4=0.308），否则正常步进就被截断 |
| `CTX_SOFT_CAP` | 必须 ≥ 最大组合 2580（工具+custom+多目标），否则有档位一出生就被裁 |
| `MOOD_ANCHOR_COUNT_SKIPPED` | 设成 0 会让「全是噪声的群」永远不锚定，状态冻结 |

> 这些不是「不能调」，是「调了要同时改别的」。校验脚本会替你拦一部分。

---

## 5. 改完怎么验证（30 秒）

```bash
# ① 参数本身有没有写错（类型/说明/引用）
python3 scripts/check_tuning.py

# ② 文档口径和代码默认值还一致吗
python3 scripts/check_design_docs.py --strict

# ③ 语法没炸
python3 -m compileall -q core services plugins bot.py
```

三条都 PASS 再重启。

> 实现 P2 之后还会多一条：`python3 scripts/check_causal.py`（33 条因果断言，
> 0 模型调用）。**它红了才说明行为真的变了**，前三条只保证「没写错」。

---

## 6. 回滚

```text
只改了 .env   → 删掉那几行，重启
改了 tuning.py → git checkout core/tuning.py（或改回原值）
```

`docs/tuning.md` 是生成的，**不要手改**；改完 `tuning.py` 后跑：

```bash
python3 scripts/dump_tuning.py --write
```

---

## 7. 这套默认参数是什么

| 组 | 关键默认值 |
|---|---|
| 心情 | 半衰期 30min / 2h / 6h；步长 0.10 / 0.22；饱和 0.6；硬上限 0.35；P0 0.25 |
| 锚点 | 每 20 窗口 / 静默 30min；增益 0.25；死区 0.15；单次 ≤0.20 |
| 快速通道 | 强负面钳 −0.45；轻负面 −0.25；P ×0.5 |
| 关系 | affinity 7d / trust 45d / respect 90d / intimacy 30d；日上限 0.15 |
| 预算 | 常态 2120；有工具 2420；custom 2220；多目标 2180；soft cap 2600；hard cap 12k（仅 long_doc） |
| 检索 | 证据 ≤5 条 / 500 token；每人 ≤3 条；工具 top6 |
| 输出 | 单泡 ≤40 字；单轮 ≤2 泡 |
| 决策 | `a` 档位 0.2/0.5/0.8（平滑 0.4）；节奏 0.9~1.1（`off=1.0`） |

> 这就是「一套默认参数」：**换一套 = 改 `tuning.py` 里这 120 个设计键**，
> 不用碰业务代码。改完 `check_design_docs.py` 会告诉你文档有没有跟上。
