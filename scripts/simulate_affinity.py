#!/usr/bin/env python3
"""好感模型 v2.3 的数值模拟（只算数，不碰线上数据）。

v2.3 的核心原则（用户拍板）：
    好感 = 寄存在每个人那里的「心情涨落历史」。
    每条好感痕迹的权重来自 mood.observe 走出的实际位移 dv，
    **不用判读档位 impact，也不用任何名词（milestone=help/...）**。

模型：
    a = bond + Σ traces
    w   = AFF_DV_K · |dv|^γ · (1-|a|)^sat · conf · novelty   （正向）
        = -AFF_DV_K · |dv|^γ · (1-|a|)^sat_neg · NEG_BIAS      （负向）
    bond 上限 = 0.60·I^1.5 + 0.30·I_deep^1.2
    I_deep 由 peaks / repairs / warm_days 统计（全部从 dv 序列推）

用法：
    python3 scripts/simulate_affinity.py            # 全部表
    python3 scripts/simulate_affinity.py --json     # 机器可读
"""
from __future__ import annotations

import argparse
import json
import math

# ---------------- 参数（与设计文档 §3 一致）----------------
AFF_MAX = 0.99        # 好感硬上限：0.99 留给「知己」，不设 1.0
T_TRACE = 10.0        # 痕迹衰减时间常数（天）
P_TRACE = 0.5         # 幂律指数（ACT-R d≈0.5）
BOND_MAX = 0.60       # 浅层投入（日常）能撑到的底子
BOND_DEEP_MAX = 0.30  # 深度投入额外贡献（0.60+0.30=0.90）
BOND_EXP = 1.5        # 浅层 bond 随 I 的陡度
BOND_DEEP_EXP = 1.2   # 深层 bond 随 I_deep 的陡度
BOND_HL_D = 180.0     # bond 自身极慢衰减（半年减半）

# dv → 好感（三段式：日常线性 / 显著凸性 / 尾部跳变）
DV_K = 0.60           # 日常区换算系数
DV_GAMMA = 1.0        # 日常区 dv 的幂次
SAT_POS = 0.70        # 日常区正向饱和指数
SAT_NEG = 0.35        # 日常区负向饱和指数
NEG_BIAS = 1.6        # 负面事件权重（全局）

# 第二段：显著事件（0.15 ≤ |dv| < 0.30）——「心动 / 骤冷」
JUMP_FROM = 0.15      # 显著事件起点
JUMP_K = 1.6          # 凸性放大系数
JUMP_GAMMA = 1.5      # 凸性指数（>1 = 越大越不成比例）
JUMP_SAT = 0.35       # 高好感也推得动（比日常 0.70 小）

# 第三段：尾部事件（|dv| ≥ 0.30）——「心碎 / 一见钟情」
TAIL_FROM = 0.30      # 尾部事件起点
TAIL_BASE = 0.06      # 尾部基础跳变
TAIL_K = 2.0          # 尾部放大系数
TAIL_GAMMA = 1.8      # 尾部凸性（比第二段更陡）
TAIL_SAT = 0.15       # 几乎不看当前好感（0.95 也打得动）

W_MAX = 0.42          # 单次痕迹硬上限（当前 dv≤0.35 → 最大 0.362；0.42 为放宽预留）
GUARD_POS = 0.70      # a<0（有防备）时正向跳变的折扣

# 特殊通道
REPAIR_BASE = 0.16    # 道歉破冰基础步长（a<0 且 dv>0）
COMP_W = 0.004        # 一次「陪伴」（中性闲聊）的痕迹权重
COMP_CAP = 0.80       # 光靠陪伴最多到 0.80

# 涨落统计阈值（全部作用在 dv 上，不做名词判断）
PEAK_DV = 0.15        # |dv| ≥ 0.15 → 记一次 peak（显著事件）
LOVE_DV = 0.30        # dv ≥ 0.30 → 一见钟情/心碎级（peaks+2、κ≥0.5）
WARM_FULL = 0.25      # 7 天暖流达到多少算「一直在暖」（κ 满载）
PEAK_BONUS = 0.25     # 一次 peak 额外加的巩固比例
HURT_GRUDGE = 0.15    # 负向 peak 加多少记仇
REPAIR_WINDOW_D = 7   # 负 peak 后多少天内暖回来算一次 repair

