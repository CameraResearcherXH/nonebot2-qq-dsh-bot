# 收发件人标记设计（v1 + v2 草案）

> **导航**：[设计主文档](DESIGN.md) ｜ 相关：[流水线](pipeline-map.md) · [情绪](emotion-dynamics-design.md) · [检索](retrieval-design.md) · [识图](vision-pipeline.md)
> **本文回答**：群里「谁 → 谁」怎么判、置信度怎么用、它该影响哪些模块；
> **v2（§7，2026-11-27）**：怎么从「显示对了」走到「接得准」—— 收件人分级 → 动作、
> 话题漂移、意图类型、图片消息的收件人（ImageReport）、生成后自检、预算与验收。
> **本文不回答**：心情公式、检索实现、OpenCV/OCR 的内部实现（见 vision-pipeline.md）。

---

## 0. 结论

> **收发件人不是「显示格式」，是整条链路的一等公民。**
> 它决定：这句话是不是对她说的、这件事该记到谁头上、好感该记到谁头上、她该引哪条。

现状：**已经算出来了，但只用在了展示上**。
- 计算：`group_scene._infer_addressee()`（@/引用/称呼/点名/问答相邻/「你」/反应词，7 级信号 + 置信度）
- 展示：`segments.format()` 渲染成 `[i] ⭐#mid 谁 → 你/某人/大家：文本`；`addressee_line()` 给 prompt 一行
- **没用上**：判读归因仍是 `about_qq = speaker_qq`；记忆 `about_qq` 靠 LLM 自己填；关系更新没有「收件人」概念。

---

## 1. 现有实现（as-built）

### 1.1 数据结构（`segments.push`）

```python
{
  "mid": "100000204", "gid": "...", "name": "芒果", "uid": "100000012",
  "text": "...", "ts": 1795...,
  "mentioned": False,          # 是否 @ 她 / 引用她
  "reply_id": "...",           # 引用的消息号
  "to_kind": "bot|person|group|unknown",
  "to_name": "老大", "to_qq": "100000003", "to_conf": 0.8,
}
```

### 1.2 推断顺序（`group_scene._infer_addressee`）

| 顺序 | 信号 | `to_kind` | 置信度 | 备注 |
|---|---|---|---|---|
| ① | @ / 引用（显式） | bot / person | 0.99 / 0.95 | 历史号也认 |
| ② | 开头称呼「X，…」「X 你…」 | person | 0.80 | `_vocative` |
| ③ | 正文点名 | person | 0.70 | 长名字优先 |
| ④ | 上一句提问 → 这句回答 | person | 0.65 | 120s 内 |
| ⑤ | 用了「你」（非「你们/你俩」） | person | 0.60 | 上一句的人 |
| ⑥ | 反应词 / 附和 / 明显接话 | person | 0.55 | 短句 |
| ⑦ | 短句冲最近被提到的人 | person | 0.50 | 「找打了」这类 |
| ⑧ | 没信号 | unknown | 0 | 不要硬认成「大家」 |

### 1.3 展示规则（`segments.addressee_of`）

```text
→ 你            （mentioned 或 to_kind=bot）
→ 老大          （person 且 conf ≥ 0.5）
→ 大概老大      （person 且 conf < 0.5）
→ 老大?         （unknown 但有候选）
→ 大家          （真的没信号）
```

截断规则：`_pick_rows` **绝不把 ⭐（→ 你）截掉**；段末再列一遍「必须回应」。
这条是 2026-11-23 真机事故换来的（@ 她的那条被挤出窗口，她只回了最后一条）。

---

## 2. 现在的问题（为什么「显示对了」还不够）

| # | 问题 | 后果 |
|---|---|---|
| A1 | 判读归因用 `speaker_qq`，不用 `to_qq` | 「A 对 B 说难受的事」被记到 A 头上；B 的关系没动 |
| A2 | 记忆 `about_qq` 由 LLM 自己填 | 同一件事有时记成说话人、有时记成被说的人 |
| A3 | 一条消息只能有一个收件人 | 「你们仨都来」只记一个人 |
| A4 | 她的**发出**消息没有落成边 | 学不到「她对谁说话多/什么语气」 |
| A5 | 线程 `thread_id` 只在内存 | 重启后现场空，收件人/线程丢失 |
| A6 | 置信度没有校准 | 不知道 0.7 到底准不准；0.8 断言可能错 |
| A7 | 「你」出现在**引用文本**里也计入 | 引用了别人的「你」→ 误判收件人 |
| A8 | 昵称相似（小明 / 小明哥）只靠最长匹配 | 仍可能认错，且没有反馈闭环 |

---

## 3. 目标设计

### 3.1 统一结构：`Address`

```python
Address = {
  "from_qq": "100000012",
  "to_qqs": ["100000003"],        # 支持多收件人
  "to_kind": "bot|person|group|unknown",
  "conf": 0.80,
  "signals": ["vocative", "second_person"],   # 命中了哪些，便于调试
  "anchor_mid": "100000204",       # 引用/被 @ 的原消息
  "thread_id": "t_100000016_42",
  "ts": 1795...,
}
```

