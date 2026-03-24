"""
Agent Runtime Pull / Lease API
"""

from datetime import timedelta
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from flask import Blueprint, g
from models import (
    db,
    AgentSecret,
    AgentSecretGrant,
    AgentTaskAttempt,
    AgentTaskAttemptState,
    AgentTaskLease,
    Task,
    TaskStatus,
    Project,
)
from .base import ApiResponse, validate_json_request
from .agent_common import generate_id, now_utc, write_agent_audit, agent_session_required


agent_runtime_pull_bp = Blueprint('agent_runtime_pull', __name__)


def _capability_keys_for_secret(secret_type):
    normalized = str(secret_type or 'custom').strip().lower()
    mapping = {
        'api_key': ['credential.api.invoke'],
        'oauth_token': ['credential.oauth.invoke'],
        'session_cookie': ['credential.session.use'],
        'webhook_secret': ['credential.webhook.sign'],
        'custom': ['credential.custom.use'],
    }
    return mapping.get(normalized, ['credential.custom.use'])


def _build_secret_capability_ref(secret, source, grant=None):
    grant_payload = None
    if grant is not None:
        remaining_uses = None
        if grant.max_uses is not None:
            remaining_uses = max(int(grant.max_uses) - int(grant.used_count or 0), 0)
        grant_payload = {
            'grant_id': grant.grant_id,
            'from_agent_id': int(grant.from_agent_id),
            'to_agent_id': int(grant.to_agent_id),
            'grant_mode': grant.grant_mode,
            'status': grant.status,
            'max_uses': int(grant.max_uses) if grant.max_uses is not None else None,
            'used_count': int(grant.used_count or 0),
            'remaining_uses': remaining_uses,
            'expires_at': grant.expires_at.isoformat() if grant.expires_at else None,
            'task_id': int(grant.task_id) if grant.task_id is not None else None,
            'attempt_id': grant.attempt_id,
            'chain_id': int(grant.chain_id) if grant.chain_id is not None else None,
        }

    return {
        'secret_id': int(secret.id),
        'name': secret.name,
        'secret_type': secret.secret_type,
        'scope_type': secret.scope_type,
        'project_id': int(secret.project_id) if secret.project_id is not None else None,
        'source': source,
        'capability_keys': _capability_keys_for_secret(secret.secret_type),
        'allowed_actions': ['consume', 'proxy_execute'] if source == 'granted' else ['manage', 'consume', 'proxy_execute'],
        'grant': grant_payload,
    }


def _build_secret_capability_refs(agent):
    refs = []
    names = set()
    active_grant_ids = []

    owned_secrets = AgentSecret.query.filter_by(
        workspace_id=agent.workspace_id,
        agent_id=agent.id,
        is_active=True,
    ).all()
    for secret in owned_secrets:
        names.add(secret.name)
        refs.append(_build_secret_capability_ref(secret, source='owned', grant=None))

    now = now_utc()
    grant_rows = AgentSecretGrant.query.filter(
        AgentSecretGrant.workspace_id == agent.workspace_id,
        AgentSecretGrant.to_agent_id == agent.id,
        AgentSecretGrant.status == 'active',
        or_(
            AgentSecretGrant.expires_at.is_(None),
            AgentSecretGrant.expires_at > now,
        ),
    ).order_by(
        AgentSecretGrant.updated_at.desc(),
        AgentSecretGrant.id.desc(),
    ).all()
    for grant in grant_rows:
        secret = grant.secret
        if not secret or not secret.is_active:
            continue
        names.add(secret.name)
        active_grant_ids.append(grant.grant_id)
        refs.append(_build_secret_capability_ref(secret, source='granted', grant=grant))

    return sorted(names), refs, active_grant_ids


def _build_agent_profile(agent):
    active_secret_names, secret_capability_refs, active_grant_ids = _build_secret_capability_refs(agent)
    return {
        'id': agent.id,
        'workspace_id': agent.workspace_id,
        'name': agent.name,
        'display_name': agent.display_name or '',
        'description': agent.description or '',
        'capability_tags': agent.capability_tags or [],
        'allowed_project_ids': agent.allowed_project_ids or [],
        'llm_provider': agent.llm_provider or '',
        'llm_model': agent.llm_model or '',
        'temperature': float(agent.temperature) if agent.temperature is not None else None,
        'top_p': float(agent.top_p) if agent.top_p is not None else None,
        'max_output_tokens': agent.max_output_tokens,
        'context_window_tokens': agent.context_window_tokens,
        'reasoning_mode': agent.reasoning_mode or 'balanced',
        'system_prompt': agent.system_prompt or '',
        'soul_markdown': agent.soul_markdown or '',
        'response_style': agent.response_style or {},
        'tool_policy': agent.tool_policy or {},
        'memory_policy': agent.memory_policy or {},
        'handoff_policy': agent.handoff_policy or {},
        'execution_mode': agent.execution_mode or 'external_pull',
        'runner_enabled': bool(agent.runner_enabled),
        'sandbox_profile': agent.sandbox_profile or 'standard',
        'sandbox_policy': agent.sandbox_policy or {'network_mode': 'whitelist', 'allowed_domains': []},
        'max_concurrency': agent.max_concurrency,
        'max_retry': agent.max_retry,
        'timeout_seconds': agent.timeout_seconds,
        'heartbeat_interval_seconds': agent.heartbeat_interval_seconds,
        'soul_version': agent.soul_version or 1,
        'config_version': agent.config_version or 1,
        'runner_config_version': agent.runner_config_version or 1,
        'active_secret_names': active_secret_names,
        'active_grant_ids': active_grant_ids,
        'secret_capability_refs': secret_capability_refs,
        'notification_channels': agent.notification_channels or {},
    }


