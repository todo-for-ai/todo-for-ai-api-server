"""Run list/detail routes for agent automation."""

from flask import request

from models import AgentRun, AgentRunState
from core.auth import unified_auth_required, get_current_user
from ..base import ApiResponse, get_request_args
from ..agent_access_control import ensure_agent_detail_access

from . import agent_automation_bp
from .shared import _get_agent_or_404

@agent_automation_bp.route('/workspaces/<int:workspace_id>/agents/<int:agent_id>/runs', methods=['GET'])
@unified_auth_required
def list_agent_runs(workspace_id, agent_id):
    user = get_current_user()
    agent, err = _get_agent_or_404(workspace_id, agent_id)
    if err:
        return err

    access_err = ensure_agent_detail_access(actor_user=user, target_agent=agent)
    if access_err:
        return access_err

    args = get_request_args()
    page = max(args['page'], 1)
    per_page = min(max(args['per_page'], 1), 100)

    query = AgentRun.query.filter_by(workspace_id=workspace_id, agent_id=agent_id)
    state_value = request.args.get('state', '').strip().lower()
    if state_value:
        if state_value not in {item.value for item in AgentRunState}:
            return ApiResponse.error('Invalid state filter', 400).to_response()
        query = query.filter(AgentRun.state == state_value)

    query = query.order_by(AgentRun.scheduled_at.desc(), AgentRun.id.desc())
    total = query.count()
    items = query.offset((page - 1) * per_page).limit(per_page).all()

    return ApiResponse.success(
        {
            'items': [row.to_dict() for row in items],
            'pagination': {
                'page': page,
                'per_page': per_page,
                'total': total,
                'has_prev': page > 1,
                'has_next': page * per_page < total,
            },
        },
        'Runs retrieved successfully',
    ).to_response()


@agent_automation_bp.route('/workspaces/<int:workspace_id>/agents/<int:agent_id>/runs/<string:run_id>', methods=['GET'])
@unified_auth_required
def get_agent_run(workspace_id, agent_id, run_id):
    user = get_current_user()
    agent, err = _get_agent_or_404(workspace_id, agent_id)
    if err:
        return err

    access_err = ensure_agent_detail_access(actor_user=user, target_agent=agent)
    if access_err:
        return access_err

    run = AgentRun.query.filter_by(
        workspace_id=workspace_id,
        agent_id=agent_id,
        run_id=run_id,
    ).first()
    if not run:
        return ApiResponse.not_found('Run not found').to_response()

    return ApiResponse.success(run.to_dict(), 'Run retrieved successfully').to_response()
