# 功能链路体检报告（feature-audit）

> 生成时间：2026-11-20
> 范围：主动接话、安静、破冰、回复判定、表情、记忆召回 六条链路。
> 方法：只读代码，按「触发 → 闸门 → 决策 → 执行 → 写回 → 可观测」六段逐条走通；
> 静态检查脚本：`scripts/audit_features.py`（可在每次改动后重跑）。
> 结论标记：✅ 通；⚠️ 通但代价/语义不对；❌ 断（功能不生效）；➖ 该链路没有这一段。

---

## 0. 总览

| 链路 | 结论 | 主要断点 |
|---|---|---|
| A 主动接话 | ⚠️ 半通 | 无 tool context、`addressed=True`、只查群安静、无 per-session 互斥、不破冰 |
| B 安静 | ⚠️ 半通 | `_need_reply` 不查开关（白烧模型）、建议未接执行、人桶全局 |
| C 破冰 | ❌ 断 | 只在 `_need_reply` 内，上游 block 插件命中即丢失 |
| D 回复判定 | ⚠️ 带副作用 | 记账/破冰/转写全在 Rule 里，被上游 block 打断 |
| E 表情 | ⚠️ 半通 | 标签固定 18 词、`sticker_save` 无 tag、发送失败仍记已发 |
| F 记忆召回 | ❌ 断 | BM25 当硬门槛、`gid` 两处未传、群前缀只有一条路径写 |

---

## A. 主动接话（`_random_chat_loop`，`plugins/chat/__init__.py:4565`）

| 段 | 现状 | 结论 |
|---|---|---|
| 触发 | 每 15s 轮询 `ALLOWED_GROUPS`；群 5 分钟无活动清空 recent 并跳过 | ✅ |
| 闸门 | 冷却 / 高刷 / `_is_muted` / 群安静 / 关注期 / 队列非空 / 10s 内回过 / 最小消息数 / 已看过 | ⚠️ 见 A2/A3 |
| 决策 | `policy_for` + `decide_group_reply`（含静默时段、额度、相关度、概率） | ✅ |
| 执行 | `call_llm(f"group_{gid}", prompt, group_context=_ctx_lines)` → 只发 1 条 | ❌ 见 A1 |
| 写回 | `_spawn_mood_judge` + MEMO → `memory_v2.remember` + `mood.bump_by_emotion` | ⚠️ 见 A4 |
| 可观测 | `[SILENCE]` / `[GROUP]` 日志；`_warn_audit_wire` | ✅ |

**断点**

- **A1（❌ 执行）不设 tool context**：`chat:4830` 直接 `call_llm(...)`，全文件只有 4 处 `set_tool_context`，都不在插话路径。
  → 插话时任何工具（搜索、画图、表情、记忆）都会返回「缺会话信息」；模型只能空口说。
- **A1b（⚠️ 语义）`addressed` 默认 True**：插话不是「在叫她」，但 `call_llm` 默认按「在对她说」处理，
  → 群里可能突然发语音、唱歌、@ 人。
- **A2（⚠️ 闸门）只查群安静**：`chat:4604` 只 `chat_switch.is_off(f"group_{gid}")`，不查最后说话人。
  → 她对某个人 strict 安静后，主动插话仍可能接那个人的话。
- **A3（❌ 并发）无 per-session 互斥**：只用「队列非空 / 10s 内回过」当代理锁，
  `_session_worker` 与插话循环可同时对一个群开口 → 「刚回完又插一句」「同一条回两次」。
- **A4（❌ 写回）不破冰**：`maybe_thaw` 只在 `_need_reply` 调，插话路径没有。
  → 冷战时插话永远不会自己回暖。
- **A5（⚠️ 写回）`about_qq=""`**：插话写的 MEMO 不归属任何人（`chat:4856`），
  与主回复路径（`about_qq=speaker_qq`）不一致，档案里看不到这段。

---

## B. 安静（`chat_switch` + `_need_reply`）

