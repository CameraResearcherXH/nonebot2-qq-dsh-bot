"""
每日总结 —— 每天 0 点把当天的对话压成「昨日总结」，第二天新开对话时交接。

为什么要它（用户 2026-11-11）：
「每日 0 点记忆总结一次，总结为昨日总结。第二天在新开的时候方便交接。」

因为我们把短历史改成**每天新开**（CHAT_HISTORY_DAILY），
第二天她看不到昨天聊过什么 —— 这一块就是补那个断层：
不塞原始对话（太长），只塞一段几十字的"昨天大概聊了啥"。
"""
from __future__ import annotations
from core import tuning

import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger("services")

ROOT = Path(__file__).resolve().parent.parent
SUMMARY_PATH = Path(os.getenv("DAILY_SUMMARY_PATH", str(ROOT / "data" / "daily_summary.jsonl")))
KEEP_DAYS = tuning.i("DAILY_SUMMARY_KEEP_DAYS")
MIN_MSGS = tuning.i("DAILY_SUMMARY_MIN_MSGS")


def _load_all() -> list:
    if not SUMMARY_PATH.exists():
        return []
    out = []
    try:
        for line in SUMMARY_PATH.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        pass
    return out


def _save_all(recs: list) -> None:
    try:
        SUMMARY_PATH.parent.mkdir(parents=True, exist_ok=True)
        cut = time.strftime("%Y-%m-%d", time.localtime(time.time() - KEEP_DAYS * 86400))
        recs = [r for r in recs if str(r.get("date") or "") >= cut]
        tmp = SUMMARY_PATH.with_suffix(".tmp")
        tmp.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n",
            encoding="utf-8",
        )
        tmp.replace(SUMMARY_PATH)
    except Exception as e:
        logger.warning(f"[SUMMARY] 保存失败: {type(e).__name__}: {e}")


def get_yesterday_summary(gid: str, day: str = "") -> str:
    """
    取某个会话的「昨日总结」。没有就返回空串。

    day 不传则取真正的昨天；传入则取那一天的（测试用）。
    """
    if not day:
        day = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
    gid = str(gid or "")
    for r in _load_all():
        if str(r.get("date")) == day and str(r.get("gid")) == gid:
            return str(r.get("summary") or "")
    return ""


async def summarize_day(day: str = "", max_sessions: int = 30) -> int:
    """
    把某一天的对话按会话分组，各总结成 1~3 句话。返回写入的会话数。

    用 GLM 总结（便宜、快）；GLM 不可用就跳过，绝不阻塞。
    """
    from .dialogue_log import load_range
    from .glm import available as glm_ok, chat as glm_chat

    if not day:
        day = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
    if not glm_ok():
        logger.info("[SUMMARY] GLM 不可用，跳过每日总结")
        return 0

    try:
        recs = [r for r in load_range(3)
                if time.strftime("%Y-%m-%d", time.localtime(float(r.get("ts") or 0))) == day]
    except Exception as e:
        logger.warning(f"[SUMMARY] 读对话失败: {type(e).__name__}: {e}")
        return 0

    # 按会话分组（群的 gid 是裸号，私聊带 private_ 前缀）
    groups: dict = {}
    for r in recs:
        gid = str(r.get("gid") or "")
        if not gid:
            continue
        groups.setdefault(gid, []).append(r)

    old = [r for r in _load_all() if str(r.get("date")) != day]
    written = 0
    for gid, msgs in list(groups.items())[:max_sessions]:
        if len(msgs) < MIN_MSGS:
            continue
        # 只取有信息量的行（去掉纯表情、太短的）
        lines = []
        for m in msgs[-120:]:
            t = str(m.get("text") or "").strip()
            if len(t) < 2:
                continue
            who = "我" if m.get("kind") == "bot" else (m.get("who") or "?")
            lines.append(f"{who}: {t[:60]}")
        if len(lines) < MIN_MSGS:
            continue
        prompt = (
            "下面是某个 QQ 群/私聊里一整天的聊天记录（「我」是 AI 角色小鲸鱼）。\n"
            "请用**第一人称**写一段「昨日回顾」，要求：\n"
            "1. 2~4 句，总共不超过 80 字\n"
            "2. 只写真正发生的事、聊过的话题、谁说了什么重要的话\n"
            "3. 不要写「今天」「昨天」这种相对时间，直接说事\n"
            "4. 不要客套、不要总结成「大家聊得很开心」这种废话\n"
            "5. 如果没什么实质内容，就写「没什么特别的」\n\n"
            + "\n".join(lines[-80:])
        )
        try:
            summary = await glm_chat(
                [{"role": "user", "content": prompt}], max_tokens=300, timeout=40
            )
        except Exception as e:
            logger.warning(f"[SUMMARY] {gid} 总结失败: {type(e).__name__}: {e}")
            continue
        summary = str(summary or "").strip().replace("\n", " ")
        if not summary:
            continue
        old.append({"date": day, "gid": gid, "summary": summary[:300],
                    "n_msgs": len(msgs), "ts": time.time()})
        written += 1
        logger.info(f"[SUMMARY] {day} · {gid} → {summary[:40]}…")

    if written:
        _save_all(old)
    logger.info(f"[SUMMARY] {day} 完成，{written} 个会话写了昨日总结")
    return written
