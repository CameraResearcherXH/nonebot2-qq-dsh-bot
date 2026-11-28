# services/mood_judge.py
"""
心情判读（第三方 LLM）。

用户 2026-11-19：
  「ai 自己写心情值很容易崩，它会乱写。不如让她写自己是什么感觉、什么事的心里话，
   然后放一个新对话模型根据她的心里话 + 话来判断心情名词和阈值 1.0 / 0 / -1.0，
   两位小数，让三方 LLM 来判断。先搞定这套，准了再接进心情系统。」

所以分工是：
  · **她**只写「感觉 + 心里话」（自由文本：`[[MOOD|…]]` / `[[MEMO|think:…]]`）；
  · **判读模型**给名词（word）和强度（value，-1.00~1.00）；
  · 判读依据：她的心里话 + **最近 20 条群聊**（和审核窗口同一个口径）+ 上一次的心情；
  · 这一版**影子运行**：只记不用（不改变她现在的行为），
    `/测函数 心情` 能对照「她的原话 / 判读结果 / 老数值」，验证准了再接全链路。

为什么要三方模型：她自己既写感受又写数字，等于自己给自己打分，
「被骂了」也可能写成 +0.8（她不想显得脆弱）。分开就骗不了自己。
"""
from __future__ import annotations
from core import tuning

import json
import os
import re
import time
from pathlib import Path

try:
    from nonebot import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)

ENABLED = os.getenv("MOOD_JUDGE_ENABLED", "true").lower() in ("1", "true", "yes", "on")
# 2026-11-24：判读用哪个模型由 providers 决定（JUDGE_PROVIDER / JUDGE_MODEL）。
from .providers import (resolve as _resolve_provider,
                        call_tokens as _call_tokens)  # noqa: E402

_JUDGE_P = _resolve_provider("judge")
MODEL = _JUDGE_P["model"]
TIMEOUT = tuning.f("MOOD_JUDGE_TIMEOUT")
CTX_LINES = tuning.i("MOOD_JUDGE_CTX")
# 窗口的时间上限：慢群攒不满 8 条也要判（emotion §5.1）
WINDOW_MAX_SEC = tuning.f("MOOD_WINDOW_MAX_SEC")
MIN_INTERVAL = tuning.f("MOOD_JUDGE_MIN_INTERVAL")
# 每会话每小时判读上限（含补判窗口）：防止「40 条/段 × 5 窗」把成本打穿
JUDGE_MAX_PER_HOUR = tuning.i("MOOD_JUDGE_MAX_PER_HOUR")
# judge 的 arousal/dominance 是三档枚举，映射成 0~1（只记录，不进 v）
_AROUSAL_LEVELS = {
    "low": tuning.f("AROUSAL_LEVEL_LOW"),
    "mid": tuning.f("AROUSAL_LEVEL_MID"),
    "high": tuning.f("AROUSAL_LEVEL_HIGH"),
}
# 最近一次「段判读」覆盖到的 msg_id（P4：并集必须 == 段内全集）
_COVERAGE: dict = {}
# 每会话最近 1 小时的判读调用时间戳（判读预算）
_CALLS: dict = {}


def _fget(d: dict, key: str, default: float) -> float:
    """float(d[key])，保留合法 0；仅 None/缺失/异常时回退默认。

    `d.get(key) or default` 会把模型给的 0（完全无关 / 零置信 / 零反应）
    当假值吞掉、回填默认 → 系统性高估相关度/置信（2026-11-28 审计）。
    """
    try:
        v = (d or {}).get(key)
        return default if v is None else float(v)
    except Exception:
        return default


def _calls_left(key: str, now: float) -> int:
    ts = [t for t in _CALLS.get(key, []) if now - float(t) < 3600]
    _CALLS[key] = ts
    return max(0, JUDGE_MAX_PER_HOUR - len(ts))
# ⚠️ 2026-11-27（外部审计）：AFFINITY_GAIN 以前只有这一句赋值、没有任何使用，
# 靠「有读点」混过 check_tuning。现在已从代码移除，键进 `tuning.UNWIRED`（LEGACY）。
# 她没主动写心情时，多久用群聊重新判一次（秒）。0 = 只在她说的时候判
REFRESH = tuning.f("MOOD_JUDGE_REFRESH")
# 判读结果在「记忆写回」里有效多久（秒）：超过就退回用 MEMO 自报的情绪
FRESH_SEC = tuning.f("MOOD_JUDGE_FRESH_SEC")
# 她没写 [[THINK]] 时，后台补一句心里话（保证每条回复都有记录）
BACKFILL = os.getenv("MOOD_THINK_BACKFILL", "true").lower() in ("1", "true", "yes", "on")

# ---- Δ 入口护栏（2026-11-22）----
# 用户：「我们不是一直看涨幅加减吗，怎么可能一边倒到 ±1？」
# 实测根因：40 条判读里 32 条是负的、均值 -0.073，而且全是
# 「懒得掺和 / 被刷图刷到眼晕 / 旁观」这类**跟她没关系**的事。
# 设计里刻痕按 |Δ| 阈值（0.10/0.25/0.50）分优先级 —— 前提是 Δ 本身要校准：
# 旁观就是 0，不是 -0.05。所以在入口做三件事：
#   1) |Δ| < NOISE → 0（噪音丢弃，不动数值/好感/刻痕/群心情）；
#   2) 她没说话（passive / 无 THINK）→ **直接不改状态**（ADR-038，2026-11-25：
#      旧的 PASSIVE_MAX 上限已删除，旁观的 Δ 连 0.03 都不该记）；
#   3) 不是冲她来（cause≠speaker 且没被叫）→ |Δ| ≤ ENV_MAX。
NOISE_DELTA = tuning.f("MOOD_NOISE_DELTA")
ENV_MAX_DELTA = tuning.f("MOOD_ENV_MAX_DELTA")

_LAST_AT: dict = {}       # session_key -> 上次判读时间（节流）
_LAST: dict = {}          # session_key -> 最近一次结果（给 /测函数 用）
# 每次「尝试」的结果，成功失败都记 —— 用户 2026-11-19：
# 「我没有从测心情的 log 里看到有效信息」。以前失败只打 debug，真机上等于没有。
_TRACE: dict = {}         # session_key -> {"at", "ok", "word"/"err", ...}
_SKIP_AT: dict = {}       # session_key -> (原因, 上次打日志时间)，同一个原因 60s 内只打一次

# ---- 攒着的心理活动（2026-11-26 用户实测发现的问题）----
# 用户：「我发现她其实每一句都有写心理活动的，所以并不是心理活动素材没有，
#       而是 cd 给隔离了。」
# 实测：`MIN_INTERVAL=60s` 节流时，这一轮带回来的 `think` 直接 return 丢掉；
# 下一窗如果她没再说新的话，`wrote` 就是 False → 判成「她没写 THINK」→ 整窗不判。
# 于是「每条都写了 THINK」在链路上表现成「大部分窗口没有 THINK」。
# 现在节流/预算用完时把 THINK 攒进队列，下一次真判读时合并进来一起判
# （不增加判读次数，只是不丢素材）。上限 8 条，超过丢最老的。
_PENDING_THINK: dict = {}   # session_key -> [[at, text], ...]
PENDING_THINK_MAX = 8


def _push_pending_think(key: str, text: str) -> int:
    t = str(text or "").strip()
    if not t:
        return len(_PENDING_THINK.get(key) or [])
    q = _PENDING_THINK.setdefault(key, [])
    q.append([time.time(), t[:200]])
    if len(q) > PENDING_THINK_MAX:
        del q[:len(q) - PENDING_THINK_MAX]
    return len(q)


def _pop_pending_think(key: str, *, max_age: float = 600.0) -> list:
    """取出攒着的 THINK（太老的丢掉：10 分钟前的心情不能算「这一窗」）。"""
    q = _PENDING_THINK.pop(key, None) or []
    now = time.time()
    return [str(t) for at, t in q if now - float(at or 0) <= max_age]


def pending_think_count(key: str) -> int:
    return len(_PENDING_THINK.get(str(key or "")) or [])

# 她的「心理活动」（MEMO 里的 think:）—— 用户 2026-11-19：
# 「我看这些内容没有心里话啊，只有回复。」
# 以前 think 只在**生成回复那一刻**传给判读，判完就丢：重判 / /测函数 心情 /
# 事后复盘全都看不到，等于「心里话」这个设计只活了一瞬间。这里落盘。
THINK_FILE = Path(os.getenv("MOOD_THINK_FILE", "data/mood_think.json"))
THINK_KEEP = tuning.i("MOOD_THINK_KEEP")
# 2026-11-25 P0-2：判读模型「补写」的心里话是**猜测**，不能写进 THINK_FILE
# （那是她的心理活动，是标注/状态卡的唯一来源）。猜测单独落盘，任何展示都不读它。
INFERRED_FILE = Path(os.getenv("MOOD_THINK_INFERRED_FILE", "data/mood_think_inferred.json"))
# 判读输入/输出留痕（2026-11-25）：要能回答「判读模型到底看到了什么、读成什么」。
# 只存 prompt + 原始回复，轮转保留最近 400 条；`MOOD_JUDGE_TRACE=0` 可关。
JUDGE_TRACE = str(os.getenv("MOOD_JUDGE_TRACE", "1")) != "0"
TRACE_FILE = Path(os.getenv("MOOD_JUDGE_TRACE_FILE", "data/mood_judge_trace.jsonl"))
TRACE_KEEP = 400
_THINK: dict = {}


def _trace(session_key: str, prompt: str, raw: str) -> None:
    try:
        row = {"at": time.time(), "session": str(session_key or ""),
               "prompt": str(prompt or "")[:1600], "raw": str(raw or "")[:600]}
        lines = []
        if TRACE_FILE.exists():
            lines = TRACE_FILE.read_text(encoding="utf-8").splitlines()[-TRACE_KEEP:]
        lines.append(json.dumps(row, ensure_ascii=False))
        TRACE_FILE.parent.mkdir(parents=True, exist_ok=True)
        TRACE_FILE.write_text("\n".join(lines[-TRACE_KEEP:]) + "\n", encoding="utf-8")
    except Exception:
        pass


# 命中统计：她每回一句就 +1，看 [[THINK]] 协议到底有没有被执行
# （用户 2026-11-20：「还是没有每句回复都带心理活动」——得能量化）
THINK_STAT = {"hit": 0, "miss": 0}


def note_think_stat(has_think: bool) -> None:
    THINK_STAT["hit" if has_think else "miss"] += 1


def think_stat_text() -> str:
    h, m = THINK_STAT["hit"], THINK_STAT["miss"]
    if not (h or m):
        return ("· 心里话命中：本次启动后还没统计到"
                "（重启会归零；上面的逐条记录是落盘的，不受影响）")
    return (f"· 心里话命中：{h}/{h + m} 条回复带了 THINK"
            f"（缺 {m} 条；本次启动后统计，重启归零）")


