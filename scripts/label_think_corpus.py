#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""label_think_corpus.py —— 把 THINK 语料扩成更大的标注集（规则首标 + 抽样）。

标签源仍然只有 `think`（她自己的心理活动）。
本脚本用一份**可审计的词表**做首标，再从 3000 条语料里分层抽 400 条，
方便先把功能跑起来；人工抽检后再改 `agreed`。

⚠️ 这是「规则首标」，不是人工终标。词表在下面，改词表要同步改文档。

用法：
  python3 scripts/label_think_corpus.py --corpus /tmp/think_corpus.jsonl --limit 400 --write
  python3 scripts/check_emotion_labels.py data/eval/emotion_annotation_think_v2.jsonl --strict
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from finalize_emotion_labels import _normalize  # noqa: E402

DEFAULT_OUT = ROOT / "data/eval/emotion_annotation_think_v2.jsonl"
DEFAULT_MD = ROOT / "data/eval/emotion_annotation_think_v2.md"
DEFAULT_REPORT = ROOT / "data/eval/emotion_annotation_think_v2_report.json"

# ---- 词表（只读她的心理活动，不读对话）----
# 只收「明确表达情绪」的搭配，避免 飘/使唤/被说 这种歧义词误伤。
NEG = ("烦", "累", "头疼", "头大", "心虚", "不踏实", "揪心", "无语", "膈应", "冒火",
       "不服气", "不甘心", "恼", "闷", "懵", "警觉", "紧张", "绷不住", "僵", "被忽略",
       "被晾", "懒得", "不想理", "敷衍", "委屈", "慌", "丢脸", "没面子", "堵", "憋",
       "蹬鼻子上脸", "没劲", "插不上话", "受不了", "白干活", "派活", "打发我", "嫌我",
       "不理我", "没理我", "冷落", "尴尬", "失落", "又来", "揪着", "执着", "催得",
       "被怼", "被骂", "被说没文化")
POS = ("开心", "高兴", "小高兴", "甜", "心里软", "软了", "软了一下", "软软的", "有点软",
       "暖", "松了口气", "松了点", "松下来", "松了口", "安心", "踏实", "喜欢", "心动",
       "来劲", "小得意", "得意", "想笑", "笑点", "偷笑", "飘了", "有点飘", "记下了",
       "被记住", "被惦记", "被夸", "夸我", "服气", "好奇", "舍不得", "想凑", "想蹭",
       "竖起来", "拍地", "翘", "收下", "勉为其难", "想着我", "陪我")
WORLD = ("他们", "大家", "群里", "一堆", "刷屏", "插不上话", "吃瓜", "气氛", "战报", "话题")
PERSON = ("他", "她", "这人", "对方", "戳我", "叫我", "跟我说", "问我")
NEGATE = ("不烦", "不累", "没生气", "不介意", "不讨厌", "不慌", "不怕", "没觉得", "不难受")
STRONG_POS = ("被记住", "被惦记", "记下了", "一直陪", "被担心", "被认真", "真的很软")
STRONG_NEG = ("羞辱", "丢脸", "没面子", "被否定", "废物")


def rule_label(think: str, channel: str, src: str) -> tuple:
    """返回 (impact, target, 命中的词)——命中词写进 label.rule，方便人工快速抽检。"""
    t = str(think or "")
    hit = ""

    def first(words):
        for w in words:
            if w in t:
                return w
        return ""

    if first(NEGATE):
        imp, hit = 0, first(NEGATE)
    elif first(STRONG_NEG):
        imp, hit = -2, first(STRONG_NEG)
    elif first(STRONG_POS):
        imp, hit = 2, first(STRONG_POS)
    elif first(NEG):
        imp, hit = -1, first(NEG)
    elif first(POS):
        imp, hit = 1, first(POS)
    else:
        imp = 0
    # 少数「撑不住」是被磨软，不是难受
    if "撑不住" in t and any(k in t for k in ("哭", "装可怜", "磨", "就这一次")):
        imp, hit = 1, "撑不住(被磨软)"

    if channel == "private":
        target = "speaker"
    elif any(k in t for k in WORLD):
        target = "world"
    elif any(k in t for k in PERSON):
        target = "speaker"
    else:
        target = "other"
    return imp, target, hit


