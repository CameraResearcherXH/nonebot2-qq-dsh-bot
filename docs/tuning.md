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
- 生效位置标 **（设计，未接线）** 的键：设计已定、代码还没读，**改了不生效**；接线后会自动去掉标记。

---

## 参数总览

| 分组 | 项数 |
|---|---|
| 回复 / 限流 / 主动插话 | 46 |
| 心情 | 83 |
| 档案 / 好感 | 52 |
| 记忆 | 34 |
| 表情 | 7 |
| 主动私聊 | 17 |
| 安静 / 破冰 | 6 |
| 提示词 | 8 |
| 模型调用 | 20 |
| 用量监测 | 4 |
| 回复审核 | 14 |
| 表达 / 语气 | 7 |
| 视觉 | 19 |
| 画图 | 2 |
| 语音 / 音乐 | 31 |
| 世界书 | 4 |
| 安全 / 工具 | 9 |
| 其他 | 123 |
| **合计** | **486** |

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
| `GROUP_REPLY_COOLDOWN` | `45` | 群聊刚回过之后多少秒内，非点名消息不单独回（防同一件事回两遍） | `plugins/chat/__init__.py` |
| `GROUP_SEND_LIMIT` | `9` | 群发配额：每窗口最多几条 | `plugins/chat/__init__.py` |
| `GROUP_SEND_MAX_WAIT` | `15` | 配额不足时最多等多久（秒） | `plugins/chat/__init__.py` |
| `LONG_FORWARD_MIN` | `120` | 超过多少字改用合并转发 | `plugins/chat/__init__.py` |
| `MAX_GROUP_RECENT` | `16` | 群聊上下文保留多少条消息（16 = 修过长消息被挤出窗口的问题） | `plugins/chat/__init__.py` |
| `MEMORY_EMOTION_MIN_DELTA` | `0.1` | 记忆刻痕门槛：|Δ| 达到多少才算（0.10 = 设计里的 💥 档） | `plugins/chat/__init__.py` |
| `MERGE_WINDOW` | `0.6` | 合并转发判定窗口（秒） | `plugins/chat/__init__.py` |
| `MOOD_FOLLOW_MIN` | `-35` | 心情低于这个值就不再追着接话（只回明确叫她） | `plugins/chat/__init__.py` |
| `MOOD_WATCH_SEC` | `60` | 被动心情观察的最小间隔（秒） | `plugins/chat/__init__.py` |
| `OUT_BUBBLES_MAX` | `2` | 单轮气泡上限 | `plugins/chat/__init__.py` |
| `OUT_BUBBLE_CHARS` | `40` | 单泡汉字上限 | `plugins/chat/__init__.py` |
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
| `SLEEP_HOUR_END` | `8` | 群聊免打扰结束小时（0-23） | `plugins/chat/__init__.py` |
| `SLEEP_HOUR_START` | `0` | 群聊免打扰开始小时（0-23） | `plugins/chat/__init__.py` |
| `TTS_PICKUP_TIMEOUT` | `40` | 等算力机接单的超时（秒） | `plugins/chat/__init__.py` |
| `TTS_WAIT_TOTAL` | `240` | 等语音合成结果的总超时（秒） | `plugins/chat/__init__.py` |
| `UPLOAD_MAX_MB` | `200` | 上传文件大小上限（MB） | `plugins/chat/__init__.py` |
| `UPLOAD_WAIT_SEC` | `180` | 等待文件上传的超时（秒） | `plugins/chat/__init__.py` |

## 心情