# 巩固 / 门槛
CONS_FROM = 0.75      # 巩固起点 a
CONS_TO = 0.95        # 巩固满载点 a
CONS_K = 0.60         # 满载时的沉淀比例
TIER_SOFT = 0.02      # 门槛软上限余量
TIER_BASE = 0.10      # 连「眼熟」都没到时的软上限
TRACE_MAX = 400       # 每人最多保留多少条痕迹（按天聚合，裁最弱的）

# 分层门槛（v2.5 刻度重标定：0.75~0.85 = 暧昧，0.85+ = 爱）
TIERS = [
    (0.15, "眼熟", dict(days=7)),
    (0.30, "熟人", dict(days=14)),
    (0.45, "朋友", dict(days=60, notes=10)),
    (0.60, "很好", dict(days=120, notes=20)),
    (0.75, "暧昧", dict(days=180, peaks=3)),
    (0.85, "爱", dict(days=240, peaks=5)),
    (0.95, "唯一的爱", dict(days=365, peaks=8, repairs=1)),
]

PERSONAS = [
    # 名字, 记录条数, 认识天数, 近7天消息数, 当前好感, peaks, repairs, warm_days
    ("新认识", 2, 3, 10, 0.15, 0, 0, 1),
    ("普通群友", 8, 30, 60, 0.30, 0, 0, 5),
    ("好朋友", 20, 90, 150, 0.75, 1, 0, 20),
    ("挚友", 30, 180, 300, 0.90, 5, 2, 60),
    ("讨厌的人", 6, 60, 40, -0.60, 0, 0, 0),
]

# 现网 v1 的 intimacy / 近期互动（仅用于对比）
CUR = {"新认识": (0.10, 0.10), "普通群友": (0.30, 0.25),
       "好朋友": (0.98, 1.00), "挚友": (0.98, 1.00), "讨厌的人": (0.30, 0.20)}


