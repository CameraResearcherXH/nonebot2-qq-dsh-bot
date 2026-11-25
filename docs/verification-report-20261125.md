# NapCat 日志 ↔ 设计文档 对接验证报告

- 生成时间：2026-11-25 20:45:43
- 日志来源：`/tmp/bot3h.log`
- 结论：**2 PASS / 0 FAIL / 17 SKIP**

> SKIP = 这段日志没有覆盖到（比如没触发主动插话），不代表失败。

| 断言 | 文档 | 证据 | 结果 |
|---|---|---|---|
| M1 | emotion §3.1 单调累积 | 没有 impact≠0 的相邻判读记录 | SKIP |
| M10 | emotion §3.1 K 随 P 下降 | 没有连续同向记录 | SKIP |
| M4 | emotion §3.1 R 用 confidence | 样本不足（需要 conf≤0.4 和 ≥0.8 各一条） | SKIP |
| M3 | emotion §3.1 saturation | 样本不足（需要 v 接近 ±1 的记录） | SKIP |
| M8 | DESIGN §9 红线 1 例外 | 没有触发快速护栏（没有 @她+强负面） | SKIP |
| P4 | causal P4 段内消息不丢 | 没有分段判读记录 | SKIP |
| P4b | emotion §5.1 预算内丢最早窗 | 没有分段记录 | SKIP |
| O4 | DESIGN §9 红线 1 气泡 | 没有超过 2 泡的回复 | SKIP |
| O5 | DESIGN §9 红线 1 不截断 | 最大气泡数 2（上限 2） | PASS |
| R2 | emotion §3.2 trust/respect 写入口 | 没有 trust/respect 事件（普通闲聊不会触发） | SKIP |
| G36 | features §20 省流模式 | GROUP_WATCH_MODE=full（没走省流分支） | SKIP |
| S4b | retrieval §7 角色脑 ≤50ms | 后端=json（50ms 只对 sqlite 生效） | SKIP |
| M4b | emotion §5.1 relevance gate | 没有触发 relevance 闸门 | SKIP |
| M0 | emotion §3.1 状态值域 | 9 条状态；值域正常 | PASS |
| R1 | emotion §3.2 关系值域 | 没有 relationship.json | SKIP |
| R6 | emotion §3.2 日上限/步长 | 没有 relationship_log.jsonl | SKIP |
| M7 | emotion §3.1 锚点/步长有界 | 没有新链路波动记录（旧 llm-judge 不计） | SKIP |
| M1h | emotion §3.1 单调累积（data） | 没有同向观测 | SKIP |
| M10h | emotion §3.1 步长递减（data） | 没有 ≥3 次观测的会话 | SKIP |

## 复跑

```bash
docker logs --since 2h qq-grok-bot > /tmp/bot.log 2>&1
python3 scripts/verify_napcat_log.py --log /tmp/bot.log
```
