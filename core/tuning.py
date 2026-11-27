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
    # 图书馆（RAG v3）单次检索上限：实测 5~14s，2.5s 会把所有检索都判超时。
    'AGENT_LIBRARY_TIMEOUT': 14.0,
    # 2026-11-26：smart-query 会卡 >70s（LLM 规划层），直接调 rag_search 工具最稳。
    'AGENT_RAG_TOOL_TIMEOUT': 12.0,
    # ---------- 模型输出 token（2026-11-27）----------
    # 主聊天输出上限；GLM 当主模型时 800 会把正文挤没（thinking 先吃 token）。
    # 优先级：<ROLE>_MAX_TOKENS > LLM_MAX_TOKENS > GLM_MAX_TOKENS(glm) > 这里的默认值。
    # 2026-11-27 用户：「输出 max token 改成 4000，不要 think 占满输出」。
    # 推理型模型（deepseek-flash / GLM thinking）会先花掉一大截 token 在思考上，
    # 1200 经常只剩半句甚至空正文。4000 是「思考 + 正文」的总上限。
    'ROLEPLAY_MAX_TOKENS': 4000,
    'LLM_DEFAULT_MAX_TOKENS': 800,
    'LLM_MAX_TOKENS': 0,
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

    # ---------- 图片文字（OpenCV 增强，2026-11-27）----------
    'OCR_MAX_SIDE': 2200,
    'OCR_UPSCALE_MIN_W': 1400,
    'OCR_MAX_CROPS': 3,
    'OCR_TIMEOUT': 45.0,

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
    # 借风格三护栏（style §4.3）：默认不借；借也 ≤0.5，冷漠人设再压到 0.2
    'WHITE_MOON_BORROW': 0,
    'WHITE_MOON_BORROW_RATIO': 0.3,
    'WHITE_MOON_BORROW_COLD_MAX': 0.2,
    'WHITE_MOON_BORROW_CHARS': 120,
    'PERSONA_EXPRESSIVENESS': 0.5,
    # 心情回落点 / 锚点目标（-0.3~0.3）；0 = 中性，负 = 底色偏冷
    'PERSONA_BASELINE_V': 0.0,
    'PERSONA_WARMTH': 0.5,
    'PERSONA_FORGIVENESS': 0.5,
    'PERSONA_TRUST_SPEED': 0.5,
    'PERSONA_REACTIVITY': 0.5,
    'PERSONA_SENSITIVITY': 1.0,

    # ---------- 说话样本库（style_vault） ----------
    # 1=开：省流模式也会把每个人的原话记进 data/style_corpus（只记文本，不调模型）
    'STYLE_VAULT_ENABLED': 1,
    'STYLE_VAULT_MAX_SAMPLES': 200,
    'STYLE_VAULT_SAMPLE_CHARS': 60,
    'STYLE_VAULT_MAX_UPLOAD_CHARS': 4000,

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
    # 回复前「动态取数」流水线的总预算（秒）：到点就带着已拿到的部分回复。
    # 2026-11-24 用户：「pipeline 一定要完整去调一遍」→ 7s 提到 12s；
    # 图书馆（RAG v3）实测 5~14s，再提到 16s，让它在问题类消息上真的能返回。
    'PIPELINE_BUDGET': 16.0,

    # ---------- 用量监测 ----------
    'LLM_DAILY_BUDGET': 0.0,
    # 到多少比例先「说一声要休息」（0.9 = 90%）；到 100% 直接不调模型
    'LLM_BUDGET_SOFT_RATIO': 0.9,

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
    # 2026-11-25 删除 MAX_REPLY_CHARS：从来没有读点（总长由 OUT_BUBBLE_CHARS /
    # OUT_BUBBLES_MAX 控制），留着会让人误以为有「整条回复总长上限」。

    # ---------- 模型调用 ----------
    'MAX_REQ_ROUNDS': 6,
    'MAX_REQ_ROUNDS_FAST': 2,
    'MAX_TOOL_TURNS': 6,
    # long_doc（DESIGN §3.3 / ADR-033）：用户明确要长答时，单轮给更大的输出与证据预算
    'LONG_DOC_ENABLED': 1,
    'LONG_DOC_MAX_TOKENS': 4000,
    # 候选条数（检索多取几条，真正进 prompt 的只有 PIECES 篇）
    'LONG_DOC_EVIDENCE': 8,
    # 2026-11-27（外部审计）：DESIGN §3.3 写的是「3 篇 × ≤1900 字 + 出处 300」，
    # 所以进 prompt 的是 3 篇、每篇 1900 **字符**（不是 token）。
    'LONG_DOC_PIECES': 3,
    'LONG_DOC_PIECE_CHARS': 1900,

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
    # 没有 ⭐ 的段 = 她主动插话：只给最后几条，避免捡起早就翻篇的话题
    'SEG_INSERT_WINDOW': 5,

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
    'MOOD_JUDGE_MAX_PER_HOUR': 30,
    'MOOD_JUDGE_REFRESH': 300.0,
    'MOOD_JUDGE_SMOOTH': 0.35,
    # ---------- 接话前「读气氛」（services/atmosphere.py）----------
    # 只读最近 N 句标签语，输出「谁在难过 / 整体氛围 / 接话注意」；
    # 只作提示，不改心情/好感（心情唯一来源仍是她的 THINK）。
    'ATMO_ENABLED': 1,
    'ATMO_MSGS': 8,
    'ATMO_TTL': 90.0,
    'ATMO_TIMEOUT': 5.0,
    # 2026-11-27：多了意图四行（在聊/在问什么/该不该接/已有人答），
    # 240 字会把最后两行截掉 → 400。
    'ATMO_MAX_CHARS': 400,
    # 意图字段（topic/asks/need_reply/why/replied/keywords/queries）是否进 prompt。
    # 1 = 只加不减（默认）；0 = 退回 2026-11-26 的四行气氛块。
    'ATMO_INTENT': 1,
    # 2026-11-27：意图类型（提问/吐槽/梗/陈述/安慰）—— 只加一行，不加请求
    'ATMO_KIND': 1,
    # 主动插话闸门：need_reply=no 时这一轮不插话。**默认 0**，
    # 必须先跑 scripts/eval_intent.py，need_reply 准确率 ≥85% 再开。
    'ATMO_GATE': 0,
    # ---------- 个人档案库（services/archive.py，C 方案）----------
    # 只存 ARCHIVE_MIN_AGE_DAYS 天以前的旧事；本地 sqlite，不依赖 RAG。
    'ARCHIVE_ENABLED': 1,
    'ARCHIVE_MIN_AGE_DAYS': 30,
    'ARCHIVE_TOP': 4,
    'ARCHIVE_MAX_CHARS': 420,
    'MOOD_MAX_STEP': 0.3,
    # 旧单次涨幅上限（legacy）；新口径见 DESIGN §3.1 的 0.35 护栏。
    # ⚠️ 不要靠调小它来「防一边倒」：0.2 会让 §13.3 的 0.10/0.25/0.50
    #    刻痕阈值永远到不了 💥💥/💥💥💥。真正的收紧在 Δ 入口的噪音丢弃。
    'MOOD_MAX_DELTA': 0.5,
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
    # 2026-11-25 删除 MOOD_PASSIVE_MAX_DELTA：ADR-038 之后「没 THINK 不改心情」，
    # 被动通道整个没了，这个上限不再有意义。
    # 不是冲她来（别人之间 / 群气氛）且没被叫 → 最多 ±0.05
    'MOOD_ENV_MAX_DELTA': 0.05,
    # 记忆刻痕 / 档案只收「明显动过」的事：|Δ| 低于这个值不进刻痕
    'MEMORY_EMOTION_MIN_DELTA': 0.10,
    # 档案「事情」只收**大波动**：|Δ| 低于它只进记忆，不进档案（避免档案变流水账）
    'PROFILE_NOTE_MIN_DELTA': 0.15,

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
    # 主动闭环（proactive §A）：群接话日上限 + 私聊 accept_rate 反馈
    'PROACTIVE_GROUP_MAX_PER_DAY': 3,
    'PROACTIVE_ACCEPT_WINDOW_H': 12.0,
    'PROACTIVE_ACCEPT_BONUS': 0.3,

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
    # ---------- 图片结构化报告 ImageReport（2026-11-27，docs/addressing-design §7.6）----------
    # 1 = 走「一次产出、三处复用」（质量/文字/画面合成一份 report）；0 = 退回旧路径
    'VISION_REPORT': 1,
    # 第二家视觉（GLM）：always / on_miss（trace 没命中且 qwen 没给专名才问）/ off
    'VISION_GLM_MODE': 'on_miss',
    # report 里的文字**进 prompt**最多多少字（全文留在缓存里）
    'VISION_TEXT_MAX': 60,
}


