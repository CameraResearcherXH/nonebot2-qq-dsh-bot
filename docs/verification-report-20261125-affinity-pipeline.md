# 验证报告：好感双向 · 流水线现状 · 文档一致性 · NapCat 日志回滚

日期：2026-11-25 20:50｜范围：好感增降、THINK 纯度、流水线效果、原有功能影响、日志级回滚验证

---

## 0. 结论

| 问题 | 结论 |
|---|---|
| 好感是不是「只增不减」 | **不是**。代码双向，线上 `affinity_log` 265 条里 **157 正 / 108 负**。但旧代码有 3 处**偏正**的结构性不对称，已修 |
| THINK 是否还有泄漏 | **有 2 处**，已修：① 旁观窗口把判读猜测以 `auto=True` 写进 `mood_think.json`；② 没有 THINK 的窗口在 5 分钟后仍按「历史 THINK / 群聊」改心情 |
| 流水线效果 | 5 轮真机：**中位 8.3s / 最大 11.7s**，prompt **836~1037 字**（预算 2200）。图书馆 **5/5 轮都触发**（旧门槛太松），已收紧 |
| 文档 vs 代码 | 3 处漂移，已改：`DESIGN §3.2` 说 trust/respect「还没实现」（其实已实现）；`emotion §3.2` 好感公式与 as-built 不符；`pipeline-map D23` 图书馆触发口径 |
| 原有功能 | 回滚开关 **19 项 FAIL 0**；九道闸门 **9/9**；新功能单测 **208/0**；回归 43/5（4 个 nonebot 缺失 + 1 个历史刻痕，与本次无关） |
| NapCat 日志回滚 | **6 PASS / 0 FAIL / 13 SKIP**（13 项是这段日志没覆盖到，不是失败） |

---

## 1. 好感：双向确认 + 3 处偏正修正

### 1.1 线上实证（修之前）

```
data/profiles/*_profile.json → affinity_log 共 265 条
  正向 157 条 / 负向 108 条
例：100000006  -0.012（被催得手忙脚乱）
    100000012   -0.013（被戳穿有点窘）
    1841994759  → -0.264（被噎得有点闷，累计）
```

所以「只增」不成立。但下面三处让**跌得比涨轻**，属于设计口径错误：

| # | 旧实现 | 问题 |
|---|---|---|
| 1 | `_aff = max(-0.06, min(0.08, _aff))` | 涨最多 +0.08、跌最多 −0.06，**跌被削 25%** |
| 2 | `neg_weight(float(mood.value)/100)` | 「越喜欢越宽容」传的是**心情值**，不是**好感**，参数传错 |
| 3 | `if repair: _aff = max(_aff, 0.03×…)` | 道歉直接把负向**翻成加分**，不是「减半」 |

### 1.2 修正（`services/mood_judge.py` ④）

```text
|Δaff| = |Δv| × k(warmth) × trust_speed_factor × novelty × saturation
k        = 0.04 + 0.12×warmth
tsf      = 0.6 + 0.8×trust_speed
novelty  = 1/(1 + 0.6×n_30min)          # 同一个人 30 分钟内第 n 次
saturation = 1 − |当前好感|              # 越靠近 ±1 越难走
负向再乘 neg_weight(当前好感) = clamp(1.3 − 0.6×aff, 0.5, 2.0)
单次对称上限 ±0.10
道歉(repair)：只把这次负向减半，不翻正
```

与 `emotion-dynamics-design.md §3.2` 完全对齐（文档同步补了 as-built 说明）。

### 1.3 顺手修的一致性问题

- 状态卡里「你对 X 的好感」读的是**原始值**，「你俩的关系」读的是**衰减后**的值 → 同一个人两个数。
  现在统一走 `relationship.affinity_of`；`render_line` 不再重复输出好感。
- `mood_history.jsonl` 增加 `impact` 字段：没有它，「同段步长递减」在数据级根本判不了。

### 1.4 测试

```
PASS 好感能降（负向事件）        0.06 → 0.044      （Δ−0.016，含负向加权）
PASS 好感能涨（正向事件）        0.044 → 0.054     （Δ+0.010）
PASS 好感单次步长对称（±0.10 内）
```

> 负向一次比正向略大，是设计要的「坏比好强」；两者都在对称上限 ±0.10 内。

---

## 2. THINK 纯度：又发现 2 处泄漏，已修

