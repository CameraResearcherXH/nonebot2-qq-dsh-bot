# 更新日志

## 2026-11-24

### 重构（第六轮：称呼唯一入口 / 回复前动态取数流水线）

- **称呼统一入口 `services/callname.py`**：`他叫她` / `她叫他` 两个方向各一个字段，
  群级索引（`data/callname.json`）+ 档案双写，永不再互相覆盖。
  - `/叫你 X` = 他叫她 X（喊这个名字 = 在叫她，**群内硬匹配**）；
  - `/叫我 Y` = 她叫他 Y；`/叫他 @某人 Z` = 她叫他 Z；
  - `[[CALL]]` 会做方向纠错（模型把两个方向写反时自动换回来）；
  - `profile_update` 里带「叫我/叫你/称呼」的内容不再写进「注意」，自动救回 callname；
  - 新工具 `set_call(who=user|bot, name)`，方向显式，不靠模型分你我。
- **回复前动态取数流水线 `services/reply_pipeline.py`**：
  固定上下文只留 人设卡 + 工具 + 标签处理过的上下文 + 称呼；
  拿到要回的那句之后按顺序取：心情值/当前状态 → 对方档案+好感+印象最深的一件事 →
  判读模型对这句话的总结 → 用总结查记忆 → 用总结查图书馆（标注人称不是现在的你我他）→
  这条消息是否已回过 → 白月光风格 → 综合回复。
  每一步独立降级、总预算 `PIPELINE_BUDGET=7s`（记忆与图书馆并行），
  `/测函数 流水线` 看每步耗时、`/测函数 称呼` 看称呼索引。
- 有流水线时，主 prompt 不再重复注入 心情/好感/记忆目录/图书馆/白月光（省 token、避免说两遍）。
- 白月光/表达学习在学风格前先洗掉协议残留（`〔引`、`引用`、`<reply:>`、`⭐`），
  之前把「引用」当成了某人的口头禅。

### 修复（第五轮：识图原图 / 图书馆路由 / 调用可见）

- **识图还是糊**：QQ 消息段里的 `url` 常是预览图。现在先问 NapCat `get_image`
  拿原图地址和本地缓存，再把所有候选都下一遍**取字节数最大**的那份；
  如果消息段自报 `file_size` 比拿到的大 15% 以上，日志直接警告「可能还是压缩图」。
- **图书馆（算力机 RAG）根本没在工作**：实测 8001 是 Agentic RAG v3，
  路由是 `/api/agent/query`、`/api/agent/smart-query`，**没有** `/api/agent/recall`、
  `/api/agent/reflection` —— 我们一直在打 404，所以图书馆永远空。
  现在按「结构化 → smart-query → query」三级降级，404 只试一次；
  发现库里语料不是聊天记录（实测是 `poet.song.rank.*` 宋词）→ 自动暂停 6 小时并告警。
- **回忆超时**：`AGENT_RECALL_TIMEOUT` 6 → 3 秒（它在回复关键路径上）。
- **兜底命中不算「本地搜到」**：`recent_about` 的兜底结果不再阻止图书馆查询。
- **调用可见**：新增 `/测函数 记忆` —— 记忆库条数、召回次数/命中率/兜底次数、
  图书馆调用/成功/暂停状态。
- **新增 `scripts/library_sync.py`**：把 `data/dialogue/*.jsonl` 转纯文本上传到 RAG
  并建索引，图书馆要真的能回忆旧聊天，得先跑它。

### 修复（第四轮：记忆回想 / 艾特插队 / 唱歌重复）

- **记忆回想变少**：召回用的 query 从「整段带行号/⭐/名字前缀的文本」改成
  「最近 3 句正文」（`_recall_query`），关键词没命中时再补「关于这个人的最近几条」
  （`memory_v2.recent_about`）；上下文里已回过的消息打 `〔已回〕`；
  规矩新增「提到上次/之前/相似的事 → 先 [[MEMORY|关键词]] 翻一遍」。
