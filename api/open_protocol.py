"""互操作开放事件协议（Phase 4：开放协议，为 Linear/Jira/GitLab 双向同步打底）

把平台内三类核心事件投影为统一开放 schema，供外部系统集成方消费：

- 任务事件（task）  ：TaskEventOutbox（状态流转、repo 代码事件等）
- 证据事件（evidence）：TaskEvidenceRecord（DoD 验证证据的产生与结论更新）
- 审批事件（approval）：AgentTaskEvent 中 interaction_request / interaction_approval

传输语义：游标分页（不透明 cursor，多源合并单调推进），幂等可重放——
消费方按 (occurred_at, id) 去重后写入自身系统即可实现至少一次投递。

- GET /workspaces/<id>/open/events?cursor=&limit=&category=  统一事件流
- GET /workspaces/<id>/open/schema                           事件 schema 自描述
"""

import base64
import json

from flask import Blueprint, request

from models import (
    AgentTaskEvent,
    Project,
    Task,
    TaskEvidenceRecord,
    TaskEventOutbox,
    db,
)
from core.auth import get_current_user, unified_auth_required
from .agent_common import ensure_workspace_manage_access, get_workspace_or_404
from .base import ApiResponse

open_protocol_bp = Blueprint('open_protocol', __name__)

CATEGORIES = ('task', 'evidence', 'approval')

# 审批类 AgentTaskEvent 的开放类型映射（其余运行时噪音事件不进入开放流）
APPROVAL_EVENT_TYPES = {
    'interaction_request': 'approval.requested',
    'interaction_approval': 'approval.decided',
}

_SCHEMA = {
    'version': 1,
    'transport': {
        'pagination': 'opaque cursor (base64url JSON of per-source last ids)',
        'delivery': 'at-least-once; dedupe by (id, occurred_at)',
        'ordering': 'occurred_at ascending within a page',
    },
    'categories': {
        'task': {
            'source': 'task_event_outbox',
            'types': 'source-defined, e.g. task.status_changed, repo.pull_request.merged, repo.issues.opened, repo.workflow_run.failure',
            'data': 'outbox payload verbatim',
        },
        'evidence': {
            'source': 'task_evidence_records',
            'type': 'evidence.recorded',
            'data': '{evidence_type, status, summary, detail, url, agent_id}',
        },
        'approval': {
            'source': 'agent_task_events(interaction_request|interaction_approval)',
            'types': 'approval.requested | approval.decided',
            'data': 'interaction payload verbatim',
        },
    },
    'event_envelope': ['id', 'category', 'type', 'occurred_at', 'workspace_id',
                       'project_id', 'task_id', 'data'],
}


def _encode_cursor(cursor: dict) -> str:
    raw = json.dumps(cursor, separators=(',', ':')).encode()
    return base64.urlsafe_b64encode(raw).decode()


def _decode_cursor(value):
    if not value:
        return {'o': 0, 'e': 0, 'a': 0}
    try:
        data = json.loads(base64.urlsafe_b64decode(value.encode()))
        return {'o': int(data.get('o', 0)), 'e': int(data.get('e', 0)), 'a': int(data.get('a', 0))}
    except Exception:  # noqa: BLE001 - 非法游标按起点处理
        return {'o': 0, 'e': 0, 'a': 0}


def _iso(value):
    return value.isoformat() if value else None


def _fetch_task_events(workspace_id, after_id, limit):
    rows = (
        TaskEventOutbox.query
        .filter(
            TaskEventOutbox.workspace_id == workspace_id,
            TaskEventOutbox.id > after_id,
        )
        .order_by(TaskEventOutbox.id.asc())
        .limit(limit)
        .all()
    )
    return [{
        'id': f"evt_task_{row.id}",
        'category': 'task',
        'type': row.event_type,
        'occurred_at': _iso(row.occurred_at),
        'workspace_id': row.workspace_id,
        'project_id': row.project_id,
        'task_id': row.task_id,
        'data': row.payload or {},
        '_source': 'o',
        '_source_id': row.id,
    } for row in rows]


