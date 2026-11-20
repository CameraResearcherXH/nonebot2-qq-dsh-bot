# core/pipeline.py
"""一条消息的一生：observe → should_reply → handle。

本文件只定义**协议与注册点**，具体实现仍由 plugins/chat 注入，
避免一次性搬 8000 行导致行为漂移。

用法（在 chat 插件启动时）：
    from core import pipeline
    pipeline.set_observer(_early_observe)
    pipeline.set_decider(_need_reply)
    pipeline.set_handler(_enqueue)

之后新插件只要 `await pipeline.observe(event)` 就能统一记账，
不必再各自写 dialogue_log / group_scene / recent。
"""
from typing import Awaitable, Callable, Optional

Observer = Callable[[object], Awaitable[None]]
Decider = Callable[[object], Awaitable[bool]]
Handler = Callable[[object], Awaitable[None]]

_observer: Optional[Observer] = None
_decider: Optional[Decider] = None
_handler: Optional[Handler] = None


def set_observer(fn: Observer) -> None:
    global _observer
    _observer = fn


def set_decider(fn: Decider) -> None:
    global _decider
    _decider = fn


def set_handler(fn: Handler) -> None:
    global _handler
    _handler = fn


async def observe(event) -> None:
    """唯一记账入口（破冰/心情观察/对话日志/现场/记忆 pending）。"""
    if _observer is not None:
        await _observer(event)


async def should_reply(event) -> bool:
    """纯判断：白名单/睡眠/指令/媒体/关注/高刷。无 IO、无副作用。"""
    if _decider is None:
        return False
    return bool(await _decider(event))


async def handle(event) -> None:
    if _handler is not None:
        await _handler(event)


def registered() -> dict:
    return {"observer": _observer is not None,
            "decider": _decider is not None,
            "handler": _handler is not None}
