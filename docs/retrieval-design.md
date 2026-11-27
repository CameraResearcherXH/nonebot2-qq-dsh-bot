# 检索设计 v2：角色脑 sqlite + 图书馆 RAG

> **导航**：[设计主文档](DESIGN.md) ｜ 相关：[情绪](emotion-dynamics-design.md) · [注意力](attention-design.md) · [收发件人](addressing-design.md)
> **本文回答**：data 怎么 sqlite 化、时间/情绪怎么优先、关键词工程怎么做、和 RAG 怎么分、怎么评。
> **本文不回答**：情绪公式、上下文预算。
> **数字以 [DESIGN §3.4](DESIGN.md) 为准。**

---

## 0. 结论

1. **必须分开**：角色脑（个人事实/记忆/关系/风格，毫秒级，结构化）与图书馆（世界知识，秒级，语义）**不是一套库**。
2. **时间和情绪是「过滤器」，不是「打分权重」**：先用 `(about_qq, ts)`、`(mood_sign, ts)` 把候选砍到几十条，再用关键词/向量排序。
3. **关键词工程优先于向量**：实体/别名/同义/繁简/拼音/标签扩展，性价比最高。
4. **sqlite 是角色脑的唯一事实源**：JSON 只做导入/导出，不再各写各的。
5. **评测只测「取没取到」**：生成另算，避免一次喂太多。

---

## 1. 为什么必须分开

| 维度 | 角色脑（sqlite，近 30 天） | 个人档案库（sqlite，30 天以前） | 图书馆（RAG，**可选**） |
|---|---|---|---|
| 内容 | 她记得的事、对谁什么关系、谁怎么说话 | 已经从近期记忆滑出去的旧对话/旧事 | 诗词/百科/论文/贴吧 |
| 更新 | 每条消息都在写 | 每天/每周增量搬一次（`archive_migrate.py`） | 批量入库，增量 |
| 延迟 | 毫秒 | 毫秒（本地 FTS5） | 秒级（10s+） |
| 隐私 | 高（QQ/群聊） | 高（只她自己能查，不出本机） | 低（公开语料） |
| 失败影响 | 记错人/答错事 | 想不起很久以前的事 | 引错百科 |
| 查询方式 | SQL 过滤 + FTS | 时间窗 + FTS | BM25 + 向量 + 分区 |

> 三者**不是一套库**：档案库不依赖 RAG；没有 RAG 的部署照样有近期记忆 + 个人档案库。

> ⚠️ 2026-11-26 正名：sqlite 侧的「关键词分」实际是 **token 覆盖率**（`|qset∩toks|/|qset|`，**没有 idf**），不是 BM25；键名 `RET_W_BM25` 是历史遗留。json 后端（回滚用）才是真 BM25（`memory_v2._bm25_scores`）。

**混在一起的后果**：个人记忆被百科挤掉（实测 `xiaoming.txt` 31 字节被 9000 篇文档淹没）；
反过来，百科内容被当成「她自己的经历」说出来。

---

## 2. 现状（as-built，核实过）

### 2.1 代码里有，线上没启用

`services/memdb.py` 有：

```sql
CREATE TABLE memories (...)
CREATE VIRTUAL TABLE memories_fts USING fts5(tokens)
```

但线上：

```text
MEMORY_BACKEND=json
data/memory.db 实际表：histories（14196 行）
```

→ **R0 不是「加个索引」，是「回填 8414 条 JSON blocks + 切后端 + 校验」。**

### 2.2 现在的问题

| # | 问题 | 后果 |
|---|---|---|
| R1 | 只有一路关键词 + 加权分 | 换个说法就找不到 |
| R2 | 时间/情绪只是「加权」，不是过滤 | 「上周的事」会混进去年 |
| R3 | `tags` 非空仅 54/8414（0.6%） | 「按心情/称呼找」是空的 |
| R4 | 没有实体表 | 昵称/别名/QQ 散在 3 个文件 |
| R5 | 图书馆无分区、无去重 | 近重复文档互抢（实测「环凤论」3 个版本） |
| R6 | 无增量构建 | 全量 3 天 |
| R7 | 无出处 | 百科当经历 |

---

## 3. 角色脑 schema（v2）

