# 设计主文档 v2

> 状态：**v2 设计稿（2026-11-25）**。本文混有三种状态，读时先看 §0.1：
> `【设计】` = 目标、还没写代码；`【已决】` = ADR 已拍板；`【已接线】` = 代码已在跑。
> **不要假设「文档写了」= 「代码在跑」**；接线状态以 §0.1 和 `tuning.UNWIRED` 为准。
> 重写原则：**按会话记录重建，不保留补丁**。旧文档里互相冲突的数字，以本文 §3「口径表」为唯一准。
> **例外**：好感 v2（`AFF_MODEL=v2`）的公式以
> [affinity-quantification-design.md](affinity-quantification-design.md)（v2.5）为准；
> 本文 §3.2 只描述 v1（默认线上口径）并指向它。
> 阅读路线：§0 结论 → §0.1 状态 → §1 一张图 → §3 口径表 → §4 决策记录 → 对应子文档。

---

## 0. 结论（三句话）

0. **因果链只有一条（2026-11-26 定死，不许绕）**：
   ```
   收发件人（外部信息，只帮她理解「谁对谁说」）
        ↓
   她的 [[THINK]]（她理解后的第一人称心理活动）  ← 心情的唯一来源
        ↓
   判读 → impact + anchor_level → mood.observe → dv（**事件**位移）+ anchored（水平纠偏）
        ↓                                        ↓                      ↓
   好感：只吃 dv（v1 另加小的 Δ 事件项）    v 的实际变化 = dv + anchored（只进状态）
   ```
   - **心情**：任何时候都只来自 THINK（v1/v2 都成立）。
   - **`dv` ≠ `v` 的总变化**：`dv` 只是「这一窗这件事推了多少」；`anchored` 是
     「对她整体水平的重新估计」。**好感只吃 `dv`**——把 `anchored` 算进好感就变成
     「她今天心情好，顺手记到旁边人头上」。想看总变化用 `dv_total`（仅诊断）。
   - **好感**：v1 与 v2 **都吃 `dv`**（2026-11-26 用户拍板）。区别只在：
     · v1 = `AFF_V1_DELTA_K(0.25)×|Δ|×gain(≈0.10)×T(trust_speed)×novelty×k_eff×(1−|a|)`，
       其中 `T(ts) = 0.6 + 0.8×ts`（`mood_judge` 传入的是这个**映射后的值**，不是原始的
       `trust_speed` 本身；`ts=0.5` 时 T=1.0，所以默认人设下看不出差别，人设一偏就分叉 —— 2026-11-27 更正）
       + `AFF_V1_DV_K(0.30)×三段式(dv)`，单次夹 `AFF_V1_CAP=0.15`。
       **`0.25` 只是原始乘数，实际事件项量级 ≈ `0.25×|Δ|×0.10`**（|Δ|=0.22 → ≈0.0044）；
       日常因此比旧 v1（等效系数 1.0）慢；`|dv|≥0.30` 的尾部（一见钟情/心碎）≈0.11~0.13；
     · v1 仍要求 `cause=speaker`（过渡口径），v2 只看 `target ∈ {speaker,recipient,other}`。
   - 任何「从群聊内容直接推心情」或「从事件类型直接加好感」的路径都是 bug。
   - **当前状态**：v1 是默认口径（已含 dv），v2 是目标口径（痕迹求和 + 巩固 + 分层门槛）。
1. **事件不切，窗口归因**：群聊里 8 句一个窗口，判「谁让她的状态往哪动了多少」，不追求事件起止。
2. **LLM 只报档位，代码做步进**：LLM 给 `desirability(-3~+3) / cared(0/1/2) / anchor_level(必填) / arousal / confidence / 目标人`，代码合成 `impact` 并查表得相对位移，**置信加权步进**推进状态；同时每窗把 `v` 按权重（0.45×conf）向 `anchor_level` 的**绝对档位**混入（占大头），每 20 窗回补一次 `P`。**只有这一条更新链路。**（2026-11-26：5 档扩 7 档；锚点从「每 20 轮纠偏」改为「每窗权重 + 周期回补」）
3. **取数分两个脑**：角色脑（本地 sqlite，毫秒）+ 图书馆（RAG，秒级），中间路由；上下文有硬预算，不堆垃圾。

### 0.1 接线状态（2026-11-27，代码为准）

| 模块 | 状态 | 证据 |
|---|---|---|
| 判读窗口 8 条（`MOOD_JUDGE_CTX`；5min 实际不可达，见 §3.1） | **【已接线】** | `mood_judge.split_windows` + `judge_and_store(records=)` |
| a/d（唤醒/掌控） | **【已接线】只记录** | `mood.update_ad`；不进 `v`/`chatty`，但进状态卡 |
| 检索规划器 P4 | **【已接线】默认后端仍是 json（可切 sqlite）** | `memdb.recall_planned`；`MEMORY_BACKEND=sqlite` 才生效 |
| 主动节奏 rhythm | **【已接线】** | `proactive._rhythm` |
| `v` 步进 + 锚点 | **【已接线】** | `mood.observe` / `mood.anchor`；`MOOD_UPDATE=v2`（回滚 `=delta`） |
| 判读 schema（desirability/cared/target） | **【已接线】** | `mood_judge.judge` 输出 `desirability(-3~+3)/cared(0/1/2)/anchor_level(必填)/relation_*`，代码合成 `impact`；`anchor_level` 缺失记 `ANCHOR_STAT.miss` 并打 INFO |
| 快速通道护栏 | **【已删除 2026-11-25】** | 拿对方的话猜她的心情；心情只来自她的 `[[THINK]]`（ADR-038） |
| 关系四量 | **【已接线】** | trust/respect 在 `services/relationship.py`；affinity 衰减在 `profiles.decay_affinity`；intimacy 派生 |
| 上下文预算 | **【已接线】已按线上块长校准** | **18 个**块级 `CTX_*` 进 `services/ctx_budget.py`：**13 个 token 口径 ×1.5** + **5 个字符口径 ×1.0**（`profile/evidence/summary` + `long_doc_evidence/long_doc_library`，后两个 2026-11-27 从 ×1.5 改成 ×1.0）；`CTX_ENFORCE=1` 只裁异常膨胀 |
| 好感 v2（痕迹求和） | **【已接线】默认关** | `AFF_MODEL=v1|v2`；v2 走 `profiles.affinity_now/add_affinity_event`，默认 v1（旧指数衰减），切换不改旧档案 |
| 收发件人阈值 | **【已接线】** | `ADDR_*` → `group_scene._infer_addressee` / 展示三档 |
| 检索 top-k / 多样性 | **【已接线】** | `RET_EVIDENCE_MAX/MEMORY_TOP/MAX_PER_PERSON/SAME_PERSON_RATIO/NEUTRAL_QUOTA/CHAR_LATENCY_MS` |
| 检索二期（倒 U / τ_kind / MMR） | **【已接线】** | `memdb.salience_weight/tau_days` + `services/mmr.py`；`MEM_*` |
| 注意力位置指标 | **【已接线】** | `scripts/eval_gold.py --stage position` + `scripts/position_report.py`（`gold_position` / `lost_in_middle`） |
| p95 周报 | **【已接线】** | `reply_pipeline` 落 `data/metrics/pipeline_stages.jsonl`；`scripts/p95_report.py` + 周报第七节 |
| 输出气泡 40 字 / 2 泡 | **【已接线】群聊 40 / 私聊 20（拍板不改）** | `OUT_BUBBLE_CHARS` / `OUT_BUBBLES_MAX`（超 2 泡合并转发，不截断）；私聊 `PRIVATE_MAX_HANZI=20` —— **20 是「超过就发合并转发」的阈值，不是字数上限**（用户 2026-11-27 拍板保持 20） |
| 运行档位（full/eco） | **【已接线】** | `config.POWER_MODE` + `power_on()`；与 `GROUP_WATCH_MODE` 正交 |
| 长答 long_doc | **【已接线】** | `services/long_doc.py`（判定 + `LONG_DOC_PIECES=3`×`LONG_DOC_PIECE_CHARS=1900`）被 `llm.py` 与 `reply_pipeline` 共用；`build_packet(long_doc=True)` 把 `memory_index` 放到 `CTX_LONG_DOC_EVIDENCE=6000`、`library` 放到 `CTX_LONG_DOC_LIBRARY=3000`（**字符**口径），渲染正文而不是目录；`scripts/check_runtime_wiring.py` 断言 3×1900≤6000 且块真有正文 |
| 工具检索 top6 | **【已接线】** | `llm.tools_for(key, text)` + `RET_TOOLS_TOP`（send/stay_silent 必留） |
| 收发件人归因 | **【已接线】仅高置信** | `mood_judge.resolve_target` + `[ADDR]`；conf≥`ADDR_ASSERT_MIN` 才改归属 |
| 互动边 `interaction_edges` | **【已接线】** | `memdb.note_edge` + `[EDGE]`；只记高置信「谁 → 谁」 |
| 记忆双写 json→sqlite | **【已接线】默认开** | `memory_v2._dual_mirror` + `[MEM] dual-write`；`MEMORY_DUAL_WRITE=0` 关 |
| 风格 custom 层 | **【已接线】** | `style_vault.submit/approve` + `white_moon.render`；≤`CTX_CUSTOM` 字，优先于 samples |
| 白月光借风格护栏 | **【已接线】** | `WHITE_MOON_BORROW/RATIO` + `PERSONA_EXPRESSIVENESS`；默认 0，只给 1 行事实 |
| profiles/style_samples/threads | **【已接线】** | `memdb.upsert_profile/add_style_sample/upsert_thread` + `[MEM] … sync` |
| 主动闭环 accept_rate | **【已接线】** | `proactive.note_reply/accept_rate`；回话窗口 `PROACTIVE_ACCEPT_WINDOW_H` |
| 群主动日上限 | **【已接线】** | `PROACTIVE_GROUP_MAX_PER_DAY=3` + `[PROACTIVE] 群 … 今天主动 N 次` |
| 人设底色 | **【已接线】** | `services/persona_traits.py`：warmth/forgiveness/trust_speed/reactivity/sensitivity |
| prompt 单入口去重 | **【已接线】** | `_staged` 存在时跳过 focus / 白月光重复注入（ADR-020） |
| 接话前读气氛 + 判意图 | **【已接线】第一版只加不减** | `services/atmosphere.py` + `reply_pipeline` 第 ⓪ 步；8 句标签语（带〔已回〕）+ 当前这句 → `mood/who/feel/others/tip` **+** `topic/asks/need_reply/why/replied/keywords/queries{memory,library,web}`；检索词驱动记忆/图书馆/档案库；**只提示，不改心情/好感**；插话闸门 `ATMO_GATE` **默认 0（关）**，先跑 `scripts/eval_intent.py` 到 85%/85% 再谈打开；`ATMO_*` |
| 出站清洗（send 工具 / 发送前兜底） | **【已接线】** | `aura_logic.clean_outgoing()`：剥 `[[X|…]]`、裸标记、动作括号、`<sticker:…>`、`<reply:…>`、`<at:…>`，`|||`→换行；`send` 工具用 `extract_stickers()` 把 `<sticker:tag>` 摘出来**真发表情**（`send_sticker_by_tag`），库里没有就静默跳过并回模型「别跟对方说你发了表情」 |
| 个人档案库（C 方案） | **【已接线】** | `services/archive.py`（`data/archive.db`，30 天以前，本地 sqlite+FTS5）；`archive_migrate.py`；`[[ARCHIVE|词]]`；**不依赖 RAG**；`ARCHIVE_*` |
| 引用回复 ≠ @ | **【已接线】** | `_at_me()` 先判真 `at` 段，再判 `_quote_me()`；引用走软通道；`services/quote_guard.py` 120s 内 3 次 → 冷却 5min |
| 识图跨 provider 降级 | **【已接线】** | `vision_agent._vision_targets`：429/额度用尽换模型/换 provider；`vision_fallback` 默认 qwen；去重**成功才写** |
| 输出 token 可配置 | **【已接线】** | `providers.resolve(role)["max_tokens"]`：`<ROLE>_MAX_TOKENS` > `LLM_MAX_TOKENS` > `GLM_MAX_TOKENS(glm)` > 默认（主聊天 **4000**（含 thinking，2026-11-27 用户拍板）/ 其它 800）；按 provider 上限 clamp；OpenAI 推理模型自动换 `max_completion_tokens` |

