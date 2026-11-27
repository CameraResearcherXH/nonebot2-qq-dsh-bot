# 记忆 / 检索 / 刻痕 / 目录 / 时间维度 —— 全链路审计

日期：2026-11-26｜范围：`services/memory_v2.py`、`services/memdb.py`、
`services/retrieval`（在 memdb 里）、`docs/retrieval-design.md`、`docs/emotion-dynamics-design.md`

> 结论先放前面：**架构是对的**（结构化过滤在前 → 多路打分 → 每人配额 → MMR），
> 但「关键词」这一路在中文上有三个真问题，其中两个会让检索**静默退化成「最近 N 条」**；
> 另有 4 处文档与实现不符、1 处 kind 漏配。本轮修了 4 个（标 ✅），其余列为待办。

---

## 0. 一句话说清这套东西在干什么

```text
她说一句 → 判读给 impact/dv → 写记忆（summary + tokens + 刻痕）
                                    │
检索：结构化过滤（人/群/时间/情绪/标签）  ← WHERE，不是加分项
      → 候选打分（结构 1.2 / 关键词 1.0 / 标签 0.8 / 提取强度 0.6 / 新鲜度 0.4 / 显著性 0.5）
      → 每人配额 + 反向保底 + MMR
      → 取 5 条进 prompt
目录：同一批数据，只回「编号 + 时间 + 心情 + 60 字摘要」，翻页/展开按需
```

---

## 1. 关键词工程

| 环节 | 实现 | 位置 |
|---|---|---|
| 分词 | 英文/数字整词 + **中文 bigram + 单字** | `memory_v2.tokenize()` |
| 写入 | `tokens = " ".join(tokenize(text))` | `memdb.remember()` |
| 索引 | FTS5 外部内容表 `memories_fts(tokens)`，`tokenize='unicode61'`，靠触发器同步 | `memdb.init()` |
| 查询 | `MATCH 'a OR b OR …'` → 再按「覆盖率」打分 | `memdb.recall_planned()` |

> unicode61 对中文本来是「整句一个 token」，我们用**空格分隔的 bigram** 规避了这一点 ——
> 这个做法是对的，索引里一个 token 就是一个 bigram/单字。

### ✅ K1（已修）查询词按码点截断，单字挤掉关键词

```python
match = " OR ".join(sorted(qset)[:24])      # 旧
```

`qset` 同时含 bigram（`白米`）和单字（`的`/`了`/`我`）。`sorted()` 按码点排，
单字常常排在前面，24 个名额被高频单字吃掉 → **真正的关键词没进 MATCH**，
FTS 返回空 → 落到 `ORDER BY ts DESC` 兜底 → 看起来「关键词检索没用，只按时间给」。

现在：bigram 优先（最多 12 个），没有 bigram 才用单字（最多 6 个）。

### ✅ K2（已修·查询侧）单字进索引，命中率虚高

`tokenize()` 把单字也写进 `tokens`。后果有两个：

1. **打分虚高**：`bm = |qset ∩ toks| / |qset|`，一个「的」就能让任意记忆拿到分；
2. **索引膨胀**：一条 40 字记忆 ≈ 39 bigram + 40 单字 ≈ 80 token（约 2 倍）。

**已做（2026-11-26）**：`recall_planned` 的**查询侧**改成「有 bigram 就只用 bigram」，
单字只在整条查询没有 bigram（比如搜一个字）时兜底。索引侧暂时还留着单字
（重建 FTS 是独立动作，要停写 + 重扫，留到下次维护窗口）；
所以「索引膨胀 ~2x」还在，但**打分虚高已经修掉**了 —— 这是真正影响排序的那一半。

### ✅ K3（已修·正名）「关键词分」不是 BM25，是覆盖率

`memdb.recall_planned()` 里：

```python
bm = len(qset & toks) / max(1, len(qset))     # 覆盖率，没有 idf
```

而参数叫 `RET_W_BM25`，`docs/retrieval-design.md` 也写「FTS5 MATCH + BM25」。
覆盖率的问题：**常见词和稀有词同权**（「今天」和「白米饭」一样重），
长 query 会被稀释。json 后端用的是真 BM25（`_bm25_scores`），两个后端不等价。

**已做（2026-11-26）**：按「先修名不副实」处理 —— 键名 `RET_W_BM25` **保留**
（改键名会让线上 `.env` 失效），但 DOCS 改成「关键词**覆盖率**权重（键名沿用历史；
sqlite 侧不是 BM25，没有 idf）」，`docs/retrieval-design.md` 同步注明。
要不要真的接 BM25（对 48~400 条候选在 Python 里算 idf）**另开一轮**，需要标注集 A/B。

---

## 2. 刻痕（salience / 情绪强度）

设计是「倒 U」：小冲击几乎不记、中等最显著、极端回落。实现在 `memdb.salience_weight()`：

| 参数 | 值 | 含义 |
|---|---|---|
| `MEM_SAL_LOW` | 0.15 | 低于它线性很小 |
| `MEM_SAL_HIGH` | 0.60 | 峰值位置 |
| `MEM_SAL_SLOPE_MID` | 2.1 | 峰值 = 0.045 + 2.1×(0.6−0.15) ≈ 0.99 |
| `MEM_SAL_TAIL` | 0.6 | u=1.0 时回落到 0.6 |

