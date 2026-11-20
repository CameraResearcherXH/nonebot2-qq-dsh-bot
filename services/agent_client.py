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

RECALL_TIMEOUT = tuning.f("AGENT_RECALL_TIMEOUT")
REFLECT_HTTP_TIMEOUT = tuning.f("AGENT_REFLECT_TIMEOUT")
REFLECT_QUEUE_TIMEOUT = tuning.f("AGENT_REFLECT_QUEUE_TIMEOUT")

QUEUE_DIR = Path("data/temp_files")
QUEUE_DIR.mkdir(parents=True, exist_ok=True)

EMPTY_RECALL = {"facts": [], "events": [], "emotions": [], "self_state": "", "source": "none"}


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

    # 优先：结构化端点
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
    if data is not None:
        out = _normalize_recall(data, "recall")
        logger.info(
            f"[AGENT] 🧠 recall 结构化命中：facts={len(out['facts'])} "
            f"events={len(out['events'])} emotions={len(out['emotions'])}"
        )
        return out

    # 兜底：现有 RAG 的 /api/agent/query（这条链路已经在跑）
    data = await _post_json(
        f"{AGENT_BASE_URL}{QUERY_ENDPOINT}",
        {"question": query, "agent_type": "full", "provider": "ollama", "max_iterations": 1},
        to + 8,
    )
    if data is not None:
        if data.get("success") is False:
            return empty_recall()
        out = _normalize_recall(data, "query")
        if out["facts"]:
            logger.info(f"[AGENT] 🧠 recall 退回 /query，拿到 {len(out['facts'])} 条事实")
        return out

    return dict(EMPTY_RECALL)


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
