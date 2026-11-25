#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""p95 性能周报（DESIGN §8：每周只看最慢一块）。

用法：
    python scripts/p95_report.py                 # 最近 7 天，打印
    python scripts/p95_report.py --days 14
    python scripts/p95_report.py --write         # 同时写 data/weekly/p95_YYYY-MM-DD.md

数据来源：
  · data/metrics/pipeline_stages.jsonl（reply_pipeline 每轮各步耗时）
  · data/llm_usage.db（每次模型调用 latency_ms）
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)


def main() -> int:
    ap = argparse.ArgumentParser(description="p95 性能周报")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--write", action="store_true", help="写到 data/weekly/")
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    from services import perf_stats

    text = perf_stats.render_text(a.days)
    print(text)

    if a.write or a.out:
        md = perf_stats.render_md(a.days)
        out = Path(a.out) if a.out else (
            ROOT / "data" / "weekly" / f"p95_{time.strftime('%Y-%m-%d')}.md")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md + "\n", encoding="utf-8")
        print(f"\n[out] {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
