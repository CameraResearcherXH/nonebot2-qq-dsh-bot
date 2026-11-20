#!/usr/bin/env python3
# scripts/check_mounts.py
"""检查 docker-compose 的挂载是否覆盖了「代码真正 import 的顶层模块」。

背景（2026-11-20 真实事故预演）：
  容器只挂了 plugins / services / scripts / bot.py / .env / data，
  新增 `core/` 后，`from core import tuning` 在容器里会 ImportError，
  而镜像里也没有这个目录（镜像是加 core 之前构建的）→ 容器直接起不来。

规则：
  1. 扫 bot.py + plugins/ + services/ + core/ 里的 import，
     只保留「仓库根目录下真实存在」的顶层包/模块（core、services、plugins、config…）。
  2. 这些顶层模块必须在 docker-compose.yml 的 volumes 里挂载到 /app/<同名>。
  3. bot.py 本身也必须在挂载列表里。

用法：python3 scripts/check_mounts.py [--strict]
"""
import argparse
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP = ("plugins/third", "plugins_backup", "venv", "__pycache__", ".workbuddy")


def runtime_files():
    yield ROOT / "bot.py"
    for base in ("plugins", "services", "core"):
        d = ROOT / base
        if not d.exists():
            continue
        for f in d.rglob("*.py"):
            rel = f.relative_to(ROOT).as_posix()
            if any(s in rel for s in SKIP):
                continue
            yield f


def root_modules():
    """仓库根目录下真实存在的顶层包/模块名。"""
    out = set()
    for p in ROOT.iterdir():
        if p.name.startswith((".", "_")):
            continue
        if p.is_dir() and (p / "__init__.py").exists():
            out.add(p.name)
        elif p.suffix == ".py" and p.stem not in ("setup", "conftest"):
            out.add(p.stem)
    return out


def imported_root_modules(mods):
    used = set()
    for f in runtime_files():
        try:
            tree = ast.parse(f.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    top = a.name.split(".")[0]
                    if top in mods:
                        used.add(top)
            elif isinstance(node, ast.ImportFrom):
                if node.level:      # 相对 import（from . import x）
                    continue
                top = (node.module or "").split(".")[0]
                if top in mods:
                    used.add(top)
    return used


def compose_mounts():
    """返回 (挂载的源路径集合, 目标路径集合)。"""
    p = ROOT / "docker-compose.yml"
    if not p.exists():
        return set(), set()
    src, dst = set(), set()
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        m = re.match(r"\s*-\s*\./([^:\s]+)\s*:\s*(\S+)", line)
        if m:
            src.add(m.group(1).rstrip("/"))
            dst.add(m.group(2).rstrip("/"))
    return src, dst


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args()

    mods = root_modules()
    used = imported_root_modules(mods)
    # plugins 不是 import 进来的（nonebot 按目录加载），但必须挂
    used |= {"plugins"}
    src, dst = compose_mounts()

    missing = []
    for m in sorted(used):
        # bot.py 是文件挂载
        target = "/app/%s" % m
        if m not in src and target not in dst:
            missing.append(m)

    print("=" * 66)
    print("挂载完整性检查（代码 import 的顶层模块 是否都挂进容器）")
    print("=" * 66)
    print("仓库根模块：%s" % "、".join(sorted(mods)))
    print("代码实际使用：%s" % "、".join(sorted(used)))
    print("compose 已挂载：%s" % "、".join(sorted(src)) or "（空）")
    print("-" * 66)
    if not missing:
        print("✅ 所有被 import 的顶层模块都已挂载")
        return 0
    print("❌ 以下模块代码在用，但容器里没有（会 ImportError，容器起不来）：")
    for m in missing:
        print("   %-12s → 在 docker-compose.yml volumes 里加：- ./%s:/app/%s" % (m, m, m))
    print("   改完必须 `docker compose up -d`（重建容器；restart 不会更新挂载）")
    return 0 if not args.strict else 1


if __name__ == "__main__":
    raise SystemExit(main())
