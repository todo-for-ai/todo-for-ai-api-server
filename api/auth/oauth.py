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


@auth_bp.route('/login/github', methods=['GET'])
def github_login():
    """启动GitHub登录流程"""
    try:
        # 获取重定向URL - 根据环境动态设置
        is_docker = os.environ.get('DOCKER_ENV') == 'true'
        if is_docker:
            # 生产环境使用域名
            default_redirect_uri = 'https://todo4ai.org/todo-for-ai/api/v1/auth/callback'
            frontend_base = 'https://todo4ai.org'
        else:
            # 开发环境使用localhost
            default_redirect_uri = 'http://localhost:50110/todo-for-ai/api/v1/auth/callback'
            frontend_base = _pkg.request.headers.get('Origin') or 'http://localhost:50111'

        redirect_uri = _pkg.request.args.get('redirect_uri', default_redirect_uri)

        # 存储原始重定向URL，确保重定向到前端dashboard
        return_to = _pkg.request.args.get('return_to', '/todo-for-ai/pages/dashboard')

        return_to = _normalize_return_to(return_to, frontend_base)

        session['redirect_after_login'] = return_to
        session['auth_provider'] = 'github'

        # 重定向到GitHub登录页面
        return _pkg.github_service.oauth.github.authorize_redirect(redirect_uri)

    except Exception as e:
        return handle_api_error(e)


@auth_bp.route('/login/google', methods=['GET'])
def google_login():
    """启动Google登录流程"""
    try:
        # 获取重定向URL - 根据环境动态设置
        is_docker = os.environ.get('DOCKER_ENV') == 'true'
        if is_docker:
            # 生产环境使用域名
            default_redirect_uri = 'https://todo4ai.org/todo-for-ai/api/v1/auth/google/callback'
            frontend_base = 'https://todo4ai.org'
        else:
            # 开发环境使用localhost
            default_redirect_uri = 'http://localhost:50110/todo-for-ai/api/v1/auth/google/callback'
            frontend_base = _pkg.request.headers.get('Origin') or 'http://localhost:50111'

        redirect_uri = _pkg.request.args.get('redirect_uri', default_redirect_uri)

        # 存储原始重定向URL，确保重定向到前端dashboard
        return_to = _pkg.request.args.get('return_to', '/todo-for-ai/pages/dashboard')

        return_to = _normalize_return_to(return_to, frontend_base)

        session['redirect_after_login'] = return_to
        session['auth_provider'] = 'google'

        # 重定向到Google登录页面
        return _pkg.google_service.oauth.google.authorize_redirect(redirect_uri)

    except Exception as e:
        return handle_api_error(e)




@auth_bp.route('/callback/github', methods=['GET'])
def github_callback():
    """GitHub OAuth回调处理"""
    try:
        # 获取授权码并交换令牌
        token = _pkg.github_service.oauth.github.authorize_access_token()

        if not token:
            return ApiResponse.error("Failed to get access token from GitHub", 400).to_response()

        # 获取用户信息
        user_info = _pkg.github_service.get_user_info(token['access_token'])
        if not user_info:
            return ApiResponse.error("Failed to get user information", 400).to_response()

        # 创建或更新用户
        user = _pkg.github_service.create_or_update_user(user_info)
        if not user:
            return ApiResponse.error("Failed to create or update user", 500).to_response()

        # 生成JWT令牌
        tokens = _pkg.github_service.generate_tokens(user)
        if not tokens:
            return ApiResponse.error("Failed to generate tokens", 500).to_response()

        # 获取重定向URL，默认到dashboard - 根据环境动态设置
        is_docker = os.environ.get('DOCKER_ENV') == 'true'
        default_dashboard = 'https://todo4ai.org/todo-for-ai/pages/dashboard' if is_docker else 'http://127.0.0.1:50111/todo-for-ai/pages/dashboard'
        redirect_url = session.pop('redirect_after_login', default_dashboard)

        # 重定向到前端，并在URL中包含令牌（包括access_token和refresh_token）
        params = {
            'access_token': tokens['access_token'],
            'refresh_token': tokens['refresh_token'],
            'token_type': tokens['token_type']
        }
        return redirect(_append_query_params(redirect_url, params))

    except Exception as e:
        return handle_api_error(e)


@auth_bp.route('/google/callback', methods=['GET'])
def google_callback():
    """Google OAuth回调处理"""
    try:
        # 获取授权码并交换令牌
        token = _pkg.google_service.oauth.google.authorize_access_token()

        if not token:
            return ApiResponse.error("Failed to get access token from Google", 400).to_response()

        # 获取用户信息
        user_info = _pkg.google_service.get_user_info(token['access_token'])
        if not user_info:
            return ApiResponse.error("Failed to get user information from Google", 400).to_response()

        # 创建或更新用户
        user = _pkg.google_service.create_or_update_user(user_info)
        if not user:
            return ApiResponse.error("Failed to create or update user", 500).to_response()

        # 生成JWT令牌
        tokens = _pkg.google_service.generate_tokens(user)
        if not tokens:
            return ApiResponse.error("Failed to generate tokens", 500).to_response()

        # 获取重定向URL，默认到dashboard - 根据环境动态设置
        is_docker = os.environ.get('DOCKER_ENV') == 'true'
        default_dashboard = 'https://todo4ai.org/todo-for-ai/pages/dashboard' if is_docker else 'http://127.0.0.1:50111/todo-for-ai/pages/dashboard'
        redirect_url = session.pop('redirect_after_login', default_dashboard)

        # 重定向到前端，并在URL中包含令牌（包括access_token和refresh_token）
        params = {
            'access_token': tokens['access_token'],
            'refresh_token': tokens['refresh_token'],
            'token_type': tokens['token_type']
        }
        return redirect(_append_query_params(redirect_url, params))

    except Exception as e:
        return handle_api_error(e)
