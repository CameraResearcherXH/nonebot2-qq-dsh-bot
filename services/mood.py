# services/mood.py
"""
心情系统（分层：全局 + 会话）。

为什么分层（2026-11-17 审计）：
  以前 data/mood.json 里只有一个 value —— **全局单值**。
  群里有人骂她一句，她到另一个群、到私聊也一起冷淡。
  这既不符合人味，也让「一个群的冲突」污染所有关系。

现在的模型：
  · global  —— 整体状态（睡没睡好、今天顺不顺），所有会话都能感受到一点
  · session —— 单个群/私聊里的关系温度，只影响这个会话
  写入时：会话扣满额，全局只吃 GLOBAL_SHARE（默认 35%）；
  读取时：有会话记录就用会话值，没有就继承全局值。

存储格式（向后兼容）：
  旧：{"value": 0.0, "updated_at": ...}
  新：{"global": {...}, "sessions": {"group_123": {...}, ...}}
  读到旧格式会自动迁移。
"""
# 2026-11-25：本地 Python 3.8 也要能 import（PEP 585 的 list[dict] 在 3.8 会炸）。
from __future__ import annotations

from core import tuning
import functools
import json
import math
import os
import re
import threading
import time
from pathlib import Path

try:  # 允许脱离 nonebot 单独跑脚本（selftest / 数据修复）
    from nonebot import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)

try:
    import fcntl
except Exception:  # pragma: no cover - Windows 本地调试
    fcntl = None

# 允许用 MOOD_FILE 指向别处（测试/多实例隔离）
MOOD_FILE = Path(os.getenv("MOOD_FILE", "data/mood.json"))
MOOD_FILE.parent.mkdir(parents=True, exist_ok=True)

# 心情波动流水（给「两天一复盘」的 journal_agent 读，不进 Prompt）
MOOD_HISTORY = Path("data/mood_history.jsonl")

# ============ ADR-008：单一状态源 + 并发写（2026-11-25） ============
# 问题：mood.json 以前有 10+ 个函数各自 `_load() → 改 → _save()`。
#   · 同进程多线程（APScheduler / 主动私聊 / 回复链）会丢更新；
#   · 多进程（脚本 + 容器）可能读到写一半的文件。
# 做法：
#   ① 所有**写入口**用 `_serialized` 串行化（RLock，进程内）；
#   ② `_save` 用 `flock` 保护 `tmp → os.replace`（跨进程）；
#   ③ 读接口不抢锁（os.replace 是原子的，读到的永远是完整旧/新文件）。
_STATE_LOCK = threading.RLock()


def _serialized(fn):
    """把一次「load → 改 → save」包成不可分割的事务（进程内）。"""
    @functools.wraps(fn)
    def _wrapper(*a, **kw):
        with _STATE_LOCK:
            return fn(*a, **kw)
    return _wrapper

# 心情回到 0 的半衰期（设计默认 12 小时，可在 .env / tuning 调）。
# 注意：这是「慢慢忘掉」的兜底；真正防一边倒的是 Δ 入口的噪音丢弃
# （见 mood_judge：旁观/别人之间/她没说话 → Δ 压到 0 或 ±0.03）。
HALF_LIFE_HOURS = tuning.f("MOOD_HALF_LIFE_HOURS")

# 👇 她自己写的心情（原话）能管多久。
#    用户 2026-11-19：「心情不能是我们指定的几种，她要自己有写心情的权利。」
#    数值负责「多活泼/多冷淡」，**原话**负责「她此刻到底什么感觉」；
#    原话过期（默认 12 小时，和半衰期一致）就退回按数值描述，别让三天前的心情一直挂着。
MOOD_LABEL_TTL_H = tuning.f("MOOD_LABEL_TTL_HOURS")
MOOD_LABEL_MAX = tuning.i("MOOD_LABEL_MAX")

# 单次波动里，全局吃多少（剩下的算这个会话自己的）
GLOBAL_SHARE = tuning.f("MOOD_GLOBAL_SHARE")

# 👇 2026-11-20：数值只认第三方判读。
#    用户：「她自己记的心情并不可靠，还是要以三方 llm 为准……
#      心情阈值也只能以 llm 判定的来增减，而且这个增减肯定也不能太多。」
#    true  → MEMO/关键词只留原话，不动数值；数值由判读的 delta 驱动
#    false → 老行为（自报情绪也改数值），调试/回滚用
MOOD_FROM_JUDGE = os.getenv("MOOD_FROM_JUDGE", "true").lower() in ("1", "true", "yes", "on")
# 判读直接给「当前心情值」（-1~1），**不再有 delta/增量**。
# 1.0 = **直接采用判读值**（默认）。用户 2026-11-20：「只需要参考 llm 生成的
# 心情词、阈值和摘来的心理活动」——所以不做二次换算。
# 调小（如 0.6）可以让情绪过渡更慢，属于调试选项，不给模型看。
SMOOTH = tuning.f("MOOD_JUDGE_SMOOTH")
# 单次判读最多改变多少（外口径；内部 ×100）。防「一句话翻脸/一句话上头」。
MAX_STEP = tuning.f("MOOD_MAX_STEP")

# ============ 空闲衰减（2026-11-20 定稿） ============
# 有人聊天 → 由对话驱动；没人找她 → 每 5 分钟向 0 走 1 点（内部 -100~100）。
# 空闲 10 分钟后才开始，避免刚聊完就掉。
# ============ 心情的两套口径（2026-11-20 定稿） ============
#   · 判读模型报的是「这件事让她涨/跌多少」（Δ），不是当前心情值；
#   · 当前心情值是**一个持续累加的变量**（-1~1），随时增减；
#   · 心情**名词**不再由模型每次自由起，而是按数值分档（每 0.2 一个词）。
MOOD_MODE = (os.getenv("MOOD_MODE", "delta") or "delta").strip().lower()
# 心情作用域：session = 每个群/私聊各一个（默认）；global = 全机器人共用一个
MOOD_SCOPE = (os.getenv("MOOD_SCOPE", "session") or "session").strip().lower()
MOOD_MAX_DELTA = tuning.f("MOOD_MAX_DELTA")

# ============ P2：v 步进 + 锚点（DESIGN §3.1，2026-11-25 接线） ============
#   v2 = 判读只报 impact 档位，代码做「伪观测 z → K → 步进」；
#   delta = 旧口径（累加 Δ），只作回滚（MOOD_UPDATE=delta）。
MOOD_UPDATE = (os.getenv("MOOD_UPDATE", "v2") or "v2").strip().lower()
_HL_MIN = {
    "group": tuning.f("MOOD_HALF_LIFE_GROUP_MIN"),
    "private": tuning.f("MOOD_HALF_LIFE_PRIVATE_MIN"),
    "global": tuning.f("MOOD_HALF_LIFE_GLOBAL_MIN"),
}
STEP_1 = tuning.f("MOOD_STEP_1")
STEP_2 = tuning.f("MOOD_STEP_2")
STEP_3 = tuning.f("MOOD_STEP_3")
IMPACT_MAX = tuning.i("MOOD_IMPACT_MAX")
# 2026-11-27（外部审计）：prompt 写了「±3 一天最多一次」，代码没有计数。
# 按**会话**按自然日计数；超了降到 ±2（保留「这是件大事」，只是不再顶格）。
IMPACT3_DAILY_MAX = tuning.i("MOOD_IMPACT3_DAILY_MAX")
IMPACT3_OVER_TO = tuning.i("MOOD_IMPACT3_OVER_TO")
SENS_MIN = tuning.f("MOOD_SENS_MIN")
SENS_MAX = tuning.f("MOOD_SENS_MAX")
SATURATION = tuning.f("MOOD_SATURATION")
HARD_CAP = tuning.f("MOOD_HARD_CAP")
P0 = tuning.f("MOOD_P0")
Q_BASE = tuning.f("MOOD_Q_RATE_BASE")
Q_SLOPE = tuning.f("MOOD_Q_RATE_SLOPE")
R_BASE = tuning.f("MOOD_R_BASE")
R_SLOPE = tuning.f("MOOD_R_SLOPE")
ANCHOR_EVERY = tuning.i("MOOD_ANCHOR_EVERY")
ANCHOR_IDLE_MIN = tuning.f("MOOD_ANCHOR_IDLE_MIN")
# 2026-11-26 用户拍板：anchor_level 必填，且**每窗按权重混入**（占大头）。
#   ANCHOR_WEIGHT 是每窗的混入权重；ANCHOR_GAIN 只给独立锚点（静默定时器/手动）。
ANCHOR_WEIGHT = tuning.f("MOOD_ANCHOR_WEIGHT")
ANCHOR_GAIN = tuning.f("MOOD_ANCHOR_GAIN")
ANCHOR_DEADBAND = tuning.f("MOOD_ANCHOR_DEADBAND")
ANCHOR_MAX_MOVE = tuning.f("MOOD_ANCHOR_MAX_MOVE")
ANCHOR_P_ADD = tuning.f("MOOD_ANCHOR_P_ADD")
ANCHOR_LEVEL1 = tuning.f("MOOD_ANCHOR_LEVEL1")
ANCHOR_LEVEL2 = tuning.f("MOOD_ANCHOR_LEVEL2")
ANCHOR_COUNT_SKIPPED = tuning.i("MOOD_ANCHOR_COUNT_SKIPPED")
FAST_CONF = tuning.f("MOOD_FAST_CONF")
FAST_FLOOR = tuning.f("MOOD_FAST_FLOOR")
FAST_FLOOR_LIGHT = tuning.f("MOOD_FAST_FLOOR_LIGHT")
FAST_P_FACTOR = tuning.f("MOOD_FAST_P_FACTOR")
RELEVANCE_GATE = tuning.f("MOOD_RELEVANCE_GATE")

# -1.00 ~ 1.00，每 0.2 一档 → 10 个名词（由低到高）
MOOD_LABELS = [
    (-0.8, "被击穿"),
    (-0.6, "很难受"),
    (-0.4, "憋着一口气"),
    (-0.2, "有点扫兴"),
    (0.0, "淡淡的"),
    (0.2, "平平的"),
    (0.4, "有点高兴"),
    (0.6, "心情不错"),
    (0.8, "挺开心"),
    (1.01, "特别好"),
]


def label_for(value: float) -> str:
    """按数值取心情名词（每 0.2 一档）。value 是外口径 -1~1。"""
    try:
        v = float(value)
    except Exception:
        return MOOD_LABELS[5][1]
    for bound, word in MOOD_LABELS:
        if v < bound:
            return word
    return MOOD_LABELS[-1][1]


