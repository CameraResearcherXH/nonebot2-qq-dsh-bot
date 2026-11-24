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

| 文档 | 说明 |
|---|---|
| [architecture-map.md](architecture-map.md) | 代码地图：core / services / plugins 各层职责 |
| [master-design.md](master-design.md) | 总体设计（记忆 / 心情 / 档案 / 群聊现场） |
| [mood-affinity-design.md](mood-affinity-design.md) | **心情 / 好感 / 群心情 / 峰值事件** 的完整设计 |
| [memory-profile-design.md](memory-profile-design.md) | **记忆 / 档案 / 心情 / 好感** 的数据结构、读写链、进 prompt 顺序 |
| [whitemoon.md](whitemoon.md) | **白月光模式**：两层统计、风格取样、注入时机 |
| [usage-report.md](usage-report.md) | **用量与 prompt 体检**：缓存命中、分段占比、图表 |
| [feature-audit.md](feature-audit.md) | 功能链路审计：每个断点编号与对应闸门 |
| [harness_design.md](harness_design.md) | 自测 / 闸门体系 |
| [harness_stack_selection.md](harness_stack_selection.md) | 自测技术选型 |
| [implementation-status.md](implementation-status.md) | 实现状态与已知限制 |
| [maibot-analysis.md](maibot-analysis.md) | 对 MaiBot 思路的分析与本项目落地 |
| [full-audit-2026-11-20.md](full-audit-2026-11-20.md) | 2026-11-20 全仓审计记录 |

## 算力机 / 多媒体

| 文档 | 说明 |
|---|---|
| [node-compute.md](node-compute.md) | **算力机**：`node.py` / `node.env` / ComfyUI / RVC / IndexTTS 部署 |
| [node-params.md](node-params.md) | 算力机参数审计：哪些参数两端必须一致 |
| [../comfy_workflows/README.md](../comfy_workflows/README.md) | ComfyUI workflow 模板与占位符 |

## 配置与合规

| 文档 | 说明 |
|---|---|
| [tuning.md](tuning.md) | **全部可调参数**（229 项，自动生成；改 `core/tuning.py`） |
| [third-party-audit.md](third-party-audit.md) | 第三方插件清单（上游 / 许可证 / 是否修改） |
| [CREDITS.md](CREDITS.md) | 思路参考与开源依赖（MaiBot / SillyTavern / maimai…） |
| [../THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) | 第三方插件声明 |

## 常用自检

```bash
python3 scripts/verify_all.py --quick   # 静态检查 + 单测
python3 scripts/verify_all.py --full    # 再加全链路自测
python3 scripts/check_tuning.py --strict
python3 scripts/audit_features.py --strict
```

`scripts/make_dist.py` 用于从源仓生成这个开源发行仓（自动去掉 data / 密钥 / 私人人格卡）。
