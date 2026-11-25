# 第十七轮验证报告（2026-11-25）

> 范围：检索二期（倒 U / τ_kind / MMR）、注意力位置指标、p95 周报、切换
> `MEMORY_BACKEND=sqlite`。
> 环境：`qqbot`（127.0.0.1）`/home/admin/qq-grok-bot`，容器 `qq-grok-bot`。
> 日志：合并 `/tmp/before_*.log`、`/tmp/bot_*.log`、`/tmp/pre_restart*.log`、
> `/tmp/post*.log` + 当前 `docker logs`，共 **14,390 行**。

---

## 0. 结论

| 项 | 结果 |
|---|---|
| 本地全量自检 `verify_all` | **9/9 PASS** |
| 文档功能审计 `audit_features` | **52/52 PASS** |
| 调参表 `check_tuning` | **PASS（414 个参数，UNWIRED 19）** |
| 回滚开关体检 `verify_rollbacks` | **19/19 PASS** |
| 线上日志（全部历史） | **12 PASS / 2 FAIL / 5 SKIP**（2 FAIL 均来自 16:05~16:31 旧版本日志，见 §4） |
| 线上日志（当前版本 18:24 之后） | **6 PASS / 0 FAIL / 13 SKIP** |
| 角色脑延迟（sqlite，20 次/查询） | p95 **22~34ms**（目标 ≤50ms） |
| 位置评测（10 题 × 3 位置，≈1k token 上下文） | 头 90% / 中 100% / 尾 100%，`lost_in_middle = 0.0pp` **PASS** |
| 记忆后端 | `BACKEND=sqlite`，json 6126 / sqlite 6136，最近 turn_id 全覆盖 |

---

## 1. 代码与文档一致性

```bash
python scripts/verify_all.py
python scripts/audit_features.py --strict
python scripts/check_tuning.py --strict
```

- `verify_all`：语法编译 / 依赖方向 / 调参表 / 名字检查 / 挂载检查 / 接线检查 /
  功能链路 / 新功能单测 / 第三方插件合规 全部通过。
- `audit_features`：52 项功能断言 0 FAIL。
- `check_tuning`：新增 `MEM_*` / `PIPE_*` / `STYLE_MMR_*` / `RET_W_SALIENCE`
  全部有中文说明、有使用点、FILES 映射正确。

---

## 2. 回滚开关体检（19/19 PASS）

脚本：`scripts/verify_rollbacks.py`（每个用例独立子进程，避免模块级常量缓存）。

| 开关 | 验证点 | 结果 |
|---|---|---|
| `MEMORY_BACKEND=json` | `BACKEND=json`，`stats()` 读 JSON（6126） | PASS |
| `MEMORY_BACKEND=sqlite` | `BACKEND=sqlite`，`stats()` 读 sqlite（6136） | PASS |
| `MEM_MMR_ENABLED=0` | 3 条近重复占满 top3（纯分数） | PASS |
| `MEM_MMR_ENABLED=1` | 近重复被降权，多样性条目进入 top3 | PASS |
| `MEM_SAL_TAIL` | 1.0 冲击从 0.6 可回滚到 0.9 | PASS |
| `MEM_TAU_FACT` | fact 半衰期可从 365 改回 30 天 | PASS |
| `MOOD_UPDATE=v2 / delta` | 新链路 / 旧口径都能加载 | PASS |
| `POWER_MODE=full / eco` | eco 关 audit、保留 memory | PASS |
| `GROUP_WATCH_MODE=lite` | 常量切到 lite | PASS |
| `PIPE_METRICS=0` | 指标落盘关闭 | PASS |
| `RET_TOOLS_TOP=0 / 2` | 0=全挂（8 个假工具），2=只留 2 个 | PASS |
| `LONG_DOC_ENABLED=0 / 1` | 0=不识别长答，1=识别 | PASS |
| `MEMORY_DUAL_WRITE=0` | 双写关闭 | PASS |
| `WHITE_MOON_BORROW=0` | 只给事实行、不借句式 | PASS |

