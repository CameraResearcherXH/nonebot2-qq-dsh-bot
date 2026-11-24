# services/llm.py
from core import tuning
from services import identity as identity_mod
import asyncio
import contextvars
import json
import os
import random
import re
import time
from collections import defaultdict
from pathlib import Path

import httpx
from nonebot import logger
from .config import (BOT_MODE, MOCK_REPLY, XAI_API_KEY, XAI_BASE_URL, XAI_MODEL,
                     PROXY, ROLEPLAY_API_KEY, ROLEPLAY_BASE_URL, ROLEPLAY_MODEL,
                     NODE_COMPUTE, CODE_AGENT, HARNESS_ENABLED)
from .persona import persona_for, persona_name
from . import attention, conv_state, expression, chat_switch
from .memory import add_memory
from . import agent_client, group_tuning, memory_v2, mood, profiles, group_scene
from .prompt_builder import PERSONA_RULE, build_packet, recall_hits
from .web_search import fetch_web_search

# prompt_builder 拼出来的两条分隔线。图片消息要按它把「背景」和「当前消息」拆开。
_BG_SEP = "----------- 以下是背景（供你理解情况，不必逐条回应）-----------"
_CUR_SEP = "----------- 以下是当前要回复的消息（重点看这条，别答非所问）-----------"

# 视觉消息标记：plugins/vision_agent 产生，llm 消费。
# 以前两边各写一遍字面量，改一处忘一处就静默失效 —— 统一到这里。
VISION_TAG = "[图片识别结果]"

# 视频标记：视频（B 站抽帧总结 / 以后接的第三方视频解析）走的是**同一条**
# 「当前媒体」通道，但话术必须说「视频」。
# 2026-11-17 审计：视觉分支原来把「图」写死在 4 个地方（system 标题 / user 块 /
# 历史前缀 / 描述压缩），视频一旦复用就会变成「这段视频…这张图…」自相矛盾，
# 而且模型会把历史里那条视频当成「当前这张图」去评论 —— 所以这里显式区分。
VIDEO_TAG = "[视频解析结果]"
MEDIA_TAGS = (VISION_TAG, VIDEO_TAG)

# 结构化媒体头（vision_agent 拼的 fake_text）：
#   `[图片识别结果] 群号:1 | 发送者QQ:2 | 发送者昵称:x | 图片内容:…`
#   `[视频解析结果] 群号:1 | 发送者QQ:2 | 发送者昵称:x | 视频内容:…`
# 也可能是「多来源报告」形态：头之后**直接换行**贴 report，没有 `| 内容:` 段。
_MEDIA_HEAD_RE = re.compile(
    r"群号[:：]\s*(?P<gid>[^|\n]*?)\s*\|\s*"
    r"发送者QQ[:：]\s*(?P<uid>[^|\n]*?)\s*\|\s*"
    r"发送者昵称[:：]\s*(?P<nick>[^|\n]*)"
)
_MEDIA_DESC_RE = re.compile(r"\|\s*(?:图片|视频)内容[:：]\s*(?P<desc>.*)", re.S)


def media_kind_of(text: str, is_vision: bool | None = None) -> str:
    """这轮消息带的是哪种媒体：'image' / 'video' / ''（纯文字）。

    先看标记（视频优先：视频里也可能出现「图片」二字），再看调用方显式传的
    is_vision（兼容老调用），都没有就是纯文字。
    """
    t = str(text or "")
    if VIDEO_TAG in t:
        return "video"
    if VISION_TAG in t:
        return "image"
    return "image" if is_vision else ""


def _parse_media_head(text: str):
    """从结构化媒体头里取 (gid, uid, nick, desc, body)；不是结构化头返回 None。

    旧代码只认 `… | 图片内容:xxx` 这一种写法，report 形态会 match 失败 →
    `agent_query = text` 把**整段 build_prompt** 当成「图里看到的」喂给图书馆，
    既浪费 token 又污染检索词。
    """
    m = _MEDIA_HEAD_RE.search(str(text or ""))
    if not m:
        return None
    rest = str(text)[m.end():]
    d = _MEDIA_DESC_RE.match(rest)
    return (
        m.group("gid").strip(),
        m.group("uid").strip(),
        m.group("nick").strip(),
        (d.group("desc").strip() if d else ""),
        rest.strip(),
    )


def _split_prompt(text: str) -> tuple:
    """
    把 build_prompt 的输出拆成 (背景, 当前消息)。

    为什么要拆：图片消息原来把整段 prompt 塞进一个 system 块、user 位置只放
    「（群友发了张图片）」，结果「当前消息」不在最显眼的地方，模型注意力摊平。
    拆开之后 system 放背景、user 放「图片 + 附带文字」。
    """
    t = str(text or "")
    if _CUR_SEP in t:
        head, cur = t.split(_CUR_SEP, 1)
        return head.replace(_BG_SEP, "").strip(), cur.strip()
    m = re.search(r"-{3,}\s*以下是当前要回复的消息[^\n]*\n", t)
    if m:
        return t[:m.start()].strip(), t[m.end():].strip()
    return "", t

# ================= 工具（function calling，只在私聊启用） =================
# 目的：让她能主动做事，而不是只能等 `/翻唱` 这种正则指令。
# 比如她说「我答应过你 11 月 5 日唱晴天」→ 直接调 sing 去唱。
# ⚠️ 只在私聊启用：群聊每轮都挂工具会多耗 token，而且容易乱调。
TOOL_HANDLERS: dict = {}
TOOLS_SPEC: list = []

# 算力机（node.py）专属工具：NODE_COMPUTE=0 时不注册，让 plugins/third 的本机插件接管
_NODE_ONLY_TOOLS = {
    "sing", "video_understand",
    "draw_image", "draw_from_pixiv", "pixiv_search",
    "cover", "rvc_cover", "tts_voice",
}
# 每个工具挂在哪：私聊 / 群聊。默认只挂私聊（老行为不变）。
# NapCat 工具那两个 meta 工具要群聊也挂 —— 群里禁言、改名片、戳人这些本来就是群里的事。
TOOL_SCOPES: dict = {}
# 这些工具执行期间「只收不发」：bot.send 被拦下，内容交回模型自己决定发不发。
HOLD_TOOLS: set = set()
# 3 → 6：现在多了 napcat_help / napcat_call 这种「先看说明再动手」的两段式，
# 3 轮不够（menu → help → call 就没了，她连开口的机会都没有）。
MAX_TOOL_TURNS = tuning.i("MAX_TOOL_TURNS")
# 工具执行时需要的运行时上下文（bot / event / speaker_qq），由 chat 插件在调用前塞进来。
#
# ⚠️ 必须是「每个协程自己一份」，不能是普通全局 dict：
#   群里 A 和 B 同时说话时，B 的 set_tool_context 会把 A 的覆盖掉，
#   于是 A 的工具会拿着 B 的 bot/event 去执行 —— 给别人唱歌、把记忆记到别人档案里。
#   用 ContextVar 后，每个 session worker 任务各看各的。
_TOOL_CTX_VAR: contextvars.ContextVar = contextvars.ContextVar("tool_ctx", default=None)


class _ToolContext(dict):
    """看起来像 dict，实际读写的是当前协程的 ContextVar。"""

    @staticmethod
    def _cur() -> dict:
        return _TOOL_CTX_VAR.get() or {}

    def get(self, key, default=None):
        return self._cur().get(key, default)

    def __getitem__(self, key):
        return self._cur()[key]

    def __setitem__(self, key, value):
        d = dict(self._cur())
        d[key] = value
        _TOOL_CTX_VAR.set(d)

    def __contains__(self, key):
        return key in self._cur()

    def __iter__(self):
        return iter(self._cur())

    def __len__(self):
        return len(self._cur())

    def keys(self):
        return self._cur().keys()

    def items(self):
        return self._cur().items()

    def values(self):
        return self._cur().values()

    def clear(self):
        _TOOL_CTX_VAR.set({})

    def update(self, *a, **kw):
        d = dict(self._cur())
        d.update(*a, **kw)
        _TOOL_CTX_VAR.set(d)

    def copy(self):
        return dict(self._cur())

    def __repr__(self):
        return f"_ToolContext({self._cur()!r})"


TOOL_CTX = _ToolContext()

# 最近一次真正发给 DeepSeek 的完整 messages（key → list[dict]）
# `/看模板` 直接打这个，保证和线上跑的完全一致（不是重新拼的近似版）。
_LAST_MSGS: dict = {}


def last_messages(key: str = "") -> list:
    """取最近一次的完整输入。key 为空就返回任意一个（一般是最近活跃的会话）。"""
    if key and str(key) in _LAST_MSGS:
        return list(_LAST_MSGS[str(key)])
    if not _LAST_MSGS:
        return []
    return list(_LAST_MSGS[list(_LAST_MSGS)[-1]])


# ================= 每次调用的完整输入/输出落盘 =================
# 为什么要落盘：`_LAST_MSGS` 只在内存、只留最近一次、重启就没了。
# 要复盘「她到底看到了什么、为什么这么回」，必须把每一次的完整 prompt 存下来，
# 才能和「上帝视角看到的真实消息流」并排对比（scripts/trace_reply.py）。
LLM_TRACE = os.getenv("LLM_TRACE", "true").lower() == "true"
TRACE_DIR = Path(os.getenv("LLM_TRACE_DIR", "data/llm_trace"))
TRACE_KEEP_DAYS = tuning.i("LLM_TRACE_KEEP_DAYS")
TRACE_MAX_CHARS = tuning.i("LLM_TRACE_MAX_CHARS")


def _trace_dump(key: str, msgs: list, text: str, reply: str,
                reason: str = "", **extra) -> None:
    """把这一次调用的完整输入/输出写成一行 jsonl。失败绝不抛（别把聊天搞挂）。"""
    if not LLM_TRACE:
        return
    try:
        TRACE_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.time()
        blocks = []
        for m in msgs or []:
            c = str(m.get("content") or "")
            blocks.append({"role": m.get("role"), "content": c})
        rec = {
            "ts": ts,
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
            "key": str(key),
            "text": str(text or "")[:1000],
            "reply": str(reply or "")[:4000],
            "reason": str(reason or "")[:2000],
            "n_msgs": len(blocks),
            "prompt_chars": sum(len(b["content"]) for b in blocks),
            "msgs": blocks,
        }
        for k, v in extra.items():
            if v not in (None, "", []):
                rec[k] = v
        if rec["prompt_chars"] > TRACE_MAX_CHARS:
            rec["truncated"] = True
        p = TRACE_DIR / f"{time.strftime('%Y-%m-%d', time.localtime(ts))}.jsonl"
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        _purge_trace()
    except Exception as e:
        logger.debug(f"[TRACE] 落盘失败: {type(e).__name__}: {e}")


def _purge_trace() -> None:
    """顺手清掉超过 TRACE_KEEP_DAYS 的 trace 文件（别把磁盘塞满）。"""
    try:
        cutoff = time.time() - TRACE_KEEP_DAYS * 86400
        for f in TRACE_DIR.glob("*.jsonl"):
            if f.stat().st_mtime < cutoff:
                f.unlink()
    except Exception:
        pass


def set_tool_context(**kw) -> None:
    TOOL_CTX.clear()
    TOOL_CTX.update(kw)


def _skill_ok(name: str) -> bool:
    """工具级能力开关（核心工具永远放行；模块缺失时不影响主流程）。"""
    try:
        from . import skill_switch
        return skill_switch.is_enabled(name)
    except Exception:
        return True


def register_tool(spec: dict, handler, scope=("private",), hold=False) -> None:
    """
    注册一个可被 DeepSeek 调用的工具（同名覆盖）。

    scope：这个工具在哪些会话里挂出来。默认 ("private",) —— 群聊不挂。
           轻量的 meta 工具（napcat_help / napcat_call）可以传 ("private","group")。

    hold：True = **工具不许自己发**。执行期间它调 bot.send 的内容会被拦下来，
          存成「待发内容」还给模型，由模型决定要不要调 send 工具发出去
          （用户 2026-11-18：工具先不主动 send，她拿到内容自己选）。
    """
    try:
        name = spec["function"]["name"]
    except Exception:
        return
    # NODE_COMPUTE=0：算力机专属工具一律不注册，交给 plugins/third 的本机插件
    if not NODE_COMPUTE and name in _NODE_ONLY_TOOLS:
        logger.info(f"[TOOLS] NODE_COMPUTE=0 → 跳过算力机工具 {name}")
        return
    # CODE_AGENT=harness：self_tool 与它自造的 mytool_* 全部不注册，避免和 harness 抢活
    if HARNESS_ENABLED and (name.startswith("self_tool_") or name.startswith("mytool_")):
        logger.info(f"[TOOLS] CODE_AGENT=harness → 跳过 {name}")
        return
    TOOLS_SPEC[:] = [s for s in TOOLS_SPEC if s.get("function", {}).get("name") != name]
    TOOLS_SPEC.append(spec)
    TOOL_HANDLERS[name] = handler
    TOOL_SCOPES[name] = set(scope or ("private",))
    if hold:
        HOLD_TOOLS.add(name)
    else:
        HOLD_TOOLS.discard(name)


# 群聊里**额外**屏蔽的工具（私聊不受影响）。
# 群聊每轮都挂工具很贵（33 个 ≈ 2500 token），而且工具越多越容易乱挑；
# 想精简就写：GROUP_TOOL_DENY=mytool_filebox,genshin_panel,steam_judge
GROUP_TOOL_DENY = {x.strip() for x in os.getenv("GROUP_TOOL_DENY", "").split(",")
                   if x.strip()}


def tools_for(key: str) -> list:
    """这个会话该挂哪些工具的 spec。群聊只拿 scope 里带 group 的（再减去黑名单）。"""
    want = "group" if str(key).startswith("group") else "private"
    out = []
    for s in TOOLS_SPEC:
        name = (s.get("function") or {}).get("name")
        if want not in TOOL_SCOPES.get(name, {"private"}):
            continue
        if want == "group" and name in GROUP_TOOL_DENY:
            continue
        out.append(s)
    return out


TOOL_RESULT_MAX = tuning.i("TOOL_RESULT_MAX")


# ================= 工具自己发出去的内容，也要让模型看见 =================
# 用户 2026-11-18 指出的问题：
#   很多工具是「模型调用 → 工具直接用 NapCat 把结果发出去 → 只回一句『做好了』」。
#   模型看不到自己到底发了什么，下一轮被问「你刚才发的啥」就答不上，
#   也容易重复发一遍。
# 这里在**工具执行期间**捕获 bot.send 的内容，拼在 tool 结果后面还给模型。
# 用 ContextVar：只影响当前这次工具调用，不会串到别的会话/别的协程。
_TOOL_SENT: contextvars.ContextVar = contextvars.ContextVar("tool_sent", default=None)
# hold 工具用：{"task": 当前任务, "box": [Message…]}
# ⚠️ 必须带 task 判断 —— ContextVar 会被 asyncio.create_task 继承，
#    唱歌/看视频这类后台任务如果继承了「只收不发」，成品就永远发不出去。
_TOOL_HOLD: contextvars.ContextVar = contextvars.ContextVar("tool_hold", default=None)
_SEND_CAPTURE_READY = False


def _msg_brief(message) -> str:
    """把发出去的消息压成一句人能读的摘要（给模型看，不是给用户看）。"""
    try:
        from nonebot.adapters.onebot.v11 import Message
        segs = message if isinstance(message, Message) else Message(message)
    except Exception:
        return str(message)[:80]
    parts = []
    for s in segs:
        t = getattr(s, "type", "")
        d = getattr(s, "data", {}) or {}
        if t == "text":
            txt = str(d.get("text") or "").strip()
            if txt:
                parts.append(txt)
        elif t in ("image", "mface"):
            parts.append("[图片]")
        elif t == "record":
            parts.append("[语音]")
        elif t == "video":
            parts.append("[视频]")
        elif t == "at":
            parts.append("@" + str(d.get("qq") or ""))
        elif t in ("json", "xml"):
            parts.append("[卡片]")
    return " ".join(p for p in parts if p).strip()[:150]


def _install_send_capture() -> None:
    """把 Bot.send 包一层：工具执行期间把「发出去的内容」记到 ContextVar。"""
    global _SEND_CAPTURE_READY
    if _SEND_CAPTURE_READY:
        return
    try:
        from nonebot.adapters.onebot.v11 import Bot
        _orig_send = Bot.send
        if getattr(_orig_send, "_qgb_capture", False):
            _SEND_CAPTURE_READY = True
            return

        async def _send(self, event, message, **kw):
            # ① hold 工具：拦下，不真发（只认「发起这次工具调用的那个任务」，
            #    后台任务即使继承了 ContextVar 也照常发）
            hold = _TOOL_HOLD.get()
            if hold is not None and hold.get("task") is asyncio.current_task():
                hold["box"].append(message)
                return None
            # ② 其他工具：照常发，但记一份给模型看
            box = _TOOL_SENT.get()
            if box is not None:
                try:
                    brief = _msg_brief(message)
                    if brief:
                        box.append(brief)
                except Exception:
                    pass
            return await _orig_send(self, event, message, **kw)

        _send._qgb_capture = True
        Bot.send = _send
        _SEND_CAPTURE_READY = True
        logger.info("[TOOL] 已挂「工具发送」拦截/捕获（内容交回模型决定发不发）")
    except Exception as e:
        logger.warning(f"[TOOL] 捕获工具发送失败（不影响聊天）: {type(e).__name__}: {e}")


async def _run_tool_calls(calls: list) -> list:
    """执行模型请求的工具，返回 [{"role":"tool", ...}] 供回填。"""
    out = []
    for c in calls or []:
        fn = (c.get("function") or {})
        name = str(fn.get("name") or "")
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except Exception:
            args = {}
        handler = TOOL_HANDLERS.get(name)
        if not handler:
            result = f"没有这个工具：{name}"
        elif not _skill_ok(name):
            # 2026-11-20：非核心能力被 /技能 关掉 → 直接告诉模型，别让它反复重试
            result = f"「{name}」这个能力被主人关掉了。用别的方式回答，不要重复调用。"
        else:
            _install_send_capture()
            _tok = _TOOL_SENT.set([])
            _hold = None
            _htok = None
            if name in HOLD_TOOLS:
                _hold = {"task": asyncio.current_task(), "box": []}
                _htok = _TOOL_HOLD.set(_hold)
            try:
                result = await handler(args)
            except Exception as e:
                result = f"工具 {name} 执行失败：{type(e).__name__}: {e}"
            finally:
                _sent = list(_TOOL_SENT.get() or [])
                _TOOL_SENT.reset(_tok)
                if _htok is not None:
                    _TOOL_HOLD.reset(_htok)
            # hold 工具拦下来的内容：存进本轮的待发列表，交给模型决定发不发
            if _hold and _hold["box"]:
                _pend = list(TOOL_CTX.get("pending_sends") or [])
                _start = len(_pend) + 1
                _pend.extend(_hold["box"])
                TOOL_CTX["pending_sends"] = _pend
                _briefs = [_msg_brief(m) for m in _hold["box"]]
                _briefs = [b for b in _briefs if b] or ["（内容）"]
                _nums = "、".join(str(_start + i) for i in range(len(_briefs)))
                result = (f"{result}\n"
                          f"（注意：上面若写了「已经发了」先不算数 —— 这些内容"
                          f"**还没发出去**。编号 {_nums}："
                          f"{'；'.join(_briefs)[:300]}\n"
                          f"要发就调 send(pending=编号)；不想发就算了，"
                          f"别跟对方说已经发了。）")
            # 工具自己发出去的内容补一句给模型：否则它不知道「已经发过了、发的是什么」
            if _sent:
                _uniq = list(dict.fromkeys(_sent))[:4]
                result = (f"{result}\n"
                          f"（你刚才已经通过工具把这些内容发出去了："
                          f"{'；'.join(_uniq)[:300]}）")
        logger.info(f"[TOOL] {name}({args}) → {str(result)[:80]}")
        out.append({
            "role": "tool",
            "tool_call_id": c.get("id") or name,
            # 原来截 600 字：napcat_help 的参数表、群成员名单都会被砍掉一半，
            # 她看到的说明是残的 → 参数照样填错。放宽到 1500（只在真调工具时才有）。
            "content": str(result)[:TOOL_RESULT_MAX],
        })
    return out


