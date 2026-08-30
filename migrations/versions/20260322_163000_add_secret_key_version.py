"""
添加 agent_secrets.key_version 字段

Description: 支持密钥轮换和多版本密钥管理
Created: 2026-03-22T16:30:00
"""


def upgrade(connection):
    """执行迁移 - 添加 key_version 字段到 agent_secrets 表"""
    from sqlalchemy import text

    # 检查列是否已存在
    result = connection.execute(text("""
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
        AND TABLE_NAME = 'agent_secrets'
        AND COLUMN_NAME = 'key_version'
    """))

    if result.fetchone():
        print("  ℹ️ key_version 列已存在，跳过添加")
    else:
        # 添加 key_version 字段
        connection.execute(text("""
            ALTER TABLE agent_secrets
            ADD COLUMN key_version VARCHAR(32) NOT NULL DEFAULT 'primary' COMMENT '加密密钥版本'
        """))
        print("  ✅ key_version 列添加成功")

    # 为现有记录设置默认 key_version
    connection.execute(text("""
        UPDATE agent_secrets SET key_version = 'primary' WHERE key_version IS NULL OR key_version = ''
    """))
    print("  ✅ 现有记录的 key_version 已更新")


def downgrade(connection):
    """回滚迁移 - 删除 key_version 字段"""
    from sqlalchemy import text

    # 检查列是否存在
    result = connection.execute(text("""
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
        AND TABLE_NAME = 'agent_secrets'
        AND COLUMN_NAME = 'key_version'
    """))

    if not result.fetchone():
        print("  ℹ️ key_version 列不存在，跳过")
        return

    # 删除 key_version 字段
    connection.execute(text("""
        ALTER TABLE agent_secrets
        DROP COLUMN key_version
    """))
    print("  ✅ key_version 列删除成功")
