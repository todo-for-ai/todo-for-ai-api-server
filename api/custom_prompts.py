#!/usr/bin/env python3
"""
自定义提示词 API

提供用户自定义提示词的增删改查功能
"""

from flask import Blueprint, request, jsonify
from core.github_config import require_auth, get_current_user
from api.base import api_response, api_error, handle_api_error
from models.user_settings import UserSettings


custom_prompts_bp = Blueprint('custom_prompts', __name__)


@custom_prompts_bp.route('', methods=['GET'])
@require_auth
def get_user_custom_prompts():
    """获取用户的自定义提示词配置"""
    try:
        current_user = get_current_user()
        
        # 获取或创建用户设置
        settings = UserSettings.get_or_create_for_user(current_user.id)
        
        # 从settings_data中获取自定义提示词配置
        custom_prompts_config = {}
        if settings.settings_data:
            custom_prompts_config = settings.settings_data.get('custom_prompts', {})
        
        # 返回配置，如果没有配置则返回空对象
        return api_response({
            'success': True,
            'data': custom_prompts_config
        }, "Custom prompts retrieved successfully")
        
    except Exception as e:
        return handle_api_error(e)


@custom_prompts_bp.route('', methods=['POST'])
@require_auth
def save_user_custom_prompts():
    """保存用户的自定义提示词配置"""
    try:
        current_user = get_current_user()
        
        if not request.is_json:
            return api_error("Content-Type must be application/json", 400)
        
        data = request.get_json()
        
        # 获取或创建用户设置
        settings = UserSettings.get_or_create_for_user(current_user.id)
        
        # 确保settings_data存在
        if not settings.settings_data:
            settings.settings_data = {}
        
        # 更新自定义提示词配置
        # 支持部分更新：如果只传了projectPromptTemplate或taskPromptButtons，只更新对应部分
        if 'custom_prompts' not in settings.settings_data:
            settings.settings_data['custom_prompts'] = {}
        
        if 'projectPromptTemplate' in data:
            settings.settings_data['custom_prompts']['projectPromptTemplate'] = data['projectPromptTemplate']
        
        if 'taskPromptButtons' in data:
            settings.settings_data['custom_prompts']['taskPromptButtons'] = data['taskPromptButtons']
        
        settings.save()
        
        return api_response({
            'success': True,
            'data': settings.settings_data.get('custom_prompts', {})
        }, "Custom prompts saved successfully")
        
    except Exception as e:
        return handle_api_error(e)


@custom_prompts_bp.route('/reset', methods=['POST'])
@require_auth
def reset_custom_prompts_to_default():
    """重置为默认配置"""
    try:
        current_user = get_current_user()
        
        # 获取或创建用户设置
        settings = UserSettings.get_or_create_for_user(current_user.id)
        
        # 确保settings_data存在
        if not settings.settings_data:
            settings.settings_data = {}
        
        # 清空自定义提示词配置
        settings.settings_data['custom_prompts'] = {}
        settings.save()
        
        return api_response({
            'success': True,
            'data': {}
        }, "Custom prompts reset to default successfully")
        
    except Exception as e:
        return handle_api_error(e)


@custom_prompts_bp.route('/project-template', methods=['GET'])
@require_auth
def get_project_prompt_template():
    """获取项目提示词模板"""
    try:
        current_user = get_current_user()
        
        # 获取或创建用户设置
        settings = UserSettings.get_or_create_for_user(current_user.id)
        
        # 从settings_data中获取项目提示词模板
        template = ''
        if settings.settings_data and 'custom_prompts' in settings.settings_data:
            template = settings.settings_data['custom_prompts'].get('projectPromptTemplate', '')
        
        return api_response({
            'template': template
        }, "Project prompt template retrieved successfully")
        
    except Exception as e:
        return handle_api_error(e)


@custom_prompts_bp.route('/project-template', methods=['POST'])
@require_auth
def save_project_prompt_template():
    """保存项目提示词模板"""
    try:
        current_user = get_current_user()
        
        if not request.is_json:
            return api_error("Content-Type must be application/json", 400)
        
        data = request.get_json()
        template = data.get('template')
        
        if template is None:
            return api_error("Template is required", 400)
        
        # 获取或创建用户设置
        settings = UserSettings.get_or_create_for_user(current_user.id)
        
        # 确保settings_data存在
        if not settings.settings_data:
            settings.settings_data = {}
        
        if 'custom_prompts' not in settings.settings_data:
            settings.settings_data['custom_prompts'] = {}
        
        # 更新项目提示词模板
        settings.settings_data['custom_prompts']['projectPromptTemplate'] = template
        settings.save()
        
        return api_response({
            'success': True
        }, "Project prompt template saved successfully")
        
    except Exception as e:
        return handle_api_error(e)


@custom_prompts_bp.route('/task-buttons', methods=['GET'])
@require_auth
def get_task_prompt_buttons():
    """获取任务提示词按钮配置"""
    try:
        current_user = get_current_user()
        
        # 获取或创建用户设置
        settings = UserSettings.get_or_create_for_user(current_user.id)
        
        # 从settings_data中获取任务提示词按钮配置
        buttons = []
        if settings.settings_data and 'custom_prompts' in settings.settings_data:
            buttons = settings.settings_data['custom_prompts'].get('taskPromptButtons', [])
        
        return api_response({
            'buttons': buttons
        }, "Task prompt buttons retrieved successfully")
        
    except Exception as e:
        return handle_api_error(e)


@custom_prompts_bp.route('/task-buttons', methods=['POST'])
@require_auth
def save_task_prompt_buttons():
    """保存任务提示词按钮配置"""
    try:
        current_user = get_current_user()
        
        if not request.is_json:
            return api_error("Content-Type must be application/json", 400)
        
        data = request.get_json()
        buttons = data.get('buttons')
        
        if buttons is None:
            return api_error("Buttons is required", 400)
        
        if not isinstance(buttons, list):
            return api_error("Buttons must be an array", 400)
        
        # 获取或创建用户设置
        settings = UserSettings.get_or_create_for_user(current_user.id)
        
        # 确保settings_data存在
        if not settings.settings_data:
            settings.settings_data = {}
        
        if 'custom_prompts' not in settings.settings_data:
            settings.settings_data['custom_prompts'] = {}
        
        # 更新任务提示词按钮配置
        settings.settings_data['custom_prompts']['taskPromptButtons'] = buttons
        settings.save()
        
        return api_response({
            'success': True
        }, "Task prompt buttons saved successfully")
        
    except Exception as e:
        return handle_api_error(e)
