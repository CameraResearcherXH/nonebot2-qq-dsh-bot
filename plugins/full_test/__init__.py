# plugins/full_test/__init__.py
"""
/全量测试 —— 一条命令把「所有功能」串起来跑一遍，当场看出哪条链路坏了。

和现有三个测试入口的分工（不要重复造轮子，也不要互相替代）：

    scripts/selftest_all.py   离线单元测试（docker exec 里跑，不碰 QQ）
    scripts/test_regressions.py 纯函数回归（这次修复的 89 项断言）
    /自检                      运行时浅体检（NapCat / 模型 / 配置，3~6 秒）
    /全量测试（本文件）          **端到端串通**：真调模型、真写记忆、真解析回复、
                               真读索引/档案/日志/磁盘，最后给一份带「怎么修」的报告

用法（仅主人）：
    /全量测试            → 标准档（约 8~20 秒，含 1 次很短的模型调用）
    /全量测试 全          → 深检（额外测本地 Qwen / GLM / 算力机图书馆）
    /全量测试 记忆        → 只跑名字里带「记忆」的分组
    /全量测试 详细        → 私聊里把完整报告分段发出来
    /全量测试 发送        → 额外真发一条测试消息（只在私聊生效，避免群里刷屏）
    /全量测试 报告        → 重发上一次的报告

设计原则：
  · 只读为主。唯一会写数据的是「记忆闭环 / 心情闭环 / 模型调用」，
    都用独立 session key（test_full）+ 唯一 token，跑完立刻 mark_outdated 清理。
  · 不往群里发测试消息（除非显式 `发送` 且是私聊）。
  · 任何一项崩了都不影响后面 —— 每项独立超时、独立 try。
  · 报告落盘 data/full_test/report_*.txt，出问题直接发给开发者。
"""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import os
import re
import shutil
import time
from pathlib import Path

from nonebot import on_command, logger
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent, GroupMessageEvent
from nonebot.params import CommandArg

from services.access import is_owner
from services import llm as L

OK, WARN, BAD, SKIP = "ok", "warn", "bad", "skip"
ICON = {OK: "✅", WARN: "⚠️", BAD: "❌", SKIP: "⏭"}
REPORT_DIR = Path("data/full_test")
TEST_KEY = "test_full"

full_test = on_command("全量测试", aliases={"全测", "alltest"}, priority=3, block=True)


def _r(status, text, fix="", detail=""):
    return (status, str(text)[:200], str(fix)[:160], str(detail)[:2000])


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ============================================================
# 🧩 环境与配置
# ============================================================
async def t_env(bot, ev, ctx):
    need = {"XAI_API_KEY": "模型调用", "XAI_BASE_URL": "模型地址",
            "ALLOWED_GROUPS": "群白名单"}
    miss = [k for k in need if not os.getenv(k)]
    if miss:
        return _r(BAD, f"缺关键配置：{'、'.join(miss)}", "补 .env 后重启容器")

    # 主人判定走 services.access（它优先读 nonebot 的 SUPERUSERS，
    # 没配 SUPERUSERS 才回落 OWNER_QQ）。直接查 os.getenv("OWNER_QQ") 会误报 ——
    # 2026-11-17 线上就是 SUPERUSERS 配了、OWNER_QQ 没配，被这条检查误判成警告。
    from services.access import OWNER_QQ, ADMIN_QQS
    if not OWNER_QQ:
        return _r(WARN, "没有主人（SUPERUSERS / OWNER_QQ 都为空）",
                  "在 .env 配 SUPERUSERS=你的QQ")
    return _r(OK, f"{len(need)} 个关键变量齐全，主人 {OWNER_QQ}"
                  + (f"，管理员 {len(ADMIN_QQS)} 人" if ADMIN_QQS else ""))


async def t_persona(bot, ev, ctx):
    from services.persona import persona_for, list_personas, _EMERGENCY_PROMPT
    name, text = persona_for("group_0")
    if text == _EMERGENCY_PROMPT or len(text) < 500:
        return _r(BAD, f"人格没加载（{name}，{len(text)} 字）",
                  "检查 services/Personality/*.txt 是否部署")
    ctx["details"].append(f"人格 {name}：{len(text)} 字；可用：{list_personas()}")
    return _r(OK, f"{name}（{len(text)} 字）")