def clamp(v: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


def investment(notes: float, days: float, msgs: float) -> float:
    """浅层投入：日常聊天的量。"""
    return (0.50 * min(1.0, notes / 20.0)
            + 0.30 * min(1.0, days / 120.0)
            + 0.20 * min(1.0, msgs / 150.0))


def deep_investment(peaks: float, repairs: float, warm_days: float) -> float:
    """深度投入：全部来自心情涨落统计（没有名词判断）。"""
    return (0.50 * min(1.0, peaks / 10.0)
            + 0.30 * min(1.0, repairs / 3.0)
            + 0.20 * min(1.0, warm_days / 60.0))


def bond_max_of(inv: float, inv_deep: float = 0.0) -> float:
    return (BOND_MAX * (max(0.0, inv) ** BOND_EXP)
            + BOND_DEEP_MAX * (max(0.0, inv_deep) ** BOND_DEEP_EXP))


def bond_of(inv: float) -> float:
    """兼容旧调用：只给浅层投入时的 bond 上限。"""
    return BOND_MAX * (max(0.0, inv) ** BOND_EXP)


def w_from_dv(dv: float, a: float, *, conf: float = 1.0,
              novelty: float = 1.0) -> float:
    """一条 dv 对应的好感痕迹权重（三段式，取涨跌幅不取当前值）。

    日常区 |dv| < 0.15：线性 + 强饱和（高好感推不动）
    显著区 0.15~0.30 ：凸性放大 + 弱饱和（心动 / 骤冷）
    尾部区 |dv| ≥ 0.30 ：跳变 + 几乎不饱和（心碎 / 一见钟情）
    单次夹 ±W_MAX（单次上限）。
    """
    a = clamp(a)
    x = abs(float(dv))
    if x < 1e-9:
        return 0.0
    if x >= TAIL_FROM:
        gain = TAIL_BASE + TAIL_K * (x ** TAIL_GAMMA)
        sat = TAIL_SAT
    elif x >= JUMP_FROM:
        gain = JUMP_K * (x ** JUMP_GAMMA)
        sat = JUMP_SAT
    else:
        gain = DV_K * (x ** DV_GAMMA)
        sat = SAT_NEG if (dv > 0 and a < 0) else (SAT_POS if dv > 0 else SAT_NEG)
    gain *= max(0.0, conf) * max(0.0, novelty)
    if dv < 0:
        w = -gain * NEG_BIAS * (1.0 - abs(a)) ** sat
    else:
        if a < 0 and x >= JUMP_FROM:
            gain *= GUARD_POS      # 心里还有防备，好事也得打折
        w = gain * (1.0 - abs(a)) ** sat
    return max(-W_MAX, min(W_MAX, w))


def comp_w(a: float) -> float:
    """中性陪伴（没走出 dv）的小额痕迹，有 0.80 天花板。"""
    a = clamp(a)
    room = max(0.0, (COMP_CAP - max(0.0, a)) / COMP_CAP)
    return COMP_W * (1.0 - abs(a)) ** 0.5 * room


def repair_w(a: float, grudge: float = 0.0) -> float:
    """a<0 且这次 dv>0：用 0.35 的饱和指数，更容易化冰。"""
    a = clamp(a)
    return REPAIR_BASE * (1.0 - abs(a)) ** 0.35 * (1.0 - 0.5 * grudge)


def kappa(a: float, warmth7: float) -> float:
    """巩固比例：既要好感高，又要最近真的暖（warmth7 是闸门）。"""
    if a <= CONS_FROM:
        return 0.0
    ka = CONS_K * clamp01((a - CONS_FROM) / (CONS_TO - CONS_FROM))
    kw = clamp01(warmth7 / WARM_FULL)
    return ka * kw


def tier_cap(*, days: float, notes: float, peaks: float,
             repairs: float) -> float:
    """分层门槛：条件不满足时，好感**最多涨到**「上一档 + TIER_SOFT」。

    这是「上涨上限」，不是「当前值上限」：已 0.9 的人不会被打回 0.82。
    """
    have = dict(days=days, notes=notes, peaks=peaks, repairs=repairs)
    prev = TIER_BASE
    for th, _name, need in TIERS:
        for key, val in need.items():
            if have.get(key, 0) < val:
                return prev + TIER_SOFT
        prev = th
    return AFF_MAX


def growth_cap(a_old: float, a_new: float, cap: float) -> float:
    """门槛只封上涨，不回溯打压：min(新值, max(旧值, 门槛))。"""
    return min(a_new, max(a_old, cap))


def tier_name(a: float) -> str:
    """纯按数值给层级名（条件是否满足看 cap，不混在一起）。"""
    if a < -0.60:
        return "仇视"
    if a < -0.45:
        return "厌恶"
    if a < -0.30:
        return "讨厌"
    if a < -0.15:
        return "不爽"
    name = "中性"
    for th, nm, _need in TIERS:
        if a + 1e-9 >= th:
            name = nm
    return name


def value_of(bond: float, traces, now: float = 0.0,
             hi: float = AFF_MAX) -> float:
    """a = bond + Σ w·(1+Δt/T)^-p，硬上限 AFF_MAX=0.99。"""
    s = 0.0
    for t, w in traces:
        s += w * (1.0 + max(0.0, now - t) / T_TRACE) ** (-P_TRACE)
    return clamp(bond + s, -1.0, hi)


def add_trace(traces, now: float, w: float):
    """同一天的多条痕迹合并成一条（否则长尾会被条数上限裁掉）。"""
    if abs(w) < 1e-6:
        return traces
    if traces and int(traces[-1][0]) == int(now):
        t0, w0 = traces[-1]
        traces[-1] = (t0, w0 + w)
    else:
        traces.append((now, w))
    if len(traces) > TRACE_MAX:
        traces.sort(key=lambda x: -abs(x[1]))
        traces = sorted(traces[:TRACE_MAX], key=lambda x: x[0])
    return traces


class AffState:
    """一个人的好感状态：bond + traces + 涨落统计。"""

    def __init__(self, a0: float = 0.0, bond: float = 0.0,
                 bond_max: float = BOND_MAX):
        self.bond = bond
        self.bond_max = bond_max
        self.traces = [(0.0, a0 - bond)] if abs(a0 - bond) > 1e-9 else []
        self.dv_log = []          # [(t, dv)] 只用于算 warmth7 / 审计
        self.peaks = 0
        self.hurts = 0
        self.repairs = 0
        self.warm_days = 0
        self.peak_last = None     # 上次 peak 时间
        self.hurt_pending = False # 有负 peak 还没被暖回来
        self._warm_day = None

    def value(self, now: float) -> float:
        return value_of(self.bond, self.traces, now)

    def warmth7(self, now: float) -> float:
        return sum(dv for t, dv in self.dv_log
                   if dv > 0 and now - t <= 7.0)

    def apply_dv(self, now: float, dv: float, *, novelty: float = 1.0,
                 consolidate: bool = True, repair: bool = False) -> float:
        """吃一条心情位移，返回这次好感痕迹的总权重。

        repair=True（判读明确是道歉，且 a<0）时走独立破冰通道，
        不再乘 dv —— 这是唯一保留的语义通道（用户已拍板 REPAIR_BASE）。
        """
        if abs(dv) < 1e-9:
            return 0.0
        a = self.value(now)
        if repair and a < 0:
            w = repair_w(a)
        else:
            w = w_from_dv(dv, a, novelty=novelty)
        if abs(w) < 1e-9:
            return 0.0
        k = kappa(a, self.warmth7(now)) if consolidate else 0.0
        if consolidate and abs(dv) >= PEAK_DV:
            k = min(0.85, k + PEAK_BONUS)
        if consolidate and dv >= LOVE_DV:
            k = max(k, 0.5)
        self.traces = add_trace(self.traces, now, w * (1.0 - k))
        self.bond = min(self.bond + w * k, self.bond_max)
        self.dv_log.append((now, dv))
        # ---- 涨落统计（没有一条是名词判断）----
        if abs(dv) >= PEAK_DV:
            self.peaks += 1
            if dv >= LOVE_DV:
                self.peaks += 1          # 一见钟情算两次
            if dv < 0:
                self.hurts += 1
                self.hurt_pending = True
            self.peak_last = now
        if dv > 0:
            d = int(now)
            if self._warm_day != d:
                self.warm_days += 1
                self._warm_day = d
            if self.hurt_pending:
                self.repairs += 1        # 负 peak 后 7 天内暖回来 = 和好
                self.hurt_pending = False
        return w

    def apply_comp(self, now: float, n: int = 1) -> None:
        for _ in range(n):
            a = self.value(now)
            self.traces = add_trace(self.traces, now, comp_w(a))

    def i_deep(self) -> float:
        return deep_investment(self.peaks, self.repairs, self.warm_days)


# ---------------------------------------------------------------- 衰减
def decay_v2(a0: float, inv: float, days: float, inv_deep: float = 0.0) -> float:
    b = bond_max_of(inv, inv_deep) if a0 > 0 else 0.0
    b = min(b, max(0.0, a0))
    heat = a0 - b
    if heat >= 0:
        return clamp(b + heat * (1.0 + days / T_TRACE) ** (-P_TRACE))
    return clamp(a0 * (1.0 + days / 30.0) ** (-0.5))


def decay_v1(a0: float, intimacy: float, contact: float, days: float) -> float:
    depth = max(0.1, (1.0 + 2.0 * intimacy) * (1.0 + 0.5 * contact))
    return a0 * math.pow(0.5, days / (7.0 * depth))


# ---------------------------------------------------------------- 表
def table_decay() -> list:
    out = []
    for name, n, d, m, a0, pk, rp, wd in PERSONAS:
        inv = investment(n, d, m)
        inv_deep = deep_investment(pk, rp, wd)
        it, ct = CUR[name]
        row = {"who": name, "a0": a0, "I": round(inv, 3),
               "I_deep": round(inv_deep, 3),
               "bond_max": round(bond_max_of(inv, inv_deep), 3)}
        for label, days in (("1周", 7), ("1月", 30), ("3月", 90), ("1年", 365)):
            row[label] = round(decay_v2(a0, inv, days, inv_deep), 3)
            row[label + "Δ"] = round(row[label] - a0, 3)
            row[label + "v1Δ"] = round(decay_v1(a0, it, ct, days) - a0, 3)
        out.append(row)
    return out


DV_COLS = (0.02, 0.10, 0.15, 0.22, 0.30, 0.35, -0.10, -0.22, -0.30, -0.35)


def table_dv_map() -> list:
    out = []
    for a in (-0.60, -0.30, 0.0, 0.30, 0.60, 0.80, 0.90, 0.95):
        row = {"a": a}
        for dv in DV_COLS:
            row["%+.2f" % dv] = round(w_from_dv(dv, a), 3)
        out.append(row)
    return out


def sim_dv_stream(days: int = 365, *, per_day: int = 3, bond: float = 0.43,
                  bond_max: float = 0.60, a0: float = 0.80,
                  weekly_dv: float = 0.0, peak_every: int = 0,
                  peak_dv: float = 0.28, hurt_every: int = 0,
                  hurt_dv: float = -0.22, consolidate: bool = True,
                  notes: int = 30, apply_tier: bool = True) -> dict:
    """模拟真实 dv 流：日常陪伴 + 每周一次普通暖 + 偶尔高光/受伤。

    apply_tier=True 时套 §2.9 分层门槛（只封上涨、不回溯）。
    """
    st = AffState(a0=a0, bond=bond, bond_max=bond_max)
    curve, prev = [], a0
    for d in range(days):
        st.apply_comp(d, per_day)
        if weekly_dv and d > 0 and d % 7 == 0:
            st.apply_dv(d, weekly_dv, consolidate=consolidate)
        if peak_every and d > 0 and d % peak_every == 0:
            st.apply_dv(d, peak_dv, consolidate=consolidate)
        if hurt_every and d > 0 and d % hurt_every == 0:
            st.apply_dv(d, hurt_dv, consolidate=consolidate)
        a_now = st.value(d)
        if apply_tier:
            cap = tier_cap(days=d + 1, notes=notes, peaks=st.peaks,
                           repairs=st.repairs)
            a_now = growth_cap(prev, a_now, cap)
        prev = a_now
        if d in (0, 29, 59, 89, 179, 269, 364):
            curve.append((d + 1, round(a_now, 3)))
    stop30 = value_of(st.bond, st.traces, days + 30)
    stop90 = value_of(st.bond, st.traces, days + 90)
    stop365 = value_of(st.bond, st.traces, days + 365)
    return {"curve": curve, "peaks": st.peaks, "hurts": st.hurts,
            "repairs": st.repairs, "warm_days": st.warm_days,
            "bond": round(st.bond, 3), "a": round(prev, 3),
            "stop30": round(stop30, 3), "stop90": round(stop90, 3),
            "stop365": round(stop365, 3)}


def sim_stock(days: int = 730, seed: int = 7) -> list:
    """像心情起伏一样：大部分日子小波动，偶尔暴涨（心动）或暴跌（心碎）。

    日常：每天 2 句陪伴 + 70% 概率一次小 dv（均值 +0.02）
    心动：平均每 45 天一次 dv≈+0.32
    心碎：平均每 90 天一次 dv≈-0.33
    返回每 30 天的 (day, a) 序列。
    """
    import random
    rnd = random.Random(seed)
    st = AffState(a0=0.30, bond=0.10, bond_max=0.60)
    out = []
    for d in range(days):
        st.apply_comp(d, 2)
        if rnd.random() < 0.70:
            st.apply_dv(d, rnd.gauss(0.02, 0.04))
        if rnd.random() < 1.0 / 120:
            st.apply_dv(d, rnd.choice([0.35, 0.32, 0.30]))
        if rnd.random() < 1.0 / 180:
            st.apply_dv(d, rnd.choice([-0.35, -0.32, -0.30]))
        if d % 30 == 29:
            out.append((d + 1, round(st.value(d), 3)))
    return out


def sim_breakice(a0: float = -0.60) -> list:
    """讨厌的人：第 0/1/3 天各一次真诚道歉，之后正常聊天。"""
    st = AffState(a0=a0, bond=0.0, bond_max=0.60)
    out = []
    for d in (0, 1, 3):
        st.apply_dv(d, 0.12, repair=True)
        out.append((d + 1, round(st.value(d), 3)))
    for d in (6, 29, 89):
        out.append((d + 1, round(st.value(d), 3)))
    return out


def breakeven_w(a_target: float, per_day: int = 1, days: int = 365) -> float:
    """每天 per_day 句陪伴、持续 days 天，恰好把好感维持在 a_target 所需的单句权重。"""
    lo, hi = 0.0, 0.05
    for _ in range(40):
        mid = (lo + hi) / 2
        traces = []
        for d in range(days):
            for _ in range(per_day):
                a = value_of(0.0, traces, d)
                room = max(0.0, (COMP_CAP - max(0.0, a)) / COMP_CAP)
                traces = add_trace(traces, d,
                                   mid * (1.0 - abs(a)) ** 0.5 * room)
        if value_of(0.0, traces, days) < a_target:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2, 4)


