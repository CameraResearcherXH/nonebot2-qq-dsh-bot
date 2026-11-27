"""关系四量（DESIGN §3.2）：affinity / trust / respect / intimacy。

分工
----
- `affinity` 好恶（-1~+1）：**仍然住在 profiles**（`<qq>_profile.json`），
  本模块只负责「按深度系数衰减」和「负向加权」，不另存一份。
- `trust` 信任（0~1，基线 0.20，半衰期 45 天）：只由「可信/失信」事件改。
- `respect` 尊重（0~1，基线 0.30，半衰期 90 天）：只由「能力/地位」事件改。
- `intimacy` 亲近（0~1）：**派生量**，由 `profiles.intimacy()` 从 bond notes 算，
  本模块不写它（避免两个状态变量同时累加）。

为什么要单独一个模块
--------------------
2026-11-25 审计：`trust` / `respect` 之前只有参数、没有写入口，也没有 audit；
关系系统实际上只有 affinity 一个量在动。这里补齐写入口 + 衰减 + 审计流水。

审计
----
`data/relationship_log.jsonl`：每行 {at, qq, kind, delta, to, why, relevance}。
"""

from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path

from core import tuning

REL_FILE = Path(os.getenv("REL_FILE", "data/relationship.json"))
REL_LOG = Path("data/relationship_log.jsonl")
REL_FILE.parent.mkdir(parents=True, exist_ok=True)

TRUST_BASE = tuning.f("REL_TRUST_BASE")
RESPECT_BASE = tuning.f("REL_RESPECT_BASE")
AFF_BASE = tuning.f("REL_AFFINITY_BASE")
INTIMACY_BASE = tuning.f("REL_INTIMACY_BASE")
TRUST_HL_D = tuning.f("REL_TRUST_HALF_LIFE_DAYS")
RESPECT_HL_D = tuning.f("REL_RESPECT_HALF_LIFE_DAYS")
AFF_HL_D = tuning.f("REL_AFFINITY_HALF_LIFE_DAYS")
# 亲近（intimacy）是派生量，半衰期在 profiles.intimacy() 里生效（2026-11-25 移到那边）。
DEPTH_INTIMACY = tuning.f("REL_DEPTH_INTIMACY_COEF")
DEPTH_CONTACT = tuning.f("REL_DEPTH_CONTACT_COEF")
CONTACT_NORM_N = tuning.f("REL_CONTACT_NORM_N")
NOVELTY_COEF = tuning.f("REL_NOVELTY_COEF")
DAILY_CAP = tuning.f("REL_DAILY_CAP")
NEG_BASE = tuning.f("REL_NEG_BASE")
NEG_SLOPE = tuning.f("REL_NEG_SLOPE")
TRUST_UP = tuning.f("REL_TRUST_UP")
TRUST_DOWN = tuning.f("REL_TRUST_DOWN")
RESPECT_UP = tuning.f("REL_RESPECT_UP")
RESPECT_DOWN = tuning.f("REL_RESPECT_DOWN")
GRUDGE_HL_D = tuning.f("REL_GRUDGE_HALF_LIFE_DAYS")


def _load() -> dict:
    if REL_FILE.exists():
        try:
            raw = json.loads(REL_FILE.read_text(encoding="utf-8")) or {}
            if isinstance(raw, dict):
                return raw
        except Exception:
            pass
    return {}


def _save(st: dict) -> None:
    try:
        tmp = REL_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, REL_FILE)
    except Exception:
        pass


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(v)))


def _new(qq: str, now: float = 0) -> dict:
    return {"trust": TRUST_BASE, "respect": RESPECT_BASE,
            "grudge": 0.0,
            "at": now or time.time(), "day": time.strftime("%Y-%m-%d"),
            "day_gain": 0.0}


def _decay_rec(rec: dict, now: float) -> dict:
    """trust / respect 各自按半衰期向基线回落（affinity 在 profiles 里单独衰减）。"""
    try:
        at = float(rec.get("at") or now)
    except Exception:
        at = now
    days = max(0.0, (now - at) / 86400.0)
    if days <= 0:
        return rec
    for key, base, hl in (("trust", TRUST_BASE, TRUST_HL_D),
                          ("respect", RESPECT_BASE, RESPECT_HL_D)):
        try:
            cur = float(rec.get(key, base))
        except Exception:
            cur = base
        # 2026-11-25（P3-5）：trust/respect 一律走自己的半衰期（45 / 90 天）。
        # 「伤害掉得快」交给独立的 grudge 变量表达（emotion §3.6），
        # 不再拿 14 天替换 trust 的半衰期 —— 那样一次失信两周就回到基线。
        rec[key] = round(base + (cur - base) * math.pow(0.5, days / max(0.01, hl)), 4)
    # 记仇（0~1）：半衰期 14 天，只影响「信任恢复速度 / 提不提旧事 / 语气」
    try:
        _g = float(rec.get("grudge") or 0.0)
        rec["grudge"] = round(max(0.0, _g * math.pow(0.5, days / max(0.01, GRUDGE_HL_D))), 4)
    except Exception:
        rec["grudge"] = 0.0
    rec["at"] = now
    return rec