async def t_dirs(bot, ev, ctx):
    bad = []
    for d in ("data", "data/memory", "data/profiles", "data/llm_trace",
              "data/self_tools/tools"):
        p = Path(d)
        try:
            p.mkdir(parents=True, exist_ok=True)
            f = p / ".write_test"
            f.write_text("ok", encoding="utf-8")
            f.unlink()
        except Exception as e:
            bad.append(f"{d}: {type(e).__name__}")
    if bad:
        return _r(BAD, "目录不可写：" + "；".join(bad), "检查挂载与权限")
    return _r(OK, "data 下 5 个关键目录可写")


async def t_disk(bot, ev, ctx):
    try:
        u = shutil.disk_usage("data")
        free_gb = u.free / 1024 ** 3
    except Exception as e:
        return _r(WARN, f"读不到磁盘：{type(e).__name__}", "忽略或检查挂载")
    if free_gb < 1:
        return _r(BAD, f"剩余 {free_gb:.2f} GB", "清 data/llm_trace、data/temp_*")
    if free_gb < 5:
        return _r(WARN, f"剩余 {free_gb:.2f} GB", "建议清理缓存/日志")
    return _r(OK, f"剩余 {free_gb:.1f} GB")


async def t_time(bot, ev, ctx):
    tz = os.getenv("TZ", "")
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    if tz and tz != "Asia/Shanghai":
        return _r(WARN, f"TZ={tz}，现在 {now}", "国内群建议 Asia/Shanghai")
    return _r(OK, f"{now}（TZ={tz or '未设置，用系统默认'}）")


# ============================================================
# 🧠 模型链路
# ============================================================
async def t_deepseek(bot, ev, ctx):
    import httpx
    if not L.XAI_API_KEY:
        return _r(BAD, "没有 XAI_API_KEY", "补 .env")
    payload = {
        "model": L.XAI_MODEL,
        "messages": [{"role": "user", "content": "只回复两个字：正常"}],
        "temperature": 0.0, "max_tokens": 8,
    }
    kwargs = {"timeout": 30}
    if L.PROXY:
        kwargs["proxy"] = L.PROXY
    t0 = time.time()
    async with httpx.AsyncClient(**kwargs) as c:
        r = await c.post(f"{L.XAI_BASE_URL}/chat/completions",
                         headers={"Authorization": f"Bearer {L.XAI_API_KEY}",
                                  "Content-Type": "application/json"},
                         json=payload)
    if r.status_code != 200:
        return _r(BAD, f"HTTP {r.status_code}: {r.text[:80]}", "检查 key / 余额 / 代理")
    j = r.json()
    txt = (j["choices"][0]["message"].get("content") or "").strip()
    u = j.get("usage") or {}
    if not txt:
        return _r(WARN, "返回空内容", "模型可能被限流")
    ctx["details"].append(
        f"DeepSeek {L.XAI_MODEL} → {txt[:20]!r}，"
        f"{time.time() - t0:.2f}s，tokens={u.get('total_tokens')}"
    )
    return _r(OK, f"{L.XAI_MODEL} 正常（{time.time() - t0:.1f}s）")


async def t_local_qwen(bot, ev, ctx):
    sample = "这是一段用于测试本地压缩模型的文本。" * 12
    try:
        out = await asyncio.wait_for(L.summarize_by_local_qwen(sample), 25)
    except asyncio.TimeoutError:
        return _r(WARN, "本地 Qwen 超时", "检查 11434 隧道")
    if not out:
        return _r(WARN, "返回空", "检查 LOCAL_LLM_URL / 模型名")
    if len(out) >= len(sample):
        return _r(WARN, "未压缩（降级为原文）",
                  f"本地 Qwen 不可用：{L.LOCAL_LLM_URL}")
    ctx["details"].append(f"本地压缩 {len(sample)} → {len(out)} 字")
    return _r(OK, f"{len(sample)} → {len(out)} 字")


