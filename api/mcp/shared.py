import html
import re
import time
from collections import defaultdict
from functools import wraps

from flask import g, jsonify, request

from core.redis_client import get_json as redis_get_json, set_json as redis_set_json

# 简单的内存频率限制器
rate_limiter = defaultdict(list)

# 用户项目统计缓存，降低重复聚合查询开销
project_stats_cache = {}
PROJECT_STATS_CACHE_TTL_SECONDS = 30


def _get_project_stats_cache(cache_key):
    """读取项目统计缓存（优先 Redis，回退内存）"""
    redis_key = f"mcp:project_stats:{cache_key}"
    cached_value = redis_get_json(redis_key)
    if cached_value is not None:
        return cached_value, 'redis'

    memory_item = project_stats_cache.get(cache_key)
    if memory_item and (time.time() - memory_item['cached_at'] <= PROJECT_STATS_CACHE_TTL_SECONDS):
        return {
            'task_stats_map': memory_item['task_stats_map'],
            'context_rules_map': memory_item['context_rules_map'],
            'cached_at': memory_item['cached_at'],
        }, 'memory'

    return None, None


def _set_project_stats_cache(cache_key, task_stats_map, context_rules_map):
    """写入项目统计缓存（Redis + 内存）"""
    now = time.time()
    payload = {
        'cached_at': now,
        'task_stats_map': task_stats_map,
        'context_rules_map': context_rules_map,
    }
    redis_key = f"mcp:project_stats:{cache_key}"
    redis_set_json(redis_key, payload, PROJECT_STATS_CACHE_TTL_SECONDS)

    project_stats_cache[cache_key] = payload
    if len(project_stats_cache) > 200:
        oldest_key = min(project_stats_cache.items(), key=lambda item: item[1]['cached_at'])[0]
        project_stats_cache.pop(oldest_key, None)


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
    """清理输入，防止XSS攻击"""
    if not isinstance(text, str):
        return text

    # HTML转义
    text = html.escape(text)

    # 移除潜在的脚本标签
    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'javascript:', '', text, flags=re.IGNORECASE)
    text = re.sub(r'on\w+\s*=', '', text, flags=re.IGNORECASE)

    return text


def validate_integer(value, field_name):
    """验证整数输入"""
    if isinstance(value, int):
        return value

    if isinstance(value, str) and value.isdigit():
        return int(value)

    raise ValueError(f"{field_name} must be a valid integer")
