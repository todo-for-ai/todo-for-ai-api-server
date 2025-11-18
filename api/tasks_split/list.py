"""
任务列表API - 获取任务列表
"""

def register_routes(bp):
    """注册路由"""
    from datetime import datetime
    from flask import Blueprint, request
    from models import db, Task, TaskStatus, TaskPriority, Project, TaskHistory, ActionType, UserActivity
    from ..base import ApiResponse, paginate_query, validate_json_request, get_request_args, APIException, handle_api_error
    from core.auth import unified_auth_required, get_current_user
    
    @bp.route('', methods=['GET'])
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
    
