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
from core.redis_client import get_json as redis_get_json, set_json as redis_set_json
from core.cache_invalidation import invalidate_user_caches

# 创建蓝图
projects_bp = Blueprint('projects', __name__)

PROJECTS_LIST_CACHE_TTL_SECONDS = 20
PROJECTS_LIST_HEAVY_CACHE_TTL_SECONDS = 120
TASK_SORT_FIELDS = {'total_tasks', 'pending_tasks', 'completed_tasks'}
projects_list_fallback_cache = {}


def _projects_cache_get(key):
    redis_key = f"projects:list:{key}"
    cached = redis_get_json(redis_key)
    if cached is not None:
        return cached

    item = projects_list_fallback_cache.get(key)
    if item:
        ttl_seconds = item.get('ttl', PROJECTS_LIST_CACHE_TTL_SECONDS)
        if datetime.utcnow().timestamp() - item['cached_at'] <= ttl_seconds:
            return item['value']
    return None


def _projects_cache_set(key, value, ttl=PROJECTS_LIST_CACHE_TTL_SECONDS):
    redis_key = f"projects:list:{key}"
    redis_set_json(redis_key, value, ttl)
    projects_list_fallback_cache[key] = {
        'cached_at': datetime.utcnow().timestamp(),
        'ttl': ttl,
        'value': value,
    }


@projects_bp.route('', methods=['GET'])
@projects_bp.route('/', methods=['GET'])
@unified_auth_required
def list_projects():
    """获取项目列表"""
    try:
        args = get_request_args()
        current_user = get_current_user()

        # 构建查询
        query = Project.query
        pending_statuses = [TaskStatus.TODO, TaskStatus.IN_PROGRESS, TaskStatus.REVIEW]

        # 用户权限控制 - 所有用户（包括管理员）只能看到自己的项目
        if current_user:
            query = query.filter_by(owner_id=current_user.id)
        else:
            # 未登录用户不能访问项目列表
            return ApiResponse.unauthorized("Authentication required").to_response()

        # 缓存仅用于列表查询（按用户 + 查询参数隔离）
        cache_key = f"user:{current_user.id}:q:{request.query_string.decode('utf-8')}"
        cached_result = _projects_cache_get(cache_key)
        if cached_result is not None:
            return ApiResponse.success(
                data=cached_result,
                message="Projects retrieved successfully"
            ).to_response()
        
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
        pending_tasks_exists = db.session.query(Task.id).filter(
            Task.project_id == Project.id,
            Task.owner_id == current_user.id,
            Task.status.in_(pending_statuses)
        ).exists()
        if has_pending_tasks == 'true':
            query = query.filter(pending_tasks_exists)
        elif has_pending_tasks == 'false':
            query = query.filter(~pending_tasks_exists)

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

        if sort_by in TASK_SORT_FIELDS:
            task_stats_subquery = db.session.query(
                Task.project_id.label('project_id'),
                func.count(Task.id).label('total_tasks'),
                func.sum(case((Task.status.in_(pending_statuses), 1), else_=0)).label('pending_tasks'),
                func.sum(case((Task.status == TaskStatus.DONE, 1), else_=0)).label('completed_tasks')
            ).group_by(Task.project_id).subquery()

            sorted_query = query.outerjoin(
                task_stats_subquery,
                task_stats_subquery.c.project_id == Project.id
            )

            if sort_by == 'total_tasks':
                order_column = func.coalesce(task_stats_subquery.c.total_tasks, 0)
            elif sort_by == 'pending_tasks':
                order_column = func.coalesce(task_stats_subquery.c.pending_tasks, 0)
            else:
                order_column = func.coalesce(task_stats_subquery.c.completed_tasks, 0)

            if sort_order == 'desc':
                sorted_query = sorted_query.order_by(order_column.desc())
            else:
                sorted_query = sorted_query.order_by(order_column.asc())

            page = max(args['page'], 1)
            per_page = max(min(args['per_page'], 100), 1)
            offset = (page - 1) * per_page

            # 对任务统计排序场景，total 不依赖任务聚合，直接使用基础项目查询计数避免慢查询
            total = query.count()
            items = sorted_query.limit(per_page).offset(offset).all()
            pages = (total + per_page - 1) // per_page if total > 0 else 1

            result = {
                'items': [item.to_dict() for item in items],
                'pagination': {
                    'page': page,
                    'per_page': per_page,
                    'total': total,
                    'pages': pages,
                    'has_prev': page > 1,
                    'has_next': (offset + per_page) < total,
                    'prev_num': page - 1 if page > 1 else None,
                    'next_num': page + 1 if (offset + per_page) < total else None
                }
            }
        else:
            if sort_by == 'name':
                order_column = Project.name
            elif sort_by == 'created_at':
                order_column = Project.created_at
            elif sort_by == 'updated_at':
                order_column = Project.updated_at
            else:
                # 默认按最后活跃时间排序，NULL 值排在最后
                order_column = func.coalesce(Project.last_activity_at, datetime(1970, 1, 1))

            if sort_order == 'desc':
                query = query.order_by(order_column.desc())
            else:
                query = query.order_by(order_column.asc())

            result = paginate_query(query, args['page'], args['per_page'])

        include_stats = request.args.get('include_stats', 'true').lower() != 'false'
        if include_stats:
            # 批量统计当前页项目的任务数据，避免 N+1 查询
            project_ids = [item['id'] for item in result['items'] if item.get('id')]
            task_stats_map = {}

            if project_ids:
                stats_rows = db.session.query(
                    Task.project_id.label('project_id'),
                    func.count(Task.id).label('total_tasks'),
                    func.sum(case((Task.status.in_(pending_statuses), 1), else_=0)).label('pending_tasks'),
                    func.sum(case((Task.status == TaskStatus.DONE, 1), else_=0)).label('completed_tasks')
                ).filter(
                    Task.owner_id == current_user.id,
                    Task.project_id.in_(project_ids)
                ).group_by(
                    Task.project_id
                ).all()

                task_stats_map = {
                    row.project_id: {
                        'total_tasks': int(row.total_tasks or 0),
                        'pending_tasks': int(row.pending_tasks or 0),
                        'completed_tasks': int(row.completed_tasks or 0),
                    }
                    for row in stats_rows
                }

            for project_dict in result['items']:
                stats = task_stats_map.get(project_dict['id'], {
                    'total_tasks': 0,
                    'pending_tasks': 0,
                    'completed_tasks': 0,
                })
                total_tasks = stats['total_tasks']
                completed_tasks = stats['completed_tasks']
                project_dict.update({
                    **stats,
                    'completion_rate': round((completed_tasks / total_tasks * 100) if total_tasks > 0 else 0, 1)
                })

        cache_ttl = (
            PROJECTS_LIST_HEAVY_CACHE_TTL_SECONDS
            if sort_by in TASK_SORT_FIELDS or has_pending_tasks == 'true'
            else PROJECTS_LIST_CACHE_TTL_SECONDS
        )
        _projects_cache_set(cache_key, result, ttl=cache_ttl)

        # 使用新的ApiResponse类，统一响应格式
        return ApiResponse.success(
            data=result,
            message="Projects retrieved successfully"
        ).to_response()
        
    except Exception as e:
        return ApiResponse.error(f"Failed to retrieve projects: {str(e)}", 500).to_response()


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
        invalidate_user_caches(current_user.id)
        
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
        invalidate_user_caches(current_user.id)
        
        return ApiResponse.success(
            project.to_dict(include_stats=True),
            "Project updated successfully"
        ).to_response()
        
    except Exception as e:
        db.session.rollback()
        return api_error(f"Failed to update project: {str(e)}", 500)


