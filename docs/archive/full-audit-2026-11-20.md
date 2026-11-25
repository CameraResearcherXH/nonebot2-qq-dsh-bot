> ⚠️ **已归档（2026-11-25）**：本文是历史设计，可能和现行冲突。
> 现行设计以 [DESIGN.md](../DESIGN.md) 为准；差异见 [设计审计](../design-audit-2026-11-25.md)。

# 全仓接线审计（2026-11-20）

> 目的：找出「写了但没接上 / 接了一半 / 接了但没人用」的功能。
> 可重复执行：`python3 scripts/audit_coverage.py --strict`（已并入 `verify_all.py` 的「接线检查」）

## 一、怎么查的（9 项静态检查）

| 编号 | 检查 | 手段 |
|---|---|---|
| A | 孤立服务模块 | AST 收集全仓 import，`services/*.py` 没出现在任何 import 里 |
| B | 工具没接线 | `_tool_*` 定义后，全项目除 def 外再无引用 |
| C | 工具处理器断线 | `register_tool(handler=X)` 但 `X` 不存在 |
| D | 命令重复注册 | 同一 `on_command` 名字注册两次（后者顶掉前者） |
| E | 吞异常 | `except: pass/continue` 分布（信息项，不判失败） |
| F | 占位/未完成 | TODO/FIXME/NotImplementedError/函数体只有 pass |
| G | 参数空转 | `core/tuning.py` 登记了但代码没读 |
| H | 开关没覆盖 | 注册了工具但 `/技能` 关不掉（不在核心、也不在任何组） |
| I | 命令无闸门 | 有 `on_command` 但没挂 `skill_switch.gate`（信息项） |

## 二、结论

```
孤立 0｜工具未接线 0｜处理器断线 0｜命令重复 0
占位 0｜参数空转 0｜开关未覆盖 0｜命令无闸门 6（均为设计如此，见第四节）
```

## 三、本轮新发现并已修（4 个真问题）

### 1. `services/profile_miner.py` 写好了，但**从来没有被调用过**

- 它是 2026-11-20 为了修「群友档案只有称呼、没有内容和事情」专门写的：
  数群友发言 → 攒够阈值 → 后台让模型提炼 identity / personality / 事情 → 写回档案。
- 但全仓没有任何地方 import 它。**所以那个问题其实一直没修上**，
  档案卡里的「事情」仍然是空的。
- 修复：在 `_early_observe`（群消息，priority=1）里接上
  `note_message(gid, qq)` → 到阈值就 `asyncio.create_task(mine(qq, gid))`。

### 2. `/技能` 的组名和真实工具名对不上 → 关了没效果

原来：

```python
"draw":  {"draw", "comfy_workflow"},      # 真实工具叫 draw_image / draw_from_pixiv
"media": {"tts", "song", "cover", ...},   # 真实工具叫 sing / video_understand
"social": {"poke", "napcat_tools"},       # 真实工具叫 napcat_call / napcat_help
```

`/技能 关 画图` 关的是 `draw` 这个不存在的名字，`draw_image` 照常可用 —— 开关看起来生效，实际没关。

修复：组内改成 `register_tool` 里的**真实 name**（从代码里抓出来的 32 个工具名逐一归类）。

### 3. 24 个工具既不在核心、也不在任何组（隐身）

后果有三：
- `/技能` 状态面板里看不到它们；
- `/技能 关 全部` 关不掉它们；
- 出了事没法单独关。

修复：8 个归入核心（`recall`/`remember`/`stay_silent`/`send`/`chat_switch`/`profile_card`/`profile_update`/`api_usage`），
其余进 `draw` / `media` / `social` / `self_tools` / `vision` / `lore` / `third_party` 组。

### 4. 命令级完全绕过开关（`/画图`、`/翻唱`、`/发表情`、`/tavily`）

`skill_switch` 之前只在**工具分发**（`llm._skill_ok`）里生效。
但 `/画图`、`/翻唱`、`/清唱`、`/测歌`、`/导入表情`、`/发表情`、`/识表情`、`/收表情`、`/tavily`
都是各插件自己 `on_command` 注册的，走不到工具分发 —— 关掉能力后这些命令照样能用。

修复：新增 `skill_switch.gate(cmd, skill, label)`，在 4 个插件里给 12 个命令挂了前置闸门
（被关时直接提示「这个能力被主人关掉了（/技能 开 XX 可恢复）」，不再进生成链路）。

## 四、仍存在，但属于「设计如此」（不修，写清楚原因）

| 项 | 数量 | 为什么保留 |
|---|---|---|
| 有命令但没挂闸门的插件 | 6 | `chat`（21 个核心命令：心情/记忆/技能/用量/调参/安静…）、`napcat_tools`（审批流 `/批准` `/拒绝`，关了就没人能批准）、`help_menu`、`selfcheck`、`full_test`、`memory_migration` —— 全是核心或诊断入口，关掉等于自断手脚 |
| `except: pass/continue` | chat 78、llm 16、song_cover 14、profiles 9… | 绝大多数是「可选能力失败不影响主流程」的兜底。风险是**藏错**：真正的 bug 只留 debug 日志。建议后续只保留「确实无关紧要」的，其余改成 `logger.warning` 一次 |
| 第三方插件（`plugins/third`）自己的命令 | 9 个插件 | 它们的 nonebot 命令无法统一挂闸门；只有它们**注册成 tool** 的部分受 `/技能` 控制。已在 `docs/plugin-dev.md` 说明 |

## 五、本轮之前修掉的同类问题（复查仍在）

| 问题 | 后果 |
|---|---|
| `memory_v2.BACKEND` 常量被误删 | 默认 json 后端所有记忆操作 NameError |
| `mood_judge.judge_and_store` 缺 `turn_id` | Δ→加粗/好感那段永远抛异常被吞，从没生效 |
| `_WORKER_BUSY` 没有 finally 复位 | 第一次回复后主动插话永久失效 |
| `mood.touch` 未清 `last_decay_at` | 心情会一步衰减到 0 |
| 新增 `core/` 但 compose 没挂载 | 容器 `from core import tuning` 直接 ImportError |

## 六、复查命令

```bash
python3 scripts/verify_all.py              # 9 道闸门
python3 scripts/audit_coverage.py --strict # 只看接线（A~I）
python3 scripts/selftest_all.py            # 全链路离线回归（1713 项）
```