### 3.2 下游怎么用（这是本文的重点）

| 模块 | 现在 | 改成 |
|---|---|---|
| **prompt 锚点** | 一行 `addressee_line` | `anchor` 结构化：被引原文紧贴当前消息，只给一次 |
| **心情判读** | `about_qq = speaker_qq` | `target ∈ {speaker, recipient, other, world, self}`；`about_qq` 由 target 决定 |
| **记忆归因** | LLM 自由填 | 默认 `about_qq = recipient`（如果是关于对方的事），LLM 只能改不能空 |
| **关系更新** | 只按 speaker | 只有 `target ∈ {speaker,recipient,other}` 才动对应人的 affinity/trust |
| **主动/安静** | 没有对象 | 「找谁倾诉」直接用 `interaction_edges` 的信任排序 |
| **引用回复** | 靠模型写 `<reply:mid>` | 分气泡时每个气泡带自己的 `anchor_mid` |
| **社交图** | 无 | `interaction_edges` 落盘，可查「谁常跟谁说话」「谁对谁语气差」 |

### 3.3 新增两张表（进 sqlite，和记忆同库）

```sql
-- 谁 → 谁 的每一次交互（可聚合、可回放）
CREATE TABLE interaction_edges (
  id TEXT PRIMARY KEY,
  ts REAL, gid TEXT,
  from_qq TEXT, to_qq TEXT,          -- to_qq 为空 = 对大家
  kind TEXT,                         -- msg / bot_reply
  signal TEXT,                       -- at / reply / vocative / name / answer / you / adjacent
  conf REAL,
  sentiment REAL,                    -- -1~1，来自判读（可空）
  thread_id TEXT
);
CREATE INDEX idx_edge_from ON interaction_edges(from_qq, ts);
CREATE INDEX idx_edge_to   ON interaction_edges(to_qq, ts);
CREATE INDEX idx_edge_gid  ON interaction_edges(gid, ts);

-- 线程（重启可恢复）
CREATE TABLE threads (
  id TEXT PRIMARY KEY, gid TEXT,
  started_at REAL, last_at REAL,
  topic TEXT, participants TEXT,     -- JSON 数组
  summary TEXT
);
```

### 3.4 判定规则（在现有基础上补 3 条）

1. **引用优先于正文**：有 `reply_id` 时，收件人 = 被引用那条的发送者（除非正文明确点名别人）。
2. **「你」只在正文里算**：先把 `〔引用 …〕` 段落剥掉再匹配（修 A7）。
3. **多收件人**：`@A @B`、或正文同时点名两人 → `to_qqs=[A,B]`，`to_kind=person`，`conf` 取较低值。

### 3.5 置信度校准（补 A6）

用 100 条人工标注（谁 → 谁）算两件事：

```text
accuracy(conf ≥ 0.8)   目标 ≥ 0.95   # 断言要准
accuracy(0.5 ≤ conf<0.8) 目标 ≥ 0.75 # 「像是在回」可以错一点
覆盖率（有明确收件人的比例） 目标 ≥ 0.60
```

校准方法：按 `signal` 分组统计准确率，把置信度改成**经验准确率**（isotonic / 分桶），
而不是拍脑袋的 0.7/0.8。

---

## 4. 失败模式与硬规则

| 场景 | 规则 |
|---|---|
| 引用的是图片/表情 | `anchor` 带视觉摘要 + 标「这条是图」；正文没说图就不自动引 |
| 群里两个人同名 | 长名字优先；仍不确定 → `unknown`，不硬认 |
| 她自己的历史号 | 统一映射到人格 ID（`_self_id()`） |
| 「你们」「你俩」 | 不算单收件人，`to_kind=group` |
| 一条消息回三个人 | `to_qqs` 三个，回复时**分气泡**，每气泡各自 anchor |
| 纯表情/纯图 | 跳过收件人推断，`unknown` |
| 置信 <0.5 | prompt 里写「不确定」，禁止断言「→ X」 |

---

## 5. 和流水线的接口

```text
① 接入：拿到 mid / reply_id / at 列表 / 文本
② 规则层：group_scene.observe() → Address（含 signals/conf/thread）
③ 分段：segments.push(to_qqs, to_conf, thread_id, anchor_mid)
④ 取数：addressee_line() + anchor 原文（只给一次）
⑤ 生成：分气泡时每气泡带 anchor_mid
⑥ 输出：她发出的消息也写一条 interaction_edges(kind=bot_reply)
⑦ 判读：target/recipient 一起给 judge；记忆 about_qq 默认 recipient
```

---

## 6. 分阶段

| 阶段 | 做什么 | 验收 |
|---|---|---|
| A0 | `to_qqs` 多收件人 + 剥引用再匹配「你」 | 现有 8 条规则单测通过 |
| A1 | `Address` 进判读/记忆归因（target 三选一） | 「A 对 B 说」记到 B 的比例 ≥80% |
| A2 | `interaction_edges` + `threads` 落盘 | 重启后现场可恢复 |
| A3 | 100 条标注校准置信度 | `conf≥0.8` 准确率 ≥95% |
| A4 | 社交图查询（谁常跟谁说话/语气） | `/测函数 关系 <qq>` 可看 top 对象 |

