# 功能预期对照审计（2026-11-28）

> 状态：**审计快照 + 修复计划**。本文对照「文档声明的功能预期」与「线上实测 + 代码核查」，
> 逐项判定 **达到 / 部分达到 / 未达到 / 潜伏 / 仅 dist**，并给出**为何**（根因 + 证据）。
> 预期基线：[features.md](features.md)（as-built 2026-11-24）+ [DESIGN.md §0.1](DESIGN.md)（接线状态 2026-11-27，代码为准）。
> 实测来源：服务器 `admin@127.0.0.1:~/qq-grok-bot/`（只读，bot.log / napcat log / data/ 运行时状态，2026-11-28）。
> 修复对象：本地副本 `E:\deepseek\qq-grok-bot`（**不改服务器**）。
> 判定原则：先核对 docs / ADR / tuning 语义，**设计权衡不算 bug**；只有「文档承诺 ≠ 代码行为」才记问题。

---

## 0. 结论速览

- **大部分核心功能达到预期**：对话/人格、记忆写入与档案挖掘、心情/好感/群心情主链路、
  收发件人归因、互动边、用量统计、人格 ID、NODE_COMPUTE 开关、provider 注册表、picsearch 等均在线正常工作。
- **未达到 / 部分达到共 14 项**（下文 §2 详列），其中**线上已发生**的 6 项、**潜伏**（配置未触发）5 项、**仅 dist** 1 项、**重启恢复**1 项、**待拍板**1 项。
- **4 项早期误判已撤回 / 收窄**（§3），避免把设计当 bug。
- **修复进度（2026-11-28）**：P1 全 5 项 + P2 第 6/7/8/9/11 项 + P3 第 12/13/14/15/16/17 项已修并验证（共 16 项代码修复 + 2 处文档同步），
  全部 `py_compile` 通过、可独立导入模块跑纯逻辑/sqlite 断言。
- **上线验证（2026-11-28）**：按用户要求「逐项部署 → docker restart → 查 napcat/bot 日志 → 通过再下一项」。
  已上线并验证：① mood（q_at 回填 global+26/26 sessions、P 不丢、WS 重连正常）；② memdb（gid 隔离生效、公共 `gid=''` 保留、age 子句 + imp/sal 救援实测通过）；
  ③ white_moon（容器内双向验证：`BORROW=0→0`、`BORROW=1→120`，总开关不再被 reply_pipeline 绕过）；④ skill_switch（`/技能 关 主动接话`/`主动插话` → `proactive` 组，interject+private 同关，隔离测试通过）；
  ⑤ agent_client（`_rag_search` payload 实测含 `method=hybrid`；真机 `127.0.0.1:8001` hybrid 检索返回「记忆/召回」相关片段，非诗词无关命中）；
  ⑥ llm+chat（llm `TOOL_CTX.get("proactive")` 门控 + chat `proactive=True` 成对生效：容器内 set→True、finally 恢复→None；vision 合并模块可导入；chat 插件加载成功；两文件 md5 与本地一致）；
  ⑦ tool_dispatcher+draw（sudo 覆盖 root 文件、md5 一致；`_secret_of` 头/query 回退保留旧 node 不掉线；`/api/heartbeat` 缺/错 secret 均返回 `auth failed` 无 500，Request 注入正常；两插件加载成功）；
  ⑧ petpet/depends.py（sudo 覆盖、md5 一致；`Arg`/`RegexArg` 非法/缺值返回 `""` 而非 None，避免 nonebot TypeMisMatch；插件加载成功）。
  服务器侧 8/8 批次代码修复已全部上线并逐项验证（每个批次：tar 推送→md5 对齐→`docker restart`→查启动/插件加载/针对性子验证）。整体复检：容器全 Up、无 traceback、群消息正常处理、算力机心跳 200（旧 node 走 query secret 仍可用，证明 `_secret_of` 回退兼容）。
  **node.py 属本地算力机进程（无 docker），需单独重启才激活 header 心跳/降级 note；重启会打断在途任务、依赖启动 cwd/env，留作需确认的单步。** 待拍板项（§5）仍待决策。
  剩余：P2-10（archive 自动迁移）与 P3-18（线程重启恢复）待拍板（§5）；P3-19 低优先一致性 bundle 逐项待核对。详见 §4。
