#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日体检：把「她此刻的数值」和「功能预期」摆在一起，一眼看出哪里不对。

为什么要有（2026-11-26 用户要求）：
  改了模型之后，光看日志只能确认「代码在跑」，看不出「数值是不是我们想要的样子」。
  这个脚本每天打一份：心情 / 好感 / 判读链路 / 今日事件分布 / 成本，
  最后用一张「期望 vs 实测」表把**设计意图**和**线上数字**对起来。

用法（容器内，只读，不改任何状态）：
    python scripts/daily_report.py
    python scripts/daily_report.py --top 12 --days 1
    python scripts/daily_report.py --json
    python scripts/daily_report.py --no-save          # 只打印，不落盘

每天自动跑一次（二选一）：
    ① 容器 crontab： 0 9 * * * cd /app && python scripts/daily_report.py >> data/reports/daily.log 2>&1
    ② 宿主机 crontab：0 9 * * * docker compose exec -T bot python scripts/daily_report.py
       >> /home/admin/qq-grok-bot/data/reports/daily.log 2>&1

退出码：0 = 全部通过；1 = 有 ⚠️（方便挂到 watchdog 里）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------- 小工具
def _f(x, nd=3):
    try:
        return round(float(x), nd)
    except Exception:
        return 0.0


def _pct(a, b):
    return 0.0 if not b else 100.0 * float(a) / float(b)


TIERS = (
    (0.95, "唯一的爱"), (0.85, "爱"), (0.75, "暧昧"), (0.60, "很好/喜欢"),
    (0.45, "朋友"), (0.30, "熟人"), (0.15, "眼熟"), (-0.15, "中性"),
    (-0.30, "不爽"), (-0.45, "讨厌"), (-0.60, "厌恶"), (-0.75, "反感/记恨"),
    (-0.90, "仇视"), (-2.0, "决裂"),
)


def tier_of(a: float) -> str:
    """好感刻度（docs/affinity-quantification-design.md §2.13）。"""
    for lo, name in TIERS:
        if a >= lo:
            return name
    return "决裂"


def _age(ts, now=None) -> str:
    try:
        ts = float(ts or 0)
    except Exception:
        return "-"
    if ts <= 0:
        return "-"
    d = max(0.0, float(now or time.time()) - ts)
    if d < 60:
        return "%ds" % int(d)
    if d < 3600:
        return "%dm" % int(d / 60)
    if d < 86400:
        return "%.1fh" % (d / 3600.0)
    return "%.1fd" % (d / 86400.0)


# ---------------------------------------------------------------- A 运行档位
def sec_runtime() -> dict:
    from core import tuning
    out = {}
    try:
        from services import config as cfg
        out["POWER_MODE"] = cfg.POWER_MODE
        out["GROUP_WATCH_MODE"] = cfg.GROUP_WATCH_MODE
        out["BOT_MODE"] = cfg.BOT_MODE
        out["NODE_COMPUTE"] = cfg.NODE_COMPUTE
        out["CODE_AGENT"] = cfg.CODE_AGENT
        out["stages"] = {s: bool(cfg.power_on(s))
                         for s in ("audit", "library", "proactive", "expression")}
    except Exception as e:
        out["error"] = "%s: %s" % (type(e).__name__, e)
    for k in ("AFF_MODEL", "AFF_V1_CAP", "AFF_V1_DELTA_K",
              "AFF_V1_DV_K", "AFFINITY_DAILY_CAP", "MOOD_ANCHOR_WEIGHT",
              "MOOD_ANCHOR_GAIN", "MOOD_IMPACT_MAX", "MOOD_STEP_3"):
        try:
            out[k] = tuning.raw(k)
        except Exception:
            out[k] = "?"
    # MOOD_UPDATE 不是 tuning 键（在 services/mood.py 里读 env）
    try:
        from services import mood as _m
        out["MOOD_UPDATE"] = getattr(_m, "MOOD_UPDATE", "?")
        out["MOOD_SCOPE"] = getattr(_m, "MOOD_SCOPE", "?")
    except Exception:
        out["MOOD_UPDATE"] = "?"
    return out


