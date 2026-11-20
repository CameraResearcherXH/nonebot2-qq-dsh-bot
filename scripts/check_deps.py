#!/usr/bin/env python3
# scripts/check_deps.py
"""依赖方向检查：plugins → services → core，禁止反向与插件互相 import。

用法：
    python3 scripts/check_deps.py           # 打印违规
    python3 scripts/check_deps.py --strict  # 有违规退出码 1

规则：
- core/** 不得 import services / plugins
- services/** 不得 import plugins
- plugins/<a>/** 不得 import plugins/<b>（跨插件）
"""
import argparse
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def module_of(path: Path) -> str:
    rel = path.relative_to(ROOT).with_suffix("")
    return ".".join(rel.parts)


def imports_of(path: Path):
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return []
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.module:
                out.append(node.module)
            elif node.module:
                out.append(node.module)
    return out


def check() -> list:
    bad = []
    for p in ROOT.rglob("*.py"):
        parts = p.relative_to(ROOT).parts
        if not parts or parts[0] not in ("core", "services", "plugins"):
            continue
        if "third" in parts or "__pycache__" in parts:
            continue
        mod = module_of(p)
        for imp in imports_of(p):
            if parts[0] == "core" and (imp.startswith("services") or imp.startswith("plugins")):
                bad.append((str(p.relative_to(ROOT)), imp, "core 不得依赖 services/plugins"))
            elif parts[0] == "services" and imp.startswith("plugins"):
                bad.append((str(p.relative_to(ROOT)), imp, "services 不得依赖 plugins"))
            elif parts[0] == "plugins":
                if len(parts) > 1:
                    own = parts[1]
                    for other in ("chat", "draw", "vision_agent", "selfcheck", "self_tools",
                                  "tool_dispatcher", "napcat_tools", "sticker_admin",
                                  "help_menu", "memory_migration", "cover_api", "full_test",
                                  "song_cover"):
                        if other != own and imp.startswith("plugins." + other):
                            bad.append((str(p.relative_to(ROOT)), imp, "插件之间不得互相 import"))
    return bad


BASELINE = ROOT / "scripts" / "dep_baseline.txt"


def load_baseline() -> set:
    if not BASELINE.exists():
        return set()
    out = set()
    for line in BASELINE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.add(line)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--write-baseline", action="store_true")
    args = ap.parse_args()
    bad = check()
    if args.write_baseline:
        BASELINE.write_text(
            "# 历史遗留依赖（逐步消除；新违规会让 --strict 失败）\n"
            + "\n".join("%s|%s" % (f, imp) for f, imp, _ in bad) + "\n",
            encoding="utf-8")
        print("已写入 baseline：%d 条 → %s" % (len(bad), BASELINE))
        return 0
    base = load_baseline()
    legacy = [x for x in bad if ("%s|%s" % (x[0], x[1])) in base]
    new = [x for x in bad if ("%s|%s" % (x[0], x[1])) not in base]
    if not new:
        print("✅ 依赖方向检查通过（plugins → services → core）")
        if legacy:
            print("   （历史遗留 %d 条已记录在 scripts/dep_baseline.txt）" % len(legacy))
        return 0
    print("❌ 新增 %d 处依赖违规：" % len(new))
    for f, imp, why in new:
        print("  %-40s %-32s %s" % (f, imp, why))
    return 1 if args.strict else 0


if __name__ == "__main__":
    sys.exit(main())
