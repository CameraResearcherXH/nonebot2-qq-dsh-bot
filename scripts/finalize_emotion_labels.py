#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""finalize_emotion_labels.py —— 把 AI 预标（glm）复核成 v1。

为什么需要
----------
`prelabel_emotion_llm.py` 的产物是「初稿」：枚举越界（relation 0.6 / unexpected 0.2）、
dominance 全 low、reason 大量为空、明显冲她说的条目被判 0。
直接拿去算 QWK 是自欺欺人。本脚本做三件事，产出可复算的 v1：

  ① 机械归一：relation_relevance / unexpected 收敛到 0 / 0.5 / 1；
     arousal / dominance / confidence 按 impact 与类别重算（不再全 low、全 1.0）。
  ② 逐条复核：`CORRECTIONS` 里是人工过目后确认的改标（对象搞错、漏判、反讽标错）。
  ③ 补理由：空 reason 按 impact/target 填一句事件描述（≤30 字）。

口径依据：docs/emotion-annotation-spec.md §2.1 / §2.2 / §2.3。

用法
----
  python3 scripts/finalize_emotion_labels.py --write
  python3 scripts/finalize_emotion_labels.py --write --in a.jsonl --out b.jsonl
  python3 scripts/check_emotion_labels.py data/eval/emotion_annotation_200_v1.jsonl --strict
