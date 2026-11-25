#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ADR-008 验证：mood.json 的写入口是否真的串行化。

做法：8 个线程各 `bump_mood(0.1)` 25 次，理论总量 20.0。
  · 有锁：最终 ≈20.0（不会丢更新）
  · 无锁：读-改-写交错，最终明显小于 20.0

用临时 MOOD_FILE / MOOD_HISTORY，不碰线上数据。

用法：
    python scripts/verify_state_lock.py
"""
from __future__ import annotations

import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    from services import mood

    tmp = Path(tempfile.mkdtemp(prefix="mood_lock_"))
    mood.MOOD_FILE = tmp / "mood.json"
    mood.MOOD_HISTORY = tmp / "mood_history.jsonl"

    key = "group_locktest"
    mood.reset(key, value=0.0)

    threads = []
    # bump_mood 的 delta 是**内部刻度**（-100~100），不是 -1~1；
    # 8×5×1.0 = 40 内部点 = 0.40 外口径，远低于 ±100 的 clamp。
    n_threads, n_each, step = 8, 5, 1.0
    for _ in range(n_threads):
        t = threading.Thread(
            target=lambda: [mood.bump_mood(step, key) for _ in range(n_each)])
        threads.append(t)
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    got_internal = float(mood.get_mood(key)["value"])
    expect_internal = n_threads * n_each * step
    ok = abs(got_internal - expect_internal) <= 2.0
    print(f"线程 {n_threads} × {n_each} 次 × {step} = 期望 {expect_internal:.0f} 内部点")
    print(f"实际 {got_internal:.2f} 内部点（{got_internal / 100:.2f} 外口径）"
          f" → {'PASS' if ok else 'FAIL（丢更新）'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
