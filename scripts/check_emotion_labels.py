#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""check_emotion_labels.py —— 校验 P6 情绪标注文件是否符合规范。

规范见 docs/emotion-annotation-spec.md §2；本脚本只做「格式 + 取值 + 退化」检查，
不判断标得对不对（那是人和 eval_emotion_labels.py 的事）。

用法：
  python3 scripts/check_emotion_labels.py data/eval/emotion_annotation_200_v1.jsonl
  python3 scripts/check_emotion_labels.py --strict          # warning 也算 FAIL
  python3 scripts/check_emotion_labels.py --json            # 机器可读

退出码：0 通过；1 有硬错误（或 --strict 下有 warning）；2 文件读不到。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT = ROOT / "data/eval/emotion_annotation_200.jsonl"

CATEGORIES = {"praise", "care", "criticism", "ignore", "tease",
              "world", "apology", "neutral", "hard"}
IMPACTS = {-2, -1, 0, 1, 2}
LEVELS = {"low", "mid", "high"}
TARGETS = {"speaker", "recipient", "other", "world", "self"}
GRADES = {0.0, 0.5, 1.0}          # relation_relevance / unexpected 只允许这三档
REQUIRED_LABEL = ("impact", "arousal", "dominance", "target",
                  "relation_relevance", "unexpected", "confidence",
                  "reason", "is_sarcasm")


def load(path: Path) -> tuple[list, list]:
    rows, errors = [], []
    if not path.exists():
        return rows, [f"文件不存在：{path}"]
    for n, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception as exc:
            errors.append(f"第 {n} 行不是合法 JSON：{exc}")
    return rows, errors


def _num(v):
    try:
        return float(v)
    except Exception:
        return None


def check(rows: list) -> tuple[list, list]:
    errors, warns = [], []
    ids = Counter(str(r.get("id") or "") for r in rows)
    for rid, n in ids.items():
        if not rid:
            errors.append("存在空 id")
        elif n > 1:
            errors.append(f"id 重复：{rid} × {n}")

    for r in rows:
        rid = str(r.get("id") or "?")
        cat = str(r.get("category") or "")
        if cat not in CATEGORIES:
            errors.append(f"{rid}: category 不认识 `{cat}`")
        lab = r.get("label")
        if not isinstance(lab, dict):
            errors.append(f"{rid}: 缺少 label")
            continue
        for k in REQUIRED_LABEL:
            if k not in lab:
                errors.append(f"{rid}: label 缺字段 `{k}`")
        if lab.get("impact") is not None and lab.get("impact") not in IMPACTS:
            errors.append(f"{rid}: impact={lab.get('impact')} 不在 -2..2")
        for k in ("arousal", "dominance"):
            if lab.get(k) is not None and lab.get(k) not in LEVELS:
                errors.append(f"{rid}: {k}={lab.get(k)} 不在 low/mid/high")
        if lab.get("target") is not None and lab.get("target") not in TARGETS:
            errors.append(f"{rid}: target={lab.get('target')} 不在 5 值枚举")
        for k in ("relation_relevance", "unexpected"):
            v = _num(lab.get(k))
            if v is not None and v not in GRADES:
                errors.append(f"{rid}: {k}={lab.get(k)} 只允许 0 / 0.5 / 1")
        c = _num(lab.get("confidence"))
        if c is None or not 0.0 <= c <= 1.0:
            errors.append(f"{rid}: confidence={lab.get('confidence')} 不在 0~1")
        reason = str(lab.get("reason") or "")
        if not reason.strip():
            warns.append(f"{rid}: reason 为空")
        elif len(reason) > 30:
            warns.append(f"{rid}: reason {len(reason)} 字 > 30")
        if not isinstance(lab.get("is_sarcasm"), bool):
            errors.append(f"{rid}: is_sarcasm 必须是 true/false")

    # 退化检查：某一档占比过高时，QWK/ECE 都失去意义
    def _flat(field, getter, limit=0.90):
        vals = [getter(r) for r in rows if isinstance(r.get("label"), dict)]
        vals = [v for v in vals if v is not None]
        if len(vals) < 20:
            return
        top, n = Counter(vals).most_common(1)[0]
        if n / len(vals) >= limit:
            warns.append(f"{field} 退化：{top} 占 {n}/{len(vals)}（≥{int(limit * 100)}%）")

    _flat("impact", lambda r: r["label"].get("impact"))
    _flat("arousal", lambda r: r["label"].get("arousal"))
    # dominance / unexpected 不查退化：它们是由 impact 派生的（dominance 只在 impact<0 时 low），
    # 分布天然跟着 impact 走；真实群聊里例行消息占多数，0 也正常。
    # 反讽是否漏标由下面 hard 类的 is_sarcasm 检查兜。

    confs = [_num(r["label"].get("confidence")) for r in rows if isinstance(r.get("label"), dict)]
    confs = [c for c in confs if c is not None]
    if confs and sum(confs) / len(confs) >= 0.95:
        warns.append(f"confidence 均值 {sum(confs) / len(confs):.2f}，接近全 1.0，校准会失效")

    hard = [r for r in rows if r.get("category") == "hard" and isinstance(r.get("label"), dict)]
    if len(hard) >= 5 and not any(r["label"].get("is_sarcasm") for r in hard):
        warns.append("hard 类没有一条 is_sarcasm=true，反讽标记可能没打")
    return errors, warns


