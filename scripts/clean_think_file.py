#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""clean_think_file.py —— 清掉 mood_think.json 里「判读模型补写」的假心理活动。

2026-11-25 之前 `backfill_think` 会把猜测写成 `auto=true` 存进 mood_think.json。
现在补写已改存 `mood_think_inferred.json`，但历史文件里还留着那批（实测 87/281）。

用法：
  python3 scripts/clean_think_file.py                 # 只统计，不写
  python3 scripts/clean_think_file.py --write         # 先备份再清理
"""
from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT = ROOT / "data/mood_think.json"


def main() -> int:
    ap = argparse.ArgumentParser(description="清理 mood_think.json 里的补写条目")
    ap.add_argument("--file", default=str(DEFAULT))
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    p = Path(args.file)
    if not p.exists():
        print(f"没有文件：{p}")
        return 2
    raw = json.loads(p.read_text(encoding="utf-8")) or {}
    total = sum(len(v or []) for v in raw.values())
    auto = sum(1 for v in raw.values() for x in (v or []) if x.get("auto"))
    print(f"{p}：共 {total} 条，其中 auto=true（补写）{auto} 条，保留 {total - auto} 条")
    if not args.write:
        print("（未写盘；加 --write）")
        return 0
    if not auto:
        print("没有要清理的条目")
        return 0

    bak = p.with_suffix(p.suffix + f".bak_{time.strftime('%Y%m%d_%H%M')}")
    shutil.copy2(p, bak)
    cleaned = {}
    for key, lst in raw.items():
        keep = [x for x in (lst or []) if not x.get("auto")]
        if keep:
            cleaned[key] = keep
    p.write_text(json.dumps(cleaned, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已清理 {auto} 条；备份：{bak}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
