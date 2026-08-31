"""Tests for Phase 4 open event protocol (unified task/evidence/approval stream)."""

import base64
import json
import sys
import os
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))

import pytest

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
    ctx = app.app_context()
    ctx.push()
    db.create_all()
    yield app
    db.session.remove()
    db.drop_all()
    ctx.pop()


@pytest.fixture
def db_session(_isolated_app):
    from models import db
    with _isolated_app.app_context():
        yield db.session
    db.session.rollback()


@pytest.fixture
def client(_isolated_app):
    return _isolated_app.test_client()


@pytest.fixture
def owner_auth(_isolated_app, db_session):
    import uuid as _uuid
    from models import User, Organization
    from werkzeug.security import generate_password_hash
    from flask_jwt_extended import create_access_token

    unique_id = str(_uuid.uuid4())[:8]
    user = User(username=f"testuser_{unique_id}", email=f"test_{unique_id}@example.com")
    user.password_hash = generate_password_hash("password123")
    db_session.add(user)
    db_session.commit()

    org = Organization(name=f"org-{unique_id}", slug=f"org-{unique_id}", owner_id=user.id)
    db_session.add(org)
    db_session.commit()

    with _isolated_app.app_context():
        token = create_access_token(identity=str(user.id))
    return {"user": user, "org": org, "headers": {"Authorization": f"Bearer {token}"}}


@pytest.fixture
def stream_ctx(db_session, owner_auth, project_factory, task_factory):
    """三类事件各若干条：outbox 任务事件、证据、审批事件。"""
    from datetime import datetime, timedelta

    from models import AgentTaskEvent, TaskEventOutbox, TaskEvidenceRecord

    ws = owner_auth["org"].id
    project = project_factory(owner_id=owner_auth["user"].id, organization_id=ws)
    now = datetime.utcnow()

    task_a = task_factory(project_id=project.id, owner_id=owner_auth["user"].id)
    task_b = task_factory(project_id=project.id, owner_id=owner_auth["user"].id)

    outbox1 = TaskEventOutbox(
        event_id="rev-1", event_type="repo.pull_request.merged",
        task_id=task_a.id, project_id=project.id, workspace_id=ws,
        payload={"pr_number": 5, "merged": True},
        occurred_at=now - timedelta(minutes=30),
        created_by="test",
    )
    outbox2 = TaskEventOutbox(
        event_id="rev-2", event_type="repo.issues.opened",
        task_id=task_b.id, project_id=project.id, workspace_id=ws,
        payload={"issue_number": 9},
        occurred_at=now - timedelta(minutes=20),
        created_by="test",
    )
    evidence = TaskEvidenceRecord(
        task_id=task_a.id, evidence_type="test", status="passed",
        summary="24 passed", detail={"exit_code": 0},
        verified_at=now - timedelta(minutes=10),
        created_by="test",
    )
    approval_req = AgentTaskEvent(
        task_id=task_a.id, attempt_id="", agent_id=None, workspace_id=ws,
        event_type="interaction_request", seq=1,
        event_timestamp=now - timedelta(minutes=5),
        payload={"interaction_type": "pr_merge", "status": "pending_approval"},
        message="merge approval", created_by="test",
    )
    approval_dec = AgentTaskEvent(
        task_id=task_a.id, attempt_id="", agent_id=None, workspace_id=ws,
        event_type="interaction_approval", seq=2,
        event_timestamp=now - timedelta(minutes=1),
        payload={"decision": "approved"},
        message="merge approved", created_by="test",
    )
    db_session.add_all([outbox1, outbox2, evidence, approval_req, approval_dec])
    db_session.commit()

    return {
        "project": project, "task_a": task_a, "task_b": task_b,
        "outbox1": outbox1, "outbox2": outbox2, "evidence": evidence,
        "approval_req": approval_req, "approval_dec": approval_dec,
    }


def _get_events(client, owner_auth, ws, **params):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    suffix = f"?{query}" if query else ""
    return client.get(
        f"{BASE_URL}/workspaces/{ws}/open/events{suffix}",
        headers=owner_auth["headers"],
    )


class TestOpenEventStream:
    def test_unified_stream_covers_three_categories(self, client, owner_auth, stream_ctx):
        ws = owner_auth["org"].id
        resp = _get_events(client, owner_auth, ws)
        assert resp.status_code == 200
        data = resp.get_json()["data"]

        events = data["events"]
        categories = {event["category"] for event in events}
        assert categories == {"task", "evidence", "approval"}

        by_type = {event["type"] for event in events}
        assert "repo.pull_request.merged" in by_type
        assert "evidence.recorded" in by_type
        assert "approval.requested" in by_type
        assert "approval.decided" in by_type

        # envelope 字段齐全
        for event in events:
            assert set(event.keys()) >= {
                "id", "category", "type", "occurred_at", "workspace_id", "task_id", "data",
            }

        # 时间升序
        occurred = [event["occurred_at"] for event in events]
        assert occurred == sorted(occurred)

    def test_cursor_pagination_advances_without_replay(self, client, owner_auth, stream_ctx):
        ws = owner_auth["org"].id
        first = _get_events(client, owner_auth, ws, limit=2).get_json()["data"]
        assert first["has_more"] is True
        assert len(first["events"]) == 2
        assert first["next_cursor"]

        second = _get_events(
            client, owner_auth, ws, limit=2, cursor=first["next_cursor"],
        ).get_json()["data"]
        first_ids = {event["id"] for event in first["events"]}
        second_ids = {event["id"] for event in second["events"]}
        assert not (first_ids & second_ids), "游标分页不允许重放已消费事件"

        third = _get_events(
            client, owner_auth, ws, limit=2, cursor=second["next_cursor"],
        ).get_json()["data"]
        assert third["events"], "剩余事件应可继续消费"
        third_ids = {event["id"] for event in third["events"]}
        assert not (first_ids & third_ids) and not (second_ids & third_ids)

    def test_category_filter(self, client, owner_auth, stream_ctx):
        ws = owner_auth["org"].id
        resp = _get_events(client, owner_auth, ws, category="approval")
        events = resp.get_json()["data"]["events"]
        assert events and all(event["category"] == "approval" for event in events)

    def test_invalid_cursor_treated_as_start(self, client, owner_auth, stream_ctx):
        ws = owner_auth["org"].id
        resp = _get_events(client, owner_auth, ws, cursor="not-a-cursor")
        assert resp.status_code == 200
        assert resp.get_json()["data"]["events"]

    def test_requires_workspace_manage(self, client, db_session, _isolated_app, owner_auth, stream_ctx, user_factory):
        import uuid as _uuid
        from flask_jwt_extended import create_access_token
        from models import User
        from werkzeug.security import generate_password_hash

        ws = owner_auth["org"].id
        outsider = User(username=f"out_{str(_uuid.uuid4())[:6]}", email=f"out_{str(_uuid.uuid4())[:6]}@x.com")
        outsider.password_hash = generate_password_hash("password123")
        db_session.add(outsider)
        db_session.commit()
        with _isolated_app.app_context():
            headers = {"Authorization": f"Bearer {create_access_token(identity=str(outsider.id))}"}

        resp = _get_events(client, {"headers": headers}, ws)
        assert resp.status_code == 403


class TestOpenSchema:
    def test_schema_self_description(self, client, owner_auth):
        ws = owner_auth["org"].id
        resp = client.get(
            f"{BASE_URL}/workspaces/{ws}/open/schema",
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["version"] == 1
        assert set(data["categories"]) == {"task", "evidence", "approval"}
        assert "event_envelope" in data
