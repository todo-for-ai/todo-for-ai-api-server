"""write_agent_audit 的 project_id 派生回归测试。

项目详情页「最近动态」按 project_id 索引召回审计事件（overview 端点
纯过滤），但多数调用方只带 task_id。write_agent_audit 现在会在 payload
未显式给 project_id 时按 tasks 主键派生一次；显式传入时以调用方为准。
"""

import uuid

import pytest

from app import create_app
from models import db

BASE_URL = "/todo-for-ai/api/v1"


@pytest.fixture(scope="function", autouse=True)
def _isolated_app():
    """每测试独立内存库。"""
    app = create_app("testing")
    app.config.update({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SQLALCHEMY_ENGINE_OPTIONS": {},
    })
    ctx = app.app_context()
    ctx.push()
    db.create_all()
    # 预热 agent_common 的表检查缓存（同 test_task_coauthoring）
    from api.agent_common import _agent_activity_events_table_exists
    _agent_activity_events_table_exists()
    yield app
    db.session.remove()
    db.drop_all()
    ctx.pop()


def _make_project_task():
    from models import Organization, Project, Task, User

    user = User(username=f"u_{uuid.uuid4().hex[:8]}", email=f"u_{uuid.uuid4().hex[:6]}@t.io")
    db.session.add(user)
    db.session.flush()
    org = Organization(name=f"o_{uuid.uuid4().hex[:8]}", slug=f"o_{uuid.uuid4().hex[:8]}", owner_id=user.id)
    db.session.add(org)
    db.session.flush()
    project = Project(name=f"p_{uuid.uuid4().hex[:6]}", owner_id=user.id, organization_id=org.id)
    db.session.add(project)
    db.session.flush()
    task = Task(
        title="审计派生测试任务",
        content="x",
        project_id=project.id,
        owner_id=user.id,
    )
    db.session.add(task)
    db.session.commit()
    return project, task


class TestAuditProjectDerivation:
    def _write(self, _app, target_type="task", target_id=None, **payload):
        from api.agent_common import write_agent_audit
        from models import AgentAuditEvent

        # write_agent_audit 读取 request 头（correlation/request id），需要请求上下文
        with _app.test_request_context("/"):
            write_agent_audit(
                event_type="test.event",
                actor_type="agent",
                actor_id=1,
                target_type=target_type,
                target_id=target_id if target_id is not None else (payload.get("task_id") or 0),
                workspace_id=1,
                payload=payload,
            )
        db.session.commit()
        return AgentAuditEvent.query.order_by(AgentAuditEvent.id.desc()).first()

    def test_project_id_derived_from_task_id(self, _isolated_app):
        project, task = _make_project_task()
        event = self._write(_isolated_app, task_id=task.id)
        assert event.task_id == task.id
        assert event.project_id == project.id

    def test_project_id_derived_from_target_id(self, _isolated_app):
        """历史调用方只传 target_type='task' + target_id=任务ID，两列都应补全。"""
        project, task = _make_project_task()
        event = self._write(_isolated_app, target_id=task.id)
        assert event.task_id == task.id
        assert event.project_id == project.id

    def test_non_task_target_not_derived(self, _isolated_app):
        """target_type 不是 task 时不应误把 target_id 当任务 ID。"""
        event = self._write(_isolated_app, target_type="agent", target_id=7)
        assert event.task_id is None
        assert event.project_id is None

    def test_explicit_project_id_wins(self, _isolated_app):
        project, task = _make_project_task()
        event = self._write(_isolated_app, task_id=task.id, project_id=project.id)
        assert event.project_id == project.id

    def test_unknown_task_id_stays_none(self, _isolated_app):
        event = self._write(_isolated_app, task_id=424242)
        assert event.task_id == 424242
        assert event.project_id is None

    def test_no_task_id_stays_none(self, _isolated_app):
        event = self._write(_isolated_app, target_id=0)
        assert event.task_id is None
        assert event.project_id is None
