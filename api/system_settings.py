#!/usr/bin/env python3
"""
系统设置 API

提供系统级别的配置管理（仅管理员可访问）
"""

from flask import Blueprint, request
from core.auth import unified_auth_required, get_current_user
from api.base import ApiResponse, handle_api_error
from models.system_settings import SystemSettings
from models.user import UserRole

system_settings_bp = Blueprint('system_settings', __name__)


def require_admin():
    """检查当前用户是否为管理员"""
    user = get_current_user()
    if not user or user.role != UserRole.ADMIN:
        return False
    return True


@system_settings_bp.route('/llm-config', methods=['GET'])
@unified_auth_required
def get_llm_config():
    """获取大模型 API 配置"""
    try:
        # 检查权限（仅管理员可查看完整配置）
        user = get_current_user()
        is_admin = user and user.role == UserRole.ADMIN

        config = SystemSettings.get_llm_config()

        # 非管理员隐藏 API Key
        if not is_admin:
            safe_config = config.copy()
            if 'api_key' in safe_config:
                safe_config['api_key'] = '***hidden***'
            return ApiResponse.success(safe_config, "LLM configuration retrieved").to_response()

        return ApiResponse.success(config, "LLM configuration retrieved").to_response()

    except Exception as e:
        return handle_api_error(e)


@system_settings_bp.route('/llm-config', methods=['PUT'])
@unified_auth_required
def update_llm_config():
    """更新大模型 API 配置（仅管理员）"""
    try:
        # 检查管理员权限
        if not require_admin():
            return ApiResponse.error("Admin access required", 403).to_response()

        if not request.is_json:
            return ApiResponse.error("Content-Type must be application/json", 400).to_response()

        data = request.get_json()
        user = get_current_user()

        # 验证必填字段
        required_fields = ['provider', 'api_base', 'model']
        for field in required_fields:
            if field not in data:
                return ApiResponse.error(f"Missing required field: {field}", 400).to_response()

        # 获取现有配置
        existing_config = SystemSettings.get_llm_config()

        # 更新配置
        new_config = {
            'provider': data.get('provider', existing_config.get('provider', 'openai')),
            'api_base': data.get('api_base', existing_config.get('api_base', '')),
            'api_key': data.get('api_key', existing_config.get('api_key', '')),
            'model': data.get('model', existing_config.get('model', 'gpt-4')),
            'temperature': data.get('temperature', existing_config.get('temperature', 0.7)),
            'max_tokens': data.get('max_tokens', existing_config.get('max_tokens', 2000)),
            'timeout': data.get('timeout', existing_config.get('timeout', 60)),
        }

        # 保存配置
        setting = SystemSettings.set_llm_config(new_config, updated_by=user.id if user else None)

        return ApiResponse.success(setting.to_dict(), "LLM configuration updated successfully").to_response()

    except Exception as e:
        return handle_api_error(e)


@system_settings_bp.route('', methods=['GET'])
@unified_auth_required
def get_all_settings():
    """获取所有系统设置（仅管理员）"""
    try:
        # 检查管理员权限
        if not require_admin():
            return ApiResponse.error("Admin access required", 403).to_response()

        settings = SystemSettings.get_all_settings()

        return ApiResponse.success(settings, "System settings retrieved").to_response()

    except Exception as e:
        return handle_api_error(e)


@system_settings_bp.route('/<key>', methods=['GET'])
@unified_auth_required
def get_setting(key):
    """获取指定系统设置（仅管理员）"""
    try:
        # 检查管理员权限
        if not require_admin():
            return ApiResponse.error("Admin access required", 403).to_response()

        setting = SystemSettings.get_setting(key)

        if setting is None:
            return ApiResponse.error(f"Setting '{key}' not found", 404).to_response()

        return ApiResponse.success(setting, f"Setting '{key}' retrieved").to_response()

    except Exception as e:
        return handle_api_error(e)


