#!/usr/bin/env python3
"""
加密现有 llm_config 的脚本

用于将明文的 llm_config 迁移为加密存储
"""

import sys
sys.path.insert(0, '/Users/cc11001100/github/todo-for-ai/todo-for-ai/todo-for-ai-api-server')

from app import app
from models.system_settings import SystemSettings


def encrypt_llm_config():
    """加密现有的 llm_config"""
    with app.app_context():
        # 检查是否存在 llm_config
        setting = SystemSettings.query.filter_by(key='llm_config').first()

        if not setting:
            print("❌ 未找到 llm_config 配置")
            return False

        if setting.is_encrypted:
            print("✅ llm_config 已经是加密状态，无需迁移")
            return True

        # 执行迁移
        success, message = SystemSettings.migrate_to_encrypted('llm_config')

        if success:
            print(f"✅ {message}")
            return True
        else:
            print(f"❌ {message}")
            return False


if __name__ == '__main__':
    encrypt_llm_config()
