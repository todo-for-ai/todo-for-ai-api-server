"""
任务 API 蓝图

提供任务的 CRUD 操作接口
"""

from datetime import datetime
import os
import uuid
from flask import Blueprint, request, send_file
from sqlalchemy import and_, or_
from werkzeug.utils import secure_filename
from models import (
    db,
    Task,
    TaskStatus,
    TaskPriority,
    Project,
    ProjectMember,
    ProjectMemberStatus,
    TaskLabel,
    BUILTIN_TASK_LABELS,
    TaskHistory,
    ActionType,
    UserActivity,
    Attachment,
)
from .base import ApiResponse, paginate_query, paginate_query_fast, validate_json_request, get_request_args, APIException, handle_api_error
from core.auth import unified_auth_required, get_current_user
from core.redis_client import get_json as redis_get_json, set_json as redis_set_json
from core.cache_invalidation import invalidate_user_caches
from .agent_trigger_engine import emit_task_event
from .notification_service import create_task_notifications, enqueue_pending_deliveries_for_events

# 创建蓝图
tasks_bp = Blueprint('tasks', __name__)

TASKS_LIST_CACHE_TTL_SECONDS = 15
tasks_list_fallback_cache = {}
MAX_ATTACHMENT_SIZE_BYTES = 20 * 1024 * 1024
ALLOWED_ATTACHMENT_EXTENSIONS = {
    '.txt', '.md', '.json', '.csv', '.pdf',
    '.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg',
    '.zip', '.tar', '.gz',
    '.py', '.js', '.ts', '.tsx', '.jsx', '.java', '.go', '.rs', '.sql', '.yaml', '.yml'
}


def _normalize_tags(raw_tags):
    if not raw_tags:
        return []
    normalized = []
    seen = set()
    for raw in raw_tags:
        tag = str(raw).strip().lower()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        normalized.append(tag)
    return normalized


def _ensure_builtin_labels():
    for item in BUILTIN_TASK_LABELS:
        exists = TaskLabel.query.filter(
            TaskLabel.is_builtin.is_(True),
            TaskLabel.name == item['name'],
            TaskLabel.owner_id.is_(None),
            TaskLabel.project_id.is_(None)
        ).first()
        if exists:
            # 如果内置标签被误禁用或属性漂移，自动修复
            exists.is_active = True
            exists.color = item['color']
            exists.description = item['description']
            continue
        TaskLabel.create(
            owner_id=None,
            project_id=None,
            name=item['name'],
            color=item['color'],
            description=item['description'],
            is_builtin=True,
            is_active=True,
            created_by='system',
            created_by_user_id=None,
        )
    db.session.flush()


def _ensure_labels_for_tags(current_user, project_id, tags):
    if not tags:
        return
    _ensure_builtin_labels()
    for tag in tags:
        exists = TaskLabel.query.filter(
            or_(
                and_(TaskLabel.is_builtin.is_(True), TaskLabel.name == tag),
                and_(TaskLabel.owner_id == current_user.id, TaskLabel.project_id == project_id, TaskLabel.name == tag),
                and_(TaskLabel.owner_id == current_user.id, TaskLabel.project_id.is_(None), TaskLabel.name == tag),
            )
        ).first()
        if exists:
            # 软删除标签在任务输入中再次出现时，自动恢复可见性
            if not exists.is_active:
                exists.is_active = True
            continue
        TaskLabel.create(
            owner_id=current_user.id,
            project_id=project_id,
            name=tag,
            color='#1677ff',
            description='',
            is_builtin=False,
            is_active=True,
            created_by=current_user.email,
            created_by_user_id=current_user.id,
        )
    db.session.flush()


def _normalize_participants(raw_items):
    if not raw_items:
        return []

    normalized = []
    seen = set()
    for item in raw_items:
        if not isinstance(item, dict):
            continue

        ptype = str(item.get('type') or '').strip().lower()
        if ptype not in {'human', 'agent'}:
            continue

        try:
            pid = int(item.get('id'))
        except Exception:
            continue

        key = f"{ptype}:{pid}"
        if key in seen:
            continue
        seen.add(key)
        normalized.append({'type': ptype, 'id': pid})
    return normalized


def _invalidate_project_users(project_id):
    project = Project.query.get(project_id)
    if not project:
        return
    user_ids = {project.owner_id}
    member_rows = db.session.query(ProjectMember.user_id).filter(
        ProjectMember.project_id == project_id,
        ProjectMember.status == ProjectMemberStatus.ACTIVE
    ).all()
    user_ids.update([row.user_id for row in member_rows])
    for user_id in user_ids:
        invalidate_user_caches(user_id)


def _tasks_cache_get(key):
    redis_key = f"tasks:list:{key}"
    cached = redis_get_json(redis_key)
    if cached is not None:
        return cached

    item = tasks_list_fallback_cache.get(key)
    if item and (datetime.utcnow().timestamp() - item['cached_at'] <= TASKS_LIST_CACHE_TTL_SECONDS):
        return item['value']
    return None


def _tasks_cache_set(key, value):
    redis_key = f"tasks:list:{key}"
    redis_set_json(redis_key, value, TASKS_LIST_CACHE_TTL_SECONDS)
    tasks_list_fallback_cache[key] = {
        'cached_at': datetime.utcnow().timestamp(),
        'value': value,
    }


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