def _ad_word(a: float) -> str:
    """把 a（唤醒）说成人话，只用于状态卡展示。"""
    try:
        v = float(a)
    except Exception:
        return "状态一般"
    if v >= 0.65:
        return "有点上头（心跳快、话多）"
    if v <= 0.2:
        return "很平静（懒得动）"
    return "不激动也不困"


def _scope_key(session_key: str = "") -> str:
    """MOOD_SCOPE=global 时所有会话共用全局那一个变量。"""
    if MOOD_SCOPE == "global":
        return ""
    return str(session_key or "")


MOOD_IDLE_AFTER = tuning.f("MOOD_IDLE_AFTER")
MOOD_IDLE_TICK = tuning.f("MOOD_IDLE_TICK")
MOOD_IDLE_STEP = tuning.f("MOOD_IDLE_STEP")

# ============ 峰值事件（2026-11-20 定稿） ============
# 用户：「心情名词这里需要增加一个算法：显示最近一件涨降幅度最大的一件事和心理活动。
#   比如说有件事很伤心 -0.38，后面又发生几件开心的事 +0.18 / +0.22，
#   这两次已经消掉了这个 -0.38，-0.38 就不再是最大值，砍掉去换下一个最大值。
#   2 小时内没抵消就自动过滤掉 2 小时之前的增减 —— 每件事最多挂两个小时。」
# 所以峰值不是「历史最大 |Δ|」，而是「还没被后续反向波动抵消掉的最大 |Δ|」。
MOOD_PEAK_WINDOW_SEC = tuning.f("MOOD_PEAK_WINDOW_SEC")
MOOD_PEAK_MIN_DELTA = tuning.f("MOOD_PEAK_MIN_DELTA")

# ---- a（唤醒）/ d（掌控）：一期只记录（衰减 + 展示），不进 z/Δ ----
AD_BASE_A = tuning.f("MOOD_AD_BASE_AROUSAL")
AD_BASE_D = tuning.f("MOOD_AD_BASE_DOMINANCE")
AD_SMOOTH = tuning.f("AROUSAL_SMOOTH")     # 0.4：单次最大挪 40%（再乘 confidence）
_HALF_A = {
    "group": tuning.f("MOOD_HALF_LIFE_A_GROUP_MIN"),
    "private": tuning.f("MOOD_HALF_LIFE_A_PRIVATE_MIN"),
    "global": tuning.f("MOOD_HALF_LIFE_A_GLOBAL_MIN"),
}
_HALF_D = {
    "group": tuning.f("MOOD_HALF_LIFE_D_GROUP_MIN"),
    "private": tuning.f("MOOD_HALF_LIFE_D_PRIVATE_MIN"),
    "global": tuning.f("MOOD_HALF_LIFE_D_GLOBAL_MIN"),
}


def _scope_of(session_key: str = "") -> str:
    key = str(session_key or "")
    if key.startswith("private_"):
        return "private"
    if key.startswith("group_"):
        return "group"
    return "global"


def _ad_decay(rec: dict, now: float, scope: str = "group"):
    """a/d 按半衰期回落到基线。返回 (rec, changed)。"""
    changed = False
    for axis, base, halves in (("a", AD_BASE_A, _HALF_A),
                               ("d", AD_BASE_D, _HALF_D)):
        try:
            val = float(rec.get(axis, base))
        except Exception:
            val = base
        try:
            at = float(rec.get(axis + "_at") or 0.0)
        except Exception:
            at = 0.0
        if not at:
            if axis not in rec:
                rec[axis] = round(base, 4)
                rec[axis + "_at"] = now
                changed = True
            continue
        half = max(1.0, float(halves.get(scope, halves["group"])))
        mins = max(0.0, (now - at) / 60.0)
        nv = base + (val - base) * math.pow(0.5, mins / half)
        nv = max(0.0, min(1.0, nv))
        if abs(nv - val) > 1e-4:
            changed = True
        rec[axis] = round(nv, 4)
        rec[axis + "_at"] = now
    return rec, changed


@_serialized
def update_ad(session_key: str = "", arousal=None, dominance=None,
              confidence: float = 0.6) -> dict:
    """judge 给的绝对档位 → a/d（置信加权 EMA）。

    ⚠️ 必须乘 confidence：否则低置信判读也会一次挪 40%（AD_SMOOTH），
    比 v 的首条 0.085 快 4 倍。a/d **不进** v 的 z/Δ（emotion §1.5）。
    """
    key = str(session_key or "")
    now = time.time()
    st = _load()
    if key:
        rec = st["sessions"].get(key)
        if rec is None:
            rec = {"value": float(st["global"].get("value") or 0.0),
                   "updated_at": now}
        rec = _decay(rec, now)
    else:
        rec = st["global"]
    scope = _scope_of(key)
    rec, _ = _ad_decay(rec, now, scope)
    try:
        conf = max(0.0, min(1.0, float(confidence)))
    except Exception:
        conf = 0.6
    for axis, target, base in (("a", arousal, AD_BASE_A),
                               ("d", dominance, AD_BASE_D)):
        if target is None:
            continue
        try:
            t = max(0.0, min(1.0, float(target)))
        except Exception:
            continue
        cur = float(rec.get(axis, base))
        rec[axis] = round(cur + conf * AD_SMOOTH * (t - cur), 4)
        rec[axis + "_at"] = now
    if key:
        st["sessions"][key] = rec
    else:
        st["global"] = rec
    _save(st)
    return {"a": float(rec.get("a", AD_BASE_A)),
            "d": float(rec.get("d", AD_BASE_D))}


def ad_of(session_key: str = "") -> dict:
    """读 a/d（顺手衰减）。给状态卡 / /测函数 用。"""
    m = get_mood(session_key)
    return {"a": float(m.get("a", AD_BASE_A)),
            "d": float(m.get("d", AD_BASE_D)),
            "base_a": AD_BASE_A, "base_d": AD_BASE_D}


# ============ P2 状态机：impact → z → K → 步进（DESIGN §3.1） ============
# 为什么要有这一层：
#   旧口径把 LLM 报的 Δ 直接累加，模型说多少就是多少（一句话能翻脸）；
#   新口径 LLM 只报**档位** impact∈{-2..+2}，步长/饱和/置信加权都在代码里，
#   并且每 20 个判读窗口用一次**绝对锚点**纠偏（纯相对会漂）。
def _p_of(rec: dict) -> float:
    try:
        return max(1e-4, min(1.0, float(rec.get("P", P0))))
    except Exception:
        return P0


# P 的下限（工程护栏，不是设计口径）：
# 连续判读会让 P 指数衰减到 0，K→0 之后新事件再也推不动 v（情绪「冻住」）。
# 留 1/4 的观测噪声当底，稳态 K≈0.2，单次 impact=±1 约走 0.02 —— 与 DESIGN
# §3.1 写的「稳态 0.02~0.05」一致；锚点仍会把 P 抬回 0.02 做纠偏。
P_FLOOR = max(1e-4, R_BASE * 0.25)
# 2026-11-26 用户拍板：「impact≥2 时回补一点 P」。
# 为什么：稳态 P 长期贴 P_FLOOR，K≈0.04，±3 档 dv 只有 0.012 ——
# 心情「推不动」，所谓突发事件在数值上不存在。
# 只在 |impact|≥2 时补，且补完不超过 P_EVENT_CAP（否则连刷会把 K 顶到 1）。
P_EVENT_BOOST = tuning.f("MOOD_P_EVENT_BOOST")
P_EVENT_CAP = tuning.f("MOOD_P_EVENT_CAP")


def sensitivity(session_key: str = "") -> float:
    """人设敏感度（乘在观测步长上），默认 1.0，夹在 [SENS_MIN, SENS_MAX]。"""
    try:
        _s = float(get_mood(session_key).get("sens", 1.0))
    except Exception:
        _s = 1.0
    return max(SENS_MIN, min(SENS_MAX, _s))


@_serialized
def set_sensitivity(value: float, session_key: str = "") -> float:
    _s = max(SENS_MIN, min(SENS_MAX, float(value)))
    now = time.time()
    st = _load()
    key = _scope_key(session_key)
    rec = st["sessions"].get(key) or _new_rec(now, _scope_of(key))
    rec = _decay(rec, now)
    rec["sens"] = _s
    st["sessions"][key] = rec
    _save(st)
    return _s


def _step_of(impact: int) -> float:
    """档位 → 观测步长。2026-11-26（v2.5）：加 ±3「猛击」档。"""
    a = abs(int(impact))
    if a >= 3:
        return STEP_3
    if a >= 2:
        return STEP_2
    return STEP_1


def _anchor_map(level: int) -> float:
    return {2: ANCHOR_LEVEL2, 1: ANCHOR_LEVEL1, 0: 0.0,
            -1: -ANCHOR_LEVEL1, -2: -ANCHOR_LEVEL2}.get(int(level), 0.0)


def event_p_boost(p: float, imp: int) -> float:
    """|impact|≥2 时回补 P，**只补不降**（2026-11-27 修）。

    旧写法 `p = min(max(P_FLOOR, P_EVENT_CAP), p + BOOST)` 在 P0=0.25 >
    P_EVENT_CAP=0.20 时，第一条 ±2/±3 会把 P **调低**到 0.20 —— 更大的事件
    反而更确定。正确语义：p 已经高于 CAP 就保持原值。
    """
    try:
        p = max(P_FLOOR, float(p))
    except Exception:
        p = P_FLOOR
    if abs(int(imp or 0)) >= 2:
        return max(p, min(P_EVENT_CAP, p + P_EVENT_BOOST))
    return p


def _anchor_diff(rec: dict, level: int, baseline: float = 0.0) -> float:
    """当前 v 到「绝对档位目标」的差值（外口径）。诊断/死区判定共用。"""
    v = float(rec.get("value") or 0.0) / 100.0
    target = max(-1.0, min(1.0, float(baseline) + _anchor_map(level)))
    return target - v


