#!/usr/bin/env python3
# scripts/audit_coverage.py
"""全仓「接线完整性」体检：找没接上 / 接了一半 / 接了但没人用的功能。

检查项：
  A 孤立服务模块      services/*.py 没有被任何插件或服务 import
  B 工具没接线        plugins 里定义了 _tool_xxx 但没有 register_tool
  C 工具处理器断线    register_tool(handler=X) 但 X 未定义
  D 命令重复注册      同一个 on_command 名字注册了两次（后者会顶掉前者）
  E 吞异常            except: pass / except Exception: pass 的分布（藏 bug）
  F 占位/未完成       TODO / FIXME / NotImplementedError / 函数体只有 pass
  G 参数没人读        tuning 里登记了，但代码里没有 tuning.f/i("KEY")
  H skill 开关没覆盖  注册了工具，但 /技能 关不掉它（没有归属组）

用法：python3 scripts/audit_coverage.py [--top N]
"""
import argparse
import ast
import collections
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SKIP = ("plugins/third", "plugins_backup", "venv", "__pycache__",
        ".workbuddy", "node.py", "scripts/")


def py_files(*bases):
    for b in bases:
        d = ROOT / b
        if d.is_file():
            yield d
            continue
        if not d.exists():
            continue
        for f in d.rglob("*.py"):
            rel = f.relative_to(ROOT).as_posix()
            if any(s in rel for s in SKIP):
                continue
            yield f


def read(p):
    try:
        return p.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


# ---------------- A 孤立服务模块 ----------------
def imported_names():
    """用 AST 收集所有被 import 的名字（能处理括号多行 import）。"""
    names = set()
    for f in py_files("plugins", "services", "core", "bot.py"):
        try:
            tree = ast.parse(read(f))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    names.add(a.name.split(".")[0])
                    names.add(a.name.split(".")[-1])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    names.add(node.module.split(".")[0])
                    names.add(node.module.split(".")[-1])
                for a in node.names:
                    names.add(a.name)
    # plugin.toml 里的 handler 也是真接线（例如 services.picsearch_tool:picsearch）。
    # 只按 AST 收 import 会把它误判成孤儿模块（2026-11-22）。
    for toml in (ROOT / "plugins").rglob("*.toml"):
        for m in re.finditer(r'handler\s*=\s*"([A-Za-z_][\w\.]*):([A-Za-z_]\w*)"',
                             read(toml)):
            mod = m.group(1)
            names.add(mod.split(".")[0])
            names.add(mod.split(".")[-1])
            names.add(m.group(2))
    return names


def check_orphans():
    names = imported_names()
    out = []
    for f in sorted((ROOT / "services").glob("*.py")):
        stem = f.stem
        if stem.startswith("_"):
            continue
        if stem not in names:
            out.append("services/%s.py" % stem)
    return out


# ---------------- B/C 工具接线 ----------------
def check_tools():
    registered = {}          # tool name -> handler name
    defined = collections.defaultdict(list)   # handler name -> file
    for f in py_files("plugins", "services"):
        txt = read(f)
        rel = f.relative_to(ROOT).as_posix()
        for m in re.finditer(r'register_tool\(\s*(?:spec\s*=\s*)?\{[^}]*?"name"\s*:\s*"([^"]+)"',
                             txt, re.S):
            registered.setdefault(m.group(1), rel)
        for m in re.finditer(r'register_tool\([\s\S]{0,600}?handler\s*=\s*([A-Za-z_]\w*)', txt):
            registered.setdefault("#" + m.group(1), rel)
        for m in re.finditer(r'(?:async\s+)?def\s+(_tool_[A-Za-z0-9_]+)\s*\(', txt):
            defined[m.group(1)].append(rel)

    all_text = "\n".join(read(f) for f in py_files("plugins", "services"))
    # B: 定义了 _tool_xxx 但除了 def 之外全项目再没出现过（= 没人注册、没人调用）
    unwired = []
    for fn, files in defined.items():
        if len(re.findall(r"\b%s\b" % re.escape(fn), all_text)) <= 1:
            unwired.append("%s  (%s)" % (fn, files[0]))
    # C: register_tool 里 handler=xxx 但 xxx 未定义
    broken = []
    for key, rel in registered.items():
        if not key.startswith("#"):
            continue
        fn = key[1:]
        if not re.search(r'(?:async\s+)?def\s+%s\s*\(' % re.escape(fn), all_text):
            broken.append("%s  (%s)" % (fn, rel))
    return unwired, broken


# ---------------- D 命令重复 ----------------
def check_dup_commands():
    seen = collections.defaultdict(list)
    for f in py_files("plugins"):
        txt = read(f)
        rel = f.relative_to(ROOT).as_posix()
        for m in re.finditer(r'on_command\(\s*"([^"]+)"', txt):
            seen[m.group(1)].append(rel)
    return {k: v for k, v in seen.items() if len(set(v)) > 1 or len(v) > 1}


# ---------------- E 吞异常 ----------------
def check_swallow():
    hits = collections.Counter()
    samples = collections.defaultdict(list)
    for f in py_files("plugins", "services", "core"):
        txt = read(f)
        rel = f.relative_to(ROOT).as_posix()
        for m in re.finditer(r'except[^\n:]*:\s*\n(\s*)(pass|continue)\s*(\n|$)', txt):
            hits[rel] += 1
            if len(samples[rel]) < 2:
                line = txt[:m.start()].count("\n") + 1
                samples[rel].append(line)
    return hits, samples


