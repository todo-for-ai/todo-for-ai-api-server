from datetime import datetime

from flask import g

from core.cache_invalidation import invalidate_user_caches
from models import ContextRule, Project, Task, db

from ..shared import sanitize_input, validate_integer


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


def get_task_by_id(arguments):
    """根据任务ID获取任务详情"""
    task_id = arguments.get('task_id')

    if not task_id:
        return {'error': 'task_id is required'}

    # 验证task_id是整数
    try:
        task_id = validate_integer(task_id, 'task_id')
    except ValueError as e:
        return {'error': str(e)}

    task = Task.query.get(task_id)
    if not task:
        return {'error': f'Task with ID {task_id} not found'}

    # 检查权限 - 只能访问自己创建的任务或自己项目中的任务
    if task.creator_id != g.current_user.id:
        # 检查是否是项目创建者
        project = Project.query.get(task.project_id)
        if not project or project.owner_id != g.current_user.id:
            return {'error': 'Access denied: You can only access your own tasks'}

    # 获取项目信息
    project = Project.query.get(task.project_id)

    task_data = task.to_dict()
    task_data['project_name'] = project.name if project else None
    task_data['project_description'] = project.description if project else None

    # 获取项目级别的上下文规则并拼接到任务内容后
    if project:
        # 获取任务创建者的用户ID
        task_user_id = task.creator_id if task.creator_id else None

        project_context = ContextRule.build_context_string(
            project_id=project.id,
            user_id=task_user_id,
            for_tasks=True,
            for_projects=False
        )

        if project_context:
            # 将项目上下文拼接到任务内容后
            original_content = task_data.get('content', '')
            task_data['content'] = f"{original_content}\n\n## 项目上下文规则\n\n{project_context}"

    return task_data


def submit_task_feedback(arguments):
    """提交任务反馈"""
    task_id = arguments.get('task_id')
    project_name = arguments.get('project_name')
    feedback_content = arguments.get('feedback_content')
    status = arguments.get('status')
    ai_identifier = arguments.get('ai_identifier', 'AI Assistant')

    if not all([task_id, project_name, feedback_content, status]):
        return {'error': 'task_id, project_name, feedback_content, and status are required'}

    # 验证和清理输入
    try:
        task_id = validate_integer(task_id, 'task_id')
    except ValueError as e:
        return {'error': str(e)}

    project_name = sanitize_input(project_name)
    feedback_content = sanitize_input(feedback_content)
    ai_identifier = sanitize_input(ai_identifier)

    # 验证状态值
    valid_statuses = ['in_progress', 'review', 'done', 'cancelled']
    if status not in valid_statuses:
        return {'error': f'Invalid status. Must be one of: {", ".join(valid_statuses)}'}

    # 验证任务存在并属于指定项目
    task = Task.query.get(task_id)
    if not task:
        return {'error': f'Task with ID {task_id} not found'}

    project = Project.query.get(task.project_id)
    if not project or project.name != project_name:
        return {'error': f'Task {task_id} does not belong to project "{project_name}"'}

    # 检查权限 - 只能修改自己创建的任务或自己项目中的任务
    if task.creator_id != g.current_user.id and project.owner_id != g.current_user.id:
        return {'error': 'Access denied: You can only modify your own tasks'}

    # 跟踪状态变更
    old_status = task.status
    status_changed = str(old_status) != str(status)

    # 更新任务
    task.feedback_content = feedback_content
    task.feedback_at = datetime.utcnow()
    task.status = status

    # 更新项目最后活动时间
    project.last_activity_at = datetime.utcnow()

    db.session.commit()
    invalidate_user_caches(g.current_user.id)

    # 记录用户活跃度
    user_id = None
    if task.creator_id:
        user_id = task.creator_id
    elif project.owner_id:
        user_id = project.owner_id

    if user_id:
        from models import UserActivity
        try:
            if status_changed:
                UserActivity.record_activity(user_id, 'task_status_changed')
                # 如果任务状态变为完成，额外记录完成任务活跃度
                if status == 'done':
                    UserActivity.record_activity(user_id, 'task_completed')
            else:
                UserActivity.record_activity(user_id, 'task_updated')
        except Exception as e:
            print(f"Warning: Failed to record user activity: {str(e)}")

    return {
        'task_id': task_id,
        'project_name': project_name,
        'status': status,
        'feedback_submitted': True,
        'feedback_content': feedback_content,
        'ai_identifier': ai_identifier,
        'timestamp': datetime.utcnow().isoformat()
    }


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
        invalidate_user_caches(g.current_user.id)

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
