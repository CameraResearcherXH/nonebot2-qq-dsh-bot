"""设计口径的**只读视图**（默认值住在 core/tuning.py）。

定位
----
- **参数默认值只有一个地方**：`core/tuning.py` 的 `DEFAULTS`（`KEYS` 共 120 个设计键；
  全部参数 377 个）。
  用户调试改那里或 `.env`；`docs/tuning.md` 由它生成。
- 本文件不存值，只把 tuning 的平铺键**分组 + 加语义**（区间、枚举、派生计算），
  供代码与文档校验使用。这样「改值」和「理解结构」不混在一起。

为什么读 DEFAULTS 而不是 tuning.f()
----------------------------------
`tuning.f()` 会被 `.env` 覆盖。本文件描述的是**设计默认集**，文档校验必须对着默认值，
否则本地一改 `.env`，`check_design_docs.py` 就会误报。

用法
----
    from core import design_constants as D
    D.CTX["persona"]            # 400
    D.MOOD["hard_cap"]          # 0.35
    D.ENUMS["target"]           # 5 值
    D.ctx_total()               # 2120
"""
from __future__ import annotations

try:
    from core import tuning as _t
except ImportError:  # 直接运行本文件时（python3 core/design_constants.py）
    import os as _os
    import sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from core import tuning as _t


def _v(key: str):
    if key not in _t.DEFAULTS:
        raise KeyError("design key missing in tuning.py: " + key)
    return _t.DEFAULTS[key]