async def t_glm(bot, ev, ctx):
    from services import glm
    if not glm.available():
        return _r(SKIP, "没配 GLM key（可选）", "要事前参考就配 GLM_API_KEY")
    if not ctx["full"]:
        return _r(OK, "已配置（深检才真调）")
    # ⚠️ max_tokens 别给太小：GLM-5.x 在 token 很少时会吐一段英文前缀
    #    （线上实测 8 tokens → "The user has sent a "），看着像坏了其实只是截断。
    out = await asyncio.wait_for(
        glm.chat([{"role": "user", "content": "用一句中文回答：你现在正常吗？"}],
                 max_tokens=48, timeout=20), 25
    )
    out = str(out or "").strip()
    if not out:
        return _r(WARN, "返回空（thinking 吃 token？）", "调大 GLM_MAX_TOKENS 或关 GLM_THINKING")
    if not re.search(r"[\u4e00-\u9fff]", out):
        return _r(WARN, f"返回不像中文：{out[:30]!r}", "检查 GLM_MODEL 是否是对话模型")
    ctx["details"].append(f"GLM {glm._working_model or glm.GLM_MODEL} → {out[:60]!r}")
    return _r(OK, f"GLM 正常：{out[:20]}")


async def t_prompt(bot, ev, ctx):
    from services.prompt_builder import build_packet
    pk = build_packet(session_key=f"group_{getattr(ev, 'group_id', 0) or 1}",
                      user_text="全量测试：检查上下文包",
                      speaker_qq=str(ev.user_id),
                      speaker_name="测试")
    blocks = [k for k, v in pk.items() if k != "_meta" and isinstance(v, str) and v]
    total = sum(len(v) for k, v in pk.items() if k != "_meta")
    meta = pk.get("_meta") or {}
    ctx["details"].append(f"上下文块：{blocks}，共 {total} 字，{meta}")
    if "memory_index" not in pk and meta.get("memory_hits", 0) > 0:
        return _r(BAD, "有命中但记忆目录没进包", "检查 BLOCK_BUDGET 是否包含 memory_index")
    if total > 4000:
        return _r(WARN, f"上下文 {total} 字偏长", "调小 PROMPT_BUDGET")
    return _r(OK, f"{len(blocks)} 块 / {total} 字 / 命中 {meta.get('memory_hits', 0)} 条")


# ============================================================
# 💬 对话主链路（干跑 + 一次真调用）
# ============================================================
async def t_parse(bot, ev, ctx):
    from services.aura_logic import parse_reply
    sample = "哈哈这图笑死[[MEMO|emotion:happy|text:测试记忆]]|||<sticker:开心>"
    p = parse_reply(sample)
    kinds = [i["kind"] for i in p.get("items") or []]
    if not p.get("memos"):
        return _r(BAD, "MEMO 没被解析出来", "检查 parse_memos 正则")
    if "sticker" not in kinds:
        return _r(BAD, f"表情没被解析（items={kinds}）", "检查 <sticker:..> 正则")
    if "[[" in json.dumps(p, ensure_ascii=False):
        return _r(WARN, "解析结果里还残留 [[", "兜底清理可能失效")
    return _r(OK, f"items={kinds}，memo={len(p['memos'])}")


async def t_llm_call(bot, ev, ctx):
    t0 = time.time()
    try:
        reply, _ = await asyncio.wait_for(
            L.call_llm(TEST_KEY, "（系统自检，不用查资料）只回复四个字：链路正常",
                       group_context=[], speaker_qq=str(ev.user_id),
                       speaker_name="测试", no_tools=True, no_library=True),
            90,
        )
    except asyncio.TimeoutError:
        return _r(BAD, "call_llm 超时 90s", "看 docker logs 的 [LLM] 行")
    cost = time.time() - t0
    _fallbacks = {x for v in getattr(L, "FALLBACK_LINES", {}).values() for x in v}
    if not reply or reply.strip() in _fallbacks:
        return _r(BAD, f"回复异常：{reply!r}", "看 [LLM] ❌ 云端请求失败")
    if re.search(r"\[\[|<sticker:|<reply:", reply):
        return _r(WARN, f"回复里残留标记：{reply[:40]!r}", "parse_reply 兜底没接上")
    ctx["details"].append(f"call_llm → {reply[:60]!r}（{cost:.1f}s，{len(reply)} 字）")
    return _r(OK, f"{cost:.1f}s，{len(reply)} 字：{reply[:20]}")


