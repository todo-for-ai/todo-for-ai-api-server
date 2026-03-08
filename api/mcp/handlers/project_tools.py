import time
from datetime import datetime

from flask import g

from models import ContextRule, Project, Task, TaskStatus, db

from ..shared import _get_project_stats_cache, _set_project_stats_cache, sanitize_input


def get_project_info(arguments):
    """获取项目详细信息"""
    from flask import current_app

    func_start_time = time.time()
    func_id = f"get-project-info-{int(time.time() * 1000)}-{id(arguments)}"

    current_app.logger.info(f"[GET_PROJECT_INFO_START] {func_id} Function started", extra={
        'func_id': func_id,
        'arguments': arguments,
        'user_id': g.current_user.id if hasattr(g, 'current_user') and g.current_user else None,
        'timestamp': datetime.utcnow().isoformat()
    })

    project_id = arguments.get('project_id')
    project_name = arguments.get('project_name')

    current_app.logger.debug(f"[GET_PROJECT_INFO_ARGS] {func_id} Arguments parsed", extra={
        'func_id': func_id,
        'project_id': project_id,
        'project_name': project_name,
        'has_project_id': bool(project_id),
        'has_project_name': bool(project_name)
    })

    if not project_id and not project_name:
        current_app.logger.warning(f"[GET_PROJECT_INFO_ERROR] {func_id} Missing required arguments")
        return {'error': 'Either project_id or project_name is required'}

    # 查找项目
    query_start_time = time.time()
    if project_id:
        current_app.logger.debug(f"[GET_PROJECT_INFO_QUERY] {func_id} Querying by project_id: {project_id}")
        project = Project.query.filter_by(id=project_id).first()
    else:
        project_name = sanitize_input(project_name)
        current_app.logger.debug(f"[GET_PROJECT_INFO_QUERY] {func_id} Querying by project_name: {project_name}")
        project = Project.query.filter_by(name=project_name).first()

    query_duration = time.time() - query_start_time
    current_app.logger.debug(f"[GET_PROJECT_INFO_QUERY_RESULT] {func_id} Query completed", extra={
        'func_id': func_id,
        'query_duration_ms': round(query_duration * 1000, 2),
        'project_found': bool(project),
        'project_id': project.id if project else None,
        'project_name': project.name if project else None
    })

    if not project:
        # 只返回当前用户有权限访问的项目
        user_projects_query_start = time.time()
        user_projects = Project.query.filter_by(owner_id=g.current_user.id).all()
        user_projects_query_duration = time.time() - user_projects_query_start

        identifier = f'ID {project_id}' if project_id else f'name "{project_name}"'

        current_app.logger.warning(f"[GET_PROJECT_INFO_NOT_FOUND] {func_id} Project not found", extra={
            'func_id': func_id,
            'identifier': identifier,
            'user_projects_count': len(user_projects),
            'user_projects_query_duration_ms': round(user_projects_query_duration * 1000, 2),
            'available_projects': [{'id': p.id, 'name': p.name} for p in user_projects]
        })

        return {
            'error': f'Project with {identifier} not found',
            'available_projects': [{'id': p.id, 'name': p.name} for p in user_projects]
        }

    # 检查权限 - 只能访问自己创建的项目
    if project.owner_id != g.current_user.id:
        current_app.logger.warning(f"[GET_PROJECT_INFO_ACCESS_DENIED] {func_id} Access denied", extra={
            'func_id': func_id,
            'project_id': project.id,
            'project_name': project.name,
            'project_owner_id': project.owner_id,
            'current_user_id': g.current_user.id
        })
        return {'error': 'Access denied: You can only access your own projects'}

    try:
        # 获取项目统计信息
        stats_start_time = time.time()
        current_app.logger.debug(f"[GET_PROJECT_INFO_STATS] {func_id} Starting statistics queries")

        from sqlalchemy import case, func
        stats_row = db.session.query(
            func.count(Task.id).label('total_tasks'),
            func.sum(case((Task.status == TaskStatus.TODO, 1), else_=0)).label('todo_tasks'),
            func.sum(case((Task.status == TaskStatus.IN_PROGRESS, 1), else_=0)).label('in_progress_tasks'),
            func.sum(case((Task.status == TaskStatus.REVIEW, 1), else_=0)).label('review_tasks'),
            func.sum(case((Task.status == TaskStatus.DONE, 1), else_=0)).label('done_tasks'),
            func.sum(case((Task.status == TaskStatus.CANCELLED, 1), else_=0)).label('cancelled_tasks')
        ).filter(
            Task.project_id == project.id,
            Task.owner_id == g.current_user.id
        ).first()

        total_tasks = int((stats_row.total_tasks if stats_row else 0) or 0)
        todo_tasks = int((stats_row.todo_tasks if stats_row else 0) or 0)
        in_progress_tasks = int((stats_row.in_progress_tasks if stats_row else 0) or 0)
        review_tasks = int((stats_row.review_tasks if stats_row else 0) or 0)
        done_tasks = int((stats_row.done_tasks if stats_row else 0) or 0)
        cancelled_tasks = int((stats_row.cancelled_tasks if stats_row else 0) or 0)

        stats_duration = time.time() - stats_start_time
        current_app.logger.debug(f"[GET_PROJECT_INFO_STATS_RESULT] {func_id} Statistics queries completed", extra={
            'func_id': func_id,
            'stats_duration_ms': round(stats_duration * 1000, 2),
            'total_tasks': total_tasks,
            'todo_tasks': todo_tasks,
            'in_progress_tasks': in_progress_tasks,
            'review_tasks': review_tasks,
            'done_tasks': done_tasks,
            'cancelled_tasks': cancelled_tasks
        })

        # 获取最近的任务
        recent_tasks_start_time = time.time()
        current_app.logger.debug(f"[GET_PROJECT_INFO_RECENT] {func_id} Querying recent tasks")

        recent_tasks = Task.query.filter_by(project_id=project.id)\
                          .order_by(Task.updated_at.desc())\
                          .limit(5)\
                          .all()

        recent_tasks_duration = time.time() - recent_tasks_start_time
        current_app.logger.debug(f"[GET_PROJECT_INFO_RECENT_RESULT] {func_id} Recent tasks query completed", extra={
            'func_id': func_id,
            'recent_tasks_duration_ms': round(recent_tasks_duration * 1000, 2),
            'recent_tasks_count': len(recent_tasks)
        })

        recent_tasks_data = []
        for task in recent_tasks:
            recent_tasks_data.append({
                'id': task.id,
                'title': task.title,
                'status': task.status.value if hasattr(task.status, 'value') else task.status,
                'priority': task.priority.value if hasattr(task.priority, 'value') else task.priority,
                'updated_at': task.updated_at.isoformat()
            })

        completion_rate = round((done_tasks / total_tasks * 100) if total_tasks > 0 else 0, 2)

        result = {
            'id': project.id,
            'name': project.name,
            'description': project.description,
            'status': project.status.value if hasattr(project.status, 'value') else getattr(project, 'status', 'active'),
            'github_url': project.github_url,
            'project_context': project.project_context,
            'owner_id': project.owner_id,
            'created_at': project.created_at.isoformat(),
            'updated_at': project.updated_at.isoformat(),
            'total_tasks': total_tasks,
            'pending_tasks': todo_tasks + in_progress_tasks + review_tasks,
            'completed_tasks': done_tasks,
            'completion_rate': completion_rate,
            'statistics': {
                'total_tasks': total_tasks,
                'todo_tasks': todo_tasks,
                'in_progress_tasks': in_progress_tasks,
                'review_tasks': review_tasks,
                'done_tasks': done_tasks,
                'cancelled_tasks': cancelled_tasks,
                'completion_rate': completion_rate
            },
            'recent_tasks': recent_tasks_data
        }

        func_duration = time.time() - func_start_time
        current_app.logger.info(f"[GET_PROJECT_INFO_SUCCESS] {func_id} Function completed successfully", extra={
            'func_id': func_id,
            'project_id': project.id,
            'project_name': project.name,
            'func_duration_ms': round(func_duration * 1000, 2),
            'result_size': len(str(result)),
            'total_tasks': total_tasks,
            'completion_rate': completion_rate
        })

        return result

    except Exception as e:
        func_duration = time.time() - func_start_time
        current_app.logger.error(f"[GET_PROJECT_INFO_EXCEPTION] {func_id} Exception occurred", extra={
            'func_id': func_id,
            'project_id': project.id if 'project' in locals() and project else None,
            'func_duration_ms': round(func_duration * 1000, 2),
            'exception_type': type(e).__name__,
            'exception_message': str(e)
        }, exc_info=True)
        return {'error': f'Failed to get project info: {str(e)}'}


