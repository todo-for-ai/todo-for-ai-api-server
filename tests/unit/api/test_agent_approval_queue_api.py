"""agent_approval_queue 端点补测：pending 列表与统计（迭代 137，72%→100% 缺口）。"""

import uuid
from datetime import datetime

import pytest

BASE_URL = "/todo-for-ai/api/v1"


@pytest.fixture
def queue_env(app, db_session, user_factory):
    """用户（org owner）+ JWT + 事件写入器。"""
    from models import User, Organization
    from werkzeug.security import generate_password_hash
    from flask_jwt_extended import create_access_token

    unique_id = uuid.uuid4().hex[:8]
    user = User(username=f"aq_{unique_id}", email=f"aq_{unique_id}@example.com")
    user.password_hash = generate_password_hash("password123")
    db_session.add(user)
    db_session.commit()

    org = Organization(name=f"aq-org-{unique_id}", slug=f"aq-org-{unique_id}", owner_id=user.id)
    db_session.add(org)
    db_session.commit()

    def _event(event_type, interaction_id, workspace_id=None, task_id=9600001, status="pending_approval"):
        from models import AgentTaskEvent

        ev = AgentTaskEvent(
            task_id=task_id,
            attempt_id=f"att-{uuid.uuid4().hex[:8]}",
            workspace_id=workspace_id if workspace_id is not None else org.id,
            event_type=event_type,
            seq=1,
            event_timestamp=datetime.utcnow(),
            payload={
                "interaction_id": interaction_id,
                "interaction_type": "pr_create",
                "status": status,
                "metadata": {"head_branch": "agent/x"},
            },
            created_by=f"user:{user.id}",
        )
        db_session.add(ev)
        db_session.commit()
        return ev

    return {
        "user": user,
        "org": org,
        "headers": {"Authorization": "Bearer " + create_access_token(identity=str(user.id))},
        "event": _event,
    }


def _add_approval(db_session, queue_env, interaction_id, task_id=9600001):
    from models import AgentTaskEvent

    db_session.add(AgentTaskEvent(
        task_id=task_id,
        attempt_id=f"att-{uuid.uuid4().hex[:8]}",
        workspace_id=queue_env["org"].id,
        event_type="interaction_approval",
        seq=1,
        event_timestamp=datetime.utcnow(),
        payload={
            "interaction_id": interaction_id,
            "interaction_type": "pr_create",
            "decision": "approved",
            "status": "approved",
        },
        created_by=f"user:{queue_env['user'].id}",
    ))
    db_session.commit()


class TestPendingApprovalsList:
    def test_empty_queue(self, client, queue_env):
        resp = client.get(
            f"{BASE_URL}/workspaces/{queue_env['org'].id}/approvals/pending",
            headers=queue_env["headers"],
        )
        assert resp.status_code == 200
        assert resp.get_json()["data"]["items"] == []

    def test_list_returns_pending_with_task_and_project(self, client, db_session, queue_env):
        from models import Project, Task, TaskStatus

        project = Project(name=f"aq-p-{uuid.uuid4().hex[:6]}", status="ACTIVE",
                          owner_id=queue_env["user"].id, organization_id=queue_env["org"].id)
        db_session.add(project)
        db_session.commit()
        task = Task(id=9600101, title="queue task", content="", project_id=project.id,
                    creator_id=queue_env["user"].id, is_ai_task=False, status=TaskStatus.TODO)
        db_session.add(task)
        db_session.commit()
        queue_env["event"]("interaction_request", "i-list-1", task_id=task.id)

        resp = client.get(
            f"{BASE_URL}/workspaces/{queue_env['org'].id}/approvals/pending",
            headers=queue_env["headers"],
        )
        assert resp.status_code == 200
        items = resp.get_json()["data"]["items"]
        assert any(i["interaction_id"] == "i-list-1" for i in items)

    def test_decided_interactions_excluded(self, client, db_session, queue_env):
        ev = queue_env["event"]("interaction_request", "i-decided", status="pending_approval")
        _add_approval(db_session, queue_env, "i-decided", task_id=ev.task_id)
        resp = client.get(
            f"{BASE_URL}/workspaces/{queue_env['org'].id}/approvals/pending",
            headers=queue_env["headers"],
        )
        items = resp.get_json()["data"]["items"]
        assert all(i["interaction_id"] != "i-decided" for i in items)

    def test_workspace_404(self, client, queue_env):
        resp = client.get(f"{BASE_URL}/workspaces/987654/approvals/pending", headers=queue_env["headers"])
        assert resp.status_code == 404


