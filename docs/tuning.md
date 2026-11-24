# 调参手册（tuning）

> 本文件由 `python3 scripts/dump_tuning.py --write` 生成，**不要手改**。
> 所有默认值来自 `core/tuning.py` 的 `DEFAULTS`。

## 怎么改

两种方式，二选一：

1. **环境变量（推荐）**：在 `.env` 里写同名键，例如 `STICKER_COLLECT_MIN=0.3`，重启生效。
2. **改默认值**：直接改 `core/tuning.py` 的 `DEFAULTS`（适合做发行版预设）。

查看当前实际生效值（含 .env 覆盖）：`/调参` 或 `python3 scripts/dump_tuning.py`。

单位约定：

- 时间一律**秒**，除非键名带 `_H`（小时）/`_DAYS`（天）。
- 概率/权重一律 **0~1**。
- 心情内部口径 **-100~100**；判读模型给的是 -1.00~1.00，代码里会 ×100。

---

## 参数总览

| 分组 | 项数 |
|---|---|
| 回复 / 限流 / 主动插话 | 44 |
| 心情 | 34 |
| 档案 / 好感 | 12 |
| 记忆 | 16 |
| 表情 | 7 |
| 主动私聊 | 9 |
| 安静 / 破冰 | 6 |
| 提示词 | 3 |
| 模型调用 | 18 |
| 用量监测 | 4 |
| 回复审核 | 14 |
| 表达 / 语气 | 7 |
| 视觉 | 19 |
| 画图 | 2 |
| 语音 / 音乐 | 27 |
| 世界书 | 4 |
| 安全 / 工具 | 10 |
| 其他 | 15 |
| **合计** | **251** |

## 回复 / 限流 / 主动插话

