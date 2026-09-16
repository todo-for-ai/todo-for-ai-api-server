"""
Migration: add_webhook_subscriptions
Description: 出站 Webhook 订阅中心——webhook_subscriptions（订阅）与
             webhook_deliveries（派发记录，含重试终态）。与 connectors
             入站互操作构成双向闭环。
Created: 2026-09-17
"""

import os  # noqa: E402
import sys  # noqa: E402

sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")

from models import db  # noqa: E402


def _table_exists(connection, table_name):
    dialect = connection.dialect.name
    if dialect == "mysql":
        row = connection.execute(
            db.text(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_name = :t"
            ),
            {"t": table_name},
        ).first()
        return bool(row and row[0])
    row = connection.execute(
        db.text("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=:t"),
        {"t": table_name},
    ).first()
    return bool(row and row[0])


def _column_exists(connection, table_name, column_name):
    dialect = connection.dialect.name
    if dialect == "mysql":
        row = connection.execute(
            db.text(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c"
            ),
            {"t": table_name, "c": column_name},
        ).first()
        return bool(row and row[0])
    row = connection.execute(
        db.text(f"PRAGMA table_info({table_name})")
    ).fetchall()
    return any(row[1] == column_name for row in row)


def upgrade(connection):
    dialect = connection.dialect.name

    if not _table_exists(connection, "webhook_subscriptions"):
        print("➕ 创建表 webhook_subscriptions ...")
        if dialect == "mysql":
            connection.execute(db.text("""
                CREATE TABLE webhook_subscriptions (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    workspace_id INT NOT NULL,
                    url VARCHAR(512) NOT NULL,
                    events JSON NOT NULL,
                    secret_encrypted VARCHAR(2000) NULL,
                    active TINYINT(1) NOT NULL DEFAULT 1,
                    description VARCHAR(200) NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    CONSTRAINT fk_webhook_subs_ws FOREIGN KEY (workspace_id) REFERENCES organizations(id),
                    KEY idx_webhook_subs_ws (workspace_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """))
        else:
            connection.execute(db.text("""
                CREATE TABLE webhook_subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workspace_id INTEGER NOT NULL REFERENCES organizations(id),
                    url VARCHAR(512) NOT NULL,
                    events JSON NOT NULL,
                    secret_encrypted VARCHAR(2000),
                    active BOOLEAN NOT NULL DEFAULT 1,
                    description VARCHAR(200),
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
            """))
            connection.execute(db.text(
                "CREATE INDEX idx_webhook_subs_ws ON webhook_subscriptions (workspace_id)"))
    else:
        print("⏭️  表 webhook_subscriptions 已存在，跳过")

    if not _table_exists(connection, "webhook_deliveries"):
        print("➕ 创建表 webhook_deliveries ...")
        if dialect == "mysql":
            connection.execute(db.text("""
                CREATE TABLE webhook_deliveries (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    subscription_id INT NOT NULL,
                    event_type VARCHAR(64) NOT NULL,
                    ok TINYINT(1) NOT NULL,
                    status_code INT NULL,
                    attempts INT NOT NULL DEFAULT 1,
                    error VARCHAR(500) NULL,
                    duration_ms INT NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    CONSTRAINT fk_webhook_del_sub FOREIGN KEY (subscription_id) REFERENCES webhook_subscriptions(id),
                    KEY idx_webhook_del_sub (subscription_id),
                    KEY idx_webhook_del_event (event_type)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """))
        else:
            connection.execute(db.text("""
                CREATE TABLE webhook_deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subscription_id INTEGER NOT NULL REFERENCES webhook_subscriptions(id),
                    event_type VARCHAR(64) NOT NULL,
                    ok BOOLEAN NOT NULL,
                    status_code INTEGER,
                    attempts INTEGER NOT NULL DEFAULT 1,
                    error VARCHAR(500),
                    duration_ms INTEGER,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
            """))
            connection.execute(db.text(
                "CREATE INDEX idx_webhook_del_sub ON webhook_deliveries (subscription_id)"))
            connection.execute(db.text(
                "CREATE INDEX idx_webhook_del_event ON webhook_deliveries (event_type)"))
    else:
        print("⏭️  表 webhook_deliveries 已存在，跳过")

    # 连接器非敏感配置列（IM 群路由/API base/字段映射模板）
    if not _column_exists(connection, "external_connector_configs", "config_json"):
        print("➕ external_connector_configs 增加 config_json 列 ...")
        if dialect == "mysql":
            connection.execute(db.text(
                "ALTER TABLE external_connector_configs "
                "ADD COLUMN config_json JSON NULL COMMENT '非敏感集成配置'"))
        else:
            connection.execute(db.text(
                "ALTER TABLE external_connector_configs ADD COLUMN config_json JSON"))
    else:
        print("⏭️  config_json 列已存在，跳过")


def downgrade(connection):
    connection.execute(db.text("DROP TABLE IF EXISTS webhook_deliveries"))
    connection.execute(db.text("DROP TABLE IF EXISTS webhook_subscriptions"))