- 修复优先级见 §4；需用户拍板的见 §5。

状态图例：✅达到 ⚠️部分达到 ❌未达到 🔒潜伏（默认配置不触发） 📦仅 dist（ anonymized 发布件） ⏸待拍板

---

## 1. 达到预期的功能（在线实测正常，简列）

| features.md | 功能 | 证据 |
|---|---|---|
| §1.1/1.2/1.3 | 群/私聊回复、人格卡、隐藏标记协议 | bot.log `[THINK]`/`[MOOD]`/回复链正常；persona 切换日志 |
| §2.1(写入)/2.3 | 长期记忆写入、群友档案挖掘 | data/memory.db 9834 条；`[MEM]` 683 行；profiles 持续更新 |
| §3.1(Δ累积/空闲自减)/3.3 | 心情事件位移、空闲自减、群心情 | mood.json 会话 Δ 累积；group_mood R2/R6 实测通过 |
| §3.2 | 人物好感（v1 cause=speaker 护栏） | mood_judge 双重护栏；affinity 只吃 dv |
| §4.1/4.2/4.3 | 关系底色、安静/屏蔽、破冰回暖 | chat_switch / quiet_advisor / recent_thaw 在线 |
| §6(运行时) | 群聊现场线程进 prompt | group_scene 内存 `_THREADS` → render（873-895）正常 |
| §9/§13 | /技能 /用量 /调参 /测函数 /记忆统计 | 诊断命令日志正常；usage_chart 出图 |
| §14 | 以图搜源 picsearch | trace.moe / SauceNAO cap 日志在线 |
| §15 | 人格 ID 换号不丢记忆 | data/self_id.json 在线；identity 归一正常 |
| §16 | 戳一戳次数屏蔽 | poke_guard 滑动窗口（无异常证据） |
| §17 | NODE_COMPUTE 开关 | bot.py 跳过 draw/song_cover 逻辑核查通过 |
| §19 | provider 自由搭配 | providers.resolve/status；persona 日志按角色落模型 |
| §20 | GROUP_WATCH_MODE full/lite | 线上 full；lite 分支 11-25 已修缩进 |
| DESIGN §0.1 | accept_rate 闭环（12h 窗口） | proactive.note_reply 被 chat:2238 调用，ACCEPT_WINDOW_H 生效 |
| DESIGN §0.1 | a/d 只记录、互动边、检索 top-k/多样性/MMR、长答 long_doc、上下文预算、输出气泡 | 代码核查 + 测试通过；属设计内行为 |

---

## 2. 未达到 / 部分达到（含为何）

### ❌ A. 心情 P 的时间驱动回补（Q 通道）对全局失效 —— 线上已发生
- **预期**：[emotion-dynamics-design.md §3.1:44](emotion-dynamics-design.md)
  `P⁻ = P_prev×exp(−2Δt/τ) + q_rate×Δt_h`，`q_rate=0.02+0.10×reactivity`/小时；
  DESIGN §0.1「v 步进 + 锚点【已接线】」。P 应随真实时间回升，滤波器才有效。
- **实测**：data/mood.json 全局 `P=0.00986`、`judge_n=606`；会话 P 持续走低（0.015→0.012→0.011）。
  P 塌缩后 K≈0，判读几乎不再推动心情 → 「她越来越麻木」。
- **为何（根因）**：
  1. `services/mood.py:515` 在进 `_one` **之前**先 `st["global"]=_decay(global, now)`，
     把 global 的 `updated_at` 写成 now；`_one`（519-530）再算 `dt_h=now-updated_at≈0`
     → 全局 Q 回补恒 0。（`_one` 内部顺序注释已修，但**全局预衰减**这条漏了。）
  2. `touch/get_mood/note_window/anchor_tick` 会把 `updated_at` 重置为 now 却**不积分 Q**，
     两次判读之间的真实流逝时间被吞掉 → 会话级 Q 回补也偏小。
  3. 代码 P 公式 `_p_of(rec)+q_rate*dt_h` 缺文档的 `exp(−2Δt/τ)` 衰减项（需核对 `_decay` 是否已含）。