@tasks_bp.route('/<int:task_id>/attachments', methods=['GET'])
@unified_auth_required
def get_task_attachments(task_id):
    """获取任务附件列表"""
    try:
        current_user = get_current_user()

        # 验证任务是否存在
        task = Task.query.get(task_id)
        if not task:
            return ApiResponse.error("Task not found", 404, error_details={"code": "TASK_NOT_FOUND"}).to_response()

        # 权限检查 - 只能访问自己项目中的任务附件
        if not current_user.can_access_project(task.project):
            return ApiResponse.error("Access denied", 403, error_details={"code": "PERMISSION_DENIED"}).to_response()

        result = [item.to_dict() for item in Attachment.get_task_attachments(task_id)]

        return ApiResponse.success(
            result,
            "Task attachments retrieved successfully"
        ).to_response()

    except Exception as e:
        return ApiResponse.error(f"Failed to retrieve task attachments: {str(e)}", 500).to_response()


@tasks_bp.route('/<int:task_id>/attachments/<int:attachment_id>', methods=['DELETE'])
@unified_auth_required
def delete_task_attachment(task_id, attachment_id):
    """删除任务附件"""
    try:
        current_user = get_current_user()

        # 验证任务是否存在
        task = Task.query.get(task_id)
        if not task:
            return ApiResponse.error("Task not found", 404, error_details={"code": "TASK_NOT_FOUND"}).to_response()

        # 权限检查 - 只能删除自己项目中的任务附件
        if not current_user.can_access_project(task.project):
            return ApiResponse.error("Access denied", 403, error_details={"code": "PERMISSION_DENIED"}).to_response()

        attachment = Attachment.query.filter_by(id=attachment_id, task_id=task_id).first()
        if not attachment:
            return ApiResponse.not_found("Attachment not found").to_response()

        attachment.delete_file()

        return ApiResponse.success(
            None,
            f"Task attachment {attachment_id} deleted successfully"
        ).to_response()

    except Exception as e:
        return ApiResponse.error(f"Failed to delete task attachment: {str(e)}", 500).to_response()


@tasks_bp.route('/<int:task_id>/attachments', methods=['POST'])
@unified_auth_required
def upload_task_attachment(task_id):
    """上传任务附件"""
    try:
        current_user = get_current_user()
        task = Task.query.get(task_id)
        if not task:
            return ApiResponse.not_found("Task not found").to_response()

        if not current_user.can_access_project(task.project):
            return ApiResponse.forbidden("Access denied").to_response()

        uploaded_file = request.files.get('file')
        if not uploaded_file:
            return ApiResponse.error("Missing file field", 400).to_response()

        original_filename = uploaded_file.filename or ''
        if not original_filename.strip():
            return ApiResponse.error("Empty filename", 400).to_response()

        ext = os.path.splitext(original_filename)[1].lower()
        if ext and ext not in ALLOWED_ATTACHMENT_EXTENSIONS:
            return ApiResponse.error(f"File extension {ext} is not allowed", 400).to_response()

        upload_root = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'uploads', 'tasks', str(task_id))
        os.makedirs(upload_root, exist_ok=True)

        safe_name = secure_filename(original_filename) or f"file{ext or ''}"
        stored_name = f"{uuid.uuid4().hex}_{safe_name}"
        abs_path = os.path.join(upload_root, stored_name)
        uploaded_file.save(abs_path)

        file_size = os.path.getsize(abs_path)
        if file_size > MAX_ATTACHMENT_SIZE_BYTES:
            os.remove(abs_path)
            return ApiResponse.error(f"File too large. Max size is {MAX_ATTACHMENT_SIZE_BYTES} bytes", 400).to_response()

        attachment = Attachment.create_attachment(
            task_id=task_id,
            filename=stored_name,
            original_filename=original_filename,
            file_path=abs_path,
            file_size=file_size,
            mime_type=uploaded_file.mimetype,
            uploaded_by=current_user.email,
        )

        return ApiResponse.created(attachment.to_dict(), "Task attachment uploaded successfully").to_response()
    except Exception as e:
        return ApiResponse.error(f"Failed to upload task attachment: {str(e)}", 500).to_response()


@tasks_bp.route('/<int:task_id>/attachments/<int:attachment_id>/download', methods=['GET'])
@unified_auth_required
def download_task_attachment(task_id, attachment_id):
    """下载任务附件"""
    try:
        current_user = get_current_user()
        task = Task.query.get(task_id)
        if not task:
            return ApiResponse.not_found("Task not found").to_response()

        if not current_user.can_access_project(task.project):
            return ApiResponse.forbidden("Access denied").to_response()

        attachment = Attachment.query.filter_by(id=attachment_id, task_id=task_id).first()
        if not attachment:
            return ApiResponse.not_found("Attachment not found").to_response()

        if not os.path.exists(attachment.file_path):
            return ApiResponse.not_found("Attachment file not found").to_response()

        return send_file(
            attachment.file_path,
            as_attachment=True,
            download_name=attachment.original_filename,
            mimetype=attachment.mime_type or 'application/octet-stream',
        )
    except Exception as e:
        return ApiResponse.error(f"Failed to download task attachment: {str(e)}", 500).to_response()
