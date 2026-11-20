# services/referee.py
"""
三方差分（referee）：我们的回答 vs GLM 参考1 vs 群里那只鱼 参考2。

为什么是三方：
  只用鱼 → 鱼不常在场，样本稀；而且鱼也可能瞎编。
  只用 GLM → 模型换了一个，但没人给它「现场信息」。
  两个都上 → 两个独立来源都说 A 我们说 B，那 B 大概率错了。以参考为准。

两个入口（对应两种场景）：

  场景① 我们被触发（正常回复）
      用户 @ 我们 → 我们答 → 同时让 GLM 拿**同一份模板**答一遍（参考1）
                 → 若那只鱼也在 PEER_WAIT 秒内答了（参考2）就一起比
                 → diff → 有差异写进记忆（kind="golden"）

  场景② 鱼被触发（我们没被 @，本来不会答）
      用户 @ 了那只鱼 → 记下问题 + 最近 10 条上下文
      鱼答了 → **用我们这一套跑一遍影子回答**（build_prompt + call_llm，
               但绝不发到群里）+ GLM 参考1 → 三方 diff
      这样它的每一次对话都不浪费，即使我们当时没被叫。

场景③ 两条鱼在对话
      只记录到 data/peer_chat.jsonl，**不做对错核对**
      （两边都是 bot，谁也不是标准答案；但要留档，也允许偶尔互相接两句）
"""
from core import tuning
import asyncio
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

REFEREE_ENABLED = os.getenv("REFEREE_ENABLED", "true").lower() == "true"
PEER_GROUPS = {g.strip() for g in os.getenv("PEER_GROUPS", "822659531").split(",") if g.strip()}
PEER_QQS = {q.strip() for q in os.getenv("PEER_QQS", "2267107632").split(",") if q.strip()}

PEER_WAIT = tuning.f("PEER_WAIT")          # 我们答完等鱼多久（秒）
PEER_WINDOW = tuning.f("PEER_WINDOW")     # 鱼被 @ 后多久内回复算有效
REFEREE_MIN_CHARS = tuning.i("REFEREE_MIN_CHARS")   # 太短的回复不值得核对
REFEREE_MAX_PER_HOUR = tuning.i("REFEREE_MAX_PER_HOUR")
CTX_LINES = tuning.i("REFEREE_CTX_LINES")    # 影子回答带几条上下文
KEEP_SAMPLES = 6

# 那只鱼一次触发经常连发 3 条（「再让网页版那版…」/「就是一层扣一层…」/…），
# 逐条核对的话第一条被拿去比，后两条直接丢掉，样本就废了。
# 所以攒一下：静默 PEER_MERGE_WINDOW 秒后再合并成一条处理，最长不超过 PEER_MERGE_MAX。
PEER_MERGE_WINDOW = tuning.f("PEER_MERGE_WINDOW")
PEER_MERGE_MAX = tuning.f("PEER_MERGE_MAX")

# ---- 事前参考（preflight）----
# 用户要的是「GLM 的回答给 DeepSeek 做最终决策」，不是事后比对：
#   我们回复**之前**先让 GLM 拿同一份模板答一遍 → 塞进 DeepSeek 的 prompt →
#   DeepSeek 看到两个版本，自己决定采信哪些名词。
# 代价：每轮多 1~3 秒（GLM 调用）。短寒暄跳过，别让「哈哈」也等 3 秒。
PREFLIGHT_ENABLED = os.getenv("REFEREE_PREFLIGHT", "true").lower() == "true"
PREFLIGHT_MIN_CHARS = tuning.i("PREFLIGHT_MIN_CHARS")
# ⚠️ preflight 是串行挡在回复前面的，GLM 慢就直接拖慢聊天。
#    超时就不等了，用没有参考的版本照常回复（宁可少一层参考，别让人等）。
PREFLIGHT_TIMEOUT = tuning.f("PREFLIGHT_TIMEOUT")
_preflight: dict = {}      # session_key -> {ts, ref, question}  给事后 diff 复用

SAMPLE_LOG = Path("data/referee_samples.jsonl")   # 全量样本（以后调 prompt / 做数据集）
PEER_CHAT_LOG = Path("data/peer_chat.jsonl")      # 两条鱼自己的对话

_our_samples: dict = {}      # gid -> [sample]   我们已答、等参考
_peer_questions: dict = {}   # gid -> {ts, question, context, qq, name}
_peer_buf: dict = {}         # gid -> {parts, first_ts, task, to_us}  鱼的连发缓冲
_hour_window: list = []


