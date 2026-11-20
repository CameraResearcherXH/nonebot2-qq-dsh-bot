# services/access.py
import os
import re
from typing import Any

# ---------- 优先从 nonebot 读取 SUPERUSERS ----------
def _load_owner():
    try:
        from nonebot import get_driver
        # nonebot2 的 SUPERUSERS 就是主人（超管）
        supers = get_driver().config.superusers
        if supers:
            # 取第一个作为"唯一主人"判定，或者你想全算主人可以把 == 换成 in
            return str(next(iter(supers)))
    except Exception:
        pass
    return os.getenv("OWNER_QQ", "").strip()

def _load_admins():
    try:
        from nonebot import get_driver
        supers = get_driver().config.superusers
        if supers:
            return set(str(x) for x in supers)
    except Exception:
        pass
    return set(x.strip() for x in os.getenv("ADMIN_QQS", "").split(",") if x.strip())

# 这里在模块导入初始化一次；也可提供 refresh()
OWNER_QQ = _load_owner()
ADMIN_QQS = _load_admins()

LEVEL_OWNER = "owner"
LEVEL_ADMIN = "admin"
LEVEL_MEMBER = "member"


def level_of(user_id: int | str) -> str:
    uid = str(user_id)
    if OWNER_QQ and uid == OWNER_QQ:
        return LEVEL_OWNER
    if uid in ADMIN_QQS:
        return LEVEL_ADMIN
    return LEVEL_MEMBER


def is_owner(user_id: int | str) -> bool:
    return level_of(user_id) == LEVEL_OWNER


# ---------- nickname 兜底 + 去括号 ----------
_BRACKET_TAIL = re.compile(r"\s*[（(【\[][^）)】\]]*[）)】\]]\s*$")


def display_name(event: Any) -> str:
    if not hasattr(event, "sender") or event.sender is None:
        return str(getattr(event, "user_id", ""))
    s = event.sender
    raw = getattr(s, "card", "") or getattr(s, "nickname", "") or str(event.user_id)
    cleaned = _BRACKET_TAIL.sub("", raw).strip()
    return cleaned or raw


def nickname_variants(event: Any) -> list[str]:
    if not hasattr(event, "sender") or event.sender is None:
        return [str(getattr(event, "user_id", ""))]
    s = event.sender
    out = set()
    for raw in (getattr(s, "card", ""), getattr(s, "nickname", "")):
        if not raw:
            continue
        raw = str(raw).strip()
        out.add(raw)
        cleaned = _BRACKET_TAIL.sub("", raw).strip()
        if cleaned:
            out.add(cleaned)
    out.add(str(event.user_id))
    return list(out)
