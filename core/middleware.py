"""
Flask 中间件配置

统一的中间件入口，组合所有中间件功能

导入说明：
- logging_config: 日志系统和请求日志中间件
- error_handlers: HTTP错误处理器
- security_headers: 安全响应头配置
- decorators: HTTP请求处理装饰器
"""

from .logging_config import setup_logging, setup_request_logging
from .error_handlers import setup_error_handlers
from .security_headers import setup_security_headers


def setup_all_middleware(app):
    """设置所有中间件"""
    setup_logging(app)
    setup_request_logging(app)
    setup_error_handlers(app)
    setup_security_headers(app)

    app.logger.info("All middleware configured successfully")
