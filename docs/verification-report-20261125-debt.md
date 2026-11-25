# 验证报告：三项工程债闭环 + 参数真接线（2026-11-25 22:15）

> 范围：上一轮遗留的 3 项工程债（`check_design_docs` 语义、`ADDR_RECENT_MENTION` 零引用、
> `check_tuning` 非 AST），以及顺带查出的 5 个「读了不用 / 从未实现」的调参键。
> 结论：**全部闭环**；容器内 `verify_all` 9/9、`verify_rollbacks` 19/0、
> 部署后日志回滚 6 PASS / 0 FAIL / 13 SKIP。

---

## 1. 三项工程债

### 1.1 `check_tuning`：正则 → AST

| 项 | 旧版 | 新版 |
|---|---|---|
| 键引用识别 | 正则 `tuning\.(f\|i)\("X"\)` | `ast.parse` 找真实 Call 节点（含 `from core import tuning as _t` 别名、`from core.tuning import f`） |
| 假接线判定 | 文件里出现 `"X"` 字符串就算接线 | 只认**字符串字面量**，注释 / docstring 不算（`_docstring_ids`） |
| 新增规则 5 | — | 模块级 `NAME = tuning.f("KEY")` 必须真被用到（同文件 Load，或跨模块 `模块.NAME`）；`LEGACY` / `UNWIRED` 豁免 |
| 数值型 env | 正则 | AST：`float/int(os.getenv("X", "1.2"))` |
| 版本兼容 | — | 宿主 py3.8 无法解析 3.9+ 装饰器时，该文件降级正则并在结论里列出（容器 3.11 走完整 AST） |

**立即抓到的真问题**（旧版全部漏报）：

| 常量 | 键 | 处理 |
|---|---|---|
| `group_scene.ADDR_RECENT_MENTION` | `ADDR_RECENT_MENTION` | 接线：短句冲人推断的两处硬编码 `0.55 / 0.5` 改用键值 |
| `lorebook.SCAN_DEPTH` | `LORE_SCAN_DEPTH` | 接线：`scan()` 只匹配最近 N 行（SillyTavern depth 语义） |
| `llm_usage.KEEP_DAYS` | `LLM_USAGE_KEEP_DAYS` | 接线：`init()` 启动时删除超期明细（docstring 一直这么承诺） |
| `relationship.INTIMACY_HL_D` | `REL_INTIMACY_HALF_LIFE_DAYS` | 接线：移到 `profiles.intimacy()`（`0.5 ** (age/HL)`），`FILES` 改指向 |
| `memory_v2.RET_MEMORY_TOP` | `RET_MEMORY_TOP` | 接线：`build_packet` 记忆块默认条数，`FILES` 改指向 `prompt_builder` |
| `mood_judge.PASSIVE_MAX_DELTA` | `MOOD_PASSIVE_MAX_DELTA` | 删除（ADR-038 已删被动通道） |
| `config.MAX_REPLY_CHARS` | `MAX_REPLY_CHARS` | 删除（从来没有读点；`architecture-map B18` 早已标「死配置」） |
| `chat.SHORT_QUERY_WINDOW_SEC` | `SHORT_QUERY_WINDOW_SEC` | 删除（短查询窗口从未实现） |

调参键总数 420 → **418**（删 2 个；另外 3 个改归属）。

### 1.2 `check_design_docs`：加 `--runtime`

默认模式仍然只做「文档 ↔ 设计常量」自洽检查（保持 CI 快、不受 `.env` 影响）。
新增 `--runtime`：

1. **接线证明**：`design_constants.KEYS` 里 120 个设计键逐个查 AST 读点，
   没有读点且不在 `UNWIRED` → FAIL。实测 **120/120 全部有真实读点**。
2. **线上覆盖不变式**（读 `.env` 覆盖后的 live 值）：
   - `PROMPT_BUDGET ≥ budget_total()`（2120）
   - `CTX_STYLE_TOTAL × 1.5 ≥ style_total()`（730 字）
   - 每个块 `CTX_<BLOCK> × scale ≥ BUDGET[block]`
   - `CTX_ENFORCE ∈ {0,1}`
3. 覆盖清单（默认值 → 线上值）只提示不判错。

容器内实测：`PASS`（无任何块被线上覆盖压到设计目标以下）。

### 1.3 `ADDR_RECENT_MENTION` 零引用

已并入 1.1 的接线。现在 `to_reason="recent_mention"` 的置信度来自调参键，
`segments.addressee_of` 的三档渲染（≥0.8 / 0.5~0.8 / <0.5）随之可控。

---

## 2. 顺带修掉的宿主兼容问题

| 问题 | 影响 | 修法 |
|---|---|---|
| `plugins/chat/__init__.py` 两处 `@get_driver().on_bot_connect` | 3.9+ 语法，宿主 py3.8 的 `ast.parse` 整文件失败 → `check_names` 崩、`check_tuning` 降级 | 提成模块级 `_driver = get_driver()`，改 `@_driver.on_bot_connect`（行为不变） |
| `scripts/check_names.py` 引用 `ast.MatchAs` | py3.8 直接 `AttributeError` | `getattr(ast, "MatchAs", ())` 兜底 |

