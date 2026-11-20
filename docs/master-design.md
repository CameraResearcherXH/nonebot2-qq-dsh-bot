# 00 · 总纲：设计 / 功能 / 新提案地图

> **这份文档存在的唯一目的**：以后实现某一个功能时，不要因为专注细节而忘掉整体。
>
> 读法：
> 1. 动手前先看 §3（红线）和 §6（变更影响矩阵）—— 90% 的"改一处坏三处"都能在这里拦住；
> 2. 想知道某个能力有没有、在哪、什么状态，看 §2；
> 3. 想动心情/好感/记忆/表情，看 §4 + §5；
> 4. 有新想法，先加到 §7 决策表，再动手。
>
> 维护规则：**新功能必须同时在 §2（能力清单）、§6（影响矩阵）、§9（文档索引）各留一行**，
> 否则三个月后又会出现"代码里有、文档里没有、没人知道它在生效"。
>
> 可信度：本文以**代码**为准；`agent-pipeline.md` 等旧文档已过期（见 §9）。

---

## 1. 三张图

### 1.1 运行时拓扑

```
                    ┌────────────── 外部 API ──────────────┐
                    │ DeepSeek(主) GLM DashScope(Qwen)     │
                    │ Tavily trace.moe B站 pixiv qlogo     │
                    └──────▲──────────────────▲────────────┘
┌────────── 香港服务器 · docker · host 网络 ───┴──────────────────────┐
│  ┌──────────────┐  ws://127.0.0.1:3001  ┌────────────────────────┐ │
│  │ napcat 容器  │◄─────────────────────►│ qq-grok-bot 容器       │ │
│  │ (QQ 客户端)  │  OneBot v11            │ python 3.11            │ │
│  └──────────────┘                        │ bot.py → nonebot2      │ │
│                                          │  plugins/ + services/  │ │
│                                          │  FastAPI :8080         │ │
│                                          └───────────┬────────────┘ │
└──────────────────────────────────────────────────────┼──────────────┘
                                                       │ HTTP 轮询
┌────────── 上海算力机 · Windows · node.py ─────────────▼──────────────┐
│ 心跳 → pull → TASK_HANDLERS[type] → 上传结果                          │
│ Demucs:8005 │ IndexTTS2:7860 │ Ollama:11434 │ ComfyUI:8188 │ RVC     │
│ 任务：cover/split/audio_separation/rvc/rvc_mix/tts/reflection/probe/draw │
└──────────────────────────────────────────────────────────────────────┘
```

两个部署约束（写死在这里，别再忘）：
- `.env` 不能进镜像（`Dockerfile` 是 `COPY . .`，`.dockerignore` 必须含 `.env`）；
- `deploy_manifest.txt` 目前**只校验 `node.env`，不校验 `node.py`** → 算力机代码可以静默不同步。

### 1.2 代码分层

```
bot.py
├─ plugins/   （15 自研 + 21 三方）
│   chat(8628)   ← 入口/决策/状态/发送/工具/指令/测试，全在这
│   vision_agent / draw / song_cover / cover_api / tool_dispatcher
│   napcat_tools / sticker_admin / selfcheck / full_test / help_menu
└─ services/  （50 个模块，无层边界）
    llm(3180)   ← 协议+工具+历史+prompt+网络+落盘
    memory_v2 / profiles / mood / mood_judge / group_scene / ...
```

### 1.3 一条消息的生命周期（状态写入点）

```
事件 → ① 插件过滤（priority 1~5，block 会截断后续）
     → ② chat._need_reply（Rule，带副作用：破冰/记账/现场/表达/静音）
     → ③ _session_worker（档案3写 → 审核LLM → build_prompt → preflight LLM → 主LLM）
     → ④ parse_reply（MEMO/MOOD/THINK/CALL/FOCUS）
     → ⑤ 写回（memory_v2 + bond + mood + affinity）
     → ⑥ 发送（配额/气泡/长文转发/引用/@）
     → ⑦ 后台（mood_judge / conv_state / expression / profile_miner）
```