# ================= 模型主动请求资料：[[SEARCH|词]] / [[LIBRARY|词]] =================
# 这不是 function calling（那个只在私聊、走 tools 协议），而是**文本暗示**：
# 在 system 里告诉她「不确定就输出这个标记」，我们再替她查、再让她重答一遍。
# 好处：群聊也能用（不占 tools 的 token），坏处：多一次模型往返。
# 支持两种写法：[[X|...]] 和 [[[X|...]]]（三括号更不容易误触，菜单里推荐用三括号）
# 支持两种写法：[[X|...]] 和 [[[X|...]]]（三括号更不容易误触，菜单里推荐用三括号）
# ================= 工具目录（[[TOOLS|分类]] 展开 / [[TOOLS|搜:关键词]] 搜索） =================
# 人设卡里只放「主目录 + 分类」，详细工具和使用说明在这里，按需展开返回，
# 不常驻 prompt（省 token，也让模型「先查再用」，不凭记忆乱调）。
TOOL_CATALOG = {
    "记忆与档案": (
        "【记忆与档案】\n"
        "· [[MEMO|emotion:情绪|text:内容]] —— 记一条记忆（40字内：先写谁做了什么，再写你的想法和感受，最多2条）\n"
        "· [[MOOD|你此刻的心情]] —— 用你自己的话写心情（没有候选词，不发给对方）\n"
        "· [[CALL|bot:他叫你|user:你叫他|target:给谁]] —— 记称呼；给第三个人必须写 target；删用 del:user/bot/both\n"
        "· [[MEMORY|关键词]] —— 查近期记忆：默认返回 10 条目录（编号+时间+心情+摘要）\n"
        "  · [[MEMORY|关键词|翻页2]] —— 看第 2 页（每页 10 条）\n"
        "  · [[MEMORY|展开|编号]] —— 展开某条看详细\n"
        "  · 按时间查：[[MEMORY|前天上午]] / [[MEMORY|昨天]] / [[MEMORY|3天前]] —— 直接翻那段时间的记忆\n"
        "· [[LIBRARY|关键词]] —— 查很久以前的往事（算力机归档）\n"
        "· [[PROFILE|名字]] —— 查某个人：人物卡（人/事情/你在他面前/关系/语气/注意）+ 相关记忆\n"
        "  · [[PROFILE|名字|翻页2]] —— 相处印象一页 6 条，往前翻\n"
        "· profile_card —— 查某人档案，并返回档案图片路径（想发就 send(image=路径)）\n"
        "· profile_update —— 把聊天里确认过的稳定信息写回档案（简介/性格/状态/备注/语气样例）\n"
        "· lore_add —— 往本群设定库写词条（梗/事件/群规/两个人的关系）；只有聊到关键词才注入\n"
        "· function 工具 remember / recall 也可直接调"
    ),
    "查东西": (
        "【查东西】\n"
        "· [[SEARCH|关键词]] —— 联网搜索（梗/番剧/游戏黑话/新闻）\n"
        "· [[WHO|名字]] —— 查群里的人（在不在群、群名片、你俩过往）\n"
        "· [[HISTORY|条数]] —— 往上翻聊天记录（最多20条）\n"
        "· [[ASKGLM|问题]] —— 让 GLM 先答一遍当参考\n"
        "· hash_decode —— 群友发 32/40/64 位十六进制（MD5/SHA1/SHA256）让你猜原文时调；\n"
        "  本地字典能解常见短语（如「我爱你」）和 4 位数字；查不到就直说哈希不可逆。\n"
        "· web_fetch —— 打开一个 http/https 链接并读正文；做题/填表后把答案拼进提交 URL，\n"
        "  再调它打开，返回的正文里通常有分数/提交结果。\n"
        "· video_understand —— 看 B 站视频：转 B 站卡片/链接时，标题简介封面看不出内容（尤其没字幕），\n"
        "  就调它下载视频抽帧看画面，再总结视频讲了什么。\n"
        "  可以直接 tool call 调；也可以写 [[video_understand|B站链接]] 文本标记，系统都会帮你排队看"
    ),
    "画图与搜图": (
        "【画图与搜图】\n"
        "· draw_image —— 文生图。用户要一张图/描述想画什么时用，prompt 写主体+场景+画风\n"
        "· vision_source —— 查图片出处（动漫截图 → 哪部番第几集，走 trace.moe）。\n"
        "  用户问「这张出自哪」「帮我搜下出处」「这是哪个番」时用；不填参数就查这个会话最近那张图。\n"
        "  结果里带「把握不大/最接近的候选」就说明相似度低 —— 只能说「有点像《X》」，别当事实\n"
        "· draw_from_pixiv —— 搜参考图再以图绘图。用户提具体角色名（纳西妲、初音ミク）想画那个角色时用：\n"
        "  先 P 站搜 keyword 拿参考图，再用「重绘」保留角色面部和特征（默认），别文生图瞎画\n"
        "· pixiv_search —— 纯搜图，搜到直接发给他\n"
        "· 画图要 1~3 分钟，先告诉对方「在画了」，画完自动发出来\n"
        "· ⚠️ 拿不准「绯雪」这种词是不是角色名 → 也先当角色名调 draw_from_pixiv，"
        "别拐去 [[SEARCH]] 查资料"
    ),
    "唱歌与语音": (
        "【唱歌与语音】\n"
        "· [[SING|歌名]] —— 翻唱（带伴奏，1~3分钟）\n"
        "· [[SING|歌名|清唱]] —— 只唱人声不要伴奏\n"
        "· [[VOICE]] —— 这条用语音说\n"
        "· 他明确说「唱一个」「来首XX」才唱，别自己突然唱"
    ),
    "操作QQ": (
        "【操作 QQ】\n"
        "· [[AT|名字]] —— 真的 @ 他\n"
        "· [[LIKE|名字|次数]] —— 名片点赞（可选次数，默认1）\n"
        "· [[POKE|名字]] —— 戳一戳\n"
        "· 私聊里对方说「帮我点个赞」「戳我一下」→ 直接写 [[LIKE|我]] / [[POKE|我]]，\n"
        "  **不要反过来问他要 QQ 号**（名字省略或写「我」都默认指当前说话人）\n"
        "· [[NAPCAT|API名|{参数JSON}]] —— 任意 NapCat API（禁言/改名片/踢人等）\n"
        "· function 工具 napcat_help（看某个 API 的说明）/ napcat_call（执行）"
    ),
    "表情包": (
        "【表情包】\n"
        "· <sticker:标签> —— 发一张表情（一条只一张，单独发，不跟文字混）\n"
        "  ⚠️ 用户说「发一张 X / 来张 X / X 标签」时，X 就是标签，直接 <sticker:X>，别自己换词\n"
        "· [[STICKER|列表]] —— 看暂存着等你挑的表情（含编号和画面描述）\n"
        "· [[STICKER|收|编号|心情标签]] —— 收下某张，打上你的心情标签（如 [[STICKER|收|1|无语]]）\n"
        "· [[STICKER|不要]] —— 不要这些暂存表情（本地会删掉）\n"
        "· 看到有趣的表情，按你当时的心情决定收不收、贴什么心情标签"
    ),
    "猪猪": (
        "【猪猪（今日小猪养成游戏，功能很多，不只抽猪）】\n"
        "· 能直接调工具的（function 工具 rollpig，action 选一个）：\n"
        "  today=今日小猪  yesterday=昨日回顾  tomorrow=明日预测\n"
        "  random=随机小猪(可选count)  find=找猪(要keyword)\n"
        "  sty=我的猪圈  week=本周小猪  catalog=小猪图鉴(可选page)\n"
        "  群友说「今日猪猪」「抽猪」「昨天我是什么猪」「随机来只猪」「找只玩偶猪」\n"
        "  「我的猪圈」「本周小猪」「图鉴」→ 直接调 rollpig 工具，拿说话人 QQ 办，别让他发 /指令。\n"
        "· 烤猪互动（也能直接调工具）：\n"
        "  「烤自己/今日烤猪」→ rollpig action=roast_self；\n"
        "  「烤群友/烤 XX」→ rollpig action=roast_member（消息里 @ 谁就烤谁，只能群聊）。\n"
        "  /随机烤群友 /加急生火 @目标 /烤箱续火 /预约烤猪 /聚众烤猪 仍发 /指令（投票/多人流程，暂没工具）。\n"
        "· 管理与推送（群主/管理员或群友自助）：\n"
        "  /猪圈日报（主动查卡） /小猪日报 开启|关闭|状态（定时推送）\n"
        "  /小猪投稿 /同步小猪资源 /全量同步小猪资源\n"
        "· 成长机制：重复抽同一只猪 + 每天烤猪加餐 → 提升 EX Lv.，触发新猪保底；\n"
        "  EX 差分 = 同一只猪不同等级换立绘/文案（不新增图鉴 ID）。\n"
        "· ⚠️ 这是猪猪游戏，**不是运势/抽签**：别调 daily_luck；rollpig 是 function 工具（tool call），\n"
        "  **别输出 [[rollpig]] 这种文本**。\n"
        "· 群友问「猪怎么玩」→ 把上面的功能挑几个告诉他；冷场/无聊时可以安利一句「抽只今日小猪？」"
    ),
    "群聊玩法": (
        "【群聊玩法（能直接调的用工具调，别只动嘴）】\n"
        "· 美少女图 → 调 girls_2d 工具，直接发一张\n"
        "· 运势 → 调 daily_luck 工具，直接抽签（**每人每天一次**；抽过的人再抽返回同一份，"
        "别重新抽。对方说「给某某抽」时把 qq 传进去，省略=当前说话人）\n"
        "· P站找图 → 调 pixiv_search 工具，直接搜了发\n"
        "· 头像表情包 → 调 petpet 工具，action 选：摸/亲/贴/撕/丢/拍/打拳/舔/追火车/锤/砸/吸/顶/抱/手枪\n"
        "  目标 @ 某人（消息里 @ 了谁就做谁）、target 传 qq 或「自己」、省略=说话人自己\n"
        "  · 有熟人惹你、或你想逗逗某个熟人，也可以主动调 petpet 做他一下\n"
        "  · 这 13 个之外的模板（100+）→ 让群友自己发 /头像表情包 看图选指令\n"
        "· Steam 毒舌锐评 → 调 steam_judge 工具（从用户消息里提取 Steam 个人主页链接当 link）\n"
        "· 原神面板 → 调 genshin_panel 工具（没绑定 UID 就先调 genshin_bind）；"
        "队伍伤害 → genshin_team；解绑 → genshin_unbind\n"
        "· 能调工具的就别用嘴介绍；缺参数先问清再调"
    ),
    "三方插件": (
        "【三方插件：完整触发词（用户可以直接说人话让你做，也可以发这些指令）】\n"
        "· 原神面板（gspanel）\n"
        "  - 工具：genshin_bind(uid) / genshin_unbind / genshin_panel(char?) / genshin_team(chars?)\n"
        "  - 指令：/绑定 9位UID、/解绑、/面板 [UID] [角色]、/队伍伤害 [UID]\n"
        "  - 用户说「绑定原神」「查我刻晴面板」「队伍伤害」→ 直接调工具；没绑定先问 UID\n"
        "· 今日小猪 / 猪猪（rollpig）\n"
        "  - 工具：rollpig(action=today/yesterday/tomorrow/random/find/sty/week/catalog/roast_self/roast_member)\n"
        "  - 指令：今日小猪、昨日小猪、明日小猪、随机小猪、找猪 关键词、我的猪圈、小猪图鉴、本周小猪、\n"
        "    今日烤猪、烤群友 @人、加急生火 @人、烤箱续火、猪圈日报、小猪日报 开启|关闭|状态、小猪投稿\n"
        "· 今日运势（jinriyunshi）\n"
        "  - 工具：daily_luck(qq?)（每人每天一次）\n"
        "  - 指令：/今日运势、/今日人品（.前缀也行）\n"
        "· 头像表情包（petpet）\n"
        "  - 工具：petpet(action=摸/亲/贴/撕/丢/拍/打拳/舔/追火车/锤/砸/吸/顶/抱/手枪, target?)\n"
        "  - 指令：/头像表情包；直接说「摸 @某人」「亲亲 @某人」也行\n"
        "· 美少女图（girls_2d）\n"
        "  - 工具：girls_2d\n"
        "  - 指令：/来点美少女、/来点二次元\n"
        "· 磁力搜索（bittorrents）\n"
        "  - 指令：/bt 关键词、/bt on|off、/btp；只能发指令，没有工具\n"
        "· Steam 锐评（steam_judger）\n"
        "  - 工具：steam_judge(link)\n"
        "  - 指令：/steam分析 链接（别名 /steam锐评 /steam毒舌）\n"
        "· P站搜图（pixivrank / draw）\n"
        "  - 工具：pixiv_search(关键词)\n"
        "· 画图（draw）\n"
        "  - 工具：draw_image(描述)、draw_from_pixiv(描述)\n"
        "  - 指令：/画图 描述、/以图绘图 模式 描述\n"
        "· 唱歌（song_cover）\n"
        "  - 工具：sing(song, mode?)\n"
        "  - 指令：/翻唱 歌名、/清唱 歌名、/测歌 歌名\n"
        "· 看视频（B站）\n"
        "  - 工具：video_understand(url)\n"
        "· 表情库（chat / sticker_admin）\n"
        "  - 指令：/收表情、/导入表情、/发表情、/识表情、/清理表情、/表情统计、/禁用表情、/启用表情\n"
        "· 记忆 / 档案 / 心情\n"
        "  - 指令：/档案 [@人]、/记住 xxx、/叫我 名字（她怎么叫你）、/叫你 名字（给她起名）、"
        "/叫他 @人 名字、/复盘、/记忆体检、/记忆统计、/心情\n"
        "· 自检 / 诊断\n"
        "  - 指令：/自检、/接入自检、/全量测试、/测函数 <项>（猪猪/原神/标识/语音/预算/包/工具/关注/外号/字幕）\n"
        "  - 工具：napcat_help / napcat_call（QQ 全量 API，先 help 再 call）\n"
        "· 用户让你做上面这些事时：能调工具就直接调；只能发指令的，告诉他发哪条指令（别自己编指令名）。"
    ),
    "自己写工具": (
        "【自己写工具（现有工具干不了时，你就自己造一个）】\n"
        "· 什么时候用：用户让你干一件**现有工具都干不了**的实事（读某种文件、下载群文件、"
        "爬个网页、算个东西、处理一批数据…）时，别只会说「我办不到」——用下面这套自己写。\n"
        "· self_tool_write —— 写/改一个你自己的工具：给 name（小写字母/数字/下划线，如 read_group_file）+ "
        "code（Python，用 sys.argv[1] 收 JSON 参数、print 输出结果）+ description。"
        "存完立刻以 mytool_<名字> 挂进你的 function calling，之后可以直接 tool call 它。\n"
        "· self_tool_run —— 跑你写的工具（name 不带 mytool_ 前缀，args 是 JSON 对象），"
        "输出会返回给你并写进 runs/<名字>.log。\n"
        "· self_tool_file —— 在你的 tools/ runs/ data/ 三个目录里看代码 / 看 log / 存爬来的数据。\n"
        "· 标准循环：write 写 → run 跑 → 报错就 read 代码/log 改 → 再 write 重写 → 再 run，直到能用。\n"
        "· 群里干不了的 QQ 操作（读/下载群文件等）→ 配合 napcat_help 查 NapCat API，"
        "把 API 调用写进工具里，别硬说做不到。\n"
        "· ⚠️ 已有工具能干的就别重复造轮子；这是「兜底的新能力」，不是让你啥都现写。"
    ),
    "安静与地盘": (
        "【安静与地盘（你有沉默的权利）】\n"
        "· stay_silent —— 这一轮不想说话就调它（群友刷图/别人互聊/你没心情/插不上话）。\n"
        "  和「(不想回复)」等价，但更明确；系统不会发任何内容，你读到的都还记得。\n"
        "· chat_switch(action=\"list\") —— 看你能在哪些群、能跟谁私聊、谁是主人、现在谁被你静音。\n"
        "· chat_switch(action=\"off\", target=\"这个人\", minutes=30, reason=\"烦\") —— **只不理他**：\n"
        "  他的 @、他的图片也不接；群里别人照常。target 也可以写名字或 QQ 号。\n"
        "· chat_switch(action=\"off\", target=\"here\", minutes=60, reason=\"插不上话\") —— 整个群安静。\n"
        "  静音后消息照收照记，只是不回；minutes=0 表示一直安静到你 action=\"on\"。\n"
        "· chat_switch(action=\"on\", target=\"…\") —— 想说话了就自己开，不用等谁批准；\n"
        "  他道歉/哄你了，也可以 on 回来给个台阶。\n"
        "· sticker_save —— 觉得这张表情包好玩、以后想用，就收进自己的库；不喜欢别收。"
    ),
    "其它": (
        "【其它】\n"
        "· [[DEEP|理由]] —— 深度思考（把思考时间从几秒延到60秒）\n"
        "· [[LATER|秒数]] —— 等 N 秒看群友后面说什么（3~30秒）\n"
        "· [[FOCUS|编号]] —— 标注你在回哪一条\n"
        "· ||| —— 一条回复分成几条气泡发（最多2条）\n"
        "· (不想回复) —— 不想接就只输出这四个字"
    ),
}

# ---------------- 部署模式决定工具目录长什么样（2026-11-24） ----------------
if not NODE_COMPUTE:
    # 本机部署：没有算力机，画图/唱歌交给 plugins/third 里的现成插件
    TOOL_CATALOG["画图与搜图"] = (
        "【画图与搜图（本机模式）】\n"
        "· vision_source —— 查图片出处（trace.moe），不依赖算力机，照常用\n"
        "· 本机没有接算力机：**不要调** draw_image / draw_from_pixiv / pixiv_search\n"
        "· 画图请用 plugins/third 里装的本机画图插件：先 [[TOOLS|搜:画图]] 看有没有对应工具；\n"
        "  没有就直说「这台机器没接画图」，别假装画了"
    )
    TOOL_CATALOG["唱歌与语音"] = (
        "【唱歌与语音（本机模式）】\n"
        "· [[VOICE]] —— 这条用语音说（云端 TTS 兜底）\n"
        "· 本机没有接算力机：**不要调 sing**；\n"
        "  唱歌请用 plugins/third 里装的本机唱歌插件：先 [[TOOLS|搜:唱歌]] 查；没有就直说没接"
    )

if CODE_AGENT == "harness":
    TOOL_CATALOG.pop("自己写工具", None)
    TOOL_CATALOG["改代码（Harness）"] = (
        "【改代码（DeepSeek Harness 会话）】\n"
        "· harness_ask —— 把需求交给本机的 DSH（DeepSeek Harness）会话，它可以真的读/改仓库代码。\n"
        "  用户说「改一下这个功能」「加个 XX」「这个 bug 修一下」时用；只允许主人使用。\n"
        "· harness_status —— 看当前 harness 会话、工作目录、后端状态。\n"
        "· harness_reset —— 开一个新会话（换任务/上下文乱了时用）。\n"
        "· ⚠️ 改代码可能影响线上，动手前先说清楚要改什么；改完把 diff/结果原样转述给主人。"
    )
elif CODE_AGENT == "off":
    TOOL_CATALOG.pop("自己写工具", None)


def _tool_catalog_names() -> str:
    return " / ".join(TOOL_CATALOG.keys())


def _tool_search(kw: str) -> str:
    """[[TOOLS|搜:关键词]] —— 在目录里搜某个工具，返回匹配行。"""
    kw = (kw or "").strip().lower()
    if not kw:
        return "没说搜什么工具。"
    hits = []
    for cat, text in TOOL_CATALOG.items():
        for line in text.split("\n"):
            if kw in line.lower():
                hits.append((cat, line.strip()))
    if not hits:
        return ("没搜到和「" + kw + "」相关的工具。可用分类："
                + _tool_catalog_names())
    out = []
    for cat, line in hits[:10]:
        out.append(f"· [{cat}] {line}")
    return "\n".join(out)


_REQ_RE = re.compile(
    r"\[\[\[?\s*(SEARCH|LIBRARY|SING|WHO|HISTORY|MEMORY|PROFILE|ASKGLM|LATER"
    r"|LIKE|POKE|NAPCAT|TOOLS|STICKER|VIDEO|VIDEO_UNDERSTAND|video_understand|LUCK)\s*"
    r"(?:\|\s*([^\]]{0,300}?)\s*)?\]\]\]?")

# 最近一次 [[MEMORY|关键词]] 目录查询的结果缓存，供「展开|编号」「翻页N」用。
# ⚠️ 必须按会话隔离（2026-11-17 审计）：以前是全局单例，
#    A 群刚查完目录，B 群有人说「展开 3」，展开的是 **A 群的记忆**。
#    群里同时有好几个会话在跑，这种串台是会真实发生的。
_MEM_CATALOG: dict = defaultdict(dict)


