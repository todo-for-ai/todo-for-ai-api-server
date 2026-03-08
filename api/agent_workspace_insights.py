"""
Agent 详情洞察 API

提供 Agent 活动轨迹、参与项目、交互用户、任务记录等可分页查询能力。
"""

import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from flask import Blueprint, current_app, request
from sqlalchemy import case, func, or_
from sqlalchemy.exc import OperationalError

from core.auth import get_current_user, unified_auth_required
from models import (
    Agent,
    AgentAuditEvent,
    AgentRun,
    AgentTaskAttempt,
    AgentTaskAttemptState,
    AgentTaskEvent,
    Project,
    Task,
    TaskLog,
    TaskStatus,
    User,
    db,
)
from .agent_common import ensure_workspace_access, get_workspace_or_404
from .base import ApiResponse, get_request_args


agent_workspace_insights_bp = Blueprint('agent_workspace_insights', __name__)


def _get_agent_or_404(workspace_id: int, agent_id: int):
    agent = Agent.query.filter_by(id=agent_id, workspace_id=workspace_id).first()
    if not agent:
        return None, ApiResponse.not_found('Agent not found').to_response()
    return agent, None


def _iso(dt_value: Optional[datetime]) -> Optional[str]:
    if not dt_value:
        return None
    return dt_value.isoformat()


def _parse_iso_datetime(raw_value: Optional[str]) -> Optional[datetime]:
    text = str(raw_value or '').strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace('Z', '+00:00')).replace(tzinfo=None)
    except Exception:
        return None


def _parse_source_filter(raw_value: Optional[str]) -> Set[str]:
    value = str(raw_value or '').strip()
    if not value:
        return set()
    result = set()
    for item in value.split(','):
        normalized = item.strip().lower()
        if normalized:
            result.add(normalized)
    return result


def _value_to_int_list(raw_value: Any) -> List[int]:
    if not raw_value:
        return []

    values = raw_value if isinstance(raw_value, list) else [raw_value]
    result = []
    for item in values:
        try:
            result.append(int(item))
        except Exception:
            continue
    return result


def _parse_int_optional(raw_value: Any) -> Optional[int]:
    if raw_value is None:
        return None
    try:
        return int(str(raw_value).strip())
    except Exception:
        return None


def _safe_text(raw_value: Any, max_length: int = 240) -> str:
    text = str(raw_value or '').strip()
    if len(text) <= max_length:
        return text
    return f"{text[:max_length]}..."


def _activity_item_matches(
    item: Dict[str, Any],
    source_filter: Set[str],
    level_filter: Set[str],
    event_type_filter: str,
    query_text: str,
    task_id_filter: Optional[int] = None,
    project_id_filter: Optional[int] = None,
    run_id_filter: str = '',
    attempt_id_filter: str = '',
    actor_type_filter: str = '',
    min_risk_score: Optional[int] = None,
    max_risk_score: Optional[int] = None,
) -> bool:
    if source_filter and str(item.get('source') or '').lower() not in source_filter:
        return False

    if level_filter and str(item.get('level') or '').lower() not in level_filter:
        return False

    event_name = str(item.get('event_type') or '').lower()
    if event_type_filter and event_type_filter not in event_name:
        return False

    if task_id_filter is not None and _parse_int_optional(item.get('task_id')) != int(task_id_filter):
        return False

    if project_id_filter is not None and _parse_int_optional(item.get('project_id')) != int(project_id_filter):
        return False

    if run_id_filter:
        item_run_id = str(item.get('run_id') or '').strip().lower()
        if run_id_filter not in item_run_id:
            return False

    if attempt_id_filter:
        item_attempt_id = str(item.get('attempt_id') or '').strip().lower()
        if attempt_id_filter not in item_attempt_id:
            return False

    if actor_type_filter:
        if str(item.get('actor_type') or '').strip().lower() != actor_type_filter:
            return False

    if min_risk_score is not None:
        risk_score = _parse_int_optional(item.get('risk_score'))
        if risk_score is None or risk_score < int(min_risk_score):
            return False

    if max_risk_score is not None:
        risk_score = _parse_int_optional(item.get('risk_score'))
        if risk_score is None or risk_score > int(max_risk_score):
            return False

    if query_text:
        payload_text = json.dumps(item.get('payload') or {}, ensure_ascii=False)
        message_text = str(item.get('message') or '')
        haystack = f"{event_name} {message_text} {payload_text}".lower()
        if query_text not in haystack:
            return False

    return True


