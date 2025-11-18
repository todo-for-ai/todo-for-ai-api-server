from flask import request
from api.base import handle_api_error, ApiResponse

from ..auth_submodule import auth_bp

@auth_bp.route('/logout', methods=['POST'])
# @require_auth
def logout():
    """用户登出"""
    try:
        current_user = get_current_user()
        
        # 记录登出时间
        current_user.last_active_at = None
        current_user.save()

        # 简单的登出响应（不再使用Auth0）
        return_to = request.json.get('return_to', 'http://localhost:50111/todo-for-ai/pages')

        return ApiResponse.success({
            'message': 'Logout successful',
            'redirect_url': return_to
        }, 'Logout successful').to_response()
        
    except Exception as e:
        return handle_api_error(e)


@auth_bp.route('/me', methods=['GET'])
# @require_auth
def get_current_user_info():
    """获取当前用户信息"""
    try:
        current_user = get_current_user()
        return ApiResponse.success(
            data=current_user.to_dict(),
            message='User information retrieved successfully'
        ).to_response()
        
    except Exception as e:
        return handle_api_error(e)


@auth_bp.route('/me', methods=['PUT'])
# @require_auth
def update_current_user():
    """更新当前用户信息"""
    try:
        current_user = get_current_user()
        
        if not request.is_json:
            return ApiResponse.error("Content-Type must be application/json", 400).to_response()
        
        data = request.get_json()
        
        # 允许更新的字段
        allowed_fields = ['nickname', 'full_name', 'bio', 'timezone', 'locale']
        
        for field in allowed_fields:
            if field in data:
                setattr(current_user, field, data[field])
        
        # 处理偏好设置
        if 'preferences' in data:
            if not current_user.preferences:
                current_user.preferences = {}
            current_user.preferences.update(data['preferences'])
        
        current_user.save()
        
        return ApiResponse.success(current_user.to_dict(), "User information updated successfully").to_response()
        
    except Exception as e:
        return handle_api_error(e)


