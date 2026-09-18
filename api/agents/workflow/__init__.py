"""工作流域包（2026-09-18 由平铺的 api/agents/workflow_*.py 域包化拆分）。

保持空实现：各子模块由 api/agents/__init__.py 经旧路径 shim 按原顺序触发导入，
路由注册顺序与拆分前完全一致。旧导入路径（api.agents.workflow_routes 等）
经 sys.modules 替换型 shim 指向本包子模块，模块对象同一性不变，
monkeypatch / 私有名访问 / `from api.agents import workflow_dsl` 全部兼容。
"""
