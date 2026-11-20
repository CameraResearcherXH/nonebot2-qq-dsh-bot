# 整体架构地图 + 问题定位（2026-11-20）

> 用途：把「项目实际长什么样」画成一张可以指着说话的图，然后把所有问题钉在图上，
> 标注**影响半径**（用户能感知到什么），方便你决定改哪些、先改哪些。
>
> 前置澄清（按你的说明修正）：
> - `bot.py` + `plugins/` + `services/` 跑在**香港服务器 docker**；
> - `node.py` + `node.env` 跑在**上海算力机**；
> - 本地只做源码查看/修改，**不运行**。
> 所以本文讨论的是「两套运行时的代码与数据契约」，不是本地进程。
>
> 编号约定：`C1~C6` = 结构矛盾；`B1~B20` = 具体 bug；`D1~D5` = 需要你拍板的决策点。

---

## 1. 运行时拓扑（谁在哪、怎么说话）

```
                        ┌──────────────────── 外部 API ────────────────────┐
                        │ DeepSeek(主)  GLM  DashScope(Qwen-VL/Audio)      │
                        │ Tavily  trace.moe  B站  pixiv  qlogo  GitHub     │
                        └───────▲──────────────────────────▲───────────────┘
                                │ HTTPS                    │ HTTPS
┌─────────────── 香港服务器 · docker · network_mode=host ───┴───────────────┐
│                                                                          │
│   ┌──────────────────┐   ws://127.0.0.1:3001   ┌───────────────────────┐ │
│   │  napcat 容器     │◄───────────────────────►│ qq-grok-bot 容器      │ │
│   │  (QQ 客户端)     │   OneBot v11 事件/调用   │ python 3.11           │ │
│   │  /app/.config/QQ │                          │                       │ │
│   └────────┬─────────┘                          │ bot.py → nonebot2     │ │
│            │ QQ 协议                            │  ├ plugins/    15 自研 │ │
│            ▼                                    │  ├ plugins/third/ 21  │ │
│        腾讯 QQ 服务器                            │  ├ FastAPI 路由        │ │
│                                                 │  └ data/  (bind mount) │ │
│                                                 └───────────┬───────────┘ │
│   端口：8080 = 任务队列 API（tool_dispatcher）                │             │
│         8001 = 算力机 Agent（图书馆 recall/reflection）      │             │
│         1200 = RSSHub（已下线，代码里留了降级注释）          │             │
└──────────────────────────────────────────────────────────────┼─────────────┘
                                                               │ HTTP 轮询
                                                               │ ?secret=API_SECRET
┌─────────────── 上海算力机 · Windows · node.py ────────────────▼─────────────┐
│  node.py（客户端，不监听端口）                                               │
│    loop: 心跳(15s) → pull pending → TASK_HANDLERS[type] → 上传结果(5s 轮询)  │
│    本地服务：Demucs:8005 │ IndexTTS2:7860 │ Ollama:11434 │ ComfyUI:8188     │
│    子进程：RVC runtime python.exe                                            │
│    任务类型：cover / split / audio_separation / rvc / rvc_mix / tts /        │
│              reflection / probe / draw                                       │
└──────────────────────────────────────────────────────────────────────────────┘
```

### 1.1 部署方式与一致性缺口

| 项 | 事实 | 风险 |
|---|---|---|
| 代码挂载 | compose 把 `plugins/ services/ bot.py .env data napcat/qq scripts` 全部 bind mount | 镜像里的 `COPY . .` 基本被覆盖，但 **`.env` 仍被烤进镜像层**（B1） |
| 部署校验 | `deploy_manifest.txt` 82 条 md5 | **只含 `node.env`，不含 `node.py`** → 算力机代码没有任何完整性校验，服务器与算力机可以静默不同步（C5 的直接成因） |
| 版本控制 | 无 `.git` | 两端代码谁新谁旧只能靠文件名（`bot.py.working`、`plugins_backup/`）判断（B20） |
| Python 版本 | 容器 3.11；`node.py` 用了 `Path \| None` 且**没有** `from __future__ import annotations` | 算力机必须 ≥3.10，否则 import 就崩 —— 这条约束没有写进任何文档 |

---

