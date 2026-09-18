"""兼容 shim：实现在 api/agents/workflow/test_run.py（域包化拆分）。

sys.modules 替换保证旧路径拿到同一模块对象——私有名、monkeypatch、
`from api.agents import workflow_test_run` 全部与拆分前等价。
"""
import sys as _sys

from api.agents.workflow import test_run as _impl  # noqa: E402

_sys.modules[__name__] = _impl
