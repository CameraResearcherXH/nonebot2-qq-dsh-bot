# plugins/selfcheck/__init__.py
"""
/自检 —— 一键跑「今晚所有集成」，当场看出哪条链路坏了。

和 `scripts/selftest_all.py` 的分工：
  · selftest_all.py：离线单元测试（docker exec 里跑，1145 项，不进 QQ）
  · /自检       ：**运行时**集成体检（在真 QQ 里跑，真调 NapCat / 真调模型）
两者互补：单元测试全绿但线上还是坏，通常是「配置没生效」「NapCat 没连上」
「key 过期」这类只有运行时才看得见的问题 —— 这个命令就是查这些。

用法：
  /自检        → 快检（约 3~6 秒，不烧钱）
  /自检 全     → 全检（多测 DeepSeek / 本地 Qwen / 算力机，约 20~40 秒）

每项输出：✅ 正常 / ⚠️ 能跑但不对 / ❌ 坏了，后面跟一句「怎么修」。
仅主人可用（会真调接口、会消耗额度）。
"""
import asyncio
import os
import time

from nonebot import on_command, logger
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent, GroupMessageEvent
from nonebot.params import CommandArg

from services.access import is_owner
from services import llm as L
from services import napcat_tools as NT

OK, WARN, BAD = "ok", "warn", "bad"
ICON = {OK: "✅", WARN: "⚠️", BAD: "❌"}

check_cmd = on_command("自检", priority=3, block=True)


def _r(status, text, fix=""):
    return (status, str(text)[:160], str(fix)[:120])


# ============================================================
# 🧰 NapCat 工具
# ============================================================
async def c_tool_reg(bot, ev, full):
    n_all = len(L.TOOLS_SPEC)
    g = L.tools_for("group_1")
    p = L.tools_for("private_1")
    names = [(s.get("function") or {}).get("name") for s in g]
    if "napcat_help" not in names or "napcat_call" not in names:
        return _r(BAD, f"群聊没挂 napcat（{names}）", "检查 plugins/napcat_tools 是否加载失败")
    if len(p) < len(g):
        return _r(BAD, f"私聊({len(p)}) 比群聊({len(g)}) 还少", "tools_for 的 scope 过滤写反了")
    return _r(OK, f"共 {n_all} 个，群挂 {len(g)} / 私聊 {len(p)}")


async def c_tool_catalog(bot, ev, full):
    try:
        top = NT.menu_text()
        one = NT.help_text("set_group_ban")
    except Exception as e:
        return _r(BAD, f"{type(e).__name__}: {e}", "services/napcat_tools.py 的表坏了")
    if "set_group_kick" not in NT.menu_text("group_admin"):
        return _r(BAD, "类别下查不到工具名", "menu_text(category) 坏了")
    if "必填" not in one or "【调用】" not in one:
        return _r(BAD, "工具说明缺参数表", "help_text(tool) 坏了")
    hint = NT.menu_hint(True)
    return _r(OK, f"{len(NT.TOOLS)} 个 / {len(NT.CATEGORIES)} 类，常驻提示 {len(hint)} 字")


async def c_tool_hint_folded(bot, ev, full):
    """
    折叠是否生效：常驻提示不能把工具目录列出来（那等于没折叠）。

    ⚠️ 判据不能是「出现任意一个工具名」—— 规则句里本来就会提 send_group_msg
    （「别用 send_group_msg 重复发正文」），那是用法说明，不是目录。
    真正的判据：① 有没有「类别速查」那一行（HINT_FULL 开着才有）
                ② 工具名出现 ≥4 个（目录才可能列这么多）
    """
    hint = NT.menu_hint(True)
    if "类别速查" in hint:
        return _r(WARN, f"提示里带类别速查（{len(hint)} 字）",
                  "NAPCAT_HINT_FULL 设成 false 才会折叠成目录")
    # COMMON 里那 6 个是**故意**列出来的常用速查（点赞/戳一戳…），不算「泄露目录」。
    # 判据只看「COMMON 之外还列了几个」—— 规则句里提到 1 个（send_group_msg）是正常的。
    common = {n for n, _a, _d in NT.COMMON}
    leaked = [n for n in NT.TOOLS if n in hint and n not in common]
    if len(leaked) >= 4:
        return _r(WARN, f"目录外还列了 {len(leaked)} 个工具名 {leaked[:5]}", "折叠没生效")
    if len(hint) > 1200:
        return _r(WARN, f"常驻提示 {len(hint)} 字，偏长", "精简一下")
    return _r(OK, f"{len(hint)} 字，目录入口 + {len(common)} 个常用速查"
                  f"（目录外只提到 {len(leaked)} 个当例子）")