- **修复**：引入专用 `q_at` 时间戳，只在 `_one` 真正积分 Q 时前进，免疫 touch/get_mood 时钟重置；
  去掉/重排 :515 全局预衰减，使 global 的 dt_h 在衰减前计算。详见 §4-P1。

### ❌ B. 记忆群隔离被 FTS 召回分支击穿 —— 线上已发生
- **预期**：features.md §2.1「记忆带群号，**默认群内记忆只在群内用**」。
- **实测**：FTS 召回跨 6 个 gid 命中（live 复现）；data/memory.db 中 **8115 条 gid=''**。
- **为何**：`services/memdb.py:698-720` `_candidates` 的 FTS 分支（707-717）只过滤
  `m.status='active'`，**没有 gid / 时间过滤**；向量/关键词分支有 gid 护栏，FTS 分支漏。
  gid='' 的历史记忆（早期未带群号）进一步放大跨群泄露。
- **修复**：FTS 分支补 `gid IN (本群, '')` 与时间窗过滤，与其余分支对齐；gid='' 视为「公共」需用户确认口径（§5）。

### ❌ C. 算力机翻唱 demucs 解包崩溃 + vocal_path 未定义 —— 线上已发生（任务失败）
- **预期**：features.md §7 翻唱 `/翻唱 /清唱 /测歌`；§8 node.py RVC 流水线。
- **为何**：`node.py:620` demucs 分离返回 **2 元组** `return None, None`，
  但 :702/:839/:2796/:3105 按 **3 元组**解包 → ValueError；
  `process_cover_full_task`（:3035+）`vocal_local`/`vocal_path` 在 :3178/:3190 读取处可能未赋值 → NameError；
  :3246 `shutil.copy(vocal_local, eng_in)` 在变调前丢弃原始人声。
- **修复**：统一 demucs 返回元数；补 vocal_path 赋值与失败分支；保留变调前人声。§4-P1。

### ❌ D. 工具调度吞掉失败状态 —— 线上已发生
- **预期**：features.md §7 画图/翻唱失败应如实回报；§10 skill 调用。
- **为何**：`plugins/tool_dispatcher/__init__.py:282` 无条件把任务标 `completed`，
  失败/超时也被当成功 → 用户收到「画好了」其实没图；二级 task_id（:562/:591）秒级精度可碰撞。
- **修复**：按真实结果置 completed/failed；task_id 加毫秒/随机后缀。§4-P1。

### ❌ E. 算力机心跳同步阻塞主循环 —— 线上已发生（70s 心跳缺口）
- **预期**：features.md §8 node.py 拉任务/回传应稳定心跳。
- **实测**：bot.log 锚定 `^11-28 12:(3[89]|4[0-6])` 确认 **70s 心跳缺口**。
- **为何**：`node.py:4505` 心跳处理是**同步**函数，跑在主循环（:4455+）里，
  重活（TTS/翻唱）期间阻塞心跳 → 服务器判离线。
- **修复**：心跳放独立线程/异步，与任务执行解耦。§4-P1。

### ❌ F. 识图简报被排除出回复上下文 —— 线上已发生（视觉丢失链）
- **预期**：features.md §7 识图「发图片 → vision_agent」，回复应能用上识图结果。
- **实测**：16:06:17→21→24 视觉丢失链（识图成功但回复没引用）。
- **为何**：`plugins/chat/__init__.py:4815` `text=segments.format`，
  :4820-4831 `exclude_mids=_seg_mids` 把识图简报所在 mid 一并排除 → 模型看不到自己刚识的图。
- **修复**：合并视觉简报段，exclude 只排原始图片占位、保留 vision brief。§4-P1。

### ⚠️ G. 白月光「借风格总开关」被 v2 回复路径绕过 —— 线上已发生
- **预期**：tuning:709 `WHITE_MOON_BORROW`「白月光借风格**总开关**（0=只给 1 行事实，1=允许借句式）」，默认 0；
  DESIGN §0.1「白月光借风格护栏【已接线】…默认 0，只给 1 行事实」+ `PERSONA_EXPRESSIVENESS` 冷人设压比例。
