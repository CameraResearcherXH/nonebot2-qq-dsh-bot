# services/watchdog.py
"""事件循环 lag 自检（docs/overall-design-20261128.md §7，AstrBot watchdog 思路）。

每 LAG_CHECK_SEC 秒量一次 asyncio.sleep 的迟到量；超过 LAG_WARN_MS 就把
未完成任务的栈打进日志（节流 120s），方便直接看出卡在哪只协程。
只诊断：不杀任务、不重启进程。启动阶段（插件加载、资源同步）天然阻塞，
先预热 WARMUP_SEC 秒再开始判定，避免重启就报一次假阳性。
"""
import asyncio
import os
import time

from nonebot import logger

from services import diag

LAG_MS = float(os.getenv("LAG_WARN_MS", "1500") or 1500)
INTERVAL = max(0.5, float(os.getenv("LAG_CHECK_SEC", "2") or 2))
WARMUP_SEC = float(os.getenv("LAG_WARMUP_SEC", "60") or 60)
_REPORT_GAP = 120.0

_last_report = 0.0


def _dump_stacks(limit: int = 25) -> list:
    lines = []
    for t in asyncio.all_tasks():
        if t.done():
            continue
        name = t.get_name()
        stack = t.get_stack(limit=3)
        if not stack:
            lines.append(f"  · {name}: (suspended, no stack)")
        for frame, lineno in stack:
            lines.append(f"  · {name}: {frame.f_code.co_filename}:{lineno} "
                         f"in {frame.f_code.co_name}")
        if len(lines) >= limit:
            break
    return lines[:limit]


async def loop() -> None:
    global _last_report
    await asyncio.sleep(WARMUP_SEC)
    while True:
        t0 = time.perf_counter()
        await asyncio.sleep(INTERVAL)
        lag = (time.perf_counter() - t0 - INTERVAL) * 1000
        if lag < LAG_MS:
            continue
        now = time.time()
        if now - _last_report < _REPORT_GAP:
            continue
        _last_report = now
        try:
            lines = _dump_stacks()
            diag.emit("lag", ms=round(lag), tasks=len(asyncio.all_tasks()),
                      top=lines[:6])
            logger.warning(f"[WATCHDOG] 事件循环 lag {lag:.0f}ms，卡住栈：\n"
                           + "\n".join(lines))
        except Exception as e:
            logger.debug(f"[WATCHDOG] 记录失败: {type(e).__name__}: {e}")
