#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
eval_replay.py —— 用真实 trace 做「离线回放评测」，把改 prompt 从手感变成数字。

它解决什么问题
--------------
现在改一句 prompt / 一个阈值，只能上线撞 bug，撞到了再回滚。
`data/llm_trace/*.jsonl` 里已经存了每一次**真正发给模型**的完整 msgs，
把它冻结成评测集，就能在本地做 A/B：

    同一批输入 × 旧 prompt / 新 prompt → 比相关性、长度、标记泄漏、延迟、成本

这不是「线上实时评价」，也不需要在群里重新聊一遍：
输入是死的（trace 里的 msgs），只有输出是新的。

三个子命令
----------
    # 1) 从最近 7 天的 trace 里抽 60 条，冻结成评测集
    python scripts/eval_replay.py build --days 7 --limit 60

    # 2) 用当前 prompt 回放（可加 --system-append 做 A/B）
    python scripts/eval_replay.py run --set data/eval/golden.jsonl --concurrency 4

    # 3) 打分 + 出报告
    python scripts/eval_replay.py judge --set data/eval/golden.jsonl

一次性跑完：
    python scripts/eval_replay.py all --limit 40

注意
----
· trace 里带工具调用（role=tool）的记录**不能**直接回放：OpenAI 协议要求
  tool 消息带 tool_call_id，而 trace 只存了 role/content。默认跳过并在报告里计数。
· 回放会真的花钱（输入基本都能吃前缀缓存，成本很低，但不是零）。
  先用 --limit 20 试水。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TRACE_DIR = Path(os.getenv("LLM_TRACE_DIR", "data/llm_trace"))
EVAL_DIR = Path(os.getenv("EVAL_DIR", "data/eval"))


# ============================================================
# 配置：优先读环境变量，其次读项目 .env（脚本可能脱离容器跑）
# ============================================================
def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        # 去掉行内注释：docker 的 env_file 不会替你剥，`MAX_BUBBLES=2  # 注释`
        # 会原样变成 "2  # 注释"，int() 直接炸（2026-11-17 本地跑评测踩到）。
        if v[:1] not in ("'", '"') and " #" in v:
            v = v.split(" #", 1)[0].rstrip()
        v = v.strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


_load_env_file(ROOT / ".env")

API_KEY = os.getenv("XAI_API_KEY", "")
BASE_URL = (os.getenv("XAI_BASE_URL", "https://api.x.ai/v1") or "").rstrip("/")
MODEL = os.getenv("XAI_MODEL", "deepseek-chat")
PROXY = os.getenv("XAI_PROXY", "")


# ============================================================
# build：从 trace 抽评测集
# ============================================================
def _iter_trace(days: int):
    cutoff = time.time() - days * 86400
    files = sorted(TRACE_DIR.glob("*.jsonl"))
    for f in files:
        try:
            if f.stat().st_mtime < cutoff:
                continue
        except Exception:
            continue
        for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if float(rec.get("ts") or 0) >= cutoff:
                yield rec


# 这些是「指令/玩法」，不是聊天。回放时没有工具，必然判成答非所问，
# 放进评测集只会污染相关性分数（2026-11-17 诊断发现「抽」「今日小猪」拉低平均）。
_COMMAND_HINTS = (
    "今日小猪", "昨日小猪", "随机小猪", "我的猪圈", "小猪图鉴", "本周小猪",
    "今日运势", "今日人品", "运势", "气运", "人品",
    "来点美少女", "来点二次元", "翻唱", "清唱", "画图",
)
_COMMAND_EXACT = {"抽", "抽一只", "抽猪", "来一张", "来张图"}

# 「动作类」请求：回放时没有挂工具，模型只能嘴上答应 → 必然判成答非所问。
# 这类样本不适合用来衡量「聊天相关性」，直接从评测集剔除。
_ACTION_HINTS = (
    "点赞", "点个赞", "赞一下", "赞我", "戳一戳", "戳一下", "戳我",
    "摸我", "rua", "发表情", "发张表情", "拍一拍",
    "画一张", "画个", "画只", "搜图", "翻唱", "清唱", "唱一首", "来一首",
)


