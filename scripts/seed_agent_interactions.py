"""
插入 Agent Interactions 测试数据脚本

使用方法:
    cd todo-for-ai-api-server
    python scripts/seed_agent_interactions.py

这个脚本会为指定 agent 插入一些 TaskLog 测试数据，用于测试 interactions 列表功能
"""

import random
from datetime import datetime, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import sys
import os

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import TaskLog, TaskLogActorType, Task, Agent, User, Organization, db
from app import create_app


def seed_interactions_data(workspace_id: int = 5, agent_id: int = 1, num_records: int = 50):
    """
    为指定 agent 插入 interactions 测试数据

    Args:
        workspace_id: 工作空间/组织ID
        agent_id: Agent ID
        num_records: 要创建的记录数量
    """
    app = create_app()

    with app.app_context():
        # 检查 agent 是否存在
        agent = Agent.query.get(agent_id)
        if not agent:
            print(f"❌ Agent {agent_id} 不存在")
            return

        # 检查 workspace/organization 是否存在
        org = Organization.query.get(workspace_id)
        if not org:
            print(f"❌ Organization {workspace_id} 不存在")
            return

        # 获取该组织下的所有用户
        users = User.query.filter(
            User.organization_id == workspace_id
        ).limit(10).all()

        if not users:
            print(f"❌ Organization {workspace_id} 下没有用户")
            return

        # 获取该组织下的任务
        tasks = Task.query.filter(
            Task.organization_id == workspace_id
        ).limit(20).all()

        if not tasks:
            print(f"⚠️ Organization {workspace_id} 下没有任务，创建一些任务...")
            # 创建一些测试任务
            for i in range(5):
                task = Task(
                    organization_id=workspace_id,
                    title=f"Test Task {i+1} for Agent {agent_id}",
                    description=f"This is a test task for interactions",
                    status='in_progress',
                    priority='medium',
                    created_by=users[0].id if users else 1,
                )
                db.session.add(task)
            db.session.commit()
            tasks = Task.query.filter(
                Task.organization_id == workspace_id
            ).limit(20).all()

        print(f"✅ 找到 {len(users)} 个用户和 {len(tasks)} 个任务")

        # 删除该 agent 在指定 workspace 下的现有 task logs（避免重复）
        existing_logs = TaskLog.query.filter(
            TaskLog.actor_agent_id == agent_id,
            TaskLog.task_id.in_([t.id for t in tasks])
        ).all()

        if existing_logs:
            print(f"🗑️  删除 {len(existing_logs)} 条现有记录...")
            for log in existing_logs:
                db.session.delete(log)
            db.session.commit()

        # 创建新的测试数据
        now = datetime.utcnow()
        content_templates = [
            "分析了任务需求并提供了建议",
            "生成了代码实现方案",
            "进行了代码审查并指出问题",
            "优化了任务执行流程",
            "提供了技术文档和说明",
            "协助解决了技术难题",
            "完成了代码重构工作",
            "提供了最佳实践建议",
            "进行了性能优化分析",
            "协助完成了测试用例设计",
            "提供了架构设计建议",
            "协助排查了系统问题",
            "完成了安全审查",
            "提供了部署方案",
            "协助完成了数据迁移",
        ]

        created_count = 0
        for i in range(num_records):
            user = random.choice(users)
            task = random.choice(tasks)

            # 随机时间（过去30天内）
            random_days = random.randint(0, 30)
            random_hours = random.randint(0, 23)
            random_minutes = random.randint(0, 59)
            created_at = now - timedelta(
                days=random_days,
                hours=random_hours,
                minutes=random_minutes
            )

            log = TaskLog(
                task_id=task.id,
                actor_type=TaskLogActorType.HUMAN,
                actor_user_id=user.id,
                actor_agent_id=agent_id,
                content=random.choice(content_templates),
                content_type='text/markdown',
                created_at=created_at,
                updated_at=created_at,
            )
            db.session.add(log)
            created_count += 1

            # 每10条提交一次
            if created_count % 10 == 0:
                db.session.commit()
                print(f"⏳ 已创建 {created_count}/{num_records} 条记录...")

        db.session.commit()
        print(f"✅ 成功创建 {created_count} 条 interactions 测试数据")

        # 显示统计信息
        print("\n📊 数据统计:")
        for user in users[:5]:  # 只显示前5个用户
            count = TaskLog.query.filter(
                TaskLog.actor_agent_id == agent_id,
                TaskLog.actor_user_id == user.id
            ).count()
            if count > 0:
                print(f"   - User {user.email or user.username}: {count} interactions")


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='插入 Agent Interactions 测试数据')
    parser.add_argument('--workspace', type=int, default=5, help='Workspace/Organization ID (default: 5)')
    parser.add_argument('--agent', type=int, default=1, help='Agent ID (default: 1)')
    parser.add_argument('--count', type=int, default=50, help='Number of records to create (default: 50)')

    args = parser.parse_args()

    print(f"🚀 开始为 Agent {args.agent} (Workspace {args.workspace}) 插入 {args.count} 条测试数据...\n")
    seed_interactions_data(args.workspace, args.agent, args.count)
    print("\n🎉 完成!")