async def c_napcat_live(bot, ev, full):
    """真调一次 NapCat：这是「QQ 那边通不通」的唯一可靠判断。"""
    try:
        d = await asyncio.wait_for(bot.call_api("get_login_info"), 8)
    except Exception as e:
        return _r(BAD, f"{type(e).__name__}: {e}",
                  "NapCat 没连上 / WS 断了，看 napcat 容器日志")
    return _r(OK, f"登录 {d.get('nickname')}({d.get('user_id')})")


async def c_napcat_group(bot, ev, full):
    try:
        d = await asyncio.wait_for(bot.call_api("get_group_list"), 8)
        n = len(d or [])
    except Exception as e:
        return _r(BAD, f"{type(e).__name__}: {e}", "NapCat 拿不到群列表")
    if isinstance(ev, GroupMessageEvent):
        gid = int(ev.group_id)
        if gid not in {int(x.get("group_id") or 0) for x in (d or [])}:
            return _r(WARN, f"{n} 个群，但不含当前群 {gid}", "账号可能刚退群/换号")
        try:
            info = await asyncio.wait_for(
                bot.call_api("get_group_info", group_id=gid), 8)
            return _r(OK, f"{n} 个群，当前群《{info.get('group_name')}》"
                          f"{info.get('member_count')} 人")
        except Exception as e:
            return _r(WARN, f"{n} 个群，但拿不到当前群资料：{e}", "")
    return _r(OK, f"{n} 个群")


async def c_quick_actions(bot, ev, full):
    """
    高频小动作（点赞/戳一戳/改名片）能不能「一句话就做到」。

    用户 2026-11-12：「让她给群友点赞却没点」。
    根因是折叠太狠 —— 她要 napcat_help() → category → tool 查三次才知道 send_like
    怎么调，干脆放弃了。现在常驻提示里直接给这几个的完整调用式，这里验的是
    「照着抄能不能真的发出去」：名字→QQ 解析 + times 默认值。
    """
    class _FB:
        self_id = 999

        def __init__(self):
            self.calls = []

        async def call_api(self, action, **kw):
            self.calls.append((action, kw))
            return {}

        async def get_group_member_list(self, group_id=0):
            return [{"user_id": 555, "card": "小明", "nickname": "xm", "role": "member"}]

    fb = _FB()
    ctx = {"user_id": 111, "group_id": 222, "self_id": "999"}
    res = await NT.execute(fb, "send_like", {"user_id": "小明"}, requester="111", ctx=ctx)
    if not fb.calls:
        return _r(BAD, f"没发出调用：{res[:60]}", "send_like 参数组装坏了")
    action, kw = fb.calls[-1]
    if action != "send_like":
        return _r(BAD, f"调成了 {action}", "工具名映射错了")
    if kw.get("user_id") != 555:
        return _r(BAD, f"user_id={kw.get('user_id')}", "名字→QQ 解析坏了")
    if kw.get("times") != 1:
        return _r(WARN, f"times={kw.get('times')}", "应默认 1，否则 NapCat 可能拒")
    if not any(n == "send_like" for n, _a, _d in NT.COMMON):
        return _r(WARN, "常用速查里没有点赞", "她可能还是想不到用")
    return _r(OK, "名字→QQ 555，times=1（只验组装，不真点）")