# ================== 影子回答注入（避免和 chat 插件循环 import）==================
_SHADOW_FN = None


def set_shadow_fn(fn) -> None:
    """
    chat 插件启动时注册：
        async (question, context, speaker_qq, speaker_name, gid) -> (reply, prompt_text)
    """
    global _SHADOW_FN
    _SHADOW_FN = fn


# ================== 限流 ==================
def _allow() -> bool:
    now = time.time()
    while _hour_window and now - _hour_window[0] > 3600:
        _hour_window.pop(0)
    if len(_hour_window) >= REFEREE_MAX_PER_HOUR:
        logger.info(f"[REF] 本小时核对已达上限 {REFEREE_MAX_PER_HOUR} 次，跳过")
        return False
    _hour_window.append(now)
    return True


def _prune(gid: str) -> None:
    now = time.time()
    lst = [s for s in _our_samples.get(gid, [])
           if not s.get("done") and now - s["ts"] <= PEER_WAIT + 30]
    _our_samples[gid] = lst[-KEEP_SAMPLES:]
    q = _peer_questions.get(gid)
    if q and now - q["ts"] > PEER_WINDOW:
        _peer_questions.pop(gid, None)


# ================== 事前参考（preflight）==================
async def preflight(key: str, question: str, prompt_text: str, persona: str = "") -> str:
    """
    我们回复**之前**，先让 GLM 拿同一份模板答一遍，返回「参考1」。

    这一份会被 chat 插件塞进 DeepSeek 的 prompt，所以最终决策权在 DeepSeek 手里
    —— 它看到两个版本，自己决定采信哪些名词，而不是我们事后硬判谁对。

    结果会暂存起来，事后 diff 直接复用，不重复调 GLM。
    """
    if not PREFLIGHT_ENABLED or not REFEREE_ENABLED:
        return ""
    q = str(question or "").strip()
    if len(q) < PREFLIGHT_MIN_CHARS:
        return ""                     # 短寒暄不值得多等 3 秒
    try:
        from . import glm
        if not glm.available():
            return ""
        ref = await glm.reply(persona, prompt_text, max_tokens=320)
        ref = str(ref or "").strip()
        if not ref:
            return ""
        _preflight[str(key)] = {"ts": time.time(), "ref": ref, "question": q}
        if len(_preflight) > 60:
            _preflight.clear()
        logger.info(f"[REF] preflight 参考1 已生成（{len(ref)} 字）")
        return ref
    except Exception as e:
        logger.warning(f"[REF] preflight 失败（忽略）: {type(e).__name__}: {e}")
        return ""


def take_preflight(key: str) -> str:
    """取走 preflight 生成的参考1（给事后 diff 用，5 分钟内有效）。"""
    rec = _preflight.pop(str(key), None)
    if not rec:
        return ""
    if time.time() - rec.get("ts", 0) > 300:
        return ""
    return str(rec.get("ref") or "")


# ================== 场景①：我们被触发 ==================
def note_our_reply(
    gid: str,
    question: str,
    ours: str,
    prompt_text: str = "",
    persona: str = "",
    ref1: str = "",
) -> None:
    """
    我们刚发完一轮 → 建一个样本，后台去要参考1（GLM）和参考2（鱼）。

    ref1 如果已经由 preflight 生成过就直接传进来，别重复调 GLM。
    """
    gid = str(gid or "")
    ours = str(ours or "").strip()
    if not REFEREE_ENABLED or gid not in PEER_GROUPS:
        return
    if len(ours) < REFEREE_MIN_CHARS:
        return

    sample = {
        "ts": time.time(),
        "gid": gid,
        "scene": "ours",
        "question": str(question or "")[:400],
        "ours": ours[:800],
        "ours_source": "real",
        "prompt_text": str(prompt_text or "")[:6000],
        "persona": str(persona or "")[:4000],
        "ref1": str(ref1 or "")[:800],
        "ref2": "",
        "done": False,
    }
    _prune(gid)
    _our_samples.setdefault(gid, []).append(sample)
    # 我们自己已经答了 → 场景②那条待核对的问题作废，别重复比一次
    _peer_questions.pop(gid, None)
    asyncio.create_task(_finish_our(gid, sample))