def sec_modes() -> dict:
    """档位自检：把 POWER_MODE 临时切到 full/eco 各看一遍 4 个段。

    只改内存里的模块全局（改完立刻还原），不动 .env、不需要重启。
    """
    try:
        from services import config as cfg
        cur = cfg.POWER_MODE
        out = {}
        try:
            for m in ("full", "eco"):
                cfg.POWER_MODE = m
                out[m] = {s: bool(cfg.power_on(s))
                          for s in ("audit", "library", "proactive", "expression")}
        finally:
            cfg.POWER_MODE = cur
        return {"current": cur, "matrix": out}
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, e)}


# ---------------------------------------------------------------- B 心情
def sec_mood(top: int = 8, now: float = 0) -> dict:
    from services import mood
    now = now or time.time()
    st = mood._load()
    rows = []
    # label 有两种来源：她自己写的原话（judge），或分档词（label_for）。
    # 只有「分档词」才该和数值一致；原话不一致正常。另外分档词有 TTL，
    # 过期的 label 在 get_mood 里会被清掉 —— 这里要按同样的规则看，否则误报。
    try:
        _ttl = float(getattr(mood, "MOOD_LABEL_TTL_H", 12)) * 3600
    except Exception:
        _ttl = 12 * 3600
    _order = {w: i for i, w in enumerate(w for _, w in mood.MOOD_LABELS)}

    def _label_state(rec, v):
        lab = str(rec.get("label") or "").strip()
        try:
            fresh = (now - float(rec.get("label_at") or 0)) <= _ttl
        except Exception:
            fresh = False
        if not fresh:
            lab = ""
        show = lab or mood.label_for(v)
        if lab in _order:
            # 分档词：衰减可能刚好跨过一档边界（0.2 一档），差 1 档算正常
            ok = abs(_order[lab] - _order.get(mood.label_for(v), 0)) <= 1
        else:
            ok = True
        return show, ok

    for key, rec in (st.get("sessions") or {}).items():
        v = _f(float(rec.get("value") or 0.0) / 100.0)
        _show, _ok = _label_state(rec, v)
        rows.append({
            "session": key, "v": v,
            "label": _show, "label_ok": _ok,
            # 老记录没有 P 字段 → 用 _p_of 兜底（P_FLOOR），别显示成 0
            "P": _f(mood._p_of(rec)), "judge_n": int(rec.get("judge_n") or 0),
            "anchor_n": int(rec.get("anchor_n") or 0),
            "base_v": _f(rec.get("base_v")),
            "age": _age(rec.get("updated_at"), now),
            "_abs": abs(v),
        })
    rows.sort(key=lambda r: (-r["_abs"], -r["judge_n"]))
    g = st.get("global") or {}
    gv = _f(float(g.get("value") or 0.0) / 100.0)
    return {
        "global": {"v": gv, "label": mood.label_for(gv),
                   "P": _f(mood._p_of(g)), "judge_n": int(g.get("judge_n") or 0),
                   "age": _age(g.get("updated_at"), now)},
        "sessions": rows[:top],
        "n_sessions": len(rows),
        "_all": rows,
    }


# ---------------------------------------------------------------- C 好感
def sec_affinity(top: int = 8, now: float = 0) -> dict:
    from services import profiles, relationship
    now = now or time.time()
    rows = []
    for p in sorted(Path(profiles.PROFILE_DIR).glob("*_profile.json")):
        qq = p.name.split("_")[0]
        if not qq.isdigit():
            continue
        try:
            prof = profiles.get_profile(qq) or {}
            a = _f(profiles.get_affinity(qq))
            rel = relationship.state_of(qq)
            tr = profiles.affinity_trend(qq, 86400)
            rows.append({
                "qq": qq,
                "name": prof.get("name") or prof.get("call_name") or "",
                "aff": a, "tier": tier_of(a),
                "intimacy": _f(rel.get("intimacy")),
                "trust": _f(rel.get("trust")),
                "respect": _f(rel.get("respect")),
                "day": str(prof.get("affinity_day") or ""),
                "day_sum": _f(prof.get("affinity_day_sum"), 4),
                "d24_drop": _f(tr.get("drop")), "d24_spike": _f(tr.get("spike")),
                "bond_word": prof.get("bond_word") or "",
                "peaks": prof.get("aff_peaks"), "repairs": prof.get("aff_repairs"),
                "warm_days": prof.get("aff_warm_days"),
                "age": _age(prof.get("affinity_at"), now),
                "_abs": abs(a),
            })
        except Exception:
            continue
    rows.sort(key=lambda r: -r["_abs"])
    return {"persons": rows[:top], "n_persons": len(rows), "_all": rows}