- **为何**：`WHITE_MOON_MODE` 只是布尔总开关（ENABLED），**没有 style_only 子模式**。
  两条渲染路径：`llm.py:3219→white_moon.render()`（:486/:511 **读** BORROW，护栏生效）；
  `reply_pipeline.py:551→white_moon.render_style_only()`（:579-647 **完全不读** BORROW/RATIO/EXPRESSIVENESS，
  无论开关都注入风格摘要 + 2~3 句原话）。v2 回复流水线走后者 → 「总开关 0」名不副实，冷人设护栏也缺失。
- **判定**：tuning 明确称其为「总开关」、DESIGN 承诺默认 0 只给 1 行事实 → 一条路径绕过属**真实不符**，非设计权衡。
- **修复**：render_style_only 接入 BORROW（0 时退化为 1 行事实）+ EXPRESSIVENESS 压比例，与 render() 对齐。§4-P2。

### ⚠️ H. 判读 falsy-zero 吞掉合法 0 值 —— 线上已发生（量级偏小）
- **预期**：DESIGN §0.1 判读 schema relation_relevance/confidence/reactivity/warmth/trust_speed 按模型给值合成。
- **为何**：`services/mood_judge.py` 多处 `float(res.get(x) or default)`：
  :1303/:1565 `relation_relevance or 0.5`、:1370/:1450/:1502/:1671 `confidence or 0.6`、
  :1520-1521 `warmth/trust_speed or 0.5`、:1373 `reactivity or 0.5`。
  模型给 **0**（合法：完全无关/零置信/零反应）时被 `or` 当成假值吞掉、回填默认 → 系统性高估相关度/置信。
  （:965 解析处用 `d.get(x, 0.5)` 是对的，问题在下游消费处。）
- **修复**：改 `res.get(x)` + `None` 判定（`v if v is not None else default`），保留合法 0。§4-P2。

### ⚠️ I. 图书馆 archive.db 空 + RAG 命中无关文档自 suspend —— 线上已发生
- **预期**：DESIGN §0「角色脑 + 图书馆（RAG）」；features.md 记忆/图书馆应可检索历史。
- **实测**：data/archive.db **0 行**（对话已 20 天）；图书馆 16:14:08 因 RAG 命中无关文档（宋词/结构化文档）**自 suspend 6h**。
- **为何**：归档只靠手动 `scripts/weekly_export.py:246` purge，无自动迁移 → archive 长期空；
  RAG 索引混入无关文档，检索相关性差触发熔断。（本地 rag-api 已用 patched retriever 重启缓解，见 H:\Agentic-RAG。）
- **修复**：archive 自动迁移 + purge 安全护栏；RAG 导入前过滤无关文档。§4-P2（部分需拍板 §5）。

### ⚠️ J. 心跳 secret 走 URL/日志 —— 线上已发生（安全）
- **预期**：部署文档心跳鉴权应不泄露密钥。
- **实测**：bot.log / napcat log 中心跳 secret 出现在 **URL 查询串**并被记录。
- **为何**：心跳把 secret 放 query param，日志默认记 URL → 密钥落盘。
- **修复**：secret 改放 HTTP header；日志脱敏。§4-P2（**本文不重复密钥值**）。

### ⚠️ K. 主动私聊把 target 标成「当前说话人」，与主动框架矛盾 —— 潜伏（DRY_RUN 挡住）〔已修，措辞收窄〕
- **预期**：features.md §5.2 主动私聊「挑人主动找他说一句」，应以**她自己的身份**发、且框架是「她主动开口」。
- **核查纠正**：发送身份**本就正确**——`bot.send_private_msg(user_id=target)` 以 bot 自身发出，
  dialogue_log 用 `_self_id(bot)` 记发送者。**不存在「把她当成对方/冒充说话人」**（早期措辞不准，已收窄）。
