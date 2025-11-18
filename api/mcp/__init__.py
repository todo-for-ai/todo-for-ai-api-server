"""
MCP (Model Context Protocol) API 模块
拆分后的模块结构：
- decorators: 装饰器（认证、频率限制等）
- handlers: 工具处理器函数
- routes: 路由定义
"""

from flask import Blueprint
from .decorators import rate_limit, require_api_token_auth, sanitize_input, validate_integer
from . import handlers
from . import routes

# 创建蓝图
mcp_bp = Blueprint('mcp', __name__)

# 注册路由
routes.register_routes(mcp_bp)

__all__ = [
    'mcp_bp',
    'rate_limit',
    'require_api_token_auth',
    'sanitize_input',
    'validate_integer',
    'handlers'
]
