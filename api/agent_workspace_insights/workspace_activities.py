from typing import Any, Dict, List, Set

from flask import request
from sqlalchemy import or_

from core.auth import get_current_user, unified_auth_required
from models import Agent, AgentAuditEvent, AgentRun, AgentTaskAttempt, AgentTaskEvent, TaskLog

from ..agent_common import ensure_workspace_access, get_workspace_or_404
from ..base import ApiResponse, get_request_args
from . import agent_workspace_insights_bp
from .shared import (
    _activity_item_matches,
    _activity_sort_key,
    _build_agent_profile_map,
    _build_project_name_map,
    _build_task_context_map,
    _fetch_agent_audit_rows,
    _parse_int_optional,
    _parse_iso_datetime,
    _parse_source_filter,
    _safe_text,
    _serialize_activity_item,
)

@agent_workspace_insights_bp.route('/workspaces/<int:workspace_id>/insights/activities', methods=['GET'])
@unified_auth_required
def list_workspace_activities(workspace_id: int):
    user = get_current_user()
    workspace, err = get_workspace_or_404(workspace_id)
    if err:
        return err

    access_err = ensure_workspace_access(user, workspace)
    if access_err:
        return access_err

    args = get_request_args()
    page = max(args['page'], 1)
    per_page = min(max(args['per_page'], 1), 100)

    agent_id_filter = request.args.get('agent_id', type=int)
    source_filter = _parse_source_filter(request.args.get('source'))
    level_filter = _parse_source_filter(request.args.get('level'))
    event_type_filter = str(request.args.get('event_type') or '').strip().lower()
    query_text = str(request.args.get('q') or '').strip().lower()
    task_id_filter = request.args.get('task_id', type=int)
    project_id_filter = request.args.get('project_id', type=int)
    run_id_filter = str(request.args.get('run_id') or '').strip().lower()
    attempt_id_filter = str(request.args.get('attempt_id') or '').strip().lower()
    actor_type_filter = str(request.args.get('actor_type') or '').strip().lower()
    min_risk_score = request.args.get('min_risk_score', type=int)
    max_risk_score = request.args.get('max_risk_score', type=int)
    since = _parse_iso_datetime(request.args.get('from'))
    until = _parse_iso_datetime(request.args.get('to'))

    default_scan_limit = max(page * per_page * 8, 600)
    scan_limit = min(max(int(request.args.get('scan_limit') or default_scan_limit), 100), 6000)

    activity_items: List[Dict[str, Any]] = []
    related_task_ids: Set[int] = set()
    related_project_ids: Set[int] = set()
    related_agent_ids: Set[int] = set()

    run_query = AgentRun.query.filter_by(workspace_id=workspace_id)
    if agent_id_filter:
        run_query = run_query.filter(AgentRun.agent_id == agent_id_filter)
    if since:
        run_query = run_query.filter(AgentRun.scheduled_at >= since)
    if until:
        run_query = run_query.filter(AgentRun.scheduled_at <= until)
    run_rows = run_query.order_by(AgentRun.scheduled_at.desc(), AgentRun.id.desc()).limit(scan_limit).all()
    for row in run_rows:
        payload = row.input_payload or {}
        task_id = _parse_int_optional(payload.get('task_id'))
        project_id = _parse_int_optional(payload.get('project_id'))
        if task_id:
            related_task_ids.add(task_id)
        if project_id:
            related_project_ids.add(project_id)

        agent_id = int(row.agent_id)
        related_agent_ids.add(agent_id)

        row_state = str(row.state or '').lower()
        event_type = f"run.{row_state}"
        failure_reason = _safe_text(row.failure_reason or '')
        summary = f"Run {row.run_id} {row_state}"
        if failure_reason:
            summary = f"{summary}: {failure_reason}"

        activity_items.append(
            {
                'id': f"run:{row.id}",
                'entity_id': int(row.id),
                'source': 'agent_run',
                'event_type': event_type,
                'level': 'error' if row_state in {'failed', 'expired'} else 'info',
                'message': summary,
                'payload': payload,
                'occurred_at': row.scheduled_at,
                'state': row_state,
                'trigger_reason': row.trigger_reason,
                'run_id': row.run_id,
                'attempt_count': int(row.attempt_count or 0),
                'started_at': row.started_at,
                'ended_at': row.ended_at,
                'scheduled_at': row.scheduled_at,
                'lease_id': row.lease_id,
                'lease_expires_at': row.lease_expires_at,
                'failure_code': row.failure_code,
                'failure_reason': row.failure_reason,
                'agent_id': agent_id,
                'task_id': task_id,
                'project_id': project_id,
                '_sort_id': row.id,
            }
        )

    attempt_query = AgentTaskAttempt.query.filter_by(workspace_id=workspace_id)
    if agent_id_filter:
        attempt_query = attempt_query.filter(AgentTaskAttempt.agent_id == agent_id_filter)
    if since:
        attempt_query = attempt_query.filter(
            or_(
                AgentTaskAttempt.started_at >= since,
                AgentTaskAttempt.ended_at >= since,
            )
        )
    if until:
        attempt_query = attempt_query.filter(AgentTaskAttempt.started_at <= until)
    attempt_rows = attempt_query.order_by(AgentTaskAttempt.started_at.desc(), AgentTaskAttempt.id.desc()).limit(scan_limit).all()
    for row in attempt_rows:
        task_id = int(row.task_id)
        related_task_ids.add(task_id)

        agent_id = int(row.agent_id)
        related_agent_ids.add(agent_id)

        row_state = str(row.state.value if row.state else '').lower()
        event_type = f"attempt.{row_state}"
        occurred_at = row.ended_at or row.started_at
        level = 'error' if row_state == 'aborted' else 'info'
        summary = f"Attempt {row.attempt_id} {row_state}"
        if row.failure_reason:
            summary = f"{summary}: {_safe_text(row.failure_reason)}"

        activity_items.append(
            {
                'id': f"attempt:{row.id}",
                'entity_id': int(row.id),
                'source': 'agent_task_attempt',
                'event_type': event_type,
                'level': level,
                'message': summary,
                'payload': {
                    'attempt_id': row.attempt_id,
                    'lease_id': row.lease_id,
                    'failure_code': row.failure_code,
                    'failure_reason': row.failure_reason,
                },
                'occurred_at': occurred_at,
                'attempt_id': row.attempt_id,
                'lease_id': row.lease_id,
                'state': row_state,
                'started_at': row.started_at,
                'ended_at': row.ended_at,
                'failure_code': row.failure_code,
                'failure_reason': row.failure_reason,
                'agent_id': agent_id,
                'task_id': task_id,
                '_sort_id': row.id,
            }
        )

    event_query = AgentTaskEvent.query.filter_by(workspace_id=workspace_id)
    if agent_id_filter:
        event_query = event_query.filter(AgentTaskEvent.agent_id == agent_id_filter)
    if since:
        event_query = event_query.filter(AgentTaskEvent.event_timestamp >= since)
    if until:
        event_query = event_query.filter(AgentTaskEvent.event_timestamp <= until)
    event_rows = event_query.order_by(AgentTaskEvent.event_timestamp.desc(), AgentTaskEvent.id.desc()).limit(scan_limit).all()
    for row in event_rows:
        task_id = int(row.task_id)
        related_task_ids.add(task_id)

        agent_id = int(row.agent_id)
        related_agent_ids.add(agent_id)

        row_event_type = str(row.event_type or '').strip().lower()
        level = 'error' if row_event_type in {'error', 'failed'} else 'info'
        activity_items.append(
            {
                'id': f"event:{row.id}",
                'entity_id': int(row.id),
                'source': 'agent_task_event',
                'event_type': f"event.{row_event_type}",
                'level': level,
                'message': _safe_text(row.message or f"Event {row_event_type}", 300),
                'payload': row.payload or {},
                'occurred_at': row.event_timestamp,
                'attempt_id': row.attempt_id,
                'seq': int(row.seq or 0),
                'state': row_event_type,
                'agent_id': agent_id,
                'task_id': task_id,
                '_sort_id': row.id,
            }
        )

    log_query = (
        TaskLog.query
        .join(Agent, Agent.id == TaskLog.actor_agent_id)
        .filter(Agent.workspace_id == workspace_id)
    )
    if agent_id_filter:
        log_query = log_query.filter(TaskLog.actor_agent_id == agent_id_filter)
    if since:
        log_query = log_query.filter(TaskLog.created_at >= since)
    if until:
        log_query = log_query.filter(TaskLog.created_at <= until)
    log_rows = log_query.order_by(TaskLog.created_at.desc(), TaskLog.id.desc()).limit(scan_limit).all()
    for row in log_rows:
        task_id = int(row.task_id)
        related_task_ids.add(task_id)

        agent_id = int(row.actor_agent_id)
        related_agent_ids.add(agent_id)

        activity_items.append(
            {
                'id': f"log:{row.id}",
                'entity_id': int(row.id),
                'source': 'task_log',
                'event_type': 'log.appended',
                'level': 'info',
                'message': _safe_text(row.content, 300),
                'payload': {'content_type': row.content_type},
                'occurred_at': row.created_at,
                'content_type': row.content_type,
                'actor_type': row.actor_type.value if hasattr(row.actor_type, 'value') else str(row.actor_type or '').lower(),
                'actor_user_id': _parse_int_optional(row.actor_user_id),
                'actor_agent_id': _parse_int_optional(row.actor_agent_id),
                'agent_id': agent_id,
                'task_id': task_id,
                '_sort_id': row.id,
            }
        )

    audit_query = AgentAuditEvent.query.filter(
        AgentAuditEvent.workspace_id == workspace_id,
        or_(
            AgentAuditEvent.actor_type == 'agent',
            AgentAuditEvent.target_type == 'agent',
        ),
    )
    if agent_id_filter:
        agent_text = str(agent_id_filter)
        audit_query = audit_query.filter(
            or_(
                (AgentAuditEvent.actor_type == 'agent') & (AgentAuditEvent.actor_id == agent_text),
                (AgentAuditEvent.target_type == 'agent') & (AgentAuditEvent.target_id == agent_text),
            )
        )
    if since:
        audit_query = audit_query.filter(AgentAuditEvent.occurred_at >= since)
    if until:
        audit_query = audit_query.filter(AgentAuditEvent.occurred_at <= until)
    audit_rows = _fetch_agent_audit_rows(
        audit_query=audit_query,
        scan_limit=scan_limit,
        endpoint_name='list_workspace_activities',
    )
    for row in audit_rows:
        actor_agent_id = _parse_int_optional(getattr(row, 'actor_agent_id', None))
        target_agent_id = _parse_int_optional(getattr(row, 'target_agent_id', None))
        if actor_agent_id is None and str(row.actor_type or '') == 'agent':
            actor_agent_id = _parse_int_optional(row.actor_id)
        if target_agent_id is None and str(row.target_type or '') == 'agent':
            target_agent_id = _parse_int_optional(row.target_id)
        item_agent_id = actor_agent_id or target_agent_id
        if not item_agent_id:
            continue
        related_agent_ids.add(item_agent_id)

        level = str(row.level or '').strip().lower()
        if level not in {'info', 'warn', 'error'}:
            level = 'error' if int(row.risk_score or 0) >= 50 else ('warn' if int(row.risk_score or 0) >= 20 else 'info')

        audit_task_id = _parse_int_optional(getattr(row, 'task_id', None))
        audit_project_id = _parse_int_optional(getattr(row, 'project_id', None))
        if audit_task_id:
            related_task_ids.add(audit_task_id)
        if audit_project_id:
            related_project_ids.add(audit_project_id)

        activity_items.append(
            {
                'id': f"audit:{row.id}",
                'entity_id': int(row.id),
                'source': 'agent_audit',
                'event_type': f"audit.{str(row.event_type or '').lower()}",
                'level': level,
                'message': _safe_text(f"{row.actor_type}:{row.actor_id} -> {row.target_type}:{row.target_id}", 300),
                'payload': row.payload or {},
                'occurred_at': row.occurred_at,
                'audit_source': getattr(row, 'source', None),
                'risk_score': int(row.risk_score or 0),
                'actor_type': row.actor_type,
                'actor_id': row.actor_id,
                'target_type': row.target_type,
                'target_id': row.target_id,
                'correlation_id': getattr(row, 'correlation_id', None),
                'request_id': getattr(row, 'request_id', None),
                'run_id': getattr(row, 'run_id', None),
                'attempt_id': getattr(row, 'attempt_id', None),
                'task_id': audit_task_id,
                'project_id': audit_project_id,
                'actor_agent_id': actor_agent_id,
                'target_agent_id': target_agent_id,
                'duration_ms': _parse_int_optional(getattr(row, 'duration_ms', None)),
                'error_code': getattr(row, 'error_code', None),
                'agent_id': item_agent_id,
                '_sort_id': row.id,
            }
        )

    task_context_map = _build_task_context_map(related_task_ids)
    agent_profile_map = _build_agent_profile_map(related_agent_ids)

    for item in activity_items:
        task_id = item.get('task_id')
        if task_id and int(task_id) in task_context_map:
            context = task_context_map[int(task_id)]
            item['task_title'] = context.get('task_title')
            if not item.get('project_id'):
                item['project_id'] = context.get('project_id')
            if not item.get('project_name'):
                item['project_name'] = context.get('project_name')
        project_id = item.get('project_id')
        if project_id:
            related_project_ids.add(int(project_id))

        item_agent_id = item.get('agent_id')
        if item_agent_id:
            profile = agent_profile_map.get(int(item_agent_id)) or {}
            item['agent_name'] = profile.get('name')
            item['agent_display_name'] = profile.get('display_name')

    project_name_map = _build_project_name_map(related_project_ids)
    for item in activity_items:
        project_id = item.get('project_id')
        if project_id and not item.get('project_name'):
            item['project_name'] = project_name_map.get(int(project_id))

    matched_items = [
        item
        for item in activity_items
        if _activity_item_matches(
            item=item,
            source_filter=source_filter,
            level_filter=level_filter,
            event_type_filter=event_type_filter,
            query_text=query_text,
            task_id_filter=task_id_filter,
            project_id_filter=project_id_filter,
            run_id_filter=run_id_filter,
            attempt_id_filter=attempt_id_filter,
            actor_type_filter=actor_type_filter,
            min_risk_score=min_risk_score,
            max_risk_score=max_risk_score,
        )
    ]
    matched_items.sort(key=_activity_sort_key, reverse=True)

    total = len(matched_items)
    start = (page - 1) * per_page
    end = start + per_page
    page_items = matched_items[start:end]

    source_summary: Dict[str, int] = {}
    level_summary: Dict[str, int] = {}
    event_type_summary: Dict[str, int] = {}
    for item in matched_items:
        source_key = str(item.get('source') or 'unknown')
        source_summary[source_key] = source_summary.get(source_key, 0) + 1

        level_key = str(item.get('level') or 'unknown').lower()
        level_summary[level_key] = level_summary.get(level_key, 0) + 1

        event_key = str(item.get('event_type') or 'unknown')
        event_type_summary[event_key] = event_type_summary.get(event_key, 0) + 1

    return ApiResponse.success(
        {
            'items': [_serialize_activity_item(item) for item in page_items],
            'summary': {
                'sources': source_summary,
                'levels': level_summary,
                'event_types': event_type_summary,
                'scan_limit': scan_limit,
            },
            'pagination': {
                'page': page,
                'per_page': per_page,
                'total': total,
                'has_prev': page > 1,
                'has_next': page * per_page < total,
            },
        },
        'Workspace activities retrieved successfully',
    ).to_response()
