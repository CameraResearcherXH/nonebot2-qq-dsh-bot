# 线上验证 Runbook（NapCat 日志 ↔ 设计文档）

> 上级：[DESIGN.md](DESIGN.md) §0.1
> 相关：[causal-assertions.md](causal-assertions.md)、[emotion-dynamics-design.md](emotion-dynamics-design.md)
> 本文回答：**合入服务器、扫码登录之后，怎么用日志证明「代码真的按文档在跑」**
> 不回答：参数怎么调（见 [tuning-guide.md](tuning-guide.md)）

---

## 1. 什么时候跑

部署完成 → NapCat 扫码登录 → **至少跑 30~60 分钟**，并让群里发生这几类事：

| 要覆盖的行为 | 为什么 |
|---|---|
| @她 / 引用她 | 触发 `ADDR_AT_OR_QUOTE`、快速通道护栏、LITE 硬信号 |
| 有人夸她 / 骂她 | `impact ±1/±2`、`trust/respect` 事件 |
| 连续 8 条以上群聊 | 判读窗口 `split_windows`（P4） |
| 她回一条超过 2 泡 | `OUT_BUBBLES_MAX` 合并转发 |
| 私聊一句 | 私聊半衰期（2h）与群聊（30min）分开 |

> `POWER_MODE=eco` 或 `GROUP_WATCH_MODE=lite` 时，审核/图书馆/主动/表达会被跳过，
> 相关断言会自然 **SKIP** —— 这是预期，不是失败。

---

## 2. 三步

```bash
# ① 拉日志（容器名按实际改）
docker logs --since 2h qq-grok-bot > /tmp/bot.log 2>&1

# ② 验证（日志 + data/ 一起看）
python3 scripts/verify_napcat_log.py --log /tmp/bot.log

# ③ 看报告
cat docs/verification-report-$(date +%Y%m%d).md
```

脚本退出码：`0` = 没有 FAIL（SKIP 不算失败）；`1` = 有 FAIL。

> ⚠️ `docker logs` 是**按容器**存的：`--force-recreate` 之后旧容器的日志就看不到了。
> 每次重建前先把日志落盘（`docker logs qq-grok-bot > /tmp/before_restart.log`），
> 或用 `--log a.log --log b.log` 合并多份。
> 日志会丢，但 `data/mood_history.jsonl` 不会 —— 所以 M1/M10 还有 `M1h/M10h` 两条
> **data 级**补证（按 session 看同向观测是否单调、步长是否递减）。

没有服务器时先验证脚本本身：

```bash
python3 scripts/verify_napcat_log.py --self-test
```

---

## 3. 断言映射（FAIL 先看这张表）

| 断言 | 文档 | 日志证据 | FAIL 说明 | 先查哪里 |
|---|---|---|---|---|
| **M1** | emotion §3.1 | `[MOOD-JUDGE] ... impact → v` | 同向事件把 `v` 推反了 | `mood.observe` 的 `z` 方向 / `impact` 解析 |
| **M3** | emotion §3.1 | 同上，按 `v` 分桶 | 接近 ±1 还在大步走 | `MOOD_SATURATION` 是否乘进 `dv` |
| **M4** | emotion §3.1 | 同上，按 `conf` 分桶 | 低置信也大幅移动 | `R = R_BASE + R_SLOPE×(1−conf)²` |
| **M7** | emotion §3.1 | `data/mood_history.jsonl` | 单次波动 >0.40 | 锚点/步进 clamp |
| **M8** | DESIGN §9 红线 1 例外 | `[FAST] ... → v=-0.45` | 护栏把 `v` 抬上去了 | `mood.fast_guard` 只能 `min()` |
| **M10** | emotion §3.1 | 连续同向 `Δv` 序列 | `Δv` 不降反升 | `P` 是否随观测下降（K 衰减） |
| **M4b** | emotion §5.1 | `relevance x < gate → 只衰减` | 低相关窗口仍然步进 | `MOOD_RELEVANCE_GATE` 分支 |
| **P4** | causal P4 | `[MOOD-JUDGE] 段 N 条 → M 窗` | 40 条只判 1 窗（前 32 条丢了） | `mood_judge.split_windows` / 预算丢窗顺序 |
| **O4/O5** | DESIGN §9 红线 1 | `[OUT] 气泡 N 个 > 2`、`气泡=N` | 超过 2 泡没合并 / 又被 20 字截断 | `OUT_BUBBLES_MAX`、`OUT_BUBBLE_CHARS` |
| **R1/R2/R6** | emotion §3.2 | `[REL] qq trust+1→0.25` | trust/respect 越界 / 没有写入口 | `services/relationship.py` |
| **G36** | features §20 | `[LITE] 硬信号命中 / 不是在叫她` | lite 完全等于 full（门没生效） | `chat._need_reply` 里 lite 块的**缩进** |
| **S4b** | retrieval §7 | `[RECALL] 角色脑 Xms > 目标 50ms` | 角色脑超过 50ms 目标 | `memory_v2.recall` / sqlite 索引 |
| **M0** | emotion §3.1 | `data/mood.json` | `value`/`P`/`label` 越界 | `_clamp` / `_p_of` |

