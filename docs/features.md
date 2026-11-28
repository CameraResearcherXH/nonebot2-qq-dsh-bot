> ⚠️ **as-built 快照（2026-11-24）**：描述当时的实现，不是目标设计。
> 现行设计以 [DESIGN.md](DESIGN.md) 为准；差异见 [design-audit-2026-11-25.md](design-audit-2026-11-25.md)。
> 已知：功能总览；流水线/主动/安静/白月光已由 v2 重新设计。

# 功能总览：每个能力是干嘛的、怎么用

> 这一页按「用户能感知到的能力」分类。想改参数看 [tuning.md](tuning.md)；
> 想加插件看 [plugin-dev.md](plugin-dev.md)；算力机看 [node-compute.md](node-compute.md)。

## 0. 技术栈

- **框架**：基于 **NoneBot2** 开发；
- **协议**：通过 **OneBot v11** 接入 QQ；
- **协议端**：默认使用 **NapCatQQ**（`napcat` 容器 / 本地进程）；
- **链路**：`NapCatQQ → OneBot v11 → NoneBot2 → core / services / plugins`；
- **算力机**：Python + ComfyUI（画图）/ RVC（翻唱）/ IndexTTS（语音）。

所有功能都是在这套框架上实现的；`plugins/` 下的插件遵循 NoneBot2 的 matcher / 事件模型，
第三方 nonebot 插件放进 `plugins/third/` 即可被加载和注册成 skill。

---

## 1. 对话与人格

### 1.1 群聊 / 私聊回复
- **是什么**：收到 @、喊名字、私聊时，结合人格 + 当前消息 + 群聊现场 + 档案 + 记忆生成回复。
- **怎么用**：正常聊天即可；群里需要 @ 她或喊她名字。
- **代码**：`plugins/chat/__init__.py`、`services/llm.py`、`services/prompt_builder.py`
- **配置**：`.env` 的 `XAI_BASE_URL` / `XAI_API_KEY` / `XAI_MODEL`；`GROUP_MAX_HANZI`、`REPLY_INTERVAL`。

### 1.2 人格卡
- **是什么**：决定「她是谁、什么性格、怎么说话」。纯文本，一段一人格。
- **怎么用**：把 `services/Personality/<名字>.txt` 放好；`.env` 里 `DEFAULT_PERSONA=<名字>`；
  聊天里 `/persona list` 看全部，`/persona 名字` 切换。
- **模板**：`personality.example.txt`
- **说明**：[persona-card.md](persona-card.md)

### 1.3 回复协议（隐藏标记）
她可以在回复里带这些标记，系统解析后不会发出去：
- `[[THINK|心里话]]`：每轮心理活动（心情判读用）
- `[[MOOD|心情原话]]` / `[[MEMO|...]]`：她自己写的心情 / 记忆
- `[[PROFILE|名字]]`：查档案
- `[[STICKER|标签]]`：发表情
- 具体协议在 `services/llm.py` 的 `*_PROTOCOL` 常量里。

---

## 2. 记忆

### 2.1 长期记忆
- **是什么**：每个 turn 一条目录项，按时间累积；带摘要、标签、重要性、情绪刻痕（Δ）。
- **怎么用**：自动；`/记忆统计` 看条数；`/记忆体检` 做一致性检查；`/记住 <事>` 手动写一条。
- **代码**：`services/memory_v2.py`（JSON 后端）、`services/memdb.py`（sqlite 后端）
- **配置**：`MEMORY_BACKEND=json|sqlite`、`MEMORY_*`、`RECALL_*`
- **隔离**：记忆带群号，默认群内记忆只在群内用。

### 2.2 记忆召回 / 回访
- **是什么**：按关键词 + 时间 + 情绪强度 + 说话人相关度召回；7 天以前、很久没提的旧事会给一条「可以顺口提一句」。
- **怎么用**：自动进 prompt；`MEMORY_REVISIT=false` 可关。

### 2.3 群友档案挖掘
- **是什么**：从群聊里持续提取「这个人是谁、聊过什么、说话风格」，写进 `data/profiles/`。
- **怎么用**：自动；`/档案 @某人` 查看；`/档案` 不带人看自己的。

---

## 3. 心情 / 好感 / 群心情

### 3.1 当前心情
- **是什么**：-1.00 ~ 1.00 的持续变量，按会话；由第三方判读的 **Δ（这件事让她涨/跌多少）** 累加。
- **名词**：按数值每 0.2 分一档（被击穿 / 很难受 / … / 特别好）。
- **峰值事件**：显示最近 2 小时内「还没被反向波动抵消」的最大 |Δ|，并带当时的心理活动。
- **空闲自减**：5 分钟没人理，向 0 走 0.01；再 5 分钟再 0.01。
- **怎么用**：`/心情` 查看；`/测函数 心情` 看判读明细。
- **代码**：`services/mood.py`、`services/mood_judge.py`
- **参数**：`MOOD_*`

