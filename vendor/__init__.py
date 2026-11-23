"""第三方 vendored 库（只读，不含业务逻辑）。

放在仓库根而不是 plugins/third，是为了让 services 可以合法依赖它：
依赖方向是 plugins → services → core，services 不能 import plugins。
"""
