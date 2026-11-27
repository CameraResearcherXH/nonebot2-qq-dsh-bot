# services/scene_auditor.py
"""
群聊“收件人审核员”（用户 2026-11-19）。

定位
----
**不是角色扮演，不参与聊天。**
硬逻辑（group_scene）先快速判断「谁 → 谁」，这个模块再开一个独立 LLM 对话，
只做标注审核：
  · 输入：一段只有「发送者: 内容」的群聊记录；
  · 输出：每一句是 对某个成员 / 对大家 / 自言自语 / 不确定；
  · 回写 group_scene，纠正硬逻辑、补齐不确定项。

为什么不让主模型顺手做：
  主模型带着人格、记忆、工具，注意力在“怎么回”上；让它同时做标注会互相污染。
  单独一个 temperature=0 的小请求，便宜、稳定、可对照。

成本控制
--------
· 只审 `group_scene.needs_audit()` 选出来的（大家/不确定/低置信），
  @ / 引用 / 开头称呼 这些强证据不审；
· 每个群最少间隔 SCENE_AUDIT_MIN_INTERVAL 秒；
· 审过的消息打 `audited=True`，不重复审；
· 失败/超时一律静默退回硬逻辑，不影响聊天。
"""
from core import tuning

import json
import os
import re
import time

try:
    import httpx
except Exception:  # 离线自检/没装依赖时也能 import，只是不启用
    httpx = None

try:
    from nonebot import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)

from .config import BOT_MODE, PROXY, XAI_API_KEY, XAI_BASE_URL, XAI_MODEL
from . import group_scene

ENABLED = os.getenv("SCENE_AUDIT_ENABLED", "true").lower() in ("1", "true", "yes", "on")
# 2026-11-24：审核模型由 providers 决定（AUDIT_PROVIDER / AUDIT_MODEL）
from .providers import (resolve as _resolve_provider,
                        call_tokens as _call_tokens)  # noqa: E402

_AUDIT_P = _resolve_provider("audit")
MODEL = _AUDIT_P["model"]
# 下游代码沿用 XAI_* 这三个名字，但值已经换成「audit 角色」解析出来的
XAI_API_KEY = _AUDIT_P["api_key"]
XAI_BASE_URL = _AUDIT_P["base_url"].rstrip("/")
XAI_MODEL = _AUDIT_P["model"]
TIMEOUT = tuning.f("SCENE_AUDIT_TIMEOUT")
# 默认 20 条：用户 2026-11-19 ——「A：新番看了吗」……10 分钟后「B：对啊，都是原作的锅」，
# 这种中间没人说话的对话，按时间窗拉会拆成两句，审核 LLM 根本没法判断。
# 所以窗口按**条数**，且不低于 20（.env 里写小了也不生效，只能往大写）。
# ⚠️ 这里只做「群摘要」；心情/好感不看这段对话，只看她的 [[THINK]]（ADR-038）。
MAX_MSGS = max(20, tuning.i("SCENE_AUDIT_MAX_MSGS"))
MIN_INTERVAL = tuning.f("SCENE_AUDIT_MIN_INTERVAL")
# 审核同时给一段「这段在聊什么」的滚动总结（用户 2026-11-19，像 QQ 元宝总结）
SUMMARY_ENABLED = os.getenv("SCENE_SUMMARY_ENABLED", "true").lower() in ("1", "true", "yes", "on")
SUMMARY_TTL = tuning.f("SCENE_SUMMARY_TTL")
SUMMARY_MAX_CHARS = tuning.i("SCENE_SUMMARY_MAX_CHARS")

# 版本号：/测函数 审核 第一行会打印。
# 用户 2026-11-19 实测「审：未审核」查了半天，最后发现是**容器里跑的是旧文件**——
# 有这个号，一眼就能确认「服务器上到底是哪一版」，不用再靠猜。
# 这个号是「现场/审核这一套」的批次标记：改 chat/persona/group_scene/profile_card
# 里任何一处相关逻辑都要跟着 +1，这样 `/测函数 审核` 一跑就知道服务器同步到没。
VERSION = "2026-11-20l"

