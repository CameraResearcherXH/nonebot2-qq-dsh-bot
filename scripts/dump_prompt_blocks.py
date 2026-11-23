#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把「角色卡 + 规矩」原样打到 stdout，方便人工审阅/对比。"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from services import llm, persona  # noqa: E402

gid = sys.argv[1] if len(sys.argv) > 1 else "100000017"
key = f"group_{gid}"
name = persona.persona_name(key)
card = (ROOT / "services" / "Personality" / f"{name}.txt").read_text(encoding="utf-8")

sys.stdout.write("########## 角色卡 ##########\n")
sys.stdout.write(card)
sys.stdout.write("\n\n########## 规矩 ##########\n")
sys.stdout.write(llm._behaviour_protocol(key, is_group=True, has_ctx=True))
sys.stdout.write("\n")
