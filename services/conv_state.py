# services/conv_state.py
"""
对话状态（rolling conversation state）——把「更早的对话」压成 5 类可接续状态。

参考 MaiBot 的 context compaction（只借分类思路，不抄代码）。

为什么要它（用户 2026-11-19）：
  我们现在的做法是「只留最近 N 条 + 一句『更早的已被折叠』」。
  结果是：话题一长，她就只记得最近几句；跨天/重启更是完全接不上
  「上次说到哪、答应了谁什么、还有什么事没做完」。

它和现有设计的关系（**不覆盖，只补一层**）：
  · 最近 N 条原文：不动（最鲜活，模型最信这个）；
  · 词条 lorebook：不动（那是「知道什么」——设定、梗、事实）；
  · 每日总结：不动（那是「昨天干了什么」）；
  · **本模块**：补一个「这段对话进行到哪了」的滚动状态，
    只在历史被折叠时才有内容，且**永远排在历史块前面**（越靠前越像背景）。

五类（每条信息只放一处）：
  topics      当前话题（含指代关系）
  background  关键背景（人物/关系/事件；区分事实、转述、猜测、玩笑）
  progress    进展与结论（完成状态以实际结果为准）
  pending     未完事项（未答问题、承诺，注明是谁的）
  agreements  对话约定（谁提的、对谁有效、过期就删）

配置（.env）：
  CONV_STATE_ENABLED=true
  CONV_STATE_MODEL=（默认跟 XAI_MODEL）
  CONV_STATE_MIN_INTERVAL=150    # 同一会话两次压缩的最小间隔（秒）
  CONV_STATE_MAX_CHARS=420       # 注入块的最大长度
"""
from core import tuning

import json
import os
import re
import time
from pathlib import Path

try:
    import httpx
except Exception:  # 离线自检/没装依赖时也能 import
    httpx = None

try:
    from nonebot import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)

from .config import BOT_MODE, PROXY, XAI_API_KEY, XAI_BASE_URL, XAI_MODEL

ENABLED = os.getenv("CONV_STATE_ENABLED", "true").lower() in ("1", "true", "yes", "on")
MODEL = os.getenv("CONV_STATE_MODEL", "") or XAI_MODEL
MIN_INTERVAL = tuning.f("CONV_STATE_MIN_INTERVAL")
MAX_CHARS = tuning.i("CONV_STATE_MAX_CHARS")
TIMEOUT = tuning.f("CONV_STATE_TIMEOUT")

STATE_DIR = Path("data/state")
_LABELS = {
    "topics": "当前话题",
    "background": "关键背景",
    "progress": "进展与结论",
    "pending": "未完事项",
    "agreements": "对话约定",
}
_ORDER = ("topics", "background", "progress", "pending", "agreements")

# key -> 上次压缩时间 / 上次输入指纹（防重复压同一段）
_LAST_AT = {}
_LAST_SIG = {}
_WARNED = set()


def enabled() -> bool:
    return bool(ENABLED and BOT_MODE != "mock" and XAI_API_KEY and httpx is not None)


def _safe(key: str) -> str:
    return re.sub(r"[^0-9A-Za-z_-]", "_", str(key or ""))[:80] or "default"


def _path(key: str) -> Path:
    return STATE_DIR / f"{_safe(key)}.json"


def get(key: str) -> dict:
    """读状态；坏文件当作空，不抛。"""
    try:
        p = _path(key)
        if not p.exists():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        out = {}
        for k in _ORDER:
            v = data.get(k)
            if isinstance(v, list):
                out[k] = [str(x).strip() for x in v if str(x).strip()][:6]
        if out:
            out["_at"] = float(data.get("_at") or 0)
        return out
    except Exception:
        return {}


