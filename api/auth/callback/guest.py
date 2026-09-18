import os
import secrets
import urllib.parse
from datetime import datetime
from flask import session, redirect
from flask_jwt_extended import create_access_token, create_refresh_token
from api.base import handle_api_error
from models import db, User
from models.user import UserRole, UserStatus


from ..auth_submodule import auth_bp


def get_or_create_guest_user():
    """获取或创建访客用户"""
    # 查找现有的访客用户
    guest_user = User.query.filter_by(username='guest').first()

    if not guest_user:
        # 创建新的访客用户
        guest_user = User(
            username='guest',
            email='guest@todo4ai.local',
            role=UserRole.GUEST,
            status=UserStatus.ACTIVE,
            bio='Guest user account'
        )
        db.session.add(guest_user)
        db.session.commit()

    return guest_user


@auth_bp.route('/guest/callback', methods=['GET'])
def guest_callback():
    """Guest游客登录回调处理"""
    try:
        # 获取或创建访客用户
        guest_user = get_or_create_guest_user()

        # 更新最后登录时间
        guest_user.last_login_at = datetime.utcnow()
        db.session.commit()

        # 生成真实的JWT令牌
        access_token = create_access_token(
            identity=guest_user.id,
            additional_claims={
                'username': guest_user.username,
                'email': guest_user.email,
                'role': guest_user.role.value,
                'provider': 'guest'
            }
        )

        refresh_token = create_refresh_token(
            identity=guest_user.id,
            additional_claims={
                'username': guest_user.username,
                'email': guest_user.email,
                'role': guest_user.role.value,
                'provider': 'guest'
            }
        )

        # 获取重定向URL
        is_docker = os.environ.get('DOCKER_ENV') == 'true'
        if is_docker:
            base_url = os.environ.get('BASE_URL', 'https://todo4ai.org')
            default_dashboard = f'{base_url}/todo-for-ai/pages'
        else:
            default_dashboard = 'http://localhost:50112/todo-for-ai/pages'
        redirect_url = session.pop('redirect_after_login', default_dashboard)

        # 重定向到前端，并在URL中包含JWT令牌
        params = {
            'access_token': access_token,
            'refresh_token': refresh_token,
            'token_type': 'Bearer'
        }
        query_string = urllib.parse.urlencode(params)
        return redirect(f"{redirect_url}?{query_string}")

    except Exception as e:
        return handle_api_error(e)




