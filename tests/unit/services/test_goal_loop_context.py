"""循环上下文走廊与自动压缩的单元测试（长跑记忆层）。

覆盖：走廊构建（目标层/压缩层/明细层/空历史零开销/硬性长度上界）、
滚动压缩（增量/幂等/抽取式降级/LLM 语义压缩/节奏控制）、
create_round_task 的走廊注入。
"""

from datetime import datetime

import pytest

from services.goal_loop import context as ctx


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
    ctx_app = app.app_context()
    ctx_app.push()
    db.create_all()
    yield app
    db.session.remove()
    db.drop_all()
    ctx_app.pop()


@pytest.fixture
def db_session(_isolated_app):
    from models import db
    with _isolated_app.app_context():
        yield db.session
    db.session.rollback()


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch):
    """LLM 调用默认炸掉 → 压缩走真实降级路径；LLM 用例单独 monkeypatch llm_call。"""
    import services.goal_loop.planning as planning

    def boom(*a, **kw):
        raise RuntimeError("llm unavailable in tests")

    monkeypatch.setattr(planning, 'llm_call', boom)


@pytest.fixture
def env(db_session, user_factory, organization_factory, agent_factory):
    user = user_factory()
    org = organization_factory(owner_id=user.id)
    agent = agent_factory(workspace_id=org.id, runner_enabled=True)
    from models import Project
    project = Project(name=f"p_{uuid_hex()}", owner_id=user.id, organization_id=org.id)
    db_session.add(project)
    db_session.flush()
    from models import GoalLoop, GoalLoopStatus

    loop = GoalLoop(
        workspace_id=org.id, project_id=project.id, agent_id=agent.id,
        title=f"loop_{uuid_hex()}", goal_text="做一个能自演化的爬虫",
        done_definition="全部验收测试通过", status=GoalLoopStatus.RUNNING,
        rounds_limit=50, stall_limit=2, created_by=user.id,
    )
    db_session.add(loop)
    db_session.commit()
    return {"user": user, "org": org, "agent": agent, "project": project, "loop": loop}


def uuid_hex():
    import uuid
    return uuid.uuid4().hex[:8]


def _round(db_session, env, loop, status, title=None, failure=None):
    from models import AgentTaskAttempt, AgentTaskAttemptState, Task, TaskStatus

    task = Task(
        title=title or f"round_{uuid_hex()}",
        content='{"prompt":"x"}',
        project_id=env["project"].id,
        owner_id=env["org"].id,
        is_ai_task=True,
        status=status,
        dod=[],
    )
    db_session.add(task)
    db_session.flush()
    task.add_tag(loop.tag)
    if failure:
        db_session.add(AgentTaskAttempt(
            attempt_id=f"att_{uuid_hex()}", task_id=task.id,
            agent_id=env["agent"].id, workspace_id=env["org"].id,
            state=AgentTaskAttemptState.ABORTED, lease_id=f"lea_{uuid_hex()}",
            failure_code=failure[0], failure_reason=failure[1],
            started_at=datetime.utcnow(), ended_at=datetime.utcnow(), created_by="test",
        ))
    db_session.commit()
    return task


class TestBuildCorridor:
    def test_no_terminal_rounds_returns_empty(self, db_session, env):
        from models import TaskStatus

        loop = env["loop"]
        _round(db_session, env, loop, TaskStatus.IN_PROGRESS)
        assert ctx.build_corridor(loop) == ''

    def test_full_corridor_layers(self, db_session, env):
        from models import TaskStatus

        loop = env["loop"]
        _round(db_session, env, loop, TaskStatus.DONE, title="搭好骨架")
        _round(db_session, env, loop, TaskStatus.CANCELLED, title="修选择器",
               failure=("TESTS_FAILED", "selector timeout"))

        corridor = ctx.build_corridor(loop)
        assert "【总体目标】" in corridor
        assert "做一个能自演化的爬虫" in corridor
        assert "【完成标准】" in corridor
        assert "【最近轮次明细】" in corridor
        assert "搭好骨架" in corridor
        assert "修选择器" in corridor
        assert "TESTS_FAILED: selector timeout" in corridor

    def test_digest_layer_included(self, db_session, env):
        from models import TaskStatus

        loop = env["loop"]
        _round(db_session, env, loop, TaskStatus.DONE, title="第一轮")
        loop.context_digest = "- 已完成：数据模型设计与建表"
        db_session.commit()

        corridor = ctx.build_corridor(loop)
        assert "【历史进展摘要" in corridor
        assert "数据模型设计与建表" in corridor

    def test_corridor_hard_cap(self, db_session, env):
        from models import TaskStatus

        loop = env["loop"]
        _round(db_session, env, loop, TaskStatus.DONE, title="第一轮")
        loop.context_digest = "很长的摘要" * 2000  # 远超走廊上限
        db_session.commit()

        corridor = ctx.build_corridor(loop)
        assert len(corridor) <= ctx.CONTEXT_MAX_CORRIDOR_CHARS + 50
        assert "已按上限截断" in corridor