> 这张表就是「文档 vs 代码」的唯一对照。**表里写【设计】的，代码没跑；不要拿 §3 的数字去调线上。**
> `tuning.UNWIRED` **只剩 1 个**：`AFFINITY_GAIN`（2026-11-27 外部审计登记的
> 「设计先行键」——有默认值、代码里没有真实读点；接线后必须从名单删掉）。
> 其余设计键都有真实读点。**别把「名单很小」读成「已清空」。**
> `RET_LIB_LATENCY_MS` 是图书馆延迟告警阈值；`RET_STYLE_TOP` 控制白月光注入几句原话；
> `RET_WORLD_TOP` 控制图书馆候选池条数；`RET_FUSION_RRF=1` 可开多路 RRF。
> 线上验证方法见 [verification-runbook.md](verification-runbook.md)。

---

## 1. 一张图

```mermaid
flowchart LR
    IN["消息 + 收发件人<br/>（外部信息：→ 你 / → 某人 / → 大家）"] --> R{"① 路由<br/>查不查？查哪个脑？"}
    R -- 不查 --> CARD
    R -- 角色脑 --> RB["角色脑 sqlite<br/>档案/记忆/心情/风格"]
    R -- 图书馆 --> LB["图书馆 RAG<br/>诗词/百科/论文"]
    RB --> Q["② 配额 + 去重<br/>每人 ≤3 条，带出处"]
    LB --> Q
    Q --> CARD["③ 状态卡<br/>心情 / 关系 / 约束"]
    CARD --> G["④ 生成<br/>人格 + 锚点 + 工具"]
    G --> THINK["她理解<br/>第一人称心理活动 THINK"]
    G --> OUT["⑤ 输出<br/>分气泡 / 引用 / 配额"]
    THINK --> JUDGE["⑥ 判读（只读 THINK）<br/>desirability / cared / target"]
    OUT --> AUD["⑦ 审计<br/>mood_audit + 注入清单"]
    JUDGE --> KAL["心情步进<br/>impact → dv"]
    KAL --> ST["心情状态 v<br/>全局 / 会话"]
    KAL --> REL["好感<br/>只吃 dv，不读对话"]
    ST --> CARD
    REL --> CARD
```

八段：**接入 → 收发件人 → 该不该回 → 队列 → 路由取数 → 状态卡/生成 → 输出 → 判读/审计**。
和旧版的区别：收发件人、配额、审计全部画进来了；工具走单独车道（不取记忆/图书馆）；
判读在生成之后，写回**下一轮**的状态——**只写相对步进，不再有快速通道**（ADR-038 已删
ADR-013 的快速通道；`mind` 只认她的 `[[THINK]]`）。详见 [pipeline-map.md](pipeline-map.md) §8。

---

## 2. 文档地图

| 文档 | 回答什么 | 不回答什么 |
|---|---|---|
| **DESIGN.md（本文）** | 全局怎么串、口径是什么、决策是什么 | 具体公式推导 |
| [addressing-design.md](addressing-design.md) | 「谁 → 谁」怎么判、置信度、怎么用于归因/关系/记忆 | 状态公式 |
| [emotion-dynamics-design.md](emotion-dynamics-design.md) | 心情怎么更新、关系怎么涨/怎么衰减、人设怎么量化、安静/主动 | 检索实现 |
| [retrieval-design.md](retrieval-design.md) | sqlite 怎么建、时间/情绪怎么优先、关键词工程、和 RAG 怎么分 | 情绪公式 |
| [style-white-moon-design.md](style-white-moon-design.md) | 风格四层、白月光定位、注意力额度、借风格护栏 | 情绪公式 |
| [emotion-annotation-spec.md](emotion-annotation-spec.md) | 200 条情绪标注怎么抽/怎么标/怎么算一致率 | 代码实现 |
| [attention-design.md](attention-design.md) | 给多少、怎么排、引用怎么当锚点、成本怎么控 | 检索算法 |
| [prompt-skeleton-design.md](prompt-skeleton-design.md) | 各块的最终文本、空块规则、证据/anchor/分气泡结构 | 取数逻辑 |
| [proactive-design.md](proactive-design.md) | 主动接话 / 主动私聊：按心情+好感驱动的自驱车道 | 回复道细节 |
| [pipeline-map.md](pipeline-map.md) | 一条消息经过哪些阶段、还剩哪些缺陷 | 参数细节 |
| [design-foundations.md](design-foundations.md) | 为什么这么设计、参考什么、闭环还差什么 | 实现步骤 |
| [causal-assertions.md](causal-assertions.md) | 33 条可证伪断言：怎么判断「系统真的按设计在动」 | 实现细节 |
| [tuning-guide.md](tuning-guide.md) | 使用者怎么调参：改哪个键、安全范围、怎么验证 | 设计推导 |
| [design-audit-2026-11-25.md](design-audit-2026-11-25.md) | 历史文档 vs v2：矛盾 / 漏看 / 该归档 | 最终决策 |
| [mood-judge-audit-20261125.md](mood-judge-audit-20261125.md) | 判读链路审计：她读到什么 → 判读看到什么 → 标成什么；两段式建议 | 状态公式推导 |
| [affinity-quantification-design.md](affinity-quantification-design.md) | **好感 v2.5**：痕迹求和 / dv 三段式 / 巩固 / 分层门槛 / 刻度重标定 | 心情步进公式 |
| [verification-runbook.md](verification-runbook.md) | 线上怎么验：日志、回滚、比对脚本 | 设计推导 |
| [tuning.md](tuning.md) | 调参键全表（`scripts/dump_tuning.py --write` 生成） | 设计理由 |

> 已删除的旧内容：`retrieval` 里「金标准谁出题」、`foundations` 里与现设计冲突的 C1~C9 原文（只保留状态表）。
> `pipeline` 的 D 表以 `pipeline-map.md` 为准；本文不再声称某条 D 已修。

---

## 3. 口径表（唯一准，所有文档引用这里）

### 3.1 心情（VAD）

> **量纲（先看这条）**：所有公式（`step / saturation / P / R / Q / anchor`）一律在
> **归一化空间 `v ∈ [-1, +1]`** 计算；落盘/展示时 ×100（内部 -100~100，见 [tuning.md](tuning.md)）。
> `mood_audit` 记**归一化值**。别把 `abs(v)=32` 直接代进 `1−0.6×abs(v)`——那会得到负数，Δ 反向。

