"""
Agent Runtime Events / Commit API
"""

import json
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


def _normalize_event_payload(data):
    """统一 runtime 发送的事件格式到后端存储格式。"""
    events = data.get('events') if isinstance(data.get('events'), list) else None
    attempt_id = data.get('attempt_id')
    task_id = data.get('task_id')

    if events is not None:
        normalized = []
        for event in events:
            if not isinstance(event, dict):
                continue
            normalized.append({
                'task_id': int(event.get('task_id')) if event.get('task_id') is not None else task_id,
                'attempt_id': str(event.get('attempt_id')) if event.get('attempt_id') is not None else attempt_id,
                'event_type': (event.get('event_type') or event.get('type') or 'log')[:32],
                'seq': int(event.get('seq', 1)),
                'event_timestamp': event.get('timestamp'),
                'payload': event.get('metadata') if event.get('metadata') is not None else event.get('payload', {}),
                'message': str(event.get('message') or ''),
            })
        return normalized

    # 单条直传格式（runtime 的 emit_task_event）
    if isinstance(data, dict) and (data.get('event_type') or data.get('type')):
        return [{
            'task_id': task_id,
            'attempt_id': attempt_id,
            'event_type': (data.get('event_type') or data.get('type') or 'log')[:32],
            'seq': int(data.get('seq', 1)),
            'event_timestamp': data.get('timestamp'),
            'payload': data.get('metadata') if data.get('metadata') is not None else data.get('payload', {}),
            'message': str(data.get('message') or ''),
        }]

    return []


def _persist_events(agent, raw_events, default_task_id=None):
    accepted = 0
    for event in raw_events:
        if not isinstance(event, dict):
            continue

        timestamp = event.get('event_timestamp')
        if isinstance(timestamp, str):
            try:
                event_time = datetime.fromisoformat(timestamp.replace('Z', '+00:00')).replace(tzinfo=None)
            except Exception:
                event_time = now_utc()
        else:
            event_time = now_utc()

        row = AgentTaskEvent(
            task_id=int(event.get('task_id')) if event.get('task_id') is not None else default_task_id,
            attempt_id=str(event.get('attempt_id')) if event.get('attempt_id') is not None else '',
            agent_id=agent.id,
            workspace_id=agent.workspace_id,
            event_type=(event.get('event_type') or 'log')[:32],
            seq=int(event.get('seq', 1)),
            event_timestamp=event_time,
            payload=event.get('payload') or {},
            message=str(event.get('message') or ''),
            created_by=f'agent:{agent.id}',
        )
        db.session.add(row)
        accepted += 1
    return accepted


@agent_runtime_commit_bp.route('/agent/tasks/<int:task_id>/events', methods=['POST'])
@agent_session_required
def emit_events(task_id):
    agent = g.current_agent
    data = validate_json_request()
    if isinstance(data, tuple):
        return data

    raw_events = _normalize_event_payload({**data, 'task_id': task_id})
    if not raw_events:
        return ApiResponse.error('No valid events found', 400).to_response()

    accepted = _persist_events(agent, raw_events, default_task_id=task_id)
    db.session.commit()
    return ApiResponse.success({'accepted': accepted}, 'Events accepted').to_response()


@agent_runtime_commit_bp.route('/agent/tasks/events/batch', methods=['POST'])
@agent_session_required
def emit_events_batch():
    agent = g.current_agent
    data = validate_json_request()
    if isinstance(data, tuple):
        return data

    events = data.get('events') or []
    if not isinstance(events, list):
        return ApiResponse.error('events must be array', 400).to_response()

    raw_events = _normalize_event_payload({'events': events})
    accepted = _persist_events(agent, raw_events)
    db.session.commit()
    return ApiResponse.success({'accepted': accepted, 'sent': accepted}, 'Events accepted').to_response()


@agent_runtime_commit_bp.route('/agent/tasks/<int:task_id>/commit', methods=['POST'])
@agent_session_required
def commit_task(task_id):
    agent = g.current_agent
    data = validate_json_request(required_fields=['attempt_id', 'lease_id', 'status'], optional_fields=['result', 'failure_code', 'failure_reason', 'execution_time_ms'])
    if isinstance(data, tuple):
        return data

    idem_key = request.headers.get('Idempotency-Key') or data.get('attempt_id')
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
        # Write agent result back to task content
        result_data = data.get('result') or {}
        output = result_data.get('output', '')
        if output:
            try:
                existing = json.loads(task.content) if task.content else {}
                if not isinstance(existing, dict):
                    existing = {"content": task.content}
            except Exception:
                existing = {"content": task.content}
            existing['agent_output'] = output
            existing['agent_metadata'] = result_data.get('metadata', {})
            existing['processed_by'] = result_data.get('processed_by', 'agent')
            task.content = json.dumps(existing, ensure_ascii=False)
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
    # Handle unique constraint on (task_id, active) - delete lease if inactive already exists
    inactive_exists = AgentTaskLease.query.filter(
        AgentTaskLease.task_id == task_id,
        AgentTaskLease.active.is_(False),
    ).first()
    if inactive_exists:
        db.session.delete(lease)
    else:
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
