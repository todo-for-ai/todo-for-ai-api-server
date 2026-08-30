"""Project listing route."""

from datetime import datetime, timedelta

from flask import request
from sqlalchemy import func, case

from models import db, Project, ProjectStatus, ProjectMember, ProjectMemberStatus, Task, TaskStatus
from core.auth import unified_auth_required, get_current_user
from ..base import paginate_query, get_request_args, ApiResponse

from . import projects_bp
from .shared import (
    PROJECTS_LIST_CACHE_TTL_SECONDS,
    PROJECTS_LIST_HEAVY_CACHE_TTL_SECONDS,
    TASK_SORT_FIELDS,
    _projects_cache_get,
    _projects_cache_set,
    _accessible_projects_query,
)


@projects_bp.route('', methods=['GET'])
@projects_bp.route('/', methods=['GET'])
@unified_auth_required
def list_projects():
    """获取项目列表"""
    try:
        args = get_request_args()
        current_user = get_current_user()

        # 构建查询
        query = _accessible_projects_query(current_user)
        pending_statuses = [TaskStatus.TODO, TaskStatus.IN_PROGRESS, TaskStatus.REVIEW]

        # 用户权限控制
        if not current_user:
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

        # 组织筛选
        organization_id = request.args.get('organization_id', type=int)
        if organization_id is not None:
            query = query.filter(Project.organization_id == organization_id)

        # 是否有未完成任务筛选
        has_pending_tasks = request.args.get('has_pending_tasks')
        pending_tasks_exists = db.session.query(Task.id).filter(
            Task.project_id == Project.id,
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

        project_ids = [item['id'] for item in result['items'] if item.get('id')]
        role_map = {}
        if project_ids:
            role_rows = ProjectMember.query.filter(
                ProjectMember.project_id.in_(project_ids),
                ProjectMember.user_id == current_user.id,
                ProjectMember.status == ProjectMemberStatus.ACTIVE
            ).all()
            role_map = {
                row.project_id: row.role.value
                for row in role_rows
            }
        for project_dict in result['items']:
            project_dict['current_user_role'] = (
                'owner' if project_dict.get('owner_id') == current_user.id
                else role_map.get(project_dict.get('id'))
            )

        include_stats = request.args.get('include_stats', 'true').lower() != 'false'
        if include_stats:
            # 批量统计当前页项目的任务数据，避免 N+1 查询
            task_stats_map = {}

            if project_ids:
                stats_rows = db.session.query(
                    Task.project_id.label('project_id'),
                    func.count(Task.id).label('total_tasks'),
                    func.sum(case((Task.status.in_(pending_statuses), 1), else_=0)).label('pending_tasks'),
                    func.sum(case((Task.status == TaskStatus.DONE, 1), else_=0)).label('completed_tasks')
                ).filter(
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

        return ApiResponse.success(
            data=result,
            message="Projects retrieved successfully"
        ).to_response()

    except Exception as e:
        return ApiResponse.error(f"Failed to retrieve projects: {str(e)}", 500).to_response()
