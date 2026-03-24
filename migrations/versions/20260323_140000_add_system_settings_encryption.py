"""
添加 system_settings 表的 is_encrypted 字段

Description: 支持敏感配置的加密存储
Created: 2026-03-23T14:00:00
"""


def upgrade(connection):
    """执行迁移 - 添加 is_encrypted 字段到 system_settings 表"""
    from sqlalchemy import text

    # 检查列是否已存在
    result = connection.execute(text("""
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
        AND TABLE_NAME = 'system_settings'
        AND COLUMN_NAME = 'is_encrypted'
    """))

    if result.fetchone():
        print("  ℹ️ is_encrypted 列已存在，跳过")
        return

    # 添加 is_encrypted 字段
    connection.execute(text("""
        ALTER TABLE system_settings
        ADD COLUMN is_encrypted INT NOT NULL DEFAULT 0 COMMENT '是否加密存储（1=是，0=否）'
    """))
    print("  ✅ is_encrypted 列添加成功")


def downgrade(connection):
    """回滚迁移 - 删除 is_encrypted 字段"""
    from sqlalchemy import text

    # 检查列是否存在
    result = connection.execute(text("""
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
        AND TABLE_NAME = 'system_settings'
        AND COLUMN_NAME = 'is_encrypted'
    """))

    if not result.fetchone():
        print("  ℹ️ is_encrypted 列不存在，跳过")
        return

    # 删除 is_encrypted 字段
    connection.execute(text("""
        ALTER TABLE system_settings
        DROP COLUMN is_encrypted
    """))
    print("  ✅ is_encrypted 列删除成功")