| 参数 | 默认 | 说明 | 生效位置 |
|---|---|---|---|
| `AFFINITY_GAIN` | `0.05` | ⚠️ LEGACY（无读点，保留键名兼容旧 .env）：新口径见 AFF_* / REL_* | `services/mood_judge.py（设计，未接线）` |
| `AROUSAL_LEVEL_HIGH` | `0.8` | judge arousal=high 对应的 a 值 | `services/mood_judge.py` |
| `AROUSAL_LEVEL_LOW` | `0.2` | judge arousal=low 对应的 a 值 | `services/mood_judge.py` |
| `AROUSAL_LEVEL_MID` | `0.5` | judge arousal=mid 对应的 a 值 | `services/mood_judge.py` |
| `AROUSAL_SMOOTH` | `0.4` | a 平滑：a = (1-s)*old + s*level | `services/mood.py` |
| `GROUP_MOOD_CHAT_MAX` | `1.35` | 群心情最好时「愿不愿意多说话」的乘数上限（1.35） | `services/group_mood.py` |
| `GROUP_MOOD_CHAT_MIN` | `0.55` | 群心情最差时「愿不愿意多说话」的乘数下限（0.55） | `services/group_mood.py` |
| `GROUP_MOOD_GROUP_DROP` | `0.7` | 整个群净掉多少 → 判定群聊不愉快（0.7） | `services/group_mood.py` |
| `GROUP_MOOD_LIKE_MIN` | `0.05` | 净 Δ 达到多少算喜欢/讨厌（0.05，算人数和比例用） | `services/group_mood.py` |
| `GROUP_MOOD_MAX_DELTA` | `0.2` | 单条群心情事件上限（0.20，防小事刷满） | `services/group_mood.py` |
| `GROUP_MOOD_MAX_EVENTS` | `400` | 每个群最多保留多少条心情事件 | `services/group_mood.py` |
| `GROUP_MOOD_MIN_DELTA` | `0.03` | 进群心情的最小 |Δ|（0.03，防小事噪声） | `services/group_mood.py` |
| `GROUP_MOOD_MIN_PEOPLE` | `3` | 至少几个不同的人让心情掉，才算「群聊导致」（3） | `services/group_mood.py` |
| `GROUP_MOOD_PERSON_DROP` | `0.4` | 某个人在窗口内净让心情掉多少 → 单独不理他（0.4） | `services/group_mood.py` |
| `GROUP_MOOD_WINDOW_SEC` | `18000` | 群心情窗口（秒，默认 18000 = 5 小时；超时事件自动过期） | `services/group_mood.py` |
| `MOOD_AD_BASE_AROUSAL` | `0.35` | 唤醒 a 的回落基线（0~1） | `services/mood.py` |
| `MOOD_AD_BASE_DOMINANCE` | `0.5` | 掌控 d 的回落基线（0~1） | `services/mood.py` |
| `MOOD_ANCHOR_COUNT_SKIPPED` | `1` | 1=被 relevance 跳过的窗口也计入锚点计数；0=只数真判读 | `services/mood.py` |
| `MOOD_ANCHOR_DEADBAND` | `0.15` | 锚点软死区：差值小于它时按 smoothstep 衰减混入（不再硬跳过） | `services/mood.py` |
| `MOOD_ANCHOR_EVERY` | `20` | 锚点：每多少窗额外回补一次 P（每窗都已按 WEIGHT 混入） | `services/mood.py` |
| `MOOD_ANCHOR_GAIN` | `0.25` | 独立锚点（静默定时器/手动）的增益 K_a = gain × confidence | `services/mood.py` |
| `MOOD_ANCHOR_IDLE_MIN` | `30` | 锚点：静默多少分钟也触发 | `services/mood.py` |
| `MOOD_ANCHOR_LEVEL1` | `0.35` | 锚点 level ±1 对应的绝对偏移 | `services/mood.py` |
| `MOOD_ANCHOR_LEVEL2` | `0.7` | 锚点 level ±2 对应的绝对偏移 | `services/mood.py` |
| `MOOD_ANCHOR_MAX_MOVE` | `0.2` | 锚点单次最大位移 | `services/mood.py` |
| `MOOD_ANCHOR_P_ADD` | `0.02` | 锚点后 P 的回补量 | `services/mood.py` |
| `MOOD_ANCHOR_THINK_AGE_SEC` | `2100` | 静默锚点只认多久内的 THINK（秒；必须 ≥ 静默时长 + tick 间隔，默认 2100） | `services/mood_judge.py` |
| `MOOD_ANCHOR_TIMER` | `1` | 静默锚点定时器（1=开，0=只在下一条消息时补锚） | `services/mood_judge.py` |
| `MOOD_ANCHOR_WEIGHT` | `0.45` | 每窗锚点权重：v 向绝对档位拉 W×conf（anchor_level 占大头） | `services/mood.py` |
| `MOOD_ENV_MAX_DELTA` | `0.05` | 不是冲她来（别人之间/群气氛）时单次 Δ 上限（0.05） | `services/mood_judge.py` |
| `MOOD_FAST_CONF` | `0.4` | 快速通道置信度 | `services/mood.py` |
| `MOOD_FAST_FLOOR` | `-0.45` | 快速通道强负面钳位（只降不升） | `services/mood.py` |
| `MOOD_FAST_FLOOR_LIGHT` | `-0.25` | 快速通道轻负面钳位 | `services/mood.py` |
| `MOOD_FAST_P_FACTOR` | `1.5` | 快速通道 P 放大（>1 让下一轮判读能拉回；上限 1） | `services/mood.py` |
| `MOOD_GLOBAL_SHARE` | `0.35` | 全局心情占会话心情的权重（0~1） | `services/mood.py` |
| `MOOD_HALF_LIFE_A_GLOBAL_MIN` | `60` | 唤醒 a 的全局半衰期（分钟） | `services/mood.py` |
| `MOOD_HALF_LIFE_A_GROUP_MIN` | `15` | 唤醒 a 的群聊半衰期（分钟） | `services/mood.py` |
| `MOOD_HALF_LIFE_A_PRIVATE_MIN` | `60` | 唤醒 a 的私聊半衰期（分钟） | `services/mood.py` |
| `MOOD_HALF_LIFE_D_GLOBAL_MIN` | `360` | 掌控 d 的全局半衰期（分钟） | `services/mood.py` |
| `MOOD_HALF_LIFE_D_GROUP_MIN` | `120` | 掌控 d 的群聊半衰期（分钟） | `services/mood.py` |
| `MOOD_HALF_LIFE_D_PRIVATE_MIN` | `360` | 掌控 d 的私聊半衰期（分钟） | `services/mood.py` |
| `MOOD_HALF_LIFE_GLOBAL_MIN` | `360` | 全局心情半衰期（分钟） | `services/mood.py` |
| `MOOD_HALF_LIFE_GROUP_MIN` | `30` | 群聊心情半衰期（分钟） | `services/mood.py` |
| `MOOD_HALF_LIFE_HOURS` | `12` | ⚠️ LEGACY（P2 删）：旧单一半衰期 12h；新口径按场景 30min/2h/6h | `services/mood.py` |
| `MOOD_HALF_LIFE_PRIVATE_MIN` | `120` | 私聊心情半衰期（分钟） | `services/mood.py` |
| `MOOD_HARD_CAP` | `0.35` | 单次硬上限（±3 首条会触发：0.507→0.35，不是「永不触发」） | `services/mood.py` |
| `MOOD_IDLE_AFTER` | `300` | 空闲多少秒后心情开始衰减（默认 300 = 5 分钟；之后每 5 分钟向 0 走一步） | `services/mood.py` |
| `MOOD_IDLE_STEP` | `1` | 每步走多少点（内部 -100~100 口径，1.0 ≈ 外口径 0.01） | `services/mood.py` |
| `MOOD_IDLE_TICK` | `300` | 衰减步长（秒）：每这么久向 0 走一步（默认 300 = 5 分钟走 0.01） | `services/mood.py` |
| `MOOD_IMPACT3_DAILY_MAX` | `1` | ±3 档每会话每天最多几次（超了降到 OVER_TO；0=不限） | `services/mood.py` |
| `MOOD_IMPACT3_OVER_TO` | `2` | ±3 超限后降到的档位（2 = 保留「大事」，但不再顶格） | `services/mood.py` |
| `MOOD_IMPACT_MAX` | `3` | 判读档位上限（3 = 七档 -3~+3） | `services/mood.py` |
| `MOOD_JUDGE_CTX` | `8` | 判读时带多少条上下文（8 = 5~8 条，和主模型上下文同源同条数） | `services/mood_judge.py` |
| `MOOD_JUDGE_FRESH_SEC` | `600` | 判读结果有效期（秒），过期不当「现在」 | `services/mood.py` |
| `MOOD_JUDGE_MAX_PER_HOUR` | `30` | 每会话每小时最多判读几次（含补判窗口）；超了只判最后一窗 | `services/mood_judge.py` |
| `MOOD_JUDGE_MIN_INTERVAL` | `60` | 两次判读的最小间隔（秒），防刷屏时来回震荡 | `services/mood_judge.py` |
| `MOOD_JUDGE_REFRESH` | `300` | 她没主动写心情时，多久用群聊重新判一次（秒）。0 = 只在她说的时候判 | `services/mood_judge.py` |
| `MOOD_JUDGE_SMOOTH` | `0.35` | ⚠️ LEGACY（P2 删）：判读值平滑系数，旧公式用 | `services/mood.py` |
| `MOOD_JUDGE_TIMEOUT` | `12` | 判读调用超时（秒） | `services/mood_judge.py` |
| `MOOD_LABEL_MAX` | `60` | 心情词最多保留多少个 | `services/mood.py` |
| `MOOD_LABEL_TTL_HOURS` | `12` | 心情词的有效期（小时），过期不再当「现在」 | `services/mood.py` |
| `MOOD_MAX_DELTA` | `0.5` | 名义 Δ 的夹取上限（判读护栏 / mood.apply_delta 仍在用，不是死键） | `services/mood.py` |
| `MOOD_MAX_SESSIONS` | `50` | 最多保留多少个会话的心情（防字典无限长） | `services/mood.py` |
| `MOOD_MAX_STEP` | `0.3` | ⚠️ LEGACY（P2 删）：单次判读最多改变多少，旧公式用 | `services/mood.py` |
| `MOOD_NOISE_DELTA` | `0.03` | Δ 噪音门槛：|Δ| 小于它视为 0（0.03，不动数值/好感/刻痕） | `services/mood_judge.py` |
| `MOOD_P0` | `0.25` | 不确定度初值 | `services/mood.py` |
| `MOOD_PEAK_MIN_DELTA` | `0.03` | 进入「峰值事件」候选的最小 |Δ|（0.03，防小事顶掉大事） | `services/mood.py` |
| `MOOD_PEAK_WINDOW_SEC` | `7200` | 心情峰值事件的保质期（秒，默认 7200 = 2 小时；每件事最多挂 2 小时） | `services/mood.py` |
| `MOOD_P_EVENT_BOOST` | `0.05` | |impact|≥2 时 P 的回补量（让突发事件推得动心情） | `services/mood.py` |
| `MOOD_P_EVENT_CAP` | `0.2` | 回补后 P 的上限（防止连刷把 K 顶太高） | `services/mood.py` |
| `MOOD_Q_RATE_BASE` | `0.02` | 过程噪声速率（每小时，基数） | `services/mood.py` |
| `MOOD_Q_RATE_SLOPE` | `0.1` | 过程噪声速率（每小时，乘 reactivity） | `services/mood.py` |
| `MOOD_RELEVANCE_GATE` | `0.25` | 窗口相关度低于此值只衰减不判读 | `services/mood.py` |
| `MOOD_R_BASE` | `0.02` | 观测噪声基数 | `services/mood.py` |
| `MOOD_R_SLOPE` | `0.6` | 观测噪声置信项系数 | `services/mood.py` |
| `MOOD_SATURATION` | `0.6` | 饱和系数：Δ×(1−s×abs(v)) | `services/mood.py` |
| `MOOD_SENS_MAX` | `1.4` | 人设敏感度上限 | `services/mood.py` |
| `MOOD_SENS_MIN` | `0.6` | 人设敏感度下限 | `services/mood.py` |
| `MOOD_STEP_1` | `0.1` | 观测步长 impact=±1（相对位移） | `services/mood.py` |
| `MOOD_STEP_2` | `0.22` | 观测步长 impact=±2 | `services/mood.py` |
| `MOOD_STEP_3` | `0.4` | 观测步长 impact=±3（猛击：一见钟情/被背叛） | `services/mood.py` |
| `MOOD_THINK_KEEP` | `30` | 判读心理活动保留多少条 | `services/mood_judge.py` |
| `MOOD_WINDOW_MAX_SEC` | `300` | 判读窗口时间上限（秒）：不足 N 句但过了这么久也判（慢群防滞后） | `services/mood_judge.py` |

