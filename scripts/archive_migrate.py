#!/usr/bin/env python3
"""把 30 天以前的旧事搬进「个人档案库」（`data/archive.db`）。

这是 C 方案（2026-11-26 用户拍板）：
  · 近期记忆留在本地 `memories`（memdb，毫秒级）；
  · 30 天以前的旧对话/旧记忆搬进 `archive.db`（也本地，只她自己能查）；
  · **不碰 RAG**：图书馆是外部语料，和档案库不是一套索引。

用法：
    python scripts/archive_migrate.py --dry-run        # 只看会搬多少
    python scripts/archive_migrate.py                  # 搬 30 天以前
    python scripts/archive_migrate.py --days 7         # 测试用：搬 7 天以前
    python scripts/archive_migrate.py --stats          # 只看档案库现状
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from services import archive  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="把旧对话/旧记忆搬进个人档案库")
    ap.add_argument("--days", type=int, default=0,
                    help="搬多少天以前的（默认读 ARCHIVE_MIN_AGE_DAYS=30）")
    ap.add_argument("--dialogue-dir", default="data/dialogue")
    ap.add_argument("--skip-dialogue", action="store_true")
    ap.add_argument("--skip-memdb", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args()

    if args.stats:
        print(json.dumps(archive.stats(), ensure_ascii=False, indent=1))
        print(archive.status_text())
        return 0

    days = int(args.days or archive.MIN_AGE_DAYS)
    print(f"个人档案库：{archive.DB_PATH}｜只搬 {days} 天以前"
          f"｜dry-run={args.dry_run}")
    out = {}
    if not args.skip_dialogue:
        out["dialogue"] = archive.migrate_dialogue(
            dir_path=args.dialogue_dir, min_age_days=days,
            dry_run=args.dry_run)
        print("对话：", out["dialogue"])
    if not args.skip_memdb:
        out["memdb"] = archive.migrate_memdb(min_age_days=days,
                                            dry_run=args.dry_run)
        print("旧记忆：", out["memdb"])
    print("档案库现状：", archive.stats())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
