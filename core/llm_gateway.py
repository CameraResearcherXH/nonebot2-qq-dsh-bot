# core/llm_gateway.py
"""模型调用的统一出口（协议层）。

core 不能 import services（依赖单向），所以这里只保存一个 provider 回调：
    from core import llm_gateway
    llm_gateway.register(llm.call_llm)      # 启动时注册
    reply, usage = await llm_gateway.chat(key, text, purpose="chat")

长期目标：provider/超时/重试/并发/预算都在这里收口；
短期先做转发 + 计时 + 用量记录，行为与直接 call_llm 一致。
"""
import time
from typing import Awaitable, Callable, Optional

Provider = Callable[..., Awaitable[tuple]]
UsageHook = Callable[..., None]
_provider: Optional[Provider] = None
_usage_hook: Optional[UsageHook] = None


def register(fn: Provider) -> None:
    global _provider
    _provider = fn


def register_usage_hook(fn: UsageHook) -> None:
    """由 services 侧注册（core 不反向 import services）。"""
    global _usage_hook
    _usage_hook = fn


async def chat(key: str, text: str, *, purpose: str = "chat", **kwargs):
    if _provider is None:
        raise RuntimeError("llm_gateway 未注册 provider（启动时应调用 register）")
    t0 = time.monotonic()
    reply, usage = await _provider(key, text, **kwargs)
    if _usage_hook is not None:
        try:
            _usage_hook(usage or {}, purpose=purpose, session=str(key))
        except Exception:
            pass
    return reply, usage, int((time.monotonic() - t0) * 1000)


def registered() -> bool:
    return _provider is not None