# 分组字段 → tuning 键（唯一映射，check_design_docs 用它查漏）
KEYS = {
    "MOOD": {
        "half_life_group_min": "MOOD_HALF_LIFE_GROUP_MIN",
        "half_life_private_min": "MOOD_HALF_LIFE_PRIVATE_MIN",
        "half_life_global_min": "MOOD_HALF_LIFE_GLOBAL_MIN",
        "step_1": "MOOD_STEP_1",
        "step_2": "MOOD_STEP_2",
        "sensitivity_min": "MOOD_SENS_MIN",
        "sensitivity_max": "MOOD_SENS_MAX",
        "saturation": "MOOD_SATURATION",
        "hard_cap": "MOOD_HARD_CAP",
        "P0": "MOOD_P0",
        "Q_base": "MOOD_Q_RATE_BASE",
        "Q_slope": "MOOD_Q_RATE_SLOPE",
        "R_base": "MOOD_R_BASE",
        "R_slope": "MOOD_R_SLOPE",
        "judge_window": "MOOD_JUDGE_CTX",
        "window_max_sec": "MOOD_WINDOW_MAX_SEC",
        "fast_conf": "MOOD_FAST_CONF",
        "fast_floor": "MOOD_FAST_FLOOR",
        "fast_floor_light": "MOOD_FAST_FLOOR_LIGHT",
        "fast_p_factor": "MOOD_FAST_P_FACTOR",
        "relevance_gate": "MOOD_RELEVANCE_GATE",
    },
    "ANCHOR": {
        "every_turns": "MOOD_ANCHOR_EVERY",
        "idle_min": "MOOD_ANCHOR_IDLE_MIN",
        "gain": "MOOD_ANCHOR_GAIN",
        "deadband": "MOOD_ANCHOR_DEADBAND",
        "max_move": "MOOD_ANCHOR_MAX_MOVE",
        "p_add": "MOOD_ANCHOR_P_ADD",
        "count_skipped": "MOOD_ANCHOR_COUNT_SKIPPED",
        "half_life_a_group_min": "MOOD_HALF_LIFE_A_GROUP_MIN",
        "half_life_a_private_min": "MOOD_HALF_LIFE_A_PRIVATE_MIN",
        "half_life_a_global_min": "MOOD_HALF_LIFE_A_GLOBAL_MIN",
        "half_life_d_group_min": "MOOD_HALF_LIFE_D_GROUP_MIN",
        "half_life_d_private_min": "MOOD_HALF_LIFE_D_PRIVATE_MIN",
        "half_life_d_global_min": "MOOD_HALF_LIFE_D_GLOBAL_MIN",
        "ad_base_arousal": "MOOD_AD_BASE_AROUSAL",
        "ad_base_dominance": "MOOD_AD_BASE_DOMINANCE",
        "level1": "MOOD_ANCHOR_LEVEL1",
        "level2": "MOOD_ANCHOR_LEVEL2",
    },
    "REL": {
        "affinity_base": "REL_AFFINITY_BASE",
        "affinity_half_life_days": "REL_AFFINITY_HALF_LIFE_DAYS",
        "trust_base": "REL_TRUST_BASE",
        "trust_half_life_days": "REL_TRUST_HALF_LIFE_DAYS",
        "respect_base": "REL_RESPECT_BASE",
        "respect_half_life_days": "REL_RESPECT_HALF_LIFE_DAYS",
        "intimacy_base": "REL_INTIMACY_BASE",
        "intimacy_half_life_days": "REL_INTIMACY_HALF_LIFE_DAYS",
        "depth_intimacy_coef": "REL_DEPTH_INTIMACY_COEF",
        "depth_contact_coef": "REL_DEPTH_CONTACT_COEF",
        "contact_norm_n": "REL_CONTACT_NORM_N",
        "novelty_coef": "REL_NOVELTY_COEF",
        "daily_cap_aff": "REL_DAILY_CAP",
        "negative_weight_base": "REL_NEG_BASE",
        "negative_weight_slope": "REL_NEG_SLOPE",
        "trust_up": "REL_TRUST_UP",
        "trust_down": "REL_TRUST_DOWN",
        "respect_up": "REL_RESPECT_UP",
        "respect_down": "REL_RESPECT_DOWN",
        "grudge_half_life_days": "REL_GRUDGE_HALF_LIFE_DAYS",
    },
    "CTX": {
        "persona": "CTX_PERSONA",
        "expression": "CTX_EXPRESSION",
        "protocol": "CTX_PROTOCOL",
        "state": "CTX_STATE",
        "profile": "CTX_PROFILE",
        "summary": "CTX_SUMMARY",
        "evidence": "CTX_EVIDENCE",
        "style": "CTX_STYLE",
        "custom": "CTX_CUSTOM",
        "rhythm": "CTX_RHYTHM",
        "history": "CTX_HISTORY",
        "post_rules": "CTX_POST_RULES",
        "anchor": "CTX_ANCHOR",
        "tools": "CTX_TOOLS",
        "multitarget": "CTX_MULTITARGET",
        "white_moon": "CTX_WHITE_MOON",
        "style_total": "CTX_STYLE_TOTAL",
        "soft_cap": "CTX_SOFT_CAP",
        "hard_cap": "CTX_HARD_CAP",
        "evidence_long": "CTX_LONG_DOC_EVIDENCE",
        "library_long": "CTX_LONG_DOC_LIBRARY",
    },
    "RETRIEVAL": {
        "char_latency_ms": "RET_CHAR_LATENCY_MS",
        "lib_latency_ms": "RET_LIB_LATENCY_MS",
        "memory_top": "RET_MEMORY_TOP",
        "style_top": "RET_STYLE_TOP",
        "world_top": "RET_WORLD_TOP",
        "tools_top": "RET_TOOLS_TOP",
        "evidence_max_items": "RET_EVIDENCE_MAX",
        "same_person_ratio": "RET_SAME_PERSON_RATIO",
        "max_per_person": "RET_MAX_PER_PERSON",
        "neutral_quota": "RET_NEUTRAL_QUOTA",
        "rrf_k": "RET_RRF_K",
        "w_struct": "RET_W_STRUCT",
        "w_bm25": "RET_W_BM25",
        "w_tag": "RET_W_TAG",
        "w_strength": "RET_W_STRENGTH",
        "w_recency": "RET_W_RECENCY",
        "plan_candidates": "RET_PLAN_CAND",
        "tag_top": "RET_TAG_TOP",
        "strength_gain": "RET_STRENGTH_GAIN",
        "recency_tau_days": "RET_RECENCY_TAU_DAYS",
    },
    "ADDRESS": {
        "at_or_quote": "ADDR_AT_OR_QUOTE",
        "quote": "ADDR_QUOTE",
        "vocative": "ADDR_VOCATIVE",
        "name": "ADDR_NAME",
        "qa_pair": "ADDR_QA_PAIR",
        "you": "ADDR_YOU",
        "backchannel": "ADDR_BACKCHANNEL",
        "recent_mention": "ADDR_RECENT_MENTION",
        "assert_min": "ADDR_ASSERT_MIN",
        "maybe_min": "ADDR_MAYBE_MIN",
    },
    "OUTPUT": {
        "bubble_chars": "OUT_BUBBLE_CHARS",
        "bubbles_max": "OUT_BUBBLES_MAX",
    },
    "DECISION": {
        "arousal_low": "AROUSAL_LEVEL_LOW",
        "arousal_mid": "AROUSAL_LEVEL_MID",
        "arousal_high": "AROUSAL_LEVEL_HIGH",
        "arousal_smooth": "AROUSAL_SMOOTH",
        "rhythm_subtle": "RHYTHM_SUBTLE",
        "rhythm_active": "RHYTHM_ACTIVE",
        "rhythm_scattered": "RHYTHM_SCATTERED",
        "rhythm_wild": "RHYTHM_WILD",
        "rhythm_off": "RHYTHM_OFF",
    },
}

MOOD = {f: _v(k) for f, k in KEYS["MOOD"].items()}
# 值域不是参数，是类型约束
MOOD.update({
    "v_min": -1.0, "v_max": 1.0,
    "a_min": 0.0, "a_max": 1.0,
    "d_min": 0.0, "d_max": 1.0,
    "impact_min": -2, "impact_max": 2,
})

ANCHOR = {f: _v(k) for f, k in KEYS["ANCHOR"].items()}
ANCHOR["map"] = {
    -2: -ANCHOR["level2"],
    -1: -ANCHOR["level1"],
    0: 0.0,
    1: ANCHOR["level1"],
    2: ANCHOR["level2"],
}