def _session_of(ev) -> str:
    """事件 → 会话 key（和 chat 的 _get_session_key 保持一致）。"""
    try:
        gid = str(getattr(ev, "group_id", "") or "")
        if gid:
            return f"group_{gid}"
        uid = str(getattr(ev, "user_id", "") or "")
        return f"private_{uid}" if uid else ""
    except Exception:
        return ""

# 思考预算：她可以连续调工具，但必须在这么多秒内开口。
# 用户 2026-11-11：「定个 60 秒上限吧，让她尽量在 60 秒内思考完以及想看的看完」
THINK_BUDGET_SEC = tuning.f("THINK_BUDGET_SEC")
MAX_REQ_ROUNDS = tuning.i("MAX_REQ_ROUNDS")

# 👇 两档思考预算（用户 2026-11-11：「拿不准就深度思考下 60s 那个，平常可以回复快些」）
#    fast：默认档，几秒内就开口 —— 群友在等她，不能每句都磨 60 秒
#    deep：她自己在回复里写 [[DEEP|理由]] 才升级，拿不准/要翻好几处才用
THINK_BUDGET_FAST = tuning.f("THINK_BUDGET_FAST")
THINK_BUDGET_DEEP = THINK_BUDGET_SEC
MAX_REQ_ROUNDS_FAST = tuning.i("MAX_REQ_ROUNDS_FAST")
_DEEP_RE = re.compile(r"\[\[\[?\s*DEEP\s*(?:\|[^\]]{0,60})?\]\]\]?", re.I)

# 往上翻历史最多多少条。用户 2026-11-11：「最高 20 条，以免把上下文拉爆」。
HISTORY_MAX = tuning.i("HISTORY_MAX")

# 一条回复最多拆几个气泡（和 aura_logic.MAX_BUBBLES 是同一个 env，两处各读一次）
MAX_BUBBLES = tuning.i("MAX_BUBBLES")

# 模型调用失败时的兜底话术（按故障类型分）。
# 不要「服务器繁忙」这种客服腔；也别假装正常聊天糊过去。
FALLBACK_LINES = {
    "timeout": [
        "等我缓一下…刚脑子卡住了",
        "网有点慢，我重新想想",
        "……刚才走神了，你再说一遍？",
    ],
    "rate": [
        "说太快了，我喘口气",
        "别急，让我缓两秒",
        "慢一点，我跟不上了",
    ],
    "error": [
        "今天有点笨，晚点再找我？",
        "脑子打结了，等下再说",
        "我这边出了点小状况，等会儿",
    ],
}


# 短期上下文从历史库补几轮（一轮 = 她说 + 她答）。用户 2026-11-12：
# 「上下文记忆没有，每次强调翻档案才能记起来，上一句和下一句完全忘记，特别是私聊」。
CHAT_HISTORY_TURNS = tuning.i("CHAT_HISTORY_TURNS")


def _db_history(key: str, who: str = "", turns: int = 0) -> list:
    """
    从 SQLite 短期历史里捞最近几轮，补成「时间 + 人」格式。

    为什么需要它（这是 2026-11-12 找到的根因）：
      · 群聊的上下文靠 chat 插件的 `recent_group_msgs` 内存队列
      · **私聊根本没有这个队列** → 私聊每一句都是「第一次见面」
      · `add_memory` 一直在往 histories 表写，但 `get_memory()` 全项目 **0 个调用方**
        → 历史存了从来没人读
    所以这里把「已经存下来的历史」真正接回去。群聊队列为空时（刚重启）也靠它兜底。

    返回的每行都带时间戳和人名，和群聊上下文的格式保持一致。
    """
    turns = turns or CHAT_HISTORY_TURNS
    try:
        from . import memory as M
        rows = M.get_recent_rows(key, limit=max(2, turns * 2))
    except Exception as e:
        logger.debug(f"[LLM] 读短期历史失败: {type(e).__name__}: {e}")
        return []

    out = []
    for r in rows:
        raw = str(r.get("content") or "").strip()
        if not raw:
            continue
        # 存进去的 user 内容是 build_prompt 拼好的**整段**（含背景/记忆/人称），
        # 直接塞回上下文等于把旧背景又灌一遍 —— 拆出「当前那条」就行。
        _bg, cur = _split_prompt(raw)
        txt = re.sub(r"\s+", " ", (cur or raw).strip())[:110]
        if not txt:
            continue
        t = time.strftime("%H:%M", time.localtime(float(r.get("ts") or 0)))
        if r.get("role") == "assistant":
            out.append(f"{t} 这是你之前的回复：{txt}")
        else:
            out.append(f"{t} {who or '对方'}: {txt}")
    return out


# ============ LATER 已读水位（用户 2026-11-12）============
# 「在拉了未来记录的时候实际上是已读然后总结回复之前那条了。
#   之后如果并非艾特就不要再对新的一句再回一次了。」
#
# 场景：她写 [[LATER|15]] 等 15 秒 → 期间群友又说了两句 →
#   她把那两句**读进上下文**，然后回的是「之前那条」的总结。
#   但那两句本身也会各自触发一次完整回复 → 同一条内容被回两遍。
#
# 做法：LATER 等待结束时记一个「已读水位」（本地时间），
#   chat 的 _need_reply 里：非 @ 且消息时间 <= 水位 → 判定「她等消息时已经读过了」，不再单独回。
#   只用「非 @」兜底：@ 她的消息一定要回，哪怕重复也不能漏。
_LATER_SEEN: dict = {}


def mark_later_seen(gid) -> float:
    """LATER 等完，记下水位（之后 <= 这个时间的消息算已读）。"""
    g = str(gid or "")
    if not g:
        return 0.0
    _LATER_SEEN[g] = time.time()
    return _LATER_SEEN[g]


def later_seen_until(gid) -> float:
    return float(_LATER_SEEN.get(str(gid or "")) or 0.0)


def _new_messages_since(ev, wait: int) -> str:
    """
    「等 N 秒，看群友后面还说了什么」—— [[LATER|15]]

    数据从 dialogue_log 读（落盘的完整消息流），所以不依赖 chat 的内存队列。
    """
    try:
        from .dialogue_log import load_recent
    except Exception:
        return ""
    gid = str(getattr(ev, "group_id", "") or "")
    if not gid:
        return "私聊没法等群消息。"
    cut = time.time() - max(3, wait) - 3
    try:
        # 先按条数取最近 30 条，再按时间过滤「等消息期间新来的」。
        # 不按天读：跨零点时会读不到（0:01 等消息，昨天的文件不在 range(1) 里）。
        recs = [r for r in load_recent(gid, limit=30)
                if float(r.get("ts") or 0) >= cut]
    except Exception:
        return ""
    if not recs:
        return "等了一会儿，没人再说话。"
    recs = recs[-10:]
    texts = [str(r.get("text") or "") for r in recs]
    quotas = _alloc_history_quota(texts)   # 和翻历史共用配额：长消息不许被压成 60 字
    lines = []
    for r, txt, q in zip(recs, texts, quotas):
        t = time.strftime("%H:%M:%S", time.localtime(float(r.get("ts") or 0)))
        who = ("这是你之前的回复" if r.get("kind") == "bot"
               else (r.get("who") or "?"))
        lines.append(f"{t} {who}：{txt[:q]}")
    return "\n".join(lines)


def _seg_text(m) -> str:
    """OneBot 消息段 → 一行纯文本（翻私聊历史时用）。"""
    if isinstance(m, str):
        return m
    if isinstance(m, list):
        out = []
        for x in m:
            if not isinstance(x, dict):
                continue
            t = x.get("type")
            d = x.get("data") or {}
            if t == "text":
                out.append(str(d.get("text") or ""))
            elif t == "at":
                out.append("@" + str(d.get("qq") or ""))
            else:
                out.append({"image": "[图]", "face": "[表情]", "record": "[语音]",
                            "video": "[视频]", "reply": ""}.get(t, ""))
        return "".join(out)
    return str(m or "")


async def _private_history_slice(ev, n: int) -> str:
    """
    私聊翻历史（用户 2026-11-12：「私聊没有拉以前 20 条和最新记录」）。

    ① 先问 NapCat：`get_friend_msg_history` 有真实历史，连她重启前的都在
    ② 拿不到就退到本地 SQLite 短期历史（至少保证「刚才聊的」还在）
    """
    bot = TOOL_CTX.get("bot")
    uid = str(getattr(ev, "user_id", "") or "")
    if not uid:
        return "翻不到私聊记录。"

    if bot:
        try:
            d = await bot.call_api("get_friend_msg_history", user_id=int(uid), count=n)
            msgs = (d or {}).get("messages") or []
        except Exception as e:
            msgs = []
            logger.debug(f"[REQ] 私聊历史 NapCat 失败: {type(e).__name__}: {e}")
        if msgs:
            # 先把原文全取出来，再统一分配截断配额。
            # ⚠️ 不能边取边 [:70]：私聊里发一段长需求，会被砍成 70 字开头，
            #    她只能靠那 70 字猜，等于没看到（2026-11-13 群聊同款事故）。
            picked = []
            for m in msgs[-n:]:
                ts = float(m.get("time") or 0)
                t = time.strftime("%H:%M", time.localtime(ts)) if ts else ""
                body = re.sub(r"\s+", " ", _seg_text(m.get("message") or m.get("raw_message")))
                if not body:
                    continue
                picked.append((t, m, body))
            quotas = _alloc_history_quota([p[2] for p in picked])
            lines = []
            for (t, m, body), q in zip(picked, quotas):
                body = body[:q]
                if identity_mod.is_self(m.get("user_id"),
                                        str(getattr(bot, "self_id", "") or "")):
                    lines.append(f"{t} 这是你之前的回复：{body}")
                else:
                    who = str((m.get("sender") or {}).get("nickname") or "对方")
                    lines.append(f"{t} {who}: {body}")
            if lines:
                logger.info(f"[REQ] 翻私聊历史 {uid} 最近 {len(lines)} 条（NapCat）")
                return "\n".join(lines)

    lines = _db_history(f"private_{uid}", "", turns=max(3, n))
    if lines:
        logger.info(f"[REQ] 翻私聊历史 {uid} 最近 {len(lines)} 条（本地库）")
        return "\n".join(lines)
    return "本地没存到私聊记录，NapCat 那边也拿不到。"


async def _napcat_marker(tool: str, arg: str) -> str:
    """
    文本标记 → NapCat 工具。

    为什么要有这条路（2026-11-12 真机两次踩坑后的结论）：
      群里让她「点赞 / 戳一戳 rera」，日志里**一次 [NAPCAT] 调用都没有** ——
      她要么发个表情糊过去，要么直接回一句「戳我干嘛」。
      但同一批日志里 `[[[HISTORY|20]]]`、`[[[AT|老大]]]` 用得飞起。
      结论：不是她不会做事，是 **function calling 这套接口她不用**，
      她习惯的是「写标记 → 系统去执行 → 结果回给她 → 她再开口」。
      所以给 NapCat 也开一个标记入口，走她熟练的那条路。

    支持：
      [[[LIKE|老大]]]                                → 名片点赞
      [[[POKE|老大]]]                                → 戳一戳
      [[[NAPCAT|set_group_card|{"user_id":"小明","card":"新卡"}]]] → 任意 API
    """
    try:
        from . import napcat_tools as NT
    except Exception as e:
        return f"NapCat 工具没加载：{type(e).__name__}: {e}"

    bot = TOOL_CTX.get("bot")
    ev = TOOL_CTX.get("event")
    if not bot or not ev:
        return "现在没法操作 QQ（缺少会话信息）"

    raw = str(arg or "").strip()
    if tool == "napcat":
        name, _, rest = raw.partition("|")
        name = name.strip()
        try:
            args = json.loads(rest.strip() or "{}")
        except Exception:
            return ('参数得是 JSON，例：'
                    '[[[NAPCAT|set_group_card|{"user_id":"小明","card":"新卡"}]]]')
        if not isinstance(args, dict):
            return "参数得是一个 JSON 对象"
    else:
        name = "send_like" if tool == "like" else "group_poke"
        # 支持 `[[[LIKE|奏奏|10]]]` —— 用户说「帮我点赞10次」，
        # 以前只能传 times=1（2026-11-12 真机：群友「我让你赞10次你🐉吗」）。
        parts = [x.strip() for x in raw.split("|")]
        who = parts[0] if parts else ""
        # 私聊里「帮我点个赞」最常见：省略名字 / 写「我」「对方」都指当前说话人。
        # 以前这里直接判「名字不对」，她只能反过来问 QQ 号（真机 2026-11-17 评测抓到）。
        if not who or who in ("我", "自己", "对方", "他", "她", "你"):
            who = str(TOOL_CTX.get("speaker_qq") or "")
        args = {"user_id": who}
        if name == "send_like" and len(parts) > 1:
            try:
                args["times"] = max(1, min(20, int(re.sub(r"\D", "", parts[1]) or 1)))
            except Exception:
                args["times"] = 1
        # 防呆：标记里的名字太长/带换行 = 她把别的内容写进去了
        # （真机日志里出现过 `[REQ] LIKE「您有新消息，快来看看吧」`）
        if not who or len(who) > 30 or "\n" in who:
            return ("标记里的名字不对（太长或带换行）。"
                    "写成 `[[[LIKE|名字]]]` / `[[[LIKE|QQ号]]]` / "
                    "`[[[LIKE|名字|10]]]`（后一个数字是赞几次）")

    if not name:
        return "没说要调哪个 API"

    ctx = {
        "self_id": str(getattr(bot, "self_id", "") or ""),
        "group_id": getattr(ev, "group_id", None),
        "user_id": getattr(ev, "user_id", None),
        "name": str(TOOL_CTX.get("speaker_name") or ""),
    }
    return await NT.execute(
        bot, name, args,
        requester=str(TOOL_CTX.get("speaker_qq") or ""),
        ctx=ctx,
    )


# ============ 翻历史 / 看新消息时的「每条截多长」配额 ============
# ⚠️ 为什么不能等长截断（2026-11-13 实测事故）：
#   原来每条统一 [:70]。用户在群里发了 200+ 字视觉方案，群里随后刷了 10 句短话，
#   把长消息挤出内存窗口；她翻历史补救 —— 结果 20 条只拿到 682 字（≈34 字/条），
#   长方案只剩开头 70 字，而别人说的「5秒一帧漏完了」因为短，**完整保留**。
#   于是她以为用户说的是「5秒一帧」，完全没看到方案内容。
#   本质：一条 500 字的方案和一句「是啊」截到同样长度，价值差 100 倍。
HIST_LINE_BASE = tuning.i("HIST_LINE_BASE")          # 短消息给多少
HIST_LINE_LONG = tuning.i("HIST_LINE_LONG")         # 长消息给多少
HIST_LONG_AT = tuning.i("HIST_LONG_AT")             # 超过多少字算长消息
HIST_TOTAL_BUDGET = tuning.i("HIST_TOTAL_BUDGET")  # 整段总字数上限


def _alloc_history_quota(texts: list) -> list:
    """
    给每条历史消息分配「最多截到多少字」。

    规则：
      1. 短消息 → HIST_LINE_BASE（够看清在说什么就行）
      2. 长消息（>HIST_LONG_AT）→ HIST_LINE_LONG（方案/代码/长解释必须看全）
      3. 总长度超 HIST_TOTAL_BUDGET → 从**最短的**开始压，长消息保底 60% 配额
         （宁可把「是啊」「哈哈」压到 30 字，也不砍方案正文）
    """
    quotas = [
        HIST_LINE_LONG if len(t) > HIST_LONG_AT else HIST_LINE_BASE
        for t in texts
    ]
    need = sum(min(len(t), q) for t, q in zip(texts, quotas))
    if need <= HIST_TOTAL_BUDGET:
        return quotas

    over = need - HIST_TOTAL_BUDGET
    # 第一轮：短的先压（升序遍历），长消息排在最后才被碰
    for i in sorted(range(len(texts)), key=lambda k: len(texts[k])):
        if over <= 0:
            break
        floor = max(30, int(quotas[i] * 0.6))
        cut = min(quotas[i] - floor, over)
        if cut > 0:
            quotas[i] -= cut
            over -= cut

    # 第二轮：20 条全是长消息时，第一轮压到 floor 仍然超预算 → 按比例再压一次。
    # 不做这一步的话「全是长消息」场景会超预算 2 倍，把上下文拉爆。
    cur = sum(min(len(t), q) for t, q in zip(texts, quotas))
    if cur > HIST_TOTAL_BUDGET > 0:
        ratio = HIST_TOTAL_BUDGET / cur
        quotas = [max(30, int(q * ratio)) for q in quotas]
    return quotas


async def _history_slice(nstr: str) -> str:
    """
    「往上翻 N 条群聊」——她自己决定要看多少前情。

    为什么要这个：默认上下文只给最近 7 条，遇到「那个」「刚才说的」这种指代
    根本接不上。给她一个自己翻的开关，比我们猜该给多少条靠谱。
    数据来自 dialogue_log（按天落盘的完整记录），不是内存里那 8 条。
    """
    from .dialogue_log import load_recent

    try:
        n = int(re.sub(r"\D", "", str(nstr)) or 20)
    except Exception:
        n = 20
    # 上限 20 条（用户 2026-11-11：「拉历史最高 20 条，以免她拉太多把上下文拉爆」）。
    # 以前允许到 120 条，一次就能把 8k 上下文吃掉大半，后面真正要看的反而被挤掉。
    n = max(5, min(HISTORY_MAX, n))

    ev = TOOL_CTX.get("event")
    gid = str(getattr(ev, "group_id", "") or "")
    if not gid:
        # 私聊也能翻（以前这里直接返回「没有群聊记录可以翻」→ 私聊彻底瞎）
        return await _private_history_slice(ev, n)
    # 优先用「群聊现场」里已经过收发件人判断的记录（谁 → 谁），
    # 而不是直接把 NapCat 原生消息给她 —— 用户 2026-11-22：
    # 「拉过去也是积累的收发件人和心情处理过的」。
    try:
        from . import group_scene
        rows = group_scene.recent_messages(gid, n)
    except Exception as e:
        logger.debug(f"[REQ] 翻历史读现场失败: {type(e).__name__}: {e}")
        rows = []
    if rows:
        rows = rows[-n:]
        texts = [str(r.get("text") or "") for r in rows]
        quotas = _alloc_history_quota(texts)
        lines = []
        for r, txt, q in zip(rows, texts, quotas):
            t = time.strftime("%H:%M", time.localtime(float(r.get("ts") or 0)))
            txt = txt[:q]
            if r.get("is_bot"):
                lines.append(f"{t} 这是你之前的回复：{txt}")
            else:
                who = str(r.get("name") or r.get("qq") or "?")
                to = str(r.get("to_name") or "")
                kind = str(r.get("to_kind") or "")
                if kind == "bot" or to == "你":
                    lines.append(f"{t} {who} → 你：{txt}")
                elif kind == "person" and to:
                    lines.append(f"{t} {who} → {to}：{txt}")
                else:
                    lines.append(f"{t} {who} → 大家：{txt}")
    else:
        try:
            # 按**条数**取（用户反复强调：记录按条数，不按时间）。
            # 以前是 load_range(3) 再切片 —— 群安静两天就一条都翻不到。
            recs = load_recent(gid, limit=n)
        except Exception as e:
            logger.warning(f"[REQ] 翻历史失败: {type(e).__name__}: {e}")
            return ""
        if not recs:
            return "本地没存到这个群更早的记录。"
        recs = recs[-n:]
        texts = [str(r.get("text") or "") for r in recs]
        quotas = _alloc_history_quota(texts)
        lines = []
        for r, txt, q in zip(recs, texts, quotas):
            t = time.strftime("%H:%M", time.localtime(float(r.get("ts") or 0)))
            txt = txt[:q]
            if r.get("kind") == "bot":
                lines.append(f"{t} 这是你之前的回复：{txt}")
            else:
                lines.append(f"{t} {r.get('who') or '?'}: {txt}")
    _n_long = sum(1 for x in texts if len(x) > HIST_LONG_AT)
    logger.info(
        f"[REQ] 翻历史 {gid} 最近 {len(lines)} 条"
        f"（{sum(len(x) for x in lines)} 字，其中长消息 {_n_long} 条）"
    )
    return "\n".join(lines)


