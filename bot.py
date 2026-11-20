import nonebot
from nonebot.adapters.onebot.v11 import Adapter

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

# 加载自研插件（NoneBot 只扫一层，不递归子目录）
nonebot.load_plugins("plugins")
# 加载第三方插件：plugins/third/ 是单独的三方口，
# 和自研插件物理隔离 —— 以后开源时 3rd 那部分能一眼区分归属。
# 注意：目录名必须是合法包名（下划线，不能带 - 和 .），否则 import 会直接失败。
nonebot.load_plugins("plugins/third")

if __name__ == "__main__":
    nonebot.run()
