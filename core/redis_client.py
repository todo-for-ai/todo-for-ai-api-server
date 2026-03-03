"""
Redis 客户端工具
"""

import json
import redis
from flask import current_app
from datetime import date, datetime
from decimal import Decimal


_redis_client = None


def _json_default(value):
    """JSON 序列化兜底处理"""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if hasattr(value, 'value'):
        return value.value
    return str(value)


def get_redis_client():
    """获取 Redis 客户端，失败时返回 None"""
    global _redis_client

    if _redis_client is not None:
        return _redis_client

    try:
        if not current_app.config.get('REDIS_ENABLED', True):
            return None

        _redis_client = redis.Redis(
            host=current_app.config.get('REDIS_HOST', '127.0.0.1'),
            port=current_app.config.get('REDIS_PORT', 6379),
            db=current_app.config.get('REDIS_DB', 0),
            password=current_app.config.get('REDIS_PASSWORD'),
            decode_responses=True,
            socket_connect_timeout=1,
            socket_timeout=1,
        )
        _redis_client.ping()
        return _redis_client
    except Exception as e:
        current_app.logger.warning(f"Redis unavailable, fallback to memory cache: {e}")
        _redis_client = None
        return None


def get_json(key):
    """从 Redis 读取 JSON"""
    client = get_redis_client()
    if not client:
        return None

    raw = client.get(key)
    if not raw:
        return None

    try:
        return json.loads(raw)
    except Exception:
        return None


def set_json(key, value, ttl_seconds):
    """写入 JSON 到 Redis"""
    client = get_redis_client()
    if not client:
        return False

    client.setex(key, ttl_seconds, json.dumps(value, default=_json_default))
    return True