---

## 2. 现有能力清单（as-built）

> 状态：✅ 稳定 ｜ ⚠️ 可用但有已知问题 ｜ 🚧 半成品 ｜ 📝 只有设计
> 问题编号引用 `docs/review-2026-11-20.md`。

### 2.1 对话

| 能力 | 代码 | 状态 | 备注 |
|---|---|---|---|
| 群/私聊入口、白名单、睡眠 | `chat:1819+` | ✅ | 群 0-8 点睡，私聊 24h |
| 关注会话（被 @ 后跟 3 句） | `chat:402-470` | ✅ | |
| 防抖 / 连发合并 / 高刷清理 / 去重 | `chat:1729,3742,3278,3703` | ⚠️ | B7 `REPLY_INTERVAL` 失效 |
| 纯问候 / 短指令 / 收尾语过滤 | `chat:3533-3574` | ✅ | |
| 被嫌弃 → 群静默 | `chat:3468` | ✅ | 弱信号需"冲她"证据 |
| 20 字→合并转发 | `chat:711` | ⚠️ | B10 只数汉字 |
| 气泡上限 2 / @ / 引用 / 配额 | `aura_logic` + `chat:4328` | ✅ | |
| 工具循环（32 个） | `llm:254` | ✅ | scope 分散在 5 个插件 |
| 文本标记（17 种 `[[X\|..]]`） | `llm:664` | ✅ | 与工具是两套机制 |
| 主动插话（群） | `chat:4565` | ⚠️ | B6 未设 tool context |
| 主动私聊 | `proactive.py` + `chat:6053` | ✅ | 默认 DRY_RUN=true |
| 三方核对 / 影子回答 | `referee.py` | ⚠️ | C4 在关键路径 |

### 2.2 记忆与关系

| 能力 | 代码 | 状态 | 备注 |
|---|---|---|---|
| 长期记忆（BM25+遗忘+情绪） | `memory_v2.py` | ⚠️ | B14 索引全量读写（2383 条≈12MB） |
| 目录 / 翻页 / 展开 / 按时间 | `memory_v2:426+` | ✅ | |
| 记忆回访（7 天前旧事） | `memory_v2:468` | ✅ | 按人，1 条 |
| 人物档案 / 称谓 / 群覆盖 | `profiles.py` | ✅ | 每条消息 3 次写盘（B15） |
| 相处印象 bond（带情绪） | `profiles:302` | ✅ | 20 条 / 60 天 TTL |
| 亲近度 intimacy | `profiles:448` | ✅ | 30 天半衰 + affinity×0.25 |
| 显式好感 affinity | `profiles:353` | ⚠️ | 无衰减/无上限，20 分钟可饱和 |
| 词条 lorebook | `lorebook.py` | ✅ | 关键词命中才注入 |
| 昨日总结 / 滚动状态 / 复盘 | `daily_summary` / `conv_state` / `journal_agent` | ✅ | |
| 表达学习（群/人语气） | `expression.py` | 🚧 | stance 长期为空 |

### 2.3 状态

| 能力 | 代码 | 状态 | 备注 |
|---|---|---|---|
| 心情（global + session） | `mood.py` | ⚠️ | 12h 半衰被 SMOOTH=1.0 架空 |
| 心情判读（三方 LLM） | `mood_judge.py` | ⚠️ | 已上线（文档说影子，过期） |
| 安静开关（群/人 + 限时 + strict） | `chat_switch.py` | ✅ | users 桶是**全局**（D1） |
| 安静建议（对人/对群/找谁） | `quiet_advisor.py` | ✅ | 设计已经很贴合 |
| 注意力漂移 | `attention.py` | ✅ | env 静态 |

### 2.4 场景

