# 外部审计复核（2026-11-27）：16 条逐条判定

> 审计对象：`DESIGN.md` §0.1 / §3 与 `services/` 实际实现。
> 复核口径：**能在代码里指出行号、且能用一段脚本复现的，算真问题**；
> 只是文档措辞/设计取向不同的，单列「理解问题 / 待拍板」。

## 结论

- **真问题 12 条**：全部已修（含 long_doc 未接线、±3 无计数）；
- **部分真 / 设计取舍 3 条**：14 软死区、15 ±3 计数已改成代码行为；
  16「绝对锚点」是反回声设计，只改文档措辞；
- **理解问题 1 条**（16 的「绝对锚点」）；
- 审计最狠的一句「校验是自证」**成立**：除了 `ctx_total_chars()` 交叉校验，
  2026-11-27 新增 `scripts/check_runtime_wiring.py` —— 直接 import 运行时、
  构造输入、断言输出变化（long_doc 块大小 / ±3 降档 / 锚点连续性 / relevance 不挡 observe），
  不再只比「文档↔常量镜像」。

---

## 一、已修的真问题

### 1. long_doc 证据 6000 / 图书馆 3000 没生效（真，2026-11-27 已接线）

- 证据：`services/llm.py:2915` 的 `ctx_budget.observe("long_doc_evidence", ...)`
  返回值被直接丢掉；`build_packet` 的 `memory_index` 仍被
  `BLOCK_BUDGET["memory_index"]=CTX_EVIDENCE=500` 夹；staged 固定 `limit=4`。
- 修（2026-11-27）：
  - 判定/常量抽到 `services/long_doc.py`，`llm` 与 `reply_pipeline` 共用；
  - `build_packet(long_doc=True)` 把 `memory_index` 上限放开到
    `CTX_LONG_DOC_EVIDENCE=6000`、`library` 放开到 `CTX_LONG_DOC_LIBRARY=3000`，
    按 `LONG_DOC_PIECES=3` × `LONG_DOC_PIECE_CHARS=1900` 渲染正文；
  - staged 路径（线上真正入口）由 `reply_pipeline` 同样放开；
  - 单位修正：这两个键是**字符**，`ctx_budget` 以前 ×1.5 变 9000/4500，已改 1.0；
  - `check_design_docs` 新增「long_doc 块字符数 == DESIGN 6000/3000」交叉校验。
- 证据：`check_runtime_wiring.py` 断言 long_doc 记忆块 5795 字（常态 135 字）。

### 2. `ctx_total()=4620` 是 token 值之和，不是字符（真）

- 实测：`ctx_total()=4620`；`sum(limit_chars)=6540`（常态）/ **7170**（含 tools+multi）。
- 处置：
  - `design_constants.ctx_total` docstring 正名「token 值之和」；
  - 新增 `ctx_total_chars()`（6540/7170）；
  - `check_design_docs` 用 `ctx_budget.limit_chars` 反算字符和，和 `ctx_total_chars()` 对不上就 FAIL；
  - DESIGN §3.3 常态合计行、§0.1 预算行都写明「token 值之和 / 实际字符 6540」。

### 3. 「live 总池 2200」不管整份 prompt（真）

- `PROMPT_BUDGET=2200` 只分配给 `BLOCK_BUDGET` 的 10 个动态块；
  persona/protocol/post_rules/tools/history 走 `ctx_budget.observe`，
  `CTX_ENFORCE=0` 时只统计不裁；protocol 单块上限 1500×1.5=**2250 字符**。
- 处置：DESIGN §3.3 加显式警告「整份 prompt ≤2200 不成立」。

### 4. P4 因果断言与实现冲突（真，文档冲突）

- `causal-assertions.md` P4 写「各窗口 msg_ids 并集 == 段内全集」；
  代码 `mood_judge.py:1115-1129` 前窗只 `note_window`、只判最后一窗。