@_serialized
def cap_daily_impact(session_key: str, impact: int) -> int:
    """±3 档「一天最多 N 次」的代码护栏（2026-11-27，外部审计缺口）。

    为什么放在 mood 而不是 prompt：prompt 只是纪律，活跃群一天能刷好几次 ±3。
    口径：
      · **按会话**（心情本来就是会话级）按**自然日**计数；
      · 超限后把 ±3 降成 `IMPACT3_OVER_TO`（默认 2）——保留「这是件大事」，
        只是不再顶格；**不丢弃**，避免模型报了大事件却完全不生效；
      · `MOOD_IMPACT3_DAILY_MAX=0` = 不限（回滚）。
    返回实际生效的档位（-3..+3）。
    """
    try:
        imp = max(-IMPACT_MAX, min(IMPACT_MAX, int(impact)))
    except Exception:
        return 0
    if abs(imp) < 3 or IMPACT3_DAILY_MAX <= 0:
        return imp
    now = time.time()
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    key = _scope_key(session_key)
    st = _load()
    rec = st["sessions"].get(key) if key else st["global"]
    rec = _decay(rec, now) if rec else _new_rec(now, _scope_of(key))
    if str(rec.get("impact3_day") or "") != day:
        rec["impact3_day"] = day
        rec["impact3_n"] = 0
    n = int(rec.get("impact3_n") or 0)
    if n >= IMPACT3_DAILY_MAX:
        eff = (1 if imp > 0 else -1) * max(0, min(IMPACT_MAX, IMPACT3_OVER_TO))
    else:
        rec["impact3_n"] = n + 1
        eff = imp
    if key:
        st["sessions"][key] = rec
    else:
        st["global"] = rec
    _save(st)
    return eff


def _anchor_rec(rec: dict, level: int, confidence: float,
                baseline: float = 0.0, *, gain: float = None,
                p_add: float = None, count: int = 1) -> float:
    """在 rec 上做一次绝对锚点，返回实际位移（外口径）。

    `gain`  —— 这次向目标靠多少（默认 `ANCHOR_GAIN`；每窗混入时传 `ANCHOR_WEIGHT`）；
    `p_add` —— P 的回补量（每窗混入时不回补，只有周期/静默锚点才补，
               否则 P 永远很高、K 永远大，滤波器等于没有）；
    `count` —— 1 = 这次算一次锚点（写 `anchor_n`）；0 = 只混入不计次。
    """
    v = float(rec.get("value") or 0.0) / 100.0
    diff = _anchor_diff(rec, level, baseline)
    if abs(diff) < 1e-12:
        return 0.0
    _g = ANCHOR_GAIN if gain is None else max(0.0, float(gain))
    # 2026-11-27（外部审计）：旧写法「|diff| < 0.15 直接 return 0，>0.15 一次拉 45%」
    # 把 EMA 变成了 bang-bang（稳态锯齿）。现在改成**软死区**：
    #   |diff| < DEADBAND 时按 smoothstep 衰减权重（0 → 1），边界处与全速连续；
    #   既保留「小抖动不动」的初衷，又不会在 0.149→0.151 之间突然跳 6 个点。
    # 位移 <0.01 的窗口本来就不写 mood_history，所以不会把流水刷满。
    if ANCHOR_DEADBAND > 1e-9:
        _r = min(1.0, abs(diff) / ANCHOR_DEADBAND)
        _g *= _r * _r * (3.0 - 2.0 * _r)
    move = diff * (_g * max(0.0, min(1.0, float(confidence))))
    move = max(-ANCHOR_MAX_MOVE, min(ANCHOR_MAX_MOVE, move))
    rec["value"] = _clamp((v + move) * 100.0)
    if p_add:
        rec["P"] = min(1.0, _p_of(rec) + float(p_add))
    rec["anchor_at"] = time.time()
    if count:
        rec["anchor_n"] = int(rec.get("anchor_n") or 0) + 1
    return round(move, 4)


@_serialized
def observe(session_key: str, impact: int, confidence: float = 0.6, *,
            sensitivity: float = None, reactivity: float = 1.0,
            anchor_level: int = None, word: str = "", why: str = "",
            src: str = "", about_qq: str = "", mind: str = "") -> dict:
    """**唯一的心情更新链路**（P2）：impact 档位 → 伪观测 z → K → 步进。

    返回 {v, dv, z, K, P, impact, anchored}；v 是外口径（-1~1）。
    `anchor_level` 非 None 且到第 ANCHOR_EVERY 个窗口 / 静默够久时做一次锚点。
    """
    try:
        imp = max(-IMPACT_MAX, min(IMPACT_MAX, int(impact)))
    except Exception:
        imp = 0
    try:
        conf = max(0.0, min(1.0, float(confidence)))
    except Exception:
        conf = 0.6
    now = time.time()
    st = _load()
    # 人设基线（emotion §3.1/§2.1）：衰减回落点和锚点目标都叠在它上面。
    # 没有这个字段时恒为 0 —— 冷漠/热情人设的长期回落点会完全一样。
    _base_v = 0.0
    try:
        from . import persona_traits as _pt
        _base_v = float(_pt.traits_for_session(str(session_key or "")).get("baseline_v") or 0.0)
    except Exception:
        _base_v = 0.0
    _base_v = max(-0.3, min(0.3, _base_v))
    st["global"] = _decay(st["global"], now, _base_v)
    key = _scope_key(session_key)
    _sens = max(SENS_MIN, min(SENS_MAX, float(sensitivity if sensitivity is not None else 1.0)))

    def _one(rec: dict, scope: str, scale: float = 1.0, base: float = 0.0):
        # ⚠️ dt_h 必须在 _decay **之前**算：_decay 会把 updated_at 写成 now，
        #    之后再算永远是 0 → Q 恒 0、P 永不随时间回升（2026-11-25 审计 P0-1）。
        try:
            _prev_ts = float((rec or {}).get("updated_at") or now)
        except Exception:
            _prev_ts = now
        dt_h = max(0.0, (now - _prev_ts) / 3600.0)
        rec = _decay(rec, now, base) if rec else _new_rec(now, scope)
        v = float(rec.get("value") or 0.0) / 100.0
        p = max(P_FLOOR, min(1.0, _p_of(rec)
                             + (Q_BASE + Q_SLOPE * max(0.0, float(reactivity))) * dt_h))
        # 突发事件回补 P（2026-11-26 用户拍板）。
        # 只补到 P_EVENT_CAP：连刷 ±3 时 P 不会一路顶到 1，K 最多 ≈0.63，
        # ±3 的 dv ≈0.176，仍远低于 HARD_CAP(0.35)。
        p = event_p_boost(p, imp)
        r = R_BASE + R_SLOPE * (1.0 - conf) ** 2
        k = (p / (p + r)) if (p + r) > 0 else 0.0
        d = 0.0 if imp == 0 else (1.0 if imp > 0 else -1.0)
        z = max(-1.0, min(1.0, v + d * _step_of(imp) * _sens))
        dv = k * (z - v)
        dv *= (1.0 - SATURATION * abs(v))
        dv = max(-HARD_CAP, min(HARD_CAP, dv)) * scale
        rec["value"] = _clamp((v + dv) * 100.0)
        rec["updated_at"] = now
        rec["P"] = max(P_FLOOR, min(1.0, (1.0 - k) * p))
        rec["judge_n"] = int(rec.get("judge_n") or 0) + 1
        if abs(dv) > 1e-6:
            rec["label"] = label_for(rec["value"] / 100.0)
            rec["label_at"] = now
        return rec, dv, z, k, p

    anchored = 0.0
    anchored_g = 0.0
    if key:
        scope = _scope_of(key)
        rec, dv, z, k, p = _one(st["sessions"].get(key), scope, base=_base_v)
        n = int(rec.get("judge_n") or 0)
        # 2026-11-26（外部审计）：原来这里还有 `or idle`，但 `_anchor_rec` 每窗都会写
        # `anchor_at=now`，而 `_one` 每窗都写 `updated_at=now` —— 只要模型每窗都给
        # anchor_level，「静默 30min」在这里**永远不成立**，是死分支。
        # 静默回补由 `anchor_tick`（定时器）负责，observe 内只按窗口数回补。
        _cadence = (n % max(1, ANCHOR_EVERY) == 0)
        if anchor_level is not None:
            # 软死区里（|diff|<DEADBAND）的窗口算不算「一次锚点」由
            # MOOD_ANCHOR_COUNT_SKIPPED 决定；0 = 只数真挪动过的。
            _dead = abs(_anchor_diff(rec, int(anchor_level), _base_v)) < ANCHOR_DEADBAND
            anchored = _anchor_rec(
                rec, int(anchor_level), conf, _base_v,
                gain=ANCHOR_WEIGHT,
                p_add=(ANCHOR_P_ADD if _cadence else 0.0),
                count=(1 if _cadence else 0))
            if _dead and _cadence and not ANCHOR_COUNT_SKIPPED:
                rec["judge_n"] = max(0, n - 1)
        if abs(_base_v) > 1e-9:
            rec["base_v"] = round(_base_v, 4)
        st["sessions"][key] = rec
        g, dv_g, _, _, _ = _one(st["global"], "global", GLOBAL_SHARE, base=_base_v)
        # 2026-11-25：全局也要锚（emotion §4.1「锚点同样作用于全局」）。
        # 以前只有会话锚，全局漂了只能靠 6h 衰减，quiet/proactive 会跟着偏。
        if anchor_level is not None:
            # ⚠️ 2026-11-27（外部审计，真问题）：全局**事件**乘了 GLOBAL_SHARE(0.35)，
            # 但全局**锚点**以前是满权重 0.45×conf → 全局在锚点上比会话还快，
            # 最吵的群会主导全局。这里统一乘 GLOBAL_SHARE。
            anchored_g = _anchor_rec(
                g, int(anchor_level), conf, _base_v,
                gain=ANCHOR_WEIGHT * GLOBAL_SHARE,
                p_add=((ANCHOR_P_ADD * GLOBAL_SHARE) if _cadence else 0.0),
                count=(1 if _cadence else 0))
        if abs(_base_v) > 1e-9:
            g["base_v"] = round(_base_v, 4)
        st["global"] = g
        value_now = float(rec["value"])
    else:
        g, dv, z, k, p = _one(st["global"], "global", base=_base_v)
        if anchor_level is not None:
            # MOOD_SCOPE=global：没有会话计数，按全局窗口数走同样的「每窗混入 + 周期回补 P」。
            _gn = int(g.get("judge_n") or 0)
            _gcad = (_gn % max(1, ANCHOR_EVERY) == 0)
            anchored_g = _anchor_rec(
                g, int(anchor_level), conf, _base_v,
                gain=ANCHOR_WEIGHT * GLOBAL_SHARE,
                p_add=((ANCHOR_P_ADD * GLOBAL_SHARE) if _gcad else 0.0),
                count=(1 if _gcad else 0))
        if abs(_base_v) > 1e-9:
            g["base_v"] = round(_base_v, 4)
        st["global"] = g
        value_now = float(g["value"])
    _save(st)
    try:
        if abs(dv) > 1e-6:
            log_shift(dv * 100.0, emotion=(word or "judge"),
                      label=(why or ""), reason="observe",
                      session_key=session_key, about_qq=about_qq,
                      src=src, mind=mind, impact=imp)
        # 2026-11-26：锚点现在**每窗都可能挪**（ADR-042）。只有挪得够多才记流水，
        # 否则 mood_history 会被 0.002 级别的纠偏刷满，`recent_shifts` 的 600 行缓存
        # 覆盖不到 30 分钟，安静建议反而看不清「真事件」。
        if abs(anchored) >= 0.01:
            log_shift(anchored * 100.0, emotion="anchor",
                      label="绝对锚点纠偏", reason="anchor",
                      session_key=session_key, about_qq=about_qq)
        if abs(anchored_g) >= 0.01:
            log_shift(anchored_g * 100.0, emotion="anchor",
                      label="全局锚点纠偏", reason="anchor",
                      session_key="", about_qq="")
    except Exception:
        pass
    # P 返回**后验**（更新后的不确定度）：日志/诊断要能看出「越判越确定」，
    # 返回先验 p 会让线上每条都像 P0=0.25（2026-11-25 真机日志踩到）。
    _post = _p_of(st["sessions"][key]) if key else _p_of(st["global"])
    # `dv` 只含**事件步进**（不含 anchored）——好感/记忆/峰值事件只吃它：
    #   锚点是「对她此刻水平的重新估计」，不是「这个人造成了这件事」，
    #   把它算进好感就变成「她今天心情好，顺手记到旁边人头上」（违反唯一原则）。
    # 想知道「这一窗 v 到底动了多少」看 `dv_total`（诊断用，不进任何模型）。
    return {"v": round(value_now / 100.0, 4), "dv": round(dv, 4),
            "dv_total": round(dv + anchored, 4),
            "z": round(z, 4), "K": round(k, 4), "P": round(_post, 4),
            "P_prior": round(p, 4), "impact": imp, "anchored": anchored}


