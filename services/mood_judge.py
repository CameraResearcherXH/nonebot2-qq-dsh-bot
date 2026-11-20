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
MODEL = (os.getenv("MOOD_JUDGE_MODEL")
         or os.getenv("SCENE_AUDIT_MODEL") or os.getenv("XAI_MODEL") or "").strip()
TIMEOUT = tuning.f("MOOD_JUDGE_TIMEOUT")
CTX_LINES = tuning.i("MOOD_JUDGE_CTX")
MIN_INTERVAL = tuning.f("MOOD_JUDGE_MIN_INTERVAL")
# 心情变化 → 好感增幅的系数（2026-11-20 定稿：好感由 Δmood 推导，不再单独判读）
# 0.05 意味着「Δmood=1.0 的大事件」一次只涨 +0.05，从 0 到 +1.0 需要约 20 次。
AFFINITY_GAIN = tuning.f("AFFINITY_GAIN")
# 她没主动写心情时，多久用群聊重新判一次（秒）。0 = 只在她说的时候判
REFRESH = tuning.f("MOOD_JUDGE_REFRESH")
# 判读结果在「记忆写回」里有效多久（秒）：超过就退回用 MEMO 自报的情绪
FRESH_SEC = tuning.f("MOOD_JUDGE_FRESH_SEC")
# 她没写 [[THINK]] 时，后台补一句心里话（保证每条回复都有记录）
BACKFILL = os.getenv("MOOD_THINK_BACKFILL", "true").lower() in ("1", "true", "yes", "on")

_LAST_AT: dict = {}       # session_key -> 上次判读时间（节流）
_LAST: dict = {}          # session_key -> 最近一次结果（给 /测函数 用）
# 每次「尝试」的结果，成功失败都记 —— 用户 2026-11-19：
# 「我没有从测心情的 log 里看到有效信息」。以前失败只打 debug，真机上等于没有。
_TRACE: dict = {}         # session_key -> {"at", "ok", "word"/"err", ...}
_SKIP_AT: dict = {}       # session_key -> (原因, 上次打日志时间)，同一个原因 60s 内只打一次

# 她的「心理活动」（MEMO 里的 think:）—— 用户 2026-11-19：
# 「我看这些内容没有心里话啊，只有回复。」
# 以前 think 只在**生成回复那一刻**传给判读，判完就丢：重判 / /测函数 心情 /
# 事后复盘全都看不到，等于「心里话」这个设计只活了一瞬间。这里落盘。
THINK_FILE = Path(os.getenv("MOOD_THINK_FILE", "data/mood_think.json"))
THINK_KEEP = tuning.i("MOOD_THINK_KEEP")
_THINK: dict = {}


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


def _base() -> tuple:
    return ((os.getenv("XAI_BASE_URL") or "").rstrip("/"),
            (os.getenv("XAI_API_KEY") or "").strip())


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
            for m in group_scene.recent_messages(key[len("group_"):], n):
                who = "你" if m.get("is_bot") else str(
                    m.get("name") or m.get("qq") or "?")
                txt = str(m.get("text") or "").strip()
                if txt:
                    out.append(f"{who}: {_slim(txt)}"[:160])
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


def _clip_ctx(ctx_lines) -> list:
    out = []
    for x in (ctx_lines or [])[-CTX_LINES:]:
        s = re.sub(r"\s+", " ", str(x or "")).strip()
        if s:
            out.append(s[:160])
    return out


