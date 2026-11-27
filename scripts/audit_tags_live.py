#!/usr/bin/env python3
# scripts/audit_tags_live.py
"""线上收发件人标签体检（只读，不改任何东西）。

为什么要有（2026-11-27 用户：「用 ssh 监控 napcat log 和 bot log，看功能是否达到预期」）：
  `docs/reply-audit-20261127.md` 复盘出 3 类问题（接歪 / 人称错 / 命令装死），
  改完之后需要**持续盯**这几个指标，而不是改完就完事。

看 5 件事（都是「应该为 0」或「应该 100%」的硬指标）：
  1. 无 ⭐ 段的最长行数   —— 她主动插话时只该给 SEG_INSERT_WINDOW=5 条
  2. 「→ 你」但正文 @ 的是别人 —— 人称错（2026-11-27 修过）
  3. 模型写了段外 `<reply:>` —— 发送层会拦，但说明她想回旧消息（容易接歪）
  4. 昵称含人称代词却没加「」 —— `我的床会长猫` 这种会被当成句子
  5. 新 prompt 两句覆盖率 —— 确认跑的是新代码

用法（部署机上）：
    python3 scripts/audit_tags_live.py                # 最近 6 小时
    python3 scripts/audit_tags_live.py --hours 24
    python3 scripts/audit_tags_live.py --hours 3 --show 5   # 顺带打印 5 条反例
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRACE = ROOT / "data" / "llm_trace"

ROW = re.compile(r"^\[(\d+)\]\s*(⭐)?#?(\d*)\s*(.+?)\((\d{6,12})\)\s*→\s*([^:：]+)[:：]\s*(.*)$")
AT = re.compile(r"@[^@\s()（）]{1,20}[(（](\d{6,12})[)）]")
QUOTED_NAME = re.compile(r"「([^」]{3,30})」\(\d{6,12}\)")
PRONOUN = re.compile(r"(你|我|他|她|咱|大家|我们|你们|他们)")
SELF = {"100000010", "100000018"}     # 她的号（含历史号）；换号后按 data/self_id.json 改
NEW_PROMPT = ("只能引本段", "没有 ⭐ 就是你在插话")


def load(hours: float, since: str = "") -> list:
    if since:
        cutoff = time.mktime(time.strptime(since, "%Y-%m-%d %H:%M"))
    else:
        cutoff = time.time() - hours * 3600
    out = []
    for f in sorted(TRACE.glob("*.jsonl"))[-3:]:
        try:
            fh = f.open(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if not str(r.get("key") or "").startswith("group_"):
                continue
            if float(r.get("ts") or 0) < cutoff:
                continue
            out.append(r)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="线上收发件人标签体检")
    ap.add_argument("--hours", type=float, default=6.0)
    ap.add_argument("--since", default="",
                    help='只看这个时间之后（本地时间，如 "2026-11-27 16:15"）')
    ap.add_argument("--show", type=int, default=0, help="打印几条反例")
    args = ap.parse_args()

    recs = load(args.hours, args.since)
    if not recs:
        print("最近 %.1fh 没有群聊请求（trace 里没样本）" % args.hours)
        return 0

    no_star = []          # (行数, time)
    bad_at = []           # 「→ 你」但 @ 别人
    outside_quote = []    # 段外 <reply:>
    unquoted = []         # 昵称含代词却没加「」
    new_ok = 0

    for r in recs:
        text = str(r.get("text") or "")
        reply = str(r.get("reply") or "")
        ts = float(r.get("ts") or 0)
        if all(k in text for k in NEW_PROMPT):
            new_ok += 1
        rows = [x.strip() for x in text.splitlines() if re.match(r"^\[\d+\]", x.strip())]
        if rows and not any("⭐" in x for x in rows):
            no_star.append((len(rows), ts))
        mids = set(re.findall(r"#(\d{6,})", text))
        for q in re.findall(r"<reply:(\d+)>", reply):
            if q not in mids:
                outside_quote.append((q, ts))
                break
        for x in rows:
            m = ROW.match(x)
            if not m:
                continue
            name, to, body = m.group(4).strip(), m.group(6).strip(), m.group(7)
            ats = set(AT.findall(body))
            # 只算「纯 @ 别人」；「引用她 + 正文里 @ 别人」是混合消息，
            # 标 → 你 是对的（她确实被引用了），不算人称错。
            _quoted_me = "〔引用 大肥鱼" in body
            if to == "你" and ats and not (ats & SELF) and not _quoted_me:
                bad_at.append((x[:100], ts))
            if len(name) >= 3 and PRONOUN.search(name) and not name.startswith("「"):
                unquoted.append((x[:100], ts))

    n = len(recs)
    print("=" * 62)
    _win = ("自 %s 起" % args.since) if args.since else ("最近 %.1f 小时" % args.hours)
    print("收发件人标签体检｜%s｜群请求 %d 条" % (_win, n))
    print("=" * 62)
    over = [x for x in no_star if x[0] > 5]
    print("1) 无⭐段（她主动插话）：%d 段｜最长 %s 行｜>5 行的 %d 段  %s"
          % (len(no_star), max([x[0] for x in no_star], default=0), len(over),
             "✅" if not over else "❌（SEG_INSERT_WINDOW 没生效？）"))
    print("2) 「→ 你」但 @ 的是别人：%d 条  %s"
          % (len(bad_at), "✅" if not bad_at else "❌"))
    print("3) 模型写了段外 <reply:>：%d 条  %s"
          % (len(outside_quote),
             "✅" if not outside_quote else "⚠️（发送层会拦，但说明她想回旧消息）"))
    print("4) 昵称含人称代词却没加「」：%d 行  %s"
          % (len(unquoted), "✅" if not unquoted else "❌"))
    print("5) 新 prompt 覆盖率：%d/%d（%.0f%%）  %s"
          % (new_ok, n, 100.0 * new_ok / max(1, n),
             "✅" if new_ok == n else "⚠️（有旧进程/旧代码在跑？）"))

    if args.show:
        for title, items in (("无⭐段 >5 行", [x[0] for x in over]),
                             ("「→ 你」但 @ 别人", [x[0] for x in bad_at]),
                             ("段外引用", ["<reply:%s>" % x[0] for x in outside_quote]),
                             ("昵称没加「」", [x[0] for x in unquoted])):
            if not items:
                continue
            print("\n—— %s ——" % title)
            for it in items[:args.show]:
                print("   " + str(it))
    return 0


if __name__ == "__main__":
    sys.exit(main())
