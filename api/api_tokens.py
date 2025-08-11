"""
API Token管理接口
"""

from flask import Blueprint, request, g
from functools import wraps
import secrets
import hashlib
from datetime import datetime, timedelta

from models import db, ApiToken, User
from core.auth import unified_auth_required, get_current_user
from .base import ApiResponse

api_tokens_bp = Blueprint('api_tokens', __name__)


def require_api_token_auth(f):
    """API Token认证装饰器"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # 从请求头获取token
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return ApiResponse.error('Missing or invalid authorization header', 401).to_response()
        
        token = auth_header.split(' ')[1]
        
        # 验证token
        api_token = ApiToken.verify_token(token)
        if not api_token:
            return ApiResponse.error('Invalid or expired token', 401).to_response()
        
        # 将token信息添加到g对象
        g.api_token = api_token
        g.current_user = api_token.user
        
        return f(*args, **kwargs)
    
    return decorated_function


@api_tokens_bp.route('', methods=['GET'])
@unified_auth_required
def list_tokens():
    """获取当前用户的API Token列表"""
    try:
        user_id = g.current_user.id
        
        # 获取用户的所有token
        tokens = ApiToken.query.filter_by(user_id=user_id).order_by(ApiToken.created_at.desc()).all()
        
        # 转换为字典格式（不包含敏感信息）
        token_list = [token.to_dict(include_sensitive=False) for token in tokens]
        
        return ApiResponse.success({
            'items': token_list,
            'pagination': {
                'page': 1,
                'per_page': len(token_list),
                'total': len(token_list),
                'has_prev': False,
                'has_next': False
            }
        }, "API tokens retrieved successfully").to_response()

    except Exception as e:
        return ApiResponse.error(f"Failed to retrieve API tokens: {str(e)}", 500).to_response()


@api_tokens_bp.route('', methods=['POST'])
@unified_auth_required
def create_token():
    """创建新的API Token"""
    try:
        data = request.get_json()
        
        # 验证必需字段
        if not data or not data.get('name'):
            return ApiResponse.error('Token name is required', 400).to_response()
        
        user_id = g.current_user.id
        name = data.get('name')
        description = data.get('description', '')
        expires_days = data.get('expires_days')
        
        # 检查token名称是否已存在
        existing_token = ApiToken.query.filter_by(
            user_id=user_id,
            name=name,
            is_active=True
        ).first()
        
        if existing_token:
            return ApiResponse.error('Token name already exists', 400).to_response()
        
        # 生成新token
        api_token, raw_token = ApiToken.generate_token(
            name=name,
            description=description,
            expires_days=expires_days
        )
        
        # 设置用户ID
        api_token.user_id = user_id
        
        # 保存到数据库
        db.session.add(api_token)
        db.session.commit()
        
        # 返回token信息（包含原始token，仅此一次）
        response_data = api_token.to_dict(include_sensitive=False)
        response_data['token'] = raw_token  # 仅在创建时返回原始token
        
        return ApiResponse.created(
            response_data,
            'API Token created successfully. Please save the token as it will not be shown again.'
        ).to_response()

    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to create API token: {str(e)}", 500).to_response()


@api_tokens_bp.route('/<int:token_id>', methods=['PUT'])
@unified_auth_required
def update_token(token_id):
    """更新API Token"""
    try:
        user_id = g.current_user.id
        
        # 查找token
        api_token = ApiToken.query.filter_by(
            id=token_id,
            user_id=user_id
        ).first()
        
        if not api_token:
            return ApiResponse.error('Token not found', 404).to_response()
        
        data = request.get_json()
        if not data:
            return ApiResponse.error('No data provided', 400).to_response()
        
        # 更新字段
        if 'name' in data:
            # 检查新名称是否已存在
            existing_token = ApiToken.query.filter_by(
                user_id=user_id,
                name=data['name'],
                is_active=True
            ).filter(ApiToken.id != token_id).first()
            
            if existing_token:
                return ApiResponse.error('Token name already exists', 400).to_response()
            
            api_token.name = data['name']
        
        if 'description' in data:
            api_token.description = data['description']
        
        if 'expires_days' in data:
            expires_days = data['expires_days']
            if expires_days:
                api_token.expires_at = datetime.utcnow() + timedelta(days=expires_days)
            else:
                api_token.expires_at = None
        
        if 'is_active' in data:
            api_token.is_active = data['is_active']
        
        # 保存更改
        db.session.commit()
        
        return ApiResponse.success(
            api_token.to_dict(include_sensitive=False),
            'Token updated successfully'
        ).to_response()
        
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f'Operation failed: {str(e)}', 500).to_response()


@api_tokens_bp.route('/<int:token_id>/reveal', methods=['GET'])
@unified_auth_required
def reveal_token(token_id):
    """获取解密的完整token"""
    try:
        user_id = g.current_user.id

        # 查找token，确保只能查看自己的token
        api_token = ApiToken.query.filter_by(
            id=token_id,
            user_id=user_id,
            is_active=True
        ).first()

        if not api_token:
            return ApiResponse.error('Token not found', 404).to_response()

        # 获取解密的token
        decrypted_token = api_token.get_decrypted_token()

        if not decrypted_token:
            return ApiResponse.error('Unable to decrypt token. This token may have been created before encryption was enabled.', 400).to_response()

        return ApiResponse.success({
            'token': decrypted_token,
            'name': api_token.name,
            'prefix': api_token.prefix
        }, 'Token revealed successfully').to_response()

    except Exception as e:
        return ApiResponse.error(f'Operation failed: {str(e)}', 500).to_response()


@api_tokens_bp.route('/<int:token_id>', methods=['DELETE'])
@unified_auth_required
def delete_token(token_id):
    """删除API Token"""
    try:
        user_id = g.current_user.id
        
        # 查找token
        api_token = ApiToken.query.filter_by(
            id=token_id,
            user_id=user_id
        ).first()
        
        if not api_token:
            return ApiResponse.error('Token not found', 404).to_response()
        
        # 软删除（设置为非活跃状态）
        api_token.deactivate()
        
        return ApiResponse.success(None, 'Token deleted successfully').to_response()
        
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f'Operation failed: {str(e)}', 500).to_response()


@api_tokens_bp.route('/verify', methods=['POST'])
def verify_token():
    """验证API Token（用于MCP认证）"""
    try:
        data = request.get_json()
        if not data or not data.get('token'):
            return ApiResponse.error('Token is required', 400).to_response()
        
        token = data.get('token')
        
        # 验证token
        api_token = ApiToken.verify_token(token)
        if not api_token:
            return ApiResponse.error('Invalid or expired token', 401).to_response()
        
        # 返回token信息和用户信息
        return ApiResponse.success({
            'token': api_token.to_dict(include_sensitive=False),
            'user': api_token.user.to_public_dict()
        }, 'Token verified successfully').to_response()
        
    except Exception as e:
        return ApiResponse.error(f'Operation failed: {str(e)}', 500).to_response()
