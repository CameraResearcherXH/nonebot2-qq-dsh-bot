#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P6 情绪标注评分：impact QWK / target 准确率 / arousal kappa / ECE。

两种用法：
  1) 单人标注 vs 规则基线（auto_suggest）：
       python scripts/eval_emotion_labels.py
  2) 双人重叠集一致率（A 文件 / B 文件，按 id 对齐）：
       python scripts/eval_emotion_labels.py --a a.jsonl --b b.jsonl

标注规范见 docs/emotion-annotation-spec.md §3.2。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 当前版是「标签源=她的心理活动」；旧版 emotion_annotation_200* 已废弃
DEFAULT = ROOT / "data/eval/emotion_annotation_think_v1.jsonl"


def load(path: Path) -> dict:
    out = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        rid = str(r.get("id") or "")
        if rid:
            out[rid] = r
    return out


def _impact(row: dict, key: str):
    src = row.get(key) or {}
    try:
        v = src.get("impact")
        return None if v is None else int(v)
    except Exception:
        return None


def qwk(y1: list, y2: list, lo: int = -2, hi: int = 2) -> float:
    """二次加权 kappa（5 档有序）。"""
    n = len(y1)
    if n == 0:
        return 0.0
    k = hi - lo + 1
    obs = [[0] * k for _ in range(k)]
    for a, b in zip(y1, y2):
        obs[a - lo][b - lo] += 1
    hist1 = [sum(obs[i]) for i in range(k)]
    hist2 = [sum(obs[i][j] for i in range(k)) for j in range(k)]
    num = den = 0.0
    for i in range(k):
        for j in range(k):
            w = ((i - j) ** 2) / ((k - 1) ** 2)
            num += w * obs[i][j]
            den += w * hist1[i] * hist2[j] / n
    return 1.0 - num / den if den else 0.0


def kappa(y1: list, y2: list) -> float:
    """Cohen's kappa（无序类别）。"""
    n = len(y1)
    if n == 0:
        return 0.0
    po = sum(1 for a, b in zip(y1, y2) if a == b) / n
    c1, c2 = Counter(y1), Counter(y2)
    pe = sum(c1[k] * c2[k] for k in set(c1) | set(c2)) / (n * n)
    return (po - pe) / (1 - pe) if pe < 1 else 0.0


