# services/agent_client.py
"""
算力机 Agent 的统一客户端。

两条通道，按「延迟要求」分开：

  ① recall（每条消息都要用，必须快）
     → HTTP，走已有的 RAG 隧道 {AGENT_BASE_URL}
     → 优先 POST /api/agent/recall（结构化：facts/events/emotions/self_state）
     → 没有就退回 POST /api/agent/query（现有 RAG，返回一段 answer，包成 facts）

  ② reflection（两天一次，慢一点无所谓）
     → 优先 HTTP POST /api/agent/reflection（如果算力机开了端口 + 隧道）
     → 否则退回「任务队列」：把 bundle 落成 json 文件，挂进 STATUS，
       node.py 通过 /api/tasks/pull 拉走，算完 /api/tasks/complete 回传。
       这条路不依赖任何反向隧道，纯拉取，一定能通。

设计原则：**任何一条通道挂了都不抛异常**，只降级。
      聊天链路永远不会因为算力机掉线而失败。
"""
from core import tuning
import asyncio
import json
import os
import re
import time
from pathlib import Path

import aiohttp
from nonebot import logger

# ⚠️ docker-compose 是 network_mode: "host" → 容器复用宿主机网络栈，
#    必须用 127.0.0.1。host.docker.internal 只在 bridge 模式下才有 DNS 记录，
#    在 host 模式下会报 "Name or service not known"（之前就是这么静默失败的）。
AGENT_BASE_URL = os.getenv("AGENT_BASE_URL", "http://127.0.0.1:8001").rstrip("/")

RECALL_ENDPOINT = os.getenv("AGENT_RECALL_ENDPOINT", "/api/agent/recall")
QUERY_ENDPOINT = os.getenv("AGENT_QUERY_ENDPOINT", "/api/agent/query")
REFLECT_ENDPOINT = os.getenv("AGENT_REFLECT_ENDPOINT", "/api/agent/reflection")
# 2026-11-24 实测：算力机那套是 Agentic RAG v3，路由是 /api/agent/query 和
# /api/agent/smart-query，**没有** /api/agent/recall、/api/agent/reflection。
# 所以默认按「结构化 → smart-query → query」三级降级，别再打不存在的端点。
SMART_QUERY_ENDPOINT = os.getenv("AGENT_SMART_QUERY_ENDPOINT", "/api/agent/smart-query")
# 2026-11-26 实测：`/api/agent/smart-query` 会卡 >70s（LLM 规划层），
# 但 `POST /api/agent/execute-tool?tool_name=rag_search` 1~9s 就返回、还带来源。
# 所以它现在是「第一兜底」，smart-query 退到第二。
RAG_TOOL_ENDPOINT = os.getenv(
    "AGENT_RAG_TOOL_ENDPOINT", "/api/agent/execute-tool?tool_name=rag_search")

# 知识库里如果根本不是聊天记录（实测是 poet.song.rank.* 宋词数据），
# 就别每条消息都去问一次（一次 6~13s，纯浪费）。6 小时内不再试。
_LIBRARY_DEAD_UNTIL = 0.0
_LIBRARY_WARNED = False
# /api/agent/recall 不存在时记一下，别每条消息都白打一次 404
_RECALL_ENDPOINT_MISSING = False

# ⚠️ 2026-11-24 实测：给 RAG 加「只列谁说过什么」这类指令，反而让它更容易回「没有」
# （它把这句当成要检索的内容，检索不到就放弃）。直接送原话，命中最稳、也最快。
# 2026-11-26：给 RAG 一点上下文。
# 以前 `_RECALL_PROMPT = "{q}"` —— 原样把一句话丢过去，RAG 不知道「上周」「他」指谁，
# 也不知道今天是几号，只能瞎猜（真机里「上周那件事」经常捞到去年的）。
_RECALL_PROMPT = (
    "今天是 {date}。{who}在问：{q}\n"
    "只回答归档里查得到的事实；查不到就说「没有」。"
    "人称用第三人称（她/他），别照抄原文里的你我他。"
)

