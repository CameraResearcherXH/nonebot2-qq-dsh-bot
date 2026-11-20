#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
eval_diag.py —— 把 eval_replay 的评分拆开看：到底是哪一类在拖后腿。

为什么需要
----------
总分 3.4 是个没有信息量的数字。真正有用的是：
    · 是群聊还是私聊低？
    · 是「被叫到」还是「自己插话」低？
    · 是问句答不上来，还是闲聊接不住？
    · 低分样本长什么样？

这个脚本只读 `data/eval/judged.jsonl`，不调模型、不花钱。

用法
----
    python scripts/eval_diag.py
    python scripts/eval_diag.py --worst 15
    python scripts/eval_diag.py --out data/eval/diag.md

判读原则
--------
· 群聊「主动插话」天然比「被 @"低 —— 别用一个总分去优化两种任务。
· judge 有 ±0.05 的抖动：两次差 < 0.15 视为没变化。
· 先看 worst 样本的「她回了什么」，再决定改 prompt 还是改上下文。
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVAL_DIR = Path("data/eval")
CUR_SEP = "以下是当前要回复的消息"
_FULFILL_PREFIX = (
    "搜索到的内容", "本地agent知识库图书馆内容", "你记得的", "更早的群聊记录",
    "QQ 操作结果", "看视频结果", "群里的人员信息", "关于「", "GLM 给的参考",
    "工具说明", "表情收藏结果", "你等了", "运势结果", "你记得",
)


# ---------- 从 trace 的 msgs 里还原「当前消息」 ----------
def _current_text(rec: dict) -> str:
    # 新版 replay 已经存了还原后的消息，直接用
    t = str(rec.get("text") or "").strip()
    if t and len(t) < 600 and "以下是背景" not in t and "【现在时间】" not in t:
        return t
    msgs = rec.get("msgs") or []
    for m in reversed(msgs):
        if str(m.get("role")) != "user":
            continue
        c = str(m.get("content") or "")
        # 补资料那一轮的 user 是系统结果，不是群友说的话
        if c.lstrip().startswith(_FULFILL_PREFIX) or "基于上面的资料" in c:
            continue
        if "要不要插话" in c or "你自己判断要不要插一句" in c:
            return "（主动插话：群里刚才的对话）"
        # 事前参考（GLM）被拼在 user 末尾，不算用户说的话
        for _mark in ("【另一个模型对同一段上下文", "【要求】事实、专有名词"):
            if _mark in c:
                c = c.split(_mark, 1)[0]
        if CUR_SEP in c:
            tail = c.split(CUR_SEP, 1)[-1]
            # 去掉分隔线剩下的「（重点看这条）-----------」这一行
            if "\n" in tail:
                tail = tail.split("\n", 1)[1]
            return tail.strip(" -\n")
        # 老格式：整段 build_prompt 都塞在 user 里，当前消息在最后一行
        lines = [x.strip() for x in c.splitlines()
                 if x.strip() and set(x.strip()) - {"-"}]
        return lines[-1] if lines else c.strip()
    return str(rec.get("text") or "")


def _system_blob(rec: dict) -> str:
    return "\n".join(str(m.get("content") or "") for m in (rec.get("msgs") or [])
                     if str(m.get("role")) == "system")


def _is_question(text: str) -> bool:
    return bool(re.search(r"[?？]|吗|呢|怎么|为什么|为啥|多少|哪[个里]|是不是|能不能", text or ""))


def _is_addressed(rec: dict) -> bool:
    """群里被 @ / 叫到名字的痕迹：上下文行首有 ⭐。"""
    return "⭐" in _system_blob(rec)


def _has_memory(rec: dict) -> bool:
    return "记忆目录" in _system_blob(rec)


def _is_old_format(rec: dict) -> bool:
    """老格式 = 整段 build_prompt 都塞在 user 里（今天拆分背景之前）。"""
    for m in rec.get("msgs") or []:
        if str(m.get("role")) != "user":
            continue
        c = str(m.get("content") or "")
        if "以下是背景" in c or "【现在时间】" in c:
            return True
    return False


def _is_interjection(rec: dict) -> bool:
    """主动插话 = random_chat_loop 生成的那条（user 里带着「要不要插话」指令）。"""
    for m in rec.get("msgs") or []:
        c = str(m.get("content") or "")
        if "要不要插话" in c or "你自己判断要不要插一句" in c:
            return True
    return False


def _bucket(n: int) -> str:
    if n < 10:
        return "<10字"
    if n <= 20:
        return "10-20字"
    return ">20字"


def _avg(rows, key):
    xs = [float(r["judge"][key]) for r in rows
          if isinstance(r.get("judge"), dict)
          and isinstance(r["judge"].get(key), (int, float))]
    return sum(xs) / len(xs) if xs else 0.0


