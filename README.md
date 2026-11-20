# qq-dsh-bot

一个跑在 QQ 上的角色扮演机器人：有记忆、有心情、有对人的好感，能自己判断「该不该接话」。

> **技术栈**：本项目基于 **NoneBot2** 框架开发，通过 **OneBot v11** 协议接入 QQ，
> 默认使用 **NapCatQQ** 作为 QQ 协议端。核心运行链路是
> `NapCatQQ → OneBot v11 → NoneBot2 → 本项目的 core / services / plugins`。

> 本仓库是**开源发行仓**（由源仓 `qq-grok-bot` 生成），不含任何私密数据、密钥或私人人格卡。

## 特性

- **记忆**：每个 turn 一条目录项，按时间累积；情绪刻痕（Δ）决定加粗与检索优先级；可展开心理活动与原文。
- **心情**：现在进行时，按会话；由第三方判读的 Δ 累加；名词按 -1~1 每 0.2 一档。
- **峰值事件**：显示最近 2 小时「还没被反向波动抵消」的最大涨/跌事件，并带当时的心理活动。
- **好感**：分人的慢变量，只被「某个人导致的心情变化」推动；有每日净变化上限；带好感名词和「印象最深的一件事」。
- **群心情**：独立于整体心情的 5 小时窗口；统计「多少人喜欢你 / 讨厌你」，决定愿不愿意在这个群多说话。
- **关系底色**：看到某个人时，会想起「跟他有关的开心/难受的事」，再决定这一句怎么说。
- **群聊现场**：消息带「谁 → 谁」，按线程聚合；能判断谁在叫她、哪句不是对她说的。
- **安静 / 破冰**：可以整群安静，也可以只屏蔽某个人（含 @ 和图片）；道歉会回暖。
- **能力开关**：`/技能` 一键关掉除核心外的所有能力（搜索/画图/表情/主动开口…）。
- **用量归因**：`/用量` 按用途给出 token 花费与 Top/Bottom，知道钱花在哪。
- **插件商城**：把 nonebot 插件放进 `plugins/third/`，用 `plugin.toml` 声明能力即可变成 function calling skill。
- **算力机**：画图 / RVC 翻唱 / IndexTTS 语音 / 视频理解交给 `node.py`，服务器只发任务。

## 快速开始

1. 安装 Python 3.11+，`pip install -r requirements.txt`
2. 复制配置：`cp .env.example .env`，至少填：
   - `SUPERUSERS` / `OWNER_QQ`（主人）
   - `ALLOWED_GROUPS` / `ALLOWED_PRIVATE`（允许的群和私聊）
   - `XAI_BASE_URL` / `XAI_API_KEY` / `XAI_MODEL`（主对话模型）
3. 启动 NapCat（QQ 协议端），监听 `ws://127.0.0.1:3001`
4. 启动机器人：`python bot.py`

Docker 用户：`docker compose up -d`（`data/` 与 `.env` 用挂载，不打进镜像）。

## 算力机（可选，画图 / 翻唱 / 语音）

如果你要用 `/画图`、`/翻唱`、语音消息，需要一台有 GPU 的机器跑 `node.py`：

```bash
cp node.env.example node.env     # 填 API_SECRET / 模型路径 / 服务地址
python3 node.py --check          # 依赖自检
python3 node.py                  # 启动
```

文件：`node.py`、`node.env.example`、`node_config/`、`comfy_workflows/`、
`my_kkI_rvc.py`、`rvc_pipeline.py`。完整部署见 [docs/node-compute.md](docs/node-compute.md)。

## 常用命令

| 命令 | 作用 |
|---|---|
| `/技能` | 查看能力开关 |
| `/技能 关 全部` | 关掉所有非核心能力（急停） |
| `/技能 开 搜索,画图` | 按组开启 |
| `/用量` | token 用量与 Top/Bottom |
| `/安静` / `/说话` | 让她安静 / 解除 |
| `/档案 [@某人]` | 查看人物卡 |
| `/心情` | 查看当前心情 |
| `/记忆统计` | 记忆条数与分布 |
| `/persona list` / `/persona 名字` | 查看 / 切换人格 |

更多命令见 [docs/features.md](docs/features.md)。

## 目录结构

```
core/              内核：tuning（全部参数）/ pipeline / config / llm_gateway
services/          领域服务：记忆、心情、好感、群心情、提示词、插件注册…
plugins/           功能插件（chat / vision_agent / draw / song_cover / …）
plugins/third/     第三方插件（商城安装目录）
scripts/           工具：make_dist / verify_all / audit_features / 迁移…
docs/              文档：功能总览、设计、算力机、人格卡、第三方合规
comfy_workflows/   算力机 ComfyUI workflow 模板
node_config/       算力机画图 prompt 配方
node.py            算力机主程序
my_kkI_rvc.py      RVC 单文件推理入口
rvc_pipeline.py    RVC 音域对齐 / 变调 / 混音流水线
```

依赖方向：`plugins → services → core`（`scripts/check_deps.py` 会检查）。

## 文档

从 [docs/README.md](docs/README.md) 开始：

- [功能总览](docs/features.md)：每个能力是干嘛的、怎么用
- [心情 / 好感 / 群心情设计](docs/mood-affinity-design.md)
- [算力机部署](docs/node-compute.md)
- [人格卡格式](docs/persona-card.md)
- [全部参数](docs/tuning.md)
- [第三方插件来源](docs/third-party-audit.md)
- [思路参考与致谢](docs/CREDITS.md)

## 插件开发

见 `docs/plugin-dev.md`。最小示例：

```toml
[plugin]
name = "hello"
version = "0.1.0"
description = "示例插件"

[[tools]]
name = "hello_world"
description = "说一句 hello"
level = "safe"
handler = "plugins.third.hello:hello_world"
scope = ["group", "private"]
```

## 致谢

- 用户对话学习：参考 **MaiBot** 的思路（独立实现；MaiBot 为 GPL-3.0）
- 群聊规则 / 档案卡 / World Info / 人格卡字段：参考 **SillyTavern（AI 酒馆）** 的思路
  （独立实现；SillyTavern 为 AGPL-3.0）
- 详见 [docs/CREDITS.md](docs/CREDITS.md) 与 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)

## 许可证

见 [LICENSE](LICENSE)。
