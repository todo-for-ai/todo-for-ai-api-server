"""Task CRUD and history routes."""

from datetime import datetime

from flask import request
from sqlalchemy import or_

from models import (
    db,
    Task,
    TaskStatus,
    TaskPriority,
    Project,
    ProjectMember,
    ProjectMemberStatus,
    TaskHistory,
    ActionType,
    UserActivity,
    TaskEventOutbox,
)
from ..base import ApiResponse, paginate_query, paginate_query_fast, validate_json_request, get_request_args
from core.auth import unified_auth_required, get_current_user
from ..agent_trigger_engine import emit_task_event
from ..notification_service import create_task_notifications, enqueue_pending_deliveries_for_events
from api.organizations.events import record_organization_event

from . import tasks_bp
from .shared import (
    _ensure_labels_for_tags,
    _invalidate_project_users,
    _normalize_participants,
    _normalize_tags,
    _tasks_cache_get,
    _tasks_cache_set,
)


def _user_display_name(user):
    if not user:
        return None
    return user.full_name or user.nickname or user.username or user.email or str(user.id)

@tasks_bp.route('', methods=['GET'])
@tasks_bp.route('/', methods=['GET'])
@unified_auth_required
def list_tasks():
    """获取任务列表"""
    try:
        args = get_request_args()
        current_user = get_current_user()

        if not current_user:
            # 未登录用户不能访问任务列表
            return ApiResponse.error("Authentication required", 401).to_response()

        # 缓存仅用于列表查询（按用户 + 查询参数隔离）
        cache_key = f"user:{current_user.id}:q:{request.query_string.decode('utf-8')}"
        cached_result = _tasks_cache_get(cache_key)
        if cached_result is not None:
            return ApiResponse.success(cached_result, "Tasks retrieved successfully").to_response()

        member_project_ids = db.session.query(ProjectMember.project_id).filter(
            ProjectMember.user_id == current_user.id,
            ProjectMember.status == ProjectMemberStatus.ACTIVE
        ).subquery()

        accessible_project_ids = db.session.query(Project.id).filter(
            or_(
                Project.owner_id == current_user.id,
                Project.id.in_(member_project_ids)
            )
        ).subquery()

        # 构建查询（owner + member 可访问项目）
        query = Task.query.filter(Task.project_id.in_(accessible_project_ids))
        
        # 项目筛选
        if args['project_id']:
            query = query.filter(Task.project_id == args['project_id'])
        
        # 状态筛选
        if args['status']:
            try:
                # 支持多状态筛选，用逗号分隔
                if ',' in args['status']:
                    status_list = [s.strip() for s in args['status'].split(',')]
                    status_enums = []
                    for status_str in status_list:
                        status_enums.append(TaskStatus(status_str))
                    query = query.filter(Task.status.in_(status_enums))
                else:
                    status = TaskStatus(args['status'])
                    query = query.filter_by(status=status)
            except ValueError:
                return ApiResponse.error(f"Invalid status: {args['status']}", 400).to_response()
        
        # 优先级筛选
        if args['priority']:
            try:
                priority = TaskPriority(args['priority'])
                query = query.filter_by(priority=priority)
            except ValueError:
                return ApiResponse.error(f"Invalid priority: {args['priority']}", 400).to_response()
        

        
        # 搜索
        if args['search']:
            search_term = f"%{args['search']}%"
            query = query.filter(
                Task.title.like(search_term) |
                Task.content.like(search_term)
            )
        
        # 排序
        if args['sort_by'] == 'title':
            order_column = Task.title
        elif args['sort_by'] == 'priority':
            order_column = Task.priority
        elif args['sort_by'] == 'status':
            order_column = Task.status
        elif args['sort_by'] == 'due_date':
            order_column = Task.due_date
        elif args['sort_by'] == 'updated_at':
            order_column = Task.updated_at
        else:
            order_column = Task.created_at
        
        if args['sort_order'] == 'desc':
            query = query.order_by(order_column.desc())
        else:
            query = query.order_by(order_column.asc())
        
        # 分页：默认使用快速分页，避免大数据量下 COUNT(*) 成为瓶颈
        include_total = request.args.get('include_total', 'false').lower() == 'true'
        if include_total:
            result = paginate_query(query, args['page'], args['per_page'])
        else:
            result = paginate_query_fast(query, args['page'], args['per_page'])
        
        # 批量加载项目基础信息，避免 N+1 查询
        project_ids = list({item['project_id'] for item in result['items'] if item.get('project_id')})
        project_map = {}
        if project_ids:
            projects = db.session.query(Project.id, Project.name, Project.color).filter(
                Project.id.in_(project_ids)
            ).all()
            project_map = {
                project.id: {
                    'id': project.id,
                    'name': project.name,
                    'color': project.color
                }
                for project in projects
            }

        for item in result['items']:
            project_id = item.get('project_id')
            if project_id and project_id in project_map:
                item['project'] = project_map[project_id]
        
        _tasks_cache_set(cache_key, result)
        return ApiResponse.success(result, "Tasks retrieved successfully").to_response()
        
    except Exception as e:
        return ApiResponse.error(f"Failed to retrieve tasks: {str(e)}", 500).to_response()


