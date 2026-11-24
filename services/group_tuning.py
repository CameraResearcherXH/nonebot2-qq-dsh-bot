# services/group_tuning.py
"""单群体验参数覆盖（用户 2026-11-18）。

为什么要有这个
--------------
`diag_conversation.py` 的基线（9 个群 / 7 天）显示两个硬伤：

  · 延迟中位 7~22 秒 —— 她回的是十几秒前那句话，群里早翻篇了；
  · 平均字数 12~22  —— 群聊 20 字上限把话截断，复杂一点必然说不完整。

但这两项不能一次改全局（别的群会炸），所以做成**单群覆盖**：

    GROUP_CHAT_OVERRIDES=100000021:4
    格式：群号:回复间隔秒:直接发字数阈值
    多个群用英文逗号分隔；第 2 项之后可省略。

⚠️ 第 3 项是「直接发文字」的阈值，不是硬上限：
   用户 2026-11-18 确认规定一直是「20 字内直接发，20 字以上合并转发」，
   所以超过阈值的回复会自动转卡片，不需要靠调大这个数来让话说完整。

不配 = 所有群走 .env 默认（REPLY_INTERVAL / GROUP_MAX_HANZI）。
"""
import os


def _parse(raw: str) -> dict:
    out = {}
    for part in str(raw or "").split(","):
        bits = [b.strip() for b in part.split(":")]
        if not bits or not bits[0]:
            continue

        def _num(idx, cast):
            if len(bits) > idx and bits[idx]:
                try:
                    return cast(bits[idx])
                except Exception:
                    return None
            return None

        ov = {}
        _r = _num(1, float)
        _h = _num(2, int)
        if _r is not None:
            ov["reply_interval"] = _r
        if _h is not None:
            ov["max_hanzi"] = _h
        out[bits[0]] = ov
    return out


OVERRIDES = _parse(os.getenv("GROUP_CHAT_OVERRIDES", ""))


def _gid(key: str) -> str:
    k = str(key or "")
    return k[len("group_"):] if k.startswith("group_") else ""


def for_key(key: str) -> dict:
    """session_key（group_xxx）或群号都能查。"""
    k = str(key or "")
    if k.isdigit():
        return OVERRIDES.get(k) or {}
    return OVERRIDES.get(_gid(k)) or {}


def reply_interval(key: str, default: float) -> float:
    try:
        return float(for_key(key).get("reply_interval", default))
    except Exception:
        return float(default)


def max_hanzi(key: str, default: int) -> int:
    try:
        return int(for_key(key).get("max_hanzi", default))
    except Exception:
        return int(default)

