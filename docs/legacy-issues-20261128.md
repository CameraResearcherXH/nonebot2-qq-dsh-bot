# 遗留问题（2026-11-28 闭环验证发现）

> 状态：**待处理**。两条都不影响正常回复，先记录，等用户拍板再改。
> 证据来自 2026-11-28 线上 diag 账本 + eco 模式闭环回放。

## L1. 主动开口（view）几乎永远不通过

**现象**：2026-11-28 全天 `view` 事件 53 次，`pass` 0 次。

| gate | 次数 | 含义 |
|---|---|---|
| `decide` | 26 | 相关度 0.00（`话题和她关系不大`）直接否掉 |
| `cap` | 24 | 群日上限已满（日志 `n=3 lim=3`） |
| `freq` | 3 | 查看频率（ViewFrequency）未放行 |

**位置**：`plugins/chat/__init__.py` 群主动接话 tick（6529–6593 行），
相关度在 `decide` 前算，`proactive.GROUP_MAX_PER_DAY` 在频率闸门之后。

**可能原因**：
- 相关度算法对日常闲聊给 0.00 过于严格（`rel` 只来自少量关键词/记忆命中）。
- `cap` 日志 `n=3 lim=3` 说明每个 tick 都在撞日上限，即上限按「tick 次数」
  而不是「实际开口次数」计数，或 mark 后未正确跨天清零——需要核对
  `proactive.group_count_today` / `mark_group_sent`。

**建议**：先离线用回放样本评估 `rel` 分布，再决定是调阈值/加成
（`_ext = _ext * (1+4*rel)`），还是修 cap 计数。

**当前影响**：eco 模式下 proactive 整体被 `power_on("proactive")` 关掉，
不再产生消耗；full 模式下则每 tick 空跑判定。

## L2. 记忆写入带〔引用…〕脚手架

**现象**：回合记忆（`services/memdb.py` / `memory_v2.py` 的 `write`）里存进
了用于拼 prompt 的引用脚手架，例如：

```
我一个钢板的不懂。 〔引用 大肥鱼(9194) 16:02 的消息（只作指代，回复时不要复述原文）：这叫丰满，懂不懂〕
```

**位置**：脚手架在 `plugins/chat/__init__.py:1466` 拼接（`_quote_block`），
随后混入 `group_scene` 历史；写入路径直接取历史文本落库
（`memdb.remember` / `memory_v2` 的 `write`）。

**影响**：记忆检索命中后会把脚手架原样带进 prompt，浪费 token，且可能让
模型把「只作指代」当正文。LLM 压缩链路（Qwen/RAG）不保证能剔干净。

**建议**：在写入边界（记住/落库前）剥掉 `〔引用 …（只作指代，回复时不要复述原文）：…〕`
整段，只留用户原话；或落库时保存 `text` 与 `quote` 两个字段。

**验证方法**：回放后 `grep data/memory.db` 或 `diag write` 的 about 样本；
修复后跑 `scripts/replay_closed_loop.py --buckets addr` 复核。
