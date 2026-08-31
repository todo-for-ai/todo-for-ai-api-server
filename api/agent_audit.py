"""
Agent Audit Events API

List and query audit events for a workspace.
Enterprise export: full CSV/JSON export with time-window filters (Phase 4).
"""

import csv
import io
import json
from datetime import datetime

from flask import Blueprint, Response, request
from sqlalchemy import func

from models import db, AgentAuditEvent
from core.auth import unified_auth_required, get_current_user
from api.agent_common import get_workspace_or_404, ensure_workspace_access, ensure_workspace_manage_access, write_agent_audit
from api.base import ApiResponse

audit_bp = Blueprint('agent_audit', __name__)

# 导出行数上限（防内存失控；超出时响应标记 truncated）
EXPORT_MAX_ROWS = 100000

# CSV 导出列（payload 以 JSON 字符串并入）
EXPORT_CSV_COLUMNS = (
    'id', 'occurred_at', 'event_type', 'actor_type', 'actor_id',
    'target_type', 'target_id', 'source', 'level', 'risk_score',
    'correlation_id', 'request_id', 'run_id', 'attempt_id',
    'task_id', 'project_id', 'actor_agent_id', 'target_agent_id',
    'duration_ms', 'error_code', 'ip', 'payload',
)


def _export_filters(query):
    """导出与列表共用的过滤条件（时间窗 + 维度过滤）。"""
    for param, column in (
        ('event_type', AgentAuditEvent.event_type),
        ('actor_type', AgentAuditEvent.actor_type),
        ('target_type', AgentAuditEvent.target_type),
        ('level', AgentAuditEvent.level),
    ):
        value = request.args.get(param)
        if value:
            query = query.filter(column == value)

    task_id = request.args.get('task_id', type=int)
    if task_id:
        query = query.filter(AgentAuditEvent.task_id == task_id)

    risk_min = request.args.get('risk_min', type=int)
    if risk_min is not None:
        query = query.filter(AgentAuditEvent.risk_score >= risk_min)

    start_date = request.args.get('start_date')
    if start_date:
        query = query.filter(AgentAuditEvent.occurred_at >= start_date)
    end_date = request.args.get('end_date')
    if end_date:
        query = query.filter(AgentAuditEvent.occurred_at <= end_date)
    return query


@audit_bp.route('/workspaces/<int:workspace_id>/audit-events', methods=['GET'])
@unified_auth_required
def list_audit_events(workspace_id):
    """List audit events for a workspace with filters and pagination."""
    user = get_current_user()
    workspace, err = get_workspace_or_404(workspace_id)
    if err:
        return err

    access_err = ensure_workspace_access(user, workspace)
    if access_err:
        return access_err

    query = AgentAuditEvent.query.filter_by(workspace_id=workspace_id)

    # Optional filters
    event_type = request.args.get('event_type')
    if event_type:
        query = query.filter(AgentAuditEvent.event_type == event_type)

    actor_type = request.args.get('actor_type')
    if actor_type:
        query = query.filter(AgentAuditEvent.actor_type == actor_type)

    target_type = request.args.get('target_type')
    if target_type:
        query = query.filter(AgentAuditEvent.target_type == target_type)

    task_id = request.args.get('task_id', type=int)
    if task_id:
        query = query.filter(AgentAuditEvent.task_id == task_id)

    level = request.args.get('level')
    if level:
        query = query.filter(AgentAuditEvent.level == level)

    start_date = request.args.get('start_date')
    if start_date:
        query = query.filter(AgentAuditEvent.occurred_at >= start_date)

    end_date = request.args.get('end_date')
    if end_date:
        query = query.filter(AgentAuditEvent.occurred_at <= end_date)

    # Default ordering: newest first
    query = query.order_by(AgentAuditEvent.occurred_at.desc(), AgentAuditEvent.id.desc())

    # Pagination
    page = max(request.args.get('page', 1, type=int), 1)
    per_page = min(max(request.args.get('per_page', 20, type=int), 1), 100)

    total = query.count()
    items = query.offset((page - 1) * per_page).limit(per_page).all()

    return ApiResponse.success(
        {
            'items': [item.to_dict() for item in items],
            'total': total,
            'page': page,
            'per_page': per_page,
        },
        'Audit events retrieved successfully',
    ).to_response()


