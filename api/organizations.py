"""
组织 API 蓝图
"""

from datetime import datetime
from sqlalchemy import or_
from flask import Blueprint
from models import (
    db,
    User,
    Organization,
    OrganizationStatus,
    OrganizationMember,
    OrganizationRole,
    OrganizationMemberStatus,
)
from .base import ApiResponse, validate_json_request, get_request_args, paginate_query
from core.auth import unified_auth_required, get_current_user
from core.cache_invalidation import invalidate_user_caches

organizations_bp = Blueprint('organizations', __name__)


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
        role_map = {}
        if org_ids:
            rows = OrganizationMember.query.filter(
                OrganizationMember.organization_id.in_(org_ids),
                OrganizationMember.user_id == current_user.id,
                OrganizationMember.status == OrganizationMemberStatus.ACTIVE
            ).all()
            role_map = {
                row.organization_id: row.role.value
                for row in rows
            }

        for item in result['items']:
            item['current_user_role'] = 'owner' if item.get('owner_id') == current_user.id else role_map.get(item['id'])

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

        OrganizationMember.create(
            organization_id=organization.id,
            user_id=current_user.id,
            role=OrganizationRole.OWNER,
            status=OrganizationMemberStatus.ACTIVE,
            invited_by=current_user.id,
            joined_at=datetime.utcnow(),
            created_by=current_user.email,
        )

        db.session.commit()
        invalidate_user_caches(current_user.id)

        payload = organization.to_dict(include_stats=True)
        payload['current_user_role'] = 'owner'
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
        return ApiResponse.success(payload, "Organization updated successfully").to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to update organization: {str(e)}", 500).to_response()


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

        members = OrganizationMember.query.filter(
            OrganizationMember.organization_id == organization_id,
            OrganizationMember.status != OrganizationMemberStatus.REMOVED
        ).order_by(OrganizationMember.joined_at.asc()).all()

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

        data = validate_json_request(
            required_fields=['email'],
            optional_fields=['role']
        )
        if isinstance(data, tuple):
            return data

        target_user = User.query.filter_by(email=data['email']).first()
        if not target_user:
            return ApiResponse.not_found("Target user not found").to_response()
        if target_user.id == organization.owner_id:
            return ApiResponse.error("Organization owner is already a member", 409).to_response()

        role_value = data.get('role', 'member')
        try:
            role = OrganizationRole(role_value)
        except ValueError:
            return ApiResponse.error(f"Invalid role: {role_value}", 400).to_response()

        member = OrganizationMember.query.filter_by(
            organization_id=organization_id,
            user_id=target_user.id
        ).first()
        if member:
            member.role = role
            member.status = OrganizationMemberStatus.ACTIVE
            member.invited_by = current_user.id
            member.joined_at = datetime.utcnow()
        else:
            member = OrganizationMember.create(
                organization_id=organization_id,
                user_id=target_user.id,
                role=role,
                status=OrganizationMemberStatus.ACTIVE,
                invited_by=current_user.id,
                joined_at=datetime.utcnow(),
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

        member = OrganizationMember.query.filter_by(
            organization_id=organization_id,
            user_id=user_id
        ).first()
        if not member:
            return ApiResponse.not_found("Organization member not found").to_response()

        data = validate_json_request(optional_fields=['role', 'status'])
        if isinstance(data, tuple):
            return data

        if 'role' in data:
            try:
                member.role = OrganizationRole(data['role'])
            except ValueError:
                return ApiResponse.error(f"Invalid role: {data['role']}", 400).to_response()

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
