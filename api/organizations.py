"""
组织 API 蓝图
"""

import re
from datetime import datetime
from sqlalchemy import or_
from sqlalchemy.orm import selectinload
from flask import Blueprint
from models import (
    db,
    User,
    Organization,
    OrganizationStatus,
    OrganizationMember,
    OrganizationRole,
    OrganizationMemberStatus,
    OrganizationRoleDefinition,
    OrganizationMemberRole,
)
from .base import ApiResponse, validate_json_request, get_request_args, paginate_query
from core.auth import unified_auth_required, get_current_user
from core.cache_invalidation import invalidate_user_caches

organizations_bp = Blueprint('organizations', __name__)


SYSTEM_ROLE_DEFINITIONS = [
    {'key': 'owner', 'name': 'Owner', 'description': 'Organization owner'},
    {'key': 'admin', 'name': 'Admin', 'description': 'Organization admin'},
    {'key': 'member', 'name': 'Member', 'description': 'Organization member'},
    {'key': 'viewer', 'name': 'Viewer', 'description': 'Read-only member'},
]

ROLE_PRIORITY = ['owner', 'admin', 'member', 'viewer']


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


@organizations_bp.route('', methods=['GET'])
@organizations_bp.route('/', methods=['GET'])
@unified_auth_required
def list_organizations():
    try:
        current_user = get_current_user()
        args = get_request_args()

        query = _accessible_org_query(current_user)

        if args['search']:
            search_term = f"%{args['search']}%"
            query = query.filter(
                Organization.name.like(search_term) |
                Organization.description.like(search_term)
            )

        if args['status']:
            try:
                query = query.filter(
                    Organization.status == OrganizationStatus(args['status'])
                )
            except ValueError:
                return ApiResponse.error(f"Invalid status: {args['status']}", 400).to_response()
        else:
            query = query.filter(Organization.status == OrganizationStatus.ACTIVE)

        sort_by = args.get('sort_by', 'updated_at')
        sort_order = args.get('sort_order', 'desc')
        if sort_by == 'name':
            order_column = Organization.name
        elif sort_by == 'created_at':
            order_column = Organization.created_at
        else:
            order_column = Organization.updated_at
        query = query.order_by(order_column.desc() if sort_order == 'desc' else order_column.asc())

        result = paginate_query(query, args['page'], args['per_page'])

        org_ids = [item['id'] for item in result['items']]
        roles_map = _get_user_org_roles_map(org_ids, current_user.id)

        for item in result['items']:
            if item.get('owner_id') == current_user.id:
                item['current_user_roles'] = ['owner']
                item['current_user_role'] = 'owner'
                continue
            role_keys = roles_map.get(item['id'], [])
            item['current_user_roles'] = role_keys
            item['current_user_role'] = _compute_primary_role_from_keys(role_keys)

        return ApiResponse.success(result, "Organizations retrieved successfully").to_response()
    except Exception as e:
        return ApiResponse.error(f"Failed to retrieve organizations: {str(e)}", 500).to_response()


@organizations_bp.route('', methods=['POST'])
@organizations_bp.route('/', methods=['POST'])
@unified_auth_required
def create_organization():
    try:
        current_user = get_current_user()
        data = validate_json_request(
            required_fields=['name'],
            optional_fields=['slug', 'description']
        )
        if isinstance(data, tuple):
            return data

        base_slug = data.get('slug') or Organization.slugify(data['name'])
        slug = base_slug
        suffix = 1
        while Organization.query.filter_by(slug=slug).first():
            suffix += 1
            slug = f"{base_slug}-{suffix}"

        organization = Organization.create(
            owner_id=current_user.id,
            name=data['name'].strip(),
            slug=slug,
            description=data.get('description', ''),
            created_by=current_user.email,
            status=OrganizationStatus.ACTIVE,
        )
        db.session.flush()

        owner_member = OrganizationMember.create(
            organization_id=organization.id,
            user_id=current_user.id,
            role=OrganizationRole.OWNER,
            status=OrganizationMemberStatus.ACTIVE,
            invited_by=current_user.id,
            joined_at=datetime.utcnow(),
            created_by=current_user.email,
        )
        db.session.flush()

        _ensure_system_roles(organization.id, created_by=current_user.email)
        owner_role = OrganizationRoleDefinition.query.filter_by(
            organization_id=organization.id,
            key='owner'
        ).first()
        if owner_role:
            OrganizationMemberRole.create(
                organization_id=organization.id,
                member_id=owner_member.id,
                role_id=owner_role.id,
                created_by=current_user.email,
            )

        db.session.commit()
        invalidate_user_caches(current_user.id)

        payload = organization.to_dict(include_stats=True)
        payload['current_user_role'] = 'owner'
        payload['current_user_roles'] = ['owner']
        return ApiResponse.created(payload, "Organization created successfully").to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to create organization: {str(e)}", 500).to_response()