# 说明（与 DEFAULTS 同键）
DOCS = {
    'AFFINITY_GAIN': '⚠️ LEGACY（无读点，保留键名兼容旧 .env）：新口径见 AFF_* / REL_*',
    'AFFINITY_DAILY_CAP': 'v1 好感每日净变化上限（profiles.bump_affinity 用；v2 不用）',
    'BOND_EVENT_MIN_DELTA': '「印象最深的一件事」的门槛：记忆 |Δ| 达到多少才算（0.15）',
    'AFFINITY_TREND_WINDOW_SEC': '好感走势的统计窗口（秒）',
    'AGENT_RECALL_TIMEOUT': '算力机回忆接口超时（秒，默认 3；它在回复关键路径上）',
    'AGENT_LIBRARY_TIMEOUT': '图书馆 RAG 单次检索上限（秒，默认 14；实测 5~14s）',
    'AGENT_RAG_TOOL_TIMEOUT': '直接调 rag_search 工具的超时（秒，默认 12；smart-query 卡住时用它兜底）',
    'ROLEPLAY_MAX_TOKENS': '主聊天单次输出 token 上限（默认 4000，含 thinking；推理模型别低于 4000）',
    'LLM_DEFAULT_MAX_TOKENS': '其它角色（判读/审核/表达…）默认输出 token 上限',
    'LLM_MAX_TOKENS': '全局输出 token 覆盖（>0 时压过各角色默认；0=不覆盖）',
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
    'WHITE_MOON_BORROW': '白月光借风格总开关（0=只给 1 行事实，1=允许借句式）',
    'WHITE_MOON_BORROW_RATIO': '借风格比例（只允许 0 / 0.3 / 0.5，代码会夹）',
    'WHITE_MOON_BORROW_COLD_MAX': '冷漠人设（expressiveness<0.3）的借风格上限',
    'WHITE_MOON_BORROW_CHARS': '白月光借风格块的字数上限',
    'PERSONA_EXPRESSIVENESS': '人设表达欲 0~1（<0.3 视为冷/内敛，压借风格比例）',
    'PERSONA_BASELINE_V': '人设心情基线 -0.3~0.3（衰减回落点和锚点都叠在它上面）',
    'PERSONA_WARMTH': '人设温度 0~1：好感步长 = 0.04 + 0.12×warmth',
    'PERSONA_FORGIVENESS': '人设宽容 0~1：道歉回补 = 0.03×(0.5+forgiveness)',
    'PERSONA_TRUST_SPEED': '人设信任速度 0~1：trust 正向步长 ×(0.5+trust_speed)',
    'PERSONA_REACTIVITY': '人设情绪反应 0~1：进 mood.observe 的 reactivity',
    'PERSONA_SENSITIVITY': '人设敏感度乘数（再夹到 MOOD_SENS_MIN/MAX）',
    'STYLE_VAULT_ENABLED': '说话样本库：1=自动记每个人的原话（省流模式也记）',
    'STYLE_VAULT_MAX_SAMPLES': '说话样本库：每个人保留最近多少条原话',
    'STYLE_VAULT_SAMPLE_CHARS': '说话样本库：单条原话最多存多少字',
    'STYLE_VAULT_MAX_UPLOAD_CHARS': '自定义风格：用户一次最多上传多少字',
    'HEARTBEAT_TIMEOUT_SEC': '工具分发心跳超时（秒）',
    'HISTORY_MAX': '模型侧历史消息条数',
    'HIST_LINE_BASE': '历史里短消息给多少字',
    'HIST_LINE_LONG': '历史里长消息给多少字',
    'HIST_LONG_AT': '历史里超过多少字算长消息',
    'HIST_TOTAL_BUDGET': '历史总字数预算',
    'IMAGE_DEDUP_HOURS': '群友：「这肥鱼魔了」。60 秒 DEDUP 拦不住这种跨 20 分钟的复读。',
    'OCR_MAX_SIDE': '图片文字增强：增强后最长边上限（像素）',
    'OCR_UPSCALE_MIN_W': '图片文字增强：宽度不足这个值就放大（小字靠这个救）',
    'OCR_MAX_CROPS': '图片文字增强：最多裁几个文字块送模型（越多越贵）',
    'OCR_TIMEOUT': '图片文字增强：单次视觉调用超时（秒）',
    'LIBRARY_COOLDOWN': '同一梗/素材的冷却时间（秒）',
    'PIPELINE_BUDGET': '回复前动态取数（心情/档案/记忆/图书馆）的总预算（秒）',
    'LLM_DAILY_BUDGET': '每日 token 预算（0 = 不刹车）；到 100% 她回一句固定话术然后当天不再消耗',
    'LLM_BUDGET_SOFT_RATIO': '日预算的「提前说休息」比例（0.9 = 用到 90% 就先提醒）',
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
    'MAX_BUBBLES': '⚠️ LEGACY（2026-11-26 合并进 OUT_BUBBLES_MAX）：一条回复最多拆几个气泡',
    'MAX_GROUP_RECENT': '群聊上下文保留多少条消息（16 = 修过长消息被挤出窗口的问题）',
    'MAX_REQ_ROUNDS': '单次生成最多请求几轮',
    'MAX_REQ_ROUNDS_FAST': '快路径最多请求几轮',
    'MAX_TOOL_TURNS': '一次对话里最多几轮工具调用',
    'LONG_DOC_ENABLED': '长答模式总开关（1=用户明确要长答时放开输出/证据预算；0=回滚成普通回复）',
    'LONG_DOC_MAX_TOKENS': '长答模式单次生成的最大 token（默认 4000，含 thinking）',
    'LONG_DOC_EVIDENCE': '长答模式检索候选条数（普通模式按 RET_EVIDENCE_MAX）',
    'LONG_DOC_PIECES': '长答模式真正进 prompt 的证据篇数（DESIGN：3 篇）',
    'LONG_DOC_PIECE_CHARS': '长答模式每篇证据最多多少**字符**（DESIGN：1900）',
    'MEMORY_REVISIT_AGE_DAYS': '回访记忆：至少多少天前的事',
    'MEMORY_REVISIT_IDLE_DAYS': '回访记忆：至少多久没提过',
    'MERGE_WINDOW': '合并转发判定窗口（秒）',
    'SEGMENT_WAIT_SEC': '群聊分段：安静多少秒算「这一段说完了」（默认 3.5）',
    'SEGMENT_WAIT_FAST': '群聊分段：@ 她 / 叫名字时只等多久（默认 1.2，别让她慢半拍）',
    'SEGMENT_MAX_SEC': '群聊分段：最长攒多久必须处理（默认 30 秒）',
    'SEGMENT_MAX_MSGS': '群聊分段：最多攒多少条（防刷屏）',
    'SEGMENT_HISTORY_KEEP': '保留多少个「处理过的段」给上下文/翻历史',
    'SEG_INSERT_WINDOW': '主动插话时只给最后几条（没有 ⭐ 的段；防止接旧话题）',
    'MOOD_GLOBAL_SHARE': '全局心情占会话心情的权重（0~1）',
    'MOOD_IDLE_AFTER': '空闲多少秒后心情开始衰减（默认 300 = 5 分钟；之后每 5 分钟向 0 走一步）',
    'MOOD_IDLE_STEP': '每步走多少点（内部 -100~100 口径，1.0 ≈ 外口径 0.01）',
    'MOOD_IDLE_TICK': '衰减步长（秒）：每这么久向 0 走一步（默认 300 = 5 分钟走 0.01）',
    'MOOD_PEAK_WINDOW_SEC': '心情峰值事件的保质期（秒，默认 7200 = 2 小时；每件事最多挂 2 小时）',
    'MOOD_PEAK_MIN_DELTA': '进入「峰值事件」候选的最小 |Δ|（0.03，防小事顶掉大事）',
    'MOOD_JUDGE_CTX': '判读时带多少条上下文（8 = 5~8 条，和主模型上下文同源同条数）',
    'MOOD_JUDGE_FRESH_SEC': '判读结果有效期（秒），过期不当「现在」',
    'MOOD_JUDGE_MIN_INTERVAL': '两次判读的最小间隔（秒），防刷屏时来回震荡',
    'MOOD_JUDGE_MAX_PER_HOUR': '每会话每小时最多判读几次（含补判窗口）；超了只判最后一窗',
    'MOOD_JUDGE_REFRESH': '她没主动写心情时，多久用群聊重新判一次（秒）。0 = 只在她说的时候判',
    'MOOD_JUDGE_SMOOTH': '⚠️ LEGACY（P2 删）：判读值平滑系数，旧公式用',
    'ATMO_ENABLED': '接话前是否读群气氛（1=开；只作提示，不改心情/好感）',
    'ATMO_MSGS': '读气氛看最近多少句（默认 8，和主模型上下文同源同条数）',
    'ATMO_TTL': '同一窗口的气氛缓存多久（秒），避免连发时重复调用',
    'ATMO_TIMEOUT': '读气氛调用超时（秒），超时只丢这一块，不拖慢回复',
    'ATMO_MAX_CHARS': '气氛+意图块最多多少字（进 prompt 的上限）',
    'ATMO_INTENT': '气氛块是否追加意图字段（在聊/在问什么/该不该接；1=开，0=退回四行）',
    'ATMO_KIND': '气氛块是否追加「这句是：提问/吐槽/梗/陈述/安慰」（只加一行）',
    'ATMO_GATE': '主动插话闸门：need_reply=no 时不插话（默认 0；离线评测≥85% 才开）',
    'ARCHIVE_ENABLED': '个人档案库开关（1=开；本地 sqlite，只存旧事，不依赖 RAG）',
    'ARCHIVE_MIN_AGE_DAYS': '档案库只收多少天以前的旧事（默认 30；近期记忆走 memdb）',
    'ARCHIVE_TOP': '档案库一次最多回几条',
    'ARCHIVE_MAX_CHARS': '档案块最多多少字（进 prompt 的上限）',
    'MOOD_MAX_STEP': '⚠️ LEGACY（P2 删）：单次判读最多改变多少，旧公式用',
    'MOOD_MAX_DELTA': '名义 Δ 的夹取上限（判读护栏 / mood.apply_delta 仍在用，不是死键）',
    'MOOD_JUDGE_TIMEOUT': '判读调用超时（秒）',
    'MOOD_LABEL_MAX': '心情词最多保留多少个',
    'MOOD_LABEL_TTL_HOURS': '心情词的有效期（小时），过期不再当「现在」',
    'MOOD_MAX_SESSIONS': '最多保留多少个会话的心情（防字典无限长）',
    'MOOD_THINK_KEEP': '判读心理活动保留多少条',
    'MOOD_FOLLOW_MIN': '心情低于这个值就不再追着接话（只回明确叫她）',
    'MOOD_HALF_LIFE_HOURS': '⚠️ LEGACY（P2 删）：旧单一半衰期 12h；新口径按场景 30min/2h/6h',
    'MOOD_NOISE_DELTA': 'Δ 噪音门槛：|Δ| 小于它视为 0（0.03，不动数值/好感/刻痕）',
    'MOOD_ENV_MAX_DELTA': '不是冲她来（别人之间/群气氛）时单次 Δ 上限（0.05）',
    'MEMORY_EMOTION_MIN_DELTA': '记忆刻痕门槛：|Δ| 达到多少才算（0.10 = 设计里的 💥 档）',
    'PROFILE_NOTE_MIN_DELTA': '档案事情门槛：|Δ| 达到多少才写进档案（0.15 ≈ 只有 impact±2 的大事）',
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
    'PROACTIVE_GROUP_MAX_PER_DAY': '群主动接话每天最多几次（每个群）',
    'PROACTIVE_ACCEPT_WINDOW_H': '主动私聊后多久内对方回话算「接住了」（小时）',
    'PROACTIVE_ACCEPT_BONUS': 'accept_rate 对主动评分的加成系数',
    'PROBE_UPLOAD_TTL': '探测上传的有效期（秒）',
    'PROFILE_CARD_EVENTS': '人物卡里最多列几条事件',
    'PROFILE_MINE_INTERVAL_H': '同一个人多久挖一次',
    'PROFILE_MINE_MIN_MSGS': '攒多少条发言挖一次',
    'PROFILE_MINE_SAMPLE': '一次喂多少条他的发言',
    'PROFILE_MINE_TIMEOUT': '档案挖掘模型超时（秒）',
    'PROMPT_BUDGET': '整份提示词的**字符**预算（与 DESIGN §3.3 同单位）',
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
    'VISION_REPORT': '图片结构化报告（1=一次产出三处复用；0=退回旧识图路径）',
    'VISION_GLM_MODE': '第二家视觉模型策略：always（每家都问）/ on_miss（默认，只在没认出专名时问）/ off',
    'VISION_TEXT_MAX': '图片里的文字**进 prompt**最多多少字（全文留在缓存，防 token 爆）',
}


