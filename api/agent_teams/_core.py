"""Agent 团队蓝图与共享工具（原样搬移）。"""

from flask import Blueprint, request
from sqlalchemy import func

from models import (
    db, AgentTeam, AgentTeamStatus, AgentTeamMember, AgentTeamMemberRole,
    AgentTeamProject, Agent, AgentStatus, Organization
)
from core.auth import unified_auth_required, get_current_user
from api.agent_common import (
    get_workspace_or_404, ensure_workspace_access,
    ensure_agent_manage_access, write_agent_audit
)
from api.base import ApiResponse, validate_json_request, get_request_args


agent_teams_bp = Blueprint('agent_teams', __name__)

TEAM_EDITABLE_FIELDS = ['name', 'description', 'avatar_url', 'config', 'default_strategy']


def _filter_team_editable_fields(data):
    return {k: v for k, v in data.items() if k in TEAM_EDITABLE_FIELDS}