# ---------------------------------------------------------------- D 判读链路
def sec_judge(trace_n: int = 40) -> dict:
    """判读链路健康。

    `trace_n` 默认只取**最近 40 条**：trace 文件保留 400 条，里面混着
    「还没加 desirability 字段」的旧版本行，拿 200 条算命中率会得出
    「desirability 缺 88%」这种吓人但没意义的数（2026-11-26 踩过）。
    """
    out = {}
    try:
        from services import mood_judge as mj
        h, m = mj.THINK_STAT["hit"], mj.THINK_STAT["miss"]
        out["think"] = {"hit": h, "miss": m,
                        "rate": _f(_pct(h, h + m) / 100.0, 4)}
        ah, am = mj.ANCHOR_STAT["hit"], mj.ANCHOR_STAT["miss"]
        out["anchor"] = {"hit": ah, "miss": am,
                         "rate": _f(_pct(ah, ah + am) / 100.0, 4)}
        out["trace_enabled"] = bool(mj.JUDGE_TRACE)
        out["note"] = "进程内统计，重启归零；anchor 缺失率以 trace 为准"
    except Exception as e:
        out["error"] = "%s: %s" % (type(e).__name__, e)

    # trace 落盘：看最近 N 条里 anchor_level 缺了几条（这才是跨重启的真值）
    try:
        from services import mood_judge as mj
        tf = Path(mj.TRACE_FILE)
        if tf.exists():
            lines = tf.read_text(encoding="utf-8").splitlines()[-trace_n:]
            miss = ok = 0
            des_miss = 0
            for ln in lines:
                try:
                    raw = json.loads(json.loads(ln).get("raw") or "{}")
                except Exception:
                    continue
                if not isinstance(raw, dict):
                    continue
                if raw.get("anchor_level") is None:
                    miss += 1
                else:
                    ok += 1
                if raw.get("desirability") is None:
                    des_miss += 1
            out["trace"] = {"n": ok + miss, "anchor_miss": miss,
                            "anchor_rate": _f(_pct(ok, ok + miss) / 100.0, 4),
                            "desirability_miss": des_miss}
        else:
            out["trace"] = {"n": 0, "note": "没有 %s" % tf}
    except Exception as e:
        out["trace"] = {"error": "%s: %s" % (type(e).__name__, e)}
    return out


# ---------------------------------------------------------------- E 今日事件
def sec_events(days: float = 1.0, now: float = 0) -> dict:
    now = now or time.time()
    t0 = now - float(days) * 86400
    try:
        from services import memdb
        c = memdb._conn()
        rows = c.execute(
            "SELECT dv, impact, target, confidence FROM mood_events WHERE ts>=?",
            (t0,)).fetchall()
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, e)}
    dvs = [float(r[0] or 0.0) for r in rows]
    impacts = {}
    targets = {}
    for r in rows:
        impacts[int(r[1] or 0)] = impacts.get(int(r[1] or 0), 0) + 1
        targets[str(r[2] or "-")] = targets.get(str(r[2] or "-"), 0) + 1
    tail = sum(1 for d in dvs if abs(d) >= 0.30)
    jump = sum(1 for d in dvs if 0.15 <= abs(d) < 0.30)
    daily = sum(1 for d in dvs if 0 < abs(d) < 0.15)
    return {
        "n": len(rows),
        "abs_dv_mean": _f(sum(abs(d) for d in dvs) / len(dvs) if dvs else 0.0, 4),
        "abs_dv_max": _f(max((abs(d) for d in dvs), default=0.0), 4),
        "seg": {"日常(<0.15)": daily, "显著(0.15~0.30)": jump, "尾部(>=0.30)": tail},
        "impact": {str(k): impacts[k] for k in sorted(impacts)},
        "target": targets,
    }


# ---------------------------------------------------------------- G 成本
def sec_cost(days: int = 1) -> str:
    try:
        from services import llm_usage
        return llm_usage.report_text(days)
    except Exception as e:
        return "读取失败：%s: %s" % (type(e).__name__, e)


