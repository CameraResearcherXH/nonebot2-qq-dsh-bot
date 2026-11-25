#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""finalize_emotion_think.py —— 把「她的心理活动」候选标成 v1。

标签源 = `think`（她自己写的 [[THINK|…]]），**不是**对话。
`src` 只用来判 target（这件事关于谁）。

字段口径与 `finalize_emotion_labels._normalize` 一致：
  impact/arousal/dominance/target/relation_relevance/unexpected/confidence
  其中 arousal/dominance/relation/unexpected/confidence 由 impact+target 派生。

reason 直接引用她的心理活动（≤30 字）——这是最忠实的「依据」。

用法
----
  python3 scripts/build_emotion_annotation_think.py --write
  python3 scripts/finalize_emotion_think.py --write
  python3 scripts/check_emotion_labels.py data/eval/emotion_annotation_think_v1.jsonl --strict
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from finalize_emotion_labels import _normalize  # noqa: E402

DEFAULT_IN = ROOT / "data/eval/emotion_think_candidates.jsonl"
DEFAULT_OUT = ROOT / "data/eval/emotion_annotation_think_v1.jsonl"
DEFAULT_MD = ROOT / "data/eval/emotion_annotation_think_v1.md"
DEFAULT_REPORT = ROOT / "data/eval/emotion_annotation_think_v1_report.json"

# id impact target —— 逐条从 THINK 判；target 由 src 判断
_RAW = """
001 -1 self
002 -1 speaker
003 -1 speaker
004 -1 speaker
005 -1 speaker
006 -1 speaker
007 -1 speaker
008 -1 speaker
009 0 speaker
010 -1 speaker
011 0 other
012 0 other
013 0 world
014 -1 speaker
015 0 other
016 1 speaker
017 0 other
018 -1 speaker
019 -1 speaker
020 -1 speaker
021 -1 speaker
022 1 speaker
023 2 speaker
024 0 speaker
025 -1 speaker
026 -1 speaker
027 0 speaker
028 0 speaker
029 1 speaker
030 0 speaker
031 1 speaker
032 1 speaker
033 1 speaker
034 1 speaker
035 1 speaker
036 1 speaker
037 1 speaker
038 0 speaker
039 0 speaker
040 0 speaker
041 1 speaker
042 0 speaker
043 1 speaker
044 1 speaker
045 1 speaker
046 1 speaker
047 0 speaker
048 -1 speaker
049 0 speaker
050 0 speaker
051 0 speaker
052 1 speaker
053 -1 speaker
054 -1 speaker
055 -1 speaker
056 0 speaker
057 0 speaker
058 0 speaker
059 0 world
060 0 speaker
061 0 world
062 0 world
063 0 world
064 -1 speaker
065 0 other
066 -1 speaker
067 0 world
068 -1 speaker
069 -1 speaker
070 0 other
071 1 speaker
072 1 speaker
073 0 world
074 0 world
075 0 world
076 0 world
077 0 speaker
078 1 speaker
079 1 speaker
080 -1 world
081 1 speaker
082 -1 speaker
083 1 speaker
084 1 speaker
085 -1 speaker
086 -1 speaker
087 0 world
088 -1 speaker
089 1 other
090 1 speaker
091 1 speaker
092 -1 speaker
093 0 speaker
094 -1 speaker
095 0 world
096 1 speaker
097 -1 self
098 0 other
099 1 speaker
100 -1 speaker
101 -1 speaker
102 0 speaker
103 0 speaker
104 0 speaker
105 -1 speaker
106 -1 world
107 0 other
108 1 other
109 -1 speaker
110 -1 speaker
111 -1 speaker
112 0 speaker
113 1 speaker
114 1 speaker
115 0 speaker
116 0 speaker
117 1 speaker
118 1 speaker
119 0 world
120 0 world
121 1 speaker
122 1 speaker
123 1 speaker
124 1 speaker
125 0 world
126 0 world
127 0 other
128 1 other
129 -1 speaker
130 0 other
131 -1 self
132 -1 speaker
133 0 other
134 -1 speaker
135 -1 speaker
136 1 speaker
137 -1 world
138 1 other
139 0 world
140 1 speaker
141 0 speaker
142 0 speaker
143 0 speaker
144 -1 speaker
145 -1 speaker
146 0 speaker
147 0 speaker
148 0 speaker
149 0 speaker
150 0 speaker
151 1 speaker
152 1 speaker
153 1 speaker
154 1 speaker
155 -1 speaker
156 1 speaker
157 -1 speaker
158 0 speaker
159 -1 speaker
160 -1 speaker
161 -1 speaker
162 -1 speaker
163 1 speaker
164 1 speaker
165 2 speaker
166 1 speaker
167 -1 world
168 0 speaker
169 0 speaker
170 0 speaker
171 1 speaker
172 1 speaker
173 0 other
174 1 speaker
175 0 speaker
176 1 speaker
177 -1 speaker
178 -1 speaker
179 0 speaker
180 1 speaker
181 1 speaker
182 -1 speaker
183 1 speaker
184 1 speaker
185 1 speaker
186 1 speaker
187 0 speaker
188 1 speaker
189 1 speaker
190 -1 speaker
"""

