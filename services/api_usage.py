# services/api_usage.py
"""
API 余额 / 用量（给 function calling 用）。

用户 2026-11-19：
  「增加一个 function calling，可以通过 .env 的 XAI_API_KEY 查询余额和用量」

两个来源分开看，别混：
  · **余额**：问供应商。`.env` 里 `XAI_BASE_URL` 现在指向 DeepSeek
    （https://api.deepseek.com），走官方 `GET /user/balance`。
    xAI 官方（api.x.ai）没有余额接口，只有 `GET /v1/api-key`（key 本身的信息）→
    照实说「查不到余额」，不要编。
  · **用量**：DeepSeek / xAI 都**没有**公开的用量接口，所以自己记账：
    每次 `llm.call_llm` 拿到 `usage` 就 `note_usage()` 累加到
    `data/api_usage.json`（按天、按模型）。她问「今天用了多少」时读这个。
    注意：这里只记**聊天主链路**的调用；审核/状态压缩那些旁路没接进来（已知）。

为什么要做成工具而不是命令：主人问「还有多少钱」是自然语言，
不该要求他记一条 `/余额`。
"""
from __future__ import annotations
from core import tuning

import json
import os
import time
from pathlib import Path

try:
    from nonebot import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)

BASE_URL = (os.getenv("XAI_BASE_URL") or "https://api.x.ai/v1").rstrip("/")
API_KEY = (os.getenv("XAI_API_KEY") or "").strip()
MODEL = (os.getenv("XAI_MODEL") or "").strip()
STATE_FILE = Path(os.getenv("API_USAGE_FILE", "data/api_usage.json"))
KEEP_DAYS = tuning.i("API_USAGE_KEEP_DAYS")
TIMEOUT = tuning.f("API_USAGE_TIMEOUT")


# ============================== 供应商 ============================== #
def root_url() -> str:
    """`https://api.deepseek.com/v1` → `https://api.deepseek.com`（余额接口不在 /v1 下）。"""
    u = BASE_URL
    for suffix in ("/v1", "/v1/"):
        if u.endswith(suffix):
            u = u[: -len(suffix)]
    return u.rstrip("/")


def provider() -> str:
    u = BASE_URL.lower()
    if "deepseek" in u:
        return "deepseek"
    if "x.ai" in u:
        return "xai"
    return "openai"


async def _get_json(url: str, headers: dict) -> tuple:
    """返回 (data, err)。不抛异常，调用方按 err 说人话。"""
    try:
        import httpx
    except Exception as e:  # pragma: no cover
        return None, f"没装 httpx（{type(e).__name__}）"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.get(url, headers=headers)
            if r.status_code >= 400:
                return None, f"HTTP {r.status_code}：{str(getattr(r, 'text', '') or '')[:120]}"
            return r.json(), ""
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


async def fetch_balance() -> dict:
    """
    查余额。返回：
      {ok, provider, available, items:[{currency,total,granted,topped_up}], note, err}

    ok=False 只表示「没查到」，不代表账户没钱。
    """
    p = provider()
    if not API_KEY:
        return {"ok": False, "provider": p, "err": "没配 XAI_API_KEY"}
    root = root_url()
    headers = {"Authorization": f"Bearer {API_KEY}", "Accept": "application/json"}

    if p == "deepseek":
        data, err = await _get_json(f"{root}/user/balance", headers)
        if not data:
            return {"ok": False, "provider": p, "err": err or "返回为空"}
        items = []
        for b in (data.get("balance_infos") or []):
            items.append({
                "currency": str(b.get("currency") or ""),
                "total": str(b.get("total_balance") or ""),
                "granted": str(b.get("granted_balance") or ""),
                "topped_up": str(b.get("topped_up_balance") or ""),
            })
        return {"ok": True, "provider": p,
                "available": bool(data.get("is_available")),
                "items": items, "err": ""}

    if p == "xai":
        # xAI 官方没有余额接口，只有 key 信息（别把「查到 key」说成「查到余额」）
        data, err = await _get_json(f"{root}/v1/api-key", headers)
        if not data:
            return {"ok": False, "provider": p, "err": err or "返回为空"}
        name = data.get("name") or data.get("redactedApiKey") or "?"
        return {"ok": True, "provider": p, "available": True, "items": [],
                "note": f"xAI 官方只提供 key 信息（{name}），没有余额接口；"
                        f"余额/账单要登录 console.x.ai 看。", "err": ""}

    # 聚合网关：试一下 DeepSeek 风格的 /user/balance，不行就明说
    data, err = await _get_json(f"{root}/user/balance", headers)
    if data and data.get("balance_infos"):
        items = [{"currency": str(b.get("currency") or ""),
                  "total": str(b.get("total_balance") or ""),
                  "granted": str(b.get("granted_balance") or ""),
                  "topped_up": str(b.get("topped_up_balance") or "")}
                 for b in (data.get("balance_infos") or [])]
        return {"ok": True, "provider": p, "available": bool(data.get("is_available")),
                "items": items, "err": ""}
    return {"ok": False, "provider": p,
            "err": err or "这个网关没有余额接口（只认 DeepSeek / xAI 官方）"}


