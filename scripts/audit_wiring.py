#!/usr/bin/env python
"""
audit_wiring.py —— 检查「架构是不是都接上了」。

三件事：
  1. 工具链完整性：模型能输出的每个标记（[[SEARCH]] 等），系统是否真有处理分支
  2. 模块接线：services/ 下每个模块是否真的被别处用上（找孤儿）
  3. 函数接线：每个公开函数被谁调用（找死代码）

用法：
    python scripts/audit_wiring.py
    python scripts/audit_wiring.py --dead      # 详细列出没人调用的函数
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVICES = ROOT / "services"
PLUGINS = ROOT / "plugins"


def all_py() -> list:
    files = sorted(SERVICES.glob("*.py"))
    files += sorted(PLUGINS.glob("*/__init__.py"))
    files += sorted((ROOT / "scripts").glob("*.py"))
    files += [ROOT / "node.py"] if (ROOT / "node.py").exists() else []
    return [f for f in files if f.exists()]


def module_funcs(path: Path) -> list:
    """模块里的顶层函数/类名。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except SyntaxError:
        return []
    out = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.append((node.name, node.lineno))
    return out


def count_refs(name: str, files: list, owner: Path) -> list:
    """name 在哪些文件里被引用（排除定义行本身）。返回 [(文件, 次数)]。"""
    pat = re.compile(rf"\b{re.escape(name)}\b")
    hits = []
    for f in files:
        try:
            src = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        n = len(pat.findall(src))
        if f == owner:
            # 定义本身算 1 次，本模块内部再用也算
            n -= 1
        if n > 0:
            hits.append((f.relative_to(ROOT).as_posix(), n))
    return hits


# ============================================================
# ① 工具链完整性
# ============================================================
def check_tool_chain() -> list:
    llm = (SERVICES / "llm.py").read_text(encoding="utf-8", errors="ignore")
    problems = []

    # 模型能输出的标记：从 _REQ_RE 里抓
    m = re.search(r"_REQ_RE\s*=\s*re\.compile\(\s*r?\"([^\"]+)\"", llm)
    kinds = []
    if m:
        inner = re.search(r"\(([A-Z|]+)\)", m.group(1))
        if inner:
            kinds = inner.group(1).split("|")
    # 再加上 parse_reply 认识的（MEMO/CALL/FOCUS/AT/WHO/VOICE）
    for extra in ("MEMO", "CALL", "FOCUS", "AT", "VOICE"):
        if extra not in kinds:
            kinds.append(extra)

    for k in kinds:
        has_branch = f'kind == "{k}"' in llm
        # 有些标记由别的模块处理（aura_logic / chat）
        elsewhere = False
        for f in all_py():
            if f == SERVICES / "llm.py":
                continue
            try:
                s = f.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            if f'"{k}"' in s and ("parse" in s or "kind" in s or "re.compile" in s):
                elsewhere = True
                break
        if not has_branch and not elsewhere:
            problems.append(f"⚠️ 标记 [[{k}]] 没有处理分支（写了也白写）")
    return kinds, problems


# ============================================================
# ② 模块接线
# ============================================================
def check_modules() -> list:
    files = all_py()
    rows = []
    for f in sorted(SERVICES.glob("*.py")):
        mod = f.stem
        if mod in ("__init__",):
            continue
        users = []
        for other in files:
            if other == f:
                continue
            try:
                s = other.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            if re.search(rf"\b{re.escape(mod)}\b", s):
                users.append(other.relative_to(ROOT).as_posix())
        rows.append((mod, users))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="架构接线审计")
    ap.add_argument("--dead", action="store_true", help="详细列出没人调用的函数")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    files = all_py()

    print("=" * 70)
    print("① 工具链：模型能输出的标记，是否都有处理分支")
    print("=" * 70)
    kinds, problems = check_tool_chain()
    print(f"认识的标记（{len(kinds)} 个）：{', '.join(kinds)}")
    if problems:
        for p in problems:
            print(p)
    else:
        print("✅ 全部有处理分支")

    print()
    print("=" * 70)
    print("② 模块接线：services/ 每个模块被谁用")
    print("=" * 70)
    rows = check_modules()
    orphan = []
    for mod, users in rows:
        if not users:
            orphan.append(mod)
            print(f"❌ {mod:<20} 没有任何地方引用（孤儿模块）")
        else:
            print(f"✅ {mod:<20} ← {', '.join(users[:4])}"
                  + (f" 等 {len(users)} 处" if len(users) > 4 else ""))

    print()
    print("=" * 70)
    print("③ 函数接线：分两档看（别被噪音淹了）")
    print("=" * 70)
    dead_real = []      # 全项目 0 引用 → 真死代码
    dead_inner = []     # 只在本模块内部被调用 → 正常（内部工具函数）
    for f in sorted(SERVICES.glob("*.py")):
        rel = f.relative_to(ROOT).as_posix()
        for name, lineno in module_funcs(f):
            if name.startswith("_"):
                continue
            refs = count_refs(name, files, f)
            ext = [r for r in refs if r[0] != rel]
            if ext:
                continue
            inner = refs[0][1] if refs else 0
            (dead_real if inner <= 0 else dead_inner).append((rel, name, lineno, inner))

    if dead_real:
        print(f"❌ 纯死代码（全项目 0 引用，建议删）：{len(dead_real)} 个")
        for path, name, ln, _ in dead_real:
            print(f"   · {path}::{name}()  L{ln}")
    else:
        print("✅ 没有纯死代码")

    print()
    print(f"ℹ️ 仅模块内部使用（正常，不算死代码）：{len(dead_inner)} 个")
    if args.dead:
        for path, name, ln, inner in dead_inner:
            print(f"   · {path}::{name}()  L{ln}（本模块内 {inner} 次）")
    else:
        print("   （加 --dead 看明细。这类都是 tokenize / split_lines 之类的内部工具函数）")

    print()
    print("=" * 70)
    print(f"小结：孤儿模块 {len(orphan)}｜真死代码 {len(dead_real)}"
          f"｜内部函数 {len(dead_inner)}｜工具链问题 {len(problems)}")
    return 1 if (orphan or dead_real or problems) else 0


if __name__ == "__main__":
    raise SystemExit(main())