### 3.2 人物好感
- **是什么**：对某个人的 -1 ~ +1 显式好恶，只被「这个人导致的心情 Δ」推动；有每日净变化上限。
- **好感名词**：判读模型参考档案给一个描述性说法（「嘴硬但吃这套的熟人」）。
- **印象最深的一件事**：档案里会显示「你与他印象最深刻的一件事（当时你心里：…）」。
- **怎么用**：自动；`/档案 @某人` 查看。
- **代码**：`services/profiles.py`、`services/mood_judge.py`
- **参数**：`AFFINITY_*`、`BOND_*`

### 3.3 群心情（独立于整体心情）
- **是什么**：这个群最近 5 小时让她涨/跌了多少；正负互相抵消。
- **喜欢 / 讨厌人数**：每个人净 Δ ≥ +0.05 / ≤ -0.05，算人数和比例。
- **判定**：
  - 某个人 5 小时净跌 ≥ 0.4 → 单独不理他（含 @）
  - 整群净跌 ≥ 0.7 且 ≥ 3 人 → 判定「群聊导致的不愉快」，建议整群安静
- **影响**：群心情差 → 明显少说话（`chatty` 乘数 0.55~1.35）。
- **怎么用**：`/测函数 安静` 查看群心情明细。
- **代码**：`services/group_mood.py`、`services/quiet_advisor.py`
- **参数**：`GROUP_MOOD_*`

---

## 4. 关系与安静

### 4.1 关系底色
- **是什么**：长期关系（好感 + 亲近 + 印象最深的事），和「此刻心情」分开。
- **怎么用**：自动进 prompt；`/档案` 看完整卡片。

### 4.2 安静 / 只不理某个人
- **是什么**：她可以整个群安静，也可以只屏蔽某个人（他的 @ 和图片也不接）。
- **怎么用**：
  - `/安静 [分钟]`：整个会话安静
  - `/说话`：解除
  - 她自己也会用 `chat_switch` 工具；`/测函数 安静` 看建议依据
- **代码**：`services/chat_switch.py`、`services/quiet_advisor.py`

### 4.3 破冰 / 道歉
- **是什么**：对方道歉、哄她 → 好感回暖、心情允许回升，安静建议降权。
- **怎么用**：自动识别；`services/chat_switch.py` 的 `recent_thaw`。

---

## 5. 主动行为

### 5.1 主动接话
- **是什么**：群里聊得热闹、她插得上话时，偶尔主动接一句。
- **怎么用**：自动；`/技能 关 主动接话` 可关。
- **代码**：`plugins/chat/__init__.py` 的 `_random_chat_loop`

### 5.2 主动私聊
- **是什么**：按「亲密度 + 当前心情 + 对他的好感 + 注意力漂移」挑人，主动找他说一句。
- **怎么用**：自动；`PROACTIVE_DRY_RUN=true` 只打日志不发消息。
- **代码**：`services/proactive.py`
- **参数**：`PROACTIVE_*`

### 5.3 主动倾诉
- **是什么**：心情低 + 对某个人好感高 → 更愿意主动找他说话（×1.15）。
- **怎么用**：自动，属于主动私聊打分的一部分。

---

## 6. 群聊现场

- **是什么**：把群消息补上「谁 → 谁」，按回复链 / 提及 / 参与者聚成线程；给模型「线程 + 参与者 + 最近两句 + 谁在叫她」。
- **怎么用**：自动进 prompt；`/测函数 现场` 看硬逻辑 vs 审核 LLM。
- **代码**：`services/group_scene.py`、`services/scene_auditor.py`

---

## 7. 多媒体能力

| 能力 | 命令 / 触发 | 代码 | 依赖 |
|---|---|---|---|
| 画图 | `/画图 <提示词>` | `plugins/draw/` | 算力机 ComfyUI |
| 以图绘图 | `/以图绘图 重绘/线稿/姿势/景深/画风` | `plugins/draw/` | ComfyUI + workflow |
| 翻唱 | `/翻唱` / `/清唱` / `/测歌` | `plugins/song_cover/`、`services/song_gender.py` | 算力机 RVC |
| 语音 | `/测语音`、聊天里自动发语音 | `services/tts.py` | 算力机 IndexTTS / 云端 CosyVoice |
| 表情包 | `/发表情`、自动配表情 | `plugins/sticker_admin/`、`services/sticker_*` | 本地表情库 |
| 识图 | 发图片 | `plugins/vision_agent/` | 视觉模型 |
| 视频理解 | 发 B 站视频 | `plugins/vision_agent/` | 视频理解接口 |
| 搜索 | 自动 / `/tavily` | `plugins/nonebot_plugin_tavily_search/` | Tavily API |

