# services/atmosphere.py
"""群聊「读气氛 + 判意图」——接话前先读最近 8 句（2026-11-26 / 11-27）。

用户原话（2026-11-26）：
  「需要对接话功能加个读气氛功能。因为群友气氛很压抑有个人在哭他忽然进来哈哈哈的。
   所以接话前每次先通过读气氛 8 句标签语得到群氛围然后再接话。
   这个气氛可以直接由标签 llm 来输出一个。谁在难过，大概什么心情，其他人什么状态。」

2026-11-27 用户提案（第一版：只加不减）：
  把「读气氛」升级成「意图理解」：同一次调用再输出
  `topic / asks / need_reply / why / replied / keywords / queries{memory,library,web}`，
  用来 ① 驱动记忆/图书馆的检索词；② 让 8 句窗口补上〔已回〕；
  ③ 给主动插话一个**可评测**的门槛（`ATMO_GATE`，默认关，评测 ≥85% 才开）。

为什么不让主模型顺手读：
  · 主模型带着人格/记忆/工具，注意力在「怎么回」，容易只盯着最后一句；
  · 单独一个 temperature=0 的小请求，只读 8 句 + 当前这句，输出都是短字段，便宜且可对照；
  · 单独一次调用才能离线重放（`scripts/eval_intent.py` 用的是**同一条 prompt**）。

边界（很重要）：
  · **只读气氛/意图，不改心情/好感**。心情仍然只来自她的 `[[THINK]]`（ADR-038/042/043）；
  · 这是「提示」，不是「判决」：最终接不接、怎么接仍然是她自己决定；
  · `ATMO_GATE` 只挡「主动插话」这一条路，**不影响 @ 她的正常回复**；默认 0（关）；
  · 8 句里有人在哭/说难受/告别时，prompt 强制要求 mood 落到低落类，
    不许因为别人在刷图就写成「气氛热闹」。
"""
from __future__ import annotations

import json
import re
import time

from core import tuning

try:
    import httpx
except Exception:  # 离线自检 / 没装依赖时也能 import
    httpx = None

try:
    from nonebot import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)

from . import group_scene

ENABLED = bool(tuning.i("ATMO_ENABLED"))
MSGS = max(4, tuning.i("ATMO_MSGS"))
TTL = max(10.0, tuning.f("ATMO_TTL"))
TIMEOUT = max(1.0, tuning.f("ATMO_TIMEOUT"))
MAX_CHARS = max(120, tuning.i("ATMO_MAX_CHARS"))
# 意图字段（topic/asks/need_reply/...）是否渲染进 prompt。默认 1 = 只加不减：
# 老的四行照样在，新的四行追加在后面。
INTENT = bool(tuning.i("ATMO_INTENT"))
# 插话闸门：默认 0（关）。只有 scripts/eval_intent.py 在 204 条 trace 上
# 跑到 need_reply 准确率 ≥85% 之后，才由用户拍板打开。
GATE = bool(tuning.i("ATMO_GATE"))

# gid -> {"key": 最后一行的 mid/文本, "at": 时间, "data": {...}}
_CACHE: dict = {}
# gid -> {"at": 失败时间, "err": "..."}：失败后短暂不再重试，避免刷图时每张图都打一次
_FAIL: dict = {}
_FAIL_COOLDOWN = 60.0

