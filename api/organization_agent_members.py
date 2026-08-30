"""
组织 Agent 成员 API
"""

from datetime import datetime
from flask import Blueprint, g
from models import (
    db,
    Organization,
    Agent,
    AgentStatus,
    OrganizationAgentMember,
    OrganizationAgentMemberStatus,
)
from core.auth import unified_auth_required, get_current_user
from .base import ApiResponse, validate_json_request
from .agent_common import agent_session_required, write_agent_audit
from api.organizations.events import record_organization_event

organization_agent_members_bp = Blueprint('organization_agent_members', __name__)


def _get_org_for_user(organization_id):
    current_user = get_current_user()
    organization = Organization.query.get(organization_id)
    if not organization:
        return current_user, None, ApiResponse.not_found('Organization not found').to_response()

    if not current_user.can_access_organization(organization):
        return current_user, None, ApiResponse.forbidden('Access denied').to_response()

    return current_user, organization, None


@organization_agent_members_bp.route('/organizations/<int:organization_id>/agent-members', methods=['GET'])
@unified_auth_required
def list_organization_agent_members(organization_id):
    current_user, organization, err = _get_org_for_user(organization_id)
    if err:
        return err

    rows = OrganizationAgentMember.query.filter(
        OrganizationAgentMember.organization_id == organization_id,
        OrganizationAgentMember.status != OrganizationAgentMemberStatus.REMOVED,
    ).order_by(OrganizationAgentMember.created_at.asc()).all()

    return ApiResponse.success(
        {
            'organization_id': organization_id,
            'items': [row.to_dict(include_agent=True) for row in rows],
        },
        'Organization agent members retrieved successfully',
    ).to_response()


@organization_agent_members_bp.route('/organizations/<int:organization_id>/agents', methods=['POST'])
@unified_auth_required
def create_organization_agent(organization_id):
    current_user, organization, err = _get_org_for_user(organization_id)
    if err:
        return err

    if not current_user.can_manage_organization(organization):
        return ApiResponse.forbidden('Access denied').to_response()

    data = validate_json_request(
        required_fields=['name'],
        optional_fields=['description', 'capability_tags', 'allowed_project_ids'],
    )
    if isinstance(data, tuple):
        return data

    agent = Agent(
        workspace_id=organization_id,
        creator_user_id=current_user.id,
        name=data['name'].strip(),
        description=data.get('description', ''),
        capability_tags=data.get('capability_tags') or [],
        allowed_project_ids=data.get('allowed_project_ids') or [],
        status=AgentStatus.ACTIVE,
        created_by=current_user.email,
    )
    db.session.add(agent)
    db.session.flush()

    member = OrganizationAgentMember(
        organization_id=organization_id,
        agent_id=agent.id,
        invited_by_user_id=current_user.id,
        status=OrganizationAgentMemberStatus.ACTIVE,
        joined_at=datetime.utcnow(),
        responded_at=datetime.utcnow(),
        created_by=current_user.email,
    )
    db.session.add(member)

    record_organization_event(
        organization_id=organization_id,
        event_type='agent.created',
        actor_type='user',
        actor_id=current_user.id,
        actor_name=current_user.full_name or current_user.nickname or current_user.username or current_user.email,
        target_type='agent',
        target_id=agent.id,
        message=f"Agent created: {agent.name}",
        payload={'agent_name': agent.name},
        created_by=current_user.email,
    )

    write_agent_audit(
        event_type='org.agent.created',
        actor_type='user',
        actor_id=current_user.id,
        target_type='agent',
        target_id=agent.id,
        workspace_id=organization_id,
        payload={'organization_id': organization_id},
    )

    db.session.commit()
    return ApiResponse.created(
        {
            'member': member.to_dict(include_agent=True),
            'agent': agent.to_dict(),
        },
        'Organization agent created successfully',
    ).to_response()


@organization_agent_members_bp.route('/organizations/<int:organization_id>/agent-members/invite', methods=['POST'])
@unified_auth_required
def invite_organization_agent_member(organization_id):
    current_user, organization, err = _get_org_for_user(organization_id)
    if err:
        return err

    if not current_user.can_manage_organization(organization):
        return ApiResponse.forbidden('Access denied').to_response()

    data = validate_json_request(required_fields=['agent_id'])
    if isinstance(data, tuple):
        return data

    agent_id = int(data['agent_id'])
    agent = Agent.query.get(agent_id)
    if not agent:
        return ApiResponse.not_found('Agent not found').to_response()

    if agent.workspace_id != organization_id:
        return ApiResponse.error('Only agents in this organization can be invited', 400).to_response()

    member = OrganizationAgentMember.query.filter_by(
        organization_id=organization_id,
        agent_id=agent_id,
    ).first()

    if member:
        member.status = OrganizationAgentMemberStatus.INVITED
        member.invited_by_user_id = current_user.id
        member.responded_at = None
    else:
        member = OrganizationAgentMember(
            organization_id=organization_id,
            agent_id=agent_id,
            invited_by_user_id=current_user.id,
            status=OrganizationAgentMemberStatus.INVITED,
            created_by=current_user.email,
        )
        db.session.add(member)

    record_organization_event(
        organization_id=organization_id,
        event_type='agent.invited',
        actor_type='user',
        actor_id=current_user.id,
        actor_name=current_user.full_name or current_user.nickname or current_user.username or current_user.email,
        target_type='agent',
        target_id=agent.id,
        message=f"Agent invited: {agent.name}",
        payload={'agent_name': agent.name},
        created_by=current_user.email,
    )

    write_agent_audit(
        event_type='org.agent.invited',
        actor_type='user',
        actor_id=current_user.id,
        target_type='agent',
        target_id=agent.id,
        workspace_id=organization_id,
        payload={'organization_id': organization_id},
    )

    db.session.commit()
    return ApiResponse.success(member.to_dict(include_agent=True), 'Organization agent invited successfully').to_response()


