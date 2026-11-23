# services/image_search.py
"""以图搜源：走 PicImageSearch 聚合库，不再直接裸调 trace.moe。

用户 2026-11-22：
  「加一个以图搜源的插件 PicImageSearch-main……以后不直接搜 trace.moe，
   而是直接走这个插件综合搜，否则太容易搜不到了。单 trace.moe 也很容易搜错。」

所以这里做三件事：
  1. 统一入口 `search(image_bytes)`：并行问 TraceMoe / SauceNAO / Ascii2D / IQDB；
  2. 统一结果结构（engine/title/url/similarity/author/extra），按相似度排序；
  3. `trace_moe_raw()` 给 `vision_verify` 兼容用：内部还是 trace.moe 的原始
     result 结构，但请求已经走 PicImageSearch，不再自己拼 HTTP。
"""
from __future__ import annotations

import asyncio
import os

try:
    from nonebot import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)

# PicImageSearch 的 SauceNAO 可选 key（没有也能搜，只是限速更严）
SAUCENAO_KEY = (os.getenv("PICSEARCH_SAUCENAO_KEY") or "").strip()
DEFAULT_ENGINES = ("tracemoe", "saucenao", "ascii2d", "iqdb")


def _num(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return float(default)


def _sim(it) -> float:
    v = _num(getattr(it, "similarity", 0), 0)
    # 不同引擎有的给 0~1，有的给 0~100；统一成 0~100
    return v * 100 if 0 < v <= 1 else v


def _title(it) -> str:
    for k in ("title", "title_chinese", "title_native", "title_english",
              "title_romaji", "jp_name", "eng_name", "material",
              "content"):        # iqdb 用 content（Best match / Additional match）
        v = getattr(it, k, "")
        if v:
            return str(v)
    return ""


def _author(it) -> str:
    for k in ("author", "artist", "creator", "member_name", "user_name",
              "twitter_user_handle", "source"):   # iqdb 用 source（Danbooru 等）
        v = getattr(it, k, "")
        if v:
            return ", ".join(v) if isinstance(v, list) else str(v)
    return ""


def _one(engine: str, it) -> dict:
    extra = {}
    if engine == "iqdb":
        extra = {
            "match": getattr(it, "content", None),
            "source": getattr(it, "source", None),
            "size": getattr(it, "size", None),
        }
    if engine == "tracemoe":
        extra = {
            "episode": getattr(it, "episode", None),
            "from": getattr(it, "From", None),
            "to": getattr(it, "To", None),
            "anilist_id": getattr(it, "anilist_id", None),
            "filename": getattr(it, "filename", ""),
        }
    return {
        "engine": engine,
        "title": _title(it)[:120],
        "url": str(getattr(it, "url", "") or "")[:300],
        "thumbnail": str(getattr(it, "thumbnail", "") or "")[:300],
        "similarity": round(_sim(it), 1),
        "author": _author(it)[:80],
        "extra": {k: v for k, v in extra.items() if v not in (None, "", 0)},
    }


async def _run_engine(client, engine: str, image_bytes: bytes):
    from vendor.PicImageSearch import Ascii2D, Iqdb, SauceNAO, TraceMoe

    if engine == "tracemoe":
        resp = await TraceMoe(client=client).search(file=image_bytes)
    elif engine == "saucenao":
        resp = await SauceNAO(client=client, api_key=SAUCENAO_KEY or None).search(
            file=image_bytes)
    elif engine == "ascii2d":
        resp = await Ascii2D(client=client).search(file=image_bytes)
    elif engine == "iqdb":
        resp = await Iqdb(client=client).search(file=image_bytes)
    else:
        return []
    return [_one(engine, it) for it in (getattr(resp, "raw", None) or [])[:5]]


async def search(image_bytes: bytes, *, engines=None, timeout: float = 25) -> list[dict]:
    """并行搜多个源，返回统一结构（按相似度降序）。失败/超时静默跳过。"""
    if not image_bytes:
        return []
    from vendor.PicImageSearch import Network

    want = tuple(engines or DEFAULT_ENGINES)
    out: list[dict] = []
    try:
        async with Network(timeout=timeout) as client:
            tasks = [asyncio.create_task(_run_engine(client, e, image_bytes))
                     for e in want]
            for t in tasks:
                try:
                    out.extend(await asyncio.wait_for(t, timeout=timeout))
                except Exception as e:
                    logger.debug(f"[IMG-SEARCH] 源失败: {type(e).__name__}: {e}")
    except Exception as e:
        logger.warning(f"[IMG-SEARCH] 聚合搜索失败: {type(e).__name__}: {e}")
        return []
    # 同一标题只留相似度最高的一条
    best = {}
    for r in out:
        key = (r.get("engine"), str(r.get("title") or "").strip().lower())
        if key not in best or r["similarity"] > best[key]["similarity"]:
            best[key] = r
    return sorted(best.values(), key=lambda x: -x.get("similarity", 0))


async def trace_moe_raw(image_bytes: bytes, *, timeout: float = 25) -> dict:
    """给 vision_verify 兼容：返回 trace.moe 原始 JSON 结构（result 列表）。

    请求已经走 PicImageSearch.TraceMoe，不再自己拼 HTTP。
    """
    if not image_bytes:
        return {}
    from vendor.PicImageSearch import Network, TraceMoe

    try:
        async with Network(timeout=timeout) as client:
            resp = await TraceMoe(client=client).search(file=image_bytes)
        origin = getattr(resp, "origin", None)
        if not isinstance(origin, dict) or "result" not in origin:
            # trace.moe 额度用完 / 返回 {"error":...} 时没有 result 键。
            # 这是常态（不是 bug），别每张图都打 WARNING。
            logger.debug(f"[IMG-SEARCH] trace.moe 没有 result：{str(origin)[:120]}")
            return {}
        return origin
    except KeyError as e:
        logger.debug(f"[IMG-SEARCH] trace.moe 无结果（{e}）")
        return {}
    except Exception as e:
        logger.warning(f"[IMG-SEARCH] trace.moe 失败: {type(e).__name__}: {e}")
        return {}


def format_results(results: list[dict], *, limit: int = 8) -> str:
    rows = list(results or [])[:max(1, int(limit))]
    if not rows:
        return ""
    lines = []
    for r in rows:
        sim = _num(r.get("similarity"))
        bits = [f"[{r.get('engine')}]", str(r.get("title") or "（无标题）")]
        if sim:
            bits.append(f"相似度 {sim:.0f}%")
        if r.get("author"):
            bits.append(f"作者 {r['author']}")
        if r.get("extra", {}).get("episode"):
            bits.append(f"第 {r['extra']['episode']} 集")
        if r.get("url"):
            bits.append(str(r["url"]))
        lines.append("- " + "｜".join(bits))
    return "\n".join(lines)
