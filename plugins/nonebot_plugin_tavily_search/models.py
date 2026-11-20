from pydantic import BaseModel
from typing import Optional, List, Dict, Any

class TavilySearchResult(BaseModel):
    title: str
    url: str
    content: str
    score: float
    raw_content: Optional[str] = None

class SearchResponse(BaseModel):
    source: str  # "tavily" 或 "serper"
    query: str
    answer: Optional[str] = None  # Tavily 的 include_answer 合成答案
    results: List[TavilySearchResult] = []
    raw: Optional[Dict[str, Any]] = None  # 原始响应，便于调试
