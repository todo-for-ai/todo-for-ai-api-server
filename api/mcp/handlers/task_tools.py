from datetime import datetime

from flask import g
from sqlalchemy import or_

from core.cache_invalidation import invalidate_user_caches
from models import (
    AgentTaskEvent,
    ContextRule,
    Project,
    Task,
    TaskEvidenceRecord,
    TaskLog,
    TaskLogActorType,
    TaskStatus,
    db,
)

from ...agent_common import generate_id, now_utc
from ..shared import sanitize_input, validate_integer

VALID_STATUSES = ['todo', 'in_progress', 'review', 'done', 'cancelled']


def _status_members(status_values):
    """状态字符串（value）→ TaskStatus 成员。Enum 列按 name 落库，直接用 value 字符串过滤会命中 0 行。"""
    return [TaskStatus(value) for value in status_values]


def _accessible_tasks_scope(user_id):
    """任务可见范围（与既有 MCP 权限模型一致）：
    自己创建的 / 自己拥有的 / 自己项目里的 / assignees 指派给自己的。
    assignees 是 JSON 列，先用 LIKE 粗筛（MySQL/SQLite 通用），再在 Python 侧精确校验。
    """
    return or_(
        Task.creator_id == user_id,
        Task.owner_id == user_id,
        Task.project_id.in_(db.session.query(Project.id).filter_by(owner_id=user_id)),
        Task.assignees.like(f'%"id": {user_id}%'),
    )


def _assignee_matches_user(assignees, user_id):
    for item in assignees or []:
        if not isinstance(item, dict):
            continue
        if str(item.get('type') or '').lower() == 'human':
            try:
                if int(item.get('id')) == int(user_id):
                    return True
            except (TypeError, ValueError):
                continue
    return False


def _task_status_value(task):
    return task.status.value if hasattr(task.status, 'value') else task.status


def list_my_tasks(arguments):
    """列出与当前 token 用户相关的任务（外部 Agent 发现工作的入口）"""
    status_filter = arguments.get('status_filter') or ['todo', 'in_progress', 'review']
    project_id = arguments.get('project_id')
    limit = arguments.get('limit', 50)

    try:
        limit = min(max(int(limit or 50), 1), 200)
    except (TypeError, ValueError):
        return {'error': 'limit must be an integer'}

    if project_id is not None:
        try:
            project_id = validate_integer(project_id, 'project_id')
        except ValueError as e:
            return {'error': str(e)}

    valid_statuses = VALID_STATUSES
    if not isinstance(status_filter, list):
        return {'error': 'status_filter must be an array of status strings'}
    for status in status_filter:
        if status not in valid_statuses:
            return {'error': f'Invalid status in status_filter: {status}'}

    query = Task.query.filter(_accessible_tasks_scope(g.current_user.id))
    if project_id is not None:
        query = query.filter(Task.project_id == project_id)
    if status_filter:
        query = query.filter(Task.status.in_(_status_members(status_filter)))

    rows = query.order_by(Task.updated_at.desc()).limit(limit * 2).all()

    tasks_data = []
    for task in rows:
        # LIKE 粗筛可能命中 agent 的同号 id，这里精确校验一次
        if task.creator_id != g.current_user.id \
                and task.owner_id != g.current_user.id \
                and not _assignee_matches_user(task.assignees, g.current_user.id):
            project_owner = db.session.query(Project.owner_id).filter_by(id=task.project_id).scalar()
            if project_owner != g.current_user.id:
                continue
        task_dict = task.to_dict()
        project = Project.query.get(task.project_id)
        task_dict['project_name'] = project.name if project else None
        tasks_data.append(task_dict)
        if len(tasks_data) >= limit:
            break

    return {
        'total_tasks': len(tasks_data),
        'status_filter': status_filter,
        'tasks': tasks_data,
        'hint': 'Pick a task and set it in_progress with update_task_status; report_progress as you work; mark review/done when finished',
    }