async def _finish_our(gid: str, sample: dict) -> None:
    """后台：先要 GLM 参考1（preflight 已经给过就跳过），再等鱼，最后一次性 diff。"""
    try:
        if not sample.get("ref1"):
            from . import glm
            if glm.available() and sample.get("prompt_text"):
                sample["ref1"] = await glm.reply(sample["persona"], sample["prompt_text"])

        deadline = time.time() + PEER_WAIT
        while time.time() < deadline and not sample.get("ref2"):
            await asyncio.sleep(3)

        sample["done"] = True
        if sample.get("ref2"):
            logger.info(f"[REF] 场景① 拿到鱼参考2（{len(sample['ref2'])} 字）")
        await _diff_and_record(sample)
    except Exception as e:
        sample["done"] = True
        logger.warning(f"[REF] 场景① 核对失败: {type(e).__name__}: {e}")


# ================== 场景②：鱼被触发，我们没被 @ ==================
def note_peer_question(
    gid: str,
    question: str,
    context: list | None = None,
    speaker_qq: str = "",
    speaker_name: str = "",
) -> None:
    """有人 @ 了那只鱼 → 记下问题和上下文，等它答完好做影子核对。"""
    gid = str(gid or "")
    if not REFEREE_ENABLED or gid not in PEER_GROUPS:
        return
    q = str(question or "").strip()
    if not q:
        return
    ctx = [str(x)[:200] for x in (context or [])][-CTX_LINES:]
    _peer_questions[gid] = {
        "ts": time.time(),
        "question": q[:400],
        "context": ctx,
        "qq": str(speaker_qq or ""),
        "name": str(speaker_name or ""),
    }
    logger.info(f"[REF] 鱼被 @：「{q[:40]}」，已记下 {len(ctx)} 条上下文等它回答")


# ================== 场景③：两条鱼在对话（只留档）==================
def record_peer_chat(gid: str, speaker: str, text: str) -> None:
    try:
        PEER_CHAT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with PEER_CHAT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(
                {"ts": time.time(), "gid": str(gid), "who": str(speaker),
                 "text": str(text)[:600]},
                ensure_ascii=False) + "\n")
    except Exception as e:
        logger.debug(f"[REF] 写 peer_chat 失败: {e}")


# ================== 鱼开口（先攒再处理）==================
def queue_peer_reply(gid: str, text: str, talking_to_us: bool = False) -> None:
    """
    鱼说话了 → 先丢进缓冲，静默 PEER_MERGE_WINDOW 秒后再合并处理。

    为什么必须合并：实测它一次触发会连发 3 条
    （「再让网页版那版代入人设给回复打分纠错」/「就是一层扣一层 真跑起来得盯着」…），
    逐条处理的话只有第 1 条能配上问题，后两条全丢 —— 样本就只剩 1/3。
    """
    gid = str(gid or "")
    text = str(text or "").strip()
    if not REFEREE_ENABLED or gid not in PEER_GROUPS or not text:
        return

    now = time.time()
    buf = _peer_buf.get(gid)
    if not buf or (now - buf["first_ts"]) > PEER_MERGE_MAX:
        buf = {"parts": [], "first_ts": now, "task": None, "to_us": False}
        _peer_buf[gid] = buf

    buf["parts"].append(text)
    buf["to_us"] = bool(buf["to_us"] or talking_to_us)

    if buf.get("task") and not buf["task"].done():
        buf["task"].cancel()          # 又来了，重新计时
    buf["task"] = asyncio.create_task(_flush_peer(gid))


async def _flush_peer(gid: str) -> None:
    try:
        await asyncio.sleep(PEER_MERGE_WINDOW)
    except asyncio.CancelledError:
        return
    buf = _peer_buf.pop(str(gid), None)
    if not buf or not buf["parts"]:
        return
    merged = " ".join(buf["parts"]).strip()
    if len(buf["parts"]) > 1:
        logger.info(f"[REF] 鱼连发 {len(buf['parts'])} 条，已合并成 1 条（{len(merged)} 字）")
    await on_peer_reply(gid, merged, talking_to_us=buf["to_us"])