# ---------------------------------------------------------------- F 期望校验
def checks(rt: dict, mood_s: dict, aff_s: dict, judge_s: dict,
           ev_s: dict) -> list:
    """每一条都对应一个「设计意图」。⚠️ 不代表程序崩了，代表**和设计不一致**。"""
    from core import tuning
    out = []

    def add(name, ok, detail=""):
        out.append({"name": name, "ok": bool(ok), "detail": str(detail)})

    # --- 全量 / 节能 ---
    pm = str(rt.get("POWER_MODE") or "?")
    stages = rt.get("stages") or {}
    if pm == "eco":
        off = [s for s, on in stages.items() if on]
        add("节能档只关 4 个花钱段（audit/library/proactive/expression）",
            not off, "仍开着：" + (",".join(off) or "无"))
    elif pm == "full":
        on = [s for s, on in stages.items() if not on]
        add("全量档 4 个段全开", not on, "被关掉：" + (",".join(on) or "无"))
    else:
        add("POWER_MODE 合法", False, pm)

    # --- 好感上限（ADR-043 的算术）---
    cap = float(tuning.raw("AFF_V1_CAP") or 0)
    daily = float(tuning.raw("AFFINITY_DAILY_CAP") or 0)
    dvk = float(tuning.raw("AFF_V1_DV_K") or 0)
    wmax = float(tuning.raw("AFF_W_MAX") or 0)
    reach = dvk * wmax + 0.02
    add("AFF_V1_CAP ≤ 日上限", cap <= daily + 1e-9, "CAP=%.3f / 日=%.3f" % (cap, daily))
    add("AFF_V1_CAP 不是死护栏（≤ 可达上限×1.3）",
        cap <= reach * 1.3, "CAP=%.3f / 可达≈%.3f" % (cap, reach))

    # --- 数值域 ---
    amax = float(tuning.raw("AFF_MAX") or 0.99)
    bad = [p["qq"] for p in aff_s.get("_all") or [] if abs(p["aff"]) > amax + 1e-6]
    add("好感都在 ±%.2f 内" % amax, not bad, "越界：" + ",".join(bad[:5]))

    _today = time.strftime("%Y-%m-%d")
    over = [p["qq"] for p in aff_s.get("_all") or []
            if p.get("day") == _today and abs(p["day_sum"]) > daily + 1e-4]
    add("今日好感净变化都 ≤ %.2f（日上限没被绕过）" % daily, not over,
        "超了：" + (",".join(over[:5]) or "无"))

    bad_v = [s["session"] for s in mood_s.get("_all") or [] if abs(s["v"]) > 1.0 + 1e-6]
    add("心情 v 都在 [-1,1]", not bad_v, "越界：" + ",".join(bad_v[:5]))

    bad_lab = [s["session"] for s in mood_s.get("_all") or [] if not s["label_ok"]]
    add("心情分档词与数值一致（她自己写的原话不参与）", not bad_lab,
        "不一致：" + (",".join(bad_lab[:5]) or "无"))

    # --- 判读链路 ---
    tr = judge_s.get("trace") or {}
    n = int(tr.get("n") or 0)
    if n >= 20:
        add("anchor_level 命中率 ≥ 90%（必填字段真的填了）",
            float(tr.get("anchor_rate") or 0) >= 0.90,
            "%d/%d 命中" % (n - int(tr.get("anchor_miss") or 0), n))
        add("desirability 命中率 ≥ 95%",
            float(tr.get("desirability_miss") or 0) <= n * 0.05,
            "缺 %s 条 / 共 %d" % (tr.get("desirability_miss"), n))
    else:
        add("判读 trace 样本够（≥20 条）", False,
            "只有 %d 条，先跑一会儿再看" % n)

    # --- 今日事件分布 ---
    if not ev_s.get("error"):
        seg = ev_s.get("seg") or {}
        add("今日有判读事件（链路没断）", int(ev_s.get("n") or 0) > 0,
            "n=%s" % ev_s.get("n"))
        add("事件位移量级合理（|dv| 均值 0.01~0.20）",
            0.01 <= float(ev_s.get("abs_dv_mean") or 0) <= 0.20,
            "均值 %.4f" % float(ev_s.get("abs_dv_mean") or 0))
        tail = int(seg.get("尾部(>=0.30)") or 0)
        add("突变通道有记录（尾部次数，0 也正常但要知道）", True,
            "尾部 %d 次 / 显著 %d 次 / 日常 %d 次"
            % (tail, int(seg.get("显著(0.15~0.30)") or 0),
               int(seg.get("日常(<0.15)") or 0)))

    # --- 数据文件健康 ---
    try:
        from services import mood as _m
        corrupt = list(Path(_m.MOOD_FILE).parent.glob("*.corrupt-*.json"))
        add("mood.json 没有损坏备份（最近）", not corrupt,
            "发现 %d 个 .corrupt-*" % len(corrupt))
    except Exception:
        pass
    return out


