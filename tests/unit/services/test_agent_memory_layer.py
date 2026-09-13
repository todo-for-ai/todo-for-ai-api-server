"""Agent 记忆层单元测试：可插拔召回 + 循环注入 + 成功经验写入。"""

from datetime import datetime

import pytest

from services.memory import MemoryHit, get_memory_backend, recall_for_query
from services.memory.builtin import BuiltinMemoryBackend


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
def db_session(_isolated_app):
    from models import db
    with _isolated_app.app_context():
        yield db.session
    db.session.rollback()


@pytest.fixture(autouse=True)
def _builtin_backend(monkeypatch):
    monkeypatch.delenv('AGENT_MEMORY_BACKEND', raising=False)
    monkeypatch.setenv('GOAL_LOOP_PLANNER', 'scripted')


def _uuid():
    import uuid
    return uuid.uuid4().hex[:8]


@pytest.fixture
def env(db_session, user_factory, organization_factory, agent_factory):
    user = user_factory()
    org = organization_factory(owner_id=user.id)
    agent = agent_factory(workspace_id=org.id, runner_enabled=True)
    from models import Project
    project = Project(name=f"p_{_uuid()}", owner_id=user.id, organization_id=org.id)
    db_session.add(project)
    db_session.commit()
    box = {"user": user, "org": org, "agent": agent, "project": project}
    yield box
    # 先清引用 agent 的行再让 agent_factory 删 agent，避免 FK 置空冲突
    from models import AgentExperience, GoalLoop, KnowledgeEntry

    AgentExperience.query.filter_by(agent_id=agent.id).delete()
    KnowledgeEntry.query.filter_by(agent_id=agent.id).delete()
    GoalLoop.query.filter_by(agent_id=agent.id).delete()
    db_session.commit()


def _experience(db_session, env, learnings, outcome="ok", domain=None, shared=False, times=3):
    from models import AgentExperience

    row = AgentExperience(
        agent_id=env["agent"].id,
        experience_type='failure_pattern',
        domain=domain,
        task_type='test_failure',
        outcome_pattern=outcome[:300],
        key_learnings=learnings[:500],
        confidence=0.8,
        times_reused=times,
        is_shared=shared,
    )
    db_session.add(row)
    db_session.commit()
    return row


def _make_loop(db_session, env):
    from models import GoalLoop, GoalLoopStatus

    loop = GoalLoop(
        workspace_id=env["org"].id, project_id=env["project"].id,
        agent_id=env["agent"].id, title=f"loop_{_uuid()}",
        goal_text="修复登录超时问题", done_definition="全部测试通过",
        status=GoalLoopStatus.RUNNING, rounds_limit=10, stall_limit=2,
        created_by=env["user"].id,
    )
    db_session.add(loop)
    db_session.commit()
    return loop


def _knowledge(db_session, env, title, content, confidence=0.9):
    from models import KnowledgeEntry

    row = KnowledgeEntry(
        agent_id=env["agent"].id,
        title=title,
        content=content,
        entry_type='solution',
        confidence=confidence,
    )
    db_session.add(row)
    db_session.commit()
    return row


