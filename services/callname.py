# services/callname.py
"""称呼的唯一入口（2026-11-24 重构）。

用户踩过的坑：
  「/叫我 /叫你 /叫他 有人称混淆，而且写不进档案里的『我叫你什么/你叫我什么』；
   跟 LLM 说『我叫你 X、你叫我 Y』，它的工具也写不稳，反而偶尔写到『注意』里。」

所以称呼不再散落在 profiles 的 call_name / bot_alias / note / 记忆文本里，
统一到这里，两个方向各一个字段，**永不互相覆盖**：

    他叫她（bot）：这个人（或这个群）怎么叫她 → 喊这个名字 = 在叫她
    她叫他（user）：她该怎么叫这个人

存储两处：
  ① `data/callname.json` —— 索引（群 → 别名 → QQ），给硬匹配用，读得快；
  ② `profiles` —— 展示用（call_name / groups.<gid>.call_name / bot_alias）。

方向判定（`parse_direction`）：
    「我叫你 X」  → user 方向（她叫他 X）
    「你叫我 X」  → bot 方向（他叫她 X）
    「你可以叫我 X 吗」→ bot 方向（对机器人说「叫我」= 给机器人起名）

反了会自动纠正（`normalize_pair`）：如果模型把「他叫我」和「我叫他」写反，
用「名字像不像群名片 / 像不像人格名」判断后换回来。
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

try:
    from nonebot import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)

FILE = Path("data/callname.json")
_LOCK = threading.Lock()
_CACHE = {"mtime": -1.0, "data": {}}

MAX_LEN = 12


def _load() -> dict:
    try:
        m = FILE.stat().st_mtime
    except Exception:
        m = -1.0
    if _CACHE["mtime"] == m:
        _d = _CACHE["data"]
        if not isinstance(_d, dict):
            _d = {}
        _d.setdefault("groups", {})
        _d.setdefault("global", {"bot": {}, "user": {}})
        _CACHE["data"] = _d
        return _d
    data = {}
    if FILE.exists():
        try:
            data = json.loads(FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("groups", {})
    data.setdefault("global", {"bot": {}, "user": {}})
    _CACHE["mtime"], _CACHE["data"] = m, data
    return data


def _save(d: dict) -> None:
    with _LOCK:
        FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(FILE)
        try:
            _CACHE["mtime"] = FILE.stat().st_mtime
        except Exception:
            _CACHE["mtime"] = -1.0
        _CACHE["data"] = d


def _g(gid: str) -> dict:
    d = _load()
    g = d["groups"].setdefault(str(gid or ""), {}) if gid else None
    if g is None:
        return d["global"]
    g.setdefault("bot", {})
    g.setdefault("user", {})
    return g


def _clean(name: str) -> str:
    return re.sub(r"\s+", "", str(name or ""))[:MAX_LEN]


# ================= 写入 =================
def set_bot_call(qq: str, name: str, gid: str = "") -> str:
    """他叫她什么（bot 方向）。喊这个名字 = 在叫她。"""
    qq, name = str(qq or ""), _clean(name)
    if not qq or not name:
        return ""
    d = _load()
    scope = _g(gid)
    scope["bot"][name] = qq
    _save(d)
    try:
        from . import profiles
        if gid:
            profiles.set_group_fields(qq, gid, bot_alias=name)
        profiles.set_call_names(qq, bot_alias=name)
    except Exception as e:
        logger.debug(f"[CALLNAME] 写档案失败: {type(e).__name__}: {e}")
    logger.info(f"[CALLNAME] {qq} 叫她「{name}」{'@' + str(gid) if gid else ''}")
    return name


def set_user_call(qq: str, name: str, gid: str = "") -> str:
    """她叫他什么（user 方向）。"""
    qq, name = str(qq or ""), _clean(name)
    if not qq or not name:
        return ""
    d = _load()
    scope = _g(gid)
    scope["user"][qq] = name
    _save(d)
    try:
        from . import profiles
        profiles.set_call_names(qq, call_name=name)
        if gid:
            profiles.set_group_fields(qq, gid, call_name=name)
    except Exception as e:
        logger.debug(f"[CALLNAME] 写档案失败: {type(e).__name__}: {e}")
    logger.info(f"[CALLNAME] 她叫 {qq} 「{name}」{'@' + str(gid) if gid else ''}")
    return name


# ================= 删除 =================
def clear_user_call(qq: str, gid: str = "") -> bool:
    """删掉「她叫他什么」（本群 + 全局），并清档案里的 call_name。"""
    qq = str(qq or "").strip()
    if not qq:
        return False
    d = _load()
    (d.get("global", {}).get("user") or {}).pop(qq, None)
    if gid:
        g = ((d.get("groups") or {}).get(str(gid)) or {})
        (g.get("user") or {}).pop(qq, None)
    _save(d)
    try:
        from . import profiles
        profiles.clear_call_names(qq, call_name=True, bot_alias=False, gid=gid)
    except Exception as e:
        logger.debug(f"[CALLNAME] 清档案失败: {type(e).__name__}: {e}")
    logger.info(f"[CALLNAME] 删除「她叫 {qq}」{'@' + str(gid) if gid else ''}")
    return True


def clear_bot_call(qq: str, gid: str = "") -> bool:
    """删掉「他叫她什么」（这个 QQ 名下的所有别名），并清档案 bot_alias。"""
    qq = str(qq or "").strip()
    if not qq:
        return False
    d = _load()

    def _drop(scope: dict) -> None:
        for name, owner in list((scope.get("bot") or {}).items()):
            if str(owner) == qq:
                scope["bot"].pop(name, None)

    _drop(d.get("global", {}))
    if gid:
        _drop(((d.get("groups") or {}).get(str(gid)) or {}))
    _save(d)
    try:
        from . import profiles
        profiles.clear_call_names(qq, call_name=False, bot_alias=True, gid=gid)
    except Exception as e:
        logger.debug(f"[CALLNAME] 清档案失败: {type(e).__name__}: {e}")
    logger.info(f"[CALLNAME] 删除「{qq} 叫她」{'@' + str(gid) if gid else ''}")
    return True


def clear_all(qq: str, gid: str = "") -> None:
    """两个方向一起删（/删称呼 默认行为）。"""
    clear_user_call(qq, gid)
    clear_bot_call(qq, gid)


def clear_name(qq: str, name: str, gid: str = "") -> bool:
    """只删一个具体名字（`/删称呼 QQ 阿明`）。

    为什么不能只靠 clear_all：
      · clear_all 只清「当前 call_name」，如果「阿明」早就被换成「老大」，
        它还留在 profiles.aliases 里，`group_roster` 下次又会把它找回来；
      · 用户明确点名要删「阿明」时，应该按名字精确清，而不是把两个方向全清掉。
    """
    qq = str(qq or "").strip()
    name = _clean(name)
    if not qq or not name:
        return False
    d = _load()

    def _drop(scope: dict) -> None:
        bot = scope.get("bot") or {}
        if str(bot.get(name) or "") == qq:
            bot.pop(name, None)
        user = scope.get("user") or {}
        if str(user.get(qq) or "") == name:
            user.pop(qq, None)

    _drop(d.get("global", {}))
    if gid:
        _drop((d.get("groups") or {}).get(str(gid)) or {})
    _save(d)
    try:
        from . import profiles
        profiles.remove_alias(qq, name)
    except Exception as e:
        logger.debug(f"[CALLNAME] 清别名失败: {type(e).__name__}: {e}")
    logger.info(f"[CALLNAME] 删除 {qq} 的称呼「{name}」"
                f"{'@' + str(gid) if gid else ''}")
    return True


# ================= 读取 =================
def bot_names(gid: str = "") -> set:
    """这个群里「他叫她」的所有名字（含全局兜底）。"""
    d = _load()
    names = set((d.get("global", {}).get("bot") or {}).keys())
    if gid:
        names |= set((((d.get("groups") or {}).get(str(gid)) or {}).get("bot") or {}).keys())
    return {n for n in names if n}


def who_calls_me(text: str, gid: str = "") -> str:
    """消息里出现了「他叫她」的名字 → 返回起这个名字的人 QQ（硬匹配用）。"""
    t = str(text or "")
    if not t:
        return ""
    d = _load()
    scope = ((d.get("groups") or {}).get(str(gid)) or {}).get("bot") or {}
    for name in sorted(scope, key=len, reverse=True):
        if name and name in t:
            return str(scope[name])
    for name in sorted((d.get("global", {}).get("bot") or {}), key=len, reverse=True):
        if name and name in t:
            return str(d["global"]["bot"][name])
    return ""


def user_call_of(qq: str, gid: str = "", fallback: str = "") -> str:
    """她该叫这个人什么：本群 > 全局索引 > 档案 call_name > fallback。"""
    qq = str(qq or "")
    d = _load()
    if gid:
        v = (((d.get("groups") or {}).get(str(gid)) or {}).get("user") or {}).get(qq)
        if v:
            return str(v)
    v = (d.get("global", {}).get("user") or {}).get(qq)
    if v:
        return str(v)
    try:
        from . import profiles
        p = profiles.group_of(qq, gid) or {}
        return str(p.get("call_name") or "") or str(
            (profiles.get_profile(qq) or {}).get("call_name") or "") or fallback
    except Exception:
        return fallback


def bot_call_of(qq: str, gid: str = "") -> str:
    """他叫她什么：本群 > 全局（这个 QQ 名下记过的名字）。

    档案卡 / /档案 文字 / reply_pipeline 都走这里，避免它们各读一份
    profiles.bot_alias，导致「索引改了、档案没改」。
    """
    qq = str(qq or "").strip()
    if not qq:
        return ""
    d = _load()
    if gid:
        for name, owner in (((d.get("groups") or {}).get(str(gid)) or {}).get("bot") or {}).items():
            if str(owner) == qq:
                return str(name)
    for name, owner in (d.get("global", {}).get("bot") or {}).items():
        if str(owner) == qq:
            return str(name)
    try:
        from . import profiles
        return str((profiles.get_profile(qq) or {}).get("bot_alias") or "")
    except Exception:
        return ""


def render(qq: str, gid: str = "", speaker_name: str = "") -> str:
    """给 prompt 用的一小段（固定区，省 token）。"""
    u = user_call_of(qq, gid, fallback=speaker_name or "")
    b = bot_call_of(qq, gid)
    if not u and not b:
        return ""
    return (f"▍你俩的称呼\n"
            f"· 你叫他：{u or '（还没定，先按群名片）'}\n"
            f"· 他叫你：{b or '（还没定）'}\n"
            f"（这是硬约定，别自己改、别叫混；叫错了对方会纠正你）")


# ================= 方向判定 / 纠错 =================
_USER_RE = re.compile(r"我\s*(?:以后|就)?\s*叫\s*你\s*[「\"']?([^\s，。,.!?！？「」\"']{1,12})")
_BOT_RE = re.compile(r"你\s*(?:以后|就)?\s*叫\s*我\s*[「\"']?([^\s，。,.!?！？「」\"']{1,12})")


def parse_direction(text: str) -> list:
    """从一句自然语言里抽出 [(方向, 名字)]。

    ⚠️ 这两条正则**互不重叠**：先匹配「我叫你X」（她叫他），
    再从剩下的文本里找「你叫我X」（他叫她）。
    """
    t = str(text or "")
    out = []
    for m in _USER_RE.finditer(t):
        out.append(("user", m.group(1)))
    rest = _USER_RE.sub(" ", t)
    for m in _BOT_RE.finditer(rest):
        out.append(("bot", m.group(1)))
    return out


def normalize_pair(bot: str, user: str, *, speaker_name: str = "",
                   bot_names: set | None = None) -> tuple:
    """模型给的 (他叫她, 她叫他) 有没有写反 → 换回来。

    判据（按可靠度）：
      1. user 名字出现在「她叫他」位置却等于人格名 → 反了；
      2. bot 名字等于说话人群名片 → 反了；
      3. 只剩一个非空时不动。
    """
    bot, user = _clean(bot), _clean(user)
    bn = {str(x) for x in (bot_names or set()) if str(x).strip()}
    sn = _clean(speaker_name)
    if not bot or not user:
        return bot, user
    if bn and user in bn:
        return user, bot          # user 其实是她的名字 → 反了
    if sn and bot == sn:
        return user, bot          # bot 其实是说话人的名字 → 反了
    return bot, user


def reset() -> None:
    _CACHE["mtime"] = -1.0
    _CACHE["data"] = {"groups": {}, "global": {"bot": {}, "user": {}}}


def status(qq: str = "", gid: str = "") -> str:
    d = _load()
    lines = ["▍称呼索引"]
    if gid:
        g = (d.get("groups") or {}).get(str(gid)) or {}
        lines.append(f"· 本群（{gid}）")
        for name, owner in (g.get("bot") or {}).items():
            lines.append(f"   他叫她「{name}」← {owner}")
        for owner, name in (g.get("user") or {}).items():
            lines.append(f"   她叫 {owner} 「{name}」")
    for name, owner in (d.get("global", {}).get("bot") or {}).items():
        lines.append(f"· 全局：他叫她「{name}」← {owner}")
    for owner, name in (d.get("global", {}).get("user") or {}).items():
        lines.append(f"· 全局：她叫 {owner} 「{name}」")
    if qq:
        lines.append(f"· 当前：{render(qq, gid)}".replace("\n", " "))
    return "\n".join(lines) if len(lines) > 1 else "还没有任何称呼约定"
