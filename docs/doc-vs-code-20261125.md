# 文档 ↔ 实现 对照表（2026-11-25 21:40，P0 修复后）

范围：`DESIGN.md` §3.1/§3.2/§3.3/§3.5/§5.2/§9.1、`emotion-dynamics-design.md` §3.1~§5.3、
`pipeline-map.md`、`retrieval-profile-audit`、`affect-and-response-design.md`

判定口径：**一致** = 文档断言能在代码里指到行；**已修** = 本轮改完；
**仍偏差** = 文档与代码仍不一致（列建议）；**设计稿** = 还没实现。

---

## 1. 心情状态机（emotion §3.1 / DESIGN §3.1）

| 文档断言 | 代码 | 判定 |
|---|---|---|
| 衰减回到 `baseline_v`（-0.3~0.3） | `mood._decay(rec, now, baseline)`，`_base + (v-_base)*0.5**…`；`observe` 从 `persona_traits.baseline_v` 取；记录里存 `base_v` | **已修** |
| `Q_eff = q_rate × Δt_h` 随时间缩放 | `_one` 在 `_decay` **之前**算 `dt_h`（`mood.py:401` 附近） | **已修**（原来恒 0） |
| `P / judge_n / anchor_at / anchor_n / sens` 衰减时保留 | `_decay` 的 `for _k in (...)` | 一致 |
| 锚点每 20 窗 / 静默 30min | `ANCHOR_EVERY=20`、`ANCHOR_IDLE_MIN=30`；**新增** `mood_judge.anchor_tick()` + 5 分钟定时器（`chat` 的 `on_bot_connect`） | **已修** |
| 锚点同样作用于全局 | `observe` 里 `_do_anchor` 同时 `_anchor_rec(g, …)`；日志 `全局锚点纠偏` | **已修** |
| 锚点 `z_abs = clamp(baseline_v + map(level))` | `_anchor_rec(rec, level, conf, baseline)`；`anchor_tick` 传 baseline | **已修** |
| a/d 只记录、不进 v | `update_ad` 独立 EMA；`_one` 不读 a/d | 一致 |
| 强负面护栏（P×1.5） | 函数在，**无调用**；文档 §3.1 已改「已删除（ADR-038）」 | 一致（文档已跟上） |
| 单次 `|Δv| ≤ 0.35` | `HARD_CAP` | 一致 |

## 2. 关系（emotion §3.2 / DESIGN §3.2）

| 文档断言 | 代码 | 判定 |
|---|---|---|
| `affinity` 双向、对称 ±0.10 | `_aff` 对称夹 `±0.10`；负向乘 `neg_weight(当前好感)` | 已修（上一轮） |
| 负向用「当前好感」而不是心情 | `_rel.neg_weight(_cur_aff)` | **已修**（原来误传 mood） |
| 好感乘置信（低置信不该满格） | `0.5 + 0.5×K` | **已修** |
| `trust/respect` 记到 **target 那个人** | `note_judge(_rel_qq, …)`，`_rel_qq = _tgt_qq` | **已修** |
| `target=other` 能落到第三人 | judge 输出 `other_name` → `resolve_name_qq` → `resolve_target(..., other_qq)`；解析失败返回空 QQ（不记到说话人） | **已修** |
| `intimacy` 由 bond notes 推导 | `profiles.intimacy` | 一致 |
| `τ_aff = 7天 × 深度系数` | `relationship.affinity_of` | 一致 |
| `grudge` 是独立变量（14 天半衰期） | **没有 grudge 字段**；负向直接用 `GRUDGE_HL_D=14` 替代 trust 45 / respect 90 | **仍偏差**（建议：要么加 grudge，要么把文档改成「负向偏差走 14 天」） |
| trust/respect 镜像到 sqlite | `memdb.upsert_profile` 支持参数，但 `relationship.bump` 不调用；sqlite 两列**没人读** | **仍偏差**（低优先，属死数据） |

## 3. 预算（DESIGN §3.3 / prompt_builder）

| 文档断言 | 代码 | 判定 |
|---|---|---|
| 目标 2120 / 上限合计 4620 / 总池 2200 三套口径 | `budget_total()=2120`、`ctx_total()=4620`、`PROMPT_BUDGET=2200`；文档 §3.3 已写清三层 | **已修（文档）** |
| `style_total() ≤ CTX_STYLE_TOTAL` | `CTX_STYLE_TOTAL` 650→**730**；`check_design_docs` 新增运行时不变式 | **已修** |
| `CTX_ENFORCE=0` 只是观测 | 文档已注明「这种状态叫观测，不叫硬截断」 | **已修（文档）** |
| 同一键两种单位（×1.5 vs 字符） | `ctx_budget.limit_chars` ×1.5；`prompt_builder.BLOCK_BUDGET` 直接字符 | **仍偏差**（建议：BLOCK_BUDGET 统一用 `ctx_budget.limit_chars`） |
| `check_design_docs` 证明「文档↔运行时一致」 | 现在只证明「文档↔BUDGET 自洽」 | **仍偏差（语义）**：建议改名 `check_doc_targets`，另加运行时检查 |