# 生效位置（与 DEFAULTS 同键）
FILES = {
    'AFFINITY_GAIN': 'services/mood_judge.py',
    'AFFINITY_DAILY_CAP': 'services/profiles.py',
    'BOND_EVENT_MIN_DELTA': 'services/profiles.py',
    'AFFINITY_TREND_WINDOW_SEC': 'services/profiles.py',
    'AGENT_RECALL_TIMEOUT': 'services/agent_client.py',
    'AGENT_LIBRARY_TIMEOUT': 'services/agent_client.py',
    'AGENT_RAG_TOOL_TIMEOUT': 'services/agent_client.py',
    'ROLEPLAY_MAX_TOKENS': 'services/providers.py',
    'LLM_DEFAULT_MAX_TOKENS': 'services/providers.py',
    'LLM_MAX_TOKENS': 'services/providers.py',
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
    'WHITE_MOON_BORROW': 'services/white_moon.py',
    'WHITE_MOON_BORROW_RATIO': 'services/white_moon.py',
    'WHITE_MOON_BORROW_COLD_MAX': 'services/white_moon.py',
    'WHITE_MOON_BORROW_CHARS': 'services/white_moon.py',
    'PERSONA_EXPRESSIVENESS': 'services/white_moon.py',
    'PERSONA_BASELINE_V': 'services/persona_traits.py',
    'PERSONA_WARMTH': 'services/persona_traits.py',
    'PERSONA_FORGIVENESS': 'services/persona_traits.py',
    'PERSONA_TRUST_SPEED': 'services/persona_traits.py',
    'PERSONA_REACTIVITY': 'services/persona_traits.py',
    'PERSONA_SENSITIVITY': 'services/persona_traits.py',
    'STYLE_VAULT_ENABLED': 'services/style_vault.py',
    'STYLE_VAULT_MAX_SAMPLES': 'services/style_vault.py',
    'STYLE_VAULT_SAMPLE_CHARS': 'services/style_vault.py',
    'STYLE_VAULT_MAX_UPLOAD_CHARS': 'services/style_vault.py',
    'HEARTBEAT_TIMEOUT_SEC': 'plugins/tool_dispatcher/__init__.py',
    'HISTORY_MAX': 'services/napcat_tools.py',
    'HIST_LINE_BASE': 'services/llm.py',
    'HIST_LINE_LONG': 'services/llm.py',
    'HIST_LONG_AT': 'services/llm.py',
    'HIST_TOTAL_BUDGET': 'services/llm.py',
    'IMAGE_DEDUP_HOURS': 'plugins/vision_agent/__init__.py',
    'OCR_MAX_SIDE': 'services/ocr.py',
    'OCR_UPSCALE_MIN_W': 'services/ocr.py',
    'OCR_MAX_CROPS': 'services/ocr.py',
    'OCR_TIMEOUT': 'services/ocr.py',
    'LIBRARY_COOLDOWN': 'services/llm.py',
    'PIPELINE_BUDGET': 'services/reply_pipeline.py',
    'LLM_DAILY_BUDGET': 'services/llm_usage.py',
    'LLM_BUDGET_SOFT_RATIO': 'services/budget_guard.py',
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
    # MAX_BUBBLES 已 LEGACY（合并进 OUT_BUBBLES_MAX），不再有代码读点，故不进 FILES
    'MAX_GROUP_RECENT': 'plugins/chat/__init__.py',
    'MAX_REQ_ROUNDS': 'services/llm.py',
    'MAX_REQ_ROUNDS_FAST': 'services/llm.py',
    'MAX_TOOL_TURNS': 'services/llm.py',
    'LONG_DOC_ENABLED': 'services/long_doc.py',
    'LONG_DOC_MAX_TOKENS': 'services/long_doc.py',
    'LONG_DOC_EVIDENCE': 'services/long_doc.py',
    'LONG_DOC_PIECES': 'services/long_doc.py',
    'LONG_DOC_PIECE_CHARS': 'services/long_doc.py',
    'MEMORY_REVISIT_AGE_DAYS': 'services/memory_v2.py',
    'MEMORY_REVISIT_IDLE_DAYS': 'services/memory_v2.py',
    'MERGE_WINDOW': 'plugins/chat/__init__.py',
    'SEGMENT_WAIT_SEC': 'services/segments.py',
    'SEGMENT_WAIT_FAST': 'plugins/chat/__init__.py',
    'SEGMENT_MAX_SEC': 'services/segments.py',
    'SEGMENT_MAX_MSGS': 'services/segments.py',
    'SEGMENT_HISTORY_KEEP': 'services/segments.py',
    'SEG_INSERT_WINDOW': 'services/segments.py',
    'MOOD_GLOBAL_SHARE': 'services/mood.py',
    'MOOD_IDLE_AFTER': 'services/mood.py',
    'MOOD_IDLE_STEP': 'services/mood.py',
    'MOOD_IDLE_TICK': 'services/mood.py',
    'MOOD_PEAK_WINDOW_SEC': 'services/mood.py',
    'MOOD_PEAK_MIN_DELTA': 'services/mood.py',
    'MOOD_JUDGE_CTX': 'services/mood_judge.py',
    'MOOD_JUDGE_FRESH_SEC': 'services/mood.py',
    'MOOD_JUDGE_MIN_INTERVAL': 'services/mood_judge.py',
    'MOOD_JUDGE_MAX_PER_HOUR': 'services/mood_judge.py',
    'MOOD_JUDGE_REFRESH': 'services/mood_judge.py',
    'MOOD_JUDGE_SMOOTH': 'services/mood.py',
    'ATMO_ENABLED': 'services/atmosphere.py',
    'ATMO_MSGS': 'services/atmosphere.py',
    'ATMO_TTL': 'services/atmosphere.py',
    'ATMO_TIMEOUT': 'services/atmosphere.py',
    'ATMO_MAX_CHARS': 'services/atmosphere.py',
    'ATMO_INTENT': 'services/atmosphere.py',
    'ATMO_KIND': 'services/atmosphere.py',
    # ATMO_GATE 由 plugins/chat 的插话循环读（atmosphere.gate_on() 内部用）
    'ATMO_GATE': 'services/atmosphere.py',
    'ARCHIVE_ENABLED': 'services/archive.py',
    'ARCHIVE_MIN_AGE_DAYS': 'services/archive.py',
    'ARCHIVE_TOP': 'services/archive.py',
    'ARCHIVE_MAX_CHARS': 'services/archive.py',
    'MOOD_MAX_STEP': 'services/mood.py',
    'MOOD_MAX_DELTA': 'services/mood.py',
    'MOOD_JUDGE_TIMEOUT': 'services/mood_judge.py',
    'MOOD_LABEL_MAX': 'services/mood.py',
    'MOOD_LABEL_TTL_HOURS': 'services/mood.py',
    'MOOD_MAX_SESSIONS': 'services/mood.py',
    'MOOD_THINK_KEEP': 'services/mood_judge.py',
    'MOOD_FOLLOW_MIN': 'plugins/chat/__init__.py',
    'MOOD_HALF_LIFE_HOURS': 'services/mood.py',
    'MOOD_NOISE_DELTA': 'services/mood_judge.py',
    'MOOD_ENV_MAX_DELTA': 'services/mood_judge.py',
    'MEMORY_EMOTION_MIN_DELTA': 'plugins/chat/__init__.py',
    'PROFILE_NOTE_MIN_DELTA': 'services/profiles.py',
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
    'PROACTIVE_GROUP_MAX_PER_DAY': 'services/proactive.py',
    'PROACTIVE_ACCEPT_WINDOW_H': 'services/proactive.py',
    'PROACTIVE_ACCEPT_BONUS': 'services/proactive.py',
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
    'GROUP_MOOD_CHAT_MIN': 'services/group_mood.py',
    'GROUP_MOOD_CHAT_MAX': 'services/group_mood.py',
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
    'VISION_REPORT': 'services/vision_report.py',
    'VISION_GLM_MODE': 'services/vision_verify.py',
    'VISION_TEXT_MAX': 'services/vision_report.py',
}


