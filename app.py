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
import threading
from flask import Flask, jsonify
from flask_cors import CORS
from flask_migrate import Migrate

# 导入模型和配置
from models import db
from core.config import config
from core.middleware import setup_all_middleware
from core.github_config import github_service
from core.google_config import google_service
from core.redis_client import get_redis_client


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

    # 初始化Session存储 (解决OAuth state不匹配问题)
    session_dir = app.config.get('SESSION_FILE_DIR', '/tmp/flask-sessions')
    os.makedirs(session_dir, exist_ok=True)

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

    # 设置中间件
    setup_all_middleware(app)

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

        try:
            redis_client = get_redis_client()
            if redis_client:
                redis_status = 'connected'
            else:
                redis_status = 'disabled_or_unavailable'
        except Exception as e:
            redis_status = f'error: {str(e)}'

        return ApiResponse.success(
            data={
                'status': 'healthy',
                'database': db_status,
                'redis': redis_status
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

        try:
            redis_client = get_redis_client()
            if redis_client:
                redis_status = 'connected'
            else:
                redis_status = 'disabled_or_unavailable'
        except Exception as e:
            redis_status = f'error: {str(e)}'

        return ApiResponse.success(
            data={
                'status': 'healthy',
                'service': 'Todo for AI API',
                'version': '1.0.0',
                'database': db_status,
                'redis': redis_status,
                'environment': app.config.get('ENV', 'development')
            },
            message='API service is healthy'
        ).to_response()
    
    # 注册API蓝图
    from api.auth import auth_bp
    from api.projects import projects_bp
    from api.tasks import tasks_bp
    from api.context_rules import context_rules_bp
    from api.tokens import tokens_bp
    from api.mcp import mcp_bp
    from api.docs import docs_bp
    from api.pins import pins_bp
    from api.dashboard import dashboard_bp
    from api.user_settings import user_settings_bp
    from api.api_tokens import api_tokens_bp
    from api.custom_prompts import custom_prompts_bp

    app.register_blueprint(auth_bp, url_prefix='/todo-for-ai/api/v1/auth')
    app.register_blueprint(projects_bp, url_prefix='/todo-for-ai/api/v1/projects')
    app.register_blueprint(tasks_bp, url_prefix='/todo-for-ai/api/v1/tasks')
    app.register_blueprint(context_rules_bp, url_prefix='/todo-for-ai/api/v1/context-rules')
    app.register_blueprint(tokens_bp, url_prefix='/todo-for-ai/api/v1/tokens')
    app.register_blueprint(mcp_bp, url_prefix='/todo-for-ai/api/v1/mcp')
    app.register_blueprint(docs_bp, url_prefix='/todo-for-ai/api/v1/docs')
    app.register_blueprint(pins_bp, url_prefix='/todo-for-ai/api/v1/pins')
    app.register_blueprint(dashboard_bp, url_prefix='/todo-for-ai/api/v1/dashboard')
    app.register_blueprint(user_settings_bp, url_prefix='/todo-for-ai/api/v1/user-settings')
    app.register_blueprint(api_tokens_bp, url_prefix='/todo-for-ai/api/v1/api-tokens')
    app.register_blueprint(custom_prompts_bp, url_prefix='/todo-for-ai/api/v1/custom-prompts')





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


def _prewarm_dashboard_cache_on_startup(flask_app):
    """启动后异步预热 dashboard 缓存，降低首个用户请求冷启动耗时"""
    with flask_app.app_context():
        try:
            from models import User, UserStatus
            from api.dashboard import (
                _build_dashboard_stats,
                _dashboard_cache_set,
                DASHBOARD_STATS_CACHE_TTL_SECONDS,
                DASHBOARD_STATS_STALE_TTL_SECONDS,
            )

            prewarm_users = int(os.environ.get('DASHBOARD_PREWARM_USERS', '3'))
            active_users = User.query.filter(
                User.status == UserStatus.ACTIVE
            ).order_by(
                User.last_active_at.desc(),
                User.id.desc()
            ).limit(
                prewarm_users
            ).all()

            for user in active_users:
                cache_key = f"user:{user.id}:stats"
                data = _build_dashboard_stats(user.id)
                _dashboard_cache_set(
                    cache_key,
                    data,
                    DASHBOARD_STATS_CACHE_TTL_SECONDS,
                    DASHBOARD_STATS_STALE_TTL_SECONDS
                )
            flask_app.logger.info(f"Dashboard cache prewarm finished for {len(active_users)} users")
        except Exception as e:
            flask_app.logger.warning(f"Dashboard cache prewarm failed: {e}")


def start_dashboard_cache_prewarm(flask_app):
    """根据开关启动 dashboard 预热线程"""
    enabled = os.environ.get('DASHBOARD_PREWARM_ON_STARTUP', 'true').lower() == 'true'
    if not enabled:
        return
    blocking = os.environ.get('DASHBOARD_PREWARM_BLOCKING', 'false').lower() == 'true'
    if blocking:
        _prewarm_dashboard_cache_on_startup(flask_app)
        return
    thread = threading.Thread(
        target=_prewarm_dashboard_cache_on_startup,
        args=(flask_app,),
        daemon=True
    )
    thread.start()


# 创建应用实例
app = create_app()

# 在应用启动时创建数据库表
with app.app_context():
    db.create_all()
    start_dashboard_cache_prewarm(app)
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