def _fetch_evidence_events(workspace_id, after_id, limit):
    rows = (
        TaskEvidenceRecord.query
        .join(Task, TaskEvidenceRecord.task_id == Task.id)
        .join(Project, Task.project_id == Project.id)
        .filter(
            Project.organization_id == workspace_id,
            TaskEvidenceRecord.id > after_id,
        )
        .order_by(TaskEvidenceRecord.id.asc())
        .limit(limit)
        .all()
    )
    return [{
        'id': f"evt_evidence_{row.id}",
        'category': 'evidence',
        'type': 'evidence.recorded',
        'occurred_at': _iso(row.verified_at or row.created_at),
        'workspace_id': workspace_id,
        'project_id': None,
        'task_id': row.task_id,
        'data': {
            'evidence_type': row.evidence_type,
            'status': row.status,
            'summary': row.summary,
            'detail': row.detail or {},
            'url': row.url,
            'agent_id': row.agent_id,
        },
        '_source': 'e',
        '_source_id': row.id,
    } for row in rows]


def _fetch_approval_events(workspace_id, after_id, limit):
    rows = (
        AgentTaskEvent.query
        .filter(
            AgentTaskEvent.workspace_id == workspace_id,
            AgentTaskEvent.event_type.in_(tuple(APPROVAL_EVENT_TYPES)),
            AgentTaskEvent.id > after_id,
        )
        .order_by(AgentTaskEvent.id.asc())
        .limit(limit)
        .all()
    )
    return [{
        'id': f"evt_approval_{row.id}",
        'category': 'approval',
        'type': APPROVAL_EVENT_TYPES.get(row.event_type, 'approval.requested'),
        'occurred_at': _iso(row.event_timestamp or row.created_at),
        'workspace_id': row.workspace_id,
        'project_id': None,
        'task_id': row.task_id,
        'data': {
            'agent_id': row.agent_id,
            'attempt_id': row.attempt_id,
            'message': row.message,
            'payload': row.payload or {},
        },
        '_source': 'a',
        '_source_id': row.id,
    } for row in rows]


_FETCHERS = {
    'task': _fetch_task_events,
    'evidence': _fetch_evidence_events,
    'approval': _fetch_approval_events,
}


@open_protocol_bp.route('/workspaces/<int:workspace_id>/open/events', methods=['GET'])
@unified_auth_required
def open_events(workspace_id: int):
    """统一开放事件流（任务/证据/审批），游标分页，供外部系统双向同步打底。"""
    user = get_current_user()
    workspace, not_found = get_workspace_or_404(workspace_id)
    if not_found:
        return not_found
    access_err = ensure_workspace_manage_access(user, workspace)
    if access_err:
        return access_err

    cursor = _decode_cursor(request.args.get('cursor'))
    try:
        limit = min(max(int(request.args.get('limit', 50)), 1), 200)
    except (TypeError, ValueError):
        limit = 50

    categories = {
        item.strip().lower()
        for item in (request.args.get('category') or ','.join(CATEGORIES)).split(',')
        if item.strip().lower() in CATEGORIES
    } or set(CATEGORIES)

    merged = []
    source_cap_reached = False
    for category in categories:
        fetcher = _FETCHERS[category]
        key = {'task': 'o', 'evidence': 'e', 'approval': 'a'}[category]
        rows = fetcher(workspace_id, cursor[key], limit + 1)
        if len(rows) > limit:
            source_cap_reached = True
        merged.extend(rows[:limit])

    # 稳定排序：时间升序，其次类目，再次源内 id
    merged.sort(key=lambda item: (item['occurred_at'] or '', item['category'], item['_source_id']))
    has_more = len(merged) > limit or source_cap_reached
    page = merged[:limit]

    next_cursor_state = dict(cursor)
    for item in page:
        key = item['_source']
        next_cursor_state[key] = max(next_cursor_state.get(key, 0), item['_source_id'])

    for item in page:
        item.pop('_source', None)
        item.pop('_source_id', None)

    return ApiResponse.success(data={
        'events': page,
        'next_cursor': _encode_cursor(next_cursor_state) if page or has_more else None,
        'has_more': has_more,
    }).to_response()


@open_protocol_bp.route('/workspaces/<int:workspace_id>/open/schema', methods=['GET'])
@unified_auth_required
def open_schema(workspace_id: int):
    """开放协议 schema 自描述（集成方发现用）。"""
    user = get_current_user()
    workspace, not_found = get_workspace_or_404(workspace_id)
    if not_found:
        return not_found
    access_err = ensure_workspace_manage_access(user, workspace)
    if access_err:
        return access_err
    return ApiResponse.success(data=_SCHEMA).to_response()