| 项 | 值 | 说明 |
|---|---|---|
| **输入** | **只有她的 `[[THINK]]`**（群聊上下文只用于理解「谁对谁说」） | 群聊内容本身**不**改心情；没有 THINK 就不更新（ADR-038） |
| `v` 效价 | `-1 ~ +1` | 难受 ↔ 开心 |
| `a` 唤醒 | `0 ~ 1` | 平静 ↔ 上头（**只记录**：置信加权 EMA + 独立衰减，不进 z） |
| `d` 支配 | `0 ~ 1` | 被压着 ↔ 我说了算（同上，不进 z） |
| 半衰期 | 群 **30min** / 私聊 **2h** / 全局 **6h** | 每轮先衰减再步进 |
| 观测档位 | `impact ∈ {-3,-2,-1,0,+1,+2,+3}`（7 档） | 判读报 `desirability(-3~+3) × cared(0/1/2)`，代码合成 impact（2026-11-26 v2.5） |
| 观测 `z` | `z = clamp(v⁻ + dir×step×sens, −1, +1)` | **伪观测**（不是独立测量）；`z−v⁻` 才是这次位移 |
| 判读窗口 | **8 条**（`MOOD_JUDGE_CTX`） | 段 ≤30s（`SEGMENT_MAX_SEC`），所以「5min」永远到不了；实际是「段内切窗，最后一窗 3~8 条」。前面的窗只 `note_window`、不调模型（省 ~80% 调用） |
| 观测步长（名义） | `±1 → 0.10`；`±2 → 0.22`；`±3 → 0.40` | 代码查表（`MOOD_STEP_3`，2026-11-26 新增） |
| 实际位移（首条 / 稳态） | 实测（2026-11-27）：首条 conf=0.9 → **dv≈0.091**，conf=0.6 → **dv≈0.068**（`P0=0.25`，不是 0.004）；稳态 conf=0.9 → **dv≈0.015**；`±3` 首条 ≈0.20~0.30（需 conf≥0.68） | 由 K 与 saturation 决定；`0.004` 是「稳态 conf=0.6」的值，旧文档误写进「首条」格 |
| 人设敏感度 | `sensitivity ∈ [0.6, 1.4]` | 乘在观测步长上 |
| 饱和 | `Δ × (1 − 0.6×abs(v))` | 越接近 ±1 越难走 |
| 步进硬上限 | `abs(Δv) ≤ 0.35` | **±3 档会触发**：`0.40×1.4×K(0.906)=0.507` 被砍到 0.35（砍 31%）。**心情层**确实被削平；但**好感层不再无补偿**：v1/v2 都吃 `dv`，首条高置信的 `±3` 能走到 `dv≈0.30~0.35` → 进三段式**尾部**（实测单次 ≈0.12，上限 `AFF_V1_CAP=0.15`），也就是「一见钟情 / 心碎」这一档。注意尾部要求 `P` 高（刚静默过 / 首条），稳态连发同一档进不去 |
| 不确定度初值 | `P0 = 0.25` | 初始不确定度 |
| 过程噪声 q_rate | `0.02 + 0.10×reactivity`（每小时） | `Q_eff = q_rate × Δt_h`，随 Δt 缩放 |
| 突发事件回补 P | `|impact|≥2` 时 `P⁻ = max(P, min(0.20, P+0.05))`（`MOOD_P_EVENT_BOOST/CAP`，**只补不降**） | 2026-11-26 用户拍板；2026-11-27 修：旧写法 `min(max(floor,cap), p+boost)` 在 P0=0.25 > cap=0.20 时会把 P **调低**。补后 ±2→0.074、±3→0.135，`±1` 不变 |
| 观测噪声 R | `0.02 + 0.60×(1−confidence)²` | 置信低 → 几乎不动 |
| 锚点触发 | **每窗按权重混入**；`P` 每 **20 个判读窗口**回补一次（`mood.observe`）；静默回补由**定时器** `anchor_tick` 负责（阈值 `MOOD_ANCHOR_IDLE_MIN=30min`；只认 `MOOD_ANCHOR_THINK_AGE_SEC=**2100s/35min**` 内的 THINK —— 必须 ≥ 30min + tick 间隔 5min，否则几乎永远为假） | `anchor_level` 必填（2026-11-26 用户拍板）；按会话计数，不是全局。**注意**：`observe` 内部没有「静默」判断——它每窗刷新 `anchor_at`/`updated_at`，静默分支会恒 False，2026-11-26 已删 |
| 锚点映射 | `level ±2 → ±0.7`、`±1 → ±0.35`，**叠在她本人基线上**：`z_abs = clamp(baseline_v + map(level))` | `level` 是相对基线的档位，不是绝对心情值 |
| 锚点增益 | 每窗混入 `W = 0.45×confidence`（`MOOD_ANCHOR_WEIGHT`）；**全局锚点再 ×`GLOBAL_SHARE`(0.35)**；独立锚点 `K_a = 0.25×confidence`；死区 **0.15（软死区：smoothstep 平滑，不是硬跳过）**；单次 `|Δ_a| ≤ 0.20`（`MOOD_ANCHOR_MAX_MOVE`） | 每窗最多挪「差距的 45%」→ `anchor_level` 占最终 `v` 的大头；周期锚点只补 `P`，不重复拉。**全局 ×0.35 是 2026-11-27 修的**（以前事件步进乘了、锚点没乘，导致最吵的群把全局拉走）；按本表算全局锚点必须记得乘 |
| ~~强负面护栏~~ | **已删除（ADR-038）**：不再按「@她 + 辱骂词」钳 `v` —— 那是拿对方的话猜她的心情 | 没有 THINK 就不改心情；`mood.fast_guard` 函数保留，**线上无调用**（只有 `test_new_features` 用它做回归） |

> **`P` 到底在管什么（2026-11-27 补，回应「卡尔曼是不是摆设」）**
>
> 「主项是锚点 EMA」不等于「`P/Q/R/K` 可以删」。`P` 决定 `K = P/(P+R)`，而 `K` 是**残差项的权重**：
>
> - 同一话题连续判读 → 每窗 `P` 衰减 → `K` 变小 → **同样的话对她的影响递减（脱敏）**；
> - 静默 / 每 20 窗 → `P` 回补 → 下一句重新「有分量」；
> - `|impact|≥2` → `P` 再补一点（`MOOD_P_EVENT_BOOST`，只补不降）。
>
> 删掉 `K` 之后：连发十句和只说一句效果一样，`dv` 会变成固定值 —— 好感的
> 「尾部突变（一见钟情 / 心碎，`|dv|≥0.30`）」通道也会跟着消失。
> **复杂度确实高，但每块都有读点**；要简化应该先砍「每 20 窗回补」这类 cadence，
> 而不是整台状态机。

> ⚠️ **谁在管事（2026-11-26 明确，别读错）**：ADR-042 之后，`v` 的主项是
> `W(0.45)×conf×(z_abs−v)`（绝对档位），`P/Q/R/K` 只决定**残差步进**那部分
> （稳态 `K≈0.15~0.2`，`±3` 只挪 ≈0.04~0.05，不到锚点的 1/4）。
> 也就是说这套参数是「**绝对档位 EMA + 一点残差**」，不是「卡尔曼滤波为主、锚点纠偏为辅」。
> §3.1 上面那些行仍然准（数值没变），但**权重不是**：不要把 `K` 当成主要增益。
>
> ✅ **`±3 一天最多一次`已接线（2026-11-27，ADR-044）**：不再只靠 prompt 纪律。
> `mood.cap_daily_impact()` 按**会话**按自然日计数（`MOOD_IMPACT3_DAILY_MAX=1`），
> 超了把 ±3 降成 ±2（`MOOD_IMPACT3_OVER_TO=2`）——保留「这是件大事」，只是不再顶格；
> `0` = 不限（回滚）。降档会打一条 `[MOOD-JUDGE] ±3 当日额度已用完` 日志。
>
> ⚠️ 「绝对档位」是**模型对此刻整体水平的重估**，不是带数值参照的测量：
> 判读故意不给当前 `v`（防回声，ADR-029），所以它只能把这一窗的语气重新映射到
> `baseline + map(level)`，再用 `W` 混进去。它防的是漂移，不是精确刻度。
> 另外差值 <`ANCHOR_DEADBAND=0.15` 时**不是硬跳过**，而是按 smoothstep 衰减混入
> （软死区，2026-11-27）：`|diff|→0` 时权重→0、`|diff|→0.15` 时权重→1，
> 边界处与全速连续，所以它仍是「每窗 EMA」，不会在 0.149→0.151 之间 bang-bang；
> 小抖动位移 <0.01，不写 `mood_history`。

### 3.2 关系（四个量，单位不同）