def _looks_like_command_text(t: str) -> bool:
    t = str(t or "").strip()
    if not t:
        return False
    if t[:1] in "/／.!！":
        return True
    if t in _COMMAND_EXACT:
        return True
    # 含玩法/动作关键词就排除：回放没有工具，这类必然被判「答非所问」
    return any(c in t for c in _COMMAND_HINTS + _ACTION_HINTS)


# 「补资料」那一轮的 user 消息不是群友说的话，是系统塞回去的结果。
# 不跳过的话，评测会拿「你记得的（本地近期记忆）：…」当群友提问（2026-11-17 抓到）。
_FULFILL_PREFIX = (
    "搜索到的内容", "本地agent知识库图书馆内容", "你记得的", "更早的群聊记录",
    "QQ 操作结果", "看视频结果", "群里的人员信息", "关于「", "GLM 给的参考",
    "工具说明", "表情收藏结果", "你等了", "运势结果", "你记得",
)


def _last_user_content(msgs: list) -> str:
    for m in reversed(msgs):
        if str(m.get("role")) != "user":
            continue
        c = str(m.get("content") or "")
        s = c.lstrip()
        if s.startswith(_FULFILL_PREFIX):
            continue
        if "基于上面的资料，用你自己的语气直接回答" in c:
            continue
        return c
    return ""


def _current_message(msgs: list) -> str:
    """
    从 trace 的 msgs 里还原「群友这一句」——**不是整段 prompt**。

    ⚠️ 之前直接拿 trace 的 `text` 字段当群友消息，而那个字段存的是
    build_prompt 的完整产物（背景+档案+记忆+上下文）。judge 等于在问
    「她有没有回答这一大段 prompt」，分数自然失真（2026-11-17 发现）。
    """
    cur = _last_user_content(msgs)
    if "要不要插话" in cur or "你自己判断要不要插一句" in cur:
        return "（主动插话）"
    # 去掉 GLM 事前参考块
    for _mark in ("【另一个模型对同一段上下文", "【要求】事实、专有名词"):
        if _mark in cur:
            cur = cur.split(_mark, 1)[0]
    if "以下是当前要回复的消息" in cur:
        cur = cur.split("以下是当前要回复的消息", 1)[-1]
        # 去掉分隔线剩下的「（重点看这条）-----------」这一行
        if "\n" in cur:
            cur = cur.split("\n", 1)[1]
    cur = cur.strip(" -\n")
    # 老格式：整段 prompt 都在 user 里，最后一行才是当前消息
    if "以下是背景" in cur or "【现在时间】" in cur:
        lines = [x.strip() for x in cur.splitlines()
                 if x.strip() and set(x.strip()) - {"-"}]
        cur = lines[-1] if lines else cur
    return cur or "（空）"


def _replayable(rec: dict, include_tools: bool = False) -> tuple[bool, str]:
    msgs = rec.get("msgs") or []
    if not msgs:
        return False, "no_msgs"
    if len(msgs) < 2:
        return False, "too_short"

    # 内部影子回答，从来没发给用户，不能当评测样本
    if str(rec.get("key") or "").startswith("shadow_"):
        return False, "shadow"

    # 图片消息：trace 里没有图，纯文本回放必然瞎猜（诊断里 6 条都是这个）
    blob = "\n".join(str(m.get("content") or "") for m in msgs)
    if "[图片识别结果]" in blob or "（群友发了张图片）" in blob:
        return False, "vision"

    for m in msgs:
        if str(m.get("role")) == "tool" or m.get("tool_calls"):
            if not include_tools:
                return False, "has_tool_role"

    raw = str(rec.get("reply") or "").strip()
    if not raw:
        return False, "empty_reply"

    # 指令/玩法：没有工具回放不了
    if _looks_like_command_text(_current_message(msgs)):
        return False, "command"

    # 去掉协议标记后什么都没有（"[[LUCK]]" / "]" 这种），不是有效回复
    stripped = re.sub(r"\[\[[^\]]*\]\]", "", raw)
    stripped = re.sub(r"<sticker:[^>]*>|<reply:\s*\d+\s*>", "", stripped)
    if len(stripped.strip(" \n\t|")) < 2:
        return False, "empty_after_parse"

    return True, ""


