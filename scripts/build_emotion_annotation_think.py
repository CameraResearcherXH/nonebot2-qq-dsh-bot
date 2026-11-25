#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""build_emotion_annotation_think.py —— 从**她的心理活动**抽情绪标注候选。

为什么重做（2026-11-25 用户指出）
--------------------------------
旧版（build_emotion_annotation.py）从群聊里抽消息，让人看对话猜她的心情 —— 方向错了。
她的心情只写在她自己的 `[[THINK|…]]` 里（`data/mood_think.json`），
判读模型（mood_judge）也是读这段心理活动。所以：

  输入（给模型看） = `src`：她当时在回的那句话 + 上下文
  标签（要人标）   = `think`：她自己写的心理活动 → 映射成 impact/arousal/target…

**标注时只看 `think`，不要用 `src` 去猜她应该是什么心情。**
`src` 只用来判断「这件事关于谁」（target）。

数据来源
--------
  data/mood_think.json    {session_key: [{at, text, src, reply, auto}]}
  data/mood_history.jsonl {at, delta, emotion, reason, about_qq, value}  ← 只作参考，不当标签

用法
----
  python3 scripts/build_emotion_annotation_think.py --write
  python3 scripts/build_emotion_annotation_think.py --limit 200 --include-auto

输出
----
  data/eval/emotion_think_candidates.jsonl
  data/eval/emotion_think_candidates.md
  data/eval/emotion_think_stats.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_THINK = ROOT / "data/mood_think.json"
DEFAULT_HISTORY = ROOT / "data/mood_history.jsonl"
OUT_DIR = ROOT / "data/eval"


def _load_think(path: Path) -> list:
    raw = json.loads(path.read_text(encoding="utf-8")) or {}
    out = []
    for key, lst in raw.items():
        if not isinstance(lst, list):
            continue
        kind, _, ident = str(key).partition("_")
        for x in lst:
            if not isinstance(x, dict):
                continue
            text = re.sub(r"\s+", " ", str(x.get("text") or "")).strip()
            if not text:
                continue
            out.append({
                "session": str(key), "channel": kind or "group",
                "peer": ident, "at": float(x.get("at") or 0),
                "think": text[:200],
                "src": re.sub(r"\s+", " ", str(x.get("src") or "")).strip()[:300],
                "reply": re.sub(r"\s+", " ", str(x.get("reply") or "")).strip()[:300],
                "auto": bool(x.get("auto")),
            })
    out.sort(key=lambda r: r["at"])
    return out


def _load_history(path: Path) -> list:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    out.sort(key=lambda r: float(r.get("at") or 0))
    return out


def _nearest(history: list, ts: float, window: float = 180.0):
    """最近的一条心情事件（只作参考列，不当标签）。"""
    best, gap = None, window
    for h in history:
        d = abs(float(h.get("at") or 0) - ts)
        if d < gap:
            best, gap = h, d
    return best


def build(think_path: Path, history_path: Path, include_auto: bool) -> list:
    rows = _load_think(think_path)
    hist = _load_history(history_path)
    out, seen = [], set()
    for r in rows:
        if r["auto"] and not include_auto:
            continue
        if not r["src"]:                       # 没有「在回哪句」就没法判 target
            continue
        h = hashlib.md5((r["think"] + "\x00" + r["src"]).encode("utf-8")).hexdigest()
        if h in seen:
            continue
        seen.add(h)
        m = _nearest(hist, r["at"])
        r["judge"] = ({"emotion": m.get("emotion"), "delta": m.get("delta"),
                       "reason": m.get("reason")} if m else None)
        out.append(r)
    return out


def write_md(items: list, path: Path) -> None:
    def esc(s, n=0):
        s = str(s or "").replace("\r", " ").replace("\n", " ⏎ ").replace("|", "｜").strip()
        return s[:n] + "…" if n and len(s) > n else s

    lines = ["# 情绪标注 · 心理活动候选（标签源 = 她自己的 THINK）", "",
             "> **只看「她的心理活动」标 impact**；「她在回什么」只用来判 target。",
             "> 「判读参考」是线上 mood_judge 的输出，**不是标签**，标完再对照。", "",
             "| id | 渠道 | 她的心理活动（标签源） | 她在回什么（src） | 判读参考 | impact | target | 理由 |",
             "|---|---|---|---|---|---|---|---|"]
    for it in items:
        j = it.get("judge") or {}
        ref = f"{j.get('emotion') or '-'} / {j.get('delta')}" if j else "-"
        lines.append(
            f"| {it['id']} | {it['channel']} | {esc(it['think'], 40)} | {esc(it['src'], 42)} "
            f"| {esc(ref, 18)} | ☐-2 ☐-1 ☐0 ☐+1 ☐+2 "
            f"| ☐speaker ☐recipient ☐other ☐world ☐self | ____ |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="从她的 THINK 抽情绪标注候选")
    ap.add_argument("--think", default=str(DEFAULT_THINK))
    ap.add_argument("--history", default=str(DEFAULT_HISTORY))
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--include-auto", action="store_true", help="连后台补写的 THINK 一起要")
    ap.add_argument("--out", default=str(OUT_DIR / "emotion_think_candidates.jsonl"))
    ap.add_argument("--md", default=str(OUT_DIR / "emotion_think_candidates.md"))
    ap.add_argument("--stats", default=str(OUT_DIR / "emotion_think_stats.json"))
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    items = build(Path(args.think), Path(args.history), args.include_auto)
    print(f"候选 {len(items)} 条（auto={args.include_auto}）")
    print("渠道：", Counter(x["channel"] for x in items))
    print("会话：", Counter(x["session"] for x in items).most_common(5))
    if args.limit and len(items) > args.limit:
        step = len(items) / args.limit
        items = [items[int(i * step)] for i in range(args.limit)]
        print(f"按时间等距抽到 {len(items)} 条")

    for i, it in enumerate(items, 1):
        it["id"] = f"ethink_{i:03d}"
        it["label"] = {"impact": None, "arousal": None, "dominance": None,
                       "target": None, "relation_relevance": None, "unexpected": None,
                       "confidence": None, "reason": "", "is_sarcasm": None}
        it["meta"] = {"labeler": "", "round": 1, "status": "draft",
                      "note": "标签源 = 她的 THINK；src 只用于判 target"}
        it["origin"] = "think_v1"

    stats = {
        "think_file": args.think, "history_file": args.history,
        "candidates": len(items), "include_auto": args.include_auto,
        "channels": dict(Counter(x["channel"] for x in items)),
        "sessions": len({x["session"] for x in items}),
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    if not args.write:
        print("（未写盘；加 --write）")
        return 0
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in items) + "\n",
                   encoding="utf-8")
    write_md(items, Path(args.md))
    Path(args.stats).write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[out] {out}\n[out] {args.md}\n[out] {args.stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