---

# 7. v2 设计（2026-11-27）：从「显示对」到「接得准」

> 用户原话：「还是容易接偏消息或者词不达意；希望更准确、更有人味、更智慧、更幽默。」
> **幽默本轮不做**：persona 卡是固定区、吃前缀缓存，输入 token 现在刚好，
> 加 few-shot 会顶掉别的块 → 等 §7.10 的 token 实测有富余再谈（见 §7.11）。
>
> 本节经过一次「锐评」（§7.12），初稿里 10 处想当然的地方已经改掉，改法写在表里。

## 7.0 大纲

```text
7.1  目标 / 边界（level 只作用于「主动插话」这条路）
7.2  总体图
7.3  收件人四级 → 动作矩阵（L0/L3 已有，v2 只补 L2）
7.4  话题漂移检测（零模型调用；比较对象是「当前句 vs 前文」）
7.5  意图类型 kind（并入 atmosphere，同一次调用）
7.6  图片消息：ImageReport 单一事实源（P0 不删「文字」行）
7.7  自检：L0 机械修正 + 异步 LLM 自检（不阻塞发送）
7.8  预算（先量基线，再定目标）
7.9  开关 / 回滚 / 可观测
7.10 验收指标
7.11 落地顺序 + 不做清单
7.12 锐评记录（10 条）+ 8 条硬约束自检
```

## 7.1 目标 / 边界

| | 内容 |
|---|---|
| **目标 1** | 「接偏」下降：答窗口外旧问题、把别人的对话当成对自己的、把吐槽当提问 |
| **目标 2** | 图片消息「看懂」：同一张图不再被读 2~6 遍；图里的字能进收件人判断 |
| **目标 3** | 词不达意有兜底：**机械问题当场修**，语义问题异步复盘、下一轮回填 |
| **边界（重要）** | `level`（§7.3）**只作用于「主动插话」这条路**（`_random_chat_loop`）。正常回复（已被 @ / 段已入队）不经过 level 过滤 —— 因为那时已经决定要回了，再拦就是「@ 她也不理」 |

为什么划这条边界（锐评 #1）：

- `atmosphere.need_reply` 是在 `reply_pipeline` 里算的，**那时消息已经进队列**，拦不了；
- L0（⭐ 必回）和 L3（对别人不回）今天**已经实现**（`_random_chat_loop` 里
  `to_kind=person & conf>=0.5 → continue`），v2 只是把它们统一成一个 `level_of()`；
- 真正新增的只有 **L2 观望**（`unknown` 不再硬插话），所以 §7.11 里它排在 P2、默认关。

## 7.2 总体图

```text
群消息（文本 / 图片 / 表情）
   │
   ├─(图片)──► ImageReport（§7.6，典型 1 次视觉 + 按需第 2 次）──┐
   │                                                            │
   ▼                                                            ▼
group_scene.observe() ── 8 级硬逻辑 ──► Address{to_kind, to_conf}
   │
   │  ┌───────────── 只在「主动插话」这条路 ─────────────┐
   │  ▼                                                  ▼
   │ level = level_of(to_kind, to_conf)            正常回复（@她/段已入队）
   │  ├─ L0 必回  → 不进插话（已有回复在跑）             │
   │  ├─ L1 可回  → atmosphere.need_reply 决定           │
   │  ├─ L2 观望  → 本轮跳过，靠 tick 冷却形成延迟        │
   │  └─ L3 不回  → 只更新上下文（已有）                  │
   │                                                     │
   ▼                                                     ▼
segments.format ──► reply_pipeline ──► 生成 ──► L0 机械自检 ──► 发送
                                                      └─ 异步 LLM 自检 → 落盘 → 下一轮 prompt 回填
```

## 7.3 收件人四级 → 动作矩阵

```python
def level_of(to_kind: str, to_conf: float) -> str:
    k = (to_kind or "").lower()
    if k == "bot":
        return "L0"                    # ⭐：@她 / 引用她 / 喊名
    if k == "person" and to_conf >= 0.5:
        return "L3"                    # 「→ 某人」：明确对别人
    if k == "group":
        return "L1"                    # 「→ 大家」：公开场合
    return "L2"                        # unknown / person conf<0.5（「→ 不确定」）
```

| 级别 | 判定（现有信号） | 例子 | 动作 | 现状 |
|---|---|---|---|---|
| **L0** | `to_kind=bot` | 「鱼宝，这配置咋样」 | 必回（由正常回复路径处理，**插话循环不进**） | ✅ 已有 |
| **L1** | `to_kind=group` | 「这破服务器又崩了」 | 可回：`atmosphere.need_reply` + `ATMO_GATE` | ✅ 已有（闸门默认关） |
| **L2** | `unknown` / `person` conf<0.5 | 「那个谁…」「？」 | **观望**：本轮跳过；下一 tick（≥冷却时间）再看 | 🆕 v2 |
| **L3** | `person` conf≥0.5 | 「A → B：晚安」 | 不回，只更新上下文 | ✅ 已有 |