_LAST_AUDIT: dict = {}
# 诊断用：/测函数 现场 / 审核 会打印这些，定位「为什么没审」
_LAST_ERROR = ""
_LAST_RAW = ""
_LAST_TODO = 0
_LAST_ROWS = 0
_LAST_ITEMS = 0
_LAST_CHANGED = 0
_LAST_SUMMARY = 0
_LAST_SKIP = ""
_LAST_ELAPSED = 0.0
_LAST_AT = 0.0
# 失败日志限流：同一个群 60 秒最多 WARNING 一次（以前是「整个进程只报一次」，
# 结果 24h 跑下来第二次之后全静默，等于没有日志）
_FAIL_LOG: dict = {}


def enabled() -> bool:
    if not ENABLED or BOT_MODE == "mock" or not XAI_API_KEY or httpx is None:
        return False
    return True


def status(brief: bool = False, gid: str = "") -> str:
    """
    给 /测函数 现场 用：为什么没审 / 上次审成什么样。

    brief=True：图片卡片用，只留「版本 + 启用 + 模型」，别让一行占满整张卡。
    gid 给了就再打一行**现在**的窗口大小 —— 以前只有「上次审核：窗口 1 条」，
    重启后看着像「窗口永远是 1 条」，分不清是没补回来还是上次审得早。
    """
    why = []
    if not ENABLED:
        why.append("SCENE_AUDIT_ENABLED=false")
    if BOT_MODE == "mock":
        why.append("BOT_MODE=mock")
    if not XAI_API_KEY:
        why.append("XAI_API_KEY 空")
    if httpx is None:
        why.append("httpx 没装")
    if brief and not why:
        head = f"审核 v{VERSION}：启用｜model={MODEL or '(空)'}"
    else:
        head = (
            f"审核 v{VERSION}：{'启用' if enabled() else '关闭'}"
            f"｜BOT_MODE={BOT_MODE}"
            f"｜key={'有' if XAI_API_KEY else '无'}"
            f"｜httpx={'有' if httpx is not None else '无'}"
            f"｜model={MODEL or '(空)'}"
        )
        if why:
            head += "｜原因：" + "、".join(why)
    gid = str(gid or "").strip()
    if gid:
        try:
            _rows = group_scene.recent_messages(gid, MAX_MSGS)
            _n = len(_rows)
            _mine = sum(1 for _m in _rows if _m.get("is_bot"))
            _todo = len(group_scene.needs_audit(gid, MAX_MSGS))
            _seed = group_scene.seeded_count(gid)
            # 「你 3 句」必须写出来：用户 2026-11-19 看到审核列表里没有「你」，
            # 以为她自己的话没进审核（其实进了，只是列表只显示最后 6 条）。
            head += (f"\n现在：窗口 {_n} 条（你自己 {_mine} 句，也一起给审核看）"
                     f"｜待审 {_todo} 条"
                     + (f"｜日志补回 {_seed} 条" if _seed else ""))
        except Exception:
            pass
    if _LAST_AT:
        head += (
            f"\n上次审核（{time.strftime('%H:%M:%S', time.localtime(_LAST_AT))}"
            f"｜{_LAST_ELAPSED:.1f}s）：窗口 {_LAST_ROWS} 条｜待审 {_LAST_TODO}"
            f" → 改写 {_LAST_CHANGED}｜返回 {_LAST_ITEMS} 条"
            f"｜总结{'有' if _LAST_SUMMARY else '无'}"
        )
    if _LAST_SKIP:
        head += f"\n跳过：{_LAST_SKIP[:160]}"
    if _LAST_ERROR:
        head += f"\n错误：{_LAST_ERROR[:200]}"
    if _LAST_RAW and not _LAST_CHANGED:
        head += f"\n返回原文：{_LAST_RAW[:200]}"
    return head


def _log_fail(gid: str, msg: str) -> None:
    """失败日志限流：同一个群 60 秒最多 WARNING 一次，其余降级成 DEBUG。"""
    now = time.time()
    if now - float(_FAIL_LOG.get(gid) or 0) >= 60:
        _FAIL_LOG[gid] = now
        logger.warning(f"[SCENE-AUDIT] {gid} 审核失败（退回硬逻辑）: {msg}")
    else:
        logger.debug(f"[SCENE-AUDIT] {gid} 审核失败: {msg}")


def _gap_text(sec: float) -> str:
    sec = max(0.0, float(sec or 0))
    if sec < 3600:
        return f"{int(sec // 60)} 分钟"
    if sec < 86400:
        return f"{int(sec // 3600)} 小时"
    return f"{int(sec // 86400)} 天"