## 4. 收发件人（DESIGN §3.5）

| 文档断言 | 代码 | 判定 |
|---|---|---|
| ≥0.8「→ X」/ 0.5~0.8「像是在回 X」/ <0.5「不确定」 | `segments.addressee_of` 三档（实测 0.9/0.7/0.3 → 三种渲染） | **已修** |
| 无信号是 unknown，不是「大家」 | `to_kind=group & to_reason=none` → 「→ 不确定」；她自己的消息（reason=bot）仍是「→ 大家」 | **已修** |
| `ADDR_MAYBE_MIN` 参与判定 | `group_scene.py:1028` 在用 | 一致 |
| `ADDR_RECENT_MENTION` | 全仓 0 引用 | **仍偏差**（删键或接线） |

## 5. 判读 schema（emotion §5.2）

| 文档断言 | 代码 | 判定 |
|---|---|---|
| judge 输出 `cause`，`target` 由代码派生 | prompt 问 `cause`；`resolve_target` 派生；文档已写明桥 | **已修（文档）** |
| `other` 需要 `other_name` 才能归因 | prompt 新增 `other_name` 规则；parse 有字段 | **已修** |
| `mind` 是输入不是输出 | prompt 仍产出 `mind`（作 `guess`）；`res["mind"]` 被她的 THINK 覆盖；文档已改口 | **已修（文档）** |
| `unexpected` 进 storage 公式 | 全仓 0 引用；文档已标「二期再补，当前恒 0」 | 一致（文档已注明） |
| relevance gate 管整条链路 | `_skip_update` 现在同时挡住 群心情 / mood_events / 记忆刻痕 / 好感 / trust | **已修** |
| 被 relevance 跳过的窗口也计数（ADR-037） | 跳过分支调 `mood.note_window(key)` | **已修** |
| `mood_events.dv` = 实际位移 | `dv=_dv_actual`；日志加「实际Δ」 | **已修** |

## 6. 输出（DESIGN §9.1 / ADR-034）

| 文档断言 | 代码 | 判定 |
|---|---|---|
| 单泡 ≤40 字 | `_length_rule` 用 `OUT_BUBBLE_CHARS=40`（实测输出「单泡 40 字内」） | **已修** |
| 单轮 ≤2 泡；超 2 泡合并转发 | `MAX_BUBBLES=2`；`OUT_BUBBLES_MAX=2` | 一致 |
| **不截断** | 合并后仍超 2 泡（含表情/语音）会**丢弃尾部** | **仍偏差**（建议文档写「非文字泡可丢」例外，或把非文字也合并） |
| `PRIVATE_MAX_HANZI` 走 tuning | `tuning.i("PRIVATE_MAX_HANZI")` | **已修** |

## 7. 检索 / 档案（retrieval-profile-audit）

| 文档断言 | 代码 | 判定 |
|---|---|---|
| 档案只收 \|Δ\|≥0.15 的大波动 | `PROFILE_NOTE_MIN_DELTA` + `add_bond_note` 拦截 | 一致 |
| 档案块不查记忆 | `reply_pipeline` ③ 读 `bond_notes` | 一致 |
| 图书馆只在问事时查 | `_want_library` 收紧（实测「—(不是问事，不查)」） | 一致 |
| 心情只来自 THINK | `judge_and_store` 无 THINK → passive，不调判读 | 一致 |

## 8. 工程（P3）

| 项 | 状态 |
|---|---|
| 本地 Python 3.8 能 import `services.mood` | **已修**（`from __future__ import annotations`；`python3 -c` 实测通过） |
| `check_tuning` 用 AST 而不是字符串匹配 | **已修**（2026-11-25 22:10，见 §12） |
| 两套气泡上限有联动测试 | **已修**（`MAX_BUBBLES == OUT_BUBBLES_MAX`） |
| `intimacy` 饱和 | 7 人顶到 1.00（240 人里），**待观察** |
| 判读两段式（desirability/cared） | **设计稿**（`affect-and-response-design.md`），未实施 |
| 工具收敛 36→20 | **设计稿**，未实施 |