| 量 | 范围 | 基线 | 半衰期 | 谁改它 |
|---|---|---|---|---|
| `affinity` 好恶 | `-1 ~ +0.99` | `0` | v1：**7 天 × 深度系数**；v2：幂律痕迹（T=10 天） | v1 = `0.25×|Δ|` 事件项 + `0.30×三段式(dv)`（仍要求 `cause=speaker`）；v2 = 只吃 `target ∈ {speaker,recipient,other}` 的 `dv` |
| `trust` 信任 | `0 ~ 1` | `0.20` | **45 天** | 只由「可信/失信」事件改 |
| `respect` 尊重 | `0 ~ 1` | `0.30` | **90 天** | 只由「能力/地位」事件改 |
| `intimacy` 亲近 | `0 ~ 1` | `0` | **30 天**（`REL_INTIMACY_HALF_LIFE_DAYS`，as-built 在 `profiles.intimacy()`） | 由相处量 + 情绪 + 时间算 |

深度系数：`τ_eff = 7天 × (1 + 2×intimacy) × (1 + 0.5×近7天互动次数归一化)`。
→ **越亲近，好感掉得越慢**（投资模型）。

> `intimacy` 以**派生量**为准（`bond notes` 的条数/情绪/时间 + affinity 贡献）；
> `emotion §3.1b` 的 `Δintimacy` 只是**写 notes 时的权重建议**，不是第二个状态变量——不要两者同时累加。

> `relation_relevance`：judge 输出 [0,1]；**缺省 0.5 = 「不确定，半信」**。
> 不想给未知事件加好感，就把缺省设 0；`world`/`self` 恒 0（只动心情，不动关系）。
>
> **关系审计现状**（2026-11-25）：
> `affinity` 有 `affinity_log`（最近 50 条：at/delta/to/why）；
> `intimacy` 由 `bond notes` 推导，notes 本身就是输入审计；
> `trust` / `respect` **已实现**：写入口 `relationship.note_judge`（读 judge 的
> `relation.{trust,respect}`，只由关系事件触发，普通闲聊不动），
> 流水 `data/relationship_log.jsonl`，状态卡由 `relationship.render_line` 输出。
> **`affinity` 必须双向**（v1 口径）：涨跌同一套步长，单次夹在 **±0.15**
> （`AFF_V1_CAP`，= 日上限；旧文档的 ±0.10 已废）；负向再乘
> `neg_weight(当前好感)`（越喜欢越宽容）。
>
> **已修（2026-11-26 审计 E6）**：
> - `relationship.bump` 不再对 `trust/respect` 的负向乘 `neg_weight(affinity)`——
>   宽容只作用于好感；信任被骗就是被骗，不因喜欢而罚轻。
> - `REL_DAILY_CAP` 的说明改为「信任+尊重每日净变化上限」；好感的日上限是
>   `AFFINITY_DAILY_CAP`（v1 用，v2 不用）。**一个概念一个键**。
>
> **输入**：`dv` 只来自她的 `[[THINK]]` 判读。**v2 好感只吃 `dv`**；
> **v1（默认）也吃 `dv`**（2026-11-26 ADR-043），只是额外保留一个事件重量项：
> `Δaff = 0.25×|Δ|×gain(≈0.10)×人设项 + 0.30×三段式(dv)`，单次上限 0.15，且仍要求 `cause=speaker`。
> 两者都**不读群聊原文、不读关键词、不读「事件类型」**——同一句话她理解成什么，才算什么。
> 差别只在：v2 用痕迹求和 + 巩固 + 分层门槛，v1 用指数衰减 + 每日上限。
>
> **v2（2026-11-26，`AFF_MODEL=v2` 启用，默认 v1）**：不再是标量指数衰减，而是
> `a = bond + Σ 痕迹`（ACT-R 幂律，T=10 天），单次权重按 `dv` 三段式（日常/显著/尾部，
> 单次夹 ±0.42），高区「巩固」把一部分沉淀进 bond，分层门槛（0.75 暧昧 / 0.95 唯一的爱）。
> 35 个 `AFF_*` 键在 `core/tuning.py`，完整公式见 `docs/affinity-quantification-design.md`（v2.5）。
> **本节其余数字（±0.15、7 天半衰）只描述 v1**。

### 3.3 上下文预算

> 口径：**最终都以字符计，但两种来源**：`CTX_*` 存的是 **token 值**，
> `limit_chars` 统一 **×1.5** 转字符；例外：`CTX_PROFILE / CTX_EVIDENCE / CTX_SUMMARY`
> 本身是**字符**（×1.0）。三层数字别混（2026-11-26 重新对齐）：
>
> | 层 | 数字 | 谁在用 |
> |---|---|---|
> | 设计目标（本表） | **2120 字符** | 只用于 `check_design_docs` 校验文档自身 |
> | 运行时单块上限 | **token 值之和** 常态 **4620**｜全量 **5040**（`ctx_total(all_blocks=True)`；实际字符和 **6540 / 7170**） | `services/ctx_budget.py::limit_chars`（token 口径 ×1.5），`CTX_ENFORCE=1` 才裁 |
> | live 总池 | **`PROMPT_BUDGET=2200` 字符** | `services/prompt_builder.py` 真正裁剪 |
>
> **2120 不是任何真实配置**：它是「常态目标」，用来发现「某个块偷偷膨胀」；
> 线上真实裁剪只看 `PROMPT_BUDGET=2200`。`CTX_*` 合计（常态 `ctx_total()`=**4620**、
> 全量 `all_blocks=True`=**5040**）也不是总池，是**单块上限之和**——
> 单块都顶满当然超 2200，所以总池才是最后一道闸。
>
> ⚠️ **`PROMPT_BUDGET=2200` 只约束 `BLOCK_BUDGET` 里的动态块**
> （profiles / memory_index / library / mood / relation / quiet / lore…）。
> `persona` / `protocol` / `post_rules` / `tools` / `history` 走
> `ctx_budget.observe`，默认 `CTX_ENFORCE=0` **只统计不裁剪** ——
> 所以「整份 prompt ≤2200」不成立；protocol 一个块上限就是 1500 token×1.5=**2250 字符**。
> **2026-11-27**：整份 prompt 现在每次调用都记 `prompt_total`（`ctx_budget.observe_prompt_total`），
> 超 `CTX_SOFT_CAP=2700` 打告警，`/测函数 预算` 与 `/测函数 链路`（`prompt_chars`）都能看到；
> 仍**不裁剪整份**（裁了会破坏前缀缓存），所以 `2200` 请只当作「动态块池」，不要当总闸。
> 常态 = 单目标 / 无工具意图 / 无 custom；其余场景单列。
> as-built 的分块在 `services/prompt_builder.py::BLOCK_BUDGET`（profiles/mood/relation/quiet/lore…），
> 和本表**不是一一对应**；本表是目标拆分。

| 区 | 上限（字符） |
|---|---|
| persona（她是谁） | 400（运行时单块上限 `CTX_PERSONA=700` token ×1.5 = 1050 字符，只防异常膨胀） |
| expression（她自己的口癖） | 120 |
| 协议 + 状态卡 | 180 + 120 |
| profile（身份/性格/称呼/好感词/印象） | 160 |
| 白月光（档案里 1 行事实） | 40 |
| 群摘要 | 120 |
| 证据（≤5 条） | 500 |
| 历史（≤8 行） | 150 |
| 引用锚点 | 60 |
| 原话样本（3 句） | 150（custom 时替换为 ≤250） |
| 表达节奏（rhythm） | 40 |
| post_rules | 80 |
| 工具（top6） | 300（无工具意图 = 0） |
| 多目标（≥2 人） | 60 |
| 可选块（lore / conv_state / focus / threads） | 从池子里抢，不单列；lore ≤520 |
| **常态合计（单目标 / 无工具 / 无 custom）** | **≈ 2.1k（2120 字符）**；运行时单块上限 `ctx_total()`=**4620 是 token 值之和**（实际字符和 **6540**；含 tools/multi **7170**） |
| 有工具意图 | ≈ 2.4k（2420） |
| custom 替代样本 | ≈ 2.2k（2220） |
| 多目标 | ≈ 2.2k（2180） |
| **硬上限** | **12k（仅 `intent=long_doc` 且用户明确要长答）；否则设计上限 2.7k / live 池 2.2k** |
| long_doc：证据（长文，3 篇 × ≤1900 + 出处 300，替代上面的 500） | 6000 **（已接线，字符）**：`build_packet(long_doc=True)` 放开 `memory_index` 上限到 `CTX_LONG_DOC_EVIDENCE=6000`，按 `LONG_DOC_PIECES=3` × `LONG_DOC_PIECE_CHARS=1900` 渲染正文；staged 路径由 `reply_pipeline` 读同一个 `services/long_doc.py` |
| long_doc：图书馆 | 3000（单独给，不含在上面 6000 里；`CTX_LONG_DOC_LIBRARY`，字符口径） |
| long_doc：其它块 | persona / 协议 / 状态卡 / 历史 / anchor **不变**（不随 long_doc 膨胀） |
| long_doc 合计 | ≈10.6k（2120−500+6000+3000），≤12k |

> **裁剪顺序**（避免「单条上限 > 块上限」）：
> 证据用 `summary`（≤80 字/条），`RECALL_TEXT_MAX=600` 是展开全文、**不进 prompt**；
> anchor 只放 ≤60 字摘要，`QUOTE_MAX_CHARS=500` 是引用解析上限；
> 群摘要块 120 字，`SCENE_SUMMARY_MAX_CHARS=160` / `CONV_STATE_MAX_CHARS=420` 是存储上限；
> 历史只放 8 行，`MAX_GROUP_RECENT=16` / `HISTORY_MAX=20` 是现场缓存；
> persona 400 字是目标，`PERSONA_GROUP_CHARS=0` 表示不按群限制、进 prompt 时仍按 400 裁。