# anchor_level 从 2026-11-26 起是**必填**（用户拍板：它占心情的大头）。
# 但 prompt 写「必填」≠ 模型一定填 —— 换模型/降级时可能静默失效，
# 所以要能量化命中率，而不是等状态卡看起来「不纠偏」才发现。
ANCHOR_STAT = {"hit": 0, "miss": 0}
_ANCHOR_MISS_AT: dict = {}


def note_anchor_stat(has: bool) -> None:
    ANCHOR_STAT["hit" if has else "miss"] += 1


def anchor_stat_text() -> str:
    h, m = ANCHOR_STAT["hit"], ANCHOR_STAT["miss"]
    if not (h or m):
        return "· anchor_level 命中：本次启动后还没判读"
    return (f"· anchor_level 命中：{h}/{h + m}（必填，缺 {m} 条）"
            "（本次启动后统计，重启归零）")


def _think_load() -> dict:
    global _THINK
    if _THINK is not None and _THINK:
        return _THINK
    try:
        _THINK = json.loads(THINK_FILE.read_text(encoding="utf-8")) or {}
    except Exception:
        _THINK = {}
    return _THINK


def note_think(session_key: str, text: str, *, src: str = "",
               reply: str = "", auto: bool = False) -> None:
    """
    记一条她的心理活动。

    用户 2026-11-20：「它总是综合最后在一起算一件事，而不是每句话都反馈心理活动。」
    所以每条要带**上下文**：`src` = 她在接的那句话，`reply` = 她自己说的那句。
    这样 /测函数 心情 能逐条对照，而不是只有一句汇总。
    """
    key = str(session_key or "").strip()
    t = re.sub(r"\s+", " ", str(text or "")).strip()[:200]
    if not key or not t:
        return
    s = re.sub(r"\s+", " ", str(src or "")).strip()[:80]
    r = re.sub(r"\s+", " ", str(reply or "")).strip()[:80]
    d = _think_load()
    lst = d.setdefault(key, [])
    # 同一句 + 同一个上下文才算重复（以前只看 text，导致不同场景的同一句被吞掉）
    if lst:
        last = lst[-1]
        if str(last.get("text") or "") == t and str(last.get("src") or "") == s:
            return
    lst.append({"at": time.time(), "text": t, "src": s, "reply": r,
                "auto": bool(auto)})
    d[key] = lst[-max(1, THINK_KEEP):]
    try:
        THINK_FILE.parent.mkdir(parents=True, exist_ok=True)
        THINK_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    except Exception as e:
        logger.debug(f"[MOOD-JUDGE] 心理活动落盘失败: {type(e).__name__}: {e}")


def recent_think_items(session_key: str, n: int = 6) -> list:
    """最近 n 条心理活动（完整记录：at/text/src/reply），给 /测函数 心情 逐条展示。"""
    lst = _think_load().get(str(session_key or "")) or []
    out = []
    for x in lst[-max(1, int(n)):]:
        if not isinstance(x, dict):
            continue
        t = str(x.get("text") or "").strip()
        if not t:
            continue
        out.append({"at": float(x.get("at") or 0), "text": t,
                    "src": str(x.get("src") or ""),
                    "reply": str(x.get("reply") or ""),
                    "auto": bool(x.get("auto"))})
    return out


def recent_think(session_key: str, n: int = 3) -> list:
    """最近 n 条心理活动（只要文字，给判读 prompt 用）。"""
    return [x["text"] for x in recent_think_items(session_key, n)]


_SUM_CACHE: dict = {}