### ✅ S0（已修）`impression` 漏配 τ，和一次性事件同寿命

`TAU_KIND` 只有 event/bond/fact/preference/style，而线上真实在写的 kind 有
**`impression`**（MEMO 默认 kind、`/印象`、`aura_logic` 都写它）→ 落到
`TAU_DEFAULT=30`，和「随口一句」一样一个月掉到 37%。

已补 `MEM_TAU_IMPRESSION=180`，并写进 DESIGN §5 / emotion §6 的表。
**这条是回归测试抓出来的**（`情绪刻痕仍比中性记忆更容易被想起` 变红）。

### ⏳ S1（待办）两个后端的「显著性」口径不同

- sqlite：`w_sal = salience_weight(r["salience"])` —— `salience` 是写入时按 |ΔVAD| 算的；
- json：`w_emo = 1 + 2×salience_weight(_emo_of(d))` —— 用的是**情绪权重**（0~1 的标签强度）。

同一批数据切 `MEMORY_BACKEND`，排序会变。建议统一成 `salience`（写进 json 索引时也存它）。

### ⏳ S2（待办）`emotion_hit=True` 一律 ≥0.6，落在倒 U 的**回落段**

```python
_sal = float(salience) if salience is not None else max(0.0, min(1.0, _ew))
_ew  = max(emotion_weight, 0.6 if emotion_hit else 0.0)     # memdb.remember
```

`emotion_hit` 但没有 `emotion_weight` 的老数据 → `_sal = 0.6`，正好在峰值 0.99 的右边，
按倒 U 会**略低于**峰值。语义上有点反直觉（「有情绪」反而不如「0.55 的中等情绪」显著），
但幅度很小（0.99 vs 0.6×…），先记着，等 P6 标注校准再定。

### ⏳ S3（待办）`retrieval_strength` 只有 sqlite 有

sqlite：`strength = retrieval_strength × exp(−Δt/τ)`；
json：`strength = exp(−Δt/τ)`（没有 `retrieval_strength` 这一项）。
后端不一致，同上。

---

## 3. 目录（「海马体」）

`recall_catalog()` 三种模式：无关键词 → **时间倒序**；有关键词 → 相关度；给了时间区间 → 按区间。
`mark_hit=False` —— **翻目录不算「想起」**，这条设计是对的（否则越翻越难忘，遗忘曲线被检索行为污染）。

### ✅ C1（已修）`total` 丢了 gid / 人 / 时间三个过滤

```python
total = c.execute("SELECT count(*) FROM memories WHERE status='active'").fetchone()[0]
```

不管你在看「这个群的目录」还是「这个人的目录」，`total` 都是**全库条数** →
「共 N 条」和 `has_more` 都错。现在过滤条件只写一次，`select` 和 `count` 共用。

### ⚠️ C2（已知限制）有关键词时 `total = len(hits)`

`hits` 只取了 `offset+limit` 条 → 目录显示「共 N 条」永远 ≤ 一页。
**这条故意没改**：关键词路的真值要跑一次 FTS count（每翻一页多一次全表扫），
而目录本来就是「给你看看有什么」，`mode=relevance` 时 `total` 应该理解成
「这一页里能看到的条数」。要精确 count 时按 C1 的写法复用 WHERE 即可。

---

## 4. 时间维度

| 机制 | 值 | 说明 |
|---|---|---|
| `τ_kind` | event 30 / bond 90 / preference 180 / **impression 180** / fact 365 / style 365 | 提取强度 `exp(−Δt/τ)` |
| 新鲜度 | `0.5^(days/30)` | `RET_RECENCY_TAU_DAYS=30`，权重 0.4 |
| 时间词 | 「N天前 / 大前天 / 前天 / 昨天 / 今天 / 上下午 / 上周」 | `parse_time_expression()` |
| 时间窗 | `m.ts>=? OR importance>=0.8 OR salience>=0.5` | `recall_planned()` |

### ✅ T1（已修·按方案①+②）时间窗被 `salience>=0.5` 大面积绕过

`salience` 对任何 `emotion_hit` 的记忆都 ≥0.6 → **所有带情绪的记忆永远无视时间窗**。
设计意图是「高情绪/高重要的旧事不能因为过期就搜不到」（对的），但阈值 0.5 太松，
实际上「30 天以前的记忆交给图书馆」这条分工对**绝大多数有情绪的记忆**不成立。

**已做（2026-11-26 用户拍板，①+②）**：
- WHERE 的 `salience>=0.5` 提到 **0.8**（只有极强刻痕才可能绕过时间窗）；
- 绕过的候选在打分里 **×`MEM_OLD_FACTOR`(0.5)**，不再和窗内记忆同权。
效果：普通情绪记忆（salience 0.6~0.7）**不再**绕过时间窗；
真正的强刻痕（≥0.8）还能被翻出来，但排在有近期证据之后。