```sql
-- ① 记忆主表（在现有基础上加列）
ALTER TABLE memories ADD COLUMN impact INTEGER DEFAULT 0;        -- -2..+2
ALTER TABLE memories ADD COLUMN unexpected REAL DEFAULT 0;
ALTER TABLE memories ADD COLUMN relation_relevance REAL DEFAULT 0.5;
ALTER TABLE memories ADD COLUMN storage_strength REAL DEFAULT 0.5;
ALTER TABLE memories ADD COLUMN retrieval_strength REAL DEFAULT 0.5;
ALTER TABLE memories ADD COLUMN embedding BLOB;

-- ② 标签（多对多）
CREATE TABLE memory_tags (
  memory_id TEXT NOT NULL,
  tag_type  TEXT NOT NULL,   -- person/group/topic/mood/time_bucket/event_type/style
  tag_value TEXT NOT NULL,
  weight    REAL DEFAULT 1.0,
  PRIMARY KEY (memory_id, tag_type, tag_value)
);
CREATE INDEX idx_tag_lookup ON memory_tags(tag_type, tag_value);

-- ③ 实体收敛：QQ / 称呼 / 别名 / 群名片
CREATE TABLE entities (
  entity_id TEXT PRIMARY KEY,        -- qq:123 / group:719
  kind TEXT, qq TEXT, gid TEXT,
  primary_name TEXT, aliases TEXT,   -- JSON 数组
  style_id TEXT, updated_at REAL
);
CREATE INDEX idx_entity_qq ON entities(qq);
CREATE INDEX idx_entity_name ON entities(primary_name);

-- ④ 档案（从 profiles 镜像）
CREATE TABLE profiles (
  qq TEXT PRIMARY KEY,
  call_name TEXT, identity TEXT, personality TEXT,
  affinity REAL, trust REAL, respect REAL, intimacy REAL, grudge REAL,
  bond_word TEXT, updated_at REAL
);

-- ⑤ 风格样本
CREATE TABLE style_samples (
  id TEXT PRIMARY KEY, qq TEXT, gid TEXT,
  text TEXT, tags TEXT, approved INTEGER, ts REAL
);

-- ⑥ 心情事件（结构化，唯一 schema）
CREATE TABLE mood_events (
  id TEXT PRIMARY KEY, ts REAL, session TEXT,
  about_qq TEXT, target TEXT,          -- speaker/recipient/other/world/self
  impact INTEGER, direction TEXT,      -- '+' / '-'
  dv REAL, da REAL, dd REAL,
  confidence REAL, relevance REAL,
  reason TEXT, msg_ids TEXT
);
CREATE INDEX idx_mood_person ON mood_events(about_qq, direction, ts);

-- ⑦ 交互边（来自 addressing）
CREATE TABLE interaction_edges (
  id TEXT PRIMARY KEY, ts REAL, gid TEXT,
  from_qq TEXT, to_qq TEXT, kind TEXT,
  signal TEXT, conf REAL, sentiment REAL, thread_id TEXT
);
CREATE INDEX idx_edge_from ON interaction_edges(from_qq, ts);
CREATE INDEX idx_edge_to   ON interaction_edges(to_qq, ts);

-- ⑧ 线程
CREATE TABLE threads (
  id TEXT PRIMARY KEY, gid TEXT, started_at REAL, last_at REAL,
  topic TEXT, participants TEXT, summary TEXT
);
```

**索引原则**：所有「人 + 时间」「群 + 时间」「心情方向 + 时间」都要有复合索引——
因为它们是**过滤器**，不是排序项。

### 3.4 非记忆类资源（旧设计，别丢）

| 资源 | 内容 | 来源 | 预算 | 何时给 |
|---|---|---|---|---|
| `conv_state` | topics / background / progress / pending / agreements | 每 6 条压缩一次，`data/state/*.json` | 150 | 总是（摘要升级） |
| `focus_card` | 人 / 事 / 关系 / 她在他面前 / 注意 | 档案深度视图 | 100 | 只在 focus / depth 场景，**替代简版 profile** |
| `lore` | 群设定/梗/世界观词条 | 词条库 | ≤80 | 命中关键词才给，带出处 |
| `threads` | 线程 + 参与者 + 摘要 | addressing | 60 | 多线程时 |

**记录 vs 检索（ADR-024）**：

```text
记忆：每个 turn 都记（MEMO/事件/fact），不按 Δ 筛；
Δ 只决定 storage（加粗/显著性）和检索权重；
sqlite 改的是「时间 × 情绪」双维检索，不是记录语义。
```

**群隔离**：

```sql
-- 新库：memories.gid
WHERE gid = ?
-- 旧数据兼容：文本前缀 `（群号:XXX）`，迁移时解析成 gid 列
```

