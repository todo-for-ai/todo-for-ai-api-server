"""多维度作用域记忆的隔离与生命周期测试。

核心保证：
- 隔离：跨组织不可见；user 记忆 per-org；查询永远带 organization_id；
- 继承链：session → project → agent → user → organization 优先级合并，
  命中带维度标签；
- 幂等：同作用域同内容只存一条（重复验证升置信度）；
- 生命周期：循环 DONE / STALLED / 额度停车自动沉淀对应维度记忆，
  注入块带维度标签；沉淀失败绝不影响循环状态流转。
"""

import uuid

import pytest

from services.memory import scopes as memory_scopes
from services.memory import store as memory_store
from services.memory.scopes import MemoryScopeRef
from models import MemoryScopeType

BASE_URL = "/todo-for-ai/api/v1"


@pytest.fixture(scope="function", autouse=True)
def _isolated_app():
    from app import create_app
    from models import db

    app = create_app("testing")
    app.config.update({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SQLALCHEMY_ENGINE_OPTIONS": {},
    })
    _ctx = app.app_context()
    _ctx.push()
    db.create_all()
    yield app
    db.session.remove()
    db.drop_all()
    _ctx.pop()


@pytest.fixture
def client(_isolated_app):
    return _isolated_app.test_client()


@pytest.fixture
def db_session(_isolated_app):
    from models import db
    with _isolated_app.app_context():
        yield db.session
    db.session.rollback()


@pytest.fixture(autouse=True)
def _scripted_planner(monkeypatch):
    monkeypatch.setenv('GOAL_LOOP_PLANNER', 'scripted')


def _uuid():
    return uuid.uuid4().hex[:8]


@pytest.fixture
def two_orgs(db_session, user_factory, organization_factory, agent_factory):
    """两个独立组织，各带一个 agent 和一个项目（用于隔离验证）。"""
    boxes = []
    for _ in range(2):
        user = user_factory()
        org = organization_factory(owner_id=user.id)
        agent = agent_factory(workspace_id=org.id, runner_enabled=True)
        from models import Project
        project = Project(name=f"p_{_uuid()}", owner_id=user.id, organization_id=org.id)
        db_session.add(project)
        db_session.commit()
        boxes.append({"user": user, "org": org, "agent": agent, "project": project})
    yield boxes


@pytest.fixture
def env(two_orgs):
    return two_orgs[0]


@pytest.fixture(autouse=True)
def _cleanup_memories(db_session, two_orgs):
    yield
    from models import AgentExperience, AgentMemory, KnowledgeEntry
    for box in two_orgs:
        AgentMemory.query.filter_by(organization_id=box["org"].id).delete()
        AgentExperience.query.filter_by(agent_id=box["agent"].id).delete()
        KnowledgeEntry.query.filter_by(agent_id=box["agent"].id).delete()
    db_session.commit()


class TestScopeIsolation:
    def test_cross_org_memories_invisible(self, db_session, two_orgs):
        org_a, org_b = two_orgs
        ref_a = MemoryScopeRef(MemoryScopeType.ORGANIZATION, org_a["org"].id, org_a["org"].id)
        memory_store.remember(ref_a, 'rule', '达梦分页用 OFFSET FETCH',
                              '达梦不支持 LIMIT 语法', confidence=80)

        # 组织 B 的同维度作用域查不到组织 A 的记忆
        ref_b = MemoryScopeRef(MemoryScopeType.ORGANIZATION, org_b["org"].id, org_b["org"].id)
        hits = memory_store.recall([ref_b], "达梦分页", top_k=5)
        assert hits == []

        # 组织 A 自己可见
        hits_a = memory_store.recall([ref_a], "达梦分页", top_k=5)
        assert len(hits_a) == 1
        assert hits_a[0]['scope_label'] == '组织记忆'

    def test_user_memory_is_per_org(self, db_session, env):
        """user 记忆挂在 user_id 上，但被 organization_id 隔离——
        同一用户在组织 A 的个人记忆不会泄漏到组织 B。"""
        user_ref_org_a = MemoryScopeRef(
            MemoryScopeType.USER, env["user"].id, env["org"].id)
        memory_store.remember(user_ref_org_a, 'preference', '偏好 pytest 风格断言',
                              '断言用 assert 语句而非 self.assertEqual')

        # 另一个组织里同 user_id 的作用域（模拟用户在 B 组织的身份）
        other_org = MemoryScopeRef(MemoryScopeType.USER, env["user"].id,
                                   env["org"].id + 999)
        assert memory_store.recall([other_org], "pytest 断言", top_k=5) == []

    def test_scope_ref_requires_org(self):
        with pytest.raises(ValueError):
            MemoryScopeRef(MemoryScopeType.PROJECT, 1, None)