def _sanitize_msgs(msgs: list) -> list:
    """
    把带工具调用的历史压成「纯对话」，让 OpenAI 协议能接受。

    为什么需要（2026-11-17 线上）：trace 里只存了 role/content，
    **没有 tool_call_id**，所以带 tool 消息的会话直接回放会被 API 拒绝。
    线上 127 条里 69 条是这种 —— 全丢掉的话评测集只剩群聊，样本有偏。

    近似做法（只用于评测，不影响线上）：
      · assistant 带 tool_calls 且没有正文 → 丢掉
      · tool 消息 → 改成 user，前缀「[工具返回]」
    这样能保留上下文和最终回答，但**模型看不到真实的工具结果结构**，
    分数只适合做「版本间相对比较」，不能当绝对值。
    """
    out = []
    for m in msgs:
        role = str(m.get("role") or "")
        content = str(m.get("content") or "")
        if role == "assistant" and m.get("tool_calls"):
            if content.strip():
                out.append({"role": "assistant", "content": content})
            continue
        if role == "tool":
            out.append({"role": "user", "content": f"[工具返回] {content}"})
            continue
        out.append({"role": role, "content": content})
    return out


def cmd_build(args) -> int:
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.out) if args.out else EVAL_DIR / "golden.jsonl"

    picked, skipped = [], {}
    seen_keys = {}
    for rec in _iter_trace(args.days):
        ok, why = _replayable(rec, getattr(args, "include_tools", False))
        if not ok:
            skipped[why] = skipped.get(why, 0) + 1
            continue
        # 每个会话最多取 N 条，避免一个话痨群占满数据集
        key = str(rec.get("key") or "")
        if seen_keys.get(key, 0) >= args.per_session:
            continue
        seen_keys[key] = seen_keys.get(key, 0) + 1
        picked.append({
            "id": f"{int(float(rec.get('ts') or 0))}-{len(picked)}",
            "key": key,
            "time": rec.get("time", ""),
            # ⚠️ 存「还原后的群友消息」，不是 trace 里那段完整 prompt
            "text": _current_message(rec["msgs"]),
            "baseline_reply": rec.get("reply", ""),
            "msgs": rec["msgs"],
        })
        if len(picked) >= args.limit:
            break

    with out_path.open("w", encoding="utf-8") as f:
        for item in picked:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    print(f"✅ 评测集已生成：{out_path}")
    print(f"   样本 {len(picked)} 条，覆盖 {len(seen_keys)} 个会话")
    if skipped:
        print("   跳过：" + "，".join(f"{k}={v}" for k, v in sorted(skipped.items())))
    if picked:
        lens = [len(str(x["baseline_reply"])) for x in picked]
        print(f"   基线回复长度：中位 {int(statistics.median(lens))} 字"
              f"，范围 {min(lens)}~{max(lens)}")
    return 0


# ============================================================
# run：回放
# ============================================================
def _post(payload: dict, timeout: float = 120.0):
    import httpx
    kwargs = {"timeout": timeout, "trust_env": False}
    if PROXY:
        kwargs["proxy"] = PROXY
    with httpx.Client(**kwargs) as c:
        return c.post(f"{BASE_URL}/chat/completions",
                      headers={"Authorization": f"Bearer {API_KEY}",
                               "Content-Type": "application/json"},
                      json=payload)