| 参数 | 默认 | 说明 | 生效位置 |
|---|---|---|---|
| `BURST_MSG_COUNT` | `4` | 窗口内多少条算刷屏 | `plugins/chat/__init__.py` |
| `BURST_WINDOW_SEC` | `30` | 高刷检测窗口（秒） | `plugins/chat/__init__.py` |
| `CTX_LONG_MSG_CHARS` | `150` | 多少字算长消息（上下文补齐用） | `plugins/chat/__init__.py` |
| `CTX_LONG_MSG_CHARS_MAX` | `800` | 单条长消息最多给多少字 | `plugins/chat/__init__.py` |
| `CTX_LONG_MSG_KEEP` | `3` | 长消息最多补几条 | `plugins/chat/__init__.py` |
| `DEBOUNCE_AT` | `0.8` | 被 @ / 喊名字时的等待（秒），越短越像立刻回应 | `plugins/chat/__init__.py` |
| `DEBOUNCE_BARE_IMAGE` | `4` | 只发一张图、没有文字时的等待（等图下载） | `plugins/chat/__init__.py` |
| `DEBOUNCE_MAX` | `8` | 一批消息最多等多久（秒），防止一直等 | `plugins/chat/__init__.py` |
| `DEBOUNCE_OTHER` | `0.5` | 换人说话时的等待（秒） | `plugins/chat/__init__.py` |
| `DEBOUNCE_SAME` | `1.2` | 同一个人连续发消息时的等待（秒） | `plugins/chat/__init__.py` |
| `DIGEST_AFTER` | `8` | 群里累积多少条后做一次小结 | `plugins/chat/__init__.py` |
| `FOCUS_FOLLOW_COUNT` | `3` | 被叫住后额外跟几句 | `plugins/chat/__init__.py` |
| `FOCUS_JUDGE_TIMEOUT` | `3` | 关注判定超时（秒） | `plugins/chat/__init__.py` |
| `FOCUS_NEG_STREAK_STOP` | `2` | 连续几次负面就停止关注 | `plugins/chat/__init__.py` |
| `FOCUS_TIMEOUT` | `120` | 关注会话的有效期（秒） | `plugins/chat/__init__.py` |
| `GROUP_MAX_HANZI` | `20` | 群聊单条回复的字数上限（按显示宽度算：汉字 1 / 半角 0.5） | `plugins/chat/__init__.py` |
| `GROUP_MUTE_SEC` | `900` | 被群友嫌弃后静默多少秒 | `plugins/chat/__init__.py` |
| `GROUP_SEND_LIMIT` | `9` | 群发配额：每窗口最多几条 | `plugins/chat/__init__.py` |
| `GROUP_SEND_MAX_WAIT` | `15` | 配额不足时最多等多久（秒） | `plugins/chat/__init__.py` |
| `LONG_FORWARD_MIN` | `120` | 超过多少字改用合并转发 | `plugins/chat/__init__.py` |
| `MAX_GROUP_RECENT` | `16` | 群聊上下文保留多少条消息（16 = 修过长消息被挤出窗口的问题） | `plugins/chat/__init__.py` |
| `MEMORY_EMOTION_MIN_DELTA` | `0.1` | 记忆刻痕门槛：|Δ| 达到多少才算（0.10 = 设计里的 💥 档） | `plugins/chat/__init__.py` |
| `MERGE_WINDOW` | `0.6` | 合并转发判定窗口（秒） | `plugins/chat/__init__.py` |
| `MOOD_FOLLOW_MIN` | `-35` | 心情低于这个值就不再追着接话（只回明确叫她） | `plugins/chat/__init__.py` |
| `MOOD_WATCH_SEC` | `60` | 被动心情观察的最小间隔（秒） | `plugins/chat/__init__.py` |
| `PEER_REPLY_COOLDOWN` | `180` | 对鱼接话的冷却（秒） | `plugins/chat/__init__.py` |
| `PEER_REPLY_PROB` | `0.15` | 对鱼的消息接话概率（0~1） | `plugins/chat/__init__.py` |
| `PRIVATE_MAX_HANZI` | `20` | 私聊单条回复的字数上限（同上） | `plugins/chat/__init__.py` |
| `QUOTE_MAX_CHARS` | `500` | 引用原文最多带多少字 | `plugins/chat/__init__.py` |
| `RANDOM_CHAT_INTERVAL` | `180` | 主动插话的巡检间隔（秒） | `plugins/chat/__init__.py` |
| `RANDOM_CHAT_MIN_MSGS` | `3` | 群里至少累积多少条消息才考虑插话 | `plugins/chat/__init__.py` |
| `RANDOM_CHAT_PROB` | `0.35` | 满足条件后实际插话的概率（0~1） | `plugins/chat/__init__.py` |
| `RANDOM_CHAT_VOICE_RATE` | `0.3` | 插话时发语音的概率（0~1） | `plugins/chat/__init__.py` |
| `REPLY_DEDUP_RATIO` | `0.72` | 判定为重复的相似度阈值（0~1） | `plugins/chat/__init__.py` |
| `REPLY_DEDUP_SEC` | `60` | 重复回复去重窗口（秒） | `plugins/chat/__init__.py` |
| `REPLY_INTERVAL` | `10` | 同一会话两条回复之间最少间隔秒数（限流） | `plugins/chat/__init__.py` |
| `SEGMENT_WAIT_FAST` | `1.2` | 群聊分段：@ 她 / 叫名字时只等多久（默认 1.2，别让她慢半拍） | `plugins/chat/__init__.py` |
| `SHORT_QUERY_WINDOW_SEC` | `120` | 短查询上下文窗口（秒） | `plugins/chat/__init__.py` |
| `SLEEP_HOUR_END` | `8` | 群聊免打扰结束小时（0-23） | `plugins/chat/__init__.py` |
| `SLEEP_HOUR_START` | `0` | 群聊免打扰开始小时（0-23） | `plugins/chat/__init__.py` |
| `TTS_PICKUP_TIMEOUT` | `40` | 等算力机接单的超时（秒） | `plugins/chat/__init__.py` |
| `TTS_WAIT_TOTAL` | `240` | 等语音合成结果的总超时（秒） | `plugins/chat/__init__.py` |
| `UPLOAD_MAX_MB` | `200` | 上传文件大小上限（MB） | `plugins/chat/__init__.py` |
| `UPLOAD_WAIT_SEC` | `180` | 等待文件上传的超时（秒） | `plugins/chat/__init__.py` |

## 心情

