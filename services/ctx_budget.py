# services/ctx_budget.py
"""块级上下文预算（CTX_*）：先量，再决定裁不裁。

为什么单独一个模块：
  · CTX_* 的默认值来自 DESIGN §3.3（**token** 口径），而 live 的块是**字符**；
    直接拿 120 当字符会把状态卡/协议裁成残句。
  · 所以默认 `CTX_ENFORCE=0`：只统计每个块多大、超了多少，日志里报一次；
    确认某个块真的该裁，再把 `CTX_ENFORCE=1`（或以后按块开关）打开。
  · 这样 15 个键全部**有真实读点**，但不会一上来就改变线上 prompt。

换算：中文约 1 token ≈ 1.5 字（保守），所以 chars = CTX_* × 1.5。
"""
from __future__ import annotations

import time

from core import tuning

try:
    from nonebot import logger
except Exception:  # pragma: no cover
    import logging
    logger = logging.getLogger(__name__)

# block → (tuning 键, 字符/token 换算)
# ⚠️ 这些键名必须写字面量：scripts/check_tuning.py 靠它判断「已接线」。
BUDGET = {
    "persona": ("CTX_PERSONA", 1.5),
    "expression": ("CTX_EXPRESSION", 1.5),
    "protocol": ("CTX_PROTOCOL", 1.5),
    "state": ("CTX_STATE", 1.5),
    "style": ("CTX_STYLE", 1.5),
    "rhythm": ("CTX_RHYTHM", 1.5),
    "history": ("CTX_HISTORY", 1.5),
    "post_rules": ("CTX_POST_RULES", 1.5),
    "anchor": ("CTX_ANCHOR", 1.5),
    "tools": ("CTX_TOOLS", 1.5),
    "multitarget": ("CTX_MULTITARGET", 1.5),
    "white_moon": ("CTX_WHITE_MOON", 1.5),
    "style_total": ("CTX_STYLE_TOTAL", 1.5),
    "long_doc_evidence": ("CTX_LONG_DOC_EVIDENCE", 1.5),
    "long_doc_library": ("CTX_LONG_DOC_LIBRARY", 1.5),
    # ⚠️ 下面三个是**字符口径**（scale=1.0）：prompt_builder.BLOCK_BUDGET 直接拿它们
    # 当字符用（profile / memory_index / yesterday）。以前这里没有映射，导致
    # 同一个键在别处被 ×1.5、在这里没人观测（2026-11-25 统一）。
    "profile": ("CTX_PROFILE", 1.0),
    "evidence": ("CTX_EVIDENCE", 1.0),
    "summary": ("CTX_SUMMARY", 1.0),
}

# ⚠️ 这里必须用 `tuning.i("字面量")`：audit_coverage 靠它判断「参数有没有真读」。
#    动态 `tuning.i(key)` 会被判成空转（15 个 CTX_* 全被标红）。
_LIMITS = {
    "CTX_PERSONA": tuning.i("CTX_PERSONA"),
    "CTX_EXPRESSION": tuning.i("CTX_EXPRESSION"),
    "CTX_PROTOCOL": tuning.i("CTX_PROTOCOL"),
    "CTX_STATE": tuning.i("CTX_STATE"),
    "CTX_STYLE": tuning.i("CTX_STYLE"),
    "CTX_RHYTHM": tuning.i("CTX_RHYTHM"),
    "CTX_HISTORY": tuning.i("CTX_HISTORY"),
    "CTX_POST_RULES": tuning.i("CTX_POST_RULES"),
    "CTX_ANCHOR": tuning.i("CTX_ANCHOR"),
    "CTX_TOOLS": tuning.i("CTX_TOOLS"),
    "CTX_MULTITARGET": tuning.i("CTX_MULTITARGET"),
    "CTX_WHITE_MOON": tuning.i("CTX_WHITE_MOON"),
    "CTX_STYLE_TOTAL": tuning.i("CTX_STYLE_TOTAL"),
    "CTX_LONG_DOC_EVIDENCE": tuning.i("CTX_LONG_DOC_EVIDENCE"),
    "CTX_LONG_DOC_LIBRARY": tuning.i("CTX_LONG_DOC_LIBRARY"),
    # 字符口径（scale=1.0），见 BUDGET 末尾注释
    "CTX_PROFILE": tuning.i("CTX_PROFILE"),
    "CTX_EVIDENCE": tuning.i("CTX_EVIDENCE"),
    "CTX_SUMMARY": tuning.i("CTX_SUMMARY"),
}