def _table(title: str, groups: dict) -> list[str]:
    out = [f"\n{title}"]
    for name, rows in groups.items():
        if not rows:
            continue
        # 「选择不回」没有分数，不能算进平均（否则整组显示 0.00，看着像全崩了）
        scored = [r for r in rows if not r.get("silent")]
        silent = len(rows) - len(scored)
        if not scored:
            out.append(f"  {name:<14} {len(rows):>3} 条   全部选择不回（{silent}）")
            continue
        tail = f"   ｜ 沉默 {silent}" if silent else ""
        out.append(f"  {name:<14} {len(rows):>3} 条   "
                   f"相关性 {_avg(scored, 'relevant'):.2f}   "
                   f"贴题 {_avg(scored, 'on_topic'):.2f}   "
                   f"自然 {_avg(scored, 'natural'):.2f}{tail}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="评分诊断（不调模型）")
    ap.add_argument("--set", default=str(EVAL_DIR / "judged.jsonl"))
    ap.add_argument("--worst", type=int, default=12)
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    p = Path(a.set)
    if not p.exists():
        raise SystemExit(f"找不到 {p}，先跑 eval_replay.py judge")
    rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    rows = [r for r in rows if isinstance(r.get("judge"), dict) and not r["judge"].get("error")]
    if not rows:
        raise SystemExit("没有有效评分")

    lines = [f"评分诊断：{p}（{len(rows)} 条）"]

    # ---- 总体 ----
    lines.append(f"\n总体：相关性 {_avg(rows,'relevant'):.2f} ｜ "
                 f"贴题 {_avg(rows,'on_topic'):.2f} ｜ "
                 f"自然 {_avg(rows,'natural'):.2f}")

    # ---- 分群 ----
    groups = {"群聊": [], "私聊": []}
    for r in rows:
        groups["私聊" if str(r.get("key", "")).startswith("private") else "群聊"].append(r)
    lines += _table("按会话类型", groups)

    # ---- 群聊里：正常回复 vs 主动插话 ----
    # 主动插话是「没人叫她，她自己接一句」，天然比被叫到时低分。
    # 混在一起算平均，会让人误以为「她聊天能力差」。
    g_rows0 = [r for r in rows if not str(r.get("key", "")).startswith("private")]
    if g_rows0:
        groups = {"正常回复": [], "主动插话": []}
        for r in g_rows0:
            (groups["主动插话"] if _is_interjection(r) else groups["正常回复"]).append(r)
        lines += _table("群聊里按触发方式（主动插话天然低，别混着看）", groups)

    # ---- 样本质量（比分数更重要：脏样本会让分数没有意义）----
    shadow = [r for r in rows if str(r.get("key", "")).startswith("shadow_")]
    vision = [r for r in rows
              if "[图片识别结果]" in _system_blob(r) or "（群友发了张图片）" in _system_blob(r)]
    old_fmt = [r for r in rows if _is_old_format(r)]
    lines.append(f"\n样本质量：shadow {len(shadow)} 条 ｜ 图片 {len(vision)} 条 ｜ "
                 f"老格式 prompt {len(old_fmt)} 条")
    if shadow or vision:
        lines.append("  ⚠️ 这三类应该从评测集里剔除（重建时已加过滤），否则会拉低相关性")

    # 「是否被 @」这一维已停用：
    # ⭐ 标记是打在**历史行**上的，当前消息不在历史里，
    # 所以只要群里之前有人 @ 过她，整条记录就会被误判成「被叫到」。
    # 用「主动插话 / 正常回复」这个维度代替（它由提示词直接判定，可靠）。

    # ---- 问句 vs 闲聊 ----
    groups = {"问句": [], "非问句": []}
    for r in rows:
        (groups["问句"] if _is_question(_current_text(r)) else groups["非问句"]).append(r)
    lines += _table("按是否提问", groups)

    # ---- 回复长度 ----
    groups = {"<10字": [], "10-20字": [], ">20字": []}
    for r in rows:
        groups[_bucket(len(str(r.get("visible") or "")))].append(r)
    lines += _table("按回复长度", groups)

    # ---- 有没有记忆块 ----
    groups = {"有记忆目录": [], "无记忆目录": []}
    for r in rows:
        (groups["有记忆目录"] if _has_memory(r) else groups["无记忆目录"]).append(r)
    lines += _table("按上下文是否带记忆目录", groups)

    # ---- 低分里「问句但没答」 ----
    bad_q = [r for r in rows
             if _is_question(_current_text(r))
             and float((r.get("judge") or {}).get("relevant") or 5) <= 2]
    lines.append(f"\n问句但相关性 ≤2：{len(bad_q)} 条（最该先修的就是这类）")

    # ---- worst 样本 ----
    def _score(r):
        j = r.get("judge") or {}
        return (float(j.get("relevant") or 0), float(j.get("on_topic") or 0))

    # 选择不回的样本没有分数，别混进 worst（会显示成 [None/None]）
    cand = [r for r in rows if not r.get("silent")]
    worst = sorted(cand, key=_score)[: max(0, a.worst)]
    lines.append(f"\n最差 {len(worst)} 条（已排除「选择不回」）：")
    for r in worst:
        j = r.get("judge") or {}
        q = _current_text(r).replace("\n", " ")[:120]
        ans = str(r.get("visible") or "（没说话）").replace("\n", " ")[:120]
        lines.append(
            f"\n[{j.get('relevant')}/{j.get('on_topic')}] {r.get('key','')} {r.get('time','')}\n"
            f"  群友：{q}\n"
            f"  她  ：{ans}\n"
            f"  评语：{str(j.get('reason') or '')[:80]}"
        )

    text = "\n".join(lines)
    print(text)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(text, encoding="utf-8")
        print(f"\n已写入 {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
