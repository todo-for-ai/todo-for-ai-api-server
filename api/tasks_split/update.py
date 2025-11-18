"""
更新任务API - 更新任务信息
"""

def register_routes(bp):
    """注册路由"""
    from datetime import datetime
    from flask import Blueprint, request
    from models import db, Task, TaskStatus, TaskPriority, Project, TaskHistory, ActionType, UserActivity
    from ..base import ApiResponse, paginate_query, validate_json_request, get_request_args, APIException, handle_api_error
    from core.auth import unified_auth_required, get_current_user
    
    @bp.route('/<int:task_id>', methods=['PUT'])
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
                    'due_date', 'completion_rate', 'tags', 'is_ai_task', 'related_files'
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
            simple_fields = ['title', 'content', 'completion_rate', 'tags', 'is_ai_task', 'related_files']
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
    
