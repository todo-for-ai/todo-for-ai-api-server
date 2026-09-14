"""认证 API 包（由单文件 api/auth.py 原样拆分）。

兼容导出：auth_bp、全部 14 个路由函数与 5 个共享 helpers；被单测 patch 的
符号（get_current_user / github_service / google_service / request）从本包
re-export，路由经 `_pkg.` 运行时解析，setattr 语义不变。
"""

from api.auth._core import (  # noqa: F401
    auth_bp,
    _normalize_local_loopback_url,
    _normalize_return_to,
    _append_query_params,
    _collect_accessible_org_ids,
    _collect_user_org_role_keys,
)
from core.github_config import github_service, require_auth, get_current_user  # noqa: F401
from core.google_config import google_service  # noqa: F401
from flask import request  # noqa: F401  (re-export 供 setattr("api.auth.request") 与路由 _pkg.request 运行时解析)
from api.auth.routes_core import (  # noqa: F401
    login,
    guest_login,
    callback,
    logout,
    get_current_user_info,
    update_current_user,
    verify_token,
    refresh,
)
from api.auth.oauth import (  # noqa: F401
    github_login,
    google_login,
    github_callback,
    google_callback,
)
from api.auth.users import (  # noqa: F401
    list_users,
    get_user,
)