## 2. 代码分层（实际依赖方向）

```
bot.py ── 只做 nonebot.init / load_plugins / run
  │
  ├─ plugins/ ─────────────────────────────────────────────────────────────
  │   chat(8628)  ← 入口 + 决策 + 状态 + 发送 + 20 个工具 + 20 条指令 + 810 行测试
  │     ▲  │
  │     │  └─► from plugins.chat import recent_group_msgs, session_queues,
  │     │       session_workers, _session_worker      ← vision_agent 直接拿私有状态
  │   vision_agent(818)  draw(756)  song_cover(1695)  cover_api(331)
  │   tool_dispatcher(475)  napcat_tools(221)  sticker_admin(477)
  │   selfcheck(660)  full_test(749)  help_menu(413+660)   ← 测试/文档进生产
  │   third/ (21 个三方插件，独立生态)
  │
  └─ services/ ────────────────────────────────────────────────────────────
      llm(3180) ← 协议 + 工具注册 + 历史 + prompt + 调用 + 落盘 + 工具实现，全在一个文件
      prompt_builder(539) memory_v2(917) profiles(808) mood(668) mood_judge(623)
      group_scene(1419) scene_auditor(451) vision_verify(1253) profile_card(961)
      ... 50 个模块，彼此随意 import，全部直接读写 data/*.json
```

### 2.1 分层违规（不是风格问题，是「改一处不知道会碰哪」）

| 违规 | 位置 | 后果 |
|---|---|---|
| **插件读插件私有符号** | `vision_agent:479` | chat 重构即断；加载顺序成隐式依赖；上下文记录责任被拆到两个插件 |
| **`llm.py` 兼任 6 个角色** | 3180 行 | 协议/工具/历史/prompt/网络/存储耦合，任何改动的影响面都无法估算 |
| **测试进生产** | `selfcheck 660 + full_test 749 + chat 内 /测函数 810` ≈ 2200 行 | 生产包里有 1/6 是诊断代码；`/全量测试` 能改状态、发消息 |
| **services 无边界** | 50 个模块平铺 | 没有「领域层/基础设施层」之分，`mood` 可以 import `profiles`，`profiles` 又 import `aura_logic`… |

---

## 3. 一条群消息的完整生命周期（问题最密集的地方）

```
① 事件到达 (OneBot v11)
   │
   ├─► priority 1/2/3/4/5 的插件先过：draw.wait_img / sticker_wait / full_test
   │   sticker_intent / help_menu / vision_agent / tool_dispatcher …
   │   ⚠️ 任何一个 block=True 命中 → 下面 ②③ 全部不执行          ← C6
   │
② chat 的 Rule: _need_reply(event)                                ← 带副作用的规则
   ├─ chat_switch.maybe_thaw（破冰）
   ├─ _maybe_mood_watch（判读触发）
   ├─ _enrich_text：语音转写 / 转发卡片抓取 / 链接预览 / 引用展开（网络 IO）
   ├─ dialogue_log.log_msg（第 2 份副本）
   ├─ recent_group_msgs.append（第 4 份）
   ├─ group_scene.observe（第 5 份）
   ├─ _note_dislike（静音判定）
   └─ 返回 True/False（判断和记账混在一起，False 也已经记账）
   │
③ 入队 → _session_worker
   ├─ 防抖 / 合并连发 / 高刷清理 / 去重
   ├─ profiles.upsert + touch + update_style   ← 3 次同步写盘（B15）
   ├─ scene_auditor.audit_recent   ── await，timeout 6s   ← 关键路径 LLM #1
   ├─ group_scene.snapshot + addressee_line
   ├─ build_prompt（背景/人称/现场）
   ├─ set_tool_context
   ├─ referee.preflight (GLM)      ── await，timeout 2.5s ← 关键路径 LLM #2
   ├─ call_llm                     ── 关键路径 LLM #3
   │    ├─ recall_hits → memory_v2.recall（全量读 index + BM25 + 全量写回，B14）
   │    ├─ build_packet（8 个块）
   │    ├─ 工具循环（最多 6 轮）
   │    └─ 补资料循环（[[HISTORY]] 等，最多 N 轮）
   ├─ parse_reply → items / memos / calls / mood / think
   ├─ 写回：memory_v2 + bond + mood.bump + affinity（4 类写）
   ├─ 发送：配额等待(≤15s) + 气泡延迟(≤4.5s) + 长文合并转发
   └─ 后台：mood_judge / conv_state.update / expression.mine / profile_miner.mine
```