class TestScopeChain:
    def test_chain_from_loop_order(self, db_session, env):
        from models import GoalLoop, GoalLoopStatus

        loop = GoalLoop(
            workspace_id=env["org"].id, project_id=env["project"].id,
            agent_id=env["agent"].id, title=f"loop_{_uuid()}",
            goal_text="g", status=GoalLoopStatus.RUNNING,
            rounds_limit=10, stall_limit=2, created_by=env["user"].id,
        )
        db_session.add(loop)
        db_session.commit()
        chain = memory_scopes.chain_from_loop(loop)
        values = [r.scope_type.value for r in chain]
        assert values == ['session', 'project', 'agent', 'user', 'organization']
        assert all(r.organization_id == env["org"].id for r in chain)

    def test_precedence_session_over_project(self, db_session, env):
        """同关键词命中多个维度时，越具体的维度排越前。"""
        loop_id = 424242
        memory_store.remember(
            MemoryScopeRef(MemoryScopeType.PROJECT, env["project"].id, env["org"].id),
            'insight', '项目层：登录超时通常来自网关',
            '先查网关', confidence=70)
        memory_store.remember(
            MemoryScopeRef(MemoryScopeType.SESSION, loop_id, env["org"].id),
            'insight', '会话层：本轮已排除网关因素',
            '网关已排除，聚焦会话存储', confidence=70)

        chain = [
            MemoryScopeRef(MemoryScopeType.SESSION, loop_id, env["org"].id),
            MemoryScopeRef(MemoryScopeType.PROJECT, env["project"].id, env["org"].id),
        ]
        hits = memory_store.recall(chain, "登录超时 网关", top_k=2)
        assert len(hits) == 2
        assert hits[0]['scope_label'] == '会话记忆'
        assert hits[1]['scope_label'] == '项目记忆'

    def test_dedupe_idempotent_with_confidence_boost(self, db_session, env):
        ref = MemoryScopeRef(MemoryScopeType.PROJECT, env["project"].id, env["org"].id)
        first = memory_store.remember(ref, 'rule', '构建必须 linux/amd64',
                                      'Apple Silicon 构建需加平台参数', confidence=70)
        second = memory_store.remember(ref, 'rule', '构建必须 linux/amd64',
                                       'Apple Silicon 构建需加平台参数', confidence=70)
        assert first['created'] is True
        assert second['created'] is False
        assert second['memory'].confidence == 75
        from models import AgentMemory
        assert AgentMemory.query.filter_by(
            scope_type='project', scope_id=env["project"].id).count() == 1


