import httpx
from nonebot.log import logger
from .models import TavilySearchResult, SearchResponse

TAVILY_URL = "https://api.tavily.com/search"
SERPER_URL = "https://google.serper.dev/search"

async def tavily_search(
    query: str,
    api_key: str,
    max_results: int = 5,
    search_depth: str = "basic",
) -> SearchResponse:
    """调用 Tavily Search API"""
    payload = {
        "query": query,
        "max_results": max_results,
        "search_depth": search_depth,
        "include_answer": True,      # 让 Tavily 直接给一段摘要，方便喂给 DeepSeek
        "include_raw_content": False, # 不需要原始 HTML，节省 token
        "include_images": True,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(TAVILY_URL, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    results = [
        TavilySearchResult(
            title=r.get("title", ""),
            url=r.get("url", ""),
            content=r.get("content", ""),
            score=r.get("score", 0.0),
            raw_content=r.get("raw_content"),
        )
        for r in data.get("results", [])
    ]

    return SearchResponse(
        source="tavily",
        query=query,
        answer=data.get("answer"),
        results=results,
        raw=data,
    )


async def serper_search(
    query: str,
    api_key: str,
    max_results: int = 5,
) -> SearchResponse:
    """调用 Serper API（Google SERP），作为 Tavily 的兜底"""
    payload = {"q": query, "num": max_results}
    headers = {
        "X-API-KEY": api_key,
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(SERPER_URL, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    results = [
        TavilySearchResult(
            title=r.get("title", ""),
            url=r.get("link", ""),
            content=r.get("snippet", ""),
            score=1.0,  # Serper 不返回相关性分数，统一给 1.0
        )
        for r in data.get("organic", [])
    ]

    return SearchResponse(
        source="serper",
        query=query,
        results=results,
        raw=data,
    )


async def smart_web_search(
    query: str,
    tavily_key: str,
    serper_key: str = "",
    max_results: int = 5,
    search_depth: str = "basic",
) -> SearchResponse:
    """
    智能搜索路由：
    1. 优先 Tavily
    2. Tavily 额度耗尽（429/402）、超时、报错时，降级到 Serper
    3. 两者都失败则抛出异常
    """
    # ---- 第一优先级：Tavily ----
    try:
        logger.info(f"[Tavily] 搜索: {query}")
        return await tavily_search(
            query=query,
            api_key=tavily_key,
            max_results=max_results,
            search_depth=search_depth,
        )
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (402, 429):
            logger.warning(f"[Tavily] 额度耗尽或限流 ({e.response.status_code})，切换 Serper")
        else:
            logger.warning(f"[Tavily] HTTP 错误 {e.response.status_code}，切换 Serper")
    except Exception as e:
        logger.warning(f"[Tavily] 搜索失败: {e}，切换 Serper")

    # ---- 第二优先级：Serper 兜底 ----
    if not serper_key:
        logger.error("[Serper] 未配置 API Key，无法兜底")
        raise RuntimeError("Tavily 搜索失败，且未配置 Serper 兜底")

    logger.info(f"[Serper] 兜底搜索: {query}")
    return await serper_search(
        query=query,
        api_key=serper_key,
        max_results=max_results,
    )