@organizations_bp.route('/<int:organization_id>', methods=['GET'])
@unified_auth_required
def get_organization(organization_id):
    try:
        current_user = get_current_user()
        organization = Organization.query.get(organization_id)
        if not organization:
            return ApiResponse.not_found("Organization not found").to_response()
        if not current_user.can_access_organization(organization):
            return ApiResponse.forbidden("Access denied").to_response()

        payload = organization.to_dict(include_stats=True)
        payload['current_user_role'] = current_user.get_organization_role(organization)
        payload['current_user_roles'] = current_user.get_organization_roles(organization)
        return ApiResponse.success(payload, "Organization retrieved successfully").to_response()
    except Exception as e:
        return ApiResponse.error(f"Failed to retrieve organization: {str(e)}", 500).to_response()


@organizations_bp.route('/<int:organization_id>', methods=['PUT'])
@unified_auth_required
def update_organization(organization_id):
    try:
        current_user = get_current_user()
        organization = Organization.query.get(organization_id)
        if not organization:
            return ApiResponse.not_found("Organization not found").to_response()
        if not current_user.can_manage_organization(organization):
            return ApiResponse.forbidden("Access denied").to_response()

        data = validate_json_request(
            optional_fields=['name', 'slug', 'description', 'status']
        )
        if isinstance(data, tuple):
            return data

        if 'slug' in data and data['slug'] and data['slug'] != organization.slug:
            existing = Organization.query.filter_by(slug=data['slug']).first()
            if existing:
                return ApiResponse.error("Organization slug already exists", 409).to_response()
            organization.slug = data['slug']

        if 'status' in data:
            try:
                organization.status = OrganizationStatus(data['status'])
            except ValueError:
                return ApiResponse.error(f"Invalid status: {data['status']}", 400).to_response()

        for key in ['name', 'description']:
            if key in data:
                setattr(organization, key, data[key])

        db.session.commit()
        _invalidate_org_users(organization_id)

        payload = organization.to_dict(include_stats=True)
        payload['current_user_role'] = current_user.get_organization_role(organization)
        payload['current_user_roles'] = current_user.get_organization_roles(organization)
        return ApiResponse.success(payload, "Organization updated successfully").to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to update organization: {str(e)}", 500).to_response()


@organizations_bp.route('/<int:organization_id>/roles', methods=['GET'])
@unified_auth_required
def list_organization_roles(organization_id):
    try:
        current_user = get_current_user()
        organization = Organization.query.get(organization_id)
        if not organization:
            return ApiResponse.not_found("Organization not found").to_response()
        if not current_user.can_access_organization(organization):
            return ApiResponse.forbidden("Access denied").to_response()

        _ensure_system_roles(organization_id)
        roles = (
            OrganizationRoleDefinition.query
            .filter_by(organization_id=organization_id)
            .order_by(OrganizationRoleDefinition.is_system.desc(), OrganizationRoleDefinition.id.asc())
            .all()
        )
        return ApiResponse.success(
            {
                'items': [item.to_dict() for item in roles],
                'organization_id': organization_id
            },
            "Organization roles retrieved successfully"
        ).to_response()
    except Exception as e:
        return ApiResponse.error(f"Failed to retrieve organization roles: {str(e)}", 500).to_response()


@organizations_bp.route('/<int:organization_id>/roles', methods=['POST'])
@unified_auth_required
def create_organization_role(organization_id):
    try:
        current_user = get_current_user()
        organization = Organization.query.get(organization_id)
        if not organization:
            return ApiResponse.not_found("Organization not found").to_response()
        if not current_user.can_manage_organization(organization):
            return ApiResponse.forbidden("Access denied").to_response()

        _ensure_system_roles(organization_id, created_by=current_user.email)

        data = validate_json_request(optional_fields=['name', 'title', 'key', 'description', 'content'])
        if isinstance(data, tuple):
            return data

        role_title = str(data.get('title') or data.get('name') or '').strip()
        if not role_title:
            return ApiResponse.error("Role title is required", 400).to_response()

        base_key = _slugify_role_key(data.get('key') or role_title)
        role_key = base_key
        suffix = 1
        while OrganizationRoleDefinition.query.filter_by(
            organization_id=organization_id,
            key=role_key
        ).first():
            suffix += 1
            role_key = f"{base_key}_{suffix}"

        role = OrganizationRoleDefinition.create(
            organization_id=organization_id,
            key=role_key,
            name=role_title,
            description=_normalize_optional_text(data.get('description')),
            content=_normalize_optional_text(data.get('content')),
            is_system=False,
            is_active=True,
            created_by=current_user.email,
        )
        db.session.commit()
        _invalidate_org_users(organization_id)

        return ApiResponse.created(role.to_dict(), "Organization role created successfully").to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to create organization role: {str(e)}", 500).to_response()


