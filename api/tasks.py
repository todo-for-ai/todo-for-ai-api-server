"""
任务 API 蓝图

提供任务的 CRUD 操作接口
"""

from datetime import datetime, timedelta
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
            "by_priority_status": {},
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

    # 优先级 × 状态矩阵：{priority: {status: count}}
    ps_rows = base_query.with_entities(Task.priority, Task.status, func.count(Task.id)).group_by(Task.priority, Task.status).all()
    by_priority_status: dict = {}
    for p, s, c in ps_rows:
        pk = p.value if p else "(未知)"
        sk = s.value if s else "(未知)"
        by_priority_status.setdefault(pk, {})[sk] = by_priority_status.get(pk, {}).get(sk, 0) + c

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
        "by_priority_status": by_priority_status,
    }).to_response()


@tasks_bp.route('/overdue-trend', methods=['GET'])
@unified_auth_required
def task_overdue_trend():
    """Daily overdue task trend by due_date for the current user.

    Buckets overdue tasks (due_date < now, status not done/cancelled) by the
    calendar day of their due_date within the lookback window. Also reports
    per-priority overdue counts for the same set. Reveals whether overdue
    workload is accumulating over time and which priorities bear the brunt.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
    except (TypeError, ValueError):
        days = 30

    base_query = (
        Task.query
        .join(Project)
        .filter(Project.owner_id == user.id)
    )
    now = datetime.utcnow()
    since = now - timedelta(days=days)
    terminal_states = [TaskStatus.DONE, TaskStatus.CANCELLED]

    # 逾期且 due_date 在窗口内的任务，按 due_date 日期分桶
    rows = (
        base_query
        .filter(
            Task.due_date.isnot(None),
            Task.due_date < now,
            Task.due_date >= since,
            ~Task.status.in_(terminal_states),
        )
        .with_entities(
            func.date(Task.due_date).label("d"),
            Task.priority,
            func.count(Task.id),
        )
        .group_by(func.date(Task.due_date), Task.priority)
        .all()
    )

    by_day: dict = {}
    by_priority_totals: dict = {}
    total_overdue = 0
    for d, priority, c in rows:
        if not d:
            continue
        key = str(d)
        bucket = by_day.setdefault(key, {"date": key, "overdue": 0, "by_priority": {}})
        bucket["overdue"] += c
        pk = priority.value if priority else "(未知)"
        bucket["by_priority"][pk] = bucket["by_priority"].get(pk, 0) + c
        by_priority_totals[pk] = by_priority_totals.get(pk, 0) + c
        total_overdue += c

    trend = sorted(by_day.values(), key=lambda x: x["date"])
    by_priority_sorted = dict(sorted(by_priority_totals.items(), key=lambda kv: kv[1], reverse=True))
    return ApiResponse.success({
        "days": days,
        "trend": trend,
        "total_overdue": total_overdue,
        "by_priority_totals": by_priority_sorted,
    }).to_response()


@tasks_bp.route('/overdue-by-assignee', methods=['GET'])
@unified_auth_required
def task_overdue_by_assignee():
    """Overdue task count grouped by assignee (Agent) for the current user.

    Counts tasks that are overdue (due_date < now, status not done/cancelled)
    and have an active assignment. Per agent: overdue count, by-priority
    breakdown, and earliest overdue due_date. Sorted by overdue count
    descending. Reveals which agents bear the heaviest overdue burden.
    """
    user = get_current_user()
    try:
        limit = max(1, min(20, int(request.args.get("limit", 10))))
    except (TypeError, ValueError):
        limit = 10

    from models.agent import TaskAssignment, TaskAssignmentState, Agent

    now = datetime.utcnow()
    non_terminal = [TaskStatus.TODO, TaskStatus.IN_PROGRESS, TaskStatus.REVIEW, TaskStatus.BLOCKED]

    # Find overdue tasks with active assignments
    overdue_tasks = (
        Task.query
        .join(Project)
        .filter(
            Project.owner_id == user.id,
            Task.due_date.isnot(None),
            Task.due_date < now,
            Task.status.in_(non_terminal),
        )
        .with_entities(Task.id, Task.priority, Task.due_date)
        .all()
    )

    overdue_ids = [t.id for t in overdue_tasks]
    if not overdue_ids:
        return ApiResponse.success({"items": [], "total_overdue": 0}).to_response()

    # Map task_id -> (priority, due_date)
    task_meta = {t.id: (t.priority, t.due_date) for t in overdue_tasks}

    # Find active assignments for these overdue tasks
    assignments = (
        TaskAssignment.query
        .filter(
            TaskAssignment.task_id.in_(overdue_ids),
            TaskAssignment.state.in_([TaskAssignmentState.ASSIGNED, TaskAssignmentState.CLAIMED]),
        )
        .with_entities(TaskAssignment.task_id, TaskAssignment.agent_id)
        .all()
    )

    # Resolve agent names
    agent_ids = list(set(a.agent_id for a in assignments))
    agent_names = {}
    if agent_ids:
        for a in Agent.query.filter(Agent.id.in_(agent_ids)).with_entities(Agent.id, Agent.name).all():
            agent_names[a.id] = a.name or f"Agent#{a.id}"

    buckets = {}  # {agent_id: {count, by_priority, earliest_due}}
    for task_id, agent_id in assignments:
        priority, due_date = task_meta.get(task_id, (None, None))
        b = buckets.get(agent_id)
        if b is None:
            b = {"count": 0, "by_priority": {}, "earliest_due": None}
            buckets[agent_id] = b
        b["count"] += 1
        p = priority or "unknown"
        b["by_priority"][p] = b["by_priority"].get(p, 0) + 1
        if due_date and (b["earliest_due"] is None or due_date < b["earliest_due"]):
            b["earliest_due"] = due_date

    items = []
    for aid, b in buckets.items():
        items.append({
            "agent_id": aid,
            "name": agent_names.get(aid, f"Agent#{aid}"),
            "overdue": b["count"],
            "by_priority": b["by_priority"],
            "earliest_due": b["earliest_due"].isoformat() if b["earliest_due"] else None,
        })
    items.sort(key=lambda x: x["overdue"], reverse=True)

    return ApiResponse.success({
        "items": items[:limit],
        "total_overdue": len(overdue_ids),
    }).to_response()


@tasks_bp.route('/overdue-clustering', methods=['GET'])
@unified_auth_required
def task_overdue_clustering():
    """Overdue task clustering analysis by project and priority for the current user.

    Groups overdue tasks (due_date < now, status not done/cancelled) by
    project_id and priority. Per cluster: project name, priority, count,
    avg days overdue, and representative task names. Sorted by count
    descending. Reveals where overdue tasks concentrate and why.
    """
    user = get_current_user()
    try:
        limit = max(1, min(30, int(request.args.get("limit", 15))))
    except (TypeError, ValueError):
        limit = 15

    now = datetime.utcnow()
    non_terminal = [TaskStatus.TODO, TaskStatus.IN_PROGRESS, TaskStatus.REVIEW, TaskStatus.BLOCKED]

    overdue_tasks = (
        Task.query
        .join(Project)
        .filter(
            Project.owner_id == user.id,
            Task.due_date.isnot(None),
            Task.due_date < now,
            Task.status.in_(non_terminal),
        )
        .with_entities(
            Task.id, Task.title, Task.priority, Task.due_date,
            Task.project_id, Project.name,
        )
        .all()
    )

    if not overdue_tasks:
        return ApiResponse.success({"clusters": [], "total_overdue": 0}).to_response()

    # Group by (project_id, priority)
    cluster_data: dict = {}  # {(pid, priority): {name, count, overdue_days_sum, titles}}
    total_overdue = 0
    for tid, title, priority, due_date, pid, pname in overdue_tasks:
        p = priority.value if priority else "unknown"
        key = (pid, p)
        if key not in cluster_data:
            cluster_data[key] = {
                "project_id": pid, "project_name": pname or f"Project#{pid}",
                "priority": p, "count": 0, "overdue_days_sum": 0.0, "titles": [],
            }
        cluster_data[key]["count"] += 1
        total_overdue += 1
        days_overdue = (now - due_date).total_seconds() / 86400 if due_date else 0
        cluster_data[key]["overdue_days_sum"] += days_overdue
        if title and len(cluster_data[key]["titles"]) < 3:
            cluster_data[key]["titles"].append(title[:60])

    clusters = []
    for key, d in sorted(cluster_data.items(), key=lambda kv: kv[1]["count"], reverse=True)[:limit]:
        clusters.append({
            "project_id": d["project_id"],
            "project_name": d["project_name"],
            "priority": d["priority"],
            "count": d["count"],
            "avg_days_overdue": round(d["overdue_days_sum"] / d["count"], 1) if d["count"] else 0,
            "titles": d["titles"],
        })

    return ApiResponse.success({
        "clusters": clusters,
        "total_overdue": total_overdue,
    }).to_response()


@tasks_bp.route('/completion-by-project', methods=['GET'])
@unified_auth_required
def task_completion_by_project():
    """Daily task completion trend grouped by project for the current user.

    Buckets done tasks (state=DONE, completed_at within window) by calendar
    day of completed_at and project_id. Returns a per-project series plus
    per-project totals, sorted by total completed descending. Reveals which
    projects are actively delivering over time.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
        limit = max(1, min(20, int(request.args.get("limit", 8))))
    except (TypeError, ValueError):
        days = 30
        limit = 8

    since = datetime.utcnow() - timedelta(days=days)

    rows = (
        Task.query
        .join(Project)
        .filter(
            Project.owner_id == user.id,
            Task.status == TaskStatus.DONE,
            Task.completed_at.isnot(None),
            Task.completed_at >= since,
        )
        .with_entities(
            func.date(Task.completed_at).label("d"),
            Task.project_id,
            Project.name,
            func.count(Task.id),
        )
        .group_by(func.date(Task.completed_at), Task.project_id, Project.name)
        .all()
    )

    proj_meta: dict = {}  # {project_id: name}
    proj_totals: dict = {}  # {project_id: total}
    by_day_proj: dict = {}  # {date: {project_id: count}}
    for d, pid, pname, c in rows:
        if not d:
            continue
        key = str(d)
        proj_meta[pid] = pname or f"Project#{pid}"
        proj_totals[pid] = proj_totals.get(pid, 0) + c
        by_day_proj.setdefault(key, {})[pid] = c

    # 按 total 降序取 top N
    top = sorted(proj_totals.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    top_ids = [pid for pid, _ in top]

    # 构建每个 top 项目的每日序列
    all_days = sorted(by_day_proj.keys())
    series = []
    for pid, total in top:
        daily = [{"date": d, "done": (by_day_proj.get(d, {}) or {}).get(pid, 0)} for d in all_days]
        series.append({
            "project_id": pid,
            "name": proj_meta.get(pid, f"Project#{pid}"),
            "total": total,
            "daily": daily,
        })

    return ApiResponse.success({
        "days": days,
        "total_done": sum(proj_totals.values()),
        "all_days": all_days,
        "series": series,
    }).to_response()


@tasks_bp.route("/completion-by-assignee", methods=["GET"])
@unified_auth_required
def task_completion_by_assignee():
    """Daily task completion trend grouped by assignee (Agent) for the current user.

    Buckets done assignments (state=DONE, completed_at within window) by calendar
    day of completed_at and agent_id. Returns a per-agent series plus per-agent
    totals, sorted by total completed descending. Reveals which agents are
    actively delivering over time.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
        limit = max(1, min(20, int(request.args.get("limit", 8))))
    except (TypeError, ValueError):
        days = 30
        limit = 8

    since = datetime.utcnow() - timedelta(days=days)

    from models.agent import TaskAssignment, TaskAssignmentState, Agent

    rows = (
        TaskAssignment.query
        .join(Agent)
        .filter(
            Agent.owner_id == user.id,
            TaskAssignment.state == TaskAssignmentState.DONE,
            TaskAssignment.completed_at.isnot(None),
            TaskAssignment.completed_at >= since,
        )
        .with_entities(
            func.date(TaskAssignment.completed_at).label("d"),
            TaskAssignment.agent_id,
            Agent.name,
            func.count(TaskAssignment.id),
        )
        .group_by(func.date(TaskAssignment.completed_at), TaskAssignment.agent_id, Agent.name)
        .all()
    )

    agent_meta: dict = {}   # {agent_id: name}
    agent_totals: dict = {}  # {agent_id: total}
    by_day_agent: dict = {}  # {date: {agent_id: count}}
    for d, aid, aname, c in rows:
        if not d:
            continue
        key = str(d)
        agent_meta[aid] = aname or f"Agent#{aid}"
        agent_totals[aid] = agent_totals.get(aid, 0) + c
        by_day_agent.setdefault(key, {})[aid] = c

    top = sorted(agent_totals.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    top_ids = [aid for aid, _ in top]

    all_days = sorted(by_day_agent.keys())
    series = []
    for aid, total in top:
        daily = [{"date": d, "done": (by_day_agent.get(d, {}) or {}).get(aid, 0)} for d in all_days]
        series.append({
            "agent_id": aid,
            "name": agent_meta.get(aid, f"Agent#{aid}"),
            "total": total,
            "daily": daily,
        })

    return ApiResponse.success({
        "days": days,
        "total_done": sum(agent_totals.values()),
        "all_days": all_days,
        "series": series,
    }).to_response()


@tasks_bp.route("/completion-by-priority", methods=["GET"])
@unified_auth_required
def task_completion_by_priority():
    """Task completion rate by priority for the current user's projects.

    Groups tasks by priority and reports total, done, cancelled, and
    completion rate. Reveals whether high-priority tasks are being
    delivered at a comparable rate to low-priority ones.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
    except (TypeError, ValueError):
        days = 30

    since = datetime.utcnow() - timedelta(days=days)
    project_ids = [p.id for p in Project.query.filter_by(owner_id=user.id).with_entities(Project.id).all()]
    if not project_ids:
        return ApiResponse.success({"priorities": [], "total": 0}).to_response()

    from models.agent import TaskAssignment, TaskAssignmentState

    # Get all tasks in window by priority
    tasks = (
        Task.query
        .filter(
            Task.project_id.in_(project_ids),
            Task.created_at >= since,
        )
        .with_entities(
            Task.priority,
            Task.status,
            func.count(Task.id),
        )
        .group_by(Task.priority, Task.status)
        .all()
    )

    priority_data: dict = {}  # {priority: {total, done, cancelled, ...}}
    total_count = 0
    for priority, status, count in tasks:
        p = priority.value if priority else "unknown"
        if p not in priority_data:
            priority_data[p] = {"total": 0, "done": 0, "cancelled": 0, "in_progress": 0, "other": 0}
        priority_data[p]["total"] += count
        total_count += count
        s = status.value if status else ""
        if s == "done":
            priority_data[p]["done"] += count
        elif s == "cancelled":
            priority_data[p]["cancelled"] += count
        elif s == "in_progress":
            priority_data[p]["in_progress"] += count
        else:
            priority_data[p]["other"] += count

    priorities = []
    for p, d in sorted(priority_data.items(), key=lambda kv: kv[1]["total"], reverse=True):
        total = d["total"]
        priorities.append({
            "priority": p,
            "total": total,
            "done": d["done"],
            "cancelled": d["cancelled"],
            "in_progress": d["in_progress"],
            "completion_rate": round(d["done"] / total * 100, 1) if total else 0.0,
        })

    return ApiResponse.success({"priorities": priorities, "total": total_count}).to_response()


@tasks_bp.route('/completion-rate-by-project', methods=['GET'])
@unified_auth_required
def task_completion_rate_by_project():
    """Task completion rate snapshot comparison across projects.

    Groups tasks by project and reports total, done, in_progress, cancelled,
    and completion_rate. Sorted by total descending, limited to top N projects.
    Reveals which projects have the best/worst delivery rates.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
        limit = max(1, min(30, int(request.args.get("limit", 10))))
    except (TypeError, ValueError):
        days = 30
        limit = 10

    since = datetime.utcnow() - timedelta(days=days)
    project_ids = [p.id for p in Project.query.filter_by(owner_id=user.id).with_entities(Project.id).all()]
    if not project_ids:
        return ApiResponse.success({"projects": [], "total_tasks": 0, "total_done": 0}).to_response()

    rows = (
        Task.query
        .join(Project)
        .filter(
            Task.project_id.in_(project_ids),
            Task.created_at >= since,
        )
        .with_entities(
            Task.project_id,
            Project.name,
            Task.status,
            func.count(Task.id),
        )
        .group_by(Task.project_id, Project.name, Task.status)
        .all()
    )

    proj_data: dict = {}  # {project_id: {name, total, done, cancelled, in_progress, other}}
    total_tasks = 0
    total_done = 0
    for pid, pname, status, count in rows:
        if pid not in proj_data:
            proj_data[pid] = {"name": pname or f"Project#{pid}", "total": 0, "done": 0, "cancelled": 0, "in_progress": 0, "other": 0}
        proj_data[pid]["total"] += count
        total_tasks += count
        s = status.value if status else ""
        if s == "done":
            proj_data[pid]["done"] += count
            total_done += count
        elif s == "cancelled":
            proj_data[pid]["cancelled"] += count
        elif s == "in_progress":
            proj_data[pid]["in_progress"] += count
        else:
            proj_data[pid]["other"] += count

    projects = []
    for pid, d in sorted(proj_data.items(), key=lambda kv: kv[1]["total"], reverse=True)[:limit]:
        total = d["total"]
        projects.append({
            "project_id": pid,
            "name": d["name"],
            "total": total,
            "done": d["done"],
            "cancelled": d["cancelled"],
            "in_progress": d["in_progress"],
            "completion_rate": round(d["done"] / total * 100, 1) if total else 0.0,
        })

    return ApiResponse.success({"projects": projects, "total_tasks": total_tasks, "total_done": total_done}).to_response()


@tasks_bp.route('/priority-trend', methods=['GET'])
@unified_auth_required
def task_priority_trend():
    """Daily task priority distribution trend for the current user.

    Groups tasks by created_at date and priority (critical/high/medium/low),
    returning per-day counts per priority level over the last N days.
    Reveals how the task priority mix shifts over time.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
    except (TypeError, ValueError):
        days = 30

    since = datetime.utcnow() - timedelta(days=days)

    rows = (
        Task.query
        .filter(Task.owner_id == user.id, Task.created_at >= since)
        .with_entities(
            func.date(Task.created_at).label("d"),
            Task.priority,
            func.count().label("cnt"),
        )
        .group_by(func.date(Task.created_at), Task.priority)
        .order_by(func.date(Task.created_at))
        .all()
    )

    priority_keys = ["critical", "high", "medium", "low"]
    trend: dict = {}  # {date_str: {priority: count}}
    for d, pri, cnt in rows:
        ds = d.isoformat() if d else None
        if not ds:
            continue
        bucket = trend.setdefault(ds, {})
        p = pri.value if hasattr(pri, "value") else str(pri)
        bucket[p] = cnt

    # Build full date range
    date_range = []
    cur = since.date() + timedelta(days=1)
    end = datetime.utcnow().date()
    while cur <= end:
        date_range.append(cur.isoformat())
        cur += timedelta(days=1)

    # Fill gaps
    out = []
    for ds in date_range:
        b = trend.get(ds, {})
        out.append({
            "date": ds,
            "critical": b.get("critical", 0),
            "high": b.get("high", 0),
            "medium": b.get("medium", 0),
            "low": b.get("low", 0),
        })

    totals = {k: sum(d[k] for d in out) for k in priority_keys}

    return ApiResponse.success({
        "days": days,
        "trend": out,
        "totals": totals,
    }).to_response()


@tasks_bp.route('/completion-forecast', methods=['GET'])
@unified_auth_required
def task_completion_forecast():
    """Task completion forecast based on historical velocity.

    Computes daily completion velocity (done tasks per day) over the
    lookback window, then extrapolates to estimate when all remaining
    non-done tasks will be completed. Also provides per-priority
    breakdown of remaining counts and estimated completion dates.
    """
    user = get_current_user()
    try:
        days = max(7, min(365, int(request.args.get("days", 30))))
    except (TypeError, ValueError):
        days = 30

    since = datetime.utcnow() - timedelta(days=days)

    # Count done tasks per day in window
    done_rows = (
        Task.query
        .filter(Task.owner_id == user.id, Task.status == TaskStatus.DONE, Task.updated_at >= since)
        .with_entities(func.date(Task.updated_at).label("d"), func.count().label("cnt"))
        .group_by(func.date(Task.updated_at))
        .all()
    )

    # Calculate velocity
    total_done_in_window = sum(r.cnt for r in done_rows)
    velocity = total_done_in_window / days  # tasks/day

    # Count remaining tasks by status and priority
    remaining = (
        Task.query
        .filter(Task.owner_id == user.id, ~Task.status.in_([TaskStatus.DONE, TaskStatus.CANCELLED]))
        .with_entities(Task.status, Task.priority, func.count().label("cnt"))
        .group_by(Task.status, Task.priority)
        .all()
    )

    total_remaining = sum(r.cnt for r in remaining)
    priority_remaining: dict = {}
    for status, pri, cnt in remaining:
        p = pri.value if hasattr(pri, "value") else str(pri)
        priority_remaining.setdefault(p, {"remaining": 0})
        priority_remaining[p]["remaining"] += cnt

    # Estimate completion date
    if velocity > 0 and total_remaining > 0:
        days_to_complete = total_remaining / velocity
        estimated_date = (datetime.utcnow() + timedelta(days=days_to_complete)).strftime("%Y-%m-%d")
    else:
        days_to_complete = None
        estimated_date = None

    # Per-priority estimated dates (proportional share of velocity)
    priority_forecast = []
    priority_order = ["critical", "high", "medium", "low"]
    cum_days = 0.0
    for p in priority_order:
        pr = priority_remaining.get(p, {})
        rem = pr.get("remaining", 0)
        if rem > 0 and velocity > 0:
            days_for_p = rem / velocity
            cum_days += days_for_p
            est = (datetime.utcnow() + timedelta(days=cum_days)).strftime("%Y-%m-%d")
        else:
            days_for_p = 0
            est = None
        priority_forecast.append({
            "priority": p,
            "remaining": rem,
            "estimated_days": round(days_for_p, 1) if days_for_p else 0,
            "estimated_date": est,
        })

    return ApiResponse.success({
        "days": days,
        "velocity": round(velocity, 2),
        "total_done_in_window": total_done_in_window,
        "total_remaining": total_remaining,
        "days_to_complete": round(days_to_complete, 1) if days_to_complete else None,
        "estimated_completion_date": estimated_date,
        "priority_forecast": priority_forecast,
    }).to_response()


@tasks_bp.route("/dependency-chain", methods=["GET"])
@unified_auth_required
def task_dependency_chain():
    """Analyze task dependency chains for the current user.

    Finds tasks with subtask relationships and builds dependency chains.
    Returns per-chain: root task, depth, total tasks, completion progress.

    Query params:
    - project_id: optional project filter
    - limit: max chains returned (1-20, default 10)
    """
    user = get_current_user()
    try:
        limit = max(1, min(20, int(request.args.get("limit", 10))))
    except (TypeError, ValueError):
        limit = 10
    project_id = request.args.get("project_id", type=int)

    from models.agent import Task
    q = Task.query.filter(Task.owner_id == user.id, Task.parent_id == None)
    if project_id:
        q = q.filter(Task.project_id == project_id)

    root_tasks = q.order_by(Task.created_at.desc()).limit(limit * 3).all()

    chains = []
    for root in root_tasks:
        # BFS to find all descendants
        visited = set()
        queue = [root.id]
        all_ids = [root.id]
        max_depth = 0
        depth_map = {root.id: 0}
        while queue:
            tid = queue.pop(0)
            if tid in visited:
                continue
            visited.add(tid)
            children = Task.query.filter_by(parent_id=tid).all()
            for child in children:
                if child.id not in visited:
                    all_ids.append(child.id)
                    depth_map[child.id] = depth_map[tid] + 1
                    max_depth = max(max_depth, depth_map[child.id])
                    queue.append(child.id)

        if len(all_ids) < 2:
            continue

        # Count completed
        all_tasks = Task.query.filter(Task.id.in_(all_ids)).all()
        completed = sum(1 for t in all_tasks if t.status and t.status.value == "done")
        in_progress = sum(1 for t in all_tasks if t.status and t.status.value == "in_progress")

        chains.append({
            "root_id": root.id,
            "root_title": root.title or f"Task#{root.id}",
            "depth": max_depth,
            "total_tasks": len(all_ids),
            "completed": completed,
            "in_progress": in_progress,
            "progress_pct": round(completed / len(all_ids) * 100, 1) if all_ids else 0.0,
        })

    chains.sort(key=lambda c: c["total_tasks"], reverse=True)
    return ApiResponse.success({"chains": chains[:limit]}).to_response()


@tasks_bp.route("/comment-sentiment-trend", methods=["GET"])
@unified_auth_required
def task_comment_sentiment_trend():
    """Task comment sentiment trend.

    Aggregates comment events by day and classifies sentiment
    based on keyword matching.

    Positive: 完成/成功/好/赞/解决/通过
    Negative: 失败/问题/bug/错/崩溃/超时/拒绝
    Neutral: everything else

    Query params:
    - days: lookback window (1-90, default 30)
    """
    user = get_current_user()
    try:
        days = max(1, min(90, int(request.args.get("days", 30))))
    except (TypeError, ValueError):
        days = 30

    since = datetime.utcnow() - timedelta(days=days)

    from models.agent import TaskEvent
    comments = (
        TaskEvent.query
        .filter(
            TaskEvent.owner_id == user.id,
            TaskEvent.event_type == "comment",
            TaskEvent.created_at >= since,
        )
        .with_entities(
            func.date(TaskEvent.created_at).label("event_date"),
            TaskEvent.content,
        )
        .all()
    )

    positive_words = {"完成", "成功", "好", "赞", "解决", "通过", "修复", "合并", "上线", "搞定"}
    negative_words = {"失败", "问题", "bug", "错", "崩溃", "超时", "拒绝", "阻塞", "错误", "异常", "报错"}

    day_data = {}  # date -> {positive, negative, neutral}
    for event_date, content in comments:
        date_str = event_date.isoformat() if hasattr(event_date, 'isoformat') else str(event_date)
        if date_str not in day_data:
            day_data[date_str] = {"positive": 0, "negative": 0, "neutral": 0}

        if not content:
            day_data[date_str]["neutral"] += 1
            continue

        text_lower = content.lower()
        has_pos = any(w in text_lower for w in positive_words)
        has_neg = any(w in text_lower for w in negative_words)

        if has_neg and not has_pos:
            day_data[date_str]["negative"] += 1
        elif has_pos and not has_neg:
            day_data[date_str]["positive"] += 1
        else:
            day_data[date_str]["neutral"] += 1

    # Build full date range
    date_range = []
    for i in range(days):
        d = (datetime.utcnow() - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
        date_range.append(d)

    trend = []
    for d in date_range:
        data = day_data.get(d, {"positive": 0, "negative": 0, "neutral": 0})
        trend.append({
            "date": d,
            "positive": data["positive"],
            "negative": data["negative"],
            "neutral": data["neutral"],
        })

    return ApiResponse.success({"trend": trend, "days": days}).to_response()
