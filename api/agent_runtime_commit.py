"""
Agent Runtime Events / Commit API
"""

import json
import os
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
    TaskEvidenceRecord,
)
from .base import ApiResponse, validate_json_request
from .agent_common import now_utc, write_agent_audit, agent_session_required
from services.failure_recovery import handle_failed_commit
from services.task_content import append_agent_section


agent_runtime_commit_bp = Blueprint('agent_runtime_commit', __name__)

# 可由 Agent 提供证据的 DoD 类型；pr/manual 由平台/人类侧核验
_AGENT_ENFORCEABLE_DOD_TYPES = ('test', 'build', 'lint', 'command')


def _dod_evidence_required():
    """DoD 证据强制开关（向后兼容闸门：设为 false 可整体关闭）。"""
    return os.environ.get('DOD_EVIDENCE_REQUIRED', 'true').strip().lower() not in ('false', '0', 'no', 'off')


def _validate_evidence_items(raw_items):
    """规范化并校验 runtime 提交的证据列表，返回 (items, error_message)。"""
    if raw_items is None:
        return [], None
    if not isinstance(raw_items, list):
        return [], 'evidence must be an array'

    items = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            return [], 'each evidence item must be an object'
        evidence_type = str(raw.get('evidence_type') or raw.get('type') or '').strip().lower()
        status = str(raw.get('status') or 'unknown').strip().lower()
        if evidence_type not in TaskEvidenceRecord.TYPES:
            return [], f"invalid evidence_type: {evidence_type!r}"
        if status not in TaskEvidenceRecord.STATUSES:
            return [], f"invalid evidence status: {status!r}"
        items.append({
            'evidence_type': evidence_type,
            'status': status,
            'summary': (str(raw.get('summary'))[:500] if raw.get('summary') else None),
            'detail': raw.get('detail') if isinstance(raw.get('detail'), (dict, list)) else None,
            'url': (str(raw.get('url'))[:1000] if raw.get('url') else None),
        })
    return items, None


def _check_dod_coverage(task, evidence_items):
    """检查证据是否覆盖任务的 DoD。返回未满足的描述列表（空即通过）。"""
    unmet = []
    provided = {(i['evidence_type'], i['status']) for i in evidence_items}
    for criterion in (task.dod or []):
        if not isinstance(criterion, dict):
            continue
        dod_type = str(criterion.get('type') or '').strip().lower()
        if dod_type not in _AGENT_ENFORCEABLE_DOD_TYPES:
            # pr / manual 类型由平台侧（PR 合并）或人类核验，不在 commit 强制范围
            continue
        if (dod_type, 'passed') not in provided:
            label = criterion.get('value') or criterion.get('description') or dod_type
            unmet.append(f"{dod_type}: {label}")
    return unmet


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
    data = validate_json_request(
        required_fields=['attempt_id', 'lease_id', 'status'],
        optional_fields=['result', 'failure_code', 'failure_reason', 'execution_time_ms', 'evidence'],
    )
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

    # ── 验证门：解析证据并按 DoD 校验（在任何状态变更之前） ──
    evidence_items, evidence_error = _validate_evidence_items(data.get('evidence'))
    if evidence_error:
        return ApiResponse.error(f'Invalid evidence: {evidence_error}', 400).to_response()

    final_status = (data.get('status') or '').lower()
    recovery = None

    if final_status == 'succeeded' and task.dod and _dod_evidence_required():
        unmet = _check_dod_coverage(task, evidence_items)
        if unmet:
            return ApiResponse.error(
                'DoD evidence incomplete: ' + '; '.join(unmet),
                400,
                error_details={'code': 'DOD_EVIDENCE_MISSING', 'unmet': unmet},
            ).to_response()

    if final_status == 'succeeded':
        task.status = TaskStatus.DONE
        task.completed_at = now
        attempt.state = AgentTaskAttemptState.COMMITTED
        # Write agent result back to task content as a co-authoring section
        result_data = data.get('result') or {}
        output = result_data.get('output', '')
        if output:
            agent_label = str(result_data.get('processed_by')
                              or getattr(agent, 'name', '') or 'agent').strip()
            task.content = append_agent_section(
                task.content,
                output,
                agent_label=agent_label,
                agent_metadata=result_data.get('metadata'),
            )
    elif final_status == 'failed':
        task.status = TaskStatus.REVIEW
        attempt.state = AgentTaskAttemptState.ABORTED
        attempt.failure_code = str(data.get('failure_code') or 'FAILED')
        attempt.failure_reason = str(data.get('failure_reason') or 'Agent reported failure')

        # P2.3 失败自愈：归因 + 修复子任务回流 / 封顶升级人工
        recovery = None
        try:
            recovery = handle_failed_commit(
                task, agent, attempt_id=data['attempt_id'],
                failure_code=attempt.failure_code,
                failure_reason=attempt.failure_reason,
            )
        except Exception as recovery_error:
            # 自愈失败不影响失败提交本身
            import structlog
            structlog.get_logger().warning(
                "commit.recovery_failed", task_id=task.id, error=str(recovery_error),
            )
    elif final_status == 'cancelled':
        task.status = TaskStatus.CANCELLED
        attempt.state = AgentTaskAttemptState.ABORTED
    else:
        return ApiResponse.error('Invalid status, expected succeeded|failed|cancelled', 400).to_response()

    # 持久化证据（无论任务是否声明 DoD，提交的证据都保留作为审计材料）
    for item in evidence_items:
        db.session.add(TaskEvidenceRecord(
            task_id=task.id,
            attempt_id=attempt.attempt_id,
            agent_id=agent.id,
            evidence_type=item['evidence_type'],
            status=item['status'],
            summary=item['summary'],
            detail=item['detail'],
            url=item['url'],
            verified_at=now,
            created_by=f'agent:{agent.id}',
        ))

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

    # GoalLoop 目标循环：循环任务提交到终态后推进下一轮（非循环任务零开销）
    try:
        from services.goal_loop_service import notify_task_finished
        notify_task_finished(task.id)
    except Exception:
        pass

    return ApiResponse.success(
        {
            'task_id': task.id,
            'final_status': final_status,
            'committed_at': now.isoformat(),
            'evidence_count': len(evidence_items),
            'recovery': recovery,
        },
        'Task committed successfully',
    ).to_response()