| 段 | 现状 | 结论 |
|---|---|---|
| 触发 | 主人 `/安静`；她自己 `chat_switch(action=off)`；`quiet_advisor` 建议 | ✅ |
| 闸门 | `llm.py:2574` `is_off` → 强制 `stay_silent` | ✅ |
| 决策 | `_need_reply` 完全不查 `chat_switch` | ❌ B1 |
| 执行 | 仍入队、仍调模型，由 LLM 内部强制沉默 | ⚠️ B1 |
| 写回 | 照常记上下文/记忆（设计如此） | ✅ |
| 可观测 | `/状态`、`/测函数 安静`、`[SWITCH]` 日志 | ✅ |

**断点**

- **B1（❌ 判定/成本）`_need_reply` 不查开关**：`chat:1819-2170` 无 `chat_switch.is_off`。
  安静期间每条消息仍然：入队 → 防抖 → 写 profile → 审核 → GLM 预检 → 调 DeepSeek（再被强制沉默）。
  → 行为看似对，实际白烧 token + 延迟；且 `_maybe_mood_watch` 继续判读。
  → 修法：`_need_reply` 里在记账之后、入队之前判断；`CHAT_OFF_LLM=false` 时直接返回 False。
- **B2（❌ 闭环）建议未接执行**：`quiet_advisor.render` 只写「想安静可以用 chat_switch」，
  代码里没有任何地方把「建议」变成 `set_off`。她「觉得该安静」≠ 真的安静，只能靠模型自己输出 `[沉默]`。
- **B3（⚠️ 语义）人桶全局**：`users[qq]` 不带群号 → 在 A 群被惹，B 群也不理他。
  是否改成 `gid:qq` 属待定项 D1（本次不擅自改）。
- **B4（⚠️ 语义）整群安静 non-strict**：`ANSWER_WHEN_CALLED=true` 时被 @ 仍会回。
  这是设计选择（给台阶），但和「安静就是安静」的直觉冲突，需在帮助里写清。

---

## C. 破冰（`chat_switch.maybe_thaw`）

| 段 | 现状 | 结论 |
|---|---|---|
| 触发 | 她安静中，有人道歉/哄她 | ✅ |
| 闸门 | 只在 `_need_reply`（`chat:1827`） | ❌ C1 |
| 决策 | `maybe_thaw` 关键词 + 是否 strict | ✅ |
| 执行 | 解除 `users/groups` 安静 + 记 `_THAW` | ✅ |
| 写回 | `event._thaw_note` → 判读 `repair=True` 强制回暖 | ✅ |
| 可观测 | `[SWITCH] 破冰` | ✅ |

**断点**

- **C1（❌ 触发）位置错误**：`_need_reply` 是 `chat` matcher 的 Rule。
  任何 `priority<10 && block=True` 的插件命中（`vision_agent` p5、`sticker_intent` p2、各 `on_command` p4/5），
  Rule 不会执行 → 破冰不发生。用户道歉后她仍在冷战。
  → 修法：拆成独立 `on_message(priority=1, block=False)`，先记账 + 破冰，再放行。

---

## D. 回复判定（`_need_reply`）

| 段 | 现状 | 结论 |
|---|---|---|
| 触发 | 每条群/私聊消息 | ✅ |
| 闸门 | 命令 / 纯媒体 / 睡眠 / 白名单 / LATER / 纯问候 / 操作短句 / 收尾语 / 需 @ / 关注 / 喊名字 | ✅ |
| 决策 | 返回 True/False | ✅ |
| 执行 | 入队 | ✅ |
| 写回 | **在 Rule 里做**：破冰、mood watch、`_enrich_text`（网络 IO）、dialogue_log、recent、group_scene、`_note_dislike` | ❌ D1 |
| 可观测 | `[CMD]` / `[LATER]` / `[GREET]` / `[OP]` / `[CLOSE]` / `[FOCUS]` | ✅ |

**断点**

- **D1（❌ 结构）Rule 带副作用**：上游 `block=True` 插件一命中，以上全部不执行 → 上下文缺口。
- **D2（⚠️ 成本）`_enrich_text` 在 Rule 里**：语音转写、转发抓取、链接预览都是网络 IO，
  即使这条消息最后判定「不回」，也已经付了网络代价。应移到「确定要处理」之后或独立异步。