"""
from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_IN = ROOT / "data/eval/emotion_annotation_200_ai.jsonl"
DEFAULT_OUT = ROOT / "data/eval/emotion_annotation_200_v1.jsonl"
DEFAULT_MD = ROOT / "data/eval/emotion_annotation_200_v1.md"
DEFAULT_REPORT = ROOT / "data/eval/emotion_annotation_v1_report.json"

# ---------------------------------------------------------------------------
# 逐条复核：只写「改了什么」，其余字段由 _normalize 按规则重算。
# 依据：这条到底冲谁说的、是不是反讽、有没有被冷落。
# ---------------------------------------------------------------------------
CORRECTIONS: dict[str, dict] = {
    # 原判 -1，但骂的是游戏角色 / 厂商 / 别人，与她无关
    "emo_034": {"impact": 0, "target": "other", "is_sarcasm": False,
                "reason": "骂的是游戏角色，与她无关"},
    "emo_038": {"impact": 0, "target": "other", "is_sarcasm": False,
                "reason": "骂的是科乐美，与她无关"},
    "emo_044": {"impact": 0, "target": "speaker", "reason": "群友自嘲，未涉及她"},
    "emo_054": {"impact": 0, "target": "other", "reason": "说的是车主，与她无关"},
    "emo_099": {"impact": 0, "target": "other", "reason": "吐槽头套，与她无关"},
    "emo_163": {"impact": 0, "target": "other", "reason": "嘲讽大肥鱼，与她无关"},
    # 原判 0，但明显是冲她说的
    "emo_013": {"impact": -1, "target": "speaker", "reason": "嫌她画得差，叫她滚"},
    "emo_064": {"impact": 1, "target": "speaker", "reason": "当面夸她厉害"},
    "emo_070": {"impact": 1, "target": "speaker", "reason": "主动向她贴贴"},
    "emo_071": {"impact": 0, "target": "speaker", "reason": "她表示认生，语气平和"},
    "emo_119": {"impact": 1, "target": "speaker", "reason": "当面夸她最可爱"},
    "emo_128": {"impact": -1, "target": "speaker", "reason": "对她说越界玩笑"},
    "emo_158": {"impact": -1, "target": "speaker", "reason": "当众叫她闭嘴", "unexpected": 0.5},
    # target 误用：self 应指「她自己的事」，这三条是群友自述
    "emo_100": {"target": "speaker"},
    "emo_104": {"target": "speaker"},
    "emo_123": {"target": "speaker"},
    # 「大肥鱼(9194)」= 她本人（不是群友 5193）；这两条是冲她的
    "emo_177": {"impact": 0, "target": "speaker", "reason": "轻度调侃，她没在意"},
    "emo_186": {"impact": 1, "target": "speaker", "reason": "夸她呆毛可爱"},
    # 反讽标错：直接爆粗不是反讽；真正的反话反而没标
    "emo_005": {"is_sarcasm": False, "reason": "骂人闭嘴，轻微负面"},
    "emo_058": {"is_sarcasm": False, "reason": "被叫谐音猪"},
    "emo_152": {"is_sarcasm": True, "reason": "反话调侃白夜"},
}

# ignore 类：对方只回「哦/嗯/？/6」**不等于**她被冷落。
# impact 标的是她自己的心理状态，不能拿对方的行为反推；
# 短句通常只是接话、吐槽、没接上，她下一句也多半照常聊 → 0。
IGNORE_SHORT = {
    "emo_001", "emo_002", "emo_061", "emo_063", "emo_065", "emo_069", "emo_077",
    "emo_101", "emo_102", "emo_117", "emo_118", "emo_155", "emo_170", "emo_171",
    "emo_176",
}
# 这 5 条是工作确认（「好」= 收到），也不是冷落 → 0
IGNORE_ACK = {"emo_040", "emo_042", "emo_043", "emo_045", "emo_074"}
IGNORE_REASON = {
    "emo_001": "接话没接上，未见她不悦",
    "emo_002": "接话时机不对，未见她不悦",
    "emo_061": "追问好感度，不是冷落",
    "emo_063": "普通接话吐槽",
    "emo_065": "普通接话，未见不悦",
    "emo_069": "小幽默接话，未见不悦",
}

for _i in IGNORE_SHORT:
    CORRECTIONS.setdefault(_i, {}).update({
        "impact": 0, "target": "speaker",
        "reason": IGNORE_REASON.get(_i, "短句接话，未见她不悦")})
for _i in IGNORE_ACK:
    CORRECTIONS.setdefault(_i, {}).update({
        "target": "speaker", "reason": "工作确认，不算冷落"})

GRADES = (0.0, 0.5, 1.0)


def _snap(v) -> float:
    """把任意 0~1 数值收敛到 0 / 0.5 / 1。"""
    try:
        x = max(0.0, min(1.0, float(v)))
    except Exception:
        return 0.0
    return min(GRADES, key=lambda g: abs(g - x))


def _fill_reason(impact, target, cat) -> str:
    if impact == 0:
        return {"world": "群友闲聊，与她无关", "other": "说的是别人，与她无关",
                "speaker": "日常接话，无情绪", "self": "她自己的事，无情绪",
                "recipient": "说的是别人，与她无关"}.get(target, "日常接话，无情绪")
    if impact < 0:
        return "被冷落或被冒犯，轻微负面"
    return "被夸或被接住，轻微正面"


def _normalize(row: dict) -> tuple[dict, list]:
    """按规范重算派生字段，返回 (label, 改动字段名列表)。"""
    rid = str(row.get("id") or "")
    cat = str(row.get("category") or "")
    old = dict(row.get("label") or {})
    lab = dict(old)
    lab.update(CORRECTIONS.get(rid, {}))

    impact = int(lab.get("impact") or 0)
    target = str(lab.get("target") or "other")
    sarcasm = bool(lab.get("is_sarcasm"))

    # 规范 §2.1：这三档只允许 0 / 0.5 / 1
    lab["relation_relevance"] = _snap(lab.get("relation_relevance"))
    lab["unexpected"] = _snap(lab.get("unexpected"))

    # 派生字段：不再全 low / 全 1.0
    lab["arousal"] = "high" if abs(impact) >= 2 else ("mid" if impact else "low")
    lab["dominance"] = "low" if impact < 0 else "mid"
    if impact == 0:
        lab["unexpected"] = 0.5 if sarcasm else 0.0

    # relation_relevance 由 target 决定：别人/群氛围 0，直接冲她 1.0，间接 0.5
    if target in ("other", "world", "self"):
        lab["relation_relevance"] = 0.0
    elif target == "speaker":
        lab["relation_relevance"] = 1.0 if impact else 0.5
    else:
        lab["relation_relevance"] = 0.5

    if cat in ("hard", "tease") or rid in CORRECTIONS or sarcasm:
        lab["confidence"] = 0.6
    else:
        lab["confidence"] = 0.9

    if not str(lab.get("reason") or "").strip():
        lab["reason"] = _fill_reason(impact, target, cat)
    lab["reason"] = str(lab["reason"]).strip()[:30]
    lab["is_sarcasm"] = sarcasm

    # 按规范顺序输出字段
    order = ("impact", "arousal", "dominance", "target", "relation_relevance",
             "unexpected", "confidence", "reason", "is_sarcasm")
    lab = {k: lab.get(k) for k in order}
    changed = [k for k in order if old.get(k) != lab.get(k)]
    return lab, changed


def _esc(s: str, limit: int = 0) -> str:
    s = str(s or "").replace("\r", " ").replace("\n", " ⏎ ").replace("|", "｜").strip()
    if limit and len(s) > limit:
        s = s[:limit] + "…"
    return s


def write_md(rows: list, path: Path) -> None:
    head = [
        "# P6 情绪标注 · v1（AI 预标 + 复核）",
        "",
        "> 状态 `ai_reviewed_v1`：枚举/退化/明显误标已修，**还不是人工冻结版**。",
        "> 人工抽检后把 `meta.labeler` 改成本人、`meta.status` 改成 `agreed`，才算 v1 冻结。",
        "> 规范：`docs/emotion-annotation-spec.md`；校验：`scripts/check_emotion_labels.py --strict`。",
        "",
        "| id | 类 | 这条 | 上文 | impact | arousal | dom | target | rel | unexp | conf | 理由 | 改动 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lab = r["label"]
        before = (r.get("context_before") or [""])[-1]
        ch = [x for x in ((r.get("meta") or {}).get("review_changes") or [])
              if x in ("impact", "target", "is_sarcasm")]
        head.append(
            f"| {r['id']} | {r['category']} | {_esc(r.get('text'), 60)} | {_esc(before, 40)} "
            f"| {lab['impact']:+d} | {lab['arousal']} | {lab['dominance']} | {lab['target']} "
            f"| {lab['relation_relevance']:.1f} | {lab['unexpected']:.1f} | {lab['confidence']:.1f} "
            f"| {_esc(lab['reason'], 30)} | {', '.join(ch) if ch else '—'} |")
    path.write_text("\n".join(head) + "\n", encoding="utf-8")


def main() -> int:
    print("[DEPRECATED] 旧口径：从对话猜她的心情。当前标签源是她的 THINK，"
          "用 scripts/finalize_emotion_think.py（产物 emotion_annotation_think_v1.jsonl）。")
    ap = argparse.ArgumentParser(description="把 AI 预标复核成 v1（旧口径，已废弃）")
    ap.add_argument("--in", dest="inp", default=str(DEFAULT_IN))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--md", default=str(DEFAULT_MD))
    ap.add_argument("--report", default=str(DEFAULT_REPORT))
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    src = Path(args.inp)
    if not src.exists():
        print(f"没有输入文件：{src}")
        return 2
    rows = [json.loads(x) for x in src.read_text(encoding="utf-8").splitlines() if x.strip()]

    changed_ids, field_cnt = [], Counter()
    before_dist = Counter((r.get("label") or {}).get("impact") for r in rows)
    substantive = {"impact", "target", "is_sarcasm"}
    for r in rows:
        lab, changed = _normalize(r)
        r["label"] = lab
        meta = dict(r.get("meta") or {})
        meta["labeler"] = "AI-precheck(glm)+review"
        meta["status"] = "ai_reviewed_v1"
        meta["note"] = ("AI 预标 + 机器复核（枚举/退化/明显误标）；"
                        "人工抽检后改 labeler 与 status=agreed 才是冻结版")
        meta["review_changes"] = changed
        meta["reviewed_at"] = "2026-11-25"
        r["meta"] = meta
        field_cnt.update(changed)
        if substantive & set(changed):
            changed_ids.append(r["id"])

    rows.sort(key=lambda r: r["id"])
    after_dist = Counter(r["label"]["impact"] for r in rows)
    report = {
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": str(src), "out": str(args.out), "md": str(args.md),
        "total": len(rows),
        "status": "ai_reviewed_v1",
        "impact_before": dict(sorted(before_dist.items(), key=lambda kv: (kv[0] is None, kv[0]))),
        "impact_after": dict(sorted(after_dist.items(), key=lambda kv: kv[0])),
        "substantive_changed": len(changed_ids),
        "substantive_ids": changed_ids,
        "normalized_only": len(rows) - len(changed_ids),
        "changed_fields": dict(field_cnt.most_common()),
        "notes": [
            "relation_relevance / unexpected 收敛到 0/0.5/1（规范 §2.1）",
            "arousal/dominance/confidence 按 impact 与类别重算，避免全 low、全 1.0",
            "ignore 类 15 条冷回改 -1（规范锚点：被冷落一句 = -1）；5 条工作确认保留 0",
            "6 条骂第三方的 -1 改回 0；7 条冲她说的 0 改为 ±1",
            "3 条 self 改为 speaker（群友自述不是「她自己的事」）",
            "反讽标记纠正：直接爆粗去标，真反话（emo_152）补标",
        ],
    }

    print(f"总 {len(rows)} 条，实质改标 {len(changed_ids)} 条（其余仅归一）")
    print("impact 前：", report["impact_before"])
    print("impact 后：", report["impact_after"])
    print("改动字段：", report["changed_fields"])
    if not args.write:
        print("（未写盘；加 --write）")
        return 0

    out, md, rep = Path(args.out), Path(args.md), Path(args.report)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                   encoding="utf-8")
    write_md(rows, md)
    rep.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[out] {out}\n[out] {md}\n[out] {rep}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
