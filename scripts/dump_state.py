#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打印「她此刻看到的数值」——验证参数有没有正确传达给模型。

用法（容器内）：
  python scripts/dump_state.py --session group_100000016 --qq 100000003

输出四段：
  ① 心情状态：value / P / judge / dv / label
  ② 关系四量：affinity / trust / respect / grudge / intimacy
  ③ 档案：bond_word / notes 条数 / 最近一条
  ④ status_block 原文（= 真正进 prompt 的那段）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default="group_100000016")
    ap.add_argument("--qq", default="")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    out = {"session": args.session, "qq": args.qq}
    try:
        from services import mood, relationship, profiles

        m = mood.get_mood(args.session) or {}
        out["mood"] = {
            # value 是内部 ±100；v 是外口径 ±1（状态卡显示的就是 v）
            "value": m.get("value"),
            "v": round(float(m.get("value") or 0.0) / 100.0, 4),
            "label": m.get("label"),
            "P": m.get("P"), "judge_n": m.get("judge_n"),
            "anchor_n": m.get("anchor_n"), "base_v": m.get("base_v"),
            "judge": m.get("judge"),
        }
        if args.qq:
            out["relation"] = relationship.state_of(args.qq)
            try:
                out["relation"]["grudge"] = round(
                    float((relationship._load().get(args.qq) or {}).get("grudge") or 0.0), 3)
            except Exception:
                pass
            out["affinity_trend"] = profiles.affinity_trend(args.qq)
            b = profiles.get_bond(args.qq) or {}
            out["bond"] = {"word": profiles.get_bond_word(args.qq),
                           "notes": len(b.get("notes") or []),
                           "last": (b.get("notes") or [{}])[-1]}
            out["profile"] = {k: (profiles.get_profile(args.qq) or {}).get(k)
                              for k in ("name", "call_name", "identity",
                                        "personality", "affinity_day_sum")}
        try:
            out["status_block"] = mood.status_block(args.session, speaker_qq=args.qq)
        except Exception as e:
            out["status_block"] = f"<{type(e).__name__}: {e}>"
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0

    print(f"会话 {args.session}｜说话人 {args.qq or '-'}")
    print("① 心情：", json.dumps(out.get("mood") or {}, ensure_ascii=False))
    if "relation" in out:
        print("② 关系：", json.dumps(out["relation"], ensure_ascii=False))
        print("   走势：", json.dumps(out.get("affinity_trend") or {}, ensure_ascii=False))
        print("③ 档案：", json.dumps(out.get("bond") or {}, ensure_ascii=False))
        print("   profile：", json.dumps(out.get("profile") or {}, ensure_ascii=False))
    print("④ status_block（真正进 prompt 的）:")
    print(out.get("status_block") or "（空）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