- **真实为何**：`_send_proactive` 传 `speaker_qq=target` 给 call_llm，触发 llm.py:3128 渲染「【当前说话人】target（QQ target）」，
  与 `proactive.prompt_block:410`「现在是你主动找他，**不是他找你**」自相矛盾（target 并没说话）。
  线上 `PROACTIVE_DRY_RUN=true` 只打日志不发 → 未触发；关 dry-run 后模型会收到矛盾框架。
- **修复**：`set_tool_context(..., proactive=True)`；call_llm「当前说话人」行加 `and not TOOL_CTX.get("proactive")` 抑制。
  speaker_qq 保留供安静判定/post_rules/工具用。§4-P3-12 ✅。

### 🔒 L. 第三方插件 level confirm/owner 未强制 —— 潜伏
- **预期**：features.md §10 插件商城「核心能力不可关」、第三方 skill 应有权限分级。
- **为何**：plugin.toml 声明的 level=confirm/owner 在调度处未强制校验（单 SUPERUSERS 下 `next(iter)` 也属潜伏）。
- **修复**：调度前按 level 校验权限。§4-P3。

### 🔒 M. json→sqlite 反向迁移缺口 —— 潜伏（sqlite 后端不触发）
- **预期**：DESIGN §0.1「记忆双写 json→sqlite【已接线】默认开」。
- **核查**：`memory_v2._dual_mirror`（:61-63）`BACKEND=="sqlite"` 时早退 —— 双写是 **json→sqlite 单向**，属设计。
  线上 sqlite，json 镜像按设计关闭。**非 bug**；但若日后 sqlite→json 回切，近期记忆不在 json → 迁移缺口。
- **判定**：🔒潜伏（回切场景），记为迁移注意事项，不改默认行为。

### 📦 N. dist 发布件 UA 串被 anonymization 损坏 —— 仅 dist
- **预期**：make_dist.py 只 anonymize QQ/昵称/密钥/IP，不应破坏无关字符串。
- **为何**：`scripts/make_dist.py:237-262` `redact_hosts` 把公网 IPv4→127.0.0.1，
  误伤 User-Agent 里的版本号 `Chrome/120.0.0.0→Chrome/120.0.0.0`（约 10 个 dist 文件）。
  服务器/本地真件不受影响，**仅 GitHub 发布件**。
- **修复**：✅ redact_hosts 加 UA token 护栏（匹配点前 `[A-Za-z]+/$` 即 Chrome/Edge… 跳过；`://` 不误判）。
  功能测试 UA 版本号保留、真主机照常脱敏。§4-P3-14。**注：已生成的旧 dist 件需重跑 make_dist 才会修正**。

### ⚠️ O. 线程持久化只写不读（重启恢复） —— 重启场景
- **预期**：features.md §6 线程进 prompt；DESIGN §0.1「threads【已接线】upsert_thread + sync」。
- **核查**：运行时线程走内存 `_THREADS`（group_scene:873-895 render），**功能在线达到**；
  `memdb.upsert_thread` 只写表、无人读 → **重启后线程结构丢失**，靠 dialogue replay 合成 mid 重建（D22）。
- **判定**：⚠️部分达到（运行正常、重启恢复降级）。修复：重启时读 threads 表恢复，或明确以 replay 为准并删写-only 表。§4-P3（需拍板 §5）。

### ⏸ P. 视频理解 / 以图绘图 姿势·景深·画风 —— 待拍板 / 未实现
- **预期**：features.md §7 视频理解「发 B 站视频」、以图绘图「重绘/线稿/姿势/景深/画风」；§8 node.py 视频理解 offload。
- **实测/核查**：node 从未上报 `video_understand` cap → 工具未挂载；
  以图绘图的 姿势/景深/画风 workflow 模板缺失，`node.py:3993-4001` **静默回退 txt2img**（用户以为按姿势画，实际文生图）。
- **判定**：⏸未实现/降级。完整视频理解需新实现（待拍板）；静默回退应改为**显式告知降级**而非假装成功。§4-P3 + §5。

---

## 3. 早期误判撤回 / 收窄（避免把设计当 bug）

1. **私聊心情 2h 半衰期「失效」→ 撤回**：`mood.py:_load()`（920-923）按会话 key 回填 `_scope`
   （`_scope_of`:233-239），私聊半衰期实际生效。
