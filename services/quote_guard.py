# services/quote_guard.py
"""引用互刷护栏（纯结构信号，不猜语义；2026-11-26）。

真机事故：17:08:49~17:09:13，月下橘（另一个 AI）引用她 4 次，她回了 4 次，
45 秒里互相引用了一串。根因不是「她判断错」，而是 `_quote_me()` 读不到被引用者，
「引用她」被 `is_tome()` 当成「@她」→ 必回。

这里只回答一个结构问题：**同一对人，短时间内是不是在互相引用刷屏？**
  · 120s 内第 3 次 → 判定互刷；
  · 判定后冷却 5 分钟（冷却期内直接返回 True，调用方不接）；
  · 不读文本、不猜谁是人谁是 AI。

独立成 service 的原因：`plugins/chat` 依赖 nonebot，离线单测 import 不了；
放在这里 `scripts/test_new_features.py` 可以直接验证。
"""
from __future__ import annotations

import time

WINDOW = 120.0
MAX_HITS = 3
BLOCK_SEC = 300.0

_EVENTS: dict = {}      # (gid, pair) -> [ts, ...]
_BLOCK: dict = {}       # (gid, pair) -> until_ts


def hit(gid, a, b, *, log=None) -> bool:
    """这一对是不是已经在互相引用刷屏了（或还在冷却期）。"""
    try:
        pair = tuple(sorted((str(a), str(b))))
        key = (str(gid), pair)
        now = time.time()
        until = float(_BLOCK.get(key) or 0.0)
        if until > now:
            if log:
                log(f"[LOOP] {gid} {pair[0][-4:]}/{pair[1][-4:]} 还在冷却"
                    f"（剩 {int(until - now)}s）→ 不接")
            return True
        ts = [t for t in _EVENTS.get(key, []) if now - t <= WINDOW]
        ts.append(now)
        _EVENTS[key] = ts[-8:]
        if len(ts) >= MAX_HITS:
            _BLOCK[key] = now + BLOCK_SEC
            if log:
                log(f"[LOOP] {gid} {pair[0][-4:]}/{pair[1][-4:]} "
                    f"{int(WINDOW)}s 内互相引用 {len(ts)} 次 → "
                    f"不再硬接，冷却 {int(BLOCK_SEC / 60)} 分钟")
            return True
    except Exception:
        pass
    return False


def reset() -> None:
    """测试用：清空所有计数与冷却。"""
    _EVENTS.clear()
    _BLOCK.clear()