@tasks_bp.route('', methods=['POST'])
@tasks_bp.route('/', methods=['POST'])
@unified_auth_required
def create_task():
    """创建新任务"""
    try:
        current_user = get_current_user()

        # 验证请求数据
        data = validate_json_request(
            required_fields=['project_id'],
            optional_fields=[
                'title', 'content', 'status', 'priority',
                'due_date', 'tags', 'labels', 'is_ai_task',
                'assignees', 'mentions'
            ]
        )

        if isinstance(data, tuple):  # 错误响应
            return data

        # 验证项目是否存在
        project = Project.query.get(data['project_id'])
        if not project:
            return ApiResponse.error("Project not found", 404, error_details={"code": "PROJECT_NOT_FOUND"}).to_response()

        # 验证用户是否有权限在该项目中创建任务
        if not current_user.can_access_project(project):
            return ApiResponse.error("Permission denied", 403, error_details={"code": "PERMISSION_DENIED"}).to_response()
        
        # 处理日期字段
        due_date = None
        if 'due_date' in data and data['due_date']:
            try:
                due_date = datetime.fromisoformat(data['due_date'].replace('Z', '+00:00'))
            except ValueError:
                return ApiResponse.error("Invalid due_date format. Use ISO format.", 400).to_response()
        
        # 处理状态和优先级
        status = TaskStatus.TODO
        if 'status' in data:
            try:
                status = TaskStatus(data['status'])
            except ValueError:
                return ApiResponse.error(f"Invalid status: {data['status']}", 400).to_response()
        
        priority = TaskPriority.MEDIUM
        if 'priority' in data:
            try:
                priority = TaskPriority(data['priority'])
            except ValueError:
                return ApiResponse.error(f"Invalid priority: {data['priority']}", 400).to_response()
        
        # 处理标题：如果没有提供标题，从内容中生成
        title = data.get('title', '').strip()
        if not title:
            content = data.get('content', '').strip()

            if content:
                # 从内容中提取第一行或前50个字符作为标题
                first_line = content.split('\n')[0].strip()
                if first_line.startswith('#'):
                    # 如果是Markdown标题，去掉#号
                    title = first_line.lstrip('#').strip()
                else:
                    title = first_line[:50] + ('...' if len(first_line) > 50 else '')
            else:
                # 如果都没有，生成默认标题
                title = f"新任务 - {datetime.now().strftime('%Y-%m-%d %H:%M')}"

        incoming_tags = data.get('labels', data.get('tags', []))
        normalized_tags = _normalize_tags(incoming_tags)
        normalized_assignees = _normalize_participants(data.get('assignees'))
        normalized_mentions = _normalize_participants(data.get('mentions'))
        _ensure_labels_for_tags(current_user, project.id, normalized_tags)

        # 创建任务
        task = Task.create(
            project_id=data['project_id'],
            owner_id=project.owner_id,
            title=title,
            content=data.get('content', ''),
            status=status,
            priority=priority,
            due_date=due_date,
            tags=normalized_tags,
            assignees=normalized_assignees,
            mentions=normalized_mentions,
            revision=1,
            is_ai_task=data.get('is_ai_task', False),
            creator_id=current_user.id,  # 设置创建者ID
            created_by=current_user.email  # 设置创建者邮箱
        )
        db.session.flush()

        # 更新项目最后活动时间
        project.last_activity_at = datetime.utcnow()

        queued_notification_event_ids = []

        created_event_id = emit_task_event(
            task,
            'created',
            {
                'title': task.title,
                'status': task.status.value if task.status else None,
                'priority': task.priority.value if task.priority else None,
                'tags': task.tags or [],
            },
            actor=current_user.email,
        )

        create_task_notifications(
            task,
            'created',
            actor_user=current_user,
            payload={
                'title': task.title,
                'status': task.status.value if task.status else None,
                'priority': task.priority.value if task.priority else None,
                'tags': task.tags or [],
            },
            event_id=created_event_id,
        )
        if created_event_id:
            queued_notification_event_ids.append(created_event_id)

        if normalized_assignees:
            assigned_event_id = emit_task_event(
                task,
                'assigned',
                {
                    'assignees': normalized_assignees,
                },
                actor=current_user.email,
            )
            create_task_notifications(
                task,
                'assigned',
                actor_user=current_user,
                payload={'assignees': normalized_assignees},
                event_id=assigned_event_id,
                previous_assignees=[],
            )
            if assigned_event_id:
                queued_notification_event_ids.append(assigned_event_id)

        if normalized_mentions:
            mentioned_event_id = emit_task_event(
                task,
                'mentioned',
                {
                    'mentions': normalized_mentions,
                },
                actor=current_user.email,
            )
            create_task_notifications(
                task,
                'mentioned',
                actor_user=current_user,
                payload={'mentions': normalized_mentions},
                event_id=mentioned_event_id,
                previous_mentions=[],
            )
            if mentioned_event_id:
                queued_notification_event_ids.append(mentioned_event_id)

        record_organization_event(
            organization_id=project.organization_id,
            event_type='task.created',
            actor_type='user',
            actor_id=current_user.id,
            actor_name=_user_display_name(current_user),
            target_type='task',
            target_id=task.id,
            project_id=project.id,
            task_id=task.id,
            message=f"Task created: {task.title}",
            payload={
                'task_title': task.title,
                'task_status': task.status.value if task.status else None,
                'task_priority': task.priority.value if task.priority else None,
                'project_name': project.name,
            },
            created_by=current_user.email,
        )

        db.session.commit()
        enqueue_pending_deliveries_for_events(queued_notification_event_ids)
        _invalidate_project_users(project.id)

        # 记录历史
        TaskHistory.log_action(
            task_id=task.id,
            action=ActionType.CREATED,
            changed_by='api',
            comment='Task created via API'
        )

        # 记录用户活跃度
        if current_user:
            try:
                UserActivity.record_activity(current_user.id, 'task_created')
            except Exception as e:
                # 记录活跃度失败不应该影响任务创建
                print(f"Warning: Failed to record user activity: {str(e)}")

        return ApiResponse.created(
            task.to_dict(include_project=True, include_stats=True),
            "Task created successfully"
        ).to_response()
        
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to create task: {str(e)}", 500).to_response()