@_serialized
def note_window(session_key: str = "") -> int:
    """只记「这一窗判过了」，不改 v。

    ADR-037：被 relevance gate 跳过的窗口也要计入锚点计数，
    否则噪声群 judge_n 永远是 0、锚点永不触发（状态冻结）。
    返回累计窗口数。
    """
    now = time.time()
    st = _load()
    key = _scope_key(session_key)
    if key:
        rec = st["sessions"].get(key) or _new_rec(now, _scope_of(key))
        rec = _decay(rec, now)
        rec["judge_n"] = int(rec.get("judge_n") or 0) + 1
        st["sessions"][key] = rec
    else:
        g = _decay(st["global"], now)
        g["judge_n"] = int(g.get("judge_n") or 0) + 1
        st["global"] = g
    _save(st)
    rec2 = st["sessions"].get(key) if key else st["global"]
    return int((rec2 or {}).get("judge_n") or 0)


@_serialized
def anchor(session_key: str, level: int, confidence: float = 0.6,
           baseline: float = 0.0) -> float:
    """手动/独立触发一次绝对锚点，返回位移（外口径）。"""
    now = time.time()
    st = _load()
    key = _scope_key(session_key)
    rec = st["sessions"].get(key) or _new_rec(now, _scope_of(key))
    rec = _decay(rec, now)
    move = _anchor_rec(rec, int(level), confidence, baseline)
    st["sessions"][key] = rec
    _save(st)
    if abs(move) > 1e-9:
        log_shift(move * 100.0, emotion="anchor", label="绝对锚点纠偏",
                  reason="anchor", session_key=session_key)
    return move


@_serialized
def fast_guard(session_key: str = "", light: bool = False,
               why: str = "") -> float:
    """快速通道护栏：@她/引用她 + 强负面词时，只做**负向钳制**。

    - `v = min(v, floor)`：只降不升（强负面 -0.45 / 轻负面 -0.25）；
    - `P = min(1, P×FAST_P_FACTOR)`：**提高**不确定度，让下一轮判读能把她拉回来。
    规则不判决正向（红线 3），也不写好感。
    """
    now = time.time()
    st = _load()
    st["global"] = _decay(st["global"], now)
    key = _scope_key(session_key)
    floor = FAST_FLOOR_LIGHT if light else FAST_FLOOR
    if key:
        rec = st["sessions"].get(key) or _new_rec(now, _scope_of(key))
        rec = _decay(rec, now)
    else:
        rec = st["global"]
    v = float(rec.get("value") or 0.0) / 100.0
    moved = 0.0
    if v > floor:
        moved = floor - v
        rec["value"] = _clamp(floor * 100.0)
        rec["label"] = label_for(floor)
        rec["label_at"] = now
    rec["P"] = min(1.0, _p_of(rec) * FAST_P_FACTOR)
    rec["updated_at"] = now
    # 快速通道的「置信度」按设计固定为 MOOD_FAST_CONF（0.4）：它不是一次完整判读，
    # 只在下一轮判读前占个位，让状态卡/诊断看得出「这条是护栏写的」。
    rec["judge"] = {**(rec.get("judge") or {}), "fast_conf": FAST_CONF,
                    "fast_at": now}
    if key:
        st["sessions"][key] = rec
    else:
        st["global"] = rec
    _save(st)
    if moved:
        log_shift(moved * 100.0, emotion="guard",
                  label=(why or "快速通道负向钳制"), reason="fast-guard",
                  session_key=session_key)
    return round(float(rec["value"]) / 100.0, 4)


def state_of(session_key: str = "") -> dict:
    """给状态卡 / 诊断用：v、a、d、P、判读次数、作用域。"""
    m = get_mood(session_key)
    return {"v": round(float(m.get("value") or 0.0) / 100.0, 4),
            "a": round(float(m.get("a", AD_BASE_A)), 3),
            "d": round(float(m.get("d", AD_BASE_D)), 3),
            "P": round(_p_of(m), 4),
            "judge_n": int(m.get("judge_n") or 0),
            "scope": str(m.get("_scope") or "group")}


