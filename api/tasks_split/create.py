"""
创建任务API - 创建新任务
"""

def register_routes(bp):
    """注册路由"""
    from datetime import datetime
    from flask import Blueprint, request
    from models import db, Task, TaskStatus, TaskPriority, Project, TaskHistory, ActionType, UserActivity
    from ..base import ApiResponse, paginate_query, validate_json_request, get_request_args, APIException, handle_api_error
    from core.auth import unified_auth_required, get_current_user
    
    @bp.route('', methods=['POST'])
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
                    'due_date', 'tags', 'is_ai_task', 'related_files'
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
                related_files=data.get('related_files', []),
                creator_id=current_user.id,  # 设置创建者ID
                created_by=current_user.email  # 设置创建者邮箱
            )
    
            # 更新项目最后活动时间
            project.last_activity_at = datetime.utcnow()
    
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
    
