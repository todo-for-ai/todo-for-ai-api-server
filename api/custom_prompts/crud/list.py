@custom_prompts_bp.route('', methods=['GET'])
@unified_auth_required
def get_custom_prompts():
    """获取用户的自定义提示词列表"""
    try:
        current_user = get_current_user()
        args = get_request_args()

        # 获取查询参数
        prompt_type = args.get('prompt_type')  # 'project' 或 'task_button'
        is_active = args.get('is_active', True)  # 默认只返回激活的

        # 构建查询
        query = CustomPrompt.query.filter(CustomPrompt.user_id == current_user.id)
        
        if prompt_type:
            try:
                prompt_type_enum = PromptType(prompt_type)
                query = query.filter(CustomPrompt.prompt_type == prompt_type_enum)
            except ValueError:
                return ApiResponse.error("Invalid prompt_type. Must be 'project' or 'task_button'", 400).to_response()
        
        if is_active is not None:
            query = query.filter(CustomPrompt.is_active == is_active)
        
        # 排序：按order_index升序，然后按创建时间降序
        query = query.order_by(CustomPrompt.order_index.asc(), CustomPrompt.created_at.desc())
        
        # 手动分页
        page = args['page']
        per_page = min(args['per_page'], 100)  # 限制最大每页数量

        # 执行分页查询
        pagination = query.paginate(
            page=page,
            per_page=per_page,
            error_out=False
        )

        result = {
            'items': [prompt.to_dict() for prompt in pagination.items],
            'pagination': {
                'page': pagination.page,
                'per_page': pagination.per_page,
                'total': pagination.total,
                'pages': pagination.pages,
                'has_prev': pagination.has_prev,
                'has_next': pagination.has_next,
                'prev_num': pagination.prev_num,
                'next_num': pagination.next_num
            }
        }

        return ApiResponse.success(result, "Custom prompts retrieved successfully").to_response()
        
    except Exception as e:
        return handle_api_error(e, "Failed to retrieve custom prompts")


