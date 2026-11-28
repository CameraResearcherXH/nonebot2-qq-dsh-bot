#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""闭环回放：把**真实历史群聊消息**重新喂进真实聊天管线，看整条决策链。

与 eval_replay.py 的区别：
  · eval_replay 只回放冻结的 msgs（比 prompt A/B）；
  · 本脚本从 data/dialogue 取历史消息，构造 OneBot 群消息事件，走真实
    matcher 链（early_observe → _need_reply → 回复流水线 → call_llm → 解析），
    用假 Bot 拦截所有 send_*，回复只记录不外发。

安全边界：
  · 必须在沙箱 cwd 下运行（/tmp/replay_app），数据是线上 data/ 的副本；
  · DIAG/TRACE/用量库全部重定向到沙箱内的 replay_* 文件；
  · 不连接 NapCat、不注册 wss、不触发任何外发。

用法（容器内）：
  docker exec -w /tmp/replay_app qq-grok-bot python /app/scripts/replay_closed_loop.py \
      --gid 100000016 --limit 12

可选：
  --buckets addr,cmd,q,vent,short,normal   参与回放的场景（默认全开）
  --per-bucket 3     每类取几条
  --gap 2.0          两条之间的间隔秒数（背景任务收尾）
  --settle 3.0       handle_event 返回后等后台诊断事件的秒数
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SANDBOX_MARK = "replay_app"

AT_RE = re.compile(r"@([^@()\s]{1,24})\((\d+)\)")
VENT_RE = re.compile(r"累|烦|难受|崩溃|哭了|想哭|生气|讨厌|无语|压力")


def _prep_env(args) -> Path:
    sandbox = Path(args.sandbox).resolve()
    if SANDBOX_MARK not in str(sandbox) or not sandbox.is_dir():
        raise SystemExit(f"拒绝在没有沙箱标记的目录跑回放: {sandbox}")
    os.chdir(sandbox)
    # 线上进程的配置来自 docker compose env_file（创建时烘进去的），
    # 回放进程没有这个环境，必须自己读同一份 .env，否则 POWER_MODE /
    # GROUP_WATCH_MODE 等开关和线上不一致（eco 回放成 full）。
    from dotenv import load_dotenv
    _envf = os.getenv("REPLAY_ENV_FILE", "/app/.env")
    if os.path.isfile(_envf):
        load_dotenv(_envf, override=False)
    os.environ["DIAG_DIR"] = "data/replay_diag"
    os.environ["LLM_TRACE"] = "true"
    os.environ["LLM_TRACE_DIR"] = "data/replay_trace"
    os.environ["PIPE_METRICS_FILE"] = "data/replay_metrics.jsonl"
    os.environ["LLM_USAGE_DB"] = "data/replay_usage.db"
    # 回放不占线上日预算：给一个极高的上限（真实 API 费用照常发生）
    os.environ["LLM_DAILY_BUDGET"] = "999999999"
    (sandbox / "data/replay_diag").mkdir(parents=True, exist_ok=True)
    (sandbox / "data/replay_trace").mkdir(parents=True, exist_ok=True)
    return sandbox


# ---------------- 历史消息 → 场景分桶 ----------------
def _load_history(gid: str, days: int) -> list:
    rows = []
    files = sorted(Path("data/dialogue").glob("*.jsonl"), reverse=True)[:max(1, days)]
    for f in files:
        for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                r = json.loads(line)
            except Exception:
                continue
            if str(r.get("gid")) != str(gid) or r.get("kind") != "user":
                continue
            t = str(r.get("text") or "").strip()
            if not t or len(t) > 300:
                continue
            r["_text"] = t
            rows.append(r)
    rows.sort(key=lambda r: float(r.get("ts") or 0))
    return rows


def _bucket_of(text: str, self_id: str) -> str:
    if f"({self_id})" in text:
        return "addr"
    if text.startswith("/"):
        return "cmd"
    if "？" in text or "?" in text:
        return "q"
    if VENT_RE.search(text):
        return "vent"
    if len(text) <= 6:
        return "short"
    return "normal"


