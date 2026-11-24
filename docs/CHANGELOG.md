# 更新日志

## 2026-11-24

### 修复（第十二轮：图书馆真能查 / 群聊工具放开 / 引用别挂错图）

- **图书馆一直 0 条（真 bug）**：
  - `AGENT_RECALL_TIMEOUT` 之外新增 `AGENT_LIBRARY_TIMEOUT`（默认 14s）；
    以前 `smart-query` 写死 2.5s，而 RAG 实测 5~14s → 每条消息都超时、
    然后「图书馆不可用 10 分钟」，所以永远 0 条；
  - `reply_pipeline` 把图书馆查询**提前到总结之前并行发起**，不再串行叠加；
  - 只对「像在问事/提旧事」的消息查（问号/记得/上次/谁/什么/怎么…），
    纯插科打诨不花这 5~14s；
  - 同一问题 10 分钟内复用缓存（空结果 2 分钟）；
  - `is_no_answer()` 过滤 RAG 的「没有/无法回答/问题不完整」，不再把「没有」当事实；
  - `_looks_like_chat()` 不再因为来源是 `/app/documents/*.json` 就全丢
    （以前把 `documents/xiaoming.txt` 这类归档也误杀），只排除诗词语料 + 百科式定义；
  - 新增 `/测函数 图书馆 <问题>`：直接在 QQ 里看 RAG 命中什么、耗时多少。
- **群聊里找不到翻唱工具**：`sing` / `remember` / `recall` 之前只挂私聊
  （`register_tool` 默认 `scope=("private",)`）。现在三个都挂 `("private","group")`，
  群里点歌、记承诺、查记忆都能用。
- **引用回复又挂错（第二次）**：这一轮在回「在开会」，系统却把最后那张表情包自动引上了。
  - 自动引用图/视频时，只有这个气泡正文真在说图（图/猫/这张/表情/照片/画面…）才引，
    否则先不引；
  - post_rules 明确：不同人/不同事分气泡（`|||`），每个气泡 `#消息号` 引自己回的那条，
    别把两件事塞进一个引用里。
- **发行仓补齐**：`docs/agent-pipeline.md`、`scripts/pixiv_get_*.py`、
  `plugins/third/**/patches/*.patch` 不再被误排除；`roll.py` 与服务器版本对齐。

### 补充（第十一轮补：流水线按人查看 / 档案白月光占位）

- `/看模板 流水线` 支持可选 `@人` / `QQ号`：
  - 不带人：打印上一次真实回复的流水线；
  - 带人：用这个人在当前群的最近一句话**现跑一遍** `reply_pipeline.build`，
    排查“这个人的档案为什么不对”。
- `/看模板 流水线` 顶部增加「这次是回：谁（QQ）｜会话」+「要回的原话」，
  避免把“上一条群消息的取数”误当成 `/档案`。
- 档案卡「白月光」块不再直接消失：没统计出来也显示
  「还没统计出来（他 @/引用谁最多就会记）」；reply_pipeline 档案块同样保留占位。
- `mood_judge.summarize` 遇到 `[图片]/[表情]/[视频]` 时总结「他在接哪件事/群里在聊什么」，
  不再输出「内容未显示完整」这类元评论。
- **图片/视频不再走 pipeline 的「总结」步**：`reply_pipeline` 检测到
  `[图片识别结果]/[视频解析结果]/[图片]/[表情]/[视频]` 时直接跳过总结，
  检索词改用「某人 图片」——避免把视觉报告重新压回 prompt 让她先描述图。
  视觉分支自己的「不要复述画面」规则保留不动（`llm.py` + `vision_agent`）。
- **表情描述过滤**：`aura_logic.strip_sticker_description()` 清掉
  `（发个嘲笑表情）` / `给你一个嘲笑表情` / `嘲笑表情 |||<sticker:嘲笑>` 里的文字描述，
  只发图；正常聊「这个表情好可爱」不误伤。
- **〔已回〕误判（真 bug）**：worker 防重投的 `_REPLIED_MIDS` 在进流水线**之前**
  就登记了当前 mid，导致每条新消息都被判「已回=是」，模型只敢补一句。
  现在拆成两个集合：
  - `_REPLIED_MIDS` = 见过/处理过（只用于 DEDUP）；
  - `_DONE_MIDS` = **真的发出去过回复**（历史块和 pipeline 的〔已回〕只认它）。
  发送成功后由 `_mark_replied_done()` 写入；沉默不写。
