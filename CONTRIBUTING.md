# 贡献指南

## 提交前

```bash
python -m compileall -q core services plugins scripts bot.py
python scripts/check_deps.py --strict      # 依赖方向
python scripts/audit_features.py --strict  # 功能链路断点
python scripts/plugin_cli.py doctor        # 第三方插件来源/风险
```

## 代码约定

- 依赖方向：`plugins → services → core`，禁止反向与插件互相 import。
- 新增配置项必须写进 `.env.example`（由 `make_dist` 自动生成，但源仓要保留注释）。
- 不在源码里写死密钥、真实 QQ/群号、私人人格。
- 新增功能请同时更新 `docs/` 里对应的说明。

## 第三方插件

- 放 `plugins/third/<name>/`，必须带 `UPSTREAM.md`。
- 有 `plugin.toml` 才会被注册成 function calling skill。
- GPL/AGPL 插件请先开 Issue 讨论隔离方案。

## 提交信息

用一句话说清「改了什么 + 为什么」，不要只写「fix」。