**判读预算**（生成有预算，判读也要有；否则活跃群会线性涨）：

| 项 | 上限 |
|---|---|
| 单窗 | 8 条消息（`MOOD_JUDGE_CTX`） |
| 单段 | ≤5 窗（40/8，段上限天然封顶）；**passive 窗只记 `note_window`，不调模型**（2026-11-26，省 ~80% 调用） |
| 每会话每小时 | ≤30 次（`MOOD_JUDGE_MAX_PER_HOUR`；超了丢掉最早的窗，只判最近几窗） |
| 单窗 token | **实际 ≈2k**（规则段 ~1.1k 字 + 8×160 ctx + archive/think/src）；1.2k 是旧估算，已不成立 |
| 锚点 | 每窗按权重混入（`MOOD_ANCHOR_WEIGHT=0.45`）；每 20 窗回补 `P`；静默定时器**只认 30 分钟内的 `[[THINK]]`**（`MOOD_ANCHOR_THINK_AGE_SEC`），且吃判读预算 |

> **三套口径，别混**（2026-11-25 明确）：
> ① 本表 = **常态目标**，加总用 `design_constants.budget_total()`（2120），`check_design_docs` 校验它；
> ② `tuning.CTX_*` = **运行时单块上限**（合计 4620，`ctx_total()`），只防单块膨胀；
> ③ `PROMPT_BUDGET=2200` 字符 = **动态块池**（只约束 `BLOCK_BUDGET` 里那 10 个动态块，
> **不是整份 prompt 的总闸**，见下面的 ⚠️），`prompt_builder` 真正裁剪用；
> `CTX_ENFORCE=0` 时 ② 只统计不裁剪 —— 那种状态下它叫「观测」，不叫「硬截断」。
> 旧表漏列 profile / rhythm / post_rules / 多目标，所以「2.1k」只在常态口径下成立。
> 风格相关合计（expression + 样本或 custom + rhythm + 白月光）：**单块 token 值之和 = 730**
> （`CTX_STYLE_TOTAL`，即 `CTX_EXPRESSION 120 + CTX_STYLE 250 + CTX_RHYTHM 180 +
> CTX_WHITE_MOON 180`）；这几个键在 `ctx_budget` 里是 **×1.5**，
> 所以**运行时字符上限 = 1095**。
> ⚠️ 2026-11-27 更正：旧文写「≤730 字符（token 口径约 487）」把单位说反了 ——
> 730 是 token 值、不是字符；「487」是 730÷1.5 的产物，只在该假设下成立。
> 别把这里的 730 和 `design_constants.style_total()`（**目标拆分** 350 常态 / 450 custom）混为一谈。
> 旧文档写「≤450 token」是 v1 的数，已作废。见 [style-white-moon-design.md](style-white-moon-design.md)。

### 3.4 检索

| 项 | 值 |
|---|---|
| 角色脑延迟目标 | ≤50ms |
| 图书馆延迟 | 目标 **3s**｜现状 **10s**｜超时 **14s**（`AGENT_LIBRARY_TIMEOUT`）｜硬 deadline **16s**（`PIPELINE_BUDGET`）。**降级**：超时用已到块回复（ADR-019）；14s 是「最坏情况」，不是目标 |
| 候选池 | 全库约 **8000** → **实际 48**（`_cand = max(48, min(RET_PLAN_CAND, limit×8))`，limit=5；`RET_PLAN_CAND=400` 只是上限，常态到不了）→ **最终证据 ≤5**。**没有独立的「预过滤 50」阶段** |
| 候选配额（不是最终条数） | memory **4** / world **3** / profile **2** 是**各路最多给几条候选**，合计 9 条候选再按分数取 5——**不是最终 9 条**；style 走 ⑦ 块不进证据池；tools 6 单独注入 |
| 每人配额 | `cap = max(1, min(RET_MAX_PER_PERSON, round(limit × RET_SAME_PERSON_RATIO)))` —— **两个键联合约束**，不是独立键。5 条时 = `min(3, 3) = 3`；把 `RET_MAX_PER_PERSON` 改到 8 仍被 ratio 夹到 6，反之亦然 |
| 证据选法（顺序固定） | ① 按人分组（cap 见上）；② 组内按分数；③ 跨组取 top5；④ 反向/中性保底；⑤ 最后 MMR 去重 |
| 反向保底 | 在 ③ 之后、⑤ 之前：top5 全同向时，**替换 `picked[-1]`（末位）**；代码里**没有**「插到第 1 位」的逻辑——旧文档写错了 |
| long_doc 判据 | ①「详细讲讲/展开说/写个总结/分析一下」②「这本书/论文/代码讲了什么」→ **单轮长答**（直接分配 6000+3000，不存在「先短后长」）。③「图书馆已返回 ≥3 篇」只用于 ①② 未命中时的补充判定：**不追加第二条回复**，命中结果留给下一轮 `[[LIBRARY]]` |
| 长文模式例外 | long_doc 的 6000 是**证据块**（3 篇 × ≤1900 + 出处 300）；「每人 ≤3 条 / 反向保底」只作用于常态短证据，不适用于长文 |
| RRF `k` | 60 |
| 过滤优先级 | **人 → 时间 → 情绪 → 标签 → 关键词** |

### 3.5 收发件人（详见 addressing）

| 信号 | 置信度 |
|---|---|
| @她 / 引用她 | 0.99 / 0.95 |
| 开头称呼「X，…」 | 0.80 |
| 正文点名 | 0.70 |
| 上一句提问 → 这句回答 | 0.65 |
| 用了「你」（非你们） | 0.60 |
| 反应词/附和相邻 | 0.55 |
| 短句冲最近被提到的人 | 0.50 |
| 无信号 | `unknown` |

展示阈值：`≥0.8` 断言「→ X」；`0.5~0.8` 写「像是在回 X」；`<0.5` 写「不确定」。

**Address → target 的桥**：**实际由 `resolve_target(cause, address)` 决定**，
judge 输出的 `target` 字段会被覆盖（`mood_judge.py:1147`）。
`resolve_target` 只认 `to_kind == "person"` 且 `conf ≥ ADDR_ASSERT_MIN`：

| `to_kind` | 实际 `target` | 说明 |
|---|---|---|
| `bot` | `speaker`（**仅当 `cause=speaker`**） | 冲她说。`to_kind=bot` 本身不进 `resolve_target` 的 `ok` 分支，实际走 `cause` 映射；`cause=environment` 时仍是 `world` |
| `person` | `recipient` | 冲另一个人说（唯一会改归属的分支） |
| `group` | **`speaker`**（不是 `world`） | 代码走 `cause` 映射；旧文档写 `world`，与代码不符 |
| `unknown` | judge 决定（缺省 `world`） | **低置信（conf < `ADDR_ASSERT_MIN`）时退回 `speaker`**——这是安全默认：宁可记到说话人，也不记到无关的人。不要算成 `recipient`（旧文档写「不要算成 speaker」，与代码相反，已更正） |

### 3.6 口径缺口与定案（G1~G10）

> 这 10 条是「文档已引用、但定义缺失或数值自相矛盾」的地方，**2026-11-25 已全部定案**。
> 口径类由 `scripts/check_design_docs.py` 锁住；语义类见对应 ADR。
> 保留本表是为了记录「为什么当时必须改」，实现时**不要回退**。

| # | 缺口 | 修复前 | 建议 | 不修的后果 |
|---|---|---|---|---|
| G1 | `relation_rel` 未定义 | `emotion §3.2` 公式引用；`retrieval` 建列 `DEFAULT 0` | 改用 judge 的 `relation_relevance`，`DEFAULT 0.5` | **Δaff 恒为 0，好感永不增长** |
| G2 | `intimacy` 无更新式 | 却驱动 `τ_aff`、关系阶段、proactive warm | `Δintimacy = 0.03×(1−i)×depth`，depth 取 0/0.5/1 | 关系系统冻结在初始值 |
| G3 | `z` 定义在预测之上 | `z = v⁻ + dir×step×sens` | ✅ 已定 A+锚点：步进 + **每窗**绝对档位权重（ADR-042；原为每 20 轮） | 纯相对会漂；纯绝对会回声 |
| G4 | `Q` 不随 Δt 缩放 | 每次更新加一次 | ✅ `q_rate=0.02+0.10×reactivity`，`Q_eff=q_rate×Δt_h` | 群越热闹越易变 |
| G5 | `max_step` / 硬限幅 0.35 | 5 档时 `Δ ≤ 0.285` 不触发；**7 档（±3）后会触发**（0.507 → 0.35） | ✅ 删 `max_step`；0.35 保留为「单次上限」（有意设计，2026-11-26 起生效）；`saturation` 仍在 | 删护栏后又加档位，护栏重新变成必需 |
| G6 | `a/d` 无档位→步长表 | 只定义了 `impact` 的步长 | ✅ 一期 `a/d` 只记录、不进 `z`；**2026-11-25 已接线**（`mood.update_ad`，置信加权） | a/d 算不出来（且 clamp 域错） |
| G7 | 快速通道数值无效 | `conf=0.4 → Δ≈0.03` | ⚠️ **已删除（ADR-038，2026-11-25）**：`v=min(v,−0.45)` 那套是拿对方的话猜她的心情，不再实现；没有 `[[THINK]]` 就不改心情 | 「刚被骂完还在笑」由「她自己的 THINK」解决，不由护栏解决 |
| G8 | 12k 无触发条件 | 「仅 mixed + 长文档」 | ✅ 仅 `intent=long_doc` 且用户明确要长答；否则 2.7k | 预算后门 |
| G9 | 20 字 vs 拒绝权 | `§9.1` 按字符，人格按气泡 | ✅ 改按气泡：单泡 ≤40 字、总 ≤2 泡 | 一解释就被塞进合并转发 |
| G10 | 红线 3 vs 关键词路由 | 「不用正则猜语义」但路由/破冰都在用词表 | ✅ 改为「规则可**召回**、不可**判决**」；快速通道已删，**不再有例外** | 实现时不知道听哪条 |

