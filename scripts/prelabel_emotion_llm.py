#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P6 情绪标注「AI 预标」：用独立模型先过一遍，人工再 check。

为什么用独立模型：
  · 不能拿 mood_judge 自己标自己（会自洽，校准失效）；
  · 这里默认用 GLM（`services.glm`），和 DeepSeek 判读不同源。

盲标：**不把 auto_suggest / category 给模型**，只给原文 + 前后文 + 说话人。

输出：
  data/eval/emotion_annotation_200_ai.jsonl
  · label 填好；meta.labeler="AI-precheck(glm)"，status="ai_precheck"
  · 原文件不动，人工 check 后再合并/改 labeler

用法（容器内）：
  python scripts/prelabel_emotion_llm.py --write
  python scripts/prelabel_emotion_llm.py --limit 20 --write   # 先试 20 条
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEFAULT_IN = ROOT / "data/eval/emotion_annotation_200.jsonl"
DEFAULT_OUT = ROOT / "data/eval/emotion_annotation_200_ai.jsonl"

_SYS = """你是 QQ 群聊情绪标注员。你要判断：**如果机器人「小鲸鱼」看到这条消息，她的情绪会怎么变**。
不要假设每条都在对她说；群里大部分闲聊对她没有情绪影响。

只输出 JSON 数组，不要解释。每条格式：
{"id":"emo_001","label":{
  "impact": -2|-1|0|1|2,
  "arousal": "low|mid|high",
  "dominance": "low|mid|high",
  "target": "speaker|recipient|other|world|self",
  "relation_relevance": 0|0.5|1,
  "unexpected": 0|0.5|1,
  "confidence": 0.0~1.0,
  "reason": "20字内，写发生了什么，不写心理活动",
  "is_sarcasm": true|false
}}

判定要点：
- impact：这条消息让她心情变好/变坏多少。0=基本无感；±1=轻微；±2=强烈。
  群友之间普通聊天、游戏讨论、别人互怼 → 0（哪怕带脏字，只要不是冲她）。
  明确夸她/关心她/向她道歉 → +1；特别真诚/重要 → +2。
  明确骂她/羞辱她/冷落她 → -1；严重辱骂/威胁 → -2。
  调侃她：看语气，轻微 0，明显恶意 -1。
- target：情绪冲谁来的。对她说=speaker；她看到 A 对 B 说=recipient/other；
  群氛围/话题=world；她自己的事=self。群友自述（「我是傻逼」）算 speaker，不是 self。
- relation_relevance：**只能 0 / 0.5 / 1**。world/self/别人 → 0；
  直接针对她 → 1；间接相关（她在意的人、群氛围）→ 0.5。
- unexpected：**只能 0 / 0.5 / 1**。例行消息 0；反讽/突然翻脸/意外 0.5；
  极罕见 1。日常寒暄不要给 0.2 这种中间值。
- confidence：你有多确定。反讽/歧义/上下文不足 → 0.4~0.6；清楚 → 0.8~0.9。
  **不要写 1.0**。
- is_sarcasm：呵呵/真棒呢/你可真行/阴阳怪气 → true；
  直接爆粗（「傻逼」「滚」）是骂人，不是反讽 → false。
- reason：每条都要写，20 字内，写事件（「当面夸她」「骂的是游戏角色」）。
"""


def _fallback(row: dict) -> dict:
    return {"impact": 0, "arousal": "low", "dominance": "mid",
            "target": "world", "relation_relevance": 0.0,
            "unexpected": 0.0, "confidence": 0.4,
            "reason": "预标失败，人工判", "is_sarcasm": False}


def _clip_int(v, lo=-2, hi=2):
    try:
        return max(lo, min(hi, int(v)))
    except Exception:
        return 0


_GRADES = (0.0, 0.5, 1.0)


def _snap(v) -> float:
    """relation_relevance / unexpected 规范只允许 0 / 0.5 / 1。"""
    try:
        x = max(0.0, min(1.0, float(v)))
    except Exception:
        return 0.0
    return min(_GRADES, key=lambda g: abs(g - x))


