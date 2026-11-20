#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
diag_conversation.py —— 群聊「她到底有没有被接话」体检（只读，不调模型）

为什么要有这个
--------------
`/全量测试` 和 eval_replay 测的是「链路通不通」「单条回复打分」，
但它们看不见用户真正的体验：**她在群里说完，有没有人理她**。

所以这里换一套口径 —— 不看单条回复写得好不好，只看**对话有没有继续**：

  · 被接话率   她说完 90 秒内，有人**冲她**说话（带「你/@她名字/问号」或短促反应）
  · 被淹没率   有人说话，但明显不是冲她（群里本来就在聊，她的话被冲过去）
  · 被无视率   她说完 90 秒内没有任何真人说话（她像在自言自语）
  · 被吐槽率   她说完之后 2 条内出现「闭嘴/烦/别说话/呆/傻/滚…」
  · 重复率     她这条和上一条的相似度 ≥0.8（车轱辘话）
  · 连发率     两条她的消息间隔 <5 秒（看起来像"接话两次"）
  · 平均字数   群聊被截到多短（20 字上限的直接证据）
  · 中位延迟   她回的是多少秒前的那句话（越大越像"答非所问"）
  · 问句应答   她问了问题之后，有没有人**回她**

用法
----
    # 容器内（推荐）
    docker exec qq-grok-bot python scripts/diag_conversation.py
    docker exec qq-grok-bot python scripts/diag_conversation.py --days 7 --top 12
    docker exec qq-grok-bot python scripts/diag_conversation.py --gid 936601568 --samples 8
    docker exec qq-grok-bot python scripts/diag_conversation.py --include-private

    # A/B 用：只看改动之后的几个小时（避开旧数据污染）
    docker exec qq-grok-bot python scripts/diag_conversation.py --hours 3

