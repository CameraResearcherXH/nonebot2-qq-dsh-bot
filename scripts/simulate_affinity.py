#!/usr/bin/env python3
"""好感模型 v2.1 的数值模拟（只算数，不碰线上数据）。

模型：a = bond + Σ traces（ACT-R 式幂律痕迹求和）
  每条事件/陪伴都是一条会衰减的「痕迹」，天生解决「幂律不能增量叠加」的问题，
  也让「陪伴抵消衰减」有解析解（见 docs/affinity-quantification-design.md §2.5）。

用法：
    python3 scripts/simulate_affinity.py            # 全部表
    python3 scripts/simulate_affinity.py --json     # 机器可读
"""
from __future__ import annotations

import argparse
import json
import math

# ---------------- 参数（与设计文档 §3 一致）----------------
T_TRACE = 10.0        # 痕迹衰减时间常数（天）
P_TRACE = 0.5         # 幂律指数（ACT-R d≈0.5）
BOND_MAX = 0.60       # bond 上限
BOND_EXP = 1.5        # bond 随投入 I 的陡度
BOND_HL_D = 180.0     # bond 自身极慢衰减（半年减半）
STEP = {1: 0.030, 2: 0.070}   # |impact| → 基础步长
SAT_POS = 0.70        # 正向饱和指数
SAT_NEG = 0.35        # 负向饱和指数
NEG_BIAS = 1.6        # 负面事件权重
REPAIR_BASE = 0.16    # 道歉破冰基础步长（只对负好感生效）
COMP_W = 0.004        # 一次「陪伴」（闲聊一句）的痕迹权重
COMP_CAP = 0.75       # 光靠陪伴最多到 0.75：再往上要靠事件/投入
TRACE_MAX = 400       # 每人最多保留多少条痕迹（按天聚合，裁最弱的）

PERSONAS = [
    # 名字, 记录条数, 认识天数, 近7天消息数, 当前好感
    ("新认识", 2, 3, 10, 0.15),
    ("普通群友", 8, 30, 60, 0.30),
    ("好朋友", 20, 90, 150, 0.75),
    ("挚友", 30, 180, 300, 0.90),
    ("讨厌的人", 6, 60, 40, -0.60),
]

# 现网 v1 的 intimacy / 近期互动（仅用于对比）
CUR = {"新认识": (0.10, 0.10), "普通群友": (0.30, 0.25),
       "好朋友": (0.98, 1.00), "挚友": (0.98, 1.00), "讨厌的人": (0.30, 0.20)}


