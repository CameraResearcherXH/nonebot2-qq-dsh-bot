import nonebot
from nonebot import logger
from nonebot.adapters.onebot.v11 import Adapter
from pathlib import Path

# 初始化 NoneBot
nonebot.init()

# 注册 OneBot V11 适配器
driver = nonebot.get_driver()
driver.register_adapter(Adapter)

# 诊断：nonebot 的依赖解析跑在 anyio TaskGroup 里，依赖抛的异常会被包成
#   ExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)
# 且子异常细节会被 nonebot 的 catch() 吃掉，日志里看不出根因。
# 这里挂个观测层，出错时把内层异常 + 依赖函数名打出来（DEP_TRACE=0 可关）。
from services.dep_trace import install as _install_dep_trace

_install_dep_trace()

from services.config import NODE_COMPUTE, CODE_AGENT

# 加载自研插件（NoneBot 只扫一层，不递归子目录）。
# NODE_COMPUTE=0 时跳过「算力机专属」插件，让 plugins/third 里的现成插件接管
# 同名指令（例如本机版 /画图、/唱歌）。
_SKIP_WHEN_NO_NODE = {"draw", "song_cover"}
# CODE_AGENT=harness 时，self_tools（自己写 Python 工具）整个不加载
_SKIP_ALWAYS = {"self_tools"} if CODE_AGENT == "harness" else set()
_PLUGIN_DIR = Path("plugins")
for _p in sorted(_PLUGIN_DIR.iterdir()):
    if not _p.is_dir() or _p.name == "third" or _p.name.startswith((".", "_")):
        continue
    if not (_p / "__init__.py").exists():
        continue
    if not NODE_COMPUTE and _p.name in _SKIP_WHEN_NO_NODE:
        logger.info(
            f"[BOOT] NODE_COMPUTE=0 → 跳过 plugins/{_p.name}（交给 plugins/third 的本机插件）")
        continue
    if _p.name in _SKIP_ALWAYS:
        logger.info(f"[BOOT] CODE_AGENT={CODE_AGENT} → 跳过 plugins/{_p.name}")
        continue
    nonebot.load_plugin(f"plugins.{_p.name}")

# 加载第三方插件：plugins/third/ 是单独的三方口，
# 和自研插件物理隔离 —— 以后开源时 3rd 那部分能一眼区分归属。
# 注意：目录名必须是合法包名（不能带 - 和 .），否则 import 会直接失败。
nonebot.load_plugins("plugins/third")

if __name__ == "__main__":
    nonebot.run()