async def c_perm(bot, ev, full):
    from services import access
    owner = str(access.OWNER_QQ or "")
    if not owner:
        return _r(BAD, "没配主人", ".env 里 SUPERUSERS=[\"你的QQ\"]")
    ok = NT.level_ok(NT.OWNER, owner) and not NT.level_ok(NT.OWNER, "10000")
    if not ok:
        return _r(BAD, "权限判定不对", "services/access.py 的 level_of 坏了")
    return _r(OK, f"主人 {owner}，危险操作已锁；待批准 {len(NT.PENDING)} 个")


# ============================================================
# 🔗 引用回复
# ============================================================
async def c_reply_parse(bot, ev, full):
    try:
        from services.aura_logic import parse_reply
        p = parse_reply("好呀 <reply:123456> 你说的是这个吧")
    except Exception as e:
        return _r(BAD, f"{type(e).__name__}: {e}", "aura_logic.parse_reply 坏了")
    items = p.get("items") or []
    got = next((i.get("replyTo") for i in items if i.get("replyTo")), "")
    if got != "123456":
        return _r(BAD, f"解析不出引用（replyTo={got!r}）", "parse_reply 的 <reply:> 正则坏了")
    if any("reply:" in (i.get("value") or "") for i in items):
        return _r(BAD, "标记漏进正文了", "parse_reply 没把标记剔干净")
    return _r(OK, "解析 + 剔除都正常")


async def c_reply_send(bot, ev, full):
    """发送层有没有真的接 reply 段（这是上次漏掉的地方）。"""
    try:
        from plugins.chat import _segs_to_message
        m = _segs_to_message([{"type": "reply", "data": {"id": "123456"}},
                              {"type": "text", "data": {"text": "hi"}}])
        types = [s.type for s in m]
    except Exception as e:
        return _r(BAD, f"{type(e).__name__}: {e}", "_segs_to_message 不认识 reply 段")
    if "reply" not in types:
        return _r(BAD, f"发不出 reply 段（{types}）", "chat 的 _segs_to_message 漏了 reply 分支")
    return _r(OK, f"能发 reply 段（{types}）")


async def c_reply_ctx(bot, ev, full):
    """群聊上下文里有没有带消息号 —— 没带她就不知道引哪条。"""
    from plugins.chat import recent_group_msgs
    if not isinstance(ev, GroupMessageEvent):
        # 私聊上下文本来就不带消息号（引用主要用在群里），这是「不适用」不是「有问题」
        return _r(OK, "私聊（上下文消息号只用在群里，这里不用测）")
    lines = list(recent_group_msgs.get(str(ev.group_id), []))
    if not lines:
        return _r(OK, "这个群还没有上下文（发一句再说就有了）")
    hit = [x for x in lines if "#" in x.split(": ", 1)[0]]
    if not hit:
        return _r(BAD, "上下文没带消息号", "chat 里 recent_group_msgs 的 append 少了 #id")
    return _r(OK, f"最近 {len(lines)} 条里 {len(hit)} 条带消息号")


# ============================================================
# 🧠 分级思考
# ============================================================
async def c_think(bot, ev, full):
    fast, deep = L.THINK_BUDGET_FAST, L.THINK_BUDGET_DEEP
    if fast >= deep:
        return _r(BAD, f"快速档 {fast}s ≥ 深度档 {deep}s", "THINK_BUDGET_FAST 要小于 THINK_BUDGET_SEC")
    if not L._DEEP_RE.search("[[DEEP|要翻好几处]]"):
        return _r(BAD, "DEEP 标记匹配不上", "_DEEP_RE 正则坏了")
    if not L._DEEP_RE.search("[[[DEEP]]]"):
        return _r(BAD, "三括号 DEEP 匹配不上", "_DEEP_RE 要同时吃两括号和三括号")
    if "DEEP" in L._DEEP_RE.sub("", "好 [[DEEP|x]] 的"):
        return _r(BAD, "DEEP 标记剔不干净", "会把标记发给用户")
    return _r(OK, f"快速 {fast:.0f}s/{L.MAX_REQ_ROUNDS_FAST} 轮，"
                  f"深度 {deep:.0f}s/{L.MAX_REQ_ROUNDS} 轮")