def search_tasks(arguments):
    """在可访问范围内按关键词搜索任务"""
    keyword = arguments.get('keyword')
    if not keyword or not str(keyword).strip():
        return {'error': 'keyword is required'}
    keyword = sanitize_input(str(keyword).strip())

    project_id = arguments.get('project_id')
    status = arguments.get('status')
    limit = arguments.get('limit', 50)

    try:
        limit = min(max(int(limit or 50), 1), 200)
    except (TypeError, ValueError):
        return {'error': 'limit must be an integer'}

    valid_statuses = VALID_STATUSES
    if status is not None and status not in valid_statuses:
        return {'error': f'Invalid status: {status}'}

    query = Task.query.filter(_accessible_tasks_scope(g.current_user.id)).filter(
        Task.title.like(f'%{keyword}%') | Task.content.like(f'%{keyword}%')
    )
    if project_id is not None:
        try:
            project_id = validate_integer(project_id, 'project_id')
        except ValueError as e:
            return {'error': str(e)}
        query = query.filter(Task.project_id == project_id)
    if status:
        query = query.filter(Task.status == TaskStatus(status))

    rows = query.order_by(Task.updated_at.desc()).limit(limit * 2).all()

    tasks_data = []
    for task in rows:
        if task.creator_id != g.current_user.id \
                and task.owner_id != g.current_user.id \
                and not _assignee_matches_user(task.assignees, g.current_user.id):
            project_owner = db.session.query(Project.owner_id).filter_by(id=task.project_id).scalar()
            if project_owner != g.current_user.id:
                continue
        task_dict = task.to_dict()
        project = Project.query.get(task.project_id)
        task_dict['project_name'] = project.name if project else None
        tasks_data.append(task_dict)
        if len(tasks_data) >= limit:
            break

    return {
        'keyword': keyword,
        'total_tasks': len(tasks_data),
        'tasks': tasks_data,
    }


def update_task_status(arguments):
    """更新任务状态，支持 expected_revision 乐观锁"""
    task_id = arguments.get('task_id')
    status = arguments.get('status')
    expected_revision = arguments.get('expected_revision')

    if not task_id:
        return {'error': 'task_id is required'}
    try:
        task_id = validate_integer(task_id, 'task_id')
    except ValueError as e:
        return {'error': str(e)}

    valid_statuses = VALID_STATUSES
    if status not in valid_statuses:
        return {'error': f'Invalid status. Must be one of: {", ".join(valid_statuses)}'}

    task = Task.query.get(task_id)
    if not task:
        return {'error': f'Task with ID {task_id} not found'}

    access_error = _task_access_error(task)
    if access_error:
        return access_error

    if expected_revision is not None:
        try:
            expected_revision = int(expected_revision)
        except (TypeError, ValueError):
            return {'error': 'expected_revision must be an integer'}
        if int(task.revision or 1) != expected_revision:
            return {
                'error': f'Revision conflict: task is at revision {task.revision}, expected {expected_revision}',
                'conflict': True,
                'current_revision': task.revision,
            }

    old_status = _task_status_value(task)
    task.status = TaskStatus(status)
    task.revision = int(task.revision or 1) + 1

    project = Project.query.get(task.project_id)
    if project:
        project.last_activity_at = datetime.utcnow()

    db.session.commit()
    invalidate_user_caches(g.current_user.id)

    from models import UserActivity
    try:
        UserActivity.record_activity(g.current_user.id, 'task_status_changed')
        if status == 'done':
            UserActivity.record_activity(g.current_user.id, 'task_completed')
    except Exception as e:
        print(f"Warning: Failed to record user activity: {str(e)}")

    result = {
        'task_id': task.id,
        'title': task.title,
        'old_status': old_status,
        'status': _task_status_value(task),
        'revision': task.revision,
        'updated': True,
    }

    # 软性 DoD 提醒：声明了验收标准但没有对应类型的通过证据时，提示而非阻断
    if status == 'done' and task.dod:
        evidence = TaskEvidenceRecord.query.filter_by(task_id=task.id).all()
        passed_types = {ev.evidence_type for ev in evidence if ev.status == 'passed'}
        missing = [
            f"{item.get('type')}:{item.get('value', '')}"
            for item in task.dod
            if item.get('type') not in passed_types
        ]
        if missing:
            result['dod_warning'] = 'Task has DoD criteria without passing evidence: ' + '; '.join(missing)
            result['hint'] = 'Submit evidence via the runtime commit protocol or use get_task_evidence to review current evidence'

    return result