def _depth(qq: str) -> float:
    """深度系数：越亲近、最近互动越多，好感掉得越慢（投资模型）。"""
    _pf = None
    try:
        from . import profiles as _pf
        _intim = float(_pf.intimacy(qq))
    except Exception:
        _intim = 0.0
    try:
        notes = (_pf.get_bond(qq).get("notes") or []) if _pf else []
        now = time.time()
        n7 = sum(1 for n in notes
                 if now - float(n.get("at") or 0) <= 7 * 86400)
    except Exception:
        n7 = 0
    contact = min(1.0, n7 / max(1.0, CONTACT_NORM_N))
    return max(0.1, (1.0 + DEPTH_INTIMACY * _intim) * (1.0 + DEPTH_CONTACT * contact))


def affinity_of(qq: str) -> float:
    """读好感（顺手按 τ_eff 衰减）。τ_eff = 7d × 深度系数。"""
    qq = str(qq or "").strip()
    if not qq.isdigit():
        return 0.0
    try:
        from . import profiles
        tau = AFF_HL_D * _depth(qq)
        return float(profiles.decay_affinity(qq, tau) or AFF_BASE)
    except Exception:
        try:
            from . import profiles
            return float(profiles.get_affinity(qq))
        except Exception:
            return 0.0


def _neg_weight(affinity: float) -> float:
    """负向事件权重：越喜欢越宽容（1.3 − 0.6×affinity，夹在 0.5~2.0）。"""
    return _clamp(NEG_BASE - NEG_SLOPE * float(affinity), 0.5, 2.0)


def neg_weight(affinity: float) -> float:
    """公开别名（mood_judge 写好感时用）。"""
    return _neg_weight(affinity)


def bump(qq: str, kind: str, value: float = 1.0, *,
         reason: str = "", relevance: float = 1.0,
         novelty: float = 1.0) -> float:
    """改 trust / respect。`value` 只取方向（±1），步长查表。

    返回新值；kind 不合法或 qq 不是数字时返回当前值。
    """
    qq = str(qq or "").strip()
    if not qq.isdigit() or kind not in ("trust", "respect"):
        return 0.0
    try:
        sign = 1 if float(value) > 0 else (-1 if float(value) < 0 else 0)
    except Exception:
        sign = 0
    if not sign:
        return state_of(qq)[kind]
    rel = _clamp(relevance, 0.0, 1.0)
    nov = _clamp(novelty, 0.0, 1.0)
    step = (TRUST_UP if kind == "trust" else RESPECT_UP) if sign > 0 else \
           (TRUST_DOWN if kind == "trust" else RESPECT_DOWN)
    d = sign * step * rel * (0.5 + 0.5 * nov) * (NOVELTY_COEF if sign > 0 else 1.0)
    # 人设底色（ADR-010）：信任涨得快慢 / 记仇深浅
    try:
        from . import persona_traits as _pt
        _tr = _pt.traits_for_session("")
        if kind == "trust":
            d *= ((0.5 + float(_tr.get("trust_speed") or 0.5)) if sign > 0
                  else (1.2 - 0.4 * float(_tr.get("forgiveness") or 0.5)))
    except Exception:
        pass
    # 2026-11-26（审计 E6）：**不要**把 affinity 的宽容度乘到 trust/respect 上。
    # 四个量单位不同、各改各的：好感可以「越喜欢越宽容」，
    # 但信任被骗就是被骗，不该因为她喜欢这个人就罚得轻。
    # 宽容只作用于 affinity（v1 在 mood_judge 里单独乘 neg_weight）。
    now = time.time()
    st = _load()
    rec = _decay_rec(st.get(qq) or _new(qq, now), now)
    # 记仇会拖慢信任恢复（§3.6：grudge 降低信任恢复速度），最多打三折
    if sign > 0 and kind == "trust":
        d *= max(0.3, 1.0 - 0.5 * float(rec.get("grudge") or 0.0))
    day = time.strftime("%Y-%m-%d")
    if rec.get("day") != day:
        rec["day"] = day
        rec["day_gain"] = 0.0
    # 每日净涨上限（负向不受限：伤害必须记得住）
    if sign > 0:
        room = DAILY_CAP - abs(float(rec.get("day_gain") or 0.0))
        if room <= 0:
            return round(float(rec.get(kind)), 4)
        d = min(d, room)
        rec["day_gain"] = round(float(rec.get("day_gain") or 0.0) + d, 4)
    lo, hi = (0.0, 1.0)
    rec[kind] = round(_clamp(float(rec.get(kind, 0.0)) + d, lo, hi), 4)
    # 记仇：负面加深（0.30×(1-grudge)×severity），正面慢慢消（-0.10×rel）
    try:
        _g = float(rec.get("grudge") or 0.0)
        if sign < 0:
            _base_step = TRUST_DOWN if kind == "trust" else RESPECT_DOWN
            _sev = min(1.0, abs(d) / max(1e-6, _base_step))
            rec["grudge"] = round(min(1.0, _g + 0.30 * (1.0 - _g) * rel * _sev), 4)
        else:
            rec["grudge"] = round(max(0.0, _g - 0.10 * rel), 4)
    except Exception:
        pass
    rec["at"] = now
    st[qq] = rec
    _save(st)
    _log(qq, kind, d, rec[kind], reason, rel)
    # 档案镜像：trust/respect 也同步 sqlite（以前只同步 affinity，两边会漂）
    try:
        from . import memdb as _mdb
        _mdb.upsert_profile(qq=qq, trust=float(rec.get("trust") or TRUST_BASE),
                            respect=float(rec.get("respect") or RESPECT_BASE))
    except Exception:
        pass
    return rec[kind]