class TestLoopMemoryHooks:
    def _make_loop(self, db_session, env):
        from models import GoalLoop, GoalLoopStatus

        loop = GoalLoop(
            workspace_id=env["org"].id, project_id=env["project"].id,
            agent_id=env["agent"].id, title=f"loop_{_uuid()}",
            goal_text="修复登录超时", done_definition="测试全过",
            status=GoalLoopStatus.RUNNING, rounds_limit=10, stall_limit=2,
            created_by=env["user"].id,
        )
        db_session.add(loop)
        db_session.commit()
        return loop

    def test_complete_writes_session_and_project_memories(self, db_session, env, monkeypatch):
        from models import GoalLoopStatus
        from services.goal_loop import state_machine

        loop = self._make_loop(db_session, env)
        loop.plan = [{"title": "收尾", "content": "final"}]
        loop.plan_index = 1
        db_session.commit()
        monkeypatch.setattr(
            state_machine, 'call_review',
            lambda l, s: {'action': 'complete', 'reason': '三轮收敛验收全过'},
        )
        monkeypatch.setattr(state_machine, 'rounds_done', lambda lid: 3)

        assert state_machine.maybe_advance(loop.id)['reason'] == 'completed'
        db_session.expire_all()

        from models import AgentMemory
        rows = AgentMemory.query.filter_by(organization_id=env["org"].id).all()
        scopes_written = {r.scope_type for r in rows}
        assert scopes_written == {'session', 'project'}
        project_row = next(r for r in rows if r.scope_type == 'project')
        assert '修复登录超时' in project_row.title
        assert '三轮收敛' in project_row.content

    def test_stall_blocked_writes_project_memory(self, db_session, env, monkeypatch):
        """无进展护栏 STALLED → 项目级受阻教训。"""
        from models import GoalLoopStatus
        from services.goal_loop import state_machine

        loop = self._make_loop(db_session, env)
        loop.plan = [{"title": "继续", "content": "retry"}]
        loop.plan_index = 1
        db_session.commit()
        monkeypatch.setattr(
            state_machine, 'call_review',
            lambda l, s: {'action': 'extend',
                          'steps': [{'title': '再烧', 'content': 'go'}]},
        )
        monkeypatch.setattr(state_machine, 'trailing_failure_streak', lambda lid: 5)

        # 连续两次 forced stall → STALLED
        state_machine.maybe_advance(loop.id)
        state_machine.maybe_advance(loop.id)
        db_session.expire_all()
        assert loop.status == GoalLoopStatus.STALLED

        from models import AgentMemory
        row = AgentMemory.query.filter_by(
            organization_id=env["org"].id, source_type='loop_blocked').first()
        assert row is not None
        assert '受阻' in row.title
        assert 'no_progress' in row.content

    def test_quota_stall_writes_project_memory(
        self, client, db_session, two_orgs, project_factory, task_factory
    ):
        """额度停车 → 项目级「额度曾耗尽」记忆。"""
        from datetime import datetime, timedelta

        env = two_orgs[0]
        ctx_user, ctx_org, ctx_agent = env["user"], env["org"], env["agent"]
        project = project_factory(owner_id=ctx_user.id, organization_id=ctx_org.id)
        loop = self._make_loop(db_session, {"user": ctx_user, "org": ctx_org,
                                            "agent": ctx_agent, "project": project})
        from models import Task, TaskStatus

        task = Task(title=f"round_{_uuid()}", content='{"prompt":"x"}',
                    project_id=project.id, owner_id=ctx_org.id, is_ai_task=True,
                    status=TaskStatus.IN_PROGRESS, dod=[])
        db_session.add(task)
        db_session.flush()
        task.add_tag(loop.tag)
        from models import (AgentTaskAttempt, AgentTaskAttemptState,
                            AgentTaskLease, AgentExperience, AgentKey)

        attempt_id, lease_id = f"att_{_uuid()}", f"lea_{_uuid()}"
        AgentTaskLease.query.filter_by(task_id=task.id).delete(synchronize_session=False)
        db_session.add(AgentTaskAttempt(
            attempt_id=attempt_id, task_id=task.id, agent_id=ctx_agent.id,
            workspace_id=ctx_org.id, state=AgentTaskAttemptState.ABORTED,
            lease_id=lease_id, started_at=datetime.utcnow(),
            ended_at=datetime.utcnow(), created_by="test"))
        db_session.add(AgentTaskLease(
            lease_id=lease_id, task_id=task.id, attempt_id=attempt_id,
            agent_id=ctx_agent.id, workspace_id=ctx_org.id,
            expires_at=datetime.utcnow() + timedelta(seconds=300),
            active=True, created_by="test"))
        db_session.commit()

        key_row, raw_key = AgentKey.generate_key(
            name=f"Key {_uuid()}", workspace_id=ctx_org.id,
            agent_id=ctx_agent.id, created_by_user_id=ctx_user.id)
        db_session.add(key_row)
        db_session.commit()
        resp = client.post(f"{BASE_URL}/agent/auth/introspect", json={"agent_key": raw_key})
        token = resp.get_json()["data"]["access_token"]

        resp = client.post(
            f"{BASE_URL}/agent/tasks/{task.id}/commit",
            json={"attempt_id": attempt_id, "lease_id": lease_id, "status": "failed",
                  "failure_code": "QUOTA_EXCEEDED", "failure_reason": "insufficient_quota"},
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": attempt_id},
        )
        assert resp.status_code == 200

        from models import AgentMemory
        row = AgentMemory.query.filter_by(
            organization_id=ctx_org.id, source_type='loop_blocked').first()
        assert row is not None
        assert '额度' in row.content

        # 清理失败经验（agent 删除前）
        AgentExperience.query.filter_by(agent_id=ctx_agent.id).delete()
        db_session.commit()

    def test_injection_carries_scope_labels(self, db_session, env):
        """走廊记忆块带维度标签（如 [项目记忆]）。"""
        from services.goal_loop.dispatch import create_round_task

        memory_store.remember(
            MemoryScopeRef(MemoryScopeType.PROJECT, env["project"].id, env["org"].id),
            'solution', '登录超时排查手册', '先查网关再查会话存储', confidence=85)
        loop = self._make_loop(db_session, env)

        task = create_round_task(loop, {"title": "排查登录超时", "content": "定位根因"})
        assert "【相关记忆（历史经验/知识库）】" in task.content
        assert "[项目记忆]" in task.content
        assert "登录超时排查手册" in task.content

    def test_other_org_memory_never_injected(self, db_session, two_orgs):
        """隔离注入验证：组织 B 的项目记忆不会出现在组织 A 的轮次任务里。"""
        from services.goal_loop.dispatch import create_round_task

        org_b = two_orgs[1]
        memory_store.remember(
            MemoryScopeRef(MemoryScopeType.PROJECT, org_b["project"].id, org_b["org"].id),
            'solution', 'B 组织的登录超时手册', 'B 组织独有的排查步骤', confidence=90)

        env_a = two_orgs[0]
        loop = self._make_loop(db_session, env_a)
        task = create_round_task(loop, {"title": "排查登录超时", "content": "x"})
        assert "B 组织独有的排查步骤" not in task.content