def _build_prompt(words: str, think: str, ctx: list, prev_word: str = "",
                  prev_value: float = 0.0, hint: str = "",
                  repair: bool = False, archive: str = "") -> str:
    lines = [
        "你是「心情判读员」，不聊天、不解释，只做标注。",
        "给你一个 AI 角色（她）**自己写下的心里话**，以及她刚经历的最近群聊。",
        "你要判断两件事：她此刻的心情名词，和强度。",
        "",
        f"【她自己的话】{words or '（这次没写 —— 那就按下面的群聊判断她此刻的状态）'}",
    ]
    if hint:
        lines.append(f"【刚刚发生】{str(hint)[:160]}")
    if think:
        lines.append(f"【她的心理活动】{think[:200]}")
    if archive:
        lines.append(f"【她对正在说话这个人的档案】{archive[:220]}")
    if prev_word or prev_value:
        lines.append(f"【上一轮】{prev_word or '?'}（当前心情 {prev_value:+.2f}）"
                     "—— 只作参考，情绪可以变，别硬往回拉")
    if ctx:
        lines.append("")
        lines.append(f"【最近 {len(ctx)} 条群聊（最后一条最新）】")
        lines.extend(f"{i}. {x}" for i, x in enumerate(ctx, 1))
    lines += [
        "",
        "判断规则（mind / word / delta / cause / why 五个字段都要有，"
        "档案够详细时再加 bond_word）：",
        "· mind：**第一人称**的一句心里话，15 字内，像她在心里嘀咕。",
        "  不是事件复述，也不是说给别人听的客套话。例：「他夸我，我嘴上不认」「这数字看得我眼晕」。",
        "  哪怕她很平静，也要写一句（例：「就随便看看」）。**这一项不能省**。",
        "· word：一个**具体**的中文词/短语，4~10 字。不要用「开心/难过/生气/平静」这种泛词；",
        "  要具体到她此刻的状态：「被夸得有点飘」「憋着一口气」「懒得解释」「心里空了一下」；",
        "  真的没什么起伏就写「没什么起伏」。",
        "· delta：**-1.00 ~ 1.00，保留两位小数**。这是「这件事让她心情**涨/跌了多少**」，",
        "  **不是她现在的心情值**（当前心情由系统累计，不用你给）。",
        "  正 = 变好（被夸、被逗笑、被哄）｜负 = 变差（被冒犯、被无视、被恶心）。",
        "· **幅度纪律（很重要，2026-11-20）**：",
        "  日常闲聊 / 玩笑 / 旁观吐槽 → **±0.05 ~ ±0.15**；",
        "  「抽象」「无语」「好笑」「嘴硬心痒」这类小情绪 → **±0.05 ~ ±0.10**；",
        "  被明显戳到 / 被冒犯 / 被认真夸 → **±0.20 ~ ±0.40**；",
        "  真正的伤害（羞辱、被赶）或大喜（久别重逢）→ **±0.50 以上**，很少见。",
        "  同一件小事不要反复给大值；平淡的闲聊就给 0.00~±0.05。",
        "· bond_word（可选）：参考【她的档案】，给一个 **4~10 字**的描述性说法，",
        "  说明「她现在怎么看他」，例：「又爱又嫌的老熟人」「不想搭理的陌生人」「可以损两句的熟人」。",
        "  档案没提到这个人、或这次没什么变化，就省略这个字段。",
        "· cause：这次心情变化是**谁/什么**引起的，只能填四个值之一：",
        "  speaker（正在说话这个人：夸/骂/逗/哄/无视）｜environment（群里的气氛、话题、别人）｜",
        "  self（她自己的事：想起以前、困了）｜other（群里另一个人，不是当前说话人）。",
        "  ⚠️ 只有 speaker 才算到「对他的好感」上；环境好笑不等于对他好感涨。",
        "· why：一句话说清「发生了什么事、她为什么这样」（30 字内，写成**事件**，"
        "不写空泛判断）。例：「他当众喊我外号，我有点下不来台」。",
        "· **以最后 2~3 条为主**（她此刻正在接的那句），更早的只当背景 ——",
        "  别把 20 条的情绪平均成一个数，那会变成「综合印象」而不是「此刻的心情」。",
        "· 依据权重：她自己的用词强度 > 群里实际发生了什么 > 上一轮判读。",
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
        '"delta":0.12,"cause":"speaker","why":"他夸了她，她装没事",'
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


def _clean_word(raw: str) -> str:
    w = re.sub(r"\s+", "", str(raw or ""))
    w = w.strip("「」『』“”\"'。，,、：:；;")
    return w[:12]


def _ctx_of(session_key: str, ctx_lines=None) -> list:
    """判读上下文：优先按条数从现场/日志拉 20 条，拉不到才用调用方给的。"""
    rows = scene_lines(session_key)
    if rows:
        return rows[-CTX_LINES:]
    out = [re.sub(r"\s+", " ", str(x or "")).strip()[:160]
           for x in (ctx_lines or []) if str(x or "").strip()]
    return out[-CTX_LINES:]


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
        "用**第一人称**写一句她此刻的心里话（15 字内，像心里嘀咕，不复述事件、不客套）。",
        "只输出这句话本身：不要引号、不要解释、不要换行。",
    ]
    payload = {
        "model": MODEL or "deepseek-chat",
        "messages": [
            {"role": "system", "content": "你只输出一句中文，不加引号。"},
            {"role": "user", "content": "\n".join(lines)},
        ],
        "temperature": 0.4,
        "max_tokens": 60,
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
                _lu.record_from_response(data, purpose="judge")
            except Exception:
                pass
        txt = str(((((data or {}).get("choices") or [{}])[0]
                    .get("message") or {}).get("content")) or "")
        txt = re.sub(r"\s+", "", txt).strip("「」『』“”\"'。，,")
        return txt[:60]
    except Exception as e:
        logger.debug(f"[MOOD-JUDGE] 补问心里话失败: {type(e).__name__}: {e}")
        return ""


async def backfill_think(session_key: str, *, src: str = "",
                         reply: str = "") -> str:
    """
    她没写 [[THINK]] 时补一句心里话（标记 auto=1）。

    用户 2026-11-20：「每句话都反馈心理活动」——不能全靠模型自觉，
    真机命中率只有 2/7。缺了就后台补一条：记录完整，成本只有一次
    max_tokens=60 的小调用，而且只在缺失时发生。
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
        note_think(key, t, src=src, reply=reply, auto=True)
        logger.info(f"[MOOD-JUDGE] {key} 这条没写 THINK，补了一句：{t[:30]}")
    return t


async def judge(words: str, ctx_lines=None, *, think: str = "",
                prev_word: str = "", prev_value: float = 0.0,
                hint: str = "", repair: bool = False,
                archive: str = "") -> dict:
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
                           hint=hint, repair=repair, archive=archive)
    payload = {
        "model": MODEL or "deepseek-chat",
        "messages": [
            {"role": "system", "content": "你只输出 JSON，不聊天。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 200,
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
                _lu.record_from_response(data, purpose="judge")
            except Exception:
                pass
        content = str((((data or {}).get("choices") or [{}])[0]
                       .get("message") or {}).get("content") or "")
    except Exception as e:
        return {"ok": False, "err": f"{type(e).__name__}: {e}"}

    d = _parse_json(content)
    word = _clean_word(d.get("word"))
    # 2026-11-20 定稿：判读报的是**涨幅 Δ**（这件事让她涨/跌多少），不是当前心情值。
    # 兼容老模型只给 value 的情况：Δ = value - 上一轮 value。
    _raw_delta = d.get("delta", d.get("Δ"))
    try:
        if _raw_delta is not None:
            delta = round(max(-1.0, min(1.0, float(_raw_delta))), 2)
        else:
            delta = round(max(-1.0, min(1.0, float(d.get("value"))))
                          - float(prev_value or 0.0), 2)
    except Exception:
        return {"ok": False,
                "err": f"delta 不是数字：{content[:120]}", "raw": content[:400]}
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
    return {"ok": True, "word": word, "delta": delta,
            "bond_word": str(d.get("bond_word") or "").strip()[:16],
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
                         f"（心情值 {float(t.get('value') or 0):+.2f}）")
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
        n = len(scene_lines(key))
    except Exception:
        n = 0
    lines.append(f"· 现在能拉到上下文：{n} 条（上限 {CTX_LINES}）")
    return "\n".join(lines)


async def judge_and_store(session_key: str, words: str, ctx_lines=None, *,
                          think: str = "", force: bool = False,
                          speaker_qq: str = "", hint: str = "",
                          repair: bool = False, turn_id: str = "") -> dict:
    """
    判读 + 写进 mood（影子字段，不影响老数值）。

    三层闸门：
      1. 同一会话 MIN_INTERVAL 内只判一次（她连发几句不用每句都调）；
      2. 她**没主动写心情**时，只在「上次判读超过 REFRESH 秒」或还没有结果时才判
         （否则每轮都调一次模型，白花钱）；
      3. 上下文按**条数**拉最近 CTX_LINES 条（现场优先，退回落盘日志）。
    """
    key = str(session_key or "")
    if not ENABLED:
        return _skip(key, "总开关关着（MOOD_JUDGE_ENABLED=false）")
    now = time.time()
    if not force and now - float(_LAST_AT.get(key) or 0) < MIN_INTERVAL:
        left = MIN_INTERVAL - (now - float(_LAST_AT.get(key) or 0))
        return _skip(key, f"节流：还要 {left:.0f}s（MIN_INTERVAL={MIN_INTERVAL:.0f}s）")

    prev = {}
    try:
        from . import mood
        prev = mood.get_judge(key) or {}
    except Exception:
        prev = {}

    ctx = _ctx_of(key, ctx_lines)
    # 本次她刚写的心理活动（决定「算不算她主动写了」）
    _incoming = str(think or "").strip()
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
            return _skip(key, f"她没写心情，{int(REFRESH - _age)}s 后才按群聊重判")
    if not (wrote or ctx):
        return _skip(key, "她没写、现场和日志也拉不到消息")

    _LAST_AT[key] = now
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
    res = await judge(words, ctx, think=use_think,
                      prev_word=str(prev.get("word") or ""),
                      prev_value=float(prev.get("value") or 0.0),
                      hint=hint, repair=repair, archive=_archive)
    if res.get("ok"):
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
            _sal = min(1.0, abs(_delta) / 0.5)
            _val = 1.0 if _delta > 0 else (-1.0 if _delta < 0 else 0.0)
            _cause = str(res.get("cause") or "speaker")

            # ⓪ 群心情（独立于整体心情）：这个群最近 5 小时让她涨/跌了多少。
            #    · cause=speaker → 记到这个人头上（单人跌 0.4 就单独不理他）
            #    · cause=other/environment → 只算群环境（3 人以上 + 群跌 0.7 才整群安静）
            #    · cause=self → 她自己的事，不算群环境
            if key.startswith("group_"):
                try:
                    from . import group_mood
                    group_mood.record(
                        key, _delta,
                        about_qq=(speaker_qq if _cause == "speaker" else ""),
                        cause=_cause, why=res.get("why") or "",
                        mind=res.get("mind") or "",
                        word=res.get("word") or "")
                except Exception as e:
                    logger.debug(f"[MOOD-JUDGE] 写群心情失败: {type(e).__name__}: {e}")

            # ① 心情值：累加 Δ（MOOD_MODE=level 时退回旧的覆盖式，便于回滚）
            _prev_v = float(prev.get("value") or 0.0)
            _new_v = _prev_v
            try:
                if getattr(mood, "MOOD_MODE", "delta") == "level":
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
                logger.debug(f"[MOOD-JUDGE] 累加心情失败: {type(e).__name__}: {e}")

            # ② 判读记录：word = 这件事的**反应词**；value = 累加后的新水平；
            #    delta = 这一次的涨幅（记忆/好感/档案都读它）
            mood.set_judge(key, res["word"], _new_v,
                           why=res.get("why") or "", src=res.get("src") or "",
                           mind=res.get("mind") or "", delta=_delta)

            # ③ 记忆刻痕
            if turn_id:
                try:
                    from . import memory_v2
                    memory_v2.patch_turn(
                        turn_id, delta=_delta, salience=_sal, valence=_val,
                        mood_word=res["word"], mood_value=_new_v,
                        mind=res.get("mind") or "")
                except Exception as e:
                    logger.debug(f"[MOOD-JUDGE] patch_turn 失败: {type(e).__name__}: {e}")

            # ④ 好感涨落
            _aff = 0.0
            if _cause == "speaker" and abs(_delta) >= 0.005:
                _aff = _delta * AFFINITY_GAIN
                _aff = max(-0.06, min(0.08, _aff))
                if repair:
                    _aff = max(_aff, 0.03)
            if speaker_qq and abs(_aff) >= 0.005:
                try:
                    from . import profiles
                    _new_aff = profiles.bump_affinity(
                        speaker_qq, _aff, reason=res.get("word") or "")
                    logger.info(f"[MOOD-JUDGE] {speaker_qq} 好感 {_aff:+.3f}"
                                f"（Δ{_delta:+.2f}/{_cause}） → {_new_aff:+.2f}")
                    # ⑤ 好感名词：让模型参考档案给一个描述性说法
                    try:
                        _bw = str(res.get("bond_word") or "").strip()[:16]
                        if _bw:
                            profiles.set_bond_word(speaker_qq, _bw)
                    except Exception:
                        pass
                except Exception as e:
                    logger.debug(f"[MOOD-JUDGE] 写好感失败: {type(e).__name__}: {e}")
        except Exception as e:
            logger.debug(f"[MOOD-JUDGE] 写回失败: {type(e).__name__}: {e}")
        _LAST[key] = res
        _TRACE[key] = {"at": time.time(), "ok": True,
                       "word": res["word"], "delta": res.get("delta"),
                       "why": res.get("why") or "", "n": len(ctx)}
        try:
            _now_v = float((_LAST.get(key) or {}).get("value") or 0.0)
        except Exception:
            _now_v = 0.0
        logger.info(f"[MOOD-JUDGE] {key} 「{res['src'][:24]}」→ {res['word']}"
                    f"（Δ{float(res.get('delta') or 0):+.2f}）{res.get('why') or ''}")
    else:
        # 失败也要在日志里看得见（用户 2026-11-19：只打 debug 等于没打）
        _skip(key, f"判读失败：{res.get('err')}")
    return res


def last(session_key: str = "") -> dict:
    return dict(_LAST.get(str(session_key or "")) or {})


def status_text() -> str:
    return (f"心情判读：{'开' if ENABLED else '关'}｜model={MODEL or '?'}"
            f"｜窗口 {CTX_LINES} 条｜超时 {TIMEOUT:.0f}s｜节流 {MIN_INTERVAL:.0f}s")