def list_user_projects(arguments):
    """列出用户有权限访问的所有项目"""
    from flask import current_app

    func_start_time = time.time()
    func_id = f"list-user-projects-{int(time.time() * 1000)}-{id(arguments)}"

    current_app.logger.info(f"[LIST_USER_PROJECTS_START] {func_id} Starting to list user projects", extra={
        'func_id': func_id,
        'user_id': g.current_user.id if hasattr(g, 'current_user') and g.current_user else None,
        'arguments': arguments,
        'timestamp': datetime.utcnow().isoformat()
    })

    try:
        # 获取参数
        status_filter = arguments.get('status_filter', 'active')
        include_stats = arguments.get('include_stats', False)

        current_app.logger.debug(f"[LIST_USER_PROJECTS_PARAMS] {func_id} Parameters parsed", extra={
            'func_id': func_id,
            'status_filter': status_filter,
            'include_stats': include_stats,
            'user_id': g.current_user.id
        })

        # 构建查询 - 只返回当前用户拥有的项目
        query_start_time = time.time()
        query = Project.query.filter_by(owner_id=g.current_user.id)

        # 根据状态筛选
        if status_filter == 'active':
            from models.project import ProjectStatus
            query = query.filter_by(status=ProjectStatus.ACTIVE)
        elif status_filter == 'archived':
            from models.project import ProjectStatus
            query = query.filter_by(status=ProjectStatus.ARCHIVED)
        elif status_filter == 'all':
            # 不过滤状态，但排除已删除的项目
            from models.project import ProjectStatus
            query = query.filter(Project.status != ProjectStatus.DELETED)

        # 按最后活动时间排序，如果没有则按创建时间排序
        # MySQL不支持NULLS LAST，使用COALESCE处理NULL值
        from sqlalchemy import func
        projects = query.order_by(
            func.coalesce(Project.last_activity_at, Project.created_at).desc(),
            Project.created_at.desc()
        ).all()

        query_duration = time.time() - query_start_time

        current_app.logger.debug(f"[LIST_USER_PROJECTS_QUERY] {func_id} Projects query completed", extra={
            'func_id': func_id,
            'projects_count': len(projects),
            'query_duration_ms': round(query_duration * 1000, 2),
            'status_filter': status_filter
        })

        task_stats_map = {}
        context_rules_map = {}
        if include_stats and projects:
            from sqlalchemy import case, func
            project_ids = [project.id for project in projects]
            cache_key = f"user:{g.current_user.id}"
            now = time.time()
            cache_item, cache_source = _get_project_stats_cache(cache_key)
            cache_hit = cache_item is not None

            if cache_hit:
                task_stats_map = cache_item.get('task_stats_map', {})
                context_rules_map = cache_item.get('context_rules_map', {})
                current_app.logger.debug(
                    f"[LIST_USER_PROJECTS_CACHE_HIT] {func_id} Using cached project stats",
                    extra={
                        'func_id': func_id,
                        'cache_source': cache_source,
                        'cache_key': cache_key,
                        'cache_age_ms': round((now - cache_item['cached_at']) * 1000, 2),
                        'task_stats_rows': len(task_stats_map),
                        'context_rows': len(context_rules_map),
                    }
                )
            else:
                task_agg_start = time.time()
                task_stats_rows = db.session.query(
                    Task.project_id.label('project_id'),
                    func.count(Task.id).label('total_tasks'),
                    func.sum(case((Task.status == TaskStatus.TODO, 1), else_=0)).label('todo_tasks'),
                    func.sum(case((Task.status == TaskStatus.IN_PROGRESS, 1), else_=0)).label('in_progress_tasks'),
                    func.sum(case((Task.status == TaskStatus.REVIEW, 1), else_=0)).label('review_tasks'),
                    func.sum(case((Task.status == TaskStatus.DONE, 1), else_=0)).label('done_tasks'),
                    func.sum(case((Task.status == TaskStatus.CANCELLED, 1), else_=0)).label('cancelled_tasks')
                ).filter(
                    Task.owner_id == g.current_user.id
                ).group_by(
                    Task.project_id
                ).all()
                task_agg_duration = time.time() - task_agg_start

                task_stats_map = {
                    row.project_id: {
                        'total_tasks': int(row.total_tasks or 0),
                        'todo_tasks': int(row.todo_tasks or 0),
                        'in_progress_tasks': int(row.in_progress_tasks or 0),
                        'review_tasks': int(row.review_tasks or 0),
                        'done_tasks': int(row.done_tasks or 0),
                        'cancelled_tasks': int(row.cancelled_tasks or 0),
                    }
                    for row in task_stats_rows
                }

                context_agg_start = time.time()
                context_rows = db.session.query(
                    ContextRule.project_id.label('project_id'),
                    func.count(ContextRule.id).label('context_rules_count')
                ).filter(
                    ContextRule.user_id == g.current_user.id,
                    ContextRule.is_active.is_(True)
                ).group_by(
                    ContextRule.project_id
                ).all()
                context_agg_duration = time.time() - context_agg_start

                context_rules_map = {
                    row.project_id: int(row.context_rules_count or 0)
                    for row in context_rows
                }

                _set_project_stats_cache(cache_key, task_stats_map, context_rules_map)

                current_app.logger.debug(
                    f"[LIST_USER_PROJECTS_AGG] {func_id} Aggregation queries completed",
                    extra={
                        'func_id': func_id,
                        'task_agg_duration_ms': round(task_agg_duration * 1000, 2),
                        'context_agg_duration_ms': round(context_agg_duration * 1000, 2),
                        'task_stats_rows': len(task_stats_rows),
                        'context_rows': len(context_rows),
                        'cache_key': cache_key,
                    }
                )

        # 构建返回数据
        projects_data = []
        for project in projects:
            project_dict = {
                'id': project.id,
                'name': project.name,
                'description': project.description,
                'color': project.color,
                'status': project.status.value if hasattr(project.status, 'value') else getattr(project, 'status', 'active'),
                'github_url': project.github_url,
                'local_url': project.local_url,
                'production_url': project.production_url,
                'project_context': project.project_context,
                'owner_id': project.owner_id,
                'created_at': project.created_at.isoformat(),
                'updated_at': project.updated_at.isoformat(),
                'last_activity_at': project.last_activity_at.isoformat() if project.last_activity_at else None
            }

            # 如果需要包含统计信息
            if include_stats:
                stats = task_stats_map.get(project.id, {
                    'total_tasks': 0,
                    'todo_tasks': 0,
                    'in_progress_tasks': 0,
                    'review_tasks': 0,
                    'done_tasks': 0,
                    'cancelled_tasks': 0
                })
                total_tasks = stats['total_tasks']
                todo_tasks = stats['todo_tasks']
                in_progress_tasks = stats['in_progress_tasks']
                review_tasks = stats['review_tasks']
                done_tasks = stats['done_tasks']
                cancelled_tasks = stats['cancelled_tasks']

                # 计算完成率
                completion_rate = (done_tasks / total_tasks * 100) if total_tasks > 0 else 0

                # 获取上下文规则数量
                context_rules_count = context_rules_map.get(project.id, 0)

                project_dict.update({
                    'total_tasks': total_tasks,
                    'pending_tasks': todo_tasks + in_progress_tasks + review_tasks,
                    'completed_tasks': done_tasks,
                    'completion_rate': round(completion_rate, 2),
                    'context_rules_count': context_rules_count,
                    'statistics': {
                        'total_tasks': total_tasks,
                        'todo_tasks': todo_tasks,
                        'in_progress_tasks': in_progress_tasks,
                        'review_tasks': review_tasks,
                        'done_tasks': done_tasks,
                        'cancelled_tasks': cancelled_tasks,
                        'completion_rate': round(completion_rate, 2),
                        'context_rules_count': context_rules_count
                    }
                })

            projects_data.append(project_dict)

        func_duration = time.time() - func_start_time

        result = {
            'projects': projects_data,
            'total': len(projects_data),
            'status_filter': status_filter,
            'include_stats': include_stats,
            'user_id': g.current_user.id
        }

        current_app.logger.info(f"[LIST_USER_PROJECTS_SUCCESS] {func_id} Successfully listed user projects", extra={
            'func_id': func_id,
            'projects_count': len(projects_data),
            'status_filter': status_filter,
            'include_stats': include_stats,
            'func_duration_ms': round(func_duration * 1000, 2),
            'user_id': g.current_user.id
        })

        return result

    except Exception as e:
        func_duration = time.time() - func_start_time
        current_app.logger.error(f"[LIST_USER_PROJECTS_EXCEPTION] {func_id} Exception occurred", extra={
            'func_id': func_id,
            'user_id': g.current_user.id if hasattr(g, 'current_user') and g.current_user else None,
            'func_duration_ms': round(func_duration * 1000, 2),
            'exception_type': type(e).__name__,
            'exception_message': str(e)
        }, exc_info=True)
        return {'error': f'Failed to list user projects: {str(e)}'}