@organizations_bp.route('/<int:organization_id>/roles/<int:role_id>', methods=['PUT'])
@unified_auth_required
def update_organization_role(organization_id, role_id):
    try:
        current_user = get_current_user()
        organization = Organization.query.get(organization_id)
        if not organization:
            return ApiResponse.not_found("Organization not found").to_response()
        if not current_user.can_manage_organization(organization):
            return ApiResponse.forbidden("Access denied").to_response()

        role = OrganizationRoleDefinition.query.filter_by(
            id=role_id,
            organization_id=organization_id
        ).first()
        if not role:
            return ApiResponse.not_found("Organization role not found").to_response()

        data = validate_json_request(optional_fields=['name', 'title', 'description', 'content', 'is_active'])
        if isinstance(data, tuple):
            return data

        if role.is_system and ('is_active' in data and not bool(data.get('is_active'))):
            return ApiResponse.error("Cannot disable system role", 400).to_response()

        if 'title' in data or 'name' in data:
            role_title = str(data.get('title') or data.get('name') or '').strip()
            if not role_title:
                return ApiResponse.error("Role title cannot be empty", 400).to_response()
            role.name = role_title

        if 'description' in data:
            role.description = _normalize_optional_text(data.get('description'))

        if 'content' in data:
            role.content = _normalize_optional_text(data.get('content'))

        if 'is_active' in data:
            role.is_active = bool(data.get('is_active'))

        db.session.commit()
        _invalidate_org_users(organization_id)
        return ApiResponse.success(role.to_dict(), "Organization role updated successfully").to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to update organization role: {str(e)}", 500).to_response()


@organizations_bp.route('/<int:organization_id>/roles/<int:role_id>', methods=['DELETE'])
@unified_auth_required
def delete_organization_role(organization_id, role_id):
    try:
        current_user = get_current_user()
        organization = Organization.query.get(organization_id)
        if not organization:
            return ApiResponse.not_found("Organization not found").to_response()
        if not current_user.can_manage_organization(organization):
            return ApiResponse.forbidden("Access denied").to_response()

        role = OrganizationRoleDefinition.query.filter_by(
            id=role_id,
            organization_id=organization_id
        ).first()
        if not role:
            return ApiResponse.not_found("Organization role not found").to_response()
        if role.is_system:
            return ApiResponse.error("Cannot delete system role", 400).to_response()

        affected_rows = OrganizationMemberRole.query.filter_by(role_id=role_id).all()
        affected_member_ids = [row.member_id for row in affected_rows]

        for row in affected_rows:
            db.session.delete(row)
        db.session.flush()

        for member_id in affected_member_ids:
            member = OrganizationMember.query.get(member_id)
            if member:
                _sync_member_primary_role(member)

        db.session.delete(role)
        db.session.commit()
        _invalidate_org_users(organization_id)
        return ApiResponse.success(None, "Organization role deleted successfully").to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to delete organization role: {str(e)}", 500).to_response()


@organizations_bp.route('/<int:organization_id>/members', methods=['GET'])
@unified_auth_required
def list_organization_members(organization_id):
    try:
        current_user = get_current_user()
        organization = Organization.query.get(organization_id)
        if not organization:
            return ApiResponse.not_found("Organization not found").to_response()
        if not current_user.can_access_organization(organization):
            return ApiResponse.forbidden("Access denied").to_response()

        members = (
            OrganizationMember.query.options(
                selectinload(OrganizationMember.role_bindings).selectinload(OrganizationMemberRole.role)
            )
            .filter(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.status != OrganizationMemberStatus.REMOVED
            )
            .order_by(OrganizationMember.joined_at.asc())
            .all()
        )

        return ApiResponse.success(
            {
                'items': [member.to_dict(include_user=True) for member in members],
                'organization_id': organization_id
            },
            "Organization members retrieved successfully"
        ).to_response()
    except Exception as e:
        return ApiResponse.error(f"Failed to retrieve organization members: {str(e)}", 500).to_response()


