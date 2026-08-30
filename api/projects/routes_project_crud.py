"""Project CRUD and lifecycle routes."""

from datetime import datetime

from models import (
    db,
    Project,
    ProjectStatus,
    ProjectMember,
    ProjectMemberRole,
    ProjectMemberStatus,
    Organization,
)
from core.auth import unified_auth_required, get_current_user
from ..base import validate_json_request, ApiResponse

from . import projects_bp
from .shared import _invalidate_project_users
from api.organizations.events import record_organization_event


def _user_display_name(user):
    if not user:
        return None
    return user.full_name or user.nickname or user.username or user.email or str(user.id)


@projects_bp.route('', methods=['POST'])
@projects_bp.route('/', methods=['POST'])
@unified_auth_required
def create_project():
    """创建新项目"""
    try:
        current_user = get_current_user()

        # 验证请求数据
        data = validate_json_request(
            required_fields=['name'],
            optional_fields=['description', 'color', 'status', 'github_url', 'local_url', 'production_url', 'project_context', 'organization_id']
        )

        if isinstance(data, tuple):  # 错误响应
            return data

        # 检查项目名称是否已存在（在用户范围内）
        existing_project = Project.query.filter_by(
            name=data['name'],
            owner_id=current_user.id
        ).first()
        if existing_project:
            return ApiResponse.error("Project name already exists", 409,
                                   error_details={"code": "DUPLICATE_NAME"}).to_response()

        organization_id = data.get('organization_id')
        if organization_id is not None:
            organization = Organization.query.get(organization_id)
            if not organization:
                return ApiResponse.not_found("Organization not found").to_response()
            if not current_user.can_access_organization(organization):
                return ApiResponse.forbidden("Access denied to organization").to_response()

        # 创建项目
        current_time = datetime.utcnow()
        project = Project.create(
            name=data['name'],
            description=data.get('description', ''),
            color=data.get('color', '#1890ff'),
            owner_id=current_user.id,
            organization_id=organization_id,
            created_by=current_user.email,
            github_url=data.get('github_url', ''),
            local_url=data.get('local_url', ''),
            production_url=data.get('production_url', ''),
            project_context=data.get('project_context', ''),
            last_activity_at=current_time  # 设置最后活跃时间为创建时间
        )
        db.session.flush()

        ProjectMember.create(
            project_id=project.id,
            user_id=current_user.id,
            role=ProjectMemberRole.OWNER,
            status=ProjectMemberStatus.ACTIVE,
            invited_by=current_user.id,
            joined_at=current_time,
            created_by=current_user.email,
        )

        record_organization_event(
            organization_id=project.organization_id,
            event_type='project.created',
            actor_type='user',
            actor_id=current_user.id,
            actor_name=_user_display_name(current_user),
            target_type='project',
            target_id=project.id,
            project_id=project.id,
            message=f"Project created: {project.name}",
            payload={
                'project_name': project.name,
                'project_status': project.status.value if hasattr(project.status, 'value') else project.status,
            },
            created_by=current_user.email,
        )

        db.session.commit()
        _invalidate_project_users(project.id)

        payload = project.to_dict(include_stats=True)
        payload['current_user_role'] = current_user.get_project_role(project)

        return ApiResponse.created(
            data=payload,
            message="Project created successfully"
        ).to_response()

    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to create project: {str(e)}", 500).to_response()


@projects_bp.route('/<int:project_id>', methods=['GET'])
@unified_auth_required
def get_project(project_id):
    """获取单个项目详情"""
    try:
        current_user = get_current_user()

        project = Project.query.get(project_id)
        if not project:
            return ApiResponse.not_found("Project not found",
                                       error_details={"code": "PROJECT_NOT_FOUND"}).to_response()

        # 权限检查
        if current_user:
            if not current_user.can_access_project(project):
                return ApiResponse.forbidden("Access denied").to_response()
        else:
            return ApiResponse.unauthorized("Authentication required").to_response()

        payload = project.to_dict(include_stats=True)
        payload['current_user_role'] = current_user.get_project_role(project)
        return ApiResponse.success(
            payload,
            "Project retrieved successfully"
        ).to_response()

    except Exception as e:
        return ApiResponse.error(f"Failed to retrieve project: {str(e)}", 500).to_response()