> 与 `view_frequency` 的分工不变：**level 管「该不该」，频率管「今天说太多了没有」**。
> 顺序：level 过滤 → `_vf.decide()` 抽签 → 问模型「要不要说」。

## 7.4 话题漂移检测（零模型调用）

**要解决的**（11-27 复盘 A1，确证）：她在答 8 分钟前的「锁 90 / 帧数」，群里已经聊到「李李诋毁」。

```text
【要判断的这句】 ──┐
                   ├─ 2-gram Jaccard（去标点/停用词）─┐
前 3 条 + summary ─┘                                 ├─► overlap
atmosphere.topic（若有）─────────────────────────────┘        │
                                                             ▼
                                          topic_shift = overlap < 0.18
```

**比较对象必须是「当前句 vs 前文」**（锐评 #3）：不能拿「她的回复 vs 当前句」——
「你会不会唱歌」→「会啊，想听啥」重合是 0，但这是完美回答，不是漂移。

| 项 | 取值 | 说明 |
|---|---|---|
| 相似度 | 字符 2-gram Jaccard | 中文不用分词，零依赖、<1ms |
| 参照物 | 段内前 3 条 + `group_scene.snapshot()["summary"]` | summary 已有 |
| 阈值 | **0.18（初值，必须用 trace 标定）** | 真漂移 vs 换说法（「锁90」→「帧数」） |
| 兜底 | `atmosphere.topic` 与当前句有重合 → 判为不漂移 | 防同义换说法误判 |
| 输出 | `topic_shift: bool` → prompt 一行 + L1/L2 降一级 | `/测函数 话题` 可看 |

> 开关 `ADDR_TOPIC_SHIFT` **默认 0**；宁可漏判，不可误判 —— 误判会让她突然说
> 「话题换了」，比接偏更怪。

## 7.5 意图类型 `kind`（并入 atmosphere，同一次调用）

| 值 | 判定线索 | 回复策略 |
|---|---|---|
| `ask` | 有疑问词/问号，且真在要答案 | 先正面答，别绕 |
| `vent` | 抱怨/丧/自嘲，没有求解意图 | 先共情一句，**不给方案** |
| `meme` | 玩梗/抽象/接龙/表情包 | 顺着梗升级或冷处理，不解释 |
| `statement` | 陈述事实/播报 | 可只附和一句，或不接 |
| `comfort` | 别人在安慰/哄人/道歉 | 接情绪，短，慢一点 |
| `command` | 指令/玩法（已有正则拦） | 不进对话 |

实现约束：

- **零新增请求**：加进 atmosphere 的 JSON schema，输出 +8 token；
- **零新增存储**：只活在 `_intent` / `_CACHE`，不写回 `segments`；
- 渲染一行：`· 这句是：吐槽（先共情，别给方案）`（≤22 字）；
- 规则兜底只用于「模型没给 kind」的降级（`？`→ask、含「烦/累/难受/哭了」→vent、
  纯表情→meme、其余 statement）—— **不作为主判据**（11-18 的教训：别用正则猜语义）。

## 7.6 图片消息：ImageReport 单一事实源

### 7.6.1 现状（as-built，2026-11-27 实测配置）

```text
process_image → vision_verify.recognize()
  ① trace.moe（1 次网络）+ PicImageSearch（并行，超时 10s）
  ② qwen3-vl-plus 视觉 1 次   ← prompt 含「文字：逐字照抄，最多 150 字」
  ③ GLM 视觉 1 次（GLM_VISION=true）← 同一份 prompt，再抄一遍
  ④ 没认出专名 → _targeted_ask 再问 1 次
她再自己调 ocr_image → OpenCV 增强 + 最多 4 次视觉（整图 + 3 个裁块）
= 同一张图 2~6 次视觉调用
```

> `VISION_LLM_MAX_PER_HOUR=12` 只限制「进决策队列让她想」，**不限制识图**；
> `IMAGE_DEDUP_HOURS=24` 才是同一张图不重复识。

标签/收件人这边：`group_scene.observe(text=f"[图片] {desc[:60]}", no_audit=True)`
—— 只拿到 60 字摘要、且不送审核。

### 7.6.2 契约：`ImageReport`

```json
{
  "sha1": "…", "w": 1080, "h": 1920,
  "sharpness": 38.2, "quality": "clear|slightly_blur|blur",
  "has_text": true, "text_ratio": 0.07,
  "text_blocks": [{"text": "…", "conf": 0.86, "source": "vlm|ocr"}],
  "kind": "screenshot|meme|photo|art",
  "scene": "…", "emotion_tone": "搞笑|悲伤|求助|炫耀|日常",
  "char": "", "work": "", "char_conf": 0.0,
  "sources": ["qwen", "trace", "picsearch"],
  "cost_ms": 2100, "partial": false
}
```

| 字段 | 产出方 | 备注 |
|---|---|---|
| `sharpness/quality/has_text/text_ratio` | **OpenCV** | 确定性、毫秒级、不幻觉 |
| `text_blocks` | **RapidOCR（P1 起）**，VLM 兜底 | `source=ocr` 原文照抄、可当事实；`source=vlm` 只当线索 |
| `kind/scene/emotion_tone` | 视觉模型（1 次） | P1 起文字行改成「只抄最关键 1 句」（全文交给 OCR） |
| `char/work/char_conf` | trace + 视觉（+按需第二家） | |
| `partial` | 管线 | 超时/降级为 true |

