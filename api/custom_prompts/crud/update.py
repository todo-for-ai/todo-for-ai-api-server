@custom_prompts_bp.route('/<int:prompt_id>', methods=['PUT'])
@unified_auth_required
def update_custom_prompt(prompt_id):
    """更新自定义提示词"""
    try:
        current_user = get_current_user()
        data = validate_json_request(
            optional_fields=['name', 'content', 'description', 'is_active', 'order_index']
        )

        if isinstance(data, tuple):  # 错误响应
            return data

        prompt = CustomPrompt.query.filter(
            CustomPrompt.id == prompt_id,
            CustomPrompt.user_id == current_user.id
        ).first()
        
        if not prompt:
            return ApiResponse.error("Custom prompt not found", 404).to_response()
        
        # 验证名称（如果提供）
        if 'name' in data:
            name = data['name'].strip()
            if len(name) == 0:
                return ApiResponse.error("Name cannot be empty", 400).to_response()
            
            if len(name) > 255:
                return ApiResponse.error("Name too long (max 255 characters)", 400).to_response()
            
            # 检查同类型下名称是否重复（排除当前记录）
            existing = CustomPrompt.query.filter(
                CustomPrompt.user_id == current_user.id,
                CustomPrompt.prompt_type == prompt.prompt_type,
                CustomPrompt.name == name,
                CustomPrompt.id != prompt_id
            ).first()
            
            if existing:
                return ApiResponse.error(f"A {prompt.prompt_type.value} prompt with this name already exists", 400).to_response()
        
        # 更新字段
        prompt.update_prompt(
            name=data.get('name', '').strip() or None,
            content=data.get('content'),
            description=data.get('description', '').strip() or None,
            is_active=data.get('is_active'),
            order_index=data.get('order_index')
        )
        
        db.session.commit()
        
        return ApiResponse.success(
            prompt.to_dict(),
            f"{prompt.prompt_type.value.title()} prompt updated successfully"
        ).to_response()
        
    except Exception as e:
        db.session.rollback()
        return handle_api_error(e, "Failed to update custom prompt")