- NapCat 看门狗：日志出现「需要验证码 / proofWaterUrl / sms-verify」时只记日志、
  **不自动重启**，等人工在 WebUI 完成验证；无密码模式不会反复作废二维码。

### 修复（第十一轮：上下文总结 / 档案联动 / 心里话 / 表情静默 / 流水线可看）

- **总结把「身高 192」理解成「192 关」**：
  - `mood_judge.summarize()` 现在带最近 10 条上下文，先判断「这句在接哪句」；
  - 硬性要求数字/单位按字面保留，不自己补成关卡/等级/分数；
  - `reply_pipeline` ④ 把 `group_scene.history_lines` 一起传进去；
  - `scene_auditor` 的滚动总结也加了同一条数字纪律。
- **档案卡和称呼/白月光不联动**：
  - `profile_card._blocks` 的称呼改读 `callname`（`user_call_of` / `bot_call_of`），
    新增「称呼」「白月光」两块；不再把历史 aliases 当称呼展示；
  - `/档案 文字` 同样读 callname + `white_moon.profile_note`；
  - `profiles.call_of()` 统一先问 callname 索引，history/审核/工具不再各叫一个名；
  - `set_call_names()` 换称呼时自动摘掉旧 call_name 的 aliases；
  - `/删称呼 QQ 阿明` 支持**点名删一个别名**（`callname.clear_name` + `profiles.remove_alias`）。
- **又说心里话**：`aura_logic.strip_inner_thought()` 清正文里的
  `（心里话：…）` / `内心：…` / `（内心 OS：…）` / `【心里话】…`；
  `THINK_PROTOCOL` 同步禁止正文出现这些字样。
- **表情标签不存在时的提示词**：`sticker_sender` 不再发
  「没有「X」这个标签的表情哦」，只在日志留痕；宁愿不发，也不发提示。
- **流水线可看**：`reply_pipeline` 产出加 `▍PIPELINE｜…` 头；
  新增 `/看模板 流水线`（只打流水线，不混人格/规矩/历史）、
  `/测函数 流水线 全`；`prompt_report` 认识该块；`PIPELINE_BUDGET` 7s → 12s。
- **关键规矩补进 post_rules**：不认错昵称、不引错话、带〔已回〕的消息不再回。
- **NapCat 掉线看门狗**：`scripts/napcat_watchdog.sh` + cron 每 2 分钟；
  容器挂了自动 start；登录失效且 `.env` 配了 `NAPCAT_QUICK_PASSWORD(_MD5)` 才 restart
  （10 分钟冷却）；没配密码只记日志，不反复作废二维码。详见 `docs/deployment.md` 3.1。

### 修复 + 精简（第九轮：称呼第三人称 / 删称呼 / 规矩块压缩）

- **称呼记错人（真 bug）**：
  - `[[CALL]]` 新增 `target`：给第三个人定称呼必须写 `target:名字/QQ`，不写默认当前说话人；
  - 模型没写 target、但正文有「叫他/喊他/管他叫」等第三人称线索 → 先按名字解析；
    解析不到或解析回说话人 → **直接跳过，绝不写回说话人**；
  - 新增 `/删称呼 [@人]`：两个方向 + `aliases` + 名册缓存一起清，解决「删不掉」；
  - `set_call` 工具支持 `who=both`、`name=删`（留空也当删除）；
  - `profiles.clear_call_names()` 会移除旧 call_name 的 aliases，避免名册又把错名字找回来。
- **固定区规矩大砍（-83%）**：`_behaviour_protocol` 8398 → 1429 字，
  `_post_rules` 1010 → 202 字。
  - **整节删除**：02b 沟通、02c 接不上、02d 注意力、07 关注谁；
    分别交给 post_rules / 历史块 / 硬逻辑 / `focus_sessions`。
  - 保留的只有**协议**：工具目录、长度一句、接话一句、查询一句、`[[CALL]]`、
    `[[MEMO]]`、`[[THINK]]`、`[[MOOD]]`、`<sticker>`、底线一句、人称、NapCat。
  - 工具菜单改动态分类 + 先 `[[TOOLS|分类]]` / `搜:词`；NapCat 参数按需 `napcat_help`。
  - 哈希/网页提交从 post_rules 移回工具 description，不再每轮重复。
