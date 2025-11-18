"""
错误处理器中间件

处理HTTP错误状态码的中间件
"""

from flask import request
from api.base import ApiResponse


def setup_error_handlers(app):
    """配置错误处理器"""

    @app.errorhandler(400)
    def bad_request(error):
        """400 错误处理"""
        app.logger.warning(f"Bad Request: {request.url} - {error}")
        return ApiResponse.error(
            message='The request could not be understood by the server',
            code=400
        ).to_response()

    @app.errorhandler(401)
    def unauthorized(error):
        """401 错误处理"""
        app.logger.warning(f"Unauthorized: {request.url} - {error}")
        return ApiResponse.unauthorized(
            message='Authentication is required'
        ).to_response()

    @app.errorhandler(403)
    def forbidden(error):
        """403 错误处理"""
        app.logger.warning(f"Forbidden: {request.url} - {error}")
        return ApiResponse.forbidden(
            message='You do not have permission to access this resource'
        ).to_response()

    @app.errorhandler(404)
    def not_found(error):
        """404 错误处理"""
        app.logger.info(f"Not Found: {request.url}")
        return ApiResponse.not_found(
            message='The requested resource was not found'
        ).to_response()

    @app.errorhandler(405)
    def method_not_allowed(error):
        """405 错误处理"""
        app.logger.warning(f"Method Not Allowed: {request.method} {request.url}")
        return ApiResponse.error(
            message=f'The {request.method} method is not allowed for this endpoint',
            code=405
        ).to_response()

    @app.errorhandler(422)
    def unprocessable_entity(error):
        """422 错误处理"""
        app.logger.warning(f"Unprocessable Entity: {request.url} - {error}")
        return ApiResponse.error(
            message='The request was well-formed but contains semantic errors',
            code=422
        ).to_response()

    @app.errorhandler(500)
    def internal_error(error):
        """500 错误处理"""
        from models import db
        db.session.rollback()
        app.logger.error(f"Internal Server Error: {request.url} - {error}")
        return ApiResponse.error(
            message='An unexpected error occurred',
            code=500
        ).to_response()
