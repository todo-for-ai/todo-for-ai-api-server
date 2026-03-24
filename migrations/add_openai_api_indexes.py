"""
OpenAI API 兼容层数据库索引优化

为高并发场景优化，支持几百到几千 QPS

索引设计原则：
1. 高频查询字段必须有索引
2. 复合索引遵循最左前缀原则
3. 考虑索引选择性
4. 避免过多索引影响写入性能

索引列表：
- ai_request_logs: 支持按用户、功能、时间范围查询
- api_tokens: 支持token验证查询
- users: 支持用户查询
"""

from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
import os
import sys

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def get_database_url():
    """获取数据库URL"""
    return os.environ.get('DATABASE_URL') or \
        'mysql+pymysql://root:password@localhost:3306/todo_for_ai'


def add_indexes_for_openai_api():
    """为OpenAI API兼容层添加必要的索引"""

    engine = create_engine(get_database_url())

    indexes = [
        # ============== ai_request_logs 表索引优化 ==============
        {
            'name': 'idx_ai_logs_user_time',
            'table': 'ai_request_logs',
            'sql': """
                CREATE INDEX idx_ai_logs_user_time ON ai_request_logs(user_id, created_at DESC);
            """,
            'description': '支持按用户查询最近的请求日志'
        },
        {
            'name': 'idx_ai_logs_feature_time',
            'table': 'ai_request_logs',
            'sql': """
                CREATE INDEX idx_ai_logs_feature_time ON ai_request_logs(feature, created_at DESC);
            """,
            'description': '支持按功能查询最近的请求日志'
        },
        {
            'name': 'idx_ai_logs_cache_hit',
            'table': 'ai_request_logs',
            'sql': """
                CREATE INDEX idx_ai_logs_cache_hit ON ai_request_logs(user_id, cache_hit, created_at DESC);
            """,
            'description': '支持缓存命中率统计查询'
        },
        {
            'name': 'idx_ai_logs_latency',
            'table': 'ai_request_logs',
            'sql': """
                CREATE INDEX idx_ai_logs_latency ON ai_request_logs(feature, latency_ms);
            """,
            'description': '支持按延迟分析慢查询'
        },

        # ============== api_tokens 表索引优化 ==============
        {
            'name': 'idx_api_tokens_token_hash',
            'table': 'api_tokens',
            'sql': """
                CREATE INDEX idx_api_tokens_token_hash ON api_tokens(token_hash);
            """,
            'description': '支持token快速验证查询'
        },
        {
            'name': 'idx_api_tokens_user_status',
            'table': 'api_tokens',
            'sql': """
                CREATE INDEX idx_api_tokens_user_status ON api_tokens(user_id, status, created_at DESC);
            """,
            'description': '支持查询用户的有效token列表'
        },
        {
            'name': 'idx_api_tokens_expires',
            'table': 'api_tokens',
            'sql': """
                CREATE INDEX idx_api_tokens_expires ON api_tokens(expires_at, status);
            """,
            'description': '支持过期token清理任务'
        },

        # ============== users 表索引优化 ==============
        {
            'name': 'idx_users_email_status',
            'table': 'users',
            'sql': """
                CREATE INDEX idx_users_email_status ON users(email, status);
            """,
            'description': '支持邮箱登录查询'
        },
        {
            'name': 'idx_users_github_status',
            'table': 'users',
            'sql': """
                CREATE INDEX idx_users_github_status ON users(github_id, status);
            """,
            'description': '支持GitHub登录查询'
        },
        {
            'name': 'idx_users_role_status',
            'table': 'users',
            'sql': """
                CREATE INDEX idx_users_role_status ON users(role, status);
            """,
            'description': '支持按角色查询用户列表'
        },

        # ============== system_settings 表索引优化 ==============
        {
            'name': 'idx_system_settings_key',
            'table': 'system_settings',
            'sql': """
                CREATE INDEX idx_system_settings_key ON settings(`key`);
            """,
            'description': '支持配置快速查询'
        },
    ]

    print("=" * 60)
    print("OpenAI API 兼容层数据库索引优化")
    print("=" * 60)

    with engine.connect() as conn:
        for idx in indexes:
            try:
                print(f"\n创建索引: {idx['name']}")
                print(f"表: {idx['table']}")
                print(f"用途: {idx['description']}")

                conn.execute(text(idx['sql']))
                conn.commit()

                print(f"✅ 索引 {idx['name']} 创建成功")

            except OperationalError as e:
                if "Duplicate key name" in str(e) or "already exists" in str(e):
                    print(f"⚠️  索引 {idx['name']} 已存在，跳过")
                else:
                    print(f"❌ 索引 {idx['name']} 创建失败: {e}")
            except Exception as e:
                print(f"❌ 索引 {idx['name']} 创建失败: {e}")

    print("\n" + "=" * 60)
    print("索引优化完成")
    print("=" * 60)


def add_performance_monitoring_table():
    """创建性能监控表 (用于高并发监控)"""

    engine = create_engine(get_database_url())

    sql = """
    CREATE TABLE IF NOT EXISTS api_performance_metrics (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        endpoint VARCHAR(255) NOT NULL COMMENT 'API端点',
        method VARCHAR(10) NOT NULL COMMENT 'HTTP方法',
        status_code INT NOT NULL COMMENT 'HTTP状态码',
        latency_ms FLOAT NOT NULL COMMENT '响应延迟(毫秒)',
        cache_hit BOOLEAN DEFAULT FALSE COMMENT '是否命中缓存',
        user_id INT COMMENT '用户ID',
        request_id VARCHAR(64) COMMENT '请求ID',
        error_code VARCHAR(64) COMMENT '错误代码',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
        INDEX idx_perf_endpoint_time (endpoint, created_at DESC),
        INDEX idx_perf_latency (latency_ms, created_at DESC),
        INDEX idx_perf_user (user_id, created_at DESC),
        INDEX idx_perf_cache (cache_hit, created_at DESC)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='API性能指标';
    """

    print("\n创建性能监控表...")

    with engine.connect() as conn:
        try:
            conn.execute(text(sql))
            conn.commit()
            print("✅ 性能监控表创建成功")
        except Exception as e:
            if "already exists" in str(e):
                print("⚠️  性能监控表已存在，跳过")
            else:
                print(f"❌ 性能监控表创建失败: {e}")


def analyze_tables():
    """分析表以更新统计信息"""

    engine = create_engine(get_database_url())
    tables = ['ai_request_logs', 'api_tokens', 'users', 'system_settings']

    print("\n分析表统计信息...")

    with engine.connect() as conn:
        for table in tables:
            try:
                conn.execute(text(f"ANALYZE TABLE {table}"))
                print(f"✅ {table} 分析完成")
            except Exception as e:
                print(f"⚠️  {table} 分析失败: {e}")


if __name__ == '__main__':
    add_indexes_for_openai_api()
    add_performance_monitoring_table()
    analyze_tables()
    print("\n✅ 所有优化完成!")
