# 文档索引

> 先看这一页，再按需跳转。所有文档都在 `docs/` 下。
>
> **技术栈**：本项目基于 **NoneBot2** 框架开发，通过 **OneBot v11** 协议接入 QQ，
> 默认使用 **NapCatQQ** 作为 QQ 协议端；算力机侧使用 Python + ComfyUI / RVC / IndexTTS。

## 入门

| 文档 | 说明 |
|---|---|
| [../README.md](../README.md) | 项目简介、快速开始、常用命令 |
| [deployment.md](deployment.md) | **部署指南**：Docker / NapCat / `.env` / 验证 / 排错 |
| [features.md](features.md) | **功能总览**：每个能力是干嘛的、怎么用、配置在哪 |
| [plugin-dev.md](plugin-dev.md) | 插件开发：`plugin.toml`、工具声明、插件商城 |
| [third-party-integration.md](third-party-integration.md) | **三方插件接入**：怎么变成 function calling 工具 + 我们对上游改了什么 |
| [harness-bridge.md](harness-bridge.md) | **DSH 会话桥**：QQ ↔ DeepSeek Harness 改代码（`CODE_AGENT`） |
| [persona-card.md](persona-card.md) | **人格卡**：格式、字段、参考来源、怎么写自己的角色 |
| [CHANGELOG.md](CHANGELOG.md) | 更新日志（新增 / 优化 / 修复） |

## 设计

### 当前迭代（2026-11-25）：先看主文档

| 文档 | 说明 |
|---|---|
| **[DESIGN.md](DESIGN.md)** | **设计主文档**：一张脊柱图 + 决策记录 + 术语 + 文档索引（从这里进） |
| [addressing-design.md](addressing-design.md) | 收发件人：谁 → 谁、置信度、归因/关系/社交图 |
| [emotion-dynamics-design.md](emotion-dynamics-design.md) | 心情步进+锚点 / 关系衰减 / 人设量化 / 安静主动 |
| [retrieval-design.md](retrieval-design.md) | 角色脑 sqlite / 时间·情绪过滤 / 关键词工程 / RAG 分离 / 评测 |
| [style-white-moon-design.md](style-white-moon-design.md) | 风格四层 / 白月光定位 / 注意力额度 / 借风格护栏 |
| [affinity-quantification-design.md](affinity-quantification-design.md) | **好感量化 v2.1 设计稿**：痕迹求和 + 事件映射表 + 道歉破冰 + 陪伴抵消系数（待拍板） |
| [emotion-annotation-spec.md](emotion-annotation-spec.md) | 200 条情绪标注规范（抽样/字段/一致率） |
| [attention-design.md](attention-design.md) | 上下文预算 / 位置排序 / 引用锚点 / 标签预过滤 / 成本 |
| [prompt-skeleton-design.md](prompt-skeleton-design.md) | 各块最终文本 / 空块规则 / 证据升序 / anchor / 分气泡 |
| [proactive-design.md](proactive-design.md) | 主动接话 / 私聊：心情+好感驱动的自驱车道 |
| [pipeline-map.md](pipeline-map.md) | 一条消息 8 段流水线 + 30 条缺陷（含已设计） |
| [causal-assertions.md](causal-assertions.md) | 33 条因果断言（可证伪，0 模型调用） |
| [design-foundations.md](design-foundations.md) | 闭环审计 / 理论依据 / 参考文献 |
| [design-audit-2026-11-25.md](design-audit-2026-11-25.md) | **设计审计**：历史文档 vs v2 的矛盾 / 漏看 / 该归档清单 |

> 这一组是 **v2 设计稿**：数字以 `DESIGN.md §3 口径表` 为准，先读它再按需跳。
> 哪些已接线、哪些还是设计，看 `DESIGN.md §0.1`（2026-11-25 起 P2/P3/P4/输出/收发件人已接线）。
> ⚠️ 下面「as-built / 历史」里的文档**不是现行设计**；有冲突时以 v2 为准。
> 历史文档已移入 [`archive/`](archive/README.md)。

### as-built（仍有效，只描述现状）

