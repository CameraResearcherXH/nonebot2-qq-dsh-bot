# 验证报告：心情只来自 THINK（部署 + 回滚 + 设计一致性）

日期：2026-11-25 20:30｜范围：心情链路 P0 修复后的线上验证 + 全部设计文档回滚检查

---

## 1. 部署

| 文件 | 本地 md5 | 服务器 md5 |
|---|---|---|
| `services/mood.py` | `fd5fc02f…` | 一致 |
| `services/mood_judge.py` | `fd3daaf4…` | 一致 |
| `plugins/chat/__init__.py` | `332989a4…` | 一致 |
| `deploy_manifest.txt` | 已更新 4 项 | 已同步 |

- `docker compose up -d --force-recreate bot` 成功；启动 98 行日志无 Traceback / ERROR。
- 人格正常加载：`默认人格: 小鲸鱼proplus（917 字）`。
- **新行为已在线上出现**：

```
[MOOD-JUDGE] group_100000013 旁观窗口（无 THINK）→ 不写心情
[MOOD-JUDGE] group_100000016 旁观窗口（无 THINK）→ 不写心情
```

（以前这些窗口会按群聊改 `v`。）

---

## 2. 回滚与闸门

| 检查 | 结果 |
|---|---|
| `verify_rollbacks.py` | **19 项 / FAIL 0**（4 项 SKIP：本机没装 nonebot，容器内会跑） |
| `verify_all.py` | **9/9 PASS**（编译/依赖/调参/名字/挂载/接线/功能/单测/三方） |
| `test_new_features.py` | **201 PASS / 0 FAIL**（「被动观察」已改为「不写心情」） |
| `test_regressions.py` | 43 PASS / 5 FAIL（4 个 nonebot 缺失 + 1 个「情绪刻痕 vs 中性」历史失败，与本链路无关） |
| `check_design_docs.py --strict` | **PASS**（修了预算口径，见 §5） |
| `verify_napcat_log.py` | **1 PASS / 0 FAIL / 18 SKIP**（只有启动日志，18 项要真实聊天才覆盖） |

---

## 3. 表情包：是否心情驱动

| 路径 | 依据 | 判定 |
|---|---|---|
| 她主动发 `<sticker:标签>` | 标签由模型按状态卡选；状态卡现在只给**真实 THINK + 心情水平** | ✅ 心情驱动 |
| 表情概率 `mood_factors["sticker"]` | `1.0 + v/100`（v = 心情**水平**，不是 Δ） | ✅ 心情驱动 |
| 用户明确说「发个表情包」 | 确定性随机甩一张（`send_sticker_by_tag(..., "")`） | ⚠️ 例外：这是满足请求，不是表达心情 |
| `sticker_intent`（priority 2） | 只用于**收**表情（主人指令），不发送 | ✅ 不参与发送 |

> 结论：自发表情全部走心情；唯一例外是「用户点名要表情包」。

---

## 4. 心情涨幅 Δ 与心情水平 v：不混用

| 位置 | 用的是什么 | 结论 |
|---|---|---|
| `mood.observe(key, impact, …)` | impact（-2~2）→ z → K → 步进 | ✅ |
| `mood.set_judge(key, word, value=_new_v, delta=_delta)` | `value`=水平、`delta`=本次步长 | ✅ 分开传 |
| `mood.mood_factors()` | `value`（水平）→ voice/sticker/delay/chatty | ✅ |
| `memory_v2` | `delta`（±0.10/±0.22）算 salience；`mood_value` 存水平 | ✅ |
| `profiles.bump_affinity` | 由 `delta` 推好感 | ✅ |
| `group_mood.record` | 记的是 Δ（群最近净变化） | ✅ 名字叫 group_mood，但语义是 Δ |
| `memdb.log_mood_event(dv=_delta)` | Δ | ✅ |

代码里**未发现** `value=delta` / 把步长当水平用的地方。
修了一处**注释错误**：`memory_v2.py` 原写「delta -2~+2，判读原值之差」，实际是 ±0.10/±0.22 的步长。

---

## 5. 设计文档回滚检查

### 5.1 已修的口径冲突

`check_design_docs --strict` 之前 FAIL，根因是**把「运行时单块上限」当成「设计目标」**：

| 量 | 值 | 含义 |
|---|---|---|
| DESIGN §3.3 常态目标 | **2120** 字符 | 设计里 prompt 该多大 |
| `CTX_*` 单块上限（`core/tuning.py`） | 合计 **4620** | as-built 每块最多多少（persona 700 / protocol 1500…） |
| `PROMPT_BUDGET` | 2200 | live 总池 |

修法：`core/design_constants.py` 新增 `BUDGET`（§3.3 目标）+ `budget_total()`；
`check_design_docs.py` 的 §3.3 校验、PROMPT_BUDGET 对比、子文档重复预算全部改用 `BUDGET`，
`CTX_*` 只用于「上限是否够用」。**目标与上限从此不再混用。**

### 5.2 逐份检查结果

| 文档 | 检查点 | 结果 |
|---|---|---|
| `DESIGN.md` | ADR-038（心情只来自 THINK）；ADR-013/032 标废弃；§9.1 第 3 条；§3.3 预算 | ✅ 一致 |
| `emotion-dynamics-design.md` | §5.1 前窗不写 v；§5.2 删 `mind` 输出；§5.3 删快速通道 | ✅ 一致 |
| `emotion-annotation-spec.md` | §1 标签源 = THINK；文件表 | ✅ 一致 |
| `emotion-labeling-guide.md` | §0.1 铁律；impact 只看 THINK | ✅ 一致 |
| `mood-source-audit-20261125.md` | 4 个 P0 + 修复记录 | ✅ 一致 |
| `prompt-skeleton-design.md` / `style-white-moon-design.md` / `proactive-design.md` | persona 400（设计目标） | ✅ 与 BUDGET 一致 |
| `architecture-map.md` | `judge{…mind}` 仍写旧结构 | ⚠️ 待改（P1，未影响运行） |

---

## 6. 收尾（20:30）

| 项 | 处理 |
|---|---|
| `mood_think.json` 补写条目 | 停机 → 清理 **96 条 auto=true** → 重启；现存 **185 条 / auto=0**；备份 `data/mood_think.json.bak_20261125_2029` |
| `architecture-map.md` | `judge{…mind}` → `judge{…guess}`；`mood_now.mind` → `think`；注明「judge 的猜测不注入状态卡」 |
| `deploy_manifest.txt` | 逐项核对：**本地 = 服务器** 后才重算；共修 15 个历史项，`md5sum -c` 现在 **全绿** |

> 2026-11-25 20:50 追加一轮：好感双向 / THINK 纯度补漏 / 流水线过触发 / 文档漂移，
> 见 `verification-report-20261125-affinity-pipeline.md`。

## 7. 仍待观察

1. `verify_napcat_log` 的 18 项 SKIP 需要真实聊天日志；跑 1~2 小时后再拉一次：
   `docker logs --since 2h qq-grok-bot > /tmp/bot.log`。
2. 清理后第一次重启的日志只有启动段，等有聊天后再确认：
   - 状态卡里「你心里那句话」只出现她自己写的 THINK；
   - 不再出现 `[FAST]` 日志；
   - `data/mood_think_inferred.json` 只在缺 THINK 时增长。
