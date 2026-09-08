from core.auth import get_current_user, unified_auth_required

from ..agent_common import ensure_workspace_access, get_workspace_or_404
from ..base import ApiResponse
from . import agent_workspace_insights_bp
from .activity_collectors import build_activity_feed_response

@agent_workspace_insights_bp.route('/workspaces/<int:workspace_id>/insights/activities', methods=['GET'])
@unified_auth_required
def list_workspace_activities(workspace_id: int):
    user = get_current_user()
    workspace, err = get_workspace_or_404(workspace_id)
    if err:
        return err

    access_err = ensure_workspace_access(user, workspace)
    if access_err:
        return access_err

    return build_activity_feed_response(
        workspace_id=workspace_id,
        agent_id=None,
        message='Workspace activities retrieved successfully',
        default_scan_floor=600,
        max_scan_limit=6000,
        endpoint_name='list_workspace_activities',
    )
