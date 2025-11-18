def wait_for_new_tasks(arguments):
    """等待项目中的新任务"""
    import time
    from flask import current_app

    func_start_time = time.time()
    func_id = f"wait-for-new-tasks-{int(time.time() * 1000)}-{id(arguments)}"

    current_app.logger.info(f"[WAIT_FOR_NEW_TASKS_START] {func_id} Function started", extra={
        'func_id': func_id,
        'arguments': arguments,
        'user_id': g.current_user.id if hasattr(g, 'current_user') and g.current_user else None,
        'timestamp': datetime.utcnow().isoformat()
    })

    project_name = arguments.get('project_name')
    timeout_seconds = arguments.get('timeout_seconds', 3600)  # Default 1 hour
    poll_interval_seconds = arguments.get('poll_interval_seconds', 30)  # Default 30 seconds

    current_app.logger.debug(f"[WAIT_FOR_NEW_TASKS_ARGS] {func_id} Arguments parsed", extra={
        'func_id': func_id,
        'project_name': project_name,
        'timeout_seconds': timeout_seconds,
        'poll_interval_seconds': poll_interval_seconds,
        'has_project_name': bool(project_name)
    })

    if not project_name:
        current_app.logger.warning(f"[WAIT_FOR_NEW_TASKS_ERROR] {func_id} Missing required arguments")
        return {'error': 'project_name is required'}

    # 验证和清理输入
    project_name = sanitize_input(project_name)

    # 验证超时时间和轮询间隔
    try:
        timeout_seconds = max(30, min(7200, int(timeout_seconds)))  # 30秒到2小时
        poll_interval_seconds = max(10, min(300, int(poll_interval_seconds)))  # 10秒到5分钟
    except (ValueError, TypeError):
        return {'error': 'timeout_seconds and poll_interval_seconds must be valid numbers'}

    # 查找项目
    query_start_time = time.time()
    project = Project.query.filter_by(name=project_name).first()
    query_duration = time.time() - query_start_time

    current_app.logger.debug(f"[WAIT_FOR_NEW_TASKS_QUERY] {func_id} Project query completed", extra={
        'func_id': func_id,
        'query_duration_ms': round(query_duration * 1000, 2),
        'project_found': bool(project),
        'project_id': project.id if project else None,
        'project_name': project.name if project else None
    })

    if not project:
        # 只返回当前用户有权限访问的项目
        user_projects = Project.query.filter_by(owner_id=g.current_user.id).all()
        return {
            'error': f'Project "{project_name}" not found',
            'available_projects': [p.name for p in user_projects]
        }

    # 检查权限 - 只能访问自己创建的项目
    if project.owner_id != g.current_user.id:
        current_app.logger.warning(f"[WAIT_FOR_NEW_TASKS_ACCESS_DENIED] {func_id} Access denied", extra={
            'func_id': func_id,
            'project_id': project.id,
            'project_name': project.name,
            'project_owner_id': project.owner_id,
            'current_user_id': g.current_user.id
        })
        return {'error': 'Access denied: You can only access your own projects'}

    # 记录开始时间戳，用于检测新任务
    start_timestamp = datetime.utcnow()
    end_time = time.time() + timeout_seconds
    poll_count = 0

    current_app.logger.info(f"[WAIT_FOR_NEW_TASKS_POLLING_START] {func_id} Starting polling loop", extra={
        'func_id': func_id,
        'project_id': project.id,
        'project_name': project.name,
        'start_timestamp': start_timestamp.isoformat(),
        'timeout_seconds': timeout_seconds,
        'poll_interval_seconds': poll_interval_seconds,
        'end_time': datetime.fromtimestamp(end_time).isoformat()
    })

    try:
        while time.time() < end_time:
            poll_count += 1
            poll_start_time = time.time()

            current_app.logger.debug(f"[WAIT_FOR_NEW_TASKS_POLL] {func_id} Poll #{poll_count} started", extra={
                'func_id': func_id,
                'poll_count': poll_count,
                'remaining_time': end_time - time.time(),
                'timestamp': datetime.utcnow().isoformat()
            })

            # 查询在开始时间之后创建的待执行任务
            new_tasks_query = Task.query.filter(
                Task.project_id == project.id,
                Task.created_at > start_timestamp,
                Task.status.in_(['todo', 'in_progress', 'review'])
            ).order_by(Task.created_at.asc())

            new_tasks = new_tasks_query.all()
            poll_duration = time.time() - poll_start_time

            current_app.logger.debug(f"[WAIT_FOR_NEW_TASKS_POLL_RESULT] {func_id} Poll #{poll_count} completed", extra={
                'func_id': func_id,
                'poll_count': poll_count,
                'poll_duration_ms': round(poll_duration * 1000, 2),
                'new_tasks_count': len(new_tasks),
                'new_task_ids': [task.id for task in new_tasks] if new_tasks else []
            })

            if new_tasks:
                # 找到新任务，准备返回结果
                tasks_data = []
                for task in new_tasks:
                    task_dict = task.to_dict()
                    task_dict['project_name'] = project.name
                    tasks_data.append(task_dict)

                func_duration = time.time() - func_start_time

                current_app.logger.info(f"[WAIT_FOR_NEW_TASKS_SUCCESS] {func_id} Found new tasks", extra={
                    'func_id': func_id,
                    'project_id': project.id,
                    'project_name': project.name,
                    'new_tasks_count': len(new_tasks),
                    'poll_count': poll_count,
                    'func_duration_ms': round(func_duration * 1000, 2),
                    'task_ids': [task.id for task in new_tasks]
                })

                return {
                    'project_name': project.name,
                    'project_id': project.id,
                    'new_tasks': tasks_data,
                    'total_new_tasks': len(tasks_data),
                    'poll_count': poll_count,
                    'wait_duration_seconds': round(func_duration, 2),
                    'timeout': False,
                    'start_timestamp': start_timestamp.isoformat(),
                    'found_timestamp': datetime.utcnow().isoformat()
                }

            # 没有找到新任务，检查是否还有时间继续轮询
            remaining_time = end_time - time.time()
            if remaining_time <= 0:
                break

            # 等待下一次轮询，但不超过剩余时间
            sleep_time = min(poll_interval_seconds, remaining_time)
            if sleep_time > 0:
                current_app.logger.debug(f"[WAIT_FOR_NEW_TASKS_SLEEP] {func_id} Sleeping before next poll", extra={
                    'func_id': func_id,
                    'poll_count': poll_count,
                    'sleep_time': sleep_time,
                    'remaining_time': remaining_time
                })
                time.sleep(sleep_time)

        # 超时，没有找到新任务
        func_duration = time.time() - func_start_time

        current_app.logger.info(f"[WAIT_FOR_NEW_TASKS_TIMEOUT] {func_id} Wait timed out", extra={
            'func_id': func_id,
            'project_id': project.id,
            'project_name': project.name,
            'poll_count': poll_count,
            'func_duration_ms': round(func_duration * 1000, 2),
            'timeout_seconds': timeout_seconds
        })

        return {
            'project_name': project.name,
            'project_id': project.id,
            'new_tasks': [],
            'total_new_tasks': 0,
            'poll_count': poll_count,
            'wait_duration_seconds': round(func_duration, 2),
            'timeout': True,
            'timeout_seconds': timeout_seconds,
            'start_timestamp': start_timestamp.isoformat(),
            'end_timestamp': datetime.utcnow().isoformat()
        }

    except Exception as e:
        func_duration = time.time() - func_start_time
        current_app.logger.error(f"[WAIT_FOR_NEW_TASKS_EXCEPTION] {func_id} Exception occurred", extra={
            'func_id': func_id,
            'project_id': project.id if 'project' in locals() and project else None,
            'func_duration_ms': round(func_duration * 1000, 2),
            'poll_count': poll_count,
            'exception_type': type(e).__name__,
            'exception_message': str(e)
        }, exc_info=True)
        return {'error': f'Failed to wait for new tasks: {str(e)}'}
