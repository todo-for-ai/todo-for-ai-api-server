"""
通知中心 API
"""

from datetime import datetime

from flask import Blueprint, request

from api.base import ApiResponse, paginate_query
from core.auth import unified_auth_required, get_current_user
from models import db, UserNotification

from .notification_service import get_notification_event_catalog


notifications_bp = Blueprint('notifications', __name__)


def _parse_bool(value):
    return str(value or '').strip().lower() in {'1', 'true', 'yes', 'on'}


@notifications_bp.route('', methods=['GET'])
@unified_auth_required
def list_notifications():
    current_user = get_current_user()
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    unread_only = _parse_bool(request.args.get('unread_only'))
    category = str(request.args.get('category') or '').strip().lower()

    query = UserNotification.query.filter(
        UserNotification.user_id == current_user.id,
        UserNotification.archived_at.is_(None),
    ).order_by(
        UserNotification.created_at.desc(),
        UserNotification.id.desc(),
    )
    if unread_only:
        query = query.filter(UserNotification.read_at.is_(None))
    if category:
        query = query.filter(UserNotification.category == category)

    result = paginate_query(query, page=page, per_page=per_page)
    return ApiResponse.success(result, 'Notifications retrieved successfully').to_response()


@notifications_bp.route('/unread-count', methods=['GET'])
@unified_auth_required
def get_unread_count():
    current_user = get_current_user()
    count = UserNotification.query.filter(
        UserNotification.user_id == current_user.id,
        UserNotification.archived_at.is_(None),
        UserNotification.read_at.is_(None),
    ).count()
    return ApiResponse.success({'count': count}, 'Unread count retrieved successfully').to_response()


@notifications_bp.route('/<int:notification_id>/read', methods=['POST'])
@unified_auth_required
def mark_notification_read(notification_id):
    current_user = get_current_user()
    row = UserNotification.query.filter_by(id=notification_id, user_id=current_user.id).first()
    if not row:
        return ApiResponse.not_found('Notification not found').to_response()

    if not row.read_at:
        row.read_at = datetime.utcnow()
        db.session.commit()

    return ApiResponse.success(row.to_dict(), 'Notification marked as read').to_response()


@notifications_bp.route('/read-all', methods=['POST'])
@unified_auth_required
def mark_all_notifications_read():
    current_user = get_current_user()
    unread_rows = UserNotification.query.filter(
        UserNotification.user_id == current_user.id,
        UserNotification.archived_at.is_(None),
        UserNotification.read_at.is_(None),
    ).all()
    now = datetime.utcnow()
    for row in unread_rows:
        row.read_at = now
    db.session.commit()

    return ApiResponse.success({'updated': len(unread_rows)}, 'All notifications marked as read').to_response()


@notifications_bp.route('/notification-event-catalog', methods=['GET'])
@unified_auth_required
def get_notification_event_catalog_api():
    return ApiResponse.success(
        {'items': get_notification_event_catalog()},
        'Notification event catalog retrieved successfully',
    ).to_response()
