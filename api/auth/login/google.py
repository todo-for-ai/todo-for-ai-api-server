import os
from flask import request, session, redirect
from api.base import handle_api_error

from ..auth_submodule import auth_bp

@auth_bp.route('/login/google', methods=['GET'])
def google_login():
    """启动Google登录流程"""
    try:
        # 获取重定向URL - 根据环境动态设置
        is_docker = os.environ.get('DOCKER_ENV') == 'true'
        if is_docker:
            # 生产环境使用环境变量配置的域名，默认为SaaS域名
            base_url = os.environ.get('BASE_URL', 'https://todo4ai.org')
            default_redirect_uri = f'{base_url}/todo-for-ai/api/v1/auth/google/callback'
            frontend_base = base_url
        else:
            # 开发环境使用localhost
            default_redirect_uri = 'http://localhost:50110/todo-for-ai/api/v1/auth/google/callback'
            frontend_base = 'http://localhost:50111'

        redirect_uri = request.args.get('redirect_uri', default_redirect_uri)

        # 存储原始重定向URL，确保重定向到前端dashboard
        return_to = request.args.get('return_to', '/todo-for-ai/pages/dashboard')

        # 如果是相对路径，转换为前端完整URL
        if return_to.startswith('/'):
            return_to = f'{frontend_base}{return_to}'
        # 如果是后端URL，替换为前端URL
        elif 'localhost:50110' in return_to or 'todo4ai.org' in return_to:
            # 统一替换为当前环境的前端地址
            if return_to.startswith('http://localhost:50110'):
                return_to = return_to.replace('http://localhost:50110', frontend_base)
            elif return_to.startswith('https://todo4ai.org/todo-for-ai/api'):
                return_to = return_to.replace('https://todo4ai.org/todo-for-ai/api/v1', frontend_base + '/todo-for-ai/pages')
        # 如果没有指定，默认到dashboard
        elif not (frontend_base in return_to):
            return_to = f'{frontend_base}/todo-for-ai/pages/dashboard'

        session['redirect_after_login'] = return_to
        session['auth_provider'] = 'google'

        # 重定向到Google登录页面
        return google_service.oauth.google.authorize_redirect(redirect_uri)

    except Exception as e:
        return handle_api_error(e)


