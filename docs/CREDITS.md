# 致谢与思路来源

本项目是**独立实现**，基于 **NoneBot2** 框架开发，通过 **OneBot v11** 接入 QQ，
默认使用 **NapCatQQ** 作为 QQ 协议端。下面列出思路参考（不是代码复制）、开源依赖，
以及第三方插件来源。

> ⚠️ 重要：MaiBot 与 SillyTavern 均为 **GPL/AGPL 系** 项目。
> 本项目**没有复制它们的代码**，只参考公开的设计思路；如果你要合并它们的代码，
> 必须遵守对应许可证（GPL-3.0 / AGPL-3.0）。

---

## 一、思路参考

### 1. MaiBot（麦麦）

- 仓库：https://github.com/Mai-with-u/MaiBot
- 许可证：GPL-3.0
- 参考点：
  - **人设 / 记忆 / 关系分层**：角色是谁、记得什么、对谁什么态度，分开存；
  - **长期记忆**：聊天经历沉淀成可检索的记忆，再反过来影响对话；
  - **关系状态**：对不同人有不同的熟悉度和态度。
- 本项目落地：
  - 人设：`services/Personality/*.txt`
  - 记忆：`services/memory_v2.py`、`services/memdb.py`
  - 关系：`services/profiles.py`（好感 / 亲近 / 相处印象 / 印象最深的一件事）
  - 分层注入：`services/prompt_builder.py`
- 说明：仅参考设计思路，代码为独立编写。分析笔记见 [maibot-analysis.md](maibot-analysis.md)。

### 2. SillyTavern（AI 酒馆）

- 仓库：https://github.com/SillyTavern/SillyTavern
- 许可证：AGPL-3.0
- 参考点：
  - **群聊规则 / 档案卡**：角色卡字段（description / personality / scenario / mes_example / system_prompt）；
  - **World Info / Lorebook**：关键词触发的世界书词条；
  - **at-depth 注入**：把「当前说话人卡片」插到当前消息上方，而不是全塞 system prompt。
- 本项目落地：
  - 人物卡 / 关系：`services/profiles.py`
  - 分块 + 预算 + focus_card：`services/prompt_builder.py`
  - 关键词词条：`services/lorebook.py`
  - 人格卡字段对照：[persona-card.md](persona-card.md)
- 说明：仅参考设计思路，代码为独立编写。

### 3. maimai

- 参考点：**用户对话学习**——把聊天里的经历沉淀成可检索的记忆，再反过来影响对话。
- 本项目落地：`services/memory_v2.py`（记忆目录 + 展开）、`services/memdb.py`（sqlite 存储）、
  `services/mood_judge.py`（心情名词 + 心理活动 + Δ）。
- 说明：仅参考设计思路，代码为独立编写。

### 4. 人格卡参考

- 字段结构参考 SillyTavern character card v2（`description` / `personality` / `scenario` /
  `mes_example` / `system_prompt` / `post_history_instructions`）。
- 本项目使用纯文本分段格式，映射表见 [persona-card.md](persona-card.md)。
- 发行仓只提供通用模板 `personality.example.txt`，不包含任何私人人格卡。

---

## 二、开源依赖

| 依赖 | 用途 | 许可证 |
|---|---|---|
| [nonebot2](https://github.com/nonebot/nonebot2) | 机器人框架 | MIT |
| [nonebot-adapter-onebot](https://github.com/nonebot/adapter-onebot) | OneBot v11 适配 | MIT |
| [NapCatQQ](https://github.com/NapNeko/NapCatQQ) | QQ 协议端 | 见上游 |
| [ComfyUI](https://github.com/comfyanonymous/ComfyUI) | 画图工作流服务 | GPL-3.0 |
| [RVC](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI) | 声音转换 | MIT |
| [Demucs](https://github.com/facebookresearch/demucs) | 人声/伴奏分离 | MIT |
| [IndexTTS](https://github.com/index-tts/index-tts) | 本地语音合成 | 见上游 |
| [ffmpeg](https://ffmpeg.org/) | 音视频处理 | LGPL/GPL（按构建） |

> 以上依赖不随本仓库打包，需要自行安装。它们的许可证以各自上游为准。

---

## 三、第三方 nonebot 插件

`plugins/third/` 下的插件来自社区，每个目录都有 `UPSTREAM.md`（上游地址 / 作者 / 许可证 / 是否修改）。
完整清单与核对状态见：

- [third-party-audit.md](third-party-audit.md)
- [../THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)

已知来源（截至 2026-11-20）：

| 插件 | 上游 | 许可证 | 修改 |
|---|---|---|---|
| `bittorrents` | https://github.com/NightDust981989/nonebot-plugin-BitTorrents | 见上游 | 否 |
| `girls_2d` | https://github.com/zhuhiki/nonebot-plugin-2dgirls | 见目录 LICENSE | 否 |
| `nonebot_plugin_rollpig_plus` | https://github.com/Felis2026/nonebot-plugin-rollpig-plus | 见上游 | 否 |
| `petpet` | https://github.com/MeetWq/nonebot-plugin-petpet | MIT | 否 |
| `pixivrank_search` | https://github.com/HibiKier/nonebot_plugin_pixivrank_search | 见上游 | 否 |
| `steam_judger` | https://github.com/Duogeyu/nonebot_steam_judger | 见上游 | 否 |
| `gspanel` | 待人工确认 | 未声明 | 否 |
| `jinriyunshi` | 待人工确认 | 未声明 | 否 |
| `VideoZoomer_master` | 待人工确认（volce） | 见目录 LICENSE / Notice.txt | 否 |

> 未确认上游/许可证的插件，发布前必须人工核对；GPL/AGPL 插件需要评估隔离或移除。

---

## 四、如果认为署名不准确

请提 Issue，附上插件目录、上游链接和许可证，我们会补充或调整。