线上命令：

```bash
sudo docker compose exec -T bot python scripts/verify_rollbacks.py
```

---

## 3. 算法传值生效（代码级）

### 3.1 显著性倒 U

`memdb.salience_weight(u)` 实测：

| u | 0.1 | 0.3 | 0.6 | 1.0 |
|---|---|---|---|---|
| w | 0.03 | 0.36 | 0.99 | 0.60 |

- 小冲击（u<0.15）几乎不记；中等最显著；极端回落到 0.6。
- `storage_strength = 0.15 + 0.45w + 0.20×unexpected + 0.20×relation_relevance`，
  负性 ×1.15。

### 3.2 τ_kind

`memdb.tau_days()`：event 30 / bond 90 / fact 365 / preference 180 / style 365 天；
json 与 sqlite 两个后端同口径。

### 3.3 MMR

`_diversify` 用「3 条近重复 + 1 条多样性」验证：

- `MEM_MMR_ENABLED=0` → `['a b c','a b c','a b c']`
- `MEM_MMR_ENABLED=1` → `['a b c','x y z','a b c']`

### 3.4 角色脑延迟（sqlite）

线上容器内每查询 20 次：

| 查询长度 | p50 | p95 | max |
|---|---|---|---|
| 5 字 | 13.7ms | 28.0ms | 28.0ms |
| 27 字 | 33.5ms | 34.0ms | 34.0ms |
| 27 字（另一条） | 33.7ms | 34.5ms | 34.5ms |
| 10 字 | 21.5ms | 22.2ms | 22.2ms |

优化点：
- MMR 前先缓存每条候选的 token 集合（避免 O(k²) 次 `split`）；
- 检索候选从固定 400 裁到 `max(48, limit×8)`；
- 最终选中的才 `note_hits()` 回补提取强度。

---

## 4. 线上日志验证

### 4.1 全部历史日志（14,390 行）

结论：**12 PASS / 2 FAIL / 5 SKIP**。

2 个 FAIL 都是旧版本日志，不是当前代码：

| 断言 | FAIL 证据 | 归因 |
|---|---|---|
| O5 气泡 ≤2 | 16:05:20 `[LAT] ... 气泡=3` | 该版本 `[LAT]` 记的是合并前数量；17:17 之后同一场景已记 `气泡=2` |
| S4b 角色脑 ≤50ms | 16:05~16:31 有 895~1081ms | 当时还是 json 后端 / 未做候选裁剪；当前 sqlite p95 22~34ms |

M1 曾因「跨小时日志直接相邻比较」误报；已修正为**先按半衰期把 a 的值衰减到 b 时刻再比方向**，
修正后 **9 对同向记录方向全部一致**。

### 4.2 当前版本日志（18:24 之后）

结论：**6 PASS / 0 FAIL / 13 SKIP**。

- O4：1 次超泡合并，cap=2；
- O5：最大气泡数 2（上限 2）；
- S4b：无超 50ms 召回；
- M0/R1/R6/M7/M1h/M10h 全部 PASS。
- 其余 SKIP 是这段日志没覆盖（没有 @+强负面、没有连续同向判读等）。

复跑：

```bash
docker logs --since 5m qq-grok-bot > data/tmp_cur.log
sudo docker compose exec -T bot python scripts/verify_napcat_log.py \
  --log /app/data/tmp_cur.log --after "11-25 18:24"
```

---

## 5. 位置评测（gold_position / lost_in_middle）

10 题 × 头/中/尾 = 30 条，证据 5×~240 字（≈1k token）：

```bash
python scripts/eval_gold.py --stage position --limit 10 --topk 5 \
  --emit-replay data/eval/position_replay2.jsonl
python scripts/eval_replay.py run --set data/eval/position_replay2.jsonl \
  --deterministic --concurrency 4 --out data/eval/position_replay2_out.jsonl
python scripts/eval_replay.py judge --set data/eval/position_replay2_out.jsonl \
  --out data/eval/position_judged2.jsonl
python scripts/position_report.py --set data/eval/position_judged2.jsonl
```

