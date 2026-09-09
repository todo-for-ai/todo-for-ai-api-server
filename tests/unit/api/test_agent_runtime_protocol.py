"""Protocol alignment tests for agent-runtime <-> api-server interactions."""

import uuid
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from flask_jwt_extended import create_access_token


BASE_URL = "/todo-for-ai/api/v1"


@pytest.fixture
def owner_auth_context(app, user_factory):
    """Create a human owner user and JWT headers."""
    user = user_factory()
    with app.app_context():
        access_token = create_access_token(identity=str(user.id))
    return {
        "user": user,
        "headers": {"Authorization": f"Bearer {access_token}"},
    }


@pytest.fixture
def runtime_auth_factory(client, db_session, user_factory, organization_factory, agent_factory):
    """Factory to create runtime auth context via /agent/auth/introspect."""
    from models import AgentKey

    def _create():
        user = user_factory()
        org = organization_factory(owner_id=user.id)
        agent = agent_factory(
            workspace_id=org.id,
            creator_user_id=user.id,
            runner_enabled=True,
        )

        key_row, raw_key = AgentKey.generate_key(
            name=f"Runtime Key {uuid.uuid4().hex[:6]}",
            workspace_id=org.id,
            agent_id=agent.id,
            created_by_user_id=user.id,
        )
        db_session.add(key_row)
        db_session.commit()

        auth_resp = client.post(
            f"{BASE_URL}/agent/auth/introspect",
            json={"agent_key": raw_key},
        )
        assert auth_resp.status_code == 200
        token = auth_resp.get_json()["data"]["access_token"]

        return {
            "user": user,
            "org": org,
            "agent": agent,
            "raw_key": raw_key,
            "headers": {"Authorization": f"Bearer {token}"},
        }

    return _create