- **艾特插队**：分段 buffer 里出现 @ 她 / 引用她 → `_URGENT` 标记，worker 只留 0.35s
  合并窗口就处理，并且跳过 `REPLY_INTERVAL`（以前要排队等安静窗口）。
- **一次点三首歌**：`sing` 工具加在途/近期去重（同会话上一首没唱完、同一首歌 15 分钟内
  → 不重复点），对应真机「让她随便唱首歌，结果发了三次歌」。
- **协议残留**：`〔引〕` / 半个 `〔` 一律清掉（上一轮已修 `〔引用…〕`）。

### 体检

- 新增 `docs/review-2026-11-24.md`（内部，不进发行仓）：DSH 桥 vs Aurabot 能力对照、
  重复调用清单、死代码、两处设计注意点。

### 新增（第三轮：模型自由搭配 / 省流模式 / 引用修正）

- **模型自由搭配**（`services/providers.py`）：主聊天 / 心情判读 / 收发件人审核 / 识图 /
  风格学习 / 对话状态 / 档案挖掘，每个角色单独配 provider + 模型 + base_url + key，
  支持 deepseek / glm / qwen / xai / openai / 自己的中转站；`/测函数 模型` 查看。
  详见 [model-providers.md](model-providers.md)。
- **群聊「全局 / 省流」模式**（`GROUP_WATCH_MODE`）：`lite` 下消息照常记录
  （现场 / 白月光 / 心情都不受影响），只有 @她 / 引用她 / 喊名字 / 收件人是她 才入队回复；
  不主动插话、不做表达学习；私聊不受影响。
- **引用精确到消息号**：`<reply:#消息号>`；发送前校验真实 mid，行号自动映射，非法就不引用，
  修「引用回复回错人（回成上面/下面那条）」。
- **同一件事不回两遍**：`GROUP_REPLY_COOLDOWN`（默认 45s），刚回过这个群时非点名消息只进上下文。
- **协议残留清理**：`〔引用…〕` / `〔引〕` / 半个 `〔` 不再出现在气泡里。

### 调整（第二轮：响应速度 / 识图 / 心情）

- **回复更快**：群聊分段等待 6s → 3.5s；@ 她 / 喊名字时只等 1.2s（`SEGMENT_WAIT_FAST`）；
  同一会话回复间隔 `.env` `REPLY_INTERVAL` 20 → 6；多气泡停顿上限 4.5s → 1.6s。
  新增 `[LAT]` 日志（段等待 / 审核 / 模型 / 发送 / 总计），方便继续定位。
- **识图更清楚**：默认改为「自己下载 → 压到 1568 长边（quality 90）→ base64 传」，
  不再把 QQ 链接丢给模型；`VISION_PROVIDER=glm` 走智谱，默认 `glm-4v-plus`
  （实测 3~4s；`glm-5.3-flash` 能读图但「始终思考」要 23s）。
  Qwen 兜底候选改成实测可用的 `qwen3-vl-plus` 等（`Qwen3.5-Flash` 会 404 / 30~50s）。
- **心情真的参与决策**：关注期新增 `MOOD_FOLLOW_MIN=-35` —— 心情低于它就不再追着接话，
  只回明确 @ / 喊名字的（以前关注期完全不看心情）。

### 新增

- **算力机开关 `NODE_COMPUTE`**
  - `1`（默认）= 走 `node.py`：画图 / 翻唱 / 语音 / 视频理解排任务给算力机；
  - `0` = 不加载本项目 `draw` / `song_cover`，不注册 `draw_image` / `draw_from_pixiv` /
    `pixiv_search` / `sing` / `video_understand`，画图/唱歌交给 `plugins/third` 的本机插件；
  - 工具目录会切成「本机模式」，提示模型先 `[[TOOLS|搜:画图]]` 找三方工具。
- **DeepSeek Harness 改代码桥 `CODE_AGENT=harness`**
  - QQ（主人）`/h <需求>` 或 `/改代码 <需求>` → 本机 DSH → 结果原样回 QQ；
  - 支持 `HARNESS_BACKEND=cli|http`，`{prompt}/{cwd}/{session}` 占位；
  - 带上人格/心情/好感/白月光/相关记忆（`HARNESS_CONTEXT=1`）；
  - 与 `self_tool` 互斥：harness 模式不加载 `plugins/self_tools`，也不注册 `self_tool_*`/`mytool_*`；
  - 只有主人能用，输出脱敏、超时 kill、分段发送。