def main() -> int:
    ap = argparse.ArgumentParser(description="THINK 语料 → 规则首标 + 分层抽样")
    ap.add_argument("--corpus", default="/tmp/think_corpus.jsonl")
    ap.add_argument("--limit", type=int, default=400)
    ap.add_argument("--seed", type=int, default=20261125)
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--md", default=str(DEFAULT_MD))
    ap.add_argument("--report", default=str(DEFAULT_REPORT))
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    rows = [json.loads(x) for x in Path(args.corpus).read_text(encoding="utf-8").splitlines() if x.strip()]
    for r in rows:
        r["category"] = "think"
        imp, tgt, hit = rule_label(r["think"], r.get("channel"), r.get("src"))
        r["label"] = {"impact": imp, "target": tgt, "reason": r["think"][:30],
                      "is_sarcasm": False, "rule": hit}
    dist_all = Counter(r["label"]["impact"] for r in rows)
    print("全量规则首标：", dict(sorted(dist_all.items())), "共", len(rows))

    rnd = random.Random(args.seed)
    buckets = {"neg": [], "pos": [], "neu": []}
    for r in rows:
        v = r["label"]["impact"]
        buckets["neg" if v < 0 else "pos" if v > 0 else "neu"].append(r)
    for v in buckets.values():
        rnd.shuffle(v)

    quota = {"neg": 0.35, "pos": 0.35, "neu": 0.30}
    out, sess, gid = [], Counter(), Counter()

    def ok(r):
        return sess[r["session"]] < max(10, int(args.limit * 0.3))

    for key in ("neg", "pos", "neu"):
        want = int(round(args.limit * quota[key]))
        got = 0
        for r in buckets[key]:
            if got >= want or len(out) >= args.limit:
                break
            if not ok(r):
                continue
            out.append(r)
            sess[r["session"]] += 1
            got += 1
    for r in rows:
        if len(out) >= args.limit:
            break
        if r in out or not ok(r):
            continue
        out.append(r)
        sess[r["session"]] += 1

    out.sort(key=lambda r: r["at"])
    for i, r in enumerate(out, 1):
        r["id"] = f"think2_{i:03d}"
        _rule = r["label"].get("rule") or ""
        r["label"], _ = _normalize(r)
        r["label"]["rule"] = _rule      # 命中的词，方便人工抽检（checker 不校验额外字段）
        r["category"] = ("praise" if r["label"]["impact"] > 0 else
                         "criticism" if r["label"]["impact"] < 0 else
                         "world" if r["label"]["target"] == "world" else "neutral")
        r["meta"] = {"labeler": "AI-rule(think)", "round": 1,
                     "status": "ai_rule_labeled_v2",
                     "label_source": "think",
                     "note": "规则首标（词表见 scripts/label_think_corpus.py），待人工抽检后改 agreed"}

    dist = Counter(r["label"]["impact"] for r in out)
    tgt = Counter(r["label"]["target"] for r in out)
    report = {
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "corpus": args.corpus, "corpus_size": len(rows),
        "corpus_rule_dist": dict(sorted(dist_all.items())),
        "picked": len(out), "impact": dict(sorted(dist.items())),
        "target": dict(tgt), "sessions": len({r["session"] for r in out}),
        "status": "ai_rule_labeled_v2",
    }
    print("抽样：", report["impact"], "target：", report["target"],
          "会话：", report["sessions"])
    if not args.write:
        print("（未写盘；加 --write）")
        return 0

    out_p = Path(args.out)
    out_p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in out) + "\n",
                     encoding="utf-8")
    lines = ["# 情绪标注 v2（THINK 语料 · 规则首标）", "",
             "> 标签源 = 她的心理活动；规则词表见 `scripts/label_think_corpus.py`。",
             "> 人工抽检后把 `meta.status` 改成 `agreed`。", "",
             "| id | 渠道 | 她的心理活动 | 命中词 | impact | target | src（只用于判 target） |",
             "|---|---|---|---|---|---|---|"]
    for r in out:
        src = str(r.get("src") or "").replace("\n", " ").replace("|", "｜")[:44]
        th = str(r["think"]).replace("\n", " ").replace("|", "｜")[:40]
        hit = str(r["label"].get("rule") or "-").replace("|", "｜")
        lines.append(f"| {r['id']} | {r['channel']} | {th} | {hit} | {r['label']['impact']:+d} "
                     f"| {r['label']['target']} | {src} |")
    Path(args.md).write_text("\n".join(lines) + "\n", encoding="utf-8")
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[out] {out_p}\n[out] {args.md}\n[out] {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