2. **accept_rate「10min vs 12h 不一致」→ 撤回**：12h 是已接线的归因窗口（proactive.note_reply，chat:2238）；
   10min 是 eval 脚本里**未埋点**的评估指标（proactive-design.md:210），两者用途不同，非线上 bug。
   DESIGN §0.1（11-27）已标 accept_rate【已接线】，与代码一致。
3. **「P 只降不升」→ 收窄**：`event_p_boost`（:534）/`ANCHOR_P_ADD`（:569/:586）确实会抬 P；
   问题收窄为「**时间驱动 Q 通道**对全局失效」（§2-A），非 P 完全只降。
4. **相关度护栏「全面失守」→ 收窄**：affinity 有双重护栏（mood_judge:1474/:1516）；
   falsy-zero 的实际影响面是 memory/mood_events/group mood 的相关度/置信，非好感全链路。

---

## 4. 修复计划（基于 E:\deepseek\qq-grok-bot，按优先级）

> 验证手段：本地 Python 3.15 **无 nonebot**，完整测试套件跑不了 →
> 每个改动用 `py_compile` + 可独立导入的纯逻辑断言验证；`scripts/check_tuning.py --strict` 若可跑则跑。
> **不改服务器**；不输出任何密钥值。

> **修复进度（2026-11-28 更新）**：P1 全 5 项 ✅ 已修 + 验证；P2 第 6/7/8/9/11 项 ✅ 已修 + 验证，
> 第 10 项 ⏸ 待拍板（§5.5）；P3 第 12–19 项待修。所有已改文件 `py_compile` 通过。
> 状态标记：✅已修+验证 ⏸待拍板 ⬜待修。

### P1（崩溃 / 线上已发生的功能失效，先修）—— ✅ 全部完成
1. ✅ **node.py 翻唱崩溃三件套**：demucs :620 失败分支统一返回 3 元组 `None, [], "unknown"`（对齐 :702/:839/:2796/:3105 解包）；
   `process_cover_full_task` `vocal_path`→`vocal_local`，`vocal_local=Path(_shifted)` 使变调前人声流入 RVC（:3246）；
   :3375 cleanup 补 `{task_id}_vocal.wav`（变调后原始人声不再孤儿）。py_compile OK。
2. ✅ **tool_dispatcher:282** `_failed=isinstance(_out,dict) and bool(_out.get("error"))`，按真实结果置 failed/completed（HTTP 仍回 {"status":"success"}）；
   /分离 wait 循环补 failed/timeout 分支如实回报；task_id :574/:603 `str(int(time.time()))`→`str(time.time_ns())` 防碰撞。py_compile OK。
3. ✅ **node.py 心跳** 移入守护线程 `_heartbeat_loop`（`hb={"ok","fail","ok_streak"}` + `hb_wake=Event()`），主循环只读 `hb["ok"]`，
   异常 handler 置 `hb["ok"]=False; hb_wake.set()` → 重活不再阻塞心跳。py_compile OK。
4. ✅ **chat:4815 区域** `segments.format` 后合并视觉简报：按 segment records 的 `ev.get_message()` 图片段判 `_has_img`，
   `_vdesc` 取 item text（`VISION_TAG` 开头）或回退 `vision_agent.last_image(session_key)["desc"]`，追加进 text → 模型能看到自己刚识的图。py_compile OK。
5. ✅ **mood.py Q 通道**：新增专用 `q_at` 时钟（`_new_rec`/`_decay` 保留字段/`_load` 回填），`_one` 用 `q_at`（回退 updated_at）算 dt_h、
   积分后才前进 `q_at` → 免疫 touch/get_mood/全局预衰减(:515) 重置 updated_at。纯逻辑验证：流逝 1h P_prior=0.125 vs 无流逝=0.005（Δ0.12）。py_compile OK。

### P2（量级偏差 / 安全 / 护栏）
6. ✅ **mood_judge falsy-zero**：新增 `_fget(d,key,default)`（None 判定，保留合法 0），替换 confidence×4 / relation_relevance×2 /
   reactivity/sensitivity/warmth/trust_speed 各 `or` 消费点。纯逻辑验证：给 0 保留 0、给 None 回填默认。py_compile OK。
