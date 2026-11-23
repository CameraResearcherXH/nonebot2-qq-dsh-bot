#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""量一下「角色卡 + 规矩」到底多少字、哪里在重复（2026-11-23）。

用法（容器里）：
    python3 scripts/analyze_prompt_size.py [group_id]

输出：
  · 每个规矩小节的字数 / 占比
  · 角色卡与规矩、规矩小节之间的重复句（归一化后完全相同）
  · 角色卡内部的重复句
"""
from __future__ import annotations

import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from services import llm  # noqa: E402
from services import persona as persona_mod  # noqa: E402


def norm(s: str) -> str:
    s = re.sub(r"\s+", "", str(s or ""))
    s = s.replace("，", ",").replace("。", ".").replace("；", ";")
    s = s.replace("：", ":").replace("！", "!").replace("？", "?")
    s = s.replace("「", "").replace("」", "").replace("“", "").replace("”", "")
    s = s.replace("**", "").replace("`", "").replace("—", "").replace("-", "")
    return s


def sentences(text: str) -> list[str]:
    out = []
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for s in re.split(r"(?<=[。！？!?；;\n])", line):
            s = s.strip(" \t-·*")
            if len(norm(s)) >= 8:
                out.append(s)
    return out


def main() -> int:
    gid = sys.argv[1] if len(sys.argv) > 1 else "100000017"
    key = f"group_{gid}"

    rules = llm._behaviour_protocol(key, is_group=True, has_ctx=True)
    card = ""
    try:
        name = persona_mod.persona_name(key)
        fp = ROOT / "services" / "Personality" / f"{name}.txt"
        if not fp.exists():
            cands = sorted((ROOT / "services" / "Personality").glob("*.txt"))
            fp = max(cands, key=lambda p: p.stat().st_size) if cands else fp
        card = fp.read_text(encoding="utf-8")
        print(f"角色卡文件：{fp.name}（{len(card)} 字）")
    except Exception as e:
        print("读角色卡失败:", e)

    print(f"规矩总字数：{len(rules)}")
    parts = re.split(r"\n\n(?=▍)", rules)
    rows = []
    for p in parts:
        m = re.match(r"▍([^\n]*)\n", p)
        rows.append((m.group(1) if m else "(开头说明)", len(p), p))
    print("\n== 规矩分节（按字数） ==")
    for t, n, _ in sorted(rows, key=lambda x: -x[1]):
        print(f"{n:6d}  {t}")
    print(f"{sum(n for _, n, _ in rows):6d}  合计")

    # 重复：角色卡 vs 规矩
    card_s = sentences(card)
    rule_s = []
    for t, _, p in rows:
        for s in sentences(p):
            rule_s.append((t, s))

    rule_norm = Counter(norm(s) for _, s in rule_s)
    dup_in_rules = {k: v for k, v in rule_norm.items() if v > 1}
    card_norm = Counter(norm(s) for s in card_s)
    dup_in_card = {k: v for k, v in card_norm.items() if v > 1}
    cross = set(card_norm) & set(rule_norm)

    def show(title, items, pool):
        print(f"\n== {title}：{len(items)} 组 ==")
        for k, v in sorted(items.items(), key=lambda x: -len(x[0]))[:40]:
            sample = next((s for s in pool if norm(s) == k), k)
            print(f"  ×{v}  {sample[:70]}")

    show("角色卡内部重复", dup_in_card, card_s)
    show("规矩内部重复", dup_in_rules, [s for _, s in rule_s])
    print(f"\n== 角色卡 × 规矩 重复：{len(cross)} 句 ==")
    for k in sorted(cross, key=lambda x: -len(x))[:60]:
        sample = next(s for s in card_s if norm(s) == k)
        print(f"  {sample[:70]}")

    # 5-gram 重叠（近似重复/同义改写）
    def grams(text, n=5):
        t = norm(text)
        return {t[i:i + n] for i in range(max(0, len(t) - n + 1))}

    cg = grams(card)
    rg = grams(rules)
    print(f"\n5-gram：角色卡 {len(cg)}｜规矩 {len(rg)}｜交集 {len(cg & rg)}"
          f"（{100 * len(cg & rg) / max(1, len(rg)):.1f}% 的规矩片段在角色卡里出现过）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
