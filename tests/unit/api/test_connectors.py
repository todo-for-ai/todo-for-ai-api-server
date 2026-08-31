"""Tests for Phase 4 write-back connector (Linear ingest: signature, task upsert, comments)."""

import hashlib
import hmac as hmac_mod
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))

import pytest

BASE_URL = "/todo-for-ai/api/v1"
WEBHOOK_SECRET = "linear-webhook-secret"


@pytest.fixture(scope="function", autouse=True)
def _isolated_app():
    os.environ["SECRET_ENCRYPTION_KEY"] = "uCuDTIUbpnE0Z47hrUqyNY8w7SjtIwKxnvZTduXeN30="
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
def connector_ctx(client, db_session, owner_auth, project_factory):
    """启用 Linear 连接器，绑定默认项目。"""
    ws = owner_auth["org"].id
    project = project_factory(owner_id=owner_auth["user"].id, organization_id=ws)

    resp = client.put(
        f"{BASE_URL}/workspaces/{ws}/connectors/linear",
        json={"enabled": True, "secret": WEBHOOK_SECRET,
              "default_project_id": project.id},
        headers=owner_auth["headers"],
    )
    assert resp.status_code == 200, resp.get_json()
    return {"project": project, "config": resp.get_json()["data"]["connector"]}


def _linear_headers(body: bytes, secret: str = WEBHOOK_SECRET, *, signed=True):
    headers = {"Content-Type": "application/json"}
    if signed:
        digest = hmac_mod.new(secret.encode(), body, hashlib.sha256).hexdigest()
        headers["Linear-Signature"] = digest
    return headers


def _issue_payload(identifier="LIN-1", title="Sync me", state_type="backlog"):
    return {
        "action": "create", "type": "Issue",
        "data": {
            "identifier": identifier, "title": title,
            "description": "from linear",
            "url": f"https://linear.app/x/issue/{identifier}",
            "state": {"type": state_type},
        },
    }


