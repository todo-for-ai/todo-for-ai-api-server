"""
Flask 装饰器集合

包含常用的HTTP请求处理装饰器
"""

from functools import wraps
from flask import request, jsonify


def rate_limit_decorator(max_requests=100, window=3600):
    """简单的速率限制装饰器"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            # 这里可以实现基于 Redis 的速率限制
            # 目前只是一个占位符
            return f(*args, **kwargs)
        return decorated_function
    return decorator


def require_json(f):
    """要求请求内容为 JSON 的装饰器"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if request.method in ['POST', 'PUT', 'PATCH']:
            if not request.is_json:
                return jsonify({
                    'error': 'Bad Request',
                    'message': 'Content-Type must be application/json'
                }), 400
        return f(*args, **kwargs)
    return decorated_function


def validate_request_size(max_size=16 * 1024 * 1024):  # 16MB
    """验证请求大小的装饰器"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if request.content_length and request.content_length > max_size:
                return jsonify({
                    'error': 'Request Entity Too Large',
                    'message': f'Request size exceeds maximum allowed size of {max_size} bytes'
                }), 413
            return f(*args, **kwargs)
        return decorated_function
    return decorator