@tasks_bp.route('/<int:task_id>', methods=['GET'])
@unified_auth_required
def get_task(task_id):
    """获取单个任务详情"""
    try:
        current_user = get_current_user()

        task = Task.query.get(task_id)
        if not task:
            return ApiResponse.error("Task not found", 404, error_details={"code": "TASK_NOT_FOUND"}).to_response()

        # 权限检查 - 只能访问自己项目中的任务
        if not current_user.can_access_project(task.project):
            return ApiResponse.error("Access denied", 403, error_details={"code": "PERMISSION_DENIED"}).to_response()

        return ApiResponse.success(
            task.to_dict(include_project=True, include_stats=True),
            "Task retrieved successfully"
        ).to_response()

    except Exception as e:
        return ApiResponse.error(f"Failed to retrieve task: {str(e)}", 500).to_response()


@tasks_bp.route('/<int:task_id>', methods=['PUT'])
@unified_auth_required
def update_task(task_id):
    """更新任务"""
    try:
        current_user = get_current_user()

        task = Task.query.get(task_id)
        if not task:
            return ApiResponse.error("Task not found", 404, error_details={"code": "TASK_NOT_FOUND"}).to_response()

        # 验证用户是否有权限更新该任务 - 只能更新自己项目的任务
        if not current_user.can_access_project(task.project):
            return ApiResponse.error("Permission denied", 403, error_details={"code": "PERMISSION_DENIED"}).to_response()
        
        # 验证请求数据
        data = validate_json_request(
            optional_fields=[
                'title', 'content', 'status', 'priority',
                'due_date', 'completion_rate', 'tags', 'labels',
                'assignees', 'mentions', 'expected_revision'
            ]
        )
        
        if isinstance(data, tuple):  # 错误响应
            return data

        if 'expected_revision' in data:
            try:
                expected_revision = int(data['expected_revision'])
            except Exception:
                return ApiResponse.error("expected_revision must be integer", 400).to_response()

            if expected_revision != task.revision:
                return ApiResponse.error(
                    "Revision conflict. Please refresh and retry.",
                    409,
                    error_details={
                        "code": "REVISION_CONFLICT",
                        "current_revision": task.revision,
                        "expected_revision": expected_revision,
                    }
                ).to_response()
        
        # 记录变更
        changes = []
        queued_notification_event_ids = []
        previous_assignees = list(task.assignees or [])
        previous_mentions = list(task.mentions or [])
        
        # 处理日期字段
        if 'due_date' in data and data['due_date']:
            try:
                old_due_date = task.due_date
                new_due_date = datetime.fromisoformat(data['due_date'].replace('Z', '+00:00'))
                if old_due_date != new_due_date:
                    changes.append(('due_date', old_due_date, new_due_date))
                    task.due_date = new_due_date
            except ValueError:
                return ApiResponse.error("Invalid due_date format. Use ISO format.", 400).to_response()
        
        # 处理状态变更
        if 'status' in data:
            try:
                old_status = task.status
                new_status = TaskStatus(data['status'])
                if old_status != new_status:
                    changes.append(('status', old_status.value, new_status.value))
                    task.status = new_status
                    
                    # 如果状态变为完成，设置完成时间
                    if new_status == TaskStatus.DONE and old_status != TaskStatus.DONE:
                        task.completed_at = datetime.utcnow()
                        task.completion_rate = 100
            except ValueError:
                return ApiResponse.error(f"Invalid status: {data['status']}", 400).to_response()
        
        # 处理优先级变更
        if 'priority' in data:
            try:
                old_priority = task.priority
                new_priority = TaskPriority(data['priority'])
                if old_priority != new_priority:
                    changes.append(('priority', old_priority.value, new_priority.value))
                    task.priority = new_priority
            except ValueError:
                return ApiResponse.error(f"Invalid priority: {data['priority']}", 400).to_response()
        
        # 处理其他字段
        simple_fields = ['title', 'content', 'completion_rate']
        for field in simple_fields:
            if field in data:
                old_value = getattr(task, field)
                new_value = data[field]
                if old_value != new_value:
                    changes.append((field, old_value, new_value))
                    setattr(task, field, new_value)

        incoming_tags = None
        if 'labels' in data:
            incoming_tags = data.get('labels', [])
        elif 'tags' in data:
            incoming_tags = data.get('tags', [])

        if incoming_tags is not None:
            normalized_tags = _normalize_tags(incoming_tags)
            _ensure_labels_for_tags(current_user, task.project_id, normalized_tags)
            if task.tags != normalized_tags:
                changes.append(('tags', task.tags, normalized_tags))
                task.tags = normalized_tags

        if 'assignees' in data:
            normalized_assignees = _normalize_participants(data.get('assignees'))
            if task.assignees != normalized_assignees:
                changes.append(('assignees', task.assignees, normalized_assignees))
                task.assignees = normalized_assignees

        if 'mentions' in data:
            normalized_mentions = _normalize_participants(data.get('mentions'))
            if task.mentions != normalized_mentions:
                changes.append(('mentions', task.mentions, normalized_mentions))
                task.mentions = normalized_mentions

        # 更新项目最后活动时间
        if changes:  # 只有在有实际更改时才更新项目活跃时间
            task.project.last_activity_at = datetime.utcnow()
            task.revision = (task.revision or 1) + 1

            changed_field_names = [field_name for field_name, _, _ in changes]
            status_change = next((item for item in changes if item[0] == 'status'), None)
            if status_change:
                from_status_value = status_change[1].value if hasattr(status_change[1], 'value') else status_change[1]
                to_status_value = status_change[2].value if hasattr(status_change[2], 'value') else status_change[2]
                status_event_id = emit_task_event(
                    task,
                    'status_changed',
                    {
                        'from_status': from_status_value,
                        'to_status': to_status_value,
                        'changed_fields': changed_field_names,
                    },
                    actor=current_user.email,
                )
                if str(to_status_value or '').strip().lower() == TaskStatus.DONE.value:
                    create_task_notifications(
                        task,
                        'completed',
                        actor_user=current_user,
                        payload={
                            'from_status': from_status_value,
                            'to_status': to_status_value,
                            'changed_fields': changed_field_names,
                        },
                        event_id=status_event_id,
                        previous_assignees=previous_assignees,
                        previous_mentions=previous_mentions,
                    )
                    if status_event_id:
                        queued_notification_event_ids.append(status_event_id)
            else:
                emit_task_event(
                    task,
                    'updated',
                    {
                        'changed_fields': changed_field_names,
                    },
                    actor=current_user.email,
                )

            assignee_change = next((item for item in changes if item[0] == 'assignees'), None)
            if assignee_change:
                assigned_event_id = emit_task_event(
                    task,
                    'assigned',
                    {
                        'assignees': task.assignees or [],
                    },
                    actor=current_user.email,
                )
                create_task_notifications(
                    task,
                    'assigned',
                    actor_user=current_user,
                    payload={'assignees': task.assignees or []},
                    event_id=assigned_event_id,
                    previous_assignees=assignee_change[1] or [],
                )
                if assigned_event_id:
                    queued_notification_event_ids.append(assigned_event_id)

            mention_change = next((item for item in changes if item[0] == 'mentions'), None)
            if mention_change:
                mentioned_event_id = emit_task_event(
                    task,
                    'mentioned',
                    {
                        'mentions': task.mentions or [],
                    },
                    actor=current_user.email,
                )
                create_task_notifications(
                    task,
                    'mentioned',
                    actor_user=current_user,
                    payload={'mentions': task.mentions or []},
                    event_id=mentioned_event_id,
                    previous_mentions=mention_change[1] or [],
                )
                if mentioned_event_id:
                    queued_notification_event_ids.append(mentioned_event_id)

            event_payload = {
                'changed_fields': changed_field_names,
                'task_title': task.title,
                'project_name': task.project.name if task.project else None,
            }
            event_type = 'task.updated'
            if status_change:
                from_status_value = status_change[1].value if hasattr(status_change[1], 'value') else status_change[1]
                to_status_value = status_change[2].value if hasattr(status_change[2], 'value') else status_change[2]
                event_type = 'task.status_changed'
                event_payload['from_status'] = from_status_value
                event_payload['to_status'] = to_status_value

            record_organization_event(
                organization_id=task.project.organization_id if task.project else None,
                event_type=event_type,
                actor_type='user',
                actor_id=current_user.id,
                actor_name=_user_display_name(current_user),
                target_type='task',
                target_id=task.id,
                project_id=task.project_id,
                task_id=task.id,
                message=f"Task updated: {task.title}",
                payload=event_payload,
                created_by=current_user.email,
            )

        db.session.commit()
        enqueue_pending_deliveries_for_events(queued_notification_event_ids)
        _invalidate_project_users(task.project_id)

        # 记录变更历史
        status_changed = False
        for field_name, old_value, new_value in changes:
            TaskHistory.log_action(
                task_id=task.id,
                action=ActionType.UPDATED,
                changed_by='api',
                field_name=field_name,
                old_value=str(old_value) if old_value is not None else None,
                new_value=str(new_value) if new_value is not None else None,
                comment=f'Field {field_name} updated via API'
            )
            if field_name == 'status':
                status_changed = True

        # 记录用户活跃度
        if current_user:
            try:
                if status_changed:
                    UserActivity.record_activity(current_user.id, 'task_status_changed')
                    # 如果任务状态变为完成，额外记录完成任务活跃度
                    if 'status' in data and data['status'] == 'done':
                        UserActivity.record_activity(current_user.id, 'task_completed')
                else:
                    UserActivity.record_activity(current_user.id, 'task_updated')
            except Exception as e:
                # 记录活跃度失败不应该影响任务更新
                print(f"Warning: Failed to record user activity: {str(e)}")

        return ApiResponse.success(
            task.to_dict(include_project=True, include_stats=True),
            "Task updated successfully"
        ).to_response()
        
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to update task: {str(e)}", 500).to_response()