def clamp(v: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def investment(notes: float, days: float, msgs: float) -> float:
    return (0.50 * min(1.0, notes / 20.0)
            + 0.30 * min(1.0, days / 120.0)
            + 0.20 * min(1.0, msgs / 150.0))


def bond_of(inv: float) -> float:
    return BOND_MAX * (max(0.0, inv) ** BOND_EXP)


def trace_w(kind, a: float, *, conf: float = 1.0, novelty: float = 1.0,
            grudge: float = 0.0) -> float:
    """一条痕迹的初始权重。kind: 'repair' / 'comp' / +1 / +2 / -1 / -2。"""
    a = clamp(a)
    if kind == "repair":
        return REPAIR_BASE * (1.0 - abs(a)) ** 0.35 * (1.0 - 0.5 * grudge)
    if kind == "comp":
        # 陪伴有天花板：日常闲聊能把关系养到 0.75 左右，
        # 再往上必须靠「事件」（被夸/一起经历事情）和 bond 投入。
        room = max(0.0, (COMP_CAP - max(0.0, a)) / COMP_CAP)
        return COMP_W * (1.0 - abs(a)) ** 0.5 * room
    k = int(kind)
    if abs(k) not in STEP:
        return 0.0
    base = STEP[abs(k)] * max(0.0, conf) * max(0.0, novelty)
    if k > 0:
        return base * (1.0 - abs(a)) ** SAT_POS
    return -base * NEG_BIAS * (1.0 - abs(a)) ** SAT_NEG


def value_of(bond: float, traces, now: float = 0.0) -> float:
    """a = bond + Σ w·(1+Δt/T)^-p。"""
    s = 0.0
    for t, w in traces:
        s += w * (1.0 + max(0.0, now - t) / T_TRACE) ** (-P_TRACE)
    return clamp(bond + s)


def add_trace(traces, now: float, w: float):
    """同一天的多条痕迹合并成一条（否则长尾会被条数上限裁掉）。

    幂律 p=0.5 的记忆很长：如果按「最近 N 条」裁剪，等于把一年前的陪伴
    全部删掉，陪伴永远攒不起来。所以按天聚合，再按权重裁最弱的。
    """
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


# ---------------------------------------------------------------- 衰减
def decay_v2(a0: float, inv: float, days: float) -> float:
    b = bond_of(inv) if a0 > 0 else 0.0
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
    for name, n, d, m, a0 in PERSONAS:
        inv = investment(n, d, m)
        it, ct = CUR[name]
        row = {"who": name, "a0": a0, "I": round(inv, 3)}
        for label, days in (("1周", 7), ("1月", 30), ("3月", 90), ("1年", 365)):
            row[label] = round(decay_v2(a0, inv, days), 3)
            row[label + "Δ"] = round(row[label] - a0, 3)
            row[label + "v1Δ"] = round(decay_v1(a0, it, ct, days) - a0, 3)
        out.append(row)
    return out


def table_events() -> list:
    out = []
    for a in (-0.80, -0.50, 0.0, 0.30, 0.60, 0.90):
        out.append({
            "a": a,
            "+2": round(trace_w(2, a), 3),
            "+1": round(trace_w(1, a), 3),
            "-1": round(trace_w(-1, a), 3),
            "-2": round(trace_w(-2, a), 3),
            "道歉": round(trace_w("repair", a), 3),
            "陪伴": round(trace_w("comp", a), 3),
        })
    return out


def sim_daily(per_day: int, days: int = 365, a0: float = 0.0,
              extra_events=()) -> list:
    """每天 per_day 句闲聊（陪伴痕迹），可插入 (day, kind) 事件。"""
    traces, bond = [], 0.0
    ev = dict(extra_events)
    curve = []
    for d in range(days):
        for _ in range(per_day):
            a = value_of(bond, traces, d)
            traces = add_trace(traces, d, trace_w("comp", a))
        if d in ev:
            a = value_of(bond, traces, d)
            traces = add_trace(traces, d, trace_w(ev[d], a))
        if d in (0, 6, 29, 89, 179, 364):
            curve.append((d + 1, round(value_of(bond, traces, d), 3)))
    return curve


def sim_breakice(a0: float = -0.60) -> list:
    """讨厌的人：第 0/1/3 天各一次真诚道歉，之后正常聊天。"""
    traces = [(0, a0)]
    out = []
    for d, kind in ((0, "repair"), (1, "repair"), (3, "repair")):
        a = value_of(0.0, traces, d)
        traces = add_trace(traces, d, trace_w(kind, a))
        out.append((d + 1, round(value_of(0.0, traces, d), 3)))
    for d in (6, 29, 89):
        out.append((d + 1, round(value_of(0.0, traces, d), 3)))
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    dec, evt = table_decay(), table_events()
    daily = {n: sim_daily(n) for n in (1, 3, 10)}
    brk = sim_breakice()
    be = {a: breakeven_w(a, 1) for a in (0.3, 0.5)}
    if args.json:
        print(json.dumps({"decay": dec, "events": evt,
                          "daily": daily, "breakice": brk,
                          "breakeven_w": be}, ensure_ascii=False, indent=1))
        return 0

    print("=" * 92)
    print("A. 时间衰减：v1（现网）vs v2.1")
    print("=" * 92)
    print("%-8s %5s %5s | %-15s %-15s %-15s" %
          ("人设", "a0", "I", "1月(v1/v2)", "3月(v1/v2)", "1年 v2"))
    for r in dec:
        print("%-8s %5.2f %5.2f | %-15s %-15s %-15s" %
              (r["who"], r["a0"], r["I"],
               "%+.2f/%+.2f" % (r["1月v1Δ"], r["1月Δ"]),
               "%+.2f/%+.2f" % (r["3月v1Δ"], r["3月Δ"]),
               "%.2f" % r["1年"]))

    print()
    print("=" * 92)
    print("B. 事件映射：同一次事件，在不同好感下推动多少")
    print("=" * 92)
    print("%6s %9s %9s %9s %9s %9s %9s" %
          ("当前a", "被夸+2", "被夸+1", "被伤-1", "被伤-2", "道歉", "闲聊一句"))
    for r in evt:
        print("%6.2f %9.3f %9.3f %9.3f %9.3f %9.3f %9.3f" %
              (r["a"], r["+2"], r["+1"], r["-1"], r["-2"],
               r["道歉"], r["陪伴"]))

    print()
    print("=" * 92)
    print("C. 陪伴 vs 衰减：每天聊 N 句、连续 365 天，好感会走到哪")
    print("=" * 92)
    print("%-10s %s" % ("每天句数", "第1天 / 1周 / 1月 / 3月 / 半年 / 1年"))
    for n, curve in daily.items():
        print("%-10s %s" % (n, " / ".join("%.2f" % v for _, v in curve)))
    print("（起点 0.00；没有别的事件，只有「陪伴」痕迹）")

    print()
    print("=" * 92)
    print("D. 讨厌的人道歉破冰（a0=-0.60，第 1/2/4 天各一次道歉）")
    print("=" * 92)
    print(" → ".join("第%d天 %.2f" % (d, v) for d, v in brk))

    print()
    print("=" * 92)
    print("E. 抵消系数：每天 1 句陪伴，要把好感稳在目标值，单句需要多重")
    print("=" * 92)
    for a, w in be.items():
        print("  稳住 %.1f → 单句权重 ≥ %.4f（默认 %.4f，≈ 每天 %.1f 句）"
              % (a, w, COMP_W, w / COMP_W))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
