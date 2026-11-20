# services/group_roster.py
"""
群成员名册：拉一次、缓存住、按名字找人。

为什么要单独一个模块：
  · 发消息时的「真 @」（aura_logic.expand_mentions）要成员列表
  · 模型自己想查人（[[WHO|小强]]）也要成员列表
  两处都需要「拉一次就缓存」——群里有 200 人时，每条 @ 都拉一遍又慢又费流量，
  NapCat 那边也会被打爆。

找人规则（宁可找不到，也不 @ 错人）：
  1. 纯数字 → 当 QQ 号
  2. 精确命中群名片/昵称/去括号名/档案昵称/档案别名
  3. 前缀命中
  4. 子串命中
  只有**唯一候选**才会被 @，重名一律返回空。
"""
from core import tuning
import os
import re
import time

try:  # 允许脱离 nonebot 单独跑脚本
    from nonebot import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)

CACHE_TTL = tuning.f("ROSTER_TTL")   # 名册缓存 10 分钟

_CACHE: dict = {}          # gid -> (ts, members)
_BRACKET_TAIL = re.compile(r"\s*[（(【\[]\s*[^）)】\]]*[）)】\]]\s*$")


async def members_of(bot, group_id, ttl: float = 0) -> list:
    """拿群成员列表（带缓存）。拉失败返回旧缓存或空列表，绝不抛异常。"""
    gid = str(group_id or "")
    if not gid or bot is None:
        return []
    ttl = ttl or CACHE_TTL
    hit = _CACHE.get(gid)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    try:
        members = await bot.get_group_member_list(group_id=int(gid))
    except Exception as e:
        logger.warning(f"[ROSTER] 拉群 {gid} 成员失败: {type(e).__name__}: {e}")
        return hit[1] if hit else []
    members = list(members or [])
    _CACHE[gid] = (time.time(), members)
    logger.info(f"[ROSTER] 群 {gid} 名册已刷新（{len(members)} 人）")
    return members


def invalidate(group_id: str = "") -> None:
    """群成员变了 / 手动改档案后清缓存。传空则全清。"""
    if group_id:
        _CACHE.pop(str(group_id), None)
    else:
        _CACHE.clear()


def _names(m: dict) -> list:
    """一个人的所有可叫法：群名片 / 昵称 / 去括号名 / 档案昵称 / 档案别名。"""
    out = []
    for raw in (m.get("card"), m.get("nickname")):
        s = str(raw or "").strip()
        if not s:
            continue
        out.append(s)
        cleaned = _BRACKET_TAIL.sub("", s).strip()
        if cleaned and cleaned != s:
            out.append(cleaned)

    qq = str(m.get("user_id") or "")
    if qq:
        try:
            from . import profiles
            p = profiles.get_profile(qq)
            for key in ("call_name", "nickname"):
                v = str(p.get(key) or "").strip()
                if v:
                    out.append(v)
            for a in (p.get("aliases") or []):
                a = str(a or "").strip()
                if a:
                    out.append(a)
        except Exception:
            pass

    seen, uniq = set(), []
    for n in out:
        if n and n not in seen:
            seen.add(n)
            uniq.append(n)
    return uniq


def find(members: list, query: str, limit: int = 5) -> list:
    """
    按名字/QQ 找人。返回 [成员 dict, ...]，按「精确 > 前缀 > 子串」排序。
    找不到返回空列表。
    """
    q = str(query or "").strip().lstrip("@＠").strip()
    if not q:
        return []
    if re.match(r"^\d{5,12}$", q):
        return [m for m in (members or []) if str(m.get("user_id") or "") == q][:limit]

    ql = q.lower()
    exact, prefix, sub = [], [], []
    for m in members or []:
        names = _names(m)
        if not names:
            continue
        lowered = [n.lower() for n in names]
        if ql in lowered:
            exact.append(m)
        elif any(n.startswith(ql) for n in lowered):
            prefix.append(m)
        elif any(ql in n for n in lowered):
            sub.append(m)
    return (exact or prefix or sub)[:limit]


def label(m: dict) -> str:
    """`小强(1234)` —— 和群聊上下文里的标签格式保持一致。"""
    qq = str(m.get("user_id") or "")
    name = str(m.get("card") or m.get("nickname") or qq or "某人").strip()
    return f"{name}({qq[-4:]})" if len(qq) >= 4 else name