async def t_memory_roundtrip(bot, ev, ctx):
    from services import memory_v2 as MV
    token = f"全量测试{int(time.time())}"
    mid = MV.remember(text=f"{token}：她确认记忆写入与召回是通的",
                      kind="fact", about_qq=str(ev.user_id), importance=0.3)
    hits = MV.recall(token, limit=5, speaker_qq=str(ev.user_id))
    found = any(h["id"] == mid for h in hits)
    # 立刻标过期，别污染真实记忆
    try:
        MV.mark_outdated(mid)
    except Exception:
        pass
    if not found:
        return _r(BAD, "写入后召回不到", "检查 tokenize / BM25 / index 写入")
    ctx["details"].append(f"记忆闭环：写入 {mid} → 召回 {len(hits)} 条（已标过期）")
    return _r(OK, "写入→召回→清理 全通")


async def t_mood_roundtrip(bot, ev, ctx):
    from services import mood
    key = "full_test_mood"
    v0 = mood.get_mood(key)["value"]
    mood.bump_mood(6, session_key=key)
    v1 = mood.get_mood(key)["value"]
    mood.bump_mood(-6, session_key=key)
    v2 = mood.get_mood(key)["value"]
    if v1 - v0 < 4:
        return _r(BAD, f"加 6 只涨了 {v1 - v0:.1f}", "检查 bump_mood 分层逻辑")
    if abs(v2 - v0) > 3:
        return _r(WARN, f"回滚后 {v2:.1f}（原 {v0:.1f}）", "可能被并发消息影响")
    ctx["details"].append(f"心情闭环：{v0:.1f} → {v1:.1f} → {v2:.1f}（独立 session）")
    return _r(OK, f"{v0:.1f} → {v1:.1f} → 回滚")


async def t_history(bot, ev, ctx):
    lines = L._db_history(TEST_KEY, "测试", turns=4)
    if not lines:
        return _r(WARN, "刚才的模型调用没进短期历史", "检查 add_memory / memory.db 权限")
    return _r(OK, f"读到 {len(lines)} 条（刚才那轮在里面）")


async def t_prompt_build(bot, ev, ctx):
    from services.prompt_builder import build_prompt
    from services.llm import _CUR_SEP      # 分隔线定义在 llm.py，不在 prompt_builder
    out = build_prompt(session_key=TEST_KEY, user_text="这是当前消息",
                       speaker_qq=str(ev.user_id), speaker_name="测试",
                       persona_text="")
    if _CUR_SEP not in out:
        return _r(BAD, "没有「当前消息」分隔线", "build_prompt 被改坏了")
    if not out.rstrip().endswith("这是当前消息"):
        return _r(BAD, "当前消息不在最末尾", "注意力会摊平")
    return _r(OK, f"{len(out)} 字，当前消息在末尾")


# ============================================================
# 📮 QQ / NapCat
# ============================================================
async def t_bot(bot, ev, ctx):
    info = await bot.call_api("get_login_info")
    uid = str((info or {}).get("user_id") or "")
    nick = str((info or {}).get("nickname") or "")
    if not uid:
        return _r(BAD, "get_login_info 返回空", "NapCat 可能掉线")
    return _r(OK, f"{nick}（{uid}）")


async def t_group(bot, ev, ctx):
    if not isinstance(ev, GroupMessageEvent):
        return _r(SKIP, "当前是私聊", "")
    from services import group_roster
    gid = str(ev.group_id)
    members = await group_roster.members_of(bot, gid)
    from plugins.chat import ALLOWED_GROUPS
    if gid not in ALLOWED_GROUPS:
        return _r(BAD, f"群 {gid} 不在白名单", "ALLOWED_GROUPS 加这个群")
    if not members:
        return _r(WARN, "拉不到群成员", "检查 NapCat get_group_member_list 权限")
    return _r(OK, f"群 {gid}，成员 {len(members)} 人")


async def t_napcat_tools(bot, ev, ctx):
    from services import napcat_tools as NT
    if not NT.ENABLED:
        return _r(SKIP, "NAPCAT_TOOLS 关着", "要操作 QQ 就打开")
    menu = NT.menu_hint(isinstance(ev, GroupMessageEvent))
    help1 = NT.help_text("set_group_ban")
    if not menu or not help1:
        return _r(BAD, "菜单/帮助为空", "napcat_tools 的表坏了")
    return _r(OK, f"菜单 {len(menu)} 字，帮助 {len(help1)} 字")