_PROMPT_HEAD = (
    "你是群聊「读气氛 + 判意图」标注员，不参与聊天，只读下面 %d 句群聊"
    "（每行已标好谁→谁；⭐=在叫她；〔已回〕=她已经回过这条），"
    "输出这一刻的群氛围，以及**最后一句**的意图。\n"
    "硬性要求：\n"
    "1) 只依据这些行和【要判断的这句】，不猜前因后果、不编剧情、"
    "不写「信息不足」这类元评论；\n"
    "2) 有人明显难过/在哭/说丧气话/告别/被冷落 → mood 必须是低落类，"
    "绝不能因为别人在刷图就写成「热闹」；\n"
    "3) 别人在斗图/刷屏/互相接话，但没人理某个人 → others 要写清「没人在接谁」；\n"
    "4) need_reply 只判【要判断的这句】：它可能是一整段（多条），"
    "**只要其中任意一条**带 ⭐ / 写着「→ 你」→ yes"
    "（即使最后一条是别人发的图/表情，也要往上找 ⭐ 那条）；\n"
    "   · 带 ⭐ / 「→ 你」= 在跟她说（@她 / 喊她名字 / 接她上一句 / 对她做动作），"
    "**即使不是问句也要接**（「摸摸头」「早」「快乐」都算）；\n"
    "   · **正文里的「@某人」不等于在叫她**：只认行首的 ⭐ / 「→ 你」；"
    "行首写「→ 某人的名字」就是在跟那个人说话（哪怕正文里 @ 了他），→ no；\n"
    "   · 只有箭头**正好**是「→ 你」才是她：「→ 小绒鱼」「→ 大概你」「→ 老大」"
    "都是别人（哪怕名字里带「鱼」/看着像她）；\n"
    "   · 整段里没有任何 ⭐ / → 你（都是「→ 某个群友 / → 像是在回某人 / → 大家」）→ no；\n"
    "   · 她已经回过（〔已回〕）→ no；拿不准 → unsure；\n"
    "5) replied 只判【要判断的这句】**本身**有没有被别人答过"
    "（看它后面的行有没有人接它），不看窗口里其它行；\n"
    "6) queries 是给检索用的**短关键词**，不要整句、不要引号；"
    "memory=她自己的旧事；library=久远往事/书/资料；web=需要现查的事实"
    "（人名/价格/新闻/版本），不需要就留空；\n"
    "7) 看不出来的字段就留空字符串/空数组，不要硬编。\n"
    "只输出 JSON，不要解释、不要 markdown：\n"
    '{"mood":"压抑/低落/尴尬/日常/轻松/热闹（挑一个）",'
    '"who":"最需要被照顾的人名（没有就空）",'
    '"feel":"他/她大概什么心情，10 字内",'
    '"others":"其他人在干什么、什么状态，20 字内",'
    '"tip":"接话时注意什么，20 字内（例：先安慰她，别用哈哈哈开头）",'
    '"topic":"这段在聊什么，12 字内",'
    '"asks":"要判断的这句在问什么/要什么，20 字内（没问就空）",'
    '"need_reply":"yes/no/unsure",'
    '"why":"为什么该/不该接，15 字内",'
    '"replied":"yes/no",'
    '"keywords":["最多 3 个短词"],'
    '"queries":{"memory":"","library":"","web":""}}'
)


def enabled() -> bool:
    return bool(ENABLED and httpx is not None)


def _clip(s, n: int) -> str:
    return str(s or "").strip().replace("\n", " ")[:n]


def _rows(gid: str, exclude_mids=(), replied_mids=()) -> list:
    """最近 8 句「带收发件人标签」的历史（和主模型上下文同源）。

    2026-11-27：把 `replied_mids` 传下去 —— 以前这 8 行没有〔已回〕，
    意图层就无法判断「这句是不是已经答过」。
    """
    try:
        return group_scene.history_lines(
            gid, MSGS, width=100, exclude_mids=exclude_mids,
            replied_mids=replied_mids) or []
    except Exception:
        return []


def _key_of(lines: list, text: str = "") -> str:
    return ("|".join(lines[-2:]) + "||" + str(text or ""))[:220]


_SEG_LINE = re.compile(r"^\[\d+\]\s*")
# 「→ 你」必须刚好是箭头后的收件人：昵称以「你」开头（例：你已被管理员…禁言6小时）
# 会被子串误判成「在叫她」，进而把「1 条在叫你」写进 prompt（2026-11-27 评测抓到）。
_ADDR_RE = re.compile(r"→\s*你(?=[：:]|$)")


