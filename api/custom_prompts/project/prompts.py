@custom_prompts_bp.route('/project-prompts', methods=['GET'])
@unified_auth_required
def get_project_prompts():
    """获取用户的项目提示词列表"""
    try:
        current_user = get_current_user()
        args = get_request_args()
        
        is_active = args.get('is_active', True)
        prompts = CustomPrompt.get_user_project_prompts(current_user.id, is_active)
        
        result = [prompt.to_dict() for prompt in prompts]
        
        return ApiResponse.success(result, "Project prompts retrieved successfully").to_response()
        
    except Exception as e:
        return handle_api_error(e, "Failed to retrieve project prompts")


@custom_prompts_bp.route('/project-prompts/<int:prompt_id>/preview', methods=['POST'])
@unified_auth_required
def preview_project_prompt(prompt_id):
    """预览项目提示词（使用真实或示例数据）"""
    try:
        current_user = get_current_user()
        data = validate_json_request(
            optional_fields=['project_id']
        ) if request.is_json else {}

        # 获取提示词
        prompt = CustomPrompt.query.filter(
            CustomPrompt.id == prompt_id,
            CustomPrompt.user_id == current_user.id,
            CustomPrompt.prompt_type == PromptType.PROJECT
        ).first()

        if not prompt:
            return ApiResponse.error("Project prompt not found", 404).to_response()

        # 获取项目ID（可选）
        project_id = data.get('project_id')

        # TODO: 实现模板渲染逻辑
        # 这里应该使用模板引擎（如Jinja2）来渲染提示词模板
        # 暂时返回原始内容和基本信息

        preview_data = {
            'prompt_id': prompt_id,
            'prompt_name': prompt.name,
            'raw_content': prompt.content,
            'rendered_content': prompt.content,  # TODO: 实际渲染
            'project_id': project_id,
            'preview_generated_at': db.func.now()
        }

        return ApiResponse.success(preview_data, "Project prompt preview generated successfully").to_response()

    except Exception as e:
        return handle_api_error(e, "Failed to preview project prompt")