@organizations_bp.route('/<int:organization_id>/members/invite', methods=['POST'])
@unified_auth_required
def invite_organization_member(organization_id):
    try:
        current_user = get_current_user()
        organization = Organization.query.get(organization_id)
        if not organization:
            return ApiResponse.not_found("Organization not found").to_response()
        if not current_user.can_manage_organization(organization):
            return ApiResponse.forbidden("Access denied").to_response()

        _ensure_system_roles(organization_id, created_by=current_user.email)

        data = validate_json_request(
            required_fields=['email'],
            optional_fields=['role', 'role_ids']
        )
        if isinstance(data, tuple):
            return data

        target_user = User.query.filter_by(email=data['email']).first()
        if not target_user:
            return ApiResponse.not_found("Target user not found").to_response()
        if target_user.id == organization.owner_id:
            return ApiResponse.error("Organization owner is already a member", 409).to_response()

        try:
            role_ids = _resolve_role_ids_from_payload(
                organization_id,
                data,
                default_role_key='member'
            )
        except ValueError as error:
            return ApiResponse.error(str(error), 400).to_response()

        owner_role = OrganizationRoleDefinition.query.filter_by(
            organization_id=organization_id,
            key='owner'
        ).first()
        if owner_role and owner_role.id in (role_ids or []):
            return ApiResponse.error("Cannot assign owner role through invite API", 400).to_response()

        member = OrganizationMember.query.filter_by(
            organization_id=organization_id,
            user_id=target_user.id
        ).first()
        if member:
            member.status = OrganizationMemberStatus.ACTIVE
            member.invited_by = current_user.id
            member.joined_at = datetime.utcnow()
        else:
            member = OrganizationMember.create(
                organization_id=organization_id,
                user_id=target_user.id,
                role=OrganizationRole.MEMBER,
                status=OrganizationMemberStatus.ACTIVE,
                invited_by=current_user.id,
                joined_at=datetime.utcnow(),
                created_by=current_user.email,
            )
            db.session.flush()

        _replace_member_roles(member, role_ids or [], created_by=current_user.email)

        db.session.commit()
        _invalidate_org_users(organization_id)
        return ApiResponse.success(
            member.to_dict(include_user=True),
            "Organization member invited successfully"
        ).to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to invite organization member: {str(e)}", 500).to_response()


@organizations_bp.route('/<int:organization_id>/members/<int:user_id>', methods=['PUT'])
@unified_auth_required
def update_organization_member(organization_id, user_id):
    try:
        current_user = get_current_user()
        organization = Organization.query.get(organization_id)
        if not organization:
            return ApiResponse.not_found("Organization not found").to_response()
        if not current_user.can_manage_organization(organization):
            return ApiResponse.forbidden("Access denied").to_response()
        if user_id == organization.owner_id:
            return ApiResponse.error("Cannot modify organization owner role", 400).to_response()

        _ensure_system_roles(organization_id, created_by=current_user.email)

        member = OrganizationMember.query.filter_by(
            organization_id=organization_id,
            user_id=user_id
        ).first()
        if not member:
            return ApiResponse.not_found("Organization member not found").to_response()

        data = validate_json_request(optional_fields=['role', 'role_ids', 'status'])
        if isinstance(data, tuple):
            return data

        if 'role_ids' in data or 'role' in data:
            try:
                role_ids = _resolve_role_ids_from_payload(organization_id, data, default_role_key=None)
            except ValueError as error:
                return ApiResponse.error(str(error), 400).to_response()

            owner_role = OrganizationRoleDefinition.query.filter_by(
                organization_id=organization_id,
                key='owner'
            ).first()
            if owner_role and owner_role.id in (role_ids or []):
                return ApiResponse.error("Cannot assign owner role through member update API", 400).to_response()

            _replace_member_roles(member, role_ids or [], created_by=current_user.email)

        if 'status' in data:
            try:
                member.status = OrganizationMemberStatus(data['status'])
            except ValueError:
                return ApiResponse.error(f"Invalid status: {data['status']}", 400).to_response()

        db.session.commit()
        _invalidate_org_users(organization_id)
        return ApiResponse.success(
            member.to_dict(include_user=True),
            "Organization member updated successfully"
        ).to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to update organization member: {str(e)}", 500).to_response()


@organizations_bp.route('/<int:organization_id>/members/<int:user_id>', methods=['DELETE'])
@unified_auth_required
def remove_organization_member(organization_id, user_id):
    try:
        current_user = get_current_user()
        organization = Organization.query.get(organization_id)
        if not organization:
            return ApiResponse.not_found("Organization not found").to_response()
        if not current_user.can_manage_organization(organization):
            return ApiResponse.forbidden("Access denied").to_response()
        if user_id == organization.owner_id:
            return ApiResponse.error("Cannot remove organization owner", 400).to_response()

        member = OrganizationMember.query.filter_by(
            organization_id=organization_id,
            user_id=user_id
        ).first()
        if not member:
            return ApiResponse.not_found("Organization member not found").to_response()

        db.session.delete(member)
        db.session.commit()
        _invalidate_org_users(organization_id)
        return ApiResponse.success(None, "Organization member removed successfully").to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to remove organization member: {str(e)}", 500).to_response()
