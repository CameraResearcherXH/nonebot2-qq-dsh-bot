"""LLM 日预算刹车（2026-11-27 用户要求）。

用户原话：
  「每日告警 200w token，接近超过的时候她就提前说今天休息啦，然后后续就暂时不消耗了。」

四个档位（`state()`）：
  off  —— `LLM_DAILY_BUDGET<=0`：不刹车（默认）
  ok   —— 用量 < 软阈值（`LLM_BUDGET_SOFT_RATIO`，默认 0.9）
  soft —— 到软阈值：**照常回**，但 prompt 里加一句「今天快没额度了，这轮说完就说要休息」
  rest —— 到硬阈值（100%）：主聊天不调模型；每个会话当天第一次回一句固定话术，之后静默

为什么分两档：一刀切会让群里突然没反应，像机器人坏了；
先在 soft 阶段让她自己说出来，到 rest 才硬停，体感是「她累了去睡了」。

生效日期：`LLM_BUDGET_START_DATE=YYYY-MM-DD`（可选）。
  设成明天 = 今天不刹车 —— 避免「当天已经用超了，一上线就闭嘴」。
  留空 = 立即生效。

状态落 `data/budget_notice.json`（按自然日，跨重启有效）：
  {"day": "2026-11-28", "rest": ["group_123", ...], "soft": ["group_123", ...]}
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from core import tuning

try:
    from nonebot import logger
except Exception:  # pragma: no cover
    import logging
    logger = logging.getLogger(__name__)

SOFT_RATIO = tuning.f("LLM_BUDGET_SOFT_RATIO")
STATE_FILE = Path(os.getenv("BUDGET_STATE_FILE", "data/budget_notice.json"))
# 固定话术：`{used}` / `{limit}` / `{pct}` 会被替换
REST_TEXT = os.getenv(
    "LLM_BUDGET_REST_TEXT",
    "今天先休息啦～今天的额度用完了（{used}/{limit} tokens），明天再陪你聊。",
).strip()

_DONE = {"day": "", "rest": [], "soft": []}


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _start_date() -> str:
    return str(os.getenv("LLM_BUDGET_START_DATE", "") or "").strip()


def effective() -> bool:
    """今天是否真的启用刹车（`LLM_BUDGET_START_DATE` 之前一律不刹）。"""
    start = _start_date()
    if not start:
        return True
    return _today() >= start


def limit() -> float:
    try:
        return float(tuning.f("LLM_DAILY_BUDGET") or 0.0)
    except Exception:
        return 0.0


def used() -> int:
    try:
        from . import llm_usage
        return int(llm_usage.today_used())
    except Exception:
        return 0


def ratio() -> float:
    lim = limit()
    if lim <= 0:
        return 0.0
    return used() / lim


def state() -> str:
    """off / ok / soft / rest。"""
    lim = limit()
    if lim <= 0 or not effective():
        return "off"
    r = ratio()
    if r >= 1.0:
        return "rest"
    if r >= max(0.1, float(SOFT_RATIO or 0.9)):
        return "soft"
    return "ok"


def soft_hint() -> str:
    """soft 档塞进 prompt 的一句（短，别占预算）。"""
    return ("▍今天快没额度了：这轮聊完就主动说一句「今天先休息啦」，"
            "别再开新话题、别长篇大论。")


def notice_line() -> str:
    """rest 档的固定话术（不经过模型，0 token）。"""
    lim = limit()
    try:
        pct = f"{ratio() * 100:.0f}%"
    except Exception:
        pct = "?"
    try:
        return str(REST_TEXT).format(used=used(), limit=int(lim), pct=pct)
    except Exception:
        return str(REST_TEXT)


def _load() -> None:
    day = _today()
    if _DONE.get("day") == day:
        return
    _DONE["day"] = day
    _DONE["rest"] = []
    _DONE["soft"] = []
    try:
        if STATE_FILE.exists():
            d = json.loads(STATE_FILE.read_text(encoding="utf-8") or "{}")
            if str(d.get("day") or "") == day:
                _DONE["rest"] = [str(x) for x in (d.get("rest") or [])]
                _DONE["soft"] = [str(x) for x in (d.get("soft") or [])]
    except Exception:
        pass


def _save() -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(
            json.dumps(_DONE, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def claim(kind: str, key: str) -> bool:
    """这个会话今天第一次进入 `kind` 档 → True（只该说一次）。

    `kind` = "rest"（额度用完的通知）或 "soft"（快用完的提醒）。
    """
    _load()
    k = str(key or "")
    bucket = _DONE.setdefault(kind, [])
    if k in bucket:
        return False
    bucket.append(k)
    if len(bucket) > 500:
        del bucket[:-500]
    _save()
    return True


def status_text() -> str:
    """给 `/测函数 用量` 附带一行。"""
    lim = limit()
    if lim <= 0:
        return "· 日预算：没设（LLM_DAILY_BUDGET=0，不刹车）"
    return (f"· 日预算：{used()} / {int(lim)}（{ratio() * 100:.0f}%）"
            f"｜档位 {state()}｜生效日 {_start_date() or '立即'}")