注意：只读 data/dialogue/*.jsonl，不写任何东西、不花钱。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
import unicodedata
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)          # dialogue_log 用的是相对路径
sys.path.insert(0, str(ROOT))

from services.dialogue_log import load_range  # noqa: E402

# 90 秒内没人说话 = 这条掉地上了
IGNORE_WINDOW = 90.0
# 她问句之后多久没人答算「没答」
QUESTION_WINDOW = 120.0
# 两条她的消息间隔小于这个 = 连发（像接话两次）
BURST_GAP = 5.0
# 吐槽要看之后几条（有时先「…」再「闭嘴」）
COMPLAINT_LOOKAHEAD = 2

COMPLAINT_WORDS = (
    "闭嘴", "烦", "别说话", "别回", "安静", "傻", "呆", "滚", "神经",
    "关掉", "能不能别", "无语", "别叫", "吵", "别烦", "爬", "人机",
)

# 「冲她说」的线索：叫她名字 / 用「你」/ 问号 / 很短的即时反应
_HER_NAME_RE = re.compile(r"小鲸鱼|鲸鱼|鱼宝|肥鱼")
_REACT_RE = re.compile(r"哈|草|笑|6|牛|绝|离谱|确实|对啊|是啊|真的|卧槽|好家伙|？|\?")


def _dw(s: str) -> int:
    """显示宽度（中文算 2）。表格对齐用。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in str(s))


def _pad(s: str, width: int) -> str:
    s = str(s)
    return s + " " * max(0, width - _dw(s))


def _norm(text: str) -> str:
    t = re.sub(r"\[[^\]]{0,12}\]|<[^>]{0,40}>", "", str(text or ""))
    return re.sub(r"[\s\W_]+", "", t)


def _bigrams(s: str) -> set:
    return {s[i:i + 2] for i in range(len(s) - 1)} or ({s} if s else set())


def _sim(a: str, b: str) -> float:
    A, B = _bigrams(a), _bigrams(b)
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)


def _is_placeholder(text: str) -> bool:
    t = str(text or "").strip()
    return t in ("[图片]", "[语音]", "[视频]", "[表情]", "")


def _has_complaint(text: str) -> bool:
    return any(w in str(text or "") for w in COMPLAINT_WORDS)


def _is_question(text: str) -> bool:
    return bool(re.search(r"[?？]", str(text or "")))


def _is_responsive(text: str) -> bool:
    """这条真人消息是不是**冲她**说的（而不是群里本来就在聊）。"""
    t = str(text or "")
    if not t:
        return False
    if "@" in t or _HER_NAME_RE.search(t):
        return True
    if re.search(r"[你妳]", t):
        return True
    if re.search(r"[?？]", t):
        return True
    s = re.sub(r"[\s\W_]+", "", t)
    if len(s) <= 6 and _REACT_RE.search(s):
        return True
    return False


def _next_human(rows: list, i: int, window: float):
    """i 之后第一条真人消息（在窗口内）。返回 (index, 是否窗口内)。"""
    for j in range(i + 1, len(rows)):
        if rows[j].get("kind") in ("user", "peer"):
            ts_gap = float(rows[j].get("ts") or 0) - float(rows[i].get("ts") or 0)
            return (j, ts_gap <= window)
    return (None, False)


def _median(xs: list) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def analyze(records: list, gid_filter: str = "",
            include_private: bool = False, min_bot: int = 1) -> dict:
    by_gid = defaultdict(list)
    for r in records:
        g = str(r.get("gid") or "")
        if not g:
            continue
        if gid_filter and g != gid_filter:
            continue
        if g.startswith("private_") and not include_private:
            continue
        by_gid[g].append(r)

    groups = {}
    for gid, rows in by_gid.items():
        rows.sort(key=lambda x: float(x.get("ts") or 0))
        bot_idx = [i for i, r in enumerate(rows) if r.get("kind") == "bot"]
        n_bot = len(bot_idx)
        if n_bot < min_bot:
            continue
        n_human = sum(1 for r in rows if r.get("kind") in ("user", "peer"))

        engaged, ambient, ignored = [], [], []
        complaints, repeated = [], []
        burst = 0
        latencies, lengths = [], []
        questions, q_answered = 0, 0
        prev_bot = None

        for i in bot_idx:
            r = rows[i]
            ts = float(r.get("ts") or 0)
            txt = str(r.get("text") or "")

            if not _is_placeholder(txt):
                lengths.append(len(txt))

            if prev_bot is not None:
                if ts - float(rows[prev_bot].get("ts") or 0) < BURST_GAP:
                    burst += 1
                if _sim(_norm(txt), _norm(rows[prev_bot].get("text") or "")) >= 0.8:
                    repeated.append(i)
            prev_bot = i

            # 延迟：她是回多少秒前的那句话
            for j in range(i - 1, -1, -1):
                if rows[j].get("kind") in ("user", "peer"):
                    gap = ts - float(rows[j].get("ts") or 0)
                    if 0 <= gap <= 300:
                        latencies.append(gap)
                    break

            # 之后有没有人说话 / 是不是冲她说的
            nxt, in_win = _next_human(rows, i, IGNORE_WINDOW)
            if nxt is None or not in_win:
                ignored.append(i)
            elif _is_responsive(rows[nxt].get("text") or ""):
                engaged.append(i)
            else:
                ambient.append(i)

            # 吐槽：之后 1~2 条里有没有骂她
            k = 0
            for j in range(i + 1, len(rows)):
                if rows[j].get("kind") not in ("user", "peer"):
                    continue
                k += 1
                if k > COMPLAINT_LOOKAHEAD:
                    break
                if float(rows[j].get("ts") or 0) - ts > IGNORE_WINDOW:
                    break
                if _has_complaint(rows[j].get("text") or ""):
                    complaints.append(j)
                    break

            # 她问句之后有没有人回她
            if _is_question(txt):
                questions += 1
                qj, q_in = _next_human(rows, i, QUESTION_WINDOW)
                if qj is not None and q_in and _is_responsive(rows[qj].get("text") or ""):
                    q_answered += 1

        def _pct(x):
            return 100.0 * x / n_bot if n_bot else 0.0

        groups[gid] = {
            "rows": rows,
            "bot": n_bot,
            "user": n_human,
            "share": (100.0 * n_bot / (n_bot + n_human)) if (n_bot + n_human) else 0.0,
            "engaged_pct": _pct(len(engaged)),
            "ambient_pct": _pct(len(ambient)),
            "ignored_pct": _pct(len(ignored)),
            "complaint_pct": _pct(len(complaints)),
            "repeat_pct": _pct(len(repeated)),
            "burst_pct": _pct(burst),
            "avg_len": (sum(lengths) / len(lengths)) if lengths else 0.0,
            "med_latency": _median(latencies),
            "q_answer_pct": _pct(q_answered) if questions else None,
            # 存**下标**，不是 dict（之前存 dict 导致 _prev_user 报 TypeError）
            "ignored_samples": ignored,
            "complaint_samples": complaints,
            "repeat_samples": repeated,
        }
    return groups


def _fmt_ts(ts: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(float(ts or 0)))


def _short(text: str, n: int = 46) -> str:
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    return t if len(t) <= n else t[:n] + "…"


def _prev_human(rows: list, i: int):
    for j in range(i - 1, -1, -1):
        if rows[j].get("kind") in ("user", "peer"):
            return rows[j]
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="群聊「有没有被接话」体检（只读）")
    ap.add_argument("--days", type=int, default=3, help="读最近几天（默认 3）")
    ap.add_argument("--hours", type=float, default=0,
                    help="只看最近 N 小时（做 A/B 对比用，优先于 --days）")
    ap.add_argument("--gid", default="", help="只看某个群")
    ap.add_argument("--top", type=int, default=15, help="表格最多几个群")
    ap.add_argument("--samples", type=int, default=3, help="每组最差样本条数")
    ap.add_argument("--min-bot", type=int, default=5, help="她至少说过几条才统计（默认 5）")
    ap.add_argument("--include-private", action="store_true",
                    help="把私聊也算进来（私聊没有'群里没人接'这个概念，默认排除）")
    args = ap.parse_args()

    records = load_range(max(1, args.days))
    if args.hours and args.hours > 0:
        _cut = time.time() - args.hours * 3600.0
        records = [r for r in records if float(r.get("ts") or 0) >= _cut]
    if not records:
        print(f"data/dialogue 里没有最近 {args.days} 天的记录（DIALOGUE_LOG 开着吗？）")
        return 1

    groups = analyze(records, args.gid, args.include_private, args.min_bot)
    if not groups:
        print("没有解析到符合条件的消息（试试 --min-bot 1 / --include-private）。")
        return 1

    total_bot = sum(g["bot"] for g in groups.values())
    total_user = sum(g["user"] for g in groups.values())
    _span = f"最近 {args.hours:g} 小时" if args.hours else f"最近 {args.days} 天"
    print("=" * 92)
    print(f"群聊体检 · {_span} · {len(groups)} 个群 · "
          f"真人 {total_user} 条 / 她 {total_bot} 条")
    print("=" * 92)
    print("口径：被接话 = 90s 内有人冲她说（@她/叫她名字/用「你」/问号/短促反应）")
    print("      被淹没 = 有人说话，但明显不是冲她（群里本来就在聊）")
    print("      被无视 = 90s 内没有任何真人说话（自言自语）")
    print("      被吐槽 = 之后 2 条内出现「闭嘴/烦/呆/傻/滚…」")
    print("      延迟   = 她回的是多少秒前的那句话（越大越像答非所问）")
    print()

    cols = [("群号", 12), ("她", 5), ("真人", 6), ("占比", 6), ("被接话", 8),
            ("被淹没", 8), ("被无视", 8), ("被吐槽", 8), ("重复", 7),
            ("连发", 7), ("字数", 6), ("延迟s", 7), ("问句应答", 10)]
    header = "  ".join(_pad(c, w) for c, w in cols)
    print(header)
    print("-" * _dw(header))

    ranked = sorted(groups.items(), key=lambda kv: -kv[1]["ignored_pct"])[:args.top]
    for gid, g in ranked:
        qa = "—" if g["q_answer_pct"] is None else f"{g['q_answer_pct']:.0f}%"
        row = [
            (gid, 12), (g["bot"], 5), (g["user"], 6), (f"{g['share']:.0f}%", 6),
            (f"{g['engaged_pct']:.0f}%", 8), (f"{g['ambient_pct']:.0f}%", 8),
            (f"{g['ignored_pct']:.0f}%", 8), (f"{g['complaint_pct']:.0f}%", 8),
            (f"{g['repeat_pct']:.0f}%", 7), (f"{g['burst_pct']:.0f}%", 7),
            (f"{g['avg_len']:.0f}", 6), (f"{g['med_latency']:.0f}", 7), (qa, 10),
        ]
        print("  ".join(_pad(v, w) for v, w in row))

    print()
    print("怎么读这张表：")
    print("· 被无视 > 50%  → 她在自言自语，说什么都没人接（最该先修）")
    print("· 被淹没 > 50%  → 她的话被群聊冲过去（多半是回得太晚 / 太短 / 没接住话题）")
    print("· 被吐槽 > 10%  → 已经到「惹人烦」的程度，先降频再谈内容")
    print("· 重复 > 20%    → 车轱辘话（上下文里看不到自己上一句时最明显）")
    print("· 连发 > 10%    → 看起来像「接话两次」")
    print("· 字数中位 ~20  → 群聊短句为主；>20 字的回复现在走合并转发（不计入字数）")
    print("· 延迟 > 10s    → 她回的是十几秒前的话，群里早翻篇了")
    print("· 问句应答 <30% → 她问完没人答，说明大家不想接她的话")

    if args.samples > 0:
        for gid, g in ranked[:3]:
            rows = g["rows"]
            if g["ignored_samples"]:
                print()
                print(f"—— 群 {gid} · 说完没人理的例子 ——")
                for i in g["ignored_samples"][-args.samples:]:
                    pv = _prev_human(rows, i)
                    if pv is not None:
                        print(f"  [{_fmt_ts(pv.get('ts'))}] {pv.get('who')}: "
                              f"{_short(pv.get('text'), 60)}")
                    print(f"  [{_fmt_ts(rows[i].get('ts'))}] 她: "
                          f"{_short(rows[i].get('text'), 60)}")
                    print("  （90 秒内无人回应）")
            if g["complaint_samples"]:
                print()
                print(f"—— 群 {gid} · 被吐槽的例子 ——")
                for i in g["complaint_samples"][-args.samples:]:
                    print(f"  [{_fmt_ts(rows[i].get('ts'))}] {rows[i].get('who')}: "
                          f"{_short(rows[i].get('text'), 60)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
