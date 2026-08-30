"""Project member management routes."""

from datetime import datetime

from models import db, User, Project, ProjectMember, ProjectMemberRole, ProjectMemberStatus
from core.auth import unified_auth_required, get_current_user
from ..base import validate_json_request, ApiResponse

from . import projects_bp
from .shared import _invalidate_project_users


@projects_bp.route('/<int:project_id>/members', methods=['GET'])
@unified_auth_required
def list_project_members(project_id):
    """获取项目成员"""
    try:
        current_user = get_current_user()
        project = Project.query.get(project_id)
        if not project:
            return ApiResponse.not_found("Project not found").to_response()
        if not current_user.can_access_project(project):
            return ApiResponse.forbidden("Access denied").to_response()

        members = ProjectMember.query.filter(
            ProjectMember.project_id == project_id,
            ProjectMember.status != ProjectMemberStatus.REMOVED
        ).order_by(ProjectMember.joined_at.asc()).all()

        items = [member.to_dict(include_user=True) for member in members]
        return ApiResponse.success(
            {
                'items': items,
                'project_id': project_id
            },
            "Project members retrieved successfully"
        ).to_response()
    except Exception as e:
        return ApiResponse.error(f"Failed to retrieve project members: {str(e)}", 500).to_response()


@projects_bp.route('/<int:project_id>/members/invite', methods=['POST'])
@unified_auth_required
def invite_project_member(project_id):
    """邀请项目成员"""
    try:
        current_user = get_current_user()
        project = Project.query.get(project_id)
        if not project:
            return ApiResponse.not_found("Project not found").to_response()
        if not current_user.can_manage_project(project):
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
        if target_user.id == project.owner_id:
            return ApiResponse.error("Project owner is already a member", 409).to_response()

        role_value = data.get('role', 'member')
        try:
            role = ProjectMemberRole(role_value)
        except ValueError:
            return ApiResponse.error(f"Invalid role: {role_value}", 400).to_response()

        member = ProjectMember.query.filter_by(
            project_id=project_id,
            user_id=target_user.id
        ).first()
        if member:
            member.role = role
            member.status = ProjectMemberStatus.ACTIVE
            member.invited_by = current_user.id
            member.joined_at = datetime.utcnow()
        else:
            member = ProjectMember.create(
                project_id=project_id,
                user_id=target_user.id,
                role=role,
                status=ProjectMemberStatus.ACTIVE,
                invited_by=current_user.id,
                joined_at=datetime.utcnow(),
                created_by=current_user.email,
            )

        db.session.commit()
        _invalidate_project_users(project_id)
        return ApiResponse.success(
            member.to_dict(include_user=True),
            "Project member invited successfully"
        ).to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to invite project member: {str(e)}", 500).to_response()


@projects_bp.route('/<int:project_id>/members/<int:user_id>', methods=['PUT'])
@unified_auth_required
def update_project_member(project_id, user_id):
    """更新项目成员"""
    try:
        current_user = get_current_user()
        project = Project.query.get(project_id)
        if not project:
            return ApiResponse.not_found("Project not found").to_response()
        if not current_user.can_manage_project(project):
            return ApiResponse.forbidden("Access denied").to_response()
        if user_id == project.owner_id:
            return ApiResponse.error("Cannot modify project owner role", 400).to_response()

        member = ProjectMember.query.filter_by(
            project_id=project_id,
            user_id=user_id
        ).first()
        if not member:
            return ApiResponse.not_found("Project member not found").to_response()

        data = validate_json_request(optional_fields=['role', 'status'])
        if isinstance(data, tuple):
            return data

        if 'role' in data:
            try:
                member.role = ProjectMemberRole(data['role'])
            except ValueError:
                return ApiResponse.error(f"Invalid role: {data['role']}", 400).to_response()
        if 'status' in data:
            try:
                member.status = ProjectMemberStatus(data['status'])
            except ValueError:
                return ApiResponse.error(f"Invalid status: {data['status']}", 400).to_response()

        db.session.commit()
        _invalidate_project_users(project_id)
        return ApiResponse.success(
            member.to_dict(include_user=True),
            "Project member updated successfully"
        ).to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to update project member: {str(e)}", 500).to_response()


@projects_bp.route('/<int:project_id>/members/<int:user_id>', methods=['DELETE'])
@unified_auth_required
def remove_project_member(project_id, user_id):
    """移除项目成员"""
    try:
        current_user = get_current_user()
        project = Project.query.get(project_id)
        if not project:
            return ApiResponse.not_found("Project not found").to_response()
        if not current_user.can_manage_project(project):
            return ApiResponse.forbidden("Access denied").to_response()
        if user_id == project.owner_id:
            return ApiResponse.error("Cannot remove project owner", 400).to_response()

        member = ProjectMember.query.filter_by(
            project_id=project_id,
            user_id=user_id
        ).first()
        if not member:
            return ApiResponse.not_found("Project member not found").to_response()

        db.session.delete(member)
        db.session.commit()
        _invalidate_project_users(project_id)
        return ApiResponse.success(None, "Project member removed successfully").to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to remove project member: {str(e)}", 500).to_response()