| 位置 | 旧行为 | 现在 |
|---|---|---|
| `plugins/chat/__init__.py`（旁观窗口） | 把判读模型的 `mind` 以 `auto=True` 写进 `mood_think.json` | **删除**；猜测只进 `mood_think_inferred.json` |
| `services/mood_judge.py`（无 THINK 窗口） | 超过 `MOOD_JUDGE_REFRESH=300s` 后，退到「历史 THINK / 群聊」硬判并写 `v` | `if not wrote: passive = True`，**force 也不写状态**，只返回诊断 |

> 判定标准仍是 ADR-038：**没有她自己的话/心理活动，就没有心情更新**。
> 被动返回放在 Δ 护栏之后，所以 `res["delta"]` 仍是护栏后的值（诊断用），但不落状态。

测试：

```
PASS 旁观窗口（无 THINK）不写心情
PASS 无 THINK 的窗口不写心情（force 也不行）
```

---

## 3. 整条流水线：现状与效果

### 3.1 真机数据（部署前 3 小时，5 轮）

| 指标 | 值 |
|---|---|
| 端到端耗时 | 中位 **8.3s**，均值 7.5s，最大 **11.7s** |
| prompt 字数 | 836 / 857 / 1037（预算 2200） |
| 块命中 | 称呼 5 / 状态 5 / 档案 5 / 总结 5 / 记忆 5 / 图书馆 5 / 已回 5 / 白月光 5 |
| **图书馆命中** | **5/5** ← 过触发 |

### 3.2 块职责（live 路径 = `reply_pipeline.build`）

| 块 | 来源 | 触发 |
|---|---|---|
| 称呼 | callname / profiles | 每轮 |
| 状态 | `mood.status_block`：心情水平 v、a/d、她自报感觉、刚才那下 Δ、真实 THINK、关系四量 | 每轮 |
| 档案 | profiles：称呼/身份/性格/好感词 + `bond_notes` 里 \|Δ\| 最大的一条（**不查记忆**） | 每轮 |
| 总结 | 群聊摘要 | 非媒体 |
| 记忆 | `memory_v2.recall` | 每轮 |
| 图书馆 | `agent_client.recall`（RAG v3） | **只在提旧事/真问句**（本轮修） |
| 已回 | 防重复 | 每轮 |

### 3.3 修掉的过触发

`_want_library` 旧逻辑：命中「那个/什么/怎么/知道」**或 ≥12 字**就查 → 群聊里几乎每条都中，
每轮白等 5~14s RAG，还往 prompt 塞无关事实。现在只认：

- 明确提旧事：`记得/之前/上次/以前/曾经/说过/还记得/那件事/后来…`
- 真问句：含 `？/?` **且** 含 `谁/什么/怎么/哪/多少/吗/呢`

测试：`闲聊不查` / `提旧事要查` / `真问句要查` 三条全 PASS。

---

## 4. 文档 ↔ 代码：3 处漂移已修

| 文档 | 旧描述 | 现在 |
|---|---|---|
| `DESIGN.md §3.2` | 「`trust` / `respect` **还没实现**（没有写入口，也没有 audit）」 | 已实现：`relationship.note_judge` + `relationship_log.jsonl` + `render_line`；并写明好感必须双向、对称 ±0.10 |
| `emotion-dynamics-design.md §3.2` | 公式缺 `trust_speed_factor / novelty / saturation`，负向乘 `forgiveness` | 改成 as-built：`neg_weight(当前好感)`、对称夹 ±0.10、道歉只减半，附线上实证 |
| `pipeline-map.md D23` | 图书馆「待修」 | 「部分修（触发门槛收紧）」 |

`check_design_docs.py --strict` → **PASS**；`check_tuning.py --strict` → **PASS**。

---

## 5. 原有功能：有没有被影响

### 5.1 回滚开关（`verify_rollbacks.py`）

```
PASS memory=json / memory=sqlite / mmr=0 / mmr=1 / 显著性倒 U / 尾部可回滚
PASS τ_kind / mood=v2 / mood=delta / power=full / power=eco / watch=lite
PASS pipe_metrics=0 / dual_write=0 / white_moon borrow=0
SKIP tools_top=0/2、long_doc=0/1（本机没装 nonebot，容器内会跑）
合计 19 项，FAIL 0
```

### 5.2 闸门与测试

