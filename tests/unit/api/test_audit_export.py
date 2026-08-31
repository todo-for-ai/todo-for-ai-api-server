"""Tests for Phase 4 enterprise audit export (CSV/JSON, time window, permission, audit)."""

import csv
import io
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


def _seed_event(db_session, ws, event_type, occurred_at, risk=0, payload=None):
    from models import AgentAuditEvent

    event = AgentAuditEvent(
        workspace_id=ws,
        event_type=event_type,
        actor_type="user",
        actor_id="1",
        target_type="task",
        target_id="1",
        risk_score=risk,
        payload=payload or {"k": "v"},
        occurred_at=occurred_at,
    )
    db_session.add(event)
    return event


@pytest.fixture
def seeded_events(db_session, owner_auth):
    ws = owner_auth["org"].id
    now = datetime.utcnow()
    old = _seed_event(db_session, ws, "budget.exceeded", now - timedelta(days=40), risk=30)
    recent_a = _seed_event(db_session, ws, "budget.exceeded", now - timedelta(days=1), risk=25,
                           payload={"limit": 100})
    recent_b = _seed_event(db_session, ws, "agent.soul_rolled_back", now - timedelta(days=2))
    db_session.commit()
    return {"old": old, "recent_a": recent_a, "recent_b": recent_b}


class TestAuditExport:
    def test_json_export_with_time_window(self, client, db_session, owner_auth, seeded_events):
        ws = owner_auth["org"].id
        start = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")

        resp = client.get(
            f"{BASE_URL}/workspaces/{ws}/audit-events/export?format=json&start_date={start}",
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 200
        assert resp.headers["Content-Type"].startswith("application/json")
        assert "attachment" in resp.headers.get("Content-Disposition", "")

        data = resp.get_json()
        assert data["count"] == 2
        assert data["truncated"] is False
        types = {item["event_type"] for item in data["items"]}
        assert types == {"budget.exceeded", "agent.soul_rolled_back"}

    def test_csv_export_shape(self, client, db_session, owner_auth, seeded_events):
        ws = owner_auth["org"].id
        resp = client.get(
            f"{BASE_URL}/workspaces/{ws}/audit-events/export?format=csv",
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 200
        assert resp.headers["Content-Type"].startswith("text/csv")

        rows = list(csv.reader(io.StringIO(resp.get_data(as_text=True))))
        header, data_rows = rows[0], rows[1:]
        assert len(data_rows) == 3
        assert "event_type" in header
        payload_col = header.index("payload")
        event_type_col = header.index("event_type")
        by_type = {row[event_type_col]: row for row in data_rows}
        # payload 序列化为 JSON 字符串
        assert '"limit": 100' in by_type["budget.exceeded"][payload_col]

    def test_filters_and_risk_min(self, client, db_session, owner_auth, seeded_events):
        ws = owner_auth["org"].id
        resp = client.get(
            f"{BASE_URL}/workspaces/{ws}/audit-events/export?format=json&event_type=budget.exceeded&risk_min=30",
            headers=owner_auth["headers"],
        )
        data = resp.get_json()
        assert data["count"] == 1  # 只有 40 天前那条 risk=30 的
        assert data["items"][0]["risk_score"] == 30

    def test_export_action_itself_audited(self, client, db_session, owner_auth, seeded_events):
        ws = owner_auth["org"].id
        client.get(
            f"{BASE_URL}/workspaces/{ws}/audit-events/export?format=json",
            headers=owner_auth["headers"],
        )
        # 导出动作本身写审计（不在本次导出结果内，下次导出可见）
        from models import AgentAuditEvent
        export_events = AgentAuditEvent.query.filter_by(
            workspace_id=ws, event_type="audit.exported",
        ).all()
        assert len(export_events) == 1
        assert export_events[0].payload["format"] == "json"
        assert export_events[0].payload["count"] == 3

    def test_requires_workspace_manage(self, client, db_session, _isolated_app, owner_auth, seeded_events, user_factory):
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

        resp = client.get(
            f"{BASE_URL}/workspaces/{ws}/audit-events/export?format=csv",
            headers=headers,
        )
        assert resp.status_code == 403

    def test_invalid_format_400(self, client, owner_auth):
        ws = owner_auth["org"].id
        resp = client.get(
            f"{BASE_URL}/workspaces/{ws}/audit-events/export?format=xml",
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 400
