"""Organization role management routes."""

from models import (
    db,
    Organization,
    OrganizationRoleDefinition,
    OrganizationMemberRole,
    OrganizationMember,
)
from ..base import ApiResponse, validate_json_request
from core.auth import unified_auth_required, get_current_user

from . import organizations_bp
from .shared import (
    _ensure_system_roles,
    _normalize_optional_text,
    _slugify_role_key,
    _sync_member_primary_role,
    _invalidate_org_users,
)

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