def _load_set(path: str) -> list:
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"找不到评测集：{p}（先跑 build）")
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def _apply_variant(msgs: list, args) -> list:
    """只改 system 块，user / 历史一律不动 —— 变量唯一，才能归因。"""
    msgs = [dict(m) for m in msgs]
    if args.system_override:
        text = Path(args.system_override).read_text(encoding="utf-8")
        for i, m in enumerate(msgs):
            if m.get("role") == "system":
                msgs[i]["content"] = text
                return msgs
    if args.system_append:
        text = Path(args.system_append).read_text(encoding="utf-8")
        for i, m in enumerate(msgs):
            if m.get("role") == "system":
                msgs[i]["content"] = str(msgs[i].get("content") or "") + "\n\n" + text
                return msgs
    return msgs


async def _replay_one(item: dict, args, sem: asyncio.Semaphore) -> dict:
    async with sem:
        msgs = _sanitize_msgs(item["msgs"])
        msgs = _apply_variant(msgs, args)
        payload = {
            "model": MODEL,
            "messages": msgs,
            "temperature": 0.0 if args.deterministic else 1.0,
            "max_tokens": args.max_tokens,
        }
        t0 = time.time()
        try:
            # 用 run_in_executor 而不是 asyncio.to_thread：
            # 前者 3.8 就有，方便在旧环境/本地跑（服务器是 3.11，两者都行）。
            loop = asyncio.get_running_loop()
            r = await loop.run_in_executor(None, lambda: _post(payload))
            cost = time.time() - t0
            if r.status_code != 200:
                return {**item, "error": f"HTTP {r.status_code}: {r.text[:200]}",
                        "latency": cost}
            j = r.json()
            reply = (j["choices"][0]["message"].get("content") or "").strip()
            u = j.get("usage") or {}
            return {
                "id": item["id"], "key": item["key"], "text": item.get("text", ""),
                # 位置评测要保留 variant / gold 信息，否则 judge 后无法分组
                "variant": item.get("variant", ""),
                "gold_ref": item.get("gold_ref", ""),
                "gold_answer": item.get("gold_answer", ""),
                "expected_keywords": item.get("expected_keywords") or [],
                # 带上 msgs：judge / diag 要靠它判断「是否主动插话 / 有没有记忆块」
                "msgs": item.get("msgs") or [],
                "baseline_reply": item.get("baseline_reply", ""),
                "reply": reply, "latency": cost,
                "prompt_tokens": int(u.get("prompt_tokens") or 0),
                "completion_tokens": int(u.get("completion_tokens") or 0),
                "cache_hit": int(u.get("prompt_cache_hit_tokens") or 0),
                "error": "",
            }
        except Exception as e:
            return {**item, "error": f"{type(e).__name__}: {e}",
                    "latency": time.time() - t0}


async def cmd_run(args) -> int:
    if not API_KEY:
        raise SystemExit("没有 XAI_API_KEY（检查 .env 或环境变量）")
    items = _load_set(args.set)
    if args.limit:
        items = items[: args.limit]
    sem = asyncio.Semaphore(max(1, args.concurrency))
    print(f"▶ 回放 {len(items)} 条（并发 {args.concurrency}，模型 {MODEL}）…")

    async def _all():
        return await asyncio.gather(*[_replay_one(it, args, sem) for it in items])

    # ⚠️ 这里不能再 asyncio.run：cmd_run 本身已经在事件循环里（main 负责启动）。
    #    之前写成 asyncio.run 会报 "cannot be called from a running event loop"。
    results = await _all()
    out = Path(args.out) if args.out else EVAL_DIR / "replay.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    ok = [r for r in results if not r.get("error")]
    err = [r for r in results if r.get("error")]
    print(f"✅ 完成：成功 {len(ok)}，失败 {len(err)} → {out}")
    for e in err[:3]:
        print(f"   ! {e['id']}: {e['error'][:120]}")
    return 0 if ok else 1