| 能力 | 代码 | 状态 | 备注 |
|---|---|---|---|
| 群聊现场（线程/收件人） | `group_scene.py` | ✅ | 内存 + 日志补种 |
| 审核 LLM（纠错收件人/总结） | `scene_auditor.py` | ⚠️ | C4 关键路径 6s |
| 对话原文落盘 | `dialogue_log.py` | ✅ | 建议作为权威源（D5） |

### 2.5 媒体

| 能力 | 代码 | 状态 | 备注 |
|---|---|---|---|
| 识图（Qwen/GLM + trace.moe + GIF） | `vision_agent` + `vision_verify` | ✅ | |
| 视频理解（B 站抽帧） | `video_understand.py` | ✅ | |
| 表情收集/识别/发送/导入 | `sticker_*` | ⚠️ | 固定 18 标签、失败兜底"吃瓜" |
| 画图（SD via node） | `plugins/draw` | ✅ | |
| 翻唱 / TTS / RVC | `song_cover` / `node.py` | ⚠️ | C5 算法双份且已漂移 |

### 2.6 工具与运维

| 能力 | 代码 | 状态 | 备注 |
|---|---|---|---|
| NapCat 60+ API（SAFE/ADMIN/OWNER） | `services/napcat_tools.py` | ⚠️ | B3 SAFE 级无群/人白名单 |
| 算力机任务队列 | `plugins/tool_dispatcher` | ✅ | :8080 + API_SECRET |
| 自检 / 全量测试 / 帮助菜单 | `selfcheck` / `full_test` / `help_menu` | ✅ | 测试代码在生产（~2200 行） |
| 离线自测 / 链路审计 / A-B | `scripts/*` | ✅ | selftest 6.4k 行 |
| 部署清单 | `deploy_manifest.txt` | ⚠️ | 不含 node.py |

### 2.7 数据目录地图（`data/`）

```
memory/         index.json + blocks/<id>.json     长期记忆
profiles/       <qq>_profile.json / <qq>_bond.json 档案 + 相处印象
state/          <session>.json                     conv_state 滚动状态
dialogue/       YYYY-MM-DD.jsonl                   对话原文（建议权威源）
mood.json       global + sessions                  心情
mood_history.jsonl                                 心情波动流水（含 about_qq）
chat_switch.json  groups / users                   安静状态
expression/     群/人语气                           表达学习
lore/           词条                                设定库
stickers/       index + 图片                        表情库
avatars/ profile_cards/ llm_trace/ weekly/         缓存与报表
temp_*/ uploads/                                   临时（无清理策略）
```

---

## 3. 设计红线（用户明确过，不许违反）

1. **20 字规矩**：≤20 汉字直接发；>20 走合并转发，不截断。测试群用 `GROUP_CHAT_OVERRIDES` 单独放宽。
2. **工具只收不发**：工具不许自己 `bot.send`；`register_tool(..., hold=True)` 拦成待发，由她 `send(pending=N)` 决定。
3. **不用正则猜语义**：该不该接话、是不是在找她，交给 LLM + few-shot；代码只保留结构信号（@、名字、空@、引用）。
4. **记录按条数**：凡进 prompt / 影响行为的数据，用 `load_recent(limit)`；按时间只用于周报/日记。
5. **记忆必须带群前缀** `（群号:XXX）`，否则 `group_ok()` 隔离失效。
6. **安静必须带原因和起因人**，道歉要能破冰。
7. **前缀缓存**：人格 + 规矩放最前、一字不改；动态内容（消息号/待发/群总结）放最后。
8. **并发上下文用 ContextVar**，不许用全局 dict 存 bot/event。
9. **两套格式不混**：`parse_reply` 的 items 是 `{"kind","value"}`，`MessageSegment` 是 `{"type","data"}`。
10. **失败要留痕**：新代码不许再写 `except: pass`；至少 `logger.warning` 一行，并能在 `/测函数` 看到。

---

## 4. 新设计：一个 Δ，三处用

> 来源：2026-11-20 三轮讨论。完整推导见 `docs/mood-affinity-design.md` §8/§11。

