"""Organization member management routes."""

from datetime import datetime

from sqlalchemy.orm import selectinload

from models import (
    db,
    User,
    Organization,
    OrganizationMember,
    OrganizationRole,
    OrganizationMemberStatus,
    OrganizationRoleDefinition,
    OrganizationMemberRole,
)
from ..base import ApiResponse, validate_json_request
from core.auth import unified_auth_required, get_current_user

from . import organizations_bp
from .shared import (
    _ensure_system_roles,
    _resolve_role_ids_from_payload,
    _replace_member_roles,
    _invalidate_org_users,
)
from .events import record_organization_event

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

        record_organization_event(
            organization_id=organization_id,
            event_type='member.invited',
            actor_type='user',
            actor_id=current_user.id,
            actor_name=current_user.full_name or current_user.nickname or current_user.username or current_user.email,
            target_type='member',
            target_id=target_user.id,
            message=f"Member invited: {target_user.email}",
            payload={
                'member_user_id': target_user.id,
                'member_email': target_user.email,
                'role_ids': role_ids or [],
            },
            created_by=current_user.email,
        )

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

        role_ids = None
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

        event_payload = {}
        if 'role_ids' in data or 'role' in data:
            event_payload['role_ids'] = role_ids or []
        if 'status' in data:
            event_payload['status'] = data['status']

        record_organization_event(
            organization_id=organization_id,
            event_type='member.updated',
            actor_type='user',
            actor_id=current_user.id,
            actor_name=current_user.full_name or current_user.nickname or current_user.username or current_user.email,
            target_type='member',
            target_id=user_id,
            message=f"Member updated: {user_id}",
            payload=event_payload,
            created_by=current_user.email,
        )

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

        record_organization_event(
            organization_id=organization_id,
            event_type='member.removed',
            actor_type='user',
            actor_id=current_user.id,
            actor_name=current_user.full_name or current_user.nickname or current_user.username or current_user.email,
            target_type='member',
            target_id=user_id,
            message=f"Member removed: {user_id}",
            payload={'member_user_id': user_id},
            created_by=current_user.email,
        )

        db.session.delete(member)
        db.session.commit()
        _invalidate_org_users(organization_id)
        return ApiResponse.success(None, "Organization member removed successfully").to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to remove organization member: {str(e)}", 500).to_response()