@projects_bp.route('/<int:project_id>', methods=['DELETE'])
@unified_auth_required
def delete_project(project_id):
    """删除项目（软删除）"""
    try:
        current_user = get_current_user()

        project = Project.query.get(project_id)
        if not project:
            return ApiResponse.error("Project not found", 404, error_details={"code": "PROJECT_NOT_FOUND"}).to_response()

        # 权限检查 - 只能删除自己的项目
        if project.owner_id != current_user.id:
            return ApiResponse.error("Access denied: You can only delete your own projects", 403, error_details={"code": "PERMISSION_DENIED"}).to_response()
        
        # 软删除
        project.soft_delete()
        invalidate_user_caches(current_user.id)
        
        return ApiResponse.success(None, "Project deleted successfully", 204).to_response()
        
    except Exception as e:
        db.session.rollback()
        return api_error(f"Failed to delete project: {str(e)}", 500)


@projects_bp.route('/<int:project_id>/archive', methods=['POST'])
@unified_auth_required
def archive_project(project_id):
    """归档项目"""
    try:
        current_user = get_current_user()

        project = Project.query.get(project_id)
        if not project:
            return ApiResponse.error("Project not found", 404, error_details={"code": "PROJECT_NOT_FOUND"}).to_response()

        # 权限检查 - 只能归档自己的项目
        if project.owner_id != current_user.id:
            return ApiResponse.error("Access denied: You can only archive your own projects", 403, error_details={"code": "PERMISSION_DENIED"}).to_response()
        
        project.archive()
        invalidate_user_caches(current_user.id)
        
        return ApiResponse.success(
            project.to_dict(),
            "Project archived successfully"
        ).to_response()
        
    except Exception as e:
        db.session.rollback()
        return api_error(f"Failed to archive project: {str(e)}", 500)


