"""Agent cron 调度器回归（core/agent_cron_scheduler.tick + 门控启动）。

覆盖：run_agent 动作建 queued AgentRun、next_fire_at 推进、AgentRun 幂等键
防重；create_task 动作定时建任务（工作区校验/payload 缺失跳过/last_fired_key
幂等）、created 事件联动、未知动作不炸仍推进；调度线程 enabled 门控。
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
    AgentTriggerAction,
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
    user = User(username=f"cs_{uuid.uuid4().hex[:8]}", email=f"cs_{uuid.uuid4().hex[:6]}@t.io")
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
    db.session.commit()
    return user, org, agent, project


def _mk_cron_trigger(org, agent, action=AgentTriggerAction.RUN_AGENT.value,
                     action_payload=None, due=True):
    row = AgentTrigger(
        workspace_id=org.id, agent_id=agent.id,
        name=f"cron_{uuid.uuid4().hex[:6]}",
        trigger_type=AgentTriggerType.CRON.value,
        enabled=True, priority=100,
        cron_expr='* * * * *',
        misfire_policy=AgentMisfirePolicy.CATCH_UP_ONCE.value,
        action=action,
        action_payload=action_payload,
        next_fire_at=(datetime.utcnow() - timedelta(minutes=1)) if due else None,
        created_by="test",
    )
    db.session.add(row)
    db.session.commit()
    return row


class TestTickRunAgent:
    def test_due_trigger_creates_queued_run_and_advances(self):
        _, org, agent, _ = _mk_env()
        trigger = _mk_cron_trigger(org, agent)
        old_fire_at = trigger.next_fire_at

        from core.agent_cron_scheduler import tick
        fired, matched = tick()

        assert (fired, matched) == (1, 1)
        run = AgentRun.query.filter_by(trigger_id=trigger.id).one()
        assert run.state == AgentRunState.QUEUED.value
        assert run.trigger_reason == 'cron.tick'
        assert run.idempotency_key.startswith(f"cron:{trigger.id}:")
        assert trigger.last_triggered_at is not None
        assert trigger.next_fire_at > old_fire_at

    def test_idempotent_when_run_already_exists(self):
        _, org, agent, _ = _mk_env()
        trigger = _mk_cron_trigger(org, agent)
        same_past = datetime.utcnow().replace(second=0, microsecond=0) - timedelta(minutes=1)
        trigger.next_fire_at = same_past
        db.session.commit()

        from core.agent_cron_scheduler import tick
        tick()
        assert AgentRun.query.filter_by(trigger_id=trigger.id).count() == 1

        # 同一 fire_at 再次到期（模拟调度重叠/补偿扫描）：幂等键相同不重复建
        trigger.next_fire_at = same_past
        db.session.commit()
        tick()
        assert AgentRun.query.filter_by(trigger_id=trigger.id).count() == 1

    def test_not_due_trigger_skipped(self):
        _, org, agent, _ = _mk_env()
        _mk_cron_trigger(org, agent, due=False)

        from core.agent_cron_scheduler import tick
        fired, matched = tick()
        assert (fired, matched) == (0, 0)
        assert AgentRun.query.count() == 0


class TestTickCreateTask:
    def test_due_trigger_creates_task_and_sets_key(self):
        _, org, agent, project = _mk_env()
        payload = {
            'project_id': project.id,
            'title': '每日站会纪要整理',
            'description': '整理今日站会要点',
            'priority': 'high',
            'tags': ['daily', 'standup'],
        }
        trigger = _mk_cron_trigger(org, agent, AgentTriggerAction.CREATE_TASK.value, payload)

        from core.agent_cron_scheduler import tick
        fired, _ = tick()

        assert fired == 1
        task = Task.query.filter_by(project_id=project.id, title='每日站会纪要整理').one()
        assert task.creator_type == 'ai'
        assert task.is_ai_task is True
        assert task.priority.value == 'high'
        assert sorted(task.tags) == ['daily', 'standup']
        assert task.content == '整理今日站会要点'
        assert trigger.last_fired_key.startswith(f"cron:{trigger.id}:")

    def test_create_task_idempotent_on_same_fire(self):
        _, org, agent, project = _mk_env()
        payload = {'project_id': project.id, 'title': '重复触发只建一次'}
        trigger = _mk_cron_trigger(org, agent, AgentTriggerAction.CREATE_TASK.value, payload)
        same_past = datetime.utcnow().replace(second=0, microsecond=0) - timedelta(minutes=1)
        trigger.next_fire_at = same_past
        db.session.commit()

        from core.agent_cron_scheduler import tick
        tick()
        assert Task.query.filter_by(title='重复触发只建一次').count() == 1

        # 同一 fire_at 再次扫描：last_fired_key 幂等命中，不重复建任务
        trigger.next_fire_at = same_past
        db.session.commit()
        tick()
        assert Task.query.filter_by(title='重复触发只建一次').count() == 1

    def test_create_task_invalid_project_skipped(self):
        _, org, agent, project = _mk_env()
        payload = {'project_id': project.id + 99999, 'title': '不该被创建'}
        trigger = _mk_cron_trigger(org, agent, AgentTriggerAction.CREATE_TASK.value, payload)

        from core.agent_cron_scheduler import tick
        fired, _ = tick()
        assert fired == 0
        assert Task.query.count() == 0
        # 到期触发器仍要推进 next_fire_at，避免死循环扫描
        assert trigger.next_fire_at > datetime.utcnow() - timedelta(minutes=5)

    def test_create_task_emits_created_event_for_downstream(self):
        _, org, agent, project = _mk_env()
        # 下游：另一个 agent 的 task_event(created) 触发器
        downstream_agent = Agent(workspace_id=org.id, owner_id=agent.owner_id,
                                 creator_user_id=agent.owner_id,
                                 name=f"ag_{uuid.uuid4().hex[:6]}", status=AgentStatus.ACTIVE)
        db.session.add(downstream_agent)
        db.session.flush()
        db.session.add(AgentTrigger(
            workspace_id=org.id, agent_id=downstream_agent.id,
            name=f"tg_{uuid.uuid4().hex[:6]}",
            trigger_type=AgentTriggerType.TASK_EVENT.value,
            enabled=True, task_event_types=['created'], task_filter={},
            created_by="test",
        ))
        db.session.commit()

        payload = {'project_id': project.id, 'title': '联动下游触发器'}
        _mk_cron_trigger(org, agent, AgentTriggerAction.CREATE_TASK.value, payload)

        from core.agent_cron_scheduler import tick
        tick()

        downstream_runs = AgentRun.query.filter_by(
            agent_id=downstream_agent.id, trigger_reason='task.created').all()
        assert len(downstream_runs) == 1


class TestTickMisc:
    def test_unknown_action_advances_without_firing(self):
        _, org, agent, _ = _mk_env()
        trigger = _mk_cron_trigger(org, agent, action='explode')

        from core.agent_cron_scheduler import tick
        fired, matched = tick()
        assert (fired, matched) == (0, 1)
        assert AgentRun.query.count() == 0
        assert Task.query.count() == 0
        assert trigger.next_fire_at is not None


class TestSchedulerGating:
    def test_start_scheduler_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv('AGENT_CRON_SCHEDULER_ENABLED', raising=False)
        from core import agent_cron_scheduler as mod
        from app import create_app
        app = create_app("testing")
        assert mod.start_scheduler(app) is False
        assert mod.scheduler_status()['enabled'] is False

    def test_start_scheduler_enabled_starts_and_stops(self, monkeypatch):
        monkeypatch.setenv('AGENT_CRON_SCHEDULER_ENABLED', 'true')
        from core import agent_cron_scheduler as mod
        from app import create_app
        app = create_app("testing")
        try:
            assert mod.start_scheduler(app) is True
            assert mod.scheduler_status()['enabled'] is True
            # 重复启动安全
            assert mod.start_scheduler(app) is False
        finally:
            mod.stop_scheduler()
        assert mod.scheduler_status()['enabled'] is False