async def c_think_wire(bot, ev, full):
    """
    DEEP 和引用回复有没有真的拼进 system（不然她根本不知道自己能申请/能引用）。

    ⚠️ 这里不能直接调 `_behaviour_protocol` 了事：`/自检` 是独立 matcher，
    `TOOL_CTX` 里**没有 event**（只有 chat 的 worker 才会 set），
    直接调会得到「拿不到 message_id」的假警告（11-12 00:09 那次就是这样）。
    正确做法：临时塞一个带 message_id 的假 event，真跑一遍再还原。
    """
    if "DEEP" not in L._TOOL_MENU_HEAD:
        return _r(BAD, "菜单里没告诉她 DEEP", "_TOOL_MENU_HEAD 漏了【想久一点】")

    from types import SimpleNamespace
    old = dict(L.TOOL_CTX)
    try:
        L.set_tool_context(event=SimpleNamespace(message_id=987654321))
        grp = L._behaviour_protocol("group_1", is_group=True)
        single = L._reply_protocol(True)
    except Exception as e:
        return _r(BAD, f"{type(e).__name__}: {e}", "_behaviour_protocol 崩了")
    finally:
        L.TOOL_CTX.clear()
        L.TOOL_CTX.update(old)

    if "引用回复" not in grp:
        return _r(BAD, "system 里没有引用回复那节", "_behaviour_protocol 没 append 第 12 节")
    if "987654321" not in single:
        return _r(BAD, "引用协议没带当前消息号", "_reply_protocol 拿不到 message_id")
    if "<reply:" not in single:
        return _r(BAD, "引用协议没教她写 <reply:>", "")
    if "想久一点" not in grp:
        return _r(BAD, "system 里没有【想久一点】", "_TOOL_MENU_HEAD 没拼进菜单")
    return _r(OK, "system 里 DEEP + 引用协议都在（消息号 987654321 已带）")


# ============================================================
# 🌙 睡眠策略
# ============================================================
async def c_sleep(bot, ev, full):
    from plugins import chat as C
    h = time.localtime().tm_hour
    in_sleep = C._in_sleep_hour()
    tag = "群聊静默" if in_sleep else "群聊正常"
    if C.PRIVATE_SLEEP:
        return _r(WARN, f"{h} 点，{tag}；私聊也跟着静默",
                  "想让私聊 24 小时：PRIVATE_SLEEP=false")
    return _r(OK, f"{h} 点，{tag}；私聊 24 小时不受限"
                  f"（窗口 {C.SLEEP_HOUR_START}~{C.SLEEP_HOUR_END}）")


async def c_private_awake(bot, ev, full):
    """私聊半夜也要能回 —— 真的走一遍 _need_reply，不靠读代码。"""
    from plugins import chat as C
    uid = str(getattr(ev, "user_id", "") or "")
    if uid not in C.ALLOWED_PRIVATE:
        return _r(WARN, f"你的 QQ({uid}) 不在私聊白名单",
                  "想让主人随时私聊她：把这个 QQ 加进 ALLOWED_PRIVATE")
    try:
        from nonebot.adapters.onebot.v11 import Message as _M, PrivateMessageEvent
        fake = PrivateMessageEvent(
            time=int(time.time()), self_id=int(bot.self_id), post_type="message",
            sub_type="friend", user_id=int(uid), message_type="private",
            message_id=1, message=_M([]), original_message=_M([]), raw_message="",
            font=0, sender={"user_id": int(uid)}, to_me=True,
        )
        ok = await C._need_reply(fake)
    except Exception as e:
        # 构造失败不算功能坏，退化成配置检查
        blocked = C._in_sleep_hour() and C.PRIVATE_SLEEP
        return _r(WARN if blocked else OK,
                  f"没法造私聊事件（{type(e).__name__}），改看配置："
                  f"PRIVATE_SLEEP={C.PRIVATE_SLEEP}",
                  "PRIVATE_SLEEP 要是 false" if blocked else "")
    if not ok:
        return _r(BAD, "私聊被规则拦了（当前时段）", "PRIVATE_SLEEP 应为 false")
    return _r(OK, "私聊规则放行（当前时段）")


