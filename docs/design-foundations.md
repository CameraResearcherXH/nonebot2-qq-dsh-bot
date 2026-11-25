# 设计依据与闭环审计 v2

> **导航**：[设计主文档](DESIGN.md) ｜ 相关：[情绪](emotion-dynamics-design.md) · [检索](retrieval-design.md) · [收发件人](addressing-design.md) · [注意力](attention-design.md)
> **本文回答**：为什么这么设计、依据是什么、闭环还差什么。
> **本文不回答**：参数、排期、实现步骤。

---

## 0. 结论

1. **闭环不是「有循环」，是每个状态变量五件事齐全**：更新 / 衰减 / 读取 / 可观测 / 可校准。
2. 现在**检索这条线基本闭合**（有 100 题 + 基线 + 回归门）；**情绪/关系/全局心情仍半开**，主要缺「外部效标」。
3. 关系衰减不是拍脑袋：投资模型、负向不对称、修复机制、单纯曝光，都有对应理论。
4. 收发件人是「一条数据修四处」：判读归因、记忆归因、关系更新、引用锚点。
5. 我们缺的不是更多论文，是**可校准的标注集**和**单一写入**。

---

## 1. 闭环判定

| # | 属性 | 问题 |
|---|---|---|
| ① | 更新 | 什么事件、什么公式写它 |
| ② | 衰减/回归 | 不更新时怎么回落、回到哪 |
| ③ | 读取 | 在哪里影响 prompt / 行为 |
| ④ | 可观测 | 能不能回放「为什么变成这样」 |
| ⑤ | 可校准 | 怎么知道它是对的 |

缺一项就是开环：要么永远漂，要么永远不动，要么无法解释。

---

## 2. 不变量（v2）

| # | 不变量 | 违反会怎样 |
|---|---|---|
| I1 | 心情**不直接**写好感；只有 `target ∈ {speaker,recipient,other}` 且 `relation_relevance` 过阈值才写 | 坏心情迁怒无关的人 |
| I2 | 记忆显著性由**事件属性**构造，不由心情值差反推 | 记忆深度是二次描述 |
| I3 | 一个事件只写一次（`msg_ids + about_qq + category`） | 窗口重叠重复计数 |
| I4 | **读不产生写**（`get_*` 不落盘） | 观测改变状态 |
| I5 | 快慢分离：心情 分钟~小时 / 关系 天~月 / 记忆 天~年 | 模型分不清「现在生气」和「长期不信任」 |
| I6 | 每个状态有 `baseline + clamp + decay` | 漂到 ±1 卡死 |
| I7 | 所有写入可回放（审计流水） | 出问题无法定位 |
| I8 | prompt 只给状态 + 约束，不给「你应该感到 X」 | 模型表演内省，污染判读 |
| I9 | 收件人优先于说话人做归因（`target=recipient`） | A 对 B 说的事记到 A 头上 |
| I10 | **心情更新只有一条链路**（步进 + 锚点）；查表只产生相对位移 | 三套公式互相打架 |

---

## 3. 闭环状态（2026-11-25）

| 变量 | 更新 | 衰减 | 读取 | 可观测 | 可校准 | 结论 |
|---|---|---|---|---|---|---|
| 心情 VAD | ✅ 步进+锚点 | ✅ | ✅ | 🟡 审计待落 | ❌ 缺事件标注 | **半开** |
| affinity | ✅ 公式 | ✅ 7d×深度 | ✅ | ✅ log | ❌ | **半开** |
| trust | ✅ 公式 | ✅ 45d | ✅ | 🟡 | ❌ | **半开** |
| respect | ✅ 公式 | ✅ 90d | ✅ | 🟡 | ❌ | **半开** |
| intimacy | ✅ 计算 | ✅ 30d | ✅ | ✅ | 🟡 | **基本闭合** |
| 全局心情 | ✅ 独立慢变量 | ✅ 6h | ✅ | 🟡 | ❌ | **半开** |
| 安静/主动 | ✅ 倾向分 | ✅ 退避 | ✅ | ✅ 计划 | ❌ | **半开** |
| 记忆双强度 | ✅ | ✅ | ✅ | 🟡 | ❌ | **半开** |
| 检索（角色脑） | — | — | ✅ | ✅ | ✅ 100 题 | **闭合** |
| 图书馆 | — | — | ✅ | ✅ | 🟡 10 题太少 | **半开** |
| 注意力/预算 | — | — | ✅ | 🟡 | ❌ 位置指标未做 | **半开** |

**唯一真正闭合的是「检索（角色脑）」**；其余都卡在「可校准」。

---

## 4. 各模块的依据（只写「替换我们什么」）

### 4.1 心情：为什么是步进 + 锚点（原卡尔曼）