### 4.1 术语（先定死，避免代码里继续混）

| 词 | 定义 | 量纲 |
|---|---|---|
| 心情值 `level` | 她此刻的心情 | -1.00 ~ +1.00 |
| 变化量 `delta` | 本轮 value − 上轮 value（带符号 = valence） | -2.00 ~ +2.00 |
| 显著性 `salience` | \|delta\| 归一化，决定记忆取舍/加粗/检索 | 0 ~ 1 |

### 4.2 一个 Δ 三处用

```
                 Δmood = value − prev_value
                /              |                \
          |Δmood|        sign(Δmood)      Δmood × gain × tol × cause
            ↓                ↓                     ↓
        salience          valence             Δaffinity
   （加粗/检索/遗忘）   （好/坏）          （个人导致的心情记录）
```

### 4.3 好感 ≠ 纯映射（重要）

```
affinity(t) = affinity(t-1)
            + Σ 个人导致的 Δmood × gain × tol      # 主驱动：大起大落 + 小涨落都算
            + Σ 非心情事件（profile_update / 明确印象）
            - 慢回归（无互动天数）
```

- **只认 `cause=speaker`**：环境/自己/别人引起的心情，不进好感；
- **记忆和好感用的是同一个 Δ，但用法不同**：记忆里 Δ 只决定加粗/检索（都照记）；
  好感里 Δ 直接推动涨落（大起大落 + 小涨落都算）；
- **不是 1:1**：有 gain、单次上下限、每日上限、忍耐系数、慢回归；
- 判读要新增 `cause` 字段，并**删掉 `affinity`**（避免两个来源互相矛盾）。

### 4.4 记录结构（memory / bond 共用）

```
时间 ts ｜ 心情名词 mood_word ｜ 阈值 delta/salience ｜
事情目录 title ｜ 心理活动 mind ｜ 事情详细 detail
```

**记忆是二维的：时间（近→远）× 情绪（大→小）。Δ 不是"记不记"的开关。**

| 维度 | 作用 |
|---|---|
| 时间 | 近期记得清、容易查；远期难查 |
| 情绪 \|Δ\| | 加粗目录、检索加成、遗忘更慢 |

**记忆和档案是两套记录策略（不要混）：**

| | 记忆（memory） | 档案（profile / bond） |
|---|---|---|
| 记什么 | **全记**：每个 turn 一条目录项 | **只记影响好感的事件**（`cause=speaker` 且 \|Δaff\|≥0.01） |
| 排序 | 时间倒序（近→远） | weight / salience + 时间 |
| 情绪作用 | 加粗 + 检索加成 + 减慢遗忘 | `weight = 1 + 2*salience` |
| 用途 | 「我经历过什么」 | 「我跟他是什么关系」 |

- **目录层**：按时间从近到远累积；目录项 = 时间 + 心情名词 + 💥 + 事情总结（title）。
- **详情层**：点开目录 → mind + `dialogue_log` 原文（不复制，引用 turn_id）。
- **检索层**：近期 + 高 \|Δ\| 最容易查到；远期 + 低 \|Δ\| 最难查到。关键词只加分，不做硬门槛。
- **全记的硬前提**：索引必须先从 JSON 迁到 sqlite + FTS5，否则一年 300 万条直接不可用。

> 代码现状：`score = bm25 × w_emo × w_forget` 已经是二维公式，
> 但 BM25 被当成了硬门槛（`base<=0 and importance<0.7 → continue`），
> 导致「近期但没词重合」的事召不出来 → 需把 BM25 降级为加分项。

### 4.5 写入时序（前提）

```
回复 → 写 memory(pending, turn_id) → 发送
              ↓ 后台判读
      Δmood → patch_salience(turn_id) → active（Δ 只写权重，不决定归档）
```
现在用的是**上一轮**判读值（`chat:4035` 先 create_task，`4087` 才读 judge）→ 必须改。

