"""
任务 API 蓝图

提供任务的 CRUD 操作接口
"""

from datetime import datetime
from flask import Blueprint, request
from sqlalchemy import func
from models import db, Task, TaskStatus, TaskPriority, Project, TaskHistory, ActionType, UserActivity
from .base import ApiResponse, paginate_query, validate_json_request, get_request_args, APIException, handle_api_error
from core.auth import unified_auth_required, get_current_user

# 创建蓝图
tasks_bp = Blueprint('tasks', __name__)


@tasks_bp.route('', methods=['GET'])
@unified_auth_required
def list_tasks():
    """获取任务列表"""
    try:
        args = get_request_args()
        current_user = get_current_user()

        # 构建查询
        query = Task.query

        # 用户权限控制 - 所有用户（包括管理员）只能看到自己项目的任务
        if current_user:
            query = query.join(Project).filter(Project.owner_id == current_user.id)
        else:
            # 未登录用户不能访问任务列表
            return ApiResponse.error("Authentication required", 401).to_response()
        
        # 项目筛选
        if args['project_id']:
            query = query.filter(Task.project_id == args['project_id'])

        # 子任务筛选
        parent_task_id = request.args.get('parent_task_id', type=int)
        if parent_task_id:
            query = query.filter_by(parent_task_id=parent_task_id)
        
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
        
        # 分页
        result = paginate_query(query, args['page'], args['per_page'])
        
        # 包含项目信息
        for item in result['items']:
            if 'project_id' in item:
                project = Project.query.get(item['project_id'])
                if project:
                    item['project'] = {
                        'id': project.id,
                        'name': project.name,
                        'color': project.color
                    }
        
        return ApiResponse.success(result, "Tasks retrieved successfully").to_response()
        
    except Exception as e:
        return ApiResponse.error(f"Failed to retrieve tasks: {str(e)}", 500).to_response()


@tasks_bp.route('', methods=['POST'])
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
                'due_date', 'tags', 'is_ai_task', 'parent_task_id',
                'required_capabilities'
            ]
        )

        if isinstance(data, tuple):  # 错误响应
            return data

        # 验证项目是否存在
        project = Project.query.get(data['project_id'])
        if not project:
            return ApiResponse.error("Project not found", 404, error_details={"code": "PROJECT_NOT_FOUND"}).to_response()

        # 验证用户是否有权限在该项目中创建任务 - 只能在自己的项目中创建任务
        if project.owner_id != current_user.id:
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

        # 验证 parent_task_id（如果提供）
        parent_task_id = data.get('parent_task_id')
        if parent_task_id:
            parent_task = Task.query.get(parent_task_id)
            if not parent_task:
                return ApiResponse.error("Parent task not found", 404).to_response()
            if parent_task.project_id != data['project_id']:
                return ApiResponse.error("Parent task must belong to the same project", 400).to_response()

        # 创建任务
        task = Task.create(
            project_id=data['project_id'],
            title=title,
            content=data.get('content', ''),
            status=status,
            priority=priority,
            due_date=due_date,
            tags=data.get('tags', []),
            is_ai_task=data.get('is_ai_task', False),
            parent_task_id=parent_task_id,
            creator_id=current_user.id,  # 设置创建者ID
            created_by=current_user.email  # 设置创建者邮箱
        )

        # 更新项目最后活动时间
        project.last_activity_at = datetime.utcnow()

        # 如果是子任务，将父任务标记为 BLOCKED（子任务未全部完成）
        if parent_task_id and parent_task:
            if parent_task.status != TaskStatus.BLOCKED:
                parent_task.status = TaskStatus.BLOCKED

        db.session.commit()

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
        if task.project.owner_id != current_user.id:
            return ApiResponse.error("Access denied: You can only access tasks from your own projects", 403, error_details={"code": "PERMISSION_DENIED"}).to_response()

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
        if task.project.owner_id != current_user.id:
            return ApiResponse.error("Permission denied", 403, error_details={"code": "PERMISSION_DENIED"}).to_response()
        
        # 验证请求数据
        data = validate_json_request(
            optional_fields=[
                'title', 'content', 'status', 'priority',
                'due_date', 'completion_rate', 'tags',
                'required_capabilities'
            ]
        )
        
        if isinstance(data, tuple):  # 错误响应
            return data
        
        # 记录变更
        changes = []
        
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
                        # Auto-unblock parent if all subtasks are done
                        task.try_unblock_parent()
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
        simple_fields = ['title', 'content', 'completion_rate', 'tags']
        for field in simple_fields:
            if field in data:
                old_value = getattr(task, field)
                new_value = data[field]
                if old_value != new_value:
                    changes.append((field, old_value, new_value))
                    setattr(task, field, new_value)

        # 更新项目最后活动时间
        if changes:  # 只有在有实际更改时才更新项目活跃时间
            task.project.last_activity_at = datetime.utcnow()

        db.session.commit()

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

        if task.project.owner_id != current_user.id:
            return ApiResponse.error("Access denied: You can only delete tasks from your own projects", 403, error_details={"code": "PERMISSION_DENIED"}).to_response()
        
        # 记录删除历史
        TaskHistory.log_action(
            task_id=task.id,
            action=ActionType.DELETED,
            changed_by='api',
            comment='Task deleted via API'
        )
        
        # 删除任务
        task.delete()
        
        return ApiResponse.success(None, "Task deleted successfully").to_response()
        
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to delete task: {str(e)}", 500).to_response()