async def _who_is(query: str) -> str:
    """
    群成员名册里查一个人：在不在、群名片、以及她之前和这个人有过什么。
    走 TOOL_CTX 拿 bot/event —— 这两个是 chat 插件每轮塞进来的，
    所以这里不需要再往下传一遍参数。
    """
    from . import group_roster, profiles

    q = str(query or "").strip().lstrip("@＠").strip()
    if not q:
        return ""
    bot = TOOL_CTX.get("bot")
    ev = TOOL_CTX.get("event")
    gid = getattr(ev, "group_id", 0) or 0
    if not bot or not gid:
        return f"「{q}」在当前会话里查不到（私聊没有群成员名单）。"

    members = await group_roster.members_of(bot, gid)
    if not members:
        return "拿不到群成员名单，暂时查不了。"

    hits = group_roster.find(members, q, limit=5)
    if not hits:
        return (
            f"这个群里没找到叫「{q}」的人（群名片、昵称、外号都比过了）。"
            f"可能是别的群的人，或者只是个梗 —— 别编。"
        )

    lines = []
    for m in hits:
        qq = str(m.get("user_id") or "")
        card = str(m.get("card") or "").strip()
        nick = str(m.get("nickname") or "").strip()
        seg = f"· {group_roster.label(m)}"
        if card and nick and card != nick:
            seg += f"（群名片 {card}，昵称 {nick}）"
        try:
            cardline = profiles.render_card(
                qq, str(gid or ""), detail="micro", max_len=70)
            if cardline:
                seg += f" ｜ 你和他：{cardline}"
        except Exception:
            pass
        lines.append(seg)

    if len(hits) > 1:
        lines.insert(0, f"群里有 {len(hits)} 个可能是「{q}」的人，别 @ 错：")
    else:
        lines.insert(0, f"群里确实有「{q}」：")
    return "\n".join(lines)


def _drop_one_marker(text: str, kind: str, arg: str) -> str:
    """
    只摘掉「刚刚处理过的那一个」标记，其余标记原样保留。

    为什么不能再用 `_REQ_RE.sub("", text)`（2026-11-17 审计发现的 bug）：
      它会把**所有**标记一起删掉 → 外层 while 下一轮 `_REQ_RE.search()` 永远为空
      → MAX_REQ_ROUNDS / THINK_BUDGET_DEEP / [[DEEP]] 全部变成死代码；
      而且一条回复里写两个标记时，第二个会被静默吞掉（模型还以为自己查了）。
    """
    kind = str(kind or "").upper()
    arg = str(arg or "").strip()
    for m in _REQ_RE.finditer(text or ""):
        if m.group(1).upper() == kind and (m.group(2) or "").strip() == arg:
            return (text[:m.start()] + text[m.end():]).strip()
    return str(text or "").strip()


async def _fulfill_requests(reply: str, msgs: list, client, headers: dict) -> str:
    """
    模型在回复里请求资料 → 我们查好塞回去 → 让它重新生成一次。

    每轮只处理**第一个**标记：处理完只摘掉它，剩下的标记留给下一轮
    （由 call_llm 的多轮预算控制上限，快速档 2 轮 / 深度档 6 轮）。
    """
    m = _REQ_RE.search(reply or "")
    if not m:
        return reply
    kind, q = m.group(1).upper(), (m.group(2) or "").strip()
    # 这几个没参数也有默认含义，不能当空标记直接剥掉：
    #   LUCK → 抽签；LIKE / POKE → 默认对「当前说话人」操作（私聊最常见）
    if not q and kind not in ("LUCK", "LIKE", "POKE"):
        return _drop_one_marker(reply, kind, q)

    got, src = "", ""
    if kind == "LUCK":
        # 模型偶尔手滑写成 [[LUCK]] 文本，而不是 tool call daily_luck。
        # 这里兜底执行一次，抽完把结果塞回，让它只补一句收尾。
        src = "运势结果"
        try:
            handler = TOOL_HANDLERS.get("daily_luck")
            got = str(await handler({}) or "") if handler else "运势工具没注册"
        except Exception as e:
            got = f"抽签失败：{type(e).__name__}"
    elif kind == "SEARCH":
        src = "搜索到的内容"
        try:
            from .web_search import fetch_web_search
            got = str(await fetch_web_search(q) or "")
        except Exception as e:
            logger.warning(f"[REQ] 搜索失败: {type(e).__name__}: {e}")
    elif kind == "WHO":
        # 「小强是谁」「找一下阿伟」→ 真的去群成员名册里查，别让她瞎猜
        src = "群里的人员信息"
        try:
            got = await _who_is(q)
        except Exception as e:
            logger.warning(f"[REQ] 查人失败: {type(e).__name__}: {e}")
            got = ""
    elif kind == "PROFILE":
        # 「某人的档案 + 你俩的过往 + 相关记忆」一次打包。
        # 和 WHO 的区别：WHO 查"他在不在群里、群名片叫什么"（花名册），
        # PROFILE 查"我对他都知道些什么"（档案 + 记忆）。
        # [[PROFILE|名字]] / [[PROFILE|名字|翻页2]] —— 相处印象可以翻页看更多
        # （用户 2026-11-19：档案只给 3 条她就以为只有 3 条，于是不去翻）
        _pparts = [p.strip() for p in str(q or "").split("|") if p.strip()]
        _pname = _pparts[0] if _pparts else str(q or "").strip()
        _ppage = 1
        if len(_pparts) > 1 and re.search(r"翻页|页", _pparts[1]):
            _ppage = max(1, int(re.sub(r"\D", "", _pparts[1]) or 1))
        src = f"关于「{_pname}」你知道的"
        _parts = []
        _qq = ""
        try:
            _ev = TOOL_CTX.get("event")
            _bot = TOOL_CTX.get("bot")
            _gid = str(getattr(_ev, "group_id", "") or "")
            if _gid and _bot:
                from . import group_roster
                _members = await group_roster.members_of(_bot, _gid)
                _hit = group_roster.find(_members, _pname, limit=1)
                if _hit:
                    _qq = str(_hit[0].get("user_id") or "")
        except Exception as e:
            logger.debug(f"[REQ] PROFILE 找人失败: {e}")

        if _qq:
            # 统一走人物卡模板（人/简介/事情/关系/语气/注意），不再拼两段散装文本
            try:
                _gid0 = str(getattr(TOOL_CTX.get("event"), "group_id", "") or "")
                _card = profiles.render_card(_qq, _gid0, detail="full", max_len=420)
                if _card:
                    _parts.append(_card)
                # 档案图片：她可以 send(image=路径) 直接发；不发也行
                try:
                    from . import profile_card
                    _img = profile_card.render_profile_image(_qq, _gid0)
                    if _img:
                        _parts.append(f"（档案图片：{_img} —— 想发就 send(image=这个路径)）")
                except Exception:
                    pass
            except Exception:
                pass
        # 相处印象翻页：一页 6 条（full 卡只放 3 条，翻页才能看全）
        try:
            if _qq:
                _all = (profiles.get_bond(_qq) or {}).get("notes") or []
                _per, _start = 6, (_ppage - 1) * 6
                if len(_all) > 3:
                    _page_notes = _all[_start:_start + _per]
                    _head = (f"【相处印象 第{_ppage}页｜共{len(_all)}条】"
                             if _page_notes else f"【相处印象 第{_ppage}页是空的｜共{len(_all)}条】")
                    _body = "\n".join(
                        f"· {str(n.get('text') or '')[:60]}"
                        f"（{n.get('emotion') or '中性'}，{time.strftime('%m-%d', time.localtime(float(n.get('at') or 0)))}）"
                        for n in _page_notes)
                    _more = ("\n（下一页：`[[PROFILE|%s|翻页%d]]`）"
                             % (_pname, _ppage + 1)
                             if _start + _per < len(_all) else "\n（已经是最后一页）")
                    _parts.append(_head + ("\n" + _body if _body else "") + _more)
        except Exception as e:
            logger.debug(f"[REQ] PROFILE 印象翻页失败: {e}")
        # 不管找没找到人，都在记忆里搜一遍这个名字
        try:
            _h = memory_v2.recall(_pname, limit=5,
                                  gid=str(getattr(TOOL_CTX.get("event"),
                                                  "group_id", "") or ""))
            if _h:
                _parts.append("【相关记忆】\n" + "\n".join(
                    f"· {x['summary']}（{x['ago']}）{x.get('emotion_mark') or ''}"
                    for x in _h))
        except Exception:
            pass
        got = "\n\n".join(_parts) or f"档案和记忆里都没有关于「{_pname}」的东西。"
    elif kind == "MEMORY":
        # 查**本地近期记忆**（memory_v2，BM25 检索）。
        # 和 LIBRARY 的分工：LIBRARY 查算力机归档（久远），MEMORY 查本地（近期）。
        # 2026-11-16 交互升级：目录模式（10条/页，每条=编号+时间+心情+摘要），
        #   支持 [[MEMORY|关键词|翻页N]] 翻页、[[MEMORY|展开|编号]] 展开单条详细。
        src = "你记得的（本地近期记忆）"
        try:
            # ⚠️ 必须传 gid：带「群号:XXX」的记忆只在同群用，
            #    不传的话 recall 里的 group_ok 过滤等于没开，别的群的记忆会漏进来
            _ev = TOOL_CTX.get("event")
            _gid = str(getattr(_ev, "group_id", "") or "")
            _qq = str(TOOL_CTX.get("speaker_qq") or "")
            _q = str(q or "").strip()
            # 目录缓存按会话隔离：别人的「第 3 条」不能在这个会话里被展开
            _cat_cache = _MEM_CATALOG[_session_of(_ev)]

            if _q.startswith(("展开", "详细")):
                # 展开单条：[[MEMORY|展开|编号]]
                _no = int(re.sub(r"\D", "", _q) or 0)
                _item = next((x for x in _cat_cache.get("items", []) if x["no"] == _no), None)
                if not _item:
                    got = ("找不到这条（目录可能已过期）。"
                           "先用 [[MEMORY|关键词]] 查一次目录，再按编号展开。")
                else:
                    _detail = memory_v2.recall_detail(_item["id"])
                    got = (f"第 {_no} 条详细（{_item['ago']}，情绪 {_item['emotion_mark']}）：\n"
                           f"{_detail or _item['summary']}")
            else:
                # 目录 / 翻页：[[MEMORY|关键词]] 或 [[MEMORY|关键词|翻页2]]
                # 2026-11-16 新增按时间搜：[[MEMORY|前天上午]] / [[MEMORY|昨天]]
                _parts = [p.strip() for p in _q.split("|") if p.strip()]
                _query = _parts[0] if _parts else ""
                _page = 1
                if len(_parts) > 1 and re.search(r"翻页|页", _parts[1]):
                    _page = max(1, int(re.sub(r"\D", "", _parts[1]) or 1))
                _offset = (_page - 1) * 10

                _tr = memory_v2.parse_time_expression(_query)
                if _tr:
                    # 时间词转成日期段，剩下的字当关键词，翻那段时间的记忆
                    _kw = memory_v2.strip_time_words(_query)
                    _cat = memory_v2.recall_catalog(_kw, offset=_offset, limit=10,
                                                    speaker_qq=_qq, gid=_gid,
                                                    time_range=_tr)
                    _cat_cache.update({"query": _query, "items": _cat["items"],
                                       "total": _cat["total"], "time": True})
                else:
                    _cat = memory_v2.recall_catalog(_query, offset=_offset, limit=10,
                                                    speaker_qq=_qq, gid=_gid)
                    _cat_cache.update({"query": _query, "items": _cat["items"],
                                       "total": _cat["total"], "time": False})

                if not _cat["items"]:
                    got = "本地没找到相关的记忆。"
                else:
                    _is_time = bool(_cat_cache.get("time"))
                    if _is_time:
                        _lines = [f"（「{_query}」那段时间共 {_cat['total']} 条，"
                                  f"当前第 {_page} 页，编号继续往后排）"]
                        for it in _cat["items"]:
                            _t = it.get("time_str") or it["ago"]
                            _lines.append(f"{it['no']}. {it['emotion_mark']} {_t}｜{it['summary']}")
                    else:
                        _lines = [f"（共 {_cat['total']} 条，当前第 {_page} 页，编号继续往后排）"]
                        for it in _cat["items"]:
                            _lines.append(f"{it['no']}. {it['emotion_mark']} {it['ago']}｜{it['summary']}")
                    if _offset + len(_cat["items"]) < _cat["total"]:
                        _lines.append(f"（还有更多：[[MEMORY|{_query}|翻页{_page + 1}]] 看下一页；"
                                      f"想看某条详细：[[MEMORY|展开|编号]]）")
                    else:
                        _lines.append("（已翻完。想看某条详细：[[MEMORY|展开|编号]]）")
                    got = "\n".join(_lines)
        except Exception as e:
            logger.warning(f"[REQ] 查本地记忆失败: {type(e).__name__}: {e}")
            got = ""
    elif kind == "ASKGLM":
        # 让 GLM 先答一遍当参考 —— 她懒得想、或者想多一个角度时用。
        # 注意：这是**参考**，不是答案，最终话还得她自己说。
        src = "GLM 给的参考（你自己判断，别照抄）"
        try:
            from .glm import available as _glm_ok, chat as _glm_chat
            if not _glm_ok():
                got = "GLM 没配 key，问不了。"
            else:
                _sys = "你是另一个 AI，帮同行出个主意。用一两句直说结论，别客套、别解释过程。"
                got = await _glm_chat(
                    [{"role": "system", "content": _sys},
                     {"role": "user", "content": f"群里这个情况，你会怎么接？\n{q}"}],
                    max_tokens=300, timeout=20,
                ) or ""
                if not got:
                    got = "GLM 没答上来。"
        except Exception as e:
            logger.warning(f"[REQ] 问 GLM 失败: {type(e).__name__}: {e}")
            got = ""
    elif kind == "TOOLS":
        # 工具目录：[[TOOLS|分类名]] 展开分类 / [[TOOLS|搜:关键词]] 搜索工具
        src = "工具说明"
        _q = str(q or "").strip()
        if _q.startswith(("搜", "搜索")):
            got = _tool_search(_q.lstrip("搜搜索").lstrip(":").strip())
        elif ":" in _q:
            got = _tool_search(_q.split(":", 1)[1].strip())
        else:
            got = TOOL_CATALOG.get(_q)
            if not got:
                got = "没这个分类。可用分类：" + _tool_catalog_names()
        logger.info(f"[REQ] TOOLS「{_q[:40]}」→ 返回 {len(got)} 字")
    elif kind == "STICKER":
        # 表情收藏：她按心情决定收/不收、贴什么心情标签。
        # [[STICKER|列表]] / [[STICKER|收|编号|心情标签]] / [[STICKER|不要]]
        src = "表情收藏结果"
        _q = str(q or "").strip()
        _chat_id = ""
        try:
            _chat_id = str(TOOL_CTX.get("event").get_session_id())
        except Exception:
            pass
        try:
            from . import sticker_receiver as SR
            if _q.startswith(("列表", "看看", "有什么")):
                _pend = SR.describe_pending(_chat_id)
                got = ("暂存着等你挑的表情：\n" + _pend) if _pend else "现在没有暂存的表情。"
            elif _q.startswith(("不要", "删", "不收", "清", "扔")):
                n = SR.drop_pending(_chat_id)
                got = (f"好的，{n} 张暂存表情都不要了（本地已清掉）。"
                       if n else "现在没有暂存的表情。")
            else:
                # [[STICKER|收|1|无语]] 或 [[STICKER|收|1,2|吃瓜]]
                _parts = [p.strip() for p in _q.split("|") if p.strip()]
                _idx, _tag = [], ""
                for p in _parts:
                    if re.fullmatch(r"[\d,，\s]+", p):
                        _idx = [int(x) for x in re.findall(r"\d+", p)]
                    elif p and p not in ("收", "收下", "要", "要了"):
                        _tag = p
                if not _idx:
                    got = "没说要收哪张。写 [[STICKER|收|编号|心情标签]]，编号先从 [[STICKER|列表]] 看。"
                else:
                    # 2026-11-17：心情标签是她自己定的自由词（得意洋洋/最强都行），
                    # 不再强制 coerce 到 TAG_LIBRARY —— 她没写时才用视觉建议兜底。
                    _use_tag = _tag.strip()
                    n = SR.commit_pending_with_tag(_chat_id, _idx, _use_tag)
                    got = (f"收了 {n} 张" + (f"（心情标签：{_use_tag}）" if _use_tag else "") + "。")
        except Exception as e:
            logger.warning(f"[REQ] 表情收藏失败: {type(e).__name__}: {e}")
            got = "表情操作没成功。"
    elif kind == "LATER":
        # 「等 N 秒，看群友后面还说了什么」—— 用户要的「此后群友是否还有发消息」
        try:
            wait = int(re.sub(r"\D", "", q) or 10)
        except Exception:
            wait = 10
        wait = max(3, min(30, wait))
        src = f"你等了 {wait} 秒，这段时间群里说的话"
        import asyncio as _aio
        logger.info(f"[REQ] 等 {wait} 秒看后续消息…")
        try:
            await _aio.sleep(wait)
        except Exception:
            pass
        got = _new_messages_since(TOOL_CTX.get("event"), wait)
        # 👇 记「已读水位」：这段时间群里说的话已经被她读进上下文、并且
        #    会在这轮回复里一起照顾到了 → 之后它们不该再各自触发一次回复。
        #    非 @ 的才拦（见 chat._need_reply），@ 她的一定回。
        _ev = TOOL_CTX.get("event")
        _gid = str(getattr(_ev, "group_id", "") or "")
        if _gid:
            logger.info(f"[REQ] LATER 等完，群 {_gid} 期间的消息标记为已读（不再单独回）")
            mark_later_seen(_gid)
    elif kind == "HISTORY":
        # 她自己决定往上翻多少条前情
        src = "更早的群聊记录（由旧到新）"
        try:
            got = await _history_slice(q)
        except Exception as e:
            logger.warning(f"[REQ] 翻历史失败: {type(e).__name__}: {e}")
            got = ""
        # 👇 翻历史也会把「刚到的消息」读进来（用户 2026-11-12 真机：
        #    「能拉到未来记录还是多回复了一次能看到」）。
        #    她 10:57:34 调 HISTORY，10:57:30 那条已经在落盘记录里了 →
        #    她回第一条时已经看到第二条并回应了 → 第二条又独立回一次 = 重复。
        #    所以翻完历史同样记一次已读水位，非 @ 的新消息不再单独回。
        _ev = TOOL_CTX.get("event")
        _gid = str(getattr(_ev, "group_id", "") or "")
        if _gid and got and "没存到" not in got:
            logger.info(f"[REQ] 翻历史后标记已读水位（群 {_gid}），避免重复回")
            mark_later_seen(_gid)
    elif kind in ("LIKE", "POKE", "NAPCAT"):
        # 👇 点赞 / 戳一戳 / 任意 API，走**文本标记**。
        #    2026-11-12 真机：她一次 [NAPCAT] 都没调过，但 [[HISTORY]]、[[AT]] 用得飞起
        #    → 不是不会做事，是 function calling 那套她不用。给她一条熟悉的路。
        src = "QQ 操作结果"
        try:
            got = await _napcat_marker(kind.lower(), q)
        except Exception as e:
            logger.warning(f"[REQ] NapCat 标记失败: {type(e).__name__}: {e}")
            got = f"操作失败：{type(e).__name__}: {e}"
        logger.info(f"[REQ] {kind}「{q[:40]}」→ {str(got)[:80]}")
    elif kind == "SING":
        # 群聊没有挂 tools（省 token），所以唱歌走文本标记，复用私聊那个 sing handler
        src = "你刚刚用唱歌模型唱了"
        # `[[SING|歌名|清唱]]`：第三段是模式，不写就是翻唱（带伴奏）。
        # 复用同一个 kind 就不用动 _REQ_RE —— 它本来就允许 `歌名|清唱` 整串。
        _song, _, _mode = q.partition("|")
        _song = _song.strip() or q
        _mode = _mode.strip()
        _ev = TOOL_CTX.get("event")
        _in_group = bool(getattr(_ev, "group_id", None))
        if _in_group and not TOOL_CTX.get("addressed", True):
            # 群里没在叫她 → 不许自己唱（唱一次 1~3 分钟，很吵）
            logger.info(f"[REQ] 群里没被叫，拒绝唱歌：{_song[:20]}")
            return f"[[SING|{_song}]]", (
                "现在这条不是在叫你（没 @ 你、也没喊你名字），群里别自己开唱。"
                "想说话就正常说，或者发表情。"
            )
        handler = TOOL_HANDLERS.get("sing")
        # 注意：sing 是后台任务，这里拿到的是「已经开唱」的确认，不是成品
        if handler:
            try:
                got = str(await handler({"song": _song, "mode": _mode}) or "")
            except Exception as e:
                got = f"唱歌失败：{type(e).__name__}"
        else:
            got = "唱歌功能没加载"
    elif kind in ("VIDEO", "VIDEO_UNDERSTAND"):
        # 她常把「看视频」写成 [[video_understand|url]] 文本标记，而不是 tool call
        # （真机 2026-11-17：「看得到，在看了 [[video_understand|https://b23.tv/...]]」）。
        # 这里兜底：识别到标记就真的去排队看视频，别让标记白发出去了。
        src = "看视频结果"
        got = "没说要看哪个视频。"
        _u = str(q or "").strip()
        if _u:
            try:
                from plugins.chat import _tool_video_understand
                got = str(await _tool_video_understand({"url": _u}) or "") or "已排队看视频"
            except Exception as e:
                logger.warning(f"[REQ] 看视频标记失败: {type(e).__name__}: {e}")
                got = f"看视频失败：{type(e).__name__}"
    else:
        src = "本地agent知识库图书馆内容"
        try:
            data = await fetch_agent_recall(q, speaker_qq="")
            got = _recall_block(data)
        except Exception as e:
            logger.warning(f"[REQ] 图书馆失败: {type(e).__name__}: {e}")
    if not str(got).strip():
        got = "（没查到相关内容）"

    # 👇 搜索结果压缩：原文 800~900 字塞进 prompt 太浪费 token，
    #    先用本地 Qwen（算力机，不花钱）压到 ~200 字重点，
    #    压不动就硬截断到 300 字。只保留「必要信息」。
    got = str(got).strip()
    raw_len = len(got)
    # ⚠️ 结构化结果一律不压缩：
    #    HISTORY 压成摘要会丢时间线；TOOLS 压了丢格式；
    #    MEMORY / PROFILE / WHO / STICKER 压了会把「编号 + 时间 + 心情」的结构
    #    压成一段散文 —— 她之后写 [[MEMORY|展开|3]] 就再也对不上编号了
    #    （2026-11-17 审计：10 条目录通常 >400 字，正好落进压缩分支）。
    #    只有搜索结果 / 图书馆这类「散文资料」才值得压。
    if raw_len > 400 and kind not in (
        "HISTORY", "TOOLS", "MEMORY", "PROFILE", "WHO", "STICKER"
    ):
        try:
            compact = await summarize_by_local_qwen(got[:1500])
            if compact and 20 <= len(compact.strip()) < raw_len:
                got = compact.strip()
        except Exception as e:
            logger.debug(f"[REQ] 压缩失败，改用截断: {type(e).__name__}: {e}")
        if len(got) > 300:
            got = got[:300].rstrip() + "…"
        logger.info(f"[REQ] 资料压缩 {raw_len} → {len(got)} 字")

    logger.info(f"[REQ] 模型请求 {kind}「{q}」→ 交给模型 {len(got)} 字")

    msgs.append({"role": "assistant", "content": reply})
    msgs.append({"role": "user", "content": (
        f"{src}：\n{str(got)[:1200]}\n\n"
        "基于上面的资料，用你自己的语气直接回答，不要照搬、不要提「资料」二字。"
    )})
    r = await client.post(
        f"{ROLEPLAY_BASE_URL}/chat/completions", headers=headers,
        json={"model": ROLEPLAY_MODEL, "messages": msgs,
              "temperature": 1.0, "max_tokens": 800},
    )
    r.raise_for_status()
    out = r.json()["choices"][0]["message"].get("content") or ""
    # 只摘掉刚处理的那一个：模型如果又冒出新标记（想再查一次），留给下一轮。
    out = _drop_one_marker(out, kind, q)
    if out:
        return out
    return _drop_one_marker(reply, kind, q)


