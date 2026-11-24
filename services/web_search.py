# services/web_search.py
import os
import asyncio
from nonebot import get_driver, logger, require

from .text import strip_html


async def fetch_web_search(query: str) -> str:
    """借用 tavily_search 插件的智能搜索功能，带 10 秒强制熔断"""
    logger.info(f"[WebSearch] 开始处理搜索请求: {query}")

    try:
        tavily_plugin = require("nonebot_plugin_tavily_search")
        smart_web_search = tavily_plugin.smart_web_search
    except Exception as e:
        logger.error(f"[WebSearch] ❌ 无法加载 tavily 插件: {e}")
        return ""

    driver_config = get_driver().config
    tavily_key = getattr(driver_config, "tavily_api_key", os.getenv("TAVILY_API_KEY", ""))
    serper_key = getattr(driver_config, "serper_api_key", os.getenv("SERPER_API_KEY", ""))

    if not tavily_key and not serper_key:
        logger.warning("[WebSearch] ⚠️ 未配置 TAVILY_API_KEY，无法搜索！")
        return ""

    try:
        async def _do_search():
            return await smart_web_search(
                query=query,
                tavily_key=tavily_key,
                serper_key=serper_key,
                max_results=3,
                search_depth="basic"
            )

        response = await asyncio.wait_for(_do_search(), timeout=10.0)
        logger.info(f"[WebSearch] ✅ Tavily 返回成功，数据来源: {response.source}")

        snippets = []

        # ⚠️ 所有字段都要过 strip_html：
        #    Tavily 的 answer / content 常带 <br>、<b>，直接进 prompt 会被模型抄进回复
        # 1. 先放综合摘要
        answer = strip_html(response.answer or "")
        if answer:
            snippets.append(f"综合摘要：{answer}")

        # 2. 再放详细的搜索结果文本（标题 + 内容）
        for i, r in enumerate(response.results, 1):
            title = strip_html(r.title or "")
            body = strip_html(r.content or "")
            content = body if len(body) <= 200 else body[:200] + "..."
            snippets.append(f"{i}. {title}\n   摘要：{content}")

        # 3. 把文本拼接起来
        result_text = "\n".join(snippets)

        # 4. 提取图片（如果有的话）
        image_urls = []
        if hasattr(response, 'raw') and response.raw:
            for img_url in response.raw.get("images", []):
                if isinstance(img_url, str):
                    image_urls.append(img_url)
                elif isinstance(img_url, dict) and "url" in img_url:
                    image_urls.append(img_url["url"])

        if image_urls:
            result_text += f"\n\n【相关图片链接】\n" + "\n".join(image_urls[:2]) # 最多带2张

        return result_text

    except asyncio.TimeoutError:
        logger.error("[WebSearch] ❌ Tavily 搜索超时（超过10秒），放弃本次搜索。")
        return ""
    except Exception as e:
        logger.error(f"[WebSearch] ❌ 搜索过程中发生异常: {type(e).__name__} - {e}")
        return ""

