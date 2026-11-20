# 插件开发指南

## 1. 放在哪里

把插件目录放到 `plugins/third/<名字>/`，至少包含 `__init__.py`。

## 2. 元数据（二选一或都用）

### nonebot 原生

```python
from nonebot.plugin import PluginMetadata

__plugin_meta__ = PluginMetadata(
    name="hello",
    description="示例插件",
    usage="/hello",
    type="application",
    config=None,
    supported_adapters={"~onebot.v11"},
)
```

### plugin.toml（声明可调用能力）

```toml
[plugin]
name = "hello"
version = "0.1.0"
description = "示例插件"

[[tools]]
name = "hello_world"
description = "说一句 hello"
level = "safe"                 # safe | confirm | owner
handler = "plugins.third.hello:hello_world"
scope = ["group", "private"]
hold = true                    # true = 工具不许自己发，交给模型决定
parameters = { type = "object", properties = {} }
```

`handler` 指向一个 `async def fn(args: dict) -> str`。

## 3. 权限分级

- `safe`：谁都能用（查询类）
- `confirm` / `owner`：复用 `services.napcat_tools.py` 的分级语义，需要主人点头

## 4. 注册与开关

启动时 `services/plugin_registry.register_all()` 会：

1. 扫描 `plugins/third/*`
2. 读 `__plugin_meta__` / `plugin.toml`
3. 把 `[[tools]]` 注册成 function calling skill
4. 受 `/技能` 开关控制（分组 `third_party`）

## 5. 来源与许可证

每个插件目录必须有 `UPSTREAM.md`：

```markdown
upstream: https://github.com/owner/repo
author: owner
license: MIT
version_or_commit: v1.2.3
modified: no
modification: 未修改（原样引入）
```

`make_dist` 会检查：缺 `UPSTREAM.md` 的第三方插件不允许进入发行仓。

## 6. 自检

```bash
python3 scripts/plugin_cli.py list
python3 scripts/plugin_cli.py doctor     # 检查来源/许可证/风险调用
```
