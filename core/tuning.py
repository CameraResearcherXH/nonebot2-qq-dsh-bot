"""统一调参表（Single Source of Truth）。

本文件是**所有数值阈值**的唯一默认值来源。
代码里不再写 `os.getenv("X", "1.0")`，而是 `tuning.f("X")`；
环境变量同名覆盖仍然生效（.env 里写同名键即可）。

用法：
    from core import tuning
    tuning.f("STICKER_COLLECT_MIN")   # float，环境变量优先
    tuning.i("MAX_GROUP_RECENT")      # int

改值两种方式：
1. 在 `.env` 写同名环境变量（推荐，改完重启生效）；
2. 直接改本文件 `DEFAULTS`（等于改默认值，适合发行版）。

完整说明见 `docs/tuning.md`（`python3 scripts/dump_tuning.py --write` 生成）。
"""
import os


def _env(key: str):
    v = os.getenv(key)
    return v if v is not None and str(v).strip() != "" else None


def f(key: str) -> float:
    """float 型：环境变量 > DEFAULTS。"""
    raw = _env(key)
    if raw is None:
        raw = DEFAULTS.get(key, 0.0)
    try:
        return float(raw)
    except Exception:
        return float(DEFAULTS.get(key, 0.0))


def i(key: str) -> int:
    """int 型。"""
    return int(f(key))


def raw(key: str) -> str:
    """原始字符串（不做类型转换）。"""
    v = _env(key)
    return v if v is not None else str(DEFAULTS.get(key, ""))


def get(key: str):
    """按 DEFAULTS 的类型自动转换。"""
    d = DEFAULTS.get(key)
    if isinstance(d, int):
        return i(key)
    return f(key)


def describe() -> list:
    """给 /调参 与 docs 用：[(key, 当前值, 默认值, 类型, 说明, 归属文件), ...]。"""
    out = []
    for k, d in DEFAULTS.items():
        cur = _env(k)
        out.append((k, cur if cur is not None else d, d,
                    "int" if isinstance(d, int) else "float",
                    DOCS.get(k, ""), FILES.get(k, "")))
    return out