| 参数 | 默认 | 说明 | 生效位置 |
|---|---|---|---|
| `AFFINITY_GAIN` | `0.05` | 心情变化 Δ 转好感的系数（0.05 ≈ 一次大事件 +0.05） | `services/mood_judge.py` |
| `GROUP_MOOD_CHAT_MAX` | `1.35` | 群心情最好时「愿不愿意多说话」的乘数上限（1.35） | `services/mood.py` |
| `GROUP_MOOD_CHAT_MIN` | `0.55` | 群心情最差时「愿不愿意多说话」的乘数下限（0.55） | `services/mood.py` |
| `GROUP_MOOD_GROUP_DROP` | `0.7` | 整个群净掉多少 → 判定群聊不愉快（0.7） | `services/group_mood.py` |
| `GROUP_MOOD_LIKE_MIN` | `0.05` | 净 Δ 达到多少算喜欢/讨厌（0.05，算人数和比例用） | `services/group_mood.py` |
| `GROUP_MOOD_MAX_DELTA` | `0.2` | 单条群心情事件上限（0.20，防小事刷满） | `services/group_mood.py` |
| `GROUP_MOOD_MAX_EVENTS` | `400` | 每个群最多保留多少条心情事件 | `services/group_mood.py` |
| `GROUP_MOOD_MIN_DELTA` | `0.03` | 进群心情的最小 |Δ|（0.03，防小事噪声） | `services/group_mood.py` |
| `GROUP_MOOD_MIN_PEOPLE` | `3` | 至少几个不同的人让心情掉，才算「群聊导致」（3） | `services/group_mood.py` |
| `GROUP_MOOD_PERSON_DROP` | `0.4` | 某个人在窗口内净让心情掉多少 → 单独不理他（0.4） | `services/group_mood.py` |
| `GROUP_MOOD_WINDOW_SEC` | `18000` | 群心情窗口（秒，默认 18000 = 5 小时；超时事件自动过期） | `services/group_mood.py` |
| `MOOD_ENV_MAX_DELTA` | `0.05` | 不是冲她来（别人之间/群气氛）时单次 Δ 上限（0.05） | `services/mood_judge.py` |
| `MOOD_GLOBAL_SHARE` | `0.35` | 全局心情占会话心情的权重（0~1） | `services/mood.py` |
| `MOOD_HALF_LIFE_HOURS` | `12` | 心情回到 0 的半衰期（小时，设计默认 12） | `services/mood.py` |
| `MOOD_IDLE_AFTER` | `300` | 空闲多少秒后心情开始衰减（默认 300 = 5 分钟；之后每 5 分钟向 0 走一步） | `services/mood.py` |
| `MOOD_IDLE_STEP` | `1` | 每步走多少点（内部 -100~100 口径，1.0 ≈ 外口径 0.01） | `services/mood.py` |
| `MOOD_IDLE_TICK` | `300` | 衰减步长（秒）：每这么久向 0 走一步（默认 300 = 5 分钟走 0.01） | `services/mood.py` |
| `MOOD_JUDGE_CTX` | `8` | 判读时带多少条上下文（8 = 5~8 条，和主模型上下文同源同条数） | `services/mood_judge.py` |
| `MOOD_JUDGE_FRESH_SEC` | `600` | 判读结果有效期（秒），过期不当「现在」 | `services/mood.py` |
| `MOOD_JUDGE_MIN_INTERVAL` | `60` | 两次判读的最小间隔（秒），防刷屏时来回震荡 | `services/mood_judge.py` |
| `MOOD_JUDGE_REFRESH` | `300` | 她没主动写心情时，多久用群聊重新判一次（秒）。0 = 只在她说的时候判 | `services/mood_judge.py` |
| `MOOD_JUDGE_SMOOTH` | `0.35` | 判读值平滑系数（0.35 = 每次只走 35% 的差，给心情惯性） | `services/mood.py` |
| `MOOD_JUDGE_TIMEOUT` | `12` | 判读调用超时（秒） | `services/mood_judge.py` |
| `MOOD_LABEL_MAX` | `60` | 心情词最多保留多少个 | `services/mood.py` |
| `MOOD_LABEL_STEP` | `0.2` | 心情名词的分档步长（0.2 = 每 0.2 一个名词） | `services/mood.py` |
| `MOOD_LABEL_TTL_HOURS` | `12` | 心情词的有效期（小时），过期不再当「现在」 | `services/mood.py` |
| `MOOD_MAX_DELTA` | `0.5` | 单次「涨幅」上限（设计 0.50，只是护栏；防一边倒靠噪音丢弃） | `services/mood.py` |
| `MOOD_MAX_SESSIONS` | `50` | 最多保留多少个会话的心情（防字典无限长） | `services/mood.py` |
| `MOOD_MAX_STEP` | `0.3` | 单次判读最多改变多少心情（外口径，0.30 = 30 点），防一句话翻脸 | `services/mood.py` |
| `MOOD_NOISE_DELTA` | `0.03` | Δ 噪音门槛：|Δ| 小于它视为 0（0.03，不动数值/好感/刻痕） | `services/mood_judge.py` |
| `MOOD_PASSIVE_MAX_DELTA` | `0.03` | 她没说话（被动观察）时单次 Δ 上限（0.03，旁观不背锅） | `services/mood_judge.py` |
| `MOOD_PEAK_MIN_DELTA` | `0.03` | 进入「峰值事件」候选的最小 |Δ|（0.03，防小事顶掉大事） | `services/mood.py` |
| `MOOD_PEAK_WINDOW_SEC` | `7200` | 心情峰值事件的保质期（秒，默认 7200 = 2 小时；每件事最多挂 2 小时） | `services/mood.py` |
| `MOOD_THINK_KEEP` | `30` | 判读心理活动保留多少条 | `services/mood_judge.py` |

## 档案 / 好感

