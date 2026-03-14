#!/usr/bin/env python3
"""
Seed organization activity/events and projects for UI performance testing.

Usage:
  .venv/bin/python scripts/seed_org_activity_data.py --org-id 7 --events 3000 --projects 40
"""

import argparse
import random
from datetime import datetime, timedelta

from app import create_app
from models import (
    db,
    Organization,
    OrganizationEvent,
    OrganizationMember,
    Project,
    ProjectStatus,
    User,
)


EVENT_TYPES = [
    'task.created',
    'task.updated',
    'task.status_changed',
    'task.deleted',
    'task.log.appended',
    'project.created',
    'project.updated',
    'project.archived',
    'project.restored',
    'project.deleted',
    'member.invited',
    'member.updated',
    'member.removed',
    'agent.created',
    'agent.invited',
    'agent.removed',
    'agent.accepted',
    'agent.rejected',
    'org.created',
    'org.updated',
    'org.archived',
]


def _pick_actor(actor_pool):
    if not actor_pool:
        return None
    return random.choice(actor_pool)


def _make_project_name(org_name, index):
    return f"{org_name} · 项目 {index:02d}"


def _random_status():
    roll = random.random()
    if roll < 0.68:
        return ProjectStatus.ACTIVE
    if roll < 0.88:
        return ProjectStatus.ARCHIVED
    return ProjectStatus.DELETED


def _event_payload(event_type, project):
    if event_type.startswith('project.') and project:
        return {'project_name': project.name}
    if event_type.startswith('task.'):
        return {'task_title': f"任务-{random.randint(1000, 9999)}"}
    if event_type.startswith('member.'):
        return {'member_name': f"成员-{random.randint(1, 80)}"}
    if event_type.startswith('agent.'):
        return {'agent_name': f"Agent-{random.randint(1, 50)}"}
    return {}


def seed(org_id: int, events_count: int, projects_count: int):
    organization = Organization.query.get(org_id)
    if not organization:
        raise RuntimeError(f'Organization not found: {org_id}')

    members = OrganizationMember.query.filter_by(organization_id=org_id).all()
    member_ids = [member.user_id for member in members]
    actors = []
    if member_ids:
        actors = User.query.filter(User.id.in_(member_ids)).all()
    if not actors and organization.owner_id:
        owner = User.query.get(organization.owner_id)
        if owner:
            actors = [owner]

    created_projects = []
    if projects_count > 0:
        base_index = Project.query.filter_by(organization_id=org_id).count() + 1
        for idx in range(projects_count):
            status = _random_status()
            project = Project(
                owner_id=organization.owner_id,
                organization_id=org_id,
                name=_make_project_name(organization.name, base_index + idx),
                description='用于组织详情页测试的示例项目',
                status=status,
                color=random.choice(['#1677ff', '#13c2c2', '#52c41a', '#faad14', '#ff7875']),
                created_by='system:seed_org_activity_data',
                last_activity_at=datetime.utcnow() - timedelta(days=random.randint(0, 45)),
            )
            db.session.add(project)
            created_projects.append(project)
        db.session.commit()

    project_pool = Project.query.filter_by(organization_id=org_id).all()
    if not project_pool:
        project_pool = created_projects

    now = datetime.utcnow()
    window_seconds = 60 * 60 * 24 * 45
    project_latest_activity = {}
    batch = []
    batch_size = 500

    for idx in range(events_count):
        event_type = random.choice(EVENT_TYPES)
        occurred_at = now - timedelta(seconds=random.randint(0, window_seconds))
        actor = _pick_actor(actors)

        project = None
        if event_type.startswith('project.') and project_pool:
            project = random.choice(project_pool)
        elif random.random() < 0.45 and project_pool:
            project = random.choice(project_pool)

        if project and (project.id not in project_latest_activity or occurred_at > project_latest_activity[project.id]):
            project_latest_activity[project.id] = occurred_at

        message = None
        if event_type.startswith('project.') and project:
            message = f"项目 {project.name} {event_type.split('.', 1)[1]}"
        elif event_type.startswith('task.'):
            message = f"任务 {event_type.split('.', 1)[1]}"
        elif event_type.startswith('member.'):
            message = f"成员 {event_type.split('.', 1)[1]}"
        elif event_type.startswith('agent.'):
            message = f"Agent {event_type.split('.', 1)[1]}"
        else:
            message = f"组织 {event_type.split('.', 1)[1] if '.' in event_type else event_type}"

        payload = _event_payload(event_type, project)

        event = OrganizationEvent(
            organization_id=org_id,
            event_type=event_type,
            source='seed',
            level=random.choice(['info', 'success', 'warning']),
            actor_type='user' if actor else 'system',
            actor_id=str(actor.id) if actor else None,
            actor_name=(actor.full_name or actor.nickname or actor.username or actor.email) if actor else 'system',
            project_id=project.id if project else None,
            message=message,
            payload=payload,
            occurred_at=occurred_at,
            created_by='system:seed_org_activity_data',
        )
        batch.append(event)

        if len(batch) >= batch_size:
            db.session.add_all(batch)
            db.session.commit()
            batch = []

    if batch:
        db.session.add_all(batch)
        db.session.commit()

    if project_latest_activity:
        for project_id, activity_at in project_latest_activity.items():
            db.session.query(Project).filter(Project.id == project_id).update({
                Project.last_activity_at: activity_at
            })
        db.session.commit()

    print(
        f"[seed_org_activity_data] org_id={org_id} events={events_count} new_projects={projects_count} total_projects={len(project_pool)}"
    )


def main():
    parser = argparse.ArgumentParser(description='Seed organization activity and projects for UI testing.')
    parser.add_argument('--org-id', type=int, required=True)
    parser.add_argument('--events', type=int, default=2000)
    parser.add_argument('--projects', type=int, default=30)
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        seed(args.org_id, args.events, args.projects)


if __name__ == '__main__':
    main()