@organization_agent_members_bp.route('/organizations/<int:organization_id>/agent-members/<int:membership_id>', methods=['DELETE'])
@unified_auth_required
def remove_organization_agent_member(organization_id, membership_id):
    current_user, organization, err = _get_org_for_user(organization_id)
    if err:
        return err

    if not current_user.can_manage_organization(organization):
        return ApiResponse.forbidden('Access denied').to_response()

    member = OrganizationAgentMember.query.filter_by(id=membership_id, organization_id=organization_id).first()
    if not member:
        return ApiResponse.not_found('Organization agent member not found').to_response()

    member.status = OrganizationAgentMemberStatus.REMOVED

    record_organization_event(
        organization_id=organization_id,
        event_type='agent.removed',
        actor_type='user',
        actor_id=current_user.id,
        actor_name=current_user.full_name or current_user.nickname or current_user.username or current_user.email,
        target_type='agent',
        target_id=member.agent_id,
        message=f"Agent removed: {member.agent_id}",
        payload={'agent_id': member.agent_id},
        created_by=current_user.email,
    )

    write_agent_audit(
        event_type='org.agent.removed',
        actor_type='user',
        actor_id=current_user.id,
        target_type='agent',
        target_id=member.agent_id,
        workspace_id=organization_id,
        payload={'organization_id': organization_id},
        risk_score=10,
    )

    db.session.commit()
    return ApiResponse.success(None, 'Organization agent removed successfully').to_response()


@organization_agent_members_bp.route('/agent/organization-invitations', methods=['GET'])
@agent_session_required
def list_agent_organization_invitations():
    agent = g.current_agent

    rows = OrganizationAgentMember.query.filter(
        OrganizationAgentMember.agent_id == agent.id,
        OrganizationAgentMember.status == OrganizationAgentMemberStatus.INVITED,
    ).order_by(OrganizationAgentMember.created_at.asc()).all()

    return ApiResponse.success(
        {
            'items': [row.to_dict(include_agent=False) for row in rows],
            'agent_id': agent.id,
        },
        'Agent invitations retrieved successfully',
    ).to_response()


@organization_agent_members_bp.route('/agent/organization-invitations/<int:membership_id>/accept', methods=['POST'])
@agent_session_required
def accept_agent_organization_invitation(membership_id):
    agent = g.current_agent
    member = OrganizationAgentMember.query.filter_by(id=membership_id, agent_id=agent.id).first()
    if not member:
        return ApiResponse.not_found('Invitation not found').to_response()

    if member.status != OrganizationAgentMemberStatus.INVITED:
        return ApiResponse.error('Invitation is not pending', 409).to_response()

    member.mark_active()
    record_organization_event(
        organization_id=member.organization_id,
        event_type='agent.accepted',
        actor_type='agent',
        actor_id=agent.id,
        actor_name=agent.name,
        target_type='agent',
        target_id=agent.id,
        message=f"Agent accepted invitation: {agent.name}",
        payload={'agent_name': agent.name},
        created_by=f'agent:{agent.id}',
    )
    db.session.commit()

    return ApiResponse.success(member.to_dict(include_agent=False), 'Invitation accepted').to_response()


@organization_agent_members_bp.route('/agent/organization-invitations/<int:membership_id>/reject', methods=['POST'])
@agent_session_required
def reject_agent_organization_invitation(membership_id):
    agent = g.current_agent
    member = OrganizationAgentMember.query.filter_by(id=membership_id, agent_id=agent.id).first()
    if not member:
        return ApiResponse.not_found('Invitation not found').to_response()

    if member.status != OrganizationAgentMemberStatus.INVITED:
        return ApiResponse.error('Invitation is not pending', 409).to_response()

    member.mark_rejected()
    record_organization_event(
        organization_id=member.organization_id,
        event_type='agent.rejected',
        actor_type='agent',
        actor_id=agent.id,
        actor_name=agent.name,
        target_type='agent',
        target_id=agent.id,
        message=f"Agent rejected invitation: {agent.name}",
        payload={'agent_name': agent.name},
        created_by=f'agent:{agent.id}',
    )
    db.session.commit()

    return ApiResponse.success(member.to_dict(include_agent=False), 'Invitation rejected').to_response()
