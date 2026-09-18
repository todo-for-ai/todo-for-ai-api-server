"""分析域包（2026-09-18 由平铺的 api/agents/analytics*.py 等域包化拆分）。

保持空实现：各子模块由 api/agents/__init__.py 经旧路径 shim 按原顺序触发导入，
路由注册顺序与拆分前一致；旧路径经 sys.modules 替换型 shim 保持模块对象同一性。
"""
