"""
MCP装饰器和工具函数
"""

import html
import re
from flask import request, jsonify, g
from functools import wraps
from collections import defaultdict
import time
from datetime import datetime, timedelta


# 简单的内存频率限制器
rate_limiter = defaultdict(list)


def rate_limit(max_requests=10, window_seconds=60):
    """频率限制装饰器"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            # 获取客户端标识（IP地址或用户ID）
            client_id = request.remote_addr
            if hasattr(g, 'current_user') and g.current_user:
                client_id = f"user_{g.current_user.id}"

            current_time = time.time()

            # 清理过期的请求记录
            rate_limiter[client_id] = [
                req_time for req_time in rate_limiter[client_id]
                if current_time - req_time < window_seconds
            ]

            # 检查是否超过限制
            if len(rate_limiter[client_id]) >= max_requests:
                return jsonify({
                    'error': 'Rate limit exceeded',
                    'message': f'Maximum {max_requests} requests per {window_seconds} seconds'
                }), 429

            # 记录当前请求
            rate_limiter[client_id].append(current_time)

            return f(*args, **kwargs)
        return decorated_function
    return decorator


def require_api_token_auth(f):
    """API Token认证装饰器 - 专门用于MCP接口"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        from flask import current_app

        auth_start_time = time.time()
        auth_id = f"auth-{int(time.time() * 1000)}-{id(request)}"

        current_app.logger.debug(f"[AUTH_START] {auth_id} API token authentication started", extra={
            'auth_id': auth_id,
            'endpoint': request.endpoint,
            'method': request.method,
            'path': request.path,
            'remote_addr': request.remote_addr,
            'user_agent': request.headers.get('User-Agent', 'Unknown')
        })

        # 从请求头获取token
        auth_header = request.headers.get('Authorization')

        current_app.logger.debug(f"[AUTH_HEADER] {auth_id} Authorization header check", extra={
            'auth_id': auth_id,
            'has_auth_header': bool(auth_header),
            'header_format_valid': bool(auth_header and auth_header.startswith('Bearer ')) if auth_header else False
        })

        if not auth_header or not auth_header.startswith('Bearer '):
            current_app.logger.warning(f"[AUTH_FAILED] {auth_id} Missing or invalid authorization header", extra={
                'auth_id': auth_id,
                'auth_header_present': bool(auth_header),
                'auth_header_format': auth_header[:20] + '...' if auth_header and len(auth_header) > 20 else auth_header
            })
            return jsonify({'error': 'Missing or invalid authorization header'}), 401

        token = auth_header.split(' ')[1]
        token_prefix = token[:8] + '...' if len(token) > 8 else token

        current_app.logger.debug(f"[AUTH_TOKEN] {auth_id} Token extracted", extra={
            'auth_id': auth_id,
            'token_prefix': token_prefix,
            'token_length': len(token)
        })

        if not token:
            current_app.logger.warning(f"[AUTH_FAILED] {auth_id} Empty token", extra={
                'auth_id': auth_id
            })
            return jsonify({'error': 'Empty token'}), 401

        # 验证token
        from models import ApiToken
        api_token = ApiToken.verify_token(token)

        auth_duration = time.time() - auth_start_time
        auth_id_success = f"{auth_id}-success"

        if not api_token:
            current_app.logger.warning(f"[AUTH_FAILED] {auth_id} Invalid token", extra={
                'auth_id': auth_id,
                'token_prefix': token_prefix,
                'auth_duration_ms': round(auth_duration * 1000, 2)
            })
            return jsonify({'error': 'Invalid or expired token'}), 401

        # 检查token是否激活
        if not api_token.is_active:
            current_app.logger.warning(f"[AUTH_FAILED] {auth_id} Token is inactive", extra={
                'auth_id': auth_id,
                'token_id': api_token.id,
                'auth_duration_ms': round(auth_duration * 1000, 2)
            })
            return jsonify({'error': 'Token is inactive'}), 401

        # 检查token是否过期
        if api_token.expires_at and api_token.expires_at < datetime.utcnow():
            current_app.logger.warning(f"[AUTH_FAILED] {auth_id} Token expired", extra={
                'auth_id': auth_id,
                'token_id': api_token.id,
                'expires_at': api_token.expires_at.isoformat() if api_token.expires_at else None,
                'auth_duration_ms': round(auth_duration * 1000, 2)
            })
            return jsonify({'error': 'Token has expired'}), 401

        # 获取关联的用户
        user = api_token.user
        if not user:
            current_app.logger.warning(f"[AUTH_FAILED] {auth_id} No user associated with token", extra={
                'auth_id': auth_id,
                'token_id': api_token.id,
                'auth_duration_ms': round(auth_duration * 1000, 2)
            })
            return jsonify({'error': 'No user associated with this token'}), 401

        # 检查用户状态
        if not user.is_active():
            current_app.logger.warning(f"[AUTH_FAILED] {auth_id} User is inactive", extra={
                'auth_id': auth_id,
                'user_id': user.id,
                'user_status': user.status.value if user.status else None,
                'auth_duration_ms': round(auth_duration * 1000, 2)
            })
            return jsonify({'error': 'User account is inactive'}), 403

        # 认证成功
        g.current_user = user
        g.current_api_token = api_token

        # 更新token使用统计
        try:
            api_token.last_used_at = datetime.utcnow()
            api_token.usage_count = (api_token.usage_count or 0) + 1
            api_token.save()
        except Exception as e:
            current_app.logger.warning(f"[AUTH_WARNING] {auth_id} Failed to update token stats", extra={
                'auth_id': auth_id,
                'error': str(e)
            })

        current_app.logger.info(f"[AUTH_SUCCESS] {auth_id_success} Authentication successful", extra={
            'auth_id': auth_id_success,
            'user_id': user.id,
            'user_email': user.email,
            'token_id': api_token.id,
            'token_prefix': token_prefix,
            'auth_duration_ms': round(auth_duration * 1000, 2),
            'endpoint': request.endpoint,
            'method': request.method,
            'path': request.path
        })

        return f(*args, **kwargs)
    return decorated_function


def sanitize_input(text):
    """
    清理用户输入，防止XSS攻击
    """
    if not isinstance(text, str):
        return text

    # 转义HTML特殊字符
    text = html.escape(text)

    # 移除潜在的恶意脚本
    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.IGNORECASE | re.DOTALL)

    # 移除javascript:协议
    text = re.sub(r'javascript:[^"\']*', '', text, flags=re.IGNORECASE)

    # 移除on*事件处理器
    text = re.sub(r'on\w+\s*=\s*["\'][^"\']*["\']', '', text, flags=re.IGNORECASE)

    return text.strip()


def validate_integer(value, field_name):
    """
    验证整数参数
    """
    if value is None:
        return None

    try:
        return int(value)
    except (ValueError, TypeError):
        raise ValueError(f"Invalid integer value for field '{field_name}'")