def _activity_sort_key(item: Dict[str, Any]):
    raw_time = item.get('occurred_at')
    if isinstance(raw_time, datetime):
        sort_time = raw_time
    else:
        sort_time = _parse_iso_datetime(str(raw_time or '')) or datetime.min
    return sort_time, int(item.get('_sort_id') or 0)


def _serialize_activity_item(item: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(item)
    for key, value in list(result.items()):
        if isinstance(value, datetime):
            result[key] = _iso(value)
    result.pop('_sort_id', None)
    return result


def _fetch_agent_audit_rows(audit_query, scan_limit: int, endpoint_name: str):
    """兼容历史库缺少新审计字段时的降级查询，避免整页失败。"""
    try:
        return (
            audit_query
            .order_by(AgentAuditEvent.occurred_at.desc(), AgentAuditEvent.id.desc())
            .limit(scan_limit)
            .all()
        )
    except OperationalError as exc:
        error_text = str(exc).lower()
        if 'unknown column' in error_text and 'agent_audit_events' in error_text:
            current_app.logger.warning(
                '[%s] Skip agent audit events due to schema mismatch: %s',
                endpoint_name,
                exc,
            )
            return []
        raise


def _build_task_context_map(task_ids: Set[int]) -> Dict[int, Dict[str, Any]]:
    if not task_ids:
        return {}
    rows = (
        db.session.query(
            Task.id.label('task_id'),
            Task.title.label('task_title'),
            Task.project_id.label('project_id'),
            Project.name.label('project_name'),
        )
        .join(Project, Project.id == Task.project_id)
        .filter(Task.id.in_(list(task_ids)))
        .all()
    )
    result: Dict[int, Dict[str, Any]] = {}
    for row in rows:
        result[int(row.task_id)] = {
            'task_title': row.task_title,
            'project_id': int(row.project_id) if row.project_id is not None else None,
            'project_name': row.project_name,
        }
    return result


def _build_project_name_map(project_ids: Set[int]) -> Dict[int, str]:
    if not project_ids:
        return {}
    rows = db.session.query(Project.id, Project.name).filter(Project.id.in_(list(project_ids))).all()
    return {int(row.id): row.name for row in rows}


def _build_agent_profile_map(agent_ids: Set[int]) -> Dict[int, Dict[str, Optional[str]]]:
    if not agent_ids:
        return {}
    rows = (
        db.session.query(Agent.id, Agent.name, Agent.display_name)
        .filter(Agent.id.in_(list(agent_ids)))
        .all()
    )
    result: Dict[int, Dict[str, Optional[str]]] = {}
    for row in rows:
        result[int(row.id)] = {
            'name': row.name,
            'display_name': row.display_name,
        }
    return result


@agent_workspace_insights_bp.route('/workspaces/<int:workspace_id>/agents/<int:agent_id>/insights/activity', methods=['GET'])
@unified_auth_required
def list_agent_activity(workspace_id: int, agent_id: int):
    user = get_current_user()
    agent, err = _get_agent_or_404(workspace_id, agent_id)
    if err:
        return err

    access_err = ensure_workspace_access(user, agent.workspace)
    if access_err:
        return access_err

    args = get_request_args()
    page = max(args['page'], 1)
    per_page = min(max(args['per_page'], 1), 100)

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

    default_scan_limit = max(page * per_page * 8, 400)
    scan_limit = min(max(int(request.args.get('scan_limit') or default_scan_limit), 100), 4000)

    activity_items: List[Dict[str, Any]] = []
    related_task_ids: Set[int] = set()
    related_project_ids: Set[int] = set()

    run_query = AgentRun.query.filter_by(workspace_id=workspace_id, agent_id=agent_id)
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
                'task_id': task_id,
                'project_id': project_id,
                '_sort_id': row.id,
            }
        )

    attempt_query = AgentTaskAttempt.query.filter_by(workspace_id=workspace_id, agent_id=agent_id)
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
                'task_id': task_id,
                '_sort_id': row.id,
            }
        )

    event_query = AgentTaskEvent.query.filter_by(workspace_id=workspace_id, agent_id=agent_id)
    if since:
        event_query = event_query.filter(AgentTaskEvent.event_timestamp >= since)
    if until:
        event_query = event_query.filter(AgentTaskEvent.event_timestamp <= until)
    event_rows = event_query.order_by(AgentTaskEvent.event_timestamp.desc(), AgentTaskEvent.id.desc()).limit(scan_limit).all()
    for row in event_rows:
        related_task_ids.add(int(row.task_id))
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
                'task_id': int(row.task_id),
                '_sort_id': row.id,
            }
        )

    log_query = TaskLog.query.filter_by(actor_agent_id=agent_id)
    if since:
        log_query = log_query.filter(TaskLog.created_at >= since)
    if until:
        log_query = log_query.filter(TaskLog.created_at <= until)
    log_rows = log_query.order_by(TaskLog.created_at.desc(), TaskLog.id.desc()).limit(scan_limit).all()
    for row in log_rows:
        related_task_ids.add(int(row.task_id))
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
                'task_id': int(row.task_id),
                '_sort_id': row.id,
            }
        )

    audit_query = AgentAuditEvent.query.filter(
        AgentAuditEvent.workspace_id == workspace_id,
        or_(
            (AgentAuditEvent.actor_type == 'agent') & (AgentAuditEvent.actor_id == str(agent_id)),
            (AgentAuditEvent.target_type == 'agent') & (AgentAuditEvent.target_id == str(agent_id)),
        ),
    )
    if since:
        audit_query = audit_query.filter(AgentAuditEvent.occurred_at >= since)
    if until:
        audit_query = audit_query.filter(AgentAuditEvent.occurred_at <= until)
    audit_rows = _fetch_agent_audit_rows(
        audit_query=audit_query,
        scan_limit=scan_limit,
        endpoint_name='list_agent_activity',
    )
    for row in audit_rows:
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
                'actor_agent_id': _parse_int_optional(getattr(row, 'actor_agent_id', None)),
                'target_agent_id': _parse_int_optional(getattr(row, 'target_agent_id', None)),
                'duration_ms': _parse_int_optional(getattr(row, 'duration_ms', None)),
                'error_code': getattr(row, 'error_code', None),
                '_sort_id': row.id,
            }
        )

    task_context_map = _build_task_context_map(related_task_ids)
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
        'Agent activity retrieved successfully',
    ).to_response()