class TestBuiltinRecall:
    def test_recall_experiences_by_keyword(self, db_session, env):
        _experience(db_session, env, "修 selector timeout 要先加大等待再断言",
                    outcome="TESTS_FAILED: selector timeout")
        hits = BuiltinMemoryBackend().recall("selector timeout 处理", top_k=3)
        assert hits, "keyword recall should hit experience"
        assert hits[0].kind == 'experience'
        assert 'selector' in hits[0].snippet or 'selector' in hits[0].title

    def test_recall_knowledge_by_title(self, db_session, env):
        _knowledge(db_session, env, "达梦分页必须用 OFFSET FETCH",
                   "MySQL 的 LIMIT 语法在达梦上不可用")
        hits = BuiltinMemoryBackend().recall("达梦分页写法", top_k=3)
        assert hits
        assert any(h.kind == 'knowledge' and '达梦' in h.title for h in hits)

    def test_no_match_returns_empty(self, db_session, env):
        _experience(db_session, env, "完全无关的记忆内容")
        assert BuiltinMemoryBackend().recall("量子纠缠校准", top_k=3) == []

    def test_ranking_prefers_more_reuse(self, db_session, env):
        _experience(db_session, env, "缓存击穿用互斥锁", times=1)
        _experience(db_session, env, "缓存击穿先用单飞再落库", times=9)
        hits = BuiltinMemoryBackend().recall("缓存击穿", top_k=2)
        assert len(hits) == 2
        assert "单飞" in hits[0].snippet

    def test_factory_falls_back_to_builtin_when_mem0_broken(self, monkeypatch):
        monkeypatch.setenv('AGENT_MEMORY_BACKEND', 'mem0')

        def broken_init(self):
            raise RuntimeError('mem0ai not installed')

        import services.memory.mem0_backend as mem0_mod
        monkeypatch.setattr(mem0_mod.Mem0MemoryBackend, '__init__', broken_init)
        backend = get_memory_backend()
        assert isinstance(backend, BuiltinMemoryBackend)

    def test_recall_for_query_never_raises(self, db_session, env, monkeypatch):
        _experience(db_session, env, "记忆内容")

        def boom(*a, **kw):
            raise RuntimeError("backend down")

        monkeypatch.setattr(BuiltinMemoryBackend, 'recall', boom)
        assert recall_for_query("任意查询") == []
        assert recall_for_query("") == []
        assert recall_for_query(None) == []


class TestCorridorMemoryInjection:
    def test_round_task_carries_memory_section(self, db_session, env):
        _knowledge(db_session, env, "登录超时排查手册", "先查网关再查会话存储")
        from services.goal_loop.dispatch import create_round_task

        loop = _make_loop(db_session, env)
        task = create_round_task(loop, {"title": "排查登录超时", "content": "定位根因"})
        assert "【相关记忆（历史经验/知识库）】" in task.content
        assert "登录超时排查手册" in task.content

    def test_first_round_also_gets_memory(self, db_session, env):
        _experience(db_session, env, "登录超时多为会话过期配置",
                    outcome="TIMEOUT: session expired")
        from services.goal_loop.dispatch import create_round_task

        loop = _make_loop(db_session, env)
        task = create_round_task(loop, {"title": "登录超时修复", "content": "开工"})
        assert "【相关记忆（历史经验/知识库）】" in task.content

    def test_no_memory_no_section(self, db_session, env):
        from services.goal_loop.dispatch import create_round_task

        loop = _make_loop(db_session, env)
        task = create_round_task(loop, {"title": "无关任务", "content": "干活"})
        assert "【相关记忆" not in task.content

    def test_memory_recall_failure_does_not_block_dispatch(self, db_session, env, monkeypatch):
        import services.memory as memory_pkg
        from services.goal_loop.dispatch import create_round_task

        def boom(*a, **kw):
            raise RuntimeError("memory down")

        monkeypatch.setattr(memory_pkg, 'recall_for_query', boom)
        loop = _make_loop(db_session, env)
        task = create_round_task(loop, {"title": "任意", "content": "干活"})
        assert "【相关记忆" not in task.content
        assert task.title == "任意"


class TestLoopSuccessExperience:
    def test_complete_records_success_experience(self, db_session, env, monkeypatch):
        from models import AgentExperience, GoalLoopStatus
        from services.goal_loop import state_machine

        loop = _make_loop(db_session, env)
        loop.plan = [{"title": "收尾", "content": "final"}]
        loop.plan_index = 1
        db_session.commit()
        monkeypatch.setattr(
            state_machine, 'call_review',
            lambda l, s: {'action': 'complete', 'reason': '三轮收敛，验收全过'},
        )
        monkeypatch.setattr(state_machine, 'rounds_done', lambda lid: 3)

        result = state_machine.maybe_advance(loop.id)
        assert result['reason'] == 'completed'
        db_session.expire_all()

        row = AgentExperience.query.filter_by(
            agent_id=env["agent"].id, experience_type='success_pattern',
        ).first()
        assert row is not None
        assert row.task_type == 'goal_loop'
        assert "共 3 轮" in row.outcome_pattern
        assert "三轮收敛" in row.key_learnings