@tasks_bp.route('/<int:task_id>', methods=['DELETE'])
@unified_auth_required
def delete_task(task_id):
    """删除任务"""
    try:
        current_user = get_current_user()

        task = Task.query.get(task_id)
        if not task:
            return ApiResponse.error("Task not found", 404, error_details={"code": "TASK_NOT_FOUND"}).to_response()

        # 与list权限一致 所有用户（包括管理员）只能删除自己项目中的任务
        if not current_user.can_access_project(task.project):
            return ApiResponse.error("Access denied", 403, error_details={"code": "PERMISSION_DENIED"}).to_response()
        
        # 记录删除历史
        TaskHistory.log_action(
            task_id=task.id,
            action=ActionType.DELETED,
            changed_by='api',
            comment='Task deleted via API'
        )
        
        # 删除任务前清理 outbox 记录，避免外键约束阻断删除
        db.session.query(TaskEventOutbox).filter(
            TaskEventOutbox.task_id == task.id
        ).delete(synchronize_session=False)

        record_organization_event(
            organization_id=task.project.organization_id if task.project else None,
            event_type='task.deleted',
            actor_type='user',
            actor_id=current_user.id,
            actor_name=_user_display_name(current_user),
            target_type='task',
            target_id=task.id,
            project_id=task.project_id,
            task_id=task.id,
            message=f"Task deleted: {task.title}",
            payload={
                'task_title': task.title,
                'project_name': task.project.name if task.project else None,
            },
            created_by=current_user.email,
        )

        # 删除任务
        task.delete()
        _invalidate_project_users(task.project_id)
        
        return ApiResponse.success(None, "Task deleted successfully", 204).to_response()
        
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to delete task: {str(e)}", 500).to_response()


@tasks_bp.route('/<int:task_id>/history', methods=['GET'])
@unified_auth_required
def get_task_history(task_id):
    """获取任务历史记录"""
    try:
        current_user = get_current_user()

        # 验证任务是否存在
        task = Task.query.get(task_id)
        if not task:
            return ApiResponse.error("Task not found", 404, error_details={"code": "TASK_NOT_FOUND"}).to_response()

        # 权限检查 - 只能访问自己项目中的任务历史
        if not current_user.can_access_project(task.project):
            return ApiResponse.error("Access denied", 403, error_details={"code": "PERMISSION_DENIED"}).to_response()

        # 获取任务历史记录
        history_records = TaskHistory.get_task_history(task_id, limit=100)  # 限制返回最近100条记录

        result = [record.to_dict() for record in history_records]

        return ApiResponse.success(
            result,
            f"Task history retrieved successfully ({len(result)} records)"
        ).to_response()

    except Exception as e:
        return ApiResponse.error(f"Failed to retrieve task history: {str(e)}", 500).to_response()