class TestAgentRuntimeProtocol:
    """Tests for newly aligned runtime protocol endpoints."""

    def test_notifications_pull_route_is_registered(self, client):
        """Route should be reachable (401 means registered; 404 means missing blueprint)."""
        resp = client.post(f"{BASE_URL}/agent/notifications/pull", json={})
        assert resp.status_code == 401

    def test_release_lease_success(self, client, db_session, runtime_auth_factory, project_factory, task_factory):
        """Agent should be able to actively release an owned active lease."""
        from models import AgentTaskAttempt, AgentTaskAttemptState, AgentTaskLease

        ctx = runtime_auth_factory()
        agent = ctx["agent"]
        user = ctx["user"]

        project = project_factory(owner_id=user.id, organization_id=ctx["org"].id)
        task = task_factory(
            project_id=project.id,
            owner_id=user.id,
            title="Lease release test task",
            content='{"prompt":"release me"}',
            is_ai_task=True,
        )

        attempt_id = f"att_{uuid.uuid4().hex[:8]}"
        lease_id = f"lea_{uuid.uuid4().hex[:8]}"

        attempt = AgentTaskAttempt(
            attempt_id=attempt_id,
            task_id=task.id,
            agent_id=agent.id,
            workspace_id=agent.workspace_id,
            state=AgentTaskAttemptState.ACTIVE,
            lease_id=lease_id,
            started_at=datetime.utcnow(),
            created_by="test",
        )
        lease = AgentTaskLease(
            lease_id=lease_id,
            task_id=task.id,
            attempt_id=attempt_id,
            agent_id=agent.id,
            workspace_id=agent.workspace_id,
            expires_at=datetime.utcnow() + timedelta(seconds=120),
            active=True,
            created_by="test",
        )
        db_session.add(attempt)
        db_session.add(lease)
        db_session.commit()

        resp = client.post(
            f"{BASE_URL}/agent/tasks/{task.id}/lease/release",
            json={"attempt_id": attempt_id, "lease_id": lease_id},
            headers=ctx["headers"],
        )

        assert resp.status_code == 200
        payload = resp.get_json()["data"]
        assert payload["lease_id"] == lease_id
        assert payload["was_active"] is True

        db_session.refresh(lease)
        assert lease.active is False

    def test_release_lease_rejects_non_owner(self, client, db_session, runtime_auth_factory, project_factory, task_factory):
        """A different agent should not be able to release lease owned by another agent."""
        from models import AgentTaskAttempt, AgentTaskAttemptState, AgentTaskLease

        owner_ctx = runtime_auth_factory()
        other_ctx = runtime_auth_factory()

        project = project_factory(
            owner_id=owner_ctx["user"].id,
            organization_id=owner_ctx["org"].id,
        )
        task = task_factory(
            project_id=project.id,
            owner_id=owner_ctx["user"].id,
            title="Lease owner mismatch task",
            content='{"prompt":"owner check"}',
            is_ai_task=True,
        )

        attempt_id = f"att_{uuid.uuid4().hex[:8]}"
        lease_id = f"lea_{uuid.uuid4().hex[:8]}"

        attempt = AgentTaskAttempt(
            attempt_id=attempt_id,
            task_id=task.id,
            agent_id=owner_ctx["agent"].id,
            workspace_id=owner_ctx["agent"].workspace_id,
            state=AgentTaskAttemptState.ACTIVE,
            lease_id=lease_id,
            started_at=datetime.utcnow(),
            created_by="test",
        )
        lease = AgentTaskLease(
            lease_id=lease_id,
            task_id=task.id,
            attempt_id=attempt_id,
            agent_id=owner_ctx["agent"].id,
            workspace_id=owner_ctx["agent"].workspace_id,
            expires_at=datetime.utcnow() + timedelta(seconds=120),
            active=True,
            created_by="test",
        )
        db_session.add(attempt)
        db_session.add(lease)
        db_session.commit()

        resp = client.post(
            f"{BASE_URL}/agent/tasks/{task.id}/lease/release",
            json={"attempt_id": attempt_id, "lease_id": lease_id},
            headers=other_ctx["headers"],
        )

        assert resp.status_code == 409
        assert resp.get_json()["message"] == "LEASE_NOT_OWNER"

    def test_patch_agent_config_creates_new_version(self, client, runtime_auth_factory, db_session):
        """PATCH /agent/config should apply partial updates and return latest config."""
        ctx = runtime_auth_factory()

        patch_resp = client.patch(
            f"{BASE_URL}/agent/config",
            json={
                "max_concurrent_tasks": 7,
                "heartbeat_interval_seconds": 45,
                "log_level": "debug",
                "extra_config": {"feature": "on"},
            },
            headers=ctx["headers"],
        )

        assert patch_resp.status_code == 200
        patch_data = patch_resp.get_json()["data"]
        assert patch_data["max_concurrent_tasks"] == 7
        assert patch_data["heartbeat_interval_seconds"] == 45
        assert patch_data["log_level"] == "DEBUG"
        assert patch_data["extra_config"]["feature"] == "on"
        assert patch_data["version"] >= 1

        get_resp = client.get(f"{BASE_URL}/agent/config", headers=ctx["headers"])
        assert get_resp.status_code == 200
        get_data = get_resp.get_json()["data"]
        assert get_data["max_concurrent_tasks"] == 7
        assert get_data["heartbeat_interval_seconds"] == 45
        assert get_data["log_level"] == "DEBUG"

        # Cleanup to avoid FK teardown issues in fixture delete order.
        from models import AgentRuntimeConfig
        AgentRuntimeConfig.query.filter_by(agent_id=ctx["agent"].id).delete()
        db_session.commit()

    def test_patch_agent_config_rejects_invalid_integer(self, client, runtime_auth_factory):
        """PATCH /agent/config should reject non-integer numeric fields."""
        ctx = runtime_auth_factory()

        resp = client.patch(
            f"{BASE_URL}/agent/config",
            json={"heartbeat_interval_seconds": "not-a-number"},
            headers=ctx["headers"],
        )

        assert resp.status_code == 400
        assert resp.get_json()["message"] == "heartbeat_interval_seconds must be an integer"

    def test_patch_agent_config_rejects_unknown_fields_only(self, client, runtime_auth_factory):
        """PATCH /agent/config should reject payloads without any recognized mutable fields."""
        ctx = runtime_auth_factory()

        resp = client.patch(
            f"{BASE_URL}/agent/config",
            json={"unknown_field": "value"},
            headers=ctx["headers"],
        )

        assert resp.status_code == 400
        assert resp.get_json()["message"] == "No config fields provided"

    def test_logs_batch_accepts_agent_logs(self, client, runtime_auth_factory, project_factory, task_factory):
        """POST /agent/logs/batch should persist agent-side logs for accessible tasks."""
        from models import TaskLog, TaskLogActorType

        ctx = runtime_auth_factory()
        user = ctx["user"]
        agent = ctx["agent"]
        project = project_factory(owner_id=user.id, organization_id=ctx["org"].id)
        task = task_factory(
            project_id=project.id,
            owner_id=user.id,
            title="Batch logs test task",
            content='{"prompt":"collect logs"}',
            is_ai_task=True,
        )

        resp = client.post(
            f"{BASE_URL}/agent/logs/batch",
            json={
                "logs": [
                    {
                        "task_id": task.id,
                        "content": "runtime log line",
                        "content_type": "text/plain",
                    }
                ]
            },
            headers=ctx["headers"],
        )

        assert resp.status_code == 200
        payload = resp.get_json()["data"]
        assert payload["uploaded"] == 1
        assert payload["skipped"] == 0

        row = TaskLog.query.filter_by(task_id=task.id, actor_agent_id=agent.id).order_by(TaskLog.id.desc()).first()
        assert row is not None
        assert row.actor_type == TaskLogActorType.AGENT
        assert row.content == "runtime log line"

    def test_logs_batch_rejects_non_array_logs(self, client, runtime_auth_factory):
        """POST /agent/logs/batch should validate logs payload type."""
        ctx = runtime_auth_factory()

        resp = client.post(
            f"{BASE_URL}/agent/logs/batch",
            json={"logs": {"task_id": 1, "content": "bad-shape"}},
            headers=ctx["headers"],
        )

        assert resp.status_code == 400
        assert resp.get_json()["message"] == "logs must be array"

    def test_logs_batch_skips_forbidden_project_tasks(self, client, runtime_auth_factory, project_factory, task_factory, db_session):
        """Logs targeting non-accessible projects should be skipped instead of inserted."""
        from models import TaskLog

        ctx = runtime_auth_factory()
        user = ctx["user"]
        agent = ctx["agent"]

        allowed_project = project_factory(owner_id=user.id, organization_id=ctx["org"].id)
        blocked_project = project_factory(owner_id=user.id, organization_id=ctx["org"].id)
        blocked_task = task_factory(
            project_id=blocked_project.id,
            owner_id=user.id,
            title="Blocked task",
            content='{"prompt":"forbidden"}',
            is_ai_task=True,
        )

        agent.allowed_project_ids = [allowed_project.id]
        db_session.commit()

        resp = client.post(
            f"{BASE_URL}/agent/logs/batch",
            json={
                "logs": [
                    {
                        "task_id": blocked_task.id,
                        "content": "should be skipped",
                    }
                ]
            },
            headers=ctx["headers"],
        )

        assert resp.status_code == 200
        payload = resp.get_json()["data"]
        assert payload["uploaded"] == 0
        assert payload["skipped"] == 1
        assert TaskLog.query.filter_by(task_id=blocked_task.id, actor_agent_id=agent.id).count() == 0

    def test_spawn_runtime_uses_existing_key_reveal(self, client, owner_auth_context, db_session, organization_factory, agent_factory):
        """Spawn should use decrypted existing key instead of missing raw_key attribute."""
        from models import AgentKey

        user = owner_auth_context["user"]
        org = organization_factory(owner_id=user.id)
        agent = agent_factory(
            workspace_id=org.id,
            creator_user_id=user.id,
            runner_enabled=False,
        )

        key_row, raw_key = AgentKey.generate_key(
            name="Existing Runtime Key",
            workspace_id=org.id,
            agent_id=agent.id,
            created_by_user_id=user.id,
        )
        db_session.add(key_row)
        db_session.commit()

        fake_provider = MagicMock()
        fake_provider.name = "fake"
        fake_provider.get_runtime_status.return_value = None
        fake_provider.spawn.return_value = {
            "pod_name": "agent-test",
            "runtime_id": "uid-test",
            "status": "creating",
            "agent_id": agent.id,
            "created_at": datetime.utcnow().isoformat(),
        }

        with patch("services.cloud_runtime.management.get_runtime_provider", return_value=fake_provider):
            resp = client.post(
                f"{BASE_URL}/workspaces/{org.id}/agents/{agent.id}/runtime/spawn",
                json={"sandbox_profile": "standard"},
                headers=owner_auth_context["headers"],
            )

        assert resp.status_code == 200
        kwargs = fake_provider.spawn.call_args.kwargs
        assert kwargs["agent"].id == agent.id
        assert kwargs["agent_key"] == raw_key
