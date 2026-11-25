#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""sample_addressed_emotion.py —— 按「冲她说」重抽情绪标注候选。

为什么重抽
----------
`build_emotion_annotation.py` 按**关键词**分层（夸/骂/关心…），
结果大量条目其实在夸别人、骂别人，impact 92% 是 0，QWK 没有方差。
这一版改成按**收件人**抽：只留 @她 / 引用她 / 喊她名字 的消息。

三种硬信号
----------
  at     @她的 QQ 或 @她的名字
  quote  〔引用 她的名字(QQ后四位) …〕——只在名字与后缀同时命中时才算
  name   正文里出现她的名字（已剥掉引用块）

⚠️ 后缀会撞车：`大肥鱼(9194)` 是她（本号 100000010），
`这是一只大肥鱼🐟(5193)` 是群友。所以 quote 必须「名字 + 后缀」双匹配。

用法
----
  python3 scripts/sample_addressed_emotion.py --dir /tmp/dlg_srv --limit 70 --write
  python3 scripts/sample_addressed_emotion.py --exclude data/eval/emotion_annotation_200_v1.jsonl

输出
----
  data/eval/emotion_addressed_candidates.jsonl   候选（label 为空）
  data/eval/emotion_addressed_candidates.md      人工/AI 复核表
  data/eval/emotion_addressed_stats.json         分层统计
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import random
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from build_emotion_annotation import clean, classify, is_noise  # noqa: E402

OUT_DIR = ROOT / "data/eval"
QUOTE_RE = re.compile(r"〔引用\s*([^〔()]{1,30})\((\d{4})\)")
MENTION_RE = re.compile(r"@([^@\s()（）]{1,20})(?:\((\d{4,11})\))?")
OTHER_FISH = "这是一只大肥鱼🐟"
# 信号权重：@ > 引用 > 只是提到名字
SIG_W = {"at": 3, "quote": 2, "name": 1}


def load_dir(path: Path) -> dict:
    """gid -> 按时间排序的记录。"""
    by = defaultdict(list)
    for p in sorted(glob.glob(str(path / "*.jsonl"))):
        day = Path(p).stem
        for ln in open(p, encoding="utf-8", errors="ignore"):
            try:
                r = json.loads(ln)
            except Exception:
                continue
            t = clean(r.get("text"))
            if not t:
                continue
            by[str(r.get("gid") or "")].append({
                "ts": float(r.get("ts") or 0), "gid": str(r.get("gid") or ""),
                "qq": str(r.get("qq") or ""), "who": clean(r.get("who") or "")[:16],
                "kind": str(r.get("kind") or ""), "text": t, "day": day,
            })
    for v in by.values():
        v.sort(key=lambda x: x["ts"])
    return by


def strip_quotes(t: str) -> str:
    """引用块一律在句尾，从「〔引用」起截掉（日志可能截断，不依赖结尾〕）。"""
    i = t.find("〔引用")
    return (t[:i] if i >= 0 else t).strip()


def _at_her(body: str, self_qqs: set, self_names: set) -> bool:
    for m in MENTION_RE.finditer(body):
        name, qq = m.group(1), (m.group(2) or "")
        if qq:                       # 带 QQ 的以 QQ 为准，避免同名群友
            if qq in self_qqs:
                return True
        elif name in self_names:
            return True
    return False


def signals(text: str, self_qqs: set, self_names: set) -> list:
    out = []
    body = strip_quotes(text)
    if _at_her(body, self_qqs, self_names):
        out.append("at")
    last4 = {q[-4:] for q in self_qqs}
    if any(m.group(1) in self_names and m.group(2) in last4
           for m in QUOTE_RE.finditer(text)):
        out.append("quote")
    rest = MENTION_RE.sub("", body).replace(OTHER_FISH, "")
    if any(n in rest for n in self_names):
        out.append("name")
    return out


def build(by: dict, self_qqs: set, self_names: set, exclude: set) -> list:
    cands, seen = [], set()
    for gid, rows in by.items():
        for i, r in enumerate(rows):
            if r["kind"] != "user":
                continue
            t = r["text"]
            sig = signals(t, self_qqs, self_names)
            if not sig:
                continue
            body = strip_quotes(t)
            core = MENTION_RE.sub("", body).replace(OTHER_FISH, "").strip()
            if is_noise(body) or len(t) > 120 or len(core) < 2:
                continue
            if "http" in body or "LLM 响应错误" in body:
                continue
            if re.fullmatch(r"[0-9a-f]{32,64}", core):   # 哈希题答案
                continue
            h = hashlib.md5(body.encode("utf-8")).hexdigest()
            if h in seen or h in exclude:
                continue
            seen.add(h)
            cands.append({
                "category": classify(body),
                "gid": gid, "ts": r["ts"], "day": r["day"],
                "speaker": {"qq": r["qq"], "name": r["who"]},
                "text": t,
                "context_before": [x["text"][:80] for x in rows[max(0, i - 3):i]],
                "context_after": [x["text"][:80] for x in rows[i + 1:i + 4]],
                "addressed": {"signals": sig, "score": max(SIG_W[s] for s in sig),
                              "body": body[:80]},
            })
    return cands


def _hint(c: dict) -> str:
    """词表粗筛极性，只用于配额，不当标签。"""
    from build_emotion_annotation import POS, NEG, CARE, APOLOGY
    b = c["addressed"]["body"]
    if any(k in b for k in APOLOGY):
        return "apo"
    if any(k in b for k in NEG):
        return "neg"
    if any(k in b for k in CARE):
        return "care"
    if any(k in b for k in POS):
        return "pos"
    return "neu"


