#!/usr/bin/env python
"""
trace_reply.py —— 把小鲸鱼每次回复的「三份视角」并排摆出来，专门用来查上下文问题。

三份视角：
  A. DeepSeek **实际看到**的内容（system 人格 / 档案 / 记忆 / 历史对话 / 当前消息…）
     ← 来自 data/llm_trace/*.jsonl（services/llm.py::_trace_dump 落的盘）
  B. 她**实际回**的内容
  C. **上帝视角**：那一条消息前后各 N 条真实群聊（含她自己说的、另一只鱼说的）
     ← 来自 data/dialogue/*.jsonl，可选叠加 NapCat 容器日志

用法：
    # 今天最近 20 次回复，前后各 20 条
    python scripts/trace_reply.py

    # 指定日期 / 条数 / 上下文宽度
    python scripts/trace_reply.py --date 2026-11-10 --limit 10 --before 20 --after 20

    # 只看某个群
    python scripts/trace_reply.py --key group_100000017

    # 叠加容器日志（docker logs qq-grok-bot > napcat.log 之后）
    python scripts/trace_reply.py --log napcat.log

    # 输出成文件，直接拿去对比
    python scripts/trace_reply.py --limit 10 --out data/trace_report.md

    # 不截断，把 prompt 全文打出来（会很长）
    python scripts/trace_reply.py --limit 3 --full
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRACE_DIR = Path(ROOT / "data" / "llm_trace")
DIA_DIR = Path(ROOT / "data" / "dialogue")

# 容器日志里 nonebot 打的收消息行：
#   qq-grok-bot  | 11-10 20:33:10 [SUCCESS] nonebot | OneBot V11 123456789 | \
#   [message.group.normal]: Message 781523941 from 354783017@[群:100000017] '小肥鱼呢'
_LOG_MSG_RE = re.compile(
    r"\[message\.(?P<scope>group|private)\.\w+\]:\s*Message\s+(?P<mid>\d+)\s+"
    r"from\s+(?P<uid>\d+)(?:@\[群:(?P<gid>\d+)\])?\s+'(?P<text>.*)'\s*$"
)
_LOG_TIME_RE = re.compile(r"\b(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):(\d{2})\b")


# ============================================================
# 数据加载
# ============================================================
def load_jsonl(p: Path) -> list:
    out = []
    try:
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
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


def load_traces(days: int, key: str = "") -> list:
    """读 data/llm_trace 里的每次调用记录，按时间正序。"""
    if not TRACE_DIR.exists():
        return []
    files = sorted(TRACE_DIR.glob("*.jsonl"))[-max(1, days):]
    recs = []
    for f in files:
        recs.extend(load_jsonl(f))
    if key:
        recs = [r for r in recs if str(r.get("key") or "") == key]
    recs.sort(key=lambda r: r.get("ts", 0))
    return recs


def load_dialogue(days: int) -> list:
    """读 data/dialogue 的真实消息流（user / bot / peer）。"""
    if not DIA_DIR.exists():
        return []
    files = sorted(DIA_DIR.glob("*.jsonl"))[-max(1, days):]
    recs = []
    for f in files:
        recs.extend(load_jsonl(f))
    recs.sort(key=lambda r: r.get("ts", 0))
    return recs


def _read_log_text(path: str, container: str, since: str) -> str:
    """读容器日志：优先 --log 文件，其次 --docker 直接拉。"""
    if path:
        p = Path(path)
        if not p.exists():
            print(f"⚠️ 日志文件不存在：{path}", file=sys.stderr)
            return ""
        return p.read_text(encoding="utf-8", errors="ignore")
    if container:
        import subprocess
        try:
            r = subprocess.run(["docker", "logs", "--since", since, container],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=120)
            if r.returncode != 0:
                print(f"⚠️ docker logs 失败：{(r.stderr or '')[-200:]}", file=sys.stderr)
                return ""
            return r.stdout or ""
        except Exception as e:
            print(f"⚠️ docker logs 异常：{type(e).__name__}: {e}", file=sys.stderr)
            return ""
    return ""


def load_container_log(path: str, year: int, container: str = "",
                       since: str = "48h") -> list:
    """
    解析 NapCat/nonebot 容器日志里的收消息行。

    只取「收到的消息」，不含她自己发的（nonebot 发送不打内容）；
    她那部分由 dialogue_log 的 kind="bot" 补齐。
    """
    text = _read_log_text(path, container, since)
    if not text:
        return []
    out = []
    for line in text.splitlines():
        m = _LOG_MSG_RE.search(line)
        if not m:
            continue
        tm = _LOG_TIME_RE.search(line)
        ts = 0.0
        if tm:
            mo, dd, hh, mi, ss = (int(x) for x in tm.groups())
            try:
                ts = time.mktime((year, mo, dd, hh, mi, ss, 0, 0, -1))
            except Exception:
                ts = 0.0
        gid = m.group("gid") or ""
        out.append({
            "ts": ts,
            "gid": gid if gid else f"private_{m.group('uid')}",
            "who": f"QQ{m.group('uid')}",
            "qq": m.group("uid"),
            "kind": "user",
            "text": m.group("text"),
            "_from_log": True,
        })
    out.sort(key=lambda r: r.get("ts", 0))
    return out


def merge_stream(dia: list, log: list) -> list:
    """dialogue + 容器日志合并去重（同一时刻同一句话只留一条，优先 dialogue）。"""
    if not log:
        return dia
    if not dia:
        return log
    seen = set()
    for r in dia:
        seen.add((int(r.get("ts", 0)), str(r.get("text", ""))[:40]))
    merged = list(dia)
    for r in log:
        k = (int(r.get("ts", 0)), str(r.get("text", ""))[:40])
        if k in seen:
            continue
        seen.add(k)
        merged.append(r)
    merged.sort(key=lambda r: r.get("ts", 0))
    return merged


# ============================================================
# 报告
# ============================================================
def _clip(s: str, n: int) -> str:
    s = str(s or "")
    if n <= 0 or len(s) <= n:
        return s
    return s[:n] + f"\n…（截断，原 {len(s)} 字，加 --full 看全文）"


def _fmt_ts(ts: float) -> str:
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(ts)))
    except Exception:
        return "??:??:??"


def gid_of_key(key: str) -> list:
    """
    session_key → dialogue 里可能的 gid 写法。

    群的 gid 是裸号，私聊带 private_ 前缀，影子回答是 shadow_ 前缀
    （shadow_100000017 也是那个群的影子回答，C 段应该显示这个群的上帝视角）。
    """
    k = str(key or "")
    for pre in ("shadow_", "group_"):
        if k.startswith(pre):
            return [k[len(pre):], k]
    return [k]


# ============================================================
# 隐藏标记：模型输出里那些「用户看不到、被系统吃掉」的东西
# ============================================================
_MARK_RE = re.compile(r"\[\[([A-Za-z_]+)\|([^\]]*)\]\]")
_STICKER_RE = re.compile(r"<sticker:([^>]*)>")

_HIDDEN_KIND = {
    "MEMO": "写记忆（用户看不到）",
    "FOCUS": "标注在回应谁（用户看不到）",
    "CALL": "改称呼档案（用户看不到）",
    "AT": "真 @ 某人（会渲染成 @，用户看得到）",
    "WHO": "查群成员（工具调用）",
    "SEARCH": "联网搜（工具调用）",
    "LIBRARY": "查图书馆（工具调用）",
    "SING": "唱歌（工具调用）",
    "VOICE": "发语音（用户能听到）",
}


def _visible_text(reply: str) -> str:
    """去掉所有标记和表情标签后，用户实际看到的文字。"""
    t = _MARK_RE.sub("", str(reply or ""))
    t = _STICKER_RE.sub("", t)
    t = re.sub(r"\|\|\|", "\n", t)          # 分条符还原成换行
    return t.strip()


def _mark_rows(reply: str) -> list:
    """把隐藏标记拆成 [(名字, 内容, 备注)]，顺带做合法性检查。"""
    rows = []
    for name, body in _MARK_RE.findall(str(reply or "")):
        name_u = name.upper()
        note = _HIDDEN_KIND.get(name_u, "")
        if not note:
            note = "⚠️ 未知标记 —— 系统不会处理，等于白写"
        elif name_u == "MEMO":
            # MEMO 的合法格式：emotion:xxx|text:xxx
            if "text:" not in body:
                note += "｜⚠️ 缺 text: 字段，记忆可能写不进去"
            elif "emotion:" not in body:
                note += "｜⚠️ 缺 emotion: 字段"
        elif name_u == "AT" and not body.strip():
            note += "｜⚠️ @ 后面是空的"
        rows.append((name_u, body, note))
    return rows


# ============================================================
# 附录：另一只鱼的对话 / 裁判样本（影子回答）
# ============================================================
def load_jsonl_file(p: Path, days: int) -> list:
    if not p.exists():
        return []
    recs = load_jsonl(p)
    if days > 0:
        cut = time.time() - days * 86400
        recs = [r for r in recs if float(r.get("ts") or 0) >= cut]
    recs.sort(key=lambda r: float(r.get("ts") or 0))
    return recs


def render_peer_chat(recs: list, limit: int) -> str:
    L = ["## 附录 A：两条鱼的对话（只留档，不发群）", ""]
    if not recs:
        L.append("> `data/peer_chat.jsonl` 里这段时间没有记录")
        L.append("")
        return "\n".join(L)
    L.append(f"共 {len(recs)} 条，显示最近 {min(limit, len(recs))} 条：")
    L.append("")
    L.append("| 时间 | 群 | 谁 | 内容 |")
    L.append("|---|---|---|---|")
    for r in recs[-limit:]:
        L.append(f"| {time.strftime('%H:%M:%S', time.localtime(float(r.get('ts') or 0)))} "
                 f"| {r.get('gid', '')} | {r.get('who', '')} | {str(r.get('text', ''))[:140]} |")
    L.append("")
    return "\n".join(L)


def render_samples(recs: list, limit: int) -> str:
    L = ["## 附录 B：裁判样本（含影子回答，全部不发群）", ""]
    if not recs:
        L.append("> `data/referee_samples.jsonl` 里这段时间没有记录")
        L.append("")
        return "\n".join(L)
    L.append(f"共 {len(recs)} 条，显示最近 {min(limit, len(recs))} 条：")
    L.append("")
    for r in recs[-limit:]:
        ts = float(r.get("ts") or 0)
        L.append(f"### {time.strftime('%H:%M:%S', time.localtime(ts))} · {r.get('gid', '')}"
                 + ("｜✅ 参考改写了答案" if r.get("changed") else ""))
        L.append("")
        L.append(f"- 问题：{str(r.get('question', ''))[:120]}")
        if r.get("our"):
            L.append(f"- 我们的（影子）回答：{str(r['our'])[:200]}")
        if r.get("ref"):
            L.append(f"- GLM 参考：{str(r['ref'])[:200]}")
        if r.get("ref2"):
            L.append(f"- 那只鱼的回答：{str(r['ref2'])[:200]}")
        if r.get("diff"):
            L.append(f"- 差异：{str(r['diff'])[:300]}")
        L.append("")
    return "\n".join(L)


def stream_of(stream: list, key: str) -> list:
    gids = set(gid_of_key(key))
    return [r for r in stream if str(r.get("gid") or "") in gids]


def window_around(msgs: list, ts: float, before: int, after: int,
                  trigger_text: str = "") -> tuple:
    """
    取 ts 时刻前后的消息。返回 (前面的, 后面的)。

    触发的那条单独用 ▶ 行显示，所以要从「前面的」里剔掉，避免同一句出现两次。
    """
    left = [r for r in msgs if r.get("ts", 0) <= ts]
    right = [r for r in msgs if r.get("ts", 0) > ts]
    if left and trigger_text:
        tail = left[-1]
        if (abs(float(tail.get("ts", 0)) - ts) < 2.0
                and str(tail.get("text", ""))[:40] == str(trigger_text)[:40]):
            left = left[:-1]
    return left[-before:] if before > 0 else [], right[:after] if after > 0 else []


def render_one(idx: int, rec: dict, stream: list, args) -> str:
    key = str(rec.get("key") or "")
    ts = float(rec.get("ts") or 0)
    L = []
    L.append(f"## {idx}) {rec.get('time') or _fmt_ts(ts)} · `{key}`"
             + (f" · 人格 {rec.get('persona')}" if rec.get("persona") else ""))
    L.append("")
    L.append(f"- 当前消息：「{rec.get('text', '')}」"
             + (f"（来自 {rec.get('speaker_name') or rec.get('speaker_qq')}）"
                if (rec.get("speaker_name") or rec.get("speaker_qq")) else ""))
    L.append(f"- API 输入：{rec.get('n_msgs', '?')} 块 / {rec.get('prompt_chars', '?')} 字"
             + (f"｜历史对话 {rec.get('group_context_n')} 条"
                if rec.get("group_context_n") else ""))
    if rec.get("truncated"):
        L.append("- ⚠️ 这条 prompt 超过 LLM_TRACE_MAX_CHARS，落盘可能不完整")
    L.append("")

    # ---------- A. API 看到的内容 ----------
    L.append("### A. DeepSeek 实际看到的内容")
    for i, b in enumerate(rec.get("msgs") or [], 1):
        c = str(b.get("content") or "")
        L.append(f"**块 {i} · {b.get('role')}**（{len(c)} 字）")
        L.append("")
        L.append("```text")
        L.append(_clip(c, 0 if args.full else args.block_chars))
        L.append("```")
        L.append("")

    # ---------- B. 她的回复 ----------
    _raw = rec.get("reply") or ""
    L.append("### B. 模型原始输出（含隐藏标记）")
    L.append("```text")
    L.append(_clip(_raw or "(空)", 0 if args.full else 4000))
    L.append("```")

    L.append("### B1. 用户实际看到的")
    L.append("```text")
    L.append(_visible_text(_raw) or "（什么都没发出去 / 只有隐藏标记）")
    L.append("```")

    _rows = _mark_rows(_raw)
    if _rows:
        L.append("### B2. 隐藏标记（被系统吃掉的部分）")
        for _n, _b, _note in _rows:
            L.append(f"- `[[{_n}]]` → {_note}")
            if _b:
                L.append(f"  `{_b[:200]}`")
        L.append("")

    if rec.get("reason"):
        L.append(f"<sub>思维链：{_clip(rec['reason'], 300)}</sub>")
    L.append("")

    # ---------- C. 上帝视角 ----------
    sub = stream_of(stream, key)
    left, right = window_around(sub, ts, args.before, args.after,
                                trigger_text=str(rec.get("text") or ""))
    L.append(f"### C. 上帝视角（同一会话，前 {len(left)} / 后 {len(right)} 条）")
    if not sub:
        L.append("> ⚠️ dialogue_log 里没有这个会话的记录（DIALOGUE_LOG 关着？或日期不对）")
    else:
        L.append("| | 时间 | 谁 | 内容 |")
        L.append("|---|---|---|---|")
        for r in left:
            L.append(f"| - | {_fmt_ts(r.get('ts', 0))} | {r.get('who', '')} "
                     f"| {str(r.get('text', ''))[:120]} |")
        L.append(f"| ▶ | {_fmt_ts(ts)} | **触发这条** | **{str(rec.get('text', ''))[:120]}** |")
        for r in right:
            mark = "🐟" if r.get("kind") == "bot" else ""
            L.append(f"| + | {_fmt_ts(r.get('ts', 0))} | {r.get('who', '')}{mark} "
                     f"| {str(r.get('text', ''))[:120]} |")
    L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="小鲸鱼回复的「API 视角 vs 上帝视角」对比报告")
    ap.add_argument("--date", default="", help="只取这一天（YYYY-MM-DD），默认最近 N 天")
    ap.add_argument("--days", type=int, default=1, help="取最近几天（默认 1）")
    ap.add_argument("--limit", type=int, default=20, help="最近 N 次回复（默认 20）")
    ap.add_argument("--before", type=int, default=20, help="上帝视角：前 N 条（默认 20）")
    ap.add_argument("--after", type=int, default=20, help="上帝视角：后 N 条（默认 20）")
    ap.add_argument("--key", default="", help="只看某个会话，如 group_100000017")
    ap.add_argument("--block-chars", type=int, default=700,
                    help="A 段每块截断字数（默认 700，0=不截断）")
    ap.add_argument("--full", action="store_true", help="不截断，打全文")
    ap.add_argument("--log", default="", help="NapCat/容器日志文件（docker logs 导出的）")
    ap.add_argument("--docker", default="", help="直接拉容器日志，如 --docker qq-grok-bot")
    ap.add_argument("--since", default="48h", help="--docker 的时间范围（默认 48h）")
    ap.add_argument("--out", default="", help="写成 markdown 文件")
    ap.add_argument("--no-peer", action="store_true", help="不打印两条鱼的对话")
    ap.add_argument("--no-samples", action="store_true", help="不打印裁判样本/影子回答")
    ap.add_argument("--peer-limit", type=int, default=60, help="附录里最多显示多少条（默认 60）")
    args = ap.parse_args()

    days = args.days
    if args.date:
        d = Path(args.date)
        try:
            ts = time.mktime(time.strptime(args.date, "%Y-%m-%d"))
        except Exception:
            print(f"❌ --date 格式要是 YYYY-MM-DD：{args.date}", file=sys.stderr)
            return 2
        days = max(1, int((time.time() - ts) / 86400) + 1)
        _ = d

    traces = load_traces(days, args.key)
    if args.date:
        traces = [r for r in traces
                  if time.strftime("%Y-%m-%d", time.localtime(r.get("ts", 0))) == args.date]
    if not traces:
        print("❌ 没有 llm_trace 记录。\n"
              "   · 确认 .env 里 LLM_TRACE=true（默认开）\n"
              "   · 新代码部署后要有过对话才会落盘\n"
              f"   · 目录：{TRACE_DIR}", file=sys.stderr)
        return 1
    traces = traces[-max(1, args.limit):]

    dia = load_dialogue(days)
    if args.date:
        dia = [r for r in dia
               if time.strftime("%Y-%m-%d", time.localtime(r.get("ts", 0))) == args.date]
    year = int((args.date or time.strftime("%Y-%m-%d"))[:4])
    log_msgs = (load_container_log(args.log, year, args.docker, args.since)
                if (args.log or args.docker) else [])
    stream = merge_stream(dia, log_msgs)

    head = [
        "# 小鲸鱼回复对比报告",
        "",
        f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 回复条数：{len(traces)}"
        + (f"｜会话：{args.key}" if args.key else ""),
        f"- 上帝视角：前 {args.before} / 后 {args.after} 条"
        f"（消息源：dialogue {len(dia)} 条"
        + (f" + 容器日志 {len(log_msgs)} 条" if log_msgs else "") + "）",
        "",
        "对比要点：**A 段里有没有 C 段明明存在的关键消息** —— 缺了就是上下文没进 prompt；"
        "有但回错了，就是理解/提示词的问题。",
        "",
        "---",
        "",
    ]

    body = []
    for i, rec in enumerate(traces, 1):
        body.append(render_one(i, rec, stream, args))
        body.append("---")
        body.append("")

    # ---------- 附录：鱼对话 + 裁判样本（影子回答）----------
    appendix = []
    if not args.no_peer:
        appendix.append(render_peer_chat(
            load_jsonl_file(ROOT / "data" / "peer_chat.jsonl", days),
            args.peer_limit))
        appendix.append("---")
        appendix.append("")
    if not args.no_samples:
        appendix.append(render_samples(
            load_jsonl_file(ROOT / "data" / "referee_samples.jsonl", days),
            args.peer_limit))
        appendix.append("")

    text = "\n".join(head + body + appendix)
    if args.out:
        outp = Path(args.out)
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text(text, encoding="utf-8")
        print(f"✅ 已写入 {outp}（{len(text)} 字，{len(traces)} 次回复）")
    else:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
