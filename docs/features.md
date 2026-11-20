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
  - `node_config/prompt_presets.py`：画图 prompt / 采样配方
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
- **怎么用**：见 [plugin-dev.md](plugin-dev.md)；第三方来源见 [third-party-audit.md](third-party-audit.md)。
- **开关**：`/技能` 可以按组关掉；核心能力不可关。

---

## 11. 参数与阈值

所有可调参数集中在 `core/tuning.py`（229 项，全部中文说明），`.env` 同名变量可覆盖。
完整表见 [tuning.md](tuning.md)。改完用：

```bash
python3 scripts/check_tuning.py --strict
python3 scripts/dump_tuning.py --write   # 重新生成 docs/tuning.md
```
