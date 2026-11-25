"""人设底色（DESIGN ADR-010）：warmth / forgiveness / trust_speed / reactivity / sensitivity。

为什么要参数化
--------------
旧版所有关系/情绪步长是**全局常数**：冷漠人设和热情人设被夸一句，好感涨得一样多；
被骂一句，掉得也一样快 —— 人设只剩语气，不像「同一个人」。

怎么用
------
- 默认值住 `core/tuning.py`（`PERSONA_*`）；
- 可选覆盖：`data/persona_traits.json`
  `{"小鲸鱼proplus": {"warmth": 0.7, "forgiveness": 0.4}}`；
- `traits_for(name)` 返回合并后的 dict，所有值夹在 [0,1]；
- `traits_for_session(session_key)` 用 `persona.persona_name()` 找当前人格。

消费点
------
- `mood_judge`：好感步长 `0.04 + 0.12×warmth`、道歉回补 `0.03×(0.5+forgiveness)`；
- `mood.observe`：`reactivity` / `sensitivity`；
- `relationship.bump`：trust 正向步长 ×`(0.5+trust_speed)`，负向 ×`(1.2−0.4×forgiveness)`。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from core import tuning

TRAITS_FILE = Path(os.getenv("PERSONA_TRAITS_FILE", "data/persona_traits.json"))

def _defaults() -> dict:
    # 每个键都写字面量：audit_coverage 靠静态扫描判断「参数有没有被读」，
    # 用字典间接引用会被误报成空转（2026-11-25 踩过）。
    return {
        "warmth": float(tuning.f("PERSONA_WARMTH")),
        "forgiveness": float(tuning.f("PERSONA_FORGIVENESS")),
        "trust_speed": float(tuning.f("PERSONA_TRUST_SPEED")),
        "reactivity": float(tuning.f("PERSONA_REACTIVITY")),
        "sensitivity": float(tuning.f("PERSONA_SENSITIVITY")),
        "expressiveness": float(tuning.f("PERSONA_EXPRESSIVENESS")),
        # 心情回落点 / 锚点目标（emotion §2.1，-0.3~0.3）：冷漠偏负、热情偏正。
        # 不是 [0,1] 量，下面单独夹范围。
        "baseline_v": float(tuning.f("PERSONA_BASELINE_V")),
    }


def _overrides() -> dict:
    try:
        raw = json.loads(TRAITS_FILE.read_text(encoding="utf-8")) or {}
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _clamp(v: float) -> float:
    return max(0.0, min(1.0, float(v)))


def traits_for(name: str = "") -> dict:
    out = _defaults()
    ov = _overrides().get(str(name or "")) or {}
    if isinstance(ov, dict):
        for k in out:
            if k in ov:
                try:
                    if k == "baseline_v":
                        out[k] = max(-0.3, min(0.3, float(ov[k])))
                    else:
                        out[k] = _clamp(ov[k])
                except Exception:
                    pass
    return out


def traits_for_session(session_key: str = "") -> dict:
    name = ""
    try:
        from . import persona
        name = persona.persona_name(str(session_key or ""))
    except Exception:
        name = ""
    return traits_for(name)


def status_text() -> str:
    d = _defaults()
    return "｜".join(f"{k}={v:.2f}" for k, v in d.items())