算力机怎么部署看 [node-compute.md](node-compute.md)。

---

## 8. 算力机（node.py）

- **是什么**：把画图、RVC 翻唱、IndexTTS 语音、视频理解这些重活放到本地/GPU 机器上，
  通过 HTTP 从服务器拉任务、回传结果。
- **文件**：
  - `node.py`：算力机主程序
  - `node.env.example`：配置模板（复制成 `node.env` 再填）
  - `config/prompt_presets.py`：画图 prompt / 采样配方（node.py:3861 实际导入路径；`node_config/` 下有一份相同副本，无代码引用）
  - `comfy_workflows/*.json`：ComfyUI workflow 模板
  - `my_kkI_rvc.py`：RVC 单文件推理入口
  - `rvc_pipeline.py`：RVC 音域对齐 / 变调 / 混音流水线
- **怎么用**：[node-compute.md](node-compute.md)

---

## 9. 管理与自检

| 命令 | 作用 |
|---|---|
| `/技能` | 查看能力开关 |
| `/技能 关 全部` | 急停：关掉所有非核心能力 |
| `/技能 开 搜索,画图` | 按组开启 |
| `/用量` | token 用量、最贵 / 最省 Top/Bottom |
| `/调参` | 查看当前调参表（只读） |
| `/测函数 心情 / 安静 / 现场` | 诊断 |
| `/记忆统计`、`/记忆体检` | 记忆条数 / 一致性 |
| `/档案 [@某人]` | 人物卡 |
| `/词条` | Lorebook 词条 |
| `/上传文件` | 接收文件 |

---

## 10. 插件商城

- **是什么**：把第三方 nonebot 插件放进 `plugins/third/`，用 `plugin.toml` 声明成 function calling skill；
  角色扮演模型可以在对话里直接调用。
- **怎么用**：见 [plugin-dev.md](plugin-dev.md)；第三方来源见 [third-party-audit.md](archive/third-party-audit.md)。
- **开关**：`/技能` 可以按组关掉；核心能力不可关。

---

## 11. 参数与阈值

所有可调参数集中在 `core/tuning.py`（504 项，全部中文说明），`.env` 同名变量可覆盖。
完整表见 [tuning.md](tuning.md)。改完用：

```bash
python3 scripts/check_tuning.py --strict
python3 scripts/dump_tuning.py --write   # 重新生成 docs/tuning.md
```

---

## 12. 白月光（2026-11-23 新增）

- **是什么**：统计**群友之间**「谁最常 @ / 引用 / 接谁的话」，得到每个人的白月光 + 群白月光；
  她回某个人时借那个人的句式/口头禅，回大家时用群白月光。
- **怎么用**：自动；`/测函数 白月光 [群号]` 查看。
- **代码**：`services/white_moon.py`、`services/llm.py`（`white_moon.render`）
- **配置**：`WHITE_MOON_MODE`、`WHITE_MOON_MIN_SCORE`、`WHITE_MOON_STYLE_ITEMS`、`WHITE_MOON_STYLE_TTL`
- **说明**：[whitemoon.md](whitemoon.md)

## 13. 用量查看与 prompt 体检（2026-11-23 增强）

- **是什么**：按用途拆 token、缓存命中率、Top/Bottom，并出横向柱状 + 扇形图；
  `/测函数 prompt` 给出 prompt 各段占比、块内位置、缓存报告。
- **怎么用**：`/用量`（带图）、`/用量 文字`、`/测函数 用量`、`/测函数 prompt [文字]`。
- **代码**：`services/llm_usage.py`、`services/usage_chart.py`、`services/prompt_report.py`
- **说明**：[usage-report.md](usage-report.md)

## 14. 以图搜源（三方插件，2026-11-22）

- **是什么**：给一张图片链接，聚合 TraceMoe / SauceNAO / Ascii2D / IQDB 查出处。
- **怎么用**：模型自动调 `picsearch` 工具；也可以 `/以图搜源 <图片>`。
- **代码**：`plugins/third/picsearch/`、`vendor/PicImageSearch/`、`services/picsearch_tool.py`
- **接入方式**：`plugin.toml` 声明 `[[tools]]`（详见 [third-party-integration.md](third-party-integration.md)）

## 15. 人格 ID：换号不丢记忆（2026-11-23 新增）

- **是什么**：`SELF_ID` 固定「她是谁」，`NAPCAT_ACCOUNT` 是当前登录号，
  `SELF_ALIASES` 是历史号；旧号说过的话、@ 旧号的记录仍算「她」。