# ================== 鱼开口（合并后的真处理）==================
async def on_peer_reply(gid: str, peer_text: str, talking_to_us: bool = False) -> dict:
    """
    那只鱼说话了。三种可能：
      1) 它在跟我们说 → 只留档，不核对
      2) 我们刚回过（场景①）→ 补上参考2
      3) 它被 @ 了才回的（场景②）→ 跑影子回答 + GLM → 三方 diff
    """
    gid = str(gid or "")
    peer_text = str(peer_text or "").strip()
    if not REFEREE_ENABLED or gid not in PEER_GROUPS or not peer_text:
        return {}

    if talking_to_us:
        record_peer_chat(gid, "peer", peer_text)
        logger.info("[REF] 两条鱼在对话 → 只留档，不做对错核对")
        return {}

    _prune(gid)

    # ② 先看有没有我们在等参考2 的样本
    for s in reversed(_our_samples.get(gid, [])):
        if not s.get("done") and not s.get("ref2"):
            s["ref2"] = peer_text[:800]
            logger.info("[REF] 场景① 配上鱼的参考2")
            return s

    # ③ 看它是不是在回某个「被 @ 的问题」
    q = _peer_questions.pop(gid, None)
    if not q:
        logger.info("[REF] 收到鱼的话，但没有待核对的问题 → 忽略")
        return {}

    sample = {
        "ts": time.time(),
        "gid": gid,
        "scene": "peer",
        "question": q["question"],
        "context": q["context"],
        "ours": "",
        "ours_source": "shadow",
        "prompt_text": "",
        "persona": "",
        "ref1": "",
        "ref2": peer_text[:800],
        "done": True,
    }

    # 影子回答：用我们这一套答一遍，但绝不发到群里
    if _SHADOW_FN:
        try:
            ours, prompt_text = await _SHADOW_FN(
                q["question"], q["context"], q.get("qq", ""), q.get("name", ""), gid
            )
            sample["ours"] = str(ours or "").strip()[:800]
            sample["prompt_text"] = str(prompt_text or "")[:6000]
        except Exception as e:
            logger.warning(f"[REF] 影子回答失败: {type(e).__name__}: {e}")
    else:
        logger.info("[REF] 没注册影子回答函数，只能拿鱼和 GLM 比（缺少我方样本）")

    # 参考1：必须喂**和我们自己同一份**人设 + 模板，否则 diff 全是格式噪音
    try:
        from . import glm
        if glm.available() and sample["prompt_text"]:
            if not sample.get("persona"):
                try:
                    from .persona import persona_for
                    sample["persona"] = persona_for(f"shadow_{gid}")[1] or ""
                except Exception:
                    pass
            sample["ref1"] = await glm.reply(sample["persona"], sample["prompt_text"])
    except Exception as e:
        logger.debug(f"[REF] GLM 参考1 失败: {e}")

    await _diff_and_record(sample)
    return sample


# ================== 三方 diff ==================
_DIFF_PROMPT = """群里有个人问了一个问题，几个来源各自答了一遍。

【问题】
{question}
{context}

【我们的回答 A】
{ours}

{refs}

请只做一件事：找出参考里**比 A 更准确的名词性事实**。

**只核对名词**，具体是：
- 作品名、番剧名、角色名、人名、地名、组织名
- 网络梗、专有名词、术语
- 数字（年份、集数、数量、日期）

**明确不要管**（这些一律算「无差异」，写进来就是错的）：
- 语气、口吻、人格、性格、风格
- 句子长短、结构、排版、表情
- 该不该说、说得合不合适、像不像某个角色
- 主观评价、情感色彩

其他判断规则：
- 名词写法不同但指的是同一个东西（「咒术回战」vs「呪術廻戦」），算「无差异」
- A 的名词错了，就指出错在哪、正确写法是什么
- 参考自己也在瞎编（明显没把握）→ 算「无差异」，不要照抄
- 两个参考矛盾时，以 GLM 参考1 为准
- 拿不准就写「无差异」，宁缺毋滥

严格按四行输出，不要多余内容：
差异：有 / 无
纠正：A 的哪个名词错了、应该是什么（没有就写「无」）
补充：参考里有、A 漏掉的名词（没有就写「无」）
可信度：0-100 的整数"""


def _parse_diff(text: str) -> dict:
    out = {"changed": False, "fix": "", "add": "", "conf": 0}
    for line in str(text or "").splitlines():
        m = re.match(r"^(差异|纠正|补充|可信度)\s*[:：]\s*(.*)$", line.strip())
        if not m:
            continue
        k, v = m.group(1), m.group(2).strip()
        if k == "差异":
            out["changed"] = ("有" in v) and not v.startswith("无")
        elif k == "纠正":
            out["fix"] = "" if v in ("无", "没有", "-", "") else v[:140]
        elif k == "补充":
            out["add"] = "" if v in ("无", "没有", "-", "") else v[:140]
        elif k == "可信度":
            m2 = re.search(r"\d+", v)
            out["conf"] = int(m2.group(0)) if m2 else 0
    if out["fix"] or out["add"]:
        out["changed"] = True
    return out