| 依据 | 对我们的意义 |
|---|---|
| **Russell 环形 / Mehrabian PAD** | VAD 三维的合法性；唤醒最不可靠（PAIR 实测也弱） |
| **OCC / Scherer SEC / Lazarus** | 判读要有「相关性 → 含义 → 应对 → 规范」的检查清单，不是自由发挥 |
| **Frijda 情绪定律** | 习惯化=`novelty`；坏比好强=负面权重；情绪动量=惯性 |
| **Kuppens 情绪惯性** | 状态持久化才有「延迟反应」；支持卡尔曼的预测项 |
| **卡尔曼（1960）** | 噪声观测 → 潜状态；置信度天然进公式。我们只借 K 的形式，观测是相对位移，所以补锚点纠偏 |
| **OU 过程** | 均值回归 + 半衰期（`τ=ln2/θ`） |
| **PID / anti-windup** | 好感积分饱和 → `clamp`；deadband → 小事不更新 |
| **Barrett 建构情绪论** | 提醒：我们做的是角色状态机，不是心理测量 |

> 事件研究（实际−预期）**二期再做**：一期先把观测/步进跑通，避免公式叠加（ADR-004）。

### 4.2 关系：为什么会衰减、为什么不对称

| 依据 | 对我们的意义 |
|---|---|
| **Rusbult 投资模型** | 承诺 = 满意 + 投入 − 替代；投入越多越不轻易归零 → **深度系数拖慢衰减** |
| **Baumeister 负向不对称** | 坏比好强 → 负面权重 1.3、失信跌幅更大 |
| **Gottman 修复** | 道歉/补偿是独立通道 → `grudge` 减半、信任可修 |
| **Zajonc 单纯曝光** | 熟悉本身维持好感 → 互动频率拖慢衰减 |
| **Altman & Taylor 社会渗透** | 关系分层、可渗透也可退化 → 阶段可降级 |
| **Sakamoto et al. (2025)** | 信任 + closeness 双变量 → 拆维的概念锚点（不是参数） |
| **Bowlby 依恋** | 焦虑/回避影响「被冷落时的反应」和信任建立速度 |
| **Aristotle《尼各马可伦理学》** | `philia` 需要时间与共同经历；`hexis` 是稳定习性 → 人设进参数 |
| **Cicero《论友谊》** | 友谊需要维护，不然会淡 → 衰减项 |
| **孔子「久而敬之」** | 尊重衰减最慢（90 天） |
| **Hume 平静/猛烈情感** | 平静≈长期关系，猛烈≈当下心情 → 快慢分离 |
| **Spinoza conatus** | 回归基线 = 自我保存的努力 |
| **Abhidharma `vedanā`/`anuśaya`** | 受=当下，随眠=潜在倾向 → 两层结构 |

### 4.3 记忆：为什么拆双强度

| 依据 | 意义 |
|---|---|
| **Bjork & Bjork 新失用论** | 存储强度不衰减，提取强度衰减且被用强化 → `storage/retrieval` |
| **Ebbinghaus** | 提取强度的时间衰减 |
| **McGaugh 情绪调制** | 唤醒对编码是倒 U，不是线性 |
| **Anderson & Schooler / ACT-R** | 可及性 = 频率 + 新近 + 语境 → `hit_count` 进公式 |
| **Tulving 情景/语义** | `kind` 的理论依据；检索策略不同 |
| **Loftus / Schacter** | 记忆会被改写/错归因 → 低置信要标注，不当 ground truth |

### 4.4 检索与注意力：为什么先过滤再排序

| 依据 | 意义 |
|---|---|
| **BM25 / RRF / MMR** | 关键词 + 名次融合 + 多样性配额 |
| **RAG / Self-RAG / CRAG** | 检索增强；按需检索；低分不注入 |
| **Lost in the Middle / Context Rot / RULER** | 中间最差、越长越差 → 升序 + 硬预算 |
| **FILCO / RECOMP / LLMLingua** | 过滤 > 压缩；压缩保护实体 |
| **MemGPT / MemoryBank / A-MEM / HippoRAG** | 分层记忆、遗忘、标签图、关系检索 |

### 4.5 收发件人：为什么值得单独立文档

| 依据 | 意义 |
|---|---|
| **Grice 会话含义 / 共同 ground** | 「你」指谁取决于共同语境；收件人是显式 ground |
| **Clark & Brennan  grounding** | 引用回复 = 最低成本的 grounding 动作 |
| **共指消解（coreference）** | @/引用是人工标注的共指，比模型猜可靠 |
| **社会网络分析（Granovetter 弱连接）** | `interaction_edges` 可算亲疏、桥接、小圈子 |

---

## 5. 哲学锚点（保留最有用的 8 个）