def _pick(rows: list, self_id: str, buckets: set, per: int) -> list:
    by = {}
    for r in rows:
        b = _bucket_of(r["_text"], self_id)
        if b in buckets:
            by.setdefault(b, []).append(r)
    picked = []
    for b, lst in by.items():
        step = max(1, len(lst) // max(1, per))
        picked.extend(lst[::step][:per])
    picked.sort(key=lambda r: float(r.get("ts") or 0))
    return picked


# ---------------- 假 Bot：拦截全部外发 ----------------
class _Recorder:
    def __init__(self):
        self.sends = []
        self.api_calls = []


REC = _Recorder()


def _make_bot_classes():
    from nonebot.adapters.onebot.v11 import Adapter, Bot

    class ReplayAdapter(Adapter):
        pass

    class ReplayBot(Bot):
        async def call_api(self, api: str, **data):
            REC.api_calls.append({"api": api})
            if api.startswith("send_"):
                REC.sends.append({"api": api, **data})
                return {"message_id": 900000 + len(REC.sends)}
            if api == "get_login_info":
                return {"user_id": int(self.self_id), "nickname": "回放小鲸鱼"}
            if api == "get_group_info":
                return {"group_id": int(data.get("group_id") or 0),
                        "group_name": "回放群", "member_count": 50}
            if api == "get_group_member_info":
                return {"user_id": int(data.get("user_id") or 0),
                        "nickname": "群友", "card": "", "role": "member"}
            if api == "get_stranger_info":
                return {"user_id": int(data.get("user_id") or 0),
                        "nickname": "群友"}
            if api in ("get_msg", "get_group_msg_history"):
                return {"message_id": 0, "messages": []}
            return {}

    return ReplayAdapter, ReplayBot


def _build_event(rec: dict, self_id: str, mid: int):
    from nonebot.adapters.onebot.v11 import GroupMessageEvent

    text = rec["_text"]
    segs, pos, mentioned = [], 0, False
    for m in AT_RE.finditer(text):
        pre = text[pos:m.start()]
        if pre.strip():
            segs.append({"type": "text", "data": {"text": pre}})
        qq = m.group(2)
        segs.append({"type": "at", "data": {"qq": qq}})
        if qq == self_id:
            mentioned = True
        pos = m.end()
    rest = text[pos:]
    if rest.strip() or not segs:
        segs.append({"type": "text", "data": {"text": rest or text}})
    who = str(rec.get("who") or "").strip()
    nick = re.sub(r"\((\d+)\)$", "", who) or str(rec.get("qq") or "群友")
    return GroupMessageEvent.model_validate({
        "time": int(float(rec.get("ts") or time.time())),
        "self_id": int(self_id),
        "post_type": "message",
        "sub_type": "normal",
        "user_id": int(rec.get("qq") or 0),
        "message_type": "group",
        "group_id": int(rec.get("gid") or 0),
        "message_id": mid,
        "message": segs,
        "original_message": segs,
        "raw_message": text,
        "font": 0,
        "sender": {"user_id": int(rec.get("qq") or 0),
                   "nickname": nick, "card": ""},
        "to_me": mentioned,
    })


# ---------------- 诊断读取 ----------------
def _diag_path() -> Path:
    d = Path(os.environ.get("DIAG_DIR", "data/replay_diag"))
    return d / f"{time.strftime('%Y-%m-%d')}.jsonl"


def _read_new(offset: int):
    p = _diag_path()
    if not p.exists():
        return [], offset
    size = p.stat().st_size
    if size <= offset:
        return [], offset
    with p.open("rb") as f:
        f.seek(offset)
        chunk = f.read(size - offset)
    rows = []
    for line in chunk.decode("utf-8", "ignore").splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows, size


def _sent_text(send: dict) -> str:
    msg = send.get("message")
    if isinstance(msg, str):
        return msg
    out = []
    for seg in msg or []:
        if isinstance(seg, dict):
            t, d = seg.get("type"), seg.get("data") or {}
        else:
            t, d = getattr(seg, "type", ""), getattr(seg, "data", None) or {}
        if t == "text":
            out.append(str(d.get("text") or ""))
        elif t == "at":
            out.append(f"@{d.get('qq')}")
        elif t == "image":
            out.append("[图片]")
        elif t == "record":
            out.append("[语音]")
        elif t == "reply":
            out.append("")
        elif t:
            out.append(f"[{t}]")
    return "".join(out)


def _summ(rows: list) -> dict:
    s = {"view": [], "recall": [], "tools": [], "write": [], "attr": [],
         "prompt": [], "qwen": [], "atmo": [], "query": []}
    for r in rows:
        k = r.get("kind")
        if k in s:
            s[k].append(r)
    return s


async def _main(args) -> int:
    sandbox = _prep_env(args)

    self_id = args.self_id
    sid_file = sandbox / "data/self_id.json"
    if sid_file.exists():
        try:
            d = json.loads(sid_file.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                d = d.get("self_id") or (d.get("accounts") or [""])[0]
            if d:
                self_id = str(d)
        except Exception:
            pass

    rows = _load_history(args.gid, args.days)
    if not rows:
        print("没有可用历史消息")
        return 1
    buckets = {b.strip() for b in args.buckets.split(",") if b.strip()}
    picked = _pick(rows, self_id, buckets, args.per_bucket)
    picked = picked[: args.limit]
    print(f"沙箱 {sandbox}｜历史 {len(rows)} 条 → 回放 {len(picked)} 条")

    import nonebot
    nonebot.init()
    driver = nonebot.get_driver()
    ReplayAdapter, ReplayBot = _make_bot_classes()
    driver.register_adapter(ReplayAdapter)
    nonebot.load_plugin("plugins.chat")
    # 工具可用性依赖 tool_dispatcher 的心跳状态；线上算力机此刻在线，
    # 回放里补一个新鲜心跳，否则 sing/video 等会被误判「算力机离线」
    try:
        nonebot.load_plugin("plugins.tool_dispatcher")
        from plugins.tool_dispatcher import STATUS as _TS
        _TS["last_heartbeat"] = time.time()
        _TS["caps"] = {"audio_separation", "cover", "draw", "probe", "reflection",
                       "rvc", "rvc_mix", "split", "tts"}
    except Exception as e:
        print(f"tool_dispatcher 加载失败（工具可用性会偏保守）: {type(e).__name__}: {e}")
    from nonebot.message import handle_event

    bot = ReplayBot(ReplayAdapter(driver), self_id)
    off = _diag_path().stat().st_size if _diag_path().exists() else 0

    results = []
    for i, rec in enumerate(picked, 1):
        ev = _build_event(rec, self_id, 900000 + i)
        b = _bucket_of(rec["_text"], self_id)
        mark = len(REC.sends)
        t0 = time.time()
        err = ""
        try:
            await asyncio.wait_for(handle_event(bot, ev), timeout=args.timeout)
        except asyncio.TimeoutError:
            err = "timeout"
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
        # 回复是后台任务（分段缓冲 + LLM），handle_event 返回 ≠ 回复完成；
        # 等到真的发出（或超时）再收诊断，避免把「正在生成」记成沉默。
        # 没有 prompt 事件 = 本轮没进 LLM → 静默 8s 就判沉默，别干等满。
        waited = 0.0
        quiet = 0.0
        last_size = off
        saw_llm = False
        while not REC.sends[mark:] and waited < args.wait_reply:
            await asyncio.sleep(0.5)
            waited += 0.5
            p = _diag_path()
            size = p.stat().st_size if p.exists() else last_size
            if size != last_size:
                last_size = size
                quiet = 0.0
                if b"\"kind\": \"prompt\"" in p.read_bytes()[-65536:]:
                    saw_llm = True
            else:
                quiet += 0.5
            if not saw_llm and quiet >= 8.0:
                break
        cost = time.time() - t0
        await asyncio.sleep(args.settle)
        dnew, off = _read_new(off)
        sends = REC.sends[mark:]
        sm = _summ(dnew)
        item = {
            "bucket": b,
            "text": rec["_text"][:60],
            "err": err,
            "ms": round(cost * 1000),
            "replied": bool(sends),
            "send_text": _sent_text(sends[0])[:120] if sends else "",
            "send_n": len(sends),
            "view": [(r.get("gate"), bool(r.get("ok")), r.get("why")) for r in sm["view"]],
            "recall": [(r.get("n_pick"), r.get("ms")) for r in sm["recall"]],
            "tools": [(r.get("n"), r.get("total"),
                       r.get("k"), (r.get("names") or [])[:4]) for r in sm["tools"]],
            "attr": [r.get("why") for r in sm["attr"]],
            "write": len(sm["write"]),
            "qwen": [(bool(r.get("ok")), r.get("ms")) for r in sm["qwen"]],
            "prompt": [(r.get("total"), dict(list((r.get("seg") or {}).items())[:3]))
                       for r in sm["prompt"]],
        }
        results.append(item)
        flag = "回" if sends else "默"
        print(f"[{i:02d}][{b:6s}] {cost:5.1f}s {flag} {rec['_text'][:28]!r}"
              f" view={item['view'][:2]} recall={item['recall']}"
              f" tools={item['tools'][:1]} qwen={item['qwen']}"
              f" prompt={[p[0] for p in item['prompt']]}")
        if sends:
            print(f"      → {item['send_text']}")
        if err:
            print(f"      !! {err}")
        await asyncio.sleep(args.gap)

    report = sandbox / "data/replay_report.json"
    report.write_text(json.dumps(results, ensure_ascii=False, indent=1),
                      encoding="utf-8")
    print(f"\n报告: {report}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sandbox", default="/tmp/replay_app")
    ap.add_argument("--gid", default="100000016")
    ap.add_argument("--self-id", default="100000010")
    ap.add_argument("--days", type=int, default=2)
    ap.add_argument("--limit", type=int, default=12)
    ap.add_argument("--per-bucket", type=int, default=3)
    ap.add_argument("--buckets", default="addr,cmd,q,vent,short,normal")
    ap.add_argument("--gap", type=float, default=2.0)
    ap.add_argument("--settle", type=float, default=3.0)
    ap.add_argument("--wait-reply", type=float, default=40.0)
    ap.add_argument("--timeout", type=float, default=150.0)
    a = ap.parse_args()
    return asyncio.run(_main(a))


if __name__ == "__main__":
    sys.exit(main())
