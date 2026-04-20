"""
Agent Governance Rules API

Get and update governance rules for a workspace.
Rules are stored per-workspace via SystemSettings using the key pattern
``governance_rules:<workspace_id>``.
"""

from flask import Blueprint

from models import SystemSettings
from core.auth import unified_auth_required, get_current_user
from api.agent_common import (
    get_workspace_or_404,
    ensure_workspace_access,
    ensure_workspace_manage_access,
    write_agent_audit,
)
from api.base import ApiResponse, validate_json_request

gov_rules_bp = Blueprint('agent_governance_rules', __name__)

_DEFAULT_RULES = []


def _rules_key(workspace_id: int) -> str:
    return f"governance_rules:{workspace_id}"


def _get_rules(workspace_id: int):
    """Read governance rules from SystemSettings."""
    return SystemSettings.get_setting(_rules_key(workspace_id), _DEFAULT_RULES)


def _set_rules(workspace_id: int, rules, updated_by=None):
    """Persist governance rules to SystemSettings."""
    SystemSettings.set_setting(
        _rules_key(workspace_id),
        rules,
        description=f"Governance rules for workspace {workspace_id}",
        updated_by=updated_by,
    )


@gov_rules_bp.route('/workspaces/<int:workspace_id>/governance/rules', methods=['GET'])
@unified_auth_required
def get_governance_rules(workspace_id):
    """Get governance rules for a workspace."""
    user = get_current_user()
    workspace, err = get_workspace_or_404(workspace_id)
    if err:
        return err

    access_err = ensure_workspace_access(user, workspace)
    if access_err:
        return access_err

    rules = _get_rules(workspace_id)

    return ApiResponse.success(
        {'rules': rules},
        'Governance rules retrieved successfully',
    ).to_response()


@gov_rules_bp.route('/workspaces/<int:workspace_id>/governance/rules', methods=['PUT'])
@unified_auth_required
def update_governance_rules(workspace_id):
    """Update governance rules for a workspace (owner/admin only)."""
    user = get_current_user()
    workspace, err = get_workspace_or_404(workspace_id)
    if err:
        return err

    access_err = ensure_workspace_manage_access(user, workspace)
    if access_err:
        return access_err

    data = validate_json_request(required_fields=['rules'])
    if isinstance(data, tuple):
        return data

    rules = data['rules']
    if not isinstance(rules, list):
        return ApiResponse.error('rules must be an array', 400).to_response()

    _set_rules(workspace_id, rules, updated_by=user.id)

    write_agent_audit(
        event_type='governance.rules_updated',
        actor_type='user',
        actor_id=user.id,
        target_type='workspace',
        target_id=str(workspace_id),
        workspace_id=workspace_id,
        payload={'rule_count': len(rules)},
    )

    # Commit the audit event (SystemSettings.set_setting already commits, but the
    # audit event added above needs a commit too).
    from models import db
    db.session.commit()

    return ApiResponse.success(
        {'rules': rules},
        'Governance rules updated successfully',
    ).to_response()