- **D3（⚠️ 顺序）`_maybe_mood_watch` 在 Rule 里**：上游 block 时不判读，
  「越冷越不说话 → 越不判 → 越难回暖」的死锁在图片/表情消息上仍然存在。

---

## E. 表情

| 段 | 现状 | 结论 |
|---|---|---|
| 触发 | `sticker_collect`（p4, block=False）收集；聊天里 `kind=="sticker"` 发送 | ✅ |
| 闸门 | `_can_collect_here` 白名单；`sticker_receiver` 视觉判「好不好笑」 | ⚠️ E1 |
| 决策 | 视觉模型从固定 18 词里挑标签；挑不出兜底「吃瓜」 | ❌ E1 |
| 执行 | `send_sticker_by_tag(bot, ev, val)` | ⚠️ E2 |
| 写回 | `said.append(f"[表情:{val}]")` | ❌ E2 |
| 可观测 | `[CHAT] 发表情失败` warning | ⚠️ |

**断点**

- **E1（❌ 设计）标签固定 + 工具无 tag**：`sticker_receiver.TAG_LIBRARY` 18 词、`coerce_tag` 强制映射、
  失败兜底「吃瓜」；`sticker_save` 工具不接受 tag 参数（`chat:6939`）。
  → 她自己的心情词进不了标签体系，「看到图 → 判心情 → 大涨才收」无法落地。
- **E2（❌ 写回）失败仍记已发**：`chat:4456` 忽略 `send_sticker_by_tag` 的返回值，
  无论成功失败都 `said.append("[表情:val]")`。失败时函数内部还会额外发一句
  「没有「X」这个标签的表情哦」→ 双重错误。
- **E3（⚠️ 一致性）三条发送路径**：主回复 `4456`、工具 `4504`、插话 `4959`，
  返回值处理不一致，需统一成一个 `_send_sticker_safe()`。

---

## F. 记忆召回

| 段 | 现状 | 结论 |
|---|---|---|
| 触发 | 每轮 `build_packet` → `recall_hits`；`[[MEMORY]]` / `[[PROFILE]]` 工具 | ✅ |
| 闸门 | `group_ok()` 群隔离；`is_noise()` 噪音过滤 | ❌ F2 |
| 决策 | `score = bm25 × w_speaker × w_imp × w_emo × w_forget` | ❌ F1 |
| 执行 | 读 `blocks/*.json` 拼 `text` | ✅ |
| 写回 | `mark_hit` 更新 `last_hit_at/hit_count`（全量 `_save_index`） | ⚠️ F3 |
| 可观测 | `[RECALL]` 日志、`/记忆体检` | ✅ |

**断点**

- **F1（❌ 召回）BM25 是硬门槛**：`memory_v2.py:362`
  `if base <= 0 and importance < 0.7: continue`。
  → 「在吗」召不出「昨天他说的那件事」，「近期记得清」没有实现。
  → 修法：`score = (0.15 + bm25_norm) × ...`，让时间维度自然压远期。
- **F2（❌ 隔离）`gid` 两处未传**：`services/proactive.py:334`、`plugins/chat/__init__.py:6350`（`[[PROFILE]]`）
  调 `recall()` 没传 `gid` → 跨群泄漏。
- **F2b（❌ 隔离）群前缀只有一条路径写**：`chat:4118` 给 MEMO 加 `（群号:xxx）`，
  其余 `remember()`（`chat:2502/4065/4185/4856/4990/6100/6337/8326`、`referee:527`、`vision_verify:1221`）
  都没加 → `group_ok()` 对它们不生效。
- **F3（⚠️ 性能）全量读写**：`_load_index` / `_save_index` 每次全量 parse+dump（2383 条 ≈12MB），
  `recall(mark_hit=True)` 命中后还要再写一次。→ 迁 sqlite 后消除。
- **F4（⚠️ 排序）目录默认相关度**：`recall_catalog` 默认按 score 排，与「目录从近到远」不符。

