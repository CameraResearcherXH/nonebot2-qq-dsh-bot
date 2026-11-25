#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""位置评测报告：gold 放头/中/尾，看准确率差多少（Lost in the Middle）。

前置：
    python scripts/eval_gold.py --stage position
    python scripts/eval_replay.py run --set data/eval/position_replay.jsonl \
        --deterministic --out data/eval/position_replay_out.jsonl
    python scripts/eval_replay.py judge --set data/eval/position_replay_out.jsonl \
        --out data/eval/position_judged.jsonl

用法：
    python scripts/position_report.py --set data/eval/position_judged.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(path: Path) -> list:
    out = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def _hit(rec: dict) -> bool:
    """命中判定：关键词出现在可见回复里，或 judge 相关性 ≥4。"""
    visible = str(rec.get("visible") or rec.get("reply") or "")
    kws = [str(x) for x in (rec.get("expected_keywords") or []) if str(x).strip()]
    if kws and any(k in visible for k in kws):
        return True
    j = rec.get("judge") or {}
    try:
        return float(j.get("relevant") or 0) >= 4.0
    except Exception:
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="gold 位置评测报告")
    ap.add_argument("--set", default=str(ROOT / "data/eval/position_judged.jsonl"))
    ap.add_argument("--out", default=str(ROOT / "data/eval/position_report.md"))
    a = ap.parse_args()

    rows = _load(Path(a.set))
    if not rows:
        print(f"没有数据：{a.set}（先跑 eval_gold --stage position → eval_replay）")
        return 1

    by = defaultdict(list)
    for r in rows:
        # 老版 eval_replay 会丢 variant；id 形如 gq_001-head，从后缀兜底
        v = str(r.get("variant") or "").strip()
        if not v:
            v = str(r.get("id") or "").rsplit("-", 1)[-1]
        by[v or "?"].append(r)

    acc = {}
    for v, rs in by.items():
        acc[v] = sum(1 for x in rs if _hit(x)) / max(1, len(rs))

    head, mid, tail = acc.get("head", 0.0), acc.get("mid", 0.0), acc.get("tail", 0.0)
    # 目标：中 vs 尾差 <10%（绝对百分点）
    mid_tail_gap = abs(mid - tail) * 100
    head_tail_gap = abs(head - tail) * 100
    ok = mid_tail_gap < 10.0

    lines = ["# 位置评测报告（gold 头/中/尾）", "",
             f"- 样本：{len(rows)} 条（{len(by)} 个位置）", "",
             "| 位置 | n | 准确率 |", "|---|---|---|"]
    for v in ("head", "mid", "tail"):
        if v in acc:
            lines.append(f"| {v} | {len(by[v])} | {acc[v] * 100:.1f}% |")
    lines += ["",
              f"- `lost_in_middle`（中 vs 尾差）：**{mid_tail_gap:.1f} 个百分点**"
              f" → {'PASS' if ok else 'FAIL'}（目标 <10）",
              f"- 头 vs 尾差：{head_tail_gap:.1f} 个百分点", ""]
    if not ok:
        lines.append("→ 中间位置明显更差：证据应按相关度升序，最相关紧贴当前消息。")
    else:
        lines.append("→ 位置偏差在可接受范围；继续保证「最相关放最后」。")

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\n[out] {out}")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