async def _diff(sample: dict) -> dict:
    try:
        from .llm import XAI_API_KEY, XAI_BASE_URL, XAI_MODEL
    except Exception:
        return {}
    if not XAI_API_KEY:
        return {}

    refs = []
    if sample.get("ref1"):
        refs.append(f"【参考1（GLM）】\n{sample['ref1'][:800]}")
    if sample.get("ref2"):
        refs.append(f"【参考2（群里另一只 bot）】\n{sample['ref2'][:800]}")
    if not refs:
        return {}

    ctx = ""
    if sample.get("context"):
        ctx = "\n【当时的群聊上下文】\n" + "\n".join(sample["context"][-CTX_LINES:])

    prompt = _DIFF_PROMPT.format(
        question=sample.get("question") or "（没记录到原问题）",
        context=ctx,
        ours=(sample.get("ours") or "（我们当时没回答）")[:800],
        refs="\n\n".join(refs),
    )
    try:
        import httpx
        async with httpx.AsyncClient(timeout=35) as c:
            r = await c.post(
                f"{XAI_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {XAI_API_KEY}",
                         "Content-Type": "application/json"},
                json={"model": XAI_MODEL,
                      "messages": [{"role": "user", "content": prompt}],
                      "temperature": 0.1, "max_tokens": 240},
            )
        if r.status_code != 200:
            logger.warning(f"[REF] diff HTTP {r.status_code}")
            return {}
        _d = r.json()
        try:
            from . import llm_usage as _lu
            _lu.record_from_response(_d, purpose="audit")
        except Exception:
            pass
        return _parse_diff(str(_d["choices"][0]["message"].get("content") or ""))
    except Exception as e:
        logger.warning(f"[REF] diff 异常: {type(e).__name__}: {e}")
        return {}


async def _diff_and_record(sample: dict) -> dict:
    if not _allow():
        return {}
    if not (sample.get("ref1") or sample.get("ref2")):
        logger.info("[REF] 两个参考都没有，跳过核对")
        return {}

    d = await _diff(sample)
    if not d:
        return {}

    rec = {
        "ts": sample["ts"],
        "gid": sample["gid"],
        "scene": sample.get("scene"),
        "question": sample.get("question", "")[:400],
        "ours": sample.get("ours", "")[:800],
        "ours_source": sample.get("ours_source", ""),
        "ref1": sample.get("ref1", "")[:800],
        "ref2": sample.get("ref2", "")[:800],
        "has_ref1": bool(sample.get("ref1")),
        "has_ref2": bool(sample.get("ref2")),
        "changed": bool(d.get("changed")),
        "fix": d.get("fix", ""),
        "add": d.get("add", ""),
        "conf": d.get("conf", 0),
    }
    _append_log(rec)

    if not rec["changed"] or rec["conf"] < 55:
        logger.info(f"[REF] 无有效差异（conf={rec['conf']}）｜{rec['question'][:30]}")
        return rec

    bits = []
    if rec["fix"]:
        bits.append(f"我们答错了：{rec['fix']}")
    if rec["add"]:
        bits.append(f"我们漏了：{rec['add']}")
    src = "GLM" if rec["has_ref1"] and not rec["has_ref2"] else (
        "鱼" if rec["has_ref2"] and not rec["has_ref1"] else "GLM+鱼")
    text = f"关于「{rec['question'][:60]}」——（{src}核对）" + "；".join(bits)
    try:
        from . import memory_v2
        memory_v2.remember(text=text, kind="golden",
                           tags=["referee", f"gid:{rec['gid']}"], importance=0.85)
        logger.info(f"[REF] ✅ 已记入基准（{src}，conf={rec['conf']}）：{text[:70]}")
    except Exception as e:
        logger.warning(f"[REF] 写记忆失败: {type(e).__name__}: {e}")
    return rec


def _append_log(rec: dict) -> None:
    try:
        SAMPLE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with SAMPLE_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.debug(f"[REF] 写样本日志失败: {e}")


def stats() -> dict:
    """/记忆体检 用。"""
    total = changed = 0
    if SAMPLE_LOG.exists():
        try:
            for line in SAMPLE_LOG.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                total += 1
                if json.loads(line).get("changed"):
                    changed += 1
        except Exception:
            pass
    peer_chats = 0
    if PEER_CHAT_LOG.exists():
        try:
            peer_chats = sum(1 for x in PEER_CHAT_LOG.read_text(
                encoding="utf-8").splitlines() if x.strip())
        except Exception:
            pass
    return {
        "samples": total,
        "corrected": changed,
        "peer_chats": peer_chats,
        "waiting": sum(len(v) for v in _our_samples.values()),
    }
