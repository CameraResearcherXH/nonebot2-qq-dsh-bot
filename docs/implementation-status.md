# 实施状态总表（2026-11-20）

> 这份文件回答一个问题：**你要求的那些事，到底做了没有、在哪、怎么验证。**
> 每条都给出「证据」——能自己跑一遍的命令，不靠嘴说。

## 0. 一页结论

| # | 你的要求 | 状态 | 证据 / 位置 |
|---|---|---|---|
| 1 | `~/hzy/qq-dsh-bot` 开源发行仓，不带 data | ✅ 已生成 | `python3 scripts/make_dist.py --force`；868 文件，`data/`、`.env`、`node.py` 均不在 |
| 2 | 每个阶段完成 + 全部完成后都同步一次 | ✅ 已成流程 | `scripts/make_dist.py` 可重复执行；`docs/implementation-status.md`（本文件）随每阶段更新 |
| 3 | 工程结构化（依赖单向、不交叉） | ✅ 骨架完成 | `core/`（pipeline / config / llm_gateway / tuning）；`scripts/check_deps.py --strict` |
| 4 | 去掉我们的 API 与冗余 debug 注释 | ✅ 自动脱敏 | `make_dist` 从 `.env` 读真实密钥值做精确替换；`scripts/strip_private_comments.py` 出清理清单 |
| 5 | 说明文档（能上手） | ✅ | `README.md`、`docs/tuning.md`、`docs/plugin-dev.md`、`CONTRIBUTING.md`、`docs/CREDITS.md` |
| 6 | 插件商城（third 目录 + 自动变 function calling） | ✅ 骨架完成 | `services/plugin_registry.py`、`scripts/plugin_cli.py`、`plugin.toml` 规范见 `docs/plugin-dev.md` |
| 7 | 三方插件来源标注 | ✅ 9/9 | `plugins/third/*/UPSTREAM.md`、`THIRD_PARTY_NOTICES.md`、`docs/third-party-audit.md` |
| 8 | 致谢 maimai / AI 酒馆 | ✅ | `docs/CREDITS.md`，README 有「致谢」段 |
| 9 | 核心外全部 skill 开关宏 | ✅ | `services/skill_switch.py`、`/技能`、工具执行闸门（`services/llm.py:_skill_ok`） |
| 10 | token 用量监测 + 最大/最小归因 | ✅ | `services/llm_usage.py`、`/用量`、13 处 `purpose` 埋点 |
| 11 | 全部阈值收口到一个 tuning 文件 | ✅ | `core/tuning.py`（213 项）、`docs/tuning.md`、`/调参` |
| 12 | node 参数：重复 / 没用上 / 该保留 | ✅ 只读审计 | `docs/node-params.md`（node.py 未改） |
| 13 | 全部改动 diff 供合入 | ✅ | `patches/all.patch` + `patches/full/` + `patches/MANIFEST.md` |

## 1. 架构改动后「功能完好」怎么保证

新增 `scripts/verify_all.py`，一条命令跑 6 道闸门：

```bash
python3 scripts/verify_all.py
```

| 闸门 | 管什么 | 当前结果 |
|---|---|---|
| 语法编译 | 所有模块能 import（语法级） | PASS |
| 依赖方向 | `plugins → services → core` 不被打破 | PASS（历史遗留 32 条记入 baseline，新增即失败） |
| 调参表 | 阈值全部收口、没有写错的键、都 import 了 | PASS（213 项） |
| 名字检查 | 「用了但没定义」的全局名（重构删常量的典型事故） | PASS（0 处） |
| 挂载检查 | 新增顶层包（如 `core/`）没挂进容器 | PASS |
| 接线检查 | 孤立模块 / 工具没接线 / 处理器断线 / 命令重复 / 占位 / 开关没覆盖 | PASS（见 `docs/full-audit-2026-11-20.md`） |
| 功能链路 | 主动接话 / 安静 / 破冰 / 回复判定 / 表情 / 记忆召回 的断点是否真的修了 | PASS（19 项） |
| 新功能单测 | 记忆 sqlite + json 两条腿、Δ 记录、开关、用量、插件扫描 | PASS（25 项） |
| 三方插件合规 | 每个 third 插件都有 UPSTREAM | PASS（9/9） |

另外还有一套更全的离线回归（`scripts/selftest_all.py`，`verify_all.py --full` 会带上）：
**1713 项检查，0 失败，12 跳过**（跳过的都是「本机没装 fastapi/requests」这类环境项，服务器上会真跑）。

### 本轮「检验」补掉的半截改动

| 问题 | 后果 | 修复 |
|---|---|---|
| `services/memory_v2.py` 的 `BACKEND` 常量被误删 | **默认（json）后端下所有记忆操作直接 NameError**：remember/recall/stats 全废，而且因为包在 try 里，日志只留 debug | 恢复 `BACKEND` 定义；新增 `scripts/check_names.py` 专抓这类问题 |
| `mood_judge.judge_and_store` 缺 `turn_id` 参数 | Δ→salience/valence/好感 的整段逻辑永远抛 NameError 被吞掉 → **记忆加粗、好感涨落从没生效过** | 补上参数；`test_new_features` 增加 json 后端覆盖 |