- **怎么用**：换号只改 `NAPCAT_ACCOUNT`，**不要改 `SELF_ID`**；旧号追加到 `SELF_ALIASES`。
- **代码**：`services/identity.py`
- **说明**：[memory-profile-design.md](memory-profile-design.md#21-人格-id换号不丢记忆)

## 16. 戳一戳次数屏蔽（2026-11-23 新增）

- **是什么**：同一个人 2 小时内戳超过 3 次 → 第 4 次起不回、也不戳回，只记上下文；
  滑动窗口，2 小时后自动恢复。
- **怎么用**：自动；阈值 `POKE_MAX_PER_WINDOW` / `POKE_WINDOW_HOURS`。
- **代码**：`services/poke_guard.py`、`plugins/chat/__init__.py`

## 17. 算力机开关（`NODE_COMPUTE`，2026-11-24）

- **是什么**：`1` 走 `node.py`（画图/翻唱/语音/视频理解排任务给算力机）；
  `0` 不加载本项目的 `draw` / `song_cover`，也不注册这些 function 工具，
  画图/唱歌交给 `plugins/third` 里的本机插件。
- **怎么用**：`.env` 里 `NODE_COMPUTE=1|0`；改完 `docker compose up -d --force-recreate bot`。
- **代码**：`services/config.py`、`bot.py`、`services/llm.py`（`_NODE_ONLY_TOOLS`）

## 18. Harness 改代码桥（`CODE_AGENT=harness`，2026-11-24）

- **是什么**：把 QQ 会话接到本机 DeepSeek Harness，可以真的读仓库、改代码、跑测试；
  结果原样回 QQ。带上人格/心情/好感/白月光/相关记忆。
- **怎么用**：`CODE_AGENT=harness` + `HARNESS_BACKEND=cli|http`；
  `/h <需求>`、`/改代码 <需求>`、`/h 新`、`/h` 看状态。
- **和 self_tool 互斥**：`CODE_AGENT=harness` 时 `plugins/self_tools` 不加载。
- **代码**：`services/harness.py`、`plugins/harness/`
- **说明**：[harness-bridge.md](harness-bridge.md)
## 19. 模型自由搭配（provider 注册表，2026-11-24）

- **是什么**：主聊天 / 心情判读 / 收发件人审核 / 识图 / 风格学习 / 对话状态 / 档案挖掘
  每个角色都能单独指定 provider + 模型（deepseek / glm / qwen / xai / openai / 自己的中转站）。
- **怎么用**：`.env` 里 `<ROLE>_PROVIDER` / `<ROLE>_MODEL` / `<ROLE>_BASE_URL` / `<ROLE>_API_KEY`；
  `/测函数 模型` 看当前每个角色落到哪家、哪个模型、key 有没有。
- **例子**：主聊天换 `glm-5.3-flash`；识图换 `deepseek-flash`；两个检测分别用 deepseek / qwen。
- **代码**：`services/providers.py`（`resolve` / `chat_url` / `status`）
- **说明**：[model-providers.md](model-providers.md)

## 20. 群聊「全局 / 省流」两种观察模式（`GROUP_WATCH_MODE`，2026-11-24）

- **full（默认）**：所有群消息都进判定，她会关注全部群聊（现状）。
- **lite（省流）**：消息照常记录（现场 / 白月光 / 心情都不受影响），但**只有硬信号才入队回复**：
  @ 她 / 引用她 / 喊她名字 / 收发件人判定是她。上下文条数仍由 `MAX_GROUP_RECENT`（默认 16）控制；
  省流模式下不主动插话、不做表达学习。
- **私聊不受影响**；群白月光是硬逻辑统计，两种模式都在记。
- **怎么用**：`.env` 里 `GROUP_WATCH_MODE=full|lite`，改完 `up -d --force-recreate bot`。
- **代码**：`services/config.py`、`plugins/chat/__init__.py`（`_need_reply` 的省流分支）。
  ⚠️ 2026-11-25 修：这段分支以前被误缩进在 `except` 里，正常路径下 lite 等于 full。
- **另一把开关**：`POWER_MODE=full|eco` 管「算不算」（审核 / 图书馆 / 主动 / 表达蒸馏），
  和本节的「回不回」正交；见 [deployment.md §5.9](deployment.md#59-运行档位powermode和-58-正交)。

## 21. 引用精确到消息号 / 同一件事不回两遍（2026-11-24）

- **引用**：prompt 里每行都带 `#消息号`，她写 `<reply:消息号>`（或 `<reply:#消息号>`）。
  发送前会校验：不是这一段的真实 mid → 如果是行号（`<reply:2>`）就按段内顺序映射；
  都不是就**不引用**，避免「引到上面/下面那条、回错人」。
- **防重复**：刚回过这个群 `GROUP_REPLY_COOLDOWN`（默认 45s）内，没点她的消息只进上下文、
  不单独开一轮；@ 她 / 喊名字不受影响。
- **协议残留**：`〔引用…〕` / `〔引〕` / 半个 `〔` 一律当残留清掉，不再出现在气泡里。