**关键结论**：这条链上 **3 次串行 LLM + 至少 8 次同步磁盘写**，而每一段都用 `except Exception` 兜住，
所以任何一段坏了，表现都只是「她今天有点怪」。

---

## 4. 数据层地图（五层设计 vs 实际实现）

| 层 | 设计意图 | 代码 | 存储 | 谁写 | 谁读 | 偏离 |
|---|---|---|---|---|---|---|
| 事实层 | 发生过什么 | `memory_v2` | `data/memory/index.json` + `blocks/*.json` | MEMO / 识图 / 称谓 | `build_packet.memory_index`、`[[MEMORY]]` | 索引全量读写（B14）；群隔离靠文本前缀（脆） |
| 关系层 | 我跟他什么关系 | `profiles` | `data/profiles/<qq>_profile.json` / `_bond.json` | 每条消息 upsert / `profile_update` / bond | `render_card` / `intimacy` / `affinity` | 每条消息 3 次写盘（B15）；affinity 无衰减（C7/D2） |
| 自我层 | 我在他面前什么样 | `profiles.stance` + `expression` | 同上 + `data/expression/` | `profile_update(stance)` / 每 40 条挖一次 | `render_card` / `expression.render` | stance 长期为空（没人写） |
| 状态层 | 聊到哪了 / 我什么心情 | `conv_state` + `mood` | `data/state/*.json` / `data/mood.json` | 每 6 条压缩 / 判读 | `conv_state.render` / `mood.status_block` | 心情按**会话**不按人（C7）；`get_mood` 读接口也写盘（B21） |
| 场景层 | 此刻谁在跟谁说话 | `group_scene` + `recent_group_msgs` | 内存（重启从 dialogue 补） | 每条消息 observe | `format_block` / 审核 | 与 recent/ dialogue 三份重叠（C2） |

### 4.1 同一段对话的 7 个副本

```
                        ┌── data/memory.db (sqlite, add_memory)      ← 短期历史，只在 ctx 为空时读
一条群消息 ──┬── data/dialogue/*.jsonl (dialogue_log)        ← 原文，周报/补现场
             ├── data/memory/*.json (memory_v2 event)        ← 长期记忆
             ├── recent_group_msgs (deque, 内存)              ← prompt 的【历史对话】
             ├── group_scene._STORE (内存)                    ← 现场/线程/审核
             ├── data/state/*.json (conv_state)               ← 压缩后的状态
             └── data/profiles/*.json (bond notes)            ← 相处印象
```

**影响半径**：任何「她忘了/串人/重复」类问题，都要先判断是这 7 份里哪一份不同步。
`_db_history` 把历史 user 行全标成当前说话人（B9）就是典型：第 1 份与第 4 份的口径不一致。

---

## 5. 决策面地图（谁决定「回不回」）

| 决策器 | 位置 | 触发条件 | 闸门 | 与谁冲突 |
|---|---|---|---|---|
| `_need_reply` | chat:1819 | 群/私聊每条消息 | 白名单 / 睡眠(0-8) / 指令 / 纯媒体 / 关注 / 高刷 / 纯问候 / 短指令 / 收尾语 / 被嫌弃 / LATER 水位 | 与所有 block=True 插件（C6） |
| `_random_chat_loop` | chat:4565 | 每 15s 轮询每个白名单群 | 冷却 / 心情×亲密度 / 查看频率 / 静默时段 / 插话上限 / 关注期 / 队列空 / 刚回完 | 与 worker 抢同一个群（C1） |
| `_proactive_private_loop` | chat:6053 | 每 CHECK_SEC | 静音/次数/冷却/刚聊完/太久没聊/最后一句是她/不够熟 | 与私聊 worker（C1） |
| `vision_agent` | priority=5 block | 图片 | 冷却 / 每小时上限 / 是否@ | 绕开 `_need_reply`（C6） |
| `sticker_intent` | priority=2 block | 「收/存+表情」 | 群聊必须 @ | 抢走消息 → 上下文丢（C6） |

