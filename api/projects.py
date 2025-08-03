"""
项目 API 蓝图

提供项目的 CRUD 操作接口
"""

from flask import Blueprint, request
from sqlalchemy import func, case
from datetime import datetime, timedelta
from models import db, Project, ProjectStatus, Task, TaskStatus
from .base import paginate_query, validate_json_request, get_request_args, APIException, ApiResponse
from core.auth import unified_auth_required, get_current_user

# 创建蓝图
projects_bp = Blueprint('projects', __name__)


@projects_bp.route('', methods=['GET'])
@unified_auth_required
def list_projects():
    """获取项目列表"""
    try:
        args = get_request_args()
        current_user = get_current_user()

        # 构建查询
        query = Project.query

        # 用户权限控制 - 所有用户（包括管理员）只能看到自己的项目
        if current_user:
            query = query.filter_by(owner_id=current_user.id)
        else:
            # 未登录用户不能访问项目列表
            return ApiResponse.unauthorized("Authentication required").to_response()
        
        # 状态筛选
        if args['status']:
            try:
                status = ProjectStatus(args['status'])
                query = query.filter_by(status=status)
            except ValueError:
                return ApiResponse.error(f"Invalid status: {args['status']}", 400).to_response()
        else:
            # 默认只显示未归档的项目
            query = query.filter(Project.status != ProjectStatus.ARCHIVED)

        # 是否归档筛选
        archived = request.args.get('archived')
        if archived == 'true':
            query = query.filter_by(status=ProjectStatus.ARCHIVED)
        elif archived == 'false':
            # 只显示活跃项目，排除已归档和已删除的项目
            query = query.filter_by(status=ProjectStatus.ACTIVE)

        # 是否有未完成任务筛选
        has_pending_tasks = request.args.get('has_pending_tasks')
        if has_pending_tasks == 'true':
            query = query.join(Task).filter(
                Task.status.in_([TaskStatus.TODO, TaskStatus.IN_PROGRESS, TaskStatus.REVIEW])
            ).distinct()
        elif has_pending_tasks == 'false':
            # 没有未完成任务的项目
            pending_project_ids = db.session.query(Task.project_id).filter(
                Task.status.in_([TaskStatus.TODO, TaskStatus.IN_PROGRESS, TaskStatus.REVIEW])
            ).distinct().subquery()
            query = query.filter(~Project.id.in_(pending_project_ids))

        # 时间范围筛选
        time_range = request.args.get('time_range')
        if time_range:
            now = datetime.utcnow()
            if time_range == 'today':
                start_time = now.replace(hour=0, minute=0, second=0, microsecond=0)
                query = query.filter(Project.last_activity_at >= start_time)
            elif time_range == 'week':
                start_time = now - timedelta(days=7)
                query = query.filter(Project.last_activity_at >= start_time)
            elif time_range == 'month':
                start_time = now - timedelta(days=30)
                query = query.filter(Project.last_activity_at >= start_time)

        # 搜索
        if args['search']:
            search_term = f"%{args['search']}%"
            query = query.filter(
                Project.name.like(search_term) |
                Project.description.like(search_term)
            )
        
        # 排序
        sort_by = args.get('sort_by', 'last_activity_at')
        sort_order = args.get('sort_order', 'desc')

        if sort_by == 'name':
            order_column = Project.name
        elif sort_by == 'created_at':
            order_column = Project.created_at
        elif sort_by == 'updated_at':
            order_column = Project.updated_at
        elif sort_by == 'last_activity_at':
            # 使用 COALESCE 确保 NULL 值被替换为一个很早的时间，这样在倒序排序时会排在最后
            order_column = func.coalesce(Project.last_activity_at, datetime(1970, 1, 1))
        elif sort_by == 'total_tasks':
            # 按任务总数排序
            query = query.outerjoin(Task).group_by(Project.id)
            order_column = func.count(Task.id)
        elif sort_by == 'pending_tasks':
            # 按未完成任务数排序
            query = query.outerjoin(Task).filter(
                (Task.status.in_([TaskStatus.TODO, TaskStatus.IN_PROGRESS, TaskStatus.REVIEW])) |
                (Task.id.is_(None))
            ).group_by(Project.id)
            order_column = func.count(Task.id)
        elif sort_by == 'completed_tasks':
            # 按已完成任务数排序
            query = query.outerjoin(Task).filter(
                (Task.status == TaskStatus.DONE) |
                (Task.id.is_(None))
            ).group_by(Project.id)
            order_column = func.count(Task.id)
        else:
            # 默认按最后活跃时间排序，NULL 值排在最后
            order_column = func.coalesce(Project.last_activity_at, datetime(1970, 1, 1))

        if sort_order == 'desc':
            query = query.order_by(order_column.desc())
        else:
            query = query.order_by(order_column.asc())
        
        # 分页
        result = paginate_query(query, args['page'], args['per_page'])

        # 为每个项目添加统计信息
        for project_dict in result['items']:
            project = Project.query.get(project_dict['id'])
            if project:
                # 添加任务统计信息
                from models.task import Task
                total_tasks = Task.query.filter_by(project_id=project.id).count()
                pending_tasks = Task.query.filter_by(project_id=project.id).filter(
                    Task.status.in_(['todo', 'in_progress', 'review'])
                ).count()
                completed_tasks = Task.query.filter_by(project_id=project.id).filter_by(status='done').count()

                project_dict.update({
                    'total_tasks': total_tasks,
                    'pending_tasks': pending_tasks,
                    'completed_tasks': completed_tasks,
                    'completion_rate': round((completed_tasks / total_tasks * 100) if total_tasks > 0 else 0, 1)
                })

        # 使用新的ApiResponse类，统一响应格式
        return ApiResponse.success(
            data=result,
            message="Projects retrieved successfully"
        ).to_response()
        
    except Exception as e:
        return ApiResponse.error(f"Failed to retrieve projects: {str(e)}", 500).to_response()


