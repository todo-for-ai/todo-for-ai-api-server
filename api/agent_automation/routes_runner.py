"""Runner config routes for agent automation."""

from models import db
from core.auth import unified_auth_required, get_current_user
from ..base import ApiResponse, validate_json_request
from ..agent_common import ensure_agent_manage_access

from . import agent_automation_bp
from .constants import ALLOWED_EXECUTION_MODES
from .shared import _get_agent_or_404, _parse_bool

@agent_automation_bp.route('/workspaces/<int:workspace_id>/agents/<int:agent_id>/runner-config', methods=['GET'])
@unified_auth_required
def get_runner_config(workspace_id, agent_id):
    user = get_current_user()
    agent, err = _get_agent_or_404(workspace_id, agent_id)
    if err:
        return err

    manage_err = ensure_agent_manage_access(user, agent)
    if manage_err:
        return manage_err

    return ApiResponse.success(
        {
            'execution_mode': agent.execution_mode or 'external_pull',
            'runner_enabled': bool(agent.runner_enabled),
            'sandbox_profile': agent.sandbox_profile or 'standard',
            'sandbox_policy': agent.sandbox_policy or {'network_mode': 'whitelist', 'allowed_domains': []},
            'runner_config_version': agent.runner_config_version or 1,
        },
        'Runner config retrieved successfully',
    ).to_response()


@agent_automation_bp.route('/workspaces/<int:workspace_id>/agents/<int:agent_id>/runner-config', methods=['PATCH'])
@unified_auth_required
def patch_runner_config(workspace_id, agent_id):
    user = get_current_user()
    agent, err = _get_agent_or_404(workspace_id, agent_id)
    if err:
        return err

    manage_err = ensure_agent_manage_access(user, agent)
    if manage_err:
        return manage_err

    data = validate_json_request(
        optional_fields=['execution_mode', 'runner_enabled', 'sandbox_profile', 'sandbox_policy']
    )
    if isinstance(data, tuple):
        return data

    if 'execution_mode' in data:
        execution_mode = str(data.get('execution_mode') or '').strip().lower()
        if execution_mode not in ALLOWED_EXECUTION_MODES:
            return ApiResponse.error('Invalid execution_mode', 400).to_response()
        agent.execution_mode = execution_mode

    if 'runner_enabled' in data:
        agent.runner_enabled = _parse_bool(data.get('runner_enabled'), False)

    if 'sandbox_profile' in data:
        agent.sandbox_profile = str(data.get('sandbox_profile') or 'standard').strip()[:64] or 'standard'

    if 'sandbox_policy' in data:
        sandbox_policy = data.get('sandbox_policy') or {}
        if not isinstance(sandbox_policy, dict):
            return ApiResponse.error('sandbox_policy must be object', 400).to_response()
        allowed_domains = sandbox_policy.get('allowed_domains') or []
        if not isinstance(allowed_domains, list):
            return ApiResponse.error('sandbox_policy.allowed_domains must be array', 400).to_response()
        sanitized_domains = []
        for domain in allowed_domains:
            domain_str = str(domain or '').strip().lower()
            if domain_str:
                sanitized_domains.append(domain_str)

        agent.sandbox_policy = {
            'network_mode': str(sandbox_policy.get('network_mode') or 'whitelist').strip().lower(),
            'allowed_domains': sanitized_domains,
        }

    agent.runner_config_version = (agent.runner_config_version or 1) + 1
    agent.config_version = (agent.config_version or 1) + 1

    db.session.commit()
    return ApiResponse.success(agent.to_dict(), 'Runner config updated successfully').to_response()
