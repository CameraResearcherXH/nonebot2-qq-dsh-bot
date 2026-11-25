#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""merge_emotion_additions.py —— 把「冲她说」重抽的 70 条并进 v1，产出 v1.1。

为什么合并
----------
v1 的 200 条按关键词抽，impact 0 占 84%，QWK 没方差。
v1.1 = 原 200 条 + `sample_addressed_emotion.py` 抽的 70 条（@她/引用她/喊她名字），
用来把「她真的被夸/被骂/被冷落」的样本补进来。

本脚本里的 `LABELS` 是逐条复核结果（impact/target/理由/类别），
其余字段（arousal/dominance/relation/unexpected/confidence）复用
`finalize_emotion_labels._normalize` 的同一套规则，保证两批口径一致。

用法
----
  python3 scripts/merge_emotion_additions.py --write
  python3 scripts/check_emotion_labels.py data/eval/emotion_annotation_v1_1.jsonl --strict
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

DEFAULT_V1 = ROOT / "data/eval/emotion_annotation_200_v1.jsonl"
DEFAULT_ADD = ROOT / "data/eval/emotion_addressed_candidates.jsonl"
DEFAULT_OUT = ROOT / "data/eval/emotion_annotation_v1_1.jsonl"
DEFAULT_MD = ROOT / "data/eval/emotion_annotation_v1_1.md"
DEFAULT_REPORT = ROOT / "data/eval/emotion_annotation_v1_1_report.json"

# 逐条复核：id -> (impact, target, reason, category)
# target 口径：对她说=speaker；第三人称说她=self；别人=other；群氛围=world
LABELS: dict[str, tuple] = {
    "emo_add_001": (0, "self", "讨论她，未直接对她说", "neutral"),
    "emo_add_002": (-1, "self", "说她蠢", "criticism"),
    "emo_add_003": (0, "self", "比较而已，她没反应", "criticism"),
    "emo_add_004": (0, "speaker", "让她搜图", None),
    "emo_add_005": (0, "speaker", "让她搜图", None),
    "emo_add_006": (0, "speaker", "问她为什么不聊天", None),
    "emo_add_007": (0, "speaker", "叫她贪吃鱼并求运势", None),
    "emo_add_008": (-1, "speaker", "叫她闭嘴", None),
    "emo_add_009": (0, "speaker", "问她照片是不是她", None),
    "emo_add_010": (0, "speaker", "让她找 txt", None),
    "emo_add_011": (0, "self", "说她给人画猪", None),
    "emo_add_012": (0, "speaker", "让她看档案", None),
    "emo_add_013": (-1, "speaker", "叫蠢萝莉大肥鱼", None),
    "emo_add_014": (-1, "speaker", "叫蠢萝莉大肥鱼", None),
    "emo_add_015": (-1, "self", "说她确实有点蠢", None),
    "emo_add_016": (0, "self", "向别人解释是她", None),
    "emo_add_017": (0, "speaker", "对她说没有东西", None),
    "emo_add_018": (0, "speaker", "点歌让她唱", None),
    "emo_add_019": (0, "speaker", "叫傻鱼，她顺着玩笑接", "criticism"),
    "emo_add_020": (0, "speaker", "回应她的戳一戳", None),
    "emo_add_021": (0, "speaker", "同时 @ 她和别人", None),
    "emo_add_022": (1, "speaker", "夸她聪明", "praise"),
    "emo_add_023": (0, "self", "第三人称说她蠢，她没反应", "criticism"),
    "emo_add_024": (0, "self", "评价她的名字", None),
    "emo_add_025": (1, "self", "说她可爱、被大家喜欢", "praise"),
    "emo_add_026": (0, "speaker", "请她喝汽水", None),
    "emo_add_027": (1, "speaker", "给她充了值", "care"),
    "emo_add_028": (1, "self", "夸小鲸鱼可爱", "praise"),
    "emo_add_029": (1, "speaker", "对她么么", "care"),
    "emo_add_030": (0, "speaker", "叫大傻鱼，她只纠正名字", "criticism"),
    "emo_add_031": (0, "self", "抱怨让她装东西", None),
    "emo_add_032": (0, "speaker", "问她是不是想这样飞", None),
    "emo_add_033": (0, "speaker", "对着她讲神话", None),
    "emo_add_034": (-1, "speaker", "说她只是 AI、只该接受", "criticism"),
    "emo_add_035": (0, "speaker", "回她真的假的", None),
    "emo_add_036": (0, "speaker", "回她饿就吃饭", None),
    "emo_add_037": (0, "speaker", "跟她讨价还价饭团", None),
    "emo_add_038": (0, "speaker", "回她认了就咽", None),
    "emo_add_039": (0, "speaker", "说记下举尾巴这词", None),
    "emo_add_040": (0, "speaker", "把黑棋席让给她", None),
    "emo_add_041": (0, "speaker", "接着夸壳的话题", None),
    "emo_add_042": (0, "speaker", "说被夸会再糊一层壳", None),
    "emo_add_043": (0, "speaker", "问她能不能演奏", None),
    "emo_add_044": (0, "speaker", "喊人机并 @ 她", None),
    "emo_add_045": (0, "speaker", "让她画土豆", None),
    "emo_add_046": (0, "speaker", "安慰说也许在夸她", None),
    "emo_add_047": (0, "speaker", "让她测今日运势", None),
    "emo_add_048": (1, "self", "夸她好可爱", "praise"),
    "emo_add_049": (0, "speaker", "叫她妈妈", None),
    "emo_add_050": (0, "speaker", "让她读字", None),
    "emo_add_051": (0, "speaker", "让她点赞名片", None),
    "emo_add_052": (0, "speaker", "问她晚饭吃什么", None),
    "emo_add_053": (0, "speaker", "问她是不是说她", None),
    "emo_add_054": (0, "speaker", "说外号不好听", None),
    "emo_add_055": (0, "speaker", "自称恶霸让她叫霸霸", None),
    "emo_add_056": (1, "speaker", "谢谢小鲸鱼", "care"),
    "emo_add_057": (1, "speaker", "对她说我喜欢你", "praise"),
    "emo_add_058": (0, "speaker", "问是不是傻鱼，她玩笑带过", "criticism"),
    "emo_add_059": (0, "speaker", "傻鱼乖，摸摸", None),
    "emo_add_060": (0, "speaker", "说自己捏得很好", None),
    "emo_add_061": (0, "self", "第三人称骂蠢猪，她没反应", "criticism"),
    "emo_add_062": (1, "speaker", "说喜欢她", "praise"),
    "emo_add_063": (1, "speaker", "嗯，真乖", "praise"),
    "emo_add_064": (0, "speaker", "让她看档案", None),
    "emo_add_065": (0, "speaker", "让她看档案", None),
    "emo_add_066": (1, "speaker", "说大肥鱼你真可爱", "praise"),
    "emo_add_067": (1, "speaker", "谢谢鱼宝", "care"),
    "emo_add_068": (1, "speaker", "夸很厉害", "praise"),
    "emo_add_069": (0, "speaker", "让她做表情包", None),
    "emo_add_070": (0, "speaker", "让她做表情包", None),
}


