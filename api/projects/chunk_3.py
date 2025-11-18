def restore_project(project_id):
    """恢复项目"""
    try:
        project = Project.query.get(project_id)
        if not project:
            return ApiResponse.error("Project not found", 404, error_details={"code": "PROJECT_NOT_FOUND"}).to_response()
        
        project.restore()
        
        return ApiResponse.success(
            project.to_dict(),
            "Project restored successfully"
        ).to_response()
        
    except Exception as e:
        db.session.rollback()
        return api_error(f"Failed to restore project: {str(e)}", 500)


@projects_bp.route('/<int:project_id>/tasks', methods=['GET'])

def get_project_tasks(project_id):
    """获取项目的任务列表"""
    try:
        project = Project.query.get(project_id)
        if not project:
            return ApiResponse.error("Project not found", 404, error_details={"code": "PROJECT_NOT_FOUND"}).to_response()
        
        args = get_request_args()
        
        # 构建任务查询
        query = project.tasks
        
        # 状态筛选
        if args['status']:
            from models import TaskStatus
            try:
                status = TaskStatus(args['status'])
                query = query.filter_by(status=status)
            except ValueError:
                return api_error(f"Invalid status: {args['status']}", 400)
        
        # 优先级筛选
        if args['priority']:
            from models import TaskPriority
            try:
                priority = TaskPriority(args['priority'])
                query = query.filter_by(priority=priority)
            except ValueError:
                return api_error(f"Invalid priority: {args['priority']}", 400)
        
        # 分配者筛选
        if args['assignee']:
            query = query.filter_by(assignee=args['assignee'])
        
        # 搜索
        if args['search']:
            from models import Task
            search_term = f"%{args['search']}%"
            query = query.filter(
                Task.title.like(search_term) |
                Task.description.like(search_term) |
                Task.content.like(search_term)
            )
        
        # 分页
        result = paginate_query(query, args['page'], args['per_page'])
        
        return ApiResponse.success(result, "Project tasks retrieved successfully").to_response()
        
    except Exception as e:
        return api_error(f"Failed to retrieve project tasks: {str(e)}", 500)


@projects_bp.route('/<int:project_id>/context-rules', methods=['GET'])

def get_project_context_rules(project_id):
    """获取项目的上下文规则"""
    try:
        project = Project.query.get(project_id)
        if not project:
            return ApiResponse.error("Project not found", 404, error_details={"code": "PROJECT_NOT_FOUND"}).to_response()

        rules = project.get_active_context_rules()

        return ApiResponse.success(
            [rule.to_dict() for rule in rules],
            "Project context rules retrieved successfully"
        ).to_response()

    except Exception as e:
        return api_error(f"Failed to retrieve project context rules: {str(e)}", 500)

