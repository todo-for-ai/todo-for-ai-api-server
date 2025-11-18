"""
批量任务操作API - 批量删除、更新状态、更新优先级
"""

def register_routes(bp):
    """注册路由"""
    from datetime import datetime
    from flask import Blueprint, request
    from models import db, Task, TaskStatus, TaskPriority, Project, TaskHistory, ActionType, UserActivity
    from ..base import ApiResponse, paginate_query, validate_json_request, get_request_args, APIException, handle_api_error
    from core.auth import unified_auth_required, get_current_user
    
    @bp.route('/batch/delete', methods=['POST'])
    @unified_auth_required
    def batch_delete_tasks():
        """批量删除任务"""
        try:
            current_user = get_current_user()
            data = validate_json_request(required_fields=['task_ids'])
            task_ids = data.get('task_ids', [])
            
            if not task_ids:
                return ApiResponse.error("No task IDs provided", 400).to_response()
            
            if not isinstance(task_ids, list):
                return ApiResponse.error("task_ids must be a list", 400).to_response()
            
            # 验证任务是否存在并检查权限
            tasks = Task.query.filter(Task.id.in_(task_ids)).all()
            
            if len(tasks) != len(task_ids):
                return ApiResponse.error("Some tasks not found", 404).to_response()
            
            # 权限检查 - 只能删除自己项目中的任务
            for task in tasks:
                if task.project.owner_id != current_user.id:
                    return ApiResponse.error(
                        f"Access denied: You can only delete tasks from your own projects. Task ID: {task.id}", 
                        403
                    ).to_response()
            
            # 执行批量删除
            deleted_count = 0
            deleted_tasks = []
            
            for task in tasks:
                # 记录任务历史
                history = TaskHistory(
                    task_id=task.id,
                    action=ActionType.DELETE,
                    old_values={
                        'title': task.title,
                        'content': task.content,
                        'status': task.status.value,
                        'priority': task.priority.value
                    },
                    new_values={},
                    user_identifier=current_user.username,
                    user_type='user'
                )
                db.session.add(history)
                
                # 记录用户活动
                activity = UserActivity(
                    user_id=current_user.id,
                    activity_type='task_delete',
                    description=f'Deleted task: {task.title}',
                    metadata={'task_id': task.id, 'project_id': task.project_id}
                )
                db.session.add(activity)
                
                deleted_tasks.append({
                    'id': task.id,
                    'title': task.title
                })
                
                db.session.delete(task)
                deleted_count += 1
            
            db.session.commit()
            
            return ApiResponse.success({
                'deleted_count': deleted_count,
                'deleted_tasks': deleted_tasks
            }, f"Successfully deleted {deleted_count} tasks").to_response()
            
        except Exception as e:
            db.session.rollback()
            return handle_api_error(e, "Failed to batch delete tasks")
