# 归档文档（历史设计）

> 这里的文档是 **2026-11-20 ~ 2026-11-24 的历史快照**，可能和现行设计冲突。
> **现行设计以 [../DESIGN.md](../DESIGN.md) 为准**；差异与迁移记录见 [../design-audit-2026-11-25.md](../design-audit-2026-11-25.md)。

## 为什么归档

- 旧公式（12h 半衰、SMOOTH、单标量好感、Δ 直接映射）已被 v2 取代；
- 旧流水线（8 块 / 16s）已被 `pipeline-map.md` + `prompt-skeleton-design.md` 取代；
- 旧总纲的「一个 Δ 三处用」已被卡尔曼 + 5 值归因取代；
- 保留是为了**查历史决策和踩坑**，不是实现依据。

## 目录

| 文件 | 原用途 | 取代者 |
|---|---|---|
| `master-design.md` | 总纲 / 一个 Δ 三处用 / 红线 | `DESIGN.md`（红线已迁入 §9） |
| `mood-affinity-design.md` | 心情/好感 as-built + 记录模型 | `emotion-dynamics-design.md` |
| `reply-pipeline.md` | 8 块流水线对照 | `pipeline-map.md` + `prompt-skeleton-design.md` |
| `agent-pipeline.md` | 五层数据 / 翻页 / 主动 / 安静 | `pipeline-map.md` / `proactive-design.md` / `emotion` |
| `feature-audit.md` | 功能链路断点 | `pipeline-map.md` 缺陷表 |
| `implementation-status.md` | 2026-11-20 实施状态 | 本文归档 |
| `review-2026-11-20.md` / `review-2026-11-24.md` | 历史体检 | 本文归档 |
| `full-audit-2026-11-20.md` | 全仓审计 | 本文归档 |
| `harness_design.md` / `harness_stack_selection.md` | 早期 harness 方案 | `harness-bridge.md` |
| `third-party-audit.md` | 一次性三方审计 | `third-party-integration.md` |