RECALL_TIMEOUT = tuning.f("AGENT_RECALL_TIMEOUT")
# 图书馆延迟目标 / 候选池条数（retrieval §7）：超目标只告警，不阻断
LIB_LATENCY_MS = tuning.f("RET_LIB_LATENCY_MS")
WORLD_TOP = max(1, tuning.i("RET_WORLD_TOP"))
# 图书馆（RAG v3）实测一次 5~14s：2.5s 超时 = 每条消息都白等然后判「不可用」。
# 2026-11-24 用户实测「小明是如何解决温室效应的」→ 13.4s 才返回弱智吧内容。
LIB_TIMEOUT = tuning.f("AGENT_LIBRARY_TIMEOUT")
RAG_TOOL_TIMEOUT = tuning.f("AGENT_RAG_TOOL_TIMEOUT")
REFLECT_HTTP_TIMEOUT = tuning.f("AGENT_REFLECT_TIMEOUT")
REFLECT_QUEUE_TIMEOUT = tuning.f("AGENT_REFLECT_QUEUE_TIMEOUT")

QUEUE_DIR = Path("data/temp_files")
QUEUE_DIR.mkdir(parents=True, exist_ok=True)

EMPTY_RECALL = {"facts": [], "events": [], "emotions": [], "self_state": "", "source": "none"}

# 图书馆调用统计（给 `/测函数 记忆` 用）
STATS = {"calls": 0, "ok": 0, "empty": 0}

# 同一条/相似的问题 10 分钟内不重复问 RAG（一次 5~14s，太贵）。
# key = 去掉空白的 query 前 60 字；空结果只缓存 2 分钟，避免把「暂时没命中」钉死。
_CACHE: dict = {}
_CACHE_TTL_OK = 600.0
_CACHE_TTL_EMPTY = 120.0


def _cache_key(q: str) -> str:
    return re.sub(r"\s+", "", str(q or "").lower())[:60]


def stats_text() -> str:
    left = max(0.0, _LIBRARY_DEAD_UNTIL - time.time()) / 3600
    tail = f"｜已暂停 {left:.1f}h（语料不匹配）" if left > 0 else ""
    return (f"· 图书馆：调用 {STATS['calls']} 次｜有结果 {STATS['ok']} 次｜"
            f"空 {STATS['empty']} 次{tail}")


def empty_recall() -> dict:
    """每次返回全新对象，避免调用方改到共享的模板。"""
    return {"facts": [], "events": [], "emotions": [], "self_state": "", "source": "none"}


# ================= 基础 HTTP =================
# 同一个地址只警告一次，避免每轮对话都刷屏
_AGENT_WARNED = set()


def _warn_once(url: str, msg: str):
    if url in _AGENT_WARNED:
        return
    _AGENT_WARNED.add(url)
    logger.warning(f"[AGENT] ⚠️ {msg}\n   → 地址：{url}")


async def _post_json(url: str, payload: dict, timeout: float) -> dict | None:
    """POST JSON，返回 dict；任何异常/非 200 都返回 None（不抛）。"""
    try:
        to = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=to) as session:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    # 404 说明端点不存在（正常降级路径），其余才值得提醒
                    if resp.status == 404:
                        logger.debug(f"[AGENT] {url} → 404（端点不存在，走降级）")
                    else:
                        _warn_once(url, f"{url} 返回 HTTP {resp.status}")
                    return None
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    return None
                return data if isinstance(data, dict) else None
    except Exception as e:
        # 从 debug 提升为 warning：之前算力机 Agent 一直静默失败，日志里完全看不出来
        _warn_once(
            url,
            f"{url} 不可达（{type(e).__name__}: {e}）—— 算力机检索/复盘会降级，"
            f"聊天不受影响",
        )
        return None


def _as_list(v) -> list:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v if str(x).strip()]
    if isinstance(v, str):
        return [v] if v.strip() else []
    return [str(v)]


def _normalize_recall(data: dict, source: str) -> dict:
    """把各种形状的返回统一成 {facts, events, emotions, self_state, source}。"""
    if not isinstance(data, dict):
        return empty_recall()
    # 有些实现会把结果包在 data/result 里
    for key in ("data", "result", "recall"):
        inner = data.get(key)
        if isinstance(inner, dict) and any(
            k in inner for k in ("facts", "events", "emotions", "self_state")
        ):
            data = inner
            break

    facts = _as_list(data.get("facts"))
    if not facts and data.get("answer"):
        ans = str(data.get("answer") or "").strip()
        # RAG 的 answer 往往是一整段 + 「**参考来源**」；按行拆开，
        # 去掉来源行，保留真正的结论行（否则 [:60] 会把结论截掉）。
        lines = []
        for ln in ans.splitlines():
            ln = ln.strip().strip("*#-· ").strip()
            if not ln or "参考来源" in ln or ln.startswith("来源"):
                continue
            lines.append(ln)
        facts = lines[:5] or ([ans] if ans else [])

    return {
        "facts": facts,
        "events": _as_list(data.get("events")),
        "emotions": _as_list(data.get("emotions")),
        "self_state": str(data.get("self_state") or "").strip(),
        "source": source,
    }


