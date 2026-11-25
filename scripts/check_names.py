#!/usr/bin/env python3
# scripts/check_names.py
"""粗粒度「未定义全局名」检查（pyflakes 的廉价替代）。

能抓到的典型事故：重构时把一个模块级常量（如 BACKEND）删了，
但函数里还在用 —— 编译能过、静态检查也能过，一跑就 NameError。

做法：收集文件里所有「被赋值的名字」（任意作用域）+ import + def/class，
再看哪些 Name 是 Load 但从未出现在这个集合里（且不是内置/模块属性）。
"""
import argparse
import ast
import builtins
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP = ("plugins/third", "plugins_backup", "venv", "__pycache__",
        "node_modules", ".workbuddy", "node.py")

BUILTINS = set(dir(builtins)) | {"__name__", "__file__", "__doc__", "self", "cls",
                                 "True", "False", "None"}


def collect_defined(tree):
    names = set()

    def add_target(node):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, (ast.Tuple, ast.List)):
            for e in node.elts:
                add_target(e)
        elif isinstance(node, ast.Starred):
            add_target(node.value)

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
            args = getattr(node, "args", None)
            if args:
                for a in (list(args.posonlyargs) + list(args.args)
                          + list(args.kwonlyargs)):
                    names.add(a.arg)
                if args.vararg:
                    names.add(args.vararg.arg)
                if args.kwarg:
                    names.add(args.kwarg.arg)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                add_target(t)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            add_target(node.target)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            add_target(node.target)
        elif isinstance(node, ast.withitem):
            if node.optional_vars is not None:
                add_target(node.optional_vars)
        elif isinstance(node, ast.ExceptHandler):
            if node.name:
                names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.comprehension):
            add_target(node.target)
        elif isinstance(node, ast.Lambda):
            for a in (list(node.args.posonlyargs) + list(node.args.args)
                      + list(node.args.kwonlyargs)):
                names.add(a.arg)
        elif isinstance(node, ast.Global) or isinstance(node, ast.Nonlocal):
            names.update(node.names)
        # match 语句是 3.10+ 语法；宿主 3.8 的 ast 没有这些节点类型。
        # 用 getattr 兜底，让本脚本在 3.8 也能跑（容器 3.11 走完整分支）。
        elif isinstance(node, getattr(ast, "MatchAs", ())):
            if node.name:
                names.add(node.name)
        elif isinstance(node, getattr(ast, "MatchStar", ())):
            if node.name:
                names.add(node.name)
    return names


def check_file(p: Path):
    try:
        tree = ast.parse(p.read_text(encoding="utf-8", errors="ignore"))
    except SyntaxError as e:
        return [("syntax", str(e), 0)]
    defined = collect_defined(tree)
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id not in defined and node.id not in BUILTINS:
                bad.append((node.id, "", node.lineno))
    return bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args()

    problems = []
    for base in ("services", "plugins", "core", "scripts"):
        d = ROOT / base
        if not d.exists():
            continue
        for f in d.rglob("*.py"):
            rel = f.relative_to(ROOT).as_posix()
            if any(s in rel for s in SKIP) or rel.startswith("scripts/_"):
                continue
            for name, why, line in check_file(f):
                problems.append((rel, line, name, why))

    print("=" * 66)
    print("未定义名字检查（粗粒度）")
    print("=" * 66)
    if not problems:
        print("✅ 没有发现「用了但没定义」的全局名")
        return 0
    # 过滤明显误报：动态注入（globals()/exec）的文件单独提示
    for rel, line, name, why in problems[:60]:
        print("  %s:%s  %s %s" % (rel, line, name, why))
    print("-" * 66)
    print("合计 %d 处（如果是 globals()/exec 动态注入，可忽略）" % len(problems))
    return 0 if not args.strict else 1


if __name__ == "__main__":
    raise SystemExit(main())