async def t_send_dry(bot, ev, ctx):
    from services.aura_logic import parse_reply, expand_mentions
    p = parse_reply("[[AT|测试]] 你好 ||| <sticker:开心>")
    members = [{"user_id": 123456, "card": "测试", "nickname": "test"}]
    segs = []
    for it in p["items"]:
        if it["kind"] == "text":
            s, missed = expand_mentions(
                [{"type": "text", "data": {"text": it["value"]}}], members)
            segs.extend(s)
    types = [s["type"] for s in segs]
    if "at" not in types:
        return _r(WARN, f"@ 没转成真 at 段（{types}）", "检查 expand_mentions / 名册")
    return _r(OK, f"段落 {types}（未真发）")


async def t_send_real(bot, ev, ctx):
    if not ctx["send"]:
        return _r(SKIP, "没加「发送」参数", "要真发用 /全量测试 发送（仅私聊）")
    if isinstance(ev, GroupMessageEvent):
        return _r(SKIP, "群里不真发，避免刷屏", "私聊里跑")
    t0 = time.time()
    await bot.send(ev, "🧪 全量测试：发送链路正常")
    return _r(OK, f"已发到私聊（{time.time() - t0:.2f}s）")


# ============================================================
# 🎨 媒体与玩法
# ============================================================
async def t_sticker(bot, ev, ctx):
    from services.sticker_library import sticker_lib
    s = sticker_lib.stats()
    h = sticker_lib.health()
    if not s.get("total"):
        return _r(WARN, "表情库是空的", "发几张表情让她收")
    pick = sticker_lib.pick_by_tag("开心")
    if not pick:
        return _r(WARN, f"库存 {s['total']} 但挑不到「开心」", "检查标签/文件是否存在")
    ctx["details"].append(f"表情库：{s}；health={h}")
    if s.get("broken_file"):
        return _r(WARN, f"{s['total']} 张，坏文件 {s['broken_file']}",
                  "/清理表情 清掉坏数据")
    return _r(OK, f"{s['total']} 张，可用 {s.get('usable', '?')}")


async def t_tts(bot, ev, ctx):
    try:
        chat = importlib.import_module("plugins.chat")
    except Exception as e:
        return _r(SKIP, f"chat 未加载（{type(e).__name__}）", "")
    online = False
    try:
        online = bool(chat._compute_online())
    except Exception:
        pass
    if not getattr(chat, "USE_LOCAL_TTS", False):
        return _r(WARN, "USE_LOCAL_TTS 关着，语音走云端", "要本地音色就打开")
    if not online:
        return _r(WARN, "算力机 indextts 不在线", "检查 7860 端口 / 隧道")
    return _r(OK, "本地 TTS 在线")


async def t_draw(bot, ev, ctx):
    try:
        td = importlib.import_module("plugins.tool_dispatcher")
    except Exception as e:
        return _r(SKIP, f"tool_dispatcher 未加载（{type(e).__name__}）", "")
    hb = float((td.STATUS or {}).get("last_heartbeat") or 0)
    caps = set((td.STATUS or {}).get("caps") or [])
    age = time.time() - hb if hb else -1
    if age < 0:
        return _r(BAD, "算力机从未心跳", "启动 node.py")
    if age > 180:
        return _r(BAD, f"心跳已停 {age / 60:.0f} 分钟", "检查算力机 node.py")
    if "draw" not in caps:
        return _r(WARN, "算力机没报 draw 能力", "ComfyUI 没起来或 workflow 缺")
    pending = len((td.STATUS or {}).get("tasks") or {})
    ctx["details"].append(f"算力机心跳 {age:.0f}s 前，caps={sorted(caps)}，任务 {pending}")
    return _r(OK, f"在线（{age:.0f}s），caps={sorted(caps)}")


async def t_video(bot, ev, ctx):
    try:
        chat = importlib.import_module("plugins.chat")
        q = chat._video_queue
    except Exception as e:
        return _r(SKIP, f"视频队列不可用（{type(e).__name__}）", "")
    return _r(OK, f"队列 {q.qsize()}/{q.maxsize}")


async def t_tasks(bot, ev, ctx):
    try:
        td = importlib.import_module("plugins.tool_dispatcher")
    except Exception as e:
        return _r(SKIP, f"未加载（{type(e).__name__}）", "")
    tasks = (td.STATUS or {}).get("tasks") or {}
    bad = [t for t, v in tasks.items() if v.get("status") == "pending"
           and time.time() - float(v.get("created") or v.get("ts") or time.time()) > 900]
    if bad:
        return _r(WARN, f"{len(bad)} 个任务卡住 >15 分钟", "检查算力机")
    return _r(OK, f"{len(tasks)} 个任务（无卡死）")