def table_tiers() -> list:
    out = []
    for name, n, d, m, a0, pk, rp, wd in PERSONAS:
        cap = tier_cap(days=d, notes=n, peaks=pk, repairs=rp) if a0 >= 0 else None
        out.append({"who": name, "a0": a0,
                    "cap": round(cap, 2) if cap is not None else None,
                    "tier": tier_name(a0)})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    dec, dvmap, tiers = table_decay(), table_dv_map(), table_tiers()
    daily = {n: sim_dv_stream(365, per_day=n, a0=0.0, bond=0.0,
                              apply_tier=False)["curve"]
             for n in (1, 3, 10)}
    brk = sim_breakice()
    be = {a: breakeven_w(a, 1) for a in (0.3, 0.5, 0.7)}
    climb = {
        "只陪伴": sim_dv_stream(weekly_dv=0.0),
        "每周暖+0.12": sim_dv_stream(weekly_dv=0.12),
        "每周暖+每30天高光(不巩固)": sim_dv_stream(weekly_dv=0.12,
                                                peak_every=30,
                                                consolidate=False),
        "每周暖+每30天高光+0.28": sim_dv_stream(weekly_dv=0.12, peak_every=30),
        "同上+bond0.74": sim_dv_stream(weekly_dv=0.12, peak_every=30,
                                       bond=0.74, bond_max=0.90),
        "同上+每90天受伤-0.22": sim_dv_stream(weekly_dv=0.12, peak_every=30,
                                            hurt_every=90, bond=0.74,
                                            bond_max=0.90),
    }
    if args.json:
        print(json.dumps({"decay": dec, "dv_map": dvmap, "daily": daily,
                          "breakice": brk, "breakeven_w": be,
                          "climb": {k: v["curve"] for k, v in climb.items()},
                          "climb_stats": {k: {x: v[x] for x in
                                              ("peaks", "hurts", "repairs",
                                               "warm_days", "bond", "a")}
                                          for k, v in climb.items()},
                          "tiers": tiers},
                         ensure_ascii=False, indent=1))
        return 0

    print("=" * 104)
    print("A. 时间衰减：v1（现网）vs v2.3")
    print("=" * 104)
    print("%-8s %5s %6s %6s | %-15s %-15s %-10s" %
          ("人设", "a0", "I", "bond", "1月(v1/v2)", "3月(v1/v2)", "1年 v2"))
    for r in dec:
        print("%-8s %5.2f %6.2f %6.2f | %-15s %-15s %-10s" %
              (r["who"], r["a0"], r["I"], r["bond_max"],
               "%+.2f/%+.2f" % (r["1月v1Δ"], r["1月Δ"]),
               "%+.2f/%+.2f" % (r["3月v1Δ"], r["3月Δ"]),
               "%.2f" % r["1年"]))

    print()
    print("=" * 104)
    print("B. 好感只认 dv（三段式）：日常线性 / 显著凸性 / 尾部跳变")
    print("=" * 104)
    head = ["a"] + ["%+.2f" % dv for dv in DV_COLS]
    print(("%6s" + " %7s" * len(DV_COLS)) % tuple(head))
    for r in dvmap:
        print(("%6.2f" + " %7.3f" * len(DV_COLS)) %
              tuple([r["a"]] + [r["%+.2f" % dv] for dv in DV_COLS]))
    print("（|dv|<0.15 日常；0.15~0.30 心动/骤冷；≥0.30 心碎/一见钟情）")

    print()
    print("=" * 104)
    print("C. 陪伴 vs 衰减：每天聊 N 句、连续 365 天，好感会走到哪")
    print("=" * 104)
    print("%-10s %s" % ("每天句数", "第1天 / 1周 / 1月 / 2月 / 3月 / 半年 / 1年"))
    for n, curve in daily.items():
        print("%-10s %s" % (n, " / ".join("%.2f" % v for _, v in curve)))
    print("（起点 0.00；没有 dv，只有「陪伴」痕迹；天花板 0.80）")

    print()
    print("=" * 104)
    print("D. 讨厌的人道歉破冰（a0=-0.60，第 1/2/4 天各一次 dv=+0.12）")
    print("=" * 104)
    print(" → ".join("第%d天 %.2f" % (d, v) for d, v in brk))

    print()
    print("=" * 104)
    print("E. 抵消系数：每天 1 句陪伴，要把好感稳在目标值，单句需要多重")
    print("=" * 104)
    for a, w in be.items():
        print("  稳住 %.1f → 单句权重 ≥ %.4f（默认 %.4f，≈ 每天 %.1f 句）"
              % (a, w, COMP_W, w / COMP_W))

    print()
    print("=" * 104)
    print("F. 0.8 之后怎么继续涨（起点 a=0.80、bond=0.43、每天 3 句陪伴）")
    print("=" * 104)
    print("%-26s %s" % ("路径", "第1天 / 1月 / 2月 / 3月 / 半年 / 9月 / 1年"))
    for k, r in climb.items():
        print("%-26s %s" % (k, " / ".join("%.2f" % v for _, v in r["curve"])))
    print()
    print("%-26s %6s %6s %8s %10s %7s %7s %7s %8s" %
          ("路径", "peaks", "hurts", "repairs", "warm_days", "bond",
           "停30天", "停90天", "停1年"))
    for k, r in climb.items():
        print("%-26s %6d %6d %8d %10d %7.2f %7.2f %7.2f %8.2f" %
              (k, r["peaks"], r["hurts"], r["repairs"], r["warm_days"],
               r["bond"], r["stop30"], r["stop90"], r["stop365"]))
    print("（peaks/repairs/warm_days 全部从 dv 序列自动统计，无名词判断）")
    print("（「停N天」= 一年后完全不理她，好感会掉到哪：巩固过的停在 bond 附近，没巩固的继续掉）")

    print()
    print("=" * 104)
    print("G. 分层门槛：能涨到多高，取决于「心动过几次」")
    print("=" * 104)
    print("%-8s %5s %8s %8s" % ("人设", "a0", "上涨上限", "数值层级"))
    for r in tiers:
        cap = "—" if r["cap"] is None else "%.2f" % r["cap"]
        print("%-8s %5.2f %8s %8s" % (r["who"], r["a0"], cap, r["tier"]))
    print("门槛：眼熟0.15(d7) / 熟人0.30(d14) / 朋友0.45(d60,n10) / 很好0.60(d120,n20)")
    print("      / 暧昧0.75(d180,peaks3) / 爱0.85(d240,peaks5) / 唯一的爱0.95(d365,peaks8,repairs1)")
    print("（上限只封「上涨」，不回溯打压；负值不受上涨门槛限制）")

    print()
    print("=" * 104)
    print("H. 起伏式走势：日常小波动 + 偶发心动/心碎（起点 0.30，24 个月）")
    print("=" * 104)
    stock = sim_stock()
    print("月份  " + " ".join("%4d" % (d // 30) for d, _ in stock))
    print("好感  " + " ".join("%4.2f" % v for _, v in stock))
    jumps = [(stock[i][1] - stock[i - 1][1]) for i in range(1, len(stock))]
    big = [round(x, 2) for x in jumps if abs(x) >= 0.08]
    print("单月跳变 ≥0.08 的月份：" + (", ".join("%+.2f" % x for x in big) or "无"))
    print("（日常靠陪伴缓慢爬，心动/心碎靠尾部通道一次跳 0.2~0.3）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