class TestConnectorConfig:
    def test_configure_masks_secret_and_audits(self, client, db_session, owner_auth, project_ctx=None):
        from models import AgentAuditEvent

        ws = owner_auth["org"].id
        put = client.put(
            f"{BASE_URL}/workspaces/{ws}/connectors/linear",
            json={"enabled": True, "secret": WEBHOOK_SECRET},
            headers=owner_auth["headers"],
        )
        assert put.status_code == 200
        saved = put.get_json()["data"]["connector"]
        assert saved["has_secret"] is True
        assert WEBHOOK_SECRET not in str(saved)

        events = AgentAuditEvent.query.filter_by(
            workspace_id=ws, event_type="connector.configured",
        ).all()
        assert len(events) == 1

    def test_unknown_provider_400(self, client, owner_auth):
        ws = owner_auth["org"].id
        resp = client.put(
            f"{BASE_URL}/workspaces/{ws}/connectors/asana",
            json={"enabled": True},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 400


class TestLinearIngest:
    def test_issue_created_imports_task(self, client, db_session, owner_auth, connector_ctx):
        import json as _json

        from models import Task, TaskEventOutbox

        ws = owner_auth["org"].id
        payload = _issue_payload()
        body = _json.dumps(payload).encode()
        resp = client.post(
            f"{BASE_URL}/connectors/linear/{ws}/ingest",
            data=body, headers=_linear_headers(body),
        )
        assert resp.status_code == 200, resp.get_json()
        result = resp.get_json()["data"]
        assert result["handled"] is True and result["created"] is True

        task = db_session.get(Task, result["task_id"])
        assert task.creator_identifier == "linear:LIN-1"
        assert task.project_id == connector_ctx["project"].id
        assert "from linear" in (task.content or "")

        outbox = TaskEventOutbox.query.filter_by(
            event_type="connector.linear.issue_synced", task_id=task.id,
        ).all()
        assert len(outbox) == 1

    def test_issue_update_syncs_status_without_duplicating(self, client, db_session, owner_auth, connector_ctx):
        import json as _json

        from models import Task

        ws = owner_auth["org"].id
        # create
        body = _json.dumps(_issue_payload()).encode()
        client.post(f"{BASE_URL}/connectors/linear/{ws}/ingest",
                    data=body, headers=_linear_headers(body))
        # update → completed
        update_payload = _issue_payload(state_type="completed")
        update_payload["action"] = "update"
        body = _json.dumps(update_payload).encode()
        resp = client.post(f"{BASE_URL}/connectors/linear/{ws}/ingest",
                           data=body, headers=_linear_headers(body))
        assert resp.status_code == 200
        result = resp.get_json()["data"]
        assert result["created"] is False and result["status_changed"] is True

        tasks = Task.query.filter_by(creator_identifier="linear:LIN-1").all()
        assert len(tasks) == 1
        assert tasks[0].status.value == "done"

    def test_comment_appends_task_log(self, client, db_session, owner_auth, connector_ctx):
        import json as _json

        from models import TaskLog

        ws = owner_auth["org"].id
        body = _json.dumps(_issue_payload()).encode()
        client.post(f"{BASE_URL}/connectors/linear/{ws}/ingest",
                    data=body, headers=_linear_headers(body))

        comment_payload = {
            "action": "create", "type": "Comment",
            "data": {"body": "外部评论", "issue": {"identifier": "LIN-1"},
                     "user": {"name": "Alice"}},
        }
        body = _json.dumps(comment_payload).encode()
        resp = client.post(f"{BASE_URL}/connectors/linear/{ws}/ingest",
                           data=body, headers=_linear_headers(body))
        assert resp.status_code == 200
        assert resp.get_json()["data"]["handled"] is True

        logs = TaskLog.query.filter_by(created_by="connector:linear").all()
        assert len(logs) == 1
        assert "[Linear · Alice]" in logs[0].content

    def test_bad_signature_rejected(self, client, db_session, owner_auth, connector_ctx):
        import json as _json

        ws = owner_auth["org"].id
        body = _json.dumps(_issue_payload()).encode()
        resp = client.post(
            f"{BASE_URL}/connectors/linear/{ws}/ingest",
            data=body, headers=_linear_headers(body, secret="wrong"),
        )
        assert resp.status_code == 401

    def test_disabled_connector_rejected(self, client, db_session, owner_auth, project_factory):
        import json as _json

        ws = owner_auth["org"].id
        project = project_factory(owner_id=owner_auth["user"].id, organization_id=ws)
        client.put(
            f"{BASE_URL}/workspaces/{ws}/connectors/linear",
            json={"enabled": False, "secret": WEBHOOK_SECRET,
                  "default_project_id": project.id},
            headers=owner_auth["headers"],
        )
        body = _json.dumps(_issue_payload()).encode()
        resp = client.post(f"{BASE_URL}/connectors/linear/{ws}/ingest",
                           data=body, headers=_linear_headers(body))
        assert resp.status_code == 400


class TestGitLabIngest:
    """Phase 4 写回侧第二个连接器：GitLab（X-GitLab-Token 验签）。"""

    @pytest.fixture
    def gitlab_ctx(self, client, db_session, owner_auth, project_factory):
        ws = owner_auth["org"].id
        project = project_factory(owner_id=owner_auth["user"].id, organization_id=ws)
        resp = client.put(
            f"{BASE_URL}/workspaces/{ws}/connectors/gitlab",
            json={"enabled": True, "secret": WEBHOOK_SECRET,
                  "default_project_id": project.id},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 200
        return {"project": project, "ws": ws}

    def _gitlab_headers(self, token=WEBHOOK_SECRET, *, signed=True):
        headers = {"Content-Type": "application/json"}
        if signed:
            headers["X-GitLab-Token"] = token
        return headers

    def _issue_payload(self, iid=7, action="open", state="opened", gl_project_id=101):
        return {
            "object_kind": "issue",
            "event_type": "issue",
            "object_attributes": {
                "iid": iid, "title": "GitLab issue",
                "description": "from gitlab",
                "url": f"https://gitlab.com/x/y/-/issues/{iid}",
                "state": state, "action": action,
            },
            "project": {"id": gl_project_id, "path_with_namespace": "x/y"},
            "user": {"username": "alice"},
        }

    def _note_payload(self, iid=7):
        return {
            "object_kind": "note",
            "object_attributes": {"note": "GitLab 侧评论"},
            "issue": {"iid": iid},
            "project": {"id": 101},
            "user": {"username": "bob"},
        }

    def test_issue_open_imports_task(self, client, db_session, owner_auth, gitlab_ctx):
        import json as _json

        from models import Task, TaskEventOutbox

        ws = gitlab_ctx["ws"]
        body = _json.dumps(self._issue_payload()).encode()
        resp = client.post(
            f"{BASE_URL}/connectors/gitlab/{ws}/ingest",
            data=body, headers=self._gitlab_headers(),
        )
        assert resp.status_code == 200, resp.get_json()
        result = resp.get_json()["data"]
        assert result["handled"] is True and result["created"] is True

        task = db_session.get(Task, result["task_id"])
        assert task.creator_identifier == "gitlab:101:7"
        assert task.project_id == gitlab_ctx["project"].id
        assert task.status.value == "todo"

        outbox = TaskEventOutbox.query.filter_by(
            event_type="connector.gitlab.issue_synced", task_id=task.id,
        ).all()
        assert len(outbox) == 1

    def test_issue_close_updates_status_no_duplicate(self, client, db_session, owner_auth, gitlab_ctx):
        import json as _json

        from models import Task

        ws = gitlab_ctx["ws"]
        body = _json.dumps(self._issue_payload()).encode()
        client.post(f"{BASE_URL}/connectors/gitlab/{ws}/ingest",
                    data=body, headers=self._gitlab_headers())

        closed = self._issue_payload(action="close", state="closed")
        body = _json.dumps(closed).encode()
        resp = client.post(f"{BASE_URL}/connectors/gitlab/{ws}/ingest",
                           data=body, headers=self._gitlab_headers())
        assert resp.status_code == 200
        result = resp.get_json()["data"]
        assert result["created"] is False and result["status_changed"] is True

        tasks = Task.query.filter_by(creator_identifier="gitlab:101:7").all()
        assert len(tasks) == 1
        assert tasks[0].status.value == "done"

    def test_note_appends_task_log(self, client, db_session, owner_auth, gitlab_ctx):
        import json as _json

        from models import TaskLog

        ws = gitlab_ctx["ws"]
        body = _json.dumps(self._issue_payload()).encode()
        client.post(f"{BASE_URL}/connectors/gitlab/{ws}/ingest",
                    data=body, headers=self._gitlab_headers())

        body = _json.dumps(self._note_payload()).encode()
        resp = client.post(f"{BASE_URL}/connectors/gitlab/{ws}/ingest",
                           data=body, headers=self._gitlab_headers())
        assert resp.status_code == 200
        assert resp.get_json()["data"]["handled"] is True

        logs = TaskLog.query.filter_by(created_by="connector:gitlab").all()
        assert len(logs) == 1
        assert "[GitLab · bob]" in logs[0].content

    def test_bad_token_rejected(self, client, db_session, owner_auth, gitlab_ctx):
        import json as _json

        ws = gitlab_ctx["ws"]
        body = _json.dumps(self._issue_payload()).encode()
        resp = client.post(
            f"{BASE_URL}/connectors/gitlab/{ws}/ingest",
            data=body, headers=self._gitlab_headers(token="wrong-token"),
        )
        assert resp.status_code == 401


class TestJiraIngest:
    """Phase 4 写回侧第三个连接器：Jira（配置令牌验签）。"""

    @pytest.fixture
    def jira_ctx(self, client, db_session, owner_auth, project_factory):
        ws = owner_auth["org"].id
        project = project_factory(owner_id=owner_auth["user"].id, organization_id=ws)
        resp = client.put(
            f"{BASE_URL}/workspaces/{ws}/connectors/jira",
            json={"enabled": True, "secret": WEBHOOK_SECRET,
                  "default_project_id": project.id},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 200
        return {"project": project, "ws": ws}

    def _jira_headers(self, token=WEBHOOK_SECRET, *, signed=True):
        headers = {"Content-Type": "application/json"}
        if signed:
            headers["X-Todo4AI-Token"] = token
        return headers

    def _issue_payload(self, key="PROJ-12", status_name="In Progress", action="created"):
        return {
            "webhookEvent": f"jira:issue_{action}",
            "issue": {
                "key": key,
                "fields": {
                    "summary": "Jira issue title",
                    "description": "from jira",
                    "status": {"name": status_name},
                },
            },
        }

    def test_issue_created_imports_task(self, client, db_session, owner_auth, jira_ctx):
        import json as _json

        from models import Task, TaskEventOutbox

        ws = jira_ctx["ws"]
        body = _json.dumps(self._issue_payload()).encode()
        resp = client.post(
            f"{BASE_URL}/connectors/jira/{ws}/ingest",
            data=body, headers=self._jira_headers(),
        )
        assert resp.status_code == 200, resp.get_json()
        result = resp.get_json()["data"]
        assert result["handled"] is True and result["created"] is True

        task = db_session.get(Task, result["task_id"])
        assert task.creator_identifier == "jira:PROJ-12"
        assert task.project_id == jira_ctx["project"].id
        assert task.status.value == "in_progress"

        outbox = TaskEventOutbox.query.filter_by(
            event_type="connector.jira.issue_synced", task_id=task.id,
        ).all()
        assert len(outbox) == 1

    def test_issue_updated_maps_done_no_duplicate(self, client, db_session, owner_auth, jira_ctx):
        import json as _json

        from models import Task

        ws = jira_ctx["ws"]
        body = _json.dumps(self._issue_payload()).encode()
        client.post(f"{BASE_URL}/connectors/jira/{ws}/ingest",
                    data=body, headers=self._jira_headers())

        done_payload = self._issue_payload(status_name="Done", action="updated")
        body = _json.dumps(done_payload).encode()
        resp = client.post(f"{BASE_URL}/connectors/jira/{ws}/ingest",
                           data=body, headers=self._jira_headers())
        assert resp.status_code == 200
        result = resp.get_json()["data"]
        assert result["created"] is False and result["status_changed"] is True

        tasks = Task.query.filter_by(creator_identifier="jira:PROJ-12").all()
        assert len(tasks) == 1
        assert tasks[0].status.value == "done"

    def test_comment_appends_task_log(self, client, db_session, owner_auth, jira_ctx):
        import json as _json

        from models import TaskLog

        ws = jira_ctx["ws"]
        body = _json.dumps(self._issue_payload()).encode()
        client.post(f"{BASE_URL}/connectors/jira/{ws}/ingest",
                    data=body, headers=self._jira_headers())

        comment_payload = {
            "webhookEvent": "jira:comment_created",
            "issue": {"key": "PROJ-12"},
            "comment": {"body": "Jira 侧评论", "author": {"displayName": "Carol"}},
        }
        body = _json.dumps(comment_payload).encode()
        resp = client.post(f"{BASE_URL}/connectors/jira/{ws}/ingest",
                           data=body, headers=self._jira_headers())
        assert resp.status_code == 200
        assert resp.get_json()["data"]["handled"] is True

        logs = TaskLog.query.filter_by(created_by="connector:jira").all()
        assert len(logs) == 1
        assert "[Jira · Carol]" in logs[0].content

    def test_query_token_accepted_and_bad_token_rejected(self, client, db_session, owner_auth, jira_ctx):
        import json as _json

        ws = jira_ctx["ws"]
        body = _json.dumps(self._issue_payload()).encode()

        # query 参数携带令牌也可通过
        ok = client.post(
            f"{BASE_URL}/connectors/jira/{ws}/ingest?token={WEBHOOK_SECRET}",
            data=body, headers={"Content-Type": "application/json"},
        )
        assert ok.status_code == 200

        bad = client.post(
            f"{BASE_URL}/connectors/jira/{ws}/ingest",
            data=body, headers=self._jira_headers(token="wrong"),
        )
        assert bad.status_code == 401
