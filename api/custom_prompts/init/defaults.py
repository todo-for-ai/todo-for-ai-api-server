@custom_prompts_bp.route('/initialize-defaults', methods=['POST'])
@unified_auth_required
def initialize_user_defaults():
    """为当前用户初始化默认的提示词"""
    try:
        current_user = get_current_user()
        data = request.get_json() or {}

        # 获取语言参数，默认从用户设置中获取
        language = data.get('language')
        if not language:
            # 从用户设置中获取语言
            from models import UserSettings
            user_settings = UserSettings.query.filter_by(user_id=current_user.id).first()
            language = user_settings.language if user_settings else 'zh-CN'

        # 检查用户是否已有提示词
        existing_count = CustomPrompt.query.filter(CustomPrompt.user_id == current_user.id).count()

        if existing_count > 0:
            return ApiResponse.error("User already has custom prompts. Use reset-to-defaults to replace them.", 400).to_response()

        # 初始化默认提示词
        CustomPrompt.initialize_user_defaults(current_user.id, language)

        return ApiResponse.success(None, "Default prompts initialized successfully").to_response()

    except Exception as e:
        db.session.rollback()
        return handle_api_error(e, "Failed to initialize default prompts")


@custom_prompts_bp.route('/reset-to-defaults', methods=['POST'])
@unified_auth_required
def reset_to_defaults():
    """重置用户的提示词为默认配置"""
    try:
        current_user = get_current_user()
        data = request.get_json() or {}

        # 获取语言参数，默认从用户设置中获取
        language = data.get('language')
        if not language:
            # 从用户设置中获取语言
            from models import UserSettings
            user_settings = UserSettings.query.filter_by(user_id=current_user.id).first()
            language = user_settings.language if user_settings else 'zh-CN'

        # 删除用户现有的所有提示词
        CustomPrompt.query.filter(CustomPrompt.user_id == current_user.id).delete()

        # 初始化默认提示词
        CustomPrompt.initialize_user_defaults(current_user.id, language)

        return ApiResponse.success(None, "Prompts reset to defaults successfully").to_response()

    except Exception as e:
        db.session.rollback()
        return handle_api_error(e, "Failed to reset prompts to defaults")


@custom_prompts_bp.route('/export', methods=['GET'])
@unified_auth_required
def export_custom_prompts():
    """导出用户的所有自定义提示词"""
    try:
        current_user = get_current_user()

        prompts = CustomPrompt.query.filter(CustomPrompt.user_id == current_user.id).all()

        export_data = {
            'user_id': current_user.id,
            'export_time': db.func.now(),
            'prompts': [prompt.to_dict() for prompt in prompts]
        }

        return ApiResponse.success(export_data, "Custom prompts exported successfully").to_response()

    except Exception as e:
        return handle_api_error(e, "Failed to export custom prompts")


@custom_prompts_bp.route('/import', methods=['POST'])
@unified_auth_required
def import_custom_prompts():
    """导入自定义提示词"""
    try:
        current_user = get_current_user()
        data = validate_json_request(
            required_fields=['prompts']
        )

        if isinstance(data, tuple):  # 错误响应
            return data

        prompts_data = data['prompts']
        if not isinstance(prompts_data, list):
            return ApiResponse.error("prompts must be a list", 400).to_response()

        imported_count = 0
        skipped_count = 0

        for prompt_data in prompts_data:
            try:
                # 验证必需字段
                if not all(field in prompt_data for field in ['prompt_type', 'name', 'content']):
                    skipped_count += 1
                    continue

                # 验证prompt_type
                try:
                    prompt_type = PromptType(prompt_data['prompt_type'])
                except ValueError:
                    skipped_count += 1
                    continue

                # 检查名称是否重复
                existing = CustomPrompt.query.filter(
                    CustomPrompt.user_id == current_user.id,
                    CustomPrompt.prompt_type == prompt_type,
                    CustomPrompt.name == prompt_data['name']
                ).first()

                if existing:
                    skipped_count += 1
                    continue

                # 创建提示词
                CustomPrompt.create_prompt(
                    user_id=current_user.id,
                    prompt_type=prompt_type,
                    name=prompt_data['name'],
                    content=prompt_data['content'],
                    description=prompt_data.get('description'),
                    order_index=prompt_data.get('order_index', 0)
                )

                imported_count += 1

            except Exception:
                skipped_count += 1
                continue

        db.session.commit()

        result = {
            'imported_count': imported_count,
            'skipped_count': skipped_count,
            'total_count': len(prompts_data)
        }

        return ApiResponse.success(result, f"Import completed: {imported_count} imported, {skipped_count} skipped").to_response()

    except Exception as e:
        db.session.rollback()
        return handle_api_error(e, "Failed to import custom prompts")
