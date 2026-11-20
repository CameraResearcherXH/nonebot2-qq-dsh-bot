# core/__init__.py
"""内核层：pipeline（一条消息的一生）/ config（配置）/ llm_gateway（模型出口）。

依赖方向（硬规则）：plugins → services → core，禁止反向。
- core 不 import services / plugins。
- services 可以 import core。
- plugins 只能通过公开 API 使用 services/core。
"""
__all__ = ["pipeline", "config", "llm_gateway"]
