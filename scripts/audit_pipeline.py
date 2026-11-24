#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
整条 pipeline 端到端审计 —— 「分开的块到底有没有接上」。

用户 2026-11-19：
  「很多功能并没有很仔细地多轮迭代看，可能很多功能只是设计上 ok，
    说不定很多参数传值没通。所以我们现在要把整条 pipeline 串起来。」

和 selftest_all.py 的分工：
  · selftest_all.py：每个模块**自己**对不对（单元测试，1481 项）；
  · 本脚本：**A 的输出有没有真的进到 B 的输入**（接力赛）。
    比如「档案写进去了」不算过，要看到它真的出现在发给模型的 messages 里。

跑法（必须在容器里，需要真 nonebot）：
    docker compose exec bot python scripts/audit_pipeline.py
    docker compose exec bot python scripts/audit_pipeline.py -v   # 打印细节

退出码 0 = 全通；1 = 有断点。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="audit_pipeline_"))
os.chdir(TMP)                     # 所有 data/ 都写临时目录，绝不碰真实数据
(TMP / "data").mkdir(exist_ok=True)

# ⚠️ 这里 chdir 到了临时目录，读不到项目根的 .env —— 必须自己把关键开关设好，
#    否则 BOT_MODE 默认是 mock，call_llm 直接返回假回复，第 6 关永远是假的。
for _k, _v in {
    "BOT_MODE": "prod",
    "XAI_API_KEY": "audit-key",
    "XAI_BASE_URL": "http://127.0.0.1:1/v1",
    "XAI_MODEL": "audit-model",
    "XAI_PROXY": "",
    "CONV_STATE_ENABLED": "true",
    "EXPRESSION_ENABLED": "true",
    "VISION_VERIFY": "true",
    "GLM_VISION": "false",
    "GROUP_NEED_AT": "true",
    "ALLOWED_GROUPS": "100000017",
    "ALLOWED_PRIVATE": "10001",
    "PROACTIVE_PRIVATE": "true",
}.items():
    os.environ.setdefault(_k, _v)

RESULTS: list = []                # (stage, ok, msg, detail)
VERBOSE = False


def ok(stage: str, msg: str, detail: str = "") -> None:
    RESULTS.append((stage, True, msg, detail))
    print(f"  ✅ {msg}" + (f"  ← {detail}" if VERBOSE and detail else ""))


def bad(stage: str, msg: str, detail: str = "") -> None:
    RESULTS.append((stage, False, msg, detail))
    print(f"  ❌ {msg}" + (f"\n       {detail}" if detail else ""))


# ============================================================
# 假的 HTTP：把「发给模型的请求」截下来
# ============================================================
class _Resp:
    def __init__(self, payload: dict):
        self._p = payload
        self.status_code = 200
        self.text = ""

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._p


class _Client:
    """记录最后一次 POST 的 payload；返回一句固定回复。"""
    last: dict = {}
    reply: str = "嗯嗯，我在听"

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None, **kw):   # noqa: A002
        _Client.last = {"url": url, "json": json or {}}
        tc = (_Client.last["json"] or {}).get("tool_choice")
        # 被强制指定函数（安静状态）→ 真模型会返回 tool_call，这里要模拟出来，
        # 否则「强制不回」这一关永远测不到。
        if isinstance(tc, dict):
            name = (tc.get("function") or {}).get("name") or "stay_silent"
            return _Resp({
                "choices": [{"message": {"content": None, "tool_calls": [{
                    "id": "call_audit", "type": "function",
                    "function": {"name": name, "arguments": "{}"},
                }]}}],
                "usage": {},
            })
        return _Resp({
            "choices": [{"message": {"content": _Client.reply}}],
            "usage": {"prompt_cache_hit_tokens": 100, "prompt_cache_miss_tokens": 20},
        })

    async def get(self, url, **kw):
        return _Resp({})


def _fake_httpx():
    return types.SimpleNamespace(AsyncClient=_Client,
                                 TimeoutException=Exception)


def payload_text() -> str:
    """把最后一次请求的所有 message 拼成一段，方便找关键字。"""
    msgs = ((_Client.last.get("json") or {}).get("messages") or [])
    return "\n".join(str(m.get("content") or "") for m in msgs)