@agent_workspace_insights_bp.route('/workspaces/<int:workspace_id>/agents/<int:agent_id>/insights/projects', methods=['GET'])
@unified_auth_required
def list_agent_projects(workspace_id: int, agent_id: int):
    user = get_current_user()
    agent, err = _get_agent_or_404(workspace_id, agent_id)
    if err:
        return err

    access_err = ensure_workspace_access(user, agent.workspace)
    if access_err:
        return access_err

    args = get_request_args()
    page = max(args['page'], 1)
    per_page = min(max(args['per_page'], 1), 100)
    search_text = str(args.get('search') or '').strip().lower()

    allowed_project_ids = set(_value_to_int_list(agent.allowed_project_ids))

    attempt_stats_rows = (
        db.session.query(
            Task.project_id.label('project_id'),
            func.count(func.distinct(AgentTaskAttempt.task_id)).label('attempt_task_count'),
            func.sum(
                case(
                    (AgentTaskAttempt.state == AgentTaskAttemptState.COMMITTED, 1),
                    else_=0,
                )
            ).label('committed_count'),
            func.max(func.coalesce(AgentTaskAttempt.ended_at, AgentTaskAttempt.started_at)).label('last_attempt_at'),
        )
        .join(Task, Task.id == AgentTaskAttempt.task_id)
        .join(Project, Project.id == Task.project_id)
        .filter(
            AgentTaskAttempt.workspace_id == workspace_id,
            AgentTaskAttempt.agent_id == agent_id,
            Project.organization_id == workspace_id,
        )
        .group_by(Task.project_id)
        .all()
    )
    attempt_stats = {int(row.project_id): row for row in attempt_stats_rows}

    log_stats_rows = (
        db.session.query(
            Task.project_id.label('project_id'),
            func.count(TaskLog.id).label('log_count'),
            func.count(func.distinct(TaskLog.task_id)).label('log_task_count'),
            func.max(TaskLog.created_at).label('last_log_at'),
        )
        .join(Task, Task.id == TaskLog.task_id)
        .join(Project, Project.id == Task.project_id)
        .filter(
            TaskLog.actor_agent_id == agent_id,
            Project.organization_id == workspace_id,
        )
        .group_by(Task.project_id)
        .all()
    )
    log_stats = {int(row.project_id): row for row in log_stats_rows}

    project_ids = set(allowed_project_ids)
    project_ids.update(attempt_stats.keys())
    project_ids.update(log_stats.keys())

    if not project_ids:
        return ApiResponse.success(
            {
                'items': [],
                'pagination': {
                    'page': page,
                    'per_page': per_page,
                    'total': 0,
                    'has_prev': False,
                    'has_next': False,
                },
            },
            'Agent projects retrieved successfully',
        ).to_response()

    project_rows = Project.query.filter(
        Project.id.in_(list(project_ids)),
        Project.organization_id == workspace_id,
    ).all()

    items = []
    for project in project_rows:
        attempt_row = attempt_stats.get(project.id)
        log_row = log_stats.get(project.id)

        attempt_task_count = int(getattr(attempt_row, 'attempt_task_count', 0) or 0)
        log_task_count = int(getattr(log_row, 'log_task_count', 0) or 0)
        touched_task_count = max(attempt_task_count, log_task_count)
        committed_count = int(getattr(attempt_row, 'committed_count', 0) or 0)
        interaction_log_count = int(getattr(log_row, 'log_count', 0) or 0)
        last_attempt_at = getattr(attempt_row, 'last_attempt_at', None)
        last_log_at = getattr(log_row, 'last_log_at', None)

        last_activity_at = last_attempt_at
        if last_log_at and (not last_activity_at or last_log_at > last_activity_at):
            last_activity_at = last_log_at

        item = {
            'project_id': project.id,
            'project_name': project.name,
            'project_status': project.status.value if hasattr(project.status, 'value') else str(project.status),
            'project_color': project.color,
            'is_explicitly_allowed': project.id in allowed_project_ids,
            'touched_task_count': touched_task_count,
            'committed_task_count': committed_count,
            'interaction_log_count': interaction_log_count,
            'last_activity_at': _iso(last_activity_at),
        }
        items.append(item)

    if search_text:
        items = [
            row
            for row in items
            if search_text in str(row.get('project_name') or '').lower()
        ]

    items.sort(
        key=lambda row: (
            _parse_iso_datetime(row.get('last_activity_at')) or datetime.min,
            int(row.get('project_id') or 0),
        ),
        reverse=True,
    )

    total = len(items)
    start = (page - 1) * per_page
    end = start + per_page

    return ApiResponse.success(
        {
            'items': items[start:end],
            'pagination': {
                'page': page,
                'per_page': per_page,
                'total': total,
                'has_prev': page > 1,
                'has_next': page * per_page < total,
            },
        },
        'Agent projects retrieved successfully',
    ).to_response()


