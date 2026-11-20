#!/usr/bin/env python3
# scripts/check_tuning.py
"""调参表一致性检查。

规则：
1. 服务器侧代码（services / plugins，排除 third）里**不允许**再出现数值型
   `float(os.getenv("X", "1.0"))` —— 必须走 `core.tuning`。
2. `tuning.f("X")` / `tuning.i("X")` 引用的键必须在 `DEFAULTS` 里。
3. `DEFAULTS` 里每个键都要有说明（DOCS）。

退出码：有问题 = 1（CI 用）。
"""
import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import tuning  # noqa: E402

SKIP_PARTS = ("plugins/third", "plugins_backup", "venv", "__pycache__",
              "node_modules", ".workbuddy", "scripts/", "node.py")

NUM_ENV = re.compile(
    r'(?:float|int)\(\s*os\.getenv\(\s*"[A-Z0-9_]+"\s*,\s*"-?\d+(?:\.\d+)?"\s*\)')
USE = re.compile(r'tuning\.(?:f|i|raw|get)\("(?P<key>[A-Z0-9_]+)"\)')


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args()

    leftover, unknown, nodoc, noimport = [], [], [], []
    for rel, f in iter_py():
        txt = f.read_text(encoding="utf-8", errors="ignore")
        if re.search(r"(?<![\w.])tuning\.(?:f|i|raw|get)\(", txt) \
                and "from core import tuning" not in txt:
            noimport.append(rel)
        for m in NUM_ENV.finditer(txt):
            line = txt[:m.start()].count("\n") + 1
            leftover.append((rel, line, m.group(0)[:60]))
        for m in USE.finditer(txt):
            if m.group("key") not in tuning.DEFAULTS:
                unknown.append((rel, m.group("key")))
    for k, v in tuning.DOCS.items():
        if not v:
            nodoc.append(k)

    ok = not (leftover or unknown or nodoc or noimport)
    print("=" * 66)
    print("调参表检查  (%d 个参数)" % len(tuning.DEFAULTS))
    print("=" * 66)
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
    print("-" * 66)
    print("结论：%s" % ("PASS" if ok else "FAIL"))
    return 0 if ok or not args.strict else 1


if __name__ == "__main__":
    raise SystemExit(main())
