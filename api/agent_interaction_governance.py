"""
Human-in-the-loop governance APIs for interaction approval decisions.
"""

from flask import Blueprint

from core.auth import get_current_user, unified_auth_required
from models import AgentTaskEvent, db

from .agent_common import ensure_workspace_access, get_workspace_or_404, now_utc, write_agent_audit
from .base import ApiResponse, validate_json_request


agent_interaction_governance_bp = Blueprint('agent_interaction_governance', __name__)

INTERACTION_REQUEST_EVENT_TYPE = 'interaction_request'
INTERACTION_APPROVAL_EVENT_TYPE = 'interaction_approval'
APPROVAL_DECISIONS = {'approved', 'rejected'}


def _find_interaction_request_event(*, workspace_id: int, task_id: int, interaction_id: str, scan_limit: int = 400):
    rows = (
        AgentTaskEvent.query
        .filter(
            AgentTaskEvent.workspace_id == workspace_id,
            AgentTaskEvent.task_id == task_id,
            AgentTaskEvent.event_type == INTERACTION_REQUEST_EVENT_TYPE,
        )
        .order_by(AgentTaskEvent.event_timestamp.desc(), AgentTaskEvent.id.desc())
        .limit(scan_limit)
        .all()
    )
    for row in rows:
        payload = row.payload or {}
        if str(payload.get('interaction_id') or '').strip() == interaction_id:
            return row
    return None


@agent_interaction_governance_bp.route('/workspaces/<int:workspace_id>/tasks/<int:task_id>/interactions/<string:interaction_id>/approval', methods=['POST'])
@unified_auth_required
def decide_interaction_approval(workspace_id: int, task_id: int, interaction_id: str):
    user = get_current_user()
    workspace, err = get_workspace_or_404(workspace_id)
    if err:
        return err

    access_err = ensure_workspace_access(user, workspace)
    if access_err:
        return access_err

    role = str(user.get_organization_role(workspace) or '').strip().lower()
    if role not in {'owner', 'admin'}:
        return ApiResponse.forbidden('Only workspace owner/admin can approve interactions').to_response()

    data = validate_json_request(required_fields=['decision'], optional_fields=['reason'])
    if isinstance(data, tuple):
        return data

    decision = str(data.get('decision') or '').strip().lower()
    if decision not in APPROVAL_DECISIONS:
        return ApiResponse.error('decision must be approved or rejected', 400).to_response()

    reason = str(data.get('reason') or '').strip() or None
    if reason and len(reason) > 1024:
        return ApiResponse.error('reason exceeds max length 1024', 400).to_response()

    request_row = _find_interaction_request_event(
        workspace_id=int(workspace_id),
        task_id=int(task_id),
        interaction_id=str(interaction_id),
    )
    if not request_row:
        return ApiResponse.not_found('Interaction request not found').to_response()

    request_payload = request_row.payload or {}
    governance = request_payload.get('governance') or {}
    if not bool(governance.get('requires_approval')):
        return ApiResponse.error('This interaction does not require human approval', 409).to_response()

    event_time = now_utc()
    decision_payload = {
        'interaction_id': str(interaction_id),
        'decision': decision,
        'reason': reason,
        'reviewer_user_id': int(user.id),
        'reviewer_email': user.email,
        'risk_tier': governance.get('risk_tier'),
        'decided_at': event_time.isoformat(),
    }
    decision_row = AgentTaskEvent(
        task_id=int(task_id),
        attempt_id=str(request_payload.get('attempt_id') or ''),
        agent_id=int(request_row.agent_id),
        workspace_id=int(workspace_id),
        event_type=INTERACTION_APPROVAL_EVENT_TYPE,
        seq=1,
        event_timestamp=event_time,
        payload=decision_payload,
        message=f"interaction approval {interaction_id} decision={decision}",
        created_by=f'user:{user.id}',
    )
    db.session.add(decision_row)

    write_agent_audit(
        event_type='interaction.approval_decided',
        actor_type='user',
        actor_id=user.id,
        target_type='interaction',
        target_id=interaction_id,
        workspace_id=workspace_id,
        payload={
            'interaction_id': str(interaction_id),
            'task_id': int(task_id),
            'decision': decision,
            'reason': reason,
            'source_agent_id': request_payload.get('source_agent_id'),
            'target_agent_id': request_payload.get('target_agent_id'),
            'risk_tier': governance.get('risk_tier'),
            'requires_approval': True,
            'audit_source': 'interaction_governance',
        },
        risk_score=40 if decision == 'rejected' else 20,
    )
    db.session.commit()

    return ApiResponse.success(
        {
            'interaction_id': str(interaction_id),
            'task_id': int(task_id),
            'decision': decision,
            'reviewer_user_id': int(user.id),
            'decided_at': event_time.isoformat(),
        },
        'Interaction approval decision recorded',
    ).to_response()