# ============ 设计默认参数集（2026-11-25） ============
# 这是**唯一**的参数默认值来源：所有可调参数都在本文件。
# 改法：① 改这里（等于改默认值）② 在 .env 写同名键（推荐，改完重启）
# 校验：python3 scripts/check_tuning.py
# 说明：docs/tuning.md（全量表） / docs/tuning-guide.md（怎么调）
DEFAULTS.update({
    'MOOD_HALF_LIFE_GROUP_MIN': 30,
    'MOOD_HALF_LIFE_PRIVATE_MIN': 120,
    'MOOD_HALF_LIFE_GLOBAL_MIN': 360,
    'MOOD_STEP_1': 0.1,
    'MOOD_STEP_2': 0.22,
    'MOOD_STEP_3': 0.40,
    'MOOD_IMPACT_MAX': 3,
    # 2026-11-27（外部审计）：prompt 写了「±3 一天最多一次」，但代码没有计数。
    # 现在按**会话**按自然日计数；超了把 ±3 降到 ±2（保留事件强度，只是不再顶格）。
    # 0 = 不限（回滚成旧行为）。
    'MOOD_IMPACT3_DAILY_MAX': 1,
    'MOOD_IMPACT3_OVER_TO': 2,
    'MOOD_SENS_MIN': 0.6,
    'MOOD_SENS_MAX': 1.4,
    'MOOD_SATURATION': 0.6,
    'MOOD_HARD_CAP': 0.35,
    'MOOD_P0': 0.25,
    # 2026-11-26 用户拍板：「impact≥2 时回补一点 P」。
    # 稳态 P 长期贴地（P_FLOOR=0.005）→ K≈0.04 → ±3 只挪 0.012，等于推不动。
    # 只在 |impact|≥2 时把 P 抬起来，让「突发事件」真的能推动心情。
    # 有上限（P_EVENT_CAP）兜着：K 最大 ≈0.63，±3 的 dv ≈0.176，仍远低于 HARD_CAP 0.35。
    'MOOD_P_EVENT_BOOST': 0.05,
    'MOOD_P_EVENT_CAP': 0.20,
    'MOOD_Q_RATE_BASE': 0.02,
    'MOOD_Q_RATE_SLOPE': 0.1,
    'MOOD_R_BASE': 0.02,
    'MOOD_R_SLOPE': 0.6,
    'MOOD_WINDOW_MAX_SEC': 300,
    'MOOD_FAST_CONF': 0.4,
    'MOOD_FAST_FLOOR': -0.45,
    'MOOD_FAST_FLOOR_LIGHT': -0.25,
    'MOOD_FAST_P_FACTOR': 1.5,
    'MOOD_RELEVANCE_GATE': 0.25,
    'MOOD_ANCHOR_EVERY': 20,
    'MOOD_ANCHOR_IDLE_MIN': 30,
    # 静默锚点定时器：1=开（每 5 分钟扫一次），0=关（回到「下条消息补锚」）
    'MOOD_ANCHOR_TIMER': 1,
    # ⚠️ 2026-11-27（外部审计）：以前 = 1800，和 IDLE_MIN(30min) 完全相等，
    # 而 tick 是 5 分钟一轮 → 静默 30 分钟后 THINK 必然 >1800s，
    # 「静默锚点」几乎永远不触发。留 5 分钟余量（35min）让 tick 能命中。
    'MOOD_ANCHOR_THINK_AGE_SEC': 2100,
    # 2026-11-26 用户拍板：anchor_level 必填，且**占大头** —— 每窗按权重混入，
    # 不再是「每 20 窗纠偏一次」。0.45 = 每窗把 v 往绝对档位拉 45%×conf。
    'MOOD_ANCHOR_WEIGHT': 0.45,
    'MOOD_ANCHOR_GAIN': 0.25,
    'MOOD_ANCHOR_DEADBAND': 0.15,
    'MOOD_ANCHOR_MAX_MOVE': 0.2,
    'MOOD_ANCHOR_P_ADD': 0.02,
    'MOOD_ANCHOR_LEVEL1': 0.35,
    'MOOD_ANCHOR_LEVEL2': 0.7,
    'MOOD_ANCHOR_COUNT_SKIPPED': 1,
    # a/d（唤醒/掌控）：一期只记录（衰减 + 展示），不进 z/Δ
    'MOOD_HALF_LIFE_A_GROUP_MIN': 15,
    'MOOD_HALF_LIFE_A_PRIVATE_MIN': 60,
    'MOOD_HALF_LIFE_A_GLOBAL_MIN': 60,
    'MOOD_HALF_LIFE_D_GROUP_MIN': 120,
    'MOOD_HALF_LIFE_D_PRIVATE_MIN': 360,
    'MOOD_HALF_LIFE_D_GLOBAL_MIN': 360,
    'MOOD_AD_BASE_AROUSAL': 0.35,
    'MOOD_AD_BASE_DOMINANCE': 0.5,
    'REL_AFFINITY_BASE': 0.0,
    'REL_AFFINITY_HALF_LIFE_DAYS': 7,
    'REL_TRUST_BASE': 0.2,
    'REL_TRUST_HALF_LIFE_DAYS': 45,
    'REL_RESPECT_BASE': 0.3,
    'REL_RESPECT_HALF_LIFE_DAYS': 90,
    'REL_INTIMACY_BASE': 0.0,
    'REL_INTIMACY_HALF_LIFE_DAYS': 30,
    'REL_DEPTH_INTIMACY_COEF': 2.0,
    'REL_DEPTH_CONTACT_COEF': 0.5,
    'REL_CONTACT_NORM_N': 50,
    'REL_NOVELTY_COEF': 0.6,
    'REL_DAILY_CAP': 0.15,
    # ---- 好感 v1（默认）：2026-11-26 起同时吃「事件强度 delta」+「实际位移 dv」----
    #   · DELTA_K 是事件重量项的系数（旧版等效 1.0，现在压到 0.25，所以不会那么快）；
    #   · DV_K 是 dv 三段式的系数（尾部 |dv|≥0.30 的一见钟情/心碎仍能跳）；
    #   · CAP 是单次上限：要装得下尾部跳变，不能再用旧的 ±0.10。
    'AFF_V1_DELTA_K': 0.25,
    'AFF_V1_DV_K': 0.30,
    # 2026-11-26 修正：原来写 0.36，但 dv 项天花板 = 0.30×AFF_W_MAX(0.42) = 0.126，
    # 事件项最多 ≈0.010 → 实测最大 0.136，0.36 **永远不可达**（又一个死护栏，ADR-028 刚批过）。
    # 现在取 = 日上限 0.15：单次本来就不可能超过日上限，这个键只防「直接调用写爆一天」。
    'AFF_V1_CAP': 0.15,
    # ---- 好感 v2（2026-11-26，AFF_MODEL=v2 启用；默认 v1 可回滚）----
    'AFF_MODEL': 'v1',
    'AFF_MAX': 0.99,
    'AFF_TRACE_T': 10.0,
    'AFF_TRACE_P': 0.5,
    'AFF_TRACE_MAX': 400,
    'AFF_BOND_MAX': 0.60,
    'AFF_BOND_EXP': 1.5,
    'AFF_BOND_DEEP_MAX': 0.30,
    'AFF_BOND_DEEP_EXP': 1.2,
    'AFF_DV_K': 0.60,
    'AFF_JUMP_FROM': 0.15,
    'AFF_JUMP_K': 1.6,
    'AFF_JUMP_GAMMA': 1.5,
    'AFF_JUMP_SAT': 0.35,
    'AFF_TAIL_FROM': 0.30,
    'AFF_TAIL_BASE': 0.06,
    'AFF_TAIL_K': 2.0,
    'AFF_TAIL_GAMMA': 1.8,
    'AFF_TAIL_SAT': 0.15,
    'AFF_W_MAX': 0.42,
    'AFF_GUARD_POS': 0.70,
    'AFF_NEG_BIAS': 1.6,
    'AFF_SAT_POS': 0.70,
    'AFF_SAT_NEG': 0.35,
    'AFF_PEAK_DV': 0.15,
    'AFF_LOVE_DV': 0.30,
    'AFF_WARM_FULL': 0.25,
    'AFF_PEAK_BONUS': 0.25,
    'AFF_HURT_GRUDGE': 0.15,
    'AFF_CONS_FROM': 0.75,
    'AFF_CONS_TO': 0.95,
    'AFF_CONS_K': 0.60,
    'AFF_TIER_SOFT': 0.02,
    'AFF_COMP_W': 0.004,
    'AFF_COMP_CAP': 0.80,
    'REL_NEG_BASE': 1.3,
    'REL_NEG_SLOPE': 0.6,
    'REL_TRUST_UP': 0.08,
    'REL_TRUST_DOWN': 0.25,
    'REL_RESPECT_UP': 0.05,
    'REL_RESPECT_DOWN': 0.1,
    'REL_GRUDGE_HALF_LIFE_DAYS': 14,
    # 2026-11-25 校准：按线上真实块长（字符/1.5）留 ~20% 余量，
    # 这样 CTX_ENFORCE=1 只裁异常膨胀，不会把正常块裁成残句。
    'CTX_PERSONA': 700,
    'CTX_EXPRESSION': 120,
    'CTX_PROTOCOL': 1500,
    'CTX_STATE': 240,
    'CTX_PROFILE': 160,
    'CTX_SUMMARY': 120,
    'CTX_EVIDENCE': 500,
    'CTX_STYLE': 250,
    'CTX_CUSTOM': 250,
    'CTX_RHYTHM': 180,
    'CTX_HISTORY': 150,
    'CTX_POST_RULES': 320,
    'CTX_ANCHOR': 200,
    'CTX_TOOLS': 300,
    'CTX_MULTITARGET': 120,
    'CTX_WHITE_MOON': 180,
    # 必须 ≥ expression+style+custom+rhythm+white_moon = 730，否则不变式自破
    'CTX_STYLE_TOTAL': 730,
    'CTX_SOFT_CAP': 2700,
    'CTX_HARD_CAP': 12000,
    # 块级预算是否真的裁剪：0=只统计/告警（默认，先看数据）；1=超限裁剪
    'CTX_ENFORCE': 0,
    # 同一个块超限告警的最小间隔（秒），避免每轮刷屏
    'CTX_WARN_SEC': 600.0,
    'CTX_LONG_DOC_EVIDENCE': 6000,
    'CTX_LONG_DOC_LIBRARY': 3000,
    'RET_CHAR_LATENCY_MS': 50,
    'RET_LIB_LATENCY_MS': 3000,
    'RET_MEMORY_TOP': 4,
    'RET_STYLE_TOP': 3,
    'RET_WORLD_TOP': 3,
    'RET_TOOLS_TOP': 6,
    'RET_EVIDENCE_MAX': 5,
    'RET_SAME_PERSON_RATIO': 0.6,
    'RET_MAX_PER_PERSON': 3,
    'RET_NEUTRAL_QUOTA': 1,
    'RET_RRF_K': 60,
    # 0=加权求和（默认，行为不变）；1=多路 RRF 融合（服务器侧先做，向量路仍留算力机）
    'RET_FUSION_RRF': 0,
    # 检索规划器（P4）：结构化过滤后的打分权重
    'RET_W_STRUCT': 1.2,
    'RET_W_BM25': 1.0,
    'RET_W_TAG': 0.8,
    'RET_W_STRENGTH': 0.6,
    'RET_W_RECENCY': 0.4,
    'RET_PLAN_CAND': 400,
    'RET_TAG_TOP': 5,
    'RET_STRENGTH_GAIN': 0.1,
    'RET_RECENCY_TAU_DAYS': 30.0,
    # 检索二期：显著性倒 U + 按 kind 的提取半衰期 + MMR 多样性
    'RET_W_SALIENCE': 0.5,
    'MEM_SAL_LOW': 0.15,
    'MEM_SAL_HIGH': 0.60,
    'MEM_SAL_SLOPE_LOW': 0.3,
    'MEM_SAL_SLOPE_MID': 2.1,
    'MEM_SAL_FLOOR': 0.045,
    'MEM_SAL_TAIL': 0.6,
    'MEM_BAD_BOOST': 1.15,
    'MEM_TAU_EVENT': 30.0,
    'MEM_TAU_BOND': 90.0,
    'MEM_TAU_FACT': 365.0,
    'MEM_TAU_PREFERENCE': 180.0,
    # 2026-11-26 补：`impression` 是**线上真实在写的 kind**
    # （MEMO 默认 kind、`/印象`、aura_logic 都写它），但 TAU_KIND 里一直没有它 →
    # 落到 MEM_TAU_DEFAULT=30，和随口一句 event 同寿命。
    # 「印象最深的一件事」一个月就衰减到 37%，明显不对，按 preference 同档 180 天。
    'MEM_TAU_IMPRESSION': 180.0,
    'MEM_TAU_STYLE': 365.0,
    'MEM_TAU_DEFAULT': 30.0,
    # 2026-11-26：超出 max_age_days 但被「高重要/强刻痕」放行的记忆，
    # 不再和窗内记忆同权 —— 乘这个系数降权（用户拍板：绕过可以，但要付代价）。
    'MEM_OLD_FACTOR': 0.5,
    'MEM_MMR_ENABLED': 1,
    'MEM_MMR_LAMBDA': 0.7,
    'MEM_MMR_CAND': 3,
    'STYLE_MMR_ENABLED': 1,
    'STYLE_MMR_LAMBDA': 0.7,
    # 流水线 p95 周报：内存保留多少个样本 / 是否落盘
    'PIPE_STAGE_SAMPLES': 500,
    'PIPE_METRICS': 1,
    'ADDR_AT_OR_QUOTE': 0.99,
    'ADDR_QUOTE': 0.95,
    'ADDR_VOCATIVE': 0.8,
    'ADDR_NAME': 0.7,
    'ADDR_QA_PAIR': 0.65,
    'ADDR_YOU': 0.6,
    'ADDR_BACKCHANNEL': 0.55,
    'ADDR_RECENT_MENTION': 0.5,
    'ADDR_ASSERT_MIN': 0.8,
    'ADDR_MAYBE_MIN': 0.5,
    'OUT_BUBBLE_CHARS': 40,
    'OUT_BUBBLES_MAX': 2
})