def compact_segment(text: str, limit: int = 800) -> str:
    """把「一段 20 条」压成「在叫她的 + 最后 2 条」。

    为什么不能直接截断（2026-11-27 真机评测抓到）：一段 20 条时，
    ⭐ 那条常在第 3~5 条，`text[:800]` 会把它截掉 → 模型看不到「在叫她」，
    只能判 no。压缩后既不会丢 ⭐，也比整段更省 token。
    """
    raw = str(text or "").strip()
    if not raw:
        return ""
    seg = [ln.strip() for ln in raw.splitlines()
           if ln.strip() and _SEG_LINE.match(ln.strip())]
    if len(seg) <= 1:
        return raw[:limit]
    addr = [ln for ln in seg if ("⭐" in ln or _ADDR_RE.search(ln))]
    keep = addr + seg[-2:]
    picked, seen = [], set()
    for ln in seg:                       # 保持时间顺序
        if ln in keep and ln not in seen:
            seen.add(ln)
            picked.append(ln)
    head = (f"（这一段共 {len(seg)} 条，其中 {len(addr)} 条在叫你"
            f"（带 ⭐ / → 你）；下面只列这些和最后 2 条）\n"
            if len(picked) < len(seg) else "")
    return (head + "\n".join(picked))[:limit]


def build_prompt(lines: list, text: str = "") -> str:
    """组装这一次标注的 prompt（read 与 scripts/eval_intent.py 共用）。

    离线评测必须和线上**同一条 prompt**，否则测出来的准确率不能用来开闸门。
    """
    head = _PROMPT_HEAD % MSGS
    body = f"\n\n最近 {MSGS} 句（由旧到新）：\n" + "\n".join(lines[-MSGS:])
    cur = compact_segment(text)
    if cur:
        body += "\n\n【要判断的这句（最新，need_reply/replied 只判它）】\n" + cur
    return head + body


_YN = {
    "yes": "yes", "y": "yes", "是": "yes", "要": "yes", "该": "yes",
    "true": "yes", "1": "yes",
    "no": "no", "n": "no", "否": "no", "不": "no", "不该": "no",
    "不用": "no", "false": "no", "0": "no",
    "unsure": "unsure", "不确定": "unsure", "maybe": "unsure", "?": "unsure",
}


def _yn(v) -> str:
    return _YN.get(str(v or "").strip().lower(), "")