| 思想家 | 概念 | 用处 |
|---|---|---|
| **Ryle** | occurent vs dispositional | 心情=此刻，关系=倾向；不能用同一标量 |
| **Aristotle** | `hexis` / `pathos`；中道 | 人设=hexis；baseline+saturation=中道 |
| **Hume** | 平静 vs 猛烈情感 | 快慢变量分离 |
| **Spinoza** | `conatus` | 回归基线 |
| **Stoics** | `prohairesis` | 「安静但不装死」：能选择回不回，不被情绪绑架 |
| **Abhidharma** | `vedanā` / `anuśaya` | 受 vs 随眠 = 心情 vs 信任 |
| **Cicero / 孔子** | 友谊需维护；久而敬之 | 衰减项；尊重衰减最慢 |
| **Heidegger** | `Befindlichkeit` | 心情影响「她怎么理解这句话」→ 必须限幅 |

> 最有用的一刀仍是 **Ryle**：它一句话解释「为什么心情和好感不能是同一个东西」，
> 也解释了为什么旧版 `SMOOTH=1.0` 覆盖是错的。

---

## 6. 还缺什么（v2）

| # | 缺什么 | 为什么重要 |
|---|---|---|
| G1 | 单一状态源 + 并发写（文件锁） | ✅ 2026-11-25：`mood._serialized` + `_save` flock；8 线程压力测试不丢更新 |
| G2 | 事件键幂等 | 窗口重叠 |
| G3 | 情绪外部效标（200 条标注） | 判读档位无法验证 |
| G4 | 收件人标注（100 条） | 置信度未校准 |
| G5 | sqlite 回填与双写 | 线上库只有 histories |
| G6 | `mood_audit` + 注入清单 | 不可回放 |
| G7 | 位置指标（gold_position / lost_in_middle） | 注意力无法验收 |
| G8 | 成本模型（token + 延迟） | 预算无法验收 |
| G9 | 迁移/回滚演练 | schema 变更风险 |
| G10 | 隐私与保留策略（记忆多久、可删除） | 长期运行必须 |

---

## 7. 参考（可核实）

**情绪 / 关系**
- Ortony, Clore & Collins (1988) *The Cognitive Structure of Emotions*
- Scherer (2001) Appraisal considered as a process of multilevel sequential checking
- Lazarus (1991) *Emotion and Adaptation*；Frijda (1988) The laws of emotion
- Russell (1980) circumplex；Mehrabian & Russell (1974) PAD
- Kuppens et al. (2010) emotional inertia
- Rusbult (1980) investment model
- Baumeister et al. (2001) bad is stronger than good
- Gottman & Silver (1999) *The Seven Principles for Making Marriage Work*
- Zajonc (1968) mere exposure
- Altman & Taylor (1973) social penetration
- Bowlby (1969) *Attachment and Loss*
- Sakamoto et al. (2025, *Scientific Reports*) trust & closeness in LLM agents

**记忆**
- Tulving (1972/1983)；Ebbinghaus (1885)
- Bjork & Bjork (1992) A new theory of disuse
- McGaugh (2004)；Loftus (2005)；Schacter (2001)
- Anderson & Schooler (1991)

**检索 / 注意力**
- Robertson & Zaragoza (2009) BM25；Cormack et al. (2009) RRF；Carbonell & Goldstein (1998) MMR
- Lewis et al. (2020) RAG；Asai et al. (2023) Self-RAG；Yan et al. (2024) CRAG
- Liu et al. (2023/2024) Lost in the Middle；Hsieh et al. (2024) RULER
- Xiao et al. (2023) Attention Sinks；Jain & Wallace (2019) Attention is not Explanation
- Xu et al. (2023) RECOMP；Wang et al. (2023) FILCO；Jiang et al. (2023) LLMLingua
- Packer et al. (2023) MemGPT；Zhong et al. (2023) MemoryBank；Gutiérrez et al. (2024) HippoRAG

**量化金融（工具）**
- Kalman (1960)；Uhlenbeck & Ornstein (1930)；Hawkes (1971)；Engle (1982)
- Fama et al. (1969) event study；Fama & French (1993) factor model
- Åström & Hägglund (2006) *Advanced PID Control*

**哲学 / 语用**
- Ryle (1949) *The Concept of Mind*
- Aristotle *Nicomachean Ethics*；Cicero *De Amicitia*；《论语》
- Hume *A Treatise of Human Nature*；Spinoza *Ethics*
- Heidegger *Being and Time*（Befindlichkeit）
- Grice (1975) Logic and conversation；Clark & Brennan (1991) Grounding

---

## 8. 一句话

> 论文告诉我们「该测什么」，金融告诉我们「怎么在噪声里估、怎么阻尼、怎么回测」，
> 哲学告诉我们「为什么快慢要分开、为什么自报不等于内省」。
> **先把闭环补齐（尤其情绪效标），再谈参数。**
