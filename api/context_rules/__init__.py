"""上下文规则 API 包（由单文件 api/context_rules.py 原样拆分）。

兼容导出：context_rules_bp 与全部 14 个路由函数；被单测 patch 的符号
（ContextRule / get_current_user / get_request_args / invalidate_user_caches /
_context_rules_cache_get）从本包命名空间 re-export，路由经 `_pkg.` 运行时解析，
setattr 语义不变。
"""

from models import ContextRule  # noqa: F401
from core.auth import get_current_user  # noqa: F401
from api.base import get_request_args  # noqa: F401
from core.cache_invalidation import invalidate_user_caches  # noqa: F401
from core.redis_client import get_json as redis_get_json, set_json as redis_set_json  # noqa: F401  (re-export 供 setattr(cr, "redis_get_json") 打桩)

from api.context_rules._core import (  # noqa: F401
    context_rules_bp,
    CONTEXT_RULES_CACHE_TTL_SECONDS,
    context_rules_fallback_cache,
    _context_rules_cache_get,
    _context_rules_cache_set,
)
from api.context_rules.crud import (  # noqa: F401
    list_context_rules,
    create_context_rule,
    get_context_rule,
    update_context_rule,
    delete_context_rule,
    activate_context_rule,
    deactivate_context_rule,
)
from api.context_rules.builder import (  # noqa: F401
    build_context,
    preview_merged_rules,
)
from api.context_rules.sharing import (  # noqa: F401
    get_public_rules,
    copy_rule_from_marketplace,
    get_global_context_rules,
    get_merged_context_rules,
)
