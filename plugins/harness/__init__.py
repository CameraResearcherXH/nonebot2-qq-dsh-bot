# plugins/harness/__init__.py
"""QQ ⇄ DeepSeek Harness 会话桥（2026-11-24）。

用法（只有主人能用）：
    /h                 看状态
    /h 新              开一个新会话（上下文乱了 / 换任务）
    /h <需求>          把需求交给 harness（可以真的改仓库代码）
    /改代码 <需求>      同上

启用方式：
    .env: CODE_AGENT=harness
    同时 self_tools（自己写 Python 工具）会自动关闭，避免两边抢活。

后端配置见 services/harness.py 顶部注释。
"""
from __future__ import annotations

import asyncio
import os

from nonebot import on_command, logger
from nonebot.adapters.onebot.v11 import Bot, MessageEvent
from nonebot.params import CommandArg

from services import harness
from services import llm as llm_mod
from services.access import is_owner, display_name
from services.config import HARNESS_ENABLED

if HARNESS_ENABLED:
    _h = on_command("harness", aliases={"h", "改代码"}, priority=4, block=True)

    def _chunks(text: str, size: int = 1200):
        t = str(text or "")
        for i in range(0, len(t), size):
            yield t[i:i + size]

    async def _send_long(bot: Bot, event: MessageEvent, text: str):
        parts = list(_chunks(text))
        for i, p in enumerate(parts):
            if i:
                await asyncio.sleep(0.4)
            await bot.send(event, p)

    @_h.handle()
    async def _(bot: Bot, event: MessageEvent, arg=CommandArg()):
        uid = str(getattr(event, "user_id", "") or "")
        if harness.owner_only() and not is_owner(uid):
            await _h.finish("这个只有主人能用。")
        q = str(arg or "").strip()
        key = f"private_{uid}" if not getattr(event, "group_id", None) else \
            f"group_{getattr(event, 'group_id')}#{uid}"
        if not q or q in ("状态", "status"):
            await _h.finish(harness.status(key))
        if q in ("新", "new", "重置", "reset"):
            s = harness.reset(key)
            await _h.finish(f"开好了：{s.get('id')}\n工作目录：{s.get('cwd')}")
        await _h.send("🧰 交给 harness 了，可能要几分钟…")
        try:
            _nm = display_name(event)
        except Exception:
            _nm = uid
        out = await harness.ask(
            key, q, speaker_qq=uid, speaker_name=_nm,
            gid=str(getattr(event, "group_id", "") or ""),
        )
        await _send_long(bot, event, out)
        return

    # ---------- function calling：让她自己判断「这是改代码需求」 ----------
    async def _tool_harness_ask(args: dict) -> str:
        ctx = llm_mod.TOOL_CTX
        uid = str(ctx.get("speaker_qq") or "")
        if harness.owner_only() and not is_owner(uid):
            return "改代码只有主人能触发。"
        ev = ctx.get("event")
        gid = str(getattr(ev, "group_id", "") or "")
        key = f"private_{uid}" if not gid else f"group_{gid}#{uid}"
        out = await harness.ask(
            key, str(args.get("task") or ""), speaker_qq=uid,
            speaker_name=str(ctx.get("speaker_name") or uid), gid=gid)
        # 结果直接发出去，避免模型二次改写代码/diff
        bot = ctx.get("bot")
        try:
            if bot and ev:
                for i, p in enumerate(_chunks(out)):
                    if i:
                        await asyncio.sleep(0.4)
                    await bot.send(ev, p)
        except Exception as e:
            logger.debug(f"[HARNESS] 发送结果失败: {type(e).__name__}: {e}")
        return "结果已经原样发到会话里了。不要重复输出代码或 diff，只补一句简短说明。"

    async def _tool_harness_status(args: dict) -> str:
        uid = str(llm_mod.TOOL_CTX.get("speaker_qq") or "")
        if harness.owner_only() and not is_owner(uid):
            return "只有主人能看 harness 状态。"
        ev = llm_mod.TOOL_CTX.get("event")
        gid = str(getattr(ev, "group_id", "") or "")
        key = f"private_{uid}" if not gid else f"group_{gid}#{uid}"
        return harness.status(key)

    async def _tool_harness_reset(args: dict) -> str:
        uid = str(llm_mod.TOOL_CTX.get("speaker_qq") or "")
        if harness.owner_only() and not is_owner(uid):
            return "只有主人能重置 harness。"
        ev = llm_mod.TOOL_CTX.get("event")
        gid = str(getattr(ev, "group_id", "") or "")
        key = f"private_{uid}" if not gid else f"group_{gid}#{uid}"
        s = harness.reset(key)
        return f"新会话：{s.get('id')}｜目录：{s.get('cwd')}"

    llm_mod.register_tool(
        {
            "type": "function",
            "function": {
                "name": "harness_ask",
                "description": (
                    "把「改代码 / 加功能 / 修 bug」的需求交给本机 DeepSeek Harness 会话，"
                    "它可以真的读仓库、改文件、跑测试。只有主人能用。"
                    "结果会自动原样发到当前会话，你不要再重复代码或 diff。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task": {"type": "string",
                                 "description": "要改什么：现象/目标/涉及文件（尽量具体）"},
                    },
                    "required": ["task"],
                },
            },
        },
        _tool_harness_ask,
        scope=("private",),
    )
    llm_mod.register_tool(
        {
            "type": "function",
            "function": {
                "name": "harness_status",
                "description": "查看当前 harness 会话、工作目录、后端和超时配置。",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        _tool_harness_status,
        scope=("private",),
    )
    llm_mod.register_tool(
        {
            "type": "function",
            "function": {
                "name": "harness_reset",
                "description": "开一个新的 harness 会话（换任务或上下文乱了时用）。",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        _tool_harness_reset,
        scope=("private",),
    )
    logger.info("[HARNESS] 桥已启用（CODE_AGENT=harness）")
else:
    logger.debug("[HARNESS] CODE_AGENT!=harness，插件不注册")
