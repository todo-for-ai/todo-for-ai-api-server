"""
认证相关功能
"""

from functools import wraps
from flask import request, jsonify, g
from flask_jwt_extended import get_jwt_identity, verify_jwt_in_request, jwt_required as flask_jwt_required
from models import ApiToken, User
from api.base import ApiResponse


def token_required(f):
    """Token认证装饰器"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        token = None
        
        # 从Authorization header获取token
        auth_header = request.headers.get('Authorization')
        if auth_header and auth_header.startswith('Bearer '):
            token = auth_header.split(' ')[1]
        
        # 从query参数获取token（备用方式）
        if not token:
            token = request.args.get('token')
        
        if not token:
            return ApiResponse.unauthorized('Token is missing').to_response()
        
        # 验证token
        api_token = ApiToken.verify_token(token)
        if not api_token:
            return ApiResponse.unauthorized('Invalid token').to_response()
        
        # 将token信息存储到g对象中
        g.current_token = api_token
        
        return f(*args, **kwargs)
    
    return decorated_function


def optional_token_auth(f):
    """可选的Token认证装饰器"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        token = None
        
        # 从Authorization header获取token
        auth_header = request.headers.get('Authorization')
        if auth_header and auth_header.startswith('Bearer '):
            token = auth_header.split(' ')[1]
        
        # 从query参数获取token（备用方式）
        if not token:
            token = request.args.get('token')
        
        # 如果有token，验证它
        if token:
            api_token = ApiToken.verify_token(token)
            if api_token:
                g.current_token = api_token
            else:
                g.current_token = None
        else:
            g.current_token = None
        
        return f(*args, **kwargs)
    
    return decorated_function


def get_current_token():
    """获取当前请求的token"""
    return getattr(g, 'current_token', None)


def is_authenticated():
    """检查当前请求是否已认证"""
    return get_current_token() is not None


def unified_auth_required(f):
    """
    统一认证装饰器 - 同时支持JWT和API Token认证

    认证优先级：
    1. 首先尝试API Token认证
    2. 如果没有API Token，尝试JWT认证
    3. 将认证成功的用户信息存储到g.current_user
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        current_user = None
        auth_method = None

        # 1. 尝试API Token认证
        auth_header = request.headers.get('Authorization')
        if auth_header and auth_header.startswith('Bearer '):
            token = auth_header.split(' ')[1]

            # 验证API Token
            api_token = ApiToken.verify_token(token)
            if api_token:
                current_user = api_token.user
                auth_method = 'api_token'
                g.current_token = api_token
                g.current_user = current_user
                g.auth_method = auth_method
                return f(*args, **kwargs)

        # 2. 尝试JWT认证
        try:
            verify_jwt_in_request()
            user_id = get_jwt_identity()
            if user_id:
                current_user = User.query.get(user_id)
                if current_user and current_user.is_active():
                    auth_method = 'jwt'
                    g.current_user = current_user
                    g.current_token = None
                    g.auth_method = auth_method
                    return f(*args, **kwargs)
        except Exception:
            # JWT验证失败，继续尝试其他认证方式
            pass

        # 3. 认证失败
        return ApiResponse.unauthorized('Authentication required').to_response()

    return decorated_function


def optional_unified_auth(f):
    """
    可选的统一认证装饰器 - 同时支持JWT和API Token认证

    如果认证成功，将用户信息存储到g.current_user
    如果认证失败，g.current_user为None，但不会返回错误
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        current_user = None
        auth_method = None

        # 1. 尝试API Token认证
        auth_header = request.headers.get('Authorization')
        if auth_header and auth_header.startswith('Bearer '):
            token = auth_header.split(' ')[1]

            # 验证API Token
            api_token = ApiToken.verify_token(token)
            if api_token:
                current_user = api_token.user
                auth_method = 'api_token'
                g.current_token = api_token
                g.current_user = current_user
                g.auth_method = auth_method
                return f(*args, **kwargs)

        # 2. 尝试JWT认证
        try:
            verify_jwt_in_request(optional=True)
            user_id = get_jwt_identity()
            if user_id:
                current_user = User.query.get(user_id)
                if current_user and current_user.is_active():
                    auth_method = 'jwt'
                    g.current_user = current_user
                    g.current_token = None
                    g.auth_method = auth_method
                    return f(*args, **kwargs)
        except Exception:
            # JWT验证失败，继续
            pass

        # 3. 没有认证或认证失败，设置为None
        g.current_user = None
        g.current_token = None
        g.auth_method = None
        return f(*args, **kwargs)

    return decorated_function


def get_current_user():
    """获取当前认证的用户"""
    return getattr(g, 'current_user', None)


def get_auth_method():
    """获取当前的认证方式"""
    return getattr(g, 'auth_method', None)
