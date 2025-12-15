"""
数据库迁移脚本：优化用户活跃度表索引（性能优化）
"""

import sys
import os

# 添加backend目录到Python路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from sqlalchemy import create_engine, text
from core.config import config


def upgrade():
    """升级数据库 - 添加复合索引优化查询性能"""
    print("开始优化用户活跃度表索引...")

    # 获取数据库配置
    config_obj = config['development']
    engine = create_engine(config_obj.SQLALCHEMY_DATABASE_URI)

    with engine.connect() as conn:
        # 添加复合索引：user_id + activity_date (覆盖热力图查询)
        print("1. 添加复合索引 idx_user_activity_date...")
        try:
            conn.execute(text("""
                CREATE INDEX idx_user_activity_date 
                ON user_activities (user_id, activity_date)
            """))
            print("✅ 复合索引创建成功")
        except Exception as e:
            if 'Duplicate key name' in str(e) or 'already exists' in str(e):
                print("⚠️  复合索引已存在，跳过")
            else:
                print(f"❌ 复合索引创建失败: {e}")

        # 分析表以更新统计信息
        print("2. 更新表统计信息...")
        try:
            conn.execute(text("ANALYZE TABLE user_activities"))
            print("✅ 表统计信息已更新")
        except Exception as e:
            print(f"⚠️  更新统计信息失败: {e}")

        # 提交事务
        conn.commit()
        print("✅ 数据库索引优化完成！")


def downgrade():
    """降级数据库 - 删除复合索引"""
    print("开始删除优化索引...")

    # 获取数据库配置
    config_obj = config['development']
    engine = create_engine(config_obj.SQLALCHEMY_DATABASE_URI)

    with engine.connect() as conn:
        # 删除复合索引
        try:
            conn.execute(text("""
                DROP INDEX idx_user_activity_date ON user_activities
            """))
            print("✅ 复合索引已删除")
        except Exception as e:
            print(f"⚠️  索引删除失败: {e}")

        # 提交事务
        conn.commit()
        print("✅ 数据库降级完成！")


if __name__ == '__main__':
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == 'downgrade':
        downgrade()
    else:
        upgrade()