7. ✅ **memdb FTS 分支**：`_candidates` 抽出共享 `filt`/`fargs`（gid `m.gid='' OR m.gid=?`、时间 `m.ts>=? OR m.importance>=0.8 OR m.salience>=0.5`），
   FTS join 与 plain 查询（别名 `memories m`）同时套用 → 跨群泄露堵住。sqlite 实测：外群记忆不再命中。py_compile OK。（gid='' 口径仍见 §5.1）
8. ✅ **white_moon.render_style_only**：ENABLED 检查后补 `if not int(BORROW or 0): return ""`（默认 0 退化为只给 1 行事实）；
   `_r=_borrow_ratio()`（含 EXPRESSIVENESS<0.3 冷人设压比例）引导「可以借 **{_r:.0%}**」+ `[:BORROW_CHARS]` 截断，与 render() 对齐。纯逻辑验证通过。py_compile OK。
9. ✅ **心跳 secret→header**：node.py send_heartbeat/pull/download/check_server_conn 把 secret 从 query 移到 `X-Node-Secret` header（download 用 extra_headers，http_items 心跳 URL 去掉 `?secret=`）；
   tool_dispatcher 新增 `_secret_of(request, fallback)` 读 header（query 回退兼容），heartbeat/pull_task/tasks_status/download_input 签名加 `request: Request`。
   POST form-data 里的 secret（body，不进 URL 日志）保留不动。py_compile OK。（**不重复密钥值**）
10. ⏸ **archive 自动迁移 + purge 护栏**；RAG 导入过滤无关文档 —— **待拍板 §5.5**（触发点/保留窗口未定，不 unilateral 改）。
11. ✅ **anchor() p_add**（mood.py:679）：独立 anchor 唯一调用方是 anchor_tick（静默周期锚点，文档 line145「静默回补只有 anchor_tick 一条路」），
    原漏传 p_add → 静默回补实际不补 P。补 `p_add=ANCHOR_P_ADD`。纯逻辑验证：P 0.005→0.025（+0.02）；每窗混入 p_add=0 仍不回补（回归通过）。py_compile OK。

### P3（潜伏 / dist / 降级告知 / 一致性）
### P3（潜伏 / dist / 降级告知 / 一致性）
12. ✅ **主动私聊上下文框架**（chat `_send_proactive` / llm.py:3120）：核查后**收窄** §2-K 措辞——
    发送身份本就正确（`bot.send_private_msg` 以 bot 自身发、dialogue_log 用 `_self_id`），不存在「把她当成对方」。
    真实问题：`speaker_qq=target` 使 call_llm 渲染「【当前说话人】target」，与 prompt_block:410「现在是你主动找他，不是他找你」矛盾。
    修复：`set_tool_context(..., proactive=True)`，call_llm「当前说话人」行加 `and not TOOL_CTX.get("proactive")` 抑制；
    speaker_qq 仍保留供安静判定/post_rules/工具用。py_compile OK。
13. ✅ **skill 别名**「主动接话」→ proactive：新增 `GROUP_ALIAS`（主动接话/主动插话→proactive），set_enabled 的 `_rev.update(GROUP_ALIAS)`。
    纯逻辑验证：`/技能 关 主动接话` → off_groups 含 proactive，interject/private 均关，其他组不受影响；开「主动开口」恢复。py_compile OK。
14. ✅ **make_dist redact_hosts** UA 护栏：匹配点前 `re.search(r"[A-Za-z]+/$", ...)` 即 UA token（Chrome/Edge…）跳过；
    `://` 前一位是 / 不匹配，真主机照常脱敏。功能测试：Chrome/120.0.0.0、Edge/120.0.0.0 保留，http://、ws://、裸公网 IP 全换 127.0.0.1。py_compile OK。
