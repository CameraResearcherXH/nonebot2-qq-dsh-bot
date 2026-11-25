# services/perf_stats.py
"""性能周报：p95 只看最慢一块（DESIGN §8 / pipeline-map §8）。

为什么是 p95 而不是平均：
  · 平均会被大量 50ms 的快路径稀释，真正让人「等半天」的是尾部；
  · 群里一条 8s 的回复，用户体感就是「她卡住了」，哪怕平均只有 1.2s。

数据来源（都不额外打点，复用已有落盘）：
  1. `data/metrics/pipeline_stages.jsonl` —— reply_pipeline 每轮各步耗时
     （`PIPE_METRICS=1` 时写；容器重启不丢）；
  2. `data/llm_usage.db` 的 `usage.latency_ms` —— 每次模型调用的真实延迟。

输出：
  · `p95_report(days)` → dict（脚本 / 周报共用）
  · `render_text()` / `render_md()`
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
METRICS_FILE = Path(os.getenv("PIPE_METRICS_FILE", str(ROOT / "data/metrics/pipeline_stages.jsonl")))
LLM_DB = Path(os.getenv("LLM_USAGE_DB", str(ROOT / "data/llm_usage.db")))

# 最慢一步 → 先去哪儿看（周报直接给「只优化这一块」的落点）
STAGE_HINT = {
    "称呼": "services/callname.py",
    "状态": "services/mood.py / services/mood_judge.py",
    "档案": "services/profiles.py / services/memory_v2.py",
    "总结": "services/conv_state.py / services/scene_auditor.py",
    "记忆": "services/memdb.py（sqlite 检索）",
    "图书馆": "services/agent_client.py（RAG，5~14s）",
    "已回": "services/chat_switch.py / dialogue_log",
    "白月光": "services/white_moon.py / services/style_vault.py",
}


def _pct(vals, q: float) -> float:
    vals = sorted(float(x) for x in (vals or []) if x is not None)
    if not vals:
        return 0.0
    k = min(len(vals) - 1, max(0, int(round((len(vals) - 1) * float(q)))))
    return vals[k]


def _load_stage_rows(days: int) -> list:
    if not METRICS_FILE.exists():
        return []
    since = time.time() - max(1, int(days)) * 86400
    out = []
    try:
        with METRICS_FILE.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if float(r.get("ts") or 0) >= since:
                    out.append(r)
    except Exception:
        return []
    return out


def _load_llm_latency(days: int) -> list:
    if not LLM_DB.exists():
        return []
    since = time.time() - max(1, int(days)) * 86400
    try:
        c = sqlite3.connect(str(LLM_DB), timeout=5)
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT purpose,model,latency_ms FROM usage "
            "WHERE ts>=? AND latency_ms>0", (since,)).fetchall()
        c.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def p95_report(days: int = 7) -> dict:
    days = max(1, int(days or 7))
    rows = _load_stage_rows(days)
    stages: dict = {}
    turns = []
    for r in rows:
        try:
            turns.append(float(r.get("cost_ms") or 0))
        except Exception:
            pass
        for name, ms in (r.get("stages") or {}).items():
            d = stages.setdefault(name, {"n": 0, "vals": [], "status": {}})
            try:
                d["vals"].append(float(ms))
                d["n"] += 1
            except Exception:
                continue
            st = str((r.get("status") or {}).get(name) or "")
            if st:
                d["status"][st] = d["status"].get(st, 0) + 1
    stage_out = {}
    for name, d in stages.items():
        vals = d["vals"]
        stage_out[name] = {
            "n": d["n"], "p50": round(_pct(vals, 0.50), 1),
            "p95": round(_pct(vals, 0.95), 1),
            "max": round(max(vals) if vals else 0.0, 1),
            "status": dict(sorted(d["status"].items(), key=lambda kv: -kv[1])[:4]),
        }

    llm_rows = _load_llm_latency(days)
    llm: dict = {}
    for r in llm_rows:
        p = str(r.get("purpose") or "other")
        d = llm.setdefault(p, [])
        d.append(float(r.get("latency_ms") or 0))
    llm_out = {p: {"n": len(v), "p50": round(_pct(v, 0.50), 0),
                   "p95": round(_pct(v, 0.95), 0),
                   "max": round(max(v) if v else 0, 0)}
               for p, v in llm.items()}

    slowest = ""
    if stage_out:
        slowest = max(stage_out.items(), key=lambda kv: kv[1]["p95"])[0]
    return {
        "days": days,
        "turns": len(rows),
        "turn_p50_ms": round(_pct(turns, 0.50), 1),
        "turn_p95_ms": round(_pct(turns, 0.95), 1),
        "stages": stage_out,
        "slowest_stage": slowest,
        "slowest_hint": STAGE_HINT.get(slowest, ""),
        "llm": llm_out,
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def render_text(days: int = 7) -> str:
    rep = p95_report(days)
    lines = [f"▍性能周报（最近 {rep['days']} 天）",
             f"样本：{rep['turns']} 轮｜整轮 p50 {rep['turn_p50_ms']:.0f}ms"
             f"｜p95 {rep['turn_p95_ms']:.0f}ms"]
    if not rep["stages"]:
        lines.append("· 还没有流水线耗时样本（确认 PIPE_METRICS=1 且她回过话）")
    else:
        lines.append("各步（按 p95 降序，只优化最慢一块）：")
        for k, v in sorted(rep["stages"].items(), key=lambda kv: -kv[1]["p95"]):
            flag = "  ← 最慢" if k == rep["slowest_stage"] else ""
            lines.append(f"  · {k}: p50 {v['p50']:.0f}ms｜p95 {v['p95']:.0f}ms"
                         f"｜max {v['max']:.0f}ms（{v['n']} 次）{flag}")
        if rep["slowest_hint"]:
            lines.append(f"→ 先看 {rep['slowest_stage']}：{rep['slowest_hint']}")
    if rep["llm"]:
        lines.append("模型调用（purpose）：")
        for k, v in sorted(rep["llm"].items(), key=lambda kv: -kv[1]["p95"]):
            lines.append(f"  · {k}: p50 {v['p50']:.0f}ms｜p95 {v['p95']:.0f}ms"
                         f"｜max {v['max']:.0f}ms（{v['n']} 次）")
    lines.append(f"生成时间：{rep['built_at']}")
    return "\n".join(lines)


def render_md(days: int = 7) -> str:
    rep = p95_report(days)
    md = [f"# 性能 p95 周报（最近 {rep['days']} 天）", "",
          f"- 样本：{rep['turns']} 轮｜整轮 p50 {rep['turn_p50_ms']:.0f}ms"
          f"｜p95 {rep['turn_p95_ms']:.0f}ms",
          f"- 生成时间：{rep['built_at']}", ""]
    if rep["stages"]:
        md += ["## 流水线各步（p95 降序）", "",
               "| 步骤 | n | p50(ms) | p95(ms) | max(ms) | 状态 |",
               "|---|---|---|---|---|---|"]
        for k, v in sorted(rep["stages"].items(), key=lambda kv: -kv[1]["p95"]):
            st = "、".join(f"{a}×{b}" for a, b in v["status"].items())
            md.append(f"| {k} | {v['n']} | {v['p50']:.0f} | {v['p95']:.0f} | "
                      f"{v['max']:.0f} | {st} |")
        md += ["", f"**最慢一块：{rep['slowest_stage']}**"
               + (f" → `{rep['slowest_hint']}`" if rep["slowest_hint"] else ""), ""]
    else:
        md += ["（还没有流水线耗时样本）", ""]
    if rep["llm"]:
        md += ["## 模型调用延迟", "", "| purpose | n | p50(ms) | p95(ms) | max(ms) |",
               "|---|---|---|---|---|"]
        for k, v in sorted(rep["llm"].items(), key=lambda kv: -kv[1]["p95"]):
            md.append(f"| {k} | {v['n']} | {v['p50']:.0f} | {v['p95']:.0f} | {v['max']:.0f} |")
    return "\n".join(md)
