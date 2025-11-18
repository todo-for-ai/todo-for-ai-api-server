import os
import secrets
import urllib.parse
from flask import session, redirect
from api.base import handle_api_error


from ..auth_submodule import auth_bp

@auth_bp.route('/guest/callback', methods=['GET'])
def guest_callback():
    """Guest游客登录回调处理"""
    try:
        # 临时跳过数据库操作，直接生成模拟令牌
        # 生成模拟JWT令牌
        fake_token = secrets.token_hex(32)

        # 获取重定向URL
        is_docker = os.environ.get('DOCKER_ENV') == 'true'
        if is_docker:
            base_url = os.environ.get('BASE_URL', 'https://todo4ai.org')
            default_dashboard = f'{base_url}/todo-for-ai/pages'
        else:
            default_dashboard = 'http://localhost:50112/todo-for-ai/pages'
        redirect_url = session.pop('redirect_after_login', default_dashboard)

        # 重定向到前端，并在URL中包含模拟令牌
        params = {
            'access_token': fake_token,
            'refresh_token': secrets.token_hex(32),
            'token_type': 'Bearer'
        }
        query_string = urllib.parse.urlencode(params)
        return redirect(f"{redirect_url}?{query_string}")

    except Exception as e:
        return handle_api_error(e)




