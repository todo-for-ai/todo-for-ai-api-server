"""记忆管理 API（用户可编辑的多维度记忆）。"""

from flask import Blueprint

memory_bp = Blueprint('memory', __name__)

from . import routes  # noqa: E402,F401  路由注册到 memory_bp

__all__ = ['memory_bp']