# ---------------- F 占位 ----------------
def check_placeholders():
    out = []
    for f in py_files("plugins", "services", "core"):
        rel = f.relative_to(ROOT).as_posix()
        txt = read(f)
        for i, line in enumerate(txt.splitlines(), 1):
            s = line.strip()
            if re.match(r"#\s*(TODO|FIXME|XXX|待实现|未实现)", s) or "NotImplementedError" in s:
                out.append("%s:%d  %s" % (rel, i, s[:70]))
        # 函数体只有 pass 的
        try:
            tree = ast.parse(txt)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                body = [b for b in node.body
                        if not (isinstance(b, ast.Expr) and isinstance(b.value, ast.Constant))]
                if len(body) == 1 and isinstance(body[0], ast.Pass):
                    out.append("%s:%d  def %s(): pass" % (rel, node.lineno, node.name))
    return out


# ---------------- G 参数没人读 ----------------
def check_unread_params():
    from core.tuning import DEFAULTS
    all_text = "\n".join(read(f) for f in py_files("plugins", "services", "core"))
    unused = []
    for k in DEFAULTS:
        if re.search(r'tuning\.(?:f|i|raw|get)\(\s*"%s"' % re.escape(k), all_text):
            continue
        unused.append(k)
    return unused


# ---------------- H skill 开关覆盖 ----------------
def check_skill_coverage():
    from services import skill_switch
    covered = set(skill_switch.CORE_SKILLS)
    for grp in skill_switch.SKILL_GROUPS.values():
        covered |= set(grp)
    all_text = "\n".join(read(f) for f in py_files("plugins", "services"))
    names = set()
    for m in re.finditer(r'register_tool\(\s*\{[^}]*?"name"\s*:\s*"([^"]+)"', all_text, re.S):
        names.add(m.group(1))
    return sorted(names - covered), sorted(names & set(skill_switch.CORE_SKILLS))


def check_command_gates():
    """有 on_command 的插件，是否挂了 skill_switch.gate（命令级能力开关）。"""
    out = []
    for f in py_files("plugins"):
        txt = read(f)
        rel = f.relative_to(ROOT).as_posix()
        n_cmd = len(re.findall(r'on_command\(\s*"', txt))
        if not n_cmd:
            continue
        n_gate = len(re.findall(r'(?:skill_switch|_sk)\.gate\(', txt))
        if n_gate == 0:
            out.append("%s  （%d 个命令，0 个闸门）" % (rel, n_cmd))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--strict", action="store_true",
                    help="结构性硬伤（孤立/未接线/断线/重复/占位/空转/未覆盖）任一非 0 就退出码 1")
    args = ap.parse_args()

    orphans = check_orphans()
    unwired, broken = check_tools()
    dups = check_dup_commands()
    swallow, samples = check_swallow()
    ph = check_placeholders()
    unread = check_unread_params()
    uncovered, core_tools = check_skill_coverage()
    nogate = check_command_gates()

    print("=" * 72)
    print("接线完整性体检")
    print("=" * 72)

    print("\n【A】孤立服务模块（services/*.py 没人 import）：%d" % len(orphans))
    for x in orphans:
        print("   ", x)

    print("\n【B】定义了 _tool_* 但没 register_tool：%d" % len(unwired))
    for x in unwired[:args.top]:
        print("   ", x)

    print("\n【C】register_tool 指向不存在的处理器：%d" % len(broken))
    for x in broken[:args.top]:
        print("   ", x)

    print("\n【D】命令重复注册：%d" % len(dups))
    for k, v in list(dups.items())[:args.top]:
        print("    /%s  ←  %s" % (k, "、".join(sorted(set(v)))))

    print("\n【E】吞异常（except: pass/continue）Top %d：" % args.top)
    for rel, n in swallow.most_common(args.top):
        print("    %-46s %3d 处  行 %s" % (rel, n, samples[rel]))

    print("\n【F】占位 / 未完成：%d" % len(ph))
    for x in ph[:args.top]:
        print("   ", x)

    print("\n【G】tuning 里登记但代码没读：%d" % len(unread))
    for x in unread[:args.top]:
        print("   ", x)

    print("\n【H】注册了工具但 /技能 关不掉（没归属组）：%d" % len(uncovered))
    for x in uncovered[:40]:
        print("   ", x)
    print("    核心（设计上不可关，已登记）：%d 个" % len(core_tools))

    print("\n【I】有命令但没挂能力闸门的插件：%d" % len(nogate))
    for x in nogate[:20]:
        print("   ", x)

    print("\n" + "-" * 72)
    print("汇总：孤立 %d｜工具未接线 %d｜处理器断线 %d｜命令重复 %d｜占位 %d｜参数空转 %d｜开关未覆盖 %d｜命令无闸门 %d"
          % (len(orphans), len(unwired), len(broken), len(dups), len(ph),
             len(unread), len(uncovered), len(nogate)))
    hard = (len(orphans) + len(unwired) + len(broken) + len(dups)
            + len(ph) + len(unread) + len(uncovered))
    if args.strict and hard:
        print("❌ 结构性硬伤 %d 处" % hard)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
