"""人与 Agent 协同写作：commit 写回改为「正文 + Agent 分节」的回归测试。

历史行为是把 Agent 产出以 JSON 信封（{"content":..., "agent_output":...}）写回
task.content，人类在详情页看到原始 JSON、编辑保存会覆盖 Agent 产出。
新行为：Agent 产出以带归属的 Markdown 分节追加，人类正文保持原位。
"""

import hashlib
import json
import uuid
from datetime import datetime, timedelta

import pytest

from app import create_app
from models import db

BASE_URL = "/todo-for-ai/api/v1"


@pytest.fixture(scope="function", autouse=True)
def _isolated_app():
    """每测试独立内存库：commit 链路涉及任务/租约/去重的级联写入。"""
    app = create_app("testing")
    app.config.update({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SQLALCHEMY_ENGINE_OPTIONS": {},
    })
    ctx = app.app_context()
    ctx.push()
    db.create_all()
    # 预热 agent_common 的表检查缓存：首次 write_agent_audit 会走 inspect(db.engine),
    # 其 PRAGMA+ROLLBACK 在 SQLite 共享连接上会回滚请求中未提交的变更(提交事务中途触发时)
    from api.agent_common import _agent_activity_events_table_exists
    _agent_activity_events_table_exists()
    yield app
    db.session.remove()
    db.drop_all()
    ctx.pop()


@pytest.fixture
def client(_isolated_app):
    return _isolated_app.test_client()


@pytest.fixture
def agent_context(_isolated_app):
    """创建 user/org/agent + 有效 agent session token。"""
    from models import Agent, AgentSession, Organization, User

    user = User(username=f"u_{uuid.uuid4().hex[:8]}", email=f"u_{uuid.uuid4().hex[:6]}@t.io")
    db.session.add(user)
    db.session.flush()
    org = Organization(name=f"o_{uuid.uuid4().hex[:8]}", slug=f"o_{uuid.uuid4().hex[:8]}", owner_id=user.id)
    db.session.add(org)
    db.session.flush()
    agent = Agent(
        name=f"agent_{uuid.uuid4().hex[:6]}",
        workspace_id=org.id,
        creator_user_id=user.id,
        status="ACTIVE",
        runner_enabled=True,
    )
    db.session.add(agent)
    db.session.flush()

    raw_token = f"sess_{uuid.uuid4().hex}"
    session = AgentSession(
        agent_id=agent.id,
        workspace_id=org.id,
        token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
        token_prefix=raw_token[:16],
        expires_at=datetime.utcnow() + timedelta(hours=1),
        is_active=True,
    )
    db.session.add(session)
    db.session.commit()
    return {"user": user, "org": org, "agent": agent, "headers": {"Authorization": f"Bearer {raw_token}"}}


@pytest.fixture
def leased_task(_isolated_app, agent_context):
    """带活跃 attempt + lease 的 AI 任务。"""
    from models import AgentTaskAttempt, AgentTaskAttemptState, AgentTaskLease, Project, Task

    ctx = agent_context
    project = Project(name=f"p_{uuid.uuid4().hex[:6]}", owner_id=ctx["user"].id, organization_id=ctx["org"].id)
    db.session.add(project)
    db.session.flush()
    task = Task(
        title="协同写作任务",
        content="# 需求\n\n实现登录页",
        project_id=project.id,
        owner_id=ctx["user"].id,
        is_ai_task=True,
        status="IN_PROGRESS",
    )
    db.session.add(task)
    db.session.flush()

    attempt_id = f"att_{uuid.uuid4().hex[:8]}"
    lease_id = f"lea_{uuid.uuid4().hex[:8]}"
    attempt = AgentTaskAttempt(
        attempt_id=attempt_id,
        task_id=task.id,
        agent_id=ctx["agent"].id,
        workspace_id=ctx["org"].id,
        state=AgentTaskAttemptState.ACTIVE,
        lease_id=lease_id,
        started_at=datetime.utcnow(),
        created_by="test",
    )
    lease = AgentTaskLease(
        lease_id=lease_id,
        task_id=task.id,
        attempt_id=attempt_id,
        agent_id=ctx["agent"].id,
        workspace_id=ctx["org"].id,
        expires_at=datetime.utcnow() + timedelta(seconds=120),
        active=True,
        created_by="test",
    )
    db.session.add(attempt)
    db.session.add(lease)
    db.session.commit()
    return {"task": task, "attempt_id": attempt_id, "lease_id": lease_id}


