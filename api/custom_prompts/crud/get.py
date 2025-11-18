@custom_prompts_bp.route('/<int:prompt_id>', methods=['GET'])
@unified_auth_required
def get_custom_prompt(prompt_id):
    """获取单个自定义提示词详情"""
    try:
        current_user = get_current_user()
        
        prompt = CustomPrompt.query.filter(
            CustomPrompt.id == prompt_id,
            CustomPrompt.user_id == current_user.id
        ).first()
        
        if not prompt:
            return ApiResponse.error("Custom prompt not found", 404).to_response()
        
        return ApiResponse.success(prompt.to_dict(), "Custom prompt retrieved successfully").to_response()
        
    except Exception as e:
        return handle_api_error(e, "Failed to retrieve custom prompt")