def stats(rows: list) -> dict:
    def _c(field):
        return dict(sorted(Counter(
            r["label"].get(field) for r in rows
            if isinstance(r.get("label"), dict)).items(),
            key=lambda kv: (kv[0] is None, kv[0])))

    by_cat = {}
    for cat in sorted({r.get("category") for r in rows}):
        by_cat[cat] = dict(sorted(Counter(
            r["label"].get("impact") for r in rows
            if r.get("category") == cat and isinstance(r.get("label"), dict)
        ).items(), key=lambda kv: (kv[0] is None, kv[0])))
    return {"total": len(rows), "impact": _c("impact"), "arousal": _c("arousal"),
            "dominance": _c("dominance"), "target": _c("target"),
            "relation_relevance": _c("relation_relevance"),
            "unexpected": _c("unexpected"), "by_category": by_cat}


def main() -> int:
    ap = argparse.ArgumentParser(description="P6 情绪标注格式/取值校验")
    ap.add_argument("path", nargs="?", default=str(DEFAULT))
    ap.add_argument("--strict", action="store_true", help="warning 也算失败")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    path = Path(args.path)
    rows, errors = load(path)
    if not rows and errors:
        print("\n".join(errors))
        return 2
    e, w = check(rows)
    errors += e
    st = stats(rows)

    if args.json:
        print(json.dumps({"path": str(path), "errors": errors, "warnings": w,
                          "stats": st}, ensure_ascii=False, indent=2))
    else:
        print(f"文件：{path}\n条目：{st['total']}")
        print(f"impact：{st['impact']}")
        print(f"arousal：{st['arousal']}｜dominance：{st['dominance']}")
        print(f"target：{st['target']}")
        print(f"relation_relevance：{st['relation_relevance']}｜unexpected：{st['unexpected']}")
        print("分类 × impact：")
        for cat, dist in st["by_category"].items():
            print(f"  {str(cat or '-'):10} {dist}")
        if errors:
            print(f"\n硬错误 {len(errors)} 条：")
            for x in errors[:40]:
                print("  ✗", x)
            if len(errors) > 40:
                print(f"  … 另有 {len(errors) - 40} 条")
        if w:
            print(f"\n警告 {len(w)} 条：")
            for x in w[:40]:
                print("  !", x)
            if len(w) > 40:
                print(f"  … 另有 {len(w) - 40} 条")
        if not errors and not w:
            print("\n✓ 全部通过")

    if errors or (args.strict and w):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
