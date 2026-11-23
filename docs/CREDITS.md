# 致谢与思路来源

本项目是独立实现。下面列出**思路参考**（不是代码复制），以及依赖的开源项目。

## 思路参考

### maimai

- 参考点：**用户对话学习**——把聊天里的经历沉淀成可检索的记忆，再反过来影响对话。
- 本项目的落地：`services/memory_v2.py`（记忆目录 + 展开）、`services/memdb.py`（sqlite 存储）、
  `services/mood_judge.py`（心情名词 + 心理活动 + Δ）。
- 说明：仅参考设计思路，代码为独立编写。

### SillyTavern（AI 酒馆）

- 参考点：**群聊规则、档案卡、World Info 触发、at-depth 注入**。
- 本项目的落地：`services/profiles.py`（人物卡 / 关系 / 相处记录）、
  `services/prompt_builder.py`（分块 + 预算 + focus_card at-depth）、
  `services/lorebook.py`（关键词触发词条）。
- 说明：仅参考设计思路，代码为独立编写。

## 开源依赖

- [nonebot2](https://github.com/nonebot/nonebot2) 与 [OneBot v11 适配器](https://github.com/nonebot/adapter-onebot)
- [NapCatQQ](https://github.com/NapNeko/NapCatQQ)（QQ 协议端）
- 各第三方插件作者，见 `plugins/third/*/UPSTREAM.md` 与 `THIRD_PARTY_NOTICES.md`

如果你认为某处参考应当更明确地署名，请提 Issue，我们会补充。