| 问题 | 后果 | 修复 |
|---|---|---|
| `_WORKER_BUSY[session_key] = True` 没有对应的 `finally` 复位 | 第一次回复后，该会话的**主动插话永久失效** | `_session_worker` 加 `finally: _WORKER_BUSY[...] = False`，并加检查项 `A3b` |
| `mood.touch()` 没清 `last_decay_at` | 刚聊完 10 分钟，心情会**一步衰减到 0** | `touch()` 里 `pop("last_decay_at")`，并加检查项 `G8` |
| `mood.touch()` 只挂在 Rule 上 | `vision_agent` 等 block 插件命中时，心情空闲计时不刷新 | 移到 `priority=1` 的 `_early_observe`，并加检查项 `A4` |

这三条现在都在 `scripts/audit_features.py` 里，改回去会立刻 FAIL。

## 2. 调参（tuning）

- 唯一来源：`core/tuning.py` 的 `DEFAULTS`（213 项，全部带中文说明 + 生效文件）。
- 代码里已经**没有** `float(os.getenv("X","1.0"))` 这种写法，统一 `tuning.f("X")` / `tuning.i("X")`。
- 环境变量同名覆盖仍然有效（`.env` 不用改）。
- 人看的文档：`docs/tuning.md`（`python3 scripts/dump_tuning.py --write` 重新生成）。
- 群里看：`/调参`、`/调参 关键词`；只看被 `.env` 改过的：`python3 scripts/dump_tuning.py --changed`。

## 3. 开源发行仓

```bash
python3 scripts/make_dist.py --force          # 默认输出 ~/hzy/qq-dsh-bot
```

做的事：白名单复制 → 精确脱敏（`.env` 里的真实密钥值逐条替换）→ 生成 `.env.example`（339 键）/ `personality.example.txt` → 副本内 compileall + 依赖检查 → 写 `DIST_REPORT.md` → 清理 `__pycache__`。

排除项：`data/`、`.env`、`node.py`、`node.env`、`napcat/`、`plugins_backup/`、`patches/`、`__pycache__/`、私人笔记（`3a_zh.txt` 等）、过期文档（`docs/agent-pipeline.md`）。

## 4. 合入用 patch

| 文件 | 内容 |
|---|---|
| `patches/all.patch` | 35 个改造文件的 unified diff + 34 个新增文件（69 段，可直接 `git apply`） |
| `patches/full/` | 10 个「无改前快照」的文件整份内容（只有机械改动，整文件覆盖即可） |
| `patches/MANIFEST.md` | 逐文件状态与行数 |

已验证：在「改前」副本上 `patch -p1` 应用后，69 个文件与当前代码**逐字节一致**（0 处不一致）。

## 5. 仍然欠着的（明确列出，不含糊）

1. `plugins/chat/__init__.py` 仍是 8600+ 行；`core/pipeline` 只做了注册点，`_session_worker` / 命令 / 工具**没有物理拆分**。
2. `services/llm.py` 仍反向 import `plugins.chat`（32 条历史交叉依赖在 `scripts/dep_baseline.txt`，需逐步消除）。
3. 三方插件的上游地址 / 许可证多为「待确认」，开源前必须人工核对（GPL/AGPL 需隔离）。
4. 生产库未迁移：`scripts/migrate_memory_json.py` 已就绪，但要在服务器执行并设 `MEMORY_BACKEND=sqlite`。
5. `node.py` 与 `data/` 位置按约定**未动**；`docs/node-params.md` 里的 7 个「两端必须一致」参数需要人工对齐（已发现 `RVC_VOICE_F0_MIN/MAX` 两边值不同）。

## 6. 上线后 10 分钟自检清单（本地跑不了 nonebot，必须在服务器做一次）

本地只能做静态检查（`verify_all.py`），真正的运行时验证要在部署后按顺序看：

| # | 动作 | 期望 |
|---|---|---|
| 1 | 看启动日志有没有 `[CORE] 注册失败` | 没有；有就说明 `core` 导入有问题 |
| 2 | 群里 @ 她说一句话 | 正常回复，且日志有 `[TURN]` 或 `[MOOD-JUDGE]` |
| 3 | 群里发一张图（不 @） | `vision_agent` 命中，且她的上下文里**有**这条消息（D1 修复点） |
| 4 | 私聊她说「安静」→ 再发三条 | 三条都不入队（日志 `[SWITCH] 私聊安静中 → 不入队`），且不烧 token |
| 5 | 然后说「对不起」 | 日志 `[SWITCH] 破冰`，之后恢复回复 |
| 6 | 群里连发 5 条 | 不会每条都回（`REPLY_INTERVAL` 生效） |
| 7 | 等 15 分钟不说话，再看 `/调参 MOOD_IDLE` | 心情值向 0 走了，且没有「一步到 0」 |
| 8 | 发一个表情包（群里） | 日志 `[STICKER] 强度 ±x.xx`；低于 0.25 会写「不上心，不收」 |
| 9 | `/技能` `/技能 关 搜索` `/技能 开 搜索` | 列表正确；关掉后她说「这个能力被主人关掉了」 |
| 10 | `/用量` | 有按 purpose 的排行，且能看到 Top 最贵 |
| 11 | 静置 30 分钟，看有没有主动插话 | 有（说明 `_WORKER_BUSY` 复位正常；这是本轮修的 bug） |

> 第 11 条是本轮最容易回归的点：如果主动插话彻底不出现，先查 `_WORKER_BUSY`。