结果：

| 位置 | n | 准确率 |
|---|---|---|
| head | 10 | 90.0% |
| mid | 10 | 100.0% |
| tail | 10 | 100.0% |

- `lost_in_middle`（中 vs 尾）：**0.0 个百分点 → PASS**（目标 <10）
- 头 vs 尾：10.0 个百分点（头反而略差，符合「最相关放最后」）
- 同批 judge：相关性 4.43/5、贴题 4.43/5、标记泄漏 0%。

---

## 6. p95 与记忆后端

### 6.1 p95

- 每轮写 `data/metrics/pipeline_stages.jsonl`（超 8MB 只留最近 5000 行）。
- 线上真实样本：整轮 p95 11.8s，最慢 **图书馆 10.7s**，其次总结 753ms、记忆 156ms。
- `/测函数 流水线` 显示每步 p50/p95 并标「最慢」。
- 周报第七节自动带 p95；独立入口 `python scripts/p95_report.py --days 7 --write`。

### 6.2 sqlite 切换

- 切前：json active 6122 / sqlite 6134 / 最近 122 条 turn_id 全覆盖。
- 切后：`BACKEND=sqlite`，`stats()` total 6130+；写入 → patch → 召回自检通过。
- 补洞：`revisit_candidate` / `health_report` 增加 sqlite 分支。
- 回滚：`.env` 改 `MEMORY_BACKEND=json`；备份 `/tmp/env.bak_p17`、`/tmp/memory.db.bak_p17`。

---

## 7. 仍未完成（文档里明确标二期/暂缓，不是本轮回归）

| 项 | 状态 | 说明 |
|---|---|---|
| 向量检索 + RRF + 重排（R4） | RRF 框架已接，向量仍缺 | `RET_FUSION_RRF=1` 可开六路 RRF；200 条自检索：加权 R@1 0.300 / R@5 0.405，RRF R@1 0.240 / R@5 0.430；默认关，向量路需算力机。`RET_LIB_LATENCY_MS / RET_STYLE_TOP / RET_WORLD_TOP` 已接线，UNWIRED 清零 |
| 图书馆分区 / 去重 / 增量（R3） | 服务器侧上传前已完成增量/去重/分区标记；索引侧仍需算力机 | `library_sync.py --dry-run`：17 文件 3.1MB，去重 4271/35922 行（12%）；sha256 未变跳过 |
| ADR-004 事件研究（实际−预期） | 二期 | 一期先把观测/滤波跑通；公式需要先定「预期」口径 |
| ADR-008 单一状态源 | ✅ 已完成（2026-11-25） | `mood._serialized` 串行写入口 + `_save` flock；8 线程压力测试 40/40 不丢更新 |
| ADR-025 本地识图角色库 | 暂缓 | bot 无 GPU，合规风险（**需算力机**） |
| P6 情绪效标校准（QWK） | 工具已就绪，待人工标注 | `scripts/eval_emotion_labels.py` 可算 QWK/准确率/ECE；200 条 label 仍为空 |
| 15 个块级 `CTX_*` | ✅ 已接线并校准 | 默认值按真实块长（字符/1.5）校准：persona 700 / protocol 1500 / state 240 / post_rules 320 / rhythm 180 / white_moon 180 / style_total 650；`CTX_ENFORCE=1` 只裁异常膨胀；`/测函数 上下文` 可看实际/预算 |

> 这些都不影响本轮 1~4 项的上线效果；它们是需要单独排期的「二期/暂缓」项。
> **R3/R4/ADR-025 需要算力机**（图书馆入库/分区/去重/向量、GPU 识图）；
> ADR-004 / ADR-008 / CTX_* / P6 评分都是服务器侧，不需要算力机。