DOCS.update({
    'MOOD_HALF_LIFE_GROUP_MIN': '群聊心情半衰期（分钟）',
    'MOOD_HALF_LIFE_PRIVATE_MIN': '私聊心情半衰期（分钟）',
    'MOOD_HALF_LIFE_GLOBAL_MIN': '全局心情半衰期（分钟）',
    'MOOD_STEP_1': '观测步长 impact=±1（相对位移）',
    'MOOD_STEP_2': '观测步长 impact=±2',
    'MOOD_STEP_3': '观测步长 impact=±3（猛击：一见钟情/被背叛）',
    'MOOD_IMPACT_MAX': '判读档位上限（3 = 七档 -3~+3）',
    'MOOD_IMPACT3_DAILY_MAX': '±3 档每会话每天最多几次（超了降到 OVER_TO；0=不限）',
    'MOOD_IMPACT3_OVER_TO': '±3 超限后降到的档位（2 = 保留「大事」，但不再顶格）',
    'MOOD_SENS_MIN': '人设敏感度下限',
    'MOOD_SENS_MAX': '人设敏感度上限',
    'MOOD_SATURATION': '饱和系数：Δ×(1−s×abs(v))',
    'MOOD_HARD_CAP': '单次硬上限（±3 首条会触发：0.507→0.35，不是「永不触发」）',
    'MOOD_P0': '不确定度初值',
    'MOOD_P_EVENT_BOOST': '|impact|≥2 时 P 的回补量（让突发事件推得动心情）',
    'MOOD_P_EVENT_CAP': '回补后 P 的上限（防止连刷把 K 顶太高）',
    'MOOD_Q_RATE_BASE': '过程噪声速率（每小时，基数）',
    'MOOD_Q_RATE_SLOPE': '过程噪声速率（每小时，乘 reactivity）',
    'MOOD_R_BASE': '观测噪声基数',
    'MOOD_R_SLOPE': '观测噪声置信项系数',
    'MOOD_WINDOW_MAX_SEC': '判读窗口时间上限（秒）：不足 N 句但过了这么久也判（慢群防滞后）',
    'MOOD_FAST_CONF': '快速通道置信度',
    'MOOD_FAST_FLOOR': '快速通道强负面钳位（只降不升）',
    'MOOD_FAST_FLOOR_LIGHT': '快速通道轻负面钳位',
    'MOOD_FAST_P_FACTOR': '快速通道 P 放大（>1 让下一轮判读能拉回；上限 1）',
    'MOOD_RELEVANCE_GATE': '窗口相关度低于此值只衰减不判读',
    'MOOD_ANCHOR_EVERY': '锚点：每多少窗额外回补一次 P（每窗都已按 WEIGHT 混入）',
    'MOOD_ANCHOR_IDLE_MIN': '锚点：静默多少分钟也触发',
    'MOOD_ANCHOR_TIMER': '静默锚点定时器（1=开，0=只在下一条消息时补锚）',
    'MOOD_ANCHOR_THINK_AGE_SEC': '静默锚点只认多久内的 THINK（秒；必须 ≥ 静默时长 + tick 间隔，默认 2100）',
    'MOOD_ANCHOR_WEIGHT': '每窗锚点权重：v 向绝对档位拉 W×conf（anchor_level 占大头）',
    'MOOD_ANCHOR_GAIN': '独立锚点（静默定时器/手动）的增益 K_a = gain × confidence',
    'MOOD_ANCHOR_DEADBAND': '锚点软死区：差值小于它时按 smoothstep 衰减混入（不再硬跳过）',
    'MOOD_ANCHOR_MAX_MOVE': '锚点单次最大位移',
    'MOOD_ANCHOR_P_ADD': '锚点后 P 的回补量',
    'MOOD_ANCHOR_LEVEL1': '锚点 level ±1 对应的绝对偏移',
    'MOOD_ANCHOR_LEVEL2': '锚点 level ±2 对应的绝对偏移',
    'MOOD_ANCHOR_COUNT_SKIPPED': '1=被 relevance 跳过的窗口也计入锚点计数；0=只数真判读',
    'MOOD_HALF_LIFE_A_GROUP_MIN': '唤醒 a 的群聊半衰期（分钟）',
    'MOOD_HALF_LIFE_A_PRIVATE_MIN': '唤醒 a 的私聊半衰期（分钟）',
    'MOOD_HALF_LIFE_A_GLOBAL_MIN': '唤醒 a 的全局半衰期（分钟）',
    'MOOD_HALF_LIFE_D_GROUP_MIN': '掌控 d 的群聊半衰期（分钟）',
    'MOOD_HALF_LIFE_D_PRIVATE_MIN': '掌控 d 的私聊半衰期（分钟）',
    'MOOD_HALF_LIFE_D_GLOBAL_MIN': '掌控 d 的全局半衰期（分钟）',
    'MOOD_AD_BASE_AROUSAL': '唤醒 a 的回落基线（0~1）',
    'MOOD_AD_BASE_DOMINANCE': '掌控 d 的回落基线（0~1）',
    'REL_AFFINITY_BASE': '好感基线',
    'REL_AFFINITY_HALF_LIFE_DAYS': '好感半衰期（天，再乘深度系数）',
    'REL_TRUST_BASE': '信任基线',
    'REL_TRUST_HALF_LIFE_DAYS': '信任半衰期（天）',
    'REL_RESPECT_BASE': '尊重基线',
    'REL_RESPECT_HALF_LIFE_DAYS': '尊重半衰期（天）',
    'REL_INTIMACY_BASE': '亲近基线',
    'REL_INTIMACY_HALF_LIFE_DAYS': '亲近半衰期（天）',
    'REL_DEPTH_INTIMACY_COEF': '深度系数里 intimacy 的权重',
    'REL_DEPTH_CONTACT_COEF': '深度系数里联系频率的权重',
    'REL_CONTACT_NORM_N': '近 7 天互动次数归一化的分母',
    'REL_NOVELTY_COEF': '防刷：novelty = 1/(1+c×n)',
    'REL_DAILY_CAP': '信任+尊重每日净变化上限（不是好感；好感用 AFFINITY_DAILY_CAP）',
    'AFF_MODEL': '好感模型开关（v1=指数衰减 + delta/dv 双吃 / v2=痕迹求和）',
    'AFF_V1_DELTA_K': 'v1 事件重量项系数（旧版等效 1.0；压小 = 好感不再那么快）',
    'AFF_V1_DV_K': 'v1 实际位移项系数（乘三段式；尾部跳变仍能到单次上限）',
    'AFF_V1_CAP': 'v1 单次好感增量上限（= 日上限 0.15；实测最大 0.136，旧的 ±0.10 装不下尾部）',
    'AFF_MAX': '好感硬上限（0.99，不给 1.0）',
    'AFF_TRACE_T': '痕迹衰减时间常数（天）',
    'AFF_TRACE_P': '痕迹幂律指数（ACT-R d≈0.5）',
    'AFF_TRACE_MAX': '每人最多保留的痕迹天数（按天聚合）',
    'AFF_BOND_MAX': '浅层投入的 bond 上限',
    'AFF_BOND_EXP': '浅层 bond 随投入的陡度',
    'AFF_BOND_DEEP_MAX': '深层投入额外 bond（里程碑/修复/暖天数）',
    'AFF_BOND_DEEP_EXP': '深层 bond 随 I_deep 的陡度',
    'AFF_DV_K': '日常区 dv→好感 换算系数',
    'AFF_JUMP_FROM': '显著区起点 |dv|（骤暖/骤冷）',
    'AFF_JUMP_K': '显著区放大系数',
    'AFF_JUMP_GAMMA': '显著区凸性指数',
    'AFF_JUMP_SAT': '显著区饱和指数（高好感也推得动）',
    'AFF_TAIL_FROM': '尾部区起点 |dv|（心碎/一见钟情）',
    'AFF_TAIL_BASE': '尾部区基础跳变',
    'AFF_TAIL_K': '尾部区放大系数',
    'AFF_TAIL_GAMMA': '尾部区凸性指数',
    'AFF_TAIL_SAT': '尾部区饱和指数（几乎不看当前好感）',
    'AFF_W_MAX': '单次好感痕迹硬上限（单次上限；当前 dv 上限 0.35 → 最大 0.362，0.42 为 ±4/放宽预留，不是死护栏）',
    'AFF_GUARD_POS': 'a<0 时正向跳变折扣（有防备）',
    'AFF_NEG_BIAS': '负面事件权重',
    'AFF_SAT_POS': '日常区正向饱和指数',
    'AFF_SAT_NEG': '日常区负向饱和指数',
    'AFF_PEAK_DV': 'peak 计数阈值（显著事件）',
    'AFF_LOVE_DV': '一见钟情阈值（peaks+2、κ≥0.5）',
    'AFF_WARM_FULL': '7 天暖流达到多少算「一直在暖」（κ 满载）',
    'AFF_PEAK_BONUS': '一次 peak 额外加的巩固比例',
    'AFF_HURT_GRUDGE': '负向 peak 加多少记仇',
    'AFF_CONS_FROM': '巩固起点 a',
    'AFF_CONS_TO': '巩固满载点 a',
    'AFF_CONS_K': '满载时沉淀进 bond 的比例',
    'AFF_TIER_SOFT': '分层门槛软上限余量',
    'AFF_COMP_W': '一句中性陪伴的痕迹权重',
    'AFF_COMP_CAP': '陪伴通道天花板',
    'REL_NEG_BASE': '负面权重基数（坏比好强）',
    'REL_NEG_SLOPE': '负面权重里 forgiveness 的削弱系数',
    'REL_TRUST_UP': '信任正向步长',
    'REL_TRUST_DOWN': '信任失信步长',
    'REL_RESPECT_UP': '尊重正向步长',
    'REL_RESPECT_DOWN': '尊重负向步长',
    'REL_GRUDGE_HALF_LIFE_DAYS': '记仇半衰期（天）',
    'CTX_PERSONA': '人格块上限（token）',
    'CTX_EXPRESSION': '口癖块上限',
    'CTX_PROTOCOL': '协议块上限',
    'CTX_STATE': '状态卡上限',
    'CTX_PROFILE': '档案块上限（不含白月光）',
    'CTX_SUMMARY': '群摘要上限',
    'CTX_EVIDENCE': '证据块上限',
    'CTX_STYLE': '对方原话样本上限',
    'CTX_CUSTOM': '自定义样本上限（替代 style）',
    'CTX_RHYTHM': '表达节奏上限',
    'CTX_HISTORY': '历史行上限',
    'CTX_POST_RULES': '硬提醒块上限',
    'CTX_ANCHOR': '引用锚点上限',
    'CTX_TOOLS': '工具说明上限（仅 intent=tool）',
    'CTX_MULTITARGET': '多目标块上限',
    'CTX_WHITE_MOON': '白月光 1 行事实上限',
    'CTX_STYLE_TOTAL': '风格相关合计上限（= expression120+style250+rhythm180+white_moon180=730；custom 另算）',
    'CTX_SOFT_CAP': '非 long_doc 的硬截断',
    'CTX_HARD_CAP': 'long_doc 时的硬上限',
    'CTX_ENFORCE': '块级预算是否真裁剪（0=只告警，1=超限裁剪）',
    'CTX_WARN_SEC': '同一个块超限告警的最小间隔（秒）',
    'CTX_LONG_DOC_EVIDENCE': 'long_doc 时证据上限',
    'CTX_LONG_DOC_LIBRARY': 'long_doc 时图书馆上限',
    'RET_CHAR_LATENCY_MS': '角色脑延迟目标（ms）',
    'RET_LIB_LATENCY_MS': '图书馆延迟目标（ms）',
    'RET_MEMORY_TOP': '记忆块注入条数上限（0=不指定时用它）',
    'RET_STYLE_TOP': '风格候选池条数',
    'RET_WORLD_TOP': '图书馆候选池条数',
    'RET_TOOLS_TOP': '工具检索条数',
    'RET_EVIDENCE_MAX': '最终注入证据条数上限',
    'RET_SAME_PERSON_RATIO': '同一人占证据的比例上限',
    'RET_MAX_PER_PERSON': '同一人最多几条（=5×60%）',
    'RET_NEUTRAL_QUOTA': '前 N 条至少 1 条中性/反向',
    'RET_RRF_K': 'RRF 融合常数（1=开启多路 RRF）',
    'RET_FUSION_RRF': '检索融合：0=加权求和，1=多路 RRF',
    'RET_W_STRUCT': '打分：人/时间/情绪命中权重',
    'RET_W_BM25': '打分：关键词**覆盖率**权重（键名沿用历史；sqlite 侧不是 BM25，没有 idf）',
    'RET_W_TAG': '打分：标签重合权重',
    'RET_W_STRENGTH': '打分：提取强度权重',
    'RET_W_RECENCY': '打分：时间新鲜度权重',
    'RET_PLAN_CAND': '规划器一次最多取多少候选',
    'RET_TAG_TOP': '每条记忆最多打几个 topic 标签',
    'RET_STRENGTH_GAIN': '命中后提取强度回补：r += gain×(1−r)',
    'RET_RECENCY_TAU_DAYS': '新鲜度衰减时间常数（天）',
    'RET_W_SALIENCE': '打分：显著性 w（倒 U）权重',
    'MEM_SAL_LOW': '倒 U 低段上界（u<此值：小冲击几乎不记）',
    'MEM_SAL_HIGH': '倒 U 高段起点（u>此值：极端冲击回落）',
    'MEM_SAL_SLOPE_LOW': '倒 U 低段斜率（w=slope×u）',
    'MEM_SAL_SLOPE_MID': '倒 U 中段斜率',
    'MEM_SAL_FLOOR': '倒 U 中段起点 w（u=low 时的值）',
    'MEM_SAL_TAIL': '倒 U 极端回落的终点 w（u=1 时）',
    'MEM_BAD_BOOST': '负性记忆存储加成（坏比好强）',
    'MEM_TAU_EVENT': 'event 提取半衰期（天）',
    'MEM_TAU_BOND': 'bond 提取半衰期（天）',
    'MEM_TAU_FACT': 'fact 提取半衰期（天）',
    'MEM_TAU_PREFERENCE': 'preference 提取半衰期（天）',
    'MEM_TAU_IMPRESSION': 'impression 提取半衰期（天，她对这个人的印象；以前漏配 → 掉到 30）',
    'MEM_TAU_STYLE': 'style 提取半衰期（天）',
    'MEM_TAU_DEFAULT': '未知 kind 的提取半衰期（天）',
    'MEM_OLD_FACTOR': '超时间窗但被放行的记忆的降权系数（0.5 = 减半）',
    'MEM_MMR_ENABLED': '证据选择是否启用 MMR 多样性',
    'MEM_MMR_LAMBDA': 'MMR 相关性权重（1=只看分数，0=只看差异）',
    'MEM_MMR_CAND': 'MMR 候选倍数（limit×N）',
    'STYLE_MMR_ENABLED': '风格样本选择是否启用 MMR',
    'STYLE_MMR_LAMBDA': '风格 MMR 相关性权重（λ=0.7）',
    'PIPE_STAGE_SAMPLES': '流水线每步在内存里保留多少个耗时样本（算 p95）',
    'PIPE_METRICS': '流水线是否把每轮各步耗时落盘（周报 p95 用）',
    'ADDR_AT_OR_QUOTE': '@ 或引用她的置信度',
    'ADDR_QUOTE': '引用（非 @）的置信度',
    'ADDR_VOCATIVE': '开头称呼的置信度',
    'ADDR_NAME': '正文点名的置信度',
    'ADDR_QA_PAIR': '上一句提问→这句回答的置信度',
    'ADDR_YOU': '用了「你」的置信度',
    'ADDR_BACKCHANNEL': '反应词/附和的置信度',
    'ADDR_RECENT_MENTION': '短句冲最近被提到的人的置信度',
    'ADDR_ASSERT_MIN': '≥此值才敢断言「→ X」',
    'ADDR_MAYBE_MIN': '0.5~0.8 写「像是在回 X」；<0.5 写不确定',
    'OUT_BUBBLE_CHARS': '单泡汉字上限',
    'OUT_BUBBLES_MAX': '单轮气泡上限'
})

