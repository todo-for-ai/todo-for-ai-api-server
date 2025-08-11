"""
自定义提示词API端点
"""

from flask import Blueprint, request
from models import db, CustomPrompt, PromptType
from .base import ApiResponse, paginate_query, validate_json_request, get_request_args, APIException, handle_api_error
from core.auth import unified_auth_required, get_current_user

custom_prompts_bp = Blueprint('custom_prompts', __name__)


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