# ============================================================
# 默认值：键名 = 环境变量名；注释 = 这个值在管什么
# ============================================================
DEFAULTS = {

    # ---------- 心情判读 / 好感 ----------
    'AFFINITY_GAIN': 0.05,
    'AFFINITY_DAILY_CAP': 0.15,
    # 「印象最深的一件事」：记忆里 |Δ| 达到这个值才算「记最深」的事
    'BOND_EVENT_MIN_DELTA': 0.15,

    # ---------- 档案 / 好感 ----------
    'AFFINITY_TREND_WINDOW_SEC': 1800.0,

    # ---------- 算力机 ----------
    'AGENT_RECALL_TIMEOUT': 3.0,
    'AGENT_REFLECT_QUEUE_TIMEOUT': 600.0,
    'AGENT_REFLECT_TIMEOUT': 180.0,

    # ---------- 用量监测 ----------
    'API_USAGE_KEEP_DAYS': 60,
    'API_USAGE_TIMEOUT': 8.0,

    # ---------- 记忆迁移 ----------
    'ARCHIVE_HOUR': 3,
    'ARCHIVE_MINUTE': 0,
    'ARCHIVE_WINDOW_DAYS': 30.0,

    # ---------- 档案 / 好感 ----------
    'AVATAR_PX': 160,
    'AVATAR_TTL_DAYS': 7.0,
    'BOND_TTL_DAYS': 60.0,

    # ---------- 回复 / 限流 / 主动插话 ----------
    'BURST_MSG_COUNT': 4,
    'BURST_WINDOW_SEC': 30.0,
    # 戳一戳屏蔽：同一个人 POKE_WINDOW_HOURS 内戳超过 POKE_MAX_PER_WINDOW 次就不回
    'POKE_MAX_PER_WINDOW': 3,
    'POKE_WINDOW_HOURS': 2.0,
    # DeepSeek Harness 桥：单次最长秒数 / 回 QQ 的最大字数
    'HARNESS_TIMEOUT': 300.0,
    'HARNESS_MAX_OUTPUT': 3000,

    # ---------- 模型调用 ----------
    'CHAT_HISTORY_TURNS': 6,

    # ---------- 安静开关 ----------
    'CHAT_OFF_MAX_HOURS': 72.0,

    # ---------- 对话状态 ----------
    'CONV_STATE_MAX_CHARS': 420,
    'CONV_STATE_MIN_INTERVAL': 150.0,
    'CONV_STATE_TIMEOUT': 20.0,

    # ---------- 算力机 ----------
    'COVER_FULL_TTL': 600.0,
    'COVER_FULL_WAIT_SEC': 1800.0,
    'COVER_MAX_CONCURRENT': 2,
    'COVER_RVC_WAIT_SEC': 900.0,
    'COVER_SPLIT_WAIT_SEC': 900.0,
    'COVER_VOICE_MAX_MB': 3.0,
    'COVER_VOICE_MAX_SEC': 58.0,
    'COVER_PITCH_MAX_TOLERANCE': 0.1,

    # ---------- 回复 / 限流 / 主动插话 ----------
    'CTX_LONG_MSG_CHARS': 150,
    'CTX_LONG_MSG_CHARS_MAX': 800,
    'CTX_LONG_MSG_KEEP': 3,

    # ---------- 表情 ----------
    'CURATOR_TIMEOUT': 60.0,

    # ---------- 记忆迁移 ----------
    'DAILY_SUMMARY_KEEP_DAYS': 14.0,
    'DAILY_SUMMARY_MIN_MSGS': 6.0,

    # ---------- 回复 / 限流 / 主动插话 ----------
    'DEBOUNCE_AT': 0.8,
    'DEBOUNCE_BARE_IMAGE': 4.0,
    'DEBOUNCE_MAX': 8.0,
    'DEBOUNCE_OTHER': 0.5,
    'DEBOUNCE_SAME': 1.2,

    # ---------- 记忆迁移 ----------
    'DIALOGUE_IMG_SIDE': 800,
    'DIALOGUE_KEEP_DAYS': 14,

    # ---------- 回复 / 限流 / 主动插话 ----------
    'DIGEST_AFTER': 8,

    # ---------- 画图 ----------
    'DRAW_MAX_CONCURRENT': 2,
    'DRAW_WAIT_SEC': 360.0,

    # ---------- 对话状态 ----------
    'EXPRESSION_EVERY': 40,
    'EXPRESSION_MAX_PER_SCOPE': 30,
    'EXPRESSION_MIN_INTERVAL': 600.0,
    'EXPRESSION_TIMEOUT': 25.0,

    # ---------- 算力机 ----------
    'F0_PERIODICITY_MIN': 0.45,

    # ---------- 回复 / 限流 / 主动插话 ----------
    'FOCUS_FOLLOW_COUNT': 3,
    'FOCUS_JUDGE_TIMEOUT': 3.0,
    'FOCUS_NEG_STREAK_STOP': 2,
    'FOCUS_TIMEOUT': 120.0,

    # ---------- 模型调用 ----------
    'GLM_MAX_TOKENS': 800,
    'GLM_TIMEOUT': 30.0,

    # ---------- 回复 / 限流 / 主动插话 ----------
    'GROUP_MAX_HANZI': 20,
    'GROUP_MUTE_SEC': 900.0,
    'GROUP_SEND_LIMIT': 9,
    'GROUP_SEND_MAX_WAIT': 15.0,

    # ---------- 白月光模式 ----------
    'WHITE_MOON_MIN_SCORE': 3,
    'WHITE_MOON_STYLE_ITEMS': 3,
    'WHITE_MOON_STYLE_TTL': 21600.0,

    # ---------- 工具分发 ----------
    'HEARTBEAT_TIMEOUT_SEC': 90,
    'HISTORY_MAX': 20,

    # ---------- 模型调用 ----------
    'HIST_LINE_BASE': 80,
    'HIST_LINE_LONG': 500,
    'HIST_LONG_AT': 150,
    'HIST_TOTAL_BUDGET': 3000,

    # ---------- 视觉 ----------
    'IMAGE_DEDUP_HOURS': 24.0,

    # ---------- 模型调用 ----------
    'LIBRARY_COOLDOWN': 600.0,

    # ---------- 用量监测 ----------
    'LLM_DAILY_BUDGET': 0.0,

    # ---------- 模型调用 ----------
    'LLM_TRACE_KEEP_DAYS': 7,
    'LLM_TRACE_MAX_CHARS': 60000,

    # ---------- 用量监测 ----------
    'LLM_USAGE_KEEP_DAYS': 90,

    # ---------- 模型调用 ----------
    'LOCAL_MEMORY_DAYS': 30.0,

    # ---------- 回复 / 限流 / 主动插话 ----------
    'LONG_FORWARD_MIN': 120,

    # ---------- 世界书 ----------
    'LORE_BUDGET': 520,
    'LORE_BUDGET_POST': 260,
    'LORE_MAX_ENTRIES': 200,
    'LORE_SCAN_DEPTH': 4,

    # ---------- 回复 / 限流 / 主动插话 ----------
    'MAX_BUBBLES': 2,
    'MAX_GROUP_RECENT': 16,
    # 多气泡之间的「正在打字」停顿（2026-11-24 用户：回复等太久）
    #   以前 350 + 110/字、上限 4.5s —— 一句 30 字就要等 3.6s。
    'BUBBLE_DELAY_BASE_MS': 300,
    'BUBBLE_DELAY_PER_CHAR': 90,
    'BUBBLE_DELAY_MAX_MS': 1600,

    # ---------- 工具分发 ----------
    'MAX_REPLY_CHARS': 200,

    # ---------- 模型调用 ----------
    'MAX_REQ_ROUNDS': 6,
    'MAX_REQ_ROUNDS_FAST': 2,
    'MAX_TOOL_TURNS': 6,

    # ---------- 记忆迁移 ----------
    'MEMORY_REVISIT_AGE_DAYS': 7.0,
    'MEMORY_REVISIT_IDLE_DAYS': 3.0,

    # ---------- 回复 / 限流 / 主动插话 ----------
    'MERGE_WINDOW': 0.6,
    # 群聊分段：一段话说完（安静 N 秒）再交给她，别一条一回
    # 2026-11-24：6 → 3.5（用户反馈等太久）。@ 她/叫名字时走 SEGMENT_WAIT_FAST。
    'SEGMENT_WAIT_SEC': 3.5,
    'SEGMENT_WAIT_FAST': 1.2,
    'SEGMENT_MAX_SEC': 30.0,
    'SEGMENT_MAX_MSGS': 40,
    'SEGMENT_HISTORY_KEEP': 24,

    # ---------- 心情判读 / 好感 ----------
    'MOOD_GLOBAL_SHARE': 0.35,
    'MOOD_IDLE_AFTER': 300.0,
    'MOOD_IDLE_STEP': 1.0,
    'MOOD_IDLE_TICK': 300.0,
    # 心情峰值事件：只在这段时间内找「涨/跌得最狠的那件事」，过期自动不再提。
    'MOOD_PEAK_WINDOW_SEC': 7200.0,
    # 太小的波动不进峰值候选（否则一堆 ±0.02 的闲聊会顶掉真正的大事）
    'MOOD_PEAK_MIN_DELTA': 0.03,
    'MOOD_JUDGE_CTX': 8,
    'MOOD_JUDGE_FRESH_SEC': 600.0,
    'MOOD_JUDGE_MIN_INTERVAL': 60.0,
    'MOOD_JUDGE_REFRESH': 300.0,
    'MOOD_JUDGE_SMOOTH': 0.35,
    'MOOD_MAX_STEP': 0.3,
    # 单次涨幅上限（设计 §13.1 = 0.50，只是防模型一次给极端值的护栏）。
    # ⚠️ 不要靠调小它来「防一边倒」：0.2 会让 §13.3 的 0.10/0.25/0.50
    #    刻痕阈值永远到不了 💥💥/💥💥💥。真正的收紧在 Δ 入口的噪音丢弃。
    'MOOD_MAX_DELTA': 0.5,
    'MOOD_LABEL_STEP': 0.2,
    'MOOD_JUDGE_TIMEOUT': 12.0,
    'MOOD_LABEL_MAX': 60,
    'MOOD_LABEL_TTL_HOURS': 12.0,
    'MOOD_MAX_SESSIONS': 50,
    'MOOD_THINK_KEEP': 30,
    # 心情低到这个值（-100~100 口径）以下，关注期就不再「追着接话」，
    # 只回明确 @ 她 / 喊她名字的（用户 2026-11-24：「心情没那么高但一直在接」）。
    'MOOD_FOLLOW_MIN': -35.0,
    # 心情回到 0 的半衰期（小时）。12h 太慢：群里一直有负向小 Δ 时，
    # 数值会钉死在 -1（用户 2026-11-22：「明明是正负 1，结果被击穿」）。
    'MOOD_HALF_LIFE_HOURS': 12.0,
    # ---- Δ 入口护栏（2026-11-22：这才是「防一边倒」的正确位置）----
    # 噪音丢弃：|Δ| 小于这个值 → 当成 0，不动数值/好感/刻痕/群心情
    'MOOD_NOISE_DELTA': 0.03,
    # 她没说话（被动观察）时，最多按 ±0.03 记 —— 旁观刷图不该把心情拉下去
    'MOOD_PASSIVE_MAX_DELTA': 0.03,
    # 不是冲她来（别人之间 / 群气氛）且没被叫 → 最多 ±0.05
    'MOOD_ENV_MAX_DELTA': 0.05,
    # 记忆刻痕 / 档案只收「明显动过」的事：|Δ| 低于这个值不进刻痕
    'MEMORY_EMOTION_MIN_DELTA': 0.10,

    # ---------- 回复 / 限流 / 主动插话 ----------
    'MOOD_WATCH_SEC': 60.0,

    # ---------- 工具分发 ----------
    'NAPCAT_APPROVE_TTL': 600,
    'NAPCAT_TOOL_MAX_PER_MIN': 20,
    'NAPCAT_TOOL_RESULT_MAX': 550,

    # ---------- 回复审核 ----------
    'PEER_MERGE_MAX': 25.0,
    'PEER_MERGE_WINDOW': 6.0,

    # ---------- 回复 / 限流 / 主动插话 ----------
    'PEER_REPLY_COOLDOWN': 180.0,
    'PEER_REPLY_PROB': 0.15,

    # ---------- 回复审核 ----------
    'PEER_WAIT': 90.0,
    'PEER_WINDOW': 240.0,

    # ---------- 人格 ----------
    'PERSONA_GROUP_CHARS': 0,
    'PERSONA_MAX_RELOADS_PER_DAY': 2,

    # ---------- 回复审核 ----------
    'PREFLIGHT_MIN_CHARS': 6,
    'PREFLIGHT_TIMEOUT': 2.5,

    # ---------- 回复 / 限流 / 主动插话 ----------
    'PRIVATE_MAX_HANZI': 20,

    # ---------- 主动私聊 ----------
    'PROACTIVE_CHECK_SEC': 600.0,
    'PROACTIVE_COOLDOWN_H': 6.0,
    'PROACTIVE_IDLE_MAX_H': 72.0,
    'PROACTIVE_IDLE_MIN_H': 2.0,
    'PROACTIVE_MAX_PER_DAY': 2,
    'PROACTIVE_MIN_INTIMACY': 0.45,
    'PROACTIVE_MIN_SCORE': 0.9,
    'PROACTIVE_QUIET_END': 9,
    'PROACTIVE_QUIET_START': 1,

    # ---------- 算力机 ----------
    'PROBE_UPLOAD_TTL': 900.0,

    # ---------- 档案 / 好感 ----------
    'PROFILE_CARD_EVENTS': 10,
    'PROFILE_MINE_INTERVAL_H': 20.0,
    'PROFILE_MINE_MIN_MSGS': 25,
    'PROFILE_MINE_SAMPLE': 40,
    'PROFILE_MINE_TIMEOUT': 25.0,

    # ---------- 人格 ----------
    'PROMPT_BUDGET': 2200,

    # ---------- 安静开关 ----------
    'QUIET_ADVISOR_COOLDOWN_SEC': 900.0,
    'QUIET_ADVISOR_WINDOW_SEC': 1800.0,
    'QUIET_GROUP_MIN': 0.55,
    'QUIET_PERSON_MIN': 0.45,
    'QUIET_THAW_GRACE_SEC': 1800.0,

    # ---------- 群心情（独立于整体心情的「这个群让我舒不舒服」）----------
    # 5 小时窗口：群里的 Δ 累加，正值可以抵消负值（不是只统计一个方向）
    'GROUP_MOOD_WINDOW_SEC': 18000.0,
    # 某个人在 5 小时里净让心情掉了多少 → 单独不理他（含 @）
    'GROUP_MOOD_PERSON_DROP': 0.4,
    # 整个群净掉了多少 + 至少几个人 → 判定「群聊导致的不愉快」
    'GROUP_MOOD_GROUP_DROP': 0.7,
    'GROUP_MOOD_MIN_PEOPLE': 3,
    # 小于这个 |Δ| 的闲聊不进群心情（防噪声）
    'GROUP_MOOD_MIN_DELTA': 0.03,
    # 单条群心情事件的上限：一次判读最多按 ±0.2 记，防止小事先刷满 -1
    'GROUP_MOOD_MAX_DELTA': 0.2,
    # 一个人净 Δ 达到多少算「喜欢 / 讨厌」（算人数和比例用）
    'GROUP_MOOD_LIKE_MIN': 0.05,
    # 每个群最多留多少条事件（5 小时窗口内够用）
    'GROUP_MOOD_MAX_EVENTS': 400,
    # 群心情对「她愿不愿意在这个群多说话」的乘数范围
    'GROUP_MOOD_CHAT_MIN': 0.55,
    'GROUP_MOOD_CHAT_MAX': 1.35,

    # ---------- 回复 / 限流 / 主动插话 ----------
    'QUOTE_MAX_CHARS': 500,
    'RANDOM_CHAT_INTERVAL': 180,
    'RANDOM_CHAT_MIN_MSGS': 3,
    'RANDOM_CHAT_PROB': 0.35,
    'RANDOM_CHAT_VOICE_RATE': 0.3,

    # ---------- 记忆迁移 ----------
    'RECALL_TEXT_MAX': 600,

    # ---------- 回复审核 ----------
    'REFEREE_CTX_LINES': 10,
    'REFEREE_MAX_PER_HOUR': 60,
    'REFEREE_MIN_CHARS': 12,

    # ---------- 记忆迁移 ----------
    'REFLECT_HOUR': 20,
    'REFLECT_MINUTE': 0,
    'REFLECT_WINDOW_DAYS': 2.0,

    # ---------- 回复 / 限流 / 主动插话 ----------
    'REPLY_DEDUP_RATIO': 0.72,
    'REPLY_DEDUP_SEC': 60.0,
    'REPLY_INTERVAL': 10.0,
    # 刚回过这个群 → 紧接着的非点名消息先不单独回（防「同一件事回两遍」）
    'GROUP_REPLY_COOLDOWN': 45.0,

    # ---------- 记忆迁移 ----------
    'ROSTER_TTL': 600.0,

    # ---------- 算力机 ----------
    'RVC_INDEX_RATE_FEMALE': 0.5,
    'RVC_INDEX_RATE_MALE': 0.5,
    'RVC_PITCH_FEMALE': 12,
    'RVC_PITCH_MALE': -12,
    'RVC_PITCH_MAX': 24,
    'RVC_PITCH_MIN': -24,
    'RVC_PROTECT_FEMALE': 0.4,
    'RVC_PROTECT_MALE': 0.4,
    'RVC_RMS_FEMALE': 0.8,
    'RVC_RMS_MALE': 0.8,
    'RVC_SAME_GENDER_MAX_SEMIS': 3,
    'RVC_TARGET_F0': 220.0,
    'RVC_VOICE_F0_MAX': 880.0,
    'RVC_VOICE_F0_MIN': 130.0,

    # ---------- 回复审核 ----------
    'SCENE_AUDIT_MAX_MSGS': 20,
    'SCENE_AUDIT_MIN_INTERVAL': 4.0,
    'SCENE_AUDIT_TIMEOUT': 6.0,

    # ---------- 档案 / 好感 ----------
    'SCENE_CARD_LINES': 20,

    # ---------- 记忆迁移 ----------
    'SCENE_SEED_RETRY': 30.0,

    # ---------- 回复审核 ----------
    'SCENE_SUMMARY_MAX_CHARS': 160,
    'SCENE_SUMMARY_TTL': 180.0,

    # ---------- 记忆迁移 ----------
    'SCENE_WINDOW_MSGS': 20,

    # ---------- 工具分发 ----------
    'SELF_TOOL_FSIZE_MB': 200,
    'SELF_TOOL_MEM_MB': 1536,
    'SELF_TOOL_READ_MAX': 1500,
    'SELF_TOOL_TIMEOUT': 60.0,

    # ---------- 回复 / 限流 / 主动插话 ----------
    'SHORT_QUERY_WINDOW_SEC': 120,
    'SLEEP_HOUR_END': 8,
    'SLEEP_HOUR_START': 0,

    # ---------- 表情 ----------
    'STICKER_COLLECT_MIN': 0.25,
    'STICKER_MAX': 1000,
    'STICKER_MAX_SIDE': 512,
    'STICKER_PRUNE_BUFFER': 20,
    'STICKER_TAG_SOFT_CAP': 120,
    'STICKER_WAIT_TTL': 300.0,

    # ---------- 模型调用 ----------
    'SUMMARIZE_FALLBACK_MAX': 1200,
    'THINK_BUDGET_FAST': 12.0,
    'THINK_BUDGET_SEC': 60.0,
    'TOOL_RESULT_MAX': 1500,

    # ---------- 视觉 ----------
    'TRACE_FLAT_STD': 10.0,
    'TRACE_MAX_PER_DAY': 80,
    'TRACE_MIN_SHARE': 0.35,
    'TRACE_MIN_SIM': 0.75,
    'TRACE_MIN_VOTES': 2,
    'TRACE_SINGLE_SIM': 0.95,
    'TRACE_STRONG_SIM': 0.9,

    # ---------- 回复 / 限流 / 主动插话 ----------
    'TTS_PICKUP_TIMEOUT': 40,
    'TTS_WAIT_TOTAL': 240,
    'UPLOAD_MAX_MB': 200.0,
    'UPLOAD_WAIT_SEC': 180.0,

    # ---------- 视觉 ----------
    'VIDEO_FRAME_INTERVAL': 5.0,
    'VIDEO_MAX_FRAMES': 60,
    'VIDEO_MAX_FRAME_DESC': 100,
    'VIDEO_MAX_KEY_FRAMES': 12,
    'VISION_CACHE_MAX': 800,
    'VISION_COOLDOWN': 300.0,
    'VISION_LLM_MAX_PER_HOUR': 12,
    'VISION_MAX_ATTEMPTS': 6,
    # 2026-11-24：1024 → 1568。截图/表格里的字 1024 长边会被压糊，
    # 视觉模型就说「看不清」（用户实测反馈）。
    'VISION_MAX_SIDE': 1568,
    'VISION_TIMEOUT': 40.0,
    'VISION_VERIFY_MAX_PER_HOUR': 20,
}


