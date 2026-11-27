# services/selfcheck.py
"""L0 规则自检（2026-11-27，docs/addressing-design.md §7.7.1）。

只做**机械动作**：不调模型、不生成、不改写语义。
上一轮 P0 把「L0 机械自检」写进了计划但没落，这里补上（2026-11-27 用户拍板先做 1+2）。

检查项：
  1. 整条复读：与最近 N 条自己消息的 3-gram Jaccard ≥ SIM → **这条不发**
     （现有 60s dedup 只比「上一条」且存错了对象，见 chat 里的注释）；
  2. 长度异常：群聊文字泡 > 40 汉字 → **只标记**（上游已走合并转发，不截断）；
  3. 问句未答：对方有「？」且回复只有「嗯/哈哈/草/？」→ **只标记**；
  4. 数字/专名缺失：对方句里有数字/英文专名，回复一个都没有 → **只标记**；
  5. vent 禁方案词：kind=vent 且回复含「建议你/你可以/首先/其次/不妨/你应该/试试看」
     → 尝试**机械删句**（只删含禁词的片段，剩余 ≥4 汉字才采用），否则保留原文；
     无论删没删成功都记 flag（离线看 badcase）。

开关：REPLY_SELFCHECK=1（默认开，纯规则 <10ms）
边界：任何异常都返回原回复（fail-open），绝不让自检把回复弄丢。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from core import tuning

try:
    from nonebot import logger
except Exception:  # pragma: no cover
    import logging
    logger = logging.getLogger(__name__)

ENABLED = bool(tuning.i("REPLY_SELFCHECK"))
RECENT_N = max(1, tuning.i("SELFCHECK_RECENT_N"))
WINDOW_SEC = max(60.0, tuning.f("SELFCHECK_WINDOW_SEC"))
SIM = min(0.99, max(0.5, tuning.f("SELFCHECK_SIM")))
LONG_HANZI = max(10, tuning.i("SELFCHECK_LONG_HANZI"))

# vent 场景的「方案词」：命中就说明她在给建议而不是接情绪
VENT_BAN = tuple(
    w.strip() for w in (
        "建议你", "你可以", "建议", "首先", "其次", "不妨", "你应该",
        "试试看", "可以试试", "最好", "应该先", "解决办法",
    ) if w.strip()
)

# 只有语气词的短回复（对方问句只回这个 = 没答）
_ONLY_FILLER = re.compile(r"^[嗯啊哦哈嘿呵草？?！!。.…~～\s]+$")
_NUM = re.compile(r"\d")
_EN = re.compile(r"[A-Za-z]{2,}")
_HANZI = re.compile(r"[\u4e00-\u9fff]")
_SENT_SPLIT = re.compile(r"(?<=[。！？；\n])|(?<=[，,])")

_OUT = Path("data/selfcheck.jsonl")


def _hanzi(text: str) -> int:
    return len(_HANZI.findall(str(text or "")))


def _grams(text: str, n: int = 3) -> set:
    s = re.sub(r"\s+", "", str(text or ""))
    if len(s) < n:
        return {s} if s else set()
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def jaccard(a: str, b: str, n: int = 3) -> float:
    """3-gram Jaccard（0~1）。"""
    ga, gb = _grams(a, n), _grams(b, n)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / float(len(ga | gb))


def strip_plan(text: str) -> tuple:
    """机械删掉含「方案词」的句子。返回 (新文本, 命中的词列表)。

    剩余不足 4 个汉字 → 不删（宁可留着，也别把她的话删成半句）。
    """
    t = str(text or "").strip()
    if not t:
        return t, []
    hits = [w for w in VENT_BAN if w in t]
    if not hits:
        return t, []
    parts = [p for p in _SENT_SPLIT.split(t) if p and p.strip()]
    kept = [p for p in parts if not any(w in p for w in VENT_BAN)]
    new = "".join(kept).strip(" ，,。.；;\n")
    if _hanzi(new) < 4:
        return t, hits          # 删完太短 → 保留原文（fail-open）
    return new, hits


def check(reply: str, *, kind: str = "", ask_text: str = "",
          recent=(), is_group: bool = True) -> dict:
    """跑一遍 L0。返回：

    {
      "text": 可能被机械修正后的文本,
      "drop": bool,        # True = 这条别发
      "flags": [...],      # repeat / long / unanswered / missing_ref / vent_plan
      "reason": "...",
      "stripped": [...],   # vent 场景被删掉的方案词
    }
    """
    out = {"text": str(reply or ""), "drop": False, "flags": [],
           "reason": "", "stripped": []}
    t = str(reply or "").strip()
    if not t:
        return out

    # 1) 整条复读（最近 N 条自己消息）
    for old in list(recent)[-RECENT_N:]:
        s = jaccard(t, old)
        if s >= SIM:
            out.update(drop=True, reason=f"repeat:{s:.2f}")
            out["flags"].append("repeat")
            return out

    # 2) 长度异常（只标记，上游已合并转发）
    if is_group and _hanzi(t) > LONG_HANZI:
        out["flags"].append("long")

    q = str(ask_text or "")

    # 3) 问句未答（只标记）
    if ("？" in q or "?" in q) and _ONLY_FILLER.match(t):
        out["flags"].append("unanswered")

    # 4) 数字/英文专名缺失（只标记）
    if (_NUM.search(q) or _EN.search(q)) and not (_NUM.search(t) or _EN.search(t)):
        out["flags"].append("missing_ref")

    # 5) vent 禁方案词（机械删句）
    if str(kind or "").strip().lower() == "vent":
        new, hits = strip_plan(t)
        if hits:
            out["flags"].append("vent_plan")
            out["stripped"] = hits
            out["reason"] = "vent:" + ",".join(hits)
            out["text"] = new
    return out


def note(session_key: str, reply: str, result: dict, *, ask_text: str = "",
         kind: str = "") -> None:
    """落盘 badcase（离线看命中率；失败静默）。"""
    if not ENABLED:
        return
    try:
        _OUT.parent.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts": round(time.time(), 3),
            "session": str(session_key or ""),
            "kind": str(kind or ""),
            "flags": list((result or {}).get("flags") or []),
            "reason": str((result or {}).get("reason") or ""),
            "stripped": list((result or {}).get("stripped") or []),
            "reply": str(reply or "")[:300],
            "ask": str(ask_text or "")[:200],
        }
        with _OUT.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
