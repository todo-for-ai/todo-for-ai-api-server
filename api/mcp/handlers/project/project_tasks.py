def get_project_tasks_by_name(arguments):
    """根据项目名称获取任务列表"""
    project_name = arguments.get('project_name')
    status_filter = arguments.get('status_filter', ['todo', 'in_progress', 'review'])

    if not project_name:
        return {'error': 'project_name is required'}

    # 清理输入
    project_name = sanitize_input(project_name)

    # 查找项目
    project = Project.query.filter_by(name=project_name).first()
    if not project:
        # 只返回当前用户有权限访问的项目
        user_projects = Project.query.filter_by(owner_id=g.current_user.id).all()
        return {
            'error': f'Project "{project_name}" not found',
            'available_projects': [p.name for p in user_projects]
        }

    # 检查权限 - 只能访问自己创建的项目
    if project.owner_id != g.current_user.id:
        return {'error': 'Access denied: You can only access your own projects'}

    # 获取任务
    query = Task.query.filter_by(project_id=project.id)
    if status_filter:
        query = query.filter(Task.status.in_(status_filter))

    tasks = query.order_by(Task.created_at.asc()).all()

    tasks_data = []
    for task in tasks:
        task_dict = task.to_dict()
        task_dict['project_name'] = project.name
        tasks_data.append(task_dict)

    return {
        'project_name': project.name,
        'project_id': project.id,
        'status_filter': status_filter,
        'total_tasks': len(tasks_data),
        'tasks': tasks_data
    }