def pick(cands: list, limit: int, rnd: random.Random) -> list:
    """带情绪的冲她说消息全收，再用中性补足——目标是方差，不是代表性。"""
    buckets = defaultdict(list)
    for c in cands:
        buckets[_hint(c)].append(c)
    for v in buckets.values():
        v.sort(key=lambda c: (-c["addressed"]["score"], c["ts"]))

    out, gid_cnt, spk_cnt = [], Counter(), Counter()

    def ok(c):
        return gid_cnt[c["gid"]] < max(8, int(limit * 0.6)) and spk_cnt[c["speaker"]["qq"]] < 15

    def take(c):
        out.append(c)
        gid_cnt[c["gid"]] += 1
        spk_cnt[c["speaker"]["qq"]] += 1

    # ① 负 / 正 / 道歉：优先全收（池子本来就小）
    for key in ("neg", "pos", "care", "apo"):
        for c in buckets.get(key, []):
            if len(out) >= limit:
                break
            if ok(c):
                take(c)
    # ② 中性补足：先强信号（@ / 引用），再名字提及
    for score in (3, 2, 1):
        pool = [c for c in buckets.get("neu", [])
                if c["addressed"]["score"] == score and c not in out]
        rnd.shuffle(pool)
        for c in pool:
            if len(out) >= limit:
                break
            if ok(c):
                take(c)
    rnd.shuffle(out)
    return out[:limit]


def write_md(items: list, path: Path) -> None:
    def esc(s, n=0):
        s = str(s or "").replace("\r", " ").replace("\n", " ⏎ ").replace("|", "｜").strip()
        return s[:n] + "…" if n and len(s) > n else s

    lines = ["# P6 情绪标注 · 冲她说重抽候选", "",
             "> 只含 @她 / 引用她 / 喊她名字 的消息；`label` 待填。",
             "> 标完用 `finalize_emotion_labels.py` 归一，再 `merge_emotion_additions.py` 合并。", "",
             "| id | 类 | 信号 | 这条 | 上文 | impact | target | 理由 |",
             "|---|---|---|---|---|---|---|---|"]
    for it in items:
        lines.append(
            f"| {it['id']} | {it['category']} | {','.join(it['addressed']['signals'])} "
            f"| {esc(it['text'], 60)} | {esc((it['context_before'] or [''])[-1], 36)} "
            f"| ☐-2 ☐-1 ☐0 ☐+1 ☐+2 | ☐speaker ☐recipient ☐other ☐world ☐self | ____ |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="按收件人重抽情绪标注候选")
    ap.add_argument("--dir", default=str(ROOT / "data/dialogue"))
    ap.add_argument("--self-qq", default="100000010,100000018")
    ap.add_argument("--self-name", default="大肥鱼,小鲸鱼")
    ap.add_argument("--exclude", default=str(ROOT / "data/eval/emotion_annotation_200_v1.jsonl"))
    ap.add_argument("--limit", type=int, default=70)
    ap.add_argument("--seed", type=int, default=20261125)
    ap.add_argument("--out", default=str(OUT_DIR / "emotion_addressed_candidates.jsonl"))
    ap.add_argument("--md", default=str(OUT_DIR / "emotion_addressed_candidates.md"))
    ap.add_argument("--stats", default=str(OUT_DIR / "emotion_addressed_stats.json"))
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    self_qqs = {x.strip() for x in args.self_qq.split(",") if x.strip()}
    self_names = {x.strip() for x in args.self_name.split(",") if x.strip()}
    exclude = set()
    ex = Path(args.exclude)
    if ex.exists():
        for line in ex.read_text(encoding="utf-8").splitlines():
            if line.strip():
                exclude.add(hashlib.md5(
                    strip_quotes(json.loads(line).get("text") or "").encode("utf-8")).hexdigest())

    by = load_dir(Path(args.dir))
    cands = build(by, self_qqs, self_names, exclude)
    print(f"语料 {sum(len(v) for v in by.values())} 条 / 候选 {len(cands)} 条")
    print("信号：", Counter(tuple(c["addressed"]["signals"]) for c in cands).most_common())
    print("预分类：", Counter(c["category"] for c in cands).most_common())

    rnd = random.Random(args.seed)
    items = pick(cands, args.limit, rnd)
    items.sort(key=lambda x: x["ts"])
    for i, it in enumerate(items, 1):
        it["id"] = f"emo_add_{i:03d}"
        it["msg_ids"] = []
        it["label"] = {"impact": None, "arousal": None, "dominance": None,
                       "target": None, "relation_relevance": None, "unexpected": None,
                       "confidence": None, "reason": "", "is_sarcasm": None}
        it["meta"] = {"labeler": "", "round": 1, "status": "draft",
                      "note": "按收件人重抽；label 待标"}
        it["origin"] = "addressed_v1_1"

    stats = {
        "corpus_dir": args.dir, "candidates": len(cands), "picked": len(items),
        "by_signal": dict(Counter("+".join(c["addressed"]["signals"])
                                  for c in items).most_common()),
        "by_category": dict(Counter(c["category"] for c in items)),
        "days": len({c["day"] for c in items}),
        "gids": len({c["gid"] for c in items}),
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    if not args.write:
        print("（未写盘；加 --write）")
        return 0
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in items) + "\n",
                   encoding="utf-8")
    write_md(items, Path(args.md))
    Path(args.stats).write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[out] {out}\n[out] {args.md}\n[out] {args.stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
