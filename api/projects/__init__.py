"""
项目 API 蓝图 package

保持与原 api.projects 模块相同的导出能力：
- projects_bp
- projects_list_fallback_cache
"""

from flask import Blueprint

projects_bp = Blueprint('projects', __name__)

from .shared import projects_list_fallback_cache  # noqa: E402
from . import routes_project_listing as _routes_project_listing  # noqa: F401,E402
from . import routes_project_crud as _routes_project_crud  # noqa: F401,E402
from . import routes_members as _routes_members  # noqa: F401,E402

__all__ = ['projects_bp', 'projects_list_fallback_cache']
