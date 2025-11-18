@custom_prompts_bp.route('/<int:prompt_id>', methods=['DELETE'])
@unified_auth_required
def delete_custom_prompt(prompt_id):
    """删除自定义提示词"""
    try:
        current_user = get_current_user()
        
        prompt = CustomPrompt.query.filter(
            CustomPrompt.id == prompt_id,
            CustomPrompt.user_id == current_user.id
        ).first()
        
        if not prompt:
            return ApiResponse.error("Custom prompt not found", 404).to_response()
        
        prompt_type = prompt.prompt_type.value
        db.session.delete(prompt)
        db.session.commit()
        
        return ApiResponse.success(
            None,
            f"{prompt_type.title()} prompt deleted successfully"
        ).to_response()
        
    except Exception as e:
        db.session.rollback()
        return handle_api_error(e, "Failed to delete custom prompt")