_NO_ANSWER_KEYS = (
    "没有找到", "没有提到", "没有提供", "没有关于", "没有相关信息",
    "无法回答", "无法作答", "无法根据", "问题不完整", "信息不足",
    "上下文中没有", "知识库中没有", "未找到", "无法确定",
)


def clean_facts(facts, limit: int = 0) -> list:
    """RAG 结果后处理：去重 + 丢掉「没有找到」这种空答案行 + 每条截断。

    为什么（2026-11-26）：RAG 经常把同一段归档切成 3~4 条返回，
    直接进 prompt 会占满世界块；「没有找到相关信息」也会被当成事实念出来。
    """
    out, seen = [], set()
    for f in (facts or []):
        s = str(f or "").strip().strip("*-· ")
        if not s or is_no_answer(s):
            continue
        k = re.sub(r"\s+", "", s)[:40]
        if k in seen:
            continue
        seen.add(k)
        out.append(s[:160])
        if limit and len(out) >= limit:
            break
    return out


def is_no_answer(text: str) -> bool:
    """RAG 的「没检索到」也算空结果：别把「没有」当事实塞进 prompt。"""
    a = str(text or "").strip()
    if not a:
        return True
    # 先砍掉「**参考来源**: …」尾巴，否则整段很长，短答案判定会失效
    a = re.split(r"\*{0,2}参考来源\s*[:：]?", a, maxsplit=1)[0]
    a = a.strip().strip("*#-· ").strip()
    if not a:
        return True
    if len(a) <= 8 and a.strip("。.!！?？ ") in (
            "没有", "无", "不知道", "未找到", "没有找到"):
        return True
    head = a[:80]
    return any(k in head for k in _NO_ANSWER_KEYS)


# 通用定义句：「白饭是指煮熟的白色稻米。」这类是知识库百科，不是她的记忆。
# 只认「短主语 + 是指/指的是/是」且**没有**聊天痕迹（冒号/说/问/回/聊）的句子。
_GENERIC_DEF_RE = re.compile(
    r"^[\u4e00-\u9fa5A-Za-z0-9]{1,12}(?:是指|指的是|是一种|是一个|是|指)"
    r"[^。！？!?\n]{0,40}[。！？!?]?$")


def _looks_like_chat(data: dict) -> bool:
    """答案/来源像不像「聊天记录/群归档」而不是别的语料。

    实测那套 RAG 的库里还有 `poet.song.rank.*.json`（宋词排行），
    跟她的记忆完全无关；这种情况继续问只会白等 6~13 秒。

    ⚠️ 2026-11-24 修正：以前只要来源是 `/app/documents/*.json` 就判「不是聊天」，
    结果把 `documents/xiaoming.txt`（弱智吧段子）这类正常归档也丢了；
    而且「小明为了光合作用变成了植物人。」没有冒号/「说」也会被误杀。
    现在只排除明确的诗词语料 + 空答案。
    """
    try:
        txt = json.dumps(data, ensure_ascii=False)[:3000].lower()
    except Exception:
        return True
    if "poet.song" in txt or "poet.tang" in txt or "poetry" in txt:
        return False
    ans = str((data or {}).get("answer") or "").strip()
    if is_no_answer(ans):
        return False
    # 百科式定义（「白饭是指煮熟的白色稻米」）不是记忆；带聊天痕迹的放行。
    _a1 = ans.splitlines()[0].strip().strip("*#-· ")
    if (_GENERIC_DEF_RE.match(_a1)
            and not any(k in _a1 for k in ("：", ":", "说", "问", "回", "聊"))):
        return False
    return True