def _touched_task_ids_subquery(workspace_id: int, agent_id: int):
    attempt_task_ids = (
        db.session.query(AgentTaskAttempt.task_id.label('task_id'))
        .join(Task, Task.id == AgentTaskAttempt.task_id)
        .join(Project, Project.id == Task.project_id)
        .filter(
            AgentTaskAttempt.workspace_id == workspace_id,
            AgentTaskAttempt.agent_id == agent_id,
            Project.organization_id == workspace_id,
        )
    )
    log_task_ids = (
        db.session.query(TaskLog.task_id.label('task_id'))
        .join(Task, Task.id == TaskLog.task_id)
        .join(Project, Project.id == Task.project_id)
        .filter(
            TaskLog.actor_agent_id == agent_id,
            Project.organization_id == workspace_id,
        )
    )
    return attempt_task_ids.union(log_task_ids).subquery()


@agent_workspace_insights_bp.route('/workspaces/<int:workspace_id>/agents/<int:agent_id>/insights/interactions', methods=['GET'])
@unified_auth_required
def list_agent_interactions(workspace_id: int, agent_id: int):
    user = get_current_user()
    agent, err = _get_agent_or_404(workspace_id, agent_id)
    if err:
        return err

    access_err = ensure_workspace_access(user, agent.workspace)
    if access_err:
        return access_err

    args = get_request_args()
    page = max(args['page'], 1)
    per_page = min(max(args['per_page'], 1), 100)
    search_text = str(args.get('search') or '').strip().lower()

    touched_task_ids = _touched_task_ids_subquery(workspace_id, agent_id)

    interaction_query = (
        db.session.query(
            TaskLog.actor_user_id.label('user_id'),
            User.email.label('email'),
            User.username.label('username'),
            User.nickname.label('nickname'),
            User.full_name.label('full_name'),
            User.avatar_url.label('avatar_url'),
            func.count(TaskLog.id).label('interaction_count'),
            func.count(func.distinct(TaskLog.task_id)).label('task_count'),
            func.max(TaskLog.created_at).label('last_interaction_at'),
        )
        .join(User, User.id == TaskLog.actor_user_id)
        .filter(
            TaskLog.task_id.in_(db.session.query(touched_task_ids.c.task_id)),
            TaskLog.actor_user_id.isnot(None),
        )
        .group_by(
            TaskLog.actor_user_id,
            User.email,
            User.username,
            User.nickname,
            User.full_name,
            User.avatar_url,
        )
    )

    if search_text:
        like = f"%{search_text}%"
        interaction_query = interaction_query.filter(
            or_(
                User.email.like(like),
                User.username.like(like),
                User.nickname.like(like),
                User.full_name.like(like),
            )
        )

    total = interaction_query.count()
    rows = (
        interaction_query
        .order_by(func.max(TaskLog.created_at).desc(), TaskLog.actor_user_id.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    items = []
    for row in rows:
        display_name = row.full_name or row.nickname or row.username or row.email or f"User #{row.user_id}"
        items.append(
            {
                'user_id': int(row.user_id),
                'display_name': display_name,
                'email': row.email,
                'avatar_url': row.avatar_url,
                'interaction_count': int(row.interaction_count or 0),
                'task_count': int(row.task_count or 0),
                'last_interaction_at': _iso(row.last_interaction_at),
            }
        )

    return ApiResponse.success(
        {
            'items': items,
            'pagination': {
                'page': page,
                'per_page': per_page,
                'total': total,
                'has_prev': page > 1,
                'has_next': page * per_page < total,
            },
        },
        'Agent interactions retrieved successfully',
    ).to_response()


@agent_workspace_insights_bp.route('/workspaces/<int:workspace_id>/agents/<int:agent_id>/insights/tasks', methods=['GET'])
@unified_auth_required
def list_agent_tasks(workspace_id: int, agent_id: int):
    user = get_current_user()
    agent, err = _get_agent_or_404(workspace_id, agent_id)
    if err:
        return err

    access_err = ensure_workspace_access(user, agent.workspace)
    if access_err:
        return access_err

    args = get_request_args()
    page = max(args['page'], 1)
    per_page = min(max(args['per_page'], 1), 100)
    search_text = str(args.get('search') or '').strip()
    status_filter = str(request.args.get('status') or '').strip().lower()
    project_id_filter = request.args.get('project_id', type=int)

    touched_task_ids = _touched_task_ids_subquery(workspace_id, agent_id)
    query = (
        Task.query
        .join(Project, Project.id == Task.project_id)
        .filter(
            Project.organization_id == workspace_id,
            Task.id.in_(db.session.query(touched_task_ids.c.task_id)),
        )
    )

    if search_text:
        like = f"%{search_text}%"
        query = query.filter(or_(Task.title.like(like), Task.content.like(like)))

    if status_filter:
        if status_filter not in {item.value for item in TaskStatus}:
            return ApiResponse.error('Invalid status filter', 400).to_response()
        query = query.filter(Task.status == TaskStatus(status_filter))

    if project_id_filter:
        query = query.filter(Task.project_id == project_id_filter)

    total = query.count()
    task_rows = (
        query
        .order_by(Task.updated_at.desc(), Task.id.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    task_ids = [int(row.id) for row in task_rows]
    attempt_rows = (
        AgentTaskAttempt.query
        .filter(
            AgentTaskAttempt.workspace_id == workspace_id,
            AgentTaskAttempt.agent_id == agent_id,
            AgentTaskAttempt.task_id.in_(task_ids if task_ids else [-1]),
        )
        .order_by(AgentTaskAttempt.started_at.desc(), AgentTaskAttempt.id.desc())
        .all()
    )
    last_attempt_by_task: Dict[int, AgentTaskAttempt] = {}
    for row in attempt_rows:
        task_id = int(row.task_id)
        if task_id not in last_attempt_by_task:
            last_attempt_by_task[task_id] = row

    log_stats_rows = (
        db.session.query(
            TaskLog.task_id,
            func.count(TaskLog.id).label('log_count'),
            func.max(TaskLog.created_at).label('last_log_at'),
        )
        .filter(
            TaskLog.actor_agent_id == agent_id,
            TaskLog.task_id.in_(task_ids if task_ids else [-1]),
        )
        .group_by(TaskLog.task_id)
        .all()
    )
    log_stats = {int(row.task_id): row for row in log_stats_rows}

    items = []
    for task in task_rows:
        last_attempt = last_attempt_by_task.get(int(task.id))
        log_stat = log_stats.get(int(task.id))
        last_attempt_activity = None
        if last_attempt:
            last_attempt_activity = last_attempt.ended_at or last_attempt.started_at
        last_log_at = getattr(log_stat, 'last_log_at', None)
        last_activity_at = last_attempt_activity
        if last_log_at and (not last_activity_at or last_log_at > last_activity_at):
            last_activity_at = last_log_at

        items.append(
            {
                'task_id': int(task.id),
                'title': task.title,
                'status': task.status.value if hasattr(task.status, 'value') else str(task.status),
                'priority': task.priority.value if hasattr(task.priority, 'value') else str(task.priority),
                'project_id': int(task.project_id),
                'project_name': task.project.name if task.project else None,
                'updated_at': _iso(task.updated_at),
                'completed_at': _iso(task.completed_at),
                'last_activity_at': _iso(last_activity_at),
                'last_attempt': (
                    {
                        'attempt_id': last_attempt.attempt_id,
                        'state': last_attempt.state.value if hasattr(last_attempt.state, 'value') else str(last_attempt.state),
                        'started_at': _iso(last_attempt.started_at),
                        'ended_at': _iso(last_attempt.ended_at),
                        'failure_code': last_attempt.failure_code,
                        'failure_reason': last_attempt.failure_reason,
                    }
                    if last_attempt
                    else None
                ),
                'agent_log_count': int(getattr(log_stat, 'log_count', 0) or 0),
            }
        )

    return ApiResponse.success(
        {
            'items': items,
            'pagination': {
                'page': page,
                'per_page': per_page,
                'total': total,
                'has_prev': page > 1,
                'has_next': page * per_page < total,
            },
        },
        'Agent tasks retrieved successfully',
    ).to_response()



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
