"""兼容 shim：实现在 api/agents/analytics/collaboration.py（域包化拆分）。

sys.modules 替换保证旧路径拿到同一模块对象——私有名、monkeypatch、
`from api.agents import analytics_collaboration` 全部与拆分前等价。
"""
import sys as _sys

from api.agents.analytics import collaboration as _impl  # noqa: E402

_sys.modules[__name__] = _impl
