"""Organization list/detail routes."""

from datetime import datetime
from sqlalchemy import func

from models import (
    db,
    Organization,
    OrganizationStatus,
    OrganizationMember,
    OrganizationAgentMember,
    OrganizationAgentMemberStatus,
    OrganizationRole,
    OrganizationMemberStatus,
    OrganizationRoleDefinition,
    OrganizationMemberRole,
    Project,
)
from ..base import ApiResponse, validate_json_request, get_request_args, paginate_query
from core.auth import unified_auth_required, get_current_user
from core.cache_invalidation import invalidate_user_caches

from . import organizations_bp
from .shared import (
    _accessible_org_query,
    _compute_primary_role_from_keys,
    _ensure_system_roles,
    _get_user_org_roles_map,
    _invalidate_org_users,
)
from .events import record_organization_event

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
        member_counts = {}
        agent_counts = {}
        project_counts = {}
        active_role_counts = {}
        last_activity_at_map = {}

        if org_ids:
            member_counts = {
                int(row.organization_id): int(row.total or 0)
                for row in (
                    db.session.query(
                        OrganizationMember.organization_id,
                        func.count(OrganizationMember.id).label('total'),
                    )
                    .filter(
                        OrganizationMember.organization_id.in_(org_ids),
                        OrganizationMember.status == OrganizationMemberStatus.ACTIVE,
                    )
                    .group_by(OrganizationMember.organization_id)
                    .all()
                )
            }
            agent_counts = {
                int(row.organization_id): int(row.total or 0)
                for row in (
                    db.session.query(
                        OrganizationAgentMember.organization_id,
                        func.count(func.distinct(OrganizationAgentMember.agent_id)).label('total'),
                    )
                    .filter(
                        OrganizationAgentMember.organization_id.in_(org_ids),
                        OrganizationAgentMember.status != OrganizationAgentMemberStatus.REMOVED,
                    )
                    .group_by(OrganizationAgentMember.organization_id)
                    .all()
                )
            }
            project_counts = {
                int(row.organization_id): int(row.total or 0)
                for row in (
                    db.session.query(
                        Project.organization_id,
                        func.count(Project.id).label('total'),
                    )
                    .filter(Project.organization_id.in_(org_ids))
                    .group_by(Project.organization_id)
                    .all()
                )
            }
            active_role_counts = {
                int(row.organization_id): int(row.total or 0)
                for row in (
                    db.session.query(
                        OrganizationRoleDefinition.organization_id,
                        func.count(OrganizationRoleDefinition.id).label('total'),
                    )
                    .filter(
                        OrganizationRoleDefinition.organization_id.in_(org_ids),
                        OrganizationRoleDefinition.is_active.is_(True),
                    )
                    .group_by(OrganizationRoleDefinition.organization_id)
                    .all()
                )
            }
            last_activity_at_map = {
                int(row.organization_id): row.last_activity_at
                for row in (
                    db.session.query(
                        Project.organization_id,
                        func.max(
                            func.coalesce(
                                Project.last_activity_at,
                                Project.updated_at,
                                Project.created_at,
                            )
                        ).label('last_activity_at'),
                    )
                    .filter(Project.organization_id.in_(org_ids))
                    .group_by(Project.organization_id)
                    .all()
                )
            }

        for item in result['items']:
            item['member_count'] = member_counts.get(item['id'], 0)
            item['agent_count'] = agent_counts.get(item['id'], 0)
            item['project_count'] = project_counts.get(item['id'], 0)
            item['active_role_count'] = active_role_counts.get(item['id'], 0)
            project_last_activity = last_activity_at_map.get(item['id'])
            project_last_activity_iso = project_last_activity.isoformat() if project_last_activity else None
            item['last_activity_at'] = max(
                [value for value in [item.get('updated_at'), project_last_activity_iso] if value],
                default=None,
            )
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

        record_organization_event(
            organization_id=organization.id,
            event_type='org.created',
            actor_type='user',
            actor_id=current_user.id,
            actor_name=current_user.full_name or current_user.nickname or current_user.username or current_user.email,
            target_type='organization',
            target_id=organization.id,
            message=f"Organization created: {organization.name}",
            payload={'organization_name': organization.name},
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

        event_type = 'org.updated'
        if str(data.get('status') or '').lower() == 'archived':
            event_type = 'org.archived'

        record_organization_event(
            organization_id=organization.id,
            event_type=event_type,
            actor_type='user',
            actor_id=current_user.id,
            actor_name=current_user.full_name or current_user.nickname or current_user.username or current_user.email,
            target_type='organization',
            target_id=organization.id,
            message=f"Organization updated: {organization.name}",
            payload={'changed_fields': list(data.keys())},
            created_by=current_user.email,
        )

        db.session.commit()
        _invalidate_org_users(organization_id)

        payload = organization.to_dict(include_stats=True)
        payload['current_user_role'] = current_user.get_organization_role(organization)
        payload['current_user_roles'] = current_user.get_organization_roles(organization)
        return ApiResponse.success(payload, "Organization updated successfully").to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to update organization: {str(e)}", 500).to_response()