@system_settings_bp.route('/<key>', methods=['PUT'])
@unified_auth_required
def update_setting(key):
    """更新指定系统设置（仅管理员）"""
    try:
        # 检查管理员权限
        if not require_admin():
            return ApiResponse.error("Admin access required", 403).to_response()

        if not request.is_json:
            return ApiResponse.error("Content-Type must be application/json", 400).to_response()

        data = request.get_json()
        user = get_current_user()

        setting = SystemSettings.set_setting(
            key,
            data.get('value'),
            description=data.get('description'),
            updated_by=user.id if user else None
        )

        return ApiResponse.success(setting.to_dict(), f"Setting '{key}' updated").to_response()

    except Exception as e:
        return handle_api_error(e)


@system_settings_bp.route('/test-llm', methods=['POST'])
@unified_auth_required
def test_llm_connection():
    """测试大模型 API 连接（仅管理员）"""
    try:
        # 检查管理员权限
        if not require_admin():
            return ApiResponse.error("Admin access required", 403).to_response()

        if not request.is_json:
            return ApiResponse.error("Content-Type must be application/json", 400).to_response()

        data = request.get_json()

        # 获取配置（优先使用传入的配置，否则使用存储的配置）
        if data.get('provider') and data.get('api_base'):
            config = data
        else:
            config = SystemSettings.get_llm_config()

        # 测试连接
        result = test_llm_api_connection(config)

        if result['success']:
            return ApiResponse.success(result, "LLM connection test successful").to_response()
        else:
            return ApiResponse.error(result.get('error', 'Connection test failed'), 400).to_response()

    except Exception as e:
        return handle_api_error(e)


def test_llm_api_connection(config):
    """测试大模型 API 连接"""
    import requests

    provider = config.get('provider', 'openai')
    api_base = config.get('api_base', '')
    api_key = config.get('api_key', '')
    model = config.get('model', 'gpt-4')

    # ollama 本地服务通常不需要 API key
    if not api_base or (not api_key and provider != 'ollama'):
        return {'success': False, 'error': 'API base URL and API key are required'}

    try:
        if provider == 'openai':
            headers = {
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json'
            }
            response = requests.get(
                f"{api_base}/models",
                headers=headers,
                timeout=10
            )
            if response.status_code == 200:
                return {'success': True, 'message': 'OpenAI API connection successful'}
            else:
                return {'success': False, 'error': f'API returned status {response.status_code}'}

        elif provider in ['azure', 'azure_openai']:
            headers = {
                'api-key': api_key,
                'Content-Type': 'application/json'
            }
            response = requests.get(
                f"{api_base}/models",
                headers=headers,
                timeout=10
            )
            if response.status_code == 200:
                return {'success': True, 'message': 'Azure OpenAI API connection successful'}
            else:
                return {'success': False, 'error': f'API returned status {response.status_code}'}

        elif provider == 'anthropic':
            headers = {
                'x-api-key': api_key,
                'Content-Type': 'application/json'
            }
            response = requests.get(
                f"{api_base}/models",
                headers=headers,
                timeout=10
            )
            if response.status_code == 200:
                return {'success': True, 'message': 'Anthropic API connection successful'}
            else:
                return {'success': False, 'error': f'API returned status {response.status_code}'}

        elif provider == 'ollama':
            # Ollama 通常不需要 API key
            response = requests.get(
                f"{api_base}/api/tags",
                timeout=10
            )
            if response.status_code == 200:
                models = response.json().get('models', [])
                return {'success': True, 'message': f'Ollama connection successful, available models: {len(models)}'}
            else:
                return {'success': False, 'error': f'Ollama returned status {response.status_code}'}

        else:
            return {'success': False, 'error': f'Unsupported provider: {provider}'}

    except requests.exceptions.Timeout:
        return {'success': False, 'error': 'Connection timeout'}
    except requests.exceptions.ConnectionError:
        return {'success': False, 'error': 'Connection error, please check the API base URL'}
    except Exception as e:
        return {'success': False, 'error': str(e)}