LABELS = {}
for _line in _RAW.strip().splitlines():
    _n, _imp, _tgt = _line.split()
    LABELS[f"ethink_{_n}"] = (int(_imp), _tgt)


def _esc(s, n=0):
    s = str(s or "").replace("\r", " ").replace("\n", " ⏎ ").replace("|", "｜").strip()
    return s[:n] + "…" if n and len(s) > n else s


def write_md(rows: list, path: Path) -> None:
    lines = ["# 情绪标注 · v1（标签源 = 她自己的心理活动）", "",
             "> `impact` 只看「她的心理活动」；「她在回什么」只用来判 `target`。",
             "> `judge` 是线上 mood_judge 的参考输出，**不是标签**。", "",
             "| id | 渠道 | 她的心理活动（标签源） | 她在回什么（src） | judge 参考 | impact | target |",
             "|---|---|---|---|---|---|---|"]
    for r in rows:
        j = r.get("judge") or {}
        ref = f"{j.get('emotion') or '-'} / {j.get('delta')}" if j else "-"
        lines.append(
            f"| {r['id']} | {r['channel']} | {_esc(r['think'], 40)} | {_esc(r['src'], 40)} "
            f"| {_esc(ref, 16)} | {r['label']['impact']:+d} | {r['label']['target']} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="心理活动标注 → v1")
    ap.add_argument("--in", dest="inp", default=str(DEFAULT_IN))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--md", default=str(DEFAULT_MD))
    ap.add_argument("--report", default=str(DEFAULT_REPORT))
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    rows = [json.loads(x) for x in Path(args.inp).read_text(encoding="utf-8").splitlines() if x.strip()]
    missing = [r["id"] for r in rows if r["id"] not in LABELS]
    if missing:
        print("缺标签：", missing[:10], "…" if len(missing) > 10 else "")

    for r in rows:
        spec = LABELS.get(r["id"])
        if not spec:
            continue
        impact, target = spec
        # 这批不是按关键词分层抽的，category 只为分组统计：正→praise，负→criticism
        r["category"] = ("praise" if impact > 0 else
                         "criticism" if impact < 0 else
                         "world" if target == "world" else "neutral")
        r["label"] = {"impact": impact, "target": target,
                      "reason": r["think"][:30], "is_sarcasm": False}
        r["label"], _ = _normalize(r)
        r["meta"] = {"labeler": "AI-review(think)", "round": 1,
                     "status": "ai_reviewed_think_v1",
                     "label_source": "think",
                     "note": "标签源=她的[[THINK]]；src 只用于判 target；人工抽检后改 agreed",
                     "reviewed_at": "2026-11-25"}

    dist = Counter(r["label"]["impact"] for r in rows if r["id"] in LABELS)
    by_tgt = Counter(r["label"]["target"] for r in rows if r["id"] in LABELS)
    report = {
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "data/mood_think.json (THINK)",
        "total": len(rows), "labeled": len(LABELS),
        "impact": dict(sorted(dist.items())),
        "target": dict(by_tgt),
        "nonzero": sum(n for k, n in dist.items() if k),
        "status": "ai_reviewed_think_v1",
    }
    print(f"标注 {len(LABELS)} / {len(rows)} 条")
    print("impact：", report["impact"], "非零：", report["nonzero"])
    print("target：", report["target"])
    if not args.write:
        print("（未写盘；加 --write）")
        return 0

    out, md, rep = Path(args.out), Path(args.md), Path(args.report)
    out.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                   encoding="utf-8")
    write_md(rows, md)
    rep.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[out] {out}\n[out] {md}\n[out] {rep}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