def _parse(text: str) -> dict:
    t = str(text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    try:
        obj = json.loads(t)
    except Exception:
        m = re.search(r"\{.*\}", t, re.S)
        try:
            obj = json.loads(m.group(0)) if m else {}
        except Exception:
            obj = {}
    if not isinstance(obj, dict):
        return {}
    out = {
        "mood": _clip(obj.get("mood"), 12),
        "who": _clip(obj.get("who"), 20),
        "feel": _clip(obj.get("feel"), 30),
        "others": _clip(obj.get("others"), 40),
        "tip": _clip(obj.get("tip"), 40),
        # ---- 2026-11-27 意图字段（老端点只返回上面 5 个时，这里全为空）----
        "topic": _clip(obj.get("topic"), 24),
        "asks": _clip(obj.get("asks"), 40),
        "need_reply": _yn(obj.get("need_reply")),
        "why": _clip(obj.get("why"), 30),
        "replied": _yn(obj.get("replied")),
        "keywords": [],
        "queries": {},
    }
    _kws = obj.get("keywords")
    if isinstance(_kws, str):
        _kws = [_kws]
    for _k in (_kws or [])[:6]:
        _s = _clip(_k, 12)
        if _s and _s not in out["keywords"]:
            out["keywords"].append(_s)
    out["keywords"] = out["keywords"][:3]
    _qs = obj.get("queries")
    if isinstance(_qs, dict):
        for _k in ("memory", "library", "web"):
            _s = _clip(_qs.get(_k), 40)
            if _s:
                out["queries"][_k] = _s
    # 至少要有「整体」/「谁在难过」/「这句在问什么」，否则当没读到
    if not (out["mood"] or out["who"] or out["others"]
            or out["topic"] or out["asks"] or out["need_reply"]):
        return {}
    return out


def need_reply_of(data: dict) -> str:
    """意图层的结论：yes / no / unsure / ""（没读到）。"""
    return _yn((data or {}).get("need_reply"))


def queries_of(data: dict) -> dict:
    """检索词（只返回非空的），给 reply_pipeline 用。"""
    qs = (data or {}).get("queries")
    if not isinstance(qs, dict):
        return {}
    return {k: str(v).strip() for k, v in qs.items()
            if k in ("memory", "library", "web") and str(v or "").strip()}


def gate_on() -> bool:
    """插话闸门是否打开（默认关；见模块 docstring 的评测前提）。"""
    return bool(GATE and ENABLED)


def render(data: dict, n: int = 0) -> str:
    """把气氛 + 意图渲染成 prompt 块（≤MAX_CHARS）。"""
    d = data or {}
    lines = [f"▍群里的气氛（最近 {n or MSGS} 句，标签 LLM 读的；只作提示，不改心情）"]
    if d.get("mood"):
        lines.append(f"· 整体：{d['mood']}")
    if d.get("who"):
        _f = f"，{d['feel']}" if d.get("feel") else ""
        lines.append(f"· 最需要被照顾：{d['who']}{_f}")
    if d.get("others"):
        lines.append(f"· 其他人：{d['others']}")
    if d.get("tip"):
        lines.append(f"· 接话注意：{d['tip']}")
    if INTENT:
        if d.get("topic"):
            lines.append(f"· 在聊：{d['topic']}")
        # 只有真在问事时才写这一行：没问事却写「还没人答」是噪音（真机 17:54 抓到）。
        if d.get("asks"):
            _r = ("，已经有人答了" if d.get("replied") == "yes"
                  else "，还没人答" if d.get("replied") == "no" else "")
            lines.append(f"· 要回的那句：{d['asks']}{_r}")
        _nr = {"yes": "该接", "no": "不用接",
               "unsure": "拿不准（倾向不接）"}.get(need_reply_of(d), "")
        if _nr:
            _why = f"，{d['why']}" if d.get("why") else ""
            lines.append(f"· 该不该接：{_nr}{_why}（只作提示）")
        _qw = queries_of(d).get("web")
        if _qw:
            # 只提示「可能要现查什么」，不替她发请求：要不要搜、搜什么仍由她定。
            lines.append(f"· 可能要现查：{_qw}（要就自己搜，别编）")
    return "\n".join(lines)[:MAX_CHARS]


def cached(gid: str) -> dict:
    """上一次读到的气氛/意图（给诊断/测函数用，不发请求）。"""
    rec = _CACHE.get(str(gid or "")) or {}
    return dict(rec.get("data") or {})


def fresh(gid: str, tail=None) -> dict:
    """只在缓存还没过期、且窗口没变时返回（插话闸门用）。

    `tail`：当前历史的最后 2 行。对不上说明她看过的是另一段对话 →
    宁可返回空（不挡），也不能拿旧结论去闭她的嘴。
    """
    rec = _CACHE.get(str(gid or "")) or {}
    if not rec:
        return {}
    if time.time() - float(rec.get("at") or 0) > TTL:
        return {}
    if tail is not None and list(rec.get("tail") or []) != list(tail):
        return {}
    return dict(rec.get("data") or {})


async def classify(lines: list, text: str = "", *,
                   record_usage: bool = True,
                   purpose: str = "atmosphere",
                   timeout: float = 0.0) -> dict:
    """对一组标签语跑一次「气氛 + 意图」标注（read 与离线评测共用）。

    失败抛异常（read 负责吞掉并降级）；返回已经过 `_parse` 清洗的 dict。
    """
    from . import providers
    p = providers.resolve("audit")
    base = str(p.get("base_url") or "").rstrip("/")
    api_key = str(p.get("api_key") or "")
    model = str(p.get("model") or "")
    if not base or not api_key or not model:
        raise RuntimeError("audit provider 没配置（base/key/model）")
    url = base if base.endswith("/chat/completions") else base + "/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system",
             "content": "你只读群聊气氛和最后一句的意图，不参与聊天，只输出 JSON。"},
            {"role": "user", "content": build_prompt(lines, text)},
        ],
        "temperature": 0,
        **providers.call_tokens("audit", 320),
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {api_key}",
               "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=float(timeout or TIMEOUT)) as client:
        try:
            r = await client.post(url, headers=headers, json=payload)
            r.raise_for_status()
        except Exception:
            # 有些兼容端点不认 response_format
            payload.pop("response_format", None)
            r = await client.post(url, headers=headers, json=payload)
            r.raise_for_status()
        body = r.json()
    if record_usage:
        try:
            from . import llm_usage as _lu
            _lu.record_from_response(body, purpose=purpose,
                                     provider=str(p.get("provider") or "audit"),
                                     model=model)
        except Exception:
            pass
    content = ((body.get("choices") or [{}])[0]
               .get("message", {}).get("content") or "")
    return _parse(content)