def _commit(client, headers, task, leased, output, processed_by="claude-code", metadata=None):
    return client.post(
        f"{BASE_URL}/agent/tasks/{task.id}/commit",
        json={
            "attempt_id": leased["attempt_id"],
            "lease_id": leased["lease_id"],
            "status": "succeeded",
            "result": {"output": output, "processed_by": processed_by, "metadata": metadata or {}},
        },
        headers=headers,
    )


class TestCommitCoauthoring:
    def test_commit_appends_markdown_section(self, client, agent_context, leased_task):
        task = leased_task["task"]
        resp = _commit(client, agent_context["headers"], task, leased_task,
                       "登录页已实现，测试全绿。", processed_by="claude-code")
        assert resp.status_code == 200
        db.session.refresh(task)

        # 不再是 JSON 信封
        with pytest.raises(ValueError):
            json.loads(task.content)

        # 人类正文原位保留 + Agent 分节带归属
        assert task.content.startswith("# 需求\n\n实现登录页")
        assert "## 🤖 Agent 产出（claude-code" in task.content
        assert "登录页已实现，测试全绿。" in task.content

    def test_second_commit_appends_not_overwrites(self, client, agent_context, leased_task):
        task = leased_task["task"]
        assert _commit(client, agent_context["headers"], task, leased_task, "第一次产出", "agent-a").status_code == 200

        # 第二次提交需要新的 attempt/lease（唯一约束 + 去重）
        from models import AgentTaskAttempt, AgentTaskAttemptState, AgentTaskLease
        attempt_id = f"att_{uuid.uuid4().hex[:8]}"
        lease_id = f"lea_{uuid.uuid4().hex[:8]}"
        db.session.add(AgentTaskAttempt(
            attempt_id=attempt_id, task_id=task.id, agent_id=agent_context["agent"].id,
            workspace_id=agent_context["org"].id, state=AgentTaskAttemptState.ACTIVE,
            lease_id=lease_id, started_at=datetime.utcnow(), created_by="test",
        ))
        db.session.add(AgentTaskLease(
            lease_id=lease_id, task_id=task.id, attempt_id=attempt_id,
            agent_id=agent_context["agent"].id, workspace_id=agent_context["org"].id,
            expires_at=datetime.utcnow() + timedelta(seconds=120), active=True, created_by="test",
        ))
        db.session.commit()
        leased2 = {"attempt_id": attempt_id, "lease_id": lease_id}
        resp = _commit(client, agent_context["headers"], task, leased2, "第二次产出", "agent-b")
        assert resp.status_code == 200
        db.session.refresh(task)

        assert "第一次产出" in task.content
        assert "第二次产出" in task.content
        assert "agent-a" in task.content and "agent-b" in task.content

    def test_commit_migrates_legacy_json_envelope(self, client, agent_context, leased_task):
        """历史 JSON 信封任务：新提交顺便归一化为纯 Markdown，旧产出转为分节保留。"""
        from services.task_content import parse_task_document
        task = leased_task["task"]
        task.content = json.dumps({
            "content": "# 需求\n\n实现登录页",
            "agent_output": "旧产出",
            "agent_metadata": {"agent_name": "openclaw"},
            "processed_by": "agent",
        }, ensure_ascii=False)
        db.session.commit()

        resp = _commit(client, agent_context["headers"], task, leased_task, "新产出", "claude-code")
        assert resp.status_code == 200
        db.session.refresh(task)

        doc = parse_task_document(task.content)
        assert doc.source_format == "markdown"
        assert doc.body == "# 需求\n\n实现登录页"
        assert [s.label for s in doc.sections] == ["openclaw", "claude-code"]
