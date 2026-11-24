# services/reply_pipeline.py
"""回复前的「动态取数」流水线（2026-11-24 用户重构要求）。

用户原话：
  「默认固定拿到的内容只有 人设卡 + tool + 标签处理后的上下文 + 档案里你叫他什么。
   拿到要接话的那句消息之后：
     · 要用工具（唱歌等）→ 直接调工具；
     · 回复类 → 先取心情值/当前状态/那件事的心理状态 →
       对方档案 + 好感词 + 印象最深的一件事 →
       心情 LLM 对这段话的总结 → 用总结去查记忆 → 用总结去查图书馆
       （图书馆是语料库，里面的你我他不是现在的你我他，仅供参考）→
       看这条消息 id 是否已经回过 → 综合回复。」

设计：
  · 每一步都**独立降级**：超时/异常只丢那一步，不影响回复；
  · 总耗时预算 `PIPELINE_BUDGET`（默认 9s），到点就带着已拿到的部分走；
  · 产出是一段紧凑文本（`render()`），由 llm.call_llm 作为最高 recency 的数据块注入，
    同时主 prompt 不再重复注入 心情/好感/记忆目录/图书馆/白月光。
  · 每一步都记 `[PIPE]` 日志，`/测函数 链路` 能看到这次到底拉了什么。
"""
from __future__ import annotations

import asyncio
import time

from core import tuning

try:
    from nonebot import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)

BUDGET = tuning.f("PIPELINE_BUDGET")
STATS: dict = {"runs": 0, "stage_ms": {}, "last": {}}


def _mark(name: str, t0: float) -> None:
    ms = (time.monotonic() - t0) * 1000
    d = STATS["stage_ms"].setdefault(name, {"n": 0, "ms": 0.0})
    d["n"] += 1
    d["ms"] = (d["ms"] * (d["n"] - 1) + ms) / d["n"]


async def _step(name: str, coro, timeout: float, default=None):
    """跑一步，超时/异常返回 default（并记录耗时）。"""
    t0 = time.monotonic()
    try:
        out = await asyncio.wait_for(coro, timeout=max(0.2, timeout))
        return out if out is not None else default
    except Exception as e:
        logger.debug(f"[PIPE] {name} 跳过（{type(e).__name__}: {e}）")
        return default
    finally:
        _mark(name, t0)