> 表里没有的断言（R3/R4/R5/S1~S7/O1~O3）需要**构造场景**或跑
> `scripts/test_new_features.py` 的合成用例；日志只能证明「这次线上真的走到了」。

---

## 4. 报告怎么读

```
结论：12 PASS / 1 FAIL / 4 SKIP
```

- **PASS**：日志里出现了符合文档的证据。
- **FAIL**：出现了反例 —— 先按 §3 的「先查哪里」定位。
- **SKIP**：这段日志没覆盖到（比如没人骂她、没触发主动插话）。
  **SKIP 不是通过**，只是这次没测到；要补覆盖就按 §1 制造场景再拉一次。

---

## 5. 数据文件对照

| 文件 | 证明什么 | 断言 |
|---|---|---|
| `data/mood.json` | `v` / `P` / `label` 值域、作用域半衰期 | M0 / M2 |
| `data/mood_history.jsonl` | 每次波动幅度、锚点纠偏 | M7 |
| `data/relationship.json` | trust / respect 值域 | R1 |
| `data/relationship_log.jsonl` | 每次关系事件的步长与方向 | R2 / R6 |
| `data/mood_audit.jsonl`（如启用） | 判读输入/输出对账 | M1 / M10 |

---

## 6. 注意

1. **日志含聊天内容**：报告只写统计和断言，不把原文贴进仓库；需要贴证据时先脱敏。
2. **不要用 `--since` 太短**：判读是 8 条/5min 窗口，10 分钟日志可能一个窗口都没凑齐。
3. **改了 `.env` 要重建容器**：`docker compose up -d --force-recreate bot`，否则新参数不生效。
4. 报告默认写进 `docs/`，每次一份带日期；**不要覆盖旧报告**，方便对比改动前后。

---

## 7. 2026-11-25 新增：回滚 / p95 / 位置评测

```bash
# 回滚开关（每个用例独立子进程，验证 env 真的改变行为）
sudo docker compose exec -T bot python scripts/verify_rollbacks.py

# ADR-008：mood.json 并发写不丢更新（8 线程压力测试）
sudo docker compose exec -T bot python scripts/verify_state_lock.py

# P6 情绪标注评分（label 为空时会提示先标注）
sudo docker compose exec -T bot python scripts/eval_emotion_labels.py

# 图书馆同步：先 dry-run 看增量/去重，再决定是否 --build（会上传算力机）
sudo docker compose exec -T bot python scripts/library_sync.py --dry-run --dir /app/data/dialogue

# 块级上下文预算：/测函数 上下文（各块实际字数 vs CTX_*，默认只统计不裁剪）
# 想让超限真的裁剪，再设 CTX_ENFORCE=1

# p95 周报（流水线各步 + 模型调用；只优化最慢一块）
sudo docker compose exec -T bot python scripts/p95_report.py --days 7

# 位置评测：gold 放头/中/尾，看 lost_in_middle（中 vs 尾差 <10% 才 PASS）
python scripts/eval_gold.py --stage position --limit 10 --topk 5
python scripts/eval_replay.py run --set data/eval/position_replay.jsonl \
  --deterministic --out data/eval/position_replay_out.jsonl
python scripts/eval_replay.py judge --set data/eval/position_replay_out.jsonl \
  --out data/eval/position_judged.jsonl
python scripts/position_report.py
```

---

## 8. 2026-11-27 新增：意图层（need_reply）离线评测

`ATMO_GATE` 默认 0（关）。**要开闸门前**必须跑一次，准确率 ≥85% 且正例召回 ≥85%：

```bash
# 不花钱，先确认脚本/解析/打分本身没问题
sudo docker compose exec -T bot python scripts/eval_intent.py --mock

# 真模型：用当天 trace 重放（--neg-per-trace 1 约 200 条样本，成本很低）
sudo docker compose exec -T bot python scripts/eval_intent.py \
  --trace data/llm_trace/2026-11-27.jsonl --limit 200 --neg-per-trace 1 \
  --concurrency 8 --out data/eval/intent_1127.jsonl
```

看三行就够：`准确率` / `正例召回` / `精确率`；判错样本会列出来，**先人工看是不是弱标签噪音**
（正文点名「鱼宝/大肥鱼」但那一轮她没回，这种 label 本身就不对）。
2026-11-27 首轮：192 条 → 96.9% / 100% / 94.8%（6 条误报里 4 条是标签噪音）。

> 位置评测要先把证据撑到接近线上预算（5×~240 字）；90 字的上下文太短，
> 头/中/尾几乎测不出差异。
>
> 本轮完整结果见 [verification-report-20261125-p17.md](verification-report-20261125-p17.md)。