### 7.6.3 一次产出、三处复用

```text
图片 bytes
  → OpenCV：sharpness(Laplacian) + text_ratio(形态学) ──┐
  → 有字才 OCR：RapidOCR（线程池，与视觉并行）─────────┤
  → 视觉 1 次（画面 / 类型 / 关键 1 句）────────────────┤
  → 按需：专名缺失才问第二家 ────────────────────────┤
                                                     ▼
                 ImageReport（**复用现有 fingerprint 缓存**，只扩字段）
                     │
     ┌───────────────┼────────────────────┐
     ▼               ▼                    ▼
 fake_text      收件人判定            记忆 / 检索
（角色扮演）   （caption + 字 + 上下文）
```

### 7.6.4 图片的收件人判定（补 §1.2 的第 9 级信号）

1. **显式**：@她 / 引用她 → `bot`，0.99；
2. **图配文**：caption 点名/@ 了谁 → `person`，0.80（复用现有文本信号）；
3. **图里的字**：`text_blocks` 命中群成员昵称/@ → `person`，0.65（**P1 起才用**，P0 的 vlm 文字只当线索）；
4. **上下文**：上一条是「A → B」且图紧跟 → `person`，0.55（`image_target` 已有）；
5. 否则 `unknown`（不硬认成「大家」）。

降级：OCR 失败/超时 → 只用 1+2+4；`partial=true` 时 prompt 写「图里的字没看清」。

### 7.6.5 标签 LLM 复用（有门槛，不是每张图都送审）

锐评 #8：今天 `no_audit=True` 是为了省钱，改成「全送审」等于每张图多一次审核调用。

```python
should_audit = report["has_text"] and re.search(r"@|「.{1,8}」", report_text)
```

只有「图里有字，且字里出现 @ 或引号里的称呼」才送审；其余保持 `no_audit=True`。

### 7.6.6 P0 的取舍（锐评 #5）与 P1 的收尾

**P0 不删视觉 prompt 里的「文字」行**。理由：服务器还没有 OCR 引擎，
删了之后「聊天截图 / 题目截图」会直接变成瞎子，要等 P1 装 RapidOCR 才补得回来。
P0 只做：OpenCV 质量数值化 + GLM 第二意见改按需 + report 结构 + 标签复用。

**P1（2026-11-27 已落地）**：RapidOCR 实测能在 2 vCPU 轻量服务器上跑（见 §7.13），
所以视觉 prompt 的文字行从「逐字照抄 150 字」改成「只抄最关键 1 句（≤30 字）」，
全文由 OCR 给原文。这样同时省 output token、也避免 VLM 改写原文被当事实。
OCR 关闭（`VISION_OCR=0`）时自动退回「逐字照抄」，能力不掉。

## 7.7 自检：L0 机械修正 + 异步 LLM 自检

锐评 #6：`atmosphere` 一次调用实测 1.5s 量级，**300ms 的同步 LLM 自检不现实**。
所以拆成两条：

### 7.7.1 L0（同步，<10ms，每条都跑，只做「机械修正」）

| # | 检查 | 判据 | 动作（只能是机械的） |
|---|---|---|---|
| 1 | 协议残留 | 可见正文出现 `[[` / `<sticker:` / `\|\|\|` | `clean_outgoing()` 剥掉 |
| 2 | 整条复读 | 与最近 3 条自己消息的 3-gram Jaccard ≥0.8 | **这条不发**（多气泡时只丢这一泡） |
| 3 | 长度异常 | 群聊文字泡 >40 汉字 | 走现有合并转发（不截断） |
| 4 | 问句未答（**只标记**） | 对方有 `？` 且回复只有「嗯/哈哈/草/？」 | 记 `[SELFCHECK] flag=unanswered`，**不自动补话** |
| 5 | 数字/专名缺失（**只标记**） | 对方句里有数字/英文专名，回复里一个都没有 | 同上 |

> 「自动补一句正面回答」「换个开头重说」都不是规则能做的 —— 那是重生成，
> 见 7.7.2。初稿把它们写成 L0 的动作，是错的（锐评 #4）。

### 7.7.2 异步 LLM 自检（不阻塞、不撤回）

```text
发送成功
   └─ 后台任务（低频：每会话每 10 分钟最多 1 次）
        ├─ 取「要回的那句 + 实际发出的话」
        ├─ 3 行判分（aligned / in_character / repeat）
        ├─ 落 data/selfcheck.jsonl（离线看 badcase）
        └─ 分数低 → 写 data/selfcheck_hint.json（会话级）
             └─ 下一轮 prompt 末尾加一行：「上一句你说得有点客服腔，注意」
```

- **零延迟**：全部在发送之后；
- **不撤回**：撤回比说错更伤；
- 开关 `REPLY_SELFCHECK_ASYNC` 默认 0，先只落盘不回填；跑一周看命中率再开回填。