@projects_bp.route('/<int:project_id>/restore', methods=['POST'])
@unified_auth_required
def restore_project(project_id):
    """恢复项目"""
    try:
        current_user = get_current_user()

        project = Project.query.get(project_id)
        if not project:
            return ApiResponse.error("Project not found", 404, error_details={"code": "PROJECT_NOT_FOUND"}).to_response()

        # 权限检查 - 只能恢复自己的项目
        if project.owner_id != current_user.id:
            return ApiResponse.error("Access denied: You can only restore your own projects", 403, error_details={"code": "PERMISSION_DENIED"}).to_response()
        
        project.restore()
        invalidate_user_caches(current_user.id)
        
        return ApiResponse.success(
            project.to_dict(),
            "Project restored successfully"
        ).to_response()
        
    except Exception as e:
        db.session.rollback()
        return api_error(f"Failed to restore project: {str(e)}", 500)

# TODO: 我感觉这个接口并没有任何作用，因为tasks.py有list接口,确定没用的话之后整个删掉吧
# @projects_bp.route('/<int:project_id>/tasks', methods=['GET'])
# @unified_auth_required
# def get_project_tasks(project_id):
#     """获取项目的任务列表"""
#     try:
#         current_user = get_current_user()
#
#         project = Project.query.get(project_id)
#         if not project:
#             return ApiResponse.error("Project not found", 404, error_details={"code": "PROJECT_NOT_FOUND"}).to_response()
#
#         # 权限检查 - 只能访问自己项目的任务
#         if project.owner_id != current_user.id:
#             return ApiResponse.error("Access denied: You can only access tasks from your own projects", 403, error_details={"code": "PERMISSION_DENIED"}).to_response()
#
#         args = get_request_args()
#
#         # 构建任务查询
#         query = project.tasks
#
#         # 状态筛选
#         if args['status']:
#             from models import TaskStatus
#             try:
#                 status = TaskStatus(args['status'])
#                 query = query.filter_by(status=status)
#             except ValueError:
#                 return ApiResponse.error(f"Invalid status: {args['status']}", 400).to_response()
#
#         # 优先级筛选
#         if args['priority']:
#             from models import TaskPriority
#             try:
#                 priority = TaskPriority(args['priority'])
#                 query = query.filter_by(priority=priority)
#             except ValueError:
#                 return api_error(f"Invalid priority: {args['priority']}", 400)
#
#         # 分配者筛选
#         if args['assignee']:
#             query = query.filter_by(assignee=args['assignee'])
#
#         # 搜索
#         if args['search']:
#             from models import Task
#             search_term = f"%{args['search']}%"
#             query = query.filter(
#                 Task.title.like(search_term) |
#                 Task.description.like(search_term) |
#                 Task.content.like(search_term)
#             )
#
#         # 分页
#         result = paginate_query(query, args['page'], args['per_page'])
#
#         return ApiResponse.success(result, "Project tasks retrieved successfully").to_response()
#
#     except Exception as e:
#         return api_error(f"Failed to retrieve project tasks: {str(e)}", 500)

# TODO: 同上 因为有list_context_rules方法了，感觉这个也是没用的接口,确定没用的话之后整个删掉吧
# @projects_bp.route('/<int:project_id>/context-rules', methods=['GET'])
# @unified_auth_required
# def get_project_context_rules(project_id):
#     """获取项目的上下文规则"""
#     try:
#         current_user = get_current_user()
#
#         project = Project.query.get(project_id)
#         if not project:
#             return ApiResponse.error("Project not found", 404, error_details={"code": "PROJECT_NOT_FOUND"}).to_response()
#
#         # 权限检查 - 只能访问自己项目的上下文规则
#         if project.owner_id != current_user.id:
#             return ApiResponse.error("Access denied: You can only access context rules from your own projects", 403, error_details={"code": "PERMISSION_DENIED"}).to_response()
#
#         rules = project.get_active_context_rules()
#
#         return ApiResponse.success(
#             [rule.to_dict() for rule in rules],
#             "Project context rules retrieved successfully"
#         ).to_response()
#
#     except Exception as e:
#         return api_error(f"Failed to retrieve project context rules: {str(e)}", 500)