def _idle_decay(rec: dict, now: float):
    """返回 (rec, changed)。线性向 0 走，不跨 0；归零时清文案。"""
    last = float(rec.get("last_active_at") or rec.get("updated_at") or now)
    if now - last <= MOOD_IDLE_AFTER:
        return rec, False
    base = float(rec.get("last_decay_at") or (last + MOOD_IDLE_AFTER))
    steps = int((now - base) // max(1.0, MOOD_IDLE_TICK))
    if steps <= 0:
        return rec, False
    v = float(rec.get("value") or 0.0)
    for _ in range(min(steps, 1000)):
        if v == 0:
            break
        v -= (1.0 if v > 0 else -1.0) * min(abs(v), MOOD_IDLE_STEP)
    rec["value"] = round(v, 2)
    rec["last_decay_at"] = base + steps * MOOD_IDLE_TICK
    if abs(rec["value"]) < 3:
        rec["value"] = 0.0
        rec["label"] = ""
        rec["label_at"] = 0.0
        rec["judge"] = {}
    return rec, True


@_serialized
def touch(session_key: str = "") -> None:
    """有真人消息 → **先结算沉默期间的自减**，再从这一刻重新计时。

    用户 2026-11-20：「5 分钟没有任何人理，就 ±0.01 往 0 靠；再 5 分钟再 0.01。」
    以前这里直接重置 last_active_at，而 get_mood 要等 touch 之后才跑 →
    沉默期间该走的那几步永远被抹掉（等于空闲衰减只在主动私聊轮询时才偶尔生效）。
    所以先把 last_active_at 到现在的步数结掉，再刷新计时。
    """
    key = str(session_key or "")
    if not key:
        return
    now = time.time()
    st = _load()
    rec = st["sessions"].get(key)
    if rec is None:
        rec = {"value": float(st["global"].get("value") or 0.0),
               "updated_at": now}
    else:
        rec = _decay(rec, now)
        rec, _ = _idle_decay(rec, now)
    rec["last_active_at"] = now
    # ⚠️ 必须清掉上一次的空闲计时基准：否则刚聊完 5 分钟，
    #    _idle_decay 会拿「几小时前的 last_decay_at」算 steps → 一步跳到 0。
    rec.pop("last_decay_at", None)
    st["sessions"][key] = rec
    _save(st)
# 判读结果的有效期（秒）：超过就当没有，别拿半小时前的判读影响现在
JUDGE_FRESH = tuning.f("MOOD_JUDGE_FRESH_SEC")
# 最多记多少个会话的心情（防止字典无限长）
MAX_SESSIONS = tuning.i("MOOD_MAX_SESSIONS")

# ⚠️ 2026-11-20 起这张表**不再是主通道**：数值只由第三方判读的 delta 驱动
#    （MOOD_FROM_JUDGE=true）。只有在回滚（MOOD_FROM_JUDGE=false）时才用这张表。
EMOTION_DELTA = {
    "happy": 10.0,
    "excited": 15.0,
    "love": 18.0,
    "touched": 12.0,
    "sad": -10.0,
    "lonely": -6.0,
    "angry": -16.0,
    "annoyed": -8.0,
    "neutral": 0.0,
}

# 👇 兜底通道：DeepSeek 没打 MEMO 时才用关键词猜（不再是主判据）
POSITIVE_WORDS = ["可爱", "厉害", "真棒", "喜欢", "谢谢", "哈哈", "乖", "好棒", "爱你", "好厉害"]
NEGATIVE_WORDS = ["笨", "蠢", "讨厌", "滚", "废物", "闭嘴", "傻", "烦", "垃圾", "去死"]


def _new_rec(now: float = 0, scope: str = "global") -> dict:
    _t = now or time.time()
    # anchor_at 必须初始化：留 0 会让「静默 30min」在第一次判读时恒为真 →
    # 每条新会话的第一个观测都被额外锚一次（2026-11-25 真机日志发现）。
    return {"value": 0.0, "updated_at": _t, "_scope": scope, "anchor_at": _t}


def _clamp(v: float) -> float:
    return max(-100.0, min(100.0, float(v)))


def _decay(rec: dict, now: float, baseline: float = None) -> dict:
    """把一条记录按半衰期衰减到 now，返回新记录（不改原对象）。

    2026-11-25：衰减目标是**人设基线 baseline_v**，不是 0
    （emotion §3.1「衰减回到 base_v」；冷漠人设和热情人设长期回落点不同）。

    ⚠️ 必须把 label 一起带过去：以前只回 value/updated_at，
       一衰减就把她自己写的心情原话丢了（2026-11-19）。
    """
    try:
        value = float(rec.get("value") or 0.0)
        updated = float(rec.get("updated_at") or now)
    except Exception:
        return _new_rec(now)
    hours = max(0.0, (now - updated) / 3600.0)
    # 半衰期按作用域取（DESIGN §3.1：群 30min / 私聊 2h / 全局 6h）。
    # 回滚（MOOD_UPDATE=delta）时仍用旧的单一半衰期 12h。
    _scope = str(rec.get("_scope") or "group")
    if MOOD_UPDATE == "v2":
        _hl_h = max(0.01, float(_HL_MIN.get(_scope, _HL_MIN["group"])) / 60.0)
    else:
        _hl_h = HALF_LIFE_HOURS
    # baseline=None 时用记录里存的人设基线（写入口会写 base_v）；
    # 这样 get_mood 这类读路径不用每次都去算 persona_traits。
    if baseline is None:
        try:
            baseline = float(rec.get("base_v") or 0.0)
        except Exception:
            baseline = 0.0
    _base = max(-1.0, min(1.0, float(baseline or 0.0))) * 100.0
    out = {"value": _clamp(_base + (value - _base) * math.pow(0.5, hours / _hl_h)),
           "updated_at": now, "_scope": _scope}
    if abs(_base) > 1e-9:
        out["base_v"] = round(_base / 100.0, 4)
    if rec.get("label"):
        out["label"] = str(rec.get("label") or "")
        try:
            out["label_at"] = float(rec.get("label_at") or 0)
        except Exception:
            out["label_at"] = 0.0
    # ⚠️ 空闲计时字段必须带过去：以前 _decay 只回 value/updated_at，
    #    把 last_active_at 丢了 → 后面 _idle_decay 拿 updated_at(=now) 判断，
    #    「5 分钟没人就 -0.01」永远不生效（2026-11-20 修）。
    for _k in ("last_active_at", "last_decay_at"):
        if rec.get(_k):
            try:
                out[_k] = float(rec.get(_k))
            except Exception:
                pass
    # a/d（唤醒/掌控）：它们有自己的半衰期，_decay 只负责原样带过去
    for _k in ("a", "d", "a_at", "d_at"):
        if rec.get(_k) is not None:
            out[_k] = rec.get(_k)
    # 判读结果（mood_judge 写的影子字段）也不能在衰减时丢掉
    if isinstance(rec.get("judge"), dict) and rec["judge"]:
        out["judge"] = dict(rec["judge"])
    # P2 状态机字段：不确定度 P、判读次数、上次锚点、人设敏感度
    for _k in ("P", "judge_n", "anchor_at", "anchor_n", "sens",
               # ±3 日计数（2026-11-27）：衰减时不能丢，否则每天第一次 ±3 判不出来
               "impact3_day", "impact3_n"):
        if rec.get(_k) is not None:
            out[_k] = rec.get(_k)
    return out


def _load() -> dict:
    raw = {}
    if MOOD_FILE.exists():
        try:
            raw = json.loads(MOOD_FILE.read_text(encoding="utf-8")) or {}
        except Exception as e:
            # 2026-11-26（2026-11-26 审计修复）：以前这里是裸 `except: pass`，
            # mood.json 一旦损坏就静默返回空状态 —— 全员心情归零，
            # 而且没有任何告警。现在必须喊出来，并把坏文件备份走。
            try:
                _bak = MOOD_FILE.with_suffix(
                    ".corrupt-%d.json" % int(time.time()))
                MOOD_FILE.replace(_bak)
                logger.error(f"[MOOD] mood.json 解析失败（{type(e).__name__}: {e}）"
                             f"，已备份到 {_bak.name} 并按空状态继续")
            except Exception:
                logger.error(f"[MOOD] mood.json 解析失败（{type(e).__name__}: {e}）"
                             f"，备份失败，按空状态继续")
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    # 旧格式迁移：{"value":..., "updated_at":...}
    if "value" in raw and "global" not in raw:
        return {"global": _decay(raw, time.time()), "sessions": {}}
    g = raw.get("global")
    if not isinstance(g, dict):
        g = _new_rec()
    g.setdefault("_scope", "global")
    s = raw.get("sessions")
    if not isinstance(s, dict):
        s = {}
    # 旧记录没有 _scope：按 key 回填，避免升级后群聊误用 12h 半衰期
    for _k, _rec in list(s.items()):
        if isinstance(_rec, dict):
            _rec.setdefault("_scope", _scope_of(_k))
            # 老记录没有 anchor_at：用最后更新时间当起点，避免升级后第一次判读就锚
            try:
                _rec.setdefault("anchor_at", float(_rec.get("updated_at") or time.time()))
            except Exception:
                _rec["anchor_at"] = time.time()
    return {"global": g, "sessions": s}


def _save(st: dict) -> None:
    """唯一落盘入口：进程内由 `_serialized` 串行，跨进程用 flock 保护写。"""
    lock = None
    try:
        # 控制会话数量：留最近活跃的 MAX_SESSIONS 个
        sessions = st.get("sessions") or {}
        if len(sessions) > MAX_SESSIONS:
            keep = sorted(sessions.items(),
                          key=lambda kv: float((kv[1] or {}).get("updated_at") or 0),
                          reverse=True)[:MAX_SESSIONS]
            st["sessions"] = dict(keep)
        # 跨进程：两个脚本同时写时，tmp→replace 这一步必须互斥
        if fcntl is not None:
            try:
                lock = open(MOOD_FILE.with_suffix(".lock"), "a+")
                fcntl.flock(lock, fcntl.LOCK_EX)
            except Exception:
                lock = None
        tmp = MOOD_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, MOOD_FILE)
    except Exception:
        pass
    finally:
        if lock is not None:
            try:
                fcntl.flock(lock, fcntl.LOCK_UN)
                lock.close()
            except Exception:
                pass