# ============================================================
# 💬 消息链路 / 记忆 / 表情
# ============================================================
async def c_group_cfg(bot, ev, full):
    from plugins import chat as C
    g = sorted(C.ALLOWED_GROUPS)
    p = sorted(C.ALLOWED_PRIVATE)
    gid = str(getattr(ev, "group_id", "") or "")
    if gid and gid not in C.ALLOWED_GROUPS:
        return _r(BAD, f"当前群不在白名单（白名单 {g}）", ".env 的 ALLOWED_GROUPS 要加这个群")
    return _r(OK, f"群白名单 {len(g)} 个 / 私聊白名单 {len(p)} 个")


async def c_short_ctx(bot, ev, full):
    """
    私聊短期上下文（2026-11-12 修的大坑）。

    以前 `add_memory` 一直往 histories 写，但 `get_memory()` 全项目 0 调用 →
    私聊每句都是「第一次见面」，上一句完全忘。现在 `_db_history` 会把它读回来。
    这里验的就是「库里有东西 + 能拼成上下文」这条链。
    """
    from services import memory as M
    gid = getattr(ev, "group_id", None)
    key = f"group_{gid}" if gid else f"private_{ev.user_id}"
    try:
        rows = M.get_recent_rows(key, limit=12)
    except Exception as e:
        return _r(BAD, f"{type(e).__name__}: {e}", "data/memory.db 坏了或被锁")
    if not rows:
        return _r(WARN, f"{key} 库里没有近期记录",
                  "聊两句就有了；一直 0 说明 add_memory 没写进去")
    try:
        lines = L._db_history(key, "对方", 3)
    except Exception as e:
        return _r(BAD, f"拼上下文失败：{type(e).__name__}", "_db_history 坏了")
    if not lines:
        return _r(BAD, "库里有记录但拼不出上下文", "_db_history 的解析坏了")
    age = (time.time() - float(rows[-1].get("ts") or 0)) / 60
    return _r(OK, f"{len(rows)} 条，最近 {age:.0f} 分钟前；能拼出 {len(lines)} 行")


async def c_poke_sleep(bot, ev, full):
    """群聊免打扰时段不能接戳一戳（以前有人一戳她就冒出来说「干嘛」）。"""
    import inspect
    from plugins import chat as C
    try:
        src = inspect.getsource(C)
    except Exception:
        return _r(WARN, "读不到 chat 源码，跳过", "")
    if "if gid and _in_sleep_hour():" not in src:
        return _r(BAD, "戳一戳没守睡眠", "群聊免打扰时段会被戳醒")
    return _r(OK, f"群聊 {C.SLEEP_HOUR_START}~{C.SLEEP_HOUR_END} 点不接戳；"
                  f"私聊不受限")


async def c_memory(bot, ev, full):
    try:
        from services import memory_v2
        hits = memory_v2.recall("自检", limit=3, gid=str(getattr(ev, "group_id", "") or ""))
        n = len(hits or [])
    except Exception as e:
        return _r(BAD, f"{type(e).__name__}: {e}", "data/memory 坏了或索引没建")
    if n == 0:
        return _r(WARN, "一条都没召回", "可能真的没存过相关记忆，正常")
    return _r(OK, f"召回 {n} 条")


async def c_sticker(bot, ev, full):
    try:
        from services.sticker_library import sticker_lib
        s = sticker_lib.stats()
    except Exception as e:
        return _r(BAD, f"{type(e).__name__}: {e}", "表情库索引坏了")
    usable = int(s.get("usable") or 0)
    if usable == 0:
        return _r(BAD, "一张都发不出去", "跑 /导入表情，或 /清理表情 修坏路径")
    return _r(OK, f"{s.get('total')} 张，能发 {usable} 张，精品 {s.get('premium')}")


