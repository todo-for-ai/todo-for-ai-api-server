"""
GitHub 认证模块 - 负责认证装饰器和当前用户获取
"""

from functools import wraps
from flask import jsonify, g, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity
from models import User


def require_auth(f):
    """认证装饰器 - 要求用户登录并验证状态"""
    @wraps(f)
    @jwt_required()
    def decorated_function(*args, **kwargs):
        try:
            current_user_id = get_jwt_identity()
            current_user = User.query.get(current_user_id)

            if not current_user:
                return jsonify({'error': 'user_not_found'}), 401

            # 检查用户状态，只有active状态的用户才能访问
            if not current_user.is_active():
                return jsonify({
                    'error': 'user_suspended',
                    'message': 'Your account has been suspended. Please contact administrator.'
                }), 403

            g.current_user = current_user
            return f(*args, **kwargs)
        except Exception as e:
            current_app.logger.error(f"认证失败: {str(e)}")
            return jsonify({'error': 'authentication_failed'}), 401

    return decorated_function


def get_current_user():
    """获取当前登录用户"""
    return getattr(g, 'current_user', None)
