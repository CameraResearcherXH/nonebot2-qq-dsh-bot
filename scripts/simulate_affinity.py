#!/usr/bin/env python3
"""好感模型 v2 的数值模拟（只算数，不碰线上数据）。

用途：调参 / 评审时先看曲线，别直接改线上。
公式定义见 docs/affinity-quantification-design.md。

用法：
    python3 scripts/simulate_affinity.py            # 默认参数
    python3 scripts/simulate_affinity.py --json     # 给别的脚本用
"""
from __future__ import annotations

import argparse
import json
import math

# ---------------- 参数（与设计文档 §3 一致）----------------
F_MAX = 0.60          # 深度能提供的「bond」上限
F_EXP = 1.5           # bond 随投入 I 的指数（越大越陡）
T_HEAT = 10.0         # 热度分量衰减时间常数（天）
P_HEAT = 0.5          # 幂律指数
T_NEG = 30.0          # 负好感的遗忘时间常数（天）
P_NEG = 0.5
STEP = {1: 0.045, 2: 0.090}   # |impact| → 基础步长
SAT_POS = 0.70        # 正向饱和指数 (1-a)^γ
SAT_NEG = 0.35        # 负向饱和指数 (1-|a|)^γ
NEG_BIAS = 1.7        # 负面事件权重（Baumeister 2001 / Gottman 5:1）
CLAMP = 0.20          # 单次步长硬上限
DAILY_CAP = 0.15      # 每日同向净变化上限

PERSONAS = [
    # 名字, 记录条数, 认识天数, 近7天消息数, 当前好感
    ("新认识", 2, 3, 10, 0.15),
    ("普通群友", 8, 30, 60, 0.30),
    ("好朋友", 20, 90, 150, 0.75),
    ("挚友", 30, 180, 300, 0.90),
    ("讨厌的人", 6, 60, 40, -0.60),
]


def investment(notes: float, days: float, msgs: float) -> float:
    """投入 I ∈ [0,1]：相处记录 / 认识时长 / 近期互动，各占一部分。"""
    return (0.50 * min(1.0, notes / 20.0)
            + 0.30 * min(1.0, days / 120.0)
            + 0.20 * min(1.0, msgs / 150.0))


def bond_of(a: float, inv: float) -> float:
    """不随时间衰减的「底子」：只对正好感存在；投入越深越大。"""
    if a <= 0:
        return 0.0
    beta = F_MAX * (max(0.0, inv) ** F_EXP)
    return min(a, a * beta)


def decay(a: float, inv: float, days: float, grudge: float = 0.0) -> float:
    """t 天没互动后的好感（幂律 + 底子）。"""
    if days <= 0:
        return a
    if a >= 0:
        b = bond_of(a, inv)
        heat = a - b
        return b + heat * (1.0 + days / T_HEAT) ** (-P_HEAT)
    # 负好感：不会自己变正，只是慢慢淡
    tn = T_NEG * (1.0 + 2.0 * max(0.0, grudge))
    return a * (1.0 + days / tn) ** (-P_NEG)


def step(impact: int, a: float, conf: float = 1.0, novelty: float = 1.0,
         grudge: float = 0.0) -> float:
    """一次事件让好感动多少（impact ∈ {-2,-1,0,1,2}）。"""
    k = abs(int(impact))
    if k not in STEP:
        return 0.0
    base = STEP[k]
    if impact > 0:
        # 两头都饱和：高好感难再涨，负好感也难靠一次好话翻正
        sat = (1.0 - abs(a)) ** SAT_POS
        d = base * sat * max(0.0, conf) * max(0.0, novelty) * (1.0 - 0.5 * grudge)
    else:
        sat = (1.0 - abs(a)) ** SAT_NEG
        d = -base * NEG_BIAS * sat * max(0.0, conf) * max(0.0, novelty)
    return max(-CLAMP, min(CLAMP, d))