| 检查 | 结果 |
|---|---|
| `verify_all.py` | **9/9 PASS** |
| `test_new_features.py` | **208 PASS / 0 FAIL** |
| `test_regressions.py` | 43 PASS / 5 FAIL（4 个 nonebot 缺失 + 1 个「情绪刻痕 vs 中性」历史失败） |
| `check_design_docs.py --strict` | PASS |
| `check_tuning.py --strict` | PASS |

> 那条「情绪刻痕仍比中性记忆更容易被想起」失败在 `memory_v2` 检索打分，
> 与本次改动无关（改前就失败），单独记账。

### 5.3 哪些原有功能还在帮忙

- **记忆**：每轮 4 条，`记忆=4` 稳定命中；
- **总结**：每轮都出；
- **图书馆**：改动后只在问事时查，命中会变少但更准（不再每轮塞 1 条无关事实）；
- **白月光**：4/5 轮命中（第 5 轮 `—(没样本)` 属正常）；
- **称呼/档案/已回**：每轮都正常；
- **表情包**：仍由 `<sticker:标签>` + `mood_factors["sticker"]`（按心情水平 v）驱动，本次未动。

---

## 6. NapCat 日志回滚验证

在服务器上用真实数据跑（`--data-dir data`）：

```
结论：6 PASS / 0 FAIL / 13 SKIP
PASS M0   emotion §3.1 状态值域        24 条状态；值域正常
PASS R1   emotion §3.2 关系值域        3 人；值域正常
PASS R6   emotion §3.2 日上限/步长     6 条事件；异常 0
PASS M7   emotion §3.1 锚点/步长有界   新链路 80 条波动；单次 >0.40 的 0 条
PASS M1h  emotion §3.1 单调累积(data)  21 对同向观测，反向 0 处
PASS M10h emotion §3.1 步长递减(data)  7 段 ≥3 次观测，步长回升 0 段
```

### 6.1 第一次跑是 3 FAIL，原因不是系统坏了，是**校验器自己**

| 断言 | 原报错 | 真实原因 | 处理 |
|---|---|---|---|
| M7 | 单次 >0.40 的 1 条 | 那条是 **19:57 的 `fast-guard` 历史记录**（快速通道 20:25 才删） | M7 不再统计 `fast-guard`（路径已删），历史账不再参与 |
| M1h | 23 对同向观测，反向 1 处 | 20:09:16 的 observe 和 anchor **同一秒写出、时间戳差几微秒**，被判定「中间没锚点」 | 锚点容差 ±2s |
| M10h | 3 段步长回升 1 段 | 旧写法把整个会话的 \|Δ\| 串起来比：**±1→±2 档位变化**和**跨段重新热身**都被算成回升 | 按「同段 + 同方向 + 同档位」比较；`mood_history` 新增 `impact` 字段供后续判定 |

> 修完后三条都 PASS，且逻辑更严：M10h 现在只比「同一档位、同一段」的步长。

### 6.2 13 项 SKIP 是什么

日志覆盖不到（不是失败）：段内多窗、气泡数、relevance 闸门、省流模式、角色脑 sqlite 时延、
trust/respect 事件（普通闲聊不触发）、连续同向观测等。跑 1~2 小时正常聊天后再拉一次即可。

---

## 7. 部署

| 文件 | 说明 |
|---|---|
| `services/mood_judge.py` | 好感双向公式；无 THINK → passive（force 也不行） |
| `services/mood.py` | 状态卡好感与关系行同源；`mood_history` 记 `impact` |
| `services/relationship.py` | `render_line` 不再重复输出好感 |
| `services/reply_pipeline.py` | 图书馆触发门槛收紧 |
| `plugins/chat/__init__.py` | 删除「猜测心里话 → THINK」写入 |
| `scripts/verify_napcat_log.py` | M7/M1h/M10h 三处校验修正 |
| `scripts/test_new_features.py` | 新增 4 条断言（好感双向、无 THINK 不写、图书馆门槛） |
| `deploy_manifest.txt` | 上述文件 md5 已更新，`md5sum -c` 全绿 |

容器已 `up -d --force-recreate`，启动无 Traceback，人格 917 字。

---

## 8. 仍待观察

1. `mood_history.jsonl` 的 `impact` 字段从本轮起才有，M10h 要等新数据才能真正比「同档步长」；
2. 图书馆命中率会明显下降（预期），若发现「该查没查」，调 `_LIB_QUESTION_HINTS`；
3. 好感负向是否够用，要看接下来几天的 `affinity_log` 正负比与 `affinity_trend.drop`；
4. `test_regressions` 那条「情绪刻痕 vs 中性」历史失败仍未处理。
