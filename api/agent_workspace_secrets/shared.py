from datetime import datetime, timezone

from models import Agent, AgentSecret, Project

from ..base import ApiResponse


def parse_bool(value, default=False):
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {'1', 'true', 'yes', 'on'}:
        return True
    if text in {'0', 'false', 'no', 'off'}:
        return False
    return default


def parse_expires_at(raw_value):
    if raw_value in (None, ''):
        return None, None

    text = str(raw_value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return None, ApiResponse.error('Invalid expires_at, use ISO datetime format', 400).to_response()

    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)

    if parsed <= datetime.utcnow():
        return None, ApiResponse.error('expires_at must be in the future', 400).to_response()

    return parsed, None


def normalize_secret_type(value):
    return (str(value or 'api_key').strip().lower() or 'api_key')


def normalize_scope_type(value):
    return (str(value or 'agent_private').strip().lower() or 'agent_private')


def normalize_target_selector(value):
    return (str(value or 'manual').strip().lower() or 'manual')


def to_int_optional(raw_value):
    if raw_value in (None, ''):
        return None
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        return None


def is_agent_active(agent):
    status_text = agent.status.value if hasattr(agent.status, 'value') else str(agent.status).strip().lower()
    return status_text == 'active'


def normalize_project_id_for_selector(workspace_id, raw_project_id):
    project_id = to_int_optional(raw_project_id)
    if project_id is None:
        return None, ApiResponse.error('selector_project_id must be an integer', 400).to_response()

    project = Project.query.filter_by(id=project_id, organization_id=workspace_id).first()
    if not project:
        return None, ApiResponse.error('selector_project_id does not belong to this workspace', 400).to_response()
    return project_id, None


def allowed_project_id_set(agent):
    values = agent.allowed_project_ids or []
    if not isinstance(values, list):
        values = [values]
    result = set()
    for value in values:
        try:
            result.add(int(value))
        except (TypeError, ValueError):
            continue
    return result


def resolve_target_agent_ids_by_selector(workspace_id, owner_agent_id, selector_mode, selector_project_id):
    if selector_mode == 'manual':
        return [], None

    candidates = Agent.query.filter_by(workspace_id=workspace_id).all()
    resolved = []

    if selector_mode == 'workspace_active':
        for candidate in candidates:
            if candidate.id == owner_agent_id:
                continue
            if not is_agent_active(candidate):
                continue
            resolved.append(int(candidate.id))
        return sorted(set(resolved)), None

    if selector_mode == 'project_agents':
        if selector_project_id is None:
            return [], ApiResponse.error('selector_project_id is required for project_agents selector', 400).to_response()

        for candidate in candidates:
            if candidate.id == owner_agent_id:
                continue
            if not is_agent_active(candidate):
                continue
            if selector_project_id in allowed_project_id_set(candidate):
                resolved.append(int(candidate.id))
        return sorted(set(resolved)), None

    return [], ApiResponse.error('Invalid target_selector', 400).to_response()


def get_agent_or_404(workspace_id, agent_id):
    agent = Agent.query.filter_by(id=agent_id, workspace_id=workspace_id).first()
    if not agent:
        return None, ApiResponse.not_found('Agent not found').to_response()
    return agent, None


def get_secret_or_404(workspace_id, agent_id, secret_id):
    secret = AgentSecret.query.filter_by(id=secret_id, workspace_id=workspace_id, agent_id=agent_id).first()
    if not secret:
        return None, ApiResponse.not_found('Agent secret not found').to_response()
    return secret, None


def mark_secret_used(secret):
    secret.last_used_at = datetime.utcnow()
    secret.usage_count = int(secret.usage_count or 0) + 1