# ================= ① recall =================
async def recall(
    query: str,
    *,
    speaker_qq: str = "",
    mood_value: float = 0.0,
    recent_ctx: list | None = None,
    timeout: float | None = None,
) -> dict:
    """带缓存的入口：同一条/相似问题 10 分钟内直接复用上次结果。"""
    q = str(query or "").strip()
    if not q:
        return empty_recall()
    ck = _cache_key(q)
    hit = _CACHE.get(ck)
    if hit and time.time() - float(hit[0]) < float(hit[2]):
        return dict(hit[1])
    _t0 = time.monotonic()
    out = await _recall_impl(
        q, speaker_qq=speaker_qq, mood_value=mood_value,
        recent_ctx=recent_ctx, timeout=timeout)
    _ms = (time.monotonic() - _t0) * 1000.0
    if _ms > LIB_LATENCY_MS:
        logger.warning(f"[AGENT] 图书馆 {_ms:.0f}ms > 目标 {LIB_LATENCY_MS:.0f}ms")
    # 候选池截断：RAG 偶尔回很多条，去重/去空答案后再留 WORLD_TOP 条
    if out and out.get("facts"):
        out["facts"] = clean_facts(out["facts"], WORLD_TOP)
    ttl = _CACHE_TTL_OK if (out or {}).get("facts") else _CACHE_TTL_EMPTY
    _CACHE[ck] = (time.time(), dict(out or empty_recall()), ttl)
    if len(_CACHE) > 300:
        for k in list(_CACHE)[:100]:
            _CACHE.pop(k, None)
    return out


def _decode_u(s: str) -> str:
    """RAG 的 rag_search 会把文档 JSON 里的中文转义成 `\\u5e8a`，先还原再给人看。"""
    return re.sub(r"\\u([0-9a-fA-F]{4})",
                  lambda m: chr(int(m.group(1), 16)), str(s or ""))


def _rag_tool_parse(data: dict) -> tuple:
    """把 `execute-tool?tool_name=rag_search` 的 output 拆成短事实。

    返回 (facts, skipped_poet)：
      · facts        能给她看的片段（≤5，每条 ≤160 字）；
      · skipped_poet 被「宋词/结构化文档」规则丢掉的段数 —— 用来判断
                     这库到底是不是聊天语料（全是诗词就该暂停，别再每轮烧 10s）。
    """
    if not isinstance(data, dict) or data.get("success") is False:
        return [], 0
    out = data.get("output") or data.get("result") or data.get("answer") or ""
    if isinstance(out, list):
        out = "\n\n".join(str(x) for x in out)
    s = _decode_u(str(out or "")).strip()
    if not s:
        return [], 0
    parts = re.split(r"\n\s*\n|(?=\*\*片段\s*\d+\*\*)", s)
    facts, skipped = [], 0
    for p in parts:
        p = re.sub(r"\s+", " ", p).strip(" -*\n\t")
        if not p or len(p) < 8:
            continue
        if p.startswith("找到") and "个相关片段" in p[:24]:
            continue
        # 去掉 RAG 自带的片段头：`**片段 1** (来源: /app/documents/x.json)`
        p = re.sub(r"^\**片段\s*\d+\**\s*(?:\(来源:[^)]*\))?\s*", "", p).strip()
        if not p or len(p) < 8:
            continue
        # 语料里混了宋词/结构化文档：整段是 JSON 的别塞给她（她会当成聊天内容）
        if "paragraphs" in p and "author" in p:
            skipped += 1
            continue
        facts.append(p[:160])
        if len(facts) >= 5:
            break
    return facts, skipped


def _rag_tool_facts(data: dict) -> list:
    return _rag_tool_parse(data)[0]


async def _rag_search(query: str) -> dict | None:
    """直接调 RAG 的检索工具。

    返回 None = 这次请求失败（可继续降级）；
    返回 dict  = {"facts": [...], "nonchat": bool}
                 nonchat=True 表示「通了、也有内容，但全是诗词/结构化文档」。
    """
    data = await _post_json(
        f"{AGENT_BASE_URL}{RAG_TOOL_ENDPOINT}",
        # rag_search 工具硬编码默认 method="vector"，会覆盖 RAG 项目
        # Config.DEFAULT_RETRIEVAL_METHOD="hybrid"。显式传 hybrid，走 patched
        # retriever 的「关键词优先 + 向量补充」，减少宋词/结构化文档的无关命中
        # （2026-11-28 审计；不传则纯向量，正是图书馆自 suspend 的诱因之一）。
        {"query": str(query or "")[:300], "top_k": max(3, WORLD_TOP),
         "method": "hybrid"},
        RAG_TOOL_TIMEOUT,
    )
    if data is None:
        return None
    facts, skipped = _rag_tool_parse(data)
    return {"facts": facts, "nonchat": bool(skipped and not facts)}