@audit_bp.route('/workspaces/<int:workspace_id>/audit-events/stats', methods=['GET'])
@unified_auth_required
def audit_events_stats(workspace_id):
    """Return aggregated stats for audit events in a workspace."""
    user = get_current_user()
    workspace, err = get_workspace_or_404(workspace_id)
    if err:
        return err

    access_err = ensure_workspace_access(user, workspace)
    if access_err:
        return access_err

    base_filter = AgentAuditEvent.query.filter_by(workspace_id=workspace_id)

    total = base_filter.count()

    by_level_rows = (
        db.session.query(AgentAuditEvent.level, func.count(AgentAuditEvent.id))
        .filter(AgentAuditEvent.workspace_id == workspace_id)
        .group_by(AgentAuditEvent.level)
        .all()
    )
    by_level = {row[0]: row[1] for row in by_level_rows}

    by_actor_type_rows = (
        db.session.query(AgentAuditEvent.actor_type, func.count(AgentAuditEvent.id))
        .filter(AgentAuditEvent.workspace_id == workspace_id)
        .group_by(AgentAuditEvent.actor_type)
        .all()
    )
    by_actor_type = {row[0]: row[1] for row in by_actor_type_rows}

    return ApiResponse.success(
        {
            'total': total,
            'by_level': by_level,
            'by_actor_type': by_actor_type,
        },
        'Audit event stats retrieved successfully',
    ).to_response()


@audit_bp.route('/workspaces/<int:workspace_id>/audit-events/export', methods=['GET'])
@unified_auth_required
def export_audit_events(workspace_id):
    """组织级审计导出（企业合规，Phase 4）。

    - format=csv|json（默认 json）
    - 时间窗：start_date / end_date（ISO 或可比较字符串），加维度过滤
    - 工作区 owner/admin 才可导出；导出动作本身写审计（audit.exported）
    - 上限 EXPORT_MAX_ROWS，超出标记 truncated
    """
    user = get_current_user()
    workspace, err = get_workspace_or_404(workspace_id)
    if err:
        return err

    access_err = ensure_workspace_manage_access(user, workspace)
    if access_err:
        return access_err

    export_format = (request.args.get('format') or 'json').strip().lower()
    if export_format not in ('csv', 'json'):
        return ApiResponse.error("format must be 'csv' or 'json'", 400).to_response()

    try:
        limit = min(max(int(request.args.get('limit', EXPORT_MAX_ROWS)), 1), EXPORT_MAX_ROWS)
    except (TypeError, ValueError):
        limit = EXPORT_MAX_ROWS

    query = _export_filters(AgentAuditEvent.query.filter_by(workspace_id=workspace_id))
    query = query.order_by(AgentAuditEvent.occurred_at.asc(), AgentAuditEvent.id.asc())

    events = query.limit(limit + 1).all()
    truncated = len(events) > limit
    events = events[:limit]

    exported_at = datetime.utcnow().isoformat() + 'Z'
    filename = f"audit-export-ws{workspace_id}-{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}.{export_format}"

    filters_applied = {
        key: request.args.get(key)
        for key in ('event_type', 'actor_type', 'target_type', 'level',
                    'task_id', 'risk_min', 'start_date', 'end_date')
        if request.args.get(key)
    }

    if export_format == 'json':
        body = json.dumps({
            'workspace_id': workspace_id,
            'exported_at': exported_at,
            'count': len(events),
            'truncated': truncated,
            'filters': filters_applied,
            'items': [event.to_dict() for event in events],
        }, ensure_ascii=False, default=str)
        response = Response(body, mimetype='application/json')
    else:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(EXPORT_CSV_COLUMNS)
        for event in events:
            data = event.to_dict()
            row = []
            for column in EXPORT_CSV_COLUMNS:
                if column == 'payload':
                    row.append(
                        json.dumps(event.payload, ensure_ascii=False, default=str)
                        if event.payload is not None else ''
                    )
                    continue
                value = data.get(column)
                row.append(value.isoformat() if isinstance(value, datetime) else value)
            writer.writerow(row)
        response = Response(buffer.getvalue(), mimetype='text/csv; charset=utf-8')

    response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'

    write_agent_audit(
        event_type='audit.exported',
        actor_type='user',
        actor_id=user.id,
        target_type='workspace',
        target_id=workspace_id,
        workspace_id=workspace_id,
        payload={
            'format': export_format,
            'count': len(events),
            'truncated': truncated,
            'filters': filters_applied,
        },
        risk_score=15,
    )
    return response
