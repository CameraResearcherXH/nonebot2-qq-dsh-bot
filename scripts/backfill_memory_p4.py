#!/usr/bin/env python3
# scripts/backfill_memory_p4.py
"""P4 回填：JSON 记忆 → sqlite（memories + memory_tags + entities + mood_events）。

用法：
    python3 scripts/backfill_memory_p4.py                 # 只报告（dry-run）
    python3 scripts/backfill_memory_p4.py --write         # 真写（幂等）
    python3 scripts/backfill_memory_p4.py --write --retag # 重算所有标签
    python3 scripts/backfill_memory_p4.py --write --limit 100
    python3 scripts/backfill_memory_p4.py --db /tmp/mem.db --write   # 先拿副本试

⚠️ 双写还没实现：切到 `MEMORY_BACKEND=sqlite` 之后，新记忆只写 sqlite，
JSON 索引不再更新。回滚到 json 会**丢掉切换后写入的记忆** —— 切前先备份，
或先切回前把 sqlite 的 rows 导出（下一轮做）。
"""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ap = argparse.ArgumentParser()
ap.add_argument("--write", action="store_true", help="真写（默认 dry-run）")
ap.add_argument("--retag", action="store_true", help="重算所有标签（含已有）")
ap.add_argument("--limit", type=int, default=0)
ap.add_argument("--db", default="", help="覆盖 MEMORY_DB（拿副本试时用）")
args = ap.parse_args()

if args.db:
    os.environ["MEMORY_DB"] = args.db

from services import memdb  # noqa: E402


def load_items() -> list:
    """index.json + blocks/*.json（blocks 里有 text_full）。"""
    idx = ROOT / "data" / "memory" / "index.json"
    items = []
    if idx.exists():
        try:
            items = (json.loads(idx.read_text(encoding="utf-8")) or {}).get("items") or []
        except Exception:
            items = []
    out = []
    for it in items:
        d = dict(it)
        mid = str(d.get("id") or "")
        blk = ROOT / "data" / "memory" / "blocks" / ("%s.json" % mid)
        if mid and blk.exists():
            try:
                b = json.loads(blk.read_text(encoding="utf-8")) or {}
                d["detail"] = b.get("text_full") or b.get("summary") or d.get("summary") or ""
            except Exception:
                pass
        d.setdefault("detail", d.get("summary") or "")
        out.append(d)
    return out


def load_entities() -> int:
    n = 0
    for p in (ROOT / "data" / "profiles").glob("*_profile.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        qq = str(d.get("qq") or p.name.split("_")[0])
        if not qq.isdigit():
            continue
        aliases = [str(a) for a in (d.get("aliases") or [])
                   if str(a).strip() and str(a) != qq]
        if args.write:
            memdb.upsert_entity(qq=qq, name=str(d.get("nickname") or ""),
                                aliases=aliases)
        n += 1
    return n


def load_mood_events() -> int:
    f = ROOT / "data" / "mood_history.jsonl"
    if not f.exists():
        return 0
    n = 0
    for ln in f.read_text(encoding="utf-8").splitlines():
        try:
            d = json.loads(ln)
        except Exception:
            continue
        # mood_history 的 delta 是内部 ×100，转回归一化
        dv = float(d.get("delta") or 0) / 100.0
        if args.write:
            memdb.log_mood_event(
                ts=d.get("at"), session=str(d.get("session") or ""),
                about_qq=str(d.get("about_qq") or ""),
                target="speaker" if str(d.get("about_qq") or "").isdigit() else "world",
                dv=dv, confidence=0.0, relevance=0.5,
                reason=str(d.get("emotion") or ""))
        n += 1
    return n


def main() -> int:
    items = load_items()
    print("JSON 记忆：%d 条" % len(items))
    if args.write:
        for i, it in enumerate(items):
            if args.limit and i >= args.limit:
                break
            memdb.import_row(it)
    st = memdb.stats()
    print("sqlite 记忆：%d 条｜标签覆盖 %.1f%%"
          % (st["total"], st["tag_coverage"] * 100))

    if not args.write:
        c = memdb._conn()
        missing = c.execute(
            "SELECT count(*) FROM memories WHERE status='active' AND id NOT IN "
            "(SELECT DISTINCT memory_id FROM memory_tags)").fetchone()[0]
        print("待补标签：%d 条（--write 才会写）" % missing)
        print("dry-run：没有写入任何数据")
        return 0

    r = memdb.backfill_tags(force=args.retag, limit=args.limit)
    print("补标签：%s" % json.dumps(r, ensure_ascii=False))
    print("实体：%d 个｜心情事件：%d 条" % (load_entities(), load_mood_events()))
    print("最终：%s" % json.dumps(memdb.stats(), ensure_ascii=False))

    # 规划器自测（不写命中）
    try:
        hits = memdb.recall_planned("老大 心情", limit=3, mark_hit=False)
        print("planner 自测：%s"
              % [(h["summary"][:18], round(h["score"], 3)) for h in hits])
    except Exception as e:
        print("planner 自测失败：%s: %s" % (type(e).__name__, e))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