FILES.update({
    'MOOD_HALF_LIFE_GROUP_MIN': 'services/mood.py',
    'MOOD_HALF_LIFE_PRIVATE_MIN': 'services/mood.py',
    'MOOD_HALF_LIFE_GLOBAL_MIN': 'services/mood.py',
    'MOOD_STEP_1': 'services/mood.py',
    'MOOD_STEP_2': 'services/mood.py',
    'MOOD_STEP_3': 'services/mood.py',
    'MOOD_IMPACT_MAX': 'services/mood.py',
    'MOOD_IMPACT3_DAILY_MAX': 'services/mood.py',
    'MOOD_IMPACT3_OVER_TO': 'services/mood.py',
    'MOOD_SENS_MIN': 'services/mood.py',
    'MOOD_SENS_MAX': 'services/mood.py',
    'MOOD_SATURATION': 'services/mood.py',
    'MOOD_HARD_CAP': 'services/mood.py',
    'MOOD_P0': 'services/mood.py',
    'MOOD_P_EVENT_BOOST': 'services/mood.py',
    'MOOD_P_EVENT_CAP': 'services/mood.py',
    'MOOD_Q_RATE_BASE': 'services/mood.py',
    'MOOD_Q_RATE_SLOPE': 'services/mood.py',
    'MOOD_R_BASE': 'services/mood.py',
    'MOOD_R_SLOPE': 'services/mood.py',
    'MOOD_WINDOW_MAX_SEC': 'services/mood_judge.py',
    'MOOD_FAST_CONF': 'services/mood.py',
    'MOOD_FAST_FLOOR': 'services/mood.py',
    'MOOD_FAST_FLOOR_LIGHT': 'services/mood.py',
    'MOOD_FAST_P_FACTOR': 'services/mood.py',
    'MOOD_RELEVANCE_GATE': 'services/mood.py',
    'MOOD_ANCHOR_EVERY': 'services/mood.py',
    'MOOD_ANCHOR_IDLE_MIN': 'services/mood.py',
    'MOOD_ANCHOR_TIMER': 'services/mood_judge.py',
    'MOOD_ANCHOR_THINK_AGE_SEC': 'services/mood_judge.py',
    'MOOD_ANCHOR_WEIGHT': 'services/mood.py',
    'MOOD_ANCHOR_GAIN': 'services/mood.py',
    'MOOD_ANCHOR_DEADBAND': 'services/mood.py',
    'MOOD_ANCHOR_MAX_MOVE': 'services/mood.py',
    'MOOD_ANCHOR_P_ADD': 'services/mood.py',
    'MOOD_ANCHOR_LEVEL1': 'services/mood.py',
    'MOOD_ANCHOR_LEVEL2': 'services/mood.py',
    'MOOD_ANCHOR_COUNT_SKIPPED': 'services/mood.py',
    'MOOD_HALF_LIFE_A_GROUP_MIN': 'services/mood.py',
    'MOOD_HALF_LIFE_A_PRIVATE_MIN': 'services/mood.py',
    'MOOD_HALF_LIFE_A_GLOBAL_MIN': 'services/mood.py',
    'MOOD_HALF_LIFE_D_GROUP_MIN': 'services/mood.py',
    'MOOD_HALF_LIFE_D_PRIVATE_MIN': 'services/mood.py',
    'MOOD_HALF_LIFE_D_GLOBAL_MIN': 'services/mood.py',
    'MOOD_AD_BASE_AROUSAL': 'services/mood.py',
    'MOOD_AD_BASE_DOMINANCE': 'services/mood.py',
    'REL_AFFINITY_BASE': 'services/relationship.py',
    'REL_AFFINITY_HALF_LIFE_DAYS': 'services/relationship.py',
    'REL_TRUST_BASE': 'services/relationship.py',
    'REL_TRUST_HALF_LIFE_DAYS': 'services/relationship.py',
    'REL_RESPECT_BASE': 'services/relationship.py',
    'REL_RESPECT_HALF_LIFE_DAYS': 'services/relationship.py',
    'REL_INTIMACY_BASE': 'services/relationship.py',
    'REL_INTIMACY_HALF_LIFE_DAYS': 'services/profiles.py',
    'REL_DEPTH_INTIMACY_COEF': 'services/relationship.py',
    'REL_DEPTH_CONTACT_COEF': 'services/relationship.py',
    'REL_CONTACT_NORM_N': 'services/relationship.py',
    'REL_NOVELTY_COEF': 'services/relationship.py',
    'REL_DAILY_CAP': 'services/relationship.py',
    'AFF_MODEL': 'services/profiles.py',
    'AFF_V1_DELTA_K': 'services/profiles.py',
    'AFF_V1_DV_K': 'services/profiles.py',
    'AFF_V1_CAP': 'services/profiles.py',
    'AFF_MAX': 'services/profiles.py',
    'AFF_TRACE_T': 'services/profiles.py',
    'AFF_TRACE_P': 'services/profiles.py',
    'AFF_TRACE_MAX': 'services/profiles.py',
    'AFF_BOND_MAX': 'services/profiles.py',
    'AFF_BOND_EXP': 'services/profiles.py',
    'AFF_BOND_DEEP_MAX': 'services/profiles.py',
    'AFF_BOND_DEEP_EXP': 'services/profiles.py',
    'AFF_DV_K': 'services/profiles.py',
    'AFF_JUMP_FROM': 'services/profiles.py',
    'AFF_JUMP_K': 'services/profiles.py',
    'AFF_JUMP_GAMMA': 'services/profiles.py',
    'AFF_JUMP_SAT': 'services/profiles.py',
    'AFF_TAIL_FROM': 'services/profiles.py',
    'AFF_TAIL_BASE': 'services/profiles.py',
    'AFF_TAIL_K': 'services/profiles.py',
    'AFF_TAIL_GAMMA': 'services/profiles.py',
    'AFF_TAIL_SAT': 'services/profiles.py',
    'AFF_W_MAX': 'services/profiles.py',
    'AFF_GUARD_POS': 'services/profiles.py',
    'AFF_NEG_BIAS': 'services/profiles.py',
    'AFF_SAT_POS': 'services/profiles.py',
    'AFF_SAT_NEG': 'services/profiles.py',
    'AFF_PEAK_DV': 'services/profiles.py',
    'AFF_LOVE_DV': 'services/profiles.py',
    'AFF_WARM_FULL': 'services/profiles.py',
    'AFF_PEAK_BONUS': 'services/profiles.py',
    'AFF_HURT_GRUDGE': 'services/profiles.py',
    'AFF_CONS_FROM': 'services/profiles.py',
    'AFF_CONS_TO': 'services/profiles.py',
    'AFF_CONS_K': 'services/profiles.py',
    'AFF_TIER_SOFT': 'services/profiles.py',
    'AFF_COMP_W': 'services/profiles.py',
    'AFF_COMP_CAP': 'services/profiles.py',
    'REL_NEG_BASE': 'services/relationship.py',
    'REL_NEG_SLOPE': 'services/relationship.py',
    'REL_TRUST_UP': 'services/relationship.py',
    'REL_TRUST_DOWN': 'services/relationship.py',
    'REL_RESPECT_UP': 'services/relationship.py',
    'REL_RESPECT_DOWN': 'services/relationship.py',
    'REL_GRUDGE_HALF_LIFE_DAYS': 'services/relationship.py',
    'CTX_PERSONA': 'services/ctx_budget.py',
    'CTX_EXPRESSION': 'services/ctx_budget.py',
    'CTX_PROTOCOL': 'services/ctx_budget.py',
    'CTX_STATE': 'services/ctx_budget.py',
    'CTX_PROFILE': 'services/prompt_builder.py',
    'CTX_SUMMARY': 'services/prompt_builder.py',
    'CTX_EVIDENCE': 'services/prompt_builder.py',
    'CTX_STYLE': 'services/ctx_budget.py',
    'CTX_CUSTOM': 'services/white_moon.py',
    'CTX_RHYTHM': 'services/ctx_budget.py',
    'CTX_HISTORY': 'services/ctx_budget.py',
    'CTX_POST_RULES': 'services/ctx_budget.py',
    'CTX_ANCHOR': 'services/ctx_budget.py',
    'CTX_TOOLS': 'services/ctx_budget.py',
    'CTX_MULTITARGET': 'services/ctx_budget.py',
    'CTX_WHITE_MOON': 'services/ctx_budget.py',
    'CTX_STYLE_TOTAL': 'services/ctx_budget.py',
    'CTX_SOFT_CAP': 'services/prompt_builder.py',
    'CTX_HARD_CAP': 'services/llm.py',
    'CTX_ENFORCE': 'services/ctx_budget.py',
    'CTX_WARN_SEC': 'services/ctx_budget.py',
    'CTX_LONG_DOC_EVIDENCE': 'services/ctx_budget.py',
    'CTX_LONG_DOC_LIBRARY': 'services/ctx_budget.py',
    'RET_CHAR_LATENCY_MS': 'services/memory_v2.py',
    'RET_LIB_LATENCY_MS': 'services/agent_client.py',
    'RET_MEMORY_TOP': 'services/prompt_builder.py',
    'RET_STYLE_TOP': 'services/white_moon.py',
    'RET_WORLD_TOP': 'services/agent_client.py',
    'RET_TOOLS_TOP': 'services/llm.py',
    'RET_EVIDENCE_MAX': 'services/memory_v2.py',
    'RET_SAME_PERSON_RATIO': 'services/memory_v2.py',
    'RET_MAX_PER_PERSON': 'services/memory_v2.py',
    'RET_NEUTRAL_QUOTA': 'services/memory_v2.py',
    'RET_RRF_K': 'services/memdb.py',
    'RET_FUSION_RRF': 'services/memdb.py',
    'RET_W_STRUCT': 'services/memdb.py',
    'RET_W_BM25': 'services/memdb.py',
    'RET_W_TAG': 'services/memdb.py',
    'RET_W_STRENGTH': 'services/memdb.py',
    'RET_W_RECENCY': 'services/memdb.py',
    'RET_PLAN_CAND': 'services/memdb.py',
    'RET_TAG_TOP': 'services/memdb.py',
    'RET_STRENGTH_GAIN': 'services/memdb.py',
    'RET_RECENCY_TAU_DAYS': 'services/memdb.py',
    'RET_W_SALIENCE': 'services/memdb.py',
    'MEM_SAL_LOW': 'services/memdb.py',
    'MEM_SAL_HIGH': 'services/memdb.py',
    'MEM_SAL_SLOPE_LOW': 'services/memdb.py',
    'MEM_SAL_SLOPE_MID': 'services/memdb.py',
    'MEM_SAL_FLOOR': 'services/memdb.py',
    'MEM_SAL_TAIL': 'services/memdb.py',
    'MEM_BAD_BOOST': 'services/memdb.py',
    'MEM_TAU_EVENT': 'services/memdb.py',
    'MEM_TAU_BOND': 'services/memdb.py',
    'MEM_TAU_FACT': 'services/memdb.py',
    'MEM_TAU_PREFERENCE': 'services/memdb.py',
    'MEM_TAU_IMPRESSION': 'services/memdb.py',
    'MEM_TAU_STYLE': 'services/memdb.py',
    'MEM_TAU_DEFAULT': 'services/memdb.py',
    'MEM_OLD_FACTOR': 'services/memdb.py',
    'MEM_MMR_ENABLED': 'services/memory_v2.py',
    'MEM_MMR_LAMBDA': 'services/memory_v2.py',
    'MEM_MMR_CAND': 'services/memory_v2.py',
    'STYLE_MMR_ENABLED': 'services/style_vault.py',
    'STYLE_MMR_LAMBDA': 'services/style_vault.py',
    'PIPE_STAGE_SAMPLES': 'services/reply_pipeline.py',
    'PIPE_METRICS': 'services/reply_pipeline.py',
    'ADDR_AT_OR_QUOTE': 'services/group_scene.py',
    'ADDR_QUOTE': 'services/group_scene.py',
    'ADDR_VOCATIVE': 'services/group_scene.py',
    'ADDR_NAME': 'services/group_scene.py',
    'ADDR_QA_PAIR': 'services/group_scene.py',
    'ADDR_YOU': 'services/group_scene.py',
    'ADDR_BACKCHANNEL': 'services/group_scene.py',
    'ADDR_RECENT_MENTION': 'services/group_scene.py',
    'ADDR_ASSERT_MIN': 'services/group_scene.py',
    'ADDR_MAYBE_MIN': 'services/group_scene.py',
    'OUT_BUBBLE_CHARS': 'plugins/chat/__init__.py',
    'OUT_BUBBLES_MAX': 'plugins/chat/__init__.py'
})

