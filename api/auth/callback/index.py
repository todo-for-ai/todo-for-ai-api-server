import os
from flask import request, redirect
from api.base import handle_api_error, ApiResponse

from ..auth_submodule import auth_bp

@auth_bp.route('/callback', methods=['GET'])
def callback():
    """GitHub OAuth回调处理（保持向后兼容）"""
    return github_callback()


@auth_bp.route('/callback/github', methods=['GET'])
def github_callback():
    """GitHub OAuth回调处理"""
    try:
        # 手动处理OAuth回调，避免state验证问题
        code = request.args.get('code')
        if not code:
            return ApiResponse.error("Authorization code not found", 400).to_response()

        # 直接使用code交换token，跳过Authlib的state验证
        import requests
        token_data = {
            'client_id': github_service.config.client_id,
            'client_secret': github_service.config.client_secret,
            'code': code
        }

        token_response = requests.post(
            'https://github.com/login/oauth/access_token',
            data=token_data,
            headers={'Accept': 'application/json'},
            timeout=180
        )

        if token_response.status_code != 200:
            return ApiResponse.error("Failed to exchange code for token", 400).to_response()

        token_json = token_response.json()
        if 'access_token' not in token_json:
            return ApiResponse.error("Access token not found in response", 400).to_response()

        token = {'access_token': token_json['access_token']}

        # 获取用户信息
        user_info = github_service.get_user_info(token['access_token'])
        if not user_info:
            return ApiResponse.error("Failed to get user information", 400).to_response()

        # 创建或更新用户
        user = github_service.create_or_update_user(user_info)
        if not user:
            return ApiResponse.error("Failed to create or update user", 500).to_response()

        # 生成JWT令牌
        tokens = github_service.generate_tokens(user)
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


