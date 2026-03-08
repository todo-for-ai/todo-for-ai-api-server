"""Shared helpers for organizations APIs."""

import re
from datetime import datetime

from sqlalchemy import or_
from sqlalchemy.orm import selectinload

from models import (
    db,
    Organization,
    OrganizationMember,
    OrganizationRole,
    OrganizationMemberStatus,
    OrganizationRoleDefinition,
    OrganizationMemberRole,
)
from core.cache_invalidation import invalidate_user_caches

from .constants import SYSTEM_ROLE_DEFINITIONS, ROLE_PRIORITY

def _slugify_role_key(name):
    key = re.sub(r'[^a-zA-Z0-9]+', '_', (name or '').strip().lower()).strip('_')
    return key or f'role_{int(datetime.utcnow().timestamp())}'


def _normalize_optional_text(value):
    text = str(value or '').strip()
    return text or None


def _ensure_system_roles(organization_id, created_by='system'):
    for role_data in SYSTEM_ROLE_DEFINITIONS:
        role = OrganizationRoleDefinition.query.filter_by(
            organization_id=organization_id,
            key=role_data['key']
        ).first()
        if role:
            if not role.is_system:
                role.is_system = True
            if not role.is_active:
                role.is_active = True
            if not role.name:
                role.name = role_data['name']
            continue

        OrganizationRoleDefinition.create(
            organization_id=organization_id,
            key=role_data['key'],
            name=role_data['name'],
            description=role_data['description'],
            is_system=True,
            is_active=True,
            created_by=created_by,
        )
    db.session.flush()


def _get_org_roles_map(organization_id, include_inactive=False):
    query = OrganizationRoleDefinition.query.filter_by(organization_id=organization_id)
    if not include_inactive:
        query = query.filter(OrganizationRoleDefinition.is_active.is_(True))
    rows = query.all()
    return {row.id: row for row in rows}, {row.key: row for row in rows}


def _member_legacy_role_key(member):
    raw_value = None
    if member and member.role:
        raw_value = member.role.value if hasattr(member.role, 'value') else member.role
    role_key = str(raw_value or '').strip().lower()
    return role_key or 'member'


def _backfill_member_role_bindings(organization_id, created_by='system'):
    """
    兼容旧数据：
    当 organization_member_roles 还未建立绑定时，按旧 role 字段补齐一条系统角色绑定。
    """
    _ensure_system_roles(organization_id, created_by=created_by)
    _, roles_by_key = _get_org_roles_map(organization_id, include_inactive=False)
    if not roles_by_key:
        return

    members = (
        OrganizationMember.query.options(selectinload(OrganizationMember.role_bindings))
        .filter(
            OrganizationMember.organization_id == organization_id,
            OrganizationMember.status != OrganizationMemberStatus.REMOVED,
        )
        .all()
    )

    created = False
    for member in members:
        if member.role_bindings:
            continue

        role_key = _member_legacy_role_key(member)
        role = roles_by_key.get(role_key) or roles_by_key.get('member')
        if not role:
            continue

        OrganizationMemberRole.create(
            organization_id=organization_id,
            member_id=member.id,
            role_id=role.id,
            created_by=created_by,
        )
        created = True

    if created:
        db.session.flush()


def _resolve_role_ids_from_payload(organization_id, data, default_role_key=None):
    """
    支持两种入参：
    - role_ids: number[]（推荐，支持0~N）
    - role: string（兼容旧版，单角色）
    """
    _, roles_by_key = _get_org_roles_map(organization_id, include_inactive=False)
    role_ids = None

    if 'role_ids' in data:
        value = data.get('role_ids')
        if value is None:
            role_ids = []
        elif not isinstance(value, list):
            raise ValueError("Invalid role_ids: must be an array")
        else:
            normalized_ids = []
            for item in value:
                try:
                    role_id = int(item)
                except (TypeError, ValueError):
                    raise ValueError(f"Invalid role id: {item}")
                normalized_ids.append(role_id)
            role_ids = list(dict.fromkeys(normalized_ids))
            if role_ids:
                roles_by_id, _ = _get_org_roles_map(organization_id, include_inactive=False)
                invalid_ids = [rid for rid in role_ids if rid not in roles_by_id]
                if invalid_ids:
                    raise ValueError(f"Invalid role_ids: {invalid_ids}")
    elif 'role' in data:
        role_key = str(data.get('role') or '').strip().lower()
        role_obj = roles_by_key.get(role_key)
        if not role_obj:
            raise ValueError(f"Invalid role: {data.get('role')}")
        role_ids = [role_obj.id]
    elif default_role_key:
        role_obj = roles_by_key.get(default_role_key)
        role_ids = [role_obj.id] if role_obj else []

    return role_ids