def current_decay(a: float, intimacy: float, contact: float, days: float) -> float:
    """现网 v1：指数衰减到 0，τ = 7天 × 深度系数。"""
    depth = max(0.1, (1.0 + 2.0 * intimacy) * (1.0 + 0.5 * contact))
    tau = 7.0 * depth
    return a * math.pow(0.5, days / tau)


# 现网各人设的 intimacy / 近期互动归一化（用于对比）
CUR = {"新认识": (0.10, 0.10), "普通群友": (0.30, 0.25),
       "好朋友": (0.98, 1.00), "挚友": (0.98, 1.00), "讨厌的人": (0.30, 0.20)}


def table_compare() -> list:
    out = []
    for name, n, d, m, a0 in PERSONAS:
        inv = investment(n, d, m)
        it, ct = CUR[name]
        row = {"who": name, "a0": a0}
        for label, days in (("1周", 7), ("1月", 30), ("3月", 90)):
            row[label + "v1"] = round(current_decay(a0, it, ct, days) - a0, 3)
            row[label + "v2"] = round(decay(a0, inv, days) - a0, 3)
        out.append(row)
    return out


def table_decay() -> list:
    out = []
    for name, n, d, m, a0 in PERSONAS:
        inv = investment(n, d, m)
        row = {"who": name, "a0": a0, "I": round(inv, 3),
               "bond": round(bond_of(a0, inv), 3)}
        for label, days in (("1周", 7), ("1月", 30), ("3月", 90), ("1年", 365)):
            v = decay(a0, inv, days)
            row[label] = round(v, 3)
            row[label + "Δ"] = round(v - a0, 3)
        out.append(row)
    return out


def table_step() -> list:
    out = []
    for name, n, d, m, a0 in PERSONAS:
        row = {"who": name, "a": a0}
        for k in (2, -2):
            row[("+%d" % k) if k > 0 else ("%d" % k)] = round(step(k, a0), 3)
        out.append(row)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    dec, stp = table_decay(), table_step()
    if args.json:
        print(json.dumps({"decay": dec, "step": stp}, ensure_ascii=False, indent=1))
        return 0

    print("=" * 84)
    print("好感模型 v2：时间衰减（t 天完全没互动后剩多少）")
    print("=" * 84)
    print("%-8s %5s %5s %6s | %-14s %-14s %-14s %-14s"
          % ("人设", "a0", "I", "bond", "1周", "1月", "3月", "1年"))
    for r in dec:
        cells = []
        for k in ("1周", "1月", "3月", "1年"):
            cells.append("%.2f(%+.2f)" % (r[k], r[k + "Δ"]))
        print("%-8s %5.2f %5.2f %6.2f | %-14s %-14s %-14s %-14s"
              % (r["who"], r["a0"], r["I"], r["bond"], *cells))

    print()
    print("=" * 84)
    print("现网 v1 vs v2：一个月/三个月掉多少（Δ）")
    print("=" * 84)
    print("%-8s %5s | %-16s %-16s %-16s"
          % ("人设", "a0", "1周(v1/v2)", "1月(v1/v2)", "3月(v1/v2)"))
    for r in table_compare():
        print("%-8s %5.2f | %-16s %-16s %-16s"
              % (r["who"], r["a0"],
                 "%+.2f / %+.2f" % (r["1周v1"], r["1周v2"]),
                 "%+.2f / %+.2f" % (r["1月v1"], r["1月v2"]),
                 "%+.2f / %+.2f" % (r["3月v1"], r["3月v2"])))

    print()
    print("=" * 84)
    print("好感模型 v2：单次事件步长（impact=±2，conf=1，novelty=1）")
    print("=" * 84)
    print("%-8s %6s %12s %12s" % ("人设", "a", "被夸 +2", "被伤 -2"))
    for r in stp:
        print("%-8s %6.2f %12.3f %12.3f" % (r["who"], r["a"], r["+2"], r["-2"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