---

## G. 附带确认（不属于六条主链路，但影响它们）

| 编号 | 位置 | 问题 | 影响 |
|---|---|---|---|
| G1 | `chat:4087/4130` | 记忆刻痕用**上一轮**判读 `abs(value)` | Δ 从未被算过 |
| G2 | `mood_judge:593` | 判读 `speaker_qq` 每条都带，环境心情误归因 | 好感被环境心情污染 |
| G3 | `.env:71` | `MAX_GROUP_RECENT=10` 覆盖代码 16 | 长消息窗口修复失效 |
| G4 | `chat:3722` | `last` 只在沉默分支更新 | `REPLY_INTERVAL` 限流失效 |
| G5 | `services/text.py:26` | `limit_hanzi` 只数汉字 | 长英文/emoji 不截断不转发 |
| G6 | `llm.py:2542/768` | `_db_history` 给所有历史 user 行套当前说话人 | 重启后串人 |
| G7 | `mood.py:255` | `get_mood()` 读接口也 `_save()` | 每轮 3~5 次全量写盘 |

---

## 断点清单（按修复优先级）

| 优先级 | 编号 | 一句话 | 目标阶段 |
|---|---|---|---|
| P0 | C1 | 破冰移出 `_need_reply`，独立 priority=1 记账器 | behavior-wiring |
| P0 | A1 | 插话补 `set_tool_context` + `addressed=False` | behavior-wiring |
| P0 | B1 | `_need_reply` 查 `chat_switch`，安静时不入队 | behavior-wiring |
| P0 | D1 | 记账/破冰/转写移出 Rule | behavior-wiring / pipeline-observe |
| P1 | A3 | 插话与 worker 加 per-session 互斥 | behavior-wiring |
| P1 | A2 | 插话查最后说话人的 strict 安静 | behavior-wiring |
| P1 | G4 | `last` 与 `session_last_reply` 同步更新 | behavior-wiring |
| P1 | E2 | 表情发送检查返回值，失败不记已发 | behavior-wiring |
| P1 | G5 | `limit_hanzi` 按字符宽度计数 | behavior-wiring |
| P2 | F1 | BM25 降为加分 | recall-mood-sticker |
| P2 | F2/F2b | `gid` 收口 + 群前缀统一 | record-v2 |
| P2 | E1 | 表情三行识别 + 阈值收集 + 标签合并 | recall-mood-sticker |
| P2 | G1/G2 | Δ 时序 + cause 归因 | record-v2 |
| P3 | F3 | 索引迁 sqlite | memdb-sqlite |
| P3 | G3/G6/G7 | 配置覆盖、历史串人、读接口写盘 | stopgap-security / behavior-wiring |

---

## 附：静态检查

```bash
python3 scripts/audit_features.py          # 打印 PASS/FAIL 表
python3 scripts/audit_features.py --strict # 有 FAIL 时退出码 1
```

检查项与断点编号一一对应；修复 behavior-wiring 后应全部 PASS。

---

## 修复状态（2026-11-20）

A~H 全部断点已修；新增 G11/G12/G13 后，`python3 scripts/audit_features.py --strict` 23/23 PASS。
仍属结构债（不在本表检查范围）：
- `plugins/chat` 未物理拆分；
- `services/llm.py` 仍反向 import `plugins.chat`（已记入 `scripts/dep_baseline.txt`）。


## 新增闸门（2026-11-20 晚）

| 编号 | 检查项 | 防的是什么 |
|---|---|---|
| G11 | 涨幅驱动（不是水平驱动） | 记忆刻痕 / 好感 / 档案必须用 Δ，不能拿当前心情水平 |
| G12 | 峰值事件 / 印象最深一件事 | 心情峰值必须带 2 小时抵消与心理活动；档案要有「印象最深的一件事」；空闲自减 5 分钟 0.01 不能失效 |
| G13 | 群心情（单人 0.4 / 整群 0.7） | 群心情必须独立于整体心情；单人 0.4 → 单独不理；整群 0.7 + 3 人 → 群聊不愉快；主动私聊要看好感 |