- 处置：
  - `causal-assertions.md` P4 改成 **P4b 口径**：最后一窗必须判；前窗只记账（ADR-038）；
  - `verify_napcat_log.py` P4 不再数「窗数」，改为「窗口大小读 `MOOD_JUDGE_CTX` +
    排除预算截断」；
  - 未选「补判前窗」——用户 2026-11-26 已拍板被动窗不调模型（省 ~80% 调用）。

### 6. `P_EVENT_CAP < P0` 会把 P 调低（真 bug，已修）

- 旧：`p = min(max(P_FLOOR, P_EVENT_CAP), p + BOOST)`；
  P0=0.25 > CAP=0.20 → 第一条 ±2 把 P 从 0.25 压到 0.20。
- 修：抽成 `mood.event_p_boost()`，语义改成 **只补不降**：
  `p = max(p, min(CAP, p + BOOST))`；新增两条单测（P0>CAP 不降、地板能抬起）。

### 7. §3.1「首条/稳态」数字错（真）

- 实测：首条 conf=0.9 → **0.0906**；conf=0.6 → **0.0683**；稳态 conf=0.9 → **0.0148**。
- 旧文档把「稳态 conf=0.6 的 0.004」写进了「首条 conf=0.6」格。
- 处置：DESIGN §3.1 改成实测值。

### 8. 候选池 400 实际是 48（真，文档错）

- 代码：`_cand = max(48, min(RET_PLAN_CAND=400, limit×8))`，limit=5 → 48。
- 处置：DESIGN §3.4 / §7 改成「实际 48，400 只是上限」。

### 9. 诊断字段名写错（真，已修）

- `mood_judge.py` 的 `_TRACE` 存 `"v": res.get("v_after")`，
  诊断却读 `t.get("value")` → `/测函数 心情` 永远 `+0.00`。
- 处置：改成 `t.get("v")`。

### 10. tuning 描述与设计打架（真，已修）

- `MOOD_HARD_CAP` 写「不触发」→ 改成「±3 首条会触发 0.507→0.35」；
- `MOOD_MAX_DELTA` 标 LEGACY 但 `mood_judge:1238` / `mood.apply_delta` 仍在用 →
  移出 `LEGACY`，描述改成「名义 Δ 夹取上限」；
- `AFFINITY_GAIN` 只有一句赋值、无使用 → 删掉赋值，键进 `tuning.UNWIRED`；
  selftest 同步改成「不再有该属性」。

### 11. §0「只吃 dv」与 §3.2「吃 Δ+dv」自相矛盾（真，已修）

- 处置：§0 图改成「好感只吃 dv（v1 另加小的 Δ 事件项）」；
  §3.2 的「±0.10」全部改成 **±0.15（AFF_V1_CAP）**。

### 12. relevance 闸门把心情也挡了（真 P0，已修）

- `_skip_update = relation_relevance < 0.25`，而 prompt 教模型「world/self 填 0」→
  她自己的情绪（想起以前、困了）永远写不进 `v`，anchor 也丢。
- 修：**心情与锚点照常 `mood.observe`**；`_skip_update` 只挡好感/记忆/群心情。
- 单测：`relation_relevance=0.0, cause=self, impact=2` → 心情从 +14.22 涨到 +31.96。

### 13. 全局锚点没乘 GLOBAL_SHARE（真，已修）

- 全局事件 `_one(..., GLOBAL_SHARE)` ×0.35；全局锚点以前满权重 0.45×conf。
- 修：全局锚点 `gain=ANCHOR_WEIGHT × GLOBAL_SHARE`、`p_add` 同比例；
  这样「全局是慢变量」在事件和锚点上一致。

### 5. 静默锚点几乎是死分支（真，已修）

- `IDLE_MIN=30min`、`THINK_AGE=1800s`、tick=300s；静默 30min 后 THINK 必然 >1800s。
- 修：`MOOD_ANCHOR_THINK_AGE_SEC` 1800 → **2100**（35min，留一个 tick 的余量），
  并在 tuning 说明「必须 ≥ 静默时长 + tick 间隔」。

---

