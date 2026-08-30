"""
AI 容错配置管理 API

管理员可以通过此 API 管理 AI 服务的容错参数：
- 超时设置
- 重试策略
- 限流配置
- 缓存配置
"""

from functools import wraps
from flask import Blueprint, request, g
from datetime import datetime
from models import db
from models.system_settings import SystemSettings
from core.auth import unified_auth_required
from api.base import ApiResponse

admin_ai_config_bp = Blueprint('admin_ai_config', __name__)


def require_admin(f):
    """要求管理员权限的装饰器"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        current_user = getattr(g, 'current_user', None)
        if not current_user:
            return ApiResponse.unauthorized('请先登录').to_response()
        if not current_user.is_admin():
            return ApiResponse.forbidden('需要管理员权限').to_response()
        return f(*args, **kwargs)
    return decorated_function


# 配置字段验证规则
CONFIG_VALIDATION_RULES = {
    'connect_timeout': {'type': 'int', 'min': 1, 'max': 300, 'default': 30, 'description': '连接超时（秒）'},
    'read_timeout': {'type': 'int', 'min': 1, 'max': 600, 'default': 120, 'description': '读取超时（秒）'},
    'max_retries': {'type': 'int', 'min': 0, 'max': 10, 'default': 5, 'description': '最大重试次数'},
    'retry_backoff_factor': {'type': 'float', 'min': 0.1, 'max': 5.0, 'default': 1.0, 'description': '指数退避因子'},
    'max_retry_wait_time': {'type': 'int', 'min': 1, 'max': 300, 'default': 60, 'description': '最大重试等待时间（秒）'},
    'rate_limit_requests': {'type': 'int', 'min': 1, 'max': 1000, 'default': 60, 'description': '每分钟最大请求数'},
    'rate_limit_window': {'type': 'int', 'min': 1, 'max': 3600, 'default': 60, 'description': '限流窗口大小（秒）'},
    'cache_ttl': {'type': 'int', 'min': 0, 'max': 3600, 'default': 300, 'description': '缓存时间（秒）'},
}


def validate_config(config_data):
    """
    验证 AI 容错配置

    Args:
        config_data: 配置数据字典

    Returns:
        (是否有效, 验证后的配置, 错误信息)
    """
    validated = {}
    errors = {}

    for key, rules in CONFIG_VALIDATION_RULES.items():
        value = config_data.get(key)

        if value is None:
            validated[key] = rules['default']
            continue

        if rules['type'] == 'int':
            try:
                value = int(value)
                if not (rules['min'] <= value <= rules['max']):
                    errors[key] = f"必须在 {rules['min']} 到 {rules['max']} 之间"
                    continue
            except (ValueError, TypeError):
                errors[key] = "必须是整数"
                continue

        elif rules['type'] == 'float':
            try:
                value = float(value)
                if not (rules['min'] <= value <= rules['max']):
                    errors[key] = f"必须在 {rules['min']} 到 {rules['max']} 之间"
                    continue
            except (ValueError, TypeError):
                errors[key] = "必须是数字"
                continue

        validated[key] = value

    if errors:
        return False, validated, errors

    return True, validated, None


@admin_ai_config_bp.route('', methods=['GET'])
@unified_auth_required
@require_admin
def get_ai_config():
    """
    获取 AI 容错配置

    Returns:
        当前AI容错配置
    """
    config = SystemSettings.get_ai_resilience_config()

    # 添加元数据
    setting = SystemSettings.query.filter_by(key='ai_resilience_config').first()
    metadata = {}
    if setting:
        metadata = {
            'updated_at': setting.updated_at.isoformat() if setting.updated_at else None,
            'updated_by': setting.updated_by,
        }

    return ApiResponse.success({
        'config': config,
        'metadata': metadata,
        'validation_rules': CONFIG_VALIDATION_RULES
    }).to_response()


@admin_ai_config_bp.route('', methods=['PUT'])
@unified_auth_required
@require_admin
def update_ai_config():
    """
    更新 AI 容错配置

    Request Body:
        - connect_timeout: 连接超时（秒）
        - read_timeout: 读取超时（秒）
        - max_retries: 最大重试次数
        - retry_backoff_factor: 指数退避因子
        - max_retry_wait_time: 最大重试等待时间
        - rate_limit_requests: 每分钟最大请求数
        - rate_limit_window: 限流窗口大小（秒）
        - cache_ttl: 缓存时间（秒）

    Returns:
        更新后的配置
    """
    data = request.get_json()
    if not data:
        return ApiResponse.bad_request('请求体不能为空').to_response()

    # 验证配置
    is_valid, validated_config, errors = validate_config(data)
    if not is_valid:
        return ApiResponse.bad_request('配置验证失败', {'errors': errors}).to_response()

    # 获取当前用户ID
    user_id = g.current_user.id if hasattr(g, 'current_user') and g.current_user else None

    try:
        # 保存配置
        SystemSettings.set_ai_resilience_config(validated_config, updated_by=user_id)

        return ApiResponse.success({
            'config': validated_config,
            'message': '配置更新成功',
            'updated_at': datetime.utcnow().isoformat()
        }).to_response()
    except Exception as e:
        return ApiResponse.error(500, f'保存配置失败: {str(e)}').to_response()


@admin_ai_config_bp.route('/reset', methods=['POST'])
@unified_auth_required
@require_admin
def reset_ai_config():
    """
    重置 AI 容错配置为默认值

    Returns:
        默认配置
    """
    default_config = {
        'connect_timeout': 30,
        'read_timeout': 120,
        'max_retries': 5,
        'retry_backoff_factor': 1.0,
        'max_retry_wait_time': 60,
        'rate_limit_requests': 60,
        'rate_limit_window': 60,
        'cache_ttl': 300
    }

    user_id = g.current_user.id if hasattr(g, 'current_user') and g.current_user else None

    try:
        SystemSettings.set_ai_resilience_config(default_config, updated_by=user_id)

        return ApiResponse.success({
            'config': default_config,
            'message': '配置已重置为默认值'
        }).to_response()
    except Exception as e:
        return ApiResponse.error(500, f'重置配置失败: {str(e)}').to_response()
