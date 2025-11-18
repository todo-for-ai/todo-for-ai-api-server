import os
from flask import redirect
from api.base import handle_api_error, ApiResponse

from ..auth_submodule import auth_bp

@auth_bp.route('/google/callback', methods=['GET'])
def google_callback():
    """Google OAuth回调处理"""
    try:
        # 获取授权码并交换令牌
        token = google_service.oauth.google.authorize_access_token()

        if not token:
            return ApiResponse.error("Failed to get access token from Google", 400).to_response()

        # 获取用户信息
        user_info = google_service.get_user_info(token['access_token'])
        if not user_info:
            return ApiResponse.error("Failed to get user information from Google", 400).to_response()

        # 创建或更新用户
        user = google_service.create_or_update_user(user_info)
        if not user:
            return ApiResponse.error("Failed to create or update user", 500).to_response()

        # 生成JWT令牌
        tokens = google_service.generate_tokens(user)
        if not tokens:
            return ApiResponse.error("Failed to generate tokens", 500).to_response()

        # 获取重定向URL，默认到dashboard - 根据环境动态设置
        is_docker = os.environ.get('DOCKER_ENV') == 'true'
        if is_docker:
            base_url = os.environ.get('BASE_URL', 'https://todo4ai.org')
            default_dashboard = f'{base_url}/todo-for-ai/pages/dashboard'
        else:
            default_dashboard = 'http://localhost:50111/todo-for-ai/pages/dashboard'
        redirect_url = session.pop('redirect_after_login', default_dashboard)

        # 重定向到前端，并在URL中包含令牌（包括access_token和refresh_token）
        import urllib.parse
        params = {
            'access_token': tokens['access_token'],
            'refresh_token': tokens['refresh_token'],
            'token_type': tokens['token_type']
        }
        query_string = urllib.parse.urlencode(params)
        return redirect(f"{redirect_url}?{query_string}")

    except Exception as e:
        return handle_api_error(e)


