#!/usr/bin/env python3
# scripts/verify_all.py
"""一键自检：把所有闸门跑一遍，给出「能不能上线」的结论。

用法：
    python3 scripts/verify_all.py            # 全部跑
    python3 scripts/verify_all.py --quick    # 跳过编译（只跑静态检查 + 单测）

包含：
  1. 语法编译（compileall）
  2. 依赖方向（plugins → services → core）
  3. 调参表一致性（阈值是否全部收口、有没有写错键）
  4. 功能链路体检（主动接话 / 安静 / 破冰 / 回复判定 / 表情 / 记忆召回）
  5. 新功能单测（记忆 / 开关 / 用量 / 插件）
  6. 第三方插件合规（UPSTREAM / LICENSE）
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(name, cmd):
    print("\n" + "=" * 70)
    print("▶ %s" % name)
    print("=" * 70)
    # 字节码缓存统一写到 /tmp：避免仓库里的 __pycache__ 被 root 写过之后，
    # 普通用户再跑就 PermissionError（真机上踩过）。
    env = dict(os.environ)
    env["PYTHONPYCACHEPREFIX"] = "/tmp/qgb_pycache"
    r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, env=env)
    out = (r.stdout or "") + (r.stderr or "")
    tail = out.strip().splitlines()
    for line in tail[-14:]:
        print("  " + line)
    print("  → 退出码 %d" % r.returncode)
    return r.returncode == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="跳过编译")
    ap.add_argument("--full", action="store_true",
                    help="额外跑全链路自检（selftest_all.py，约 1 分钟）")
    args = ap.parse_args()
    py = sys.executable

    gates = []
    if not args.quick:
        gates.append(("语法编译", [py, "-m", "compileall", "-q",
                                   "core", "services", "plugins", "scripts", "bot.py"]))
    gates += [
        ("依赖方向", [py, "scripts/check_deps.py", "--strict"]),
        ("调参表", [py, "scripts/check_tuning.py", "--strict"]),
        # 重构最容易犯的错：删了模块级常量但函数里还在用（编译能过，一跑就 NameError）
        ("名字检查", [py, "scripts/check_names.py", "--strict"]),
        # 新增顶层包（如 core/）没挂进容器 → 线上 ImportError、容器起不来
        ("挂载检查", [py, "scripts/check_mounts.py", "--strict"]),
        # 孤立模块 / 工具没接线 / 处理器断线 / 命令重复 / 占位 / 能力开关没覆盖
        ("接线检查", [py, "scripts/audit_coverage.py", "--strict"]),
        ("功能链路", [py, "scripts/audit_features.py", "--strict"]),
        ("新功能单测", [py, "scripts/test_new_features.py"]),
        ("第三方插件合规", [py, "scripts/audit_third_party.py"]),
    ]
    if args.full:
        gates.append(("全链路自检", [py, "scripts/selftest_all.py"]))

    results = []
    for name, cmd in gates:
        results.append((name, run(name, cmd)))

    print("\n" + "=" * 70)
    print("汇总")
    print("=" * 70)
    for name, ok in results:
        print("  %-16s %s" % (name, "PASS" if ok else "FAIL"))
    bad = [n for n, ok in results if not ok]
    print("-" * 70)
    print("结论：%s" % ("✅ 全部通过" if not bad else "❌ 失败 %d 项：%s"
                    % (len(bad), "、".join(bad))))
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
