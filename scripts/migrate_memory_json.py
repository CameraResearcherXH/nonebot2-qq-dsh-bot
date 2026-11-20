#!/usr/bin/env python3
# scripts/migrate_memory_json.py
"""把 data/memory/index.json + blocks/*.json 迁移到 data/memory.db。

用法：
    python3 scripts/migrate_memory_json.py            # 迁移并校验
    python3 scripts/migrate_memory_json.py --dry-run  # 只统计，不写库

安全：
- 先备份 index.json 与 blocks/ 到 data/memory/backup_<时间戳>/
- 按 id 幂等写入（INSERT OR REPLACE），可重复跑
- 迁移后对比条数；不一致退出码 1
- 原 JSON 文件不删除，随时可 MEMORY_BACKEND=json 回滚
"""
import argparse
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services import memdb  # noqa: E402

MEM_DIR = ROOT / "data" / "memory"
INDEX = MEM_DIR / "index.json"
BLOCKS = MEM_DIR / "blocks"


def backup() -> Path:
    dest = MEM_DIR / ("backup_" + time.strftime("%Y%m%d_%H%M%S"))
    dest.mkdir(parents=True, exist_ok=True)
    if INDEX.exists():
        shutil.copy2(INDEX, dest / "index.json")
    if BLOCKS.exists():
        shutil.copytree(BLOCKS, dest / "blocks", dirs_exist_ok=True)
    return dest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not INDEX.exists():
        print("没有 %s，无需迁移" % INDEX)
        return 0
    idx = json.loads(INDEX.read_text(encoding="utf-8"))
    items = idx.get("items") or []
    print("索引条数：%d" % len(items))

    if args.dry_run:
        kinds = {}
        for d in items:
            kinds[d.get("kind")] = kinds.get(d.get("kind"), 0) + 1
        print("按 kind：", kinds)
        print("FTS5 可用：", memdb.fts_available())
        return 0

    dest = backup()
    print("已备份到：%s" % dest)

    memdb.init()
    ok = 0
    for d in items:
        mid = str(d.get("id") or "")
        text = ""
        if mid:
            f = BLOCKS / ("%s.json" % mid)
            if f.exists():
                try:
                    text = str(json.loads(f.read_text(encoding="utf-8")).get("text_full") or "")
                except Exception:
                    text = ""
        row = dict(d)
        row["detail"] = text or d.get("summary") or ""
        try:
            memdb.import_row(row)
            ok += 1
        except Exception as e:
            print("跳过 %s：%s: %s" % (mid, type(e).__name__, e))
    memdb._conn().commit()

    n = memdb.stats().get("total", 0)
    print("写入成功 %d 条；库内 active %d 条" % (ok, n))
    if n < len(items):
        print("⚠️ 条数不一致：索引 %d / 库 %d" % (len(items), n))
        return 1
    print("✅ 迁移完成。切换：MEMORY_BACKEND=sqlite")
    return 0


if __name__ == "__main__":
    sys.exit(main())