| 参数 | 默认 | 说明 | 生效位置 |
|---|---|---|---|
| `AFFINITY_DAILY_CAP` | `0.15` | 同一个人每天好感净变化上限（0.15，防「持续跌」一天到底） | `services/profiles.py` |
| `AFFINITY_TREND_WINDOW_SEC` | `1800` | 好感走势的统计窗口（秒） | `services/profiles.py` |
| `AVATAR_PX` | `160` | 头像生成尺寸（像素） | `services/profile_card.py` |
| `AVATAR_TTL_DAYS` | `7` | 头像缓存保留天数 | `services/profile_card.py` |
| `BOND_EVENT_MIN_DELTA` | `0.15` | 「印象最深的一件事」的门槛：记忆 |Δ| 达到多少才算（0.15） | `services/profiles.py` |
| `BOND_TTL_DAYS` | `60` | 相处印象（档案）保留天数 | `services/profiles.py` |
| `PROFILE_CARD_EVENTS` | `10` | 人物卡里最多列几条事件 | `services/profiles.py` |
| `PROFILE_MINE_INTERVAL_H` | `20` | 同一个人多久挖一次 | `services/profile_miner.py` |
| `PROFILE_MINE_MIN_MSGS` | `25` | 攒多少条发言挖一次 | `services/profile_miner.py` |
| `PROFILE_MINE_SAMPLE` | `40` | 一次喂多少条他的发言 | `services/profile_miner.py` |
| `PROFILE_MINE_TIMEOUT` | `25` | 档案挖掘模型超时（秒） | `services/profile_miner.py` |
| `SCENE_CARD_LINES` | `20` | 群场景卡片最多几行 | `services/profile_card.py` |

## 记忆

| 参数 | 默认 | 说明 | 生效位置 |
|---|---|---|---|
| `ARCHIVE_HOUR` | `3` | 每日归档执行的小时（0-23） | `plugins/memory_migration/__init__.py` |
| `ARCHIVE_MINUTE` | `0` | 每日归档执行的分钟（0-59） | `plugins/memory_migration/__init__.py` |
| `ARCHIVE_WINDOW_DAYS` | `30` | 归档覆盖最近多少天 | `plugins/memory_migration/__init__.py` |
| `DAILY_SUMMARY_KEEP_DAYS` | `14` | 日结文件保留天数 | `services/daily_summary.py` |
| `DAILY_SUMMARY_MIN_MSGS` | `6` | 当天至少多少条消息才写日结 | `services/daily_summary.py` |
| `DIALOGUE_IMG_SIDE` | `800` | 对话日志里图片缩略图边长（像素） | `services/dialogue_log.py` |
| `DIALOGUE_KEEP_DAYS` | `14` | 对话原文保留天数 | `services/dialogue_log.py` |
| `MEMORY_REVISIT_AGE_DAYS` | `7` | 回访记忆：至少多少天前的事 | `services/memory_v2.py` |
| `MEMORY_REVISIT_IDLE_DAYS` | `3` | 回访记忆：至少多久没提过 | `services/memory_v2.py` |
| `RECALL_TEXT_MAX` | `600` | 单条记忆注入的最大字数 | `services/memory_v2.py` |
| `REFLECT_HOUR` | `20` | 每日反思执行的小时（0-23） | `plugins/memory_migration/__init__.py` |
| `REFLECT_MINUTE` | `0` | 每日反思执行的分钟（0-59） | `plugins/memory_migration/__init__.py` |
| `REFLECT_WINDOW_DAYS` | `2` | 反思覆盖最近多少天 | `services/journal_agent.py` |
| `ROSTER_TTL` | `600` | 群成员名册缓存时间（秒） | `services/group_roster.py` |
| `SCENE_SEED_RETRY` | `30` | 群场景首次生成的补试次数 | `services/group_scene.py` |
| `SCENE_WINDOW_MSGS` | `20` | 「线程1 和我们的硬逻辑应该同时看 20 条来判断，而不是现在的五六条」） | `services/group_scene.py` |

## 表情

| 参数 | 默认 | 说明 | 生效位置 |
|---|---|---|---|
| `CURATOR_TIMEOUT` | `60` | 表情策展模型超时（秒） | `services/sticker_curator.py` |
| `STICKER_COLLECT_MIN` | `0.25` | 表情收集阈值：视觉强度绝对值低于它就不收（0.25） | `services/sticker_receiver.py` |
| `STICKER_MAX` | `1000` | 表情库容量上限（超过开始淘汰） | `services/sticker_library.py` |
| `STICKER_MAX_SIDE` | `512` | 表情图片最长边（像素），超过会缩放 | `services/sticker_importer.py` |
| `STICKER_PRUNE_BUFFER` | `20` | 淘汰时多留的缓冲条数 | `services/sticker_library.py` |
| `STICKER_TAG_SOFT_CAP` | `120` | 表情标签数量软上限（超过提示合并近义词） | `services/sticker_library.py` |
| `STICKER_WAIT_TTL` | `300` | 表情上传等待有效期（秒） | `plugins/sticker_admin/__init__.py` |

## 主动私聊