## 档案 / 好感

| 参数 | 默认 | 说明 | 生效位置 |
|---|---|---|---|
| `AFFINITY_DAILY_CAP` | `0.15` | v1 好感每日净变化上限（profiles.bump_affinity 用；v2 不用） | `services/profiles.py` |
| `AFFINITY_TREND_WINDOW_SEC` | `1800` | 好感走势的统计窗口（秒） | `services/profiles.py` |
| `AFF_BOND_DEEP_EXP` | `1.2` | 深层 bond 随 I_deep 的陡度 | `services/profiles.py` |
| `AFF_BOND_DEEP_MAX` | `0.3` | 深层投入额外 bond（里程碑/修复/暖天数） | `services/profiles.py` |
| `AFF_BOND_EXP` | `1.5` | 浅层 bond 随投入的陡度 | `services/profiles.py` |
| `AFF_BOND_MAX` | `0.6` | 浅层投入的 bond 上限 | `services/profiles.py` |
| `AFF_COMP_CAP` | `0.8` | 陪伴通道天花板 | `services/profiles.py` |
| `AFF_COMP_W` | `0.004` | 一句中性陪伴的痕迹权重 | `services/profiles.py` |
| `AFF_CONS_FROM` | `0.75` | 巩固起点 a | `services/profiles.py` |
| `AFF_CONS_K` | `0.6` | 满载时沉淀进 bond 的比例 | `services/profiles.py` |
| `AFF_CONS_TO` | `0.95` | 巩固满载点 a | `services/profiles.py` |
| `AFF_DV_K` | `0.6` | 日常区 dv→好感 换算系数 | `services/profiles.py` |
| `AFF_GUARD_POS` | `0.7` | a<0 时正向跳变折扣（有防备） | `services/profiles.py` |
| `AFF_HURT_GRUDGE` | `0.15` | 负向 peak 加多少记仇 | `services/profiles.py` |
| `AFF_JUMP_FROM` | `0.15` | 显著区起点 |dv|（骤暖/骤冷） | `services/profiles.py` |
| `AFF_JUMP_GAMMA` | `1.5` | 显著区凸性指数 | `services/profiles.py` |
| `AFF_JUMP_K` | `1.6` | 显著区放大系数 | `services/profiles.py` |
| `AFF_JUMP_SAT` | `0.35` | 显著区饱和指数（高好感也推得动） | `services/profiles.py` |
| `AFF_LOVE_DV` | `0.3` | 一见钟情阈值（peaks+2、κ≥0.5） | `services/profiles.py` |
| `AFF_MAX` | `0.99` | 好感硬上限（0.99，不给 1.0） | `services/profiles.py` |
| `AFF_MODEL` | `v1` | 好感模型开关（v1=指数衰减 + delta/dv 双吃 / v2=痕迹求和） | `services/profiles.py` |
| `AFF_NEG_BIAS` | `1.6` | 负面事件权重 | `services/profiles.py` |
| `AFF_PEAK_BONUS` | `0.25` | 一次 peak 额外加的巩固比例 | `services/profiles.py` |
| `AFF_PEAK_DV` | `0.15` | peak 计数阈值（显著事件） | `services/profiles.py` |
| `AFF_SAT_NEG` | `0.35` | 日常区负向饱和指数 | `services/profiles.py` |
| `AFF_SAT_POS` | `0.7` | 日常区正向饱和指数 | `services/profiles.py` |
| `AFF_TAIL_BASE` | `0.06` | 尾部区基础跳变 | `services/profiles.py` |
| `AFF_TAIL_FROM` | `0.3` | 尾部区起点 |dv|（心碎/一见钟情） | `services/profiles.py` |
| `AFF_TAIL_GAMMA` | `1.8` | 尾部区凸性指数 | `services/profiles.py` |
| `AFF_TAIL_K` | `2` | 尾部区放大系数 | `services/profiles.py` |
| `AFF_TAIL_SAT` | `0.15` | 尾部区饱和指数（几乎不看当前好感） | `services/profiles.py` |
| `AFF_TIER_SOFT` | `0.02` | 分层门槛软上限余量 | `services/profiles.py` |
| `AFF_TRACE_MAX` | `400` | 每人最多保留的痕迹天数（按天聚合） | `services/profiles.py` |
| `AFF_TRACE_P` | `0.5` | 痕迹幂律指数（ACT-R d≈0.5） | `services/profiles.py` |
| `AFF_TRACE_T` | `10` | 痕迹衰减时间常数（天） | `services/profiles.py` |
| `AFF_V1_CAP` | `0.15` | v1 单次好感增量上限（= 日上限 0.15；实测最大 0.136，旧的 ±0.10 装不下尾部） | `services/profiles.py` |
| `AFF_V1_DELTA_K` | `0.25` | v1 事件重量项系数（旧版等效 1.0；压小 = 好感不再那么快） | `services/profiles.py` |
| `AFF_V1_DV_K` | `0.3` | v1 实际位移项系数（乘三段式；尾部跳变仍能到单次上限） | `services/profiles.py` |
| `AFF_WARM_FULL` | `0.25` | 7 天暖流达到多少算「一直在暖」（κ 满载） | `services/profiles.py` |
| `AFF_W_MAX` | `0.42` | 单次好感痕迹硬上限（单次上限；当前 dv 上限 0.35 → 最大 0.362，0.42 为 ±4/放宽预留，不是死护栏） | `services/profiles.py` |
| `AVATAR_PX` | `160` | 头像生成尺寸（像素） | `services/profile_card.py` |
| `AVATAR_TTL_DAYS` | `7` | 头像缓存保留天数 | `services/profile_card.py` |
| `BOND_EVENT_MIN_DELTA` | `0.15` | 「印象最深的一件事」的门槛：记忆 |Δ| 达到多少才算（0.15） | `services/profiles.py` |
| `BOND_TTL_DAYS` | `60` | 相处印象（档案）保留天数 | `services/profiles.py` |
| `PROFILE_CARD_EVENTS` | `10` | 人物卡里最多列几条事件 | `services/profiles.py` |
| `PROFILE_MINE_INTERVAL_H` | `20` | 同一个人多久挖一次 | `services/profile_miner.py` |
| `PROFILE_MINE_MIN_MSGS` | `25` | 攒多少条发言挖一次 | `services/profile_miner.py` |
| `PROFILE_MINE_SAMPLE` | `40` | 一次喂多少条他的发言 | `services/profile_miner.py` |
| `PROFILE_MINE_TIMEOUT` | `25` | 档案挖掘模型超时（秒） | `services/profile_miner.py` |
| `PROFILE_NOTE_MIN_DELTA` | `0.15` | 档案事情门槛：|Δ| 达到多少才写进档案（0.15 ≈ 只有 impact±2 的大事） | `services/profiles.py` |
| `REL_INTIMACY_HALF_LIFE_DAYS` | `30` | 亲近半衰期（天） | `services/profiles.py` |
| `SCENE_CARD_LINES` | `20` | 群场景卡片最多几行 | `services/profile_card.py` |

