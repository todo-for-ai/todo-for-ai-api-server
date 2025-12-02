"""
项目 API 蓝图

提供项目的 CRUD 操作接口
"""

from flask import Blueprint, request
from sqlalchemy import func, case
from datetime import datetime, timedelta
from models import db, Project, ProjectStatus, Task, TaskStatus
from .base import api_response, api_error, paginate_query, paginate_query_fast, validate_json_request, get_request_args, APIException
from core.auth import optional_token_auth
from core.github_config import require_auth, get_current_user

# 创建蓝图
projects_bp = Blueprint('projects', __name__)


@projects_bp.route('', methods=['GET'])
@require_auth
def list_projects():
    """获取项目列表"""
    try:
        args = get_request_args()
        current_user = get_current_user()

        # 构建查询
        query = Project.query

        # 用户权限控制
        if current_user:
            if not current_user.is_admin():
                # 普通用户只能看到自己的项目
                query = query.filter_by(owner_id=current_user.id)
        else:
            # 未登录用户不能访问项目列表
            return api_error("Authentication required", 401)
        
        # 状态筛选
        if args['status']:
            try:
                status = ProjectStatus(args['status'])
                query = query.filter_by(status=status)
            except ValueError:
                return api_error(f"Invalid status: {args['status']}", 400)
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
            order_column = Project.last_activity_at
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
            order_column = Project.last_activity_at

        if sort_order == 'desc':
            query = query.order_by(order_column.desc())
        else:
            query = query.order_by(order_column.asc())
        
        # 分页（项目数量不多，使用正常分页）
        result = paginate_query(query, args['page'], args['per_page'])

        # 一次性查询所有项目的任务统计（避免N+1查询）
        if result['items']:
            project_ids = [p['id'] for p in result['items']]
            
            # 使用GROUP BY一次性查询所有统计
            stats_query = db.session.query(
                Task.project_id,
                func.count(Task.id).label('total_tasks'),
                func.sum(case((Task.status.in_([TaskStatus.TODO, TaskStatus.IN_PROGRESS, TaskStatus.REVIEW]), 1), else_=0)).label('pending_tasks'),
                func.sum(case((Task.status == TaskStatus.DONE, 1), else_=0)).label('completed_tasks')
            ).filter(
                Task.project_id.in_(project_ids)
            ).group_by(Task.project_id).all()
            
            # 构建统计字典
            stats_dict = {}
            for project_id, total, pending, completed in stats_query:
                stats_dict[project_id] = {
                    'total_tasks': total or 0,
                    'pending_tasks': pending or 0,
                    'completed_tasks': completed or 0,
                }
            
            # 为每个项目添加统计信息
            for project_dict in result['items']:
                stats = stats_dict.get(project_dict['id'], {
                    'total_tasks': 0,
                    'pending_tasks': 0,
                    'completed_tasks': 0,
                })
                total = stats['total_tasks']
                completed = stats['completed_tasks']
                project_dict.update({
                    'total_tasks': total,
                    'pending_tasks': stats['pending_tasks'],
                    'completed_tasks': completed,
                    'completion_rate': round((completed / total * 100) if total > 0 else 0, 1)
                })

        # 直接返回项目列表和分页信息，不要额外包装
        return api_response(result['items'], "Projects retrieved successfully", pagination=result['pagination'])
        
    except Exception as e:
        return api_error(f"Failed to retrieve projects: {str(e)}", 500)


@projects_bp.route('', methods=['POST'])
@require_auth
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
            return api_error("Project name already exists", 409, "DUPLICATE_NAME")

        # 创建项目
        project = Project.create(
            name=data['name'],
            description=data.get('description', ''),
            color=data.get('color', '#1890ff'),
            owner_id=current_user.id,
            created_by=current_user.email
        )
        
        db.session.commit()
        
        return api_response(
            project.to_dict(include_stats=True),
            "Project created successfully",
            201
        )
        
    except Exception as e:
        db.session.rollback()
        return api_error(f"Failed to create project: {str(e)}", 500)


@projects_bp.route('/<int:project_id>', methods=['GET'])
@require_auth
def get_project(project_id):
    """获取单个项目详情"""
    try:
        current_user = get_current_user()

        project = Project.query.get(project_id)
        if not project:
            return api_error("Project not found", 404, "PROJECT_NOT_FOUND")

        # 权限检查
        if current_user:
            if not current_user.can_access_project(project):
                return api_error("Access denied", 403)
        else:
            return api_error("Authentication required", 401)

        return api_response(
            project.to_dict(include_stats=True),
            "Project retrieved successfully"
        )
        
    except Exception as e:
        return api_error(f"Failed to retrieve project: {str(e)}", 500)


