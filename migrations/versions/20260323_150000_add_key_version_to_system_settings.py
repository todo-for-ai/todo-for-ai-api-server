"""
添加 system_settings.key_version 字段

Description: 支持加密密钥轮换和多版本密钥管理
Created: 2026-03-23T15:00:00
"""


def upgrade(connection):
    """执行迁移 - 添加 key_version 字段到 system_settings 表"""
    from sqlalchemy import text

    # 检查列是否已存在
    result = connection.execute(text("""
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
        AND TABLE_NAME = 'system_settings'
        AND COLUMN_NAME = 'key_version'
    """))

    if result.fetchone():
        print("  ℹ️ key_version 列已存在，跳过")
        return

    # 添加 key_version 字段
    connection.execute(text("""
        ALTER TABLE system_settings
        ADD COLUMN key_version VARCHAR(50) NULL COMMENT '加密密钥版本'
    """))
    print("  ✅ key_version 列添加成功")


def downgrade(connection):
    """回滚迁移 - 删除 key_version 字段"""
    from sqlalchemy import text

    # 检查列是否存在
    result = connection.execute(text("""
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
        AND TABLE_NAME = 'system_settings'
        AND COLUMN_NAME = 'key_version'
    """))

    if not result.fetchone():
        print("  ℹ️ key_version 列不存在，跳过")
        return

    # 删除 key_version 字段
    connection.execute(text("""
        ALTER TABLE system_settings
        DROP COLUMN key_version
    """))
    print("  ✅ key_version 列删除成功")