### 修复

- **其他人格卡仍出现「鲸鱼尾巴 / 呆毛」**：prompt 里写死的人格专属词已清掉：
  - `[[MEMO]]` 示例的「尾巴都摇起来了」→ 通用描述；
  - `[[CALL]]` 示例的「小鲸鱼」→ 通用名字；
  - 人称规则、每日总结、表情管理员、档案卡页脚、`_bot_name` 兜底、切人格文案等；
  - 「被讨厌就闭嘴」的强信号不再写死「鱼/鲸鱼」，改用「机器人/bot/AI + 当前人格名」。
- **`/测函数 prompt` 换人格卡后认不出角色卡**：改为「第一条 system 消息 = 角色卡」，
  不再靠 `DeepSeek 小鲸鱼` 前缀匹配（自定义人设卡也能正确统计）。
- **发行仓清理**：移除误入的 34MB `scripts/.xray_runtime/xray.exe`；
  `.env.example` 里私人基础设施地址（阿里云专属 workspace、自建站点）改为公开默认值；
  `services/tts.py` 的兜底 host 改为公共域名。

### 文档

- 新增 `docs/harness-bridge.md`（DSH 桥：配置、后端、安全、与 Aurabot 的区别）；
- `docs/deployment.md` 增加 `NODE_COMPUTE` / `CODE_AGENT` 两节；
- `docs/features.md` 增加第 17/18 节。

## 2026-11-23

### 新增

- **白月光模式**（`services/white_moon.py`）
  - 统计**群友之间**「谁最常 @ / 引用 / 接谁的话」，两层：每个人的白月光 + 群白月光；
  - 她回某个人时借那个人的句式/口头禅，回大家时用群白月光；
  - `/测函数 白月光 [群号]` 查看；详见 [whitemoon.md](whitemoon.md)。
- **详细用量与 prompt 体检**
  - `/用量` 按用途拆分 token、缓存命中率、Top/Bottom，并出横向柱状 + 扇形图；
  - `/测函数 prompt` 给出角色卡/规矩/历史/心情/好感等分段占比、块内位置、缓存报告；
  - 图表从已下线的 Google Image Charts 切到 `image-charts.com`（可用 `USAGE_CHART_BASE` 自建）；
  - 详见 [usage-report.md](usage-report.md)。
- **以图搜源插件**（`plugins/third/picsearch` + `vendor/PicImageSearch`）
  - 聚合 TraceMoe / SauceNAO / Ascii2D / IQDB；
  - 通过 `plugin.toml` 暴露 `picsearch` function 工具（`hold=true`）；
  - 详见 [third-party-integration.md](third-party-integration.md)。
- **人格 ID 与登录 QQ 解耦**（`services/identity.py`）
  - `SELF_ID` 固定人格，`NAPCAT_ACCOUNT` 是当前登录号，`SELF_ALIASES` 是历史号；
  - 换号后旧号说过的话、@ 旧号的记录仍算「她」；记忆/好感/白月光不随 QQ 走。
- **戳一戳次数屏蔽**（`services/poke_guard.py`）
  - 同一个人 2 小时内戳超过 3 次 → 第 4 次起不回、也不戳回，只记上下文；
  - 滑动窗口，2 小时后自动恢复；阈值可调（`POKE_MAX_PER_WINDOW` / `POKE_WINDOW_HOURS`）。
- **部署文档 / 三方插件接入文档 / 记忆档案设计文档**
  - [deployment.md](deployment.md)、[third-party-integration.md](third-party-integration.md)、
    [memory-profile-design.md](memory-profile-design.md)。

### 优化

