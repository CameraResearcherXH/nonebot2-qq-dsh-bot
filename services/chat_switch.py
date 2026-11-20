# services/chat_switch.py
"""
「她想安静就安静」—— CHAT_OFF / CHAT_ON。

用户 2026-11-19：
  「她可以根据心情来选择某天某时某刻安静下来。等心情好了她也可以自己打开开关。
    当然她安静下来也可以听到所有内容。以免着急找不到她。她有沉默安静的权利。」

设计要点：
  1. **照常读消息**：开关只影响「发不发」，不影响上下文/记忆/心情。
     所以她安静的时候仍然在看、在记，回头能接上话。
  2. 开关按**群**和**人**两种粒度：`groups[gid]` / `users[qq]`。
     群里某个人被关了 → 只忽略他；整个群被关了 → 群里谁都不回。
  3. 支持**限时**（`until`）：她说「安静 30 分钟」，到点自动恢复，
     不需要她记得回来开。
  4. 状态落盘 `data/chat_switch.json`，重启不丢。
  5. **对某个人安静 = strict**（2026-11-20）：她不想理某个人时，那个人的 @
     和图片也不接（否则「安静」等于没安静）；群里其他人不受影响。
     整群安静默认 non-strict —— 有人直接叫她，还给她台阶。

⚠️ 这里只管「回不回」。真正让她闭嘴的方式是在 function calling 里
   强制 `stay_silent`（见 services/llm.py），这样她**照样读完上下文**，
   只是被要求「这次不回复」—— 而不是整条链路跳过她。
"""
from core import tuning
import json
import os
import time
from pathlib import Path

STATE_FILE = Path(os.getenv("CHAT_SWITCH_FILE", "data/chat_switch.json"))

# 单次最长能安静多久（小时）。0 = 不限制
MAX_HOURS = tuning.f("CHAT_OFF_MAX_HOURS")
# 安静时是否仍然调用 LLM（强制 stay_silent）。
#   true  = 字面行为：她照常「想」，只是被要求不说话（费 token）
#   false = 省 token：只记上下文，不调模型
LLM_WHILE_OFF = os.getenv("CHAT_OFF_LLM", "true").lower() in ("1", "true", "yes", "on")
# 安静状态下，有人直接 @ / 喊名字时给不给她「想回就回」的选择权。
#   true  = 给（免得「着急找不到她」）
#   false = 一视同仁，安静就是安静
ANSWER_WHEN_CALLED = os.getenv("CHAT_OFF_ANSWER_WHEN_CALLED", "true").lower() in (
    "1", "true", "yes", "on")

_cache: dict = {"mtime": 0.0, "data": {"groups": {}, "users": {}}}

# 最近一次「破冰」的时间：他道歉/哄她 → 安静解除。
# 安静建议要用它：刚破冰就别马上又建议「对他安静」，给台阶给到底。
_THAW: dict = {}


# ==================== 读写 ====================
def _load() -> dict:
    try:
        m = STATE_FILE.stat().st_mtime
    except Exception:
        _cache["mtime"], _cache["data"] = 0.0, {"groups": {}, "users": {}}
        return _cache["data"]
    if m != _cache["mtime"]:
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8") or "{}")
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault("groups", {})
        data.setdefault("users", {})
        _cache["data"] = data
        _cache["mtime"] = m
    return _cache["data"]