async def c_mood(bot, ev, full):
    try:
        from services import mood
        m = mood.get_mood()
        return _r(OK, f"心情 {m.get('value')}")
    except Exception as e:
        return _r(WARN, f"{type(e).__name__}: {e}", "")


# ============================================================
# 🖥️ 算力机 / ☁️ 模型
# ============================================================
async def c_compute(bot, ev, full):
    try:
        from plugins.tool_dispatcher import STATUS, HEARTBEAT_TIMEOUT
    except Exception as e:
        return _r(WARN, f"调度插件没加载（{type(e).__name__}）", "看启动日志")
    last = float(STATUS.get("last_heartbeat") or 0)
    if not last:
        return _r(BAD, "从没收到心跳", "算力机 node.py 没启动")
    age = time.time() - last
    caps = ",".join(sorted(STATUS.get("caps") or [])) or "未上报"
    if age > HEARTBEAT_TIMEOUT:
        return _r(BAD, f"心跳 {age:.0f}s 前（超时 {HEARTBEAT_TIMEOUT}s）",
                  "算力机掉线：翻唱/本地TTS/复盘会降级")
    return _r(OK, f"心跳 {age:.0f}s 前，能力 {caps}")


async def c_deepseek(bot, ev, full):
    if not full:
        if not L.XAI_API_KEY:
            return _r(BAD, "没配 key", ".env 里 XAI_API_KEY")
        return _r(OK, f"{L.XAI_MODEL}（key 已配，/自检 全 可实测）")
    try:
        import httpx
        kw = {"timeout": 25}
        if L.PROXY:
            kw["proxy"] = L.PROXY
        t0 = time.time()
        async with httpx.AsyncClient(**kw) as c:
            r = await c.post(
                f"{L.XAI_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {L.XAI_API_KEY}",
                         "Content-Type": "application/json"},
                json={"model": L.XAI_MODEL,
                      "messages": [{"role": "user", "content": "回复 ok"}],
                      "max_tokens": 8},
            )
            r.raise_for_status()
            txt = (r.json()["choices"][0]["message"].get("content") or "").strip()
        return _r(OK, f"{L.XAI_MODEL} 回「{txt[:12]}」，{time.time() - t0:.1f}s")
    except Exception as e:
        return _r(BAD, f"{type(e).__name__}: {str(e)[:80]}",
                  "检查 XAI_API_KEY / XAI_BASE_URL / 余额")


async def c_local_llm(bot, ev, full):
    if not full:
        return _r(OK, f"{L.LOCAL_LLM_MODEL}（/自检 全 可实测）")
    try:
        import httpx
        t0 = time.time()
        async with httpx.AsyncClient(timeout=12, trust_env=False) as c:
            r = await c.post(L.LOCAL_LLM_URL,
                             headers={"Content-Type": "application/json"},
                             json={"model": L.LOCAL_LLM_MODEL,
                                   "messages": [{"role": "user", "content": "说 ok"}],
                                   "max_tokens": 8})
            r.raise_for_status()
        return _r(OK, f"本地 Qwen 通，{time.time() - t0:.1f}s")
    except Exception as e:
        return _r(WARN, f"不通（{type(e).__name__}）",
                  "不影响聊天，只是资料压缩会退化成原文/截断")


async def c_vision_cfg(bot, ev, full):
    key = os.getenv("DASHSCOPE_API_KEY", "")
    model = os.getenv("VISION_MODEL", "")
    if not key:
        return _r(WARN, "没配 DASHSCOPE_API_KEY", "识图会全程失败")
    return _r(OK, f"{model or '默认模型'}（key 已配）")


async def c_tts(bot, ev, full):
    local = os.getenv("USE_LOCAL_TTS", "true").lower() == "true"
    return _r(OK, f"{'本地 indextts（离线自动降 CosyVoice）' if local else '云端 CosyVoice'}")