| 参数 | 默认 | 说明 | 生效位置 |
|---|---|---|---|
| `PROACTIVE_CHECK_SEC` | `600` | 主动私聊巡检间隔（秒） | `services/proactive.py` |
| `PROACTIVE_COOLDOWN_H` | `6` | 两次主动私聊的最小间隔（小时） | `services/proactive.py` |
| `PROACTIVE_IDLE_MAX_H` | `72` | 超过多久算「太久没聊」（小时） | `services/proactive.py` |
| `PROACTIVE_IDLE_MIN_H` | `2` | 至少多久没聊才考虑主动找（小时） | `services/proactive.py` |
| `PROACTIVE_MAX_PER_DAY` | `2` | 主动私聊每人每天上限 | `services/proactive.py` |
| `PROACTIVE_MIN_INTIMACY` | `0.45` | 主动私聊要求的最低亲近度 | `services/proactive.py` |
| `PROACTIVE_MIN_SCORE` | `0.9` | 候选人打分门槛 | `services/proactive.py` |
| `PROACTIVE_QUIET_END` | `9` | 主动私聊免打扰结束小时 | `services/proactive.py` |
| `PROACTIVE_QUIET_START` | `1` | 主动私聊免打扰开始小时 | `services/proactive.py` |

## 安静 / 破冰

| 参数 | 默认 | 说明 | 生效位置 |
|---|---|---|---|
| `CHAT_OFF_MAX_HOURS` | `72` | 安静最长持续小时数（超时自动解除） | `services/chat_switch.py` |
| `QUIET_ADVISOR_COOLDOWN_SEC` | `900` | 安静建议冷却（秒） | `services/quiet_advisor.py` |
| `QUIET_ADVISOR_WINDOW_SEC` | `1800` | 安静建议的观察窗口（秒） | `services/quiet_advisor.py` |
| `QUIET_GROUP_MIN` | `0.55` | 针对整个群的安静建议触发阈值（0~1） | `services/quiet_advisor.py` |
| `QUIET_PERSON_MIN` | `0.45` | 针对某个人的安静建议触发阈值（0~1） | `services/quiet_advisor.py` |
| `QUIET_THAW_GRACE_SEC` | `1800` | 破冰后的观察宽限期（秒） | `services/quiet_advisor.py` |

## 提示词

| 参数 | 默认 | 说明 | 生效位置 |
|---|---|---|---|
| `PERSONA_GROUP_CHARS` | `0` | 人格文本在群聊里最多带多少字（0 = 不限） | `services/persona.py` |
| `PERSONA_MAX_RELOADS_PER_DAY` | `2` | 改完人设不用重启容器，最多等 10 分钟就能生效。 | `services/persona.py` |
| `PROMPT_BUDGET` | `2200` | 整份提示词的字数预算 | `services/prompt_builder.py` |

## 模型调用

| 参数 | 默认 | 说明 | 生效位置 |
|---|---|---|---|
| `CHAT_HISTORY_TURNS` | `6` | 对话历史轮数 | `services/llm.py` |
| `GLM_MAX_TOKENS` | `800` | GLM 单次最大输出 token | `services/glm.py` |
| `GLM_TIMEOUT` | `30` | GLM 调用超时（秒） | `services/glm.py` |
| `HIST_LINE_BASE` | `80` | 历史里短消息给多少字 | `services/llm.py` |
| `HIST_LINE_LONG` | `500` | 历史里长消息给多少字 | `services/llm.py` |
| `HIST_LONG_AT` | `150` | 历史里超过多少字算长消息 | `services/llm.py` |
| `HIST_TOTAL_BUDGET` | `3000` | 历史总字数预算 | `services/llm.py` |
| `LIBRARY_COOLDOWN` | `600` | 同一梗/素材的冷却时间（秒） | `services/llm.py` |
| `LLM_TRACE_KEEP_DAYS` | `7` | 模型调用 trace 保留天数 | `services/llm.py` |
| `LLM_TRACE_MAX_CHARS` | `60000` | 单条 trace 最大字符数 | `services/llm.py` |
| `LOCAL_MEMORY_DAYS` | `30` | 本地记忆回溯天数 | `services/llm.py` |
| `MAX_REQ_ROUNDS` | `6` | 单次生成最多请求几轮 | `services/llm.py` |
| `MAX_REQ_ROUNDS_FAST` | `2` | 快路径最多请求几轮 | `services/llm.py` |
| `MAX_TOOL_TURNS` | `6` | 一次对话里最多几轮工具调用 | `services/llm.py` |
| `SUMMARIZE_FALLBACK_MAX` | `1200` | 压缩失败时直接把原文给 DeepSeek（超过这个字数才截断，防止 prompt 爆炸） | `services/llm.py` |
| `THINK_BUDGET_FAST` | `12` | 快路径思考预算（秒） | `services/llm.py` |
| `THINK_BUDGET_SEC` | `60` | 一次回复的总思考预算（秒） | `services/llm.py` |
| `TOOL_RESULT_MAX` | `1500` | 单个工具结果回填给模型的最大字数 | `services/llm.py` |

## 用量监测

| 参数 | 默认 | 说明 | 生效位置 |
|---|---|---|---|
| `API_USAGE_KEEP_DAYS` | `60` | 旧版用量记录保留天数 | `services/api_usage.py` |
| `API_USAGE_TIMEOUT` | `8` | 旧版用量统计超时（秒） | `services/api_usage.py` |
| `LLM_DAILY_BUDGET` | `0` | 每日 token 预算（0 = 不告警） | `services/llm_usage.py` |
| `LLM_USAGE_KEEP_DAYS` | `90` | 用量明细保留天数 | `services/llm_usage.py` |

