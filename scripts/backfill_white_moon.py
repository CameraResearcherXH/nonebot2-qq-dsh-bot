#!/usr/bin/env python3
# scripts/backfill_white_moon.py
"""从对话日志回填白月光统计（A @ 了谁 / 引用回复了谁）。

用法（容器里）：
    python scripts/backfill_white_moon.py 2026-11-23

只统计「群友之间」的 @ 和引用；不统计 @全体、不统计她本人。
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services import white_moon  # noqa: E402

SELF_QQ = os.getenv("NAPCAT_ACCOUNT", "") or os.getenv("SELF_QQ", "")


def main() -> int:
    day = sys.argv[1] if len(sys.argv) > 1 else ""
    if not day:
        import time
        day = time.strftime("%Y-%m-%d")
    p = Path("data/dialogue") / f"{day}.jsonl"
    if not p.exists():
        print(f"没有对话日志：{p}")
        return 1
    n_at = n_quote = 0
    seen = defaultdict(int)
    rows = []
    with p.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("kind") == "bot":
                continue
            rows.append(r)
    # 群名片/标签里只带 QQ 后 4 位（「阿明(9746)」）→ 先建 4 位后缀表
    suffix = {}
    name_map = {}
    for r in rows:
        q = str(r.get("qq") or "")
        if len(q) >= 4:
            suffix.setdefault(q[-4:], set()).add(q)
        if q and r.get("who"):
            name_map.setdefault(q, str(r.get("who")))
    for r in rows:
        gid = str(r.get("gid") or "")
        qq = str(r.get("qq") or "")
        who = str(r.get("who") or qq)
        text = str(r.get("text") or "")
        if not gid.startswith(tuple("0123456789")) or not qq:
            continue
        # @QQ / @名字(QQ)
        for m in re.finditer(r"@[^\s@()（）]{0,20}[（(](\d{5,12})[)）]|@(\d{5,12})", text):
            tq = m.group(1) or m.group(2) or ""
            if tq and tq != qq and tq != SELF_QQ:
                white_moon.note_incoming(
                    gid, qq, who, tq, name_map.get(tq, ""), kind="at", self_qq=SELF_QQ)
                n_at += 1
        # 〔引用 名字(9746) 16:53 的消息…〕—— 4 位后缀 → 全 QQ
        for m in re.finditer(
                r"〔引用[^〕]{0,60}?[（(](\d{4})[)）][^〕]*〕", text):
            sfx = m.group(1)
            cand = suffix.get(sfx) or set()
            if len(cand) != 1:
                continue
            tq = next(iter(cand))
            if tq and tq != qq and tq != SELF_QQ:
                white_moon.note_incoming(
                    gid, qq, who, tq, name_map.get(tq, ""), kind="quote", self_qq=SELF_QQ)
                n_quote += 1
        seen[gid] += 1
    print(f"回填完成：{day}｜@ {n_at} 条｜引用 {n_quote} 条｜群 {len(seen)} 个")
    for gid, n in sorted(seen.items(), key=lambda kv: -kv[1])[:10]:
        print(f"· {gid}: {n} 条消息")
    return 0


if __name__ == "__main__":
    sys.exit(main())
