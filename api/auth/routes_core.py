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
from api.auth.oauth import github_login, google_login, github_callback, google_callback
from api.auth._core import (
    auth_bp,
    _normalize_local_loopback_url,
    _normalize_return_to,
    _append_query_params,
    _collect_accessible_org_ids,
    _collect_user_org_role_keys,
)


def get_current_user():
    """运行时转发：patch("api.auth.get_current_user") 语义保留。"""
    return _pkg.get_current_user()


@auth_bp.route('/login', methods=['GET'])
def login():
    """启动GitHub登录流程（保持向后兼容）"""
    return github_login()




@auth_bp.route('/login/guest', methods=['GET'])
def guest_login():
    """游客模式登录：创建/复用本地游客账号并签发JWT"""
    try:
        # 根据环境确定前端地址
        is_docker = os.environ.get('DOCKER_ENV') == 'true'
        frontend_base = 'https://todo4ai.org' if is_docker else (_pkg.request.headers.get('Origin') or 'http://127.0.0.1:50111')

        # return_to 兼容相对路径与错误域名
        return_to = _pkg.request.args.get('return_to', '/todo-for-ai/pages/dashboard')
        return_to = _normalize_return_to(return_to, frontend_base)

        guest_email = os.environ.get('GUEST_EMAIL', 'guest@todo4ai.local')
        user = User.query.filter_by(email=guest_email).first()

        # 首次登录时创建游客账户
        if not user:
            guest_id = f"guest-{secrets.token_hex(8)}"
            user = User(
                email=guest_email,
                email_verified=False,
                username='guest',
                name='Guest User',
                nickname='Guest',
                provider='guest',
                provider_user_id=guest_id,
                last_login=datetime.utcnow(),
                last_active_at=datetime.utcnow(),
            )
            db.session.add(user)
            db.session.commit()
        else:
            user.last_login = datetime.utcnow()
            user.last_active_at = datetime.utcnow()
            user.save()

        tokens = _pkg.github_service.generate_tokens(user)
        if not tokens:
            return ApiResponse.error("Failed to generate guest tokens", 500).to_response()

        params = {
            'access_token': tokens['access_token'],
            'refresh_token': tokens['refresh_token'],
            'token_type': tokens['token_type']
        }
        return redirect(_append_query_params(return_to, params))

    except Exception as e:
        return handle_api_error(e)


@auth_bp.route('/callback', methods=['GET'])
def callback():
    """GitHub OAuth回调处理（保持向后兼容）"""
    return github_callback()




@auth_bp.route('/logout', methods=['POST'])
@require_auth
def logout():
    """用户登出"""
    try:
        current_user = _pkg.get_current_user()
        
        # 记录登出时间
        current_user.last_active_at = None
        current_user.save()

        # 简单的登出响应（不再使用Auth0）
        return_to = _pkg.request.json.get('return_to', 'http://127.0.0.1:50111/todo-for-ai/pages')

        return ApiResponse.success({
            'message': 'Logout successful',
            'redirect_url': return_to
        }, 'Logout successful').to_response()
        
    except Exception as e:
        return handle_api_error(e)


@auth_bp.route('/me', methods=['GET'])
@require_auth
def get_current_user_info():
    """获取当前用户信息"""
    try:
        current_user = _pkg.get_current_user()
        return ApiResponse.success(
            data=current_user.to_dict(),
            message='User information retrieved successfully'
        ).to_response()
        
    except Exception as e:
        return handle_api_error(e)


@auth_bp.route('/me', methods=['PUT'])
@require_auth
def update_current_user():
    """更新当前用户信息"""
    try:
        current_user = _pkg.get_current_user()
        
        if not _pkg.request.is_json:
            return ApiResponse.error("Content-Type must be application/json", 400).to_response()
        
        data = _pkg.request.get_json()
        
        # 允许更新的字段
        allowed_fields = ['nickname', 'full_name', 'bio', 'timezone', 'locale']
        
        for field in allowed_fields:
            if field in data:
                setattr(current_user, field, data[field])
        
        # 处理偏好设置
        if 'preferences' in data:
            incoming_preferences = data.get('preferences') or {}
            if not isinstance(incoming_preferences, dict):
                return ApiResponse.error("preferences must be an object", 400).to_response()

            # JSON字段需要重新赋值，避免原地update导致ORM变更检测失效
            merged_preferences = dict(current_user.preferences or {})
            merged_preferences.update(incoming_preferences)
            current_user.preferences = merged_preferences
        
        current_user.save()
        
        return ApiResponse.success(current_user.to_dict(), "User information updated successfully").to_response()
        
    except Exception as e:
        return handle_api_error(e)


@auth_bp.route('/verify', methods=['POST'])
def verify_token():
    """验证JWT令牌"""
    try:
        data = _pkg.request.get_json()
        if not data or 'token' not in data:
            return ApiResponse.error("Token is required", 400).to_response()
        
        # 这里可以添加令牌验证逻辑
        # 目前使用Flask-JWT-Extended的内置验证
        
        return ApiResponse.success(
            data={
                'valid': True
            },
            message='Token is valid'
        ).to_response()

    except Exception as e:
        return ApiResponse.error(
            message='Token is invalid',
            code=400
        ).to_response()


@auth_bp.route('/refresh', methods=['POST'])
@jwt_required(refresh=True)
def refresh():
    """刷新访问令牌"""
    try:
        try:
            current_user_id = int(get_jwt_identity())
        except (TypeError, ValueError):
            current_user_id = None
        user = User.query.get(current_user_id) if current_user_id is not None else None
        
        if not user or not user.is_active():
            return ApiResponse.error("User not found or inactive", 404).to_response()
        
        # 生成新的访问令牌和刷新令牌
        tokens = _pkg.github_service.generate_tokens(user)
        if not tokens:
            return ApiResponse.error("Failed to generate tokens", 500).to_response()

        return ApiResponse.success({
            'access_token': tokens['access_token'],
            'refresh_token': tokens['refresh_token'],
            'token_type': tokens['token_type']
        }, "Tokens refreshed successfully").to_response()
        
    except Exception as e:
        return handle_api_error(e)