## 7.8 预算（先量基线）

| 环节 | 现在 | P0 | 说明 |
|---|---|---|---|
| 视觉（每图） | 2~6 次 VLM | 典型 1，P95 ≤2 | GLM 改按需；文字行保留 |
| OCR | 0（旁路工具） | 0（本地推理，不占模型预算） | P1 已装 RapidOCR；只在线程池跑、有字才跑 |
| atmosphere | 1 次 | 1 次（出 +8 token） | 加 `kind` |
| 主生成 | 1 次 | 1 次（prompt 净增 ≤60 字） | 两行短句 |
| L0 自检 | 无 | <10ms | 纯规则 |
| 异步自检 | 无 | 0（后台） | 不进主链路 |

**输入 token 硬约束**：`text_blocks` 进 prompt **最多 60 字**（全文只留缓存/report）；
新增两行短句 ≤60 字。有字图净增 ≤120 字，无字图净增 0 —— 超了就继续截。

**E2E 基线**：`[VISION]` 日志里记 `cost_ms`，P0 上线后先量一周再定目标（不写死 6s）。

## 7.9 开关 / 回滚 / 可观测

| 开关 | 代码默认 | 作用 | 回滚 |
|---|---|---|---|
| `VISION_REPORT` | **1** | 走 ImageReport 单次产出（等价重构） | 0 = 旧 `recognize` 路径 |
| `VISION_OCR` | **1** | 图里的字优先用 RapidOCR（原文照抄） | 0 = 退回视觉模型识字 |
| `OCR_RAPID_MAX_SIDE` / `OCR_RAPID_MIN_CONF` / `OCR_RAPID_TIMEOUT` / `OCR_RAPID_THREADS` | 1024 / 0.5 / 8s / 2 | RapidOCR 精度/耗时取舍 | 调小 `OCR_RAPID_MAX_SIDE` 换速度 |
| `VISION_SECOND_MODE` | `on_miss` | 第二视觉源策略（**不配 `VISION_SECOND_PROVIDER` 就关闭**） | `off` |
| `VISION_GLM_MODE` | `on_miss` | ⚠️ 旧名，只做 .env 兼容 | 用 `VISION_SECOND_MODE` |
| `ATMO_KIND` | **1** | atmosphere 输出 `kind`（只加一行） | 0 |
| `ADDR_LEVEL_ACTION` | **0** | L2 观望真正生效 | 0 = 今天的行为 |
| `ADDR_TOPIC_SHIFT` | **0** | 话题漂移检测 | 0 |
| `REPLY_SELFCHECK` | **0** | L0（1）/ 关（0） | 0 |
| `REPLY_SELFCHECK_ASYNC` | **0** | 异步自检落盘 / 回填 | 0 |

日志（每步一行，方便 ssh 看）：

```text
[ADDR] gid=… level=L2 kind=statement shift=1 → 观望
[VISION] report sha=… sharp=38 text=ocr:12字 2nd=off cost=1.8s partial=false
[VISION] 🔤 OCR 4 块：Daily Report 2026-11-27 / token used …（OCR 与 VL 并行，不额外等）
[SELFCHECK] L0 hit=protocol；flag=unanswered
[SELFCHECK-ASYNC] aligned=1 in=2 rep=2 → hint 已写
```

## 7.10 验收指标

| 指标 | 基线 | 目标 | 怎么量 |
|---|---|---|---|
| 接偏率（人工 30 条） | 11-27 复盘抽样 | ≤10% | 人工，同 `reply-audit` 格式 |
| 图片「看懂了」badcase | 待标 | 周环比 −30% | 群里发图后答非所问的条数 |
| 视觉调用次数/图 | 2~6 | 典型 1 / P95 ≤2 | `[VISION]` 计数 |
| L0 自检耗时 | — | <10ms | 日志 |
| 群聊回复率 | 现状（先埋点） | 不低于现状 −5% | `[PIPE]` 条数 / 群消息数 |
| 输入 token/轮 | 现状 | 净增 ≤120 字（有字图） | `/测函数 上下文` |

验证顺序：

1. 本地 `check_runtime_wiring` 断言（`level_of` / report 字段 / `kind` / L0 规则）；
2. 服务器 `verify_all --full`；
3. 真机 1 小时：`ssh` 看 **napcat log**（发出去的是什么）+ **bot log**（`[ADDR]/[VISION]/[SELFCHECK]`）；
4. 抽 30 条人工判「接偏/词不达意」，和 11-27 基线比；
5. 图片专项：连发 5 张（纯图 / 带字截图 / 表情包 / 糊图 / GIF）。

## 7.11 落地顺序 + 不做清单

