import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from flask import current_app
from sqlalchemy.exc import OperationalError

from models import Agent, AgentAuditEvent, AgentTaskAttempt, Project, Task, TaskLog, db

from ..base import ApiResponse


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