## 回复审核

| 参数 | 默认 | 说明 | 生效位置 |
|---|---|---|---|
| `PEER_MERGE_MAX` | `25` | 鱼连发最多合并几条 | `services/referee.py` |
| `PEER_MERGE_WINDOW` | `6` | 鱼连发合并窗口（秒） | `services/referee.py` |
| `PEER_WAIT` | `90` | 等「鱼」回复的时长（秒） | `services/referee.py` |
| `PEER_WINDOW` | `240` | 鱼被 @ 后多久内回复算有效（秒） | `services/referee.py` |
| `PREFLIGHT_MIN_CHARS` | `6` | 预检：短于多少字跳过 | `services/referee.py` |
| `PREFLIGHT_TIMEOUT` | `2.5` | 预检超时（秒） | `services/referee.py` |
| `REFEREE_CTX_LINES` | `10` | 审核带几条上下文 | `services/referee.py` |
| `REFEREE_MAX_PER_HOUR` | `60` | 审核每小时最多几次（控制成本） | `services/referee.py` |
| `REFEREE_MIN_CHARS` | `12` | 审核：短于多少字的回复不核对 | `services/referee.py` |
| `SCENE_AUDIT_MAX_MSGS` | `20` | 群聊审核最多看多少条消息 | `services/scene_auditor.py` |
| `SCENE_AUDIT_MIN_INTERVAL` | `4` | 群聊审核的最小间隔（秒） | `services/scene_auditor.py` |
| `SCENE_AUDIT_TIMEOUT` | `6` | 群聊审核超时（秒） | `services/scene_auditor.py` |
| `SCENE_SUMMARY_MAX_CHARS` | `160` | 群场景摘要最大字数 | `services/scene_auditor.py` |
| `SCENE_SUMMARY_TTL` | `180` | 群场景摘要有效期（秒） | `services/scene_auditor.py` |

## 表达 / 语气

| 参数 | 默认 | 说明 | 生效位置 |
|---|---|---|---|
| `CONV_STATE_MAX_CHARS` | `420` | 对话状态摘要最大字数 | `services/conv_state.py` |
| `CONV_STATE_MIN_INTERVAL` | `150` | 对话状态更新的最小间隔（秒） | `services/conv_state.py` |
| `CONV_STATE_TIMEOUT` | `20` | 对话状态调用超时（秒） | `services/conv_state.py` |
| `EXPRESSION_EVERY` | `40` | 每多少轮更新一次语气/表达 | `services/expression.py` |
| `EXPRESSION_MAX_PER_SCOPE` | `30` | 每个会话最多保留多少条表达记录 | `services/expression.py` |
| `EXPRESSION_MIN_INTERVAL` | `600` | 表达更新的最小间隔（秒） | `services/expression.py` |
| `EXPRESSION_TIMEOUT` | `25` | 表达模型超时（秒） | `services/expression.py` |

## 视觉

| 参数 | 默认 | 说明 | 生效位置 |
|---|---|---|---|
| `IMAGE_DEDUP_HOURS` | `24` | 群友：「这肥鱼魔了」。60 秒 DEDUP 拦不住这种跨 20 分钟的复读。 | `plugins/vision_agent/__init__.py` |
| `TRACE_FLAT_STD` | `10` | 纯色/极简图的灰度标准差阈值（挡掉「纯色帧 similarity 0.98」的假命中） | `services/vision_verify.py` |
| `TRACE_MAX_PER_DAY` | `80` | trace.moe 每天配额（默认 100 次/天） | `services/vision_verify.py` |
| `TRACE_MIN_SHARE` | `0.35` | 最高票至少占几成 | `services/vision_verify.py` |
| `TRACE_MIN_SIM` | `0.75` | 采信下限（疑似） | `services/vision_verify.py` |
| `TRACE_MIN_VOTES` | `2` | 至少几票 | `services/vision_verify.py` |
| `TRACE_SINGLE_SIM` | `0.95` | 单条即可采信 | `services/vision_verify.py` |
| `TRACE_STRONG_SIM` | `0.9` | 高置信 | `services/vision_verify.py` |
| `VIDEO_FRAME_INTERVAL` | `5` | 帧数有上限——太长会自动变稀（60 帧 × 视觉逐帧已经很慢很贵）。 | `services/video_understand.py` |
| `VIDEO_MAX_FRAMES` | `60` | 视频理解最多抽几帧 | `services/video_understand.py` |
| `VIDEO_MAX_FRAME_DESC` | `100` | 每帧描述最大字数 | `services/video_understand.py` |
| `VIDEO_MAX_KEY_FRAMES` | `12` | 字幕密集处额外取的高清关键帧上限（配合均匀抽帧一起看） | `services/video_understand.py` |
| `VISION_CACHE_MAX` | `800` | 最多存多少张图 | `services/vision_verify.py` |
| `VISION_COOLDOWN` | `300` | 被动识图冷却（识别成本，跟"要不要开口"是两回事） | `plugins/vision_agent/__init__.py` |
| `VISION_LLM_MAX_PER_HOUR` | `12` | 注意这是**频率闸门**，不是「该不该说」的判断 —— 判断在模型那边。 | `plugins/vision_agent/__init__.py` |
| `VISION_MAX_ATTEMPTS` | `6` | 总尝试次数上限 | `plugins/vision_agent/__init__.py` |
| `VISION_MAX_SIDE` | `1568` | 最长边缩到多少（越大越清楚、请求越大；默认 1568） | `plugins/vision_agent/__init__.py` |
| `VISION_TIMEOUT` | `40` | 视觉模型超时（秒） | `plugins/vision_agent/__init__.py` |
| `VISION_VERIFY_MAX_PER_HOUR` | `20` | 视觉核对每小时最多几次 | `services/vision_verify.py` |

