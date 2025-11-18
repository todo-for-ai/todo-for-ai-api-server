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

        total_tasks = Task.query.filter_by(project_id=project.id).count()
        todo_tasks = Task.query.filter_by(project_id=project.id, status='todo').count()
        in_progress_tasks = Task.query.filter_by(project_id=project.id, status='in_progress').count()
        review_tasks = Task.query.filter_by(project_id=project.id, status='review').count()
        done_tasks = Task.query.filter_by(project_id=project.id, status='done').count()
        cancelled_tasks = Task.query.filter_by(project_id=project.id, status='cancelled').count()

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