# ============================================================
# 分组
# ============================================================
GROUPS = [
    ("🧰 NapCat 工具", [
        ("工具注册", c_tool_reg),
        ("目录/说明", c_tool_catalog),
        ("折叠", c_tool_hint_folded),
        ("真调登录", c_napcat_live),
        ("群列表", c_napcat_group),
        ("点赞等小动作", c_quick_actions),
        ("权限分级", c_perm),
    ]),
    ("🔗 引用回复", [
        ("解析", c_reply_parse),
        ("发送", c_reply_send),
        ("上下文消息号", c_reply_ctx),
    ]),
    ("🧠 分级思考", [
        ("预算档位", c_think),
        ("接进 system", c_think_wire),
    ]),
    ("🌙 睡眠策略", [
        ("群/私聊", c_sleep),
        ("私聊放行", c_private_awake),
    ]),
    ("💬 消息与状态", [
        ("白名单", c_group_cfg),
        ("短期上下文", c_short_ctx),
        ("戳一戳守睡眠", c_poke_sleep),
        ("记忆", c_memory),
        ("表情库", c_sticker),
        ("心情", c_mood),
    ]),
    ("🖥️ 算力与模型", [
        ("算力机", c_compute),
        ("DeepSeek", c_deepseek),
        ("本地Qwen", c_local_llm),
        ("视觉配置", c_vision_cfg),
        ("语音", c_tts),
    ]),
]


async def _run_one(fn, bot, ev, full):
    t0 = time.time()
    try:
        st, txt, fix = await asyncio.wait_for(fn(bot, ev, full), 30)
    except asyncio.TimeoutError:
        st, txt, fix = BAD, "超时 30s", "对端没响应"
    except Exception as e:
        st, txt, fix = BAD, f"{type(e).__name__}: {e}", "看日志"
    return st, txt, fix, time.time() - t0


@check_cmd.handle()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    if not is_owner(str(event.user_id)):
        await check_cmd.finish("仅主人可用。")
    full = args.extract_plain_text().strip() in ("全", "全部", "all", "full", "深")
    t0 = time.time()

    lines, n_ok, n_warn, n_bad = [], 0, 0, 0
    fixes = []
    for gname, items in GROUPS:
        block = [f"\n{gname}"]
        for label, fn in items:
            st, txt, fix, cost = await _run_one(fn, bot, event, full)
            if st == OK:
                n_ok += 1
            elif st == WARN:
                n_warn += 1
            else:
                n_bad += 1
            block.append(f"{ICON[st]} {label}：{txt}")
            if st != OK and fix:
                fixes.append(f"{ICON[st]} {label} → {fix}")
        lines.append("\n".join(block))

    cost = time.time() - t0
    total = n_ok + n_warn + n_bad
    head = (f"🩺 自检{'（全检）' if full else '（快检）'} · "
            f"{time.strftime('%m-%d %H:%M')} · {cost:.1f}s\n"
            f"共 {total} 项：{n_ok} 通 / {n_warn} 警告 / {n_bad} 失败")
    if n_bad == 0 and n_warn == 0:
        head += "\n🎉 全绿，今晚这些集成都能用。"
    elif fixes:
        head += "\n\n🔧 要修的：\n" + "\n".join(fixes[:10])
    if not full:
        head += "\n\n（想看 DeepSeek/本地Qwen 真调：/自检 全）"

    body = "\n".join(lines)
    chunks, cur = [], head
    for part in body.split("\n"):
        if len(cur) + len(part) > 1100:
            chunks.append(cur.rstrip())
            cur = ""
        cur += part + "\n"
    if cur.strip():
        chunks.append(cur.rstrip())

    logger.info(f"[SELFCHECK] {total} 项：{n_ok} ok / {n_warn} warn / {n_bad} bad，{cost:.1f}s")
    for i, ck in enumerate(chunks):
        if i == 0:
            await check_cmd.send(ck)
        else:
            await asyncio.sleep(0.8)
            await check_cmd.send(ck)
    await check_cmd.finish()


