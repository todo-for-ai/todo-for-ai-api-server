"""
Flask 应用配置
"""

import os
from dotenv import load_dotenv

def load_env_files():
    """
    加载环境变量文件，支持多种启动方式：
    1. 本地启动: 读取根目录/.env
    2. Docker环境变量传递: 不加载文件，直接使用环境变量
    3. Docker文件传递: 通过ENV_FILE环境变量指定.env文件路径
    """
    # 获取当前文件所在目录（backend/app/）
    current_dir = os.path.dirname(os.path.abspath(__file__))
    backend_dir = os.path.dirname(current_dir)  # backend/
    project_root = os.path.dirname(backend_dir)  # 项目根目录

    # 检查是否通过ENV_FILE环境变量指定了.env文件路径
    env_file_path = os.environ.get('ENV_FILE')
    if env_file_path:
        if os.path.exists(env_file_path):
            print(f"📄 加载指定的环境变量文件: {env_file_path}")
            load_dotenv(env_file_path)
            return env_file_path
        else:
            print(f"⚠️  指定的环境变量文件不存在: {env_file_path}")

    # 检查是否在Docker环境中（通过检查特定环境变量）
    if os.environ.get('DOCKER_ENV') == 'true' or os.environ.get('DATABASE_URL'):
        print("🐳 Docker环境检测到，使用环境变量配置")
        return "environment_variables"

    # 本地启动：读取根目录.env
    root_env = os.path.join(project_root, '.env')
    if os.path.exists(root_env):
        print(f"📄 本地启动 - 加载环境变量: {root_env}")
        load_dotenv(root_env)
        return root_env
    else:
        print("⚠️  未找到环境变量文件，使用默认配置")
        return None

# 加载环境变量
loaded_env_file = load_env_files()


class Config:
    """基础配置类"""
    
    # Flask 基础配置
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev-secret-key-change-in-production'

    # Session 配置 (解决OAuth state不匹配问题)
    SESSION_TYPE = 'filesystem'
    SESSION_PERMANENT = False
    SESSION_USE_SIGNER = True
    SESSION_KEY_PREFIX = 'todo-for-ai:'
    SESSION_FILE_DIR = '/tmp/flask-sessions'

    
    # 数据库配置
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or \
        'mysql+pymysql://root:password@localhost:3306/todo_for_ai'
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        'pool_pre_ping': True,
        'pool_recycle': 300,
        'pool_timeout': 20,
        'max_overflow': 0
    }
    
    # 文件上传配置
    UPLOAD_FOLDER = os.environ.get('UPLOAD_FOLDER') or 'uploads'
    MAX_CONTENT_LENGTH = int(os.environ.get('MAX_CONTENT_LENGTH', 16 * 1024 * 1024))  # 16MB
    ALLOWED_EXTENSIONS = {
        'txt', 'pdf', 'png', 'jpg', 'jpeg', 'gif', 'doc', 'docx', 
        'xls', 'xlsx', 'ppt', 'pptx', 'zip', 'rar', 'md'
    }

    # CORS 配置（开发环境需要，生产环境可选）
    CORS_ORIGINS = os.environ.get('CORS_ORIGINS', 'http://localhost:5173,http://localhost:50111,http://localhost:50112').split(',')

    # JWT 配置
    JWT_SECRET_KEY = os.environ.get('JWT_SECRET_KEY') or SECRET_KEY
    JWT_ACCESS_TOKEN_EXPIRES = 86400  # 1 day (24 hours)
    JWT_REFRESH_TOKEN_EXPIRES = 2592000  # 30 days (1 month)

    # Redis 配置（默认本地启动无密码）
    REDIS_ENABLED = os.environ.get('REDIS_ENABLED', 'true').lower() == 'true'
    REDIS_HOST = os.environ.get('REDIS_HOST', '127.0.0.1')
    REDIS_PORT = int(os.environ.get('REDIS_PORT', 6379))
    REDIS_DB = int(os.environ.get('REDIS_DB', 0))
    REDIS_PASSWORD = os.environ.get('REDIS_PASSWORD') or None
    REDIS_URL = os.environ.get('REDIS_URL') or (
        f"redis://{REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}"
        if REDIS_PASSWORD is None else
        f"redis://:{REDIS_PASSWORD}@{REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}"
    )

    

    

    
    # 日志配置
    LOG_LEVEL = os.environ.get('LOG_LEVEL') or 'INFO'
    LOG_FILE = os.environ.get('LOG_FILE') or 'app.log'
    
    @staticmethod
    def init_app(app):
        """初始化应用配置"""
        # 创建上传目录
        upload_folder = app.config['UPLOAD_FOLDER']
        if not os.path.exists(upload_folder):
            os.makedirs(upload_folder)


class DevelopmentConfig(Config):
    """开发环境配置"""
    DEBUG = True
    SQLALCHEMY_ECHO = True  # 打印 SQL 语句


class TestingConfig(Config):
    """测试环境配置"""
    TESTING = True
    SQLALCHEMY_DATABASE_URI = os.environ.get('TEST_DATABASE_URL') or \
        'mysql+pymysql://root:password@localhost:3306/todo_for_ai_test'
    WTF_CSRF_ENABLED = False


class ProductionConfig(Config):
    """生产环境配置"""
    DEBUG = False
    SQLALCHEMY_ECHO = False
    
    @classmethod
    def init_app(cls, app):
        Config.init_app(app)
        
        # 生产环境特定配置
        import logging
        from logging.handlers import RotatingFileHandler
        
        if not app.debug:
            file_handler = RotatingFileHandler(
                'logs/todo_for_ai.log', 
                maxBytes=10240000, 
                backupCount=10
            )
            file_handler.setFormatter(logging.Formatter(
                '%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]'
            ))
            file_handler.setLevel(logging.INFO)
            app.logger.addHandler(file_handler)
            
            app.logger.setLevel(logging.INFO)
            app.logger.info('Todo for AI startup')


# 配置映射
config = {
    'development': DevelopmentConfig,
    'testing': TestingConfig,
    'production': ProductionConfig,
    'default': DevelopmentConfig
}
