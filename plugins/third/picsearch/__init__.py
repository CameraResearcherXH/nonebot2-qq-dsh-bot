"""以图搜源插件（元数据）：PicImageSearch 聚合（TraceMoe / SauceNAO / Ascii2D / IQDB）。

⚠️ 这里**故意不写任何 import 和 matcher**。
   工具实现放在 `services/picsearch_tool.py`，由 `plugin.toml` 的
   `handler = "services.picsearch_tool:picsearch"` 指定。

   原因（2026-11-22 真机事故）：`plugin_registry.register_all()` 会在 chat 插件
   import 时就解析 handler；如果 handler 指向本包，`plugins.third.picsearch`
   会被提前导入，之后 `nonebot.load_plugins("plugins/third")` 会抛
   `RuntimeError: Module plugins.third.picsearch is not loaded as a plugin!`
"""
from nonebot.plugin import PluginMetadata

__plugin_meta__ = PluginMetadata(
    name="picsearch",
    description="以图搜源：PicImageSearch 聚合（TraceMoe / SauceNAO / Ascii2D / IQDB）",
    usage="由角色扮演模型按需调用（function calling 工具 `picsearch`）；"
          "也可以 `/测函数 搜图` 直接验证。",
    type="application",
    supported_adapters={"~onebot.v11"},
)