# 旧公式常数：P2 切到「步进 + 锚点」后删除。
# 现在不能删——mood.py / mood_judge.py 仍在读。
LEGACY = {
    'AFFINITY_GAIN',
    # 2026-11-26：AFFINITY_DAILY_CAP 不是 LEGACY —— profiles.bump_affinity 仍在用，
    # 是 v1 好感的日上限（v2 不用）。移出，避免「标了 ⚠️ 却还在跑」。
    'MAX_BUBBLES',   # 已合并进 OUT_BUBBLES_MAX（两处代码都改读后者）
    'MOOD_JUDGE_SMOOTH',
    'MOOD_MAX_STEP',
    # 2026-11-27：MOOD_MAX_DELTA 移出 LEGACY —— mood_judge 护栏 / mood.apply_delta
    # 仍在真实使用，标 LEGACY 会误导调参的人。
    'MOOD_HALF_LIFE_HOURS',
}

# ============ 决策层（2026-11-25，a/节奏） ============
DEFAULTS.update({
    'AROUSAL_LEVEL_LOW': 0.2,
    'AROUSAL_LEVEL_MID': 0.5,
    'AROUSAL_LEVEL_HIGH': 0.8,
    'AROUSAL_SMOOTH': 0.4,
    'RHYTHM_SUBTLE': 0.9,
    'RHYTHM_ACTIVE': 1.0,
    'RHYTHM_SCATTERED': 1.05,
    'RHYTHM_WILD': 1.1,
    'RHYTHM_OFF': 1.0
})
DOCS.update({
    'AROUSAL_LEVEL_LOW': 'judge arousal=low 对应的 a 值',
    'AROUSAL_LEVEL_MID': 'judge arousal=mid 对应的 a 值',
    'AROUSAL_LEVEL_HIGH': 'judge arousal=high 对应的 a 值',
    'AROUSAL_SMOOTH': 'a 平滑：a = (1-s)*old + s*level',
    'RHYTHM_SUBTLE': '表达节奏 subtle 的自驱乘数',
    'RHYTHM_ACTIVE': '表达节奏 active 的自驱乘数',
    'RHYTHM_SCATTERED': '表达节奏 scattered 的自驱乘数',
    'RHYTHM_WILD': '表达节奏 wild 的自驱乘数',
    'RHYTHM_OFF': '表达节奏 off 的自驱乘数（=不漂移，不是不说话）'
})
FILES.update({
    'AROUSAL_LEVEL_LOW': 'services/mood_judge.py',
    'AROUSAL_LEVEL_MID': 'services/mood_judge.py',
    'AROUSAL_LEVEL_HIGH': 'services/mood_judge.py',
    'AROUSAL_SMOOTH': 'services/mood.py',
    'RHYTHM_SUBTLE': 'services/proactive.py',
    'RHYTHM_ACTIVE': 'services/proactive.py',
    'RHYTHM_SCATTERED': 'services/proactive.py',
    'RHYTHM_WILD': 'services/proactive.py',
    'RHYTHM_OFF': 'services/proactive.py'
})


# ============ 未接线（设计先行，代码还没读） ============
# 这些键在 FILES 里写了模块，但那个模块还没读它。
# check_tuning 会盯着：FILES 里的键若既没被读、也不在本集合里 → FAIL。
# 接线后**从这里删掉**（check 会提醒「已接线但还挂在名单里」）。
UNWIRED = frozenset({
    # 2026-11-25：全部设计键已接线；这里保留空集合，check_tuning 继续盯着。
    # 2026-11-27（外部审计）：AFFINITY_GAIN 只有一句模块级赋值、没有任何使用，
    # 以前靠「有读点」混过检查。明确标成未接线。
    "AFFINITY_GAIN",
})

