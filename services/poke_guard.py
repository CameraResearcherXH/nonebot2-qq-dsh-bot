# services/poke_guard.py
"""戳一戳频率屏蔽（2026-11-23 用户提出）。

需求原话：
    「用户有的时候会一直戳一戳。如果在 2 小时内该用户戳超过三次则不予回复
      戳一戳消息。2 小时后再看该用户的戳一戳。等于说对次数加一个屏蔽。」

做法：按 **用户 QQ** 记一个 2 小时滑动窗口。
    · 窗口内第 1~3 次戳 → 正常回（她可以戳回去）；
    · 第 4 次起 → 不回、也不戳回，只记进上下文；
    · 最早那次戳滑出 2 小时窗口后，计数自动降下来，恢复回复。

配置（core/tuning.py 默认值，`.env` 同名可覆盖）：
    POKE_MAX_PER_WINDOW = 3     窗口内最多回几次
    POKE_WINDOW_HOURS   = 2     窗口长度（小时）
"""
from __future__ import annotations

import threading
import time

from core import tuning

MAX_PER_WINDOW = tuning.i("POKE_MAX_PER_WINDOW")
WINDOW_SEC = tuning.f("POKE_WINDOW_HOURS") * 3600.0

_LOCK = threading.RLock()
_HITS: dict[str, list[float]] = {}      # uid -> [ts, ...]


def note(uid, now: float | None = None) -> tuple[bool, int]:
    """记一次戳。返回 (是否还允许回复, 窗口内第几次)。"""
    q = str(uid or "").strip()
    if not q:
        return True, 0
    t = float(now if now is not None else time.time())
    with _LOCK:
        hits = [x for x in (_HITS.get(q) or []) if t - x < WINDOW_SEC]
        hits.append(t)
        _HITS[q] = hits
        return len(hits) <= MAX_PER_WINDOW, len(hits)


def count(uid, now: float | None = None) -> int:
    """窗口内已经戳了几次（不新增）。"""
    q = str(uid or "").strip()
    if not q:
        return 0
    t = float(now if now is not None else time.time())
    with _LOCK:
        hits = [x for x in (_HITS.get(q) or []) if t - x < WINDOW_SEC]
        _HITS[q] = hits
        return len(hits)


def reset(uid=None) -> None:
    with _LOCK:
        if uid is None:
            _HITS.clear()
        else:
            _HITS.pop(str(uid or "").strip(), None)


def status() -> str:
    with _LOCK:
        rows = [(u, len(v)) for u, v in _HITS.items() if v]
    if not rows:
        return "戳一戳屏蔽：窗口内没有记录"
    return "戳一戳屏蔽：" + "、".join(f"{u}×{n}" for u, n in rows[:8])
