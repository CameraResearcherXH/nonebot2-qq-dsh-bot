# services/prompt_report.py
"""Prompt 体检：今日每段占多大、离当前消息多近、缓存命中多少。

用户 2026-11-23：
  「我想看到今日的 prompt 每段的关注程度。我发现心情和好感并没有关注太多。
    我还想看看 prompt 的缓存命中率。因为我看了昨天的用量，
    全都是两个三方 chat 用的大多数，而真正的角色扮演 flash 感觉缓存一句都没命中。」

这个模块只读数据，不调模型：
  · `data/llm_trace/<day>.jsonl` —— 每次真实请求的 msgs（每段都在）；
  · `data/llm_usage.db`          —— 按 purpose/model 的 token + cache_hit。

关注度怎么算：
  同一轮 prompt 里，越靠后的 system 块离「当前消息」越近，注意力越高。
  `关注度 = 1 - 平均位置占比`（0 = 最前面，1 = 紧挨当前消息）。
  这是启发式，不是模型真实 attention；但用来判断「心情/好感是不是被埋在中间」足够。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

TRACE_DIR = Path("data/llm_trace")
USAGE_DB = Path(os.getenv("LLM_USAGE_DB", "data/llm_usage.db"))


def _section(role: str, content: str) -> str:
    if role == "user":
        return "当前消息"
    if role != "system":
        return ""
    c = str(content or "").lstrip()
    if c.startswith("DeepSeek 小鲸鱼") or c.startswith("你是 "):
        return "角色卡"
    if "下面是几条规矩" in c or c.startswith("▍00"):
        return "规矩/协议"
    if c.startswith("▍昨日总结"):
        return "昨日总结"
    if c.startswith("▍记忆目录"):
        return "记忆目录"
    if c.startswith("▍群里的设定"):
        return "群设定/词条"
    if c.startswith("▍这条相关的设定"):
        return "词条"
    if c.startswith("▍00b") or c.startswith("▍00c") or c.startswith("▍00d"):
        return "动态规矩"
    if c.startswith("▍00e"):
        return "安静状态"
    if c.startswith("【这条回复的硬要求"):
        return "硬要求(post_rules)"
    if c.startswith("▍现在时间"):
        return "时间"
    if c.startswith("▍这一条消息的人称"):
        return "人称"
    if c.startswith("▍这条消息的收件人"):
        return "收件人"
    if c.startswith("▍群聊现场"):
        return "群聊现场"
    if c.startswith("【历史对话】"):
        return "历史对话"
    if c.startswith("【私聊前文】"):
        return "私聊前文"
    if c.startswith("▍现在的状态"):
        return "心情/状态"
    if c.startswith("▍你对") and "的底色" in c:
        return "好感底色"
    if c.startswith("▍说话风格参考"):
        return "表达风格"
    if c.startswith("▍白月光模式"):
        return "白月光"
    if c.startswith("【当前说话人】"):
        return "当前说话人"
    if c.startswith("【背景】"):
        return "背景"
    if c.startswith("▍这段对话之前的状态"):
        return "对话状态"
    if c.startswith("▍要不要安静"):
        return "安静建议"
    if c.startswith("▍最近这段在聊什么"):
        return "群聊总结"
    if c.startswith("▍") and "（QQ" in c[:60]:
        return "人物卡"
    if c.startswith("【另一个模型"):
        return "模型参考"
    if c.startswith("▍"):
        return "其它▍"
    return "其它"


def _iter_traces(day: str, limit: int = 400):
    p = TRACE_DIR / f"{day}.jsonl"
    if not p.exists():
        return []
    rows = []
    try:
        with p.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if not str(r.get("key") or "").startswith(("group_", "private_")):
                    continue
                if not isinstance(r.get("msgs"), list):
                    continue
                rows.append(r)
    except Exception:
        return []
    return rows[-max(1, int(limit)):]


def _aggregate(day: str, limit: int):
    rows = _iter_traces(day, limit)
    agg: dict = {}
    total_chars = 0
    for r in rows:
        msgs = r.get("msgs") or []
        counted = []
        for i, m in enumerate(msgs):
            role = str((m or {}).get("role") or "")
            content = str((m or {}).get("content") or "")
            sec = _section(role, content)
            if not sec:
                continue
            counted.append((i, sec, len(content)))
        n = len(counted)
        if not n:
            continue
        for idx, (orig_i, sec, ln) in enumerate(counted):
            pos = idx / max(1, n - 1)
            a = agg.setdefault(sec, {"chars": 0, "pos": 0.0, "n": 0})
            a["chars"] += ln
            a["pos"] += pos
            a["n"] += 1
            total_chars += ln
    return rows, total_chars, agg


def section_items(day: str = "", limit: int = 80) -> list:
    """[(段名, 百分比)]，给图表用。"""
    day = day or time.strftime("%Y-%m-%d")
    _rows, total, agg = _aggregate(day, limit)
    if not total:
        return []
    return [(sec, a["chars"] / total * 100.0)
            for sec, a in sorted(agg.items(), key=lambda kv: -kv[1]["chars"])]


def chart_urls(day: str = "", limit: int = 80) -> list:
    day = day or time.strftime("%Y-%m-%d")
    from . import usage_chart
    items = section_items(day, limit)
    return usage_chart.build_charts(items, f"Prompt 分段 · {day}", top_n=8)


async def download_charts(day: str = "", limit: int = 80) -> list:
    day = day or time.strftime("%Y-%m-%d")
    from . import usage_chart
    return await usage_chart.download_urls(
        chart_urls(day, limit), prefix="prompt")


def section_report(day: str = "", limit: int = 80) -> str:
    day = day or time.strftime("%Y-%m-%d")
    rows, total_chars, agg = _aggregate(day, limit)
    if not rows:
        return f"📊 {day} 没有可分析的 llm_trace（先聊几句再试）。"

    if not total_chars:
        return f"📊 {day} 的 trace 里没有可识别的 prompt 段。"

    lines = [f"📊 {day} Prompt 分段体检（最近 {len(rows)} 次请求 / {total_chars} 字）",
             "关注度 = 平均位置占比（越接近 1 = 越靠后、离当前消息越近、越容易被注意到）",
             ""]
    lines.append(f"{'段':12s} {'占比':>6s} {'平均字数':>8s} {'关注度':>6s}")
    lines.append("-" * 42)
    for sec, a in sorted(agg.items(), key=lambda kv: -kv[1]["chars"]):
        share = a["chars"] / max(1, total_chars) * 100
        avg = a["chars"] / max(1, a["n"])
        att = a["pos"] / max(1, a["n"])
        lines.append(f"{sec:12s} {share:5.1f}% {avg:8.0f} {att:6.2f}")
    lines.append("")
    # 点名用户最关心的两块
    for sec in ("心情/状态", "好感底色"):
        a = agg.get(sec)
        if not a:
            lines.append(f"⚠️ {sec}：**这次统计里完全没出现**")
        else:
            att = a["pos"] / max(1, a["n"])
            lines.append(f"· {sec}：出现 {a['n']} 次，平均 {a['chars']/a['n']:.0f} 字，关注度 {att:.2f}")
    lines.append("")
    lines.append("（心情/好感已挪到历史之后、当前消息之前；关注度应接近 1。"
                 "如果还是 <0.5，检查 build_packet 是否产出、或预算被裁空。）")
    return "\n".join(lines)


def cache_report(day: str = "") -> str:
    day = day or time.strftime("%Y-%m-%d")
    if not USAGE_DB.exists():
        return f"📊 {day} 没有 llm_usage.db。"
    try:
        c = sqlite3.connect(str(USAGE_DB), timeout=5)
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT purpose, model, COUNT(*) n, SUM(prompt) p, "
            "SUM(completion) c, SUM(cache_hit) h FROM usage "
            "WHERE day=? GROUP BY purpose, model ORDER BY p DESC", (day,)
        ).fetchall()
        c.close()
    except Exception as e:
        return f"📊 读用量失败：{type(e).__name__}: {e}"
    if not rows:
        return f"📊 {day} 还没有用量记录。"

    total_p = sum(int(r["p"] or 0) for r in rows)
    total_h = sum(int(r["h"] or 0) for r in rows)
    lines = [f"💰 {day} 缓存命中（总 prompt {total_p:,} / 命中 {total_h:,}"
             f" = {total_h / max(1, total_p) * 100:.1f}%）",
             f"{'purpose':12s} {'model':20s} {'次数':>5s} {'prompt':>10s} {'命中率':>6s}"]
    lines.append("-" * 62)
    for r in rows:
        p = int(r["p"] or 0)
        h = int(r["h"] or 0)
        lines.append(
            f"{str(r['purpose']):12s} {str(r['model'] or '-'):20s} "
            f"{int(r['n']):5d} {p:10,d} {h / max(1, p) * 100:5.1f}%")
    lines.append("")
    lines.append("提示：角色扮演是 purpose=chat（deepseek-chat）；"
                 "judge/audit 是判心情/审核，preflight 是 GLM 参考。"
                 "它们开头就是动态内容，0% 缓存是结构性的，不是 bug。")
    return "\n".join(lines)


def _split_heading(text: str, pattern: str) -> list:
    """按标题行切块，返回 [(标题, 字符数)]。"""
    import re as _re
    lines = str(text or "").splitlines()
    heads = [(i, ln) for i, ln in enumerate(lines) if _re.match(pattern, ln)]
    if not heads:
        return [("（全部）", len(text or ""))]
    out = []
    if heads[0][0] > 0:
        pre = "\n".join(lines[:heads[0][0]]).strip()
        if pre:
            out.append(("（开头）", len(pre)))
    for k, (i, h) in enumerate(heads):
        end = heads[k + 1][0] if k + 1 < len(heads) else len(lines)
        body = "\n".join(lines[i:end])
        out.append((h.strip()[:26], len(body)))
    return out


def detail_report(day: str = "", limit: int = 80) -> str:
    """把「角色卡」「规矩/协议」拆到小节，看每小节占多少、块内位置。

    块内位置 0 = 开头，1 = 结尾；越靠后越容易被注意到。
    """
    import re as _re
    day = day or time.strftime("%Y-%m-%d")
    rows = _iter_traces(day, limit)
    if not rows:
        return f"📊 {day} 没有 trace。"
    last = None
    for r in reversed(rows):
        msgs = r.get("msgs") or []
        if any("下面是几条规矩" in str((m or {}).get("content") or "") for m in msgs):
            last = r
            break
    if not last:
        return "📊 最近没有带规矩块的请求。"

    msgs = last.get("msgs") or []
    total = sum(len(str((m or {}).get("content") or ""))
                for m in msgs
                if _section(str((m or {}).get("role") or ""),
                            str((m or {}).get("content") or "")))
    persona = ""
    rules = ""
    for m in msgs:
        c = str((m or {}).get("content") or "")
        if not persona and (c.startswith("DeepSeek 小鲸鱼") or c.startswith("你是 ")):
            persona = c
        if not rules and "下面是几条规矩" in c:
            rules = c

    def _table(title, items):
        out = [f"【{title}】{len(items)} 小节（以最近一次请求为准，共 {total} 字）",
               f"{'小节':28s} {'字数':>6s} {'占prompt':>7s} {'块内位置':>6s}"]
        out.append("-" * 54)
        n = max(1, len(items) - 1)
        for i, (label, chars) in enumerate(items):
            share = chars / max(1, total) * 100
            pos = i / n
            out.append(f"{label[:28]:28s} {chars:6d} {share:6.1f}% {pos:6.2f}")
        return "\n".join(out)

    parts = [f"🔍 Prompt 小节拆解（最近一次请求 / {total} 字）"]
    if persona:
        parts.append(_table("角色卡", _split_heading(
            persona, r"^[一二三四五六七八九十]+、")))
    if rules:
        parts.append(_table("规矩/协议", _split_heading(
            rules, r"^▍\d{2}[a-z]?")))
    parts.append("说明：块内位置 0=开头、1=结尾；越靠后越容易被注意到。"
                 "占比高 + 位置靠前的小节，是「删减/合并」的首选；"
                 "重要但位置靠前的小节，应该往后挪。")
    return "\n\n".join(parts)


def report_text(day: str = "", limit: int = 80) -> str:
    return (section_report(day, limit) + "\n\n"
            + detail_report(day, limit) + "\n\n"
            + cache_report(day))
