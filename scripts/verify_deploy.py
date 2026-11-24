#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
部署自查：容器里跑的到底是哪一版？

为什么要有（用户 2026-11-19：「但是这几次的我没有看到有变更呀」）：
  改的是**开发机上的工作区**，服务器/容器里那份要真的覆盖上去才会变。
  漏传一个文件（尤其**新增文件**：services/mood_judge.py、services/api_usage.py）
  的典型表现不是报错，而是「什么都没变」——
  因为 plugins/chat 顶层 import 失败 → 整个聊天插件不加载，看起来像没更新。

跑法（容器里）：
    docker compose exec bot python scripts/verify_deploy.py
    docker compose exec bot python scripts/verify_deploy.py -v    # 全部列出

退出码 0 = 和清单一致；1 = 有缺失/不一致（说明这份代码不是最新的）。
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "deploy_manifest.txt"


def md5_of(p: Path) -> str:
    try:
        return hashlib.md5(p.read_bytes()).hexdigest()
    except Exception:
        return ""


def version_of(rel: str = "services/scene_auditor.py") -> str:
    """从文件里读版本号（不改代码、不需要 import）。"""
    try:
        src = (ROOT / rel).read_text(encoding="utf-8", errors="ignore")
        m = re.search(r'^VERSION\s*=\s*"([^"]+)"', src, re.M)
        return m.group(1) if m else "?"
    except Exception:
        return "?"


def main() -> int:
    ap = argparse.ArgumentParser(description="核对容器里的代码和 deploy_manifest.txt 是否一致")
    ap.add_argument("-v", "--verbose", action="store_true", help="列出全部文件")
    args = ap.parse_args()

    if not MANIFEST.exists():
        print(f"❌ 找不到清单：{MANIFEST}")
        print("   （清单是开发机上生成的，要一起上传）")
        return 1

    missing, stale, ok_n = [], [], 0
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([0-9a-f]{32})  (.+)$", line.strip())
        if not m:
            continue
        want, rel = m.group(1), m.group(2)
        p = ROOT / rel
        if not p.exists():
            missing.append(rel)
            continue
        got = md5_of(p)
        if got != want:
            stale.append((rel, want, got))
        else:
            ok_n += 1
            if args.verbose:
                print(f"  ✅ {rel}")

    ver = version_of()
    print("=" * 62)
    print(f"  代码版本（services/scene_auditor.py）：{ver}")
    print(f"  清单文件数：{ok_n + len(stale) + len(missing)}"
          f"｜一致 {ok_n}｜不一致 {len(stale)}｜缺失 {len(missing)}")
    print("=" * 62)

    if missing:
        print("\n❌ 缺失（多半是新增文件没上传）：")
        for rel in missing:
            print(f"   - {rel}")
        print("   注意：漏了 services/*.py 里的新文件，plugins/chat 会 import 失败 →")
        print("        整个聊天插件不加载，表现就是「改了但什么都没变」。")

    if stale:
        print("\n⚠️ 内容不是最新（上传了旧文件 / 没覆盖）：")
        for rel, want, got in stale:
            print(f"   - {rel}")
            print(f"       期望 {want}")
            print(f"       实际 {got}")

    if not missing and not stale:
        print("\n✅ 和清单完全一致，这份代码是最新的。")
        print("   如果行为还是旧的：确认容器**重启过**（改 py 不会热加载）：")
        print("     docker compose restart bot    # 或 up -d --build")
        return 0

    print("\n→ 把这些文件重新上传（保持相对路径），然后 restart：")
    print("   docker compose restart bot")
    return 1


if __name__ == "__main__":
    sys.exit(main())

