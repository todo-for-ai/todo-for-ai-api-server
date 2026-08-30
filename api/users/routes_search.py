from flask import request

from models import db, User, UserStatus, ProjectMember, ProjectMemberStatus
from api.base import ApiResponse
from core.auth import unified_auth_required, get_current_user

from . import users_bp


@users_bp.route('/search', methods=['GET'])
@unified_auth_required
def search_users():
    """Search users by username/nickname/email, optionally filtered by project"""
    query = request.args.get('q', '').strip()
    project_id = request.args.get('project_id', type=int)
    limit = min(request.args.get('limit', 10, type=int), 20)

    if not query or len(query) < 2:
        return ApiResponse.success([], 'Users retrieved successfully').to_response()

    base_query = db.session.query(User).filter(
        User.status == UserStatus.ACTIVE,
        db.or_(
            User.username.ilike(f'%{query}%'),
            User.nickname.ilike(f'%{query}%'),
            User.email.ilike(f'%{query}%'),
        )
    )

    if project_id:
        base_query = base_query.join(
            ProjectMember, ProjectMember.user_id == User.id
        ).filter(
            ProjectMember.project_id == project_id,
            ProjectMember.status == ProjectMemberStatus.ACTIVE,
        )

    users = base_query.limit(limit).all()

    return ApiResponse.success(
        [{
            'id': u.id,
            'username': u.username,
            'nickname': u.nickname,
            'avatar_url': u.avatar_url,
        } for u in users],
        'Users retrieved successfully',
    ).to_response()