### 4.6 表情包

视觉侧一次调用给「心情 + 强度 + 描述」→ `|强度|≥0.25` 自动收 → 标签 = 心情名词。
配套：标签合并（精确→别名→包含→新词，第二次出现才建桶，总数上限）。

### 4.7 空闲衰减

有人聊天 → 判读驱动；没人聊天 → 空闲 10 分钟后线性归 0（1 点/5min，内部 -100~100），
归零时同步清 `label/judge`（否则"数值 0、嘴上还说烦"）。

### 4.8 好感文字块

新增 `relation` 块（和 `mood` 平级、紧跟 `focus_card`）：
好感 + 亲近 + 想起的事（正 1~2 条、负 0~1 条）+ 跟他说话的倾向。
纯模板 + 现有数据，零额外调用。规则：**底色决定愿不愿意理，此刻决定这句怎么说**。

---

## 5. 差距矩阵（现状 vs 目标）

| 设计点 | 现状 | 缺什么 | 位置 | 决策 |
|---|---|---|---|---|
| Δ 驱动加粗/检索（非记录门槛） | `emotion_weight` 来自上一轮 \|value\| | 本轮 Δ、时序、BM25 降级为加分 | `chat:4087/4130` | D7/D8 |
| 心情名词入库 | 已有 `mood_word`（但慢一轮） | 用本轮 `judge.word` | `memory_v2:177` | D7 |
| 心理活动入库 | 只在 think 文件 | memory/bond 加 `mind` | `memory_v2` | D7 |
| 事情目录 | `text[:80]` 截断 | 生成 `title` | `memory_v2:183` | D7 |
| valence | 无 | Δ 的符号 | — | D6 已解决 |
| 记忆全记 / 档案只记好感事件 | 现在每条 MEMO 都写 bond | 分开两套记录策略 | `chat:4097+` | D9 |
| Δ → 好感 | 判读单独给 `affinity` | `cause` + 映射 + 上限 | `mood_judge:597` | D11/D3 |
| 好感小涨落 | `affinity_log` 只留 50 | `affinity_daily` 按天聚合 | `profiles:387` | D11 |
| 表情阈值收集 | 固定 18 标签 + "吃瓜" | 视觉心情+强度 + 标签治理 | `sticker_receiver:347` | D10 |
| 好感文字块 | 只有数字 | `relation` 块 | `prompt_builder` | D5 |
| 空闲衰减 | 12h 半衰（被架空） | idle 步进 + 文案归零 | `mood.py:89` | D4 |
| 对人安静粒度 | `users[qq]` 全局 | 可选 `gid:qq` | `chat_switch:135` | D1 |

---

## 6. 变更影响矩阵（**防止"只顾一点"**）

| 如果你要改… | 必须同时检查… | 为什么 |
|---|---|---|
| 记忆写入字段 | bond / sticker / 召回排序 / prompt 目录 / 迁移脚本 | 同一个 Δ 三处消费 |
| 心情值口径 | `mood_factors` / `proactive` / `quiet_advisor` / `status_block` / 衰减 | 一处改，四处行为变 |
| 好感 | `intimacy` / `render_card` / `quiet_advisor` / `proactive` / relation 块 | 好感参与 5 处 |
| 判读输出字段 | chat 写回 / memory / profiles / `/测函数 心情` / selftest | 6 个消费方 |
| 回不回的判定 | 5 个决策器 + 所有闸门（C1） | 改一个必漏一个 |
| 发送层 | 长文转发 / @ / 引用 / 配额 / 气泡 / 表情补发 | 20 字红线 |
| 表情标签 | 收集 / 发送 / 协议 / 导入 / 清理 | 标签生态 |
| 上下文来源 | `dialogue_log` / `recent_group_msgs` / `group_scene` / `conv_state` / 审核 | 7 份副本（C2） |
| 关键路径加 LLM | 延迟预算 / token 成本 / 并发 | C4 已经 3 次串行 |
| 插件优先级 / block | `_need_reply` 的副作用（C6） | 上游 block 会丢上下文 |
| 算力机相关 | `node.py` + `node.env` + manifest + caps | 两端都要改 |