## 记忆

| 参数 | 默认 | 说明 | 生效位置 |
|---|---|---|---|
| `ADDR_ASSERT_MIN` | `0.8` | ≥此值才敢断言「→ X」 | `services/group_scene.py` |
| `ADDR_AT_OR_QUOTE` | `0.99` | @ 或引用她的置信度 | `services/group_scene.py` |
| `ADDR_BACKCHANNEL` | `0.55` | 反应词/附和的置信度 | `services/group_scene.py` |
| `ADDR_MAYBE_MIN` | `0.5` | 0.5~0.8 写「像是在回 X」；<0.5 写不确定 | `services/group_scene.py` |
| `ADDR_NAME` | `0.7` | 正文点名的置信度 | `services/group_scene.py` |
| `ADDR_QA_PAIR` | `0.65` | 上一句提问→这句回答的置信度 | `services/group_scene.py` |
| `ADDR_QUOTE` | `0.95` | 引用（非 @）的置信度 | `services/group_scene.py` |
| `ADDR_RECENT_MENTION` | `0.5` | 短句冲最近被提到的人的置信度 | `services/group_scene.py` |
| `ADDR_VOCATIVE` | `0.8` | 开头称呼的置信度 | `services/group_scene.py` |
| `ADDR_YOU` | `0.6` | 用了「你」的置信度 | `services/group_scene.py` |
| `ARCHIVE_HOUR` | `3` | 每日归档执行的小时（0-23） | `plugins/memory_migration/__init__.py` |
| `ARCHIVE_MINUTE` | `0` | 每日归档执行的分钟（0-59） | `plugins/memory_migration/__init__.py` |
| `ARCHIVE_WINDOW_DAYS` | `30` | 归档覆盖最近多少天 | `plugins/memory_migration/__init__.py` |
| `DAILY_SUMMARY_KEEP_DAYS` | `14` | 日结文件保留天数 | `services/daily_summary.py` |
| `DAILY_SUMMARY_MIN_MSGS` | `6` | 当天至少多少条消息才写日结 | `services/daily_summary.py` |
| `DIALOGUE_IMG_SIDE` | `800` | 对话日志里图片缩略图边长（像素） | `services/dialogue_log.py` |
| `DIALOGUE_KEEP_DAYS` | `14` | 对话原文保留天数 | `services/dialogue_log.py` |
| `MEMORY_REVISIT_AGE_DAYS` | `7` | 回访记忆：至少多少天前的事 | `services/memory_v2.py` |
| `MEMORY_REVISIT_IDLE_DAYS` | `3` | 回访记忆：至少多久没提过 | `services/memory_v2.py` |
| `MEM_MMR_CAND` | `3` | MMR 候选倍数（limit×N） | `services/memory_v2.py` |
| `MEM_MMR_ENABLED` | `1` | 证据选择是否启用 MMR 多样性 | `services/memory_v2.py` |
| `MEM_MMR_LAMBDA` | `0.7` | MMR 相关性权重（1=只看分数，0=只看差异） | `services/memory_v2.py` |
| `RECALL_TEXT_MAX` | `600` | 单条记忆注入的最大字数 | `services/memory_v2.py` |
| `REFLECT_HOUR` | `20` | 每日反思执行的小时（0-23） | `plugins/memory_migration/__init__.py` |
| `REFLECT_MINUTE` | `0` | 每日反思执行的分钟（0-59） | `plugins/memory_migration/__init__.py` |
| `REFLECT_WINDOW_DAYS` | `2` | 反思覆盖最近多少天 | `services/journal_agent.py` |
| `RET_CHAR_LATENCY_MS` | `50` | 角色脑延迟目标（ms） | `services/memory_v2.py` |
| `RET_EVIDENCE_MAX` | `5` | 最终注入证据条数上限 | `services/memory_v2.py` |
| `RET_MAX_PER_PERSON` | `3` | 同一人最多几条（=5×60%） | `services/memory_v2.py` |
| `RET_NEUTRAL_QUOTA` | `1` | 前 N 条至少 1 条中性/反向 | `services/memory_v2.py` |
| `RET_SAME_PERSON_RATIO` | `0.6` | 同一人占证据的比例上限 | `services/memory_v2.py` |
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
| `PROACTIVE_ACCEPT_BONUS` | `0.3` | accept_rate 对主动评分的加成系数 | `services/proactive.py` |
| `PROACTIVE_ACCEPT_WINDOW_H` | `12` | 主动私聊后多久内对方回话算「接住了」（小时） | `services/proactive.py` |
| `PROACTIVE_CHECK_SEC` | `600` | 主动私聊巡检间隔（秒） | `services/proactive.py` |
| `PROACTIVE_COOLDOWN_H` | `6` | 两次主动私聊的最小间隔（小时） | `services/proactive.py` |
| `PROACTIVE_GROUP_MAX_PER_DAY` | `3` | 群主动接话每天最多几次（每个群） | `services/proactive.py` |
| `PROACTIVE_IDLE_MAX_H` | `72` | 超过多久算「太久没聊」（小时） | `services/proactive.py` |
| `PROACTIVE_IDLE_MIN_H` | `2` | 至少多久没聊才考虑主动找（小时） | `services/proactive.py` |
| `PROACTIVE_MAX_PER_DAY` | `2` | 主动私聊每人每天上限 | `services/proactive.py` |
| `PROACTIVE_MIN_INTIMACY` | `0.45` | 主动私聊要求的最低亲近度 | `services/proactive.py` |
| `PROACTIVE_MIN_SCORE` | `0.9` | 候选人打分门槛 | `services/proactive.py` |
| `PROACTIVE_QUIET_END` | `9` | 主动私聊免打扰结束小时 | `services/proactive.py` |
| `PROACTIVE_QUIET_START` | `1` | 主动私聊免打扰开始小时 | `services/proactive.py` |
| `RHYTHM_ACTIVE` | `1` | 表达节奏 active 的自驱乘数 | `services/proactive.py` |
| `RHYTHM_OFF` | `1` | 表达节奏 off 的自驱乘数（=不漂移，不是不说话） | `services/proactive.py` |
| `RHYTHM_SCATTERED` | `1.05` | 表达节奏 scattered 的自驱乘数 | `services/proactive.py` |
| `RHYTHM_SUBTLE` | `0.9` | 表达节奏 subtle 的自驱乘数 | `services/proactive.py` |
| `RHYTHM_WILD` | `1.1` | 表达节奏 wild 的自驱乘数 | `services/proactive.py` |

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
| `CTX_EVIDENCE` | `500` | 证据块上限 | `services/prompt_builder.py` |
| `CTX_PROFILE` | `160` | 档案块上限（不含白月光） | `services/prompt_builder.py` |
| `CTX_SOFT_CAP` | `2700` | 非 long_doc 的硬截断 | `services/prompt_builder.py` |
| `CTX_SUMMARY` | `120` | 群摘要上限 | `services/prompt_builder.py` |
| `PERSONA_GROUP_CHARS` | `0` | 人格文本在群聊里最多带多少字（0 = 不限） | `services/persona.py` |
| `PERSONA_MAX_RELOADS_PER_DAY` | `2` | 改完人设不用重启容器，最多等 10 分钟就能生效。 | `services/persona.py` |
| `PROMPT_BUDGET` | `2200` | 整份提示词的**字符**预算（与 DESIGN §3.3 同单位） | `services/prompt_builder.py` |
| `RET_MEMORY_TOP` | `4` | 记忆块注入条数上限（0=不指定时用它） | `services/prompt_builder.py` |

