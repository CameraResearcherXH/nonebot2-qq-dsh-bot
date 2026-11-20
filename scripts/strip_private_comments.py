#!/usr/bin/env python3
# scripts/strip_private_comments.py
"""列出「个人化调试注释」，生成可人工确认的清理清单（不自动删）。

匹配：带日期的个人记录，例如
  · 用户 2026-11-19：「……」
  · 真机踩过（2026-11-18）
  · 2026-11-20 审计
这些是开发过程记录，开源仓里应迁到 docs/changelog.md，而不是留在代码里。

用法：
    python3 scripts/strip_private_comments.py            # 打印清单
    python3 scripts/strip_private_comments.py --json      # 机器可读
"""
import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAT = re.compile(r"(用户\s*20\d\d-\d\d-\d\d|真机踩过|20\d\d-\d\d-\d\d\s*审计|"
                 r"20\d\d-\d\d-\d\d\s*修复|20\d\d-\d\d-\d\d\s*定稿)")


def scan():
    out = []
    for p in ROOT.rglob("*.py"):
        parts = p.relative_to(ROOT).parts
        if not parts or parts[0] not in ("services", "plugins", "core", "scripts", "bot.py"):
            continue
        if "third" in parts or "__pycache__" in parts:
            continue
        try:
            lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            continue
        for i, line in enumerate(lines, 1):
            if PAT.search(line):
                out.append({"file": str(p.relative_to(ROOT)), "line": i,
                            "text": line.strip()[:160]})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    rows = scan()
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    print("共 %d 条个人化调试注释（建议迁到 docs/changelog.md）" % len(rows))
    for r in rows[:200]:
        print("%s:%d  %s" % (r["file"], r["line"], r["text"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