15. ✅ **以图绘图静默回退**→显式降级（3 文件协同）：node `process_draw_task` 检测 `{mode}.json` 缺失→result 带 `note`+实际 mode；
    tool_dispatcher `elif file:` 分支保留 result JSON 到 `task["meta"]`（以前直接丢弃）；draw 插件发图后读 meta.note 发「⚠️ 已降级为文生图」。
    纯逻辑验证：pose/depth/style 缺模板判降级、txt2img/img2img 不误判。py_compile OK。
16. ✅ **agent_client rag_search method**：核查 RAG 侧 `Config.DEFAULT_RETRIEVAL_METHOD="hybrid"`，所有检索入口遵循它，
    唯独 rag_search 工具硬编码默认 "vector"（rag_tools.py:85）→ bot 不传 method 被迫纯向量，绕过 patched retriever 关键词优先（§2-I 无关命中诱因）。
    body 补 `"method":"hybrid"`。py_compile OK。
17. ✅ **petpet None 默认**：核查 NoArg 消费方无类型注解（`arg=NoArg()`）不触发 TypeMisMatch，不动；
    真问题在 `Arg`（非法受限值 return None）与 `RegexArg`（缺组/异常 return None）配 `str` 注解 → 依赖解析阶段 TypeMisMatch（my_friend `name.strip()` 还会炸）。
    两处改返回 ""，meme 体 `if arg=="右"`/`arg or 默认`/`x.strip() or 默认` 自然兜底。分支逻辑验证通过。py_compile OK。
18. ⏸ **线程重启恢复**（读 threads 表或明确 replay 口径）—— **待拍板 §5.4**，不 unilateral 改。
19. ⬜ 低优先一致性：ACK 三处谓词统一（chat:797/:3574-3578/:9889-9900）、发送配额绕过（:8712）、
    quote 工具参数校验（:8647-8648）、_TURN_MS、world/self about_qq、TTS_CMD shell=True（node:3593-3595）、
    dead configs、plugin level 强制（§2-L）。逐项需先核对 docs/语义再改，本轮未动。

---

## 5. 待用户拍板（不 unilateral 改）

1. ✅ **gid='' 的 8115 条历史记忆口径**〔用户 2026-11-28 拍板〕：视为**公共记忆**（所有群可用）。
   跨群记忆串是**主动接话设计**：「在其他地方收到欺负可以去找好感度高的人去安慰」。
   修复已兼容：`if gid:` 才加过滤（群聊正常回复隔离外群专属记忆）；proactive 传 gid="" → 不过滤 → 跨群全量可用。
   `OR m.gid=''` 保留公共分支。§2-B 的「跨 6 群命中」是群聊被动泄露（FTS 漏 gid），非主动设计路径。
2. **replied-guard 终止 vs 补一句**：防重复冷却内被点名的消息，是直接终止还是补一句？（行为口径）
3. **视频理解完整实现**：是否本轮实现 node `video_understand` cap + B 站解析？还是先显式降级告知、后续再做？
4. **线程重启恢复**：读 threads 表恢复，还是以 dialogue replay 为唯一口径并删 write-only 表？
5. **archive 自动迁移触发点**：定时（cron）还是写入时增量？purge 保留窗口多长？

---

## 6. 仅 dist / 部署文档不符（不影响线上运行）

- **UA 损坏**（§2-N）：仅 GitHub anonymized 发布件。✅ make_dist 已修（§4-P3-14）；旧 dist 件需重跑才修正。
- ✅ **features.md §11「229 项」tuning**：实测 `len(DEFAULTS)=504`，已把 features.md:206 改为 504 项。
- ✅ **features.md §8 `node_config/prompt_presets.py`**：核查 `config/` 与 `node_config/` 两份**完全相同**，
  node.py:3861 实际 `from config.prompt_presets` 导入，`node_config/` 无代码引用（不会 ImportError，只是文档指了副本）。
  已把 features.md:170 改为 `config/prompt_presets.py` 并注明副本。`node_config/` 死副本是否删除待用户确认（不 unilateral 删）。
- **dist 文档 dangling**：deploy_manifest.txt 被 make_dist:45 排除、node-params 等引用在发布件缺失（早期已记，未动）。

---

*审计人：Qoder（只读服务器实测 + 本地代码核查）。本文随修复进度更新；每项修复落地后在 §4 标注 commit/验证结果。*