结果：宿主 3.8 也能跑 `check_names` / `check_tuning` 的完整 AST 路径；
容器 3.11 行为不变。

---

## 3. 容器内验证（真实环境）

```text
docker exec qq-grok-bot python scripts/verify_all.py --full
  语法编译 PASS｜依赖方向 PASS｜调参表 PASS｜名字检查 PASS｜挂载检查 PASS
  接线检查 PASS｜功能链路 PASS｜新功能单测 PASS｜第三方插件合规 PASS
  全链路自检 PASS（1801 项 / 0 失败 / 4 跳过）
  结论：✅ 全部通过（10/10）

docker exec qq-grok-bot python scripts/verify_rollbacks.py      → 19 项 / FAIL 0
docker exec qq-grok-bot python scripts/test_new_features.py     → 222 PASS / 0 FAIL
docker exec qq-grok-bot python scripts/audit_features.py --strict → 52 项 / FAIL 0
docker exec qq-grok-bot python scripts/check_tuning.py --strict → PASS（418 参数，AST）
docker exec qq-grok-bot python scripts/check_design_docs.py --runtime --strict → PASS
md5sum -c deploy_manifest.txt → 112 个文件全绿
```

### 3.0 顺带修好「全链路自检」：22 项失败 → 0

`verify_all --full` 一跑就暴露了 22 条**测试比实现慢半拍**的断言（都是前几轮
改设计后没同步的），其中 3 条是**真 bug**，不是测试问题：

| 类型 | 问题 | 处理 |
|---|---|---|
| 真 bug | `memdb.recall_catalog` 两个分支都没有 `no`（目录编号），json 后端有、sqlite 没有 | 补 `no = offset+i+1` |
| 真 bug | `memdb.remember` **丢掉调用方显式传的 `tags`**（只留自动派生），`weekly_apply` 写的 `weekly_check` 全丢 | 显式 tags 以 `custom` 类型并入 |
| 真 bug | `journal_agent` / `weekly` 直接读 `index.json` → sqlite 后端永远「0 条记忆」 | 新增 `memory_v2.list_items()`（按后端分派）+ `memdb.list_memories()`；详情改 `recall_detail()` |
| 测试漂移 | 状态块/backfill/被动 mind（ADR-038 已删） | 改成「来自 THINK」「不再补写」「旁观不落盘」 |
| 测试漂移 | 遗忘曲线断言「情绪越强越慢」（已改 kind τ） | 改成 `fact` 比 `event` 衰减慢 |
| 测试漂移 | `20` 字铁律（已改 40 字）、`GROUP_TOOL_DENY` 条数（top-N 会补位）、`napcat_call` 永远在列表里（top-N 会挤掉）、判读 `delta=-0.46`（现按 impact 推导） | 按现设计改断言 |
| 测试漂移 | `_fake` 不接受新增的 `src` 参数 → 整个 moodjudge 组崩 | 加 `src=""` + `**kw` |
| 测试卫生 | `agent_client.recall` 有 10 分钟缓存，两个用例用同一个 query → 互相污染 | 用例前 `_CACHE.clear()` |

### 3.1 真实日志回滚

```bash
# ⚠️ 必须用 docker logs --since 截窗口：
#    [RECALL] 角色脑 … 这类行是 print 出来的，没有 "11-25 HH:MM" 前缀，
#    --after 过滤不到它们，会把部署前的慢样本算进来。
docker logs --since 12m qq-grok-bot 2>&1 \
  | docker exec -i qq-grok-bot python scripts/verify_napcat_log.py --log - --data-dir data
```

结果：**6 PASS / 0 FAIL / 13 SKIP**（SKIP = 这段日志没覆盖到，不是失败）。
首次用 8h 窗口跑出现 1 FAIL（S4b 角色脑 55~72ms），逐条查时间戳后确认
全部发生在 21:51~22:00（部署前），部署后 0 条超 50ms。

### 3.2 参数传达抽查（`scripts/dump_state.py`）

```text
心情：{"value": 9.27, "v": 0.0927, "P": 0.026, "judge_n": 127}
status_block：· 心情：平平的（+0.09）
              · 刚才那下：替他有点心酸
              · 你心里那句话：他这句自嘲听着有点心酸。   ← 是她的 THINK，不是判读猜测
```

---

## 4. 新增/修改的断言（避免「测试比实现慢半拍」）

| 位置 | 旧断言 | 新断言 |
|---|---|---|
| `test_new_features.py` | `MOOD_PASSIVE_MAX_DELTA == 0.03` | 该键已不存在（ADR-038 删通道） |
| `audit_features.py` G16 | `PASSIVE_MAX_DELTA in mood_judge` | `if passive:` 早退 + 「只记录不写状态」；残留该键则报错 |

---

## 5. 剩余（不影响运行）

- `RET_FUSION_RRF` 默认仍关（等向量路 / 线上 A/B）。
- `intimacy` 饱和曲线已上线，7 个顶满 1.00 的历史档案会随半衰期自然回落，继续观察。
- 图书馆分区去重 / 向量召回仍需算力机。