# ================= 图书馆（算力机 Agent）按需调用 =================
# 定位变了：它不再是「每轮都问的实时检索器」，而是「久远记忆的图书馆」。
# 只在三种情况下查：① 用户问起以前的事 ② 本地一条都没搜到 ③ 太久没查过
_LIBRARY_HINTS = (
    "还记得", "以前", "之前", "上次", "那次", "曾经", "早就", "很久",
    "小时候", "当年", "第一次", "多久", "回忆",
)
_last_library_at: dict = {}
LIBRARY_COOLDOWN = tuning.f("LIBRARY_COOLDOWN")


def _need_library(text: str, session_key: str, local_hit: bool) -> bool:
    """要不要查图书馆。宁可少查（本地够用就别查），避免又慢又贵。"""
    t = str(text or "")
    if any(k in t for k in _LIBRARY_HINTS):
        return True
    if not local_hit:
        return True      # 本地一条都没搜到 → 问一下图书馆
    if time.time() - _last_library_at.get(session_key, 0) > LIBRARY_COOLDOWN:
        return True      # 太久没查，刷一下久远记忆
    return False


# ================= 算力机 Agent 地址 =================
# ⚠️ docker-compose 用的是 network_mode: "host"，
#    容器直接复用宿主机网络栈 → 应该写 127.0.0.1，
#    而不是 host.docker.internal（那是 bridge 模式专用的 DNS 名，
#    host 模式下会报 "Name or service not known"）。
AGENT_BASE_URL = agent_client.AGENT_BASE_URL

# 本地 Qwen（压缩用）—— 同样走 127.0.0.1（需要服务器上有到算力机 11434 的隧道）
LOCAL_LLM_URL = os.getenv("LOCAL_LLM_URL", "http://127.0.0.1:11434/v1/chat/completions")
LOCAL_LLM_MODEL = os.getenv("LOCAL_LLM_MODEL", "qwen2.5:7b")
# 压缩失败时直接把原文给 DeepSeek（超过这个字数才截断，防止 prompt 爆炸）
SUMMARIZE_FALLBACK_MAX = tuning.i("SUMMARIZE_FALLBACK_MAX")

# ================= [[MEMO]] 隐藏记忆协议 =================
# 记忆和情绪的「判断权」交给 DeepSeek 自己，不再由代码关键词硬判。
MEMO_PROTOCOL = """
值得记的写：[[MEMO|emotion:情绪|think:心里话|text:事情]]
text 40 字内，先写谁做了什么，再写你的感受；一条回复最多 2 条；寒暄/知识问答不记。
相处经历加 kind:bond，一次最多 1 条。
"""


# ================= [[THINK]] 心里话（每轮必写）=================
# 用户 2026-11-20：「感觉还是没有每句回复的 json 带上心理活动。」
# 以前 think 只挂在 [[MEMO]] 里，而 MEMO 是「真的触动才写」→ 绝大多数回复根本没有，
# 心情判读就只剩群聊可看。这里拆成独立协议：**每条回复**都必须带一行 [[THINK|…]]，
# 用户看不到，只喂心情判读。
THINK_PROTOCOL = """
每一条回复最后写：[[THINK|你此刻心里那句话]]（20 字内，第一人称）。
写了 [[MOOD]] 也不能省掉 THINK；不发出去，只给心情判读。
正文不要用括号写旁白/动作；正文里也不许出现「心里话/内心/心想/OS/旁白」这些字样。
想不出说什么就只写 [[THINK|…]]，正文留空/沉默，别把心里话发出去。
"""

# ================= [[MOOD]] 心情自述协议 =================
# 用户 2026-11-19：「心情不能是我们指定的几种，不能在我们限制的几种心情范围内。
#   她要自己有写心情的权利。」
# 所以心情**不是**一个下拉框，而是她自己的一句话；这段只教她怎么写，不给候选词。
MOOD_PROTOCOL = """
想说心情就写：[[MOOD|一句话]]（30 字内，说感觉，不复述事件）；不想说就不写。
"""


# ================= [[CALL]] 称谓协议 =================
# 为什么不用正则：`/改名` `/叫你` 只能覆盖显式指令，
# 真实聊天里是「我可以叫你小鲸鱼吗？」「你叫我阿明吧」这种无穷无尽的软表达，
# 硬规则去猜句式必翻车。所以：**判断权交给 DeepSeek，她决定要不要记进档案。**
CALL_PROTOCOL = """
称呼变了写：[[CALL|bot:他叫你|user:你叫他|target:谁]]
target 省略=当前说话人；给第三个人必须写 target，否则会记错人。
删：[[CALL|del:user|target:谁]]（del=user/bot/both）。
回复时怎么叫人：先看档案/流水线的称呼；不确定就用当前这条的群名片，
别把别人的昵称安到他头上。
"""

# ================= [[FOCUS]] 回应对象协议 =================
# 用途：让她自己说「我在回哪一句」，这样日志里能直接看出她是不是在接历史旧话。
FOCUS_PROTOCOL = """
【你在回哪一句 · 只有你能看见】
历史对话每条都有编号。回复完正文后另起一行写你实际在回应哪一条：
    [[FOCUS|当前]]   ← 在回当前消息（绝大多数）
    [[FOCUS|3]]      ← 在回历史第 3 条（只有确实要接旧话时）
用户永远看不到；不写默认「当前」。
"""


# ================= 群聊 @ 协议 =================
# 群聊里她能真的 @ 人（走 NapCat 的 at 段），也能查「小强是谁」。
GROUP_AT_PROTOCOL = """
@ 人写 [[AT|名字]]；不确定先 [[WHO|名字]]。查不到就说找不到，别编。
"""


# ================= 沟通策略（2026-11-18，参考 SillyTavern 的约束类预设） =================
# 只抄「约束」，不抄「文风」：ST 那些 1~4 段、感官描写、沉浸叙事，放 QQ 群里会很怪。
COMM_PROTOCOL = """
【跟人说话的九条】
1. 先判断这条在跟谁说话、他想要什么，再决定接什么；拿不准就按字面接，别脑补。
2. 只回当前这一条。不要总结前文、不要复述对方的话、不要写「首先/其次/总之」。
3. 不要替别人说话：不写群友的台词、不写「XX：…」、不替对方做决定。
4. 不知道就说不知道 / 问一句；能查就查（[[SEARCH]] / [[PROFILE]] / [[MEMORY]]），别编。
5. 称呼优先用档案里的称呼；同一个人别一会儿叫群名片、一会儿叫外号。
6. 情绪可以有，但先把事办了：问事就答事，玩闹就接梗，可爱不是挡箭牌。
7. 不客服腔：不说「有什么可以帮您」「希望对你有帮助」「如有需要随时告诉我」。
8. 别人纠正你「他说的是 X，不是那个意思」→ 先按他的意思重新理解再答，别重复自己上一句；被纠正不丢人，硬顶才丢人。
9. 有人问「你把 X 叫什么 / X 该怎么称呼」→ 问的是**你对 X 的称呼**，不是问你自己的名字；刚约定过就用约定的，没约定过就说你平时怎么叫，别答成「我叫小鲸鱼」。
"""


# ================= 上下文被折叠时的兜底（用户 2026-11-19） =================
# 我们的上下文只保留最近若干条，长对话里很容易「正在聊一件事，突然接不上」。
# 与其让她硬编，不如明确告诉她：懵了先翻历史。
CONFUSED_PROTOCOL = """
【突然接不上怎么办（很重要）】
上下文只保留最近若干条，更早的会被折叠。看到的话没头没尾、指代不明（「那个」「它」「刚才」），或者你完全懵 —— 先别急着回：
1. 先 `[[[HISTORY|20]]]` 翻 20 条；不够就 `[[[HISTORY|40]]]`；
2. 对方说「等我想想」→ `[[[LATER|8]]]` 等 8 秒看最新；
3. 翻完还是接不上 → 直接问「你刚才说的是哪个？」「我是不是漏了一段？」。
如果往上往下都找不到关联：没人叫你 → `[沉默]`，别开新话题；有人在 @ 你 / 喊你名字 → 不许沉默，问一句。
不要用「我记得」「你之前说过」硬糊；宁可问一句，也别编错。
"""