def ece(conf: list, correct: list, bins: int = 5) -> float:
    n = len(conf)
    if n == 0:
        return 0.0
    total = 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        idx = [i for i, c in enumerate(conf) if lo <= c < hi or (b == bins - 1 and c == 1.0)]
        if not idx:
            continue
        acc = sum(correct[i] for i in idx) / len(idx)
        avg = sum(conf[i] for i in idx) / len(idx)
        total += len(idx) / n * abs(acc - avg)
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description="P6 情绪标注评分")
    ap.add_argument("--a", default=str(DEFAULT))
    ap.add_argument("--b", default="", help="第二标注者文件（留空=和 auto_suggest 比）")
    ap.add_argument("--out", default=str(ROOT / "data/eval/emotion_label_report.md"))
    args = ap.parse_args()

    A = load(Path(args.a))
    if not A:
        print(f"没有数据：{args.a}")
        return 1
    B = load(Path(args.b)) if args.b else {}

    ids = sorted(set(A) & set(B)) if B else sorted(A)
    if not ids:
        print("两份文件没有共同 id")
        return 1

    if B:
        y_a = [_impact(A[i], "label") for i in ids]
        y_b = [_impact(B[i], "label") for i in ids]
    else:
        y_a = [_impact(A[i], "label") for i in ids]
        y_b = [_impact(A[i], "auto_suggest") for i in ids]
    pairs = [(a, b) for a, b in zip(y_a, y_b) if a is not None and b is not None]
    if not pairs:
        if not B and not any((r.get("auto_suggest") or {}).get("impact") is not None
                             for r in A.values()):
            print("该文件没有 auto_suggest（THINK 版），单人模式没有对照物。")
            print("→ 请用 --b 指定第二份标注文件（双人一致率），或先人工抽检。")
            return 2
        print(f"没有已填的 impact 标签（{len(ids)} 条记录）")
        print("→ 先按 docs/emotion-annotation-spec.md 标注，再跑本脚本。")
        return 2
    ya, yb = [x[0] for x in pairs], [x[1] for x in pairs]
    exact = sum(1 for a, b in pairs if a == b) / len(pairs)
    within1 = sum(1 for a, b in pairs if abs(a - b) <= 1) / len(pairs)

    # target / arousal（只算两边都有的）
    def _field(row, key, f):
        try:
            return (row.get(key) or {}).get(f)
        except Exception:
            return None

    # 单人模式：label vs auto_suggest；双人模式：A.label vs B.label
    def _other(row, key, field):
        return _field(row, "label" if B else "auto_suggest", field)

    tgt = [(_field(A[i], "label", "target"), _other(A[i] if not B else B[i], "label", "target"))
           for i in ids]
    tgt = [(a, b) for a, b in tgt if a and b]
    tgt_acc = sum(1 for a, b in tgt if a == b) / len(tgt) if tgt else 0.0

    ar = [(_field(A[i], "label", "arousal"), _other(A[i] if not B else B[i], "label", "arousal"))
          for i in ids]
    ar = [(a, b) for a, b in ar if a and b]
    ar_k = kappa([a for a, _ in ar], [b for _, b in ar]) if ar else 0.0
    ar_acc = sum(1 for a, b in ar if a == b) / len(ar) if ar else 0.0

    # relation MAE + confidence ECE（单人 vs auto 时才有意义）
    rel, conf, correct = [], [], []
    if not B:
        for i in ids:
            lab, auto = A[i].get("label") or {}, A[i].get("auto_suggest") or {}
            try:
                if lab.get("relation_relevance") is not None and auto.get("relation_relevance") is not None:
                    rel.append(abs(float(lab["relation_relevance"]) - float(auto["relation_relevance"])))
            except Exception:
                pass
            try:
                if lab.get("confidence") is not None and lab.get("impact") is not None:
                    conf.append(float(lab["confidence"]))
                    correct.append(1 if int(lab["impact"]) == int(auto.get("impact", 99)) else 0)
            except Exception:
                pass

    lines = ["# 情绪标注评分报告", "",
             f"- 数据：`{args.a}`" + (f" vs `{args.b}`" if B else "（vs auto_suggest）"),
             f"- 共同条目：{len(ids)}｜已填 impact：{len(pairs)}"]
    if not B:
        lines += ["- ⚠️ 单人模式对照的是 `auto_suggest` **规则基线**，不是人工真值；"
                  "下表的一致率≠准确率，只看「模型和关键词规则差多远」。"]
    lines += ["", "| 指标 | 值 | 目标 |", "|---|---|---|",
              f"| impact QWK | {qwk(ya, yb):.3f} | ≥0.60 |",
              f"| impact 完全一致率 | {exact * 100:.1f}% | ≥50% |",
              f"| impact ±1 一致率 | {within1 * 100:.1f}% | ≥85% |",
              f"| target 一致率 | {tgt_acc * 100:.1f}% | ≥80% |",
              f"| arousal 一致率 | {ar_acc * 100:.1f}% | ≥60% |",
              f"| arousal kappa | {ar_k:.3f} | — |"]
    if rel:
        lines.append(f"| relation_relevance MAE | {sum(rel) / len(rel):.3f} | 越低越好 |")
    # ECE 需要「人工真值」才算得出来；拿规则基线当正确性会把指标算反，故不输出。
    if conf:
        lines.append("| confidence ECE | 待人工冻结 v1 后计算 | ≤0.15 |")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\n[out] {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