async def _recall_impl(
    query: str,
    *,
    speaker_qq: str = "",
    mood_value: float = 0.0,
    recent_ctx: list | None = None,
    timeout: float | None = None,
) -> dict:
    """
    问算力机：「这条消息，我记忆库里哪几条相关？我当时什么心情？」
    永远返回 dict（最差是 EMPTY_RECALL），调用方不需要 try。
    """
    query = str(query or "").strip()
    if not query:
        return empty_recall()

    to = timeout or RECALL_TIMEOUT

    global _LIBRARY_DEAD_UNTIL, _LIBRARY_WARNED, _RECALL_ENDPOINT_MISSING
    if time.time() < _LIBRARY_DEAD_UNTIL:
        return empty_recall()
    STATS["calls"] = int(STATS.get("calls") or 0) + 1
    # 先假定「这次可能失败」：调用方（流水线）超时会把协程直接取消，
    # 那样下面设置暂停的代码永远跑不到 → 每条消息都白等。先占位，成功再清掉。
    _LIBRARY_DEAD_UNTIL = time.time() + 600

    # 优先：结构化端点（第一次 404 之后就不再打）
    data = None
    if not _RECALL_ENDPOINT_MISSING:
        data = await _post_json(
            f"{AGENT_BASE_URL}{RECALL_ENDPOINT}",
            {
                "query": query,
                "user_qq": str(speaker_qq or ""),
                "mood": float(mood_value or 0.0),
                "recent_ctx": list(recent_ctx or [])[-8:],
            },
            to,
        )
        if data is None:
            _RECALL_ENDPOINT_MISSING = True
            logger.info(
                f"[AGENT] {RECALL_ENDPOINT} 不可用（404/超时）→ 之后直接用 "
                f"{SMART_QUERY_ENDPOINT}")
    if data is not None:
        out = _normalize_recall(data, "recall")
        logger.info(
            f"[AGENT] 🧠 recall 结构化命中：facts={len(out['facts'])} "
            f"events={len(out['events'])} emotions={len(out['emotions'])}"
        )
        return out

    # 兜底 1：直接调 rag_search 工具（2026-11-26 实测最稳：1~9s，带来源）
    # 为什么放在 smart-query 前面：smart-query 的 LLM 规划层会卡 >70s，
    # 每次白等 14s 还被流水线取消；rag_search 不经过规划，直接查向量库。
    _rs = await _rag_search(query)
    if _rs and _rs.get("facts"):
        _LIBRARY_DEAD_UNTIL = 0.0
        STATS["ok"] = int(STATS.get("ok") or 0) + 1
        logger.info(f"[AGENT] 🔎 rag_search 命中 {len(_rs['facts'])} 条")
        return {"facts": list(_rs["facts"]), "events": [], "emotions": [],
                "self_state": "", "source": "rag_tool"}
    if _rs is not None:
        # 通了、也有内容，但全是诗词/结构化文档 → 和 smart-query 的
        # `_looks_like_chat` 一样暂停 6 小时，别每轮白烧 10s。
        if _rs.get("nonchat"):
            _LIBRARY_DEAD_UNTIL = time.time() + 6 * 3600
            if not _LIBRARY_WARNED:
                _LIBRARY_WARNED = True
                logger.warning(
                    "[AGENT] ⚠️ rag_search 命中的全是宋词/结构化文档 → "
                    "图书馆暂停 6 小时；先把 data/dialogue 归档导入 RAG")
            return empty_recall()
        # 通了但没命中：不是「图书馆不可用」，别暂停
        _LIBRARY_DEAD_UNTIL = 0.0
        STATS["empty"] = int(STATS.get("empty") or 0) + 1
        logger.info("[AGENT] rag_search 通了但没命中")
        return empty_recall()

    # 兜底 2：/api/agent/smart-query（RAG v3 上有，实测 3~14s；可能卡住）
    # ⚠️ 2026-11-24 修正：以前写死 2.5s，而 RAG 实测要 5~14s →
    #    每条消息都在 2.5s 超时、然后判「图书馆不可用 10 分钟」，所以永远 0 条。
    #    现在按 AGENT_LIBRARY_TIMEOUT（默认 8.5s）等；流水线那边给 9s。
    _who = (f"QQ {speaker_qq} " if str(speaker_qq or "").isdigit() else "有人")
    data = await _post_json(
        f"{AGENT_BASE_URL}{SMART_QUERY_ENDPOINT}",
        {"question": _RECALL_PROMPT.format(
            date=time.strftime("%Y-%m-%d"), who=_who, q=query)},
        max(3.0, LIB_TIMEOUT),
    )
    if data is None:
        _LIBRARY_DEAD_UNTIL = time.time() + 600
        logger.info("[AGENT] 图书馆暂时不可用（超时/失败）→ 10 分钟内不再试")
        return empty_recall()
    # 「没检索到」不是语料问题：图书馆是通的，这次就是没命中，不要暂停。
    if is_no_answer(str((data or {}).get("answer") or "")):
        _LIBRARY_DEAD_UNTIL = 0.0
        STATS["empty"] = int(STATS.get("empty") or 0) + 1
        logger.info("[AGENT] 图书馆没有命中（RAG 回「没有」）")
        return empty_recall()
    if not _looks_like_chat(data):
        _LIBRARY_DEAD_UNTIL = time.time() + 6 * 3600
        if not _LIBRARY_WARNED:
            _LIBRARY_WARNED = True
            logger.warning(
                "[AGENT] ⚠️ 知识库里不是聊天记录（来源看起来是别的语料），"
                "图书馆暂停 6 小时；要恢复请先把 data/dialogue 归档导入 RAG"
                "（scripts/library_sync.py）")
        return empty_recall()

    out = _normalize_recall(data, "smart")
    if out["facts"]:
        _LIBRARY_DEAD_UNTIL = 0.0
        STATS["ok"] = int(STATS.get("ok") or 0) + 1
        logger.info(f"[AGENT] 🧠 recall 走 smart-query，拿到 {len(out['facts'])} 条")
        return out
    STATS["empty"] = int(STATS.get("empty") or 0) + 1
    return empty_recall()


