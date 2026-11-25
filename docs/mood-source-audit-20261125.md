# 心情来源审计：哪些地方还在「用对话猜她的心情」

> 2026-11-25 用户定调：
> **心情只能来自角色扮演 LLM 自己的心理活动（`[[THINK]]`）。**
> 她理解错了别人的意思，那是她的事；心情模块不负责纠正，只负责把她的感受记准。
> 标签块负责的是「这句话是不是对她说的、在说什么」（`target`）。

**判定标准（一句话）**：任何一条写 `mood` 的路径，都要能回答
「这次 Δ 的依据是她哪一句 `THINK`」。答不上来，就是外部猜测。

---

## 0. 结论

审计时发现 **4 处 P0** 在用对话猜心情，其中两处会把「猜出来的心情」当成她自己的心理活动
再喂回去（闭环污染）。

**2026-11-25 20:20 已全部修复**（见 §6 修复记录）；标注侧见 `emotion-annotation-spec.md` §1。

---

## 1. P0：会把猜测写进她的「自我」

### P0-1 状态卡把 `judge.mind` 当成「你心里那句话」

`services/mood.py:1422`

```python
mind = str(j.get("mind") or "").strip()
if mind:
    lines.append(f"· 你心里那句话：{mind}")
```

`j["mind"]` 是 `mood_judge` 从**群聊窗口**编出来的（`mood_judge.py:721`，没有 THINK 时还会
`_mind_only()` 再编一句）。它被 `prompt_builder.py:531` 拼进实时 prompt，变成她的第一人称心理。

后果：她下一轮写 `THINK` 会被这句假心理带偏；判读再读这句 THINK —— 自证循环。

**改法**：这一行改读 `mood_judge.recent_think(session_key, 1)`（她真实写过的 THINK）；
没有就整行不输出。`judge.mind` 只允许出现在 `/测函数 心情` 的诊断里，且标注「判读猜测」。

### P0-2 `backfill_think` 会伪造她的心理活动

`services/mood_judge.py:598` + `_mind_only()`（:544）

她没写 `THINK` 时，后台让模型看群聊编一句，`auto=True` 写进 `data/mood_think.json`。

实测：281 条里 **87 条是补写的**。这批数据不能当标签源（本次标注已排除），
但它仍留在文件里，会进 `recent_think()`、进状态卡、进记忆。

**改法**：`mood_think.json` 只存 `auto=false`；补写的另存
`data/mood_think_inferred.json`，字段 `source="judge_guess"`，任何「她的心理活动」展示都不读它。

### P0-3 前窗 `passive=True` 补判：没有 THINK 也改心情

`services/mood_judge.py:880-898`

段内前几个窗口她没有回复（没有 THINK），仍按群聊逐窗判读，只把 |Δ| 压小（`PASSIVE_MAX`）。

**改法**：passive 窗口不写 `v`，只写「环境事件」（给记忆/群氛围用）；
或者干脆不判。她的心情只在她自己回应过的窗口里更新。

### P0-4 强负面快速通道没看她的 THINK

`docs/emotion-dynamics-design.md` §5.3（对应 `mood_judge` 的 fast path）

判据是「@她/引用她 + 命中辱骂词」→ `v_floor = -0.45`。
这是纯外部规则：她可能根本没往心里去（本轮标注里大量 `@她 + 傻鱼` 她都是顺着玩笑接的）。

**改法**：护栏只在她的 `THINK` 里出现负面时才生效；
或删掉整段，等正式判读（它本来就读 THINK）。

---

## 2. P1：语义/存储上的错误标注

| 位置 | 现状 | 问题 |
|---|---|---|
| `services/memory_v2.py:278` | 把 `judge.mind` 存成「她当时的心里话」 | 有 THINK 就存 THINK；没有就空，别存猜测 |
| `docs/emotion-dynamics-design.md` §5.2 | judge 输出 schema 里有 `mind` | `mind` 应是**输入**（她的 THINK），不是输出 |
| `docs/architecture-map.md:284/302` | `judge{word,value,mind}` | 同上；`mind` 改名 `guess` 或删除 |
| `docs/prompt-skeleton-design.md:400` | 已经写了「不把 judge.mind 当状态注入」 | 与 `mood.py:1422` 的实现相反，代码要跟上 |
| `data/mood_history.jsonl` | `reason` 是判读模型的解释 | 只能当诊断，不能当标签 |

---

## 3. 正确的数据流

```text
她写 [[THINK|…]]  ──► 判读模型（只做 THINK → 档位/名词）
                          │
                          ▼
                     ΔVAD → mood（v / a / d）
                          │
                          ▼
                    状态卡只回显「她自己写过的 THINK」

群聊对话 ──► 只作为上下文（她看到了什么），不直接产生 Δ
```

---

## 4. 已经正确的部分

- `services/llm.py` `THINK_PROTOCOL`：每条回复必须带 `[[THINK|…]]`（20 字内，第一人称）。
- `aura_logic.parse_think()`：把 THINK 从正文剥离，不会发到群里。
- `mood_judge.judge()` 的 prompt 里有「【她的心理活动】」段——**方向是对的**，问题只在
  没有 THINK 时用对话补、以及把补出来的结果当成她的。
- `prompt_builder` 状态卡里「你自己写的感觉」（MEMO）和「刚才那下」（judge 反应词）是分开的，
  这部分没问题。

---

## 5. 建议修复顺序

1. **P0-1 + P0-2**（同一个闭环）：状态卡只回显真实 THINK；补写不再进 `mood_think.json`。
2. **P0-3**：passive 窗口不写 v。
3. **P0-4**：快速通道加 THINK 门槛。
4. P1 的文档/字段改名（`mind` → `guess`），避免下一个人再写反。
5. 改完跑 `scripts/test_regressions.py` + `scripts/verify_all.py`；涉及 prompt 的用
   `scripts/eval_replay.py` 做离线 A/B。

---

## 6. 修复记录（2026-11-25 20:20）

| 项 | 改动 | 文件 |
|---|---|---|
| P0-1 | 状态卡「你心里那句话」改读她真实的 `THINK`（`recent_think_items`），没有就不输出；不再读 `judge.mind` | `services/mood.py` |
| P0-2 | `backfill_think` 的猜测改写 `data/mood_think_inferred.json`（`source=judge_guess`），不再污染 `mood_think.json` | `services/mood_judge.py` |
| P0-3 | `judge_and_store(passive=True)` 直接返回，不写 `v`/a/d/记忆刻痕/好感/群心情 | `services/mood_judge.py` |
| P0-4 | 删除 `_early_observe` 里的快速通道调用（`mood.fast_guard` 函数保留，但不再被调用） | `plugins/chat/__init__.py` |
| P1 | `res["mind"]` 改为她真实的 THINK；判读猜测改名 `res["guess"]`，只留诊断 | `services/mood_judge.py` |

同步改的文档：`DESIGN.md`（ADR-013/032 标废弃，新增 ADR-038，§9.1 第 3 条）、
`emotion-dynamics-design.md`（§5.1/5.2/5.3）。

> `MOOD_THINK_KEEP` 仍是 30；历史补写数据（87 条 `auto=true`）还在旧 `mood_think.json` 里，
> 需要时可一次性清理：只保留 `auto=false`。