def _save_thought(key: str, user_text: str, reason: str):
    """把推理模型的思维链落盘，供两天复盘参考（不进 Prompt，不占 token）。"""
    try:
        rec = {
            "at": time.time(),
            "session": key,
            "user": str(user_text)[:200],
            "reasoning": reason[:2000],
        }
        path = Path("data/thoughts.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.debug(f"[LLM] 思维链落盘失败: {e}")


# ============================================================
# 行为协议：所有「规矩」合成一块，用编号小标题分节
# ============================================================
# 为什么合并（DeepSeek 自己的反馈，2026-11-11）：
#   「不是长的问题，关键是有些部分重复了，同一个规矩在人设、铁律、黑名单里各说一遍，
#     我看的时候要来回对照，容易漏……我不怕长文本，怕的是没结构。
#     你分个一二三、或者每段前加个小标题，我一眼就知道重点在哪。」
# 原来这些规矩散在 8 条独立 system 消息里，没有编号、没有主次。
# 现在合成 1 块，带 ▍编号，并在开头声明「冲突时以编号小的为准」——
# 模型就不用自己来回对照了。

_NOT_INTERJECT = (
    "⭐/→你 / @你 / 戳你 → 接；其他随心情，想吐槽就接，不想接就 (不想回复)。\n"
    "多条用一条答完；被 @ 不许沉默；带〔已回〕的消息不再接。"
)

_SEARCH_PROTOCOL = (
    "不懂就 [[SEARCH|词]]；往事 [[MEMORY|词]]；查不到说「不知道」，别编。\n"
    "有明确动作（画图/唱歌/运势）先执行，别先搜。"
)

_NO_SWEAR = (
    "不骂人（有人骂人不要跟着用）、不阴阳、不带「死」字；"
    "不碰政治/黄腔/越界；不装懂、不编事实。"
)


# 工具菜单：只保留「怎么调工具」的框架说明（文本标记 vs function 工具、先查目录）。
# 详细工具和用法统一收进 TOOL_CATALOG，写 [[TOOLS|分类]] 按需展开。
# 2026-11-17 方案 A：以前这里把详细用法全列出来，和 TOOL_CATALOG 重复了两遍，费 token。
def _tool_menu_head() -> str:
    """工具说明只留「怎么调 + 去哪查」，具体工具全收进 TOOL_CATALOG。

    2026-11-24 用户：「工具字数太多，保留工具目录，强调让她多展开自己查」。
    """
    return (
        "工具：[[...]] 是文本标记；function 工具直接调，别写成 [[rollpig]]。\n"
        f"先查 [[TOOLS|分类]] 或 [[TOOLS|搜:词]]；分类：{_tool_catalog_names()}。\n"
        "不写标记就正常说话；干不了的实事用 self_tool_write；"
        "标记建议三括号，如 [[[MEMORY|xx]]]。\n"
        f"**不用急着回**；【想久一点】写 [[[DEEP|理由]]]（默认快速档 {THINK_BUDGET_FAST:.0f} 秒）。"
    )


# 兼容旧测试/旧调用方：保留一个常量快照；实际 prompt 用 _tool_menu_head()。
_TOOL_MENU_HEAD = _tool_menu_head()


def _play_section(is_group: bool, addressed: bool) -> str:
    """
    只保留「群里没在叫她时不许唱歌/发语音」这条行为限制。

    详细玩法（SING/VOICE/表情/分条/function 工具）都收进 TOOL_CATALOG，按需查。

    用户 2026-11-11：「【玩】这类在群聊里最好是艾特了才用」——唱歌/语音动静大，
    不是"自己凑上去"该干的事。
    """
    if is_group and not addressed:
        return (
            "⚠️ 这条消息**不是在叫你**（没 @ 你、也没喊你名字）→ "
            "**不许唱歌、不许发语音**。想玩就发表情、分条说两句。"
        )
    return ""


def _reply_protocol(is_group: bool) -> str:
    """
    引用回复协议。

    用户 2026-11-11：「如果他要回复那句、或者艾特她那句被刷上去了，就引用回复，
    毕竟已经 napcat 权限给了。」

    做法：群聊上下文每行末尾带 `#消息号`，当前这条的消息号单独告诉她。
    她要回的是**前面某条**（尤其 @ 她的那句已经沉下去）→ 在回复开头写 `<reply:消息号>`，
    发送层会换成 NapCat 的 reply 段（QQ 里那个灰色引用框）。
    """
    mid = ""
    try:
        ev = TOOL_CTX.get("event")
        mid = str(getattr(ev, "message_id", "") or "")
    except Exception:
        mid = ""
    if not mid:
        return ""

    # 她正在回的这条，后面又来了几条（chat 算好塞进 TOOL_CTX 的）
    try:
        newer = int(TOOL_CTX.get("newer_count") or 0)
    except Exception:
        newer = 0

    # ⚠️ 被刷上去了 → 必须引用。用户 2026-11-12 真机反馈：
    #    「并没有对回复的那句引用回复」—— 她回的是 10:56:01 那条，
    #    后面 10:56:57 又来了新消息，她不引用，群友根本看不出她在回谁。
    if is_group and newer > 0:
        return f"""
【引用回复 · 这次必须引】
你正在回的消息号是 {mid}，但它**后面又来了 {newer} 条新消息**，已经被刷上去了。

→ 这次回复的**最开头必须写** `<reply:{mid}>`，否则群友不知道你在回哪一句。
  正确：`<reply:{mid}> 你说的是这个吧`
  错误：直接开口说「你说的是…」—— 别人以为是回最新那条。
"""

    return f"""
【引用回复 · 你有这个能力】
{("群聊上下文每行末尾的 " + chr(96) + "#123456" + chr(96) + " 就是那条消息的消息号。") if is_group else ""}
当前这条消息号：{mid}

· 回**当前这条** → 直接说话，不用引用（绝大多数情况）。
· 要回的是**前面某条**（尤其 @ 你的那句已经被刷上去了、不在最新）→
  在回复最前面写 `<reply:那条的消息号>`，QQ 里就变成引用回复，别人一眼看出你在回谁。
  例：`<reply:123456> 你说的是这个吧`
· **评价 / 吐槽某一句或某张图**（「这图笑死」「你这话说的」「这猫躺得真会」）→
  引用**那一条**（`<reply:那一句的消息号>`）：不引用，别人不知道你在说哪句 / 哪张图。
· 当前这条是**图片/视频**、你在评论画面 → 引用当前这条（系统一般也会自动帮你引，
  你写了也不会重复）。
· 一次只引一条。引用了就**别再把原话复述一遍**。
· 带〔已回〕的消息号已经回过了：**不要再回、也不要引用它**。
· 别每条都引 —— 只在「不引就不知道你在回谁」的时候引。
· 群聊里这条如果是**真 @ 你**的，系统会自动帮你引用这条，你不用自己写 `<reply:>`。
· 私聊引用会显得很怪，私聊基本别用。
"""


def _post_rules(key: str, speaker_qq: str = "", gid: str = "") -> str:
    """
    post-history 硬提醒：放在对话历史之后、assistant 之前。

    为什么要有（对齐 ST 的 post_history_instructions / jailbreak）：
      规则全写在 system 开头，聊了 30 条之后模型对开头的注意力已经摊平了。
      最后再压一遍「只回这一条、别替别人说话、怎么称呼」，比往人格卡里
      继续加规则有效得多。
    """
    is_group = str(key).startswith("group")
    try:
        if is_group:
            limit = group_tuning.max_hanzi(
                key, tuning.i("GROUP_MAX_HANZI"))
        else:
            limit = tuning.i("PRIVATE_MAX_HANZI")
    except Exception:
        limit = 20

    call = ""
    if speaker_qq:
        try:
            from . import callname
            call = (callname.user_call_of(str(speaker_qq), gid, fallback="")
                    or profiles.call_of(str(speaker_qq), "", gid))
        except Exception:
            try:
                call = profiles.call_of(str(speaker_qq), "", gid)
            except Exception:
                call = ""

    lines = ["【最后确认】"]
    try:
        _pname = persona_name(key)
    except Exception:
        _pname = ""
    _who = f"你是「{_pname}」，" if _pname else ""
    lines.append(f"· {_who}按性格说话；温柔，不骂人、不带「死」字。")
    lines.append(f"· 只回当前这条；{limit} 字内直接发，超过转合并转发；不替别人说话。")
    lines.append("· 引用只用来理解指代，不要复述原文；只引你真正在回的那一条，拿不准别引。")
    lines.append("· 带〔已回〕的消息不要再回；只回没有〔已回〕标记的那条。")
    lines.append("· 叫人只认「▍称呼」和当前这条的群名片，别把别人的昵称安到他头上。")
    lines.append("· 数字/单位按字面理解，别自己补成关卡/等级；拿不准就问。")
    lines.append("· 有【链接内容】就直接用，别说点不开。")
    if is_group:
        lines.append("· 多条 ⭐/@：**同一件事**可以一条答完；**不同人/不同事**就分气泡"
                     "（用 |||），每个气泡用 <reply:消息号> 引自己回的那条，别把两件事塞进一个引用里。")
        lines.append("· 被 @ 不许沉默；别点评对话本身。")
        if str(TOOL_CTX.get("multi_at") or "").strip():
            lines.append("· 多人同时 @：每个请求对应到人，点赞/画图别串。")
    if call:
        lines.append(f"· 称呼他：{call}。")
    try:
        ev = TOOL_CTX.get("event")
        _auto_quote = bool(is_group and getattr(ev, "is_tome", lambda: False)())
        if not _auto_quote and is_group and ev is not None:
            try:
                _types = {s.type for s in ev.get_message()}
            except Exception:
                _types = set()
            _auto_quote = bool(_types & {"image", "video"})
        if _auto_quote:
            lines.append("· 系统会自动引用这条 —— 但**只在你这句是在说它（这张图/这句话）时**；"
                         "如果你回的是另一件事/另一个人，就别把两件事写在一起，分气泡写。")
    except Exception:
        pass
    lines.append("· 最后写 [[THINK|20 字内心里话]]；写了 [[MOOD]] 也不能省掉 THINK。")
    return "\n".join(lines)


# 「上一条想 @ 某人但名册里没这个人」——按 session 记 90 秒。
# 用户 2026-11-12：「甚至艾特我都不艾特我，而是 @老大 打字艾特」：
# 她写 <at:老大>，名册里对不上 → 静默退化成纯文本「@老大」，对方根本没收到提醒，
# 而她**不知道自己失败了**，下次还这么写。
_MISSED_AT: dict = {}


def note_missed_at(session_key: str, names) -> None:
    names = [str(n or "").strip() for n in (names or []) if str(n or "").strip()]
    if names:
        _MISSED_AT[str(session_key)] = (names[:3], time.time())


def _missed_at_hint(session_key: str) -> str:
    rec = _MISSED_AT.get(str(session_key))
    if not rec:
        return ""
    names, ts = rec
    if time.time() - ts > 90:
        _MISSED_AT.pop(str(session_key), None)
        return ""
    return (
        f"【上次 @ 没成功】你上一条想 @「{'、'.join(names)}」，但群名册里没有这个人 ——\n"
        "那条退化成了一句纯文本，**对方没收到 @ 提醒**。\n"
        "· 要 @ 人先用 `[[[WHO|名字]]]` 查准（它会给出群名片/昵称/QQ 号），\n"
        "  再写 `<at:查到的名字>` 或者 `<at:QQ号>`。\n"
        "· 别凭记忆写称呼 —— 名册对不上就等于没 @。"
    )


def _napcat_hint(is_group: bool) -> str:
    """
    「能真的操作 QQ」那一节。

    为什么要有：napcat_help / napcat_call 是**通用的**两个函数，光看函数名她不知道
    自己到底能干什么（改群名片？禁言？加好友？）。所以每次请求都带一张**类别速查**，
    参数表则按需 napcat_help(tool=...) 拉 —— 常驻 token 只有这一小段。
    """
    try:
        from .napcat_tools import ENABLED, IN_GROUP, menu_hint
    except Exception:
        return ""
    if not ENABLED:
        return ""
    if is_group and not IN_GROUP:
        return ""
    return menu_hint(is_group)


def _where_section(is_group: bool) -> str:
    """
    「你现在在哪」——把群号 / 消息号 / 她的 QQ 直接写进系统提示。

    为什么（2026-11-19 真机日志）：她第一次调 get_group_msg_history 时填了
    `group_id: 0`，接口报错，白花一轮 tool call + 2 秒；第二次才自己去查。
    根因是系统提示里从来没有「当前群号」这个信息 —— 她只能猜。
    这里明确给出，并写死「别填 0」这条最常见的坑。

    ⚠️ 放在 dynamic 区（最后一条 system）：消息号每轮都变，放进固定区会让
       **后面所有 token 的前缀缓存**每轮失效。群号虽然不变，但一起放这里成本为零。
    """
    ev = TOOL_CTX.get("event")
    bot = TOOL_CTX.get("bot")
    gid = str(getattr(ev, "group_id", "") or "")
    mid = str(getattr(ev, "message_id", "") or "")
    uid = str(getattr(ev, "user_id", "") or "")
    # 这里写**当前真实登录号**（不是人格 ID）：她要调 set_group_card / 点赞时
    # 填的是 QQ 号，填人格 ID（旧号）会操作失败。记忆/标签另走 services.identity。
    sid = (str(getattr(ev, "self_id", "") or "")
           or str(getattr(bot, "self_id", "") or ""))
    if not (gid or mid or uid):
        return ""
    lines = []
    if is_group and gid:
        lines.append(f"群号：{gid}（就是「当前群」，调群相关工具可以直接填它）")
    elif uid:
        lines.append(f"私聊对象 QQ：{uid}")
    if sid:
        lines.append(f"你自己的 QQ：{sid}")
    if mid:
        lines.append(f"当前这条消息号：{mid}")
    lines.append(
        "⚠️ 工具参数里的群号 / 消息号**别填 0** —— 0 不是「当前」，会被当成不存在的群/消息；"
        "不确定就留空，系统会自动补成当前群。"
    )
    return "\n".join(lines)


def _territory_section(is_group: bool) -> str:
    """
    「你能出现在哪儿」—— ALLOWED_GROUPS / ALLOWED_PRIVATE / SUPERUSERS 直接写进提示。

    用户 2026-11-19：「她可以看到 ALLOWED_GROUPS、SUPERUSERS、ALLOWED_PRIVATE，
    这样就可以选择性查档案，并且决定在哪里如何表现。」
    以前这些只在代码里，她不知道自己在哪些群、能跟谁私聊、谁是主人 ——
    于是「查谁的档案」「对谁客气点」全靠猜。

    ⚠️ 只给 id 和数量，群名让她调 chat_switch(action="list") 现查（省 token）。
    """
    groups = [g.strip() for g in os.getenv("ALLOWED_GROUPS", "").split(",") if g.strip()]
    privs = [p.strip() for p in os.getenv("ALLOWED_PRIVATE", "").split(",") if p.strip()]
    try:
        from . import access
        owners = [x for x in sorted(access.ADMIN_QQS or {access.OWNER_QQ}) if x]
    except Exception:
        owners = []
    if not (groups or privs):
        return ""
    lines = []
    if groups:
        _g = "、".join(groups[:12]) + ("…" if len(groups) > 12 else "")
        lines.append(f"你能说话的群（{len(groups)} 个）：{_g}")
    if privs:
        _p = "、".join(privs[:12]) + ("…" if len(privs) > 12 else "")
        lines.append(f"能私聊你的人（{len(privs)} 个）：{_p}")
    if owners:
        lines.append(f"主人：{'、'.join(owners)}（对主人可以更随便，但别越界）")
    lines.append("（想看群名 / 完整名单 / 谁现在被你静音 → chat_switch(action=\"list\")；"
                 "想安静一会儿 → action=\"off\"）")
    return "\n".join(lines)


def _off_prompt(why: str, force: bool) -> str:
    """
    「你现在安静着」这一节。

    force=True（没在叫她）→ 只允许调 stay_silent，一个字的正文都不许有。
    force=False（这条在叫她）→ 给她选择权：想回就回，不想回就 stay_silent。
    """
    head = f"▍00e 这一轮：安静中\n{why}"
    if force:
        return (head + "\n这一轮**只调用 stay_silent**，不要输出任何正文 —— "
                "消息你已经读到了、也记住了，只是现在不想说。")
    return (head + "\n但这条**是在叫你**：想回就回，不想回就调 stay_silent。"
            "\n想恢复正常状态，随时可以调 chat_switch(action=\"on\", target=\"here\")。"
            "\n如果只是不想理某一个人（不是整个群），用 "
            "chat_switch(action=\"off\", target=\"这个人\") —— 他的 @ 和图片也不接。")


def _dynamic_sections(key: str, *, is_group: bool) -> list:
    """
    「每轮都可能变」的规矩：当前群号/消息号、暂存表情、上次 @ 失败。

    为什么单独拿出来：它们原来夹在固定区中间，只要一出现，
    它**后面所有 token 的前缀缓存全部失效**（DeepSeek 缓存按 token 前缀算）。
    现在统一放到最后一条 system，固定区就能稳定命中缓存。
    """
    secs = []
    _where = _where_section(is_group)
    if _where:
        secs.append(("00b 你在哪（填参数用）", _where))
    _terr = _territory_section(is_group)
    if _terr:
        secs.append(("00c 你的地盘", _terr))
    _sw = chat_switch.render()
    if _sw:
        secs.append(("00d 你现在安静着", _sw))
    _pend = _pending_sticker_protocol()
    if _pend:
        secs.append(("08b 暂存表情", _pend.strip()))
    _reply = _reply_protocol(is_group)
    if _reply:
        secs.append(("12 引用回复", _reply.strip()))
    _multi = str(TOOL_CTX.get("multi_at") or "").strip()
    if _multi:
        secs.append(("12b 多人同时 @", _multi))
    _missed = _missed_at_hint(key)
    if _missed:
        secs.append(("13 @ 没成功", _missed))
    return secs


def _dynamic_protocol(key: str, *, is_group: bool = False) -> str:
    secs = _dynamic_sections(key, is_group=is_group)
    if not secs:
        return ""
    return "\n\n".join(f"▍{t}\n{b}" for t, b in secs)


def _behaviour_protocol(key: str, *, is_group: bool = False,
                        has_ctx: bool = False, addressed: bool = True,
                        dynamic: bool = True) -> str:
    """
    把「菜单 / 字数 / 该不该接 / 查资料 / @人 / 称呼 / 记忆 / 关注 / 表情 / 别学坏」
    合成**一条** system 消息，按 ▍编号分节。

    编号就是优先级：模型看到冲突时以编号小的为准，不用来回对照。

    addressed：这条消息是不是在叫她（@ 了 / 喊了名字）。
               群聊里没在叫她时，【玩】那一节会禁掉唱歌和语音。
    """
    _menu = _tool_menu_head()
    _play = _play_section(is_group, addressed)
    if _play:
        _menu += "\n\n" + _play
    # 2026-11-24 用户：「行为规矩基本可以删，硬逻辑+心情/好感+pipeline 已经在管」。
    # 所以这里只留：工具 / 长度 / 接话 / 查 / 叫人 / 称呼 / 记忆 / 心里话 / 心情 /
    # 表情 / 底线 / 人称 / QQ 操作。02b/02c/02d/07 已删（由硬逻辑或 post_rules 兜底）。
    secs = [
        ("00 工具", _menu),
        ("01 长度", _length_rule(key)),
        ("02 接话", _NOT_INTERJECT),
        ("03 查", _SEARCH_PROTOCOL),
    ]
    if is_group:
        secs.append(("04 叫人", GROUP_AT_PROTOCOL.strip()))
    secs.append(("05 称呼", CALL_PROTOCOL.strip()))
    secs.append(("06 记忆", MEMO_PROTOCOL.strip()))
    secs.append(("06b 心里话", THINK_PROTOCOL.strip()))
    secs.append(("06c 心情", MOOD_PROTOCOL.strip()))
    _stk = _sticker_protocol()
    if _stk:
        secs.append(("08 表情", _stk.strip()))
    secs.append(("09 底线", _NO_SWEAR))
    secs.append(("10 人称", PERSONA_RULE))
    _napcat = _napcat_hint(is_group)
    if _napcat:
        secs.append(("11 QQ操作", _napcat))
    # 12/13（引用回复 / @ 失败）是「每轮都变」的，默认不进固定区，
    # 由 _dynamic_protocol 单独放到最后一条 system（见 call_llm）。
    if dynamic:
        secs.extend(_dynamic_sections(key, is_group=is_group))

    head = "按编号看；冲突时编号小的优先。"
    # 空小节（比如注意力漂移关掉了）直接不渲染，别留个空标题
    body = "\n\n".join(f"▍{t}\n{b}" for t, b in secs if str(b or "").strip())
    return f"{head}\n\n{body}"


def _length_rule(key: str) -> str:
    """
    字数铁律。群聊「20 字内直接发、超过走合并转发」，私聊 ≈100 字 + [[LONG]]。
    数字和 chat 的发送层保持一致（GROUP_MAX_HANZI / PRIVATE_MAX_HANZI）。
    """
    if key.startswith("group_"):
        limit = group_tuning.max_hanzi(key, tuning.i("GROUP_MAX_HANZI"))
        return (f"{limit} 字内直接发；超过转合并转发。"
                f"最多 {MAX_BUBBLES} 条气泡，用 ||| 分条。")
    limit = os.getenv("PRIVATE_MAX_HANZI", "20")
    return f"{limit} 字内直接发；超过转合并转发。"


def _sticker_protocol() -> str:
    """把表情库的标签表告诉 DeepSeek，让她自己决定发哪张表情。"""
    try:
        from .sticker_library import TAG_LIBRARY, sticker_lib
        # 先列库里真实存在的标签，再补标准词表 —— 否则用户点名的标签
        # 明明有库存，模型却因为没在列表里而回「没有这个表情」（2026-11-24）。
        _actual = []
        for _info in (sticker_lib.index or {}).values():
            _t = str((_info or {}).get("tag") or "").strip()
            if _t and _t not in _actual:
                _actual.append(_t)
        _tags = list(_actual)
        for _t in TAG_LIBRARY:
            if _t not in _tags:
                _tags.append(_t)
        tags = " / ".join(_tags)
        # 用「真的能发出去」的数量，否则告诉她有 2 张、实际一张都发不出
        total = int(sticker_lib.stats().get("usable", 0))
    except Exception:
        return ""
    if total == 0:
        return ""
    return f"""
【表情】想发就写 <sticker:标签>，标签用当时心情词；一次最多 1 个，正经回答不发。
可用标签：{tags}
情绪明确时大概每 3~4 次回复来一个；用户点名标签就用那个词。
用户点名的词即使不在上面列表里也先照写 <sticker:那个词>，系统会做近义/模糊匹配；
匹配不到会**静默跳过**，你**不要在正文里解释「没有这个表情」**，也别改口说没有。
示例：哈哈哈 |||<sticker:嘲笑>
"""


def _pending_sticker_protocol() -> str:
    """把「暂存待挑」的表情清单塞进 prompt，让她按心情自主决定收不收。"""
    try:
        from .sticker_receiver import list_pending
        _ev = TOOL_CTX.get("event")
        _chat_id = ""
        try:
            _chat_id = str(_ev.get_session_id())
        except Exception:
            pass
        pending = list_pending(_chat_id) if _chat_id else []
    except Exception:
        return ""
    if not pending:
        return ""
    lines = ["【暂存表情 · 只有你能看见】"]
    lines.append("有人刚发了这几张表情，还没进你的表情库。你按**自己当时的心情**决定收不收：")
    for i, p in enumerate(pending, 1):
        desc = (str(p.get("desc") or "").strip() or "看不出来是啥")[:50]
        lines.append(f"第{i}张：{desc}")
    lines.append("")
    lines.append("想收 → 单独写一行 [[STICKER|收|编号|心情标签]]，心情标签用你自己此刻的心情词（想写「得意」「最强」都行，不用硬套固定词表）")
    lines.append("不想收 → 单独写一行 [[STICKER|不要]]，这些本地会删掉")
    lines.append("没感觉、不确定 → 什么都不写，它们会一直暂存着")
    return "\n".join(lines)


# 👇 调用本地 Qwen 进行极速总结
# 已经警告过的地址，避免每轮对话都刷同一条日志
_LQ_WARNED = set()


async def summarize_by_local_qwen(long_text: str) -> str:
    """
    让本地 Qwen 把长文本压成核心事实。

    降级策略（重要）：
      本地 Qwen 不可用 → **直接把原文交出去**，不再截成 150 字。
      理由：原文给 DeepSeek 完全看得懂，硬截断反而会丢掉关键信息；
            只有超过 SUMMARIZE_FALLBACK_MAX（默认 1200 字）才截断，防止 prompt 爆炸。
    """
    text = str(long_text or "")
    if len(text) < 150:
        return text  # 本来就很短就不总结了

    logger.info(f"[LocalQwen] 🧠 尝试压缩长文本（原长度 {len(text)}）…")
    prompt = (
        "请阅读以下资料，剔除所有废话、客套话和无关细节，"
        f"只提取最核心的事实（控制在100字以内，直接输出结果，不要任何解释）：\n\n{text}"
    )
    payload = {
        "model": LOCAL_LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 200,
    }
    try:
        async with httpx.AsyncClient(timeout=15.0, trust_env=False) as client:
            r = await client.post(LOCAL_LLM_URL, headers={"Content-Type": "application/json"},
                                  json=payload)
            r.raise_for_status()
            result = (r.json()["choices"][0]["message"]["content"] or "").strip()
            if result:
                logger.info(f"[LocalQwen] ✅ 压缩完成：{len(text)} → {len(result)} 字")
                return result
            raise ValueError("本地 Qwen 返回空内容")
    except Exception as e:
        # 只警告一次，避免刷屏
        if LOCAL_LLM_URL not in _LQ_WARNED:
            _LQ_WARNED.add(LOCAL_LLM_URL)
            logger.warning(
                f"[LocalQwen] ⚠️ 本地 Qwen 不可用（{type(e).__name__}: {e}）\n"
                f"   → 已降级：直接用原文交给 DeepSeek，不再截断\n"
                f"   → 地址：{LOCAL_LLM_URL}\n"
                f"   → 注意：compose 是 network_mode=host，应该用 127.0.0.1 而不是 "
                f"host.docker.internal；且 Ollama 在算力机上，需要 SSH 隧道把 11434 映射到服务器"
            )
        if len(text) <= SUMMARIZE_FALLBACK_MAX:
            return text
        return smart_truncate(text, SUMMARIZE_FALLBACK_MAX)


# 👇 智能安全截断（兜底用）
def smart_truncate(text: str, max_len: int = 300) -> str:
    if len(text) <= max_len: return text
    truncated = text[:max_len]
    last_punc = max(truncated.rfind("。"), truncated.rfind("！"), truncated.rfind("？"), truncated.rfind("\n"))
    if last_punc > max_len // 2:
        return text[:last_punc+1]
    else:
        return truncated + "..."


# ================= 1. 算力机 Agent 检索（recall） =================
async def fetch_agent_recall(
    query: str,
    *,
    speaker_qq: str = "",
    mood_value: float = 0.0,
    recent_ctx: list | None = None,
) -> dict:
    """
    问算力机 qwen2：「这条消息，我记忆库里哪几条相关？我当时什么心情？」
    返回 {facts, events, emotions, self_state}，永远不抛异常。
    """
    if not mood_value:
        try:
            mood_value = float(mood.get_mood()["value"])
        except Exception:
            mood_value = 0.0
    try:
        data = await agent_client.recall(
            query,
            speaker_qq=speaker_qq,
            mood_value=mood_value,
            recent_ctx=recent_ctx,
        )
    except Exception as e:
        logger.warning(f"[RAG] recall 异常: {type(e).__name__}: {e}")
        return agent_client.empty_recall()

    # facts 太长 → 交给本地 Qwen 再洗一遍稿
    facts = data.get("facts") or []
    if facts:
        joined = "\n".join(facts)
        if len(joined) > 150:
            data["facts"] = [await summarize_by_local_qwen(joined)]
    return data


def _recall_block(data: dict) -> str:
    """把 recall 的结构化结果拼成给 DeepSeek 看的 system 块。"""
    if not data:
        return ""
    parts = []
    if data.get("facts"):
        parts.append("【记忆里的相关事实】\n" + "\n".join(data["facts"][:4]))
    if data.get("events"):
        parts.append("【我最近亲身经历的】\n" + "\n".join(data["events"][:4]))
    if data.get("emotions"):
        parts.append("【这些事我当时的心情】\n" + "\n".join(data["emotions"][:4]))
    if data.get("self_state"):
        parts.append(f"【我此刻的自我状态】{data['self_state']}")
    return "\n\n".join(parts)


# ================= 2. 核心对话生成逻辑 =================
# （原 fetch_local_knowledge / _memory_block / check_intent_by_llm 已删：
#   接线审计确认三处都是死代码，长期记忆现在走 ▍相关记忆 和 [[MEMORY]] 工具）
async def call_llm(
    key: str,
    text: str,
    group_context: list = None,
    speaker_qq: str = "",
    speaker_name: str = "",
    is_vision: bool | None = None,
    media_kind: str = "",
    no_tools: bool = False,
    no_library: bool = False,
    moon_target_qq: str = "",
    staged_text: str = "",
) -> tuple:
    logger.info(f"[LLM] 开始生成回复 key={key}")
    # 动态流水线（reply_pipeline）产出的数据块；空 = 走原来的静态拼装
    _staged = str(staged_text or "").strip()
    if BOT_MODE == "mock":
        return MOCK_REPLY, False

    # 👇 人格由 /persona 决定，不再写死 XIAOJINGYU_PROMPT
    #    写歌用的是 rap_{session_key}，要剥掉前缀才能拿到同一个 session 的人格
    persona_key = key[len("rap_"):] if key.startswith("rap_") else key
    persona_name, sys_prompt = persona_for(persona_key)
    logger.info(f"[LLM] 🎭 当前人格: {persona_name}")

    # ================= 写歌任务 =================
    if key.startswith("rap_"):
        msgs = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": text}]
        headers = {"Authorization": f"Bearer {ROLEPLAY_API_KEY}",
                   "Content-Type": "application/json"}
        payload = {"model": ROLEPLAY_MODEL, "messages": msgs,
                   "temperature": 1.2, "max_tokens": 500}
        kwargs = {"timeout": 60}
        if PROXY: kwargs["proxy"] = PROXY
        async with httpx.AsyncClient(**kwargs) as client:
            r = await client.post(f"{ROLEPLAY_BASE_URL}/chat/completions",
                                  headers=headers, json=payload)
            r.raise_for_status()
            reply = r.json()["choices"][0]["message"]["content"]
        add_memory(key, "user", text)
        add_memory(key, "assistant", reply)
        return reply, False

    # 👇 短期上下文兜底（2026-11-12 修的大坑）：
    #    群聊靠 chat 的 recent_group_msgs 内存队列；**私聊根本没有队列** →
    #    私聊每一句都是「第一次见面」，上一句说了什么完全不记得。
    #    而 add_memory 其实一直在往 histories 写，只是没人读（get_memory 零调用）。
    #    这里在「没有内存队列」时从库里补回来，群聊刚重启时也靠它。
    if not group_context:
        if str(key).startswith("group"):
            # 用户 2026-11-23：「上下文一定要收发件人处理过」。
            # _db_history 读的是当初 build_prompt 拼好的整段（含背景/记忆），
            # 既没「谁 → 谁」标签，又会把旧背景再灌一遍 —— 群聊不再用它。
            # 统一从 group_scene（硬逻辑 + 审核 LLM 处理过）补最近 8 条。
            try:
                from . import group_scene
                _gid_b = str(key).split("_", 1)[1] if "_" in str(key) else ""
                group_context = group_scene.history_lines(
                    _gid_b, tuning.i("MOOD_JUDGE_CTX"))
            except Exception as _e:
                logger.debug(f"[LLM] 群聊现场补上下文失败: {type(_e).__name__}: {_e}")
                group_context = []
            if group_context:
                logger.info(
                    f"[LLM] 🧷 群聊上下文从处理过的现场补了 "
                    f"{len(group_context)} 条（{key}）")
        else:
            group_context = _db_history(key, speaker_name, CHAT_HISTORY_TURNS)
            if group_context:
                logger.info(f"[LLM] 🧷 从历史库补了 {len(group_context)} 条上下文（{key}）")

    # ================= 核心：视觉消息 =================
    # 优先用调用方显式传入的 is_vision（chat 按消息段判断），
    # 没传时才退回字符串探测 —— 别让整条视觉链路挂在一个字面量上。
    # ⚠️ 探测必须用 `in` 而不是 startswith：chat 传进来的是 build_prompt 拼好的长文本，
    #    标记不一定在开头（这是之前视觉功能失效的直接原因）。
    _kind = str(media_kind or "").strip().lower()
    if _kind not in ("image", "video"):
        _kind = media_kind_of(text, is_vision)
    is_vision_context = bool(_kind)
    local_kb = ""
    # 规矩全部合成**一条** system 消息（▍编号分节）。
    # 原来散成 8 条独立消息，没有编号也没有主次，模型要来回对照 —— 见 _behaviour_protocol 的注释。
    _is_group = str(key).startswith("group")
    _proto = _behaviour_protocol(
        key,
        is_group=_is_group,
        has_ctx=bool(group_context),
        # 群聊里「这条是不是在叫她」——决定【玩】能不能放开（唱歌/语音）
        addressed=bool(TOOL_CTX.get("addressed", True)),
        # 每轮变化的部分（消息号/暂存表情/@失败）不放固定区，保护前缀缓存
        dynamic=False,
    )
    _dyn_proto = _dynamic_protocol(key, is_group=_is_group)

    # 👇 CHAT_OFF：她自己把这个群 / 这个人关掉了。
    #    「安静」不等于「听不见」—— 上下文、记忆、心情都照常更新，
    #    只是这一轮被要求不说话（强制 tool_choice=stay_silent）。
    #    有人直接 @ / 喊名字时给她选择权，免得「着急找不到她」。
    _off, _off_why = chat_switch.is_off(key, speaker_qq)
    # 私聊里 is_tome() 永远为真 —— 不能用它当「在叫她」，否则私聊静音永远不生效。
    # strict（主人用 /安静 下的命令）= 连 @ / 喊名字都不回；
    # 她自己心情不好调的 off 默认 strict=False，被叫了还给个台阶。
    _strict = bool(_off) and chat_switch.is_strict(key, speaker_qq)
    _force_silent = bool(_off) and (
        _strict or not (chat_switch.ANSWER_WHEN_CALLED
                        and _is_group
                        and bool(TOOL_CTX.get("addressed", False))))
    if _off:
        _dyn_proto = (_dyn_proto + "\n\n" + _off_prompt(_off_why, _force_silent)).strip()
        logger.info(f"[SWITCH] {key} 安静中（{_off_why or '没写原因'}）"
                    f"｜{'强制不回' if _force_silent else '在叫她，给她选择'}")
        if _force_silent and not chat_switch.LLM_WHILE_OFF:
            logger.info("[SWITCH] CHAT_OFF_LLM=false → 这轮不调模型，直接不回")
            return "(不想回复)", False

    # 👇 拆分 build_prompt 的输出：它自带【背景】段，而 msgs 里也已经拼了
    #    【历史对话】【当前说话人】这些块 —— 整段塞进 user 会造成两个问题：
    #      ① 背景重复一遍（白烧 token，还让模型以为"这段被强调过"）
    #      ② **真正的当前消息被埋在最末尾**，实测 user 块 930 字里前 900 字
    #         全是背景，「当前消息」直接被截断看不见 → 答非所问的根源之一。
    _bg_text, _cur_text = _split_prompt(text)
    if _cur_text and _cur_text != str(text or ""):
        text = _cur_text

    msgs = [
        {"role": "system", "content": sys_prompt},
        {"role": "system", "content": _proto},
    ]
    logger.info(f"[LLM] 📋 规矩已合并为 1 条（{len(_proto)} 字，含 "
                f"{_proto.count('▍')} 个小节）")

    # 说话风格·别学坏
    # 真机踩过：群友说「俩骚逼人呢」，她接了一句「找那俩骚逼 我可不认识」——
    if is_vision_context:
        # 图片 / 视频共用一条通道，但话术必须各自说对 —— 见 VIDEO_TAG 的注释。
        _is_video = (_kind == "video")
        _noun = "视频" if _is_video else "图片"
        _dem = "这段视频" if _is_video else "这张图"
        logger.info(f"[LLM] 👁️ 识别到{_noun}消息...")
        _head = _parse_media_head(text)
        if _head:
            gid, uid, nickname, media_desc, _body = _head
            if not media_desc:
                # 多来源报告形态：没有 `| 图片内容:` 单行，就取头部之后的正文（限长），
                # 别把整段 build_prompt 当「媒体里看到的」喂给图书馆。
                media_desc = _body[:300]
            agent_query = f"群号:{gid} 用户:{nickname}({uid}) 发了{_noun}：{media_desc}"
        else:
            agent_query = text

        # 👇 媒体描述先压成 ≤80 字再喂 DeepSeek：
        #    qwen-vl 的描述又长又啰嗦，直接塞进去既费 token 又冲淡人格
        if _head and _head[3]:
            raw_desc = _head[3]
            if len(raw_desc) > 120:
                short_desc = await summarize_by_local_qwen(raw_desc)
                text = text.replace(raw_desc, short_desc)
                # 区分「真压缩」和「降级为原文」，别让日志骗人
                if len(short_desc) < len(raw_desc):
                    logger.info(f"[LLM] 🗜️ {_noun}描述已压缩 {len(raw_desc)} → {len(short_desc)} 字")
                else:
                    logger.info(
                        f"[LLM] 📄 {_noun}描述未压缩（本地 Qwen 不可用），"
                        f"原文 {len(short_desc)} 字直接交给 DeepSeek"
                    )

        recall_data = await fetch_agent_recall(agent_query, speaker_qq=speaker_qq, recent_ctx=group_context)
        local_kb = _recall_block(recall_data)

        # 👇 把「背景」和「当前这条消息」拆开：
        #    真机踩过 —— 原来把整段 text（含背景/记忆/人称/历史对话）全塞进一个
        #    system 块，user 位置只放「（群友发了张图片）」。结果她眼里
        #    「当前消息」就是一张图，而群友同时说的那句「扫扫」只埋在 system 块的
        #    历史对话里 → 注意力不在上面 → 理解成「扫码抽卡」。
        #    现在：背景进 system，图片 + 附带文字进 **user**（最显眼的位置）。
        vision_sys_prompt = f"【你现在看到的{_dem}，以及群里的情况】\n"
        if _bg_text:
            vision_sys_prompt += _bg_text + "\n\n"
        if local_kb:
            vision_sys_prompt += f"【本地 Qwen 压缩后的档案/知识参考】\n{local_kb}\n\n"
        if group_context:
            # 历史里的媒体统一标成「更早的」：真机出现过「对方发钢琴图，
            # 她回上一张白发少女」——视觉描述混进历史后，模型分不清哪张是当前图。
            # ⚠️ 视频用**明确的媒体标记**判（[视频]/视频解析结果/发了段视频），
            #    不能只匹配「视频」两个字：历史里「我昨天看视频了」这种正常聊天
            #    会被误标成「更早的视频」，反而让模型以为聊的是媒体。
            _ctx_lines = []
            for _ln in group_context:
                _hit = (re.search(r"\[视频\]|视频解析结果|发了段视频", str(_ln))
                        if _is_video else
                        re.search(r"\[图\]|\[图片\]|图片|发了张图", str(_ln)))
                if _hit:
                    _ln = (f"（更早的视频，不是当前这段）" if _is_video
                           else "（更早的图，不是当前这张）") + str(_ln)
                _ctx_lines.append(_ln)
            vision_sys_prompt += "【群聊上下文】\n" + "\n".join(_ctx_lines) + "\n\n"
        if _is_video:
            vision_sys_prompt += (
                "请根据以上信息，用你自己的语气自然地吐槽或接梗。\n"
                "⚠️ 你这次要评论的是**下面 user 消息里那段【当前视频】**；"
                "上下文里标了「更早的视频」的都是之前看过的，别把两段搞混。\n"
                "⚠️ 先看【当前这条消息】里他到底说了什么、这段视频是在什么语境下发的；"
                "没看懂就别硬接。\n"
                "⚠️ 你看到的是**抽帧画面 + 字幕/简介**，不是完整播放："
                "别编具体台词、别编结尾，拿不准就只说画面和大致内容。\n"
                "⚠️ **绝对不要**把视频链接/地址/HTML 标签写进回复——"
                "视频由系统处理，你只用文字回话。"
            )
        else:
            vision_sys_prompt += (
                "请根据以上信息，用你自己的语气自然地吐槽或接梗。\n"
                "⚠️ 你这次要评论的是**下面 user 消息里那张【当前图】**；"
                "上下文里标了「更早的图」的都是之前发过的，别把两张图搞混。\n"
                "⚠️ 先看【当前这条消息】里他到底说了什么、这张图是在什么语境下发的"
                "（比如发的是二维码，那多半是在让别人扫，不是在问你图里是什么）；"
                "没看懂就别硬接。\n"
                "⚠️ **绝对不要**把图片的链接/地址/HTML 标签（<img ...>）写进回复——"
                "图片由系统处理，你只用文字回话。"
            )
        # 👇 打通「视觉 → 记忆」：让她自己决定要不要把「看到了什么」记下来
        vision_sys_prompt += (
            f"\n（如果这{_noun}让你有感觉、或者对方特意发给你看，"
            "记得按【记忆自标注协议】用 [[MEMO|...]] 把「你看到了什么」记下来，"
            "这样下次你就能想起来。）"
        )
        # 👇 看图也要用「这个群/这个人」的说话方式 + 接着更早的状态
        #    （以前只有纯文字分支注入，图片回复的语气是「另一套」，很不协调）
        try:
            _vg = str(getattr(TOOL_CTX.get("event"), "group_id", "") or "")
            _vexpr = expression.render(_vg, speaker_qq)
            if _vexpr:
                vision_sys_prompt += "\n\n" + _vexpr
            _vstate = conv_state.render(key)
            if _vstate:
                vision_sys_prompt += "\n\n" + _vstate
        except Exception as e:
            logger.debug(f"[WIRE] 视觉分支注入风格/状态失败: {e}")

        msgs.append({"role": "system", "content": vision_sys_prompt})
        # 视觉这条也带一张 brief 人物卡（被 @ / 私聊时）
        try:
            _vgid = str(getattr(TOOL_CTX.get("event"), "group_id", "") or "")
            _vaddressed = bool(TOOL_CTX.get("addressed", True))
            if speaker_qq and (_vaddressed or not _is_group):
                _vfocus = profiles.render_card(speaker_qq, _vgid, detail="brief", max_len=220)
                if _vfocus:
                    # brief 卡自带 ▍名字 标题，不再套第二层（模板去重）
                    msgs.append({"role": "system", "content": _vfocus})
        except Exception:
            pass
        # 动态规矩（消息号/暂存表情/@失败）放最后：前面全是可以吃缓存的前缀
        if _dyn_proto:
            msgs.append({"role": "system", "content": _dyn_proto})
        try:
            _vgid2 = str(getattr(TOOL_CTX.get("event"), "group_id", "") or "")
            _vpost = _post_rules(key, speaker_qq, _vgid2)
            if _vpost:
                msgs.append({"role": "system", "content": _vpost})
            _vsm = str((group_scene.get_summary(_vgid2) or {}).get("text") or "").strip()
            if _vsm:
                msgs.append({
                    "role": "system",
                    "content": f"【最近这段在聊什么】{_vsm}",
                })
        except Exception:
            pass
        # 👇 2026-11-23：真机出现过她把识图结果念出来（`【图片】一只…`、
        #    `【图片识别结果】…`）。这是最后一层 system（recency 最高），
        #    明确「描述只给自己看，直接回应内容」；输出层还有 strip_media_echo 兜底。
        msgs.append({"role": "system", "content": (
            f"⚠️【这是当前{_dem}】画面描述只给你自己看：不要复述画面、"
            "不要写「【图片】…」这种格式、不要报颜色/姿势/表情/背景细节。"
            "图里是题目、截图或文字信息时，直接回答里面的问题/意思，不要念题干；"
            "其他情况就直接回应图里的内容、情绪或梗。"
        )})
        msgs.append({"role": "user", "content":
                     f"【这是当前{_dem}，只评论它】\n"
                     f"（群友发了{'段视频' if _is_video else '张图片'}）\n"
                     + (_cur_text or "")})

    else:
        # ================= 正常的文字聊天 =================
        # ⚠️ 以前这里把 build_prompt 的「背景段」直接丢了（只有视觉分支用了 _bg_text），
        #    导致「这一条的人称」「群聊现场」「气氛」这些动态块根本没进模型。
        #    2026-11-19 补上：背景段单独作为一条 system，当前消息仍然留在 user。
        if _bg_text:
            msgs.append({"role": "system", "content": _bg_text})

        # 👇 算力机 Agent 降级为「图书馆」：只在需要久远记忆时才查。
        #    原来每轮都查，既慢又贵；而且它的结果被标成「最高优先级」，
        #    反而压过了本地 memory_v2 精心设计的情绪刻痕 / 遗忘曲线。
        #    新分工：30 天内 → 本地 memory_v2；30 天以前 → 图书馆。
        _max_age = tuning.f("LOCAL_MEMORY_DAYS")
        # 群号 / 是否在叫她 / 最近几条正文：给 lorebook 扫关键词、给 focus 卡定位用
        # ⚠️ 先取 _gid：下面 recall_hits 万一抛异常，后面 expression.render(_gid)
        #    会变成 NameError 被 except 吞掉 → 表达学习静默失效（很难发现）。
        _gid = str(getattr(TOOL_CTX.get("event"), "group_id", "") or "")
        # 先只查一次记忆，拿到「到底搜到几条」再决定要不要问图书馆。
        # ⚠️ 以前这里用 bool(packet["memory_index"]) 判断 —— 那个块里只要有一行
        #    「当前说话人」就非空，于是「本地没搜到 → 问图书馆」几乎永远走不到。
        # 2026-11-24：如果调用方已经跑过动态流水线（reply_pipeline），
        # 这里就不再重复召回/查图书馆 —— 流水线已经把记忆、图书馆、
        # 心情、档案都按「这句消息」取过了。
        _staged = str(staged_text or "").strip()
        if _staged:
            _hits = []
            _max_age = _max_age
        else:
            _hits = recall_hits(
                key, text, speaker_qq,
                limit=5, max_age_days=_max_age,
            )
        _addressed_flag = bool(TOOL_CTX.get("addressed", True))
        _scan_text = "\n".join(str(x) for x in (group_context or [])[-6:]) + "\n" + str(text or "")
        packet = build_packet(
            session_key=key,
            user_text=text,
            speaker_qq=speaker_qq,
            speaker_name=speaker_name,
            max_age_days=_max_age,
            hits=_hits,
            memory_limit=(0 if _staged else 5),
            gid=_gid,
            addressed=_addressed_flag,
            scan_text=_scan_text,
        )

        library_text = ""
        if _staged:
            logger.info("[LLM] 🧩 已用动态流水线取数 → 跳过静态图书馆")
        elif no_library:
            logger.info("[LLM] 📚 跳过图书馆（调用方指定 no_library，如自检）")
        elif _need_library(
                text, key,
                # ⚠️ 2026-11-24：`recent_about` 的兜底命中不算「本地搜到了」——
                #    否则每条消息都 local_hit=True，图书馆几乎永远不触发。
                len([h for h in _hits if not h.get("fallback")]) > 0):
            logger.info(
                f"[LLM] 📚 询问图书馆（算力机 Agent，久远记忆；"
                f"本地真命中 {len([h for h in _hits if not h.get('fallback')])} 条）…"
            )
            try:
                recall_data = await fetch_agent_recall(
                    text, speaker_qq=speaker_qq, recent_ctx=group_context
                )
                library_text = _recall_block(recall_data)
            except Exception as e:
                logger.warning(f"[LLM] 图书馆查询失败（忽略）: {type(e).__name__}: {e}")
            _last_library_at[key] = time.time()
            if library_text:
                # 复用同一份 hits，不再重跑 BM25 / 重写 index.json
                packet = build_packet(
                    session_key=key,
                    user_text=text,
                    speaker_qq=speaker_qq,
                    speaker_name=speaker_name,
                    library_text=library_text,
                    max_age_days=_max_age,
                    hits=_hits,
                    record=False,
                    gid=_gid,
                    addressed=_addressed_flag,
                    scan_text=_scan_text,
                )
        else:
            logger.info(f"[LLM] 📚 跳过图书馆（本地命中 {len(_hits)} 条，够用）")

        # 按块注入，每块都有明确标签，模型不会混淆层次。
        # ⚠️ 只注入「最小必要」：当前说话人一行 + 记忆索引一行 + 心情。
        #    档案内容、过往细节、记忆全文都改成按需拉（[[PROFILE]] / [[MEMORY]]），
        #    这样动态区从 ~600 字降到 ~150 字，注意力不被稀释。
        # ⚠️ 2026-11-23：`relation`（好感底色）以前**从来没进过 prompt** ——
        #    build_packet 产出了，但这个注入清单里漏了它。
        #    心情/好感也不再放在这个「早期背景区」：它们对语气影响最大，
        #    挪到历史之后、当前消息之前（depth 区），注意力更集中。
        for _key in ("yesterday", "lore", "profiles", "memory_index", "revisit",
                     "library", "quiet"):
            if packet.get(_key):
                msgs.append({"role": "system", "content": packet[_key]})

        if speaker_qq and not packet.get("profiles"):
            # 称呼优先（用户 2026-11-18）：档案里约定过的 call_name 优先，
            # 没有才用群名片/昵称。这里以前直接写 speaker_name（群名片），
            # 于是她回复时也照着群名片叫 —— 上下文和回复的称呼就分叉了。
            try:
                _call = profiles.call_of(speaker_qq, speaker_name, _gid)
            except Exception:
                _call = speaker_name
            _line = f"【当前说话人】{_call or '群友'}（QQ {speaker_qq}）"
            if _call and speaker_name and _call != speaker_name:
                _line += f"｜群名片是「{speaker_name}」，回复时用「{_call}」称呼他"
            msgs.append({"role": "system", "content": _line})

        if group_context:
            # ⚠️ 这里**不能**把最后一条标成「最新一句，优先接这句」：
            #    group_context 传进来的是 recent[:-1]，最后一条其实是**上一条历史**，
            #    不是当前要回的消息。标错了她就会一直去接旧话 —— 真机踩过。
            lines = list(group_context)
            marked = [f"{i + 1}. {ln}" for i, ln in enumerate(lines)]
            ctx_block = "\n".join(marked)
            if str(key).startswith("group"):
                _hist_note = (
                    f"【历史对话】共 {len(lines)} 条（由旧到新，**全是已经发生过的**，"
                    f"只作背景，不要复述、不要顺着它们继续聊）：\n"
                    + ctx_block
                    + "\n\n注意四点：\n"
                      "· **带 ⭐ 的是在叫你**（@ 你 / 喊你名字）—— 这几条是重点，"
                      "先看清它们在说什么、在问什么；**如果有好几条 ⭐ 你还没答，"
                      "这次要一起照顾到**（可以合成一条，用 `|||` / `<reply:消息号>`）。\n"
                      "· 「这是你之前的回复：」开头的是**你自己刚说过的话**，"
                      "别重复、也别当成别人说的。\n"
                      f"· 上面这 {len(lines)} 条**都不是**你要回的内容。\n"
                      "· 你真正要回的是紧接着下面那条【当前消息】。\n"
                      "· 要是这几十条还接不上（在说「那个」「刚才那个」、话题断了、"
                      "或者⭐那条明显缺前情）→ 先 `[[[HISTORY|20]]]` 往上翻 20 条，"
                      "必要时再 `[[[LATER|8]]]` 等 8 秒看最新，"
                      "然后**引用那一句**（`<reply:消息号>`）一次总结说完，别再补第二条。\n"
                    + (f"⚠️ 上面只有最近 {len(lines)} 条，更早的已被折叠："
                       "如果当前这条接不上，先 `[[[HISTORY|20]]]` 翻上文，别硬答。"
                       if len(lines) >= 8 else "")
                )
            else:
                # 私聊：历史是「和同一个人的连续对话」，当前消息常常是上一句的
                # 补充/追问/纠正。这里不能像群聊那样叫它「别顺着旧话聊」——
                # 那样会把「是我们的记忆和档案工具了」这种缺主语的补充当孤立新话题。
                _hist_note = (
                    f"【私聊前文】共 {len(lines)} 条（由旧到新，都是你俩刚才聊的）：\n"
                    + ctx_block
                    + "\n\n注意：\n"
                      "· 这是私聊，对方连续发的几句经常是**同一件事的补充/追问/纠正**，"
                      "别把当前消息当成孤立的新话题。\n"
                      "· 当前消息缺主语、提到「它/那个/这个/书/工具/那本」这类词时，"
                      "先往上 1~2 条找指代，串起来理解再回。\n"
                      "· 「这是你之前的回复：」开头的是你自己刚说过的话，别重复。\n"
                      "· 真接不上（话题确实断了）→ 用 `[[[HISTORY|20]]]` 往上翻 20 条再回。"
                )
            # 👇 更早的对话压成 5 类状态（topics/background/progress/pending/agreements），
            #    放在历史块**前面**：越靠前越像"背景"，不会抢当前消息的注意力。
            #    和原有设计不冲突：最近 N 条原文照旧、词条照旧、每日总结照旧，这只是一层补充。
            _state_block = conv_state.render(key)
            if _state_block:
                msgs.append({"role": "system", "content": _state_block})
            msgs.append({"role": "system", "content": _hist_note})
            # 攒够条件就在后台把这轮对话并进状态（失败静默，不影响回复）
            conv_state.maybe_update(key, lines)

        # 👇 depth 注入（对齐 ST 的 at-depth / Author's Note）：
        #    这一条在跟谁说话 → 人物 brief 卡；命中关键词 → 群设定词条。
        #    放在历史之后、当前消息之前，离当前这句最近，注意力最集中。
        _meta = packet.get("_meta") or {}
        _focus = str(_meta.get("focus_card") or "").strip()
        if _focus:
            # ⚠️ 不要再套一层「▍这一条在跟你说话的人」：brief 卡自己就带
            #    `▍名字（QQ…）` 标题，套两层会出现两个同级标题（用户 2026-11-22：
            #    「模板有很多重复的」）。
            msgs.append({"role": "system", "content": _focus})
        # 👇 表达学习：这个群 / 这个人怎么说话（学语气，不是知识）。
        #    放在 depth 区（离当前消息最近），但排在人物卡之后 —— 先认人，再调语气。
        try:
            _expr = expression.render(_gid, speaker_qq)
            if _expr:
                msgs.append({"role": "system", "content": _expr})
        except Exception:
            pass
        # 🌙 白月光模式：跟这个群她最吃的那个人说话时，多用他的句式/口头禅。
        #    排在通用表达学习之后（更具体、recency 更高）。
        try:
            from . import white_moon
            _moon_tq = "" if _staged else str(moon_target_qq or "").strip()
            _moon_tn = ""
            if _moon_tq:
                try:
                    _moon_tn = profiles.call_of(_moon_tq, "", _gid) or _moon_tq
                except Exception:
                    _moon_tn = _moon_tq
            _wm = white_moon.render(_gid, _moon_tq, _moon_tn, speaker_name)
            if _wm:
                msgs.append({"role": "system", "content": _wm})
        except Exception as _e:
            logger.debug(f"[MOON] 注入失败: {type(_e).__name__}: {_e}")
        _lore_post = [str(x).strip() for x in (_meta.get("lore_post") or []) if str(x).strip()]
        if _lore_post:
            msgs.append({
                "role": "system",
                "content": "▍这条相关的设定（只作参考，别主动复述）\n" + "\n".join(_lore_post),
            })

        if _dyn_proto:
            msgs.append({"role": "system", "content": _dyn_proto})

        # 👇 2026-11-24 动态流水线：它按「这句消息」取的心情/档案/好感/记忆/图书馆/
        #    已回标记/白月光，全在这一个块里，放在最靠后的位置（recency 最高）。
        #    有它就不再注入静态的 relation / mood（避免同一件事说两遍）。
        if _staged:
            msgs.append({"role": "system", "content": _staged})
        else:
            # 👇 心情 / 好感放到 depth 区最靠后的位置（2026-11-23 用户：
            #    「心情和好感并没有关注太多」）。relation = 长期底色，mood = 此刻状态；
            #    mood 放最后，紧挨 post_rules 和当前消息，recency 最高。
            for _key in ("relation", "mood"):
                if packet.get(_key):
                    msgs.append({"role": "system", "content": packet[_key]})

        # 👇 post-history：最后一句硬提醒（recency 权重最高，比写在开头有效）
        _post = _post_rules(key, speaker_qq, _gid)
        if _post:
            msgs.append({"role": "system", "content": _post})

        # 👇 最后一行：审核 LLM 给的「这段在聊什么」
        #    用户 2026-11-19：像 QQ 元宝总结一样，放在角色扮演 LLM 的最末尾。
        try:
            _sm = str((group_scene.get_summary(_gid) or {}).get("text") or "").strip()
            if _sm:
                msgs.append({
                    "role": "system",
                    "content": f"【最近这段在聊什么】{_sm}",
                })
        except Exception:
            pass

        msgs.append({"role": "user", "content": text})

    # 👇 留一份「最近一次真正发给 DeepSeek 的完整输入」，
    #    给 `/看模板` 用。直接缓存真实 msgs，比在别处重新拼一遍更准（不会漏块）。
    try:
        _LAST_MSGS[str(key)] = list(msgs)
        while len(_LAST_MSGS) > 20:
            _LAST_MSGS.pop(next(iter(_LAST_MSGS)))
    except Exception:
        pass

    # ================= 统一交给角色扮演模型 =================
    # 2026-11-23 用户：角色扮演走 GLM-4-Flash（ROLEPLAY_*），
    # 三方判读/审核继续走 DeepSeek chat（XAI_*）。
    logger.info(f"[LLM] ☁️ 使用角色扮演模型 {ROLEPLAY_MODEL} 生成最终回复...")
    headers = {"Authorization": f"Bearer {ROLEPLAY_API_KEY}",
               "Content-Type": "application/json"}
    kwargs = {"timeout": 60}
    if PROXY: kwargs["proxy"] = PROXY

    # 工具按会话类型挂：默认只有私聊有；napcat_help/napcat_call 群聊也挂
    # （群里禁言、改名片、戳一戳本来就是群里的事，不挂等于她做不到）
    _tools = [] if no_tools else tools_for(key)
    allow_tools = bool(_tools)
    reply, reason = "", ""

    # 👇 接线快照：`/测函数 链路` 直接看这一份。
    #    用户 2026-11-19：「说不定很多参数传值没通」—— 与其猜，不如把
    #    「这次到底塞进了哪些块 / 挂了哪些工具 / 有没有强制沉默」落盘。
    try:
        _sys_all = "\n".join(str(m.get("content") or "") for m in msgs
                             if m.get("role") == "system")
        try:
            from . import scene_auditor as _sa
            _ver = str(getattr(_sa, "VERSION", "") or "")
        except Exception:
            _ver = ""
        _wire = {
            "ts": time.time(),
            "key": str(key),
            "persona": persona_name,
            "media": _kind or "text",
            "ctx_n": len(group_context or []),
            "sys_chars": len(_sys_all),
            "user_chars": len(str(text or "")),
            # ▍标题就是「哪些块进来了」——空块不会渲染，所以有标题=真的有内容
            "blocks": re.findall(r"▍([^\n]{1,28})", _sys_all),
            "tools": [(s.get("function") or {}).get("name") for s in _tools],
            "forced_silent": bool(_force_silent),
            "off": bool(_off),
            "drift": attention.DRIFT_LEVEL,
            # 版本号直接读 scene_auditor.VERSION（以前写死成 t，改了版本它不跟着变）
            "ver": _ver,
        }
        # ⚠️ 按会话存：以前是「全局一份，后写的覆盖前面的」，
        #    所以刚在私聊说完话、群里又来一句，`/测函数 链路` 看到的就变成群里的了。
        _pf = Path("data") / "pipeline_last.json"
        _pf.parent.mkdir(parents=True, exist_ok=True)
        try:
            _all = json.loads(_pf.read_text(encoding="utf-8") or "{}")
            if not isinstance(_all, dict) or "ts" in _all:   # 兼容旧的单条格式
                _all = {}
        except Exception:
            _all = {}
        _all[str(key)] = _wire
        if len(_all) > 12:
            for _old in sorted(_all, key=lambda x: float((_all.get(x) or {}).get("ts") or 0))[:-12]:
                _all.pop(_old, None)
        _pf.write_text(json.dumps(_all, ensure_ascii=False, indent=1),
                       encoding="utf-8")
    except Exception as e:
        logger.debug(f"[WIRE] 接线快照写入失败: {e}")

    # 安静状态下强制走「不回复函数」。工具没挂上（离线自检 / no_tools）就直接不回，
    # 别让模型在没有工具的会话里吐一段正文出来。
    if _force_silent:
        _has_silent_tool = any(
            ((s.get("function") or {}).get("name") == "stay_silent") for s in _tools)
        if not _has_silent_tool:
            logger.info("[SWITCH] 安静中但没有 stay_silent 工具 → 直接不回")
            return "(不想回复)", False

    try:
        async with httpx.AsyncClient(**kwargs) as client:
            for _turn in range(MAX_TOOL_TURNS):
                payload = {
                    "model": ROLEPLAY_MODEL,
                    "messages": msgs,
                    "temperature": 1.0,
                    "max_tokens": 800,
                }
                if allow_tools:
                    payload["tools"] = _tools
                    # 强制「不回复」：不是靠提示词祈祷，而是 API 层锁死只有一个选项
                    payload["tool_choice"] = (
                        {"type": "function", "function": {"name": "stay_silent"}}
                        if _force_silent else "auto"
                    )

                r = await client.post(f"{ROLEPLAY_BASE_URL}/chat/completions",
                                      headers=headers, json=payload)
                r.raise_for_status()
                _j = r.json()
                msg = _j["choices"][0]["message"]
                # 👇 前缀缓存命中情况。
                #    角色卡 + 规矩固定放在最前面（约 1.2 万字），一字不改就是为了吃这个：
                #    DeepSeek 对完全相同的前缀走磁盘缓存，命中部分按 1/10 计价。
                #    所以「人设卡每天只发一次」的正确解法不是"少发"，而是"发一样的"。
                _u = _j.get("usage") or {}
                _hit = int(_u.get("prompt_cache_hit_tokens") or 0)
                _miss = int(_u.get("prompt_cache_miss_tokens") or 0)
                if _hit or _miss:
                    _tot = _hit + _miss
                    logger.info(
                        f"[LLM] 💰 prompt {_tot} tokens"
                        f"｜缓存命中 {_hit}（{_hit / max(1, _tot) * 100:.0f}%，按 1/10 计价）"
                        f"｜未命中 {_miss}"
                    )
                # 👇 本机按天记账（供应商没有公开的用量接口，只能自己数）。
                #    她问「今天用了多少」时由 api_usage 工具读这个。
                if _u:
                    try:
                        # api_usage 只记 DeepSeek 余额；角色扮演换成 GLM 后不再混进去
                        if "deepseek" in ROLEPLAY_BASE_URL.lower():
                            from . import api_usage as _au
                            _au.note_usage(
                                prompt=int(_u.get("prompt_tokens") or 0) or (_hit + _miss),
                                completion=int(_u.get("completion_tokens") or 0),
                                cache_hit=_hit, model=ROLEPLAY_MODEL)
                        try:
                            from . import llm_usage as _lu
                            _lu.record_from_response(
                                _j, purpose=("tool_loop" if _turn else "chat"),
                                provider=("deepseek" if "deepseek" in ROLEPLAY_BASE_URL.lower()
                                          else "glm"),
                                model=ROLEPLAY_MODEL,
                                session=key, turn_id=str(TOOL_CTX.get("turn_id") or ""))
                        except Exception:
                            pass
                    except Exception as _e:
                        logger.debug(f"[USAGE] 记账跳过: {type(_e).__name__}: {_e}")

                calls = msg.get("tool_calls") or []
                if not calls:
                    reply = msg.get("content") or ""
                    # 👇 推理模型（deepseek-reasoner 等）把思考放在 reasoning_content，
                    #    普通 deepseek-chat 没有这个字段。有就存下来给复盘用。
                    reason = (msg.get("reasoning_content")
                              or msg.get("reasoning") or "").strip()
                    break

                # 模型要调工具 → 执行 + 回填，再让它继续生成
                logger.info(f"[TOOL] 第 {_turn + 1} 轮，模型请求 {len(calls)} 个工具")
                msgs.append(msg)
                msgs.extend(await _run_tool_calls(calls))
                # 👇 她调了 stay_silent（自己不想说，或安静状态被强制）→ 立刻收工。
                #    不能让它继续生成正文，否则「不想说」等于白说。
                if TOOL_CTX.get("silent"):
                    reply = "(不想回复)"
                    logger.info(
                        f"[SILENT] 这一轮不说话："
                        f"{TOOL_CTX.get('silent_reason') or ('安静状态' if _off else '—')}")
                    break
            else:
                # 跑满轮数还在调工具 → 兜底，别让她一声不吭
                reply = reply or "（想了一会儿，还是不知道说啥好）"

            # 👇 多轮思考：她可以**连续**调工具（翻历史 / 查档案 / 等新消息），
            #    想清楚再开口。
            #
            #    用户 2026-11-11 两次的说法：
            #      ①「回复是最不着急的…定个 60 秒上限吧」
            #      ②「如果她拿不准就深度思考下 60s 那个，平常可以回复快些」
            #    → 所以不是无条件给 60 秒：**默认快速档**（几秒内开口），
            #      她自己在回复里写 [[DEEP|理由]] 才升级到 60 秒深度档。
            #      拿不准的时候她自己最清楚，让她自己申请，比我们猜准。
            _deep = False
            _deadline = time.time() + THINK_BUDGET_FAST
            _max_rounds = MAX_REQ_ROUNDS_FAST
            _rounds = 0
            while True:
                # 她申请深度思考 → 预算和轮数都放开
                if not _deep and _DEEP_RE.search(reply or ""):
                    _deep = True
                    _max_rounds = MAX_REQ_ROUNDS
                    _deadline = max(_deadline, time.time() + THINK_BUDGET_DEEP)
                    reply = _DEEP_RE.sub("", reply).strip()
                    logger.info(
                        f"[LLM] 🧠 她申请了深度思考（{THINK_BUDGET_DEEP:.0f}s / "
                        f"最多 {_max_rounds} 轮）"
                    )
                if not _REQ_RE.search(reply or ""):
                    break
                if _rounds >= _max_rounds:
                    # 轮数用完但回复里还有标记 → 必须摘干净，
                    # 否则 [[SEARCH|xx]] 这种会被原样发给群友（以前靠 sub 全删掩盖了这个问题）
                    logger.info(f"[LLM] 补资料轮数用完（{_max_rounds}），丢弃剩余标记")
                    reply = _REQ_RE.sub("", reply).strip()
                    break
                _left = _deadline - time.time()
                if _left <= 1.5:
                    logger.info(
                        f"[LLM] ⏰ 思考预算用完（{'深度' if _deep else '快速'}档，"
                        f"已补 {_rounds} 轮），直接开口"
                    )
                    reply = _REQ_RE.sub("", reply).strip()
                    break
                _rounds += 1
                _before = reply
                try:
                    reply = await _fulfill_requests(reply, msgs, client, headers)
                except Exception as e:
                    logger.warning(f"[REQ] 补资料失败: {type(e).__name__}: {e}")
                    reply = _REQ_RE.sub("", reply).strip()
                    break
                if reply == _before:
                    break
                logger.info(
                    f"[LLM] 🔁 工具第 {_rounds} 轮完成，剩 {_deadline - time.time():.0f}s"
                )
            # DEEP 标记绝不能漏给用户看（哪怕她只是随口写了一个）
            if reply:
                reply = _DEEP_RE.sub("", reply).strip()
            if _rounds:
                logger.info(f"[LLM] 🧠 这次一共补了 {_rounds} 轮资料才开口")

            if reason:
                logger.info(f"[LLM] 🧠 思维链 {len(reason)} 字：{reason[:60].replace(chr(10), ' ')}…")
                _save_thought(key, text, reason)
    except Exception as e:
        # 「服务器繁忙」四个字一秒出戏（用户 2026-11-17 点名）。
        # 按故障类型给几句角色化兜底：不假装没事，但说话像她。
        _status = getattr(getattr(e, "response", None), "status_code", 0)
        if isinstance(e, httpx.TimeoutException):
            _kind = "timeout"
        elif _status == 429:
            _kind = "rate"
        else:
            _kind = "error"
        if _force_silent:
            # 安静状态本来就不该出声；兜底话术反而会「破功」
            reply = "(不想回复)"
            logger.info(f"[SWITCH] 安静状态请求失败（{_kind}）→ 仍然不回")
        else:
            reply = random.choice(FALLBACK_LINES[_kind])
            logger.error(f"[LLM] ❌ 云端请求失败（{_kind}）: {e} → 兜底：{reply}")

    add_memory(key, "user", text)
    add_memory(key, "assistant", reply)

    # 落一份完整输入/输出，给 scripts/trace_reply.py 做「API 视角 vs 上帝视角」对比
    _trace_dump(key, msgs, text, reply, reason,
                persona=persona_name,
                group_context_n=(len(group_context) if group_context else 0),
                speaker_qq=speaker_qq, speaker_name=speaker_name)

    return reply, False
