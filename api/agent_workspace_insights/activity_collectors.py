"""五类事件源的活动条目收集器（agent 视角与工作区视角共用）。

activity.py 与 workspace_activities.py 原是两份约 90% 同构的聚合循环；
本模块以 ActivityScope 参数化两者差异（是否携带 agent 归属、审计匹配
口径），逐字段保持原输出不变——行为由
tests/unit/api/test_agent_workspace_insights_api.py 钉住。
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

from flask import request
from sqlalchemy import or_

from models import (
    Agent,
    AgentAuditEvent,
    AgentRun,
    AgentTaskAttempt,
    AgentTaskEvent,
    TaskLog,
)

from ..base import ApiResponse, get_request_args
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


@dataclass
class ActivityScope:
    """活动聚合的查询口径。

    agent_id      —— agent 视角的固定主体；工作区视角为 None。
    agent_id_filter —— 工作区视角的 ?agent_id= 缩小条件。
    include_agent —— 工作区视角为 True：条目带 agent_id 并回填档案。
    """

    workspace_id: int
    agent_id: Optional[int] = None
    agent_id_filter: Optional[int] = None
    since: Optional[datetime] = None
    until: Optional[datetime] = None
    scan_limit: int = 400
    include_agent: bool = False


@dataclass
class CollectionResult:
    items: List[Dict[str, Any]] = field(default_factory=list)
    task_ids: Set[int] = field(default_factory=set)
    project_ids: Set[int] = field(default_factory=set)
    agent_ids: Set[int] = field(default_factory=set)


def build_activity_feed_response(workspace_id: int, agent_id: Optional[int],
                                 message: str, default_scan_floor: int,
                                 max_scan_limit: int, endpoint_name: str):
    """聚合端点的统一装配：解析参数 → 五源收集 → 富化 → 过滤切页汇总。

    agent 视角传 agent_id；工作区视角传 None 并由 ?agent_id= 缩小。
    """
    args = get_request_args()
    page = max(args['page'], 1)
    per_page = min(max(args['per_page'], 1), 100)

    default_scan = max(page * per_page * 8, default_scan_floor)
    scan_limit = min(
        max(int(request.args.get('scan_limit') or default_scan), 100),
        max_scan_limit,
    )

    scope = ActivityScope(
        workspace_id=workspace_id,
        agent_id=agent_id,
        agent_id_filter=request.args.get('agent_id', type=int),
        since=_parse_iso_datetime(request.args.get('from')),
        until=_parse_iso_datetime(request.args.get('to')),
        scan_limit=scan_limit,
        include_agent=agent_id is None,
    )
    result = collect_activity_items(scope, endpoint_name)
    enrich_activity_items(result)

    filters = {
        'source_filter': _parse_source_filter(request.args.get('source')),
        'level_filter': _parse_source_filter(request.args.get('level')),
        'event_type_filter': str(request.args.get('event_type') or '').strip().lower(),
        'query_text': str(request.args.get('q') or '').strip().lower(),
        'task_id_filter': request.args.get('task_id', type=int),
        'project_id_filter': request.args.get('project_id', type=int),
        'run_id_filter': str(request.args.get('run_id') or '').strip().lower(),
        'attempt_id_filter': str(request.args.get('attempt_id') or '').strip().lower(),
        'actor_type_filter': str(request.args.get('actor_type') or '').strip().lower(),
        'min_risk_score': request.args.get('min_risk_score', type=int),
        'max_risk_score': request.args.get('max_risk_score', type=int),
    }
    items, summary, pagination = finalize_activity_page(
        result.items, page, per_page, filters)
    summary['scan_limit'] = scan_limit

    return ApiResponse.success(
        {'items': items, 'summary': summary, 'pagination': pagination},
        message,
    ).to_response()


def _collect_runs(scope: ActivityScope, result: CollectionResult) -> None:
    query = AgentRun.query.filter_by(workspace_id=scope.workspace_id)
    if scope.agent_id is not None:
        query = query.filter_by(agent_id=scope.agent_id)
    if scope.agent_id_filter:
        query = query.filter(AgentRun.agent_id == scope.agent_id_filter)
    if scope.since:
        query = query.filter(AgentRun.scheduled_at >= scope.since)
    if scope.until:
        query = query.filter(AgentRun.scheduled_at <= scope.until)
    rows = query.order_by(
        AgentRun.scheduled_at.desc(), AgentRun.id.desc()
    ).limit(scope.scan_limit).all()

    for row in rows:
        payload = row.input_payload or {}
        task_id = _parse_int_optional(payload.get('task_id'))
        project_id = _parse_int_optional(payload.get('project_id'))
        if task_id:
            result.task_ids.add(task_id)
        if project_id:
            result.project_ids.add(project_id)

        agent_id = None
        if scope.include_agent:
            agent_id = int(row.agent_id)
            result.agent_ids.add(agent_id)

        row_state = str(row.state or '').lower()
        failure_reason = _safe_text(row.failure_reason or '')
        summary = f"Run {row.run_id} {row_state}"
        if failure_reason:
            summary = f"{summary}: {failure_reason}"

        item = {
            'id': f"run:{row.id}",
            'entity_id': int(row.id),
            'source': 'agent_run',
            'event_type': f"run.{row_state}",
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
        if scope.include_agent:
            item['agent_id'] = agent_id
        result.items.append(item)


def _collect_attempts(scope: ActivityScope, result: CollectionResult) -> None:
    query = AgentTaskAttempt.query.filter_by(workspace_id=scope.workspace_id)
    if scope.agent_id is not None:
        query = query.filter_by(agent_id=scope.agent_id)
    if scope.agent_id_filter:
        query = query.filter(AgentTaskAttempt.agent_id == scope.agent_id_filter)
    if scope.since:
        query = query.filter(
            or_(
                AgentTaskAttempt.started_at >= scope.since,
                AgentTaskAttempt.ended_at >= scope.since,
            )
        )
    if scope.until:
        query = query.filter(AgentTaskAttempt.started_at <= scope.until)
    rows = query.order_by(
        AgentTaskAttempt.started_at.desc(), AgentTaskAttempt.id.desc()
    ).limit(scope.scan_limit).all()

    for row in rows:
        task_id = int(row.task_id)
        result.task_ids.add(task_id)

        agent_id = None
        if scope.include_agent:
            agent_id = int(row.agent_id)
            result.agent_ids.add(agent_id)

        row_state = str(row.state.value if row.state else '').lower()
        occurred_at = row.ended_at or row.started_at
        level = 'error' if row_state == 'aborted' else 'info'
        summary = f"Attempt {row.attempt_id} {row_state}"
        if row.failure_reason:
            summary = f"{summary}: {_safe_text(row.failure_reason)}"

        item = {
            'id': f"attempt:{row.id}",
            'entity_id': int(row.id),
            'source': 'agent_task_attempt',
            'event_type': f"attempt.{row_state}",
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
        if scope.include_agent:
            item['agent_id'] = agent_id
        result.items.append(item)


def _collect_task_events(scope: ActivityScope, result: CollectionResult) -> None:
    query = AgentTaskEvent.query.filter_by(workspace_id=scope.workspace_id)
    if scope.agent_id is not None:
        query = query.filter_by(agent_id=scope.agent_id)
    if scope.agent_id_filter:
        query = query.filter(AgentTaskEvent.agent_id == scope.agent_id_filter)
    if scope.since:
        query = query.filter(AgentTaskEvent.event_timestamp >= scope.since)
    if scope.until:
        query = query.filter(AgentTaskEvent.event_timestamp <= scope.until)
    rows = query.order_by(
        AgentTaskEvent.event_timestamp.desc(), AgentTaskEvent.id.desc()
    ).limit(scope.scan_limit).all()

    for row in rows:
        task_id = int(row.task_id)
        result.task_ids.add(task_id)

        agent_id = None
        if scope.include_agent:
            agent_id = int(row.agent_id)
            result.agent_ids.add(agent_id)

        row_event_type = str(row.event_type or '').strip().lower()
        item = {
            'id': f"event:{row.id}",
            'entity_id': int(row.id),
            'source': 'agent_task_event',
            'event_type': f"event.{row_event_type}",
            'level': 'error' if row_event_type in {'error', 'failed'} else 'info',
            'message': _safe_text(row.message or f"Event {row_event_type}", 300),
            'payload': row.payload or {},
            'occurred_at': row.event_timestamp,
            'attempt_id': row.attempt_id,
            'seq': int(row.seq or 0),
            'state': row_event_type,
            'task_id': task_id,
            '_sort_id': row.id,
        }
        if scope.include_agent:
            item['agent_id'] = agent_id
        result.items.append(item)


def _collect_task_logs(scope: ActivityScope, result: CollectionResult) -> None:
    if scope.agent_id is not None:
        query = TaskLog.query.filter_by(actor_agent_id=scope.agent_id)
    else:
        query = (
            TaskLog.query
            .join(Agent, Agent.id == TaskLog.actor_agent_id)
            .filter(Agent.workspace_id == scope.workspace_id)
        )
        if scope.agent_id_filter:
            query = query.filter(TaskLog.actor_agent_id == scope.agent_id_filter)
    if scope.since:
        query = query.filter(TaskLog.created_at >= scope.since)
    if scope.until:
        query = query.filter(TaskLog.created_at <= scope.until)
    rows = query.order_by(
        TaskLog.created_at.desc(), TaskLog.id.desc()
    ).limit(scope.scan_limit).all()

    for row in rows:
        task_id = int(row.task_id)
        result.task_ids.add(task_id)

        agent_id = None
        if scope.include_agent:
            agent_id = int(row.actor_agent_id)
            result.agent_ids.add(agent_id)

        item = {
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
            'task_id': task_id,
            '_sort_id': row.id,
        }
        if scope.include_agent:
            item['agent_id'] = agent_id
        result.items.append(item)


def _collect_audit_events(scope: ActivityScope, endpoint_name: str,
                          result: CollectionResult) -> None:
    if scope.agent_id is not None:
        agent_text = str(scope.agent_id)
        query = AgentAuditEvent.query.filter(
            AgentAuditEvent.workspace_id == scope.workspace_id,
            or_(
                (AgentAuditEvent.actor_type == 'agent') & (AgentAuditEvent.actor_id == agent_text),
                (AgentAuditEvent.target_type == 'agent') & (AgentAuditEvent.target_id == agent_text),
            ),
        )
    else:
        query = AgentAuditEvent.query.filter(
            AgentAuditEvent.workspace_id == scope.workspace_id,
            or_(
                AgentAuditEvent.actor_type == 'agent',
                AgentAuditEvent.target_type == 'agent',
            ),
        )
        if scope.agent_id_filter:
            agent_text = str(scope.agent_id_filter)
            query = query.filter(
                or_(
                    (AgentAuditEvent.actor_type == 'agent') & (AgentAuditEvent.actor_id == agent_text),
                    (AgentAuditEvent.target_type == 'agent') & (AgentAuditEvent.target_id == agent_text),
                )
            )
    if scope.since:
        query = query.filter(AgentAuditEvent.occurred_at >= scope.since)
    if scope.until:
        query = query.filter(AgentAuditEvent.occurred_at <= scope.until)
    rows = _fetch_agent_audit_rows(
        audit_query=query,
        scan_limit=scope.scan_limit,
        endpoint_name=endpoint_name,
    )

    for row in rows:
        actor_agent_id = _parse_int_optional(getattr(row, 'actor_agent_id', None))
        target_agent_id = _parse_int_optional(getattr(row, 'target_agent_id', None))
        item_agent_id = None
        if scope.include_agent:
            if actor_agent_id is None and str(row.actor_type or '') == 'agent':
                actor_agent_id = _parse_int_optional(row.actor_id)
            if target_agent_id is None and str(row.target_type or '') == 'agent':
                target_agent_id = _parse_int_optional(row.target_id)
            item_agent_id = actor_agent_id or target_agent_id
            if not item_agent_id:
                continue
            result.agent_ids.add(item_agent_id)

        level = str(row.level or '').strip().lower()
        if level not in {'info', 'warn', 'error'}:
            level = 'error' if int(row.risk_score or 0) >= 50 else ('warn' if int(row.risk_score or 0) >= 20 else 'info')

        audit_task_id = _parse_int_optional(getattr(row, 'task_id', None))
        audit_project_id = _parse_int_optional(getattr(row, 'project_id', None))
        if audit_task_id:
            result.task_ids.add(audit_task_id)
        if audit_project_id:
            result.project_ids.add(audit_project_id)

        item = {
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
            '_sort_id': row.id,
        }
        if scope.include_agent:
            item['agent_id'] = item_agent_id
        result.items.append(item)


def collect_activity_items(scope: ActivityScope, endpoint_name: str) -> CollectionResult:
    """跑全五类收集器；endpoint_name 仅用于降级日志定位。"""
    result = CollectionResult()
    _collect_runs(scope, result)
    _collect_attempts(scope, result)
    _collect_task_events(scope, result)
    _collect_task_logs(scope, result)
    _collect_audit_events(scope, endpoint_name, result)
    return result


def enrich_activity_items(result: CollectionResult) -> None:
    """回填任务标题/项目归属/项目名；工作区视角另回填 agent 档案。"""
    task_context_map = _build_task_context_map(result.task_ids)
    agent_profile_map = (
        _build_agent_profile_map(result.agent_ids)
        if result.agent_ids else {}
    )

    for item in result.items:
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
            result.project_ids.add(int(project_id))

        if result.agent_ids:
            item_agent_id = item.get('agent_id')
            if item_agent_id:
                profile = agent_profile_map.get(int(item_agent_id)) or {}
                item['agent_name'] = profile.get('name')
                item['agent_display_name'] = profile.get('display_name')

    project_name_map = _build_project_name_map(result.project_ids)
    for item in result.items:
        project_id = item.get('project_id')
        if project_id and not item.get('project_name'):
            item['project_name'] = project_name_map.get(int(project_id))


def finalize_activity_page(items: List[Dict[str, Any]], page: int, per_page: int,
                           filters: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Any], Dict[str, Any]]:
    """统一执行条目过滤 → 时间排序 → 切页 → 多维汇总。

    返回 (当前页条目, summary, pagination)，响应组装由
    build_activity_feed_response 负责。
    """
    matched_items = [
        item for item in items if _activity_item_matches(item=item, **filters)
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

    summary = {
        'sources': source_summary,
        'levels': level_summary,
        'event_types': event_type_summary,
    }
    pagination = {
        'page': page,
        'per_page': per_page,
        'total': total,
        'has_prev': page > 1,
        'has_next': page * per_page < total,
    }
    return [_serialize_activity_item(item) for item in page_items], summary, pagination