| 阶段 | 内容 | 默认 | 风险 |
|---|---|---|---|
| **P0** | ImageReport（质量数值化 + GLM 按需 + 标签复用 + 缓存扩字段）+ `kind` + L0 机械自检 | `VISION_REPORT=1`、`VISION_GLM_MODE=on_miss`、`ATMO_KIND=1`、`REPLY_SELFCHECK=0` | 低（等价重构 + 纯规则） |
| **P1** | ✅ RapidOCR 已接入（2026-11-27，见 §7.13）：OCR 与 VL 并行、视觉文字行改「关键 1 句」；第二视觉源改可配 provider（默认 qwen，GLM 可选） | `VISION_OCR=1`、`VISION_SECOND_MODE=on_miss`（不配 provider 即关闭） | 低（已实测 2 vCPU 可带动） |
| **P1-b** | 待做：话题漂移（`ADDR_TOPIC_SHIFT`，需先用 trace 标定阈值） | `ADDR_TOPIC_SHIFT=1` | 中（阈值误判） |
| **P2** | L2 观望 + 异步自检回填 | `ADDR_LEVEL_ACTION=1`、`REPLY_SELFCHECK_ASYNC=1` | 中（影响回复率，盯指标） |

**明确不做（本轮）**：

1. 幽默 few-shot 进 persona（输入 token 刚好，会顶掉别的块）；
2. 同步 LLM 自检进主链路（1.5s 量级，不值）；
3. 撤回消息；
4. 让视觉模型自己编排工具调用；
5. 新增设计文档（只改本文 + vision-pipeline.md 一行链接）。

## 7.12 锐评记录 + 硬约束自检

### 7.12.1 初稿里被推翻的 10 处

| # | 初稿的说法 | 问题 | 改法 |
|---|---|---|---|
| 1 | 「L0 必回、不许 stay_silent」 | 与现有行为冲突，且是没开关的行为变更 | 删掉；L0 由正常回复路径处理，v2 不加限制 |
| 2 | 「L1/L2/L3 全链路生效」 | `need_reply` 在流水线内才产生，那时消息已入队，拦不了 | 明确 level **只作用于插话路径** |
| 3 | 「回复 vs 当前句 2-gram 重合=0 → 答非所问」 | 正确回答常常不重复提问的词（「会不会唱歌」→「会啊」），必然误报 | 漂移检测改为「当前句 vs 前文」；答非所问交给异步自检 |
| 4 | 「L0：追加一句正面回答 / 换开头重说」 | 规则做不了生成 | L0 只做机械修正（清洗/丢弃/转发）；生成类交给重生成或异步 |
| 5 | 「P0 就删视觉 prompt 的『逐字抄』」 | 服务器还没 OCR，删了截图类直接变瞎子 | P0 保留文字行，P1 装 RapidOCR 后再删 |
| 6 | 「L1 小模型 ≤300ms」 | atmosphere 实测 1.5s，300ms 不现实 | 改成异步自检（发送后）+ 下一轮回填 |
| 7 | 「每张图都送审核」 | 每张图多一次审核调用，违背「不加请求」 | 加门槛：有字 + 字里有 @/称呼 才送 |
| 8 | 「新建 data/vision/<sha>.json 缓存」 | 已有 fingerprint 缓存，重复造 | 复用现有缓存，只扩字段 |
| 9 | 「text_blocks 最多 120 字进 prompt」 | 与「净增 ≤60 字」自相矛盾 | 进 prompt 截 60 字，全文留缓存 |
| 10 | 「图片 E2E P95 6s」 | 没有基线，属于拍脑袋 | 先记 `cost_ms`，量一周再定 |

### 7.12.2 8 条硬约束（逐条对过）

| # | 约束 | 结论 | 怎么验证 |
|---|---|---|---|
| 1 | 不增加主链路模型调用 | ✅ `kind` 并入 atmosphere；GLM 改按需；OCR 不用 VLM（P1） | `[ATMO]` 1 条/轮、`[VISION]` 1~2 条/图 |
| 2 | 输入 token 净增 ≤120 字（有字图）/ 0（无字图） | ✅ 两行短句 + text_blocks 截 60 | `/测函数 上下文` 前后对比 |
| 3 | 不改现有字段语义 | ✅ `level` 派生；`kind` 只活在 `_intent` | 单测 `segments.push` 结构不变 |
| 4 | 新行为默认关 | ✅ `ADDR_*`/`SELFCHECK` 默认 0；`VISION_REPORT` 是等价重构 | 关开关 = 今天行为 |
| 5 | 超时 fail-open | ✅ 视觉 `partial=true`；异步自检不阻塞 | 断网演练 |
| 6 | 不与既有机制打架 | ✅ 不动 `stay_silent` / `_FAILED_TURN` / 40 字 2 泡 / `view_frequency` | `test_regressions.py` |
| 7 | 可观测 | ✅ 4 行日志 + `/测函数` | 真机抽查 |
| 8 | 可回滚 | ✅ 7 个开关，无数据迁移 | 逐个关掉复测 |

## 7.13 P1 落地记录（2026-11-27 22:00）

### 7.13.1 RapidOCR 在轻量服务器上带得动吗

用户问「轻量服务器可以带动吗」——**可以**。实测机器：2 vCPU / 3.5GB / 根分区剩 3.5GB。

