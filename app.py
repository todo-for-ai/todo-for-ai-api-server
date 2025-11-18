#!/usr/bin/env python3
"""
Todo for AI - Flask 应用入口

主要功能:
- Flask 应用初始化
- 数据库连接
- API 路由注册
- MCP 服务器启动
"""

import os
from flask import Flask, jsonify
from flask_cors import CORS
from flask_migrate import Migrate
from dotenv import load_dotenv

# 加载.env文件
load_dotenv()

# 导入模型和配置
from models import db
from core.config import config
from core.middleware import setup_all_middleware
from core.github_config import github_service
from core.google_config import google_service


def create_app(config_name=None):
    """应用工厂函数"""
    if config_name is None:
        config_name = os.environ.get('FLASK_ENV', 'development')
    
    app = Flask(__name__)
    app.config.from_object(config[config_name])
    
    # 初始化配置
    config[config_name].init_app(app)
    
    # 初始化扩展
    db.init_app(app)

    # 初始化CORS（开发环境必需，生产环境可选）
    CORS(app,
         origins=app.config['CORS_ORIGINS'],
         supports_credentials=True,
         allow_headers=['Content-Type', 'Authorization'],
         methods=['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'])

    # 初始化OAuth服务
    github_service.init_app(app)

    # 初始化Google OAuth服务（如果配置了的话）
    try:
        google_service.init_app(app)
    except ValueError as e:
        # Google OAuth配置缺失，跳过初始化
        app.logger.warning(f"Google OAuth未配置: {e}")
        pass

    # 数据库迁移
    migrate = Migrate(app, db)

    # 设置中间件（临时注释）
    # setup_all_middleware(app)

    # 注册蓝图
    register_blueprints(app)

    # 注册命令
    register_commands(app)
    
    return app


def register_blueprints(app):
    """注册蓝图"""
    # 基础路由
    @app.route('/')
    def index():
        from api.base import ApiResponse
        return ApiResponse.success(
            data={
                'service': 'Todo for AI API',
                'version': '1.0.0',
                'status': 'running'
            },
            message='Welcome to Todo for AI API'
        ).to_response()

    @app.route('/health')
    def health_check():
        """健康检查 - 行业标准路径"""
        from api.base import ApiResponse
        try:
            # 测试数据库连接
            with db.engine.connect() as connection:
                connection.execute(db.text('SELECT 1'))
            db_status = 'connected'
        except Exception as e:
            db_status = f'error: {str(e)}'

        return ApiResponse.success(
            data={
                'status': 'healthy',
                'database': db_status
            },
            message='Service is healthy'
        ).to_response()


    @app.route('/todo-for-ai/api/v1/health')
    def api_health_check():
        """API健康检查 - 标准路径"""
        from api.base import ApiResponse
        try:
            # 测试数据库连接
            with db.engine.connect() as connection:
                connection.execute(db.text('SELECT 1'))
            db_status = 'connected'
        except Exception as e:
            db_status = f'error: {str(e)}'

        return ApiResponse.success(
            data={
                'status': 'healthy',
                'service': 'Todo for AI API',
                'version': '1.0.0',
                'database': db_status,
                'environment': app.config.get('ENV', 'development')
            },
            message='API service is healthy'
        ).to_response()
    
    # 注册API蓝图 - 只注册auth蓝图用于游客登录
    from api.auth import auth_bp

    app.register_blueprint(auth_bp, url_prefix='/todo-for-ai/api/v1/auth')

    app.logger.info("Registered auth blueprint only (minimal configuration for guest login)")





def register_commands(app):
    """注册命令行命令"""
    
    @app.cli.command()
    def init_db():
        """初始化数据库"""
        db.create_all()
        print('Database initialized.')
    
    @app.cli.command()
    def reset_db():
        """重置数据库"""
        db.drop_all()
        db.create_all()
        print('Database reset.')


# 创建应用实例
app = create_app()

# 在应用启动时创建数据库表
with app.app_context():
    db.create_all()
    print('✅ 数据库表已创建/更新')


if __name__ == '__main__':
    # 开发服务器
    port = int(os.environ.get('PORT', 50110))
    host = os.environ.get('HOST', '127.0.0.1')
    
    print(f"🚀 启动 Todo for AI 服务器...")
    print(f"📍 地址: http://{host}:{port}")
    print(f"🗄️ 数据库: {app.config['SQLALCHEMY_DATABASE_URI']}")
    print(f"🔧 环境: {app.config.get('ENV', 'development')}")
    
    app.run(host=host, port=port, debug=app.config['DEBUG'])
