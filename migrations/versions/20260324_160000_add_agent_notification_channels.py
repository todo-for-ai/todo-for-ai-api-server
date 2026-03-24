"""
添加 agents.notification_channels 字段

Description: 支持在Agent详情页配置通知渠道 (Feishu、WeCom、DingTalk等)
Created: 2026-03-24T16:00:00
"""


def upgrade(connection):
    """执行迁移 - 添加 notification_channels 字段到 agents 表"""
    from sqlalchemy import text

    # 检查列是否已存在
    result = connection.execute(text("""
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
        AND TABLE_NAME = 'agents'
        AND COLUMN_NAME = 'notification_channels'
    """))

    if result.fetchone():
        print("  ℹ️ notification_channels 列已存在，跳过添加")
    else:
        # 添加 notification_channels 字段 (JSON类型)
        connection.execute(text("""
            ALTER TABLE agents
            ADD COLUMN notification_channels JSON DEFAULT NULL COMMENT '通知渠道配置 (Feishu、WeCom、DingTalk等)'
        """))
        print("  ✅ notification_channels 列添加成功")


def downgrade(connection):
    """回滚迁移 - 删除 notification_channels 字段"""
    from sqlalchemy import text

    # 检查列是否存在
    result = connection.execute(text("""
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
        AND TABLE_NAME = 'agents'
        AND COLUMN_NAME = 'notification_channels'
    """))

    if not result.fetchone():
        print("  ℹ️ notification_channels 列不存在，跳过")
        return

    # 删除 notification_channels 字段
    connection.execute(text("""
        ALTER TABLE agents
        DROP COLUMN notification_channels
    """))
    print("  ✅ notification_channels 列删除成功")