**影响半径**：C1 导致的重复回复/该静不静/该回不回，全部是「用户直接感知」的，
而且**无法通过看某一个文件定位**——必须同时看 5 个决策器的状态。

---

## 6. 能力面地图（三套「工具」概念，别混）

| 概念 | 定义处 | 数量 | 作用域 | 备注 |
|---|---|---|---|---|
| **A. function calling** | `register_tool()`，32 处 | 32 | 默认 private；部分 group | 走 OpenAI tools 协议，多轮 tool_calls |
| **B. 文本标记** | `_REQ_RE`（llm:664） | 17 种 | 群+私聊 | `[[[HISTORY\|20]]]`、`[[MEMORY\|词]]`…，靠「再问一遍模型」实现 |
| **C. 算力机任务** | `STATUS["tasks"]`（tool_dispatcher） | 9 类 | 服务器→算力机 | HTTP 轮询 + 文件回传，**和 LLM 工具无关** |
| **D. NapCat API** | `services/napcat_tools.TOOLS` | 60+ | SAFE/ADMIN/OWNER | 通过 `napcat_call` 两段式（先 help 再 call）暴露给模型 |

**影响半径**：B3（SAFE 级 send_private_msg/send_group_msg/get_group_msg_history 无白名单）在 D 这一层，
影响的是「她能用 QQ 做什么」，和聊天质量无关，但**风险等级最高**。

---

## 7. 外部依赖与配置

| 供应商 | 用途 | 模型 | 是否关键路径 | env 前缀 |
|---|---|---|---|---|
| DeepSeek | 主聊天 / 压缩 / 判读 / 审核 / 挖矿 | `deepseek-chat` | ✅ 主 + 4 条旁路 | `XAI_*`（历史命名） |
| GLM | preflight 参考、视觉 | `glm-5.3-flash` / `glm-4v-flash` | ✅ 关键路径（preflight） | `GLM_*` |
| DashScope | 识图 / 音频 / 语音 | `Qwen3.5-Flash` 等 | 视觉链路 | `VISION_*` / `DASHSCOPE_*` |
| Ollama（算力机） | 视频/长文摘要、复盘 | `qwen2.5:7b` | 否（降级） | `LOCAL_LLM_*` |
| 算力机 Agent | 图书馆（>30 天记忆） | —— | 否（按需） | `AGENT_*` |
| Tavily | 联网搜索 | —— | 否（按需） | `TAVILY_*` |
| trace.moe | 以图搜番 | —— | 否 | `TRACE_*` |

`.env` 共 **339 个键 / 942 行**，按前缀分布：COVER 16、RVC 16、MOOD 14、VISION 13、
SCENE 12、PROACTIVE 11、STICKER 9、GROUP 8、TRACE 8、PEER 8、SELF 8、GLM 7、AGENT 7、PROFILE 7…

**影响半径**：B8（`.env` 把 `MAX_GROUP_RECENT` 覆盖回 10）说明「代码默认值」与「实际生效值」
可以长期不一致且无人发现。339 个键里没有任何一个在启动时被校验/打印。

---

## 8. 问题定位总表（钉在图上）

> 「影响半径」= 用户/你自己能感知到的现象。
> 「修复成本」：S ≤ 半小时，M ≤ 半天，L ≥ 1 天。

### 结构级（要先决策，不是直接改）

| ID | 图上位置 | 问题 | 影响半径 | 成本 |
|---|---|---|---|---|
| **C1** | 决策面（§5） | 5 个决策器各写一套闸门 | 重复回复、该静不静、该回不回、插话过频 | L（需先定「唯一决策入口」） |
| **C2** | 数据层（§4.1） | 对话 7 份副本，无权威源 | 重启变傻、串人、忘事、翻旧账 | L |
| **C3** | 生命周期 ⑤ | prompt 在 6 处拼，固定区含动态值 | 前缀缓存经常 miss；规则互相矛盾 | M |
| **C4** | 生命周期 ③④⑤ | 关键路径 3 次串行 LLM + 4 条旁路 | p99 慢、成本高、并发无上限 | M（取决于是否保留双模型） |
| **C5** | 拓扑 §1.1 | 两运行时算法双份且已漂移 | 同一首歌 pitch 不一致；node.py 无 md5 校验 | M |
| **C6** | 生命周期 ①② | Rule 带副作用 + 上游 block | 上下文缺口（表现为「她不知道刚发生了什么」） | M |
| **C7** | 数据层 状态层 | 心情按会话、好感按人，两套时间尺度 | 一人惹她全群变冷；好感饱和/永久判决 | M（见 §9） |