# ---------------------------------------------------------------- 渲染
def render(rt, mood_s, aff_s, judge_s, ev_s, chk, cost_text) -> str:
    L = []
    a = L.append
    a("=" * 72)
    a("她的一天（%s）" % time.strftime("%Y-%m-%d %H:%M:%S"))
    a("=" * 72)

    a("")
    a("【A 运行档位】")
    a("  成本档 POWER_MODE = %s   观察档 GROUP_WATCH_MODE = %s   BOT_MODE = %s"
      % (rt.get("POWER_MODE"), rt.get("GROUP_WATCH_MODE"), rt.get("BOT_MODE")))
    st = rt.get("stages") or {}
    a("  段：audit=%s library=%s proactive=%s expression=%s"
      % tuple("on" if st.get(k) else "off"
              for k in ("audit", "library", "proactive", "expression")))
    md = rt.get("modes") or {}
    if md.get("matrix"):
        for m in ("full", "eco"):
            row = md["matrix"].get(m) or {}
            a("  档位自检 %-4s：%s" % (m, "  ".join(
                "%s=%s" % (k, "on" if row.get(k) else "off") for k in
                ("audit", "library", "proactive", "expression"))))
    a("  模型：AFF_MODEL=%s MOOD_UPDATE=%s  |  v1: ΔK=%s DV_K=%s CAP=%s 日上限=%s"
      % (rt.get("AFF_MODEL"), rt.get("MOOD_UPDATE"), rt.get("AFF_V1_DELTA_K"),
         rt.get("AFF_V1_DV_K"), rt.get("AFF_V1_CAP"), rt.get("AFFINITY_DAILY_CAP")))
    a("  锚点：W=%s（每窗）GAIN=%s（独立）  档位上限 ±%s  步长 ±3=%s"
      % (rt.get("MOOD_ANCHOR_WEIGHT"), rt.get("MOOD_ANCHOR_GAIN"),
         rt.get("MOOD_IMPACT_MAX"), rt.get("MOOD_STEP_3")))

    a("")
    a("【B 心情】全局 v=%+.3f（%s）P=%.3f  判读 %d 次  %s 前"
      % (mood_s["global"]["v"], mood_s["global"]["label"], mood_s["global"]["P"],
         mood_s["global"]["judge_n"], mood_s["global"]["age"]))
    a("  %-26s %7s %-10s %6s %6s %6s %6s" %
      ("会话", "v", "标签", "P", "判读", "锚点", "更新"))
    for s in mood_s["sessions"]:
        a("  %-26s %+7.3f %-10s %6.3f %6d %6d %6s"
          % (s["session"][:26], s["v"], s["label"], s["P"],
             s["judge_n"], s["anchor_n"], s["age"]))
    a("  （共 %d 个会话，按 |v| 取前 %d）" % (mood_s["n_sessions"], len(mood_s["sessions"])))

    a("")
    a("【C 好感】共 %d 人" % aff_s["n_persons"])
    a("  %-10s %-8s %7s %-10s %6s %6s %6s %7s %s"
      % ("QQ", "名字", "a", "刻度", "亲近", "信任", "尊重", "今日净", "她怎么看他"))
    for p in aff_s["persons"]:
        a("  %-10s %-8s %+7.3f %-10s %6.2f %6.2f %6.2f %+7.3f %s"
          % (p["qq"], (p["name"] or "-")[:8], p["aff"], p["tier"],
             p["intimacy"], p["trust"], p["respect"], p["day_sum"],
             (p["bond_word"] or "-")[:18]))
    a("  （按 |a| 取前 %d；24h 跌/涨取绝对值最大的那条事件）" % len(aff_s["persons"]))
    for p in aff_s["persons"][:3]:
        a("   · %s 24h：跌 %.3f / 最大单次跌 %.3f   peaks=%s repairs=%s 暖天=%s"
          % (p["name"] or p["qq"], p["d24_drop"], p["d24_spike"],
             p["peaks"], p["repairs"], p["warm_days"]))

    a("")
    a("【D 判读链路】")
    th = judge_s.get("think") or {}
    an = judge_s.get("anchor") or {}
    a("  本次启动后：THINK 命中 %s/%s   anchor_level 命中 %s/%s"
      % (th.get("hit"), int(th.get("hit") or 0) + int(th.get("miss") or 0),
         an.get("hit"), int(an.get("hit") or 0) + int(an.get("miss") or 0)))
    tr = judge_s.get("trace") or {}
    if tr.get("error"):
        a("  trace 读取失败：%s" % tr["error"])
    elif int(tr.get("n") or 0):
        a("  最近 %d 条判读：anchor_level 缺 %s 条（命中率 %.0f%%）desirability 缺 %s 条"
          % (tr["n"], tr.get("anchor_miss"),
             float(tr.get("anchor_rate") or 0) * 100,
             tr.get("desirability_miss")))
    else:
        a("  trace：%s" % (tr.get("note") or "无数据"))

    a("")
    a("【E 今日事件】（mood_events）")
    if ev_s.get("error"):
        a("  读取失败：%s" % ev_s["error"])
    else:
        seg = ev_s.get("seg") or {}
        a("  条数 %s   |dv| 均值 %.4f  最大 %.4f"
          % (ev_s.get("n"), ev_s.get("abs_dv_mean"), ev_s.get("abs_dv_max")))
        a("  分布：日常 %s / 显著 %s / 尾部 %s"
          % (seg.get("日常(<0.15)"), seg.get("显著(0.15~0.30)"), seg.get("尾部(>=0.30)")))
        _imp = ev_s.get("impact") or {}
        a("  impact：%s" % json.dumps(_imp, ensure_ascii=False))
        if _imp and set(_imp) <= {"0"}:
            a("    ⚠️ 全是 0：这一版代码写 mood_events 时**没带档位**（日志缺口），"
              "不代表「她没情绪」")
        a("  target：%s" % json.dumps(ev_s.get("target") or {}, ensure_ascii=False))

    a("")
    a("【F 期望校验】")
    bad = [c for c in chk if not c["ok"]]
    for c in chk:
        a("  %s %s%s" % ("✅" if c["ok"] else "⚠️", c["name"],
                         ("　（%s）" % c["detail"]) if c["detail"] else ""))
    a("  → %s（%d/%d 通过）" % ("全部符合预期" if not bad else "有 %d 项不符合" % len(bad),
                              len(chk) - len(bad), len(chk)))

    a("")
    a("【G 今日成本】")
    for ln in str(cost_text or "").splitlines()[:24]:
        a("  " + ln)
    a("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=8, help="心情/好感各列前几名")
    ap.add_argument("--days", type=float, default=1.0, help="事件窗口（天）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-save", action="store_true", help="只打印，不写 data/reports")
    args = ap.parse_args()

    now = time.time()
    rt = sec_runtime()
    rt["modes"] = sec_modes()
    mood_s = sec_mood(args.top, now)
    aff_s = sec_affinity(args.top, now)
    judge_s = sec_judge()
    ev_s = sec_events(args.days, now)
    chk = checks(rt, mood_s, aff_s, judge_s, ev_s)
    cost_text = sec_cost(max(1, int(args.days)))

    if args.json:
        payload = {"at": now, "runtime": rt,
                   "mood": {k: v for k, v in mood_s.items() if k != "_all"},
                   "affinity": {k: v for k, v in aff_s.items() if k != "_all"},
                   "judge": judge_s, "events": ev_s, "checks": chk}
        text = json.dumps(payload, ensure_ascii=False, indent=1)
    else:
        text = render(rt, mood_s, aff_s, judge_s, ev_s, chk, cost_text)
    print(text)

    if not args.no_save:
        try:
            d = Path("data/reports/daily")
            d.mkdir(parents=True, exist_ok=True)
            f = d / (time.strftime("%Y-%m-%d") + ".txt")
            f.write_text(text + "\n", encoding="utf-8")
        except Exception as e:
            print("（落盘失败：%s: %s）" % (type(e).__name__, e), file=sys.stderr)

    return 0 if all(c["ok"] for c in chk) else 1


if __name__ == "__main__":
    raise SystemExit(main())