> ✅ **G1~G10 全部定案（2026-11-25）**，见 ADR-026~035。

---

## 4. 决策记录（ADR）

| # | 决策 | 状态 | 理由 |
|---|---|---|---|
| ADR-001 | 不做事件切分，做 8 句窗口冲击归因 | ✅ | 群聊发散，事件边界不可得 |
| ADR-002 | LLM 报档位 + 置信度 + 目标人，不报小数 | ✅ | 语义强、刻度弱 |
| ADR-003 | 状态更新只有一条链路：**置信加权步进**（每轮）+ **绝对锚点**（每窗按权重混入 + 每 20 窗 / 静默 35min 回补 `P`） | ✅（2026-11-26 由 ADR-042 加重锚点权重） | 旧三套公式并存；相对观测不能纠偏，故补锚点 |
| ADR-004 | 事件研究（实际−预期）**二期再做** | 🟡 二期 | 一期先把观测/滤波跑通，避免公式叠加 |
| ADR-005 | 记忆拆 `storage_strength` / `retrieval_strength` | ✅ | 「重要不忘 + 旧事变淡」 |
| ADR-006 | 双脑分离：角色脑 sqlite / 图书馆 RAG | ✅ | 延迟、隐私、更新频率不同 |
| ADR-007 | 全局心情是**慢变量**，不由会话均值反推 | ⚠️ 半实现 | 旧版循环定义（旧 X5）。**实际**：每次会话位移的 `GLOBAL_SHARE=35%` 累加到全局——不是「均值反推」，但也不是独立变量，最吵的群仍主导全局 |
| ADR-008 | 单一状态源（一个写入口，其余只读） | ✅ 已接线（**mood 有 flock，relationship 没有**） | `mood._serialized` 串行所有写入口 + `_save` 用 flock 保护 tmp→replace；读接口不抢锁。**已知缺口**：`relationship._save` 只有 tmp→replace，无 flock（多进程同时写可能丢一条），2026-11-26 审计记录 |
| ADR-009 | 评估先行：先检索（0 费用），再生成（预算内） | ✅ | 先有尺子 |
| ADR-010 | 人设底色参数化（sensitivity/warmth/forgiveness/trust_speed/reactivity） | ✅ | 冷漠/热情不能一个系数 |
| ADR-011 | 收发件人是一等公民，参与判读/记忆/关系归因 | ✅ 新增 | 现在只用于展示（旧缺口） |
| ADR-012 | 上下文预算单一口径：常态 ≈2.1~2.2k / 上限 12k | ⚠️ 被 033/039 取代 | 旧版 2.1k 与 12k 混用（旧 X3）；12k 现仅 long_doc |
| ADR-013 | 判读默认晚一轮；强负面消息走**快速通道** | ⚠️ 快速通道被 ADR-038 删除（判读晚一轮保留） | 解决「刚被骂完还在笑」（旧 X9）；但它是拿对话猜心情 |
| ADR-014 | 心情检索走结构化 `(about_qq, 方向, 时间)` | ✅ | 实测 BM25 0.33 → 结构化 1.00 |
| ADR-015 | 风格分四层（persona/expression/samples/custom），风格相关 **token 值之和 ≤730（运行时字符 ≤1095）**（旧 450 token 已作废；2026-11-27 更正单位） | ✅ | 旧版重复注入 + 1200 字样本挤爆预算 |
| ADR-016 | 白月光 = **关系参照系**，不默认借风格；借需 `borrow_ratio` + 人格护栏 | ✅ | 旧语义会「变成别人」 |
| ADR-017 | 情绪外部效标：200 条标注，50 条双人重叠，impact QWK ≥0.60 | ✅ | 100 条检索集测不了档位 |
| ADR-018 | 自然语言工具请求走**回复道**（模型决定做不做），命令直达工具道 | ✅ | 保留人格与拒绝权 |
| ADR-019 | 任一环节超时 → **用已到的块回复**；慢块后置优化 | ✅ | 不让 6s 总结拖死回复 |
| ADR-020 | prompt 单入口 + 固定顺序 + 空块不出现 + `_staged` 不重复注入 | ✅ | 现在块按调用顺序 append，重复且难查 |
| ADR-021 | 归因用 5 值：`speaker/recipient/other/world/self` | ✅ | 旧 3 值会把「第三个人骂她」记成 world，好感不动 |
| ADR-022 | 表达节奏（旧 attention）并入风格层；不参与心情/关系/记忆数值，仅作自驱强度 ±10% | ✅ | 同名不同物；`off` 不再等于「关掉主动」 |
| ADR-023 | 主动接话/主动私聊 = 独立自驱车道，按心情+好感驱动 | ✅ | 触发条件与回复道不同，混在一起互相干扰 |
| ADR-024 | 记忆「每 turn 都记，Δ 只决定加粗/检索权重」 | ✅ | Δ 不是记录门槛；sqlite 后检索会变，记录语义不变 |
| ADR-025 | 识图：不建本地角色库，云端源+视觉+别名表；只做动漫；算力机异步；**暂缓** | 🟡 暂缓 | bot 无 GPU；本地图库维护成本高；真人脸合规风险 |
| ADR-026 | 好感关系项统一为 `relation_relevance`（judge 输出，`DEFAULT 0.5`） | ✅ | 旧 `relation_rel` 无定义且 DEFAULT 0 → Δaff 恒 0 |
| ADR-027 | `intimacy` 用 depth 增量式（0/0.5/1），**30 天半衰期**；负面事件不直接降 | ✅ | 它驱动 τ_aff/阶段/proactive；不联系会变淡（depenetration） |
| ADR-028 | 删 `max_step`（恒不触发），只留 `saturation`；0.35 降为护栏 | ✅（**2026-11-26 起 0.35 会触发**） | 三个护栏里两个是死的；但 ±3 档让 0.35 重新变成有效单次上限 |
| ADR-029 | G3 定为「A+锚点」：相对位移为主，每 20 轮绝对锚点纠偏；锚点判读**不看当前 v** | ⚠️ 由 ADR-042 改为「每窗按权重混入」 | 纯相对会漂、纯绝对会回声 |
| ADR-030 | `Q` 改为速率 `q_rate=0.02+0.10×reactivity`，按 `Δt_h` 缩放 | ✅ | 旧 Q 按「每次更新」累加，活跃群更易变 |
| ADR-031 | `a/d` 一期只记录、不进 `z/Δ` | ✅ | 没有档位→步长映射；clamp 域也不对 |
| ADR-032 | 快速通道改**护栏** `v=min(v,−0.45)`（轻 −0.25）、`P×1.5`，不走步进 | ⚠️ 被 ADR-038 删除 | conf 0.4 时步进只挪 0.03；但护栏仍是在猜她的心情 |
| ADR-033 | 12k 仅在 `intent=long_doc` 且用户明确要长答时启用，否则 2.7k 硬截断 | ✅ | 12k 不能是默认逃生舱 |
| ADR-034 | 输出按**气泡**限：单泡 ≤40 字、单轮 ≤2 泡；超 2 泡才合并转发 | ✅（2026-11-26 修了收尾块仍按 20 字的问题；**私聊生成侧仍 20**，见 §9 红线 1 的更正） | 按字符限会把「拒绝+解释」塞进卡片 |
| ADR-035 | 规则可**召回**、不可**判决**（快速通道的保守钳制已随 ADR-038 删除） | ✅ | 词表路由与「不用正则猜语义」自相矛盾 |
| ADR-036 | 实现前先有 33 条**因果断言**；断言红了不许改断言，先判「设计错」还是「实现错」 | ✅ | 100 题测入口、200 条测档位，都测不到轨迹 |
| ADR-037 | 锚点计数**包含**被 relevance 跳过的窗口（否则噪声群永不锚定） | ✅ | 低信息群 `P→0` 会冻结状态 |
| ADR-038 | **心情只来自她的 `[[THINK]]`**：没有 THINK 的旁观窗口不写 `v`；删除快速通道；`judge.mind` 降级为猜测 `guess` | ✅ 已接线 | 2026-11-25 用户：「不能拿外界对话猜她的心情；她理解错了是她的事，标签块只管是不是对她说的」 |
| ADR-039 | long_doc 只膨胀证据 6000 / 图书馆 3000；人设与现场块不变 | ✅ | 放大 persona 会打掉前缀缓存 |
| ADR-040 | 检索规划器：**结构化过滤在前**（人/时间/情绪/标签），关键词与打分在后；sqlite 后端默认启用 | ✅ | 旧版把时间/情绪当权重，「上周的事」会混进去年（R2） |
| ADR-041 | 证据选法：每人 ≤3 条 + 反向保底；tools 不占证据名额 | ✅ | 纯按分数会 5 条全同一人。**（原编号 ADR-038，与「心情只来自 THINK」重号；2026-11-26 改为 041，排在 040 之后）** |
| ADR-042 | **`anchor_level` 必填，且占最终心情的大头**：每窗按 `W=0.45×conf` 向绝对档位混入（单次 ≤0.20）；每 20 窗只回补 `P`（静默回补在 `anchor_tick`）。**锚点位移 `anchored` 不进好感**，`dv` 只含事件步进 | ✅ 已接线 | 2026-11-26 用户：「锚点要不要强制模型填 anchor_level；可以做个权重，anchor_level 也占一大部分」。纯相对步进会漂，而「每 20 轮纠一次」太弱；把绝对档位变成每窗的主项，`dv` 只负责「这一窗又推了多少」 |
| ADR-043 | **v1 好感同时吃 `Δ` 和 `dv`**：`0.25×\|Δ\|×gain(≈0.10)×人设项` + `0.30×三段式(dv)`，单次夹 `AFF_V1_CAP=0.15`（= 日上限） | ✅ 已接线（`AFF_MODEL=v1` 默认） | 2026-11-26 用户：「v1 可以吃个 _delta 和 dv 的系数，不就都吃上，好感也不会那么快了，也能拟合上；当然还有一见钟情那几种突变性事件」。旧 v1 只吃名义步长 `Δ`，事件一重就按满格走（比心情实际位移快得多），且与「好感只吃心情涨落」矛盾；压小 `Δ` 系数后日常变慢，`dv` 尾部仍能一次跳 ≈0.12 |
| ADR-044 | **纪律要落到代码**：`±3` 每会话每日限 `MOOD_IMPACT3_DAILY_MAX=1` 次，超了降 `±2`；锚点死区改**软死区**（smoothstep，不再硬跳过） | ✅ 已接线（2026-11-27） | 2026-11-27 外部审计：prompt 写「±3 一天一次」但代码无计数；死区 0.15 让 EMA 变 bang-bang。都改成「可复现的代码行为」，不是文档承诺 |

