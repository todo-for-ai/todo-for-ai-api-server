"""认证蓝图与共享 helpers（原样搬移）。"""

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


auth_bp = Blueprint('auth', __name__)


def _normalize_local_loopback_url(url: str) -> str:
    """在本地开发场景下统一回环地址，减少 localhost 解析抖动。"""
    if not url:
        return url
    try:
        parsed = urlparse(url)
    except Exception:
        return url

    # 仅处理本地 localhost，避免影响生产域名与外部地址
    if parsed.hostname != 'localhost':
        return url

    netloc = parsed.netloc
    if '@' in netloc:
        userinfo, hostport = netloc.rsplit('@', 1)
        hostport = hostport.replace('localhost', '127.0.0.1', 1)
        netloc = f'{userinfo}@{hostport}'
    else:
        netloc = netloc.replace('localhost', '127.0.0.1', 1)

    return urlunparse(parsed._replace(netloc=netloc))


def _normalize_return_to(return_to: str, frontend_base: str) -> str:
    """规范化登录后回跳地址，优先保留用户当前前端域名。"""
    frontend_base = _normalize_local_loopback_url(frontend_base)

    if not return_to:
        return f'{frontend_base}/todo-for-ai/pages/dashboard'

    # 相对路径 -> 当前前端域名
    if return_to.startswith('/'):
        return f'{frontend_base}{return_to}'

    # 显式URL：仅当它指向后端地址时，替换到前端地址
    if return_to.startswith('http://') or return_to.startswith('https://'):
        return_to = _normalize_local_loopback_url(return_to)
        if 'localhost:50110' in return_to:
            return return_to.replace('http://localhost:50110', frontend_base)
        if '127.0.0.1:50110' in return_to:
            return return_to.replace('http://127.0.0.1:50110', frontend_base)
        if '/todo-for-ai/api/v1' in return_to:
            return return_to.replace('/todo-for-ai/api/v1', '/todo-for-ai/pages')
        return return_to

    return f'{frontend_base}/todo-for-ai/pages/dashboard'


def _append_query_params(url: str, params: dict) -> str:
    """Append params to URL while preserving existing query parameters."""
    parsed = urlparse(url)
    existing = dict(parse_qsl(parsed.query, keep_blank_values=True))
    existing.update(params)
    query = urlencode(existing)
    return urlunparse(parsed._replace(query=query))


def _collect_accessible_org_ids(user_id: int) -> set:
    """收集用户可访问组织（owner 或 active member）。"""
    owner_ids = {
        row.id for row in db.session.query(Organization.id).filter(Organization.owner_id == user_id).all()
    }
    member_ids = {
        row.organization_id
        for row in db.session.query(OrganizationMember.organization_id).filter(
            OrganizationMember.user_id == user_id,
            OrganizationMember.status == OrganizationMemberStatus.ACTIVE,
        ).all()
    }
    return owner_ids | member_ids


def _collect_user_org_role_keys(organization: Organization, user_id: int) -> list:
    """获取用户在组织中的角色键（兼容 owner + 旧 role 字段）。"""
    if organization.owner_id == user_id:
        return ['owner']

    member = OrganizationMember.query.filter(
        OrganizationMember.organization_id == organization.id,
        OrganizationMember.user_id == user_id,
        OrganizationMember.status == OrganizationMemberStatus.ACTIVE,
    ).first()
    if not member:
        return []

    role_rows = (
        db.session.query(OrganizationRoleDefinition.key)
        .join(OrganizationMemberRole, OrganizationMemberRole.role_id == OrganizationRoleDefinition.id)
        .filter(
            OrganizationMemberRole.member_id == member.id,
            OrganizationRoleDefinition.is_active.is_(True),
        )
        .all()
    )

    role_keys = []
    seen = set()
    for row in role_rows:
        key = str(row.key or '').strip().lower()
        if key and key not in seen:
            role_keys.append(key)
            seen.add(key)

    if role_keys:
        return role_keys

    # 兼容尚未迁移到 organization_member_roles 的旧数据
    if member.role:
        legacy_role = member.role.value if hasattr(member.role, 'value') else str(member.role)
        legacy_role = str(legacy_role or '').strip().lower()
        if legacy_role:
            return [legacy_role]
    return []
