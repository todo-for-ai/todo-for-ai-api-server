"""
任务详情API - 获取任务详情
"""

def register_routes(bp):
    """注册路由"""
    from datetime import datetime
    from flask import Blueprint, request
    from models import db, Task, TaskStatus, TaskPriority, Project, TaskHistory, ActionType, UserActivity
    from ..base import ApiResponse, paginate_query, validate_json_request, get_request_args, APIException, handle_api_error
    from core.auth import unified_auth_required, get_current_user
    
    @bp.route('/<int:task_id>', methods=['GET'])
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
    