def _build_prompt(rows: list, todo_idx: set, bot_name: str, names: list) -> str:
    member_line = "、".join([n for n in names if n][:30])
    lines = [
        "你是群聊“收件人审核员”，不参与聊天，只做标注。",
        "",
        f"群成员：{member_line}" + (f"（{bot_name} 是机器人）" if bot_name else ""),
        "下面是一段 QQ 群聊，每行格式：编号. 发送者: 内容",
        "【待判断】标记的句子需要你判断“是对谁说的”。",
        "",
    ]
    prev_ts = 0.0
    for i, m in enumerate(rows, 1):
        who = str(m.get("name") or m.get("qq") or "?")
        # 她自己说的话也要进审核（当上下文）：不标清楚的话，模型会把「你:」
        # 当成某个叫「你」的群友，或者干脆以为她没说话（用户 2026-11-19）。
        if m.get("is_bot"):
            who = "你（机器人自己）"
        text = str(m.get("text") or "").replace("\n", " ")[:160]
        mark = "   【待判断】" if i in todo_idx else ""
        # 隔了很久才接一句，必须让审核看见（用户 2026-11-19：两句隔 10 分钟，
        # 在 LLM 眼里就是两句莫名其妙的话）
        gap = ""
        try:
            ts = float(m.get("ts") or 0)
            if prev_ts and ts - prev_ts >= 120:
                gap = f"（距上一句 {_gap_text(ts - prev_ts)}）"
            prev_ts = ts or prev_ts
        except Exception:
            pass
        lines.append(f"{i}. {who}{gap}: {text}{mark}")
    lines += [
        "",
        "判断规则：",
        "· 对某个成员说 → 写成员名字（必须在群成员里，不要写 QQ 号）；",
        "· 对大家说 → 写“大家”；",
        "· 自言自语 / 没人接 → 写“自己”；",
        "· 真看不出来 → 写“不确定”。",
        "依据：问答关系、接话语气、有没有称呼、话题是否连续、时间顺序。",
        "注意：",
        "· 带「距上一句 X 分钟」只是隔了一段时间，**不代表换话题**——"
        "很可能还是在接前面那句（群里没人说话很正常）；",
        "· 正文提到某人名字，不代表在叫他（可能只是提到）；",
        "· 不要因为上一句是谁说的就机械认定，要看内容是否在回应；",
        "· 行首是「你（机器人自己）」的是机器人说的话，只当上下文，不用判断收件人；",
        "· 「找打了」「欠揍」「你完了」「他咋这样」这种**冲着某个人**的短句："
        "先看前面几条里最近被提到 / 被吐槽的是谁 —— 收件人更可能是**那个人**，"
        "而不是紧挨着的上一句发送者（用户 2026-11-19：「找打了」其实是对「肥鱼」说的）；",
        "· 拿不准就写“不确定”，不要硬猜。",
        "",
        f"另外，用 1~2 句中文总结这段群聊**在聊什么**（谁和谁、主题、有没有在问什么），"
        f"控制在 {SUMMARY_MAX_CHARS} 字以内。",
        "总结只写事实：谁在说什么、在问什么。不要逐条复述、不要评价，"
        "也不要写「缺少上下文」「无法确定对象」「信息不足」这类元评论——"
        "看不出来就只写你看到的话题（例：两人在聊单机游戏，一个说只玩 3A）。",
        "数字/单位按原话保留：192 就写「他说自己 192」，不要自己补成「192 关 / 192 级 / 192 分」；"
        "拿不准单位就写「（没说是什么）」。",
        "",
        "只输出 JSON，不要解释、不要 markdown：",
        '{"summary":"大家在聊家庭组，小红在招人，老王说自己懒得退组",'
        '"items":[{"i":2,"to":"阿明","why":"回答上一句的胃疼"}]}',
    ]
    return "\n".join(lines)


