"""
Agent 详情洞察 API

提供 Agent 活动轨迹、参与项目、交互用户、任务记录等可分页查询能力。
"""

from flask import Blueprint


agent_workspace_insights_bp = Blueprint('agent_workspace_insights', __name__)

# Import route modules so decorators register endpoints on blueprint import.
from . import activity  # noqa: E402,F401
from . import interactions  # noqa: E402,F401
from . import projects  # noqa: E402,F401
from . import tasks  # noqa: E402,F401
from . import workspace_activities  # noqa: E402,F401


__all__ = ['agent_workspace_insights_bp']