# ============================================================
# 各阶段
# ============================================================
async def main() -> int:
    global VERBOSE

    print("=" * 70)
    print("  Pipeline 端到端审计（A 的输出有没有进到 B 的输入）")
    print(f"  临时目录：{TMP}")
    print("=" * 70)

    # ---------- 0. 依赖 ----------
    print("\n[0] 依赖 & 模块")
    # 插件（on_message/on_command）需要 driver；容器里 nonebot 是真包，init 一下
    try:
        import nonebot as _nb
        _nb.init()
    except Exception as e:
        print(f"  ⚠️ nonebot.init 失败（工具检查会退化为桩）：{type(e).__name__}: {e}")

    try:
        from services import (attention, chat_switch, conv_state, dialogue_log,
                              expression, group_scene, llm as L, memory_v2, mood,
                              proactive, profiles, vision_verify as VV)
        from services.aura_logic import parse_reply
        import services.llm as _llm
        import services.conv_state as _cs
    except Exception as e:
        bad("0", "模块导入失败（请在容器里跑）", f"{type(e).__name__}: {e}")
        return 1
    ok("0", "services 全部可导入")

    # 工具是**插件**在模块顶层 register_tool 的 —— 不 import 插件就没有工具
    _plugins_ok = False
    try:
        import plugins.chat  # noqa: F401
        _plugins_ok = True
        ok("0", "plugins.chat 已加载（真实工具已注册）")
    except Exception as e:
        bad("0", "plugins.chat 加载失败（工具检查会退化为桩）",
            f"{type(e).__name__}: {e}")

    # 把 HTTP 换掉（llm / conv_state 各自 import 了 httpx）
    _llm.httpx = _fake_httpx()
    _cs.httpx = _fake_httpx()
    ok("0", "HTTP 已替换为假客户端（不会真的调模型）")

    # 外部依赖（tavily / GLM / agent / trace.moe）全部断开 —— 只测我们自己的接线
    async def _no_trace(*a, **kw):
        return {"work": "偶像大师", "similarity": 0.93, "votes": 3, "episode": 2,
                "at": 95.0, "characters": ["星井美希"], "confidence": "high",
                "candidates": []}

    async def _no_verify(*a, **kw):
        return {}

    async def _no_recall(*a, **kw):
        return {}

    VV.trace_moe = _no_trace
    VV.verify = _no_verify
    L.fetch_agent_recall = _no_recall
    try:
        from services import glm as _glm
        _glm.available = lambda: False
    except Exception:
        pass
    ok("0", "外部检索已断开（trace.moe / 联网核验 / GLM / agent）")

    # 插件没加载时，装一个最小 stay_silent 桩，保证第 10 关（强制不回）仍能测
    if not _plugins_ok:
        from services.llm import register_tool as _reg

        async def _stub_silent(args):
            L.TOOL_CTX["silent"] = True
            return "好"

        _reg({"type": "function", "function": {
            "name": "stay_silent", "description": "audit stub",
            "parameters": {"type": "object", "properties": {}}}},
            _stub_silent, scope=("private", "group"))

    # ---------- 1. 群消息 → 现场 ----------
    print("\n[1] 群消息 → 群聊现场（谁→谁 / 线程 / 收件人）")
    gid = "100000017"
    group_scene.clear(gid)
    group_scene.observe(gid, mid="101", qq="10001", name="阿明", text="英伟达跌了吗",
                        to_reason="", reply_mid="", is_bot=False, bot_qq="999",
                        names={"999": "小鲸鱼"})
    group_scene.observe(gid, mid="102", qq="10002", name="小柚子", text="对啊 都是老黄的锅",
                        to_qq="10001", to_reason="reply", reply_mid="101",
                        is_bot=False, bot_qq="999", names={"999": "小鲸鱼"})
    group_scene.observe(gid, mid="103", qq="10001", name="阿明", text="@小柚子 你买了？",
                        to_qq="10002", to_reason="at", reply_mid="",
                        is_bot=False, bot_qq="999", names={"999": "小鲸鱼"})
    snap = group_scene.snapshot(gid, bot_qq="999", my_names={"小鲸鱼"})
    block = group_scene.format_block(snap)
    if "英伟达" in block and "老黄" in block and "买了" in block:
        ok("1", f"3 条消息全部进了现场（{len(block)} 字）")
    else:
        bad("1", "现场缺消息", block[:300])
    if snap.get("threads") and len(snap["threads"]) >= 1:
        ok("1", f"聚成 {len(snap['threads'])} 条线程")
    else:
        bad("1", "没有线程", str(snap)[:300])
    if "小柚子" in block:
        ok("1", "收件人（谁对谁说）能渲染出来")
    else:
        bad("1", "收件人没进现场", block[:300])

    # ---------- 1.5 重启后补现场（对话日志 → 现场） ----------
    print("\n[1.5] 重启后：对话日志 → 现场（按条数补回，不按时间）")
    g_seed = "822659532"
    group_scene.clear(g_seed)          # 模拟重启：内存现场全没了
    for _qq, _who, _tx in [
        ("10001", "阿明(10001)", "少爷怎么没玩gta6"),
        ("10002", "小柚子(10002)", "没钱"),
        ("10003", "老王(10003)", "这猫躺得比我还会摆烂"),
        ("10003", "老王(10003)", "少爷没钱玩gta6，倒是有钱养这么个大爷"),
    ]:
        dialogue_log.log_msg(g_seed, _who, _qq, _tx)
    group_scene.seed_from_log(g_seed, bot_qq="999")
    _rows_seed = group_scene.recent_messages(g_seed, 20)
    if len(_rows_seed) >= 4:
        ok("1.5", f"重启后补回 {len(_rows_seed)} 条（以前只剩最后 1 条）")
    else:
        bad("1.5", "重启后现场还是空的", f"rows={len(_rows_seed)}")
    _blk_seed = group_scene.format_block(group_scene.snapshot(g_seed, bot_qq="999"))
    if "少爷没钱玩gta6" in _blk_seed:
        ok("1.5", "补回来的消息进了 prompt 的「群聊现场」")
    else:
        bad("1.5", "补回来的消息没进现场块", _blk_seed[:200])
    if (group_scene.seeded_count(g_seed) >= 4
            and group_scene.snapshot(g_seed, bot_qq="999").get("seeded") >= 4):
        ok("1.5", "卡片能显示「日志补 N 条」")
    else:
        bad("1.5", "seeded 计数没传出去")

    # ---------- 2. 对话 → 滚动状态 ----------
    print("\n[2] 对话 → 滚动状态（conv_state 压缩）")
    _Client.reply = json.dumps({
        "topics": ["英伟达股票"], "background": ["阿明买了英伟达"],
        "progress": [], "pending": ["小柚子还没回答买没买"],
        "agreements": [],
    }, ensure_ascii=False)
    _cs._LAST_AT.pop(f"group_{gid}", None)
    _cs._LAST_SIG.pop(f"group_{gid}", None)
    # ⚠️ conv_state 要求 ≥6 行才压（太短不值得花 token）—— 少于 6 行它会直接
    #    返回 False，看起来像"没接上"，其实是闸门。这里给够 6 行。
    _cs_lines = [
        "10:01 阿明：你们最近发现英伟达股票跌了吗",
        "10:02 小柚子：对啊 都是老黄的锅",
        "10:03 阿明：你买了？",
        "10:04 小柚子：买了点 亏麻了",
        "10:05 阿明：我也是",
        "10:06 小柚子：那就先放着吧",
    ]
    upd = await conv_state.update(f"group_{gid}", _cs_lines)
    state_block = conv_state.render(f"group_{gid}")
    if upd and "英伟达" in state_block:
        ok("2", f"状态压缩并渲染（{len(state_block)} 字）")
    else:
        bad("2", "状态没压出来", f"update={upd} render={state_block[:120]!r}")
    _Client.reply = "嗯嗯，我在听"      # 后面几关要的是正常回复，不是压缩用的 JSON

    # ---------- 3. 表达学习 ----------
    print("\n[3] 表达学习（群 / 人两种作用域）")
    try:
        expression._merge(f"group_{gid}", [
            {"text": "老黄的锅", "scene": "吐槽", "tone": "轻松", "count": 1}])
        expression._merge("person_10001", [
            {"text": "你买了没", "scene": "追问", "tone": "随意", "count": 1}])
        exp_block = expression.render(gid, "10001")
        if "老黄的锅" in exp_block and "你买了没" in exp_block:
            ok("3", "群 + 人两个作用域都能渲染")
        else:
            bad("3", "表达学习缺作用域", exp_block[:200])
    except Exception as e:
        bad("3", "表达学习异常", f"{type(e).__name__}: {e}")

    # ---------- 4. 档案 + 记忆 ----------
    print("\n[4] 档案 / 记忆 写入")
    profiles.upsert_profile("10001", nickname="阿明", aliases=["阿明"], level="member")
    profiles.set_call_names("10001", call_name="阿明", bot_alias="小鲸鱼")
    profiles.add_bond_note("10001", "聊过股票，他说亏麻了", emotion="sad")
    memory_v2.remember(text="阿明买了英伟达，最近亏麻了", kind="impression",
                       about_qq="10001", importance=0.8)
    _p4 = profiles.get_profile("10001")
    _bond = (profiles.get_bond("10001") or {}).get("notes") or []
    ok("4", f"档案写入：{_p4.get('nickname')}／称呼 {profiles.call_of('10001', '', gid)}"
            f"／相处印象 {len(_bond)} 条") if _p4.get(
        "nickname") == "阿明" else bad("4", "档案没写进去", str(_p4)[:200])
    prof_card = profiles.render_card("10001", gid, detail="brief", max_len=300)
    ok("4", f"档案卡片可渲染（{len(prof_card)} 字）") if prof_card else bad(
        "4", "档案卡片是空的")

    # ---------- 5. 上下文组装 ----------
    print("\n[5] 上下文组装（build_prompt）")
    from services.prompt_builder import build_prompt
    prompt = build_prompt(
        session_key=f"group_{gid}", user_text="你们最近发现英伟达股票跌了吗",
        speaker_qq="10001", speaker_name="阿明", persona_text="",
        atmosphere="", focus_qqs=["10002"], addressed=False,
        scene_text=block,
    )
    for k, label in (("现在时间", "时间"), ("阿明", "说话人"), ("英伟达", "记忆/话题")):
        if k in prompt:
            ok("5", f"上下文含「{label}」")
        else:
            bad("5", f"上下文缺「{label}」", prompt[:400])

    # ---------- 6. 真正发给模型的东西 ----------
    print("\n[6] 发给模型：人设 + 规矩 + 你在哪 + 地盘 + 现场 + 档案 + 状态 + 工具")
    L.set_tool_context(
        bot=None,
        event=types.SimpleNamespace(group_id=int(gid), message_id=103,
                                    user_id=10001, self_id=999),
        speaker_qq="10001", speaker_name="阿明", addressed=False, newer_count=0,
    )
    os.environ.setdefault("ALLOWED_GROUPS", gid)
    os.environ.setdefault("ALLOWED_PRIVATE", "10001")
    reply, _ = await L.call_llm(
        f"group_{gid}", prompt,
        group_context=["10:01 阿明: 英伟达跌了吗", "10:02 小柚子: 对啊 都是老黄的锅"],
        speaker_qq="10001", speaker_name="阿明",
    )
    sent = payload_text()
    must = {
        "人设": "小鲸鱼" in sent or "鱼" in sent,
        "规矩(编号)": "▍00" in sent,
        "你在哪(群号)": "群号：100000017" in sent,
        "你的地盘": "你能说话的群" in sent,
        "工具目录": "[[TOOLS|" in sent,
        "群聊现场": "英伟达" in sent,
        "档案(阿明)": "阿明" in sent,
        "滚动状态": "之前的状态" in sent or "英伟达股票" in sent,
        "说话风格参考": "说话风格参考" in sent,
        "历史对话": "老黄" in sent,
    }
    for k, good in must.items():
        ok("6", f"请求里有「{k}」") if good else bad(
            "6", f"请求里**没有**「{k}」", sent[:500])
    tools = [((s.get("function") or {}).get("name"))
             for s in ((_Client.last.get("json") or {}).get("tools") or [])]
    for t in ("stay_silent", "chat_switch", "vision_source", "sticker_save"):
        ok("6", f"工具挂了 {t}") if t in tools else bad(
            "6", f"工具**没挂** {t}", f"实际：{tools}")
    if _Client.last.get("json", {}).get("tool_choice") == "auto":
        ok("6", "tool_choice=auto（正常状态）")
    else:
        bad("6", "tool_choice 不是 auto", str(_Client.last.get("json", {}).get("tool_choice")))

    # ---------- 7. 回复解析 ----------
    print("\n[7] 回复 → 解析（引用 / 记忆 / 称谓 / 分条 / 沉默）")
    p = parse_reply(
        "<reply:101> 你说的是这个吧 ||| 我也亏了 "
        "[[MEMO|emotion:sad|text:阿明亏钱了]] "
        "[[CALL|bot:鲸鱼|user:明哥]] [[FOCUS|2]]")
    checks = [
        ("引用消息号", any(i.get("replyTo") == "101" for i in (p.get("items") or []))),
        ("分条(2 条)", len([i for i in (p.get("items") or []) if i["kind"] == "text"]) >= 2),
        ("记忆标注", bool(p.get("memos"))),
        ("称谓标注", bool(p.get("calls"))),
        ("回应对象", str(p.get("focus")) == "2"),
    ]
    for name, good in checks:
        ok("7", f"解析出「{name}」") if good else bad("7", f"没解析出「{name}」", str(p)[:300])
    ok("7", "沉默标记可用") if parse_reply("(不想回复)").get("silent") else bad(
        "7", "「(不想回复)」没被识别成沉默")

    # ---------- 8. 解析结果真的能落库 ----------
    print("\n[8] 解析结果 → 记忆 / 心情 / 档案（worker 的四条写入路径）")
    try:
        for m in (p.get("memos") or []):
            memory_v2.remember(text=m["text"], kind=m.get("kind") or "impression",
                               about_qq="10001", importance=0.7,
                               emotion_hit=True, emotion_weight=0.6)
            profiles.add_bond_note("10001", m["text"], emotion="sad")
            mood.bump_by_emotion("sad", reason=m["text"], about_qq="10001",
                                 session_key=f"group_{gid}")
        for c in (p.get("calls") or []):
            profiles.set_call_names("10001", call_name=c.get("user"),
                                    bot_alias=c.get("bot"))
        got = profiles.call_of("10001", "", gid)
        ok("8", f"称谓写回生效（{got}）") if got else bad("8", "称谓没写回")
        # 2026-11-20 起数值只认第三方判读（MOOD_FROM_JUDGE=true）；
        # 她自报的 emotion 只写「原话」label，不动数值。测试按这个真实行为断言。
        _m8 = mood.get_mood(f"group_{gid}")
        if getattr(mood, "MOOD_FROM_JUDGE", True):
            _lab8 = str(_m8.get("label") or "").strip()
            ok("8", f"心情原话已写回（{_lab8}）") if _lab8 else bad(
                "8", "心情原话没写回（MOOD_FROM_JUDGE=true 只写 label）")
        else:
            mv = float(_m8.get("value") or 0.0)
            ok("8", f"心情被触动（{mv:.1f}）") if mv != 0 else bad("8", "心情没变化")
    except Exception as e:
        bad("8", "落库异常", f"{type(e).__name__}: {e}")

    # ---------- 9. 识图 → 同一份上下文 ----------
    print("\n[9] 识图 → 多源报告 → 进 LLM（媒体通道）")
    async def _fake_vl(prompt: str, max_tokens: int = 200) -> str:
        return ("标记：正常\n类型：图片\n画面：粉发少女叼着饼干\n"
                "文字：无\n作品：偶像大师（猜）\n角色：星井美希（猜）")
    rec = await VV.recognize(
        b"\xff\xd8\xff\xe0" + b"0" * 600, _fake_vl, url="", hint="这是我 cos 的")
    if rec.get("report") and "qwen识图" in rec["report"]:
        ok("9", f"多源报告生成（{len(rec['report'])} 字）")
    else:
        bad("9", "多源报告为空", str(rec)[:300])
    media_text = (f"[图片识别结果] 群号:{gid} | 发送者QQ:10001 | 发送者昵称:阿明\n"
                  f"{rec.get('report') or ''}\n（他同时说：「这是我 cos 的」）")
    L.set_tool_context(
        bot=None,
        event=types.SimpleNamespace(group_id=int(gid), message_id=104,
                                    user_id=10001, self_id=999),
        speaker_qq="10001", speaker_name="阿明", addressed=True, newer_count=0,
    )
    await L.call_llm(f"group_{gid}", media_text, group_context=["10:03 阿明: [图片]"],
                     speaker_qq="10001", speaker_name="阿明",
                     is_vision=True, media_kind="image")
    sent2 = payload_text()
    for k, needle in (("识图报告", "qwen识图"), ("附图附言", "cos"),
                      ("当前这张图", "当前图")):
        ok("9", f"媒体请求里有「{k}」") if needle in sent2 else bad(
            "9", f"媒体请求里没有「{k}」", sent2[:500])

    # ---------- 10. 安静 → 强制不回复 ----------
    print("\n[10] 安静开关 → API 层强制 stay_silent")
    chat_switch.set_off("群", gid, minutes=30, reason="想安静")
    L.set_tool_context(
        bot=None,
        event=types.SimpleNamespace(group_id=int(gid), message_id=105,
                                    user_id=10001, self_id=999),
        speaker_qq="10001", speaker_name="阿明", addressed=False, newer_count=0,
    )
    _Client.last = {}
    silent_reply, _ = await L.call_llm(f"group_{gid}", prompt,
                                       group_context=["10:04 阿明: 在吗"],
                                       speaker_qq="10001", speaker_name="阿明")
    tc = (_Client.last.get("json") or {}).get("tool_choice")
    if isinstance(tc, dict) and (tc.get("function") or {}).get("name") == "stay_silent":
        ok("10", "tool_choice 锁死 stay_silent")
    else:
        bad("10", "tool_choice 没锁死", str(tc))
    ok("10", f"回复被吞掉（{silent_reply!r}）") if silent_reply in (
        "", "(不想回复)") else bad("10", "安静状态还是说了话", silent_reply[:80])
    chat_switch.set_on("群", gid)
    ok("10", "能自己开回来") if not chat_switch.is_off(f"group_{gid}")[0] else bad(
        "10", "开不回来")

    # ---------- 11. 主动私聊 ----------
    print("\n[11] 主动私聊（心情 × 亲密度 × 注意力漂移）")
    qq = "10001"
    # 造一条 3 小时前的私聊记录（避免"刚聊完"）
    _f = dialogue_log._day_file(time.time() - 3 * 3600)
    _f.parent.mkdir(parents=True, exist_ok=True)
    with _f.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "ts": time.time() - 3 * 3600, "gid": f"private_{qq}",
            "who": "阿明", "qq": qq, "kind": "user", "text": "今天好累啊",
        }, ensure_ascii=False) + "\n")
    _real_inti, _real_factors = proactive.profiles.intimacy, proactive.mood.mood_factors
    try:
        proactive.profiles.intimacy = lambda q: 0.9
        proactive.mood.mood_factors = lambda k="": {"chatty": 1.2}
        cand = proactive.score_of(qq)
        ok("11", f"分数够 → 会主动找他（{cand.get('score')}）") if cand.get("ok") else bad(
            "11", "该找却没选中", str(cand))
        if cand.get("ok"):
            blk = proactive.prompt_block(cand)
            for k in ("主动找", "上次聊天"):
                ok("11", f"主动提示里有「{k}」") if k in blk else bad(
                    "11", f"主动提示缺「{k}」", blk[:300])
            lines, _, _ = proactive.recent_private(qq)
            ok("11", "能读到上次私聊原文") if any("好累" in x for x in lines) else bad(
                "11", "读不到上次私聊")
        proactive.profiles.intimacy = lambda q: 0.1
        low = proactive.score_of(qq)
        ok("11", "不够熟 → 不打扰") if not low.get("ok") else bad(
            "11", "陌生人也会被主动私聊", str(low))
    finally:
        proactive.profiles.intimacy, proactive.mood.mood_factors = _real_inti, _real_factors

    # ---------- 12. 自我层 / 翻页 / 破冰 ----------
    print("\n[12] 自我层（我在他面前）／翻页提示／破冰")
    try:
        profiles.set_stance("10001", "爱撒娇")
        _bc = profiles.render_card("10001", gid, detail="brief", max_len=600)
        ok("12", "档案里有「你在他面前」") if "你在他面前：爱撒娇" in _bc else bad(
            "12", "「我在他面前的样子」没进档案卡", _bc[:300])
        for _i in range(5):
            profiles.add_bond_note("10001", f"一起做了第{_i}件事", emotion="happy")
        _bc2 = profiles.render_card("10001", gid, detail="brief", max_len=600)
        ok("12", "档案写明「还有 N 条 + 怎么翻」") if "翻页" in _bc2 else bad(
            "12", "档案没有翻页提示（她会以为只有这几条）", _bc2[-200:])
        for _i in range(4):
            memory_v2.remember(text=f"翻页测试记忆{_i}", kind="fact", importance=0.9)
        _mp = memory_v2.memory_prompt("翻页测试", limit=2)
        ok("12", "记忆块写明「还有 N 条 + 怎么翻」") if "翻页" in _mp else bad(
            "12", "记忆没有翻页提示", _mp[-200:])
    except Exception as e:
        bad("12", "自我层/翻页异常", f"{type(e).__name__}: {e}")

    try:
        chat_switch.set_off("群", gid, minutes=30, reason="他说话太难听", by_qq="10001")
        _t_wrong = chat_switch.maybe_thaw(f"group_{gid}", "99999", "对不起嘛")
        ok("12", "别人道歉不解除（不是他惹的不算）") if not _t_wrong else bad(
            "12", "谁道歉都解除 → 破冰判据太松", str(_t_wrong))
        _t = chat_switch.maybe_thaw(f"group_{gid}", "10001", "对不起，我错了")
        # ⚠️ 不能只判「返回了原因」—— 必须验证状态真的变了。
        #    2026-11-19 真踩过：maybe_thaw 把 "group" 传给只认「群」的 set_on，
        #    结果去 users 桶里删群号 → 返回"已解除"但群还是安静的。
        _still = chat_switch.is_off(f"group_{gid}", "10001")[0]
        if _t and not _still:
            ok("12", f"他道歉 → 自动破冰（{_t}）")
        elif _t and _still:
            bad("12", "破冰返回成功但状态没解除（桶名/键对不上）", f"why={_t}")
        else:
            bad("12", "道歉了还不解除安静", "")
    except Exception as e:
        bad("12", "破冰异常", f"{type(e).__name__}: {e}")

    # ---------- 13. 记录按条数 / 戳一戳 ----------
    print("\n[13] 记录按条数（不按时间）／戳一戳动作")
    try:
        # 造一条「5 天前」的记录：按天读不到，按条数必须读到
        _oldf = dialogue_log._day_file(time.time() - 5 * 86400)
        _oldf.parent.mkdir(parents=True, exist_ok=True)
        with _oldf.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.time() - 5 * 86400, "gid": gid,
                                 "who": "阿明", "qq": "10001", "kind": "user",
                                 "text": "五天前说过的话"}, ensure_ascii=False) + "\n")
        _by_day = dialogue_log.load_range(1)
        _by_cnt = dialogue_log.load_recent(gid, limit=10)
        if any("五天前" in str(m.get("text") or "") for m in _by_cnt):
            ok("13", f"按条数能读回 5 天前的记录（{len(_by_cnt)} 条）")
        else:
            bad("13", "load_recent 读不到旧记录", str([m.get("text") for m in _by_cnt])[:200])
        if not any("五天前" in str(m.get("text") or "") for m in _by_day):
            ok("13", "对照：load_range(1) 按天读不到（说明确实改成了按条数）")
    except Exception as e:
        bad("13", "按条数取记录异常", f"{type(e).__name__}: {e}")

    try:
        import plugins.chat as _chat_mod
        _pe = types.SimpleNamespace(raw_info=[
            {"type": "qq"}, {"type": "img", "src": "x.jpg"},
            {"type": "nor", "txt": "揉了揉"}, {"type": "qq"},
            {"type": "nor", "txt": "的小猫并说了句：好可爱"},
        ])
        _pa = _chat_mod._poke_action_text(_pe)
        ok("13", f"戳一戳能读出动作原文（{_pa}）") if "好可爱" in _pa else bad(
            "13", "戳一戳读不到动作原文（她只能回「干嘛」）", _pa)
    except Exception as e:
        bad("13", "戳一戳解析异常", f"{type(e).__name__}: {e}")

    # ---------- 汇总 ----------
    print("\n" + "=" * 70)
    total = len(RESULTS)
    fails = [r for r in RESULTS if not r[1]]
    print(f"  合计 {total} 项 → 通过 {total - len(fails)}，失败 {len(fails)}")
    if fails:
        print("\n  断点清单（这些就是「设计 ok 但没接上」的地方）：")
        for stage, _ok, msg, detail in fails:
            print(f"    ❌ [{stage}] {msg}")
            if detail:
                print(f"        {str(detail)[:300]}")
    else:
        print("  ✅ 整条链路接力正常：消息→现场→状态→档案/记忆→上下文→模型→解析→落库→识图→安静→主动")
    print("=" * 70)
    print(f"  临时目录未删除（方便排查）：{TMP}")
    return 0 if not fails else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    VERBOSE = args.verbose
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)