### Bug 级

| ID | 位置 | 问题 | 影响半径 | 成本 |
|---|---|---|---|---|
| **B1** | `Dockerfile:97` + `.dockerignore` | `.env` 进镜像 | **全部密钥泄漏**（含 API_SECRET） | S |
| **B2** | `services/config.py:13` | 硬编码 DashScope key | 密钥随代码分发 | S |
| **B3** | `services/napcat_tools.py:87/299/367` | SAFE 级发消息/读历史无白名单 | 群里任何人可让她向任意群/人发消息、读任意群历史 → 骚扰/封号 | S |
| **B4** | `chat:6233` | `send` 工具接受任意本地路径 | 可发容器内任意图片/音频（他人档案图等） | S |
| **B5** | `node.py:2508/3648` | `_extract_json` 重复定义 | Qwen-Audio 判定静默降级 | S |
| **B6** | `chat:4830` | 插话不 `set_tool_context` | 插话时工具全废；`addressed` 默认 True | S |
| **B7** | `chat:3722/4554` | `REPLY_INTERVAL` 的 `last` 不更新 | 同一 worker 内限流失效 | S |
| **B8** | `.env:71` | `MAX_GROUP_RECENT=10` 覆盖代码 16 | 长消息被挤出窗口（修复从未生效） | S |
| **B9** | `llm.py:2542/768` | 重启后历史 user 行全用当前说话人名 | 上下文串人 | S |
| **B10** | `services/text.py:26` | 只数汉字 | 长英文/emoji 不截断也不转发 | S |
| **B11** | `chat:4454` | 发表情失败仍记「已发」 | 她以为发过；用户收到系统口吻提示 | S |
| **B12** | `services/reply.py` | 死代码仍被 import | 误导；内含直连 CosyVoice 地雷 | S |
| **B13** | `help_menu:350/169` | 文案与代码漂移（0-6 点 / 私聊 100 字） | 用户认知错误 | S |
| **B14** | `memory_v2:104/204/384` | 索引全量读改写 + O(N) 扫描 | **回得慢**；随记忆线性恶化（2383 条 ≈12MB） | M |
| **B15** | `chat:3771/3778/3781` | 每条消息 3 次同步写 profile | 群活跃时 I/O 抖动 | S |
| **B16** | `chat:810` | 配额等待 15s 在 worker 内 sleep | 阻塞整个会话（含 @ 急事） | S |
| **B17** | 全局 | `voice_*.wav` / `temp_audio` 无清理 | 磁盘缓慢增长（185MB→） | S |
| **B18** | `services/config.py:12` | `MAX_REPLY_CHARS` 死配置 | 误以为有总长上限 | S |
| **B19** | `.env` 行内注释 | 依赖 dotenv 剥离行为 | 换 loader 会在 import 崩 | S |
| **B20** | 仓库根 | 无 git / 无 .gitignore | 不可回滚、无 diff、「复制一份改改」 | S |
| **B21** | `mood.py:255` | `get_mood()` 读接口也 `_save()`，每轮调 3~5 次 | mood.json 每条消息多次全量读写 | S |

---

## 9. 心情 / 好感：你的设计 vs 现在的实现

### 9.1 你的设计（我复述，确认我理解对）

> 心情是**现在进行时**；好感是**心情的一种记录**，而且**分在每个人那里**。
> 好感决定「看到哪个人就会想到开心的事情」。
> 所以是**过去的好感 + 现在的心情**一起编织了对话的未来。

翻成数据模型：

```
人(qq) ──── 好感：一条随时间累积的"心情记录"（分人）
会话(gid/私聊) ── 心情：此刻的状态（现在进行时）
好感(过去) × 心情(现在) ──► 这一轮的语气 / 接不接 / 找谁 / 想到什么
```

