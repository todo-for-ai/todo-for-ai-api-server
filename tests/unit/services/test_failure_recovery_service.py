"""失败自愈循环（services/failure_recovery.py）缺口补测。

补齐：无 Agent 时跳过经验沉淀、经验入库异常不阻断、知识策展异常不阻断。
归因/重试/升级/幂等主路径由 api 层测试覆盖，此处直测服务函数。
"""

import uuid

import pytest

from models import Project, Task, User, db
from services.failure_recovery import (
    classify_failure,
    handle_failed_commit,
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


@pytest.fixture
def task():
    user = User(username=f"fr_{uuid.uuid4().hex[:8]}",
                email=f"fr_{uuid.uuid4().hex[:6]}@t.io")
    db.session.add(user)
    db.session.flush()
    project = Project(name=f"fp_{uuid.uuid4().hex[:8]}", owner_id=user.id)
    db.session.add(project)
    db.session.flush()
    task = Task(project_id=project.id, owner_id=user.id,
                title="自愈源任务", status="IN_PROGRESS", priority="HIGH",
                creator_id=user.id, creator_type="human")
    db.session.add(task)
    db.session.commit()
    return task


class TestClassify:
    def test_code_and_keywords(self):
        assert classify_failure("tests_failed", "") == "test_failure"
        assert classify_failure(None, "npm build failed") == "build_failure"
        assert classify_failure(None, "request timed out") == "timeout"
        assert classify_failure(None, "mystery") == "unknown"


class TestDegradedPaths:
    def test_works_without_agent(self, task):
        result = handle_failed_commit(
            task, agent=None, attempt_id="att-noagent",
            failure_code="TESTS_FAILED", failure_reason="assert x")
        assert result["action"] == "repair_created"
        assert result["category"] == "test_failure"

    def test_experience_record_failure_does_not_block(self, task, monkeypatch):
        from models import Agent
        agent = Agent(workspace_id=task.project.organization_id,
                      owner_id=task.owner_id, creator_user_id=task.owner_id,
                      name="exp-agent")
        db.session.add(agent)
        db.session.commit()

        class Boom:
            def __init__(self, **kw):
                raise RuntimeError("experience store down")
        monkeypatch.setattr("models.AgentExperience", Boom)

        result = handle_failed_commit(
            task, agent=agent, attempt_id="att-expfail",
            failure_code="BUILD_FAILED", failure_reason="compile")
        assert result["action"] == "repair_created"
        assert result["category"] == "build_failure"

    def test_curation_failure_does_not_block(self, task, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("curation down")
        monkeypatch.setattr(
            "services.knowledge_curation.propose_from_failure", boom)

        result = handle_failed_commit(
            task, agent=None, attempt_id="att-curfail",
            failure_code="TIMEOUT", failure_reason="t")
        assert result["action"] == "repair_created"
        assert result["category"] == "timeout"

    def test_repair_task_inherits_parent(self, task):
        handle_failed_commit(
            task, agent=None, attempt_id="att-inherit",
            failure_code="TESTS_FAILED", failure_reason="assert eq")
        repair = Task.query.filter(
            Task.parent_task_id == task.id).one()
        assert "[修复]" in repair.title
        assert repair.dod == task.dod
        assert repair.is_ai_task is True
        assert repair.creator_identifier == "recovery:test_failure"
        assert repair.owner_id == task.owner_id