> **数值验算（2026-11-26 外部审计要求，写 ADR 必须算一遍）**
>
> - ADR-043 可达上限：`0.30×AFF_W_MAX(0.42) = 0.126`（dv 项）+ 事件项 ≈0.010 → **实测最大 0.136**。
>   所以 `AFF_V1_CAP` 只能取 0.15（= 日上限）；初版写 0.36 是**死护栏**（差 3 倍，永远不触发，
>   正是 ADR-028 批评过的毛病），已改。`test_new_features` 现在断言
>   「尾部 ≈ `DV_K×AFF_W_MAX`」+「CAP ≤ 可达值×1.3」，而不是只断言 `>0.10`。
> - ADR-043 事件项量级：`0.25×|Δ|×0.10`，`|Δ|=0.22` → **0.0044**（不是 0.055）。文档早期
>   写成 `0.25×|Δ|` 漏了 `gain`，把事件项夸大约 12 倍，已补全。
> - ADR-042 与 ADR-043 的接口：`dv` **不含** `anchored`（见 §0 因果链）。所以
>   「锚点占大头」**不会**让好感跳；好感跳只由 `dv` 的尾部决定（需要 `P` 高 + conf 高）。
>   这是有意的边界，不是漏接线。

---

## 5. 术语（只留最容易混的）

| 术语 | 一句话 |
|---|---|
| **观测 z** | 由 `impact` 查表得到的**绝对伪观测**（`v⁻ + dir×step×sens`）；`z−v⁻` 才是位移，`z` 不是当前心情值 |
| **Δ / Δv** | 本次实际位移 = `K×(z−v)×saturation`；落盘 ×100，公式一律在 `v∈[-1,1]` 算 |
| **步进增益 K** | 这次位移该信几成；置信低 → K 小（沿用卡尔曼的 K 形式） |
| **锚点 z_abs** | 每窗判一次「她整体在哪档」（`anchor_level` 必填），按权重混入纠偏 |
| **过程噪声 Q** | 她本身多容易变（人设） |
| **观测噪声 R** | 这次判读多不可信（置信度） |
| **存储强度** `storage` | 编码多牢，不衰减；由 `w`（倒 U）+ relation_relevance 算。⚠️ `unexpected` 目前**判读不产出、也没落库**（二期再补），公式里那一项恒 0 |
| **提取强度** `retrieval` | 现在能不能想起，按 `τ_kind` 衰减、被用会变强 |
| **显著性权重** `w` | `u=|ΔVAD|` 的倒 U 函数：小冲击不记、中等最显著、极端回落（emotion §6） |
| **提取时间常数** `τ_kind` | 按记忆 kind 的衰减半衰期（天）：event 30 / bond 90 / preference 180 / **impression 180** / fact 365 / style 365（`impression` 2026-11-26 补：以前漏登记，掉到 30） |
| **深度系数** | 越亲近，好感衰减越慢（`τ_aff = 7天 × (1+2×intimacy) × …`） |
| **anchor** | 引用/被 @ 的原消息，结构化后紧贴当前消息 |
| **预过滤** | 先用标签/SQL 把候选从 8000 → **48**（`_cand = max(48, min(RET_PLAN_CAND=400, limit×8))`；limit=5），再按分数取 5 条。**没有 400→50 这一步** |

---

## 6. 层 → 代码

| 层 | 文件 |
|---|---|
| 接入 / 收发件人 | `plugins/chat`、`services/segments.py`、`services/group_scene.py` |
| 状态（心情/关系） | `services/mood.py`、`services/mood_judge.py`、`services/profiles.py`、`services/group_mood.py`、`services/quiet_advisor.py`、`services/relationship.py`、`services/persona_traits.py` |
| 记忆 / 检索 | `services/memory_v2.py`、`services/memdb.py`、`services/scene_auditor.py`、`services/mmr.py` |
| 人格 / 生成 | `services/persona.py`、`services/prompt_builder.py`、`services/llm.py`、`services/aura_logic.py`、`services/ctx_budget.py`、`services/style_vault.py`、`services/white_moon.py` |
| 规矩 / 自驱 | `services/chat_switch.py`、`services/referee.py`、`services/group_roster.py`、`services/proactive.py` |
| 干活的手 | `plugins/draw`、`plugins/song_cover`、`plugins/vision_agent`、`plugins/napcat_tools` |
| 观测 / 评测 | `services/llm_usage.py`、`services/reply_pipeline.py`、`core/tuning.py`、`scripts/eval_gold.py`、`scripts/build_gold_questions.py` |

---

## 7. 路线图

```text
P-1 因果断言（33 条，先于实现）
  · docs/causal-assertions.md：M/R/S/O/P 五组
  · synthetic 22 条必须能在 CI 跑（0 模型、0 data）

P0 口径统一（不改行为）
  · 本文 §3 口径表落地到 core/tuning.py
  · 删掉旧文档里冲突的公式/数字

P1 收发件人 → 归因（ADR-011）
  · to_qq 进判读 / 记忆 / 关系更新
  · 多收件人 to_qqs[]
  · interaction_edges 落盘

P2 心情闭环（ADR-003/013）
  · judge 输出 impact/confidence/target
  · 步进 + 锚点（会话 + 全局 + 静默定时器）；快速通道已删（ADR-038）
  · mood_audit.jsonl

P3 关系衰减（§3.2）
  · affinity/trust/respect 半衰期 + 深度系数
  · 道歉/修复通道

P4 检索 sqlite（ADR-006/014）✅ 已实现（双写 + 对账通过，可切后端）
  · schema + 回填：scripts/backfill_memory_p4.py --write
  · 写入即打标签（person/group/event_type/mood/mood_dir/time_bucket/topic）
  · memory_tags / mood_events / entities / profiles / style_samples / edges / threads
  · 查询计划：人→时间→情绪→标签→关键词（recall_planned）
  · 检索二期：显著性倒 U（MEM_SAL_*）+ 按 kind 的 τ_kind + MMR（MEM_MMR_*）
  · 开关：`MEMORY_BACKEND=sqlite`（切前跑 `check_memory_dual.py --strict`）；`RETRIEVAL_PLANNER=1`

P5 注意力（ADR-012）✅
  · 证据升序 + anchor 结构化 + 工具 top6
  · lost_in_middle / gold_position：`eval_gold.py --stage position` + `position_report.py`

P6 情绪效标
  · 200 条人工标注事件（impact -2..+2），已生成候选：scripts/build_emotion_annotation.py
  · 50 条双人重叠，impact QWK ≥0.60；一致率 >80% 才算闭环

P7 风格 / 白月光
  · 去重复：档案只留蒸馏一句，⑧ 只留样本（省 ≥300 token/轮）
  · 风格四层 + custom 250 + 场景标签 + 质量过滤
  · 白月光改「关系参照」+ borrow_ratio + 人格护栏

P8 超时与观测 ✅
  · 每段 soft timeout + 缺块不影响开口
  · stages 记 ms/状态；每轮落 `data/metrics/pipeline_stages.jsonl`
  · `scripts/p95_report.py` / 周报第七节：只看 p95 最慢一块

P9 主动与归档
  · 群接话 / 私聊两条自驱车道（心情+好感驱动）
  · 历史文档已移入 docs/archive/，现行以本文为准

P10 识图（暂缓）
  · 不建本地角色库；云端源 + 视觉 + 轻量 character_alias
  · 只做动漫/插画；算力机异步任务；bot 只编排
```