def _save(data: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        try:
            _cache["mtime"] = STATE_FILE.stat().st_mtime
        except Exception:
            _cache["mtime"] = 0.0
        _cache["data"] = data
    except Exception:
        pass


def _norm_key(session_key: str) -> tuple:
    """'group_123' → ('group','123')；'private_456' → ('user','456')；纯数字 → ('user', x)。"""
    k = str(session_key or "").strip()
    if k.startswith("group_"):
        return "group", k[6:]
    if k.startswith("private_"):
        return "user", k[8:]
    return ("group", k) if k.isdigit() and len(k) > 6 else ("user", k)


def _alive(rec: dict) -> bool:
    if not isinstance(rec, dict):
        return False
    until = float(rec.get("until") or 0)
    return until <= 0 or until > time.time()


def _bucket(kind: str) -> str:
    """
    「群 / 人」→ 存储桶名。

    ⚠️ 这里必须同时认中文和英文两种写法：
      · 工具层传的是「群」「人」；
      · `_norm_key()` 内部返回的是 "group" / "user"。
    以前 set_on/set_off 只认中文，而 maybe_thaw 传的是英文 → 破冰时去
    `users` 里删群号，永远删不掉（自检抓到的真 bug：返回"已解除"但状态没变）。
    """
    return "groups" if str(kind or "").strip().lower() in (
        "群", "group", "groups", "g") else "users"


# ==================== 查询 ====================
def is_off(session_key: str, speaker_qq: str = "") -> tuple:
    """
    这个会话 / 这个人现在是不是安静的。返回 (bool, 原因)。

    群聊里**同时看群和个人**：整个群被关了，或者说话这个人被关了，都算安静。
    """
    data = _load()
    kind, ident = _norm_key(session_key)
    hits = []
    rec = (data.get("groups") or {}).get(ident)
    if kind == "group" and rec and _alive(rec):
        hits.append(("群", rec))
    uid = str(speaker_qq or "").strip()
    # 私聊会话 key 里就带着对方的 QQ —— 不传 speaker 也应该能查到「我关了这个人」
    if not uid and kind == "user":
        uid = ident
    if uid:
        urec = (data.get("users") or {}).get(uid)
        if urec and _alive(urec):
            hits.append(("人", urec))
    if not hits:
        return False, ""
    who, rec = hits[0]
    why = str(rec.get("reason") or "").strip()
    until = float(rec.get("until") or 0)
    tail = f"，到 {time.strftime('%m-%d %H:%M', time.localtime(until))} 自动恢复" if until else ""
    # 「被谁惹的」也说出来：她需要知道自己为什么不想说话（而不是莫名其妙不理人）
    _by = str(rec.get("by_qq") or "").strip()
    _byname = ""
    if _by:
        try:
            from . import profiles
            _byname = profiles.call_of(_by, "") or _by
        except Exception:
            _byname = _by
    if _byname:
        why = (why + f"（起因：{_byname}）") if why else f"起因：{_byname}"
    return True, (f"{who}：{why}" if why else who) + tail


def is_strict(session_key: str, speaker_qq: str = "") -> bool:
    """
    这次安静是不是「强制」的（连 @ / 喊名字都不回）。

    用户 2026-11-20：在群里说「让她安静一小时」，她口头答应了，过一会儿又说话。
    两个原因：① 她可能只是嘴上答应、没真调工具；② 真调了，但
    `CHAT_OFF_ANSWER_WHEN_CALLED=true` 时被 @ 仍会回。
    → 主人用 /安静 下的命令一律 strict=True；她自己调的默认 false。
    """
    data = _load()
    kind, ident = _norm_key(session_key)
    hits = []
    rec = (data.get("groups") or {}).get(ident)
    if kind == "group" and rec and _alive(rec):
        hits.append(rec)
    uid = str(speaker_qq or "").strip()
    if not uid and kind == "user":
        uid = ident
    if uid:
        urec = (data.get("users") or {}).get(uid)
        if urec and _alive(urec):
            hits.append(urec)
    return any(bool((r or {}).get("strict")) for r in hits)


def off_targets() -> list:
    """当前所有安静的群 / 人：[(kind, id, rec)]，顺手清掉过期的。"""
    data = _load()
    out, dirty = [], False
    for bucket, kind in (("groups", "群"), ("users", "人")):
        for ident, rec in list((data.get(bucket) or {}).items()):
            if _alive(rec):
                out.append((kind, ident, rec))
            else:
                data[bucket].pop(ident, None)
                dirty = True
    if dirty:
        _save(data)
    return out


def set_off(kind: str, ident: str, minutes: float = 0, reason: str = "",
            by: str = "self", by_qq: str = "", strict: bool = False) -> dict:
    """
    关掉某个群 / 某个人。minutes<=0 表示一直安静（直到她自己开）。

    by_qq：如果是「被某个人惹到才不想说话」，记下是谁 ——
    破冰时（他道歉了）就能自动解除，而不是一直冷战。
    """
    data = _load()
    bucket = _bucket(kind)
    mins = max(0.0, float(minutes or 0))
    if MAX_HOURS > 0:
        mins = min(mins, MAX_HOURS * 60) if mins > 0 else mins
    rec = {
        "until": (time.time() + mins * 60) if mins > 0 else 0,
        "reason": str(reason or "").strip()[:60],
        "at": time.time(),
        "by": by,
        "by_qq": str(by_qq or "").strip(),
        # strict=True：连「被 @ / 喊名字」都不回（主人明确要求「安静一小时」时用）。
        # 她自己心情不好调的 off 默认 strict=False —— 被叫了还给个台阶。
        "strict": bool(strict),
    }
    data.setdefault(bucket, {})[str(ident)] = rec
    _save(data)
    return rec


# 破冰关键词：对方道歉/哄她 → 她可以消气（不是必须，只给她台阶）
_THAW_RE = None


def _thaw_re():
    global _THAW_RE
    if _THAW_RE is None:
        import re as _re
        _THAW_RE = _re.compile(
            r"对不起|抱歉|别生气|不生气|我错了|是我不对|原谅|消消气|哄你|"
            r"不该那么说|不逗你了|和好|sorry", _re.I)
    return _THAW_RE


def maybe_thaw(session_key: str, speaker_qq: str = "", text: str = "") -> str:
    """
    他道歉/哄她了 → 自动解除安静。返回被解除的那条原因（没解除返回 ""）。

    用户 2026-11-19：「她肯定不会无缘无故不想说话，总有原因，
    用户也可以道歉和主动去和好来破冰。」

    ⚠️ 只解除「因这个人而起」的安静（by_qq 为空表示整个群惹的，谁道歉都算台阶）；
       主人道歉一律给台阶。
    """
    off, why = is_off(session_key, speaker_qq)
    if not off or not text:
        return ""
    if not _thaw_re().search(str(text)):
        return ""
    data = _load()
    kind, ident = _norm_key(session_key)
    rec = None
    if kind == "group":
        rec = (data.get("groups") or {}).get(ident)
    if not rec:
        rec = (data.get("users") or {}).get(str(speaker_qq or ""))
    if not rec or not _alive(rec):
        return ""
    _by = str(rec.get("by_qq") or "")
    if _by and speaker_qq and _by != str(speaker_qq):
        try:
            from . import access
            if not access.is_owner(speaker_qq):
                return ""
        except Exception:
            return ""
    set_on(kind, ident)
    if _by and str(speaker_qq or "") == _by:
        set_on("人", _by)
    # 记一笔「刚破冰」：安静建议会据此降权（他道歉了，别再火上浇油）。
    _THAW[str(session_key)] = {"at": time.time(), "why": why or "",
                               "qq": str(speaker_qq or "")}
    return why or "（没写原因）"


def recent_thaw(session_key: str, within: float = 1800) -> dict:
    """最近 within 秒内有没有破冰过（他道歉/哄她）。返回 {at, why, qq} 或 {}。"""
    rec = _THAW.get(str(session_key or "")) or {}
    try:
        if rec and time.time() - float(rec.get("at") or 0) <= float(within):
            return dict(rec)
    except Exception:
        pass
    return {}


def set_on(kind: str, ident: str) -> bool:
    data = _load()
    bucket = _bucket(kind)
    had = bool((data.get(bucket) or {}).pop(str(ident), None))
    if had:
        _save(data)
    return had


def clear_all() -> int:
    data = _load()
    n = len(data.get("groups") or {}) + len(data.get("users") or {})
    data["groups"], data["users"] = {}, {}
    _save(data)
    return n


# ==================== 给 prompt / 工具用 ====================
def render() -> str:
    """给 system prompt 的一段「你现在安静着」。没在安静就返回空。"""
    rows = off_targets()
    if not rows:
        return ""
    lines = []
    for kind, ident, rec in rows[:8]:
        until = float(rec.get("until") or 0)
        when = (f"到 {time.strftime('%m-%d %H:%M', time.localtime(until))}"
                if until else "直到你自己开")
        why = str(rec.get("reason") or "").strip()
        lines.append(f"· {kind} {ident}：{why or '没写原因'}（{when}）")
    return ("你现在对这些地方是**安静**状态（只读不回）：\n"
            + "\n".join(lines)
            + "\n想说话了就调 chat_switch(action=\"on\", target=\"…\")，不用等谁批准。")


def status_text() -> str:
    rows = off_targets()
    if not rows:
        return "现在哪儿都没关，都是正常状态。"
    out = ["现在安静着："]
    for kind, ident, rec in rows:
        until = float(rec.get("until") or 0)
        when = (f"{time.strftime('%m-%d %H:%M', time.localtime(until))} 自动恢复"
                if until else "一直安静，直到你自己开")
        out.append(f"· {kind} {ident}：{str(rec.get('reason') or '没写原因')}（{when}）")
    return "\n".join(out)
