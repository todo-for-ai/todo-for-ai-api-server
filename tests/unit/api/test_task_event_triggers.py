"""task_event 触发引擎链路回归（api/agent_trigger_engine.emit_task_event）。

历史注记：`_is_trigger_match` 的函数定义头曾整体丢失（函数体作为死代码
残留在 emit_repo_event 的 return 之后），导致所有 task_event 触发器在
emit_task_event 里 NameError、任务事件永远无法建 AgentRun。本文件同时
是该缺陷的回归护栏。
"""

import uuid
from datetime import datetime, timedelta

import pytest

from models import (
    Agent,
    AgentRun,
    AgentRunState,
    AgentStatus,
    AgentTrigger,
    AgentMisfirePolicy,
    AgentTriggerType,
    Organization,
    Project,
    Task,
    User,
    db,
)


@pytest.fixture(scope="function", autouse=True)
def _isolated_app():
    from app import create_app
    app = create_app("testing")
    app.config.update({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SQLALCHEMY_ENGINE_OPTIONS": {},
    })
    ctx = app.app_context()
    ctx.push()
    db.create_all()
    yield app
    db.session.remove()
    db.drop_all()
    ctx.pop()


def _mk_env():
    user = User(username=f"te_{uuid.uuid4().hex[:8]}", email=f"te_{uuid.uuid4().hex[:6]}@t.io")
    db.session.add(user)
    db.session.flush()
    org = Organization(name=f"o_{uuid.uuid4().hex[:6]}", slug=f"o_{uuid.uuid4().hex[:6]}",
                       owner_id=user.id)
    db.session.add(org)
    db.session.flush()
    agent = Agent(workspace_id=org.id, owner_id=user.id, creator_user_id=user.id,
                  name=f"ag_{uuid.uuid4().hex[:6]}", status=AgentStatus.ACTIVE)
    db.session.add(agent)
    db.session.flush()
    project = Project(name=f"p_{uuid.uuid4().hex[:6]}", owner_id=user.id, organization_id=org.id)
    db.session.add(project)
    db.session.flush()
    task = Task(project_id=project.id, title="trigger me")
    db.session.add(task)
    db.session.flush()
    db.session.commit()
    return user, org, agent, project, task


def _mk_trigger(workspace_id, agent_id, event_types, task_filter=None):
    row = AgentTrigger(
        workspace_id=workspace_id, agent_id=agent_id,
        name=f"tg_{uuid.uuid4().hex[:6]}",
        trigger_type=AgentTriggerType.TASK_EVENT.value,
        enabled=True, priority=100,
        task_event_types=event_types,
        task_filter=task_filter or {},
        misfire_policy=AgentMisfirePolicy.CATCH_UP_ONCE.value,
        created_by="test",
    )
    db.session.add(row)
    db.session.commit()
    return row


class TestEmitTaskEvent:
    def test_created_event_matches_and_creates_run(self):
        _, org, agent, _, task = _mk_env()
        _mk_trigger(org.id, agent.id, ['created'])

        from api.agent_trigger_engine import emit_task_event
        event_id = emit_task_event(task, 'created', {}, actor='test')

        assert event_id
        runs = AgentRun.query.filter_by(trigger_reason='task.created').all()
        assert len(runs) == 1
        assert runs[0].state == AgentRunState.QUEUED.value
        assert runs[0].agent_id == agent.id
        assert runs[0].input_payload['task_id'] == task.id

    def test_event_type_mismatch_creates_no_run(self):
        _, org, agent, _, task = _mk_env()
        _mk_trigger(org.id, agent.id, ['completed'])

        from api.agent_trigger_engine import emit_task_event
        emit_task_event(task, 'created', {}, actor='test')

        assert AgentRun.query.filter_by(trigger_reason='task.created').count() == 0

    def test_project_filter_mismatch_creates_no_run(self):
        _, org, agent, other_project, task = _mk_env()
        _mk_trigger(org.id, agent.id, ['created'],
                    task_filter={'project_ids': [other_project.id + 9999]})

        from api.agent_trigger_engine import emit_task_event
        emit_task_event(task, 'created', {}, actor='test')

        assert AgentRun.query.filter_by(trigger_reason='task.created').count() == 0

    def test_status_changed_from_to_filter(self):
        _, org, agent, _, task = _mk_env()
        _mk_trigger(org.id, agent.id, ['status_changed'],
                    task_filter={'from_status': ['todo'], 'to_status': ['in_progress']})

        from api.agent_trigger_engine import emit_task_event
        # 不匹配的迁移：todo -> done
        emit_task_event(task, 'status_changed',
                        {'from_status': 'todo', 'to_status': 'done'}, actor='test')
        assert AgentRun.query.filter_by(trigger_reason='task.status_changed').count() == 0
        # 匹配的迁移：todo -> in_progress
        emit_task_event(task, 'status_changed',
                        {'from_status': 'todo', 'to_status': 'in_progress'}, actor='test')
        assert AgentRun.query.filter_by(trigger_reason='task.status_changed').count() == 1

    def test_tag_filter(self):
        _, org, agent, _, task = _mk_env()
        task.tags = ['backend']
        db.session.commit()
        _mk_trigger(org.id, agent.id, ['created'], task_filter={'tags': ['frontend']})

        from api.agent_trigger_engine import emit_task_event
        emit_task_event(task, 'created', {}, actor='test')
        assert AgentRun.query.filter_by(trigger_reason='task.created').count() == 0

    def test_disabled_trigger_creates_no_run(self):
        _, org, agent, _, task = _mk_env()
        trigger = _mk_trigger(org.id, agent.id, ['created'])
        trigger.enabled = False
        db.session.commit()

        from api.agent_trigger_engine import emit_task_event
        emit_task_event(task, 'created', {}, actor='test')
        assert AgentRun.query.filter_by(trigger_reason='task.created').count() == 0

    def test_unknown_event_type_returns_none(self):
        _, _, _, _, task = _mk_env()
        from api.agent_trigger_engine import emit_task_event
        assert emit_task_event(task, 'nonsense', {}, actor='test') is None