---

### 7.1 先证明哪一条（2026-11-25）

- **质量杠杆排序：检索标签覆盖（0.6%）> 状态精度。** P4 的 `tags` 回填应尽早做——
  它不动情绪代码，就能直接改善「答得怪」（`pipeline` D24）。
- **最小可交付闭环**（2026-11-26 已更新）：P0 + P2 的「**步进 + 锚点**」+ 判读预算，
  在线上跑一周，看 `M1/M3/M4/M8/M10` 能否在真实数据上成立。
  > 快速通道已删（ADR-038）；锚点已接线（含静默定时器）。这两句以前写反了。

---

## 8. 文档纪律（防止再变补丁）

1. **数字只在 `core/tuning.py` 的 `DEFAULTS`**；本文 §3 是它的视图，`scripts/check_design_docs.py` 校验。
   子文档只引用，不复制。改数字去 `tuning.py`（或 `.env`），改完跑校验。
   > **已收口（2026-11-26）**：`services/mood_judge.py` 曾经硬编码
   > `_step = 0.40/0.22/0.10`（`MOOD_STEP_3/2/1` 的第二份副本），
   > 现在改为 `tuning.f("MOOD_STEP_3/2/1")` 查表（`mood_judge.py:908`），
   > 改 `tuning` 会直接影响它。**当前没有「数字双真相」了。**
2. 任何设计改动：先改 §4 ADR（状态 + 理由），再改子文档，最后改代码。
3. 子文档顶部保留「上级 / 相关 / 本文回答 / 不回答」四行。
4. 过期章节**直接删除**，不要用「补充：……（已废弃）」保留。
5. 讨论稿标 `v0.x`，实现后改 `v1`；**同一份文档不混用两个版本号**。
6. **符号必须登记**：公式里出现的每个符号（`z` / `w` / `τ_kind` / `storage` …）必须在
   §5 术语表或 §3 有定义；新增符号先登记再引用。**没有定义的符号 = 未闭环。**

### 8.1 已知债（2026-11-27 外部审计，明确记着、暂时不还）

写在这里是为了「不装作没有」，不是「保留过期内容」。每条都写了**现在的真实状态**：

| 债 | 真实状态 | 还债的前提 |
|---|---|---|
| 好感 v2 的 51 个 `AFF_TRACE_/BOND_/JUMP_/TAIL_/TIER_` 参数 | `AFF_MODEL=v1` 默认，v2 全套公式 + 迁移反推（`_aff_ensure` 按当前好感反推 `peaks=round(|a|×10)`，0.9 → 9 次心动）都在，但默认不跑 | 决定 v2 是否上线；不上线就删公式和参数（只留 v1） |
| `mood.fast_guard` | 线上无调用（ADR-038 已删该护栏）；只有 `scripts/test_new_features.py:1209` 拿它做回归 | 删函数 + 删那条回归，或明确标注「仅测试」 |
| 心情「数值」写入口 | 默认配置下**只有一条**（THINK → impact → `set_value_from_judge`）。`bump_by_emotion` 在 `MOOD_FROM_JUDGE=true` 时**只写 label 不动数值**；`judge_message_mood` 的 delta 只在回滚模式用；`MOOD_UPDATE/MOOD_MODE` 是回滚分支 | 删回滚分支（会失去降级能力），或继续保留并在 §3.1 标「回滚分支」 |
| 预算的 enforcement 边界 | 真正会裁的只有：① `PROMPT_BUDGET=2200`（**只裁动态块**）；② `CTX_ENFORCE=1` 时块级裁剪（**线上 .env 是 1**，tuning 默认 0）。整份 prompt **没有硬上限** | 要么接受「先观测后裁剪」，要么给整份加硬闸（会打掉前缀缓存） |
| 情绪系统的正反馈风险 | 环是：她写 THINK → 判读 THINK → 改 v → 状态卡写「你心情差/语气冷」→ 更容易写冷的 THINK。**限速件**：半衰期 30min/2h/6h、`P` 衰减（脱敏）、`relevance<0.25` 不推好感/记忆、锚点每窗回拉、`MOOD_HARD_CAP` | 加「连续 N 窗同向 → 强制回中」或外部测量；现在靠参数被动限速，**没有主动刹车** |
| 意图层闸门 `ATMO_GATE` | 代码已接（`plugins/chat` 插话循环，`need_reply=no` 时这一轮不插话），但**默认 0 = 关**；`need_reply` 目前只是 prompt 里的一行提示。**首轮实测（2026-11-27 上午 192 条）**：准确率 96.9% / 正例召回 100% / 精确率 94.8%（6 条误报里 4 条是「正文点名她」的弱标签噪音，真错约 2 条） | 已达 85%/85% 门槛，**等用户拍板**再开；标签是「她当时回没回」的弱标签，只做版本间比较 |

---

## 9. 工程红线（从旧总纲迁入，仍然有效）

1. **气泡规矩**：单泡 ≤40 汉字、**文字泡**单轮 ≤2 泡；超过 2 个文字泡才走合并转发，**不截断**。
   表情/语音不占文字泡名额（合不进文字泡，保留不丢）—— 这条是 2026-11-25 明确的例外。
   > 旧版按「20 字」限，导致「拒绝 + 解释」一超过就被塞进转发卡片，语气全丢；改按气泡更贴近真实聊天。
   > ⚠️ **2026-11-27 更正：这条「40 字」只在群聊生效**。生成侧群聊读
   > `OUT_BUBBLE_CHARS=40`，**私聊仍读 `PRIVATE_MAX_HANZI=20`**（`services/llm.py` 的
   > `if is_group:` 分支）。
   > **用户 2026-11-27 拍板：私聊保持 20，不改 40。**
   > 注意 `PRIVATE_MAX_HANZI` / `OUT_BUBBLE_CHARS` 都是**「超过就发合并转发」的阈值**，
   > 不是字数上限（`plugins/chat` 里 `>阈值 → _send_long_as_forward`，转发失败才
   > `limit_hanzi` 砍字）。所以「私聊 20」的真实含义是：**私聊超过 20 加权字就走
   > 合并转发卡片**，而不是把话砍短。
2. **工具只收不发**：工具不许自己 `bot.send`；`hold=True` 拦成待发，由她 `send(pending=N)` 决定。
3. **规则可召回、可护栏，不可判决**：关键词/正则只做**候选召回与结构信号**（@、名字、引用、空 @）；
   「该不该回、在对谁说、是不是负面」一律由 LLM 判定，规则不得单独下结论。
   > 心情是唯一例外：它**不由规则判决，也不由对话推断**——只来自她自己的 `[[THINK]]`（ADR-038）。
   > 关键词最多召回「可能被冒犯」，不能直接改 `v`；没有 THINK 就不更新。
   > 她理解错了别人的意思，也按她的 THINK 记，不替她纠正（`target` 只回答「是不是对她说的」）。
4. **记录按条数**：进 prompt / 影响行为的数据用 `load_recent(limit)`；按时间只用于周报/日记。
5. **记忆必须能群隔离**：新库用 `gid` 列；旧数据兼容 `（群号:XXX）` 前缀。
6. **安静必须带原因和起因人**：`reason` + `by_qq` + `until`；道歉要能破冰。
7. **前缀缓存**：persona + 协议放最前、一字不改；动态内容（消息号/待发/群总结）放最后。
8. **并发上下文用 ContextVar**：不许用全局 dict 存 bot/event。
9. **两套格式不混**：`parse_reply` 的 items 是 `{kind,value}`；`MessageSegment` 是 `{type,data}`。
10. **失败要留痕**：不许 `except: pass`；至少 `logger.warning`，且 `/测函数` 能看到。
    > **现状（2026-11-26 审计）**：`mood._load` 已修（损坏时 `logger.error` + 备份 `.corrupt-<ts>.json`）；
    > `relationship._save/_log`、`mood._save/_trace`、`log_shift` 仍是裸 `except: pass`，**待收口**。
    > 优先级最高的是 `_load`（读失败 = 全员心情归零且静默），已处理。
11. **单一事实源**：同一件事只写一份（memory），其余是视图/缓存。
12. **空块不存在**：prompt 缺块就整块不出现，不写「暂无/没查到」。

---

## 10. 实现前自检 10 问

1. 这个功能写在哪一层？会不会产生**第二份真相**？
2. 失败了会不会被 `except` 吞掉？日志/`/测函数` 能看见吗？
3. 改了状态，**谁读它**？消费方列全了吗（见 §6 层→代码）？
4. 会不会增加关键路径延迟/token？能不能缺块？
5. 是否违反 §9 红线（气泡 / 工具只收不发 / 规则判决）？
6. 旧数据怎么办？迁移还是兼容读？
7. 重启后还在吗？内存状态要不要落盘？
8. 群隔离 / 人隔离做了吗（`gid` / `about_qq` / per-person）？
9. 有没有对应自检 / 审计项（selftest + stages）？
10. 算力机要不要一起改（`node.py` / `node.env` / manifest / caps）？
