"""认证路由（原样搬移；被 patch 符号经 _pkg 运行时解析）。"""

import os
import secrets
from datetime import datetime
from urllib.parse import urlencode, urlparse, parse_qsl, urlunparse
from flask import Blueprint, request, jsonify, redirect, url_for, session
from flask_jwt_extended import jwt_required, get_jwt_identity
from models import (
    db,
    User,
    Organization,
    OrganizationMember,
    OrganizationMemberStatus,
    OrganizationMemberRole,
    OrganizationRoleDefinition,
)
from ..base import ApiResponse, paginate_query, validate_json_request, get_request_args, APIException, handle_api_error
from core.github_config import github_service, require_auth, get_current_user
from core.google_config import google_service

from flask import Blueprint

from api import auth as _pkg
from api.auth._core import (
    auth_bp,
    _normalize_local_loopback_url,
    _normalize_return_to,
    _append_query_params,
    _collect_accessible_org_ids,
    _collect_user_org_role_keys,
)


@auth_bp.route('/users', methods=['GET'])
@require_auth
def list_users():
    """获取用户列表（需要管理员权限）"""
    try:
        current_user = _pkg.get_current_user()
        
        if not current_user.is_admin():
            return ApiResponse.error("Admin access required", 403).to_response()
        
        # 获取查询参数
        page = _pkg.request.args.get('page', 1, type=int)
        per_page = min(_pkg.request.args.get('per_page', 20, type=int), 100)
        search = _pkg.request.args.get('search', '').strip()
        status = _pkg.request.args.get('status')
        role = _pkg.request.args.get('role')
        
        # 构建查询
        query = User.query
        
        if search:
            query = query.filter(
                User.email.contains(search) |
                User.username.contains(search) |
                User.full_name.contains(search)
            )
        
        if status:
            query = query.filter_by(status=status)
        
        if role:
            query = query.filter_by(role=role)
        
        # 分页
        pagination = query.paginate(
            page=page,
            per_page=per_page,
            error_out=False
        )
        
        return ApiResponse.success({
            'users': [user.to_dict() for user in pagination.items],
            'pagination': {
                'page': pagination.page,
                'per_page': pagination.per_page,
                'total': pagination.total,
                'pages': pagination.pages,
                'has_prev': pagination.has_prev,
                'has_next': pagination.has_next
            }
        }, "Users retrieved successfully").to_response()
        
    except Exception as e:
        return handle_api_error(e)


@auth_bp.route('/users/<int:user_id>', methods=['GET'])
@require_auth
def get_user(user_id):
    """获取指定用户信息"""
    try:
        current_user = _pkg.get_current_user()

        user = User.query.get(user_id)
        if not user:
            return ApiResponse.error("User not found", 404).to_response()

        # 管理员或用户本人：完整视图
        if current_user.is_admin() or current_user.id == user_id:
            payload = user.to_dict()
            payload['is_self'] = current_user.id == user_id
            payload['view_mode'] = 'self' if current_user.id == user_id else 'admin'
            payload['shared_organization_count'] = 0
            payload['shared_organizations'] = []
            return ApiResponse.success(payload, "User information retrieved successfully").to_response()

        # 其他用户：仅允许查看共享组织内成员的公开档案
        viewer_org_ids = _collect_accessible_org_ids(current_user.id)
        target_org_ids = _collect_accessible_org_ids(user_id)
        shared_org_ids = viewer_org_ids.intersection(target_org_ids)
        if not shared_org_ids:
            return ApiResponse.error("Access denied", 403).to_response()

        shared_orgs = (
            Organization.query
            .filter(Organization.id.in_(shared_org_ids))
            .order_by(Organization.name.asc())
            .all()
        )

        shared_organizations = []
        for org in shared_orgs:
            shared_organizations.append({
                'id': org.id,
                'name': org.name,
                'slug': org.slug,
                'status': org.status.value if org.status else None,
                'target_roles': _collect_user_org_role_keys(org, user_id),
                'viewer_roles': _collect_user_org_role_keys(org, current_user.id),
            })

        payload = user.to_public_dict()
        payload['name'] = user.name
        payload['timezone'] = user.timezone
        payload['locale'] = user.locale
        payload['last_active_at'] = user.last_active_at.isoformat() if user.last_active_at else None
        payload['updated_at'] = user.updated_at.isoformat() if user.updated_at else None
        payload['is_self'] = False
        payload['view_mode'] = 'public'
        payload['shared_organization_count'] = len(shared_organizations)
        payload['shared_organizations'] = shared_organizations

        return ApiResponse.success(payload, "User public profile retrieved successfully").to_response()
        
    except Exception as e:
        return handle_api_error(e)


@auth_bp.route('/users/<int:user_id>/status', methods=['PUT'])
@require_auth
def update_user_status(user_id):
    """更新用户状态（管理员功能）"""
    try:
        current_user = _pkg.get_current_user()
        
        if not current_user.is_admin():
            return ApiResponse.error("Admin access required", 403).to_response()
        
        user = User.query.get(user_id)
        if not user:
            return ApiResponse.error("User not found", 404).to_response()
        
        data = _pkg.request.get_json()
        if not data or 'status' not in data:
            return ApiResponse.error("Status is required", 400).to_response()
        
        # 验证状态值
        from models.user import UserStatus
        try:
            new_status = UserStatus(data['status'])
            user.status = new_status
            user.save()
            
            return ApiResponse.success(user.to_dict(), "User status updated successfully").to_response()
            
        except ValueError:
            return ApiResponse.error("Invalid status value", 400).to_response()
        
    except Exception as e:
        return handle_api_error(e)
