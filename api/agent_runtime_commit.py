"""
Agent Runtime Events / Commit API
"""

from datetime import datetime
from flask import Blueprint, request, g
from models import (
    db,
    AgentTaskAttempt,
    AgentTaskAttemptState,
    AgentTaskLease,
    AgentTaskEvent,
    AgentResultDedup,
    Task,
    TaskStatus,
)
from .base import ApiResponse, validate_json_request
from .agent_common import now_utc, write_agent_audit, agent_session_required


agent_runtime_commit_bp = Blueprint('agent_runtime_commit', __name__)


@agent_runtime_commit_bp.route('/agent/tasks/<int:task_id>/events', methods=['POST'])
@agent_session_required
def emit_events(task_id):
    agent = g.current_agent
    data = validate_json_request(required_fields=['attempt_id', 'events'])
    if isinstance(data, tuple):
        return data

    attempt_id = data['attempt_id']
    events = data.get('events') or []
    if not isinstance(events, list):
        return ApiResponse.error('events must be array', 400).to_response()

    accepted = 0
    for event in events:
        if not isinstance(event, dict):
            continue

        timestamp = event.get('timestamp')
        if isinstance(timestamp, str):
            try:
                event_time = datetime.fromisoformat(timestamp.replace('Z', '+00:00')).replace(tzinfo=None)
            except Exception:
                event_time = now_utc()
        else:
            event_time = now_utc()

        row = AgentTaskEvent(
            task_id=task_id,
            attempt_id=attempt_id,
            agent_id=agent.id,
            workspace_id=agent.workspace_id,
            event_type=(event.get('type') or 'log')[:32],
            seq=int(event.get('seq', 1)),
            event_timestamp=event_time,
            payload=event.get('payload') or {},
            message=str(event.get('message') or ''),
            created_by=f'agent:{agent.id}',
        )
        db.session.add(row)
        accepted += 1

    db.session.commit()
    return ApiResponse.success({'accepted': accepted}, 'Events accepted').to_response()


@agent_runtime_commit_bp.route('/agent/tasks/<int:task_id>/commit', methods=['POST'])
@agent_session_required
def commit_task(task_id):
    agent = g.current_agent
    data = validate_json_request(required_fields=['attempt_id', 'lease_id', 'status'])
    if isinstance(data, tuple):
        return data

    idem_key = request.headers.get('Idempotency-Key')
    if not idem_key:
        return ApiResponse.error('Missing Idempotency-Key header', 400).to_response()

    existing = AgentResultDedup.query.filter_by(idempotency_key=idem_key).first()
    if existing:
        return ApiResponse.success(
            {
                'task_id': existing.task_id,
                'attempt_id': existing.attempt_id,
                'committed_at': existing.committed_at.isoformat(),
            },
            'Idempotent replay accepted',
        ).to_response()

    attempt = AgentTaskAttempt.query.filter_by(
        task_id=task_id,
        attempt_id=data['attempt_id'],
        agent_id=agent.id,
    ).first()
    if not attempt:
        return ApiResponse.error('Attempt not found', 404).to_response()

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

    task = Task.query.get(task_id)
    if not task:
        return ApiResponse.not_found('Task not found').to_response()

    final_status = (data.get('status') or '').lower()
    if final_status == 'succeeded':
        task.status = TaskStatus.DONE
        task.completed_at = now
        attempt.state = AgentTaskAttemptState.COMMITTED
    elif final_status == 'failed':
        task.status = TaskStatus.REVIEW
        attempt.state = AgentTaskAttemptState.ABORTED
        attempt.failure_code = str(data.get('failure_code') or 'FAILED')
        attempt.failure_reason = str(data.get('failure_reason') or 'Agent reported failure')
    elif final_status == 'cancelled':
        task.status = TaskStatus.CANCELLED
        attempt.state = AgentTaskAttemptState.ABORTED
    else:
        return ApiResponse.error('Invalid status, expected succeeded|failed|cancelled', 400).to_response()

    attempt.ended_at = now
    lease.active = False

    dedup = AgentResultDedup(
        idempotency_key=idem_key,
        task_id=task_id,
        attempt_id=attempt.attempt_id,
        agent_id=agent.id,
        workspace_id=agent.workspace_id,
        committed_at=now,
        created_by=f'agent:{agent.id}',
    )
    db.session.add(dedup)

    write_agent_audit(
        event_type='task.committed',
        actor_type='agent',
        actor_id=agent.id,
        target_type='task',
        target_id=task.id,
        workspace_id=agent.workspace_id,
        payload={'attempt_id': attempt.attempt_id, 'status': final_status},
        risk_score=10,
    )

    db.session.commit()

    return ApiResponse.success(
        {'task_id': task.id, 'final_status': final_status, 'committed_at': now.isoformat()},
        'Task committed successfully',
    ).to_response()