REL = {f: _v(k) for f, k in KEYS["REL"].items()}
REL.update({
    "affinity_min": -1.0, "affinity_max": 1.0,
    "trust_min": 0.0, "trust_max": 1.0,
    "respect_min": 0.0, "respect_max": 1.0,
    "intimacy_min": 0.0, "intimacy_max": 1.0,
})

CTX = {f: _v(k) for f, k in KEYS["CTX"].items()}
# DESIGN §3.3 的**目标拆分**（字符）。与上面的 CTX_*（as-built 每块上限）不是一回事：
#   CTX_* 是运行时的「单块最多多少」（合计 4620，防止某块膨胀）；
#   BUDGET 是设计里的「常态 prompt 该多大」（合计 2120），总池由 PROMPT_BUDGET=2200 兜底。
# 两者混用会让 `check_design_docs` 拿「上限之和」去比「目标」，2026-11-25 修。
BUDGET = {
    "persona": 400, "expression": 120, "protocol": 180, "state": 120,
    "profile": 160, "summary": 120, "evidence": 500, "style": 150,
    "custom": 250, "rhythm": 40, "history": 150, "post_rules": 80,
    "anchor": 60, "tools": 300, "multitarget": 60, "white_moon": 40,
    "style_total": 350, "style_total_custom": 450,
    "soft_cap": 2700, "hard_cap": 12000,
    "evidence_long": 6000, "library_long": 3000,
}
RETRIEVAL = {f: _v(k) for f, k in KEYS["RETRIEVAL"].items()}
ADDRESS = {f: _v(k) for f, k in KEYS["ADDRESS"].items()}
OUTPUT = {f: _v(k) for f, k in KEYS["OUTPUT"].items()}
DECISION = {f: _v(k) for f, k in KEYS["DECISION"].items()}

# ---------------------------------------------------------------- 枚举（唯一准）
ENUMS = {
    # ADR-021：5 值。旧 3 值会把「第三个人骂她」记成 world，好感不动
    "target": ("speaker", "recipient", "other", "world", "self"),
    "route": ("character", "library", "both", "none"),
    "source": ("memory", "profile", "style", "world", "dialogue", "mood"),
    "to_kind": ("bot", "person", "group", "unknown"),
}

# 哪些 target 会改好感（emotion §5.2）
AFFINITY_TARGETS = ("speaker", "recipient", "other")


# ---------------------------------------------------------------- 派生
def ctx_total(*, tools: bool = False, custom: bool = False,
              multitarget: bool = False) -> int:
    """按块加总。默认 = 常态口径（单目标 / 无工具 / 无 custom）。"""
    blocks = ("persona", "expression", "protocol", "state", "profile",
              "summary", "evidence", "rhythm", "history", "post_rules",
              "anchor", "white_moon")
    total = sum(CTX[k] for k in blocks)
    total += CTX["custom"] if custom else CTX["style"]
    if tools:
        total += CTX["tools"]
    if multitarget:
        total += CTX["multitarget"]
    return total


def budget_total(*, tools: bool = False, custom: bool = False,
                 multitarget: bool = False) -> int:
    """DESIGN §3.3 的常态目标（字符）。默认 = 单目标 / 无工具 / 无 custom = 2120。"""
    blocks = ("persona", "expression", "protocol", "state", "profile",
              "summary", "evidence", "rhythm", "history", "post_rules",
              "anchor", "white_moon")
    total = sum(BUDGET[k] for k in blocks)
    total += BUDGET["custom"] if custom else BUDGET["style"]
    if tools:
        total += BUDGET["tools"]
    if multitarget:
        total += BUDGET["multitarget"]
    return total


def style_total(*, custom: bool = False) -> int:
    """风格相关（不含 persona）。"""
    return (CTX["expression"]
            + (CTX["custom"] if custom else CTX["style"])
            + CTX["rhythm"]
            + CTX["white_moon"])


def half_life_min(scope: str) -> int:
    return {
        "group": MOOD["half_life_group_min"],
        "private": MOOD["half_life_private_min"],
        "global": MOOD["half_life_global_min"],
    }[scope]


def assert_target(value: str) -> str:
    """写库/读库前用它兜一层，避免 3 值回流。"""
    if value not in ENUMS["target"]:
        raise ValueError(
            "target=%r 不在 %s 里（旧 3 值 speaker/recipient/world 已废弃）"
            % (value, ENUMS["target"]))
    return value


if __name__ == "__main__":
    print("常态(单目标/无工具/无custom) =", ctx_total())
    print("有工具 =", ctx_total(tools=True))
    print("custom =", ctx_total(custom=True))
    print("多目标 =", ctx_total(multitarget=True))
    print("最大组合 =", ctx_total(tools=True, custom=True, multitarget=True))
    print("风格相关 =", style_total(), "/ custom =", style_total(custom=True))
    print("target =", ENUMS["target"])