## 画图

| 参数 | 默认 | 说明 | 生效位置 |
|---|---|---|---|
| `DRAW_MAX_CONCURRENT` | `2` | 同时进行的画图任务上限 | `plugins/draw/__init__.py` |
| `DRAW_WAIT_SEC` | `360` | 等画图结果的最长时间（秒） | `plugins/draw/__init__.py` |

## 语音 / 音乐

| 参数 | 默认 | 说明 | 生效位置 |
|---|---|---|---|
| `AGENT_RECALL_TIMEOUT` | `6` | 算力机回忆接口超时（秒） | `services/agent_client.py` |
| `AGENT_REFLECT_QUEUE_TIMEOUT` | `600` | 算力机反思任务排队超时（秒） | `services/agent_client.py` |
| `AGENT_REFLECT_TIMEOUT` | `180` | 算力机反思任务执行超时（秒） | `services/agent_client.py` |
| `COVER_FULL_TTL` | `600` | 翻唱成品缓存时间（秒） | `plugins/song_cover/__init__.py` |
| `COVER_FULL_WAIT_SEC` | `1800` | 等翻唱成品的最长时间（秒） | `plugins/song_cover/__init__.py` |
| `COVER_MAX_CONCURRENT` | `2` | 同时进行的翻唱任务上限 | `plugins/song_cover/__init__.py` |
| `COVER_PITCH_MAX_TOLERANCE` | `0.1` | 女声源允许超出模型音域上限的比例（0.10 = 10%） | `plugins/song_cover/__init__.py` |
| `COVER_RVC_WAIT_SEC` | `900` | 等 RVC 变声的超时（秒） | `plugins/song_cover/__init__.py` |
| `COVER_SPLIT_WAIT_SEC` | `900` | 等人声分离的超时（秒） | `plugins/song_cover/__init__.py` |
| `COVER_VOICE_MAX_MB` | `3` | 翻唱输入音频大小上限（MB） | `plugins/song_cover/__init__.py` |
| `COVER_VOICE_MAX_SEC` | `58` | 翻唱输入音频时长上限（秒） | `plugins/song_cover/__init__.py` |
| `F0_PERIODICITY_MIN` | `0.45` | F0 周期度阈值：低于它的帧当噪声丢弃（0.45） | `services/song_gender.py` |
| `PROBE_UPLOAD_TTL` | `900` | 探测上传的有效期（秒） | `plugins/song_cover/__init__.py` |
| `RVC_INDEX_RATE_FEMALE` | `0.5` | 女声 RVC index 权重 | `plugins/song_cover/__init__.py` |
| `RVC_INDEX_RATE_MALE` | `0.5` | 男声 RVC index 权重 | `plugins/song_cover/__init__.py` |
| `RVC_PITCH_FEMALE` | `12` | 女声变调半音数 | `plugins/song_cover/__init__.py` |
| `RVC_PITCH_MALE` | `-12` | 男声变调半音数 | `plugins/song_cover/__init__.py` |
| `RVC_PITCH_MAX` | `24` | RVC 变调上限（半音） | `services/song_gender.py` |
| `RVC_PITCH_MIN` | `-24` | 自适应 pitch 的上下限，防止测错把歌毁了 | `services/song_gender.py` |
| `RVC_PROTECT_FEMALE` | `0.4` | 女声保护辅音参数（0~1） | `plugins/song_cover/__init__.py` |
| `RVC_PROTECT_MALE` | `0.4` | 男声保护辅音参数（0~1） | `plugins/song_cover/__init__.py` |
| `RVC_RMS_FEMALE` | `0.8` | 女声响度归一化目标 | `plugins/song_cover/__init__.py` |
| `RVC_RMS_MALE` | `0.8` | 男声响度归一化目标 | `plugins/song_cover/__init__.py` |
| `RVC_SAME_GENDER_MAX_SEMIS` | `3` | 源是女声时的移调上限（半音）：女→女只换音色，不该大幅降调 | `services/song_gender.py` |
| `RVC_TARGET_F0` | `220` | 目标音色（kikiv2 女声）的典型基频中位数。自适应 pitch 的基准点。 | `services/song_gender.py` |
| `RVC_VOICE_F0_MAX` | `880` | 判定女声的基频上限（Hz） | `services/song_gender.py` |
| `RVC_VOICE_F0_MIN` | `130` | 判定女声的基频下限（Hz） | `services/song_gender.py` |