async def read(gid: str, *, exclude_mids=(), replied_mids=(),
               text: str = "", force: bool = False) -> dict:
    """读一次群气氛 + 意图；带 TTL 缓存与并发去重，失败返回 {}（静默降级）。

    `text`：当前要回的这一句（分段文本）。不传就只判历史最后一句 ——
    主链路一定要传，否则 `need_reply` 判的不是她要回的那条。
    """
    gid = str(gid or "").strip()
    if not gid or not enabled():
        return {}

    lines = _rows(gid, exclude_mids=exclude_mids, replied_mids=replied_mids)
    if len(lines) < 2:
        return {}
    _cur = str(text or "").strip()
    key = _key_of(lines, _cur)
    now = time.time()

    rec = _CACHE.get(gid) or {}
    if (not force and rec.get("key") == key
            and now - float(rec.get("at") or 0) < TTL):
        return dict(rec.get("data") or {})

    fail = _FAIL.get(gid) or {}
    if not force and now - float(fail.get("at") or 0) < _FAIL_COOLDOWN:
        return dict(rec.get("data") or {})

    try:
        data = await classify(lines, _cur)
        if not data:
            _FAIL[gid] = {"at": now, "err": "空/无法解析"}
            return dict(rec.get("data") or {})
        _CACHE[gid] = {"key": key, "at": now, "data": data,
                       "tail": list(lines[-2:])}
        _FAIL.pop(gid, None)
        logger.info(
            f"[ATMO] {gid} 气氛={data.get('mood') or '?'}"
            f"｜{data.get('who') or '—'}：{data.get('feel') or '—'}"
            f"｜意图={need_reply_of(data) or '?'}"
            f"（{data.get('why') or '—'}）｜{data.get('tip') or '—'}")
        return dict(data)
    except Exception as e:
        _FAIL[gid] = {"at": now, "err": f"{type(e).__name__}: {e}"}
        logger.debug(f"[ATMO] {gid} 读气氛失败: {type(e).__name__}: {e}")
        return dict(rec.get("data") or {})


def status(gid: str = "") -> str:
    """`/测函数 气氛` 用：启用状态 + 上次读到什么。"""
    gid = str(gid or "").strip()
    head = (f"· 读气氛：{'启用' if enabled() else '关闭'}"
            f"｜{MSGS} 句｜TTL {TTL:.0f}s｜超时 {TIMEOUT:.0f}s"
            f"｜意图字段 {'开' if INTENT else '关'}"
            f"｜插话闸门 {'开' if gate_on() else '关（默认；评测≥85% 再开）'}")
    if not gid:
        return head
    rec = _CACHE.get(gid) or {}
    if not rec:
        return head + f"\n· {gid}：还没读过"
    d = rec.get("data") or {}
    age = time.time() - float(rec.get("at") or 0)
    _q = queries_of(d)
    _qline = ("\n· 检索词：" + "｜".join(f"{k}={v}" for k, v in _q.items())) if _q else ""
    return (head + f"\n· {gid}：{age:.0f}s 前｜{render(d)}" + _qline)
