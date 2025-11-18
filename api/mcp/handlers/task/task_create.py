def create_task(arguments):
    """创建新任务"""
    project_id = arguments.get('project_id')
    title = arguments.get('title')
    content = arguments.get('content', '')
    status = arguments.get('status', 'todo')
    priority = arguments.get('priority', 'medium')
    assignee = arguments.get('assignee')
    due_date = arguments.get('due_date')
    tags = arguments.get('tags', [])
    related_files = arguments.get('related_files', [])
    is_ai_task = arguments.get('is_ai_task', True)
    creator_identifier = arguments.get('ai_identifier', 'MCP Client')

    if not project_id:
        return {'error': 'project_id is required'}

    if not title:
        return {'error': 'title is required'}

    # 清理输入
    title = sanitize_input(title)
    content = sanitize_input(content) if content else ''
    assignee = sanitize_input(assignee) if assignee else None
    creator_identifier = sanitize_input(creator_identifier) if creator_identifier else 'MCP Client'

    # 验证项目存在且用户有权限
    project = Project.query.filter_by(id=project_id).first()
    if not project:
        return {'error': f'Project with ID {project_id} not found'}

    # 检查权限 - 只能在自己创建的项目中创建任务
    if project.owner_id != g.current_user.id:
        return {'error': 'Access denied: You can only create tasks in your own projects'}

    # 验证状态值
    valid_statuses = ['todo', 'in_progress', 'review', 'done', 'cancelled']
    if status not in valid_statuses:
        return {'error': f'Invalid status. Must be one of: {", ".join(valid_statuses)}'}

    # 验证优先级值
    valid_priorities = ['low', 'medium', 'high', 'urgent']
    if priority not in valid_priorities:
        return {'error': f'Invalid priority. Must be one of: {", ".join(valid_priorities)}'}

    # 解析due_date
    due_date_obj = None
    if due_date:
        try:
            due_date_obj = datetime.strptime(due_date, '%Y-%m-%d').date()
        except ValueError:
            return {'error': 'Invalid due_date format. Use YYYY-MM-DD'}

    try:
        # 创建任务
        task = Task(
            title=title,
            content=content,
            status=status,
            priority=priority,
            project_id=project_id,
            creator_id=g.current_user.id,
            assignee=assignee,
            due_date=due_date_obj,
            is_ai_task=is_ai_task,
            creator_identifier=creator_identifier,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
        )

        db.session.add(task)
        db.session.commit()

        # 注意：标签和相关文件功能暂时不支持，因为相关模型尚未实现
        # 这些参数会被保存在返回结果中，但不会存储到数据库

        # 记录用户活跃度
        from models import UserActivity
        try:
            UserActivity.record_activity(g.current_user.id, 'task_created')
        except Exception as e:
            print(f"Warning: Failed to record user activity: {str(e)}")

        # 返回创建的任务信息
        return {
            'id': task.id,
            'title': task.title,
            'content': task.content,
            'status': task.status.value if hasattr(task.status, 'value') else task.status,
            'priority': task.priority.value if hasattr(task.priority, 'value') else task.priority,
            'project_id': task.project_id,
            'project_name': project.name,
            'creator_id': task.creator_id,
            'assignee': task.assignee,
            'due_date': task.due_date.isoformat() if task.due_date else None,
            'is_ai_task': task.is_ai_task,
            'creator_identifier': task.creator_identifier,
            'created_at': task.created_at.isoformat(),
            'updated_at': task.updated_at.isoformat(),
            'tags': tags,
            'related_files': related_files
        }

    except Exception as e:
        db.session.rollback()
        return {'error': f'Failed to create task: {str(e)}'}