- **角色卡 + 规矩去重**：22,751 → 14,586 字（-35.9%），角色卡单项 12,378 → 6,343（-48.8%）；
  - 删掉与规矩块逐条重复的「说话铁律」17 条、「沟通策略」整节、「人格重申」重复段；
  - 合并「说话方式 / 节奏」两节；工具目录保留分类，只删重复的用法说明；
  - 规矩块合并「该不该接话」「接不上怎么办」里的重复段落。
- **收发件人贯通**：分段、判读、主模型上下文统一用 `group_scene.history_lines`
  （带「谁 → 谁」）；被截断时**保留所有 @ 她的行**，并列出「必须回应」清单。
- **多条 @ 一条答完**：一段里多人 @ 时，要求用一条消息全部答到，最多 2 条气泡，
  不再「只挑最后一条」。
- **关注期不乱接**：收件人明确是别人（`to_kind=person`、置信度 ≥0.5）→ 不回；
  判读模型「不确定 / 失败」→ 不回。
- **旁白剥离**：`strip_meta_talk()` 丢掉「串线」「不是跟我说的」「我插不上话」这类旁白；
  全丢=沉默。
- **模型分工**：角色扮演 `deepseek-flash`；心情判读/收发件人 `deepseek-chat`；
  GLM 只做视觉。
- **长回复不截断**：带 @ / 显式 `<reply:>` 的回复整段发完，不再按字数硬切。

### 修复

- **好感从未进 prompt**：`build_packet` 产出了 `relation`，但注入清单漏了 —— 补上并前移。
- **群聊分段永不执行**：`if not _seg_records` 缩进错位，群聊没有分段上下文 —— 修缩进。
- **重启后现场只剩最后一句**：现场只在内存，重启即空 —— `seed_from_log()` 按条数补回。
- **白月光写丢数据**：`_group()` 的 `setdefault` 写法会覆盖已有统计 —— 改为正确合并。
- **旧号被当群友**：身份判断用 `event.self_id`，换号后旧号变成陌生人 —— 改用别名表。
- **@ 了却不回**：prompt 里「只接一个线程 / 默认回最后一条」+ 截断丢掉 @ —— 已重写。
- **识别结果被念出来**：视觉标记/画面描述漏进正文 —— 提示词 + `strip_media_echo` 双保险。
- **NapCat 掉线收不到消息**：`.env` 缺 `NAPCAT_ACCOUNT` + 新号没有反向 WS 配置 —— 已补。
- **`/测函数 用量` 报 FinishedException**：`finish()` 放在 `try/except` 里被二次捕获 —— 已移出。
- **`/用量` 图表 404**：Google Image Charts 下线 —— 切 `image-charts.com`。

### 三方插件

- 新增 `picsearch`（以图搜源）；
- `girls_2d`：去硬编码路径、修 `UnboundLocalError`、同步改异步、uuid 临时文件；
- `petpet`：pydantic schema、异常兜底、大文件超时不重发、emoji 昵称、临时文件 `file://`；
- `pixivrank_search`：图片压缩、超时处理、串行闸门、随机抽取、尺寸前缀、`/搜图`；
- `steam_judger`：出图链路自检、可选 Chrome、`retcode=1200`、`task_done` 修正；
- `gspanel`：独立 `/绑定`、`KeyError: 'Costumes'`、图片发送、注册成工具；
- `rollpig_plus`：`CommandArg()` None 兼容、LLM 直调、身份别名感知；
- `bittorrents`：跟随重定向、隐私、卡片套层；
- `jinriyunshi`：共享每日重置、统一 `/` 前缀；
- 完整清单见 [third-party-integration.md](third-party-integration.md#4-我们对上游插件改了什么)。

### 部署 / 兼容

- 新增 `docs/deployment.md`：Docker + NapCat + `.env` + 验证 + 排错；
- `.env` 新增 `SELF_ID` / `SELF_ALIASES` / `POKE_MAX_PER_WINDOW` / `POKE_WINDOW_HOURS`；
- 改 `.env` 后必须 `docker compose up -d --force-recreate bot`（`restart` 不重读 env_file）；
- 代码闸门：`audit_features` 43 项、`test_new_features` 135 项、`selftest_all` 1781 项、
  `verify_all` 9 道、`verify_deploy` 94 个文件。