### 9.2 现在的实现

```
data/mood.json
├─ global: {value -100..100, label, judge{word,value,mind,affinity,why}}
└─ sessions: {
     "group_[REDACTED_NUMBER]": {value, label, judge, updated_at},   ← 一个群一个心情
     "private_[REDACTED_NUMBER]": {value, label, judge, updated_at}  ← 一个人一个心情
   }
   半衰期 12h；GLOBAL_SHARE=0.35（会话值向全局混合）

data/profiles/<qq>_profile.json
├─ affinity: -1.00 ~ +1.00      ← 分人，±0.10/次（判读给），**无时间衰减**
├─ affinity_log: 最近 50 次 {at, delta, to, why}
└─ bond.notes: 最近 20 条 {text, emotion, at, weight}   ← 相处印象
   intimacy = f(bond 条数 × 情绪 × 30 天半衰) + affinity×0.25
```

写入路径（判读一次，同时写三处）：

```
mood_judge.judge_and_store()
  ├─ mood.set_judge(key, word, value, mind)         ← 写会话心情
  ├─ mood.set_value_from_judge(value, key)          ← 直接覆盖数值（SMOOTH=1.0）
  └─ profiles.bump_affinity(speaker_qq, affinity)   ← 写这个人的好感
```

### 9.3 五个偏离（按影响从大到小）

**偏离 1｜心情的作用域是「会话」，不是「人」**
群里 A 惹她 → `group_xxx` 的 value 掉 → B 说话时她也是冷的。
现在只用 `quiet_advisor` 给整群建议降权 0.5 来打补丁，**数值本身仍然共享**。
你的设计里「心情是现在进行时」没规定作用域，但「好感分在每个人那里」暗示了
**心情至少要能按人归因**。当前只在 `log_shift(about_qq=)` 里留了归因痕迹，没进数值。

**偏离 2｜好感没有时间尺度，且饱和极快**

- `bump_affinity` 每次 ±0.10，判读节流 `MOOD_JUDGE_MIN_INTERVAL=20s`；
- 一个活跃群里同一个人每小时最多可判 180 次 → 理论上 **±18/小时**；
- 而 `affinity` 无衰减、无每日上限 → 20 分钟就能从 0 到 +1.0（饱和），然后永久停在 +1.0；
- 安静群则相反：一次坏印象永久留着，没有「淡化」。

结果：**「好感」实际不是长期底色，而是「最近几分钟判读的累积」+「永久判决」的混合体**，
两种极端都不符合「过去的心情记录」这个定义。

**偏离 3｜「看到他就想起开心的事」这条链路没有真正接通**
现在的记忆召回是 **query 驱动**（`memory_v2.recall` 用当前这句话做 BM25）：

- 当前消息是「在吗」→ 和「他上次送我的蛋糕」零词重合 → `base<=0` 直接丢弃（memory_v2:362）；
- `about_qq` 只在**已经命中**时把权重 ×2，不能凭空召出；
- 唯一「按人」的入口是 `revisit_candidate(qq)`：只取 **1 条、7 天以前、3 天没想起**的。

所以「看到这个人 → 想起开心的事」目前只在**关键词恰好重合**或**恰好有一条 7 天前的旧事**时发生。
你想要的「底色驱动联想」，缺一个**按人 + 按情绪极性**的主动注入通道。

**偏离 4｜心情的 12 小时半衰期实际上失效**
`MOOD_FROM_JUDGE=true` 且 `MOOD_JUDGE_SMOOTH=1.0` → 每次判读**直接覆盖**数值。
判读 20s~5min 一次，`_decay` 根本来不及起作用。
于是「现在进行时」变成了「最近一次判读的滞后值」，而不是「会自然平复的情绪」。
（这不是错，但和注释里写的半衰期设计不是一回事，需要你确认要哪个。）

**偏离 5｜文档与实现不同步**
`docs/agent-pipeline.md` 十六节写「现状：影子运行，只写 judge 字段，不改变行为」，
但 `mood_judge:584` 已经在写 `set_value_from_judge` + `bump_affinity` → **判读早已上线**。
这类「文档说 A、代码做 B」会让后续所有判断失准。