class TestApprovalQueueStats:
    def test_stats_counts_pending_and_approved_today(self, client, db_session, queue_env):
        queue_env["event"]("interaction_request", "i-p1")
        queue_env["event"]("interaction_request", "i-p2")
        queue_env["event"]("interaction_request", "i-done")
        _add_approval(db_session, queue_env, "i-done")

        resp = client.get(
            f"{BASE_URL}/workspaces/{queue_env['org'].id}/approvals/stats",
            headers=queue_env["headers"],
        )
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["pending"] == 2
        assert data["approved_today"] == 1

    def test_stats_workspace_404(self, client, queue_env):
        resp = client.get(f"{BASE_URL}/workspaces/987654/approvals/stats", headers=queue_env["headers"])
        assert resp.status_code == 404


class TestAgentNameResolutionAndAccess:
    """最后一批：agent 名解析分支与非成员 403。"""

    def test_list_resolves_agent_display_name(self, client, db_session, queue_env):
        from models import Agent, AgentStatus

        agent = Agent(name=f"agt-{uuid.uuid4().hex[:6]}", display_name="Display Bot",
                      status=AgentStatus.ACTIVE)
        db_session.add(agent)
        db_session.commit()
        ev = queue_env["event"]("interaction_request", "i-agent-name", status="pending_approval")
        ev.agent_id = agent.id  # list 端点按 row.agent_id 查 Agent 名称映射
        db_session.commit()

        resp = client.get(
            f"{BASE_URL}/workspaces/{queue_env['org'].id}/approvals/pending",
            headers=queue_env["headers"],
        )
        assert resp.status_code == 200
        items = resp.get_json()["data"]["items"]
        assert any(i.get("agent_name") == "Display Bot" for i in items)

    def test_list_falls_back_when_agent_missing(self, client, db_session, queue_env):
        ev = queue_env["event"]("interaction_request", "i-missing-agent", status="pending_approval")
        ev.payload = {**(ev.payload or {}), "target_agent_id": 987654}
        db_session.commit()
        resp = client.get(
            f"{BASE_URL}/workspaces/{queue_env['org'].id}/approvals/pending",
            headers=queue_env["headers"],
        )
        assert resp.status_code == 200

    def test_list_403_for_non_member(self, client, db_session, queue_env, user_factory):
        import uuid as _uuid
        from models import User
        from werkzeug.security import generate_password_hash
        from flask_jwt_extended import create_access_token

        outsider = User(username=f"out_{_uuid.uuid4().hex[:8]}", email=f"out_{_uuid.uuid4().hex[:8]}@example.com")
        outsider.password_hash = generate_password_hash("password123")
        db_session.add(outsider)
        db_session.commit()
        headers = {"Authorization": "Bearer " + create_access_token(identity=str(outsider.id))}

        resp = client.get(
            f"{BASE_URL}/workspaces/{queue_env['org'].id}/approvals/pending",
            headers=headers,
        )
        assert resp.status_code == 403

    def test_stats_403_for_non_member(self, client, db_session, queue_env):
        import uuid as _uuid
        from models import User
        from werkzeug.security import generate_password_hash
        from flask_jwt_extended import create_access_token

        outsider = User(username=f"out2_{_uuid.uuid4().hex[:8]}", email=f"out2_{_uuid.uuid4().hex[:8]}@example.com")
        outsider.password_hash = generate_password_hash("password123")
        db_session.add(outsider)
        db_session.commit()
        headers = {"Authorization": "Bearer " + create_access_token(identity=str(outsider.id))}

        resp = client.get(
            f"{BASE_URL}/workspaces/{queue_env['org'].id}/approvals/stats",
            headers=headers,
        )
        assert resp.status_code == 403
