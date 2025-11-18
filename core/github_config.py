"""
GitHub OAuth 配置和服务
"""

import os
from datetime import timedelta
from flask import jsonify, current_app
from flask_jwt_extended import JWTManager, create_access_token, create_refresh_token
from authlib.integrations.flask_client import OAuth

# 导入拆分的模块
from core.github_user_service import github_user_service
from core.github_defaults_service import github_defaults_service
from core.github_auth import require_auth, get_current_user


class GitHubConfig:
    """GitHub OAuth 配置类"""
    def __init__(self):
        self.client_id = os.environ.get('GITHUB_CLIENT_ID')
        self.client_secret = os.environ.get('GITHUB_CLIENT_SECRET')
        
        if not self.client_id or not self.client_secret:
            raise ValueError("GitHub OAuth 配置缺失")


class GitHubService:
    """GitHub 主服务类 - 负责 OAuth 和 JWT 配置"""
    def __init__(self, app=None):
        self.app = app
        self.config = None
        self.oauth = None
        self.jwt_manager = None
        self.user_service = github_user_service
        self.defaults_service = github_defaults_service
        
        if app:
            self.init_app(app)
    
    def init_app(self, app):
        """初始化 GitHub 服务"""
        self.app = app
        self.config = GitHubConfig()
        
        # 初始化 OAuth
        self.oauth = OAuth(app)
        self.oauth.register(
            'github',
            client_id=self.config.client_id,
            client_secret=self.config.client_secret,
            authorize_url='https://github.com/login/oauth/authorize',
            access_token_url='https://github.com/login/oauth/access_token',
            userinfo_endpoint='https://api.github.com/user',
            client_kwargs={'scope': 'user:email'}
        )
        
        # 初始化 JWT
        self.jwt_manager = JWTManager(app)
        # JWT配置已在config.py中设置，这里不再覆盖
        # 确保JWT_ACCESS_TOKEN_EXPIRES使用timedelta对象
        if isinstance(app.config.get('JWT_ACCESS_TOKEN_EXPIRES'), int):
            app.config['JWT_ACCESS_TOKEN_EXPIRES'] = timedelta(seconds=app.config['JWT_ACCESS_TOKEN_EXPIRES'])
        if isinstance(app.config.get('JWT_REFRESH_TOKEN_EXPIRES'), int):
            app.config['JWT_REFRESH_TOKEN_EXPIRES'] = timedelta(seconds=app.config['JWT_REFRESH_TOKEN_EXPIRES'])
        
        self._register_jwt_handlers()
    
    def _register_jwt_handlers(self):
        """注册 JWT 错误处理器"""
        @self.jwt_manager.expired_token_loader
        def expired_token_callback(jwt_header, jwt_payload):
            return jsonify({'error': 'token_expired'}), 401
        
        @self.jwt_manager.invalid_token_loader
        def invalid_token_callback(error):
            return jsonify({'error': 'invalid_token'}), 401
        
        @self.jwt_manager.unauthorized_loader
        def missing_token_callback(error):
            return jsonify({'error': 'authorization_required'}), 401
    
    def get_user_info(self, access_token):
        """获取 GitHub 用户信息 - 委托给用户服务"""
        return self.user_service.get_user_info(access_token)
    
    def create_or_update_user(self, github_user_info):
        """创建或更新用户 - 委托给用户服务"""
        return self.user_service.create_or_update_user(github_user_info, self.defaults_service)
    
    def generate_tokens(self, user):
        """为用户生成JWT令牌（包括access token和refresh token）"""
        try:
            # 生成access token
            access_token = create_access_token(
                identity=user.id,
                additional_claims={
                    'username': user.username,
                    'email': user.email,
                    'github_id': user.github_id,
                    'provider': 'github'
                }
            )

            # 生成refresh token
            refresh_token = create_refresh_token(
                identity=user.id,
                additional_claims={
                    'username': user.username,
                    'email': user.email,
                    'github_id': user.github_id,
                    'provider': 'github'
                }
            )

            return {
                'access_token': access_token,
                'refresh_token': refresh_token,
                'token_type': 'Bearer'
            }
        except Exception as e:
            current_app.logger.error(f"生成令牌失败: {str(e)}")
            return None


# 创建全局实例
github_service = GitHubService()


# 导出常用函数和装饰器
__all__ = ['GitHubConfig', 'GitHubService', 'github_service', 'require_auth', 'get_current_user']
