# services/picsearch_tool.py
"""以图搜源的工具实现（`plugins/third/picsearch/plugin.toml` 里的 handler）。

⚠️ 2026-11-22 真机事故：这个函数以前写在 `plugins/third/picsearch/__init__.py`。
`plugin_registry.register_all()` 在 chat 插件 import 时就会 `importlib` 解析 handler，
等于**抢在 nonebot 之前**把 `plugins.third.picsearch` 导入了；
随后 `nonebot.load_plugins("plugins/third")` 看到它已在 sys.modules 里，直接抛
`RuntimeError: Module plugins.third.picsearch is not loaded as a plugin!`

所以实现放 services（依赖方向也正好是 plugins → services），
`plugins/third/picsearch/` 只留元数据，不再被提前 import。
"""
from __future__ import annotations

try:
    from nonebot import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)


async def picsearch(args: dict) -> str:
    """给一张图片直链，综合 TraceMoe / SauceNAO / Ascii2D / IQDB 查出处。"""
    url = str((args or {}).get("image_url") or "").strip()
    if not url:
        return "没给图片链接。让用户发一张图，或把图片直链传进来。"
    try:
        import httpx
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
            r = await c.get(url)
        if r.status_code != 200 or len(r.content) < 1024:
            return "图片没下载下来（链接可能过期了），让他重发一次。"
    except Exception as e:
        return f"图片下载失败：{type(e).__name__}: {e}"
    try:
        from . import image_search
        results = await image_search.search(r.content)
    except Exception as e:
        return f"搜图失败：{type(e).__name__}: {e}"
    if not results:
        return ("综合搜图（TraceMoe/SauceNAO/Ascii2D/IQDB）都没搜到："
                "可能不是动漫/插画截图，或者画面太糊/太碎。别硬编出处。")
    body = image_search.format_results(results, limit=8)
    return ("综合搜图结果（按相似度排序）：\n" + body
            + "\n\n说清楚是哪个源、把握大不大；相似度低就只当线索，别当事实。")
