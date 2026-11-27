# NapCat 日志 ↔ 设计文档 对接验证报告

- 生成时间：2026-11-27 10:46:32
- 日志来源：`/tmp/bot_win.log`
- 结论：**10 PASS / 0 FAIL / 9 SKIP**

> SKIP = 这段日志没有覆盖到（比如没触发主动插话），不代表失败。

| 断言 | 文档 | 证据 | 结果 |
|---|---|---|---|
| M1 | emotion §3.1 单调累积 | 没有 impact≠0 的相邻判读记录 | SKIP |
| M10 | emotion §3.1 K 随 P 下降 | 没有连续同向记录 | SKIP |
| M4 | emotion §3.1 R 用 confidence | 样本不足（需要 conf≤0.4 和 ≥0.8 各一条） | SKIP |
| M3 | emotion §3.1 saturation | 样本不足（需要 v 接近 ±1 的记录） | SKIP |
| M8 | DESIGN §9 红线 1 例外 | 没有触发快速护栏（没有 @她+强负面） | SKIP |
| P4 | causal P4 段内消息不丢 | 26 个段全部覆盖（窗大小 8，预算截断已排除） | PASS |
| P4b | emotion §5.1 预算内丢最早窗 | 见 [MOOD-JUDGE] 段 N 条 → M 窗 | PASS |
| O4 | DESIGN §9 红线 1 气泡 | 28 次超泡合并，cap=2 | PASS |
| O5 | DESIGN §9 红线 1 不截断 | 最大气泡数 3（文字上限 2，+1 给表情/语音例外） | PASS |
| R2 | emotion §3.2 trust/respect 写入口 | 39 条关系事件，值域 0.17~0.59 | PASS |
| G36 | features §20 省流模式 | GROUP_WATCH_MODE=full（没走省流分支） | SKIP |
| S4b | retrieval §7 角色脑 ≤50ms | 32 次超 50ms（性能观测项，不判定） | SKIP |
| M4b | emotion §5.1 relevance gate | 10 次 relevance 跳过 | PASS |
| M0 | emotion §3.1 状态值域 | 26 条状态；值域正常 | PASS |
| R1 | emotion §3.2 关系值域 | 12 人；值域正常 | PASS |
| R6 | emotion §3.2 日上限/步长 | 47 条事件；异常 0 | PASS |
| M7 | emotion §3.1 锚点/步长有界 | 新链路 655 条波动；单次 >0.40 的 0 条 | PASS |
| M1h | emotion §3.1 单调累积（data） | 没有同向观测 | SKIP |
| M10h | emotion §3.1 步长递减（data） | 没有 ≥3 次观测的会话 | SKIP |

## 复跑

```bash
docker logs --since 2h qq-grok-bot > /tmp/bot.log 2>&1
python3 scripts/verify_napcat_log.py --log /tmp/bot.log
```
