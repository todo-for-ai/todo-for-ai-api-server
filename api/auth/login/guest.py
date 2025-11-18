import os
from flask import session, request, redirect
from api.base import handle_api_error


from ..auth_submodule import auth_bp

@auth_bp.route('/login/guest', methods=['GET'])
def guest_login():
    """Guest游客登录流程"""
    try:
        # 获取重定向URL - 根据环境动态设置
        is_docker = os.environ.get('DOCKER_ENV') == 'true'
        if is_docker:
            base_url = os.environ.get('BASE_URL', 'https://todo4ai.org')
            frontend_base = base_url
        else:
            frontend_base = 'http://localhost:50112'

        # 存储原始重定向URL，确保重定向到前端dashboard
        return_to = request.args.get('return_to', '/todo-for-ai/pages')
        if return_to.startswith('/'):
            return_to = f'{frontend_base}{return_to}'
        elif not (frontend_base in return_to):
            return_to = f'{frontend_base}/todo-for-ai/pages'

        session['redirect_after_login'] = return_to

        # 重定向到guest回调端点
        return redirect('/todo-for-ai/api/v1/auth/guest/callback')

    except Exception as e:
        return handle_api_error(e)



