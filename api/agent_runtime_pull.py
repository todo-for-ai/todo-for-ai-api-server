"""
Agent Runtime Pull / Lease API
"""

from datetime import timedelta
from sqlalchemy.exc import IntegrityError
from flask import Blueprint, g
from models import (
    db,
    AgentSecret,
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


def _build_agent_profile(agent):
    active_secret_names = [
        row.name
        for row in AgentSecret.query.with_entities(AgentSecret.name).filter_by(
            workspace_id=agent.workspace_id,
            agent_id=agent.id,
            is_active=True,
        )
    ]
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
