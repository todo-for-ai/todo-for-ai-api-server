from typing import Dict, Optional, Set, Tuple

from flask import request
from sqlalchemy import and_, or_

from core.auth import get_current_user, unified_auth_required
from models import Agent, AgentActivityEvent, Project, Task, db

from ..agent_common import ensure_workspace_access, get_workspace_or_404
from ..base import ApiResponse
from . import agent_workspace_insights_bp
from .shared import _parse_int_optional, _parse_iso_datetime, _parse_source_filter


def _parse_limit(raw_value: Optional[str]) -> int:
    try:
        value = int(str(raw_value or '50').strip())
    except Exception:
        value = 50
    return min(max(value, 1), 200)


def _decode_cursor(raw_value: Optional[str]) -> Tuple[Optional[object], Optional[int]]:
    text = str(raw_value or '').strip()
    if not text:
        return None, None
    parts = text.rsplit('|', 1)
    if len(parts) != 2:
        return None, None
    occurred_at = _parse_iso_datetime(parts[0])
    row_id = _parse_int_optional(parts[1])
    if occurred_at is None or row_id is None:
        return None, None
    return occurred_at, int(row_id)


def _encode_cursor(occurred_at, row_id: int) -> str:
    return f"{occurred_at.isoformat()}|{int(row_id)}"


@agent_workspace_insights_bp.route('/workspaces/<int:workspace_id>/insights/activity-events', methods=['GET'])
@unified_auth_required
def list_workspace_activity_events(workspace_id: int):
    user = get_current_user()
    workspace, err = get_workspace_or_404(workspace_id)
    if err:
        return err

    access_err = ensure_workspace_access(user, workspace)
    if access_err:
        return access_err

    limit = _parse_limit(request.args.get('limit'))
    source_filter = _parse_source_filter(request.args.get('source'))
    level_filter = _parse_source_filter(request.args.get('level'))
    event_type_filter = str(request.args.get('event_type') or '').strip().lower()
    agent_id_filter = request.args.get('agent_id', type=int)
    task_id_filter = request.args.get('task_id', type=int)
    project_id_filter = request.args.get('project_id', type=int)
    run_id_filter = str(request.args.get('run_id') or '').strip().lower()
    attempt_id_filter = str(request.args.get('attempt_id') or '').strip().lower()
    query_text = str(request.args.get('q') or '').strip().lower()
    since = _parse_iso_datetime(request.args.get('from'))
    until = _parse_iso_datetime(request.args.get('to'))

    cursor_dt, cursor_id = _decode_cursor(request.args.get('cursor'))
    if request.args.get('cursor') and (cursor_dt is None or cursor_id is None):
        return ApiResponse.error('Invalid cursor format', 400).to_response()

    query = AgentActivityEvent.query.filter(AgentActivityEvent.workspace_id == workspace_id)

    if source_filter:
        query = query.filter(AgentActivityEvent.source.in_(list(source_filter)))
    if level_filter:
        query = query.filter(AgentActivityEvent.level.in_(list(level_filter)))
    if event_type_filter:
        query = query.filter(AgentActivityEvent.event_type.ilike(f"%{event_type_filter}%"))
    if agent_id_filter is not None:
        query = query.filter(AgentActivityEvent.agent_id == int(agent_id_filter))
    if task_id_filter is not None:
        query = query.filter(AgentActivityEvent.task_id == int(task_id_filter))
    if project_id_filter is not None:
        query = query.filter(AgentActivityEvent.project_id == int(project_id_filter))
    if run_id_filter:
        query = query.filter(AgentActivityEvent.run_id.ilike(f"%{run_id_filter}%"))
    if attempt_id_filter:
        query = query.filter(AgentActivityEvent.attempt_id.ilike(f"%{attempt_id_filter}%"))
    if query_text:
        query = query.filter(
            or_(
                AgentActivityEvent.event_type.ilike(f"%{query_text}%"),
                AgentActivityEvent.message.ilike(f"%{query_text}%"),
            )
        )
    if since:
        query = query.filter(AgentActivityEvent.occurred_at >= since)
    if until:
        query = query.filter(AgentActivityEvent.occurred_at <= until)
    if cursor_dt and cursor_id:
        query = query.filter(
            or_(
                AgentActivityEvent.occurred_at < cursor_dt,
                and_(
                    AgentActivityEvent.occurred_at == cursor_dt,
                    AgentActivityEvent.id < cursor_id,
                ),
            )
        )

    rows = (
        query
        .order_by(AgentActivityEvent.occurred_at.desc(), AgentActivityEvent.id.desc())
        .limit(limit + 1)
        .all()
    )

    has_more = len(rows) > limit
    rows = rows[:limit]

    agent_ids: Set[int] = set()
    task_ids: Set[int] = set()
    project_ids: Set[int] = set()
    for row in rows:
        if row.agent_id is not None:
            agent_ids.add(int(row.agent_id))
        if row.task_id is not None:
            task_ids.add(int(row.task_id))
        if row.project_id is not None:
            project_ids.add(int(row.project_id))

    agent_rows = (
        db.session.query(Agent.id, Agent.name, Agent.display_name)
        .filter(Agent.id.in_(list(agent_ids if agent_ids else {-1})))
        .all()
    )
    agent_map: Dict[int, Dict[str, Optional[str]]] = {
        int(item.id): {
            'name': item.name,
            'display_name': item.display_name,
        }
        for item in agent_rows
    }

    task_rows = (
        db.session.query(Task.id, Task.title, Task.project_id)
        .filter(Task.id.in_(list(task_ids if task_ids else {-1})))
        .all()
    )
    task_map: Dict[int, Dict[str, Optional[object]]] = {
        int(item.id): {
            'title': item.title,
            'project_id': int(item.project_id) if item.project_id is not None else None,
        }
        for item in task_rows
    }
    for task_meta in task_map.values():
        project_id = _parse_int_optional(task_meta.get('project_id'))
        if project_id is not None:
            project_ids.add(int(project_id))

    project_rows = (
        db.session.query(Project.id, Project.name)
        .filter(Project.id.in_(list(project_ids if project_ids else {-1})))
        .all()
    )
    project_map: Dict[int, str] = {
        int(item.id): item.name
        for item in project_rows
    }

    items = []
    for row in rows:
        item = row.to_dict()
        item['agent'] = None
        if row.agent_id is not None:
            aid = int(row.agent_id)
            item['agent'] = {
                'id': aid,
                'name': agent_map.get(aid, {}).get('name'),
                'display_name': agent_map.get(aid, {}).get('display_name'),
            }

        item['task'] = None
        if row.task_id is not None:
            tid = int(row.task_id)
            task_meta = task_map.get(tid, {})
            item['task'] = {
                'id': tid,
                'title': task_meta.get('title'),
                'project_id': task_meta.get('project_id'),
                'project_name': project_map.get(_parse_int_optional(task_meta.get('project_id')) or -1),
            }

        item['project'] = None
        if row.project_id is not None:
            pid = int(row.project_id)
            item['project'] = {
                'id': pid,
                'name': project_map.get(pid),
            }
        items.append(item)

    next_cursor = None
    if has_more and rows:
        last_row = rows[-1]
        next_cursor = _encode_cursor(last_row.occurred_at, int(last_row.id))

    return ApiResponse.success(
        data={
            'items': items,
            'page_info': {
                'limit': limit,
                'has_more': has_more,
                'next_cursor': next_cursor,
            },
        },
        message='Workspace activity events retrieved successfully',
    ).to_response()