| 文档 | 说明 |
|---|---|
| [architecture-map.md](architecture-map.md) | 代码地图：core / services / plugins 各层职责（2026-11-20 快照） |
| [memory-profile-design.md](memory-profile-design.md) | **记忆 / 档案 / 心情 / 好感** 的数据结构、读写链、进 prompt 顺序 |
| [features.md](features.md) | 功能总览：每个能力是干嘛的、怎么用 |
| [vision-pipeline.md](vision-pipeline.md) | 识图链路：画面 + 番名 + 搜图源 → 报告 |
| [persona-card.md](persona-card.md) | 人格卡格式与字段 |
| [persona-tone.md](persona-tone.md) | 语气漂移与拉回 |
| [style-modules.md](style-modules.md) | expression 与「表达节奏」（旧 attention） |
| [whitemoon.md](whitemoon.md) | 白月光模式的字段与命令（设计以 style-white-moon 为准） |
| [usage-report.md](usage-report.md) | 用量与 prompt 体检 |
| [verification-runbook.md](verification-runbook.md) | **线上验证**：NapCat 日志 ↔ 设计断言怎么对账 |
| [verification-report-20261125-debt.md](verification-report-20261125-debt.md) | **本轮验证报告**：三项工程债闭环 + 8 个假接线键收口（含真实日志回滚） |
| [red-packet-feasibility.md](red-packet-feasibility.md) | **红包可行性**：NapCat/OneBot 没有收发接口；收需原生插件，发不可行 |
| [verification-report-20261126-fixes.md](verification-report-20261126-fixes.md) | **本轮验证报告**：@ 后续被冷却吞 / 亲近突降 / B站短链 / 部署侧 3 类问题 |
| [maibot-analysis.md](maibot-analysis.md) | 外部参考：MaiBot 思路分析 |

### 历史（已被 v2 取代，勿当现行）

| 文档 | 取代者 |
|---|---|
| [master-design.md](archive/master-design.md) | `DESIGN.md`（「一个 Δ 三处用」已被卡尔曼取代） |
| [mood-affinity-design.md](archive/mood-affinity-design.md) | `emotion-dynamics-design.md`（12h/SMOOTH/单标量已废弃） |
| [reply-pipeline.md](archive/reply-pipeline.md) | `pipeline-map.md` + `prompt-skeleton-design.md` |
| [agent-pipeline.md](archive/agent-pipeline.md) | `pipeline-map.md`（翻页/主动/安静正在迁入） |
| [feature-audit.md](archive/feature-audit.md) / [implementation-status.md](archive/implementation-status.md) | 2026-11-20 快照 |
| [review-2026-11-20.md](archive/review-2026-11-20.md) / [review-2026-11-24.md](archive/review-2026-11-24.md) / [full-audit-2026-11-20.md](archive/full-audit-2026-11-20.md) | 历史体检 |
| [harness_design.md](archive/harness_design.md) / [harness_stack_selection.md](archive/harness_stack_selection.md) | `harness-bridge.md`（现行可选） |
| [third-party-audit.md](archive/third-party-audit.md) | 一次性审计 |

## 算力机 / 多媒体

| 文档 | 说明 |
|---|---|
| [node-compute.md](node-compute.md) | **算力机**：`node.py` / `node.env` / ComfyUI / RVC / IndexTTS 部署 |
| [node-params.md](node-params.md) | 算力机参数审计：哪些参数两端必须一致 |
| [../comfy_workflows/README.md](../comfy_workflows/README.md) | ComfyUI workflow 模板与占位符 |

## 配置与合规

| 文档 | 说明 |
|---|---|
| [tuning-guide.md](tuning-guide.md) | **调参手册**：按症状查改哪个键、安全范围、别碰什么 |
| [tuning.md](tuning.md) | **全部可调参数**（418 项，自动生成；改 `core/tuning.py`） |
| [third-party-audit.md](archive/third-party-audit.md) | 第三方插件清单（上游 / 许可证 / 是否修改） |
| [CREDITS.md](CREDITS.md) | 思路参考与开源依赖（MaiBot / SillyTavern / maimai…） |
| [../THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) | 第三方插件声明 |

## 常用自检

```bash
python3 scripts/verify_all.py --quick   # 静态检查 + 单测
python3 scripts/verify_all.py --full    # 再加全链路自测
python3 scripts/check_tuning.py --strict            # 参数真接线（AST）
python3 scripts/check_design_docs.py --runtime --strict  # 文档↔常量 + 运行时接线
python3 scripts/audit_features.py --strict
md5sum -c --quiet deploy_manifest.txt   # 与部署清单一致
```

`scripts/make_dist.py` 用于从源仓生成这个开源发行仓（自动去掉 data / 密钥 / 私人人格卡）。
