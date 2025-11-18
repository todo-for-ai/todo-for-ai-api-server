"""
安全响应头配置

为HTTP响应添加各种安全相关的头部
"""


def setup_security_headers(app):
    """配置安全响应头"""

    @app.after_request
    def add_security_headers(response):
        """添加安全响应头"""
        # 防止点击劫持
        response.headers['X-Frame-Options'] = 'DENY'

        # 防止 MIME 类型嗅探
        response.headers['X-Content-Type-Options'] = 'nosniff'

        # XSS 保护
        response.headers['X-XSS-Protection'] = '1; mode=block'

        # 引用策略
        response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'

        # HSTS - 强制HTTPS（生产环境）
        if not app.debug:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains; preload'

        # 内容安全策略 - 更严格的配置
        if app.debug:
            # 开发环境：允许本地资源和必要的内联样式
            response.headers['Content-Security-Policy'] = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-eval'; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: blob:; "
                "font-src 'self'; "
                "connect-src 'self' ws: wss:; "
                "frame-ancestors 'none'"
            )
        else:
            # 生产环境：严格的CSP
            response.headers['Content-Security-Policy'] = (
                "default-src 'self'; "
                "script-src 'self'; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; "
                "font-src 'self'; "
                "connect-src 'self'; "
                "frame-ancestors 'none'; "
                "base-uri 'self'; "
                "form-action 'self'"
            )

        # 隐藏服务器信息
        response.headers.pop('Server', None)

        # 权限策略
        response.headers['Permissions-Policy'] = (
            "geolocation=(), "
            "microphone=(), "
            "camera=(), "
            "payment=(), "
            "usb=(), "
            "magnetometer=(), "
            "gyroscope=(), "
            "speaker=()"
        )

        return response
