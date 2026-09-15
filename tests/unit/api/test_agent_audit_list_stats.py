"""agent_audit 列表/统计端点补测（迭代 139，59%→100% 缺口）。

覆盖 list_audit_events 全部过滤参数与分页、audit_events_stats 聚合，
以及 export 的 workspace 404 / limit 非法回退分支。
"""

import uuid
from datetime import datetime, timedelta

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

    token = create_access_token(identity=str(user.id))
    return {"user": user, "org": org, "headers": {"Authorization": f"Bearer {token}"}}


def _seed_event(db_session, ws, event_type, occurred_at=None, risk=0, level="info",
                actor_type="user", target_type="task", task_id=None):
    from models import AgentAuditEvent

    event = AgentAuditEvent(
        workspace_id=ws,
        event_type=event_type,
        actor_type=actor_type,
        actor_id="1",
        target_type=target_type,
        target_id="1",
        risk_score=risk,
        level=level,
        task_id=task_id,
        payload={"k": "v"},
        occurred_at=occurred_at or datetime.utcnow(),
    )
    db_session.add(event)
    db_session.commit()
    return event


class TestListAuditEvents:
    def test_empty_list(self, client, owner_auth):
        resp = client.get(
            f"{BASE_URL}/workspaces/{owner_auth['org'].id}/audit-events",
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["items"] == [] and data["total"] == 0

    def test_all_filters_applied(self, client, db_session, owner_auth):
        ws = owner_auth["org"].id
        now = datetime.utcnow()
        _seed_event(db_session, ws, "budget.exceeded", now, level="warning",
                    actor_type="user", target_type="task", task_id=42, risk=30)
        base = f"{BASE_URL}/workspaces/{ws}/audit-events"
        headers = owner_auth["headers"]
        # 每种过滤单独验证
        for qs, expect in [
            ("?event_type=budget.exceeded", 200),
            ("?actor_type=user", 200),
            ("?target_type=task", 200),
            ("?level=warning", 200),
            ("?task_id=42", 200),
            ("?risk_min=10", 200),
            (f"?start_date={now.isoformat()}", 200),
            (f"?end_date={now.isoformat()}", 200),
        ]:
            resp = client.get(base + qs, headers=headers)
            assert resp.status_code == 200, (qs, resp.get_json())

    def test_list_pagination(self, client, db_session, owner_auth):
        ws = owner_auth["org"].id
        now = datetime.utcnow()
        for i in range(3):
            _seed_event(db_session, ws, f"evt.{i}", now - timedelta(minutes=i))
        resp = client.get(
            f"{BASE_URL}/workspaces/{ws}/audit-events?page=1&per_page=2",
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert len(data["items"]) == 2 and data["total"] == 3
        # 第二页拿剩余 1 条
        resp = client.get(
            f"{BASE_URL}/workspaces/{ws}/audit-events?page=2&per_page=2",
            headers=owner_auth["headers"],
        )
        assert len(resp.get_json()["data"]["items"]) == 1

    def test_list_workspace_404(self, client, owner_auth):
        resp = client.get(f"{BASE_URL}/workspaces/987654/audit-events", headers=owner_auth["headers"])
        assert resp.status_code == 404


class TestAuditStats:
    def test_stats_aggregates(self, client, db_session, owner_auth):
        ws = owner_auth["org"].id
        now = datetime.utcnow()
        _seed_event(db_session, ws, "a.evt", now, level="warning", actor_type="user")
        _seed_event(db_session, ws, "a.evt", now, level="error", actor_type="agent")

        resp = client.get(f"{BASE_URL}/workspaces/{ws}/audit-events/stats", headers=owner_auth["headers"])
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["total"] == 2
        assert data["by_level"]["warning"] == 1
        assert data["by_level"]["error"] == 1
        assert data["by_actor_type"]["user"] == 1
        assert data["by_actor_type"]["agent"] == 1

    def test_stats_workspace_404(self, client, owner_auth):
        resp = client.get(f"{BASE_URL}/workspaces/987654/audit-events/stats", headers=owner_auth["headers"])
        assert resp.status_code == 404


class TestExportGaps:
    def test_export_workspace_404(self, client, owner_auth):
        resp = client.get(f"{BASE_URL}/workspaces/987654/audit-events/export", headers=owner_auth["headers"])
        assert resp.status_code == 404

    def test_export_limit_invalid_falls_back_to_max(self, client, db_session, owner_auth):
        ws = owner_auth["org"].id
        _seed_event(db_session, ws, "x.evt", datetime.utcnow())
        resp = client.get(
            f"{BASE_URL}/workspaces/{ws}/audit-events/export?limit=abc",
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["truncated"] is False

    def test_export_event_type_and_task_id_filters(self, client, db_session, owner_auth):
        ws = owner_auth["org"].id
        now = datetime.utcnow()
        _seed_event(db_session, ws, "wanted.evt", now, task_id=7)
        _seed_event(db_session, ws, "other.evt", now, task_id=8)
        resp = client.get(
            f"{BASE_URL}/workspaces/{ws}/audit-events/export?event_type=wanted.evt&task_id=7",
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["count"] == 1
        assert body["filters"]["event_type"] == "wanted.evt"
        assert body["filters"]["task_id"] == "7"


class TestFinalGaps:
    """收口：export end_date 过滤 + list/stats 非成员 403。"""

    def test_export_end_date_filter(self, client, db_session, owner_auth):
        ws = owner_auth["org"].id
        now = datetime.utcnow()
        old = now - timedelta(days=2)
        _seed_event(db_session, ws, "old.evt", old)
        _seed_event(db_session, ws, "new.evt", now)
        end = (now + timedelta(minutes=1)).isoformat()
        resp = client.get(
            f"{BASE_URL}/workspaces/{ws}/audit-events/export?end_date={end}",
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["count"] == 2  # old + new 均早于 end
        assert body["filters"]["end_date"] == end

    def _foreign_workspace_id(self, db_session):
        from models import Organization, User
        from werkzeug.security import generate_password_hash

        tag = uuid.uuid4().hex[:8]
        with __import__('flask').current_app.app_context():
            outsider = User(username=f"aud-out-{tag}", email=f"aud-out-{tag}@example.com")
            outsider.password_hash = generate_password_hash("password123")
            db_session.add(outsider)
            db_session.commit()
            org = Organization(name=f"aud-org-{tag}", slug=f"aud-org-{tag}", owner_id=outsider.id)
            db_session.add(org)
            db_session.commit()
            return org.id

    def test_list_403_for_outsider_workspace(self, client, db_session, owner_auth):
        foreign_ws = self._foreign_workspace_id(db_session)
        resp = client.get(
            f"{BASE_URL}/workspaces/{foreign_ws}/audit-events",
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 403

    def test_stats_403_for_outsider_workspace(self, client, db_session, owner_auth):
        foreign_ws = self._foreign_workspace_id(db_session)
        resp = client.get(
            f"{BASE_URL}/workspaces/{foreign_ws}/audit-events/stats",
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 403
