"""外部系统连接器包（services/connectors/）单元回归。

覆盖：配置存取（upsert 幂等/secret 加密）、三平台验签（常量时间/fail-closed）、
Jira/GitLab/Linear 导入全路径（建任务/状态映射/评论追加/未导入评论跳过/
缺 default_project_id 报错/事件未启用/未知事件忽略/出站事件落 outbox）。
"""

import hmac
from types import SimpleNamespace

import pytest

from models import (
    ExternalConnectorConfig,
    Project,
    Task,
    TaskEventOutbox,
    TaskLog,
    TaskStatus,
    db,
)
from services.connectors import (
    ingest_gitlab,
    ingest_jira,
    ingest_linear,
    list_connectors,
    upsert_connector,
    verify_gitlab_token,
    verify_jira_token,
    verify_linear_signature,
)
from services.connectors.common import emit_sync_event, resolve_project
from services.connectors.gitlab import _apply_gitlab_issue, _gitlab_external_key
from services.connectors.jira import _apply_jira_issue, _jira_external_key, _map_jira_status
from services.connectors.linear import _apply_comment as _linear_apply_comment
from services.connectors.linear import _apply_issue as _linear_apply_issue
from services.connectors.store import get_connector
from services.github_app import decrypt_str


@pytest.fixture(scope="function", autouse=True)
def _isolated_app(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", Fernet.generate_key().decode())
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
def project():
    """自包含项目（与被测服务同一个 db.session，避免跨库不可见）。"""
    import uuid
    from models import User
    user = User(username=f"cu_{uuid.uuid4().hex[:8]}",
                email=f"cu_{uuid.uuid4().hex[:6]}@t.io")
    db.session.add(user)
    db.session.flush()
    proj = Project(name=f"cp_{uuid.uuid4().hex[:8]}", owner_id=user.id)
    db.session.add(proj)
    db.session.commit()
    return proj


@pytest.fixture
def make_config(project):
    """按 provider 造已启用的 connector 配置（默认 workspace=1）。"""

    def _make(provider="linear", workspace_id=1, enabled=True):
        row = ExternalConnectorConfig(
            workspace_id=workspace_id, provider=provider,
            enabled=enabled, default_project_id=project.id,
        )
        db.session.add(row)
        db.session.commit()
        return row

    return _make


@pytest.fixture
def config(make_config):
    return make_config("linear")


class TestStore:
    def test_upsert_creates_then_updates(self, project):
        row = upsert_connector(9, "linear", {
            "enabled": True, "default_project_id": project.id, "secret": "whsec-1",
        })
        assert row.id is not None and row.enabled is True
        assert decrypt_str(row.secret_encrypted) == "whsec-1"

        same = upsert_connector(9, "linear", {"enabled": False})
        assert same.id == row.id and same.enabled is False
        assert same.secret_encrypted  # 未传 secret 保持原值
        assert get_connector(9, "linear").id == row.id

    def test_upsert_ignores_empty_project(self, project):
        row = upsert_connector(9, "jira", {"default_project_id": None, "secret": ""})
        assert row.default_project_id is None
        assert row.secret_encrypted is None

    def test_list_connectors(self, project):
        upsert_connector(11, "linear", {"enabled": True})
        upsert_connector(11, "jira", {"enabled": False})
        upsert_connector(12, "linear", {"enabled": True})
        assert len(list_connectors(11)) == 2
        assert len(list_connectors(12)) == 1


class TestVerify:
    def test_linear_signature(self):
        body = b'{"issue": 1}'
        secret = "whsec"
        good = hmac.new(secret.encode(), body, "sha256").hexdigest()
        assert verify_linear_signature(body, good, secret) is True
        assert verify_linear_signature(body, "deadbeef", secret) is False
        assert verify_linear_signature(body, "", secret) is False
        assert verify_linear_signature(body, good, "") is False

    def test_gitlab_token(self):
        assert verify_gitlab_token("t", "t") is True
        assert verify_gitlab_token("t", "x") is False
        assert verify_gitlab_token("", "t") is False
        assert verify_gitlab_token("t", None) is False

    def test_jira_token(self):
        assert verify_jira_token("t", "t") is True
        assert verify_jira_token("t", "x") is False
        assert verify_jira_token(None, "t") is False


class TestJira:
    @pytest.fixture(autouse=True)
    def _jira_config(self, make_config):
        self.config = make_config("jira")

    def test_map_status_variants(self):
        assert _map_jira_status("In Progress") == TaskStatus.IN_PROGRESS
        assert _map_jira_status("进行中") == TaskStatus.IN_PROGRESS
        assert _map_jira_status("DONE") == TaskStatus.DONE
        assert _map_jira_status("Blocked") is None
        assert _map_jira_status(None) is None

    def test_external_key_truncated(self):
        assert _jira_external_key("AB-1") == "jira:AB-1"
        assert len(_jira_external_key("K" * 300)) == 100

    def test_issue_creates_task_and_outbox(self, project, config):
        result = _apply_jira_issue(1, config, {
            "key": "AB-1",
            "fields": {"summary": "修复登录", "description": "  详情  ",
                       "status": {"name": "To Do"}},
        })
        assert result["created"] is True and result["handled"] is True
        task = Task.query.filter_by(
            creator_identifier="jira:AB-1").one()
        assert task.title == "修复登录"
        assert task.content == "详情"
        assert task.status == TaskStatus.TODO
        event = TaskEventOutbox.query.filter_by(task_id=task.id).one()
        assert event.event_type == "connector.jira.issue_synced"
        assert event.payload["jira_status"] == "To Do"

    def test_comment_before_import_skips(self, project, config):
        result = ingest_jira(1, {
            "webhookEvent": "jira:comment_created",
            "issue": {"key": "AB-ZZ"},
            "comment": {"body": "x"},
        })
        assert result == {"handled": False, "kind": "comment",
                          "reason": "task not imported yet"}

    def test_issue_update_maps_status(self, project, config):
        _apply_jira_issue(1, self.config, {"key": "AB-2", "fields": {"summary": "t"}})
        result = _apply_jira_issue(1, self.config, {
            "key": "AB-2",
            "fields": {"summary": "t2", "status": {"name": "done"}},
        })
        task = Task.query.filter_by(creator_identifier="jira:AB-2").one()
        assert result["created"] is False
        assert task.title == "t2"
        assert task.status == TaskStatus.DONE
        assert result["status_changed"] is True

    def test_unknown_status_keeps_current(self, project, config):
        _apply_jira_issue(1, self.config, {"key": "AB-3", "fields": {"summary": "t"}})
        _apply_jira_issue(1, self.config, {
            "key": "AB-3", "fields": {"summary": "t", "status": {"name": "Blocked"}},
        })
        task = Task.query.filter_by(creator_identifier="jira:AB-3").one()
        assert task.status == TaskStatus.TODO

    def test_missing_project_raises(self):
        self.config.default_project_id = None
        db.session.commit()
        with pytest.raises(ValueError, match="default_project_id"):
            _apply_jira_issue(1, self.config, {"key": "AB-9", "fields": {}})

    def test_ingest_dispatch(self, project):
        created = ingest_jira(1, {
            "webhookEvent": "jira:issue_created",
            "issue": {"key": "AB-4", "fields": {"summary": "s"}},
        })
        assert created["handled"] is True
        # 重新查询（fixture 的会话对象可能持有提交前的缓存）
        assert get_connector(1, "jira").last_synced_at is not None

        commented = ingest_jira(1, {
            "webhookEvent": "jira:comment_created",
            "issue": {"key": "AB-4"},
            "comment": {"body": "看下", "author": {"displayName": "张三"}},
        })
        assert commented["handled"] is True
        log = TaskLog.query.filter_by(task_id=commented["task_id"]).one()
        assert log.content == "[Jira · 张三] 看下"

        ignored = ingest_jira(1, {"webhookEvent": "jira:project_updated"})
        assert ignored == {"handled": False, "reason": "jira:project_updated ignored"}

    def test_ingest_requires_enabled_connector(self):
        self.config.enabled = False
        db.session.commit()
        with pytest.raises(ValueError, match="not enabled"):
            ingest_jira(1, {"webhookEvent": "jira:issue_created",
                            "issue": {"key": "X-1"}})


class TestGitLab:
    @pytest.fixture(autouse=True)
    def _gitlab_config(self, make_config):
        self.config = make_config("gitlab")

    def _issue_payload(self, **overrides):
        attrs = {"iid": 7, "title": "GL issue", "state": "opened", "action": "open"}
        attrs.update(overrides)
        return attrs

    def test_issue_create_and_close(self, project):
        result = ingest_gitlab(1, {
            "object_kind": "issue",
            "object_attributes": self._issue_payload(),
            "project": {"id": 55},
        })
        assert result["created"] is True
        task = Task.query.filter_by(
            creator_identifier=_gitlab_external_key(55, 7)).one()
        assert task.status == TaskStatus.TODO

        ingest_gitlab(1, {
            "object_kind": "issue",
            "object_attributes": self._issue_payload(state="closed", action="close"),
            "project": {"id": 55},
        })
        assert task.status == TaskStatus.DONE

    def test_issue_description_appends_url(self, project):
        _apply_gitlab_issue(1, self.config, self._issue_payload(
            description="正文", url="https://gitlab.example/a/1"), {"id": 55})
        task = Task.query.filter_by(
            creator_identifier=_gitlab_external_key(55, 7)).one()
        assert "正文" in task.content and "来源: https://gitlab.example/a/1" in task.content

    def test_note_on_imported_issue(self, project):
        ingest_gitlab(1, {"object_kind": "issue",
                          "object_attributes": self._issue_payload(),
                          "project": {"id": 55}})
        result = ingest_gitlab(1, {
            "object_kind": "note",
            "issue": {"iid": 7},
            "project": {"id": 55},
            "user": {"username": "li"},
            "object_attributes": {"note": "跟进"},
        })
        assert result["handled"] is True
        log = TaskLog.query.filter_by(task_id=result["task_id"]).one()
        assert log.content == "[GitLab · li] 跟进"

    def test_note_before_import_skips(self, project):
        result = ingest_gitlab(1, {
            "object_kind": "note", "issue": {"iid": 99},
            "project": {"id": 55}, "object_attributes": {"note": "x"},
        })
        assert result == {"handled": False, "kind": "comment",
                          "reason": "task not imported yet"}

    def test_ignored_kind_and_disabled(self, project):
        assert ingest_gitlab(1, {"object_kind": "push"}) == {
            "handled": False, "reason": "push ignored"}
        self.config.enabled = False
        db.session.commit()
        with pytest.raises(ValueError, match="gitlab connector not enabled"):
            ingest_gitlab(1, {"object_kind": "issue"})


class TestLinear:
    @pytest.fixture(autouse=True)
    def _linear_config(self, make_config):
        self.config = make_config("linear")

    def _issue(self, **overrides):
        issue = {"identifier": "LIN-1", "title": "Lin issue",
                 "state": {"type": "unstarted"}}
        issue.update(overrides)
        return issue

    def test_issue_create_and_complete(self, project):
        result = _linear_apply_issue(1, self.config, "create", self._issue(
            description="正文", url="https://linear.app/i/LIN-1"))
        assert result["created"] is True
        task = Task.query.filter_by(creator_identifier="linear:LIN-1").one()
        assert task.status == TaskStatus.TODO
        assert "正文" in task.content and "来源: https://linear.app/i/LIN-1" in task.content

        _linear_apply_issue(1, self.config, "update",
                            self._issue(state={"type": "completed"}))
        assert task.status == TaskStatus.DONE

    def test_comment_paths(self, project):
        skipped = _linear_apply_comment(1, self.config, {"issue": {"identifier": "LIN-X"}})
        assert skipped["handled"] is False

        _linear_apply_issue(1, self.config, "create", self._issue())
        ok = _linear_apply_comment(1, self.config, {
            "issue": {"identifier": "LIN-1"},
            "user": {"name": "王五"}, "body": "收到",
        })
        log = TaskLog.query.filter_by(task_id=ok["task_id"]).one()
        assert log.content == "[Linear · 王五] 收到"

    def test_ingest_dispatch_and_guards(self, project):
        result = ingest_linear(1, {"type": "Issue", "action": "create",
                                   "data": self._issue()})
        assert result["handled"] is True and result["action"] == "create"

        commented = ingest_linear(1, {"type": "Comment", "action": "create",
                                      "data": {"issue": {"identifier": "LIN-1"},
                                               "user": {"name": "u"},
                                               "body": "ok"}})
        assert commented["handled"] is True

        ignored = ingest_linear(1, {"type": "Issue", "action": "remove"})
        assert ignored == {"handled": False, "reason": "Issue.remove ignored"}

        self.config.enabled = False
        db.session.commit()
        with pytest.raises(ValueError, match="linear connector not enabled"):
            ingest_linear(1, {"type": "Issue", "action": "create"})


class TestCommon:
    def test_resolve_project_missing(self, config):
        config.default_project_id = None
        db.session.commit()
        with pytest.raises(ValueError):
            resolve_project(config)

    def test_resolve_project_invalid_id(self, config):
        config.default_project_id = 999999
        db.session.commit()
        with pytest.raises(ValueError):
            resolve_project(config)

    def test_emit_sync_event_without_project(self, project):
        import uuid
        task = Task(
            project_id=project.id, owner_id=project.owner_id,
            title="orphan-source", status=TaskStatus.TODO,
            creator_type="ai", creator_identifier="test:orphan",
        )
        db.session.add(task)
        db.session.commit()
        # 孤儿任务形态：project 关系为空 → workspace_id 记 None
        # （用替身避免 ORM 关系赋值把 project_id 置 NULL）
        orphan = SimpleNamespace(id=task.id, project_id=task.project_id,
                                 project=None)
        emit_sync_event(orphan, "connector.test.event", {"k": 1})
        event = TaskEventOutbox.query.filter_by(
            event_type="connector.test.event").one()
        assert event.workspace_id is None
        assert event.payload == {"k": 1}


def test_facade_matches_package():
    import services.connectors as pkg
    import services.connectors.jira as jira_mod
    import services.connectors.linear as linear_mod
    assert pkg.ingest_jira is jira_mod.ingest_jira
    assert pkg.ingest_linear is linear_mod.ingest_linear
    assert pkg.LINEAR_STATE_MAP["completed"] == TaskStatus.DONE
