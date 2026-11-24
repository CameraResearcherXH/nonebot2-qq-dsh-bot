# services/cmd_guard.py
"""
「这条是不是一个真的指令」——避免对不存在的命令装死。

问题（用户 2026-11-19）：
  群里有人 @ 她发 `/搜图151768172`，她一声不吭。
  原因：chat 的 `_need_reply` 里「/ 开头 = 指令」一刀切 → 直接放行给别的插件；
  但根本没有 `/搜图` 这个命令（而且命令和参数之间还少了空格）→ 没人处理 → 她装死。

做法：
  · 扫描 plugins/ 下所有 `on_command("...")` / `aliases={...}`，建一份已知命令表（懒加载、只建一次）；
  · `classify(text)` 返回：
      ""                      → 认识（交给别的插件，chat 闭嘴）
      "missing_space:画图"    → 命令对、但参数粘在一起（`/画图一只猫`）
      "unknown:搜图"          → 根本没有这个命令
  · 不认识的两类都不再静默：让她像人一样回一句（"没这个功能" / "要加空格"）。
"""

import re
from pathlib import Path

try:
    from nonebot import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)

_CMD_START = "/／!！."
_KNOWN = None

_ON_CMD = re.compile(r'on_command\(\s*"([^"]+)"')
_ALIASES = re.compile(r"aliases\s*=\s*\{([^}]*)\}")
# 第三方插件常常不用 on_command，而是自己写 rule + 字符串列表
# （例：`_YUNSHI_CMDS = (".今日运势", "/今日运势", ...)`）。
# 这里把「/ 或 . 开头的短字符串字面量」也当命令词收进来 —— 宁可多认，
# 也不能把她不认识的真命令抢过来乱答。
_LITERAL_CMD = re.compile(r"""["']([/／.][\u4e00-\u9fffA-Za-z0-9_]{1,10})["']""")
# Alconna 命令（`Alconna("搜图", ...)` / `on_alconna(...)`）不在 on_command 里，
# 不认出来就会把她不认识的"真命令"抢过来乱答（2026-11-19：/搜图 就是这样漏的）。
_ALCONNA = re.compile(r'Alconna\(\s*"([^"]+)"')


def known_commands(refresh: bool = False) -> set:
    """扫描 plugins/ 下的 on_command / aliases，缓存一份集合。"""
    global _KNOWN
    if _KNOWN is not None and not refresh:
        return _KNOWN
    out = set()
    try:
        root = Path(__file__).resolve().parent.parent / "plugins"
        for f in root.rglob("*.py"):
            try:
                t = f.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            # ⚠️ 不能因为"没有 on_command"就跳过整个文件：
            #    Alconna 命令（/搜图）和字符串列表命令都在这种文件里。
            if ("on_command" not in t) and ("Alconna" not in t) and ("/" not in t):
                continue
            for m in _ON_CMD.finditer(t):
                out.add(m.group(1).strip())
            for m in _ALIASES.finditer(t):
                for a in re.findall(r'"([^"]+)"', m.group(1)):
                    a = a.strip()
                    if a:
                        out.add(a)
            for m in _ALCONNA.finditer(t):
                a = m.group(1).strip()
                if a:
                    out.add(a)
            for m in _LITERAL_CMD.finditer(t):
                a = m.group(1)[1:].strip()
                if a:
                    out.add(a)
    except Exception as e:
        logger.debug(f"[CMD] 扫描命令失败: {type(e).__name__}: {e}")
    _KNOWN = out
    return out


def _word(text: str) -> str:
    """取命令词：去掉前缀后按空白切第一段（`/画图 一只猫` → 画图）。"""
    t = str(text or "").strip()
    if not t:
        return ""
    if t[0] not in _CMD_START:
        return ""
    body = t[1:].strip()
    return re.split(r"[\s\u3000]", body, 1)[0].strip() if body else ""


def classify(text: str) -> str:
    """
    判断一条「看起来像指令」的消息到底是什么：

      ""                     认识 → 交给别的插件（chat 不接）
      "missing_space:<命令>" 命令对、参数没空格 → 提醒他加空格
      "unknown:<命令>"       没这个命令 → 直接说不认识
    """
    word = _word(text)
    if not word:
        return ""
    known = known_commands()
    if word in known:
        return ""
    # 命令和参数粘在一起：/画图一只猫
    for c in known:
        if len(c) >= 2 and word.startswith(c) and len(word) > len(c):
            return f"missing_space:{c}"
    return f"unknown:{word}"


def hint_for(kind: str) -> str:
    """把 classify 的结果翻成给模型的一句系统提示（插在用户消息后面）。"""
    if kind.startswith("missing_space:"):
        c = kind.split(":", 1)[1]
        return (f"（系统提示：这条像是命令，但命令和参数之间少了空格，"
                f"正确写法是「{c} 参数」。你可以顺口提醒他一句，也可以直接问他要什么。）")
    if kind.startswith("unknown:"):
        return ("（系统提示：没有这个命令。别装死——用你自己的话回一句，"
                "不要假装执行了，也不要编一个结果给他。）")
    return ""

