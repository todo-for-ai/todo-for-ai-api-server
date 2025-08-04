"""
添加自定义提示词表的数据库迁移脚本
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)) + '/..')

from models import db, CustomPrompt, PromptType


def create_custom_prompts_table():
    """创建自定义提示词表"""
    print("Creating custom_prompts table...")
    
    # 创建表
    db.create_all()
    
    print("Custom prompts table created successfully!")


def add_sample_data():
    """添加示例数据（可选）"""
    print("Adding sample custom prompts...")
    
    # 这里可以添加一些示例数据
    # 但通常我们让用户自己创建或使用initialize_user_defaults
    
    print("Sample data added successfully!")


def migrate():
    """执行迁移"""
    try:
        create_custom_prompts_table()
        # add_sample_data()  # 可选
        
        print("Migration completed successfully!")
        return True
        
    except Exception as e:
        print(f"Migration failed: {e}")
        db.session.rollback()
        return False


if __name__ == "__main__":
    from app import create_app
    
    app = create_app()
    with app.app_context():
        migrate()