# 说明（与 DEFAULTS 同键）
DOCS = {
    'AFFINITY_GAIN': '心情变化 Δ 转好感的系数（0.05 ≈ 一次大事件 +0.05）',
    'AFFINITY_DAILY_CAP': '同一个人每天好感净变化上限（0.15，防「持续跌」一天到底）',
    'BOND_EVENT_MIN_DELTA': '「印象最深的一件事」的门槛：记忆 |Δ| 达到多少才算（0.15）',
    'AFFINITY_TREND_WINDOW_SEC': '好感走势的统计窗口（秒）',
    'AGENT_RECALL_TIMEOUT': '算力机回忆接口超时（秒，默认 3；它在回复关键路径上）',
    'AGENT_REFLECT_QUEUE_TIMEOUT': '算力机反思任务排队超时（秒）',
    'AGENT_REFLECT_TIMEOUT': '算力机反思任务执行超时（秒）',
    'API_USAGE_KEEP_DAYS': '旧版用量记录保留天数',
    'API_USAGE_TIMEOUT': '旧版用量统计超时（秒）',
    'ARCHIVE_HOUR': '每日归档执行的小时（0-23）',
    'ARCHIVE_MINUTE': '每日归档执行的分钟（0-59）',
    'ARCHIVE_WINDOW_DAYS': '归档覆盖最近多少天',
    'AVATAR_PX': '头像生成尺寸（像素）',
    'AVATAR_TTL_DAYS': '头像缓存保留天数',
    'BOND_TTL_DAYS': '相处印象（档案）保留天数',
    'BURST_MSG_COUNT': '窗口内多少条算刷屏',
    'BURST_WINDOW_SEC': '高刷检测窗口（秒）',
    'POKE_MAX_PER_WINDOW': '戳一戳屏蔽：窗口内最多回几次（超过就不回）',
    'POKE_WINDOW_HOURS': '戳一戳屏蔽的统计窗口（小时）',
    'HARNESS_TIMEOUT': 'Harness 桥单次调用最长秒数',
    'HARNESS_MAX_OUTPUT': 'Harness 桥回给 QQ 的最大字数',
    'CHAT_HISTORY_TURNS': '对话历史轮数',
    'CHAT_OFF_MAX_HOURS': '安静最长持续小时数（超时自动解除）',
    'CONV_STATE_MAX_CHARS': '对话状态摘要最大字数',
    'CONV_STATE_MIN_INTERVAL': '对话状态更新的最小间隔（秒）',
    'CONV_STATE_TIMEOUT': '对话状态调用超时（秒）',
    'COVER_FULL_TTL': '翻唱成品缓存时间（秒）',
    'COVER_FULL_WAIT_SEC': '等翻唱成品的最长时间（秒）',
    'COVER_MAX_CONCURRENT': '同时进行的翻唱任务上限',
    'COVER_RVC_WAIT_SEC': '等 RVC 变声的超时（秒）',
    'COVER_SPLIT_WAIT_SEC': '等人声分离的超时（秒）',
    'COVER_VOICE_MAX_MB': '翻唱输入音频大小上限（MB）',
    'COVER_VOICE_MAX_SEC': '翻唱输入音频时长上限（秒）',
    'COVER_PITCH_MAX_TOLERANCE': '女声源允许超出模型音域上限的比例（0.10 = 10%）',
    'CTX_LONG_MSG_CHARS': '多少字算长消息（上下文补齐用）',
    'CTX_LONG_MSG_CHARS_MAX': '单条长消息最多给多少字',
    'CTX_LONG_MSG_KEEP': '长消息最多补几条',
    'CURATOR_TIMEOUT': '表情策展模型超时（秒）',
    'DAILY_SUMMARY_KEEP_DAYS': '日结文件保留天数',
    'DAILY_SUMMARY_MIN_MSGS': '当天至少多少条消息才写日结',
    'DEBOUNCE_AT': '被 @ / 喊名字时的等待（秒），越短越像立刻回应',
    'DEBOUNCE_BARE_IMAGE': '只发一张图、没有文字时的等待（等图下载）',
    'DEBOUNCE_MAX': '一批消息最多等多久（秒），防止一直等',
    'DEBOUNCE_OTHER': '换人说话时的等待（秒）',
    'DEBOUNCE_SAME': '同一个人连续发消息时的等待（秒）',
    'DIALOGUE_IMG_SIDE': '对话日志里图片缩略图边长（像素）',
    'DIALOGUE_KEEP_DAYS': '对话原文保留天数',
    'DIGEST_AFTER': '群里累积多少条后做一次小结',
    'DRAW_MAX_CONCURRENT': '同时进行的画图任务上限',
    'DRAW_WAIT_SEC': '等画图结果的最长时间（秒）',
    'EXPRESSION_EVERY': '每多少轮更新一次语气/表达',
    'EXPRESSION_MAX_PER_SCOPE': '每个会话最多保留多少条表达记录',
    'EXPRESSION_MIN_INTERVAL': '表达更新的最小间隔（秒）',
    'EXPRESSION_TIMEOUT': '表达模型超时（秒）',
    'F0_PERIODICITY_MIN': 'F0 周期度阈值：低于它的帧当噪声丢弃（0.45）',
    'FOCUS_FOLLOW_COUNT': '被叫住后额外跟几句',
    'FOCUS_JUDGE_TIMEOUT': '关注判定超时（秒）',
    'FOCUS_NEG_STREAK_STOP': '连续几次负面就停止关注',
    'FOCUS_TIMEOUT': '关注会话的有效期（秒）',
    'GLM_MAX_TOKENS': 'GLM 单次最大输出 token',
    'GLM_TIMEOUT': 'GLM 调用超时（秒）',
    'GROUP_MAX_HANZI': '群聊单条回复的字数上限（按显示宽度算：汉字 1 / 半角 0.5）',
    'GROUP_MUTE_SEC': '被群友嫌弃后静默多少秒',
    'GROUP_SEND_LIMIT': '群发配额：每窗口最多几条',
    'GROUP_SEND_MAX_WAIT': '配额不足时最多等多久（秒）',
    'WHITE_MOON_MIN_SCORE': '白月光：至少多少分才算「最吃这个人的说话方式」',
    'WHITE_MOON_STYLE_ITEMS': '白月光：注入几条学到的说法',
    'WHITE_MOON_STYLE_TTL': '白月光：说话方式多久重学一次（秒）',
    'HEARTBEAT_TIMEOUT_SEC': '工具分发心跳超时（秒）',
    'HISTORY_MAX': '模型侧历史消息条数',
    'HIST_LINE_BASE': '历史里短消息给多少字',
    'HIST_LINE_LONG': '历史里长消息给多少字',
    'HIST_LONG_AT': '历史里超过多少字算长消息',
    'HIST_TOTAL_BUDGET': '历史总字数预算',
    'IMAGE_DEDUP_HOURS': '群友：「这肥鱼魔了」。60 秒 DEDUP 拦不住这种跨 20 分钟的复读。',
    'LIBRARY_COOLDOWN': '同一梗/素材的冷却时间（秒）',
    'LLM_DAILY_BUDGET': '每日 token 预算（0 = 不告警）',
    'LLM_TRACE_KEEP_DAYS': '模型调用 trace 保留天数',
    'LLM_TRACE_MAX_CHARS': '单条 trace 最大字符数',
    'LLM_USAGE_KEEP_DAYS': '用量明细保留天数',
    'LOCAL_MEMORY_DAYS': '本地记忆回溯天数',
    'LONG_FORWARD_MIN': '超过多少字改用合并转发',
    'LORE_BUDGET': '世界书前置注入的字数预算',
    'LORE_BUDGET_POST': '世界书后置注入的字数预算',
    'LORE_MAX_ENTRIES': '世界书最多命中多少条',
    'LORE_SCAN_DEPTH': '世界书扫描深度（条）',
    'BUBBLE_DELAY_BASE_MS': '多气泡之间的基础停顿（毫秒）',
    'BUBBLE_DELAY_PER_CHAR': '多气泡停顿按字数叠加（毫秒/字）',
    'BUBBLE_DELAY_MAX_MS': '多气泡停顿上限（毫秒，别让人等太久）',
    'MAX_BUBBLES': '一条回复最多拆几个气泡',
    'MAX_GROUP_RECENT': '群聊上下文保留多少条消息（16 = 修过长消息被挤出窗口的问题）',
    'MAX_REPLY_CHARS': '.env 里留着 MAX_HISTORY 也不会有任何作用。',
    'MAX_REQ_ROUNDS': '单次生成最多请求几轮',
    'MAX_REQ_ROUNDS_FAST': '快路径最多请求几轮',
    'MAX_TOOL_TURNS': '一次对话里最多几轮工具调用',
    'MEMORY_REVISIT_AGE_DAYS': '回访记忆：至少多少天前的事',
    'MEMORY_REVISIT_IDLE_DAYS': '回访记忆：至少多久没提过',
    'MERGE_WINDOW': '合并转发判定窗口（秒）',
    'SEGMENT_WAIT_SEC': '群聊分段：安静多少秒算「这一段说完了」（默认 3.5）',
    'SEGMENT_WAIT_FAST': '群聊分段：@ 她 / 叫名字时只等多久（默认 1.2，别让她慢半拍）',
    'SEGMENT_MAX_SEC': '群聊分段：最长攒多久必须处理（默认 30 秒）',
    'SEGMENT_MAX_MSGS': '群聊分段：最多攒多少条（防刷屏）',
    'SEGMENT_HISTORY_KEEP': '保留多少个「处理过的段」给上下文/翻历史',
    'MOOD_GLOBAL_SHARE': '全局心情占会话心情的权重（0~1）',
    'MOOD_IDLE_AFTER': '空闲多少秒后心情开始衰减（默认 300 = 5 分钟；之后每 5 分钟向 0 走一步）',
    'MOOD_IDLE_STEP': '每步走多少点（内部 -100~100 口径，1.0 ≈ 外口径 0.01）',
    'MOOD_IDLE_TICK': '衰减步长（秒）：每这么久向 0 走一步（默认 300 = 5 分钟走 0.01）',
    'MOOD_PEAK_WINDOW_SEC': '心情峰值事件的保质期（秒，默认 7200 = 2 小时；每件事最多挂 2 小时）',
    'MOOD_PEAK_MIN_DELTA': '进入「峰值事件」候选的最小 |Δ|（0.03，防小事顶掉大事）',
    'MOOD_JUDGE_CTX': '判读时带多少条上下文（8 = 5~8 条，和主模型上下文同源同条数）',
    'MOOD_JUDGE_FRESH_SEC': '判读结果有效期（秒），过期不当「现在」',
    'MOOD_JUDGE_MIN_INTERVAL': '两次判读的最小间隔（秒），防刷屏时来回震荡',
    'MOOD_JUDGE_REFRESH': '她没主动写心情时，多久用群聊重新判一次（秒）。0 = 只在她说的时候判',
    'MOOD_JUDGE_SMOOTH': '判读值平滑系数（0.35 = 每次只走 35% 的差，给心情惯性）',
    'MOOD_MAX_STEP': '单次判读最多改变多少心情（外口径，0.30 = 30 点），防一句话翻脸',
    'MOOD_MAX_DELTA': '单次「涨幅」上限（设计 0.50，只是护栏；防一边倒靠噪音丢弃）',
    'MOOD_LABEL_STEP': '心情名词的分档步长（0.2 = 每 0.2 一个名词）',
    'MOOD_JUDGE_TIMEOUT': '判读调用超时（秒）',
    'MOOD_LABEL_MAX': '心情词最多保留多少个',
    'MOOD_LABEL_TTL_HOURS': '心情词的有效期（小时），过期不再当「现在」',
    'MOOD_MAX_SESSIONS': '最多保留多少个会话的心情（防字典无限长）',
    'MOOD_THINK_KEEP': '判读心理活动保留多少条',
    'MOOD_FOLLOW_MIN': '心情低于这个值就不再追着接话（只回明确叫她）',
    'MOOD_HALF_LIFE_HOURS': '心情回到 0 的半衰期（小时，设计默认 12）',
    'MOOD_NOISE_DELTA': 'Δ 噪音门槛：|Δ| 小于它视为 0（0.03，不动数值/好感/刻痕）',
    'MOOD_PASSIVE_MAX_DELTA': '她没说话（被动观察）时单次 Δ 上限（0.03，旁观不背锅）',
    'MOOD_ENV_MAX_DELTA': '不是冲她来（别人之间/群气氛）时单次 Δ 上限（0.05）',
    'MEMORY_EMOTION_MIN_DELTA': '记忆刻痕门槛：|Δ| 达到多少才算（0.10 = 设计里的 💥 档）',
    'MOOD_WATCH_SEC': '被动心情观察的最小间隔（秒）',
    'NAPCAT_APPROVE_TTL': '待批准请求有效期（秒）',
    'NAPCAT_TOOL_MAX_PER_MIN': 'NapCat 工具每分钟最多调用次数',
    'NAPCAT_TOOL_RESULT_MAX': '回给模型的结果上限',
    'PEER_MERGE_MAX': '鱼连发最多合并几条',
    'PEER_MERGE_WINDOW': '鱼连发合并窗口（秒）',
    'PEER_REPLY_COOLDOWN': '对鱼接话的冷却（秒）',
    'PEER_REPLY_PROB': '对鱼的消息接话概率（0~1）',
    'PEER_WAIT': '等「鱼」回复的时长（秒）',
    'PEER_WINDOW': '鱼被 @ 后多久内回复算有效（秒）',
    'PERSONA_GROUP_CHARS': '人格文本在群聊里最多带多少字（0 = 不限）',
    'PERSONA_MAX_RELOADS_PER_DAY': '改完人设不用重启容器，最多等 10 分钟就能生效。',
    'PREFLIGHT_MIN_CHARS': '预检：短于多少字跳过',
    'PREFLIGHT_TIMEOUT': '预检超时（秒）',
    'PRIVATE_MAX_HANZI': '私聊单条回复的字数上限（同上）',
    'PROACTIVE_CHECK_SEC': '主动私聊巡检间隔（秒）',
    'PROACTIVE_COOLDOWN_H': '两次主动私聊的最小间隔（小时）',
    'PROACTIVE_IDLE_MAX_H': '超过多久算「太久没聊」（小时）',
    'PROACTIVE_IDLE_MIN_H': '至少多久没聊才考虑主动找（小时）',
    'PROACTIVE_MAX_PER_DAY': '主动私聊每人每天上限',
    'PROACTIVE_MIN_INTIMACY': '主动私聊要求的最低亲近度',
    'PROACTIVE_MIN_SCORE': '候选人打分门槛',
    'PROACTIVE_QUIET_END': '主动私聊免打扰结束小时',
    'PROACTIVE_QUIET_START': '主动私聊免打扰开始小时',
    'PROBE_UPLOAD_TTL': '探测上传的有效期（秒）',
    'PROFILE_CARD_EVENTS': '人物卡里最多列几条事件',
    'PROFILE_MINE_INTERVAL_H': '同一个人多久挖一次',
    'PROFILE_MINE_MIN_MSGS': '攒多少条发言挖一次',
    'PROFILE_MINE_SAMPLE': '一次喂多少条他的发言',
    'PROFILE_MINE_TIMEOUT': '档案挖掘模型超时（秒）',
    'PROMPT_BUDGET': '整份提示词的字数预算',
    'QUIET_ADVISOR_COOLDOWN_SEC': '安静建议冷却（秒）',
    'QUIET_ADVISOR_WINDOW_SEC': '安静建议的观察窗口（秒）',
    'QUIET_GROUP_MIN': '针对整个群的安静建议触发阈值（0~1）',
    'QUIET_PERSON_MIN': '针对某个人的安静建议触发阈值（0~1）',
    'QUIET_THAW_GRACE_SEC': '破冰后的观察宽限期（秒）',
    'GROUP_MOOD_WINDOW_SEC': '群心情窗口（秒，默认 18000 = 5 小时；超时事件自动过期）',
    'GROUP_MOOD_PERSON_DROP': '某个人在窗口内净让心情掉多少 → 单独不理他（0.4）',
    'GROUP_MOOD_GROUP_DROP': '整个群净掉多少 → 判定群聊不愉快（0.7）',
    'GROUP_MOOD_MIN_PEOPLE': '至少几个不同的人让心情掉，才算「群聊导致」（3）',
    'GROUP_MOOD_MIN_DELTA': '进群心情的最小 |Δ|（0.03，防小事噪声）',
    'GROUP_MOOD_MAX_DELTA': '单条群心情事件上限（0.20，防小事刷满）',
    'GROUP_MOOD_LIKE_MIN': '净 Δ 达到多少算喜欢/讨厌（0.05，算人数和比例用）',
    'GROUP_MOOD_MAX_EVENTS': '每个群最多保留多少条心情事件',
    'GROUP_MOOD_CHAT_MIN': '群心情最差时「愿不愿意多说话」的乘数下限（0.55）',
    'GROUP_MOOD_CHAT_MAX': '群心情最好时「愿不愿意多说话」的乘数上限（1.35）',
    'QUOTE_MAX_CHARS': '引用原文最多带多少字',
    'RANDOM_CHAT_INTERVAL': '主动插话的巡检间隔（秒）',
    'RANDOM_CHAT_MIN_MSGS': '群里至少累积多少条消息才考虑插话',
    'RANDOM_CHAT_PROB': '满足条件后实际插话的概率（0~1）',
    'RANDOM_CHAT_VOICE_RATE': '插话时发语音的概率（0~1）',
    'RECALL_TEXT_MAX': '单条记忆注入的最大字数',
    'REFEREE_CTX_LINES': '审核带几条上下文',
    'REFEREE_MAX_PER_HOUR': '审核每小时最多几次（控制成本）',
    'REFEREE_MIN_CHARS': '审核：短于多少字的回复不核对',
    'REFLECT_HOUR': '每日反思执行的小时（0-23）',
    'REFLECT_MINUTE': '每日反思执行的分钟（0-59）',
    'REFLECT_WINDOW_DAYS': '反思覆盖最近多少天',
    'REPLY_DEDUP_RATIO': '判定为重复的相似度阈值（0~1）',
    'REPLY_DEDUP_SEC': '重复回复去重窗口（秒）',
    'REPLY_INTERVAL': '同一会话两条回复之间最少间隔秒数（限流）',
    'GROUP_REPLY_COOLDOWN': '群聊刚回过之后多少秒内，非点名消息不单独回（防同一件事回两遍）',
    'ROSTER_TTL': '群成员名册缓存时间（秒）',
    'RVC_INDEX_RATE_FEMALE': '女声 RVC index 权重',
    'RVC_INDEX_RATE_MALE': '男声 RVC index 权重',
    'RVC_PITCH_FEMALE': '女声变调半音数',
    'RVC_PITCH_MALE': '男声变调半音数',
    'RVC_PITCH_MAX': 'RVC 变调上限（半音）',
    'RVC_PITCH_MIN': '自适应 pitch 的上下限，防止测错把歌毁了',
    'RVC_PROTECT_FEMALE': '女声保护辅音参数（0~1）',
    'RVC_PROTECT_MALE': '男声保护辅音参数（0~1）',
    'RVC_RMS_FEMALE': '女声响度归一化目标',
    'RVC_RMS_MALE': '男声响度归一化目标',
    'RVC_SAME_GENDER_MAX_SEMIS': '源是女声时的移调上限（半音）：女→女只换音色，不该大幅降调',
    'RVC_TARGET_F0': '目标音色（kikiv2 女声）的典型基频中位数。自适应 pitch 的基准点。',
    'RVC_VOICE_F0_MAX': '判定女声的基频上限（Hz）',
    'RVC_VOICE_F0_MIN': '判定女声的基频下限（Hz）',
    'SCENE_AUDIT_MAX_MSGS': '群聊审核最多看多少条消息',
    'SCENE_AUDIT_MIN_INTERVAL': '群聊审核的最小间隔（秒）',
    'SCENE_AUDIT_TIMEOUT': '群聊审核超时（秒）',
    'SCENE_CARD_LINES': '群场景卡片最多几行',
    'SCENE_SEED_RETRY': '群场景首次生成的补试次数',
    'SCENE_SUMMARY_MAX_CHARS': '群场景摘要最大字数',
    'SCENE_SUMMARY_TTL': '群场景摘要有效期（秒）',
    'SCENE_WINDOW_MSGS': '「线程1 和我们的硬逻辑应该同时看 20 条来判断，而不是现在的五六条」）',
    'SELF_TOOL_FSIZE_MB': '自检工具读取文件大小上限（MB）',
    'SELF_TOOL_MEM_MB': '自检工具内存上限（MB）',
    'SELF_TOOL_READ_MAX': '自检工具最多读取多少行',
    'SELF_TOOL_TIMEOUT': '自检工具执行超时（秒）',
    'SHORT_QUERY_WINDOW_SEC': '短查询上下文窗口（秒）',
    'SLEEP_HOUR_END': '群聊免打扰结束小时（0-23）',
    'SLEEP_HOUR_START': '群聊免打扰开始小时（0-23）',
    'STICKER_COLLECT_MIN': '表情收集阈值：视觉强度绝对值低于它就不收（0.25）',
    'STICKER_MAX': '表情库容量上限（超过开始淘汰）',
    'STICKER_MAX_SIDE': '表情图片最长边（像素），超过会缩放',
    'STICKER_PRUNE_BUFFER': '淘汰时多留的缓冲条数',
    'STICKER_TAG_SOFT_CAP': '表情标签数量软上限（超过提示合并近义词）',
    'STICKER_WAIT_TTL': '表情上传等待有效期（秒）',
    'SUMMARIZE_FALLBACK_MAX': '压缩失败时直接把原文给 DeepSeek（超过这个字数才截断，防止 prompt 爆炸）',
    'THINK_BUDGET_FAST': '快路径思考预算（秒）',
    'THINK_BUDGET_SEC': '一次回复的总思考预算（秒）',
    'TOOL_RESULT_MAX': '单个工具结果回填给模型的最大字数',
    'TRACE_FLAT_STD': '纯色/极简图的灰度标准差阈值（挡掉「纯色帧 similarity 0.98」的假命中）',
    'TRACE_MAX_PER_DAY': 'trace.moe 每天配额（默认 100 次/天）',
    'TRACE_MIN_SHARE': '最高票至少占几成',
    'TRACE_MIN_SIM': '采信下限（疑似）',
    'TRACE_MIN_VOTES': '至少几票',
    'TRACE_SINGLE_SIM': '单条即可采信',
    'TRACE_STRONG_SIM': '高置信',
    'TTS_PICKUP_TIMEOUT': '等算力机接单的超时（秒）',
    'TTS_WAIT_TOTAL': '等语音合成结果的总超时（秒）',
    'UPLOAD_MAX_MB': '上传文件大小上限（MB）',
    'UPLOAD_WAIT_SEC': '等待文件上传的超时（秒）',
    'VIDEO_FRAME_INTERVAL': '帧数有上限——太长会自动变稀（60 帧 × 视觉逐帧已经很慢很贵）。',
    'VIDEO_MAX_FRAMES': '视频理解最多抽几帧',
    'VIDEO_MAX_FRAME_DESC': '每帧描述最大字数',
    'VIDEO_MAX_KEY_FRAMES': '字幕密集处额外取的高清关键帧上限（配合均匀抽帧一起看）',
    'VISION_CACHE_MAX': '最多存多少张图',
    'VISION_COOLDOWN': '被动识图冷却（识别成本，跟"要不要开口"是两回事）',
    'VISION_LLM_MAX_PER_HOUR': '注意这是**频率闸门**，不是「该不该说」的判断 —— 判断在模型那边。',
    'VISION_MAX_ATTEMPTS': '总尝试次数上限',
    'VISION_MAX_SIDE': '最长边缩到多少（越大越清楚、请求越大；默认 1568）',
    'VISION_TIMEOUT': '视觉模型超时（秒）',
    'VISION_VERIFY_MAX_PER_HOUR': '视觉核对每小时最多几次',
}