---

## 9. 本轮验证结果（容器内 = 真实运行环境）

```text
verify_all.py          9/9 PASS
verify_rollbacks.py    19 项 / FAIL 0
test_new_features.py   211 PASS / 0 FAIL（新增 M1/M5/M10 synthetic）
check_tuning --strict  PASS
check_design_docs      PASS
verify_napcat_log      9 PASS / 0 FAIL / 10 SKIP
  M0 状态值域 / R1 关系值域 / R6 日上限 / M7 步长有界 / M1h 单调 / M10h 步长递减 / P4 分段覆盖 / O5 气泡数
```

> 宿主机（不是容器）上 `verify_rollbacks` 有 4 项 FAIL、`test_new_features` 4 项导入失败，
> 原因是宿主机 Python 没装 `nonebot/httpx` 等依赖；**容器内全绿**，属环境差异，不是代码问题。

---

## 10. 第二轮修复（2026-11-25 21:55）

| 原偏差 | 处理 | 验证 |
|---|---|---|
| 气泡超限**丢弃**非文字泡 | 不再丢：文字泡 ≤2，表情/语音保留；文档 §9.1 写明例外 | `test_new_features` + `O5` PASS |
| `grudge` 未实现，负向 trust 用 14 天 | 新增 `grudge`（0~1，14 天半衰期）；trust/respect 一律 45/90 天；grudge 只拖慢信任恢复、负面加深、正面消退 | 回归 19 项 PASS |
| `BLOCK_BUDGET` 单位混用 | `ctx_budget.BUDGET` 补 `profile/evidence/summary`（scale=1.0，字符口径），与 `prompt_builder` 对齐 | `check_tuning` PASS |
| trust/respect 没镜像 sqlite | `relationship.bump` 落盘后 `memdb.upsert_profile(trust=…, respect=…)` | 代码核对 |
| 两套气泡上限无联动测试 | 新增断言 `MAX_BUBBLES == OUT_BUBBLES_MAX` | PASS |
| `intimacy` 顶到 1.00（7/240） | 改饱和曲线 `score/(score+3)`：实测 1.00 → **0.555** | `dump_state` 实测 |
| **（新发现）峰值事件混进图片描述/锚点** | `peak_event` 排除 `anchor / fast-guard / guard / keyword` 与 `**…`/`图片识别` 标签 | 实测从「**结尾的“反鸡汤”收束**…」变成真实事件「晗雨落突然说喜欢她…（当时你心里：被这么说，有点招架不住）」 |

## 11. 仍存在的偏差（3 项，都是工程债）→ **已全部关闭，见 §12**

1. ~~`check_design_docs` 只证明「文档 ↔ BUDGET 自洽」，不证明运行时~~ → 加 `--runtime`。
2. ~~`ADDR_RECENT_MENTION` 全仓 0 引用（预留键）~~ → 接进短句冲人推断。
3. ~~`check_tuning` 仍是字符串匹配，不是 AST~~ → 改 AST，并新增「常量读了不用」规则。

## 12. 第三轮：三项工程债闭环（2026-11-25 22:10）

| 债 | 处理 | 证据 |
|---|---|---|
| `check_design_docs` 语义窄 | 保留文件名（`verify_all`/manifest 都在引用），加 `--runtime`：AST 证明 120 个设计键都有真实读点 + 核对 `.env` 覆盖后的不变式 | 容器内 `--runtime --strict` PASS |
| `ADDR_RECENT_MENTION` 零引用 | `group_scene` 两处硬编码 `0.55 / 0.5` 改用键值 | `check_tuning` 规则 5 PASS |
| `check_tuning` 字符串匹配 | AST 版：别名 import、docstring 排除、数值型 `os.getenv`、**模块级常量必须被使用**；3.8 解析失败的文件降级正则 | 一次抓出 8 个假接线/死键，全部收口 |

顺带：`lorebook` 扫描深度、`llm_usage` 保留天数、`profiles` 亲近半衰期、
`prompt_builder` 记忆条数全部真接线；删除 `MOOD_PASSIVE_MAX_DELTA` /
`MAX_REPLY_CHARS` / `SHORT_QUERY_WINDOW_SEC`（调参键 420 → 418）。

验证：容器内 `verify_all` 9/9、`verify_rollbacks` 19/0、`test_new_features` 222/0、
`audit_features` 52/0、部署后日志回滚 6 PASS / 0 FAIL / 13 SKIP。
详见 `docs/verification-report-20261125-debt.md`。
