@custom_prompts_bp.route('', methods=['POST'])
@unified_auth_required
def create_custom_prompt():
    """创建新的自定义提示词"""
    try:
        current_user = get_current_user()
        data = validate_json_request(
            required_fields=['prompt_type', 'name', 'content'],
            optional_fields=['description', 'order_index']
        )

        if isinstance(data, tuple):  # 错误响应
            return data
        
        # 验证prompt_type
        try:
            prompt_type = PromptType(data['prompt_type'])
        except ValueError:
            return ApiResponse.error("Invalid prompt_type. Must be 'project' or 'task_button'", 400).to_response()
        
        # 验证名称长度
        if len(data['name'].strip()) == 0:
            return ApiResponse.error("Name cannot be empty", 400).to_response()
        
        if len(data['name']) > 255:
            return ApiResponse.error("Name too long (max 255 characters)", 400).to_response()
        
        # 检查同类型下名称是否重复
        existing = CustomPrompt.query.filter(
            CustomPrompt.user_id == current_user.id,
            CustomPrompt.prompt_type == prompt_type,
            CustomPrompt.name == data['name'].strip()
        ).first()
        
        if existing:
            return ApiResponse.error(f"A {prompt_type.value} prompt with this name already exists", 400).to_response()
        
        # 获取排序索引
        order_index = data.get('order_index', 0)
        if prompt_type == PromptType.TASK_BUTTON and order_index == 0:
            # 为任务按钮自动分配排序索引
            max_order = db.session.query(db.func.max(CustomPrompt.order_index)).filter(
                CustomPrompt.user_id == current_user.id,
                CustomPrompt.prompt_type == PromptType.TASK_BUTTON
            ).scalar() or 0
            order_index = max_order + 1
        
        # 创建提示词
        prompt = CustomPrompt.create_prompt(
            user_id=current_user.id,
            prompt_type=prompt_type,
            name=data['name'].strip(),
            content=data['content'],
            description=data.get('description', '').strip() or None,
            order_index=order_index
        )
        
        db.session.commit()
        
        return ApiResponse.success(
            prompt.to_dict(),
            f"{prompt_type.value.title()} prompt created successfully"
        ).to_response()
        
    except Exception as e:
        db.session.rollback()
        return handle_api_error(e, "Failed to create custom prompt")