# ============================================================
# 🧰 扩展与工具
# ============================================================
async def t_tools_reg(bot, ev, ctx):
    names = {(s.get("function") or {}).get("name") for s in L.TOOLS_SPEC}
    expect = {"draw_image", "draw_from_pixiv", "pixiv_search", "girls_2d",
              "daily_luck", "rollpig", "petpet", "steam_judge",
              "napcat_help", "napcat_call", "self_tool_write",
              "sing", "remember", "recall", "video_understand"}
    miss = sorted(expect - names)
    if miss:
        return _r(BAD, f"缺工具：{'、'.join(miss)}", "看启动日志哪个插件 import 失败")
    g = len(L.tools_for("group_1"))
    p = len(L.tools_for("private_1"))
    ctx["details"].append(f"工具 {len(names)} 个：{sorted(names)}")
    return _r(OK, f"{len(names)} 个，群挂 {g} / 私聊 {p}")


async def t_tool_catalog(bot, ev, ctx):
    cats = list(L.TOOL_CATALOG.keys())
    hit = L._tool_search("画图")
    if len(cats) < 5:
        return _r(WARN, f"只有 {len(cats)} 个分类", "TOOL_CATALOG 被删了？")
    if "没搜到" in hit:
        return _r(WARN, "搜「画图」搜不到", "检查 _tool_search")
    return _r(OK, f"{len(cats)} 个分类，搜索正常")


async def t_third(bot, ev, ctx):
    mods = ["plugins.third.pixivrank_search", "plugins.third.petpet",
            "plugins.third.girls_2d", "plugins.third.nonebot_plugin_rollpig_plus"]
    found, miss = [], []
    for m in mods:
        try:
            ok = importlib.util.find_spec(m) is not None
        except Exception:
            ok = False
        (found if ok else miss).append(m)
    if miss:
        return _r(WARN, f"找不到：{miss}", "确认 plugins/third 已上传")
    return _r(OK, f"{len(found)} 个三方插件在位")


async def t_self_tools(bot, ev, ctx):
    try:
        st = importlib.import_module("plugins.self_tools")
    except Exception as e:
        return _r(SKIP, f"未加载（{type(e).__name__}）", "")
    n = len(list(st.TOOLS_DIR.glob("*.py")))
    env = st._safe_env()
    leak = [k for k in env if re.search(r"(KEY|TOKEN|SECRET|PASSWORD|COOKIE)", k, re.I)]
    if leak:
        return _r(BAD, f"子进程环境泄漏密钥：{leak}", "检查 _safe_env 白名单")
    return _r(OK, f"{n} 个自造工具，环境无密钥")