---

## 4. 查询：时间/情绪是过滤器，关键词才是排序

### 4.1 两种模式，彻底分开

| 模式 | 触发 | 排序 | 返回 |
|---|---|---|---|
| **目录 catalog** | 「你都记得什么」「翻一下上周」 | **时间倒序**（不看相似度） | 编号 + 时间 + 心情 + 一句摘要 |
| **详细 detail** | 回答具体问题 | 过滤 → 关键词/向量 → RRF | 原文 + 时间 + 人物 + 出处 |

### 4.2 detail 的五步（优先级就是顺序）

```text
① 人：   about_qq IN (who)                    ← 最强，先砍 90%
② 时间： ts BETWEEN when_start AND when_end   ← 「上周/那天/之前」
③ 情绪： direction / valence sign             ← 「让我难受的/开心的」
④ 标签： memory_tags(topic/mood/person)       ← 关键词工程扩展
⑤ 关键词：FTS5 MATCH + BM25                    ← 最后才排序
（二期：⑥ 向量 + RRF）
```

**为什么这个顺序**：时间/情绪是**高精度、低召回成本**的条件；
先过滤再排序，比「给时间加 0.2 权重」稳得多——后者会让「很像但时间错」的条目混进来。

### 4.3 SQL 示例

```sql
-- 「老大上周让我难受的事」
SELECT m.* FROM memories m
JOIN memory_tags t ON t.memory_id = m.id
WHERE m.about_qq = ?
  AND m.ts BETWEEN ? AND ?
  AND m.valence < 0
  AND t.tag_type = 'topic' AND t.tag_value IN ('工作','会议')
ORDER BY bm25(memories_fts) ASC
LIMIT 20;

-- 「芒果提过的那个轴承的事」
SELECT m.* FROM memories m
JOIN memory_tags t ON t.memory_id = m.id
WHERE t.tag_type = 'person' AND t.tag_value = ?
  AND m.text LIKE '%轴承%'
ORDER BY m.ts DESC
LIMIT 20;

-- 「上次为什么对老大心情变好」（结构化，不走全文）
SELECT * FROM mood_events
WHERE about_qq = ? AND direction = '+'
ORDER BY ts DESC LIMIT 5;
```

### 4.4 排序分数（只在过滤后使用）

```text
score = 1.2×struct_match      # 人/时间/情绪命中
      + 1.0×bm25_norm
      + 0.8×tag_overlap
      + 0.6×retrieval_strength×exp(−Δt/τ_kind)
      + 0.4×recency
      + 0.5×w                    # 显著性倒 U（中等冲击最显著，极端回落）
（向量二期再加，用 RRF 融合，不直接加）
```

配额：同一个人 ≤60%；前 5 条至少 1 条中性/反向；
候选多取 `limit×MEM_MMR_CAND` 条后按 MMR（λ=0.7）去近重复，避免同一件事占满证据位。

**τ_kind**（天）：event 30 / bond 90 / fact 365 / preference 180 / style 365。
`w` 与 `τ_kind` 的默认值都在 `core/tuning.py` 的 `MEM_*`，可 env 覆盖。

---

## 5. 关键词工程（先做这个，收益最大）

| 层 | 内容 | 例子 |
|---|---|---|
| 实体词典 | 人物、作品、术语、地名、诗词牌名 | 「静夜思」→ 李白/唐诗 |
| 别名/昵称 | 群友称呼、外号、简称 | 「老大」→ QQ 100000003 |
| 同义/近义 | 蛋糕↔甜品、番剧↔动画 | 查询扩展 |
| 繁简 + 拼音 | 诗词检索必需 | 「與」→「与」；「li bai」→ 李白 |
| 标签扩展 | 命中实体自动加标签 | 「李白」→ 唐诗/诗人 |
| 查询改写 | 口语补全 | 「那个啥来着」→ 用上下文补 |

落地顺序：**entities 表 → memory_tags 回填 → FTS5 → 同义/拼音 → 向量**。

---

## 5.1 个人档案库（C 方案，本地 sqlite）

用户 2026-11-26 拍板：**另建一个「个人档案库」，独立索引，只她自己能查，不和图书馆共用。**

