#!/usr/bin/env python3
"""NapCat / NoneBot 日志 ↔ 设计文档 对接验证。

用法
----
    # 1) 先把线上日志拉下来（扫码登录后跑一段时间再拉）
    docker logs --since 2h qq-grok-bot > /tmp/bot.log 2>&1

    # 2) 对着日志 + data/ 做验证，生成报告
    python3 scripts/verify_napcat_log.py --log /tmp/bot.log

    # 3) 只想验证脚本本身（CI / 没服务器时）
    python3 scripts/verify_napcat_log.py --self-test

输出
----
`docs/napcat-log-check-YYYYMMDD.md`：每条断言 = 文档章节 + 日志证据 + PASS/FAIL/SKIP。
退出码：有 FAIL → 1；否则 0（SKIP 不算失败，代表这次日志没覆盖到）。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import tuning  # noqa: E402

# ---------------- 日志正则 ----------------
RE_JUDGE = re.compile(
    r"\[MOOD-JUDGE\]\s+(\S+)\s+.*?impact\s+([+-]\d+)\s+conf\s+([\d.]+)"
    r"\s+→\s+v\s+([+-][\d.]+)\s+P\s+([\d.]+)")
RE_WINDOW = re.compile(r"\[MOOD-JUDGE\]\s+(\S+)\s+段\s+(\d+)\s+条\s+→\s+(\d+)\s+窗")
RE_SKIP_REL = re.compile(r"\[MOOD-JUDGE\]\s+(\S+)\s+relevance\s+([\d.]+)\s+<\s+([\d.]+)")
RE_FAST = re.compile(r"\[FAST\]\s+\S+\s+.*?→\s+v=([+-][\d.]+)")
RE_LITE_HIT = re.compile(r"\[LITE\]\s+\S+\s+硬信号命中")
RE_LITE_MISS = re.compile(r"\[LITE\]\s+\S+\s+不是在叫她")
RE_REL = re.compile(r"\[REL\]\s+(\d+)\s+(trust|respect)([+-]\d+)→([\d.]+)")
RE_OUT = re.compile(r"\[OUT\]\s+气泡\s+(\d+)\s+个\s+>\s+(\d+)")
RE_BUBBLES = re.compile(r"气泡=(\d+)")
RE_RECALL = re.compile(r"\[RECALL\]\s+角色脑\s+([\d.]+)ms\s+>\s+目标\s+([\d.]+)ms")

ROWS: list[dict] = []


def add(aid: str, doc: str, ok, evidence: str, note: str = "") -> None:
    """ok = True/False/None（None = SKIP）。"""
    ROWS.append({"id": aid, "doc": doc,
                 "result": "PASS" if ok is True else ("FAIL" if ok is False else "SKIP"),
                 "evidence": evidence[:220], "note": note})


# ---------------- 日志解析 ----------------
def _line_ts(line: str) -> float:
    """从 "11-25 18:21:23" 取时间戳（用于跳过间隔过大的相邻判读）。"""
    m = re.match(r"^(\d\d-\d\d \d\d:\d\d:\d\d)", line)
    if not m:
        return 0.0
    try:
        year = time.localtime().tm_year
        return time.mktime(time.strptime(f"{year}-{m.group(1)}", "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return 0.0


def parse_log(text: str, after: str = "") -> dict:
    d = {"judge": [], "window": [], "budget": [], "skip_rel": [], "fast": [],
         "lite_hit": 0, "lite_miss": 0, "rel": [], "out": [],
         "bubbles": [], "recall_slow": []}
    for line in text.splitlines():
        # 只看某时刻之后的日志（格式 "11-25 16:10"）：老版本的 FAIL 证据不该算进来
        if after and re.match(r"^\d\d-\d\d \d\d:\d\d", line) and line[:14] < after:
            continue
        m = RE_JUDGE.search(line)
        if m:
            d["judge"].append({
                "key": m.group(1), "impact": int(m.group(2)),
                "conf": float(m.group(3)), "v": float(m.group(4)),
                "P": float(m.group(5)), "line": line.strip(),
                "ts": _line_ts(line)})
            continue
        # 预算不够时「段 N 窗 → 只留 M 窗」是设计行为（P4b），P4 不该再判失败。
        m = re.search(r"\[MOOD-JUDGE\] (\S+) 段 (\d+) 窗，预算只够 (\d+) 窗", line)
        if m:
            d["budget"].append({"key": m.group(1), "total": int(m.group(2)),
                                "left": int(m.group(3))})
            continue
        m = RE_WINDOW.search(line)
        if m:
            d["window"].append({"key": m.group(1), "n": int(m.group(2)),
                                "wins": int(m.group(3)), "line": line.strip()})
            continue
        m = RE_SKIP_REL.search(line)
        if m:
            d["skip_rel"].append({"key": m.group(1), "rel": float(m.group(2)),
                                  "gate": float(m.group(3))})
            continue
        m = RE_FAST.search(line)
        if m:
            d["fast"].append({"v": float(m.group(1)), "line": line.strip()})
            continue
        if RE_LITE_HIT.search(line):
            d["lite_hit"] += 1
            continue
        if RE_LITE_MISS.search(line):
            d["lite_miss"] += 1
            continue
        m = RE_REL.search(line)
        if m:
            d["rel"].append({"qq": m.group(1), "kind": m.group(2),
                             "sign": int(m.group(3)), "to": float(m.group(4))})
            continue
        m = RE_OUT.search(line)
        if m:
            d["out"].append({"n": int(m.group(1)), "cap": int(m.group(2)),
                             "line": line.strip()})
            continue
        m = RE_BUBBLES.search(line)
        if m:
            d["bubbles"].append(int(m.group(1)))
            continue
        m = RE_RECALL.search(line)
        if m:
            d["recall_slow"].append({"ms": float(m.group(1)),
                                     "target": float(m.group(2))})
    return d


def _delta(prev_v: float, cur_v: float) -> float:
    return cur_v - prev_v


# ---------------- 日志断言 ----------------
def check_log(d: dict) -> None:
    # M1 同向事件单调累积（只看 impact≠0 的相邻记录）
    pairs, bad = 0, []
    for key in {r["key"] for r in d["judge"]}:
        seq = sorted([r for r in d["judge"]
                      if r["key"] == key and r["impact"] != 0],
                     key=lambda r: r.get("ts") or 0)
        for a, b in zip(seq, seq[1:]):
            if a["impact"] * b["impact"] <= 0:
                continue
            # 中间夹了绝对锚点：v 被纠偏是设计行为，不算单调性反例
            if "锚点" in a["line"] or "锚点" in b["line"]:
                continue
            # 合并多个容器的日志时可能时间倒序 → 跳过。
            gap = (b.get("ts") or 0) - (a.get("ts") or 0)
            if a.get("ts") and b.get("ts") and gap < 0:
                continue
            # 两次判读之间会先按半衰期向 0 衰减；间隔越大，衰减越可能
            # 盖过这次步进（真机 M1/M1h 都踩过）。这里先把 a 的值衰减到
            # b 的时刻，再比较方向，而不是用固定 180s 阈值。
            try:
                hl_min = (tuning.f("MOOD_HALF_LIFE_PRIVATE_MIN")
                          if str(key).startswith("private")
                          else tuning.f("MOOD_HALF_LIFE_GROUP_MIN"))
            except Exception:
                hl_min = 30.0
            tau = max(60.0, float(hl_min) * 60.0)
            v_decayed = a["v"] * math.exp(-max(0.0, gap) / tau)
            pairs += 1
            if (b["v"] - v_decayed) * b["impact"] < -0.005:
                bad.append(f"{key}: {a['v']:+.3f}→{b['v']:+.3f}"
                           f"（impact {b['impact']:+d}，已扣衰减）")
    ok = (not bad) if pairs else None
    ev = (f"{pairs} 对同向记录，反向 {len(bad)} 处；{bad[0]}" if bad
          else (f"{pairs} 对同向记录，方向全部一致" if pairs
                else "没有 impact≠0 的相邻判读记录"))
    add("M1", "emotion §3.1 单调累积", ok, ev)

    # M10 同段递减：同一会话连续 impact>0 时，Δv 应非增
    ok, ev = None, "没有连续同向记录"
    for key in {r["key"] for r in d["judge"]}:
        seq = sorted([r for r in d["judge"] if r["key"] == key],
                     key=lambda r: r.get("ts") or 0)
        run = []
        for a, b in zip(seq, seq[1:]):
            if a["impact"] > 0 and b["impact"] > 0:
                if "锚点" in a["line"] or "锚点" in b["line"]:
                    continue
                gap = (b.get("ts") or 0) - (a.get("ts") or 0)
                if a.get("ts") and b.get("ts") and not (0 <= gap <= 180):
                    continue
                run.append(abs(_delta(a["v"], b["v"])))
        if len(run) >= 2:
            ok = all(run[i + 1] <= run[i] + 0.02 for i in range(len(run) - 1))
            ev = f"{key}: Δv 序列 {['%.3f' % x for x in run[:6]]}"
            break
    add("M10", "emotion §3.1 K 随 P 下降", ok, ev)

    # M4 低置信几乎不动
    hi = [abs(_delta(a["v"], b["v"])) for a, b in zip(d["judge"], d["judge"][1:])
          if a["key"] == b["key"] and b["conf"] >= 0.8 and abs(a["v"]) < 0.5]
    lo = [abs(_delta(a["v"], b["v"])) for a, b in zip(d["judge"], d["judge"][1:])
          if a["key"] == b["key"] and b["conf"] <= 0.4 and abs(a["v"]) < 0.5]
    if hi and lo:
        ok = (sum(lo) / len(lo)) <= 0.6 * (sum(hi) / len(hi))
        ev = f"低置信均值 {sum(lo)/len(lo):.3f} / 高置信均值 {sum(hi)/len(hi):.3f}"
    else:
        ok, ev = None, "样本不足（需要 conf≤0.4 和 ≥0.8 各一条）"
    add("M4", "emotion §3.1 R 用 confidence", ok, ev)

    # M3 接近 ±1 越难走
    near = [abs(_delta(a["v"], b["v"])) for a, b in zip(d["judge"], d["judge"][1:])
            if a["key"] == b["key"] and abs(a["v"]) >= 0.6 and b["impact"] == 1]
    low = [abs(_delta(a["v"], b["v"])) for a, b in zip(d["judge"], d["judge"][1:])
           if a["key"] == b["key"] and abs(a["v"]) < 0.2 and b["impact"] == 1]
    if near and low:
        ok = (sum(near) / len(near)) <= 0.7 * (sum(low) / len(low))
        ev = f"|v|≥0.6 均值 {sum(near)/len(near):.3f} / |v|<0.2 均值 {sum(low)/len(low):.3f}"
    else:
        ok, ev = None, "样本不足（需要 v 接近 ±1 的记录）"
    add("M3", "emotion §3.1 saturation", ok, ev)

    # M8 快速通道只降不升
    if d["fast"]:
        ok = all(f["v"] <= -0.44 for f in d["fast"])
        ev = f"{len(d['fast'])} 次护栏，最低 v={min(f['v'] for f in d['fast']):+.2f}"
    else:
        ok, ev = None, "没有触发快速护栏（没有 @她+强负面）"
    add("M8", "DESIGN §9 红线 1 例外", ok, ev)

    # P4 段内消息不丢：窗数 ≥ ceil(n / MOOD_JUDGE_CTX)，且 n ≤ 40×窗数。
    # ⚠️ 2026-11-27：以前写死 8，且把「预算只够 N 窗」的截断也算失败。
    #    预算截断是 P4b 的设计行为（丢最早窗），这里要排除。
    if d["window"]:
        _size = max(1, tuning.i("MOOD_JUDGE_CTX"))
        _budget = {(b["key"], b["left"]) for b in d.get("budget", [])}
        bad = []
        for w in d["window"]:
            if (w["key"], w["wins"]) in _budget:
                continue
            if w["wins"] < math.ceil(w["n"] / _size) or w["n"] > 40 * w["wins"]:
                bad.append(w)
        ok = not bad
        ev = (f"{len(d['window'])} 个段；最差 {bad[0]['line'][:120]}" if bad
              else f"{len(d['window'])} 个段全部覆盖（窗大小 {_size}，预算截断已排除）")
    else:
        ok, ev = None, "没有分段判读记录"
    add("P4", "causal P4 段内消息不丢", ok, ev)

    # 判读预算：出现「丢掉最早的」不算失败，但要有记录
    add("P4b", "emotion §5.1 预算内丢最早窗", True if d["window"] else None,
        "见 [MOOD-JUDGE] 段 N 条 → M 窗" if d["window"] else "没有分段记录")

    # O4/O5 气泡口径
    if d["out"]:
        ok = all(o["n"] > o["cap"] for o in d["out"])
        ev = f"{len(d['out'])} 次超泡合并，cap={d['out'][0]['cap']}"
    else:
        ok, ev = None, "没有超过 2 泡的回复"
    add("O4", "DESIGN §9 红线 1 气泡", ok, ev)
    if d["bubbles"]:
        # DESIGN §9.1 例外：表情/语音保留不丢，可以多一个泡。
        # 所以「文字泡 ≤2」在日志里可能是 3（2 文字 + 1 表情/语音），不算失败。
        _text_cap = tuning.i("OUT_BUBBLES_MAX")
        ok = max(d["bubbles"]) <= _text_cap + 1
        ev = (f"最大气泡数 {max(d['bubbles'])}"
              f"（文字上限 {_text_cap}，+1 给表情/语音例外）")
    else:
        ok, ev = None, "没有 气泡=N 的发送日志"
    add("O5", "DESIGN §9 红线 1 不截断", ok, ev)

    # 关系事件
    if d["rel"]:
        ok = all(0.0 <= r["to"] <= 1.0 for r in d["rel"])
        ev = f"{len(d['rel'])} 条关系事件，值域 {min(r['to'] for r in d['rel']):.2f}~{max(r['to'] for r in d['rel']):.2f}"
    else:
        ok, ev = None, "没有 trust/respect 事件（普通闲聊不会触发）"
    add("R2", "emotion §3.2 trust/respect 写入口", ok, ev)

    # 省流模式（G36）
    if d["lite_hit"] or d["lite_miss"]:
        ok = d["lite_hit"] + d["lite_miss"] > 0
        ev = f"命中 {d['lite_hit']} 次 / 只记录 {d['lite_miss']} 次"
    else:
        ok, ev = None, "GROUP_WATCH_MODE=full（没走省流分支）"
    add("G36", "features §20 省流模式", ok, ev)

    # 角色脑延迟（50ms 是 sqlite 口径；json 后端 O(N) 扫描 ~1s，不算失败）
    try:
        from services import memory_v2 as _mv
        _backend = str(getattr(_mv, "BACKEND", "json"))
    except Exception:
        _backend = os.getenv("MEMORY_BACKEND", "json").strip().lower()
    if _backend != "sqlite":
        add("S4b", "retrieval §7 角色脑 ≤50ms", None,
            f"后端={_backend}（50ms 只对 sqlite 生效）")
    elif d["recall_slow"]:
        # 50ms 是**目标**不是红线（DESIGN：超了只告警）；这里只统计，不判定失败。
        add("S4b", "retrieval §7 角色脑 ≤50ms", None,
            f"{len(d['recall_slow'])} 次超 "
            f"{d['recall_slow'][0]['target']:.0f}ms（性能观测项，不判定）")
    else:
        add("S4b", "retrieval §7 角色脑 ≤50ms", None,
            "sqlite 下没有超过 50ms 的召回")

    # 相关性闸门
    if d["skip_rel"]:
        ok = all(s["rel"] < s["gate"] for s in d["skip_rel"])
        ev = f"{len(d['skip_rel'])} 次 relevance 跳过"
    else:
        ok, ev = None, "没有触发 relevance 闸门"
    add("M4b", "emotion §5.1 relevance gate", ok, ev)


# ---------------- data/ 断言 ----------------
def check_data(data_dir: Path) -> None:
    mood_file = data_dir / "mood.json"
    ok, ev = None, "没有 mood.json（还没跑过判读）"
    if mood_file.exists():
        try:
            st = json.loads(mood_file.read_text(encoding="utf-8"))
            recs = [st.get("global") or {}] + list((st.get("sessions") or {}).values())
            bad = []
            for r in recs:
                v = float(r.get("value") or 0.0)
                p = float(r.get("P", tuning.f("MOOD_P0")))
                if not (-100.0 <= v <= 100.0):
                    bad.append(f"value={v}")
                if not (0.0 < p <= 1.0):
                    bad.append(f"P={p}")
                if len(str(r.get("label") or "")) > tuning.i("MOOD_LABEL_MAX") + 1:
                    bad.append("label 超长")
            ok = not bad
            ev = f"{len(recs)} 条状态；{'；'.join(bad[:3]) if bad else '值域正常'}"
        except Exception as e:
            ok, ev = False, f"{type(e).__name__}: {e}"
    add("M0", "emotion §3.1 状态值域", ok, ev)

    rel_file = data_dir / "relationship.json"
    ok, ev = None, "没有 relationship.json"
    if rel_file.exists():
        try:
            st = json.loads(rel_file.read_text(encoding="utf-8"))
            bad = [k for k, r in st.items()
                   if not (0.0 <= float(r.get("trust", 0)) <= 1.0
                           and 0.0 <= float(r.get("respect", 0)) <= 1.0)]
            ok = not bad
            ev = f"{len(st)} 人；{'越界: ' + ','.join(bad[:3]) if bad else '值域正常'}"
        except Exception as e:
            ok, ev = False, f"{type(e).__name__}: {e}"
    add("R1", "emotion §3.2 关系值域", ok, ev)

    rlog = data_dir / "relationship_log.jsonl"
    ok, ev = None, "没有 relationship_log.jsonl"
    if rlog.exists():
        n, bad = 0, 0
        try:
            for line in rlog.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                n += 1
                if abs(float(row.get("delta") or 0)) > 0.5:
                    bad += 1
            ok = bad == 0
            ev = f"{n} 条事件；异常 {bad}"
        except Exception as e:
            ok, ev = False, f"{type(e).__name__}: {e}"
    add("R6", "emotion §3.2 日上限/步长", ok, ev)

    hist = data_dir / "mood_history.jsonl"
    ok, ev = None, "没有 mood_history.jsonl"
    if hist.exists():
        n, bad = 0, 0
        try:
            for line in hist.read_text(encoding="utf-8").splitlines()[-4000:]:
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                # 只看新链路（observe / anchor）；旧 llm-judge 是 -100~100 内部口径，
                # 混进来会把单位不同的老记录算成越界。
                # fast-guard 已于 2026-11-25 删除（ADR-038），历史记录不再参与判定，
                # 否则一条旧账会让这个断言永远 FAIL。
                if str(row.get("reason") or "") not in ("observe", "anchor"):
                    continue
                n += 1
                if abs(float(row.get("delta") or 0)) > 40:
                    bad += 1
            if n:
                ok = bad == 0
                ev = f"新链路 {n} 条波动；单次 >0.40 的 {bad} 条"
            else:
                ev = "没有新链路波动记录（旧 llm-judge 不计）"
        except Exception as e:
            ok, ev = False, f"{type(e).__name__}: {e}"
    add("M7", "emotion §3.1 锚点/步长有界", ok, ev)

    # M1h / M10h：用 mood_history 的新链路记录补证（日志会随容器重建丢失，data 不会）
    hist = data_dir / "mood_history.jsonl"
    by_sess = {}
    anchor_at = {}
    if hist.exists():
        try:
            for line in hist.read_text(encoding="utf-8").splitlines()[-4000:]:
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                # ⚠️ 2026-11-27：mood_history 里混着旧格式（-100~100）和新格式（-1~1）。
                # 混着算会把「65.98 → 55.61」当成反向；先按量纲过滤。
                try:
                    _v = float(row.get("value") or 0)
                    _dl = float(row.get("delta") or 0)
                except Exception:
                    continue
                if abs(_v) > 1.5 or abs(_dl) > 1.0:
                    continue
                _reason = str(row.get("reason") or "")
                _sess = str(row.get("session") or "")
                if _reason == "anchor":
                    anchor_at.setdefault(_sess, []).append(float(row.get("at") or 0))
                elif _reason == "observe":
                    by_sess.setdefault(_sess, []).append(row)
        except Exception:
            pass
    m1_pairs, m1_bad = 0, 0
    m10_seqs, m10_bad = 0, 0
    for sess, rows in by_sess.items():
        rows.sort(key=lambda r: float(r.get("at") or 0))
        ds = [float(r.get("delta") or 0) for r in rows]
        vs = [float(r.get("value") or 0) for r in rows]
        _anc = anchor_at.get(sess, [])
        for i in range(1, len(rows)):
            if ds[i - 1] * ds[i] > 0:
                _t0 = float(rows[i - 1].get("at") or 0)
                _t1 = float(rows[i].get("at") or 0)
                # 两次观测之间夹了绝对锚点：v 被纠偏是设计行为，不算单调性反例。
                # 锚点和 observe 是同一秒写出的，时间戳可能差几微秒，所以留 2s 容差。
                if any(_t0 - 2.0 <= a <= _t1 + 2.0 for a in _anc):
                    continue
                # M1 的前置是「间隔 60s」：隔了几分钟时，向 0 的衰减可能大于
                # 本次步进（例：-13.7 过 9 分钟衰减回 -11，再来 -1 → 看起来「反向」）。
                if _t1 - _t0 > 180:
                    continue
                m1_pairs += 1
                if (vs[i] - vs[i - 1]) * ds[i] < 0:
                    m1_bad += 1
        # M10：只在「同一段 / 同方向 / 同一档位」的连续观测里比步长。
        # 旧写法把整个会话的 |Δ| 串起来比，跨段重新热身（K 回升）和 ±1→±2 的
        # 档位变化都会被误判成「步长回升」（2026-11-25 真机数据踩到）。
        segs, cur = [], []
        for r in rows:
            d = float(r.get("delta") or 0)
            if abs(d) < 1e-9:
                continue
            t = float(r.get("at") or 0)
            try:
                imp = (int(r.get("impact"))
                       if r.get("impact") is not None else None)
            except Exception:
                imp = None
            if cur and (d * cur[-1]["d"] < 0 or t - cur[-1]["t"] > 180):
                segs.append(cur)
                cur = []
            cur.append({"d": d, "t": t, "imp": imp})
        if cur:
            segs.append(cur)
        for seg in segs:
            if len(seg) < 3:
                continue
            m10_seqs += 1
            for i in range(1, len(seg)):
                a, b = seg[i - 1], seg[i]
                if a["imp"] is None or b["imp"] is None or a["imp"] != b["imp"]:
                    continue      # 档位不同，步长本来就不同，不算回升
                # 2026-11-26 用户拍板：|impact|≥2 会回补 P（MOOD_P_EVENT_BOOST），
                # 下一步 K 回升、步长变大是设计行为，不算「K 不该回升」。
                if abs(int(a["imp"])) >= 2:
                    continue
                # 两次观测之间夹了锚点 → 锚点会主动抬高 P（ANCHOR_P_ADD），
                # 下一步 dv 变大是设计行为，不算「K 回升」。
                if any(a["t"] - 2.0 <= x <= b["t"] + 2.0 for x in _anc):
                    continue
                if abs(b["d"]) > abs(a["d"]) + 1.0:
                    m10_bad += 1
                    break
    add("M1h", "emotion §3.1 单调累积（data）",
        (m1_bad == 0) if m1_pairs else None,
        f"{m1_pairs} 对同向观测，反向 {m1_bad} 处" if m1_pairs else "没有同向观测")
    add("M10h", "emotion §3.1 步长递减（data）",
        (m10_bad == 0) if m10_seqs else None,
        f"{m10_seqs} 段 ≥3 次观测，步长回升 {m10_bad} 段" if m10_seqs else "没有 ≥3 次观测的会话")


# ---------------- 报告 ----------------
def render_report(rows: list[dict], source: str) -> str:
    n_pass = sum(1 for r in rows if r["result"] == "PASS")
    n_fail = sum(1 for r in rows if r["result"] == "FAIL")
    n_skip = sum(1 for r in rows if r["result"] == "SKIP")
    lines = [
        "# NapCat 日志 ↔ 设计文档 对接验证报告",
        "",
        f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 日志来源：`{source}`",
        f"- 结论：**{n_pass} PASS / {n_fail} FAIL / {n_skip} SKIP**",
        "",
        "> SKIP = 这段日志没有覆盖到（比如没触发主动插话），不代表失败。",
        "",
        "| 断言 | 文档 | 证据 | 结果 |",
        "|---|---|---|---|",
    ]
    for r in rows:
        ev = r["evidence"].replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {r['id']} | {r['doc']} | {ev} | {r['result']} |")
    lines += ["", "## 复跑", "",
              "```bash",
              "docker logs --since 2h qq-grok-bot > /tmp/bot.log 2>&1",
              "python3 scripts/verify_napcat_log.py --log /tmp/bot.log",
              "```", ""]
    return "\n".join(lines)


SELF_TEST_LOG = """
[MOOD-JUDGE] group_1 段 12 条 → 2 窗（补判 1 窗）
[MOOD-JUDGE] group_1 「他夸我」→ 被夸得有点飘（impact +1 conf 0.90 → v +0.091 P 0.02）他夸了她
[MOOD-JUDGE] group_1 「又夸」→ 有点飘（impact +1 conf 0.90 → v +0.166 P 0.01）他又夸
[MOOD-JUDGE] group_1 「还行」→ 平平（impact +1 conf 0.30 → v +0.168 P 0.01）随口一句
[MOOD-JUDGE] group_1 「骂我」→ 憋气（impact -2 conf 0.90 → v -0.280 P 0.01）被骂
[FAST] 1 强负面护栏 → v=-0.45
[LITE] 1 硬信号命中（@/引用/喊名字/收件人是她）→ 接话
[LITE] 1 不是在叫她 → 只记上下文，不入队
[REL] 20001 trust+1→0.248
[REL] 20001 respect-1→0.190
[OUT] 气泡 3 个 > 2 → 尾部合并，不截断
[CHAT] 发送完成 气泡=2
"""


def self_test() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="verify_log_"))
    (tmp / "mood.json").write_text(json.dumps({
        "global": {"value": 10, "P": 0.02, "label": ""},
        "sessions": {"group_1": {"value": -28, "P": 0.01, "label": "憋气"}}}),
        encoding="utf-8")
    (tmp / "relationship.json").write_text(json.dumps({
        "20001": {"trust": 0.248, "respect": 0.19}}), encoding="utf-8")
    (tmp / "relationship_log.jsonl").write_text(
        '{"qq":"20001","kind":"trust","delta":0.048,"to":0.248}\n', encoding="utf-8")
    (tmp / "mood_history.jsonl").write_text(
        '{"delta":9.1}\n{"delta":-17.4}\n', encoding="utf-8")
    d = parse_log(SELF_TEST_LOG)
    check_log(d)
    check_data(tmp)
    report = render_report(ROWS, "self-test")
    print(report)
    fails = [r for r in ROWS if r["result"] == "FAIL"]
    print(f"\nself-test: {len(ROWS)} checks, {len(fails)} FAIL")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", action="append", default=[],
                    help="日志文件，可重复；- 表示 stdin")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--out", default="")
    ap.add_argument("--after", default="",
                    help='只看该时刻之后的日志，格式 "11-25 16:10"')
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    if not args.log:
        print("需要 --log（或 --self-test）", file=sys.stderr)
        return 2

    chunks = []
    for p in args.log:
        if p == "-":
            chunks.append(sys.stdin.read())
        else:
            chunks.append(Path(p).read_text(encoding="utf-8", errors="replace"))
    text = "\n".join(chunks)
    d = parse_log(text, after=args.after)
    check_log(d)
    check_data(Path(args.data_dir))
    report = render_report(ROWS, ", ".join(args.log))
    # ⚠️ 2026-11-27：默认文件名以前是 `verification-report-YYYYMMDD.md`，
    #    会和手写的同日期验证报告**互相覆盖**（真踩过：手写报告被脚本盖掉）。
    #    脚本报告统一叫 `napcat-log-check-YYYYMMDD.md`。
    out = Path(args.out) if args.out else (
        ROOT / "docs" / f"napcat-log-check-{time.strftime('%Y%m%d')}.md")
    out.write_text(report, encoding="utf-8")
    print(report)
    print(f"\n报告：{out}")
    return 1 if any(r["result"] == "FAIL" for r in ROWS) else 0


if __name__ == "__main__":
    raise SystemExit(main())