ENFORCE = bool(tuning.i("CTX_ENFORCE"))
WARN_SEC = max(10.0, tuning.f("CTX_WARN_SEC"))

STATS: dict = {}
_LAST_WARN: dict = {}


def limit_chars(block: str) -> int:
    key, scale = BUDGET.get(str(block or ""), ("", 1.5))
    if not key:
        return 0
    return max(1, int(float(_LIMITS.get(key, 0)) * float(scale)))


def _note(block: str, n: int, limit: int) -> None:
    d = STATS.setdefault(block, {"n": 0, "over": 0, "max": 0, "last": 0,
                                 "limit": limit})
    d["n"] += 1
    d["last"] = n
    d["limit"] = limit
    if n > limit:
        d["over"] += 1
        d["max"] = max(int(d.get("max") or 0), n)


def _warn(block: str, n: int, limit: int, source: str = "") -> None:
    now = time.time()
    if now - float(_LAST_WARN.get(block) or 0) < WARN_SEC:
        return
    _LAST_WARN[block] = now
    logger.info(f"[CTX] {block} {n} 字 > 预算 {limit} 字"
                f"（CTX_ENFORCE={'on' if ENFORCE else 'off'}）"
                + (f"｜{source}" if source else ""))


def observe(block: str, text: str, *, source: str = "") -> str:
    """统计一个块；`CTX_ENFORCE=1` 时按预算裁剪。返回可能被裁后的文本。"""
    t = str(text or "")
    limit = limit_chars(block)
    if not limit or not t:
        return t
    _note(block, len(t), limit)
    if len(t) <= limit:
        return t
    _warn(block, len(t), limit, source)
    if ENFORCE:
        return t[: max(0, limit - 1)].rstrip() + "…"
    return t


def observe_lines(block: str, lines: list, *, source: str = "") -> list:
    """按行统计/裁剪（历史行、多目标行）。返回新的 list。"""
    limit = limit_chars(block)
    out = []
    total = 0
    for ln in (lines or []):
        s = str(ln or "")
        total += len(s)
        if limit and len(s) > limit:
            _note(block, len(s), limit)
            _warn(block, len(s), limit, source)
            if ENFORCE:
                s = s[: max(0, limit - 1)].rstrip() + "…"
        out.append(s)
    if out and limit:
        _note(block, total, limit)
    return out


def note_style_total(parts) -> int:
    """风格相关合计（expression + style + rhythm + white_moon）。"""
    total = sum(len(str(x or "")) for x in (parts or []))
    limit = limit_chars("style_total")
    if limit:
        _note("style_total", total, limit)
        if total > limit:
            _warn("style_total", total, limit, "expression+style+rhythm+moon")
    return total


def report_text() -> str:
    if not STATS:
        return "· 上下文预算：还没有样本"
    rows = sorted(STATS.items(), key=lambda kv: -(kv[1]["last"] / max(1, kv[1]["limit"])))
    lines = [f"· 上下文预算：CTX_ENFORCE={'on' if ENFORCE else 'off'}"
             f"（off=只统计不裁剪）"]
    for block, d in rows:
        pct = d["last"] / max(1, d["limit"]) * 100
        lines.append(f"    {block}: {d['last']}/{d['limit']} 字（{pct:.0f}%）"
                     f"｜超限 {d['over']}/{d['n']} 次｜峰值 {d['max']}")
    return "\n".join(lines)