| 指标 | 实测 |
|---|---|
| 首次加载 | 0.5s（懒加载：第一张「有字」的图才吃内存） |
| 常驻内存 | ≈350MB（onnxruntime 会话），峰值 ≈490MB |
| 单图（长边 1024） | 0.7~2.7s（群截图 2.0s / 窄条 2.6s / 照片 1.0s） |
| 磁盘 | 依赖 + 模型 ≈400MB |
| 准确率 | 群截图 4/4 块全对；测试图里塞的 `OCR_TEST_MARKER_42` 也被原样读出（确认是 OCR，不是模型幻觉） |

结论：能带动。bot 200MB + napcat 500MB + OCR 350MB，机器仍有 1.5GB 余量；
但 2 vCPU 上 OCR 会和事件循环抢核，所以**必须**线程池 + 串行锁 + 超时（见下）。

### 7.13.2 接入方式（不增加主链路延迟）

```text
process_image
  → OpenCV quality_of()（毫秒）→ has_text?
        ├─ 有：asyncio.create_task(ocr.read_async())  ┐
        └─ 无：跳过                                   ├─ 并行
  → qwen VL 调用（10s 量级）─────────────────────────┘
  → await OCR 结果（此时早已跑完）
  → rec["text"] = OCR 原文（没有才用 VLM）；rec["ocr_blocks"] 进 report/缓存
```

- `services/ocr_rapid.py`：单例懒加载、`asyncio.to_thread`、串行锁（上一张没完就跳过本张，
  不排队拖慢回复）、8s 超时、置信度 <0.5 丢弃；没装/加载失败/超时一律返回 `[]`（fail-open）。
- `services/vision_verify.py`：OCR 与 VL 并行；文字合并顺序 **OCR > VLM 较长的那个**；
  视觉 prompt 文字行从「逐字照抄 150 字」改成「只抄最关键 1 句（≤30 字）」，省 output token；
  `VISION_OCR=0` 时自动退回旧 prompt（逐字照抄）。
- `services/vision_report.py`：`text_blocks[].source` 区分 `ocr` / `vlm`，
  `render_line` 写「图里文字 N 字（OCR）」或「（识图模型读的，可能不准）」。

### 7.13.3 视觉模型：默认 qwen，第二家变成「可选」

用户拍板：**GLM 额度用完了，之后先靠 qwen 视觉；但保留可选视觉模型的能力。**

| 角色 | 默认 | 说明 |
|---|---|---|
| `vision`（主） | **qwen**（`qwen3-vl-plus`） | 实测约 10s；GLM 想用就写 `VISION_PROVIDER=glm` |
| `vision_fallback`（额度兜底） | glm | 主 provider 1113/429 时自动换；没配 key 自动跳过 |
| `vision_second`（第二证据源） | **关闭** | 不配 `VISION_SECOND_PROVIDER` 就不调；配了才按 `VISION_SECOND_MODE` 问 |

改动：
- `services/providers.py`：`vision` 默认 glm→qwen；新增 `vision_second`（默认空 = 关闭，
  且**不拿 `LLM_PROVIDER` 兜底**，避免用户配了主模型就被动多调一家视觉）；
- `services/vlm.py`（新）：任意 role 的通用视觉调用（OpenAI 兼容，Pillow 压到 1280/JPEG85，
  失败静默返回空）——第二视觉源不再写死 `services/glm.py`；
- `services/vision_verify.py`：`GLM_VISION` → `_second_target()`；日志/报告里的「GLM识图」
  改成实际 provider 名；`VISION_GLM_MODE` 只保留为旧 .env 兼容名。

### 7.13.4 依赖与镜像

- `requirements.txt`：`onnxruntime==1.18.1`（**不能装 1.32**：它强制 numpy>=2，
  imageutils 要求 numpy<2）、`pyclipper`、`Shapely`、`tqdm`。
- `Dockerfile`：`pip install --no-deps rapidocr_onnxruntime==1.4.4` ——
  rapidocr 声明依赖 `opencv-python`（带 GUI，容器缺 libGL 会 import 崩），
  会覆盖 `opencv-python-headless`，所以本体单独 `--no-deps` 装。
- `.env`：`VISION_OCR=1`、`OCR_RAPID_MAX_SIDE=1024`、`OCR_RAPID_MIN_CONF=0.5`、
  `OCR_RAPID_TIMEOUT=8`、`OCR_RAPID_THREADS=2`、`VISION_SECOND_MODE=on_miss`。
  （`OCR_MAX_SIDE / OCR_UPSCALE_MIN_W / OCR_MAX_CROPS / OCR_TIMEOUT` 是 `ocr_lite`
  旁路工具的参数，和 RapidOCR 不是一回事，见 `services/ocr.py`。）

### 7.13.5 验证结果

| 项 | 结果 |
|---|---|
| `scripts/check_tuning.py` | PASS（顺带删掉旧「OpenCV 增强 + 裁剪送模型」遗留键） |
| `scripts/audit_coverage.py --strict` | 0 结构性硬伤 |
| `scripts/check_runtime_wiring.py` | 62 PASS / 0 FAIL |
| 端到端（真图 + 真 qwen） | `text_source=ocr`、4 块、report 出现「图里文字（OCR 原文）」 |
| 启动日志 | 无 Traceback；`[OCR] RapidOCR 已加载` 在首张有字图时出现 |
