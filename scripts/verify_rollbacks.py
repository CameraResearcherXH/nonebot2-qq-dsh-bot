#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""回滚开关体检：确认每个「可回滚」的算法/档位真的按 env 切换。

为什么不在一个进程里测：
  `BACKEND` / `MOOD_UPDATE` / `POWER_MODE` / `RET_TOOLS_TOP` 等都是
  **模块导入时**读 env 的常量，同进程改 os.environ 不会重新生效。
  所以每个用例都起一个子进程，带不同 env 跑一小段断言。

用法：
    python scripts/verify_rollbacks.py
    python scripts/verify_rollbacks.py --only memory,mood

退出码：有 FAIL → 1。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _run(code: str, env_extra: dict) -> tuple[int, str]:
    env = dict(os.environ)
    env.update({k: str(v) for k, v in (env_extra or {}).items()})
    env["PYTHONPATH"] = str(ROOT)
    p = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=env,
                       capture_output=True, text=True, timeout=120)
    return p.returncode, (p.stdout + p.stderr).strip()


CASES = [
    # (名称, env, 代码, 期望输出里的子串)
    ("memory=json", {"MEMORY_BACKEND": "json"},
     "from services import memory_v2 as m\n"
     "print('BACKEND', m.BACKEND)\n"
     "print('TOTAL', m.stats().get('total'))\n",
     "BACKEND json"),

    ("memory=sqlite", {"MEMORY_BACKEND": "sqlite"},
     "from services import memory_v2 as m\n"
     "print('BACKEND', m.BACKEND)\n"
     "print('TOTAL', m.stats().get('total'))\n",
     "BACKEND sqlite"),

    ("mmr=0（纯分数）", {"MEM_MMR_ENABLED": "0"},
     "from services import memory_v2 as m\n"
     "S=[(1.0,{'tokens':'a b c','about_qq':'','delta':1}),"
     "(0.99,{'tokens':'a b c','about_qq':'','delta':1}),"
     "(0.98,{'tokens':'a b c','about_qq':'','delta':1}),"
     "(0.75,{'tokens':'x y z','about_qq':'2','delta':1})]\n"
     "print([x[1]['tokens'] for x in m._diversify(S,3)])\n",
     "['a b c', 'a b c', 'a b c']"),

    ("mmr=1（去近重复）", {"MEM_MMR_ENABLED": "1", "MEM_MMR_LAMBDA": "0.7"},
     "from services import memory_v2 as m\n"
     "S=[(1.0,{'tokens':'a b c','about_qq':'','delta':1}),"
     "(0.99,{'tokens':'a b c','about_qq':'','delta':1}),"
     "(0.98,{'tokens':'a b c','about_qq':'','delta':1}),"
     "(0.75,{'tokens':'x y z','about_qq':'2','delta':1})]\n"
     "print([x[1]['tokens'] for x in m._diversify(S,3)])\n",
     "'x y z'"),

    ("显著性倒 U 默认", {},
     "from services import memdb as d\n"
     "print([round(d.salience_weight(x),3) for x in (0.1,0.3,0.6,1.0)])\n",
     "[0.03, 0.36, 0.99, 0.6]"),

    ("显著性尾部可回滚", {"MEM_SAL_TAIL": "0.9"},
     "from services import memdb as d\n"
     "print(round(d.salience_weight(1.0),3))\n",
     "0.9"),

    ("τ_kind 可回滚", {"MEM_TAU_FACT": "30"},
     "from services import memdb as d\n"
     "print(d.tau_days('fact'))\n",
     "30.0"),

    ("mood=v2", {"MOOD_UPDATE": "v2"},
     "from services import mood\nprint('MODE', mood.MOOD_UPDATE)\n",
     "MODE v2"),

    ("mood=delta（旧口径）", {"MOOD_UPDATE": "delta"},
     "from services import mood\nprint('MODE', mood.MOOD_UPDATE)\n",
     "MODE delta"),

    ("power=full", {"POWER_MODE": "full"},
     "from services import config\n"
     "print('MODE', config.POWER_MODE, config.power_on('audit'), config.power_on('memory'))\n",
     "MODE full True True"),

    ("power=eco", {"POWER_MODE": "eco"},
     "from services import config\n"
     "print('MODE', config.POWER_MODE, config.power_on('audit'), config.power_on('memory'))\n",
     "MODE eco False True"),

    ("watch=lite", {"GROUP_WATCH_MODE": "lite"},
     "from services import config\nprint('MODE', config.GROUP_WATCH_MODE)\n",
     "MODE lite"),

    ("pipe_metrics=0", {"PIPE_METRICS": "0"},
     "from services import reply_pipeline as r\nprint('ON', r.METRICS_ON)\n",
     "ON False"),

    ("tools_top=0（全挂）", {"RET_TOOLS_TOP": "0"},
     "from services import llm\n"
     "names=['send','stay_silent']+[f't{i}' for i in range(6)]\n"
     "llm.TOOLS_SPEC=[{'type':'function','function':{'name':n,'description':'x'}} for n in names]\n"
     "llm.TOOL_SCOPES={n:{'group'} for n in names}\n"
     "print('N', len(llm.tools_for('group_1','')))\n",
     "N 8"),

    ("tools_top=2（裁剪）", {"RET_TOOLS_TOP": "2"},
     "from services import llm\n"
     "names=['send','stay_silent']+[f't{i}' for i in range(6)]\n"
     "llm.TOOLS_SPEC=[{'type':'function','function':{'name':n,'description':'x'}} for n in names]\n"
     "llm.TOOL_SCOPES={n:{'group'} for n in names}\n"
     "print('N', len(llm.tools_for('group_1','')))\n",
     "N 2"),

    ("long_doc=0（回滚普通回复）", {"LONG_DOC_ENABLED": "0"},
     "from services import llm\nprint('LD', llm.is_long_doc('详细讲讲这本书'))\n",
     "LD False"),

    ("long_doc=1（默认）", {"LONG_DOC_ENABLED": "1"},
     "from services import llm\nprint('LD', llm.is_long_doc('详细讲讲这本书'))\n",
     "LD True"),

    ("dual_write=0", {"MEMORY_DUAL_WRITE": "0"},
     "from services import memory_v2 as m\nprint('DW', m.DUAL_WRITE)\n",
     "DW False"),

    ("white_moon borrow=0", {"WHITE_MOON_BORROW": "0"},
     "from services import white_moon as w\nprint('B', w.BORROW)\n",
     "B 0"),
]


def main() -> int:
    ap = argparse.ArgumentParser(description="回滚开关体检")
    ap.add_argument("--only", default="", help="逗号分隔的名称子串，只跑匹配项")
    a = ap.parse_args()
    only = [x.strip() for x in a.only.split(",") if x.strip()]

    rows, fails = [], 0
    for name, env, code, expect in CASES:
        if only and not any(k in name for k in only):
            continue
        rc, out = _run(code, env)
        if "No module named 'nonebot'" in out:
            rows.append((name, None, "SKIP（本机没装 nonebot；容器内会跑）"))
            continue
        ok = rc == 0 and expect in out
        rows.append((name, ok, out.replace("\n", "｜")[:160]))
        if not ok:
            fails += 1

    print("=" * 64)
    print("回滚开关体检")
    print("=" * 64)
    for name, ok, out in rows:
        label = "SKIP" if ok is None else ("PASS" if ok else "FAIL")
        print(f"{label}  {name:<24} {out}")
    print("-" * 64)
    print(f"合计 {len(rows)} 项，FAIL {fails} 项")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