@projects_bp.route('/<int:project_id>', methods=['PUT'])
@require_auth
def update_project(project_id):
    """更新项目"""
    try:
        current_user = get_current_user()

        project = Project.query.get(project_id)
        if not project:
            return api_error("Project not found", 404, "PROJECT_NOT_FOUND")

        # 权限检查
        if not current_user.can_access_project(project):
            return api_error("Access denied", 403)
        
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
                return api_error("Project name already exists", 409, "DUPLICATE_NAME")
        
        # 更新项目
        project.update_from_dict(data)
        
        # 处理状态更新
        if 'status' in data:
            try:
                project.status = ProjectStatus(data['status'])
            except ValueError:
                return api_error(f"Invalid status: {data['status']}", 400)
        
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
            return api_error("Project not found", 404, "PROJECT_NOT_FOUND")
        
        # 软删除
        project.soft_delete()
        
        return api_response(
            None,
            "Project deleted successfully",
            204
        )
        
    except Exception as e:
        db.session.rollback()
        return api_error(f"Failed to delete project: {str(e)}", 500)


@projects_bp.route('/<int:project_id>/archive', methods=['POST'])
def archive_project(project_id):
    """归档项目"""
    try:
        project = Project.query.get(project_id)
        if not project:
            return api_error("Project not found", 404, "PROJECT_NOT_FOUND")
        
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
            return api_error("Project not found", 404, "PROJECT_NOT_FOUND")
        
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
    """获取项目的任务列表（性能优化版）"""
    try:
        # 检查项目是否存在
        project = Project.query.get(project_id)
        if not project:
            return api_error("Project not found", 404, "PROJECT_NOT_FOUND")
        
        args = get_request_args()
        
        # 构建任务查询（使用Task.query而不是project.tasks，性能更好）
        from models import Task
        query = Task.query.filter_by(project_id=project_id)
        
        # 状态筛选
        if args['status']:
            from models import TaskStatus
            try:
                # 支持多状态筛选
                if ',' in args['status']:
                    status_list = [TaskStatus(s.strip()) for s in args['status'].split(',')]
                    query = query.filter(Task.status.in_(status_list))
                else:
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
            query = query.filter_by(assignee_id=args['assignee'])
        
        # 搜索
        if args['search']:
            search_term = f"%{args['search']}%"
            query = query.filter(
                Task.title.like(search_term) |
                Task.content.like(search_term)
            )
        
        # 排序（使用id降序，性能最佳）
        sort_by = args.get('sort_by', 'id')
        if sort_by == 'created_at':
            order_column = Task.created_at
        elif sort_by == 'updated_at':
            order_column = Task.updated_at
        elif sort_by == 'priority':
            order_column = Task.priority
        else:
            order_column = Task.id
        
        sort_order = args.get('sort_order', 'desc')
        if sort_order == 'desc':
            query = query.order_by(order_column.desc())
        else:
            query = query.order_by(order_column.asc())
        
        # 分页（避免慢COUNT查询）
        page = args['page']
        per_page = min(args['per_page'], 100)
        offset = (page - 1) * per_page
        
        # 获取数据（使用LIMIT+1判断是否有下一页）
        tasks = query.limit(per_page + 1).offset(offset).all()
        
        # 判断是否有下一页
        has_next = len(tasks) > per_page
        if has_next:
            tasks = tasks[:per_page]
        
        result = {
            'items': [task.to_dict() for task in tasks],
            'pagination': {
                'page': page,
                'per_page': per_page,
                'has_prev': page > 1,
                'has_next': has_next,
                'prev_num': page - 1 if page > 1 else None,
                'next_num': page + 1 if has_next else None
            }
        }
        
        return api_response(result, "Project tasks retrieved successfully")
        
    except Exception as e:
        return api_error(f"Failed to retrieve project tasks: {str(e)}", 500)


@projects_bp.route('/<int:project_id>/context-rules', methods=['GET'])
def get_project_context_rules(project_id):
    """获取项目的上下文规则"""
    try:
        project = Project.query.get(project_id)
        if not project:
            return api_error("Project not found", 404, "PROJECT_NOT_FOUND")

        rules = project.get_active_context_rules()

        return api_response(
            [rule.to_dict() for rule in rules],
            "Project context rules retrieved successfully"
        )

    except Exception as e:
        return api_error(f"Failed to retrieve project context rules: {str(e)}", 500)
