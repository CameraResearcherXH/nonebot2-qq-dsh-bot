# UPSTREAM

upstream: 本项目自建（薄封装，调用 vendor/PicImageSearch）
author: qq-grok-bot
license: 与本项目一致
version_or_commit: 0.1.0
modified: no
modification: 新写的薄封装，只负责下载图片 + 调用 PicImageSearch 聚合搜索；
工具实现放在 services/picsearch_tool.py（避免被 plugin_registry 提前 import）
