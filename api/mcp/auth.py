import time
from functools import wraps

from flask import g, jsonify, request

from models import ApiToken


def require_api_token_auth(f):
    """API Token认证装饰器 - 专门用于MCP接口"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        from flask import current_app

        auth_start_time = time.time()
        auth_id = f"auth-{int(time.time() * 1000)}-{id(request)}"

        current_app.logger.debug(f"[AUTH_START] {auth_id} API token authentication started", extra={
            'auth_id': auth_id,
            'endpoint': request.endpoint,
            'method': request.method,
            'path': request.path,
            'remote_addr': request.remote_addr,
            'user_agent': request.headers.get('User-Agent', 'Unknown')
        })

        # 从请求头获取token
        auth_header = request.headers.get('Authorization')

        current_app.logger.debug(f"[AUTH_HEADER] {auth_id} Authorization header check", extra={
            'auth_id': auth_id,
            'has_auth_header': bool(auth_header),
            'header_format_valid': bool(auth_header and auth_header.startswith('Bearer ')) if auth_header else False
        })

        if not auth_header or not auth_header.startswith('Bearer '):
            current_app.logger.warning(f"[AUTH_FAILED] {auth_id} Missing or invalid authorization header", extra={
                'auth_id': auth_id,
                'auth_header_present': bool(auth_header),
                'auth_header_format': auth_header[:20] + '...' if auth_header and len(auth_header) > 20 else auth_header
            })
            return jsonify({'error': 'Missing or invalid authorization header'}), 401

        token = auth_header.split(' ')[1]
        token_prefix = token[:8] + '...' if len(token) > 8 else token

        current_app.logger.debug(f"[AUTH_TOKEN] {auth_id} Token extracted", extra={
            'auth_id': auth_id,
            'token_prefix': token_prefix,
            'token_length': len(token)
        })

        # 验证token
        token_verify_start = time.time()
        api_token = ApiToken.verify_token(token)
        token_verify_duration = time.time() - token_verify_start

        current_app.logger.debug(f"[AUTH_VERIFY] {auth_id} Token verification completed", extra={
            'auth_id': auth_id,
            'token_prefix': token_prefix,
            'verify_duration_ms': round(token_verify_duration * 1000, 2),
            'token_valid': bool(api_token),
            'token_id': api_token.id if api_token else None,
            'user_id': api_token.user.id if api_token and api_token.user else None
        })

        if not api_token:
            current_app.logger.warning(f"[AUTH_FAILED] {auth_id} Invalid or expired token", extra={
                'auth_id': auth_id,
                'token_prefix': token_prefix,
                'verify_duration_ms': round(token_verify_duration * 1000, 2)
            })
            return jsonify({'error': 'Invalid or expired token'}), 401

        # 将token信息添加到g对象
        g.api_token = api_token
        g.current_user = api_token.user

        auth_duration = time.time() - auth_start_time
        current_app.logger.info(f"[AUTH_SUCCESS] {auth_id} Authentication successful", extra={
            'auth_id': auth_id,
            'auth_duration_ms': round(auth_duration * 1000, 2),
            'token_id': api_token.id,
            'user_id': api_token.user.id,
            'user_email': api_token.user.email if hasattr(api_token.user, 'email') else None,
            'token_name': api_token.name if hasattr(api_token, 'name') else None
        })

        return f(*args, **kwargs)

    return decorated_function
