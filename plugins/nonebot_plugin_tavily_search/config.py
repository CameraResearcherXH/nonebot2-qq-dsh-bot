from pydantic import BaseModel, Extra

class Config(BaseModel, extra=Extra.ignore):
    tavily_api_key: str = "[REDACTED_SECRET]"          # Tavily API Key
    serper_api_key: str = ""          # Serper API Key（预留，可为空）
    tavily_max_results: int = 5       # 每次搜索返回条数
    tavily_search_depth: str = "basic" # "basic" 或 "advanced"（advanced 消耗双倍 credits）
