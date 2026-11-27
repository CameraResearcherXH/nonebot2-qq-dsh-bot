"""长答意图（long_doc）判定：`llm.py` 与 `reply_pipeline.py` 共用。

为什么单独一个模块（2026-11-27）：
  以前 `is_long_doc` 长在 `llm.py` 里，而动态流水线 `reply_pipeline` 才是
  线上真正的取数入口 —— 它看不到 long_doc，所以「证据 6000 / 图书馆 3000」
  在 staged 路径上永远不生效（外部审计抓到的缺口）。
  判定 + 预算常量抽出来后，两边读同一份，避免第二份真相。
"""
from __future__ import annotations

import re

from core import tuning

ENABLED = tuning.i("LONG_DOC_ENABLED")
MAX_TOKENS = tuning.i("LONG_DOC_MAX_TOKENS")
# 检索候选条数（多取几条给重排留余量）
EVIDENCE = tuning.i("LONG_DOC_EVIDENCE")
# 真正进 prompt 的篇数 / 每篇字符数（DESIGN §3.3：3 篇 × ≤1900 字）
PIECES = tuning.i("LONG_DOC_PIECES")
PIECE_CHARS = tuning.i("LONG_DOC_PIECE_CHARS")

_HINTS = (
    "详细讲讲", "详细说说", "展开说", "展开讲", "深入讲", "具体讲讲",
    "写个总结", "总结一下", "分析一下", "分析下", "梳理一下",
    "这本书", "这篇论文", "这段代码", "讲了什么", "讲的是什么",
)


def query_of(text: str) -> str:
    """从「整段群聊」里取最后一条真人消息（和 llm._tool_query_of 同口径）。

    群聊 text 是一大段提示：有分段说明、规矩、`【时间】⭐名字(123)#456: 内容`。
    优先找带 `#消息号: ` 的真人行；找不到才退回最后一行。
    """
    t = str(text or "").strip()
    if "\n" in t or "（这一段群聊" in t:
        lines = [x.strip() for x in t.splitlines() if x.strip()]
        real = [x for x in lines if re.search(r"#\d+:\s*\S", x)]
        t = real[-1] if real else (lines[-1] if lines else "")
    return t


def is_long_doc(text: str) -> bool:
    """这句是不是在要「长答」（只认明确措辞，避免把日常闲聊拉长）。"""
    if not int(ENABLED or 0):
        return False
    t = query_of(text)
    if len(t) < 6:
        return False
    return any(k in t for k in _HINTS)