- 存储：`data/archive.db`（sqlite + FTS5，`unicode61`，bigram+单字 token，和 memdb 同一套分词）；
- 只存 `ARCHIVE_MIN_AGE_DAYS=30` 天以前的旧事；近 30 天仍走 `memories`（memdb）；
- 写入：`scripts/archive_migrate.py`（幂等，按 `hash(gid|qq|ts|text)` 去重）；
  来源：`data/dialogue/*.jsonl` + `memories` 里超过 30 天的行（**不删源文件/源表**）；
- 查询：`services/archive.py::recall()`（时间窗 + FTS，毫秒级），
  `reply_pipeline` 的「档案库」块与「记忆」并行；`[[ARCHIVE|关键词]]` 可显式查；
- **不依赖 RAG**：没有算力机/图书馆也能用；RAG 只放外部语料/书；
- 不对外：没有 HTTP 端口，只有她自己的 prompt / 标签能用。

> 为什么不再「把 data/dialogue 同步进 RAG」：会重复、会把最近闲聊顶到「久远档案」前面、
> 而且没搭 RAG 的部署行为不一致。图书馆和档案库从此各管一段。

## 6. 图书馆：分区 + 去重 + 增量

### 6.1 分区

| 集合 | 切块 | 检索偏好 |
|---|---|---|
| poetry | 一首一块 | BM25 强 |
| wiki / 萌娘 | 按小节 | 向量强 |
| tieba | 按楼 | 混合 |
| anime | 按作品/角色 | 实体优先 |
| arxiv | 摘要+章节 | 向量+关键词 |
| math | 定义/定理 | 关键词+公式 |
| history | 事件/人物 | 混合 |
| misc | 段落 | 混合 |

### 6.2 去重（实测必须做）

「环凤论」同主题有 3 个近似标题 → 检索时互抢。
入库时加：`canonical_id = simhash(正文)`；标题相似度 >0.9 视为同一簇，只保留最长/最全的一篇，
其余标 `variant_of`，检索时合并展示。

### 6.3 增量

```text
文件内容 hash → 变更队列 → 只处理新增/修改
日常几十篇，分钟级；不再全量 3 天
```

### 6.4 检索接口

- 只用 `POST /api/agent/execute-tool?tool_name=rag_search`（只检索、带来源、约 9s）；
- **不用** `/api/agent/query`（实测 39s 且报 `OLLAMA_BASE_URL` 错误）；
- 命中块回填「所在小节/整首」，不整篇塞。

---

## 7. 评测（v2）

### 7.1 金标准集

100 条，从真实数据反向出题（15 人物 / 20 事件 / 15 时间 / 15 心情 / 10 世界 / 10 风格 / 15 混合）。
生成器：`scripts/build_gold_questions.py`；评测器：`scripts/eval_gold.py`。

> ⚠️ 当前批改状态：人工改的是 `gold_questions_draft.md`（表格），`jsonl` 尚未回写。
> 需要一个 `md → jsonl` 回写工具（带列数校验），否则评测仍读旧题。

### 7.2 两阶段

```text
retrieval-only：只测取没取到，0 模型费用
generate-on-budget：top5（≤500 token）导出 replay，交给 eval_replay.py
```

### 7.3 指标

- `recall@1/5/10`、`MRR`
- `distractor@5`（同人无关事件 / 近重复文档）
- `route_acc`（角色脑 / 图书馆 / both）
- `avg_ms`、`avg_ctx_tokens`
- 生成段：`groundedness`、人机感 1~5、`cost_per_turn`

### 7.4 首轮基线（library 关，100 题）

| 类 | n | R@1 | R@5 | MRR |
|---|---|---|---|---|
| person_fact | 15 | 1.000 | 1.000 | 1.000 |
| event | 20 | 0.650 | 0.950 | 0.775 |
| time | 15 | 0.467 | 1.000 | 0.722 |
| mood | 15 | 1.000 | 1.000 | 1.000 |
| style | 10 | 0.800 | 0.800 | 0.800 |
| mixed | 15 | 0.733 | 1.000 | 0.822 |
| world（联网） | 10 | 0.400 | 0.700 | 0.520 |
| **总体** | 100 | **0.690** | **0.870** | **0.767** |

三条结论（已写进设计）：

1. 心情 BM25 0.33 → 结构化 **1.00**（所以 `mood_events` 必须建）；
2. 混合题拼接 0.867 → 配额 **1.00**（所以 `quota` 不是拍脑袋）；
3. 时间 R@5=1.0 但 R@1=0.47 → 需要「上次默认时间倒序」。

图书馆两个真问题：**路由错**（`xiaoming` 没书名号 → 判成角色脑）、**近重复文档互抢**。