def _log(qq: str, kind: str, delta: float, to: float,
         why: str = "", relevance: float = 0.0) -> None:
    try:
        with open(REL_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": time.time(), "qq": qq, "kind": kind,
                                "delta": round(delta, 4), "to": round(to, 4),
                                "why": str(why or "")[:60],
                                "relevance": round(relevance, 3)},
                               ensure_ascii=False) + "\n")
    except Exception:
        pass


def note_judge(qq: str, res: dict, *, delta: float = 0.0,
               relevance: float = 0.5) -> str:
    """判读结果里的关系事件 → trust / respect。返回一行摘要（空串=没动）。"""
    if not str(qq or "").strip().isdigit():
        return ""
    rel = _clamp(relevance, 0.0, 1.0)
    if rel <= 0:
        return ""
    out = []
    for kind in ("trust", "respect"):
        v = int(res.get(f"relation_{kind}") or 0)
        if not v:
            continue
        try:
            to = bump(qq, kind, v, reason=res.get("why") or "",
                      relevance=rel,
                      novelty=min(1.0, abs(float(delta)) / 0.22 + 0.2))
            out.append(f"{kind}{v:+d}→{to:.2f}")
        except Exception:
            continue
    return " ".join(out)


def state_of(qq: str) -> dict:
    qq = str(qq or "").strip()
    now = time.time()
    if not qq.isdigit():
        return {"affinity": 0.0, "trust": TRUST_BASE,
                "respect": RESPECT_BASE, "intimacy": 0.0}
    st = _load()
    rec = _decay_rec(st.get(qq) or _new(qq, now), now)
    try:
        from . import profiles
        intim = float(profiles.intimacy(qq))
    except Exception:
        intim = INTIMACY_BASE
    return {"affinity": round(affinity_of(qq), 3),
            "trust": round(float(rec.get("trust", TRUST_BASE)), 3),
            "respect": round(float(rec.get("respect", RESPECT_BASE)), 3),
            "intimacy": round(intim, 3)}


def render_line(qq: str) -> str:
    """给状态卡的一行：只在非基线时出现，避免每轮都占字数。"""
    s = state_of(qq)
    bits = []
    # 好感不在这一行：状态卡里有一条专门的「你对X的好感：+0.02（最近在回暖）」，
    # 这里再写一遍会出现两个数字（旧版一个是衰减后、一个是原始值），2026-11-25 去掉。
    if abs(s["trust"] - TRUST_BASE) >= 0.03:
        bits.append(f"信任{s['trust']:.2f}")
    if abs(s["respect"] - RESPECT_BASE) >= 0.03:
        bits.append(f"尊重{s['respect']:.2f}")
    if s["intimacy"] >= 0.2:
        bits.append(f"亲近{s['intimacy']:.2f}")
    return "｜".join(bits)


def audit_text(qq: str = "", n: int = 10) -> str:
    """最近的关系事件（给 /测函数 关系 用）。"""
    rows = []
    try:
        with open(REL_LOG, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if qq and str(d.get("qq")) != str(qq):
                    continue
                rows.append(d)
    except Exception:
        pass
    rows = rows[-max(1, n):]
    if not rows:
        return "（还没有关系事件）"
    return "\n".join(
        f"{time.strftime('%m-%d %H:%M', time.localtime(float(r.get('at') or 0)))} "
        f"{r.get('kind')} {float(r.get('delta') or 0):+.3f} → {float(r.get('to') or 0):.2f}"
        f"｜{r.get('why') or ''}" for r in rows)
