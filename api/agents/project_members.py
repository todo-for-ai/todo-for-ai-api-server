"""
Project member management API routes.

Manage project membership and roles.
Extracted from api/agents/_core.py for better organization.
"""

from datetime import datetime

from flask import request

from ._shared import (
    agents_bp,
    ApiResponse,
    validate_json_request,
    get_current_user,
    unified_auth_required,
    db,
    Project,
    ProjectMember,
    ProjectRole,
    AuditLog,
    _client_ip,
)


@agents_bp.route("/projects/<int:project_id>/members", methods=["GET"])
@unified_auth_required
def list_project_members(project_id):
    """List members of a project with their roles."""
    user = get_current_user()
    project = Project.query.filter_by(id=project_id, owner_id=user.id).first()
    if not project:
        # Also allow if user is a project member
        membership = ProjectMember.query.filter_by(project_id=project_id, user_id=user.id).first()
        if not membership:
            return ApiResponse.not_found("Project not found").to_response()

    members = ProjectMember.query.filter_by(project_id=project_id).all()
    items = [m.to_dict() for m in members]
    return ApiResponse.success(items).to_response()


@agents_bp.route("/projects/<int:project_id>/members", methods=["POST"])
@unified_auth_required
def add_project_member(project_id):
    """Add a member to a project with a specified role. Only admins/owners can do this."""
    user = get_current_user()

    # Check if current user can manage this project
    if not ProjectMember.can(project_id, user.id, "manage"):
        # Also allow project owner (legacy)
        project = Project.query.filter_by(id=project_id, owner_id=user.id).first()
        if not project:
            return ApiResponse.error("Insufficient permissions", 403).to_response()

    data = validate_json_request()
    target_user_id = data.get("user_id")
    role_str = data.get("role", "member")

    if not target_user_id:
        return ApiResponse.error("user_id is required", 400).to_response()

    try:
        role = ProjectRole(role_str)
    except ValueError:
        return ApiResponse.error(f"Invalid role: {role_str}. Must be owner/admin/member/viewer", 400).to_response()

    # Can't assign OWNER role via API
    if role == ProjectRole.OWNER:
        return ApiResponse.error("Cannot assign owner role via API", 400).to_response()

    # Check if already a member
    existing = ProjectMember.query.filter_by(project_id=project_id, user_id=target_user_id).first()
    if existing:
        return ApiResponse.error("User is already a member", 409).to_response()

    from models import User
    target_user = User.query.get(target_user_id)
    if not target_user:
        return ApiResponse.error("User not found", 404).to_response()

    member = ProjectMember.create(
        project_id=project_id,
        user_id=target_user_id,
        role=role,
        invited_by=user.id,
        accepted_at=datetime.utcnow(),
    )
    db.session.commit()

    AuditLog.record(
        action="project.member_added", resource_type="project", resource_id=project_id,
        actor_type="human", actor_user_id=user.id,
        project_id=project_id,
        detail={"target_user_id": target_user_id, "role": role.value},
        ip_address=_client_ip(),
    )
    db.session.commit()

    return ApiResponse.created(member.to_dict(), "Member added").to_response()


@agents_bp.route("/projects/<int:project_id>/members/<int:member_id>", methods=["PUT"])
@unified_auth_required
def update_project_member(project_id, member_id):
    """Update a member's role. Only admins/owners can do this."""
    user = get_current_user()

    if not ProjectMember.can(project_id, user.id, "manage"):
        project = Project.query.filter_by(id=project_id, owner_id=user.id).first()
        if not project:
            return ApiResponse.error("Insufficient permissions", 403).to_response()

    member = ProjectMember.query.filter_by(id=member_id, project_id=project_id).first()
    if not member:
        return ApiResponse.not_found("Member not found").to_response()

    data = validate_json_request()
    role_str = data.get("role")
    if not role_str:
        return ApiResponse.error("role is required", 400).to_response()

    try:
        new_role = ProjectRole(role_str)
    except ValueError:
        return ApiResponse.error(f"Invalid role: {role_str}", 400).to_response()

    if new_role == ProjectRole.OWNER:
        return ApiResponse.error("Cannot assign owner role via API", 400).to_response()

    old_role = member.role.value if member.role else None
    member.role = new_role
    db.session.commit()

    AuditLog.record(
        action="project.member_updated", resource_type="project", resource_id=project_id,
        actor_type="human", actor_user_id=user.id,
        project_id=project_id,
        detail={"member_id": member_id, "old_role": old_role, "new_role": new_role.value},
        ip_address=_client_ip(),
    )
    db.session.commit()

    return ApiResponse.success(member.to_dict(), "Member role updated").to_response()


@agents_bp.route("/projects/<int:project_id>/members/<int:member_id>", methods=["DELETE"])
@unified_auth_required
def remove_project_member(project_id, member_id):
    """Remove a member from a project. Only admins/owners can do this."""
    user = get_current_user()

    if not ProjectMember.can(project_id, user.id, "manage"):
        project = Project.query.filter_by(id=project_id, owner_id=user.id).first()
        if not project:
            return ApiResponse.error("Insufficient permissions", 403).to_response()

    member = ProjectMember.query.filter_by(id=member_id, project_id=project_id).first()
    if not member:
        return ApiResponse.not_found("Member not found").to_response()

    if member.role == ProjectRole.OWNER:
        return ApiResponse.error("Cannot remove the project owner", 400).to_response()

    db.session.delete(member)
    db.session.commit()

    AuditLog.record(
        action="project.member_removed", resource_type="project", resource_id=project_id,
        actor_type="human", actor_user_id=user.id,
        project_id=project_id,
        detail={"removed_user_id": member.user_id},
        ip_address=_client_ip(),
    )
    db.session.commit()

    return ApiResponse.success(None, "Member removed").to_response()