def report_progress(arguments):
    """向任务追加一条进度日志（append-only）"""
    task_id = arguments.get('task_id')
    content = arguments.get('content')
    content_type = arguments.get('content_type', 'text/markdown')

    if not task_id:
        return {'error': 'task_id is required'}
    try:
        task_id = validate_integer(task_id, 'task_id')
    except ValueError as e:
        return {'error': str(e)}

    if not content or not str(content).strip():
        return {'error': 'content is required'}
    content = sanitize_input(str(content).strip())

    task = Task.query.get(task_id)
    if not task:
        return {'error': f'Task with ID {task_id} not found'}

    access_error = _task_access_error(task)
    if access_error:
        return access_error

    row = TaskLog(
        task_id=task.id,
        actor_type=TaskLogActorType.AGENT,
        actor_user_id=g.current_user.id,
        content=content,
        content_type=(content_type or 'text/markdown')[:32],
        created_by=f'mcp:{g.current_user.username}',
    )
    db.session.add(row)
    db.session.commit()

    return {
        'task_id': task.id,
        'log_id': row.id,
        'content': row.content,
        'reported': True,
        'timestamp': row.created_at.isoformat() if row.created_at else None,
    }


def request_approval(arguments):
    """请求人类决策：写入审批队列（interaction_request），owner/admin 可批准或拒绝"""
    task_id = arguments.get('task_id')
    question = arguments.get('question')
    interaction_type = arguments.get('interaction_type', 'human_approval')
    sensitivity_level = arguments.get('sensitivity_level', 'medium')
    options = arguments.get('options')

    if not task_id:
        return {'error': 'task_id is required'}
    try:
        task_id = validate_integer(task_id, 'task_id')
    except ValueError as e:
        return {'error': str(e)}

    if not question or not str(question).strip():
        return {'error': 'question is required'}
    question = sanitize_input(str(question).strip())

    interaction_type = sanitize_input(str(interaction_type or 'human_approval')) or 'human_approval'
    if sensitivity_level not in ('low', 'medium', 'high', 'critical'):
        return {'error': 'sensitivity_level must be one of: low, medium, high, critical'}
    if options is not None:
        if not isinstance(options, list) or not all(isinstance(opt, str) for opt in options):
            return {'error': 'options must be an array of strings'}
        options = [str(opt)[:200] for opt in options][:10]

    task = Task.query.get(task_id)
    if not task:
        return {'error': f'Task with ID {task_id} not found'}

    access_error = _task_access_error(task)
    if access_error:
        return access_error

    project = Project.query.get(task.project_id)
    workspace_id = project.organization_id if project else None
    if not workspace_id:
        return {'error': 'Task project is not attached to a workspace; approval queue unavailable'}

    user = g.current_user
    interaction_id = generate_id('intx')
    event_time = now_utc()
    risk_score = {'low': 5, 'medium': 15, 'high': 30, 'critical': 60}.get(sensitivity_level, 15)

    payload = {
        'interaction_id': interaction_id,
        'interaction_type': interaction_type,
        'source': 'mcp',
        'source_user_id': user.id,
        'source_user_name': user.username,
        'target_agent_id': None,
        'task_id': task.id,
        'attempt_id': generate_id('ia'),
        'description': question,
        'options': options or [],
        'contract': {},
        'security_context': {'sensitivity_level': sensitivity_level},
        'metadata': {'channel': 'mcp', 'api_token_name': getattr(g.api_token, 'name', None)},
        'status': 'pending_approval',
        'governance': {
            'requires_approval': True,
            'risk_tier': sensitivity_level,
            'sensitivity_level': sensitivity_level,
            'risk_score': risk_score,
        },
        'requested_at': event_time.isoformat(),
    }

    row = AgentTaskEvent(
        task_id=task.id,
        attempt_id=payload['attempt_id'],
        agent_id=None,
        workspace_id=workspace_id,
        event_type='interaction_request',
        seq=1,
        event_timestamp=event_time,
        payload=payload,
        message=f"MCP approval request {interaction_id} by {user.username}",
        created_by=f'user:{user.id}',
    )
    db.session.add(row)

    from api.agent_common import write_agent_audit
    write_agent_audit(
        event_type='interaction.requested',
        actor_type='user',
        actor_id=user.id,
        target_type='task',
        target_id=task.id,
        workspace_id=workspace_id,
        payload={
            'interaction_id': interaction_id,
            'task_id': task.id,
            'interaction_type': interaction_type,
            'audit_source': 'mcp_request_approval',
            'source': 'mcp',
            'source_user_id': user.id,
            'sensitivity_level': sensitivity_level,
            'risk_tier': sensitivity_level,
            'requires_approval': True,
            'request_status': 'pending_approval',
        },
        risk_score=risk_score,
    )
    db.session.commit()

    # 通知任务房间与任务创建者
    try:
        from api.user_websocket import push_to_task_room, push_to_user
        push_to_task_room(task.id, 'approval_request', {
            'task_id': task.id,
            'interaction_id': interaction_id,
            'interaction_type': interaction_type,
            'source': 'mcp',
            'source_user_name': user.username,
            'risk_tier': sensitivity_level,
            'sensitivity_level': sensitivity_level,
        })
        if task.created_by and ':' in str(task.created_by):
            try:
                owner_id = int(str(task.created_by).split(':')[-1])
                if owner_id != user.id:
                    push_to_user(owner_id, 'approval_request', {
                        'task_id': task.id,
                        'interaction_id': interaction_id,
                        'interaction_type': interaction_type,
                        'source_user_name': user.username,
                    })
            except (ValueError, TypeError):
                pass
    except Exception:
        pass

    return {
        'interaction_id': interaction_id,
        'task_id': task.id,
        'status': 'pending_approval',
        'question': question,
        'requested_at': payload['requested_at'],
        'workspace_id': workspace_id,
        'next_step': 'Workspace owner/admin can approve or reject via POST /workspaces/<workspace_id>/tasks/'
                     f'{task.id}/interactions/{interaction_id}/approval with {{"decision": "approved"|"rejected"}}',
    }


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
        query = query.filter(Task.status.in_(_status_members(status_filter)))

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

        if task.is_ai_task:
            from services.agent_runtime_controller import AgentRuntimeController
            AgentRuntimeController.auto_assign_task(task)

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


