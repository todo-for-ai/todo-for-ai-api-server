"""
日志配置和请求日志中间件

包含应用日志配置和HTTP请求/响应日志记录功能
"""

import time
import logging
from datetime import datetime
from flask import request, g


def setup_logging(app):
    """配置日志系统"""
    if not app.debug:
        # 生产环境日志配置
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]'
        )
    else:
        # 开发环境日志配置
        logging.basicConfig(
            level=logging.DEBUG,
            format='%(asctime)s %(levelname)s: %(message)s'
        )


def setup_request_logging(app):
    """配置请求日志中间件"""

    @app.before_request
    def before_request():
        """请求开始前的处理"""
        g.start_time = time.time()
        g.request_id = f"{int(time.time() * 1000)}-{id(request)}"

        # 获取请求详细信息
        request_info = {
            'request_id': g.request_id,
            'method': request.method,
            'url': str(request.url),
            'path': request.path,
            'remote_addr': request.remote_addr,
            'user_agent': request.headers.get('User-Agent', 'Unknown'),
            'content_type': request.headers.get('Content-Type', 'None'),
            'content_length': request.headers.get('Content-Length', '0'),
            'authorization': 'Bearer ***' if request.headers.get('Authorization') else 'None',
            'timestamp': datetime.utcnow().isoformat(),
            'query_args': dict(request.args) if request.args else {},
            'form_keys': list(request.form.keys()) if request.form else [],
            'files': list(request.files.keys()) if request.files else [],
            'is_json': request.is_json,
            'is_secure': request.is_secure
        }

        # 记录请求开始
        app.logger.info(f"[REQUEST_START] {g.request_id} {request.method} {request.path}", extra=request_info)

        # 如果是 JSON 请求，记录请求体（但不记录敏感信息）
        if request.is_json and request.method in ['POST', 'PUT', 'PATCH']:
            try:
                json_data = request.get_json()
                if json_data:
                    # 过滤敏感字段
                    filtered_data = {}
                    for key, value in json_data.items():
                        if any(sensitive in key.lower() for sensitive in ['password', 'token', 'secret', 'key']):
                            filtered_data[key] = '***'
                        elif isinstance(value, str) and len(value) > 100:
                            filtered_data[key] = value[:100] + '...'
                        else:
                            filtered_data[key] = value

                    app.logger.debug(f"[REQUEST_BODY] {g.request_id}", extra={
                        'request_id': g.request_id,
                        'json_data': filtered_data,
                        'data_size': len(str(json_data))
                    })
            except Exception as e:
                app.logger.warning(f"[REQUEST_BODY_ERROR] {g.request_id} Failed to parse JSON: {e}")

    @app.after_request
    def after_request(response):
        """请求结束后的处理"""
        if hasattr(g, 'start_time'):
            duration = time.time() - g.start_time

            # 获取响应详细信息
            response_info = {
                'request_id': g.request_id,
                'method': request.method,
                'path': request.path,
                'status_code': response.status_code,
                'duration_ms': round(duration * 1000, 2),
                'duration_s': round(duration, 3),
                'content_type': response.headers.get('Content-Type', 'Unknown'),
                'content_length': response.headers.get('Content-Length', 'Unknown'),
                'timestamp': datetime.utcnow().isoformat(),
                'cache_control': response.headers.get('Cache-Control', 'None'),
                'location': response.headers.get('Location', 'None')
            }

            # 根据状态码选择日志级别
            if response.status_code >= 500:
                log_level = 'error'
            elif response.status_code >= 400:
                log_level = 'warning'
            elif response.status_code >= 300:
                log_level = 'info'
            else:
                log_level = 'info'

            getattr(app.logger, log_level)(
                f"[REQUEST_END] {g.request_id} {request.method} {request.path} - "
                f"Status: {response.status_code}, Duration: {duration:.3f}s",
                extra=response_info
            )

            # 记录慢请求
            if duration > 1.0:  # 超过1秒的请求
                app.logger.warning(f"[SLOW_REQUEST] {g.request_id} {request.method} {request.path} - Duration: {duration:.3f}s", extra=response_info)

        # 添加响应头
        response.headers['X-Request-ID'] = getattr(g, 'request_id', 'unknown')
        if hasattr(g, 'start_time'):
            duration = time.time() - g.start_time
            response.headers['X-Response-Time'] = f"{duration:.3f}s"

        return response
