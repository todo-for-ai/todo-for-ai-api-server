"""Organization activity events API."""

from datetime import datetime

from flask import request
from models import Organization, OrganizationEvent
from core.auth import unified_auth_required, get_current_user
from ..base import ApiResponse, get_request_args, paginate_query

from . import organizations_bp


def _parse_iso_datetime(value):
    text = str(value or '').strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace('Z', '+00:00')).replace(tzinfo=None)
    except Exception:
        return None


@organizations_bp.route('/<int:organization_id>/events', methods=['GET'])
@unified_auth_required
def list_organization_events(organization_id):
    current_user = get_current_user()
    organization = Organization.query.get(organization_id)
    if not organization:
        return ApiResponse.not_found('Organization not found').to_response()
    if not current_user.can_access_organization(organization):
        return ApiResponse.forbidden('Access denied').to_response()

    args = get_request_args()
    page = max(args['page'], 1)
    per_page = min(max(args['per_page'], 1), 100)

    event_type = str(request.args.get('event_type') or '').strip()
    actor_type = str(request.args.get('actor_type') or '').strip()
    project_id = request.args.get('project_id', type=int) or args.get('project_id')
    task_id = request.args.get('task_id', type=int)
    since = _parse_iso_datetime(request.args.get('from'))
    until = _parse_iso_datetime(request.args.get('to'))

    query = OrganizationEvent.query.filter(OrganizationEvent.organization_id == organization_id)
    if event_type:
        query = query.filter(OrganizationEvent.event_type == event_type)
    if actor_type:
        query = query.filter(OrganizationEvent.actor_type == actor_type)
    if project_id:
        query = query.filter(OrganizationEvent.project_id == project_id)
    if task_id:
        query = query.filter(OrganizationEvent.task_id == task_id)
    if since:
        query = query.filter(OrganizationEvent.occurred_at >= since)
    if until:
        query = query.filter(OrganizationEvent.occurred_at <= until)

    query = query.order_by(OrganizationEvent.occurred_at.desc(), OrganizationEvent.id.desc())

    result = paginate_query(query, page=page, per_page=per_page)

    return ApiResponse.success(
        result,
        'Organization events retrieved successfully',
    ).to_response()
