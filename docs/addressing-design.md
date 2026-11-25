# 收发件人标记设计（v1）

> **导航**：[设计主文档](DESIGN.md) ｜ 相关：[流水线](pipeline-map.md) · [情绪](emotion-dynamics-design.md) · [检索](retrieval-design.md)
> **本文回答**：群里「谁 → 谁」怎么判、置信度怎么用、它该影响哪些模块。
> **本文不回答**：心情公式、检索实现。

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
