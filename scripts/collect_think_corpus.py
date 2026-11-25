#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""collect_think_corpus.py —— 从 llm_trace / mood_think 汇总她的心理活动语料。

为什么
------
`mood_think.json` 每会话只留最近 `MOOD_THINK_KEEP`（30）条，样本太少。
`data/llm_trace/*.jsonl` 每条都存了 `reply`（含 `[[THINK|…]]`）和 `msgs`（完整输入），
是现成的历史归档。本脚本把它们抽成一张语料表，供标注抽样用。

输出一行：
  {at, session, channel, speaker, think, src, reply, proactive}

用法（服务器上跑，输出到 /tmp 再拉回）：
  python3 scripts/collect_think_corpus.py --out /tmp/think_corpus.jsonl
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
THINK_RE = re.compile(r"[\[【]{1,3}\s*THINK\s*[|｜:：]\s*([^\]】\n]{1,120})")
PROACTIVE = ("【你想主动找", "现在是你主动找他", "你想主动")


def parse_think(reply: str) -> tuple:
    m = None
    for x in THINK_RE.finditer(reply or ""):
        m = x
    if not m:
        return (reply or "").strip(), ""
    think = str(m.group(1) or "").strip().strip("[]【】「」")
    clean = (reply[:m.start()] + reply[m.end():]).strip()
    clean = re.sub(r"[\[【]{1,3}\s*THINK[\s\S]*$", "", clean).strip()
    return clean, think[:200]


def last_user(msgs: list) -> str:
    for m in reversed(msgs or []):
        if str(m.get("role")) == "user":
            return re.sub(r"\s+", " ", str(m.get("content") or "")).strip()
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description="汇总 THINK 语料")
    ap.add_argument("--trace-dir", default=str(ROOT / "data/llm_trace"))
    ap.add_argument("--think", default=str(ROOT / "data/mood_think.json"))
    ap.add_argument("--out", default="/tmp/think_corpus.jsonl")
    ap.add_argument("--keep-proactive", action="store_true")
    args = ap.parse_args()

    out, seen = [], set()

    def add(at, session, think, src, reply, proactive=False):
        think = re.sub(r"\s+", " ", str(think or "")).strip()
        if not think or not src:
            return
        if proactive and not args.keep_proactive:
            return
        key = (think, src[:120])
        if key in seen:
            return
        seen.add(key)
        kind, _, peer = str(session).partition("_")
        out.append({"at": float(at or 0), "session": str(session),
                    "channel": kind or "group", "peer": peer,
                    "think": think[:200], "src": src[:300],
                    "reply": str(reply or "")[:300], "proactive": bool(proactive)})

    # 1) mood_think.json（带 src/reply，最干净）
    p = Path(args.think)
    if p.exists():
        raw = json.loads(p.read_text(encoding="utf-8")) or {}
        for key, lst in raw.items():
            for x in lst or []:
                if not isinstance(x, dict) or x.get("auto"):
                    continue
                add(x.get("at"), key, x.get("text"), x.get("src"), x.get("reply"))

    # 2) llm_trace
    n_trace = 0
    for fp in sorted(glob.glob(str(Path(args.trace_dir) / "*.jsonl"))):
        for line in open(fp, encoding="utf-8", errors="ignore"):
            try:
                d = json.loads(line)
            except Exception:
                continue
            clean, think = parse_think(str(d.get("reply") or ""))
            if not think:
                continue
            n_trace += 1
            src = last_user(d.get("msgs") or [])
            proactive = any(k in src for k in PROACTIVE)
            add(d.get("ts"), d.get("key") or "", think, src, clean, proactive)

    out.sort(key=lambda r: r["at"])
    with open(args.out, "w", encoding="utf-8") as f:
        for r in out:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"trace 带 THINK 的记录 {n_trace}；去重后语料 {len(out)} 条")
    print("渠道：", Counter(r["channel"] for r in out))
    print("会话：", len({r["session"] for r in out}))
    if out:
        print("时间：", time.strftime("%m-%d %H:%M", time.localtime(out[0]["at"])),
              "~", time.strftime("%m-%d %H:%M", time.localtime(out[-1]["at"])))
    print(f"[out] {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
