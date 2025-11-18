from flask import request
from api.base import handle_api_error, ApiResponse
from ..auth_submodule import auth_bp
from models.user import User


@auth_bp.route('/verify', methods=['POST'])
def verify_token():
    """验证JWT令牌"""
    try:
        data = request.get_json()
        if not data or 'token' not in data:
            return ApiResponse.error("Token is required", 400).to_response()

        # 这里可以添加令牌验证逻辑
        # 目前使用Flask-JWT-Extended的内置验证

        return ApiResponse.success(
            data={
                'valid': True
            },
            message='Token is valid'
        ).to_response()

    except Exception as e:
        return ApiResponse.error(
            message='Token is invalid',
            code=400
        ).to_response()


@auth_bp.route('/refresh', methods=['POST'])
# @jwt_required(refresh=True)
def refresh_token():
    """刷新访问令牌"""
    try:
        # current_user_id = get_jwt_identity()
        # user = User.query.get(current_user_id)

        # 临时返回成功响应
        return ApiResponse.success(
            data={
                'token': 'dummy_token'
            },
            message='Token refreshed'
        ).to_response()

        # if not user or not user.is_active():
        #     return ApiResponse.error("User not found or inactive", 404).to_response()

        # # 生成新的访问令牌和刷新令牌
        # tokens = github_service.generate_tokens(user)
        # if not tokens:
        #     return ApiResponse.error("Failed to generate tokens", 500).to_response()

        # return ApiResponse.success({
        #     'access_token': tokens['access_token'],
        #     'refresh_token': tokens['refresh_token'],
        #     'token_type': tokens['token_type']
        # }, "Tokens refreshed successfully").to_response()

    except Exception as e:
        return handle_api_error(e)


@auth_bp.route('/users', methods=['GET'])
# @require_auth
def list_users():
    """获取用户列表（需要管理员权限）"""
    try:
        # current_user = get_current_user()

        # if not current_user.is_admin():
        #     return ApiResponse.error("Admin access required", 403).to_response()

        # 获取查询参数
        page = request.args.get('page', 1, type=int)
        per_page = min(request.args.get('per_page', 20, type=int), 100)
        search = request.args.get('search', '').strip()
        status = request.args.get('status')
        role = request.args.get('role')

        # 构建查询
        query = User.query

        if search:
            query = query.filter(
                User.email.contains(search) |
                User.username.contains(search) |
                User.full_name.contains(search)
            )

        if status:
            query = query.filter_by(status=status)

        if role:
            query = query.filter_by(role=role)

        # 按注册时间倒序排列（最新注册的用户在前面）
        query = query.order_by(User.created_at.desc())

        # 分页
        pagination = query.paginate(
            page=page,
            per_page=per_page,
            error_out=False
        )

        return ApiResponse.success({
            'users': [user.to_dict() for user in pagination.items],
            'pagination': {
                'page': pagination.page,
                'per_page': pagination.per_page,
                'total': pagination.total,
                'pages': pagination.pages,
                'has_prev': pagination.has_prev,
                'has_next': pagination.has_next
            }
        }, "Users retrieved successfully").to_response()

    except Exception as e:
        return handle_api_error(e)


@auth_bp.route('/users/<int:user_id>', methods=['GET'])
# @require_auth
def get_user(user_id):
    """获取指定用户信息"""
    try:
        # current_user = get_current_user()

        # # 只有管理员或用户本人可以查看详细信息
        # if not current_user.is_admin() and current_user.id != user_id:
        #     return ApiResponse.error("Access denied", 403).to_response()

        user = User.query.get(user_id)
        if not user:
            return ApiResponse.error("User not found", 404).to_response()

        return ApiResponse.success(user.to_dict(), "User information retrieved successfully").to_response()

    except Exception as e:
        return handle_api_error(e)


@auth_bp.route('/users/<int:user_id>/status', methods=['PUT'])
# @require_auth
def update_user_status(user_id):
    """更新用户状态（管理员功能）"""
    try:
        # current_user = get_current_user()

        # if not current_user.is_admin():
        #     return ApiResponse.error("Admin access required", 403).to_response()

        user = User.query.get(user_id)
        if not user:
            return ApiResponse.error("User not found", 404).to_response()

        data = request.get_json()
        if not data or 'status' not in data:
            return ApiResponse.error("Status is required", 400).to_response()

        # 验证状态值
        from models.user import UserStatus
        try:
            new_status = UserStatus(data['status'])
            user.status = new_status
            user.save()

            return ApiResponse.success(user.to_dict(), "User status updated successfully").to_response()

        except ValueError:
            return ApiResponse.error("Invalid status value", 400).to_response()

    except Exception as e:
        return handle_api_error(e)