@tasks_bp.route('/<int:task_id>/subtasks', methods=['GET'])
@unified_auth_required
def get_subtasks(task_id):
    """获取任务的子任务列表"""
    try:
        current_user = get_current_user()
        task = Task.query.get(task_id)
        if not task:
            return ApiResponse.error("Task not found", 404).to_response()

        subtasks = Task.query.filter_by(parent_task_id=task_id).order_by(Task.created_at.asc()).all()
        return ApiResponse.success(
            [t.to_dict(include_project=True) for t in subtasks],
            "Subtasks retrieved successfully",
        ).to_response()
    except Exception as e:
        return ApiResponse.error(f"Failed to retrieve subtasks: {str(e)}", 500).to_response()


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
        if task.project.owner_id != current_user.id:
            return ApiResponse.error("Access denied: You can only access history from your own tasks", 403, error_details={"code": "PERMISSION_DENIED"}).to_response()

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
        if task.project.owner_id != current_user.id:
            return ApiResponse.error("Access denied: You can only access attachments from your own tasks", 403, error_details={"code": "PERMISSION_DENIED"}).to_response()

        # TODO: 实现完整的附件功能
        # 目前返回空列表，避免前端调用出错
        result = []

        return ApiResponse.success(
            result,
            "Task attachments retrieved successfully (feature not fully implemented)"
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
        if task.project.owner_id != current_user.id:
            return ApiResponse.error("Access denied: You can only delete attachments from your own tasks", 403, error_details={"code": "PERMISSION_DENIED"}).to_response()

        # TODO: 实现完整的附件删除功能
        # 目前返回成功响应，避免前端调用出错
        return ApiResponse.success(
            None,
            f"Task attachment {attachment_id} deleted successfully (feature not fully implemented)"
        ).to_response()

    except Exception as e:
        return ApiResponse.error(f"Failed to delete task attachment: {str(e)}", 500).to_response()


@tasks_bp.route('/stats', methods=['GET'])
@unified_auth_required
def task_stats():
    """Aggregate task lifecycle stats for the current user's projects.

    Reports status distribution, completion/cancellation rates, average
    lifecycle duration (done tasks: completed_at - created_at) bucketed
    into ranges, and per-priority counts. Reveals throughput bottlenecks
    and how often work is abandoned vs completed.
    """
    user = get_current_user()

    # 限定当前用户的项目
    base_query = Task.query.join(Project).filter(Project.owner_id == user.id)

    total = base_query.count()
    if total == 0:
        return ApiResponse.success({
            "total": 0,
            "by_status": {},
            "by_priority": {},
            "completion_rate": 0,
            "cancellation_rate": 0,
            "avg_lifecycle_hours": None,
            "lifecycle_buckets": {},
            "avg_completion_rate": 0,
            "by_project": [],
            "overdue_count": 0,
            "with_due_date": 0,
            "overdue_rate": 0,
        }).to_response()

    # 按状态分布
    status_rows = base_query.with_entities(Task.status, func.count(Task.id)).group_by(Task.status).all()
    by_status = {s.value if s else "(未知)": c for s, c in status_rows}

    # 按优先级分布
    priority_rows = base_query.with_entities(Task.priority, func.count(Task.id)).group_by(Task.priority).all()
    by_priority = {p.value if p else "(未知)": c for p, c in priority_rows}

    done_count = by_status.get("done", 0)
    cancelled_count = by_status.get("cancelled", 0)
    completion_rate = round(done_count / total * 100, 1)
    cancellation_rate = round(cancelled_count / total * 100, 1)

    # 生命周期耗时（仅已完成且有 completed_at）
    done_tasks = base_query.filter(
        Task.status == TaskStatus.DONE,
        Task.completed_at.isnot(None),
    ).with_entities(Task.created_at, Task.completed_at).all()

    lifecycle_hours = []
    for created_at, completed_at in done_tasks:
        if created_at and completed_at and completed_at > created_at:
            delta_hours = (completed_at - created_at).total_seconds() / 3600
            if delta_hours >= 0:
                lifecycle_hours.append(delta_hours)

    avg_lifecycle = round(sum(lifecycle_hours) / len(lifecycle_hours), 2) if lifecycle_hours else None

    # 分桶：0-1h, 1-4h, 4-12h, 12-24h, 1-3d, 3-7d, >7d
    buckets = {
        "0-1h": 0, "1-4h": 0, "4-12h": 0, "12-24h": 0,
        "1-3d": 0, "3-7d": 0, ">7d": 0,
    }
    for h in lifecycle_hours:
        if h < 1:
            buckets["0-1h"] += 1
        elif h < 4:
            buckets["1-4h"] += 1
        elif h < 12:
            buckets["4-12h"] += 1
        elif h < 24:
            buckets["12-24h"] += 1
        elif h < 72:
            buckets["1-3d"] += 1
        elif h < 168:
            buckets["3-7d"] += 1
        else:
            buckets[">7d"] += 1

    # 平均完成率（completion_rate 字段）
    cr_rows = base_query.with_entities(func.avg(Task.completion_rate)).scalar()
    avg_completion_rate = round(cr_rows, 1) if cr_rows is not None else 0

    # 按项目分布
    project_rows = (
        base_query.with_entities(Task.project_id, Project.name, func.count(Task.id))
        .group_by(Task.project_id, Project.name)
        .order_by(func.count(Task.id).desc())
        .limit(10)
        .all()
    )
    by_project = [{"project_id": pid, "name": pname or f"#{pid}", "count": c} for pid, pname, c in project_rows]

    # 逾期统计：有 due_date，未结束（非 done/cancelled），且 due_date < now
    now = datetime.utcnow()
    terminal_states = [TaskStatus.DONE, TaskStatus.CANCELLED]
    overdue_query = base_query.filter(
        Task.due_date.isnot(None),
        Task.due_date < now,
        ~Task.status.in_(terminal_states),
    )
    overdue_count = overdue_query.count()
    # 有 due_date 的任务总数（用于算逾期率分母）
    with_due = base_query.filter(Task.due_date.isnot(None)).count()
    overdue_rate = round(overdue_count / with_due * 100, 1) if with_due else 0

    return ApiResponse.success({
        "total": total,
        "by_status": by_status,
        "by_priority": by_priority,
        "completion_rate": completion_rate,
        "cancellation_rate": cancellation_rate,
        "done_count": done_count,
        "cancelled_count": cancelled_count,
        "avg_lifecycle_hours": avg_lifecycle,
        "lifecycle_buckets": buckets,
        "avg_completion_rate": avg_completion_rate,
        "by_project": by_project,
        "overdue_count": overdue_count,
        "with_due_date": with_due,
        "overdue_rate": overdue_rate,
    }).to_response()