### 7.5 回归门

`recall@5` 掉 >5% 不许上线；mood/time/world 单列看。

### 7.6 P4 落地（2026-11-25）

已实现（`services/memdb.py`）：

- schema：`memories` v2 列（impact/unexpected/relation_relevance/storage/retrieval）
  + `memory_tags` / `entities` / `mood_events`；
- **写入即打标签**：person / group / event_type / mood / mood_dir / time_bucket / topic；
  旧数据用 `backfill_tags()` 补（幂等）；
- `recall_planned()`：**人 → 时间 → 情绪 → 标签 → 关键词**；
  结构化条件是 `WHERE`，不是权重（修 R2「上周的事混进去年」）；
  打分 `1.2 struct + 1.0 bm25 + 0.8 tag + 0.6 strength + 0.4 recency`；
- 命中回补 `retrieval_strength += 0.10×(1−r)`；
- `mood_events`：judge 实时写 + `mood_history.jsonl` 回填；
- 开关：`MEMORY_BACKEND=sqlite`（默认 json）+ `RETRIEVAL_PLANNER=1`（sqlite 下默认开）；
- 回填：`scripts/backfill_memory_p4.py`（默认 dry-run，`--write` / `--retag`）。

未做（下一轮）：`profiles` / `style_samples` / `interaction_edges` / `threads` 四张表；向量。

---

## 8. 迁移（R0 的真正工作量）

```text
1) 备份 data/memory + data/memory.db
2) 建表（§3）
3) 回填：
   python3 scripts/backfill_memory_p4.py --write
   （已覆盖：memory/blocks → memories + memory_tags；mood_history → mood_events；
     profiles/*.json → entities；profiles/style_samples/threads 三张表已建，
     回填脚本 `scripts/backfill_retrieval_tables.py --write`）
4) 双写一周：JSON 与 sqlite 同时写，每晚比对条数/抽样内容
   ✅ **双写已实现**（2026-11-25）：`memory_v2._dual_mirror` 在 json 写入后镜像到
   sqlite，默认开（`MEMORY_DUAL_WRITE=0` 可关）；日志 `[MEM] dual-write`。
   对账脚本：`python3 scripts/check_memory_dual.py --strict`
   （实测 json 6113 / sqlite 6116，最近 119 条 turn_id 全覆盖）。
   切前仍要备份 `data/memory.db`，先在副本上跑一遍。
5) 切 MEMORY_BACKEND=sqlite
6) 校验：条数一致、抽样 50 条逐字一致、检索 recall 不回退
7) 回滚：改回 json（JSON 文件保留）
```

---

## 9. 分阶段

| 阶段 | 做什么 | 验收 |
|---|---|---|
| R0 | 建表 + 回填 + 双写 | 条数一致；抽样一致 |
| R1 | 实体/标签回填 + FTS5 | 标签覆盖 ≥90% |
| R2 | 查询计划（人→时间→情绪→标签→关键词） | 时间题 R@1 ≥0.7 |
| R3 | 图书馆分区 + 去重 + 增量 | world R@5 ≥0.85；构建 <10min（服务器侧上传前增量/去重已完成；分区索引仍需算力机） |
| R4 | 向量 + RRF + 重排 | 换说法也能找到；R@5 ≥0.9 |
| R5 | 每周抽检 + 调权重（p95 最慢一块 + gold_position） | 连续 4 周不回退 |

> 二期已完成「显著性倒 U + τ_kind + MMR」；向量 / RRF 仍未做（`RET_RRF_K` 还在 `UNWIRED`）。

---

## 10. 明确不做

- **不把个人记忆 / `data/dialogue` 塞进图书馆 RAG**（2026-11-26 用户定边界）：
  记忆 = 本地 sqlite（按人/时间/情绪/标签，30 天内够用）；图书馆 = 外部语料/久远归档。
  `scripts/library_sync.py` 现在**默认拒绝** `--dir data/dialogue`，只有
  `--allow-memory --min-age-days 30` 才允许放「已经从本地记忆滑出去」的归档。
- **RAG 是可选组件**：没搭 RAG 的部署，`agent_client` 拿不到就返回空、
  10 分钟内不再试，聊天/记忆/心情都不受影响（开源默认就是这条路）。
- 不用「时间/情绪加权」代替过滤；
- 不在标签覆盖 0.6% 时上向量（先补标签）；
- 不整篇回填文档（只回填命中块所在小节）。
