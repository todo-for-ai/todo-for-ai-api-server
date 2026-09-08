from core.auth import get_current_user, unified_auth_required

from ..agent_access_control import ensure_agent_detail_access
from ..base import ApiResponse
from . import agent_workspace_insights_bp
from .activity_collectors import build_activity_feed_response
from .shared import _get_agent_or_404

@agent_workspace_insights_bp.route('/workspaces/<int:workspace_id>/agents/<int:agent_id>/insights/activity', methods=['GET'])
@unified_auth_required
def list_agent_activity(workspace_id: int, agent_id: int):
    user = get_current_user()
    agent, err = _get_agent_or_404(workspace_id, agent_id)
    if err:
        return err

    access_err = ensure_agent_detail_access(actor_user=user, target_agent=agent)
    if access_err:
        return access_err

    return build_activity_feed_response(
        workspace_id=workspace_id,
        agent_id=agent_id,
        message='Agent activity retrieved successfully',
        default_scan_floor=400,
        max_scan_limit=4000,
        endpoint_name='list_agent_activity',
    )
