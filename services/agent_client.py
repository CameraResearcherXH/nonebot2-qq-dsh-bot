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

# 知识库里如果根本不是聊天记录（实测是 poet.song.rank.* 宋词数据），
# 就别每条消息都去问一次（一次 6~13s，纯浪费）。6 小时内不再试。
_LIBRARY_DEAD_UNTIL = 0.0
_LIBRARY_WARNED = False
# /api/agent/recall 不存在时记一下，别每条消息都白打一次 404
_RECALL_ENDPOINT_MISSING = False

_RECALL_PROMPT = (
    "从知识库的历史聊天记录里，找出和这句话最相关的 3 条原话：{q}\n"
    "要求：只列「谁说过什么」，一条一行；没有就只回「没有」。"
    "不要解释、不要总结、不要编。"
)

RECALL_TIMEOUT = tuning.f("AGENT_RECALL_TIMEOUT")
REFLECT_HTTP_TIMEOUT = tuning.f("AGENT_REFLECT_TIMEOUT")
REFLECT_QUEUE_TIMEOUT = tuning.f("AGENT_REFLECT_QUEUE_TIMEOUT")

QUEUE_DIR = Path("data/temp_files")
QUEUE_DIR.mkdir(parents=True, exist_ok=True)

EMPTY_RECALL = {"facts": [], "events": [], "emotions": [], "self_state": "", "source": "none"}

# 图书馆调用统计（给 `/测函数 记忆` 用）
STATS = {"calls": 0, "ok": 0, "empty": 0}


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
        facts = _as_list(data.get("answer"))

    return {
        "facts": facts,
        "events": _as_list(data.get("events")),
        "emotions": _as_list(data.get("emotions")),
        "self_state": str(data.get("self_state") or "").strip(),
        "source": source,
    }


def _looks_like_chat(data: dict) -> bool:
    """答案/来源像不像「聊天记录」而不是别的语料。

    实测那套 RAG 的库里是 `poet.song.rank.*.json`（宋词排行），
    跟她的记忆完全无关；这种情况继续问只会白等 6~13 秒。
    """
    try:
        txt = json.dumps(data, ensure_ascii=False)[:3000].lower()
    except Exception:
        return True
    if "poet.song" in txt or "poetry" in txt:
        return False
    if "/app/documents/" in txt and not any(
            k in txt for k in ("dialogue", "chat", "聊天", "群聊", "私聊")):
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

    # 兜底 1：/api/agent/smart-query（RAG v3 上有，实测 3~13s）
    # ⚠️ 它在回复关键路径上，超时必须收紧：实测复杂问句要 12s+，
    #    所以这里最多等 6s；超时就当「图书馆暂时不可用」，10 分钟内不再试。
    data = await _post_json(
        f"{AGENT_BASE_URL}{SMART_QUERY_ENDPOINT}",
        {"question": _RECALL_PROMPT.format(q=query)},
        min(6.0, to + 3),
    )
    if data is None:
        _LIBRARY_DEAD_UNTIL = time.time() + 600
        logger.info("[AGENT] 图书馆暂时不可用（超时/失败）→ 10 分钟内不再试")
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