# ================= ② reflection =================
async def _reflection_via_queue(bundle: dict, timeout: float) -> dict | None:
    """没有反向隧道时走这里：把 bundle 落成 json，挂进任务队列，等 node.py 拉走。"""
    try:
        from plugins.tool_dispatcher import STATUS
    except Exception as e:
        logger.warning(f"[AGENT] 任务队列不可用: {e}")
        return None

    task_id = f"reflect_{int(time.time())}"
    bundle_path = QUEUE_DIR / f"{task_id}.json"
    try:
        bundle_path.write_text(
            json.dumps(bundle, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as e:
        logger.warning(f"[AGENT] 写 bundle 失败: {e}")
        return None

    STATUS["tasks"][task_id] = {
        "type": "reflection",
        "status": "pending",
        "input": str(bundle_path),
        "output": "",
    }
    logger.info(f"[AGENT] 📤 复盘任务已入队: {task_id}（等待算力机拉取）")

    deadline = time.time() + timeout
    while time.time() < deadline:
        await asyncio.sleep(3)
        task = STATUS["tasks"].get(task_id) or {}
        status = task.get("status")
        if status == "completed":
            out = task.get("output")
            STATUS["tasks"].pop(task_id, None)
            try:
                bundle_path.unlink(missing_ok=True)
            except Exception:
                pass
            if isinstance(out, dict):
                # 算力机失败时回传 {"error": ...}，别当成日记
                if out.get("error") and not out.get("journal"):
                    logger.warning(f"[AGENT] 复盘任务 {task_id} 算力机报错: {out.get('error')}")
                    return None
                return out
            if isinstance(out, str) and out.strip():
                try:
                    return json.loads(out)
                except Exception:
                    return {"journal": out}
            return None
        if status in ("failed", "timeout"):
            STATUS["tasks"].pop(task_id, None)
            logger.warning(f"[AGENT] 复盘任务 {task_id} 状态={status}")
            return None

    logger.warning(f"[AGENT] ⏰ 复盘任务 {task_id} 等待超时（{timeout:.0f}s）")
    STATUS["tasks"].pop(task_id, None)
    return None


async def reflection(bundle: dict, *, timeout: float | None = None) -> dict | None:
    """
    把打包好的两天记忆交给算力机，拿回 {journal, impression, archive}。
    先试 HTTP（快），不行就走任务队列（稳）。都失败返回 None。
    """
    data = await _post_json(
        f"{AGENT_BASE_URL}{REFLECT_ENDPOINT}", bundle, REFLECT_HTTP_TIMEOUT
    )
    if isinstance(data, dict) and data.get("error") and not data.get("journal"):
        logger.warning(f"[AGENT] 复盘 HTTP 通道返回错误: {data.get('error')}")
        data = None
    if isinstance(data, dict) and (data.get("journal") or data.get("impression")):
        logger.info("[AGENT] 📓 复盘走 HTTP 通道成功")
        return data

    logger.info("[AGENT] 复盘 HTTP 不通，改走任务队列…")
    return await _reflection_via_queue(
        bundle, timeout or REFLECT_QUEUE_TIMEOUT
    )