# ============================== 本机记账 ============================== #
def _today() -> str:
    return time.strftime("%Y-%m-%d", time.localtime())


def _load() -> dict:
    try:
        raw = STATE_FILE.read_text(encoding="utf-8")
        d = json.loads(raw or "{}")
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(d: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - KEEP_DAYS * 86400))
        d = {k: v for k, v in d.items() if str(k) >= cutoff}      # 日期字符串比较即可
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except Exception as e:
        logger.debug(f"[USAGE] 记账失败: {type(e).__name__}: {e}")


def note_usage(*, prompt: int = 0, completion: int = 0, cache_hit: int = 0,
               model: str = "", kind: str = "chat") -> None:
    """
    记一次调用（同步、失败静默）。prompt 传**总** prompt token，
    cache_hit 是其中命中前缀缓存的部分（只用于展示命中率）。
    """
    try:
        d = _load()
        day = _today()
        b = d.get(day) or {}
        b["calls"] = int(b.get("calls") or 0) + 1
        b["prompt"] = int(b.get("prompt") or 0) + max(0, int(prompt or 0))
        b["completion"] = int(b.get("completion") or 0) + max(0, int(completion or 0))
        b["cache_hit"] = int(b.get("cache_hit") or 0) + max(0, int(cache_hit or 0))
        models = b.get("models") or {}
        m = str(model or MODEL or "?")
        models[m] = int(models.get(m) or 0) + 1
        b["models"] = models
        kinds = b.get("kinds") or {}
        kinds[kind] = int(kinds.get(kind) or 0) + 1
        b["kinds"] = kinds
        d[day] = b
        _save(d)
    except Exception as e:
        logger.debug(f"[USAGE] note_usage 失败: {type(e).__name__}: {e}")


def usage(days: int = 1) -> list:
    """最近 N 天的用量（新的在前）。days=1 = 今天。"""
    d = _load()
    keys = sorted(d.keys(), reverse=True)[: max(1, int(days or 1))]
    return [dict(d[k], day=k) for k in keys]


def _fmt(n) -> str:
    try:
        return f"{int(n):,}"
    except Exception:
        return str(n)


async def report_text(days: int = 1) -> str:
    """给工具用的一段人话（她可以直接念给主人，也可以自己总结）。"""
    days = max(1, min(30, int(days or 1)))
    bal = await fetch_balance()
    lines = []
    if bal.get("ok"):
        head = f"💰 余额（{bal.get('provider')}）"
        if bal.get("items"):
            for it in bal["items"]:
                cur = it.get("currency") or ""
                lines.append(
                    f"{head}：{it.get('total')} {cur}"
                    f"（赠送 {it.get('granted')}｜充值 {it.get('topped_up')}）"
                    f"｜{'可用' if bal.get('available') else '不可用'}")
        else:
            lines.append(head + "：" + (bal.get("note") or "供应商没返回余额"))
        if bal.get("note") and bal.get("items"):
            lines.append("· " + str(bal["note"]))
    else:
        lines.append(f"💰 余额查不到：{bal.get('err') or '未知原因'}")

    rows = usage(days)
    if not rows:
        lines.append("📊 本机还没记到调用（重启后从零开始记）")
    else:
        t = rows[0]
        tot = int(t.get("prompt") or 0)
        hit = int(t.get("cache_hit") or 0)
        pct = f"（缓存命中 {hit / tot * 100:.0f}%）" if tot and hit else ""
        lines.append(
            f"📊 今天（{t.get('day')}）：调用 {_fmt(t.get('calls'))} 次"
            f"｜prompt {_fmt(tot)} tokens{pct}"
            f"｜completion {_fmt(t.get('completion'))} tokens")
        models = t.get("models") or {}
        if models:
            lines.append("· 模型：" + "、".join(
                f"{k} × {_fmt(v)}" for k, v in sorted(
                    models.items(), key=lambda x: -int(x[1] or 0))[:5]))
        if len(rows) > 1:
            lines.append("· 近 %d 天：" % len(rows) + "｜".join(
                f"{r.get('day')[5:]} {_fmt(r.get('calls'))} 次"
                f"/{_fmt(int(r.get('prompt') or 0) + int(r.get('completion') or 0))} tok"
                for r in rows))
    lines.append("（这里只算聊天主链路；完整 token 归因看 /用量（含 judge/audit 两个三方 LLM））")
    return "\n".join(lines)


def status_text() -> str:
    """给 /测函数 用的一行状态。"""
    return (f"API 用量记账：{STATE_FILE}｜供应商 {provider()}｜"
            f"今天 {_fmt((usage(1) or [{}])[0].get('calls'))} 次调用")