# 生效位置（与 DEFAULTS 同键）
FILES = {
    'AFFINITY_GAIN': 'services/mood_judge.py',
    'AFFINITY_DAILY_CAP': 'services/profiles.py',
    'BOND_EVENT_MIN_DELTA': 'services/profiles.py',
    'AFFINITY_TREND_WINDOW_SEC': 'services/profiles.py',
    'AGENT_RECALL_TIMEOUT': 'services/agent_client.py',
    'AGENT_REFLECT_QUEUE_TIMEOUT': 'services/agent_client.py',
    'AGENT_REFLECT_TIMEOUT': 'services/agent_client.py',
    'API_USAGE_KEEP_DAYS': 'services/api_usage.py',
    'API_USAGE_TIMEOUT': 'services/api_usage.py',
    'ARCHIVE_HOUR': 'plugins/memory_migration/__init__.py',
    'ARCHIVE_MINUTE': 'plugins/memory_migration/__init__.py',
    'ARCHIVE_WINDOW_DAYS': 'plugins/memory_migration/__init__.py',
    'AVATAR_PX': 'services/profile_card.py',
    'AVATAR_TTL_DAYS': 'services/profile_card.py',
    'BOND_TTL_DAYS': 'services/profiles.py',
    'BURST_MSG_COUNT': 'plugins/chat/__init__.py',
    'BURST_WINDOW_SEC': 'plugins/chat/__init__.py',
    'POKE_MAX_PER_WINDOW': 'services/poke_guard.py',
    'POKE_WINDOW_HOURS': 'services/poke_guard.py',
    'HARNESS_TIMEOUT': 'services/harness.py',
    'HARNESS_MAX_OUTPUT': 'services/harness.py',
    'CHAT_HISTORY_TURNS': 'services/llm.py',
    'CHAT_OFF_MAX_HOURS': 'services/chat_switch.py',
    'CONV_STATE_MAX_CHARS': 'services/conv_state.py',
    'CONV_STATE_MIN_INTERVAL': 'services/conv_state.py',
    'CONV_STATE_TIMEOUT': 'services/conv_state.py',
    'COVER_FULL_TTL': 'plugins/song_cover/__init__.py',
    'COVER_FULL_WAIT_SEC': 'plugins/song_cover/__init__.py',
    'COVER_MAX_CONCURRENT': 'plugins/song_cover/__init__.py',
    'COVER_RVC_WAIT_SEC': 'plugins/song_cover/__init__.py',
    'COVER_SPLIT_WAIT_SEC': 'plugins/song_cover/__init__.py',
    'COVER_VOICE_MAX_MB': 'plugins/song_cover/__init__.py',
    'COVER_VOICE_MAX_SEC': 'plugins/song_cover/__init__.py',
    'COVER_PITCH_MAX_TOLERANCE': 'plugins/song_cover/__init__.py',
    'CTX_LONG_MSG_CHARS': 'plugins/chat/__init__.py',
    'CTX_LONG_MSG_CHARS_MAX': 'plugins/chat/__init__.py',
    'CTX_LONG_MSG_KEEP': 'plugins/chat/__init__.py',
    'CURATOR_TIMEOUT': 'services/sticker_curator.py',
    'DAILY_SUMMARY_KEEP_DAYS': 'services/daily_summary.py',
    'DAILY_SUMMARY_MIN_MSGS': 'services/daily_summary.py',
    'DEBOUNCE_AT': 'plugins/chat/__init__.py',
    'DEBOUNCE_BARE_IMAGE': 'plugins/chat/__init__.py',
    'DEBOUNCE_MAX': 'plugins/chat/__init__.py',
    'DEBOUNCE_OTHER': 'plugins/chat/__init__.py',
    'DEBOUNCE_SAME': 'plugins/chat/__init__.py',
    'DIALOGUE_IMG_SIDE': 'services/dialogue_log.py',
    'DIALOGUE_KEEP_DAYS': 'services/dialogue_log.py',
    'DIGEST_AFTER': 'plugins/chat/__init__.py',
    'DRAW_MAX_CONCURRENT': 'plugins/draw/__init__.py',
    'DRAW_WAIT_SEC': 'plugins/draw/__init__.py',
    'EXPRESSION_EVERY': 'services/expression.py',
    'EXPRESSION_MAX_PER_SCOPE': 'services/expression.py',
    'EXPRESSION_MIN_INTERVAL': 'services/expression.py',
    'EXPRESSION_TIMEOUT': 'services/expression.py',
    'F0_PERIODICITY_MIN': 'services/song_gender.py',
    'FOCUS_FOLLOW_COUNT': 'plugins/chat/__init__.py',
    'FOCUS_JUDGE_TIMEOUT': 'plugins/chat/__init__.py',
    'FOCUS_NEG_STREAK_STOP': 'plugins/chat/__init__.py',
    'FOCUS_TIMEOUT': 'plugins/chat/__init__.py',
    'GLM_MAX_TOKENS': 'services/glm.py',
    'GLM_TIMEOUT': 'services/glm.py',
    'GROUP_MAX_HANZI': 'plugins/chat/__init__.py',
    'GROUP_MUTE_SEC': 'plugins/chat/__init__.py',
    'GROUP_SEND_LIMIT': 'plugins/chat/__init__.py',
    'GROUP_SEND_MAX_WAIT': 'plugins/chat/__init__.py',
    'WHITE_MOON_MIN_SCORE': 'services/white_moon.py',
    'WHITE_MOON_STYLE_ITEMS': 'services/white_moon.py',
    'WHITE_MOON_STYLE_TTL': 'services/white_moon.py',
    'HEARTBEAT_TIMEOUT_SEC': 'plugins/tool_dispatcher/__init__.py',
    'HISTORY_MAX': 'services/napcat_tools.py',
    'HIST_LINE_BASE': 'services/llm.py',
    'HIST_LINE_LONG': 'services/llm.py',
    'HIST_LONG_AT': 'services/llm.py',
    'HIST_TOTAL_BUDGET': 'services/llm.py',
    'IMAGE_DEDUP_HOURS': 'plugins/vision_agent/__init__.py',
    'LIBRARY_COOLDOWN': 'services/llm.py',
    'LLM_DAILY_BUDGET': 'services/llm_usage.py',
    'LLM_TRACE_KEEP_DAYS': 'services/llm.py',
    'LLM_TRACE_MAX_CHARS': 'services/llm.py',
    'LLM_USAGE_KEEP_DAYS': 'services/llm_usage.py',
    'LOCAL_MEMORY_DAYS': 'services/llm.py',
    'LONG_FORWARD_MIN': 'plugins/chat/__init__.py',
    'LORE_BUDGET': 'services/lorebook.py',
    'LORE_BUDGET_POST': 'services/lorebook.py',
    'LORE_MAX_ENTRIES': 'services/lorebook.py',
    'LORE_SCAN_DEPTH': 'services/lorebook.py',
    'BUBBLE_DELAY_BASE_MS': 'services/aura_logic.py',
    'BUBBLE_DELAY_PER_CHAR': 'services/aura_logic.py',
    'BUBBLE_DELAY_MAX_MS': 'services/aura_logic.py',
    'MAX_BUBBLES': 'services/aura_logic.py',
    'MAX_GROUP_RECENT': 'plugins/chat/__init__.py',
    'MAX_REPLY_CHARS': 'services/config.py',
    'MAX_REQ_ROUNDS': 'services/llm.py',
    'MAX_REQ_ROUNDS_FAST': 'services/llm.py',
    'MAX_TOOL_TURNS': 'services/llm.py',
    'MEMORY_REVISIT_AGE_DAYS': 'services/memory_v2.py',
    'MEMORY_REVISIT_IDLE_DAYS': 'services/memory_v2.py',
    'MERGE_WINDOW': 'plugins/chat/__init__.py',
    'SEGMENT_WAIT_SEC': 'services/segments.py',
    'SEGMENT_WAIT_FAST': 'plugins/chat/__init__.py',
    'SEGMENT_MAX_SEC': 'services/segments.py',
    'SEGMENT_MAX_MSGS': 'services/segments.py',
    'SEGMENT_HISTORY_KEEP': 'services/segments.py',
    'MOOD_GLOBAL_SHARE': 'services/mood.py',
    'MOOD_IDLE_AFTER': 'services/mood.py',
    'MOOD_IDLE_STEP': 'services/mood.py',
    'MOOD_IDLE_TICK': 'services/mood.py',
    'MOOD_PEAK_WINDOW_SEC': 'services/mood.py',
    'MOOD_PEAK_MIN_DELTA': 'services/mood.py',
    'MOOD_JUDGE_CTX': 'services/mood_judge.py',
    'MOOD_JUDGE_FRESH_SEC': 'services/mood.py',
    'MOOD_JUDGE_MIN_INTERVAL': 'services/mood_judge.py',
    'MOOD_JUDGE_REFRESH': 'services/mood_judge.py',
    'MOOD_JUDGE_SMOOTH': 'services/mood.py',
    'MOOD_MAX_STEP': 'services/mood.py',
    'MOOD_MAX_DELTA': 'services/mood.py',
    'MOOD_LABEL_STEP': 'services/mood.py',
    'MOOD_JUDGE_TIMEOUT': 'services/mood_judge.py',
    'MOOD_LABEL_MAX': 'services/mood.py',
    'MOOD_LABEL_TTL_HOURS': 'services/mood.py',
    'MOOD_MAX_SESSIONS': 'services/mood.py',
    'MOOD_THINK_KEEP': 'services/mood_judge.py',
    'MOOD_FOLLOW_MIN': 'plugins/chat/__init__.py',
    'MOOD_HALF_LIFE_HOURS': 'services/mood.py',
    'MOOD_NOISE_DELTA': 'services/mood_judge.py',
    'MOOD_PASSIVE_MAX_DELTA': 'services/mood_judge.py',
    'MOOD_ENV_MAX_DELTA': 'services/mood_judge.py',
    'MEMORY_EMOTION_MIN_DELTA': 'plugins/chat/__init__.py',
    'MOOD_WATCH_SEC': 'plugins/chat/__init__.py',
    'NAPCAT_APPROVE_TTL': 'services/napcat_tools.py',
    'NAPCAT_TOOL_MAX_PER_MIN': 'services/napcat_tools.py',
    'NAPCAT_TOOL_RESULT_MAX': 'services/napcat_tools.py',
    'PEER_MERGE_MAX': 'services/referee.py',
    'PEER_MERGE_WINDOW': 'services/referee.py',
    'PEER_REPLY_COOLDOWN': 'plugins/chat/__init__.py',
    'PEER_REPLY_PROB': 'plugins/chat/__init__.py',
    'PEER_WAIT': 'services/referee.py',
    'PEER_WINDOW': 'services/referee.py',
    'PERSONA_GROUP_CHARS': 'services/persona.py',
    'PERSONA_MAX_RELOADS_PER_DAY': 'services/persona.py',
    'PREFLIGHT_MIN_CHARS': 'services/referee.py',
    'PREFLIGHT_TIMEOUT': 'services/referee.py',
    'PRIVATE_MAX_HANZI': 'plugins/chat/__init__.py',
    'PROACTIVE_CHECK_SEC': 'services/proactive.py',
    'PROACTIVE_COOLDOWN_H': 'services/proactive.py',
    'PROACTIVE_IDLE_MAX_H': 'services/proactive.py',
    'PROACTIVE_IDLE_MIN_H': 'services/proactive.py',
    'PROACTIVE_MAX_PER_DAY': 'services/proactive.py',
    'PROACTIVE_MIN_INTIMACY': 'services/proactive.py',
    'PROACTIVE_MIN_SCORE': 'services/proactive.py',
    'PROACTIVE_QUIET_END': 'services/proactive.py',
    'PROACTIVE_QUIET_START': 'services/proactive.py',
    'PROBE_UPLOAD_TTL': 'plugins/song_cover/__init__.py',
    'PROFILE_CARD_EVENTS': 'services/profiles.py',
    'PROFILE_MINE_INTERVAL_H': 'services/profile_miner.py',
    'PROFILE_MINE_MIN_MSGS': 'services/profile_miner.py',
    'PROFILE_MINE_SAMPLE': 'services/profile_miner.py',
    'PROFILE_MINE_TIMEOUT': 'services/profile_miner.py',
    'PROMPT_BUDGET': 'services/prompt_builder.py',
    'QUIET_ADVISOR_COOLDOWN_SEC': 'services/quiet_advisor.py',
    'QUIET_ADVISOR_WINDOW_SEC': 'services/quiet_advisor.py',
    'QUIET_GROUP_MIN': 'services/quiet_advisor.py',
    'QUIET_PERSON_MIN': 'services/quiet_advisor.py',
    'QUIET_THAW_GRACE_SEC': 'services/quiet_advisor.py',
    'GROUP_MOOD_WINDOW_SEC': 'services/group_mood.py',
    'GROUP_MOOD_PERSON_DROP': 'services/group_mood.py',
    'GROUP_MOOD_GROUP_DROP': 'services/group_mood.py',
    'GROUP_MOOD_MIN_PEOPLE': 'services/group_mood.py',
    'GROUP_MOOD_MIN_DELTA': 'services/group_mood.py',
    'GROUP_MOOD_MAX_DELTA': 'services/group_mood.py',
    'GROUP_MOOD_LIKE_MIN': 'services/group_mood.py',
    'GROUP_MOOD_MAX_EVENTS': 'services/group_mood.py',
    'GROUP_MOOD_CHAT_MIN': 'services/mood.py',
    'GROUP_MOOD_CHAT_MAX': 'services/mood.py',
    'QUOTE_MAX_CHARS': 'plugins/chat/__init__.py',
    'RANDOM_CHAT_INTERVAL': 'plugins/chat/__init__.py',
    'RANDOM_CHAT_MIN_MSGS': 'plugins/chat/__init__.py',
    'RANDOM_CHAT_PROB': 'plugins/chat/__init__.py',
    'RANDOM_CHAT_VOICE_RATE': 'plugins/chat/__init__.py',
    'RECALL_TEXT_MAX': 'services/memory_v2.py',
    'REFEREE_CTX_LINES': 'services/referee.py',
    'REFEREE_MAX_PER_HOUR': 'services/referee.py',
    'REFEREE_MIN_CHARS': 'services/referee.py',
    'REFLECT_HOUR': 'plugins/memory_migration/__init__.py',
    'REFLECT_MINUTE': 'plugins/memory_migration/__init__.py',
    'REFLECT_WINDOW_DAYS': 'services/journal_agent.py',
    'REPLY_DEDUP_RATIO': 'plugins/chat/__init__.py',
    'REPLY_DEDUP_SEC': 'plugins/chat/__init__.py',
    'REPLY_INTERVAL': 'plugins/chat/__init__.py',
    'GROUP_REPLY_COOLDOWN': 'plugins/chat/__init__.py',
    'ROSTER_TTL': 'services/group_roster.py',
    'RVC_INDEX_RATE_FEMALE': 'plugins/song_cover/__init__.py',
    'RVC_INDEX_RATE_MALE': 'plugins/song_cover/__init__.py',
    'RVC_PITCH_FEMALE': 'plugins/song_cover/__init__.py',
    'RVC_PITCH_MALE': 'plugins/song_cover/__init__.py',
    'RVC_PITCH_MAX': 'services/song_gender.py',
    'RVC_PITCH_MIN': 'services/song_gender.py',
    'RVC_PROTECT_FEMALE': 'plugins/song_cover/__init__.py',
    'RVC_PROTECT_MALE': 'plugins/song_cover/__init__.py',
    'RVC_RMS_FEMALE': 'plugins/song_cover/__init__.py',
    'RVC_RMS_MALE': 'plugins/song_cover/__init__.py',
    'RVC_SAME_GENDER_MAX_SEMIS': 'services/song_gender.py',
    'RVC_TARGET_F0': 'services/song_gender.py',
    'RVC_VOICE_F0_MAX': 'services/song_gender.py',
    'RVC_VOICE_F0_MIN': 'services/song_gender.py',
    'SCENE_AUDIT_MAX_MSGS': 'services/scene_auditor.py',
    'SCENE_AUDIT_MIN_INTERVAL': 'services/scene_auditor.py',
    'SCENE_AUDIT_TIMEOUT': 'services/scene_auditor.py',
    'SCENE_CARD_LINES': 'services/profile_card.py',
    'SCENE_SEED_RETRY': 'services/group_scene.py',
    'SCENE_SUMMARY_MAX_CHARS': 'services/scene_auditor.py',
    'SCENE_SUMMARY_TTL': 'services/scene_auditor.py',
    'SCENE_WINDOW_MSGS': 'services/group_scene.py',
    'SELF_TOOL_FSIZE_MB': 'plugins/self_tools/__init__.py',
    'SELF_TOOL_MEM_MB': 'plugins/self_tools/__init__.py',
    'SELF_TOOL_READ_MAX': 'plugins/self_tools/__init__.py',
    'SELF_TOOL_TIMEOUT': 'plugins/self_tools/__init__.py',
    'SHORT_QUERY_WINDOW_SEC': 'plugins/chat/__init__.py',
    'SLEEP_HOUR_END': 'plugins/chat/__init__.py',
    'SLEEP_HOUR_START': 'plugins/chat/__init__.py',
    'STICKER_COLLECT_MIN': 'services/sticker_receiver.py',
    'STICKER_MAX': 'services/sticker_library.py',
    'STICKER_MAX_SIDE': 'services/sticker_importer.py',
    'STICKER_PRUNE_BUFFER': 'services/sticker_library.py',
    'STICKER_TAG_SOFT_CAP': 'services/sticker_library.py',
    'STICKER_WAIT_TTL': 'plugins/sticker_admin/__init__.py',
    'SUMMARIZE_FALLBACK_MAX': 'services/llm.py',
    'THINK_BUDGET_FAST': 'services/llm.py',
    'THINK_BUDGET_SEC': 'services/llm.py',
    'TOOL_RESULT_MAX': 'services/llm.py',
    'TRACE_FLAT_STD': 'services/vision_verify.py',
    'TRACE_MAX_PER_DAY': 'services/vision_verify.py',
    'TRACE_MIN_SHARE': 'services/vision_verify.py',
    'TRACE_MIN_SIM': 'services/vision_verify.py',
    'TRACE_MIN_VOTES': 'services/vision_verify.py',
    'TRACE_SINGLE_SIM': 'services/vision_verify.py',
    'TRACE_STRONG_SIM': 'services/vision_verify.py',
    'TTS_PICKUP_TIMEOUT': 'plugins/chat/__init__.py',
    'TTS_WAIT_TOTAL': 'plugins/chat/__init__.py',
    'UPLOAD_MAX_MB': 'plugins/chat/__init__.py',
    'UPLOAD_WAIT_SEC': 'plugins/chat/__init__.py',
    'VIDEO_FRAME_INTERVAL': 'services/video_understand.py',
    'VIDEO_MAX_FRAMES': 'services/video_understand.py',
    'VIDEO_MAX_FRAME_DESC': 'services/video_understand.py',
    'VIDEO_MAX_KEY_FRAMES': 'services/video_understand.py',
    'VISION_CACHE_MAX': 'services/vision_verify.py',
    'VISION_COOLDOWN': 'plugins/vision_agent/__init__.py',
    'VISION_LLM_MAX_PER_HOUR': 'plugins/vision_agent/__init__.py',
    'VISION_MAX_ATTEMPTS': 'plugins/vision_agent/__init__.py',
    'VISION_MAX_SIDE': 'plugins/vision_agent/__init__.py',
    'VISION_TIMEOUT': 'plugins/vision_agent/__init__.py',
    'VISION_VERIFY_MAX_PER_HOUR': 'services/vision_verify.py',
}