def _parse_json(text: str) -> dict:
    t = str(text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except Exception:
        m = re.search(r"\{.*\}", t, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return {}


async def audit_recent(gid: str, *, bot_qq: str = "", bot_name: str = "",
                       names: dict = None, limit: int = 0,
                       force: bool = False) -> int:
    """
    审核最近一段群聊，返回实际改写的条数；失败返回 0（静默）。
    """
    global _LAST_ERROR, _LAST_RAW, _LAST_TODO, _LAST_ROWS, _LAST_ITEMS
    global _LAST_CHANGED, _LAST_SUMMARY, _LAST_SKIP, _LAST_ELAPSED, _LAST_AT
    gid = str(gid or "").strip()
    if not gid or not enabled():
        _LAST_ERROR = "disabled: " + status().replace("\n", " ")
        return 0

    # 2026-11-27 日预算刹车：审核占全天 token 的 ~7%，额度用完就不审
    # （收发件人标签退化成硬逻辑，聊天本身不受影响）。
    try:
        from . import budget_guard as _bg
        if _bg.state() == "rest":
            _LAST_SKIP = "今日额度用完（LLM_DAILY_BUDGET）→ 不审"
            return 0
    except Exception:
        pass

    now = time.time()
    if not force and now - float(_LAST_AUDIT.get(gid) or 0) < MIN_INTERVAL:
        return 0

    t0 = time.time()
    limit = int(limit or MAX_MSGS)
    rows = group_scene.recent_messages(gid, limit)
    if not rows:
        return 0
    todo = group_scene.needs_audit(gid, limit)
    need_summary = bool(SUMMARY_ENABLED and group_scene.summary_stale(gid, SUMMARY_TTL))
    _LAST_ROWS = len(rows)
    _LAST_TODO = len(todo)
    _LAST_ITEMS = 0
    _LAST_CHANGED = 0
    _LAST_SUMMARY = 0
    _LAST_SKIP = ""
    _LAST_ELAPSED = 0.0
    _LAST_ERROR = ""
    _LAST_RAW = ""
    _LAST_AT = now
    # 没有待判、总结也没过期 → 什么都不用做
    if not todo and not need_summary:
        return 0

    todo_mids = {str(m.get("mid") or "") for m in todo}
    todo_idx = {
        i for i, m in enumerate(rows, 1)
        if str(m.get("mid") or "") in todo_mids
    }

    names = dict(names or {})
    member_names = []
    for nm in names.keys():
        nm = str(nm or "").strip()
        # 单字昵称（「雫」这种）也要留在名单里：审核只能从这份名单里挑人，
        # 名单里没有，它就写「大家」或者写个对不上的名字。
        if nm and not nm.isdigit() and nm not in member_names:
            member_names.append(nm)
    if bot_name and bot_name not in member_names:
        member_names.append(bot_name)

    prompt = _build_prompt(rows, todo_idx, bot_name, member_names)
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "你只做群聊收件人标注，不参与聊天。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        **_call_tokens("audit", 900),
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {XAI_API_KEY}", "Content-Type": "application/json"}
    kwargs = {"timeout": TIMEOUT}
    if PROXY:
        kwargs["proxy"] = PROXY

    _LAST_AUDIT[gid] = now
    content = ""
    try:
        async with httpx.AsyncClient(**kwargs) as client:
            try:
                r = await client.post(f"{XAI_BASE_URL}/chat/completions",
                                      headers=headers, json=payload)
                r.raise_for_status()
            except Exception:
                # 有些兼容端点不认 response_format，去掉再试一次
                payload.pop("response_format", None)
                r = await client.post(f"{XAI_BASE_URL}/chat/completions",
                                      headers=headers, json=payload)
                r.raise_for_status()
            data = r.json()
            try:
                from . import llm_usage as _lu
                _lu.record_from_response(data, purpose="audit", provider="audit", model=MODEL)
            except Exception:
                pass
            content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    except Exception as e:
        _LAST_ELAPSED = time.time() - t0
        _LAST_ERROR = f"{type(e).__name__}: {e}｜{MODEL}@{XAI_BASE_URL}"
        _log_fail(gid, _LAST_ERROR)
        return 0

    obj = _parse_json(content)
    # 总结：不管有没有待判，只要这次要了总结就写回
    summary = str(obj.get("summary") or "").strip()[:SUMMARY_MAX_CHARS]
    if summary:
        group_scene.set_summary(
            gid, summary, until_mid=str(rows[-1].get("mid") or ""))
        _LAST_SUMMARY = 1

    items = obj.get("items") or []
    if not isinstance(items, list):
        items = []
    _LAST_ITEMS = len(items)
    results = []
    if todo:
        for it in items:
            if not isinstance(it, dict):
                continue
            try:
                i = int(it.get("i") or it.get("index") or 0)
            except Exception:
                continue
            if i < 1 or i > len(rows):
                continue
            mid = str(rows[i - 1].get("mid") or "")
            if not mid:
                continue
            # 只改「待判」的：@ / 引用 / 开头称呼 / 高置信 这些硬证据不让审核碰，
            # 否则模型一句「大家」就能把「他在 @ 你」改掉。
            if mid not in todo_mids:
                continue
            results.append({
                "mid": mid,
                "to": str(it.get("to") or "").strip(),
                "why": str(it.get("why") or it.get("reason") or "").strip(),
            })

    if not results:
        _LAST_ELAPSED = time.time() - t0
        if summary:
            logger.info(f"[SCENE-AUDIT] {gid} 只更新总结：{summary[:60]}")
            return 0
        _LAST_RAW = str(content or "")[:400]
        _LAST_SKIP = "返回里没有可用的 items（没标 i 或 i 越界）"
        logger.info(f"[SCENE-AUDIT] {gid} 审核返回空（{len(todo)} 条待判）：{_LAST_RAW[:120]}")
        return 0

    # 审核窗口里看不到硬逻辑认定的那个人时，不要把 person 改成「大家/自己」——
    # 那通常不是审核更准，而是它没看到前情（实测：回的是 10 条之外那句）。
    by_mid = {str(m.get("mid") or ""): m for m in rows}
    visible = {str(m.get("qq") or "") for m in rows if not m.get("is_bot")}
    for r in results:
        m = by_mid.get(str(r.get("mid") or ""))
        if not m:
            continue
        if str(r.get("to") or "") in ("大家", "自己", "group", "self"):
            hk = str(m.get("hard_kind") or "")
            hq = str(m.get("to_qq") or "")
            if hk == "person" and hq and hq not in visible:
                r["to"] = "不确定"
                r["why"] = (str(r.get("why") or "") + "（目标不在审核窗口）")[:60]

    notes = []
    changed = group_scene.apply_audit(
        gid, results, names=names, bot_qq=bot_qq, bot_name=bot_name, notes=notes)
    _LAST_CHANGED = int(changed)
    _LAST_SKIP = "；".join([n for n in notes if n])[:200]
    _LAST_RAW = "" if changed else str(content or "")[:400]
    _LAST_ELAPSED = time.time() - t0
    _LAST_AT = time.time()
    logger.info(
        f"[SCENE-AUDIT] {gid} 审核 {len(todo)} 条（返回 {_LAST_ITEMS} 条），改写 {changed} 条"
        + (f"，跳过：{_LAST_SKIP}" if _LAST_SKIP else "")
        + (f"，总结：{summary[:50]}" if summary else "")
    )
    return changed


async def diagnose(gid: str, *, bot_qq: str = "", bot_name: str = "",
                   names: dict = None) -> str:
    """
    给 /测函数 审核 用：强制跑一次，把「发了什么 / 回了什么 / 改了几条」摊开。
    用户 2026-11-19：现场一直「审：未审核」，光看日志看不出是没启用、没待判、
    调用失败还是名字对不上——这条命令一次说清。
    """
    gid = str(gid or "").strip()
    if not gid:
        return "🩺 审核自检\n· 只能在群里用。"
    t0 = time.time()
    changed = await audit_recent(gid, bot_qq=bot_qq, bot_name=bot_name,
                                 names=names, force=True)
    # ⚠️ 窗口/待判要在审核**之后**数：现场为空时 audit_recent 会先从日志补回来，
    #    提前数就会打印「窗口 1 条」这种看起来像 bug 的数字。
    rows = group_scene.recent_messages(gid, MAX_MSGS)
    todo = group_scene.needs_audit(gid, MAX_MSGS)
    _seed = group_scene.seeded_count(gid)
    lines = [
        "🩺 审核自检",
        status(gid=gid),
        f"· 窗口 {len(rows)} 条｜待判 {len(todo)} 条｜本次改写 {changed} 条"
        f"（{time.time() - t0:.1f}s）"
        + (f"｜日志补回 {_seed} 条" if _seed else ""),
    ]
    if not enabled():
        lines.append("· 审核没启用（原因见上），聊天会一直用硬逻辑。")
    elif not todo:
        lines.append("· 没有待判消息：@ / 引用 / 高置信本来就不审，这是正常的。")
    elif _LAST_ERROR:
        lines.append("· 调用失败 → 聊天继续用硬逻辑；先查 key / 端点 / 代理。")
    elif _LAST_SKIP:
        lines.append(f"· 有返回但没写回：{_LAST_SKIP[:120]}")
    return "\n".join(lines)
