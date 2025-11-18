@custom_prompts_bp.route('/task-button-prompts', methods=['GET'])
@unified_auth_required
def get_task_button_prompts():
    """获取用户的任务按钮提示词列表"""
    try:
        current_user = get_current_user()
        args = get_request_args()
        
        is_active = args.get('is_active', True)
        prompts = CustomPrompt.get_user_task_button_prompts(current_user.id, is_active)
        
        result = [prompt.to_dict() for prompt in prompts]
        
        return ApiResponse.success(result, "Task button prompts retrieved successfully").to_response()
        
    except Exception as e:
        return handle_api_error(e, "Failed to retrieve task button prompts")


@custom_prompts_bp.route('/task-buttons/reorder', methods=['PUT'])
@unified_auth_required
def reorder_task_button_prompts():
    """重新排序任务按钮提示词"""
    try:
        current_user = get_current_user()
        data = validate_json_request(
            required_fields=['prompt_orders']
        )

        if isinstance(data, tuple):  # 错误响应
            return data

        prompt_orders = data['prompt_orders']
        if not isinstance(prompt_orders, list):
            return ApiResponse.error("prompt_orders must be a list", 400).to_response()

        # 验证数据格式
        for order_data in prompt_orders:
            if not isinstance(order_data, dict) or 'id' not in order_data or 'order_index' not in order_data:
                return ApiResponse.error("Each item in prompt_orders must have 'id' and 'order_index'", 400).to_response()

        # 执行重排序
        CustomPrompt.reorder_task_buttons(current_user.id, prompt_orders)
        db.session.commit()

        return ApiResponse.success(None, "Task button prompts reordered successfully").to_response()

    except Exception as e:
        db.session.rollback()
        return handle_api_error(e, "Failed to reorder task button prompts")