def _task_access_error(task):
    """MCP 侧任务访问检查（与 get_task_by_id 保持一致）。"""
    if task.creator_id != g.current_user.id:
        project = Project.query.get(task.project_id)
        if not project or project.owner_id != g.current_user.id:
            return {'error': 'Access denied: You can only access your own tasks'}
    return None


def get_task_evidence(arguments):
    """获取任务的完成标准（DoD）与验证证据"""
    task_id = arguments.get('task_id')
    if not task_id:
        return {'error': 'task_id is required'}
    try:
        task_id = validate_integer(task_id, 'task_id')
    except ValueError as e:
        return {'error': str(e)}

    task = Task.query.get(task_id)
    if not task:
        return {'error': f'Task with ID {task_id} not found'}

    access_error = _task_access_error(task)
    if access_error:
        return access_error

    items = (
        TaskEvidenceRecord.query
        .filter_by(task_id=task.id)
        .order_by(TaskEvidenceRecord.id.desc())
        .limit(50)
        .all()
    )
    return {
        'task_id': task.id,
        'task_status': task.status.value if task.status else None,
        'dod': task.dod or [],
        'evidence': [
            {
                'id': ev.id,
                'evidence_type': ev.evidence_type,
                'status': ev.status,
                'summary': ev.summary,
                'url': ev.url,
                'attempt_id': ev.attempt_id,
                'created_at': ev.created_at.isoformat() if ev.created_at else None,
            }
            for ev in items
        ],
    }


def set_task_dod(arguments):
    """设置任务的完成标准（DoD）。提交空数组可清除 DoD。"""
    task_id = arguments.get('task_id')
    dod = arguments.get('dod')

    if not task_id:
        return {'error': 'task_id is required'}
    try:
        task_id = validate_integer(task_id, 'task_id')
    except ValueError as e:
        return {'error': str(e)}

    if dod is None or not isinstance(dod, list):
        return {'error': 'dod must be an array of {type, value} objects'}

    normalized = []
    for item in dod:
        if not isinstance(item, dict):
            return {'error': 'each dod item must be an object'}
        dod_type = str(item.get('type') or '').strip().lower()
        if dod_type not in TaskEvidenceRecord.TYPES:
            return {'error': f"invalid dod type: {dod_type!r} (allowed: {', '.join(TaskEvidenceRecord.TYPES)})"}
        normalized.append({
            'type': dod_type,
            'value': str(item.get('value') or '')[:500],
        })

    task = Task.query.get(task_id)
    if not task:
        return {'error': f'Task with ID {task_id} not found'}

    access_error = _task_access_error(task)
    if access_error:
        return access_error

    task.dod = normalized
    db.session.commit()
    return {'task_id': task.id, 'dod': task.dod, 'updated': True}
