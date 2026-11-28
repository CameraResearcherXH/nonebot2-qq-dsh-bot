# 身份记忆 + 小模型预处理 方案（2026-11-28）

> 状态：方案待整体设计定稿（结合 `E:\deepseek\help\` 两参考项目 MaiBot / dsh-aurabot-qq 的 docs 分析）。
> 关联：[feature-expectation-audit-20261128.md](feature-expectation-audit-20261128.md) §5、[DESIGN.md](DESIGN.md)、[retrieval-design.md](retrieval-design.md)、[addressing-design.md](addressing-design.md)。

## 0. 触发案例（用户真机测试，拉最新 GitHub 部署后）

用户教「肥牛=雨夜带刀不带伞的小强」，她口头答应「贴标签」；随后问「淫魔是谁/奏奏是谁/把淫魔@出来」，
她先脑补「淫魔=肥牛」、再改口「奏奏=主人」、最后「本鱼不认识肥牛」。脱离 8/16 句窗口后映射全丢。

## 1. 诊断（对着代码核过，非模型能力问题）

1. **没有「当场写别名」的函数工具**：她有 `who`（查人），但无 alias-write 工具。
   「记住了」只停留在回复文本，`肥牛→QQ` 未落盘；后台 `profile_miner` 异步挖别名不可靠。
   映射只活在 `MAX_GROUP_RECENT`(16) 窗口里，滑出即忘。
2. **黑话未定义就脑补**：「淫魔」从未被教过，她自行等号；无可靠来源时跟着滑动上下文猜。
3. **问人不先查档案**：「X是谁/把X@出来」应先硬查 `profiles.aliases`+`group_roster`，
   查到注入一行事实、查不到注入「未找到」；现缺此步，只能即兴。
4. **工具动态 top-N 排不进去**：`RET_TOOLS_TOP` 按相关度挑工具，查人/写别名常落选 → 「给了工具不用」。

## 2. 修复方案（硬逻辑旁路，不进 prompt、不增主模型负担）

- **A. 硬逻辑别名学习器**：识别明确教学句式「X就是Y / X=Y / 记住X是Y」→
  `group_roster` 解析 Y→QQ → `profiles.upsert_profile(aliases+=X)` 直接落盘。
  类比既有「破冰回暖」硬逻辑，零 LLM 成本。**直接治「记住了却忘」。**
- **B. 问人先查、事实注入**：匹配「X是谁 / 把X@出来」→ 硬查别名/roster →
  注入「X=群名片(QQ)」或「未找到X」一行事实。止住脑补。
- **C.（可选，接 §3）** 小模型抽实体+教学意图，让 A/B 触发更稳。

## 3. 小模型预处理：骨架已存在 = services/atmosphere.py

- 现状：接话前读最近 8 句+当前句，temperature=0 独立小请求，输出
  `topic/asks/need_reply/why/replied/keywords/queries{memory,library,web}` 驱动检索；
  走 `providers.resolve("audit")`（与收发件人审核共用）；async + `ATMO_TIMEOUT` + TTL 缓存 + 失败降级。
- 本地 Ollama 已装 `qwen2.5:7b`(4.7GB)，无需另拉。
- **接本地（零代码）**：`AUDIT_PROVIDER=custom / AUDIT_BASE_URL=http://127.0.0.1:11434/v1 /
  AUDIT_MODEL=qwen2.5:7b / AUDIT_API_KEY=ollama`。⚠️ audit 与审核共用 → **建议单开 `atmo` 角色**隔离。
- **补实体环**：输出加 `entities:[{name,qq?}]`，小模型抽人名后硬解析注入事实；
  识别教学意图触发 §2-A。
- **异步/算力机联动**：与主回复 `asyncio.gather` 并行；超时/算力机关→退回现状不阻塞；
  Ollama `keep_alive` 调短、空闲卸载，避免与 ComfyUI/RVC 抢显存（qwen2.5:7b 量化≈5GB）。

## 4. §5 待拍板建议（待用户确认）

| 项 | 建议 |
|---|---|
| ② replied-guard | 冷却内「只提到她」终止不单独开轮；@/喊名照常 |
| ③ 视频理解 | 先显式降级告知（同画图 note 套路），完整实现排后续 |
| ④ 线程重启恢复 | dialogue replay 为唯一口径，删 write-only threads 表 |
| ⑤ archive 迁移 | 写入时增量 + 定时 purge 保留 90 天 |

## 5. node.py（compute_node）已落地修复

secret→`X-Node-Secret` 头、心跳独立守护线程、demucs 三元组、
`process_cover_full_task` 变调人声 `vocal_path`→`vocal_local` bug、画图降级 note。
备份 `compute_node/node.py.bak_20261128_205436`，用户自行运行验证。