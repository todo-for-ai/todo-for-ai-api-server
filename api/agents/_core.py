"""Agent collaboration API — core routes（兼容 shim，迭代 47）。

历史上本文件是协作路由的"残余倾倒场"。迭代 43-47 已将其全部拆分：
- Agent 生命周期路由（列表/创建/自助注册/发现/详情/更新/心跳）
  → ``api/agents/agents_crud.py``
- 任务认领与分配路由（审查队列/推荐/认领/分配查询与更新）
  → ``api/agents/agent_assignments.py``

本文件仅保留导入副作用（路由在 import 时挂到 ``agents_bp``），
``api/agents/__init__.py`` 的 ``from . import _core`` 语义不变。
"""

from . import agent_assignments  # noqa: F401,E402  (must come after agents_bp definition)
from . import agents_crud  # noqa: F401,E402  (must come after agents_bp definition)