def log_shift(delta: float, *, emotion: str = "", reason: str = "",
              about_qq: str = "", session_key: str = "", label: str = "",
              src: str = "", mind: str = "", impact=None):
    """把一次心情波动追加到流水文件（复盘 / 峰值事件 / 安静建议都读它）。

    emotion 存**归一化**的固定词（机器读），她自己写的原话另存 label。
    `mind` 是判读模型推断的「她当时的心里话」——峰值事件要连这件事的
    心理活动一起给她看（用户 2026-11-20）。
    """
    if not delta:
        return
    try:
        rec = {
            "at": time.time(),
            "delta": round(float(delta), 2),
            "emotion": emotion,
            "label": (label or "")[:60],
            "src": (src or "")[:100],
            "mind": (mind or "")[:100],
            "reason": (reason or "")[:120],
            "about_qq": str(about_qq or ""),
            "session": str(session_key or ""),
            "value": round(float(get_mood(session_key)["value"]), 2),
        }
        # 档位（-2~+2）：复盘时才能区分「步长变大了」还是「这次本来就更重」。
        # 没有它，M10「同段步长递减」在 data 级根本判不了（2026-11-25）。
        if impact is not None:
            try:
                rec["impact"] = int(impact)
            except Exception:
                pass
        with open(MOOD_HISTORY, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        # 刚追加过 → 让走势缓存失效（同一秒内连写也不会读到旧的）
        _SHIFT_CACHE["mtime"] = -1.0
    except Exception:
        pass


# 流水文件会越写越长，而安静建议每轮都要读「最近 30 分钟」。
# 这里按 mtime 缓存最后几百条，避免每轮都把整个文件读一遍。
_SHIFT_CACHE: dict = {"mtime": -1.0, "rows": []}
_SHIFT_CACHE_N = 600


def recent_shifts(since_ts: float = 0, limit: int = 200) -> list[dict]:
    """读最近的波动流水（复盘 / 安静建议用）。"""
    if not MOOD_HISTORY.exists():
        return []
    try:
        _m = MOOD_HISTORY.stat().st_mtime
    except Exception:
        return []
    want = max(int(limit or 200), 200)
    rows = _SHIFT_CACHE.get("rows") or []
    # ⚠️ 只按 mtime 判断是否重读。以前还加了「缓存条数 < want 就重读」，
    #    峰值事件要 400 条，历史不足 400 条时每轮 status_block 都会重读整文件。
    if _m != _SHIFT_CACHE.get("mtime"):
        out = []
        try:
            for line in MOOD_HISTORY.read_text(encoding="utf-8").splitlines()[-_SHIFT_CACHE_N:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                out.append(rec)
        except Exception:
            return []
        _SHIFT_CACHE["mtime"], _SHIFT_CACHE["rows"] = _m, out
        rows = out
    out = []
    for rec in rows[-want:]:
        try:
            if float(rec.get("at") or 0) >= float(since_ts or 0):
                out.append(rec)
        except Exception:
            continue
    return out


def peak_event(session_key: str = "", *,
               window_sec: float = 0) -> dict:
    """找「还没被抵消掉、涨/跌幅度最大」的那件事（含当时的心理活动）。

    规则（用户 2026-11-20 定稿）：
      · 只看最近 window_sec（默认 2 小时）内的波动，**每件事最多挂 2 小时**；
      · 取 |Δ| 最大的那件事；如果它之后出现了**反向**波动，并且累计幅度
        够抵消它（例：-0.38 后面 +0.18、+0.22），这件事就算「过去了」，
        从候选里去掉，换下一个 |Δ| 最大的；
      · 抵消用掉的反向事件也一起从候选里去掉（它们已经两清了）；
      · 2 小时内没被抵消 → 它就是现在的「峰值事件」。

    返回 {} 或 {delta, text, mind, at, about_qq}。
    """
    key = _scope_key(session_key)
    win = float(window_sec or MOOD_PEAK_WINDOW_SEC)
    now = time.time()
    try:
        rows = recent_shifts(now - win, limit=400)
    except Exception:
        return {}
    evs = []
    for r in rows:
        try:
            # MOOD_SCOPE=global 时全机器人共用一个心情 → 不按会话过滤
            if MOOD_SCOPE != "global" and str(r.get("session") or "") != key:
                continue
            d = float(r.get("delta") or 0.0)
            at = float(r.get("at") or 0.0)
        except Exception:
            continue
        if abs(d) < MOOD_PEAK_MIN_DELTA or at <= 0:
            continue
        # 2026-11-25：峰值事件只收「真发生过的事」。
        #   ① 快速通道已删除，它的历史记录（label 常是图片描述）不再上状态卡；
        #   ② 图片/视频描述不是「一件事」，丢掉。
        # 锚点（绝对锚点纠偏）、快速通道、旧关键词路径都不算「一件事」。
        # 注意：不要写成「只认 observe」—— 测试/旧数据里 reason 可能是空或自定义文本。
        if str(r.get("reason") or "") in ("anchor", "fast-guard", "guard", "keyword"):
            continue
        txt = (str(r.get("label") or "").strip()
               or str(r.get("src") or "").strip())
        if txt.startswith(("**", "【图片", "[图片", "（图片")) \
                or "图片识别" in txt[:20] or "视频解析" in txt[:20]:
            continue
        evs.append({"at": at, "delta": d,
                    "text": txt[:80],
                    "mind": str(r.get("mind") or "").strip()[:80],
                    "about_qq": str(r.get("about_qq") or ""),
                    "word": str(r.get("emotion") or "")})
    if not evs:
        return {}
    evs.sort(key=lambda e: e["at"])
    for i, e in enumerate(evs):
        e["_i"] = i

    pool = list(evs)
    while pool:
        peak = max(pool, key=lambda e: abs(e["delta"]))
        need = abs(peak["delta"])
        used = []
        for e in pool:
            if e is peak or e["_i"] <= peak["_i"]:
                continue
            # 只有反方向的波动才能抵消
            if e["delta"] * peak["delta"] >= 0:
                continue
            used.append(e)
            need -= abs(e["delta"])
            if need <= 1e-9:
                break
        if need > 1e-9:
            # 没被抵消 → 就是它
            return {"delta": round(peak["delta"], 3),
                    "text": peak["text"], "mind": peak["mind"],
                    "at": peak["at"], "about_qq": peak["about_qq"],
                    "word": peak["word"]}
        drop = {id(peak)} | {id(e) for e in used}
        pool = [e for e in pool if id(e) not in drop]
    return {}


@_serialized
def get_mood(session_key: str = "") -> dict:
    """
    取心情。session_key 为空 = 全局；否则取该会话（没有记录就继承全局）。
    读取时顺手做时间衰减并落盘。

    返回 {value, label, label_at, updated_at}：
      value —— 数值（驱动活泼度/延迟/表情概率，别拿它当「心情」讲给用户听）
      label —— **她自己写的心情原话**（没有就是空串，退回按数值描述）
    """
    now = time.time()
    st = _load()
    # 2026-11-20 审计 G7：get_mood 是读接口，以前每次调用都 _save()，
    # 而它每轮被 status_block / mood_factors / quiet_advisor 调 3~5 次
    # → mood.json 反复全量读写。现在只有「衰减真的改了值 / 新建会话 /
    # 文案过期」才落盘。
    _dirty = False
    _g0 = float(st["global"].get("value") or 0.0)
    g = _decay(st["global"], now)
    g, _ad_changed = _ad_decay(g, now, "global")
    st["global"] = g
    if _ad_changed or abs(float(g.get("value") or 0.0) - _g0) > 1e-9:
        _dirty = True

    key = str(session_key or "")
    if not key:
        rec = g
    else:
        rec = st["sessions"].get(key)
        if rec is None:
            # 第一次进这个会话 → 继承当前全局状态，别一上来就归零
            rec = {"value": g["value"], "updated_at": now,
                   "label": g.get("label") or "", "label_at": g.get("label_at") or 0,
                   "a": g.get("a", AD_BASE_A), "a_at": now,
                   "d": g.get("d", AD_BASE_D), "d_at": now,
                   "_scope": _scope_of(key), "anchor_at": now}
            _dirty = True
        else:
            _v0 = float(rec.get("value") or 0.0)
            rec = _decay(rec, now)
            # 空闲衰减：没人找她时慢慢归 0（和 12h 半衰叠加，短时间尺度以 idle 为主）
            rec, _idle_changed = _idle_decay(rec, now)
            rec, _ad_changed = _ad_decay(rec, now, _scope_of(key))
            if _idle_changed or _ad_changed:
                _dirty = True
            if abs(float(rec.get("value") or 0.0) - _v0) > 1e-9:
                _dirty = True
        st["sessions"][key] = rec

    label = str(rec.get("label") or "").strip()
    try:
        label_at = float(rec.get("label_at") or 0)
    except Exception:
        label_at = 0.0
    if label and (now - label_at) > MOOD_LABEL_TTL_H * 3600:
        label = ""            # 太久以前写的心情，别再当「现在」
        _dirty = True
    # 2026-11-26：分档词和当前数值跨过 0.2 的档位边界时，把它更新掉。
    # 否则状态卡会出现「v=+0.174 却写着『有点高兴』（那是 ≥0.2 的词）」。
    # 她自己写的原话（不在 MOOD_LABELS 里）不动。
    if label and any(label == _w for _, _w in MOOD_LABELS):
        _cur_w = label_for(float(rec.get("value") or 0.0) / 100.0)
        if _cur_w != label:
            label = _cur_w
            rec["label"] = _cur_w
            rec["label_at"] = now
            _dirty = True
    if _dirty:
        _save(st)
    return {"value": rec["value"], "label": label, "label_at": label_at,
            "judge": dict(rec.get("judge") or {}), "updated_at": now,
            "a": float(rec.get("a", AD_BASE_A)),
            "d": float(rec.get("d", AD_BASE_D)),
            # P2 状态机字段：读接口也要带出来，否则 state_of/诊断永远看到 P0
            "P": _p_of(rec),
            "judge_n": int(rec.get("judge_n") or 0),
            "_scope": str(rec.get("_scope") or _scope_of(key)),
            "sens": float(rec.get("sens") or 1.0)}


@_serialized
def set_label(label: str, session_key: str = "") -> str:
    """
    记下**她自己写的心情原话**（不是固定枚举）。

    用户 2026-11-19：「心情不能是我们指定的几种，不能在我们限制的几种心情范围内。
    她要自己有写心情的权利。」—— 所以这里存的是自由文本，最多 MOOD_LABEL_MAX 字。
    传空串 = 清掉（她想「不描述」也行）。
    """
    txt = re.sub(r"\s+", " ", str(label or "")).strip()[:MOOD_LABEL_MAX]
    now = time.time()
    st = _load()
    st["global"] = _decay(st["global"], now)
    key = str(session_key or "")
    if key:
        rec = st["sessions"].get(key)
        if rec is None:
            rec = {"value": st["global"]["value"], "updated_at": now}
        else:
            rec = _decay(rec, now)
        rec["label"] = txt
        rec["label_at"] = now if txt else 0.0
        st["sessions"][key] = rec
    else:
        st["global"]["label"] = txt
        st["global"]["label_at"] = now if txt else 0.0
    _save(st)
    return txt


@_serialized
def set_judge(session_key: str, word: str, value: float, *,
              why: str = "", src: str = "", mind: str = "",
              delta: float = 0.0) -> dict:
    """
    存**判读模型**给的心情（mood_judge）。这是影子字段：

      word  —— 心情名词（「被夸得有点飘」）
      value —— -1.00 ~ 1.00，两位小数
      src   —— 她当时的心里话（判读依据，方便复盘）

    老数值 `value`（-100~100）继续由 MEMO/关键词维护，两者先并行跑，
    等判读稳定了再把行为链路切到这边来。
    """
    key = str(session_key or "")
    now = time.time()
    try:
        v = round(max(-1.0, min(1.0, float(value))), 2)
    except Exception:
        v = 0.0
    # why 放宽到 80：40 字会把「整体氛围松散闲聊，没有明显…」这种依据截断，
    # 用户 2026-11-19 看到的依据就是断的。
    # mind：判读模型推断的「她此刻心里怎么想的」（她自己没写 MEMO 时的心里话）。
    rec = {"word": str(word or "").strip()[:12], "value": v,
           "why": str(why or "").strip()[:80],
           "mind": str(mind or "").strip()[:60],
           "src": str(src or "").strip()[:80],
           # 这一次判读的「涨幅」（判读原值之差）——好感/记忆刻痕/档案都靠它，
           # 和 value（当前水平，现在进行时）是两回事。
           "delta": round(float(delta or 0.0), 4),
           "at": now}
    st = _load()
    st["global"] = _decay(st["global"], now)
    if key:
        cur = st["sessions"].get(key)
        if cur is None:
            cur = {"value": st["global"]["value"], "updated_at": now}
        else:
            cur = _decay(cur, now)
        cur["judge"] = rec
        st["sessions"][key] = cur
    else:
        st["global"]["judge"] = rec
    _save(st)
    return rec


def get_judge(session_key: str = "") -> dict:
    """最近一次判读结果（没有就空 dict）。超过 FRESH 时间也算过期，交给调用方判断。"""
    m = get_mood(session_key)
    return dict(m.get("judge") or {})


def _label_of(raw: str) -> str:
    """`emotion:` 里写的东西 → 她的心情原话。

    固定那 9 个词（happy/sad…）翻成中文当原话；`neutral/平静` 当成「没写」；
    其它（「被夸得有点飘」「烦但还得忍着」）原样保留 —— 这就是她的自由表达。
    """
    r = re.sub(r"\s+", " ", str(raw or "")).strip()
    if not r:
        return ""
    try:
        from .aura_logic import EMOTION_ALIAS, emotion_cn
        if r.lower() in EMOTION_ALIAS:
            cn = emotion_cn(r)
            return "" if cn == "平静" else cn
    except Exception:
        pass
    return r[:MOOD_LABEL_MAX]


def _delta_from_text(text: str) -> float:
    """自由描述也让它稍微影响数值（不然「烦死了」和「好开心」对数值一样）。"""
    try:
        s, _strong = judge_message_mood(text)
    except Exception:
        return 0.0
    if not s:
        return 0.0
    return max(-12.0, min(12.0, s * 0.8))


@_serialized
def bump_mood(delta: float, session_key: str = "") -> float:
    """
    改心情：会话吃满额，全局只吃 GLOBAL_SHARE。
    这样「这个群的人惹她」只让这个群明显变冷，别的群只是轻微受牵连。
    """
    now = time.time()
    st = _load()
    st["global"] = _decay(st["global"], now)
    # 新会话要继承「加这次波动之前」的全局值，
    # 否则第一次进来就白吃一份 delta（A 群 -16 会变成 -21.6）。
    base_global = st["global"]["value"]
    st["global"]["value"] = _clamp(base_global + float(delta) * GLOBAL_SHARE)

    key = str(session_key or "")
    if not key:
        # 没有会话上下文（脚本/全局调整）→ 剩下的额度给全局
        st["global"]["value"] = _clamp(
            st["global"]["value"] + float(delta) * (1.0 - GLOBAL_SHARE)
        )
        value = st["global"]["value"]
    else:
        rec = st["sessions"].get(key)
        if rec is None:
            rec = {"value": base_global, "updated_at": now}
        else:
            rec = _decay(rec, now)
        rec["value"] = _clamp(rec["value"] + float(delta))
        rec["updated_at"] = now
        st["sessions"][key] = rec
        value = rec["value"]

    _save(st)
    return value


@_serialized
def set_value_from_judge(value: float, session_key: str = "", *, word: str = "",
                         why: str = "", src: str = "", about_qq: str = "",
                         mind: str = "") -> float:
    """
    判读模型给的**当前心情值**（-1.00~1.00）→ 直接写成她的心情。数值的唯一来源。

    用户 2026-11-20（第二轮修正）：
      「这里的设计算是外部导致内心效应，实际上不用记录 —— 我们可以看到她的内心活动，
        外部 llm 根据她的内心来判断心情以及阈值就够了，不需要外部的阈值记录。
        因为内心反应已经表达了她的承受能力，只需要参考 llm 生成的心情词、阈值和摘来的心理活动。」

    所以**没有 delta（增量）这个概念**：判读直接说「她此刻是 -0.28」，
    我们写进去就是了。`smooth` 只是防止一次误判让语气突变（代码层的事，不给模型看）。
    """
    try:
        target = max(-1.0, min(1.0, float(value))) * 100.0
    except Exception:
        return get_mood(session_key)["value"]
    # 单次判读的「最大步长」：防止模型一句话就把她从 +35 翻到 -20。
    # 外口径 0.30 = 内部 30 点；超出就按步长夹住（方向保留）。
    _lim = MAX_STEP * 100.0
    now = time.time()
    st = _load()
    st["global"] = _decay(st["global"], now)
    base_global = st["global"]["value"]

    def _mix(old: float) -> float:
        """先按 SMOOTH 平滑，再把「这一次实际走了多少」夹在 ±MAX_STEP 内。

        两件事分开做，才不会双重衰减：
          · SMOOTH 给心情惯性（小事只走一小截）；
          · MAX_STEP 兜底（真大事也最多一次走 0.30，不会一句话从 +0 顶到 +0.9）。
        """
        v = old + (target - old) * SMOOTH
        if v > old + _lim:
            v = old + _lim
        elif v < old - _lim:
            v = old - _lim
        return v

    st["global"]["value"] = _clamp(_mix(base_global) * GLOBAL_SHARE
                                   + base_global * (1.0 - GLOBAL_SHARE))
    key = str(session_key or "")
    # ⚠️ 流水要记「这个会话自己变了多少」，不是「和全局比变了多少」。
    #    以前用 value_now - base_global：会话值 -20、全局 0 → 记 -20；
    #    下一次 -35 又记 -35，累计成 -55，实际只掉了 35。
    #    安静建议靠这条流水判断「心情突然掉了多少」，所以必须修准。
    prev_for_log = base_global
    if not key:
        st["global"]["value"] = _clamp(_mix(base_global))
        value_now = st["global"]["value"]
    else:
        rec = st["sessions"].get(key)
        rec = _decay(rec, now) if rec else {"value": base_global, "updated_at": now}
        old = float(rec.get("value") or 0.0)
        prev_for_log = old
        rec["value"] = _clamp(_mix(old))
        rec["updated_at"] = now
        st["sessions"][key] = rec
        value_now = rec["value"]
    _save(st)
    try:
        # about_qq：这次心情变化是「因为谁」——安静建议要用它算「这个人让我掉了多少」。
        log_shift(value_now - prev_for_log, emotion=(word or "judge"),
                  label=(why or ""), reason="llm-judge", session_key=session_key,
                  about_qq=about_qq, src=src, mind=mind)
    except Exception:
        pass
    return value_now


def apply_delta(delta: float, session_key: str = "", *, word: str = "",
                why: str = "", src: str = "", about_qq: str = "",
                mind: str = "") -> float:
    """把判读给的**涨幅 Δ**累加到当前心情值上，返回新的心情值（外口径 -1~1）。

    和 set_value_from_judge 的区别：
      · set_value_from_judge：输入是「此刻心情水平」，会**覆盖**旧值（旧口径）；
      · apply_delta：输入是「这件事让她涨/跌多少」，**累加**到旧值上（新口径）。
    哪个生效由 MOOD_MODE 决定（默认 delta）。
    """
    try:
        d = max(-MOOD_MAX_DELTA, min(MOOD_MAX_DELTA, float(delta)))
    except Exception:
        return get_mood(session_key)["value"]
    if abs(d) < 1e-6:
        return get_mood(session_key)["value"]
    now = time.time()
    st = _load()
    st["global"] = _decay(st["global"], now)
    key = _scope_key(session_key)

    def _bump(rec: dict, amount: float) -> dict:
        old = float(rec.get("value") or 0.0)
        # 设计口径（mood-affinity-design.md §13.1）：心情值 ← clamp(心情值 + Δ)
        # ⚠️ 2026-11-22 我一度在这里加「饱和阻尼」—— 那是错的：
        #    一边倒到 -1 的根因是**旁观/刷图被给了负 Δ**（40 条里 32 条负数，
        #    均值 -0.073，全是「懒得掺和/被刷图刷到眼晕」），不是累加公式本身。
        #    正确做法是在 Δ 入口做「噪音丢弃 + 旁观归零」，让 Δ 保持设计里的分布，
        #    这样 §13.3 的 0.10/0.25/0.50 刻痕阈值才继续有意义。
        rec["value"] = _clamp(old + amount)
        rec["updated_at"] = now
        # 名词由数值分档决定（不是模型每次起名）
        rec["label"] = label_for(rec["value"] / 100.0)
        rec["label_at"] = now
        return rec

    if key:
        rec = st["sessions"].get(key) or {"value": st["global"]["value"],
                                          "updated_at": now}
        rec = _decay(rec, now)
        _bump(rec, d * 100.0)
        st["sessions"][key] = rec
        # 全局也吃一点（别的会话能感觉到），比例 GLOBAL_SHARE
        _bump(st["global"], d * 100.0 * GLOBAL_SHARE)
        value_now = float(rec["value"])
    else:
        _bump(st["global"], d * 100.0)
        value_now = float(st["global"]["value"])
    _save(st)
    try:
        log_shift(d, emotion=(word or "judge"), label=(why or ""),
                  reason="judge-delta", session_key=session_key,
                  about_qq=about_qq, src=src, mind=mind)
    except Exception:
        pass
    return round(value_now / 100.0, 4)


@_serialized
def reset(session_key: str = "", *, value: float = 0.0) -> float:
    """把某个会话的心情重置到 value（默认 0），并清掉名词/判读。

    为什么需要：历史误判（旁观被算成 -0.07，几十条累积）会把数值钉在 -1，
    改完代码也不会自己回到 0 —— `/测函数 心情 重置` 用它清理。
    """
    key = _scope_key(session_key)
    now = time.time()
    st = _load()
    st["global"] = _decay(st["global"], now)
    if key:
        rec = st["sessions"].get(key) or {"value": 0.0, "updated_at": now}
        rec["value"] = _clamp(float(value) * 100.0)
        rec["updated_at"] = now
        rec["last_active_at"] = now
        for _k in ("label", "label_at", "judge", "last_decay_at"):
            rec.pop(_k, None)
        st["sessions"][key] = rec
        out = float(rec["value"])
    else:
        st["global"]["value"] = _clamp(float(value) * 100.0)
        for _k in ("label", "label_at", "judge", "last_decay_at"):
            st["global"].pop(_k, None)
        out = float(st["global"]["value"])
    _save(st)
    return round(out / 100.0, 4)


def trend(session_key: str = "", *, about_qq: str = "",
          window_sec: float = 1800) -> dict:
    """
    最近一段时间的心情走势（统一换成 -1~1 量纲，和判读值一致）。

    ⚠️ 只看**这个会话**的流水；about_qq 给了就只看「因为他/她」的那几次。
    返回 {value, drop, spike, up, n, neg_n, window}：
      value 此刻心情｜drop 窗口内累计下降｜spike 单次最大跌幅｜up 累计回升
    """
    key = str(session_key or "")
    now = time.time()
    try:
        recs = [r for r in recent_shifts(now - float(window_sec))
                if str(r.get("session") or "") == key
                and str(r.get("reason") or "") != "anchor"
                and (not about_qq
                     or str(r.get("about_qq") or "") == str(about_qq))]
    except Exception:
        recs = []
    deltas = [float(r.get("delta") or 0.0) / 100.0 for r in recs]
    neg = [d for d in deltas if d < 0]
    try:
        value = float(get_mood(key).get("value") or 0.0) / 100.0
    except Exception:
        value = 0.0
    return {"value": round(value, 3), "n": len(deltas),
            "drop": round(-sum(neg), 3),
            "spike": round(max(0.0, -min(deltas)), 3) if deltas else 0.0,
            "up": round(sum(d for d in deltas if d > 0), 3),
            "neg_n": len(neg), "window": float(window_sec)}


def bump_by_emotion(emotion: str, *, weight: float = 1.0, reason: str = "",
                    about_qq: str = "", session_key: str = "") -> float:
    """
    她自己报的情绪 → 心情（**原话**存 label，数值按方向动一点）。

    这是心情的主通道（替代 judge_message_mood 的关键词硬判）。
    emotion 可以是固定的那 9 个词，也可以是**任何她自己写的话**
    （「被夸得有点飘」「烦但还得忍着」）—— 固定词只用来估数值方向，
    真正给她看的是原话。
    """
    from .aura_logic import normalize_emotion  # 局部导入，避免循环依赖

    raw = str(emotion or "").strip()
    emo = normalize_emotion(raw)
    label = _label_of(raw)
    if label:
        set_label(label, session_key)

    # 👇 2026-11-20：数值只认第三方判读（apply_judge_delta）。
    #    她自己写的 emotion / 关键词猜测只留「原话」，不再动数值 ——
    #    她自报的数值和字面情绪都不可靠（「被骂了」也可能写 happy）。
    if MOOD_FROM_JUDGE:
        return get_mood(session_key)["value"]

    delta = EMOTION_DELTA.get(emo, 0.0) * float(weight or 1.0)
    if not delta and label:
        # 自由描述：按字面猜个方向，别让「烦死了」和「好开心」对数值完全一样
        delta = _delta_from_text(label) * float(weight or 1.0)
    if not delta:
        return get_mood(session_key)["value"]
    log_shift(delta, emotion=emo, label=label, reason=reason,
              about_qq=about_qq, session_key=session_key)
    return bump_mood(delta, session_key)


def judge_message_mood(text: str) -> tuple[float, bool]:
    """⚠️ 兜底用：仅当 DeepSeek 这一轮没打 [[MEMO]] 时才拿它猜一下。

    返回 (delta, strong)。
    strong=True 表示情绪起伏很大，会强化记忆（情绪刻痕）。
    """
    if not text:
        return 0.0, False
    score = 0.0
    hits = 0
    for w in POSITIVE_WORDS:
        if w in text:
            score += 6.0
            hits += 1
    for w in NEGATIVE_WORDS:
        if w in text:
            score -= 8.0
            hits += 1
    strong = abs(score) >= 15.0
    return score, strong


def mood_prompt(session_key: str = "") -> str:
    """
    给 prompt 的心情块。

    ⚠️ 优先级：**她自己写的原话** > 按数值给的粗略描述。
       用户 2026-11-19：「心情不能是我们指定的几种，她要自己有写心情的权利。」
       所以有原话时就把原话给她（那才是她此刻的感觉），数值只影响语气浓淡。
    """
    m = get_mood(session_key)
    label = str(m.get("label") or "").strip()
    v = float(m.get("value") or 0.0)
    if label:
        tone = ("（语气可以更松快、更爱打趣一点）" if v >= 40
                else "（语气可以冷淡一点、话少一点）" if v <= -40 else "")
        return (f"（你现在的心情：{label} —— 这是你自己写下的，按这个感觉说话，"
                f"别跟对方解释「我现在心情是…」）" + tone)
    if v >= 40:
        return "（你现在心情特别好，说话会活泼一些，也更容易打趣）"
    if v >= 15:
        return "（你现在心情不错）"
    if v <= -40:
        return "（你现在心情很差，语气冷淡、不爱理人，容易怼人）"
    if v <= -15:
        return "（你现在心情有点低落，说话会少一点）"
    return ""


def status_block(session_key: str = "", speaker_qq: str = "") -> str:
    """
    【你现在的状态】—— 写进上下文背景。

    用户 2026-11-20：「把心情总结写到上下文背景里。比如上下文下面加上总结和状态，
      这样状态就会影响到接下来的回答，包括她是否想要安静、或者去主动私聊找某人倾诉。
      这个安静和倾诉不能硬心情阈值判定 —— 她状态只有一种，但结合当前状态和档案记忆，
      她会知道该去找谁说、在谁面前安静、安静多久。」

    所以这里**只给状态，不给结论**：想不想说话、要不要安静、找谁，由模型结合
    档案/记忆自己决定（chat_switch / 主动私聊都是她自己能调的工具）。
    """
    m = get_mood(session_key)
    j = m.get("judge") or {}
    label = str(m.get("label") or "").strip()
    word = str(j.get("word") or "").strip()
    val = None
    try:
        if j and (time.time() - float(j.get("at") or 0)) < JUDGE_FRESH:
            val = float(j.get("value"))
    except Exception:
        val = None

    lines = []
    # 2026-11-20：心情**名词**由数值分档给出（每 0.2 一档），不是模型每次起名。
    #   · label  —— 她自己写过的原话（MEMO 带出来的），有就单独一行；
    #   · word   —— 判读给的「刚才那件事的反应词」，单独一行，不混进心情名词。
    _lvl = float(m.get("value") or 0.0) / 100.0
    _state = label_for(_lvl)
    lines.append(f"· 心情：{_state}（{_lvl:+.2f}；-1 最难受 / 0 平静 / +1 最开心）")
    _a = float(m.get("a", AD_BASE_A))
    _d = float(m.get("d", AD_BASE_D))
    if abs(_a - AD_BASE_A) >= 0.08 or abs(_d - AD_BASE_D) >= 0.12:
        lines.append(f"· 你现在的状态：{_ad_word(_a)}"
                     f"｜掌控感 {_d:.2f}（0 被压着 / 1 说了算）")
    if label and label != _state:
        lines.append(f"· 你自己写的感觉：{label}")
    if word and word not in (_state, label):
        lines.append(f"· 刚才那下：{word}")
    # 2026-11-25：这里以前回显 `judge.mind`（判读模型从群聊编的），等于把猜测当成她的心理。
    # 现在只回显她自己写过的 [[THINK]]；没有就整行不写。judge.mind 只留给 /测函数 诊断。
    think = ""
    try:
        from . import mood_judge as _mj
        _items = _mj.recent_think_items(session_key, 1)
        if _items:
            think = str(_items[-1].get("text") or "").strip()
    except Exception:
        think = ""
    if think:
        lines.append(f"· 你心里那句话：{think}")

    # 关系四量（P3）：只在偏离基线时出现一行，别每轮都占字数
    try:
        if speaker_qq:
            from . import relationship as _rel
            _rl = _rel.render_line(speaker_qq)
            if _rl:
                lines.append(f"· 你俩的关系：{_rl}")
    except Exception:
        pass

    # 峰值事件：最近涨/跌幅度最大、且还没被后续反向波动抵消掉的那件事。
    # 用户 2026-11-20：心情名词要连这件事 + 当时的心理活动一起给她看；
    # 如果当前心情和这件事的方向相反，要明确说「虽然现在…但最近经历了…」。
    try:
        _pk = peak_event(session_key)
    except Exception:
        _pk = {}
    if _pk:
        _pd = float(_pk.get("delta") or 0.0)
        _pt = str(_pk.get("text") or "").strip()
        _pm = str(_pk.get("mind") or "").strip()
        if _pt:
            _opposite = ((_lvl > 0.05 and _pd < 0)
                         or (_lvl < -0.05 and _pd > 0))
            if _opposite and _pd < 0:
                _line = (f"· 你现在的心情状态是：{_state}，"
                         f"但是呢你最近经历了一件不愉快的事：{_pt}")
            elif _opposite and _pd > 0:
                _line = (f"· 你现在的心情状态是：{_state}，"
                         f"不过呢你最近也遇到一件挺开心的事：{_pt}")
            elif _pd > 0:
                _line = f"· 最近最让你开心的一件事：{_pt}"
            else:
                _line = f"· 最近最让你难受的一件事：{_pt}"
            if _pm:
                _line += f"（当时你心里：{_pm}）"
            lines.append(_line[:170])

    # 群聊总结（群聊才有）：直接复用审核那边打的标签总结
    key = str(session_key or "")
    if key.startswith("group_"):
        try:
            from . import group_scene
            sm = (group_scene.snapshot(key[6:]).get("summary") or {}).get("text") or ""
            if sm:
                lines.append(f"· 刚刚群里在聊：{str(sm)[:120]}")
        except Exception:
            pass

    if not key.startswith("group_"):
        # 私聊没有群聊总结 → 用判读的输入当「刚才发生了什么」
        _src = str(j.get("src") or "").strip()
        if _src:
            lines.append(f"· 刚才：{_src[:100]}")

    # 对「正在说话这个人」的好感 + 走势：她要知道「我喜不喜欢他、最近在涨还是在掉」，
    # 才谈得上「要不要对他安静 / 还要不要理他」。只给数值，不给结论。
    _qq = str(speaker_qq or "").strip()
    if _qq.isdigit():
        try:
            from . import profiles as _pf
            from . import relationship as _rln
            # 用 relationship.affinity_of：和「你俩的关系」那一行同源（含半衰期回落），
            # 以前这里读原始值、那里读衰减值，同一个人会显示两个数。
            _aff = float(_rln.affinity_of(_qq))
            _tr = _pf.affinity_trend(_qq)
            if abs(_aff) >= 0.05 or int(_tr.get("n") or 0) > 0:
                _gid = key[6:] if key.startswith("group_") else ""
                _nm = _pf.call_of(_qq, "", _gid) or _qq
                _mv = ("最近在往下掉" if float(_tr.get("drop") or 0) >= 0.03
                       else "最近在回暖" if float(_tr.get("up") or 0) >= 0.03
                       else "平稳")
                lines.append(f"· 你对{_nm}的好感：{_aff:+.2f}（{_mv}）")
        except Exception:
            pass
    if not lines:
        return ""
    lines.append(
        "（这些不用说出来，但它决定你现在的语气、想不想接话。"
        "要安静一会儿就用 chat_switch：target 写某个人 = 只屏蔽他（他的 @ 和图片也不接，"
        "群里别人照常）；target=\"here\" = 整个群安静；都可以指定多少分钟。"
        "想找谁说话就主动私聊 —— 找谁、说什么、安静多久，由你结合这些状态和你对他们的印象自己定，"
        "不是必须做。）")
    return "\n".join(lines)


def mood_factors(session_key: str = "") -> dict:
    """
    给发送概率、延迟、插话概率用的乘数（1 表示正常）。

    👇 2026-11-20：优先用**判读值**（-1~1，代表「此刻」），
       旧的 -100~100 只是长期基线。判读过期才退回基线。
       这样「主动接话 / 表情 / 延迟 / 主动私聊」全都自动跟着判读走。
    """
    # 数值本身已经由判读写入（set_value_from_judge，带平滑），
    # 这里直接用它 —— 不再单独读 judge.value，避免「平滑只作用于存储、不作用于行为」。
    _m = get_mood(session_key)
    base = float(_m.get("value") or 0.0)
    # 👇 群心情是**独立**的一层：整体心情不错，但在这个群 5 小时净掉 0.7，
    #    她也会明显少说话（用户 2026-11-20）。只对群会话生效。
    _gf = 1.0
    if str(session_key or "").startswith("group_"):
        try:
            from . import group_mood
            _gf = float(group_mood.chat_factor(session_key))
        except Exception:
            _gf = 1.0
    return {
        "voice": 1.0 + max(-0.8, base / 100.0),
        "sticker": 1.0 + max(-0.8, base / 100.0),
        "delay": 1.0 + max(-0.5, -base / 200.0),
        "chatty": (1.0 + max(-0.8, base / 100.0)) * _gf,
        "group": _gf,
        # a/d 一期只记录：这里只暴露，**不改** voice/sticker/delay/chatty
        "arousal": round(float(_m.get("a", AD_BASE_A)), 3),
        "dominance": round(float(_m.get("d", AD_BASE_D)), 3),
    }