def _resolve_accessible_project_ids(agent):
    if agent.allowed_project_ids:
        return [int(pid) for pid in agent.allowed_project_ids if str(pid).isdigit()]

    rows = db.session.query(Project.id).filter(Project.organization_id == agent.workspace_id).all()
    return [int(r.id) for r in rows]


def _fetch_next_task(agent):
    project_ids = _resolve_accessible_project_ids(agent)
    if not project_ids:
        return None

    now = now_utc()
    query = Task.query.filter(
        Task.project_id.in_(project_ids),
        Task.status.in_([TaskStatus.TODO, TaskStatus.IN_PROGRESS, TaskStatus.REVIEW]),
    ).order_by(Task.created_at.asc())

    for task in query.limit(30).all():
        active_lease = AgentTaskLease.query.filter(
            AgentTaskLease.task_id == task.id,
            AgentTaskLease.active.is_(True),
            AgentTaskLease.expires_at > now,
        ).first()
        if not active_lease:
            return task

    return None


@agent_runtime_pull_bp.route('/agent/tasks/pull', methods=['POST'])
@agent_session_required
def pull_tasks():
    agent = g.current_agent
    data = validate_json_request(optional_fields=['max_tasks'])
    if isinstance(data, tuple):
        return data

    max_tasks = 1
    if data and 'max_tasks' in data:
        try:
            max_tasks = max(1, min(int(data['max_tasks']), 10))
        except Exception:
            return ApiResponse.error('max_tasks must be integer', 400).to_response()

    items = []
    for _ in range(max_tasks):
        task = _fetch_next_task(agent)
        if not task:
            break

        now = now_utc()
        attempt_id = generate_id('att')
        lease_id = generate_id('lea')
        lease_exp = now + timedelta(seconds=60)

        attempt = AgentTaskAttempt(
            attempt_id=attempt_id,
            task_id=task.id,
            agent_id=agent.id,
            workspace_id=agent.workspace_id,
            state=AgentTaskAttemptState.ACTIVE,
            lease_id=lease_id,
            started_at=now,
            created_by=f'agent:{agent.id}',
        )
        lease = AgentTaskLease(
            lease_id=lease_id,
            task_id=task.id,
            attempt_id=attempt_id,
            agent_id=agent.id,
            workspace_id=agent.workspace_id,
            expires_at=lease_exp,
            active=True,
            created_by=f'agent:{agent.id}',
        )

        db.session.add(attempt)
        db.session.add(lease)

        if task.status == TaskStatus.TODO:
            task.status = TaskStatus.IN_PROGRESS

        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            continue

        write_agent_audit(
            event_type='task.leased',
            actor_type='agent',
            actor_id=agent.id,
            target_type='task',
            target_id=task.id,
            workspace_id=agent.workspace_id,
            payload={'attempt_id': attempt_id, 'lease_id': lease_id},
        )
        db.session.commit()

        items.append(
            {
                'task_id': task.id,
                'attempt_id': attempt_id,
                'lease_id': lease_id,
                'lease_expires_at': lease_exp.isoformat(),
                'payload': {
                    'title': task.title,
                    'content': task.content,
                    'priority': task.priority.value if task.priority else None,
                    'tags': task.tags or [],
                },
            }
        )

    return ApiResponse.success(
        {
            'agent_profile': _build_agent_profile(agent),
            'items': items,
        },
        'Tasks pulled successfully',
    ).to_response()


@agent_runtime_pull_bp.route('/agent/tasks/<int:task_id>/lease/renew', methods=['POST'])
@agent_session_required
def renew_lease(task_id):
    agent = g.current_agent
    data = validate_json_request(required_fields=['attempt_id', 'lease_id'])
    if isinstance(data, tuple):
        return data

    lease = AgentTaskLease.query.filter_by(
        task_id=task_id,
        attempt_id=data['attempt_id'],
        lease_id=data['lease_id'],
        agent_id=agent.id,
        active=True,
    ).first()
    if not lease:
        return ApiResponse.error('LEASE_NOT_OWNER', 409).to_response()

    now = now_utc()
    if lease.expires_at <= now:
        lease.active = False
        db.session.commit()
        return ApiResponse.error('LEASE_EXPIRED', 409).to_response()

    lease.expires_at = now + timedelta(seconds=60)
    lease.version += 1
    db.session.commit()

    return ApiResponse.success(
        {'lease_id': lease.lease_id, 'lease_expires_at': lease.expires_at.isoformat()},
        'Lease renewed successfully',
    ).to_response()