---

## 7. 决策清单

| ID | 问题 | 建议 | 状态 |
|---|---|---|---|
| D1 | 对人安静：全局 / 只在当前群 | 待定 | ⏳ |
| D2 | 好感慢回归 | 每天 -0.005 | ⏳ |
| D3 | 好感累加上限 | 日净 ±0.15，单次 +0.08/−0.06 | ⏳ |
| D4 | 空闲衰减 | 线性 1 点/5min，空闲 10min 后 | ⏳ |
| D5 | 好感文字块 | 模板 + 现有数据 | ⏳ |
| D6 | 记忆 valence | 不需要（Δ 符号即 valence） | ✅ |
| D7 | Δ 驱动加粗/检索（不是记录门槛） | 是；记忆二维，都要记 | ⏳ |
| D8 | 加粗档位 | 💥 0.10 / 💥💥 0.25 / 💥💥💥 0.50 | ⏳ |
| D9 | 记忆全记 / 档案只记好感事件 | 记忆每 turn 一条；档案 \|Δaff\|≥0.01 | ⏳ |
| D10 | 表情阈值收集 | 是（≥0.25 自动收） | ⏳ |
| D11 | 判读删 `affinity`，改 `cause`+Δ 推导 | 是 | ⏳ |
| **D12** | 好感「小涨落」的聚合粒度 | 按天聚合 + 显著事件单独留 | ⏳ |

---

## 8. 路线图（每阶段可独立验收）

| 阶段 | 内容 | 验收 |
|---|---|---|
| **0 止血** | 轮换 key、`.dockerignore`、git init、napcat 白名单 | 镜像里无密钥；`git log` 有快照 |
| **1 时序** | memory 先 pending，judge 回 patch Δ | `/测函数 心情` 能看到「这句 → Δ=+0.31」 |
| **2 存储** | 索引迁 sqlite + FTS5（**全记的硬前提**） | 2383 条迁移后可读；单条查询 < 10ms |
| **3 记录** | title/mind/delta/salience；记忆全记；档案只记好感事件 | 目录按时间倒序；旧数据兼容 |
| **4 好感** | `cause` + Δ 映射 + 日上限 + daily 聚合 | 20 分钟内好感不会饱和；环境心情不加好感 |
| **5 表情** | 视觉「心情+强度」+ 阈值收集 + 标签合并 | 新标签不超过 N；旧标签仍可发 |
| **6 呈现** | relation 块 + 空闲衰减 | prompt 里能看到"底色"；空闲 4h 后心情≈0 |
| **7 收敛** | `_need_reply` 副作用外移、决策器收敛 | block 不丢上下文；5 个决策器合成 1 个 |

---

## 9. 文档索引（哪些可信）

| 文档 | 状态 | 说明 |
|---|---|---|
| `docs/master-design.md`（本文） | ✅ 权威 | 总纲，先看这个 |
| `docs/architecture-map.md` | ✅ 可信 | 拓扑/分层/数据/问题定位 |
| `docs/mood-affinity-design.md` | ✅ 可信 | 心情/好感/记录模型（含 §8/§11 新提案） |
| `docs/review-2026-11-20.md` | ✅ 可信 | 全项目 bug 清单（C/B 编号） |
| `docs/agent-pipeline.md` | ❌ 过期 | 多处与代码不符（如"判读影子运行"） |
| `docs/harness_design.md` / `harness_stack_selection.md` | ❌ 过期 | 早期方案 |
| `docs/maibot-analysis.md` | 📚 参考 | 外部项目分析，不是本系统设计 |
| `MEMORY.md` / 日记 | ✅ 可信 | 决策与踩坑记录 |

---

## 10. 实现前自检 10 问