## 模型调用

| 参数 | 默认 | 说明 | 生效位置 |
|---|---|---|---|
| `CHAT_HISTORY_TURNS` | `6` | 对话历史轮数 | `services/llm.py` |
| `CTX_HARD_CAP` | `12000` | long_doc 时的硬上限 | `services/llm.py` |
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
| `RET_TOOLS_TOP` | `6` | 工具检索条数 | `services/llm.py` |
| `SUMMARIZE_FALLBACK_MAX` | `1200` | 压缩失败时直接把原文给 DeepSeek（超过这个字数才截断，防止 prompt 爆炸） | `services/llm.py` |
| `THINK_BUDGET_FAST` | `12` | 快路径思考预算（秒） | `services/llm.py` |
| `THINK_BUDGET_SEC` | `60` | 一次回复的总思考预算（秒） | `services/llm.py` |
| `TOOL_RESULT_MAX` | `1500` | 单个工具结果回填给模型的最大字数 | `services/llm.py` |

## 用量监测

| 参数 | 默认 | 说明 | 生效位置 |
|---|---|---|---|
| `API_USAGE_KEEP_DAYS` | `60` | 旧版用量记录保留天数 | `services/api_usage.py` |
| `API_USAGE_TIMEOUT` | `8` | 旧版用量统计超时（秒） | `services/api_usage.py` |
| `LLM_DAILY_BUDGET` | `0` | 每日 token 预算（0 = 不刹车）；到 100% 她回一句固定话术然后当天不再消耗 | `services/llm_usage.py` |
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
| `AGENT_LIBRARY_TIMEOUT` | `14` | 图书馆 RAG 单次检索上限（秒，默认 14；实测 5~14s） | `services/agent_client.py` |
| `AGENT_RAG_TOOL_TIMEOUT` | `12` | 直接调 rag_search 工具的超时（秒，默认 12；smart-query 卡住时用它兜底） | `services/agent_client.py` |
| `AGENT_RECALL_TIMEOUT` | `3` | 算力机回忆接口超时（秒，默认 3；它在回复关键路径上） | `services/agent_client.py` |
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
| `RET_LIB_LATENCY_MS` | `3000` | 图书馆延迟目标（ms） | `services/agent_client.py` |
| `RET_WORLD_TOP` | `3` | 图书馆候选池条数 | `services/agent_client.py` |
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
| `ARCHIVE_ENABLED` | `1` | 个人档案库开关（1=开；本地 sqlite，只存旧事，不依赖 RAG） | `services/archive.py` |
| `ARCHIVE_MAX_CHARS` | `420` | 档案块最多多少字（进 prompt 的上限） | `services/archive.py` |
| `ARCHIVE_MIN_AGE_DAYS` | `30` | 档案库只收多少天以前的旧事（默认 30；近期记忆走 memdb） | `services/archive.py` |
| `ARCHIVE_TOP` | `4` | 档案库一次最多回几条 | `services/archive.py` |
| `ATMO_ENABLED` | `1` | 接话前是否读群气氛（1=开；只作提示，不改心情/好感） | `services/atmosphere.py` |
| `ATMO_MAX_CHARS` | `240` | 气氛块最多多少字（进 prompt 的上限） | `services/atmosphere.py` |
| `ATMO_MSGS` | `8` | 读气氛看最近多少句（默认 8，和主模型上下文同源同条数） | `services/atmosphere.py` |
| `ATMO_TIMEOUT` | `5` | 读气氛调用超时（秒），超时只丢这一块，不拖慢回复 | `services/atmosphere.py` |
| `ATMO_TTL` | `90` | 同一窗口的气氛缓存多久（秒），避免连发时重复调用 | `services/atmosphere.py` |
| `BUBBLE_DELAY_BASE_MS` | `300` | 多气泡之间的基础停顿（毫秒） | `services/aura_logic.py` |
| `BUBBLE_DELAY_MAX_MS` | `1600` | 多气泡停顿上限（毫秒，别让人等太久） | `services/aura_logic.py` |
| `BUBBLE_DELAY_PER_CHAR` | `90` | 多气泡停顿按字数叠加（毫秒/字） | `services/aura_logic.py` |
| `CTX_ANCHOR` | `200` | 引用锚点上限 | `services/ctx_budget.py` |
| `CTX_CUSTOM` | `250` | 自定义样本上限（替代 style） | `services/white_moon.py` |
| `CTX_ENFORCE` | `0` | 块级预算是否真裁剪（0=只告警，1=超限裁剪） | `services/ctx_budget.py` |
| `CTX_EXPRESSION` | `120` | 口癖块上限 | `services/ctx_budget.py` |
| `CTX_HISTORY` | `150` | 历史行上限 | `services/ctx_budget.py` |
| `CTX_LONG_DOC_EVIDENCE` | `6000` | long_doc 时证据上限 | `services/ctx_budget.py` |
| `CTX_LONG_DOC_LIBRARY` | `3000` | long_doc 时图书馆上限 | `services/ctx_budget.py` |
| `CTX_MULTITARGET` | `120` | 多目标块上限 | `services/ctx_budget.py` |
| `CTX_PERSONA` | `700` | 人格块上限（token） | `services/ctx_budget.py` |
| `CTX_POST_RULES` | `320` | 硬提醒块上限 | `services/ctx_budget.py` |
| `CTX_PROTOCOL` | `1500` | 协议块上限 | `services/ctx_budget.py` |
| `CTX_RHYTHM` | `180` | 表达节奏上限 | `services/ctx_budget.py` |
| `CTX_STATE` | `240` | 状态卡上限 | `services/ctx_budget.py` |
| `CTX_STYLE` | `250` | 对方原话样本上限 | `services/ctx_budget.py` |
| `CTX_STYLE_TOTAL` | `730` | 风格相关合计上限（= expression120+style250+rhythm180+white_moon180=730；custom 另算） | `services/ctx_budget.py` |
| `CTX_TOOLS` | `300` | 工具说明上限（仅 intent=tool） | `services/ctx_budget.py` |
| `CTX_WARN_SEC` | `600` | 同一个块超限告警的最小间隔（秒） | `services/ctx_budget.py` |
| `CTX_WHITE_MOON` | `180` | 白月光 1 行事实上限 | `services/ctx_budget.py` |
| `HARNESS_MAX_OUTPUT` | `3000` | Harness 桥回给 QQ 的最大字数 | `services/harness.py` |
| `HARNESS_TIMEOUT` | `300` | Harness 桥单次调用最长秒数 | `services/harness.py` |
| `LLM_BUDGET_SOFT_RATIO` | `0.9` | 日预算的「提前说休息」比例（0.9 = 用到 90% 就先提醒） | `services/budget_guard.py` |
| `LLM_DEFAULT_MAX_TOKENS` | `800` | 其它角色（判读/审核/表达…）默认输出 token 上限 | `services/providers.py` |
| `LLM_MAX_TOKENS` | `0` | 全局输出 token 覆盖（>0 时压过各角色默认；0=不覆盖） | `services/providers.py` |
| `LONG_DOC_ENABLED` | `1` | 长答模式总开关（1=用户明确要长答时放开输出/证据预算；0=回滚成普通回复） | `services/long_doc.py` |
| `LONG_DOC_EVIDENCE` | `8` | 长答模式检索候选条数（普通模式按 RET_EVIDENCE_MAX） | `services/long_doc.py` |
| `LONG_DOC_MAX_TOKENS` | `2000` | 长答模式单次生成的最大 token | `services/long_doc.py` |
| `LONG_DOC_PIECES` | `3` | 长答模式真正进 prompt 的证据篇数（DESIGN：3 篇） | `services/long_doc.py` |
| `LONG_DOC_PIECE_CHARS` | `1900` | 长答模式每篇证据最多多少**字符**（DESIGN：1900） | `services/long_doc.py` |
| `MAX_BUBBLES` | `2` | ⚠️ LEGACY（2026-11-26 合并进 OUT_BUBBLES_MAX）：一条回复最多拆几个气泡 | `` |
| `MEM_BAD_BOOST` | `1.15` | 负性记忆存储加成（坏比好强） | `services/memdb.py` |
| `MEM_OLD_FACTOR` | `0.5` | 超时间窗但被放行的记忆的降权系数（0.5 = 减半） | `services/memdb.py` |
| `MEM_SAL_FLOOR` | `0.045` | 倒 U 中段起点 w（u=low 时的值） | `services/memdb.py` |
| `MEM_SAL_HIGH` | `0.6` | 倒 U 高段起点（u>此值：极端冲击回落） | `services/memdb.py` |
| `MEM_SAL_LOW` | `0.15` | 倒 U 低段上界（u<此值：小冲击几乎不记） | `services/memdb.py` |
| `MEM_SAL_SLOPE_LOW` | `0.3` | 倒 U 低段斜率（w=slope×u） | `services/memdb.py` |
| `MEM_SAL_SLOPE_MID` | `2.1` | 倒 U 中段斜率 | `services/memdb.py` |
| `MEM_SAL_TAIL` | `0.6` | 倒 U 极端回落的终点 w（u=1 时） | `services/memdb.py` |
| `MEM_TAU_BOND` | `90` | bond 提取半衰期（天） | `services/memdb.py` |
| `MEM_TAU_DEFAULT` | `30` | 未知 kind 的提取半衰期（天） | `services/memdb.py` |
| `MEM_TAU_EVENT` | `30` | event 提取半衰期（天） | `services/memdb.py` |
| `MEM_TAU_FACT` | `365` | fact 提取半衰期（天） | `services/memdb.py` |
| `MEM_TAU_IMPRESSION` | `180` | impression 提取半衰期（天，她对这个人的印象；以前漏配 → 掉到 30） | `services/memdb.py` |
| `MEM_TAU_PREFERENCE` | `180` | preference 提取半衰期（天） | `services/memdb.py` |
| `MEM_TAU_STYLE` | `365` | style 提取半衰期（天） | `services/memdb.py` |
| `OCR_MAX_CROPS` | `3` | 图片文字增强：最多裁几个文字块送模型（越多越贵） | `services/ocr.py` |
| `OCR_MAX_SIDE` | `2200` | 图片文字增强：增强后最长边上限（像素） | `services/ocr.py` |
| `OCR_TIMEOUT` | `45` | 图片文字增强：单次视觉调用超时（秒） | `services/ocr.py` |
| `OCR_UPSCALE_MIN_W` | `1400` | 图片文字增强：宽度不足这个值就放大（小字靠这个救） | `services/ocr.py` |
| `PERSONA_BASELINE_V` | `0` | 人设心情基线 -0.3~0.3（衰减回落点和锚点都叠在它上面） | `services/persona_traits.py` |
| `PERSONA_EXPRESSIVENESS` | `0.5` | 人设表达欲 0~1（<0.3 视为冷/内敛，压借风格比例） | `services/white_moon.py` |
| `PERSONA_FORGIVENESS` | `0.5` | 人设宽容 0~1：道歉回补 = 0.03×(0.5+forgiveness) | `services/persona_traits.py` |
| `PERSONA_REACTIVITY` | `0.5` | 人设情绪反应 0~1：进 mood.observe 的 reactivity | `services/persona_traits.py` |
| `PERSONA_SENSITIVITY` | `1` | 人设敏感度乘数（再夹到 MOOD_SENS_MIN/MAX） | `services/persona_traits.py` |
| `PERSONA_TRUST_SPEED` | `0.5` | 人设信任速度 0~1：trust 正向步长 ×(0.5+trust_speed) | `services/persona_traits.py` |
| `PERSONA_WARMTH` | `0.5` | 人设温度 0~1：好感步长 = 0.04 + 0.12×warmth | `services/persona_traits.py` |
| `PIPELINE_BUDGET` | `16` | 回复前动态取数（心情/档案/记忆/图书馆）的总预算（秒） | `services/reply_pipeline.py` |
| `PIPE_METRICS` | `1` | 流水线是否把每轮各步耗时落盘（周报 p95 用） | `services/reply_pipeline.py` |
| `PIPE_STAGE_SAMPLES` | `500` | 流水线每步在内存里保留多少个耗时样本（算 p95） | `services/reply_pipeline.py` |
| `POKE_MAX_PER_WINDOW` | `3` | 戳一戳屏蔽：窗口内最多回几次（超过就不回） | `services/poke_guard.py` |
| `POKE_WINDOW_HOURS` | `2` | 戳一戳屏蔽的统计窗口（小时） | `services/poke_guard.py` |
| `REL_AFFINITY_BASE` | `0` | 好感基线 | `services/relationship.py` |
| `REL_AFFINITY_HALF_LIFE_DAYS` | `7` | 好感半衰期（天，再乘深度系数） | `services/relationship.py` |
| `REL_CONTACT_NORM_N` | `50` | 近 7 天互动次数归一化的分母 | `services/relationship.py` |
| `REL_DAILY_CAP` | `0.15` | 信任+尊重每日净变化上限（不是好感；好感用 AFFINITY_DAILY_CAP） | `services/relationship.py` |
| `REL_DEPTH_CONTACT_COEF` | `0.5` | 深度系数里联系频率的权重 | `services/relationship.py` |
| `REL_DEPTH_INTIMACY_COEF` | `2` | 深度系数里 intimacy 的权重 | `services/relationship.py` |
| `REL_GRUDGE_HALF_LIFE_DAYS` | `14` | 记仇半衰期（天） | `services/relationship.py` |
| `REL_INTIMACY_BASE` | `0` | 亲近基线 | `services/relationship.py` |
| `REL_NEG_BASE` | `1.3` | 负面权重基数（坏比好强） | `services/relationship.py` |
| `REL_NEG_SLOPE` | `0.6` | 负面权重里 forgiveness 的削弱系数 | `services/relationship.py` |
| `REL_NOVELTY_COEF` | `0.6` | 防刷：novelty = 1/(1+c×n) | `services/relationship.py` |
| `REL_RESPECT_BASE` | `0.3` | 尊重基线 | `services/relationship.py` |
| `REL_RESPECT_DOWN` | `0.1` | 尊重负向步长 | `services/relationship.py` |
| `REL_RESPECT_HALF_LIFE_DAYS` | `90` | 尊重半衰期（天） | `services/relationship.py` |
| `REL_RESPECT_UP` | `0.05` | 尊重正向步长 | `services/relationship.py` |
| `REL_TRUST_BASE` | `0.2` | 信任基线 | `services/relationship.py` |
| `REL_TRUST_DOWN` | `0.25` | 信任失信步长 | `services/relationship.py` |
| `REL_TRUST_HALF_LIFE_DAYS` | `45` | 信任半衰期（天） | `services/relationship.py` |
| `REL_TRUST_UP` | `0.08` | 信任正向步长 | `services/relationship.py` |
| `RET_FUSION_RRF` | `0` | 检索融合：0=加权求和，1=多路 RRF | `services/memdb.py` |
| `RET_PLAN_CAND` | `400` | 规划器一次最多取多少候选 | `services/memdb.py` |
| `RET_RECENCY_TAU_DAYS` | `30` | 新鲜度衰减时间常数（天） | `services/memdb.py` |
| `RET_RRF_K` | `60` | RRF 融合常数（1=开启多路 RRF） | `services/memdb.py` |
| `RET_STRENGTH_GAIN` | `0.1` | 命中后提取强度回补：r += gain×(1−r) | `services/memdb.py` |
| `RET_STYLE_TOP` | `3` | 风格候选池条数 | `services/white_moon.py` |
| `RET_TAG_TOP` | `5` | 每条记忆最多打几个 topic 标签 | `services/memdb.py` |
| `RET_W_BM25` | `1` | 打分：关键词**覆盖率**权重（键名沿用历史；sqlite 侧不是 BM25，没有 idf） | `services/memdb.py` |
| `RET_W_RECENCY` | `0.4` | 打分：时间新鲜度权重 | `services/memdb.py` |
| `RET_W_SALIENCE` | `0.5` | 打分：显著性 w（倒 U）权重 | `services/memdb.py` |
| `RET_W_STRENGTH` | `0.6` | 打分：提取强度权重 | `services/memdb.py` |
| `RET_W_STRUCT` | `1.2` | 打分：人/时间/情绪命中权重 | `services/memdb.py` |
| `RET_W_TAG` | `0.8` | 打分：标签重合权重 | `services/memdb.py` |
| `ROLEPLAY_MAX_TOKENS` | `4000` | 主聊天单次输出 token 上限（含 thinking；推理模型别低于 4000） | `services/providers.py` |
| `SEGMENT_HISTORY_KEEP` | `24` | 保留多少个「处理过的段」给上下文/翻历史 | `services/segments.py` |
| `SEGMENT_MAX_MSGS` | `40` | 群聊分段：最多攒多少条（防刷屏） | `services/segments.py` |
| `SEGMENT_MAX_SEC` | `30` | 群聊分段：最长攒多久必须处理（默认 30 秒） | `services/segments.py` |
| `SEGMENT_WAIT_SEC` | `3.5` | 群聊分段：安静多少秒算「这一段说完了」（默认 3.5） | `services/segments.py` |
| `SEG_INSERT_WINDOW` | `5` | 主动插话时只给最后几条（没有 ⭐ 的段；防止接旧话题） | `services/segments.py` |
| `STYLE_MMR_ENABLED` | `1` | 风格样本选择是否启用 MMR | `services/style_vault.py` |
| `STYLE_MMR_LAMBDA` | `0.7` | 风格 MMR 相关性权重（λ=0.7） | `services/style_vault.py` |
| `STYLE_VAULT_ENABLED` | `1` | 说话样本库：1=自动记每个人的原话（省流模式也记） | `services/style_vault.py` |
| `STYLE_VAULT_MAX_SAMPLES` | `200` | 说话样本库：每个人保留最近多少条原话 | `services/style_vault.py` |
| `STYLE_VAULT_MAX_UPLOAD_CHARS` | `4000` | 自定义风格：用户一次最多上传多少字 | `services/style_vault.py` |
| `STYLE_VAULT_SAMPLE_CHARS` | `60` | 说话样本库：单条原话最多存多少字 | `services/style_vault.py` |
| `WHITE_MOON_BORROW` | `0` | 白月光借风格总开关（0=只给 1 行事实，1=允许借句式） | `services/white_moon.py` |
| `WHITE_MOON_BORROW_CHARS` | `120` | 白月光借风格块的字数上限 | `services/white_moon.py` |
| `WHITE_MOON_BORROW_COLD_MAX` | `0.2` | 冷漠人设（expressiveness<0.3）的借风格上限 | `services/white_moon.py` |
| `WHITE_MOON_BORROW_RATIO` | `0.3` | 借风格比例（只允许 0 / 0.3 / 0.5，代码会夹） | `services/white_moon.py` |
| `WHITE_MOON_MIN_SCORE` | `3` | 白月光：至少多少分才算「最吃这个人的说话方式」 | `services/white_moon.py` |
| `WHITE_MOON_STYLE_ITEMS` | `3` | 白月光：注入几条学到的说法 | `services/white_moon.py` |
| `WHITE_MOON_STYLE_TTL` | `21600` | 白月光：说话方式多久重学一次（秒） | `services/white_moon.py` |

