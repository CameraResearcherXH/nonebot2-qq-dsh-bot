#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""核查「判读模型到底看到了什么、她的心理活动是什么类型」。

用途（用户 2026-11-25）：
  「核查下标签 llm 标记内容和上下文以及标签算法，看她读到和理解到的内容是否正确。」

两种数据源：
  --think  data/mood_think.json     她的心理活动（text）+ 她在回什么（src）+ 她回的（reply）
  --trace  data/mood_judge_trace.jsonl  判读的真实输入 prompt + 原始回复（2026-11-25 起留痕）

产出：
  · 终端摘要：THINK 命中、src/reply 缺失率、THINK 类型分布
  · markdown 复核表：她的心理活动 / 她在回什么 / 她回的 / 判读输入预览 / 判读输出

THINK 类型粗分（只为人工抽检，**不是**标签）：
  emo    命中情绪词（明确在说自己的感受）
  plan   计划/决策（「记得」「盯着」「引导」「安慰」…）
  watch  观察/疑问（「这是不是」「好像」「图上」…）
  nohit  词表没命中 —— **不等于没情绪**：抽检发现大量「他这么软，我反倒不知道该硬哪」
         这类真实情绪不在词表里。所以 nohit 只能说明「关键词标不出来」，
         这正是要用语义模型（而不是词表）做标注的理由。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 情绪词表直接复用标注器那份（NEG / POS / STRONG / NEGATE），避免两套词表打架。
try:
    from label_think_corpus import (NEG, POS, STRONG_NEG, STRONG_POS,
                                    NEGATE)  # type: ignore
    EMO = tuple(NEG) + tuple(POS) + tuple(STRONG_NEG) + tuple(STRONG_POS) + tuple(NEGATE)
except Exception:                                     # 独立运行时兜底
    EMO = ("烦", "累", "心虚", "不服", "委屈", "恼", "闷", "懵", "慌",
           "开心", "高兴", "软", "暖", "得意", "想笑", "记下了", "被记住",
           "被夸", "舍不得", "受用", "窘", "失落", "堵", "憋", "服气",
           "懒得", "又来", "嫌我", "执着", "揪着", "插不上话", "被怼",
           "紧张", "不想理", "甜", "飘", "安心")
# 计划/决策：只认强特征，别用「得 / 要 / 先 / 再」这种到处都有的字
PLAN = ("先当", "先别急", "记得", "盯着", "引导", "安慰", "怎么跟", "让她自己",
        "多给几句", "接住", "不硬问", "回头再说", "下次", "等会", "帮她", "提醒")
WATCH = ("这是不是", "看起来", "好像", "估计", "图上", "可能", "要不要")


def kind_of(text: str) -> str:
    t = str(text or "")
    if any(k in t for k in EMO):
        return "emo"
    if any(k in t for k in PLAN):
        return "plan"
    if any(k in t for k in WATCH):
        return "watch"
    return "nohit"


def load_think(path: Path) -> list:
    d = json.loads(path.read_text(encoding="utf-8")) or {}
    out = []
    for sess, items in d.items():
        for it in items or []:
            if it.get("auto"):
                continue
            out.append({"session": sess, **it})
    out.sort(key=lambda r: float(r.get("at") or 0))
    return out


def load_trace(path: Path) -> list:
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def esc(s, n=0) -> str:
    s = str(s or "").replace("\r", " ").replace("\n", " ⏎ ").replace("|", "｜").strip()
    return s[:n] + "…" if n and len(s) > n else s


def main() -> int:
    ap = argparse.ArgumentParser(description="核查判读输入/心理活动类型")
    ap.add_argument("--think", default="data/mood_think.json")
    ap.add_argument("--trace", default="data/mood_judge_trace.jsonl")
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--out", default="data/eval/mood_judge_audit.md")
    args = ap.parse_args()

    items = load_think(Path(args.think))
    traces = load_trace(Path(args.trace))

    kinds = Counter(kind_of(x.get("text")) for x in items)
    no_src = [x for x in items if not str(x.get("src") or "").strip()]
    no_reply = [x for x in items if not str(x.get("reply") or "").strip()]
    short = [x for x in items if len(str(x.get("text") or "")) < 6]

    print(f"心理活动 {len(items)} 条（auto=False）")
    print(f"  THINK 类型：{dict(kinds)}")
    print(f"  src 缺失 {len(no_src)} 条｜reply 缺失 {len(no_reply)} 条｜<6 字 {len(short)} 条")
    print(f"  判读留痕 {len(traces)} 条（{args.trace}）")

    lines = ["# 判读输入 / 心理活动 核查表", "",
             f"- 心理活动：{len(items)} 条（`{args.think}`，auto=False）",
             f"- THINK 类型：{dict(kinds)}",
             f"- src 缺失 {len(no_src)}｜reply 缺失 {len(no_reply)}｜<6 字 {len(short)}",
             f"- 判读留痕：{len(traces)} 条（`{args.trace}`）",
             "",
             "> 类型是**粗分**，只为人工抽检：「plan / watch / nohit」这类不是情绪反应，",
             "> 标成 impact 0 才对；如果被标成 ±1，说明标签算法在给「计划」算情绪。", "",
             "## A. 心理活动（标签源）", "",
             "| # | 类型 | 她的心理活动 | 她这条在回 | 她回的 |",
             "|---|---|---|---|---|"]
    for i, x in enumerate(items[-args.limit:], 1):
        lines.append(f"| {i} | {kind_of(x.get('text'))} | {esc(x.get('text'), 40)} "
                     f"| {esc(x.get('src'), 40)} | {esc(x.get('reply'), 36)} |")

    if traces:
        lines += ["", "## B. 判读真实输入/输出（最近 %d 条）" % min(len(traces), args.limit), ""]
        for t in traces[-args.limit:]:
            lines += ["```text", str(t.get("prompt") or "")[:1200],
                      "---- 判读输出 ----", str(t.get("raw") or "")[:400], "```", ""]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"报告：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
