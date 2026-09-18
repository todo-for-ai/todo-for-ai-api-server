"""兼容 shim：实现在 api/agents/analytics/failure_analysis.py（域包化拆分）。

sys.modules 替换保证旧路径拿到同一模块对象——私有名、monkeypatch、
`from api.agents import failure_analysis` 全部与拆分前等价。
"""
import sys as _sys

from api.agents.analytics import failure_analysis as _impl  # noqa: E402

_sys.modules[__name__] = _impl