def _replace_member_roles(member, role_ids, created_by='system'):
    OrganizationMemberRole.query.filter_by(member_id=member.id).delete()
    for role_id in role_ids:
        OrganizationMemberRole.create(
            organization_id=member.organization_id,
            member_id=member.id,
            role_id=role_id,
            created_by=created_by,
        )
    db.session.flush()
    _sync_member_primary_role(member)


def _sync_member_primary_role(member):
    """
    兼容旧字段 organization_members.role：
    - 取系统角色中的最高优先级
    - 若无任何系统角色，回退 member
    """
    rows = (
        db.session.query(OrganizationRoleDefinition.key)
        .join(OrganizationMemberRole, OrganizationMemberRole.role_id == OrganizationRoleDefinition.id)
        .filter(
            OrganizationMemberRole.member_id == member.id,
            OrganizationRoleDefinition.is_active.is_(True),
            OrganizationRoleDefinition.is_system.is_(True),
        )
        .all()
    )
    keys = {str(row.key).strip().lower() for row in rows if row.key}

    selected = 'member'
    for key in ROLE_PRIORITY:
        if key in keys:
            selected = key
            break

    try:
        member.role = OrganizationRole(selected)
    except ValueError:
        member.role = OrganizationRole.MEMBER


def _compute_primary_role_from_keys(role_keys):
    for key in ROLE_PRIORITY:
        if key in role_keys:
            return key
    return role_keys[0] if role_keys else None


def _get_user_org_roles_map(org_ids, user_id):
    if not org_ids:
        return {}

    member_rows = (
        db.session.query(
            OrganizationMember.organization_id,
            OrganizationMember.role,
        )
        .filter(
            OrganizationMember.organization_id.in_(org_ids),
            OrganizationMember.user_id == user_id,
            OrganizationMember.status == OrganizationMemberStatus.ACTIVE,
        )
        .all()
    )

    rows = (
        db.session.query(
            OrganizationMember.organization_id,
            OrganizationRoleDefinition.key,
        )
        .join(OrganizationMemberRole, OrganizationMemberRole.member_id == OrganizationMember.id)
        .join(OrganizationRoleDefinition, OrganizationRoleDefinition.id == OrganizationMemberRole.role_id)
        .filter(
            OrganizationMember.organization_id.in_(org_ids),
            OrganizationMember.user_id == user_id,
            OrganizationMember.status == OrganizationMemberStatus.ACTIVE,
            OrganizationRoleDefinition.is_active.is_(True),
        )
        .all()
    )

    roles_map = {}
    for row in rows:
        org_id = int(row.organization_id)
        role_key = str(row.key).strip().lower()
        roles_map.setdefault(org_id, [])
        if role_key not in roles_map[org_id]:
            roles_map[org_id].append(role_key)

    # 兼容旧数据：若还没有 member-role 绑定，回退到 organization_members.role
    for row in member_rows:
        org_id = int(row.organization_id)
        if roles_map.get(org_id):
            continue

        raw_role = row.role.value if hasattr(row.role, 'value') else row.role
        role_key = str(raw_role or '').strip().lower()
        if role_key:
            roles_map[org_id] = [role_key]
        else:
            roles_map.setdefault(org_id, [])

    return roles_map


def _collect_org_user_ids(organization_id):
    user_ids = set()
    org = Organization.query.get(organization_id)
    if not org:
        return user_ids
    user_ids.add(org.owner_id)
    member_rows = db.session.query(OrganizationMember.user_id).filter(
        OrganizationMember.organization_id == organization_id,
        OrganizationMember.status == OrganizationMemberStatus.ACTIVE
    ).all()
    user_ids.update([row.user_id for row in member_rows])
    return user_ids


def _invalidate_org_users(organization_id):
    for user_id in _collect_org_user_ids(organization_id):
        invalidate_user_caches(user_id)


def _accessible_org_query(current_user):
    member_org_ids = db.session.query(OrganizationMember.organization_id).filter(
        OrganizationMember.user_id == current_user.id,
        OrganizationMember.status == OrganizationMemberStatus.ACTIVE
    ).subquery()
    return Organization.query.filter(
        or_(
            Organization.owner_id == current_user.id,
            Organization.id.in_(member_org_ids)
        )
    )