# ================= /接入自检 —— 一键测「LLM 能力接入」 =================
# 用户 2026-11-16：画图 / 记忆 / 表情包 / 三方这些接进 llm 的功能，发一条命令全测。
# 和 /自检 的分工：/自检 测**运行时链路**（NapCat/DeepSeek/算力机），
# /接入自检 测**能力有没有接进 llm**（工具注册、标记、目录、开关），纯本地，不烧钱。
integ_cmd = on_command("接入自检", aliases={"能力自检", "工具自检"}, priority=3, block=True)


@integ_cmd.handle()
async def _(event: MessageEvent):
    if not is_owner(str(event.user_id)):
        await integ_cmd.finish("仅主人可用。")

    lines = ["🔌 LLM 能力接入自检", ""]
    n_bad = 0

    def _ck(ok: bool, label: str, detail: str = ""):
        nonlocal n_bad
        if not ok:
            n_bad += 1
        lines.append(f"{'✅' if ok else '❌'} {label}" + (f"：{detail}" if detail else ""))

    # ---- 1. 画图（function 工具） ----
    spec_names = {s.get("function", {}).get("name") for s in L.TOOLS_SPEC}
    for t in ("draw_image", "draw_from_pixiv", "pixiv_search"):
        _ck(t in spec_names, f"画图工具 {t}", "" if t in spec_names else "未注册")
    _grp_tools = {s.get("function", {}).get("name") for s in L.tools_for("group_x")}
    _ck("draw_image" in _grp_tools, "画图在群聊可用", "" if "draw_image" in _grp_tools else "scope 没挂 group")

    # ---- 2. 记忆 ----
    try:
        from services import memory_v2 as MV
        _mv_ok = all(hasattr(MV, a) for a in ("recall", "recall_catalog", "recall_detail", "parse_time_expression"))
        _ck(_mv_ok, "记忆检索（目录/时间搜索）", "" if _mv_ok else "函数缺失")
    except Exception as e:
        _ck(False, "记忆检索", f"{type(e).__name__}: {e}")
    _ck("MEMORY" in L._REQ_RE.pattern, "[[MEMORY]] 标记", "" if "MEMORY" in L._REQ_RE.pattern else "没进 _REQ_RE")

    # ---- 3. 表情包 ----
    try:
        from services import sticker_library as SL
        _ck(True, f"表情库（{len(SL.TAG_LIBRARY)} 个标签）", "")
    except Exception as e:
        _ck(False, "表情库", f"{type(e).__name__}: {e}")
    _ck("STICKER" in L._REQ_RE.pattern, "[[STICKER]] 标记", "" if "STICKER" in L._REQ_RE.pattern else "没进 _REQ_RE")

    # ---- 4. 三方（认知层目录 + bt 开关） ----
    _cat = getattr(L, "TOOL_CATALOG", {})
    for cat in ("猪猪", "群聊玩法"):
        _ck(cat in _cat, f"目录分类「{cat}」", "" if cat in _cat else "没加进 TOOL_CATALOG")
    _ck("TOOLS" in L._REQ_RE.pattern, "[[TOOLS]] 标记", "" if "TOOLS" in L._REQ_RE.pattern else "没进 _REQ_RE")

    try:
        import json as _json
        from pathlib import Path as _P
        _sw = _P("data/bittorrents/switch.json")
        _bt = "开" if _json.loads(_sw.read_text(encoding="utf-8")).get("on", True) else "关"
    except Exception:
        _bt = "开（默认，未写过开关文件）"
    _ck(True, f"bt 总开关：{_bt}", "")

    head = (f"共 {sum(1 for l in lines if l.startswith(('✅', '❌')))} 项，"
            f"{n_bad} 项失败")
    if n_bad == 0:
        head += "\n🎉 画图/记忆/表情包/三方 都接进 llm 了。"
    lines.insert(1, head)

    await integ_cmd.finish("\n".join(lines))

