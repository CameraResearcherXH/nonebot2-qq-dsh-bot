#!/usr/bin/env python3
"""回填 retrieval §8 的三张表：profiles / style_samples（threads 由运行时写入）。

用法
----
    python3 scripts/backfill_retrieval_tables.py            # dry-run
    python3 scripts/backfill_retrieval_tables.py --write    # 真写

说明
----
- `profiles`：扫 `data/profiles/*_profile.json`（name/affinity）+ `profiles.intimacy()`
- `style_samples`：扫 `data/style_corpus/<gid>/<qq>.jsonl`，每人最多取最近 50 条
- `threads`：没有历史落盘（group_scene 是内存态），从本次启动开始由运行时写入
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services import memdb  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--limit", type=int, default=50)
    args = ap.parse_args()

    memdb.init()
    n_prof = n_samp = 0

    # ① profiles
    try:
        from services import profiles
    except Exception:
        profiles = None
    for f in sorted(Path("data/profiles").glob("*_profile.json")):
        qq = f.name.split("_", 1)[0]
        if not qq.isdigit():
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        name = str(d.get("name") or d.get("call_name") or "")[:40]
        aff = float(d.get("affinity") or 0.0)
        intim = 0.0
        if profiles is not None:
            try:
                intim = float(profiles.intimacy(qq))
            except Exception:
                intim = 0.0
        if args.write:
            memdb.upsert_profile(qq=qq, name=name, affinity=aff, intimacy=intim)
        n_prof += 1

    # ② style_samples
    for f in sorted(Path("data/style_corpus").glob("*/*.jsonl")):
        gid = f.parent.name
        qq = f.stem
        if not qq.isdigit():
            continue
        rows = []
        try:
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines()[-args.limit:]:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                t = str(r.get("t") or "").strip()
                if t:
                    rows.append((t, float(r.get("at") or 0)))
        except Exception:
            continue
        for t, ts in rows:
            if args.write:
                memdb.add_style_sample(
                    qq=qq, gid=gid, text=t,
                    quality=(0.6 if 6 <= len(t) <= 40 else 0.4), ts=ts)
            n_samp += 1

    c = memdb._conn()
    print(f"profiles  {n_prof} 条 → 表内 {c.execute('select count(*) from profiles').fetchone()[0]}")
    print(f"samples   {n_samp} 条 → 表内 {c.execute('select count(*) from style_samples').fetchone()[0]}")
    print(f"threads   运行时写入 → 表内 {c.execute('select count(*) from threads').fetchone()[0]}")
    if not args.write:
        print("（dry-run：加 --write 才真正写入）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