1. 这个功能写在哪一层？会不会产生**第二份真相**？
2. 失败了会不会被 `except` 吞掉？日志里能不能看见？
3. 改了状态，**谁读它**？把所有消费方列出来（对照 §6）。
4. 会不会增加**关键路径**延迟/token？（C4 已经 3 次串行）
5. 是否违反 §3 的红线（20 字 / 工具只收不发 / 正则猜语义）？
6. 旧数据怎么办？需要迁移还是兼容读？
7. 重启后还在吗？内存状态要不要落盘？
8. 群隔离 / 人隔离做了吗？（`group_ok` / `about_qq` / `users` 桶）
9. 有没有对应的自检 / 审计项？（selftest + audit_pipeline）
10. 算力机要不要一起改？（`node.py` / `node.env` / manifest / caps）

---

## 11. 一句话

> 系统的理念（五层自我、按需拉取、静默权、破冰、好感×心情）是完整的；
> 缺的从来不是想法，而是**一份权威的数据模型 + 一条权威的处理链**。
>
> 这份总纲的作用，就是在你下一次专注某个细节时，提醒你它连着哪些地方。

---

# 11. 实施状态（2026-11-20）

## 已完成

| 能力 | 落地 | 自检 |
|---|---|---|
| 功能链路修通（C1/A1/A1b/A2/A3/B1/D1/G4/E2/G5） | `plugins/chat/__init__.py`、`services/text.py`、`services/mood.py` | `scripts/audit_features.py` 16/16 PASS |
| 安全止血 | `.dockerignore` 加 `.env`、删硬编码 key、NapCat 白名单、send 路径白名单、`git init` | `git check-ignore .env` |
| sqlite 记忆基座 | `services/memdb.py`（FTS5 + 降级）、`scripts/migrate_memory_json.py` | `scripts/test_new_features.py` |
| 记录模型 v2 | turn upsert/patch、`Δ` 三处用、判读 `cause`、档案只记好感事件 | 同上 |
| 二维检索 | BM25 降为加分、目录默认时间倒序、`gid` 收口 | 同上 |
| relation 块 | `profiles.relation_block` + `prompt_builder` 注入 | 预算表已加 `relation` |
| 心情空闲衰减 | `mood._idle_decay` / `mood.touch`，归零清文案 | — |
| 表情阈值 | 视觉三行（心情/强度/描述）、`|强度|>=0.25` 收、`resolve_tag` 合并 | — |
| 能力开关 | `services/skill_switch.py`、`/技能`、工具闸门 | 18 项自检 PASS |
| 用量归因 | `services/llm_usage.py`、13 处 purpose 埋点、`/用量` | 报表含 Top/Bottom |
| 工程结构化 | `core/`（pipeline/config/llm_gateway）、`scripts/check_deps.py` + baseline | 依赖检查通过 |
| 插件商城 | `services/plugin_registry.py`、`scripts/plugin_cli.py`、9 个 `UPSTREAM.md` | `plugin_cli list/doctor` |
| 开源发行 | `scripts/make_dist.py`（脱敏 5 处、compileall PASS）、`.env.example`、README/CREDITS/LICENSE/CI | 生成到 `/tmp` 验证 |

## 仍待处理（已知）

1. `plugins/chat/__init__.py` 仍是 8600+ 行；`core/pipeline` 只做了注册点，`_session_worker` / 命令 / 工具尚未物理拆分。
2. `scripts/dep_baseline.txt` 里有 32 条历史交叉依赖（`services/llm.py` → `plugins.chat` 等），需要逐步消除。
3. 第三方插件 `UPSTREAM.md` 的上游地址/许可证多为「待确认」，发布前必须人工核对。
4. 生产库迁移未执行（脚本已就绪，先在 `/tmp` 验证过 dry-run）；切换需运维执行 `MEMORY_BACKEND=sqlite`。
5. `node.py` 与 `data/` 位置按约定未动。