def _norm_label(lab: dict) -> dict:
    out = _fallback({})
    out["impact"] = _clip_int(lab.get("impact"))
    for k in ("arousal", "dominance"):
        v = str(lab.get(k) or "").lower()
        out[k] = v if v in ("low", "mid", "high") else out[k]
    t = str(lab.get("target") or "").lower()
    out["target"] = t if t in ("speaker", "recipient", "other", "world", "self") else "world"
    out["relation_relevance"] = _snap(lab.get("relation_relevance"))
    out["unexpected"] = _snap(lab.get("unexpected"))
    try:
        out["confidence"] = round(min(0.9, max(0.0, float(lab.get("confidence")))), 2)
    except Exception:
        pass
    out["reason"] = str(lab.get("reason") or "").strip()[:20] or "预标未给理由"
    out["is_sarcasm"] = bool(lab.get("is_sarcasm"))
    return out


def _item_text(r: dict) -> str:
    before = " / ".join(str(x)[:60] for x in (r.get("context_before") or [])[-4:])
    after = " / ".join(str(x)[:60] for x in (r.get("context_after") or [])[:2])
    who = (r.get("speaker") or {}).get("name") or (r.get("speaker") or {}).get("qq") or "?"
    return (f"id: {r['id']}\n说话人: {who}\n上文: {before}\n"
            f"这条: {str(r.get('text') or '')[:300]}\n下文: {after}")


async def _label_batch(batch: list, sem: asyncio.Semaphore, model: str) -> dict:
    from services import glm
    async with sem:
        user = "逐条标注下面这些消息：\n\n" + "\n\n".join(_item_text(r) for r in batch)
        for _ in range(2):
            txt = await glm.chat(
                [{"role": "system", "content": _SYS},
                 {"role": "user", "content": user}],
                model=model, temperature=0.2, max_tokens=3000, timeout=120)
            m = re.search(r"\[.*\]", txt or "", re.S)
            if not m:
                continue
            try:
                arr = json.loads(m.group(0))
            except Exception:
                continue
            out = {}
            for x in arr:
                if isinstance(x, dict) and str(x.get("id") or ""):
                    out[str(x["id"])] = _norm_label(x.get("label") or {})
            if out:
                return out
        return {}


async def main() -> int:
    ap = argparse.ArgumentParser(description="P6 情绪标注 AI 预标")
    ap.add_argument("--in", dest="inp", default=str(DEFAULT_IN))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--model", default="")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    rows = [json.loads(l) for l in Path(args.inp).read_text(
        encoding="utf-8").splitlines() if l.strip()]
    if args.limit:
        rows = rows[:args.limit]
    batches = [rows[i:i + args.batch] for i in range(0, len(rows), args.batch)]
    sem = asyncio.Semaphore(max(1, args.concurrency))
    print(f"预标 {len(rows)} 条 / {len(batches)} 批（model={args.model or '默认 GLM'}）…")

    results = await asyncio.gather(*[_label_batch(b, sem, args.model) for b in batches])
    labels = {}
    for d in results:
        labels.update(d)

    missing = []
    for r in rows:
        lab = labels.get(r["id"])
        if not lab:
            missing.append(r["id"])
            lab = _fallback(r)
        r["label"] = lab
        meta = dict(r.get("meta") or {})
        meta["labeler"] = "AI-precheck(glm)"
        meta["status"] = "ai_precheck"
        meta["note"] = "AI 预标，请人工复核；确认后把 labeler 改成你的名字、status 改 agreed"
        r["meta"] = meta

    dist = Counter((r.get("label") or {}).get("impact") for r in rows)
    print("impact 分布：", dict(sorted(dist.items(), key=lambda kv: (kv[0] is None, kv[0]))))
    print(f"模型返回 {len(labels)} / {len(rows)}；fallback {len(missing)} 条")
    if missing[:8]:
        print("fallback 例：", "、".join(missing[:8]))

    if args.write:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                       encoding="utf-8")
        print(f"[out] {out}")
    else:
        print("（未写盘；加 --write）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