def _save(key: str, data: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        _path(key).write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        logger.debug(f"[CONV-STATE] 写入失败 {key}: {type(e).__name__}: {e}")


def render(key: str) -> str:
    """渲染成 prompt 里的一段；没内容返回空串。"""
    st = get(key)
    if not st:
        return ""
    lines = []
    for k in _ORDER:
        vals = st.get(k) or []
        if not vals:
            continue
        body = "；".join(vals[:3])
        lines.append(f"· {_LABELS[k]}：{body}")
    if not lines:
        return ""
    text = ("▍这段对话之前的状态（更早的内容已压缩，用来接上文；"
            "最近几条原文在下面，以原文为准）\n" + "\n".join(lines))
    return text[:MAX_CHARS]


def _build_prompt(old: dict, lines) -> str:
    prev = {k: (old.get(k) or []) for k in _ORDER}
    body = "\n".join(f"- {str(x)[:160]}" for x in list(lines)[-24:])
    return (
        "你在维护一段聊天对话的「可接续状态」。只输出 JSON，不要解释。\n\n"
        f"已有的状态（可能为空）：\n{json.dumps(prev, ensure_ascii=False)}\n\n"
        f"最近这段对话（由旧到新）：\n{body}\n\n"
        "请把「已有状态 + 这段对话」合并成新的状态，规则：\n"
        "· 五个字段都必须存在，值都是字符串数组，每项一句话、不超过 30 字；\n"
        "· 每条信息只放一处，不要重复；没有内容的字段给空数组；\n"
        "· topics：正在聊的主题，带上必要的指代关系（谁说的「那个」指什么）；\n"
        "· background：人物/关系/事件背景，注明说话人；区分事实、转述、猜测、玩笑；\n"
        "· progress：有结论的互动、已确认的结果；调用/答应本身不算完成；\n"
        "· pending：仍未回答的问题、没做完的事、承诺，写明是谁的；\n"
        "· agreements：对话中明确提出、对后续仍有效的要求（谁提的、对谁有效），过期的删掉；\n"
        "· 每个字段最多 4 条，优先保留最近、最具体的。\n\n"
        '只输出：{"topics":[],"background":[],"progress":[],"pending":[],"agreements":[]}'
    )


def _parse(text: str) -> dict:
    t = str(text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except Exception:
        m = re.search(r"\{.*\}", t, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return {}


async def update(key: str, lines, force: bool = False) -> bool:
    """
    把最近这段对话合并进状态。返回是否真的更新了。

    调用方不需要等它：chat/llm 里都是 create_task 丢出去，失败静默（不影响聊天）。
    """
    if not enabled() or not key:
        return False
    lines = [str(x).strip() for x in (lines or []) if str(x).strip()]
    if len(lines) < 6:      # 太短不值得压，也省 token
        return False
    now = time.time()
    sig = "|".join(lines[-3:])[:200]
    if not force:
        if now - float(_LAST_AT.get(key) or 0) < MIN_INTERVAL:
            return False
        if sig and sig == _LAST_SIG.get(key):
            return False

    old = get(key)
    prompt = _build_prompt(old, lines)
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "你只做对话状态压缩，不参与聊天，只输出 JSON。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "max_tokens": 700,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {XAI_API_KEY}", "Content-Type": "application/json"}
    kwargs = {"timeout": TIMEOUT}
    if PROXY:
        kwargs["proxy"] = PROXY

    _LAST_AT[key] = now
    _LAST_SIG[key] = sig
    try:
        async with httpx.AsyncClient(**kwargs) as client:
            try:
                r = await client.post(f"{XAI_BASE_URL}/chat/completions",
                                      headers=headers, json=payload)
                r.raise_for_status()
            except Exception:
                payload.pop("response_format", None)
                r = await client.post(f"{XAI_BASE_URL}/chat/completions",
                                      headers=headers, json=payload)
                r.raise_for_status()
            data = r.json()
            try:
                from . import llm_usage as _lu
                _lu.record_from_response(data, purpose="conv_state")
            except Exception:
                pass
            content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    except Exception as e:
        if key not in _WARNED:
            _WARNED.add(key)
            logger.warning(f"[CONV-STATE] {key} 压缩失败（不影响聊天）: {type(e).__name__}: {e}")
        else:
            logger.debug(f"[CONV-STATE] {key} 压缩失败: {type(e).__name__}: {e}")
        return False

    obj = _parse(content)
    if not isinstance(obj, dict):
        return False
    out = {"_at": time.time()}
    got = 0
    for k in _ORDER:
        vals = obj.get(k)
        if isinstance(vals, str):
            vals = [vals]
        if not isinstance(vals, list):
            vals = []
        clean = []
        for v in vals[:4]:
            v = str(v or "").strip()
            if v and v not in clean:
                clean.append(v[:60])
        out[k] = clean
        got += len(clean)
    if not got:
        return False
    _save(key, out)
    logger.info(f"[CONV-STATE] {key} 状态更新："
                + "｜".join(f"{_LABELS[k]}{len(out.get(k) or [])}" for k in _ORDER))
    return True


def maybe_update(key: str, lines) -> None:
    """
    非阻塞版：够条件就丢一个后台任务。调用方不需要 await。

    ⚠️ 只在有事件循环时调用（nonebot 里总是有）；没有循环就静默跳过。
    """
    if not enabled() or not key:
        return
    now = time.time()
    if now - float(_LAST_AT.get(key) or 0) < MIN_INTERVAL:
        return
    try:
        import asyncio
        asyncio.get_running_loop().create_task(update(key, lines))
    except RuntimeError:
        return
    except Exception:
        return


def clear(key: str) -> None:
    _LAST_AT.pop(key, None)
    _LAST_SIG.pop(key, None)
    try:
        p = _path(key)
        if p.exists():
            p.unlink()
    except Exception:
        pass
