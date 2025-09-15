"""
Migration: add_interactive_task_support
Description: Add interactive task support with AI waiting feedback functionality
Created: 2025-01-13T12:00:00
"""

def upgrade(connection):
    """执行迁移 - 添加交互式任务支持"""
    
    # 1. 为tasks表添加交互式相关字段
    connection.execute("""
        ALTER TABLE tasks 
        ADD COLUMN is_interactive BOOLEAN DEFAULT FALSE 
        COMMENT '是否为交互式任务'
    """)
    
    connection.execute("""
        ALTER TABLE tasks 
        ADD COLUMN ai_waiting_feedback BOOLEAN DEFAULT FALSE 
        COMMENT 'AI是否正在等待人工反馈'
    """)
    
    connection.execute("""
        ALTER TABLE tasks 
        ADD COLUMN interaction_session_id VARCHAR(100) 
        COMMENT '交互会话ID，用于标识一次交互流程'
    """)
    
    # 2. 扩展任务状态枚举，添加waiting_human_feedback状态
    connection.execute("""
        ALTER TABLE tasks 
        MODIFY COLUMN status ENUM(
            'todo', 
            'in_progress', 
            'review', 
            'done', 
            'cancelled', 
            'waiting_human_feedback'
        ) DEFAULT 'todo' 
        COMMENT '任务状态'
    """)
    
    # 3. 创建交互日志表
    connection.execute("""
        CREATE TABLE interaction_logs (
            id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
            task_id BIGINT NOT NULL COMMENT '关联任务ID',
            session_id VARCHAR(100) NOT NULL COMMENT '交互会话ID',
            interaction_type ENUM('ai_feedback', 'human_response') NOT NULL COMMENT '交互类型',
            content TEXT NOT NULL COMMENT '交互内容',
            status ENUM('pending', 'completed', 'continued') DEFAULT 'pending' COMMENT '交互状态',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
            created_by VARCHAR(100) COMMENT '创建者',
            metadata JSON COMMENT '额外元数据',
            INDEX idx_task_id (task_id),
            INDEX idx_session_id (session_id),
            INDEX idx_interaction_type (interaction_type),
            INDEX idx_status (status),
            INDEX idx_created_at (created_at),
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci 
        COMMENT='任务交互日志表'
    """)
    
    print("✅ 成功添加交互式任务支持字段和表")


def downgrade(connection):
    """回滚迁移 - 移除交互式任务支持"""
    
    # 1. 删除交互日志表
    connection.execute("DROP TABLE IF EXISTS interaction_logs")
    
    # 2. 恢复任务状态枚举
    connection.execute("""
        ALTER TABLE tasks 
        MODIFY COLUMN status ENUM(
            'todo', 
            'in_progress', 
            'review', 
            'done', 
            'cancelled'
        ) DEFAULT 'todo' 
        COMMENT '任务状态'
    """)
    
    # 3. 删除交互式相关字段
    connection.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS interaction_session_id")
    connection.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS ai_waiting_feedback")
    connection.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS is_interactive")
    
    print("✅ 成功回滚交互式任务支持")
