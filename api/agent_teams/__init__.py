"""Agent 团队管理包（由单文件 api/agent_teams.py 原样拆分）。

兼容导出：agent_teams_bp 与全部 13 个路由函数，app.py 与既有测试零改动。
"""

from api.agent_teams._core import agent_teams_bp, TEAM_EDITABLE_FIELDS  # noqa: F401
from api.agent_teams.teams import (  # noqa: F401
    list_teams,
    create_team,
    get_team,
    update_team,
    delete_team,
)
from api.agent_teams.members import (  # noqa: F401
    list_team_members,
    add_team_member,
    update_team_member,
    remove_team_member,
    reorder_team_members,
)
from api.agent_teams.projects import (  # noqa: F401
    list_team_projects,
    add_team_project,
    remove_team_project,
)