- 被删掉的人设卡规则仍保留在系统协议里（括号/称呼/被纠正/红线），selftest 已同步。

### 新增（第八轮：说话语料库 / 自定义风格上传 / 人设卡再瘦身）

- **说话语料库 `services/style_vault.py`**：每个人的原话按群存进
  `data/style_corpus/<gid>/<qq>.jsonl`（默认每人最近 200 条）。
  **省流模式也会记**（只写文本、不调模型），学风格不再依赖短期窗口。
- **白月光风格改成「摘要 + 原话」**：`render_style_only()` 除了风格摘要，
  再带 2~3 句这个人的原话，模型才能真正学到句长/标点/口头禅。
- **自定义风格上传 → 主人特批**：
  - `/白月光样本 @某人`：本人上传自己的说话样本（一段话 / 多行 Q&A / 引用消息）；
  - `/白月光特批 @某人`：仅主人，通过后才进 prompt；
  - `/白月光驳回 @某人`、`/白月光特批 列表`、`/白月光样本 查看`。
  存储 `data/style_vault.json`，只有 `approved` 的会生效。
- **人设卡再去掉「底线」和「说话案例」**：只剩「你是谁（含外貌）/ 性格」；
  红线（不碰政治/敏感、不越界、不装懂）移进固定区 `▍09 别学坏`。
- 调参新增：`STYLE_VAULT_ENABLED / STYLE_VAULT_MAX_SAMPLES /
  STYLE_VAULT_SAMPLE_CHARS / STYLE_VAULT_MAX_UPLOAD_CHARS`。

### 修复（第八轮补：人设卡瘦身后的回归）

- **人设卡删掉的规则没有丢**：
  - 「不要用括号」在 `THINK_PROTOCOL`；
  - 「别人纠正你先重新理解、被纠正不丢人」+「问的是你对 X 的称呼，不是你的名字」
    移进 `COMM_PROTOCOL`（七条 → 九条）。
  `selftest_all` 的 4 条旧断言改成检查 `services/llm.py`，不再要求人设卡里必须有。
- **新命令进菜单**：`/白月光样本`、`/白月光特批`、`/白月光驳回`
  补进 `plugins/help_menu` 的称呼区块和 `/帮助` 全量清单。
- **`audit_pipeline [8]` 过时断言**：`MOOD_FROM_JUDGE=true` 时她自报 emotion
  只写心情原话、不动数值；测试改成断言 `label`，不再误报「心情没变化」。

### 调整（第七轮：流水线可核对 / 白月光进档案 / 人设卡瘦身）

- **流水线每块加 ≤30 字小标题**：`▍状态｜心情值 · 当前状态 · 那件事的心理`、
  `▍档案｜称呼 · 身份 · 好感 · 印象最深的事` 等，`/测函数 模板` 一眼能对上顺序。
  完整对照表见 [reply-pipeline.md](reply-pipeline.md)。
- **每一步都留状态**：`stages` 记 `✓ / 数字 / 0 / — / ×异常名`，
  `/测函数 流水线` 直接显示上一次哪一步没拿到；失败不再静默消失。
- **心情块改用 `mood.status_block`**：一次给全 心情词+数值 / 刚才那下 / 心里话 /
  峰值事件+当时心理 / 群聊总结 / 对他的好感走势；拿不到时兜底成「心情值 ±N」一行，
  **保证「此刻状态」永远在**。
- **白月光写进档案块**：`white_moon.profile_note` 现在同时给「是谁 + 分数 + 风格摘要」；
  回复前的风格块改用 `render_style_only`，只给「怎么借」，不再重复「是谁」。
- **人设卡瘦身**：`小鲸鱼proplus.txt` 只保留「你是谁（含外貌）/ 性格」，
  原卡备份为 `小鲸鱼proplus.txt.bak`（不会被 `*.txt` 扫描）。
  长度/分条/记忆/表情/心里话/心情/人称/工具目录全部交给固定区协议。

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