# ============================================================
# 🗂️ 数据健康（含 30 天迁移前置检查）
# ============================================================
async def t_memory_index(bot, ev, ctx):
    from services import memory_v2 as MV
    try:
        idx = json.loads(MV.INDEX_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        return _r(BAD, f"索引读不了：{type(e).__name__}", "从备份恢复 data/memory/index.json")
    items = [d for d in idx.get("items", []) if d.get("status") == "active"]
    missing = [d["id"] for d in items[:500]
               if not (MV.BLOCK_DIR / f"{d['id']}.json").exists()]
    if missing:
        return _r(WARN, f"{len(items)} 条，{len(missing)} 条 block 丢失",
                  "跑一次数据修复或接受摘要模式")
    return _r(OK, f"{len(items)} 条有效记忆")


async def t_profiles(bot, ev, ctx):
    from services import profiles
    files = list(profiles.PROFILE_DIR.glob("*.json"))
    bad = 0
    for f in files:
        try:
            json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            bad += 1
    if bad:
        return _r(WARN, f"{len(files)} 个文件，{bad} 个坏了", "删除损坏文件或从备份恢复")
    return _r(OK, f"{len(files)} 个档案/相处/风格文件")


async def t_dialogue(bot, ev, ctx):
    from services import dialogue_log
    recs = dialogue_log.load_range(1)
    today = time.strftime("%Y-%m-%d")
    n_today = sum(1 for r in recs
                  if time.strftime("%Y-%m-%d", time.localtime(float(r.get("ts") or 0))) == today)
    if n_today == 0:
        return _r(WARN, "今天没有落盘记录", "检查 dialogue_log 权限/路径")
    return _r(OK, f"今天 {n_today} 条 / 近 1 天共 {len(recs)} 条")


async def t_trace(bot, ev, ctx):
    d = Path(os.getenv("LLM_TRACE_DIR", "data/llm_trace"))
    f = d / f"{time.strftime('%Y-%m-%d')}.jsonl"
    if not f.exists():
        return _r(WARN, "今天没有 trace", "LLM_TRACE=true？或今天还没聊")
    n = sum(1 for _ in f.open(encoding="utf-8", errors="ignore"))
    return _r(OK, f"{n} 条（回放评测的数据源）")


async def t_migrate(bot, ev, ctx):
    """30 天迁移前置检查：算一下有多少条已经该挪去算力机了。"""
    from services import memory_v2 as MV
    try:
        idx = json.loads(MV.INDEX_FILE.read_text(encoding="utf-8"))
    except Exception:
        return _r(SKIP, "索引读不了", "")
    now = time.time()
    old = [d for d in idx.get("items", [])
           if d.get("status") == "active"
           and (now - float(d.get("created_at") or now)) > 30 * 86400]
    if not old:
        return _r(OK, "暂无 >30 天记忆")
    return _r(WARN, f"{len(old)} 条 >30 天，可迁移到算力机图书馆",
              "scripts/ 里做一次导出后 mark_outdated")


# ============================================================
# 分组表
# ============================================================
GROUPS = [
    ("🧩 环境", [
        ("配置变量", t_env), ("人格卡", t_persona), ("目录可写", t_dirs),
        ("磁盘", t_disk), ("时区", t_time),
    ]),
    ("🧠 模型", [
        ("DeepSeek", t_deepseek), ("本地Qwen", t_local_qwen), ("GLM", t_glm),
        ("上下文包", t_prompt),
    ]),
    ("💬 对话链路", [
        ("回复解析", t_parse), ("模型真调", t_llm_call),
        ("记忆闭环", t_memory_roundtrip), ("心情闭环", t_mood_roundtrip),
        ("短期历史", t_history), ("prompt 拼装", t_prompt_build),
    ]),
    ("📮 QQ/NapCat", [
        ("登录态", t_bot), ("当前群", t_group), ("NapCat 工具", t_napcat_tools),
        ("发送干跑", t_send_dry), ("真发消息", t_send_real),
    ]),
    ("🎨 媒体", [
        ("表情库", t_sticker), ("语音", t_tts), ("画图", t_draw),
        ("视频队列", t_video), ("任务队列", t_tasks),
    ]),
    ("🧰 扩展", [
        ("工具注册", t_tools_reg), ("工具目录", t_tool_catalog),
        ("三方插件", t_third), ("自造工具", t_self_tools),
    ]),
    ("🗂️ 数据", [
        ("记忆索引", t_memory_index), ("人物档案", t_profiles),
        ("对话落盘", t_dialogue), ("LLM trace", t_trace),
        ("30天迁移", t_migrate),
    ]),
]

# 单项超时（秒）。模型调用单独放宽。
TIMEOUT = {"模型真调": 100, "DeepSeek": 40, "本地Qwen": 30, "GLM": 30}


async def _run_one(label, fn, bot, ev, ctx):
    t0 = time.time()
    try:
        st, txt, fix, detail = await asyncio.wait_for(
            fn(bot, ev, ctx), TIMEOUT.get(label, 20))
    except asyncio.TimeoutError:
        st, txt, fix, detail = BAD, f"超时 {TIMEOUT.get(label, 20)}s", "对端没响应", ""
    except Exception as e:
        import traceback
        st, txt, fix, detail = BAD, f"{type(e).__name__}: {e}", "看 docker logs", ""
        ctx["details"].append(f"[{label}] traceback:\n{traceback.format_exc()[:1500]}")
    if detail:
        ctx["details"].append(f"[{label}] {detail}")
    return st, txt, fix, time.time() - t0


def _write_report(lines: list, details: list, head: str) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    p = REPORT_DIR / f"report_{time.strftime('%Y%m%d_%H%M%S')}.txt"
    body = [head, "", *lines, "", "=" * 60, "详细信息", "=" * 60, *details]
    p.write_text("\n".join(body), encoding="utf-8")
    # 只留最近 20 份
    old = sorted(REPORT_DIR.glob("report_*.txt"))
    for f in old[:-20]:
        try:
            f.unlink()
        except Exception:
            pass
    return p


def _chunks(text: str, size: int = 900) -> list:
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > size and cur:
            out.append(cur.rstrip())
            cur = ""
        cur += line + "\n"
    if cur.strip():
        out.append(cur.rstrip())
    return out


@full_test.handle()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    if not is_owner(str(event.user_id)):
        await full_test.finish("仅主人可用（会真调模型、会消耗额度）。")

    raw = args.extract_plain_text().strip()
    is_group = isinstance(event, GroupMessageEvent)
    ctx = {
        "bot": bot, "ev": event,
        "full": raw in ("全", "全部", "all", "full", "深"),
        "send": "发送" in raw,
        "details": [],
    }

    # /全量测试 报告 → 重发上一份
    if raw in ("报告", "report", "日志"):
        files = sorted(REPORT_DIR.glob("report_*.txt"))
        if not files:
            await full_test.finish("还没有报告，先跑一次 /全量测试")
        text = files[-1].read_text(encoding="utf-8")
        for i, ck in enumerate(_chunks(text, 1200)[:8]):
            if i:
                await asyncio.sleep(0.6)
            await full_test.send(ck)
        await full_test.finish()

    # 分类过滤：/全量测试 记忆
    keyword = "" if raw in ("", "全", "全部", "all", "full", "深", "详细", "发送") else raw
    groups = GROUPS
    if keyword:
        groups = [(g, [it for it in items if keyword in it[0] or keyword in g])
                  for g, items in GROUPS]
        groups = [(g, items) for g, items in groups if items]
        if not groups:
            await full_test.finish(
                f"没找到「{keyword}」。可用的组："
                + "、".join(g.strip() for g, _ in GROUPS))

    t_all = time.time()
    lines, details = [], ctx["details"]
    n = {OK: 0, WARN: 0, BAD: 0, SKIP: 0}
    fixes = []

    for gname, items in groups:
        block = [f"\n{gname}"]
        for label, fn in items:
            st, txt, fix, cost = await _run_one(label, fn, bot, event, ctx)
            n[st] += 1
            block.append(f"{ICON[st]} {label}：{txt}（{cost:.1f}s）")
            if st in (BAD, WARN) and fix:
                fixes.append(f"{ICON[st]} {label} → {fix}")
        lines.append("\n".join(block))

    total = sum(n.values())
    cost = time.time() - t_all
    head = (f"🧪 全量测试 · {time.strftime('%m-%d %H:%M')} · {cost:.1f}s\n"
            f"共 {total} 项：{n[OK]} 通过 / {n[WARN]} 警告 / {n[BAD]} 失败 / {n[SKIP]} 跳过")
    if n[BAD] == 0 and n[WARN] == 0:
        head += "\n🎉 全绿：模型、记忆、QQ、媒体、数据链路都通。"
    elif n[BAD] == 0:
        head += "\n🟡 没有硬故障，警告项建议看一下。"
    else:
        head += "\n🔴 有硬故障，先修 ❌ 项。"

    report = _write_report(lines, details, head)
    logger.info(
        f"[FULLTEST] {total} 项：{n[OK]} ok / {n[WARN]} warn / "
        f"{n[BAD]} bad / {n[SKIP]} skip，{cost:.1f}s → {report}"
    )

    # 组装要发出去的内容
    body = head
    if fixes:
        body += "\n\n🔧 要修的：\n" + "\n".join(fixes[:8])
    body += "\n" + "\n".join(lines)

    want_detail = ("详细" in raw) or (not is_group)
    chunks = _chunks(body, 1000)
    limit = 8 if want_detail else 2
    for i, ck in enumerate(chunks[:limit]):
        if i:
            await asyncio.sleep(0.7)
        await full_test.send(ck)
    if len(chunks) > limit:
        await asyncio.sleep(0.7)
        await full_test.send(
            f"…还有 {len(chunks) - limit} 段没发（群里只发摘要）。\n"
            f"看完整报告：私聊发 /全量测试 报告\n文件：{report}"
        )
    await full_test.finish()