@projects_bp.route('', methods=['POST'])
@unified_auth_required
def create_project():
    """创建新项目"""
    try:
        current_user = get_current_user()

        # 验证请求数据
        data = validate_json_request(
            required_fields=['name'],
            optional_fields=['description', 'color', 'status', 'github_url', 'local_url', 'production_url', 'project_context']
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

        # 创建项目
        current_time = datetime.utcnow()
        project = Project.create(
            name=data['name'],
            description=data.get('description', ''),
            color=data.get('color', '#1890ff'),
            owner_id=current_user.id,
            created_by=current_user.email,
            github_url=data.get('github_url', ''),
            local_url=data.get('local_url', ''),
            production_url=data.get('production_url', ''),
            project_context=data.get('project_context', ''),
            last_activity_at=current_time  # 设置最后活跃时间为创建时间
        )
        
        db.session.commit()
        
        return ApiResponse.created(
            data=project.to_dict(include_stats=True),
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

        return ApiResponse.success(
            project.to_dict(include_stats=True),
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
        if not current_user.can_access_project(project):
            return ApiResponse.error("Access denied", 403).to_response()
        
        # 验证请求数据
        data = validate_json_request(
            optional_fields=['name', 'description', 'color', 'status', 'github_url', 'local_url', 'production_url', 'project_context']
        )
        
        if isinstance(data, tuple):  # 错误响应
            return data
        
        # 检查项目名称是否已被其他项目使用
        if 'name' in data and data['name'] != project.name:
            existing_project = Project.query.filter_by(name=data['name']).first()
            if existing_project:
                return ApiResponse.error("Project name already exists", 409, error_details={"code": "DUPLICATE_NAME"}).to_response()
        
        # 更新项目
        project.update_from_dict(data)

        # 处理状态更新
        if 'status' in data:
            try:
                project.status = ProjectStatus(data['status'])
            except ValueError:
                return api_error(f"Invalid status: {data['status']}", 400)

        # 更新项目最后活动时间
        project.last_activity_at = datetime.utcnow()

        db.session.commit()
        
        return api_response(
            project.to_dict(include_stats=True),
            "Project updated successfully"
        )
        
    except Exception as e:
        db.session.rollback()
        return api_error(f"Failed to update project: {str(e)}", 500)


@projects_bp.route('/<int:project_id>', methods=['DELETE'])
def delete_project(project_id):
    """删除项目（软删除）"""
    try:
        project = Project.query.get(project_id)
        if not project:
            return ApiResponse.error("Project not found", 404, error_details={"code": "PROJECT_NOT_FOUND"}).to_response()
        
        # 软删除
        project.soft_delete()
        
        return ApiResponse.success(None, "Project deleted successfully", 204).to_response()
        
    except Exception as e:
        db.session.rollback()
        return api_error(f"Failed to delete project: {str(e)}", 500)


@projects_bp.route('/<int:project_id>/archive', methods=['POST'])
def archive_project(project_id):
    """归档项目"""
    try:
        project = Project.query.get(project_id)
        if not project:
            return ApiResponse.error("Project not found", 404, error_details={"code": "PROJECT_NOT_FOUND"}).to_response()
        
        project.archive()
        
        return api_response(
            project.to_dict(),
            "Project archived successfully"
        )
        
    except Exception as e:
        db.session.rollback()
        return api_error(f"Failed to archive project: {str(e)}", 500)


@projects_bp.route('/<int:project_id>/restore', methods=['POST'])
def restore_project(project_id):
    """恢复项目"""
    try:
        project = Project.query.get(project_id)
        if not project:
            return ApiResponse.error("Project not found", 404, error_details={"code": "PROJECT_NOT_FOUND"}).to_response()
        
        project.restore()
        
        return api_response(
            project.to_dict(),
            "Project restored successfully"
        )
        
    except Exception as e:
        db.session.rollback()
        return api_error(f"Failed to restore project: {str(e)}", 500)


@projects_bp.route('/<int:project_id>/tasks', methods=['GET'])
def get_project_tasks(project_id):
    """获取项目的任务列表"""
    try:
        project = Project.query.get(project_id)
        if not project:
            return ApiResponse.error("Project not found", 404, error_details={"code": "PROJECT_NOT_FOUND"}).to_response()
        
        args = get_request_args()
        
        # 构建任务查询
        query = project.tasks
        
        # 状态筛选
        if args['status']:
            from models import TaskStatus
            try:
                status = TaskStatus(args['status'])
                query = query.filter_by(status=status)
            except ValueError:
                return api_error(f"Invalid status: {args['status']}", 400)
        
        # 优先级筛选
        if args['priority']:
            from models import TaskPriority
            try:
                priority = TaskPriority(args['priority'])
                query = query.filter_by(priority=priority)
            except ValueError:
                return api_error(f"Invalid priority: {args['priority']}", 400)
        
        # 分配者筛选
        if args['assignee']:
            query = query.filter_by(assignee=args['assignee'])
        
        # 搜索
        if args['search']:
            from models import Task
            search_term = f"%{args['search']}%"
            query = query.filter(
                Task.title.like(search_term) |
                Task.description.like(search_term) |
                Task.content.like(search_term)
            )
        
        # 分页
        result = paginate_query(query, args['page'], args['per_page'])
        
        return ApiResponse.success(result, "Project tasks retrieved successfully").to_response()
        
    except Exception as e:
        return api_error(f"Failed to retrieve project tasks: {str(e)}", 500)


@projects_bp.route('/<int:project_id>/context-rules', methods=['GET'])
def get_project_context_rules(project_id):
    """获取项目的上下文规则"""
    try:
        project = Project.query.get(project_id)
        if not project:
            return ApiResponse.error("Project not found", 404, error_details={"code": "PROJECT_NOT_FOUND"}).to_response()

        rules = project.get_active_context_rules()

        return api_response(
            [rule.to_dict() for rule in rules],
            "Project context rules retrieved successfully"
        )

    except Exception as e:
        return api_error(f"Failed to retrieve project context rules: {str(e)}", 500)
