"""
OpenAI API 兼容路由（包结构，由单文件 api/openai_compatible.py 原样拆分）。

模块布局：
- _core.py      蓝图、openai_auth_required 装饰器、常量配置
- cache.py      OpenAICacheManager（双级缓存 + 一致性锁）
- handler.py    OpenAIRequestHandler（校验/响应构造/日志）
- routes.py     5 个兼容端点
- __init__.py   兼容 shim：re-export 全部公开符号并持有 cache_manager 实例

单测以 `from api import openai_compatible as oc` 把本包当命名空间使用
（oc.cache_manager 实例级打桩、patch("api.openai_compatible.get_current_user")），
__init__ 的 re-export 与运行时引用保证这些语义不变。
"""

import time

from api.openai_compatible._core import (
    openai_bp,
    openai_auth_required,
    CACHE_TTL_SECONDS,
    CACHE_KEY_PREFIX,
    CACHE_CONSISTENCY_LOCK_PREFIX,
    CACHE_INVALIDATION_CHANNEL,
    MAX_REQUEST_BODY_SIZE,
    MAX_MESSAGES_LENGTH,
    MAX_PROMPT_LENGTH,
    SUPPORTED_MODELS,
)
from api.openai_compatible.cache import OpenAICacheManager
from api.openai_compatible.handler import OpenAIRequestHandler
from core.auth import get_current_user, get_current_token  # noqa: F401  (re-export 供 patch("api.openai_compatible.get_current_user"))
from core.redis_client import get_redis_client, get_json, set_json  # noqa: F401  (re-export 供实例级/包级打桩)

# 全局缓存管理器实例（路由与本包共享同一实例；测试在实例上打桩）
cache_manager = OpenAICacheManager()

from api.openai_compatible.routes import (  # noqa: F401,E402
    list_models,
    get_model,
    chat_completions,
    create_embeddings,
    get_usage,
    invalidate_cache,
)
