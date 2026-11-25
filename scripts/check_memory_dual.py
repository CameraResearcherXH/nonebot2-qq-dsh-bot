#!/usr/bin/env python3
"""双写对账：json 记忆索引 vs sqlite `memories`。

用法
----
    python3 scripts/check_memory_dual.py            # 只报告
    python3 scripts/check_memory_dual.py --strict   # sqlite 比 json 少 >10% 时退出 1

切 `MEMORY_BACKEND=sqlite` 之前，这个脚本应满足：
  · sqlite 条数 ≥ json 的 90%（回填完成）
  · 最近 200 条 json 记忆的 turn_id 在 sqlite 里都能找到（双写没漏）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services import memory_v2  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args()

    idx = memory_v2._load_index()
    active = [d for d in idx.get("items", []) if d.get("status") == "active"]
    print(f"json active = {len(active)}")

    from services import memdb
    memdb.init()
    c = memdb._conn()
    n_sql = c.execute("SELECT count(*) FROM memories").fetchone()[0]
    print(f"sqlite memories = {n_sql}")

    # 最近 200 条 json 的 turn_id 覆盖检查
    recent = [str(d.get("turn_id") or "") for d in active[-200:]
              if str(d.get("turn_id") or "")]
    missing = []
    for tid in recent:
        r = c.execute("SELECT 1 FROM memories WHERE turn_id=? LIMIT 1",
                      (tid,)).fetchone()
        if not r:
            missing.append(tid)
    print(f"最近 {len(recent)} 条 turn_id：sqlite 缺 {len(missing)}")
    if missing[:5]:
        print("  例：" + "、".join(missing[:5]))

    ok = True
    if len(active) and n_sql < len(active) * 0.9:
        print("⚠️ sqlite 条数不足 json 的 90% —— 还没回填完，别切后端")
        ok = False
    if len(recent) >= 20 and len(missing) > len(recent) * 0.1:
        print("⚠️ 最近记忆双写缺失 >10% —— 检查 [MEM] dual-write 日志")
        ok = False
    if ok:
        print("✅ 双写覆盖正常")
    return 0 if (ok or not args.strict) else 1


if __name__ == "__main__":
    raise SystemExit(main())