async def summarize(text: str, session_key: str = "", ctx_lines=None,
                    *, speaker: str = "") -> str:
    """把「要回的那句话」结合最近上下文压成一句事实（给记忆/图书馆当检索词）。

    2026-11-24 用户流水线：心情 LLM 对这段话的总结 → 用总结查记忆 → 查图书馆。
    2026-11-24 用户实测 bug：群友说「我小时候也玩过这个（抛硬币）」，另一句
    「我身高 192」被她理解成「玩到了 192 关」。根因是这里只喂了**当前这一句**，
    没有上下文，模型只能按字面猜；数字/单位一补就错。

    所以现在：
      · 带上最近 8~10 条（谁 → 谁），先判断这句在接哪句；
      · 数字/量词**按字面保留**，不自己补成关卡/等级/分数；
      · 仍然只做事实摘要，不判心情、不写库。
    """
    t = str(text or "").strip()
    if not t:
        return ""
    ctx = [re.sub(r"\s+", " ", str(x or "")).strip()[:140]
           for x in (ctx_lines or []) if str(x or "").strip()][-10:]
    key = (f"{session_key}|{speaker[:20]}|{t[:60]}|{len(t)}|"
           f"{hash(tuple(ctx[-6:]))}")
    hit = _SUM_CACHE.get(key)
    if hit and time.time() - float(hit[1]) < 600:
        return str(hit[0])

    base, key_api = _base()
    if not base or not key_api:
        return ""
    lines = [
        "你是群聊事实判读员，不聊天、不评价，只做标注。",
        "任务：先判断【要回的这一句】在接哪句、到底在说什么，再压成一句事实。",
        "规则：",
        "· 结合【最近聊天】里的指代/省略，不要只看字面；",
        "· 数字/量词按字面保留：写「他说自己 192」就够了，"
        "不要自己补成「192 关 / 192 级 / 192 分」；拿不准单位就写「（没说是什么）」；",
        "· 如果【要回的这一句】是 [图片]/[表情]/[视频] 或几乎没有文字，"
        "就总结「他在接上文的哪件事 / 群里在聊什么」，"
        "**不要写「内容未显示完整 / 缺少上下文 / 无法判断」这类元评论**；",
        "· 不要评价、不要回答它、不要用「我」；",
        "· 不超过 50 字，只输出这一句。",
        "",
    ]
    if ctx:
        lines.append("【最近聊天（最后一条最新）】")
        lines.extend(f"{i}. {x}" for i, x in enumerate(ctx, 1))
        lines.append("")
    lines.append(f"【要回的这一句】{speaker or '对方'}：{t[:400]}")
    prompt = "\n".join(lines)
    payload = {
        "model": MODEL or "deepseek-chat",
        "messages": [
            {"role": "system", "content": "你只输出一句中文事实摘要，不加引号。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        **_call_tokens("judge", 90),
    }
    try:
        import httpx
        async with httpx.AsyncClient(timeout=min(6.0, TIMEOUT)) as c:
            r = await c.post(f"{base}/chat/completions",
                             headers={"Authorization": f"Bearer {key_api}"},
                             json=payload)
            r.raise_for_status()
            data = r.json()
            try:
                from . import llm_usage as _lu
                _lu.record_from_response(data, purpose="judge",
                                         provider="judge", model=MODEL or "deepseek-chat")
            except Exception:
                pass
        txt = str(((((data or {}).get("choices") or [{}])[0]
                    .get("message") or {}).get("content")) or "")
        txt = re.sub(r"\s+", " ", txt).strip("「」『』“”\"'。，,")
        if txt:
            _SUM_CACHE[key] = (txt[:80], time.time())
            if len(_SUM_CACHE) > 500:
                for k in list(_SUM_CACHE)[:100]:
                    _SUM_CACHE.pop(k, None)
        return txt[:80]
    except Exception as e:
        logger.debug(f"[MOOD-JUDGE] 摘要失败: {type(e).__name__}: {e}")
        return ""


def _base() -> tuple:
    p = _resolve_provider("judge")
    return (p["base_url"].rstrip("/"), (p["api_key"] or "").strip())


_LINK_RE = re.compile(r"https?://\S+")


def _slim(text, n: int = 160) -> str:
    """
    压缩单条上下文：图片 OCR / 链接预览会把 160 字全占满，
    结果 20 条窗口里没几句真对话（用户 2026-11-19 的实测样本就是这样：
    「[图片] 不是 不确定。图里文字：https://m.douyin.com/item/7星掠者…」）。
    这里只压图片/带链接的消息，普通聊天原样保留。
    """
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    if "[图片]" in t or _LINK_RE.search(t):
        t = _LINK_RE.sub("…", t)
        if len(t) > 80:
            t = t[:80] + "…"
    return t[:n]


def scene_lines(session_key: str, limit: int = 0) -> list:
    """
    按**条数**拉最近 N 条聊天（不是按时间），给判读当上下文。

    用户 2026-11-19：「是要根据聊天看最新吗，还是说没有按消息条数拉。我建议拉 20 条来看。」
    → 群聊走 `group_scene.recent_messages`（有「谁说的」，还包含她自己那几句）；
      私聊/现场空 → 退回 `dialogue_log.load_recent`（同样按条数）。
    """
    key = str(session_key or "")
    n = max(1, int(limit or CTX_LINES))
    if not key:
        return []
    out = []
    if key.startswith("group_"):
        try:
            from . import group_scene
            # 统一走「处理过的历史」（收发件人标签链路），不再自己拼
            out = group_scene.history_lines(
                key[len("group_"):], n, width=150)
        except Exception:
            out = []
    if not out:
        try:
            from . import dialogue_log
            for r in dialogue_log.load_recent(key, limit=n):
                who = "你" if str(r.get("kind") or "") == "bot" else str(
                    r.get("who") or r.get("qq") or "?")
                txt = str(r.get("text") or "").strip()
                if txt:
                    out.append(f"{who}: {_slim(txt)}"[:160])
        except Exception:
            out = []
    return out[-n:]


def split_windows(records, size: int = 0, max_sec: float = 0) -> list:
    """把「段」切成判读窗口：**8 条 或 5min，先到先判**。

    段是投递单位（≤40 条 / ≤30s），窗口是判读单位；一个 40 条的段要判 5 窗，
    否则前 32 条的情绪冲击会被静默丢掉（emotion §5.1 / P4）。
    """
    size = int(size or CTX_LINES)
    max_sec = float(max_sec or WINDOW_MAX_SEC)
    out, cur, t0 = [], [], None
    for r in records or []:
        if not isinstance(r, dict):
            continue
        try:
            ts = float(r.get("ts") or 0.0)
        except Exception:
            ts = 0.0
        if not cur:
            t0 = ts
        cur.append(r)
        # ⚠️ 不能用 `t0 and ...`：ts 可能正好是 0.0（测试/相对时间戳），会被当成「没初始化」
        if len(cur) >= size or (t0 is not None and ts - t0 >= max_sec):
            out.append(cur)
            cur, t0 = [], None
    if cur:
        out.append(cur)
    return out


def _fmt_window(records, limit: int = 0) -> list:
    """窗口 → judge 的 ctx_lines（复用 segments.format，带「谁 → 谁」和 ⭐）。"""
    try:
        from . import segments
        txt = segments.format(records, limit=limit or CTX_LINES) or ""
    except Exception:
        return []
    return [ln for ln in txt.splitlines() if str(ln).strip()]


def last_coverage(session_key: str = "") -> list:
    """最近一次段判读覆盖的 msg_id（P4 用：并集 == 段内全集）。"""
    return list(_COVERAGE.get(str(session_key or "")) or [])


def _clip_ctx(ctx_lines) -> list:
    out = []
    for x in (ctx_lines or [])[-CTX_LINES:]:
        s = re.sub(r"\s+", " ", str(x or "")).strip()
        if not s:
            continue
        # 相邻重复（同一条被投递两次 / 复读）会让判读以为「被反复说了」，
        # 2026-11-25 真机日志里出现过连续两条一模一样。
        if out and s == out[-1]:
            continue
        out.append(s[:160])
    return out


def _build_prompt(words: str, think: str, ctx: list, prev_word: str = "",
                  prev_value: float = 0.0, hint: str = "",
                  repair: bool = False, archive: str = "",
                  src: str = "") -> str:
    lines = [
        "你是「心情判读员」，不聊天、不解释，只做标注。",
        "给你一个 AI 角色（她）**自己写下的心里话**，以及她刚经历的最近群聊。",
        "你要判断两件事：她此刻的心情名词，和强度。",
        "",
        f"【她自己的话】{words or '（这次没写心情标签）'}",
    ]
    if hint:
        lines.append(f"【刚刚发生】{str(hint)[:160]}")
    if think:
        lines.append(f"【她的心理活动】{think[:200]}")
    # 2026-11-25：把她**正在回的那几条**单独拎出来。以前只给 8 条上下文，
    # 让她自己猜「哪一句触发了这段心理活动」——猜错就会把情绪记到错的人/错的事上。
    if src:
        lines.append(f"【她这条在回】{str(src)[:200]}")
        lines.append("（这一行可能也出现在下面的群聊里，重复是正常的；"
                     "判「这次是怎么回事」以这一行为准）")
    if archive:
        lines.append(f"【她对正在说话这个人的档案】{archive[:220]}")
    if prev_word or prev_value:
        # 2026-11-26（外部审计）：**不给当前数值**。
        # anchor_level 现在是每窗的主项（ADR-042），prompt 里再写「当前心情 +0.35」，
        # 最省力的答案就是 anchor_level≈+1 —— 那是 ADR-029 明确警告的「回声」。
        # 只给上一轮那个词（她自己的说法），不给数字，模型就没得照抄。
        lines.append(f"【上一轮】{prev_word or '?'}"
                     "—— 只作参考，情绪可以变，别硬往回拉")
    if ctx:
        lines.append("")
        lines.append(f"【最近 {len(ctx)} 条群聊（最后一条最新）】")
        lines.append("格式：`谁 → 收件人：内容`。**`→ 你` 才是对她说的话**；")
        lines.append("`→ 某人 / → 大家` 是别人之间的对话 —— 别人被冒犯 ≠ 她被冒犯，")
        lines.append("别把旁人的情绪算到她头上（她可能只是看戏/懒得插嘴）。")
        lines.extend(f"{i}. {x}" for i, x in enumerate(ctx, 1))
    lines += [
        "",
        "判断规则（mind / word / desirability / cared / anchor_level / cause / why 都要有，"
        "档案够详细时再加 bond_word）：",
        "· mind：**第一人称**的一句心里话，20 字内，像她在心里嘀咕。",
        "  不是事件复述，也不是说给别人听的客套话。例：「他夸我，我嘴上不认」「这数字看得我眼晕」。",
        "  哪怕她很平静，也要写一句（例：「就随便看看」）。**这一项不能省**。",
        "· word：一个**具体**的中文词/短语，4~10 字。不要用「开心/难过/生气/平静」这种泛词；",
        "  要具体到她此刻的状态：「被夸得有点飘」「憋着一口气」「懒得解释」「心里空了一下」；",
        "  真的没什么起伏就写「没什么起伏」。",
        "· **desirability + cared（两段式，优先填这两个，不要直接猜 impact）**：",
        "  ⚠️ **判分依据只有她的【心理活动】**：同一件事，她心里写「受用」就是 +，",
        "     写「烦」就是 −。**不要按「这件事通常算好事/坏事」直接给分**——",
        "     她理解错了也按她的理解评（那是她的感受）。",
        "  desirability = 整数 **-3 ~ +3**，这事**在她心里**合不合意：",
        "    +3 = 整个人被击中 —— 心里写「咯噔一下」「这句话我记住了」（一见钟情级）；",
        "    +2 = 明显暖到 —— 心里写「被戳到了」「鼻子有点酸」；",
        "    +1 = 有点受用 —— 心里写「嘴上不认，其实挺受用」「被逗得没绷住」；",
        "     0 = 无所谓 / 看戏 / 看不出；",
        "    -1 = 有点不爽 —— 心里写「算了」「有点膈应」；",
        "    -2 = 心里堵 —— 心里写「下不来台」「这人怎么这样」；",
        "    -3 = 整个人凉了 —— 心里写「原来是这样」「整个人空了」（被背叛级）。",
        "  cared = 0 / 1 / 2，她在不在意：",
        "    0 = 纯看戏 / 跟她无关（**代码会给 0 分，不是负分**：别人吵架、刷屏、冷梗都填这个）；",
        "    1 = 有点在意；2 = 很在意。",
        "  纪律：**±3 一天最多一次**；同一件小事不要反复给；方向要平衡。",
        "· （兼容）impact：只想给一个数时可直接填 -3~+3，语义同 desirability，cared 按 2 算。",
        "· anchor_level：**必填**，整数 **-2/-1/0/+1/+2** = 她**此刻整体心情**的绝对档位",
        "  （相对她平时的基线），**不是**这件事对她的影响（那是 desirability）：",
        "    -2 很糟（心里写「整个人空了」）/ -1 偏低（「有点提不起劲」）/ 0 平稳（「就这样吧」）/",
        "    +1 不错（「今天还行」）/ +2 很好（「心里是亮的」）。",
        "  ⚠️ 这一项**占最终心情的大头**（每窗都按权重混进去），所以**照她的心理活动填真实档位**：",
        "     她心里亮就 +1/+2，闷就 -1/-2；只有**真的看不出**才填 0。",
        "     （别为了好看往 ±2 抬，也别一律填 0 —— 一律填 0 会把她每窗往基线拽。）",
        "     判据同样只有她的【心理活动】。缺这一项会让这窗的绝对纠偏失效。",
        "· bond_word（可选）：参考【她的档案】，给一个 **4~10 字**的描述性说法，",
        "  说明「她现在怎么看他」，例：「又爱又嫌的老熟人」「不想搭理的陌生人」「可以损两句的熟人」。",
        "  档案没提到这个人、或这次没什么变化，就省略这个字段。",
        "· arousal / dominance：各填 low / mid / high 之一，**不影响 delta**。",
        "  arousal = 她此刻多「上头」（激动/亢奋/心跳快）；dominance = 她此刻多有掌控感（被压着 / 说得上话）。",
        "  看不出来就填 mid。这两个只是记录她的状态，别为了让它们好看去改 delta。",
        "· confidence：0.00~1.00，你对这次判读有多确定。信息少、含糊、只是旁观 → 低（0.3~0.5）；",
        "  明确冲她来、她的心里话写得很清楚 → 高（0.8~0.9）。",
        "· relation：只在她和「正在说话这个人」之间发生了**关系事件**时才填，否则省略：",
        '  {"trust": -1|0|1, "respect": -1|0|1}。trust=可信/失信（答应的事做到/骗她），',
        "  respect=能力/地位（露了一手/当众贬低她）。普通闲聊一律 0，别乱加。",
        "· relation_relevance：0.00~1.00，这次的事和「她跟这个人」有多相关；",
        "  缺省 0.5（不确定，半信）；world/self 引起的填 0。",
        "· cause：这次心情变化是**谁/什么**引起的，只能填四个值之一：",
        "  speaker（正在说话这个人：夸/骂/逗/哄/无视）｜environment（群里的气氛、话题、别人）｜",
        "  self（她自己的事：想起以前、困了）｜other（群里另一个人，不是当前说话人）。",
        "  ⚠️ 只有 speaker 才算到「对他的好感」上；环境好笑不等于对他好感涨。",
        "· other_name（可选）：cause=other 时，写**第三个人的名字**"
        "（照抄群聊里显示的名字，不带括号后缀）；不确定就省略。",
        "· why：一句话说清「发生了什么事、她为什么这样」（30 字内，写成**事件**，"
        "不写空泛判断）。例：「他当众喊我外号，我有点下不来台」。",
        "· **以【她这条在回】+【她的心理活动】为主**，更早的群聊只当背景 ——",
        "  别把 8 条的情绪平均成一个数，那会变成「综合印象」而不是「此刻的心情」。",
        "· 依据权重：她的心理活动 > 她这条在回的内容 > 更早的群聊 > 上一轮判读。",
        "· 只判**她的感受**：她理解错了别人的意思也算数（那是她的感受）；",
        "  你要判的是「这件事是不是冲她说的、她有没有往心里去」，不是替她纠正理解。",
        "· 别被夸张网络用语骗（「笑死」多半只是好笑，不是极端情绪）；",
        "  也别因为她说「没事」就真当没事 —— 看她怎么写的。",
    ]
    if repair:
        # 道歉/哄她 = 修复事件：好感要回暖，心情允许回升。
        # 用户 2026-11-20：「道歉可以破冰，可以回暖好感以及回升一些心情。」
        lines += [
            "· **【修复事件】** 对方刚在道歉 / 哄她：这是关系回暖，不是又一次伤害。",
            "  cause 填 speaker，value 可以比上一轮**回升**一点；",
            "  她嘴上可能还在别扭，但心里已经软了 —— 按「被哄到」判，别继续压低。",
        ]
    lines += [
        "",
        "只输出 JSON，不要 markdown、不要解释：",
        '{"mind":"嘴上不认，其实挺受用","word":"被夸得有点飘",'
        '"desirability":1,"cared":2,"impact":1,"anchor_level":1,'
        '"arousal":"mid","dominance":"high","confidence":0.8,'
        '"cause":"speaker","why":"他夸了她，她装没事",'
        '"relation":{"trust":0,"respect":1},"relation_relevance":0.8,'
        '"bond_word":"嘴硬但吃这套的熟人"}',
    ]
    return "\n".join(lines)


def _parse_json(text: str) -> dict:
    t = str(text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    try:
        d = json.loads(t)
        return d if isinstance(d, dict) else {}
    except Exception:
        m = re.search(r"\{.*\}", t, re.S)
        if m:
            try:
                d = json.loads(m.group(0))
                return d if isinstance(d, dict) else {}
            except Exception:
                pass
    return {}


def resolve_target(cause: str, speaker_qq: str, address_qq: str = "",
                   address_kind: str = "", address_conf: float = 0.0,
                   other_qq: str = "") -> tuple:
    """把「cause + Address」合成 (target, target_qq)。

    规则（DESIGN §3.5 / R1/R2）：
      · 只有 **高置信** 且 `to_kind=person` 才信 Address；否则一律记说话人；
      · A 对 B 说（Address 指向 B、B≠说话人）→ target=recipient，好感记 B；
      · Address 指向说话人自己 → speaker；
      · environment/self → world/self，不落到人头上。
    """
    cause = str(cause or "speaker")
    speaker_qq = str(speaker_qq or "")
    target = {"speaker": "speaker", "other": "other", "self": "self",
              "environment": "world"}.get(cause, "world")
    tgt_qq = speaker_qq
    try:
        ok = (str(address_kind) == "person"
              and str(address_qq).isdigit()
              and float(address_conf) >= tuning.f("ADDR_ASSERT_MIN"))
    except Exception:
        ok = False
    if ok and cause == "speaker":
        if str(address_qq) != speaker_qq:
            return "recipient", str(address_qq)
        return "speaker", speaker_qq
    if cause == "other":
        # 2026-11-25（P0-5）：第三人必须能归因。判读输出 other_name，
        # 这里已经解析成 QQ；解析不出来就返回空 QQ（**不要**退回说话人，
        # 否则「C 骂她」会记到 A 头上）。
        _oq = str(other_qq or "")
        return "other", (_oq if _oq.isdigit() else "")
    return target, tgt_qq


def resolve_name_qq(session_key: str, name: str) -> str:
    """把群聊里显示的名字解析成 QQ（判读只看得见名字）。

    只用 group_scene 的现场名册（`names_of` 缓存），不做全量档案扫描。
    解析不出来返回空串 —— 宁可不动好感，也不要记到错的人头上。
    """
    nm = str(name or "").strip()
    if not nm:
        return ""
    key = str(session_key or "")
    gid = key[6:] if key.startswith("group_") else ""
    if not gid:
        return ""
    try:
        from . import group_scene
        mp = group_scene.names_of(gid, {}) or {}
    except Exception:
        return ""
    if str(mp.get(nm) or "").isdigit():
        return str(mp[nm])
    # 宽松匹配：判读可能少写后缀/多写括号
    for k, v in mp.items():
        k2, v2 = str(k), str(v)
        if not v2.isdigit():
            continue
        if nm and (nm in k2 or k2 in nm):
            return v2
    return ""


def _clean_word(raw: str) -> str:
    w = re.sub(r"\s+", "", str(raw or ""))
    w = w.strip("「」『』“”\"'。，,、：:；;")
    return w[:12]


def _ctx_of(session_key: str, ctx_lines=None) -> list:
    """判读上下文：**优先用调用方给的窗口**，现场只做兜底。

    2026-11-26（2026-11-26 审计修复）：以前反过来——只要群里有历史就返回「最后 8 条」，
    把 `split_windows` 切好的窗口丢掉。于是 40 条的段切成 5 窗，
    5 次判读拿到的是同一份输入，`_COVERAGE` 只证明 msg_id 被记账、
    不证明被读过。现在 `ctx_lines` 非空就必须用它。
    """
    out = [re.sub(r"\s+", " ", str(x or "")).strip()[:160]
           for x in (ctx_lines or []) if str(x or "").strip()]
    if out:
        return out[-CTX_LINES:]
    rows = scene_lines(session_key)
    return rows[-CTX_LINES:] if rows else []


async def _mind_only(words: str, think: str, ctx: list, word: str) -> str:
    """
    判读模型没给 mind 时，**单独补问一句心里话**（max_tokens 60，很便宜）。

    为什么要兜底：deepseek-chat 经常只回 word/value/why，把 mind 省掉 ——
    用户 2026-11-19 实测「还是没心里话」就是这个原因。
    补问只在她没写 MEMO、主判读又没给 mind 时才发生。
    """
    base, key = _base()
    if not base or not key:
        return ""
    lines = [f"她刚被判为「{word}」。" if word else "她刚回完一句。",
             f"她自己说的话：{words or '（没写）'}"]
    if think:
        lines.append(f"她的心理活动：{think[:120]}")
    if ctx:
        lines.append("最近聊天：")
        lines.extend(f"- {x}" for x in (ctx or [])[-8:])
    lines += [
        "",
        "用**第一人称**写一句她此刻的心里话（20 字内，像心里嘀咕，不复述事件、不客套）。",
        "只输出这句话本身：不要引号、不要解释、不要换行。",
    ]
    payload = {
        "model": MODEL or "deepseek-chat",
        "messages": [
            {"role": "system", "content": "你只输出一句中文，不加引号。"},
            {"role": "user", "content": "\n".join(lines)},
        ],
        "temperature": 0.4,
        **_call_tokens("judge", 60),
    }
    try:
        import httpx
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.post(f"{base}/chat/completions",
                             headers={"Authorization": f"Bearer {key}"},
                             json=payload)
            r.raise_for_status()
            data = r.json()
            try:
                from . import llm_usage as _lu
                _lu.record_from_response(data, purpose="judge", provider="judge", model=MODEL or "deepseek-chat")
            except Exception:
                pass
        txt = str(((((data or {}).get("choices") or [{}])[0]
                    .get("message") or {}).get("content")) or "")
        txt = re.sub(r"\s+", "", txt).strip("「」『』“”\"'。，,")
        return txt[:60]
    except Exception as e:
        logger.debug(f"[MOOD-JUDGE] 补问心里话失败: {type(e).__name__}: {e}")
        return ""


def note_inferred(session_key: str, text: str, *, src: str = "", reply: str = "") -> None:
    """记一条「判读模型猜的心里话」——只进 INFERRED_FILE，不进 THINK_FILE。"""
    key = str(session_key or "").strip()
    t = re.sub(r"\s+", " ", str(text or "")).strip()[:200]
    if not key or not t:
        return
    try:
        d = {}
        if INFERRED_FILE.exists():
            d = json.loads(INFERRED_FILE.read_text(encoding="utf-8")) or {}
        lst = d.setdefault(key, [])
        lst.append({"at": time.time(), "text": t,
                    "src": str(src or "")[:80], "reply": str(reply or "")[:80],
                    "source": "judge_guess"})
        d[key] = lst[-max(1, THINK_KEEP):]
        INFERRED_FILE.parent.mkdir(parents=True, exist_ok=True)
        INFERRED_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        logger.debug(f"[MOOD-JUDGE] 猜测心里话落盘失败: {type(e).__name__}: {e}")


async def backfill_think(session_key: str, *, src: str = "",
                         reply: str = "") -> str:
    """
    她没写 [[THINK]] 时，让模型猜一句「她可能在想什么」。

    ⚠️ 2026-11-25：这只是**猜测**，不再写进 `data/mood_think.json`（那是她的心理活动，
    是标注/状态卡的唯一来源）。猜测写 `data/mood_think_inferred.json`，任何展示都不读。
    返回值也仅供日志/诊断，不要再当 think 传给 judge。
    """
    if not BACKFILL:
        return ""
    key = str(session_key or "")
    try:
        ctx = _ctx_of(key)
    except Exception:
        ctx = []
    t = await _mind_only("", "", ctx, "")
    if t:
        note_inferred(key, t, src=src, reply=reply)
        logger.info(f"[MOOD-JUDGE] {key} 这条没写 THINK，记了一条猜测（不进 THINK_FILE）：{t[:30]}")
    return t


async def judge(words: str, ctx_lines=None, *, think: str = "",
                prev_word: str = "", prev_value: float = 0.0,
                hint: str = "", repair: bool = False,
                archive: str = "", src: str = "",
                session_key: str = "") -> dict:
    """
    让判读模型给 {word, value, why}。失败返回 {"ok": False, "err": ...}，绝不抛。
    """
    words = str(words or "").strip()
    think = str(think or "").strip()
    if not ENABLED:
        return {"ok": False, "err": "MOOD_JUDGE_ENABLED=false"}
    ctx = _clip_ctx(ctx_lines)
    # 她没写也可以判：按最近这段群聊判断她此刻的状态（用户 2026-11-19）
    if not (words or think or ctx):
        return {"ok": False, "err": "她没写、也没有上下文，不判"}
    base, key = _base()
    if not base or not key:
        return {"ok": False, "err": "没配 XAI_BASE_URL / XAI_API_KEY"}

    prompt = _build_prompt(words, think, ctx, prev_word, prev_value,
                           hint=hint, repair=repair, archive=archive, src=src)
    payload = {
        "model": MODEL or "deepseek-chat",
        "messages": [
            {"role": "system", "content": "你只输出 JSON，不聊天。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        **_call_tokens("judge", 200),
    }
    try:
        import httpx
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.post(f"{base}/chat/completions",
                             headers={"Authorization": f"Bearer {key}"},
                             json=payload)
            r.raise_for_status()
            data = r.json()
            try:
                from . import llm_usage as _lu
                _lu.record_from_response(data, purpose="judge", provider="judge", model=MODEL or "deepseek-chat")
            except Exception:
                pass
        content = str((((data or {}).get("choices") or [{}])[0]
                       .get("message") or {}).get("content") or "")
    except Exception as e:
        return {"ok": False, "err": f"{type(e).__name__}: {e}"}

    if JUDGE_TRACE and content:
        _trace(session_key, prompt, content)
    d = _parse_json(content)
    word = _clean_word(d.get("word"))
    # 2026-11-20 定稿：判读报的是**涨幅 Δ**（这件事让她涨/跌多少），不是当前心情值。
    # 兼容老模型只给 value 的情况：Δ = value - 上一轮 value。
    # 2026-11-25（P2）：判读只报**档位 impact**，步长由代码查表。
    # 仍兼容老模型给 delta / value：换算成档位后走同一条链路。
    _impact_raw = d.get("impact", d.get("档位"))
    _delta_raw = d.get("delta", d.get("Δ"))
    impact = None
    # 2026-11-26（v2.5）：优先两段式 desirability × cared（原审计 §4 的建议）。
    # 看戏（cared=0）有显式出口 → impact=0，不靠模型自觉。
    _des = d.get("desirability")
    if _des is not None:
        try:
            _dv_des = max(-3, min(3, int(round(float(_des)))))
            _cared = d.get("cared")
            _cv = 2 if _cared is None else max(0, min(2, int(round(float(_cared)))))
            if _cv == 0 or _dv_des == 0:
                impact = 0
            else:
                _mag = max(1, int(round(abs(_dv_des) * _cv / 2.0)))
                impact = _mag if _dv_des > 0 else -_mag
        except Exception:
            impact = None
    if impact is None and _impact_raw is not None:
        try:
            impact = max(-3, min(3, int(round(float(_impact_raw)))))
        except Exception:
            impact = None
    if impact is None:
        _d = None
        if _delta_raw is not None:
            try:
                _d = max(-1.0, min(1.0, float(_delta_raw)))
            except Exception:
                _d = None
        if _d is None:
            try:
                _d = max(-1.0, min(1.0, float(d.get("value"))))
                _d = max(-1.0, min(1.0, _d - float(prev_value or 0.0)))
            except Exception:
                _d = None
        if _d is None:
            return {"ok": False,
                    "err": f"impact 不是数字：{content[:120]}",
                    "raw": content[:400]}
        _a = abs(_d)
        impact = 0 if _a < 0.03 else (1 if _a < 0.15 else (2 if _a < 0.30 else 3))
        if _d < 0:
            impact = -impact
    # 2026-11-27（外部审计）：`±3 一天最多一次`以前只写在 prompt 纪律里，
    # 代码没有计数 → 活跃群一天能刷好几次。现在按会话按自然日计数，
    # 超了把 ±3 降成 ±2（保留「这是件大事」，只是不再顶格）。
    _impact_before_cap = impact
    try:
        from . import mood as _mood_cap
        _eff_imp = _mood_cap.cap_daily_impact(session_key, impact)
        if _eff_imp != impact:
            logger.info(f"[MOOD-JUDGE] {session_key or '?'} ±3 当日额度已用完"
                        f" → {impact:+d} 降为 {_eff_imp:+d}")
            impact = _eff_imp
    except Exception as e:
        logger.debug(f"[MOOD-JUDGE] ±3 日计数失败: {type(e).__name__}: {e}")
    # delta 只留给「记忆刻痕 / 好感 / 群心情」当强度用，不再是状态更新量。
    # 2026-11-26：步长从 tuning 读，不再硬编码 0.40/0.22/0.10——
    # 否则 MOOD_STEP_2 只管心情、不管好感和刻痕（第二份真相）。
    _step = (tuning.f("MOOD_STEP_3") if abs(impact) >= 3
             else (tuning.f("MOOD_STEP_2") if abs(impact) >= 2
                   else tuning.f("MOOD_STEP_1")))
    delta = round(_step * (0 if impact == 0 else (1 if impact > 0 else -1)), 2)
    if not word:
        return {"ok": False, "err": "word 为空", "raw": content[:400]}
    # src 给 /测函数 心情 看「这次主要是按什么判的」：
    # 她的话 > 她的心理活动 > 最后一条**非图片**的聊天（图片那行没信息量）
    src = words or think
    if not src:
        for x in reversed(ctx or []):
            if "[图片]" not in x and "http" not in x:
                src = x
                break
        if not src:
            src = (ctx[-1] if ctx else "")
    mind = re.sub(r"\s+", "", str(d.get("mind") or "")).strip("「」『』\"'")
    if not mind:
        # 主判读没给就单独补问一句（只在她没写 MEMO 时才会走到这儿）
        mind = await _mind_only(words, think, ctx, word)
    # cause：这次心情变化是谁引起的（speaker / environment / self / other）。
    # 2026-11-20：好感不再由判读直接给，改为「Δmood × cause」本地推导。
    cause = str(d.get("cause") or "").strip().lower()
    if cause not in ("speaker", "environment", "self", "other"):
        cause = "speaker" if (words or think) else "environment"
    # a/d：三档枚举 → 0~1。**只记录**，不进 z/Δ（emotion §1.5）。
    _ar = _AROUSAL_LEVELS.get(str(d.get("arousal") or "").strip().lower())
    _do = _AROUSAL_LEVELS.get(str(d.get("dominance") or "").strip().lower())
    try:
        _conf = max(0.0, min(1.0, float(d.get("confidence", 0.6))))
    except Exception:
        _conf = 0.6
    # 锚点档位：**必填**（2026-11-26 用户拍板：它占最终心情的大头）。
    # 模型没给就说明这条绝对链路断了 —— 记一次 miss + 打 INFO（同一会话 60s 一次），
    # 本窗不做锚点（宁可不动，也不要拿 desirability 猜她的绝对档位）。
    try:
        _anchor = max(-2, min(2, int(round(float(d.get("anchor_level"))))))
    except Exception:
        _anchor = None
    note_anchor_stat(_anchor is not None)
    if _anchor is None:
        _now = time.time()
        _prev_miss = float(_ANCHOR_MISS_AT.get(session_key) or 0)
        if _now - _prev_miss > 60:
            _ANCHOR_MISS_AT[session_key] = _now
            logger.info(f"[MOOD-JUDGE] {session_key or '?'} 判读没填 anchor_level"
                        f"（必填）→ 本窗不做绝对锚点")
    # 关系事件：只认 trust / respect 两个 -1|0|1
    _rel = d.get("relation") if isinstance(d.get("relation"), dict) else {}

    def _tri(k):
        try:
            return max(-1, min(1, int(round(float(_rel.get(k, 0))))))
        except Exception:
            return 0

    try:
        _relevance = max(0.0, min(1.0, float(d.get("relation_relevance", 0.5))))
    except Exception:
        _relevance = 0.5
    _target = str(d.get("target") or "").strip().lower()
    if _target not in ("speaker", "recipient", "other", "world", "self"):
        _target = {"speaker": "speaker", "other": "other", "self": "self",
                   "environment": "world"}.get(cause, "world")
    return {"ok": True, "word": word, "delta": delta, "impact": impact,
            # 被日额度降档时留原值，`/测函数 心情` 能看出「模型报的是 ±3」
            "impact_raw": _impact_before_cap,
            "anchor_level": _anchor,
            "relation_trust": _tri("trust"), "relation_respect": _tri("respect"),
            "relation_relevance": _relevance, "target": _target,
            "arousal": _ar, "dominance": _do, "confidence": _conf,
            "bond_word": str(d.get("bond_word") or "").strip()[:16],
            "other_name": str(d.get("other_name") or "").strip()[:20],
            "cause": cause,
            "mind": mind[:60],
            "why": str(d.get("why") or "").strip()[:80],
            "src": str(src)[:80],
            "at": time.time(), "raw": content[:400]}


def _skip(session_key: str, reason: str) -> dict:
    """
    记下「这次没判」的原因，并**打一条 INFO**（同一原因 60s 内只打一次，防刷屏）。

    为什么要 INFO：以前节流/没上下文/模型报错全是 debug，真机 docker logs 里
    一条都看不到，只能回一句「还没判」——等于没有诊断。
    """
    key = str(session_key or "")
    now = time.time()
    _TRACE[key] = {"at": now, "ok": False, "err": reason}
    prev = _SKIP_AT.get(key) or ("", 0.0)
    if prev[0] != reason or now - float(prev[1]) > 60:
        _SKIP_AT[key] = (reason, now)
        logger.info(f"[MOOD-JUDGE] {key or '?'} 这次没判：{reason}")
    return {"ok": False, "err": reason}


def trace_text(session_key: str, judge: dict = None) -> str:
    """
    /测函数 心情 用：上次到底跑没跑、卡在哪、现在能不能判。

    ⚠️ `_TRACE` 是**内存态**，重启就丢；而判读结果 `mood.get_judge()` 是落盘的。
    用户 2026-11-19 就撞上这个矛盾：上面显示「判于 21:07:34」，
    下面却写「这次启动后还没跑过」——重启过一轮。
    所以这里接受 judge 兜底：内存没有就看磁盘，说清楚「这是重启前判的」。
    """
    key = str(session_key or "")
    now = time.time()
    lines = []
    t = _TRACE.get(key) or {}
    if t:
        ts = time.strftime("%m-%d %H:%M:%S", time.localtime(float(t.get("at") or 0)))
        if t.get("ok"):
            lines.append(f"· 上次尝试：{ts} 成功 → {t.get('word')}"
                         f"（心情值 {float(t.get('v') or 0):+.2f}"
                         f"｜impact {int(t.get('impact') or 0):+d}"
                         f"｜P {float(t.get('P') or 0):.2f}）")
        else:
            lines.append(f"· 上次尝试：{ts} 没判成 → {t.get('err')}")
    elif judge and judge.get("at"):
        ts = time.strftime("%m-%d %H:%M:%S", time.localtime(float(judge.get("at") or 0)))
        lines.append(f"· 上次判读（已落盘）：{ts} → {judge.get('word')}"
                     f"（{float(judge.get('value') or 0):+.2f}）")
        lines.append("  （本次启动后还没再触发过；内存里的尝试记录重启会丢）")
    else:
        lines.append("· 上次尝试：这次启动后还没跑过（她要回一句才触发；也可以「重判」）")
    last_at = float(_LAST_AT.get(key) or 0)
    if last_at:
        left = MIN_INTERVAL - (now - last_at)
        if left > 0:
            lines.append(f"· 下次可判：{left:.0f}s 后（MIN_INTERVAL={MIN_INTERVAL:.0f}s）")
        else:
            lines.append(f"· 下次可判：现在就可以（上次调用 {now - last_at:.0f}s 前）")
    else:
        lines.append("· 下次可判：现在就可以（本会话还没判过）")
    try:
        lines.append(f"· 判读预算：本小时还剩 {_calls_left(key, now)} 次"
                     f"（≤{JUDGE_MAX_PER_HOUR}）")
    except Exception:
        pass
    lines.append(anchor_stat_text())
    try:
        _pn = pending_think_count(key)
        if _pn:
            lines.append(f"· 攒着的 THINK：{_pn} 条（节流期间没丢，下次一起判）")
    except Exception:
        pass
    try:
        n = len(scene_lines(key))
    except Exception:
        n = 0
    lines.append(f"· 现在能拉到上下文：{n} 条（上限 {CTX_LINES}）")
    try:
        from . import mood as _mood
        _ad = _mood.ad_of(key)
        lines.append(f"· a/d（只记录）：唤醒 {_ad['a']:.2f}"
                     f"（基线 {_ad['base_a']:.2f}）｜掌控 {_ad['d']:.2f}"
                     f"（基线 {_ad['base_d']:.2f}）")
    except Exception:
        pass
    _cov = last_coverage(key)
    if _cov:
        lines.append(f"· 上次段判读覆盖：{len(_cov)} 条消息（P4：应为段内全集）")
    return "\n".join(lines)


async def judge_and_store(session_key: str, words: str, ctx_lines=None, *,
                          think: str = "", force: bool = False,
                          speaker_qq: str = "", hint: str = "",
                          src: str = "",
                          repair: bool = False, turn_id: str = "",
                          passive: bool = False,
                          addressed: bool = False,
                          records=None, address_qq: str = "",
                          address_kind: str = "",
                          address_conf: float = 0.0) -> dict:
    """
    判读 + 写进 mood（影子字段，不影响老数值）。

    三层闸门：
      1. 同一会话 MIN_INTERVAL 内只判一次（她连发几句不用每句都调）；
      2. 她**没主动写心情**时，只在「上次判读超过 REFRESH 秒」或还没有结果时才判
         （否则每轮都调一次模型，白花钱）；
      3. 上下文按**条数**拉最近 CTX_LINES 条（现场优先，退回落盘日志）。
      4. 传了 `records`（段）时，段内按 **8 条 / 5min** 切窗，前面的窗按「旁观」补判
         （emotion §5.1；不传 records 就还是单窗，行为与旧版一致）。
    """
    key = str(session_key or "")
    if not ENABLED:
        return _skip(key, "总开关关着（MOOD_JUDGE_ENABLED=false）")
    # 2026-11-27 日预算刹车：判读占全天 token 的 ~24%，
    # 额度用完就整条不调（心情停在原地，明天恢复）。
    try:
        from . import budget_guard as _bg
        if _bg.state() == "rest":
            return _skip(key, "今日额度用完（LLM_DAILY_BUDGET）→ 不判")
    except Exception:
        pass
    now = time.time()
    # 这一轮带回来的心理活动（节流时也要留住，见 _PENDING_THINK 的注释）
    _think_in = str(think or "").strip()
    if not force and now - float(_LAST_AT.get(key) or 0) < MIN_INTERVAL:
        left = MIN_INTERVAL - (now - float(_LAST_AT.get(key) or 0))
        if _think_in:
            _n = _push_pending_think(key, _think_in)
            logger.info(f"[MOOD-JUDGE] {key} 节流 {left:.0f}s，THINK 先攒着"
                        f"（{_n} 条），下次一起判")
        return _skip(key, f"节流：还要 {left:.0f}s（MIN_INTERVAL={MIN_INTERVAL:.0f}s）")

    if _calls_left(key, now) <= 0:
        if _think_in:
            _n = _push_pending_think(key, _think_in)
            logger.info(f"[MOOD-JUDGE] {key} 判读预算用完，THINK 先攒着（{_n} 条）")
        return _skip(key, f"判读预算用完（≤{JUDGE_MAX_PER_HOUR} 次/小时）")

    # 段 → 窗口（emotion §5.1）：段是投递单位（≤40 条 / ≤30s），窗口是判读单位（8 条 / 5min）。
    # 前面的窗口按「旁观」补判（passive，Δ 有上限），最后一窗走正常路径（带她的回复/心里话）。
    if records:
        _wins = split_windows(records)
        # 预算不够时丢掉最早的窗，只保留最近的（最后一窗必须有）
        _left = max(1, _calls_left(key, now))
        if len(_wins) > _left:
            logger.info(f"[MOOD-JUDGE] {key} 段 {len(_wins)} 窗，预算只够 {_left} 窗，"
                        f"丢掉最早的 {len(_wins) - _left} 窗")
            _wins = _wins[-_left:]
        # 2026-11-26：只记**真正判过的窗**（最后一窗）。passive 窗不调模型，
        # 把它们的 msg_id 也记进来会让 coverage 变成「看到过」而不是「判过」。
        _COVERAGE[key] = [str(r.get("mid") or "") for r in _wins[-1]
                          if r.get("mid")]
        if len(_wins) > 1:
            # 2026-11-26（2026-11-26 审计修复）：passive 窗不写任何状态，就没必要调模型。
            # 以前每窗都 await judge_and_store(passive=True)：一次 LLM 调用 +
            # 一次预算，最后在 `if passive: return` 全部丢掉 —— 40 条的段
            # 5 次调用里 4 次纯烧钱。现在只记窗口计数（ADR-037），
            # 判读预算全部留给最后一窗。
            for _w in _wins[:-1]:
                try:
                    from . import mood as _mood_w
                    _mood_w.note_window(key)
                except Exception:
                    pass
            ctx_lines = _fmt_window(_wins[-1])
            logger.info(f"[MOOD-JUDGE] {key} 段 {len(records)} 条 → "
                        f"{len(_wins)} 窗（前 {len(_wins) - 1} 窗只记账不判）")

    prev = {}
    try:
        from . import mood
        prev = mood.get_judge(key) or {}
    except Exception:
        prev = {}

    ctx = _ctx_of(key, ctx_lines)
    # 本次她刚写的心理活动（决定「算不算她主动写了」）
    # 2026-11-26：把节流期间攒下的 THINK 一起并进来 —— 否则「她每条都写了 THINK」
    # 在链路上会表现成「这一窗没写」，整窗白丢（用户实测发现的）。
    _pend = _pop_pending_think(key)
    if _pend:
        logger.info(f"[MOOD-JUDGE] {key} 并入攒下的 {len(_pend)} 条 THINK")
    _incoming = "；".join(_pend + ([_think_in] if _think_in else []))
    # 兜底（2026-11-26）：她每条回复都写 THINK，但有的调用路径只落盘、没把 think 传进来。
    # 本次没有 incoming 时，就看「上次判读之后」有没有新落盘的 THINK，有就拿来用。
    # 只取 `at > 上次判读时间` 的，避免同一条 THINK 被判两次。
    if not _incoming:
        try:
            _since = float(prev.get("at") or 0) if prev else 0.0
            _new = [x["text"] for x in recent_think_items(key, 6)
                    if float(x.get("at") or 0) > _since and not x.get("auto")]
            if _new:
                _incoming = "；".join(_new[-3:])
                logger.info(f"[MOOD-JUDGE] {key} 用落盘的新 THINK {len(_new)} 条（调用方没传 think）")
        except Exception:
            pass
    wrote = bool(str(words or "").strip() or _incoming)
    # 判读时把最近几条心理活动也带上：重判/事后诊断时没有「当场」的 think，
    # 但落盘的历史心里话同样能说明她当时是怎么想的。
    try:
        _hist = "；".join(recent_think(key, 3))
    except Exception:
        _hist = ""
    use_think = _incoming or _hist
    if not wrote and not force:
        _age = (now - float(prev.get("at") or 0)) if prev else 1e9
        if prev and _age < REFRESH:
            return _skip(key, f"她没写 THINK，{int(REFRESH - _age)}s 后再看"
                               "（无 THINK 不改心情）")
    if not (wrote or ctx):
        return _skip(key, "她没写、现场和日志也拉不到消息")
    # 2026-11-25 ADR-038：这一窗**没有她自己的话/心理活动** → 只记录，不改状态。
    # 以前会退到「历史 THINK」或「群聊」硬判，等于用外界猜她的心情。
    if not wrote:
        passive = True
    # 既然不写状态，就不必再花一次判读调用（以前每 5 分钟白调一次）。
    # 手动 `/测函数 心情 重判` 传 force=True，仍可跑一次看诊断。
    if not wrote and not force:
        _res = {"ok": True, "passive": True, "word": "", "impact": 0,
                "delta": 0.0, "cause": "environment", "why": "", "mind": "",
                "src": "", "at": time.time(),
                "note": "没有她的心理活动，不调用判读"}
        try:
            from . import mood as _m
            _res["v_after"] = float(_m.get_mood(key).get("value") or 0.0) / 100.0
        except Exception:
            pass
        _TRACE[key] = {"at": time.time(), "ok": True, "passive": True}
        logger.info(f"[MOOD-JUDGE] {key} 无 THINK → 跳过判读（不写心情）")
        return _res

    _LAST_AT[key] = now
    # 人设底色（ADR-010）：好感步长/道歉回补/情绪反应都吃它
    try:
        from . import persona_traits as _pt
        _tr = _pt.traits_for_session(key)
        logger.info(f"[PERSONA] {key} warmth={_tr['warmth']:.2f}"
                    f" forgiveness={_tr['forgiveness']:.2f}"
                    f" trust_speed={_tr['trust_speed']:.2f}"
                    f" reactivity={_tr['reactivity']:.2f}")
    except Exception:
        _tr = {"warmth": 0.5, "forgiveness": 0.5, "trust_speed": 0.5,
               "reactivity": 0.5, "sensitivity": 1.0, "expressiveness": 0.5}
    # 档案摘要（给判读参考，用来起「好感名词」）：好感 + 亲近 + 最近两条相处印象
    _archive = ""
    try:
        if speaker_qq:
            from . import profiles as _pf
            _notes = (_pf.get_bond(speaker_qq).get("notes") or [])[-2:]
            _archive = "好感%+.2f｜亲近%.2f｜%s" % (
                float(_pf.get_affinity(speaker_qq)),
                float(_pf.intimacy(speaker_qq)),
                "；".join(str(n.get("text") or "")[:20] for n in _notes))
    except Exception:
        _archive = ""
    _CALLS.setdefault(key, []).append(now)     # 记一次判读调用（预算）
    res = await judge(words, ctx, think=use_think,
                      prev_word=str(prev.get("word") or ""),
                      prev_value=float(prev.get("value") or 0.0),
                      hint=hint, repair=repair, archive=_archive,
                      src=src, session_key=key)
    if res.get("ok"):
        # 2026-11-25 P1：`mind` 只存**她自己写的 THINK**；判读模型的猜测改名 `guess`，
        # 只留在诊断里，不能当「她当时的心里话」写进记忆/状态。
        res["guess"] = str(res.get("mind") or "")
        res["mind"] = str(use_think or "").strip()
        try:
            from . import mood
            # ---- 一个 Δ，三处用（2026-11-20 最终定稿）----
            #   判读报的就是「这件事让她涨/跌多少」（增量），不是当前心情值。
            #   |Δ|     → salience（记忆 💥 / 检索加成 / 遗忘）
            #   sign(Δ) → valence（好 / 坏）
            #   Δ×gain×cause → 好感涨落（只有 speaker 引起的心情才算到人头上）
            #   当前心情值 = 把 Δ 累加到一个持续变量上（-1~1），随时增减。
            _delta = round(float(res.get("delta") or 0.0), 4)
            _delta = max(-mood.MOOD_MAX_DELTA, min(mood.MOOD_MAX_DELTA, _delta))
            _cause = str(res.get("cause") or "speaker")
            # 收发件人（D21）：只有**高置信** Address 才改归因；低置信保持「说话人」，
            # 否则误判一次就把好感记到无关的人头上。
            # 第三人（cause=other）先把 other_name 解析成 QQ，解析不出就不动好感
            _other_qq = ""
            if _cause == "other":
                try:
                    _other_qq = resolve_name_qq(key, res.get("other_name"))
                except Exception:
                    _other_qq = ""
                logger.info(f"[ADDR] {key} 第三人归因 → "
                            f"{_other_qq[-4:] if _other_qq else '解析失败（不动好感）'}"
                            f"（other_name={res.get('other_name') or '空'}）")
            _target, _tgt_qq = resolve_target(
                _cause, speaker_qq, address_qq, address_kind, address_conf,
                other_qq=_other_qq)
            # 写入核对（design §1.1）：判读给的 about_qq 也要过硬核对 ——
            # 不是本群见过/没被喊过的人，就置空不猜。说话人/Address 目标天然通过。
            if _tgt_qq:
                try:
                    from . import attribution as _attr
                    _kept, _awhy = _attr.verify(
                        _tgt_qq, session_key=key, speaker_qq=speaker_qq,
                        at_qq=(str(address_qq) if str(address_kind) == "person" else ""),
                        text=f"{res.get('why') or ''}{res.get('other_name') or ''}")
                    _attr.note("judge", _tgt_qq, _kept, _awhy,
                               target=_target,
                               conf=round(float(address_conf or 0.0), 2))
                    if not _kept:
                        logger.info(f"[ADDR] {key} 归因核对不过（{_awhy}）"
                                    f"→ about_qq 置空")
                        _tgt_qq = ""
                except Exception as e:
                    logger.debug(f"[ADDR] 归因核对失败: {type(e).__name__}: {e}")
            if _target == "recipient":
                logger.info(f"[ADDR] {key} 判读归因 → recipient "
                            f"{_tgt_qq[-4:]}（conf {float(address_conf):.2f}）")
            res["target"] = _target
            res["target_qq"] = _tgt_qq
            # ---- Δ 入口护栏（设计 §13.3 的前提：Δ 必须校准）----
            _raw_delta = _delta
            _guard = ""
            if abs(_delta) < NOISE_DELTA:
                if abs(_raw_delta) > 1e-9:
                    _guard = f"噪音丢弃（|Δ|{abs(_raw_delta):.3f} < {NOISE_DELTA}）"
                _delta = 0.0
            elif _cause != "speaker" and not addressed:
                _delta = max(-ENV_MAX_DELTA, min(ENV_MAX_DELTA, _delta))
                if abs(_delta) < abs(_raw_delta):
                    _guard = f"非冲她来封顶 ±{ENV_MAX_DELTA}"
            if _guard:
                logger.info(f"[MOOD-JUDGE] {key} Δ {_raw_delta:+.2f} → {_delta:+.2f}"
                            f"（{_guard}）")
            # 让调用方（日志 / 返回值）也看到**护栏之后**的 Δ，
            # 否则外面拿 res["delta"] 还会按 -0.07 去算刻痕。
            res["delta"] = _delta
            res["delta_raw"] = _raw_delta
            # 2026-11-25（P0-7）：relevance 闸门要管**整条链路**，不只 v。
            # 她没写 THINK 且 relation_relevance < gate → v/好感/记忆/群心情都不写。
            try:
                _relevance = _fget(res, "relation_relevance", 0.5)
            except Exception:
                _relevance = 0.5
            # 2026-11-26（审计）：以前是 `not wrote and ...`，但 `not wrote` 的窗口
            # 已经在上面 `if passive: return` 提前返回了 —— 所以这个条件恒 False，
            # MOOD_RELEVANCE_GATE 成了「有读点、有文档、永不执行」的死护栏。
            # 现在只看 relevance：她写了 THINK、但判读说这事跟她无关（<0.25）→ 只衰减。
            _skip_update = (_relevance < mood.RELEVANCE_GATE)
            # salience 按设计 §13.3：min(1, |Δ|/0.5) → 0.10/0.25/0.50 三档刻痕
            _sal = min(1.0, abs(_delta) / 0.5)
            _val = 1.0 if _delta > 0 else (-1.0 if _delta < 0 else 0.0)

            # 2026-11-25 P0-3 / ADR-038：旁观窗口（她没回、没有 THINK）不许改
            # 心情/好感/记忆刻痕/群心情。放在 Δ 护栏之后返回，这样调用方看到的
            # `res["delta"]` 仍是护栏后的值（诊断用），但不会落任何状态。
            if passive:
                try:
                    res["v_after"] = float(
                        mood.get_mood(key).get("value") or 0.0) / 100.0
                except Exception:
                    pass
                res["passive"] = True
                res["note"] = "旁观窗口（无 THINK），只记录不写状态"
                logger.info(f"[MOOD-JUDGE] {key} 旁观窗口（无 THINK）→ 不写心情")
                return res

            # ⓪ 群心情（独立于整体心情）：这个群最近 5 小时让她涨/跌了多少。
            #    · cause=speaker → 记到这个人头上（单人跌 0.4 就单独不理他）
            #    · cause=other/environment → 只算群环境（3 人以上 + 群跌 0.7 才整群安静）
            #    · cause=self → 她自己的事，不算群环境
            if (key.startswith("group_") and abs(_delta) > 1e-9
                    and not _skip_update):
                try:
                    from . import group_mood
                    group_mood.record(
                        key, _delta,
                        about_qq=(_tgt_qq if _target in ("speaker", "recipient", "other")
                                  else ""),
                        cause=_cause, why=res.get("why") or "",
                        mind=res.get("mind") or "",
                        word=res.get("word") or "")
                except Exception as e:
                    logger.debug(f"[MOOD-JUDGE] 写群心情失败: {type(e).__name__}: {e}")

            # ① 心情值（P2 唯一链路）：impact → z → K → 步进；
            #    MOOD_UPDATE=delta 时退回旧的「累加 Δ」，便于回滚。
            _prev_v = float(prev.get("value") or 0.0)
            _new_v = _prev_v
            _obs = {}
            try:
                if getattr(mood, "MOOD_UPDATE", "v2") == "v2":
                    # ⚠️ 2026-11-27（外部审计，真问题）：以前 relevance < 0.25 会
                    # **整条 mood.observe 都不调**。而 prompt 教模型「world/self
                    # 引起的 relation_relevance 填 0」——于是她自己的情绪
                    # （想起以前、困了）永远写不进 v，anchor 也一起丢，
                    # 和 ADR-038「心情只来自 THINK」、ADR-042「锚点占大头」冲突。
                    # 现在：relevance 只挡好感/记忆/群心情（下面那些 `_skip_update`），
                    # **心情与锚点照常步进**。
                    if _skip_update:
                        logger.info(
                            f"[MOOD-JUDGE] {key} relevance {_relevance:.2f}"
                            f" < {mood.RELEVANCE_GATE} → 心情照常步进，"
                            f"好感/记忆/群心情不写")
                    _imp = int(res.get("impact") or 0)
                    if passive:
                        _imp = max(-1, min(1, _imp))
                    _obs = mood.observe(
                        key, _imp, _fget(res, "confidence", 0.6),
                        sensitivity=(mood.sensitivity(key)
                                     * _fget(_tr, "sensitivity", 1.0)),
                        reactivity=_fget(_tr, "reactivity", 0.5),
                        anchor_level=res.get("anchor_level"),
                        word=res["word"], why=res.get("why") or "",
                        src=res.get("src") or "", about_qq=_tgt_qq,
                        mind=res.get("mind") or "")
                    _new_v = float(_obs.get("v") or 0.0)
                    res["v_before"] = _prev_v
                    res["v_after"] = _new_v
                    res["P"] = _obs.get("P")
                    res["K"] = _obs.get("K")
                    if _obs.get("anchored"):
                        res["anchored"] = _obs["anchored"]
                elif abs(_delta) < 1e-9:
                    # Δ=0：只记名词/心里话，不动数值（这才是「旁观不背锅」）
                    _new_v = float(mood.get_mood(key).get("value") or 0.0) / 100.0
                elif getattr(mood, "MOOD_MODE", "delta") == "level":
                    _vn = mood.set_value_from_judge(_prev_v + _delta, key,
                                                    word=res["word"],
                                                    why=res.get("why") or "",
                                                    src=res.get("src") or "",
                                                    about_qq=speaker_qq,
                                                    mind=res.get("mind") or "")
                    if _vn is not None:
                        _new_v = float(_vn) / 100.0
                else:
                    _new_v = mood.apply_delta(_delta, key, word=res["word"],
                                              why=res.get("why") or "",
                                              src=res.get("src") or "",
                                              about_qq=speaker_qq,
                                              mind=res.get("mind") or "")
            except Exception as e:
                logger.debug(f"[MOOD-JUDGE] 更新心情失败: {type(e).__name__}: {e}")

            # 2026-11-25（P0-8）：把「事件强度」和「实际位移」分开：
            #   _delta     —— 档位换算的名义步长（±0.10/0.22），代表**这件事有多重**
            #   _dv_actual —— mood.observe 走出来的**事件步进**（K/饱和之后），代表**这件事让她动了多少**
            # 审计类字段（mood_events / 日志）用实际位移；好感/记忆/群心情用事件强度
            # （它们衡量的是事件本身，且阈值是按 ±0.10/0.22 校准的）。
            #
            # ⚠️ 2026-11-26（外部审计）：`dv` **不含**锚点位移（`anchored`）。
            #   这是故意的，不是漏了：锚点是「对她此刻整体水平的重新估计」，
            #   不是「这个人造成了这件事」。把它算进好感 = 「她今天心情好，
            #   顺手记到旁边人头上」，违反唯一原则（好感是寄存在每个人那里的**涨落历史**）。
            #   想看这一窗 v 总共动了多少，用 `dv_total`（只做诊断，不进任何模型）。
            _dv_actual = float((_obs or {}).get("dv") or 0.0) if _obs else 0.0
            res["dv_actual"] = round(_dv_actual, 4)
            res["dv_total"] = round(float((_obs or {}).get("dv_total") or 0.0), 4) \
                if _obs else 0.0

            # ② 判读记录：word = 这件事的**反应词**；value = 累加后的新水平；
            #    delta = 这一次的涨幅（记忆/好感/档案都读它）
            mood.set_judge(key, res["word"], _new_v,
                           why=res.get("why") or "", src=res.get("src") or "",
                           mind=res.get("mind") or "", delta=_delta,
                           target=_target, target_qq=_tgt_qq)

            # ①b a/d（唤醒/掌控）：一期只记录（置信加权 EMA + 独立半衰期），
            #     **不进 v 的 z/Δ**（emotion §1.5）。低置信判读不会大幅移动它。
            try:
                if res.get("arousal") is not None or res.get("dominance") is not None:
                    mood.update_ad(key, res.get("arousal"), res.get("dominance"),
                                   confidence=res.get("confidence", 0.6))
            except Exception as e:
                logger.debug(f"[MOOD-JUDGE] 写 a/d 失败: {type(e).__name__}: {e}")

            # ①c 结构化心情事件（P4）：给「上次为什么对老大心情变好」这类查询用
            # 2026-11-25：dv 记**实际位移**（以前记名义步长，事后审计会误判幅度）；
            # 同时被 relevance 闸门挡住时不写。
            if not _skip_update:
                try:
                    from . import memdb as _mdb
                    # 2026-11-26：以前没传 `impact` → 库里全是 0，
                    # 「今日事件按档位分布」这种分析永远看不到 ±1/±2/±3。
                    _mdb.log_mood_event(
                        session=key,
                        about_qq=(_tgt_qq if _target in ("speaker", "recipient", "other")
                                  else ""),
                        target=_target, impact=res.get("impact"), dv=_dv_actual,
                        confidence=_fget(res, "confidence", 0.6),
                        relevance=_relevance, reason=res.get("why") or "")
                except Exception as e:
                    logger.debug(f"[MOOD-JUDGE] 写 mood_events 失败: {type(e).__name__}: {e}")

            # ③ 记忆刻痕（Δ=0 就不刻；relevance 闸门挡住也不刻）
            if not _skip_update and turn_id and abs(_delta) > 1e-9:
                try:
                    from . import memory_v2
                    memory_v2.patch_turn(
                        turn_id, delta=_delta, salience=_sal, valence=_val,
                        mood_word=res["word"], mood_value=_new_v,
                        about_qq=_tgt_qq,
                        mind=res.get("mind") or "")
                except Exception as e:
                    logger.debug(f"[MOOD-JUDGE] patch_turn 失败: {type(e).__name__}: {e}")

            # ④ 好感涨落（emotion §3.2）：**双向**，涨和跌都要能发生，且跌不比涨轻。
            #    |Δaff| = |Δv| × k(warmth) × trust_speed_factor × novelty × saturation
            #    负向再乘 neg_weight(当前好感)：越喜欢越宽容，越讨厌越敏感。
            #    2026-11-25 修正：旧版夹成 [-0.06, +0.08]（跌得比涨少 25%），
            #    neg_weight 还误传了「心情值」而不是「好感」，等于惩罚在惩罚
            #    —— 现在改成对称 ±0.10，并按设计文档补齐 novelty / saturation。
            _aff = 0.0
            _aff_qq = _tgt_qq if _target in ("speaker", "recipient", "other") else ""
            _pf = None
            _is_v2 = False
            try:
                from . import profiles as _pf
                _is_v2 = (getattr(_pf, "AFF_MODEL", "v1") == "v2")
            except Exception:
                _pf = None
            # novelty：同一个人 30min 内第 n 次（防「连着夸 / 连着骂」刷分），v1/v2 共用
            _nov = 1.0
            if _aff_qq and _pf is not None:
                try:
                    _n30 = sum(
                        1 for x in (_pf.get_profile(_aff_qq).get("affinity_log") or [])
                        if float(x.get("at") or 0) >= time.time() - 1800)
                except Exception:
                    _n30 = 0
                _nov = 1.0 / (1.0 + 0.6 * _n30)

            if _is_v2 and _pf is not None:
                # v2（2026-11-26）：只看 target（不再要求 cause==speaker），
                # 吃 mood.observe 的实际位移 dv，由 profiles 做痕迹求和/三段式/巩固。
                if (not _skip_update and _aff_qq
                        and abs(_dv_actual) >= 0.005):
                    try:
                        _new_aff = _pf.add_affinity_event(
                            _aff_qq, _dv_actual,
                            reason=res.get("word") or "",
                            conf=_fget(res, "confidence", 0.6),
                            novelty=_nov)
                        logger.info(
                            f"[MOOD-JUDGE] {_aff_qq} 好感v2 dv={_dv_actual:+.3f}"
                            f"（impact {int(res.get('impact') or 0):+d}"
                            f"/{_cause}/target={_target}）→ {_new_aff:+.2f}")
                    except Exception as e:
                        logger.debug(f"[MOOD-JUDGE] 写好感v2失败: {type(e).__name__}: {e}")
            else:
                # v1（默认，2026-11-26 用户拍板）：**同时吃 `_delta` 和 `dv`**。
                #   · `_delta` 项 = 这件事有多重（档位 → 名义步长），系数 AFF_V1_DELTA_K=0.25；
                #   · `dv` 项    = 她真的动了多少（`observe` 走出来的实际位移），
                #                  走三段式，`|dv|≥0.30` 的尾部 = 一见钟情 / 心碎，能一次跳 0.36。
                #   两项相加后乘人设/新鲜度/饱和 → 日常更慢，但突变性事件不会消失。
                if (not _skip_update and _aff_qq and _cause == "speaker"
                        and (abs(_delta) >= 0.005 or abs(_dv_actual) >= 0.005)
                        and _pf is not None):
                    _cur_aff = float(_pf.get_affinity(_aff_qq))
                    _gain = 0.04 + 0.12 * _fget(_tr, "warmth", 0.5)
                    _tsf = 0.6 + 0.8 * _fget(_tr, "trust_speed", 0.5)
                    # 置信度加权（P0-8）：判读没把握时，事件项不该按满格走。
                    # 用观测的 K（已含 confidence 与 P），没有观测就按 1.0。
                    _k_obs = float((_obs or {}).get("K") or 0.0) if _obs else 1.0
                    _k_eff = max(0.05, min(1.0, 0.5 + 0.5 * max(0.0, min(1.0, _k_obs))))
                    _aff = _pf.affinity_v1_step(
                        _delta, _dv_actual, _cur_aff,
                        gain=_gain, trust_speed=_tsf, k_eff=_k_eff, novelty=_nov)
                    if repair and _aff < 0:
                        # 道歉是修复（§3.6 grudge×0.5）：把这次伤害减半，不强行变成加分
                        _aff *= 0.5
                # 2026-11-26：门槛从 0.005 降到 0.001。
                # ADR-043 把事件项压到 0.25 倍后，一次 ±2 只有 ≈0.004，
                # 还按 0.005 卡就会「什么都没发生」——测试里 `好感能降/能涨` 两条就是这么红的。
                if _aff_qq and abs(_aff) >= 0.001 and _pf is not None:
                    try:
                        _new_aff = _pf.bump_affinity(
                            _aff_qq, _aff, reason=res.get("word") or "")
                        logger.info(f"[MOOD-JUDGE] {_aff_qq} 好感v1 {_aff:+.3f}"
                                    f"（Δ{_delta:+.2f}/dv{_dv_actual:+.3f}"
                                    f"/{_cause}/target={_target}）"
                                    f" → {_new_aff:+.2f}")
                    except Exception as e:
                        logger.debug(f"[MOOD-JUDGE] 写好感失败: {type(e).__name__}: {e}")
            # ⑤ 好感名词：让模型参考档案给一个描述性说法（v1/v2 共用）
            if _aff_qq and not _skip_update and _pf is not None:
                try:
                    _bw = str(res.get("bond_word") or "").strip()[:16]
                    if _bw:
                        _pf.set_bond_word(_aff_qq, _bw)
                except Exception:
                    pass

            # ④b trust / respect（P3）：只由「关系事件」改，普通闲聊不动。
            # 2026-11-25（P0-6）：记到 **target 那个人**头上，不是永远记说话人
            # （「A 对 B 失信」应该改 B 对 A 的信任？不 —— 是改**她对 A** 的信任，
            # 所以 target=recipient 时记 B；target=other 时记第三人）。
            _rel_qq = (_tgt_qq if _target in ("speaker", "recipient", "other")
                       else "")
            if _rel_qq and not _skip_update:
                try:
                    from . import relationship
                    _rel_note = relationship.note_judge(
                        _rel_qq, res, delta=_delta,
                        relevance=_fget(res, "relation_relevance", 0.5))
                    if _rel_note:
                        logger.info(f"[REL] {_rel_qq} {_rel_note}")
                except Exception as e:
                    logger.debug(f"[MOOD-JUDGE] 写关系失败: {type(e).__name__}: {e}")
        except Exception as e:
            logger.debug(f"[MOOD-JUDGE] 写回失败: {type(e).__name__}: {e}")
        _LAST[key] = res
        _TRACE[key] = {"at": time.time(), "ok": True,
                       "word": res["word"], "delta": res.get("delta"),
                       "impact": res.get("impact"), "P": res.get("P"),
                       "v": res.get("v_after"),
                       "why": res.get("why") or "", "n": len(ctx)}
        try:
            _now_v = float((_LAST.get(key) or {}).get("value") or 0.0)
        except Exception:
            _now_v = 0.0
        _anc = float(res.get("anchored") or 0.0)
        logger.info(f"[MOOD-JUDGE] {key} 「{res['src'][:24]}」→ {res['word']}"
                    f"（impact {int(res.get('impact') or 0):+d}"
                    f" conf {float(res.get('confidence') or 0):.2f}"
                    f" → v {float(res.get('v_after', 0)):+.3f}"
                    f"（实际Δ {float(res.get('dv_actual') or 0):+.3f}）"
                    f" P {float(res.get('P') or 0):.3f}"
                    + (f" 锚点{_anc:+.3f}" if _anc else "")
                    + f"）{res.get('why') or ''}")
    else:
        # 失败也要在日志里看得见（用户 2026-11-19：只打 debug 等于没打）
        _skip(key, f"判读失败：{res.get('err')}")
    return res


def last(session_key: str = "") -> dict:
    return dict(_LAST.get(str(session_key or "")) or {})


# ---------------- 静默锚点定时器（2026-11-25，P0-4）----------------
# 以前「静默 30min 锚点」只在**下一条消息带来 THINK** 时顺便判一次 idle；
# 群彻底安静 → 永远不纠偏，状态冻在旧值。这里给一个独立入口：
# 定时扫一遍会话，对「久未判读、但有她心理活动」的会话补一次绝对锚点。
ANCHOR_TIMER = tuning.i("MOOD_ANCHOR_TIMER")
_ANCHOR_LAST: dict = {}


async def anchor_tick() -> int:
    """对静默超时的会话补一次绝对锚点；返回实际锚了几个。"""
    if not int(ANCHOR_TIMER or 0):
        return 0
    try:
        from . import mood
        st = mood._load()          # 同包内读状态，只读不改
    except Exception:
        return 0
    now = time.time()
    idle_sec = float(mood.ANCHOR_IDLE_MIN) * 60
    _think_age = float(tuning.i("MOOD_ANCHOR_THINK_AGE_SEC"))
    done = 0
    for key, rec in list((st.get("sessions") or {}).items()):
        # 2026-11-26（2026-11-26 审计修复）：私聊也要锚。以前只扫 group_，
        # 私聊会话的锚点永远不触发。
        if not (str(key).startswith("group_")
                or str(key).startswith("private_")):
            continue
        try:
            last_at = float(rec.get("anchor_at") or rec.get("updated_at") or 0)
        except Exception:
            last_at = 0.0
        if now - last_at < idle_sec:
            continue
        if now - float(_ANCHOR_LAST.get(key) or 0) < idle_sec:
            continue
        # ADR-038：没有她的心理活动就不锚（不能拿群聊猜）。
        # 2026-11-26：还要判**时效**——以前 recent_think 不判时间，
        # 只要群里历史上写过一条 THINK，之后每 30 分钟都拿这条陈旧
        # 心里话锚一次，永远。
        _items = recent_think_items(key, 2)
        think = "；".join(
            str(x.get("text") or "")
            for x in _items
            if now - float(x.get("at") or 0) <= _think_age
            and str(x.get("text") or "").strip())
        if not think:
            continue
        # 2026-11-26：后台车道也要吃判读预算（以前直接调 judge，
        # 绕过 _calls_left，§3.3 的「≤30 次/小时」对它完全无效）。
        if _calls_left(key, now) <= 0:
            continue
        _CALLS.setdefault(key, []).append(now)
        try:
            res = await judge(
                "", scene_lines(key, 4), think=think,
                hint="静默锚点：她这段时间没说话，估一下此刻的绝对心情档位",
                session_key=key)
        except Exception as e:
            logger.debug(f"[ANCHOR] {key} 判读失败: {type(e).__name__}: {e}")
            continue
        if not res.get("ok") or res.get("anchor_level") is None:
            continue
        _base = 0.0
        try:
            from . import persona_traits as _pt
            _base = float(_pt.traits_for_session(key).get("baseline_v") or 0.0)
        except Exception:
            _base = 0.0
        try:
            move = mood.anchor(key, int(res["anchor_level"]),
                               _fget(res, "confidence", 0.6), _base)
        except Exception as e:
            logger.debug(f"[ANCHOR] {key} 写锚点失败: {type(e).__name__}: {e}")
            continue
        _ANCHOR_LAST[key] = now
        if abs(float(move or 0.0)) > 1e-6:
            done += 1
            logger.info(f"[ANCHOR] {key} 静默锚点 level={int(res['anchor_level']):+d}"
                        f" → {float(move):+.3f}"
                        f"（conf {float(res.get('confidence') or 0):.2f}）")
    return done


def status_text() -> str:
    return (f"心情判读：{'开' if ENABLED else '关'}｜model={MODEL or '?'}"
            f"｜窗口 {CTX_LINES} 条｜超时 {TIMEOUT:.0f}s｜节流 {MIN_INTERVAL:.0f}s")
