#!/usr/bin/env python3
"""
生成通知中心演示数据
"""

from datetime import datetime
from app import create_app
from models import db, User, UserStatus, Project, Task, TaskStatus, TaskPriority, UserNotification
from api.agent_common import generate_id
from api.notification_service import ensure_notification_event


def seed():
    users = User.query.filter(User.status == UserStatus.ACTIVE).order_by(User.id.asc()).limit(5).all()
    if not users:
        print('[seed_notification_demo] no active users found')
        return 0

    owner = users[0]
    project = Project.query.filter_by(owner_id=owner.id).order_by(Project.id.asc()).first()
    if not project:
        project = Project.create(
            owner_id=owner.id,
            organization_id=None,
            name='Notification Demo Project',
            description='Demo project for notification center',
            status='active',
            created_by=owner.email,
        )
        db.session.flush()

    task = Task.query.filter_by(project_id=project.id).order_by(Task.id.desc()).first()
    if not task:
        task = Task.create(
            project_id=project.id,
            owner_id=project.owner_id,
            title='演示通知任务',
            content='这是一个用于演示通知中心的数据任务',
            status=TaskStatus.IN_PROGRESS,
            priority=TaskPriority.HIGH,
            tags=['demo', 'notifications'],
            assignees=[],
            mentions=[],
            revision=1,
            is_ai_task=False,
            creator_id=owner.id,
            created_by=owner.email,
        )
        db.session.flush()

    created = 0
    now = datetime.utcnow()
    demo_specs = [
        ('task.created', '新任务：演示通知任务', '产品经理创建了一个新的演示任务'),
        ('task.assigned', '你被分配到任务：演示通知任务', '系统已将你加入任务协作'),
        ('task.mentioned', '你在任务中被提及：演示通知任务', '有人在任务评论里 @ 了你'),
        ('task.completed', '任务已完成：演示通知任务', '该任务已经被标记为完成'),
    ]

    for user in users:
        for idx, (event_type, title, body) in enumerate(demo_specs, start=1):
            event_id = generate_id('nev')
            payload = {
                'title': title,
                'body': body,
                'rendered_title': title,
                'rendered_body': body,
                'link_url': f'/todo-for-ai/pages/tasks/{task.id}',
                'actor_name': owner.full_name or owner.nickname or owner.username or owner.email,
                'seed_demo': True,
                'seed_index': idx,
            }
            ensure_notification_event(
                event_id=event_id,
                event_type=event_type,
                category='task',
                actor_user_id=owner.id,
                resource_type='task',
                resource_id=task.id,
                project_id=project.id,
                organization_id=project.organization_id,
                payload=payload,
                target_user_ids=[user.id],
                created_by='system:seed_notification_demo',
            )
            dedup_key = f'seed-demo:{user.id}:{event_type}:{idx}'
            exists = UserNotification.query.filter_by(dedup_key=dedup_key).first()
            if exists:
                continue
            row = UserNotification(
                user_id=user.id,
                event_id=event_id,
                event_type=event_type,
                category='task',
                title=title,
                body=body,
                level='success' if event_type == 'task.completed' else 'info',
                link_url=f'/todo-for-ai/pages/tasks/{task.id}',
                resource_type='task',
                resource_id=task.id,
                actor_user_id=owner.id,
                project_id=project.id,
                organization_id=project.organization_id,
                extra_payload=payload,
                dedup_key=dedup_key,
                created_by='system:seed_notification_demo',
            )
            db.session.add(row)
            created += 1

    db.session.commit()
    print(f'[seed_notification_demo] created={created} users={len(users)} task_id={task.id} project_id={project.id}')
    return created


def main():
    app = create_app()
    with app.app_context():
        seed()


if __name__ == '__main__':
    main()