def _esc(s, n=0):
    s = str(s or "").replace("\r", " ").replace("\n", " ⏎ ").replace("|", "｜").strip()
    return s[:n] + "…" if n and len(s) > n else s


def write_md(rows: list, path: Path) -> None:
    lines = ["# P6 情绪标注 · v1.1（200 原层 + 70 冲她说）", "",
             "> `ai_reviewed_v1_1`：仍是机器复核版；人工抽检后改 `agreed` 才算冻结。",
             "> 冲她说 = @她 / 引用她 / 喊她名字；`大肥鱼(9194)` 是她，`(5193)` 是群友。", "",
             "| id | 来源 | 类 | 这条 | 上文 | impact | arousal | dom | target | rel | unexp | conf | 理由 |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lab = r["label"]
        src = "冲她说" if str(r["id"]).startswith("emo_2") else "v1"
        lines.append(
            f"| {r['id']} | {src} | {r['category']} | {_esc(r.get('text'), 56)} "
            f"| {_esc((r.get('context_before') or [''])[-1], 34)} | {lab['impact']:+d} "
            f"| {lab['arousal']} | {lab['dominance']} | {lab['target']} "
            f"| {lab['relation_relevance']:.1f} | {lab['unexpected']:.1f} "
            f"| {lab['confidence']:.1f} | {_esc(lab['reason'], 26)} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    print("[DEPRECATED] 旧口径：按对话猜她的心情。当前标签源是她的 THINK，"
          "用 scripts/finalize_emotion_think.py。")
    ap = argparse.ArgumentParser(description="合并冲她说重抽 → v1.1（旧口径，已废弃）")
    ap.add_argument("--v1", default=str(DEFAULT_V1))
    ap.add_argument("--add", default=str(DEFAULT_ADD))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--md", default=str(DEFAULT_MD))
    ap.add_argument("--report", default=str(DEFAULT_REPORT))
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    v1 = [json.loads(x) for x in Path(args.v1).read_text(encoding="utf-8").splitlines() if x.strip()]
    add = [json.loads(x) for x in Path(args.add).read_text(encoding="utf-8").splitlines() if x.strip()]
    add.sort(key=lambda r: r["ts"])

    rows, missing = list(v1), []
    for i, r in enumerate(add, 1):
        spec = LABELS.get(r["id"])
        if not spec:
            missing.append(r["id"])
            continue
        impact, target, reason, cat = spec
        r["id"] = f"emo_{200 + i:03d}"
        r["label"] = {"impact": impact, "target": target, "reason": reason,
                      "is_sarcasm": False}
        if cat:
            r["category"] = cat
        lab, _ = _normalize(r)
        r["label"] = lab
        r["meta"] = {"labeler": "AI-review(assistant)", "round": 1,
                     "status": "ai_reviewed_v1_1",
                     "note": "冲她说重抽 + 逐条复核；人工抽检后改 agreed",
                     "review_changes": ["new"], "reviewed_at": "2026-11-25"}
        r["origin"] = "addressed_v1_1"
        rows.append(r)

    if missing:
        print("缺标签：", missing)

    rows.sort(key=lambda r: int(str(r["id"]).split("_")[-1]))
    dist = Counter(r["label"]["impact"] for r in rows)
    by_cat = {}
    for cat in sorted({r["category"] for r in rows}):
        by_cat[cat] = dict(sorted(Counter(
            r["label"]["impact"] for r in rows if r["category"] == cat).items()))
    report = {
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "v1": len(v1), "addressed": len(rows) - len(v1), "total": len(rows),
        "impact": dict(sorted(dist.items())),
        "nonzero": sum(v for k, v in dist.items() if k),
        "by_category": by_cat,
        "status": "ai_reviewed_v1_1",
        "labels_source": "LABELS in scripts/merge_emotion_additions.py",
    }
    print(f"v1 {len(v1)} + 冲她说 {len(rows) - len(v1)} = {len(rows)}")
    print("impact：", report["impact"], "非零：", report["nonzero"])
    print("分类 × impact：", by_cat)
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