@projects_bp.route('/<int:project_id>', methods=['PUT'])
@unified_auth_required
def update_project(project_id):
    """更新项目"""
    try:
        current_user = get_current_user()

        project = Project.query.get(project_id)
        if not project:
            return ApiResponse.error("Project not found", 404, error_details={"code": "PROJECT_NOT_FOUND"}).to_response()

        # 权限检查
        if not current_user.can_manage_project(project):
            return ApiResponse.error("Access denied", 403).to_response()

        # 验证请求数据
        data = validate_json_request(
            optional_fields=['name', 'description', 'color', 'status', 'github_url', 'local_url', 'production_url', 'project_context', 'organization_id']
        )

        if isinstance(data, tuple):  # 错误响应
            return data

        # 检查项目名称是否已被其他项目使用
        if 'name' in data and data['name'] != project.name:
            existing_project = Project.query.filter(
                Project.name == data['name'],
                Project.owner_id == project.owner_id,
                Project.id != project.id
            ).first()
            if existing_project:
                return ApiResponse.error("Project name already exists", 409, error_details={"code": "DUPLICATE_NAME"}).to_response()

        if 'organization_id' in data:
            org_id = data['organization_id']
            if org_id is None:
                project.organization_id = None
            else:
                organization = Organization.query.get(org_id)
                if not organization:
                    return ApiResponse.not_found("Organization not found").to_response()
                if not current_user.can_access_organization(organization):
                    return ApiResponse.forbidden("Access denied to organization").to_response()
                project.organization_id = org_id

        # 更新项目
        project.update_from_dict(data)

        # 处理状态更新
        if 'status' in data:
            try:
                project.status = ProjectStatus(data['status'])
            except ValueError:
                return ApiResponse.error(f"Invalid status: {data['status']}", 400).to_response()

        # 更新项目最后活动时间
        project.last_activity_at = datetime.utcnow()

        record_organization_event(
            organization_id=project.organization_id,
            event_type='project.updated',
            actor_type='user',
            actor_id=current_user.id,
            actor_name=_user_display_name(current_user),
            target_type='project',
            target_id=project.id,
            project_id=project.id,
            message=f"Project updated: {project.name}",
            payload={
                'project_name': project.name,
                'changed_fields': list(data.keys()),
            },
            created_by=current_user.email,
        )

        db.session.commit()
        _invalidate_project_users(project.id)

        payload = project.to_dict(include_stats=True)
        payload['current_user_role'] = current_user.get_project_role(project)
        return ApiResponse.success(
            payload,
            "Project updated successfully"
        ).to_response()

    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to update project: {str(e)}", 500).to_response()


@projects_bp.route('/<int:project_id>', methods=['DELETE'])
@unified_auth_required
def delete_project(project_id):
    """删除项目（软删除）"""
    try:
        current_user = get_current_user()

        project = Project.query.get(project_id)
        if not project:
            return ApiResponse.error("Project not found", 404, error_details={"code": "PROJECT_NOT_FOUND"}).to_response()

        # 权限检查
        if not current_user.can_manage_project(project):
            return ApiResponse.error("Access denied", 403, error_details={"code": "PERMISSION_DENIED"}).to_response()

        # 软删除
        record_organization_event(
            organization_id=project.organization_id,
            event_type='project.deleted',
            actor_type='user',
            actor_id=current_user.id,
            actor_name=_user_display_name(current_user),
            target_type='project',
            target_id=project.id,
            project_id=project.id,
            message=f"Project deleted: {project.name}",
            payload={'project_name': project.name},
            created_by=current_user.email,
        )
        project.soft_delete()
        _invalidate_project_users(project.id)

        return ApiResponse.success(None, "Project deleted successfully", 204).to_response()

    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to delete project: {str(e)}", 500).to_response()


@projects_bp.route('/<int:project_id>/archive', methods=['POST'])
@unified_auth_required
def archive_project(project_id):
    """归档项目"""
    try:
        current_user = get_current_user()

        project = Project.query.get(project_id)
        if not project:
            return ApiResponse.error("Project not found", 404, error_details={"code": "PROJECT_NOT_FOUND"}).to_response()

        # 权限检查
        if not current_user.can_manage_project(project):
            return ApiResponse.error("Access denied", 403, error_details={"code": "PERMISSION_DENIED"}).to_response()

        record_organization_event(
            organization_id=project.organization_id,
            event_type='project.archived',
            actor_type='user',
            actor_id=current_user.id,
            actor_name=_user_display_name(current_user),
            target_type='project',
            target_id=project.id,
            project_id=project.id,
            message=f"Project archived: {project.name}",
            payload={'project_name': project.name},
            created_by=current_user.email,
        )
        project.archive()
        _invalidate_project_users(project.id)

        return ApiResponse.success(
            project.to_dict(),
            "Project archived successfully"
        ).to_response()

    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to archive project: {str(e)}", 500).to_response()


@projects_bp.route('/<int:project_id>/restore', methods=['POST'])
@unified_auth_required
def restore_project(project_id):
    """恢复项目"""
    try:
        current_user = get_current_user()

        project = Project.query.get(project_id)
        if not project:
            return ApiResponse.error("Project not found", 404, error_details={"code": "PROJECT_NOT_FOUND"}).to_response()

        # 权限检查
        if not current_user.can_manage_project(project):
            return ApiResponse.error("Access denied", 403, error_details={"code": "PERMISSION_DENIED"}).to_response()

        record_organization_event(
            organization_id=project.organization_id,
            event_type='project.restored',
            actor_type='user',
            actor_id=current_user.id,
            actor_name=_user_display_name(current_user),
            target_type='project',
            target_id=project.id,
            project_id=project.id,
            message=f"Project restored: {project.name}",
            payload={'project_name': project.name},
            created_by=current_user.email,
        )
        project.restore()
        _invalidate_project_users(project.id)

        return ApiResponse.success(
            project.to_dict(),
            "Project restored successfully"
        ).to_response()

    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to restore project: {str(e)}", 500).to_response()