### 9.4 如果按你的设计走，建议的数据模型（只定概念，先不改）

```
Person(qq)
├─ affinity_base    : -1..1   慢变量（半衰期 90~180 天，或按"有效互动次数"衰减）
├─ impressions[]    : 事件流  {ts, gid, valence(-1..1), text, mood_word, kind}
│                     ↑ 好感 = 这些事件的**时间衰减加权和**（可解释、可回溯）
└─ recall_index      : 按人 + valence 的索引（高正/高负各一组）

Session(group_x | private_qq)
└─ mood_now         : {value, label(自由文本), mind, at}  快变量（半衰期 2~6h）
                     群会话的 mood 可由「本群成员最近事件」聚合，而非一个全局数

本轮 prompt 注入：
  1) 底色：  「你对他：好感 +0.62（很亲近），想起的多是开心的：…（1~2 条高 valence 记忆）」
  2) 此刻：  「你现在：有点烦（-0.35），心里那句话：…」
  3) 规则：  「底色决定你愿不愿意理他；此刻决定你这一句怎么说。两者冲突时，以此刻的语气为主，
              但不要因为此刻的心情否定过去的底色。」
```

关键三条规则（建议写进设计文档，避免再打补丁）：

1. **好感是慢变量，心情是快变量**：好感只能被「事件」推动，且必须有衰减/上限；
   心情可以每轮变，但不写回好感（现在判读同时写两者，等于把快变量灌进慢变量）。
2. **好感只回答「我对他什么底色」，不直接决定「回不回」**；回不回由「此刻心情 + 场景 + 是否在叫她」决定。
3. **「想起开心的事」必须是按人检索，不是按当前句子检索**；且只给 1~2 条，给多了变翻旧账。

### 9.5 需要你拍板的 3 个问题（D1~D3）

| ID | 问题 | 选项 | 影响 |
|---|---|---|---|
| **D1** | 心情的作用域 | (a) 会话级（现状） (b) 人级 (c) 人级 + 群级聚合 | 决定「一人惹她全群冷」是否存在 |
| **D2** | 好感的衰减 | (a) 不衰减（现状） (b) 90/180 天半衰 (c) 按有效互动次数衰减 | 决定好感是「底色」还是「永久判决」 |
| **D3** | 「想起开心的事」的触发 | (a) 自动注入 1~2 条（按人+valence） (b) 只在她写 `[[MEMORY\|他]]` 时给 (c) 现状（query 命中才给） | 决定这条设计是否真的落地 |

另有两个架构决策（D4/D5）：

| ID | 问题 | 为什么必须先定 |
|---|---|---|
| **D4** | 关键路径是否保留「审核 + preflight」两次辅助 LLM | 决定 C4 是「优化」还是「删减」；也决定延迟预算 |
| **D5** | 对话的权威源选哪一份 | 决定 C2 的重构方向（建议 `dialogue_log` 为唯一 append-only 源） |

---

## 10. 建议的决策顺序（不是改代码顺序）

```
第一步：先定 D1/D2/D3（心情好感模型）—— 因为它决定数据 schema，schema 决定后面怎么重构
第二步：定 D5（权威源）+ D4（关键路径）—— 这两个决定 C2/C4 的方向
第三步：做「无争议的止血」B1/B2/B3/B4/B20（安全和回滚能力，和架构无关）
第四步：再做 C6（Rule 副作用）→ C1（决策收敛）→ C2（数据收敛）→ C4（网关）
第五步：最后才是拆 chat / 配置 schema / 算法共享
```

**为什么不建议先拆 `chat`**：拆文件只降低「读代码的难度」，不解决「多份事实源」。
在 D1/D5 定下来之前拆，等于把错误的边界固化到更多文件里。

---

## 11. 一句话总结

> 这套系统的**理念**（五层自我、按需拉取、静默权、破冰、好感×心情）是完整的，
> 缺的不是想法，而是**一份权威的数据模型 + 一条权威的处理链**。
>
> 现在所有问题都可以归结为：**同一个概念有多个实现，且没有一个能证明自己是对的。**
> 先定 D1~D5，再谈重构；否则每改一处，都只是新增第 N+1 份真相。
