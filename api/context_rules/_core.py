from api import context_rules as _pkg
"""
上下文规则 API 蓝图

提供上下文规则的 CRUD 操作接口
"""

from datetime import datetime
from flask import Blueprint, request
from models import db, ContextRule, Project
from ..base import ApiResponse, paginate_query, validate_json_request, get_request_args, APIException, handle_api_error
from core.auth import unified_auth_required, get_current_user
from core.cache_invalidation import invalidate_user_caches

from flask import Blueprint


context_rules_bp = Blueprint('context_rules', __name__)
CONTEXT_RULES_CACHE_TTL_SECONDS = 20
context_rules_fallback_cache = {}


def _context_rules_cache_get(key):
    redis_key = f"context-rules:{key}"
    cached = _pkg.redis_get_json(redis_key)
    if cached is not None:
        return cached

    item = context_rules_fallback_cache.get(key)
    if item and (datetime.utcnow().timestamp() - item['cached_at'] <= CONTEXT_RULES_CACHE_TTL_SECONDS):
        return item['value']
    return None


def _context_rules_cache_set(key, value):
    redis_key = f"context-rules:{key}"
    _pkg.redis_set_json(redis_key, value, CONTEXT_RULES_CACHE_TTL_SECONDS)
    context_rules_fallback_cache[key] = {
        'cached_at': datetime.utcnow().timestamp(),
        'value': value,
    }