### ⏳ T2（待办）时间词覆盖不全

`parse_time_expression` 不认「上个月 / 去年 / X月X号 / 周末 / 昨晚」。
`_TIME_WORDS_RE` 里有「上个星期」，但解析分支只处理「上周」——**正则和解析不同步**。

---

## 5. 与 dsh（DeepSeek Harness）的检索比一比

先说清楚能比什么、不能比什么：

| | 我们 | dsh-plugin-memory |
|---|---|---|
| 解决的问题 | **跨月跨群**「记不记得住这个人」 | **单次会话**太长时「上下文装不下」 |
| 存储 | sqlite `memories` + FTS5 + 人设档案 | 会话内的记忆文件 + 摘要压缩 |
| 检索 | SQL 结构化过滤 + 多路打分 + 每人配额 + MMR | 模型自己按需读文件 / grep（无独立排序公式） |
| 时间维度 | `τ_kind` 分档衰减 + 时间词解析 + 时间窗 | 以「最近」为主 |
| 情绪 | 倒 U 显著性 + 情绪刻痕 + 💥 标记 | 无（不建模情绪强度） |
| 人设 | affinity / trust / respect / intimacy 四量 | 无 |

我们**明确抄了它两个东西**（代码注释里写着）：
- `digest-guard`：连续 N 轮没写记忆 → 下一轮提醒自己该记了（`DIGEST_AFTER`）；
- `checkup`：`/记忆体检` 的健康报告（`memory_v2.health_report()`）。

**诚实边界**：dsh-plugin-memory 的排序公式、存储格式我没有源码，
上面「检索」那一行是**按它公开定位（会话内上下文管理）推的**，不是逐行比对。
要严谨比较，得把它的插件源码放进 `vendor/` 或 `docs/third-party/` 再读一遍。

**结论**：不该用它的指标（上下文压缩率、token 节省）评价我们，
也不该用我们的指标（R@5、人均召回）评价它——**目标不同**。
真要借鉴，它最值得学的是「**写入门槛低、检索靠模型**」这条哲学：
我们现在的写入门槛（`importance`/`emotion_hit`/去重）比它重，代价是「没被标成重要的就不写」。

---

## 6. 文档 vs 代码（本轮核对）

| # | 文档说 | 代码实际 | 处置 |
|---|---|---|---|
| 1 | `docs/retrieval-design.md`：「FTS5 MATCH + BM25」 | sqlite 是**覆盖率**，不是 BM25（`recall_planned`） | ✅ 已正名（键名保留） |
| 2 | `emotion §6`：τ_kind 5 档 | 实际写了 6 种 kind（多 `impression`，且没登记） | ✅ 已补 |
| 3 | `DESIGN §3.4`：`total` 是当前筛选下的总数 | 以前是全库总数 | ✅ 已修 |
| 4 | `retrieval-design`：时间窗「30 天交图书馆」 | `salience>=0.5` 大面积绕过 | ✅ 已修（阈值 0.8 + 降权 0.5） |
| 5 | 参数名 `RET_W_BM25` | 实际算的不是 BM25 | ✅ DOCS 已注明 |
| 6 | `_TIME_WORDS_RE` 有「上个星期」 | 解析只处理「上周」 | 待办 T2 |

---

## 7. 建议顺序（按性价比）

1. **T1（时间窗阈值）** —— 一条常量，影响最大，但要你拍板；
2. **K2（索引只留 bigram）** —— 召回质量和索引体积都受益，但要配标注集 A/B；
3. **K3（真 BM25 或改名）** —— 先把「名不副实」修掉，接不接 BM25 另说；
4. **S1/S3/C2** —— 后端一致性 + 分页总数，都是小改；
5. **T2** —— 时间词补全，等有人真的抱怨「上周三的事想不起来」再做。

---

## 8. 本轮验证（容器内 3.11）

| 套件 | 结果 |
|---|---|
| `selftest_all.py` | **1797 通过 / 0 失败 / 4 跳过**（修了 2 条因 7 档 / ADR-043 变旧的断言） |
| `test_regressions.py` | **99 通过 / 0 失败**（修了 4 条过期断言 + 抓到 2 个真 bug） |
| `check_tuning` / `check_design_docs` / `check_names` | PASS |
| 容器内 `verify_deploy` | 115 / 115 |

**这轮被测试抓出来的两个真 bug**（不是我事先看出来的）：

1. `impression` 漏配 τ → 「印象」和一次性事件同寿命（§2 S0）；
2. `dynamic=True` 时协议超预算，`ctx_budget` 从**尾部**裁，
   把 `▍12 引用回复` 这种「每轮必须遵守」的规矩静默裁掉（§6 #7）。

**两个「看着像 bug、其实是观察窗口污染」的**：

- `desirability 缺 88%`：trace 保留 400 条，里面混着加字段之前的旧行；
  只看**最新 10 条**是 10/10。体检脚本已把默认窗口从 200 收到 40，并注明原因。
- `impact 全是 0`：老进程写库时没传 `impact`（真缺口，已修），
  但当天 313 条历史行不会回填 —— 看「今日分布」要等新数据。
