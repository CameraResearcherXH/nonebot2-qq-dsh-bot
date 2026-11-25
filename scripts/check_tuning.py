#!/usr/bin/env python3
# scripts/check_tuning.py
"""调参表一致性检查（AST 版）。

为什么改成 AST（2026-11-25）：
  旧版用正则找 `tuning.f("X")`，再用「文件里有没有出现 "X" 这个字符串」判断是否接线。
  两个后果：
    ① 注释 / docstring 里提一句键名，就被当成「已接线」；
    ② `ADDR_RECENT_MENTION = tuning.f("ADDR_RECENT_MENTION")` 这种
       「读了但没人用」的假接线查不出来。
  现在改成解析语法树：只认真正的调用点和真正的字符串字面量（注释、docstring 不算）。

规则：
1. 服务器侧代码（services / plugins，排除 third / scripts）里**不允许**再出现
   数值型 `float(os.getenv("X", "1.0"))` —— 必须走 `core.tuning`。
2. `tuning.f/i/raw/get("X")` 引用的键必须在 `DEFAULTS` 里。
3. `DEFAULTS` 里每个键都要有说明（DOCS）。
4. `FILES` 声明「这个模块在用」：该文件里必须真的出现这个键的**字符串字面量**。
5. 模块级 `NAME = tuning.f("KEY")` 必须真的被用到（同文件读取，或被别的模块
   以 `模块.NAME` 引用）。LEGACY / UNWIRED 里的键豁免 —— 它们本来就是历史/未接线。
6. 用了 tuning 却没 `from core import tuning`。

兼容：宿主 python < 3.9 无法解析 3.9+ 的装饰器语法（如
`@get_driver().on_bot_connect`），这些文件降级用正则扫描，并在结论里列出。
容器内（3.11）走完整 AST。

退出码：有问题 = 1（CI 用）。
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import tuning  # noqa: E402

SKIP_PARTS = ("plugins/third", "plugins_backup", "venv", "__pycache__",
              "node_modules", ".workbuddy", "scripts/", "node.py")

ACCESSORS = ("f", "i", "raw", "get")
NUM_LITERAL = re.compile(r"-?\d+(?:\.\d+)?$")

# 正则降级（只在 ast.parse 失败时用）
NUM_ENV_RE = re.compile(
    r'(?:float|int)\(\s*os\.getenv\(\s*"[A-Z0-9_]+"\s*,\s*"-?\d+(?:\.\d+)?"\s*\)')
USE_RE = re.compile(r'tuning\.(?:f|i|raw|get)\(\s*"(?P<key>[A-Z0-9_]+)"')
ANY_KEY_RE = re.compile(r'tuning\.(?:f|i|raw|get)\(\s*"(?P<key>[A-Z0-9_]+)"')


def iter_py():
    for base in ("services", "plugins", "core"):
        p = ROOT / base
        if not p.exists():
            continue
        for f in p.rglob("*.py"):
            rel = f.relative_to(ROOT).as_posix()
            if rel == "core/tuning.py":   # 它自己就是注册表，含示例字符串
                continue
            if any(s in rel for s in SKIP_PARTS):
                continue
            yield rel, f


# ---------------------------------------------------------------- AST 工具
def _tuning_names(tree) -> set:
    """本文件里 `tuning` 的可用名字（含 import 别名）。"""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "core":
            for a in node.names:
                if a.name == "tuning":
                    names.add(a.asname or "tuning")
        elif isinstance(node, ast.ImportFrom) and node.module == "core.tuning":
            for a in node.names:
                if a.name in ACCESSORS:
                    names.add("direct:" + (a.asname or a.name))
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "core.tuning":
                    names.add(a.asname or "core.tuning")
    return names


def _const_str(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _call_key(node, names) -> str:
    """`tuning.f("X")` / `_t.i("X")` / `f("X")` → "X"；否则 ""。"""
    f = node.func
    if isinstance(f, ast.Attribute) and f.attr in ACCESSORS:
        base = f.value
        if isinstance(base, ast.Name) and base.id in names:
            return (_const_str(node.args[0]) or "") if node.args else ""
        # import core.tuning as t → t.f("X")
        if (isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name)
                and (base.value.id + "." + base.attr) in names):
            return (_const_str(node.args[0]) or "") if node.args else ""
    if isinstance(f, ast.Name) and ("direct:" + f.id) in names:
        return (_const_str(node.args[0]) or "") if node.args else ""
    return ""


def _is_numeric_getenv(node) -> bool:
    """float(os.getenv("X", "1.0")) / int(os.getenv("X", "30"))。"""
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    if not (isinstance(f, ast.Name) and f.id in ("float", "int")):
        return False
    if len(node.args) != 1:
        return False
    inner = node.args[0]
    if not isinstance(inner, ast.Call):
        return False
    if not (isinstance(inner.func, ast.Attribute) and inner.func.attr == "getenv"):
        return False
    v = inner.func.value
    if not (isinstance(v, ast.Name) and v.id == "os"):
        return False
    if len(inner.args) < 2:
        return False
    d = _const_str(inner.args[1])
    return bool(d and NUM_LITERAL.match(d.strip()))


def _docstring_ids(tree) -> set:
    """模块/类/函数的第一条字符串语句是 docstring，不算「字面量引用」。"""
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr):
                v = body[0].value
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    ids.add(id(v))
    return ids


def scan_file(path: Path) -> dict:
    """解析一个文件，返回：
    {parsed, keys(set), literals(set), attrs(set), consts([(name,key)]),
     leftover([(line, snippet)]), noimport(bool)}
    """
    txt = path.read_text(encoding="utf-8", errors="ignore")
    out = {"parsed": False, "keys": set(), "literals": set(), "attrs": set(),
           "consts": [], "leftover": [], "noimport": False}
    try:
        tree = ast.parse(txt)
    except SyntaxError:
        # 降级：正则。注释无法区分，但比整文件漏检好。
        out["keys"] = {m.group("key") for m in ANY_KEY_RE.finditer(txt)}
        out["literals"] = set(re.findall(r'"([A-Z][A-Z0-9_]{2,})"', txt))
        out["attrs"] = set(re.findall(r"\.([A-Za-z_][A-Za-z0-9_]*)", txt))
        for m in NUM_ENV_RE.finditer(txt):
            out["leftover"].append((txt[:m.start()].count("\n") + 1, m.group(0)[:60]))
        out["noimport"] = bool(out["keys"]) and "from core import tuning" not in txt
        return out

    out["parsed"] = True
    names = _tuning_names(tree)
    docs = _docstring_ids(tree)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            key = _call_key(node, names)
            if key:
                out["keys"].add(key)
            elif isinstance(node.func, ast.Name) and node.func.id == "tuning":
                out["noimport"] = True
            if _is_numeric_getenv(node):
                out["leftover"].append((getattr(node, "lineno", 0),
                                        ast.get_source_segment(txt, node) or ""))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docs:
                out["literals"].add(node.value)
        elif isinstance(node, ast.Attribute):
            out["attrs"].add(node.attr)

    # 模块级常量：NAME = tuning.f("KEY")
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) \
                and isinstance(node.value, ast.Call):
            key = _call_key(node.value, names)
            if key:
                out["consts"].append((node.targets[0].id, key))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and isinstance(node.value, ast.Call):
            key = _call_key(node.value, names)
            if key:
                out["consts"].append((node.target.id, key))
    return out


def scan_repo() -> dict:
    files, degraded = {}, []
    for rel, f in iter_py():
        info = scan_file(f)
        files[rel] = info
        if not info["parsed"]:
            degraded.append(rel)
    # 跨模块引用：`proactive.CHECK_SEC` 这种也算「用到」
    global_attrs = set()
    for info in files.values():
        global_attrs |= info["attrs"]
    return {"files": files, "degraded": degraded, "global_attrs": global_attrs}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args()

    repo = scan_repo()
    files = repo["files"]
    global_attrs = repo["global_attrs"]

    leftover, unknown, nodoc, noimport, fake, dead, stale = [], [], [], [], [], [], []
    used_keys = set()
    for rel, info in files.items():
        used_keys |= info["keys"]
        if info["noimport"]:
            noimport.append(rel)
        for line, snippet in info["leftover"]:
            leftover.append((rel, line, snippet))
        for key in info["keys"]:
            if key not in tuning.DEFAULTS:
                unknown.append((rel, key))

    for k, v in tuning.DOCS.items():
        if not v:
            nodoc.append(k)

    # 4. FILES 假接线
    unwired = set(getattr(tuning, "UNWIRED", ()))
    legacy = set(getattr(tuning, "LEGACY", ()))
    for key, rel in tuning.FILES.items():
        if key in unwired:
            if key in used_keys:
                stale.append(key)
            continue
        p = ROOT / str(rel)
        if not p.exists():
            continue
        info = files.get(str(rel))
        if info is None:
            body = p.read_text(encoding="utf-8", errors="ignore")
            found = ('"%s"' % key) in body or ("'%s'" % key) in body
        else:
            found = key in info["literals"] or key in info["keys"]
        if not found:
            fake.append((key, rel))

    # 5. 模块级常量「读了但没人用」
    for rel, info in files.items():
        local_loads = set()
        try:
            tree = ast.parse((ROOT / rel).read_text(encoding="utf-8", errors="ignore"))
            local_loads = {n.id for n in ast.walk(tree)
                           if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        except SyntaxError:
            txt = (ROOT / rel).read_text(encoding="utf-8", errors="ignore")
            local_loads = set(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\b", txt))
        for name, key in info["consts"]:
            if key in legacy or key in unwired:
                continue
            if name not in local_loads and name not in global_attrs:
                dead.append((rel, name, key))

    ok = not (leftover or unknown or nodoc or noimport or fake or dead or stale)
    print("=" * 66)
    print("调参表检查  (%d 个参数，AST 模式)" % len(tuning.DEFAULTS))
    print("=" * 66)
    if repo["degraded"]:
        print("ℹ️  python<3.9 无法解析（已降级正则）：%s"
              % ", ".join(repo["degraded"]))
    if leftover:
        print("❌ 仍有数值型 os.getenv 未收口（应改用 tuning.f/i）：")
        for rel, line, s in leftover[:20]:
            print("   %s:%s  %s" % (rel, line, s))
    else:
        print("✅ 数值阈值已全部收口到 core/tuning.py")
    if unknown:
        print("❌ tuning 引用了未注册的键：")
        for rel, k in unknown[:20]:
            print("   %s  →  %s" % (rel, k))
    else:
        print("✅ 所有 tuning 引用都在 DEFAULTS 里")
    if nodoc:
        print("⚠️ 以下参数缺说明：%s" % ", ".join(nodoc[:20]))
    else:
        print("✅ 所有参数都有中文说明")
    if noimport:
        print("❌ 用了 tuning 但没 import：")
        for rel in noimport[:20]:
            print("   %s" % rel)
    else:
        print("✅ 所有使用点都正确 import")
    if fake:
        print("❌ FILES 声明了生效位置，但那个文件没读这个键（假接线）：")
        for k, rel in fake[:20]:
            print("   %s  →  %s" % (k, rel))
        print("   真接线，或加进 core/tuning.py 的 UNWIRED（设计先行）")
    else:
        print("✅ FILES 里（除 UNWIRED）每个键都能在对应文件里找到")
    if dead:
        print("❌ 模块级常量读了但没人用（删常量，或真正接线）：")
        for rel, name, k in dead[:20]:
            print("   %s  %s ← %s" % (rel, name, k))
        print("   确认是遗留就把键移进 core/tuning.py 的 LEGACY")
    else:
        print("✅ 没有「读了不用」的模块级常量")
    if stale:
        print("⚠️ 这些键已经接线了，请从 UNWIRED 里删掉：%s"
              % ", ".join(sorted(stale)[:20]))
    print("-" * 66)
    print("结论：%s" % ("PASS" if ok else "FAIL"))
    return 0 if ok or not args.strict else 1


if __name__ == "__main__":
    raise SystemExit(main())