# ============================================================
# judge：打分
# ============================================================
_JUDGE_SYS = (
    "你是 QQ 群聊质检员。给你一条群友消息和机器人（一个有点毒舌、爱吐槽的角色）的回复。\n"
    "只输出 JSON，不要解释，格式：\n"
    '{"relevant":1-5,"natural":1-5,"on_topic":1-5,"leak":true/false,"reason":"20字内"}\n'
    "评分标准：\n"
    "· relevant：回复是否接住了对方真正在说的事（答非所问=1，精准接住=5）\n"
    "· natural：像不像真人在群里随口说话（AI 腔/客服腔=1，自然=5）\n"
    "· on_topic：有没有跑题、硬接看不懂的梗\n"
    "· leak：回复里是否残留 [[SEARCH|..]] / <sticker:..> / [[MEMO|..]] 这类内部标记"
)


def _judge_one(rec: dict, timeout: float = 60.0) -> dict:
    user = (
        f"群友消息：{str(rec.get('text') or '')[:500]}\n\n"
        f"机器人回复：{str(rec.get('visible') or rec.get('reply') or '（没有回复）')[:800]}"
    )
    payload = {
        "model": MODEL,
        "messages": [{"role": "system", "content": _JUDGE_SYS},
                     {"role": "user", "content": user}],
        "temperature": 0.0,
        "max_tokens": 200,
    }
    r = _post(payload, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    txt = (r.json()["choices"][0]["message"].get("content") or "").strip()
    m = re.search(r"\{.*\}", txt, re.S)
    if not m:
        raise RuntimeError(f"judge 返回不是 JSON: {txt[:80]}")
    return json.loads(m.group(0))


def _visible_reply(raw: str) -> str:
    """
    把模型原始输出跑一遍 parse_reply，得到「用户真正会看到」的正文。

    ⚠️ 为什么必须有这一步（2026-11-17 线上基线踩到）：
      直接拿 raw 查「标记泄漏」会得到 27% —— 因为 <sticker:开心>、
      [[MEMO|...]]、[[AT|..]] 本来就是协议的一部分，parse_reply 会抠掉。
      真正的泄漏 = **跑完 parse_reply 之后**正文里还剩 [[ / <sticker: / <reply:。
    """
    raw = str(raw or "")
    try:
        from services.aura_logic import parse_reply
        p = parse_reply(raw)
        text = " ".join(
            str(i.get("value") or "")
            for i in (p.get("items") or []) if i.get("kind") == "text"
        )
        # ||| 是分气泡符，用户看不到，别让 judge 去评它
        return text.replace("|||", " ").strip()
    except Exception:
        # 脱离 nonebot 环境跑（比如本地）时退化：至少把协议标记剥掉，
        # 否则「泄漏率」会把正常协议全算成泄漏。
        t = re.sub(r"\[\[[^\]]*\]\]", "", raw)
        t = re.sub(r"<sticker:[^>]*>|<reply:\s*\d+\s*>", "", t)
        t = re.sub(r"<voice>.*?</voice>", "", t, flags=re.S)
        return t.strip()


def _is_interjection_rec(rec: dict) -> bool:
    """主动插话样本（random_chat_loop 生成），天然比正常回复难。"""
    for m in rec.get("msgs") or []:
        c = str(m.get("content") or "")
        if "要不要插话" in c or "你自己判断要不要插一句" in c:
            return True
    return False


def _marker_leak(text: str) -> bool:
    # <at:名字> 不算泄漏：发送层会换成真 @ 段（见 chat 的 _split_at_tags）
    return bool(re.search(r"\[\[|<sticker:|<reply:|<voice>", str(text or "")))


def cmd_judge(args) -> int:
    if not API_KEY:
        raise SystemExit("没有 XAI_API_KEY")
    items = _load_set(args.set)
    items = [x for x in items if str(x.get("reply") or "").strip()]
    if args.limit:
        items = items[: args.limit]

    # 防呆：如果 text 还是整段 prompt，说明这是旧版 replay 结果，
    # judge 会把「回答整段背景」当成相关性，分数失真（2026-11-17 踩过）。
    bad = [x for x in items
           if len(str(x.get("text") or "")) > 600
           or "以下是背景" in str(x.get("text") or "")]
    if bad:
        print(f"⚠️ {len(bad)}/{len(items)} 条 text 仍是整段 prompt（旧版 replay 结果）")
        print("   → 请用新版脚本重新 build + run，再 judge；否则分数不可比")

    print(f"▶ 打分 {len(items)} 条…")

    scored = []
    for i, it in enumerate(items, 1):
        # 先还原「用户看到的正文」，再打分 / 查泄漏
        it["visible"] = _visible_reply(it.get("reply"))
        it["silent"] = not it["visible"]
        it["leak_local"] = _marker_leak(it["visible"])
        if it["silent"]:
            # 她选择不说话：不算答错，单独统计，不送 judge 浪费额度
            it["judge"] = {"silent": True}
        else:
            # --repeat N：judge 有 ±0.05 抖动，基线建议 3 次取平均
            repeats = max(1, int(getattr(args, "repeat", 1)))
            js = []
            for _ in range(repeats):
                try:
                    js.append(_judge_one(it))
                except Exception as e:
                    if not js:
                        it["judge"] = {"error": f"{type(e).__name__}: {e}"}
                    break
            if js:
                merged = dict(js[0])
                for k in ("relevant", "natural", "on_topic"):
                    vals = [float(x[k]) for x in js if isinstance(x.get(k), (int, float))]
                    if vals:
                        merged[k] = round(sum(vals) / len(vals), 2)
                merged["leak"] = any(bool(x.get("leak")) for x in js)
                it["judge"] = merged
        scored.append(it)
        if i % 10 == 0:
            print(f"   {i}/{len(items)}")

    out = Path(args.out) if args.out else EVAL_DIR / "judged.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for r in scored:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    _print_report(scored, out)
    return 0


def _print_report(rows: list, out: Path) -> None:
    def _nums(key):
        return [float(r["judge"][key]) for r in rows
                if isinstance(r.get("judge"), dict) and isinstance(r["judge"].get(key), (int, float))]

    def _avg(xs):
        return sum(xs) / len(xs) if xs else 0.0

    rel, nat, top = _nums("relevant"), _nums("natural"), _nums("on_topic")
    jerr = [r for r in rows
            if isinstance(r.get("judge"), dict) and r["judge"].get("error")]
    silent = [r for r in rows if r.get("silent")]
    leaks = sum(1 for r in rows if r.get("leak_local"))
    lens = [len(str(r.get("visible") or r.get("reply") or "")) for r in rows if not r.get("silent")]
    lat = [float(r.get("latency") or 0) for r in rows if r.get("latency")]

    print("\n================ 评测报告 ================")
    print(f"样本            {len(rows)}")
    print(f"相关性 (1-5)    {_avg(rel):.2f}")
    print(f"自然度 (1-5)    {_avg(nat):.2f}")
    print(f"贴题   (1-5)    {_avg(top):.2f}")
    print(f"标记泄漏        {leaks} 条（{leaks / max(1, len(rows)) * 100:.1f}%）")
    if silent:
        print(f"选择不回        {len(silent)} 条（不计入平均分）")
    interj = [r for r in rows if _is_interjection_rec(r)]
    interj_scored = [r for r in interj if not r.get("silent")]
    normal = [r for r in rows if not r.get("silent") and not _is_interjection_rec(r)]

    def _avg_of(rs, k):
        xs = [float(r["judge"][k]) for r in rs
              if isinstance(r.get("judge"), dict)
              and isinstance(r["judge"].get(k), (int, float))]
        return sum(xs) / len(xs) if xs else 0.0

    if interj_scored and normal:
        print(f"排除主动插话后  {len(normal)} 条："
              f"相关性 {_avg_of(normal, 'relevant'):.2f} ｜ "
              f"贴题 {_avg_of(normal, 'on_topic'):.2f}")
    elif interj:
        print(f"主动插话        {len(interj)} 条，全部选择不回（不计分）")
    if jerr:
        print(f"打分失败        {len(jerr)} 条（示例：{jerr[0]['judge']['error'][:70]}）")
    if not rel:
        print("⚠️ 没有有效评分：检查模型是否返回 JSON，或换一个 judge 模型")
    if lens:
        print(f"回复长度        中位 {int(statistics.median(lens))} 字，"
              f"最长 {max(lens)} 字")
    if lat:
        lat.sort()
        p95 = lat[min(len(lat) - 1, int(len(lat) * 0.95))]
        print(f"延迟            中位 {statistics.median(lat):.2f}s，P95 {p95:.2f}s")
    print(f"明细            {out}")
    print("==========================================")
    print("提示：和上一版报告对比才有意义（分数不是绝对值，是回归信号）。")


def cmd_all(args) -> int:
    args.out = str(EVAL_DIR / "golden.jsonl")
    cmd_build(args)
    args.set = args.out
    args.out = str(EVAL_DIR / "replay.jsonl")
    # cmd_run 是 async：这里必须真跑一次事件循环
    asyncio.run(cmd_run(args))
    args.set = args.out
    args.out = str(EVAL_DIR / "judged.jsonl")
    return cmd_judge(args)


def main() -> int:
    ap = argparse.ArgumentParser(description="trace 回放评测")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="从 trace 生成评测集")
    b.add_argument("--days", type=int, default=7)
    b.add_argument("--limit", type=int, default=60)
    b.add_argument("--per-session", type=int, default=6)
    b.add_argument("--include-tools", action="store_true",
                   help="把带工具调用的会话也纳入（近似压缩，样本更多但有偏）")
    b.add_argument("--out", default="")
    b.set_defaults(func=cmd_build)

    r = sub.add_parser("run", help="回放评测集")
    r.add_argument("--set", default=str(EVAL_DIR / "golden.jsonl"))
    r.add_argument("--limit", type=int, default=0)
    r.add_argument("--concurrency", type=int, default=4)
    r.add_argument("--max-tokens", type=int, default=800)
    r.add_argument("--deterministic", action="store_true",
                   help="temperature=0（可复现，但和线上分布不同）")
    r.add_argument("--system-append", default="", help="在 system 末尾追加一段（A/B 用）")
    r.add_argument("--system-override", default="", help="整段替换 system（A/B 用）")
    r.add_argument("--out", default="")
    r.set_defaults(func=cmd_run)

    j = sub.add_parser("judge", help="给回放结果打分")
    j.add_argument("--set", default=str(EVAL_DIR / "replay.jsonl"))
    j.add_argument("--limit", type=int, default=0)
    j.add_argument("--repeat", type=int, default=1,
                   help="每条打几次分取平均（基线建议 3，成本 ×3）")
    j.add_argument("--out", default="")
    j.set_defaults(func=cmd_judge)

    a = sub.add_parser("all", help="build → run → judge 一条龙")
    a.add_argument("--days", type=int, default=7)
    a.add_argument("--limit", type=int, default=40)
    a.add_argument("--per-session", type=int, default=6)
    a.add_argument("--include-tools", action="store_true")
    a.add_argument("--concurrency", type=int, default=4)
    a.add_argument("--max-tokens", type=int, default=800)
    a.add_argument("--deterministic", action="store_true")
    a.add_argument("--system-append", default="")
    a.add_argument("--system-override", default="")
    a.set_defaults(func=cmd_all)

    args = ap.parse_args()
    # ⚠️ cmd_run 是 async def：以前这里直接 return 协程对象，
    #    于是终端只看到「coroutine was never awaited」，什么都没跑（2026-11-17 线上踩到）。
    res = args.func(args)
    if asyncio.iscoroutine(res):
        return asyncio.run(res)
    return res


if __name__ == "__main__":
    raise SystemExit(main())