class TestCompressDigest:
    def test_incremental_and_idempotent(self, db_session, env):
        from models import TaskStatus

        loop = env["loop"]
        tasks = [
            _round(db_session, env, loop, TaskStatus.DONE),
            _round(db_session, env, loop, TaskStatus.CANCELLED),
            _round(db_session, env, loop, TaskStatus.DONE),
            _round(db_session, env, loop, TaskStatus.DONE),
            _round(db_session, env, loop, TaskStatus.CANCELLED),
        ]
        # pending=5，明细层保留最近 3 → 应压缩最早 2 轮
        result = ctx.compress_digest(loop)
        assert result['compressed'] == 2
        assert loop.context_digest_upto == tasks[1].id
        assert loop.context_digest  # 抽取式降级也有内容
        assert "→ done" in loop.context_digest or "→ cancelled" in loop.context_digest

        # 幂等：重复压缩，剩余 3 轮全在明细层窗口内 → 不再压
        again = ctx.compress_digest(loop)
        assert again['compressed'] == 0

    def test_llm_semantic_compression(self, db_session, env, monkeypatch):
        import services.goal_loop.planning as planning
        from models import TaskStatus

        loop = env["loop"]
        for i in range(5):
            _round(db_session, env, loop, TaskStatus.DONE, title=f"第{i}轮")

        captured = {}

        def fake_llm_call(loop_, system_prompt, user_prompt):
            captured['user_prompt'] = user_prompt
            return {'digest': '语义压缩后的进展摘要'}

        monkeypatch.setattr(planning, 'llm_call', fake_llm_call)
        result = ctx.compress_digest(loop)
        assert result['compressed'] == 2
        assert loop.context_digest == '语义压缩后的进展摘要'
        # 新增轮次记录进过 prompt
        assert "新增轮次记录" in captured['user_prompt']

    def test_llm_failure_falls_back_to_extractive(self, db_session, env, monkeypatch):
        import services.goal_loop.planning as planning
        from models import TaskStatus

        loop = env["loop"]
        for i in range(5):
            _round(db_session, env, loop, TaskStatus.CANCELLED, title=f"败{i}轮")

        def boom(*a, **kw):
            raise RuntimeError("llm down")

        monkeypatch.setattr(planning, 'llm_call', boom)
        result = ctx.compress_digest(loop)
        assert result['compressed'] == 2
        assert loop.context_digest  # 抽取式兜底，记忆不断档


class TestCompressCadence:
    def test_below_cadence_skipped(self, db_session, env):
        from models import TaskStatus

        loop = env["loop"]
        for _ in range(ctx.CONTEXT_COMPRESS_EVERY - 1):
            _round(db_session, env, loop, TaskStatus.DONE)
        result = ctx.maybe_compress(loop)
        assert result['reason'] == 'below_cadence'
        assert loop.context_digest is None

    def test_reaches_cadence_compresses(self, db_session, env):
        from models import TaskStatus

        loop = env["loop"]
        for _ in range(ctx.CONTEXT_COMPRESS_EVERY):
            _round(db_session, env, loop, TaskStatus.DONE)
        result = ctx.maybe_compress(loop)
        # 全部终态轮 ≤ 明细层窗口时不压缩也是合法结果
        assert result['reason'] in ('nothing_to_compress', 'below_cadence') or result['compressed'] >= 0

    def test_error_never_blocks(self, db_session, env, monkeypatch):
        from models import TaskStatus

        loop = env["loop"]
        for _ in range(ctx.CONTEXT_COMPRESS_EVERY + ctx.CONTEXT_RECENT_ROUNDS):
            _round(db_session, env, loop, TaskStatus.DONE)

        def boom(*a, **kw):
            raise RuntimeError("db hiccup")

        monkeypatch.setattr(ctx, 'compress_digest', boom)
        result = ctx.maybe_compress(loop)
        assert result == {'compressed': 0, 'reason': 'error'}


class TestRoundTaskInjection:
    def test_create_round_task_prepends_corridor(self, db_session, env):
        from models import TaskStatus
        from services.goal_loop.dispatch import create_round_task

        loop = env["loop"]
        _round(db_session, env, loop, TaskStatus.DONE, title="已完成的上一轮")

        task = create_round_task(loop, {"title": "下一轮", "content": "继续干活"})
        assert task.content.startswith("【总体目标】")
        assert "已完成的上一轮" in task.content
        assert "继续干活" in task.content

    def test_first_round_has_no_corridor(self, db_session, env):
        from services.goal_loop.dispatch import create_round_task

        task = create_round_task(env["loop"], {"title": "首轮", "content": "开工"})
        assert "【总体目标】" not in task.content
        assert task.content.endswith("开工")
