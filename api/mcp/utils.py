"""
MCP工具函数模块
"""

import html
import re
from flask import request, jsonify, g
from functools import wraps
from collections import defaultdict
import time


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