async def build(
    *,
    session_key: str,
    gid: str = "",
    text: str = "",
    speaker_qq: str = "",
    speaker_name: str = "",
    seg_mids=(),
    replied_mids=(),
    moon_target_qq: str = "",
) -> dict:
    """跑完整流水线，返回 {text, stages, summary, replied, cost}。"""
    t_all = time.monotonic()
    STATS["runs"] = int(STATS.get("runs") or 0) + 1
    blocks: list = []
    stages: dict = {}
    deadline = t_all + max(1.0, BUDGET)

    def left() -> float:
        return max(0.2, deadline - time.monotonic())

    # ---------- ① 称呼（固定区已经给了，这里补一句硬约定） ----------
    try:
        from . import callname
        _c = callname.render(speaker_qq, gid, speaker_name)
        if _c:
            blocks.append(_c)
            stages["称呼"] = _c
    except Exception:
        pass

    # ---------- ② 心情值 + 当前状态 + 心理状态 ----------
    try:
        from . import mood
        _m = mood.get_mood(session_key) or {}
        _jd = mood.get_judge(session_key) or {}
        _v = float(_m.get("value") or 0.0)
        _lab = str(_m.get("label") or "").strip()
        _mind = str(_jd.get("mind") or "").strip()
        _why = str(_jd.get("why") or "").strip()
        _parts = [f"心情值 {_v:+.0f}（-100~100，只影响语气浓淡）"]
        if _lab:
            _parts.append(f"她的心情原话：{_lab}")
        if _mind:
            _parts.append(f"刚才那件事她心里：{_mind}")
        if _why:
            _parts.append(f"原因：{_why[:60]}")
        blocks.append("▍此刻状态\n" + "\n".join(f"· {p}" for p in _parts))
        stages["状态"] = _parts
    except Exception:
        pass

    # ---------- ③ 对方档案 + 好感词 + 印象最深的一件事 ----------
    try:
        from . import profiles
        _nm = profiles.call_of(speaker_qq, speaker_name or speaker_qq, gid) or speaker_name
        _p = profiles.get_profile(speaker_qq) or {}
        _bond = ""
        try:
            _bond = profiles.get_bond_word(speaker_qq) or ""
        except Exception:
            _bond = ""
        _lines = [f"· 你叫他：{_nm}"]
        for _k, _t in (("identity", "他是谁"), ("personality", "性格"),
                       ("depth_note", "相处备注")):
            _v = str(_p.get(_k) or "").strip()
            if _v:
                _lines.append(f"· {_t}：{_v[:60]}")
        if _bond:
            _lines.append(f"· 你对他的好感：{_bond}")
        _top = ""
        try:
            from . import memory_v2
            _hits = memory_v2.recall(_nm or "他", limit=3,
                                     speaker_qq=speaker_qq, gid=gid) or []
            if _hits:
                _best = max(_hits, key=lambda h: float(h.get("salience") or 0))
                _top = f"{str(_best.get('summary') or '')[:40]}（{_best.get('ago')}）"
                _lines.append(f"· 你俩印象最深的一件事：{_top}")
        except Exception:
            pass
        blocks.append("▍这个人\n" + "\n".join(_lines))
        stages["档案"] = {"name": _nm, "bond": _bond, "top": _top}
    except Exception:
        pass

    # ---------- ④ 心情 LLM 对这段话的总结 ----------
    summary = ""
    try:
        from . import mood_judge
        summary = await _step(
            "总结", mood_judge.summarize(text, session_key), min(6.0, left())) or ""
        if summary:
            blocks.append(f"▍这句话在说什么（判读模型总结）\n· {summary}")
            stages["总结"] = summary
    except Exception:
        summary = ""

    # 总结失败就用原话当检索词（去掉 @/引用噪音）
    query = summary or str(text or "")[:120]

    # ---------- ⑤⑥ 记忆 + 图书馆（并行，互不等待） ----------
    async def _mem_stage():
        try:
            from . import memory_v2
            _hits = await _step(
                "记忆",
                asyncio.to_thread(memory_v2.recall, query, limit=4,
                                  speaker_qq=speaker_qq, gid=gid),
                min(3.0, left()), default=[]) or []
            if not _hits:
                return ""
            _lines = [f"{i}. {str(h.get('ago') or '')}｜{str(h.get('summary') or '')[:40]}"
                      for i, h in enumerate(_hits, 1)]
            stages["记忆"] = len(_hits)
            return "▍相关记忆（按这句话找的）\n" + "\n".join(_lines)
        except Exception:
            return ""

    async def _lib_stage():
        try:
            from . import agent_client
            # 图书馆在关键路径上：最多 3s；不可用会自己暂停（见 agent_client）
            _lib = await _step(
                "图书馆",
                agent_client.recall(query, speaker_qq=speaker_qq, recent_ctx=[]),
                min(3.0, left()), default=None)
            _facts = (_lib or {}).get("facts") or []
            if not _facts:
                return ""
            stages["图书馆"] = len(_facts)
            return ("▍图书馆·久远归档（**语料库里的「我/你/他」不是现在的你我他**，"
                    "只当参考线索，别照抄、别当成现在发生的事）\n"
                    + "\n".join(f"· {str(x)[:60]}" for x in _facts[:3]))
        except Exception:
            return ""

    _got = await asyncio.gather(_mem_stage(), _lib_stage())
    blocks.extend([b for b in _got if b])

    # ---------- ⑦ 这条消息回过没有 ----------
    replied = False
    try:
        _mid = str(seg_mids[0] if seg_mids else "")
        _rs = {str(x) for x in (replied_mids or [])}
        replied = bool(_mid and _mid in _rs)
        if replied:
            blocks.append("▍注意\n· 这条消息你已经回过了，**别再回一遍**；"
                          "如果确实还要补充，就只补一句新的。")
            stages["已回"] = True
    except Exception:
        pass

    # ---------- ⑧ 白月光说话风格 ----------
    try:
        from . import white_moon
        _wm = white_moon.render(gid, moon_target_qq or speaker_qq,
                                speaker_name, speaker_name)
        if _wm:
            blocks.append(_wm.strip()[:300])
            stages["白月光"] = True
    except Exception:
        pass

    text_out = "\n\n".join(b for b in blocks if b).strip()
    cost = time.monotonic() - t_all
    STATS["last"] = {"stages": list(stages), "cost": round(cost, 2),
                     "chars": len(text_out)}
    logger.info(
        f"[PIPE] {session_key} {cost:.1f}s｜拉了 {','.join(stages) or '无'}"
        f"｜{len(text_out)} 字")
    return {"text": text_out, "stages": stages, "summary": summary,
            "replied": replied, "cost": cost}


def stats_text() -> str:
    if not STATS["runs"]:
        return "· 流水线：还没跑过"
    parts = [f"· 流水线：跑了 {STATS['runs']} 次"]
    for k, v in sorted(STATS["stage_ms"].items(), key=lambda kv: -kv[1]["ms"]):
        parts.append(f"    {k}: {v['ms']:.0f}ms（{v['n']} 次）")
    last = STATS.get("last") or {}
    if last:
        parts.append(f"· 上次：{last.get('cost')}s｜{','.join(last.get('stages') or [])}")
    return "\n".join(parts)