## 二、部分真 / 设计取舍（3 条，2026-11-27 已全部落地）

### 14. 锚点死区把 EMA 变成 bang-bang（已修：软死区）

- 旧：`abs(diff) < ANCHOR_DEADBAND=0.15` 完全不混，>0.15 一次拉 45%；
  稳态是锯齿，不是严格 EMA。
- 修（2026-11-27）：`_anchor_rec` 改成**软死区** ——
  `|diff| < DEADBAND` 时权重按 smoothstep `r²(3−2r)` 从 0 平滑升到 1，
  边界处与全速连续；小抖动（diff=0.01）位移 <0.0002，不会刷 `mood_history`。
- 证据：`scripts/check_runtime_wiring.py` 断言「0.149 vs 0.151 差 <0.005」。

### 15. ±3 一天最多一次只靠模型自觉（已修：代码计数）

- 旧：prompt 写了纪律，代码无计数/冷却。
- 修（2026-11-27，ADR-044）：`mood.cap_daily_impact()` **按会话按自然日**计数，
  `MOOD_IMPACT3_DAILY_MAX=1`；超了把 ±3 降成 ±2（`MOOD_IMPACT3_OVER_TO=2`），
  不丢弃事件、只是不再顶格；`0` = 不限（回滚）。
- 证据：`check_runtime_wiring.py` / `test_new_features.py` 断言
  「第一次 3、第二次 2、换会话重新 3、±2 不受限」。

### 16. 锚点「绝对档位」其实不绝对（理解问题 → 文档措辞已改）

- 判读确实看不到当前 `v`（防回声），所以 `anchor_level` 是「这一窗语气的重映射」，
  不是带数值参照的绝对测量。
- 但这是 **ADR-029 的故意取舍**：给 `v` 会回声（模型照抄上一轮数值）。
- 处置：DESIGN §3.1 已写明「它防漂移，不是精确刻度；软死区 + 重映射是设计的一部分」。

---

## 三、理解问题（1 条）

- 审计把「锚点不是绝对测量」当成实现 bug；实际是**反回声设计**的必然结果。
  真正该改的是文档措辞，不是代码。

---

## 四、元问题：校验是自证（成立，已部分修）

- `check_design_docs` 的输入是 `DESIGN.md` + `design_constants.py`，
  而后者是前者的只读镜像 → 只能证明「文档 == 副本」，不能证明「文档 == 运行时」。
- 已做：
  - 新增 `ctx_total_chars()`，并用 `ctx_budget.limit_chars` 反算实际字符和交叉校验；
  - P4 检查改成读实际 `MOOD_JUDGE_CTX` + 预算截断；
  - `AFFINITY_GAIN` 从「有读点」改成 `UNWIRED`（不再靠一句赋值混过）。
- 2026-11-27 补：`scripts/check_runtime_wiring.py` 直接 import 运行时做行为断言，
  `long_doc 6000` / `±3` / 锚点软死区 / relevance 闸门都在里面；
  `check_design_docs` 还加了 long_doc 字符口径交叉校验。
- 仍做不到的：真机日志才能证明「线上 .env 覆盖后的值」——这层继续靠
  `verify_deploy` + `verify_napcat_log`，不要拿静态脚本替代。

---

## 五、一句话回答

审计的 **12 条真问题里，8 条是文档/常量与实现不一致，2 条是直接 bug
（P 回补方向、relevance 挡心情），2 条是设计缺口（long_doc 未接线、±3 无计数）**；
**2026-11-27 全部落地**：long_doc 在 `build_packet` 与 `reply_pipeline` 两条路径都放开，
±3 有按会话按日的代码计数（超了降 ±2），锚点死区改软死区（连续、无锯齿）。
**1 条是理解问题**（绝对锚点，只改文档），**2 条是设计取舍**（死区、±3 纪律）也已变成代码。
最狠的「校验自证」成立：新增 `check_runtime_wiring.py` 做运行时行为断言，
但静态校验永远替代不了真机日志。