## 世界书

| 参数 | 默认 | 说明 | 生效位置 |
|---|---|---|---|
| `LORE_BUDGET` | `520` | 世界书前置注入的字数预算 | `services/lorebook.py` |
| `LORE_BUDGET_POST` | `260` | 世界书后置注入的字数预算 | `services/lorebook.py` |
| `LORE_MAX_ENTRIES` | `200` | 世界书最多命中多少条 | `services/lorebook.py` |
| `LORE_SCAN_DEPTH` | `4` | 世界书扫描深度（条） | `services/lorebook.py` |

## 安全 / 工具

| 参数 | 默认 | 说明 | 生效位置 |
|---|---|---|---|
| `HEARTBEAT_TIMEOUT_SEC` | `90` | 工具分发心跳超时（秒） | `plugins/tool_dispatcher/__init__.py` |
| `HISTORY_MAX` | `20` | 模型侧历史消息条数 | `services/napcat_tools.py` |
| `MAX_REPLY_CHARS` | `200` | .env 里留着 MAX_HISTORY 也不会有任何作用。 | `services/config.py` |
| `NAPCAT_APPROVE_TTL` | `600` | 待批准请求有效期（秒） | `services/napcat_tools.py` |
| `NAPCAT_TOOL_MAX_PER_MIN` | `20` | NapCat 工具每分钟最多调用次数 | `services/napcat_tools.py` |
| `NAPCAT_TOOL_RESULT_MAX` | `550` | 回给模型的结果上限 | `services/napcat_tools.py` |
| `SELF_TOOL_FSIZE_MB` | `200` | 自检工具读取文件大小上限（MB） | `plugins/self_tools/__init__.py` |
| `SELF_TOOL_MEM_MB` | `1536` | 自检工具内存上限（MB） | `plugins/self_tools/__init__.py` |
| `SELF_TOOL_READ_MAX` | `1500` | 自检工具最多读取多少行 | `plugins/self_tools/__init__.py` |
| `SELF_TOOL_TIMEOUT` | `60` | 自检工具执行超时（秒） | `plugins/self_tools/__init__.py` |

## 其他

| 参数 | 默认 | 说明 | 生效位置 |
|---|---|---|---|
| `BUBBLE_DELAY_BASE_MS` | `300` | 多气泡之间的基础停顿（毫秒） | `services/aura_logic.py` |
| `BUBBLE_DELAY_MAX_MS` | `1600` | 多气泡停顿上限（毫秒，别让人等太久） | `services/aura_logic.py` |
| `BUBBLE_DELAY_PER_CHAR` | `90` | 多气泡停顿按字数叠加（毫秒/字） | `services/aura_logic.py` |
| `HARNESS_MAX_OUTPUT` | `3000` | Harness 桥回给 QQ 的最大字数 | `services/harness.py` |
| `HARNESS_TIMEOUT` | `300` | Harness 桥单次调用最长秒数 | `services/harness.py` |
| `MAX_BUBBLES` | `2` | 一条回复最多拆几个气泡 | `services/aura_logic.py` |
| `POKE_MAX_PER_WINDOW` | `3` | 戳一戳屏蔽：窗口内最多回几次（超过就不回） | `services/poke_guard.py` |
| `POKE_WINDOW_HOURS` | `2` | 戳一戳屏蔽的统计窗口（小时） | `services/poke_guard.py` |
| `SEGMENT_HISTORY_KEEP` | `24` | 保留多少个「处理过的段」给上下文/翻历史 | `services/segments.py` |
| `SEGMENT_MAX_MSGS` | `40` | 群聊分段：最多攒多少条（防刷屏） | `services/segments.py` |
| `SEGMENT_MAX_SEC` | `30` | 群聊分段：最长攒多久必须处理（默认 30 秒） | `services/segments.py` |
| `SEGMENT_WAIT_SEC` | `3.5` | 群聊分段：安静多少秒算「这一段说完了」（默认 3.5） | `services/segments.py` |
| `WHITE_MOON_MIN_SCORE` | `3` | 白月光：至少多少分才算「最吃这个人的说话方式」 | `services/white_moon.py` |
| `WHITE_MOON_STYLE_ITEMS` | `3` | 白月光：注入几条学到的说法 | `services/white_moon.py` |
| `WHITE_MOON_STYLE_TTL` | `21600` | 白月光：说话方式多久重学一次（秒） | `services/white_moon.py` |

