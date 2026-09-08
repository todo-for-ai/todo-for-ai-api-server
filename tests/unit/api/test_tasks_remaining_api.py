"""api/tasks 包剩余三文件单元回归：附件 / 批量操作 / Agent 聊天。

覆盖：附件（上传落盘与大小/扩展名/content-length 三重校验、下载
as_attachment、删除连文件、列表、404/403/500）、批量操作（状态/
优先级/负责人/删除四路批量、缺参与超上限 400、dependencies
GET/PUT）、Agent 聊天（agent 会话认证 401 矩阵、content 必填、
parent_id 跨任务校验、TaskLog 落库与房间推送）。
"""

import io
import os
import shutil
import uuid
from types import SimpleNamespace

import pytest
from flask_jwt_extended import create_access_token

from models import (
    Agent,
    AgentStatus,
    Attachment,
    Organization,
    Project,
    Task,
    TaskLog,
    User,
    db,
)

_TASK_ID_SEQ = iter(range(81_000_001, 81_100_000))


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
    # 清理上传落盘
    upload_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), 'uploads')
    shutil.rmtree(upload_dir, ignore_errors=True)
    ctx.pop()


@pytest.fixture
def client(_isolated_app):
    return _isolated_app.test_client()


def _uh(prefix="at"):
    u = User(username=f"{prefix}_{uuid.uuid4().hex[:8]}",
             email=f"{prefix}_{uuid.uuid4().hex[:6]}@t.io")
    db.session.add(u)
    db.session.flush()
    return u


def _headers_for(user):
    return {"Authorization": f"Bearer {create_access_token(identity=str(user.id))}"}


def _mk_project(owner):
    row = Project(name=f"p_{uuid.uuid4().hex[:6]}", owner_id=owner.id)
    db.session.add(row)
    db.session.flush()
    return row


def _mk_task(owner, project, status="TODO", **kw):
    row = Task(id=next(_TASK_ID_SEQ), title=f"t_{uuid.uuid4().hex[:6]}",
               content="c", status=status, priority="MEDIUM",
               project_id=project.id, owner_id=owner.id, **kw)
    db.session.add(row)
    db.session.flush()
    return row


def _mk_agent(workspace_id, owner, status=AgentStatus.ACTIVE):
    row = Agent(workspace_id=workspace_id, owner_id=owner.id,
                creator_user_id=owner.id,
                name=f"ag_{uuid.uuid4().hex[:6]}", status=status)
    db.session.add(row)
    db.session.flush()
    return row


@pytest.fixture
def env(_isolated_app):
    user = _uh("ow")
    org = Organization(name=f"o_{uuid.uuid4().hex[:6]}",
                       slug=f"o_{uuid.uuid4().hex[:6]}", owner_id=user.id)
    db.session.add(org)
    db.session.flush()
    project = _mk_project(user)
    task = _mk_task(user, project)
    agent = _mk_agent(org.id, user)
    db.session.commit()
    return {
        "app": _isolated_app, "user": user, "org": org,
        "project": project, "task": task, "agent": agent,
        "headers": _headers_for(user),
        "base": "/todo-for-ai/api/v1/tasks",
    }


# ─────────────────────────── 附件 ───────────────────────────


class TestAttachmentRoutes:
    def _upload(self, env, client, task_id=None, filename="notes.txt",
                content=b"hello", **kwargs):
        data = {"file": (io.BytesIO(content), filename)}
        return client.post(
            f"{env['base']}/{task_id or env['task'].id}/attachments",
            data=data, content_type="multipart/form-data",
            headers=env["headers"], **kwargs)

    def test_upload_success_and_file_on_disk(self, env, client):
        resp = self._upload(env, client, filename="report.md")
        assert resp.status_code == 201
        data = resp.get_json()["data"]
        assert data["original_filename"] == "report.md"
        # 响应刻意不含 file_path，从库里核对落盘位置
        row = db.session.get(Attachment, data["id"])
        assert os.path.exists(row.file_path)

    def test_upload_missing_file_400(self, env, client):
        resp = client.post(
            f"{env['base']}/{env['task'].id}/attachments",
            data={}, content_type="multipart/form-data",
            headers=env["headers"])
        assert resp.status_code == 400

    def test_upload_disallowed_extension(self, env, client):
        resp = self._upload(env, client, filename="evil.exe")
        assert resp.status_code == 400
        assert "not allowed" in resp.get_json()["message"]

    def test_upload_empty_filename(self, env, client):
        resp = self._upload(env, client, filename="   ")
        assert resp.status_code == 400

    def test_upload_oversize_content_length(self, env, client, monkeypatch):
        from api.tasks import routes_attachments as ra
        monkeypatch.setattr(ra, "MAX_ATTACHMENT_SIZE_BYTES", 5)
        resp = self._upload(env, client, content=b"0123456789abc")
        assert resp.status_code == 400
        assert "File too large" in resp.get_json()["message"]

    def test_list_attachments(self, env, client):
        self._upload(env, client, filename="a.txt")
        self._upload(env, client, filename="b.txt")
        resp = client.get(f"{env['base']}/{env['task'].id}/attachments",
                          headers=env["headers"])
        assert resp.status_code == 200
        assert len(resp.get_json()["data"]) == 2

    def test_download_roundtrip(self, env, client):
        self._upload(env, client, filename="dl.txt", content=b"payload-1")
        listing = client.get(f"{env['base']}/{env['task'].id}/attachments",
                             headers=env["headers"]).get_json()["data"]
        att_id = listing[0]["id"]
        resp = client.get(
            f"{env['base']}/{env['task'].id}/attachments/{att_id}/download",
            headers=env["headers"])
        assert resp.status_code == 200
        assert resp.data == b"payload-1"

    def test_download_missing_file_404(self, env, client):
        row = Attachment.create_attachment(
            task_id=env["task"].id, filename="ghost.txt",
            original_filename="ghost.txt",
            file_path="/nonexistent/ghost.txt", file_size=1,
            mime_type="text/plain", uploaded_by=env["user"].email)
        db.session.add(row)
        db.session.commit()
        resp = client.get(
            f"{env['base']}/{env['task'].id}/attachments/{row.id}/download",
            headers=env["headers"])
        assert resp.status_code == 404
        assert "file not found" in resp.get_json()["message"]

    def test_delete_attachment_removes_record_and_file(self, env, client):
        self._upload(env, client, filename="del.txt")
        listing = client.get(f"{env['base']}/{env['task'].id}/attachments",
                             headers=env["headers"]).get_json()["data"]
        att_id = listing[0]["id"]
        file_path = db.session.get(Attachment, att_id).file_path
        assert os.path.exists(file_path)
        resp = client.delete(
            f"{env['base']}/{env['task'].id}/attachments/{att_id}",
            headers=env["headers"])
        assert resp.status_code == 200
        assert not os.path.exists(file_path)
        assert db.session.get(Attachment, att_id) is None

    def test_foreign_project_attachments_403(self, env, client):
        other = _uh("ot")
        foreign_project = _mk_project(other)
        foreign_task = _mk_task(other, foreign_project)
        db.session.commit()
        base = f"{env['base']}/{foreign_task.id}/attachments"
        assert client.get(base, headers=env["headers"]).status_code == 403
        assert client.post(base, data={"file": (io.BytesIO(b"x"), "x.txt")},
                           content_type="multipart/form-data",
                           headers=env["headers"]).status_code == 403
        assert client.delete(f"{base}/1",
                             headers=env["headers"]).status_code == 403
        assert client.get(f"{base}/1/download",
                          headers=env["headers"]).status_code == 403

    def test_task_missing_404(self, env, client):
        base = f"{env['base']}/999999/attachments"
        assert client.get(base, headers=env["headers"]).status_code == 404
        assert client.post(base, data={"file": (io.BytesIO(b"x"), "x.txt")},
                           content_type="multipart/form-data",
                           headers=env["headers"]).status_code == 404
        assert client.delete(f"{base}/1",
                             headers=env["headers"]).status_code == 404
        assert client.get(f"{base}/1/download",
                          headers=env["headers"]).status_code == 404

    def test_delete_and_download_missing_attachment_404(self, env, client):
        base = f"{env['base']}/{env['task'].id}/attachments/999999"
        assert client.delete(base, headers=env["headers"]).status_code == 404
        assert client.get(f"{base}/download",
                          headers=env["headers"]).status_code == 404

    def test_upload_create_attachment_failure_500(self, env, client,
                                                  monkeypatch):
        from api.tasks import routes_attachments as ra
        monkeypatch.setattr(ra.Attachment, "create_attachment",
                            classmethod(lambda cls, **kw: 1 / 0))
        resp = self._upload(env, client)
        assert resp.status_code == 500


    def test_list_500(self, env, client, monkeypatch):
        self._upload(env, client)
        monkeypatch.setattr(Attachment, "to_dict",
                            lambda self, *a, **kw: 1 / 0)
        assert client.get(f"{env['base']}/{env['task'].id}/attachments",
                          headers=env["headers"]).status_code == 500

    def test_delete_500(self, env, client, monkeypatch):
        self._upload(env, client)
        listing = client.get(f"{env['base']}/{env['task'].id}/attachments",
                             headers=env["headers"]).get_json()["data"]
        monkeypatch.setattr(Attachment, "delete_file",
                            lambda self: 1 / 0)
        resp = client.delete(
            f"{env['base']}/{env['task'].id}/attachments/{listing[0]['id']}",
            headers=env["headers"])
        assert resp.status_code == 500

    def test_download_500(self, env, client, monkeypatch):
        from api.tasks import routes_attachments as ra

        class _BoomQuery:
            def filter_by(self, **kw):
                raise RuntimeError("db down")

        monkeypatch.setattr(ra.Attachment, "query", _BoomQuery())
        assert client.get(
            f"{env['base']}/{env['task'].id}/attachments/1/download",
            headers=env["headers"]).status_code == 500

    def test_upload_oversize_after_save(self, env, client, monkeypatch):
        """content_length 预检通过但落盘后超限：清理文件并 400。"""
        from api.tasks import routes_attachments as ra
        # MAX 保持默认让 content_length 预检放行，落盘后 getsize 超限
        monkeypatch.setattr(ra.os.path, "getsize",
                            lambda p: 99_999_999)
        resp = self._upload(env, client, filename="big.txt",
                            content=b"tiny")
        assert resp.status_code == 400
        assert "File too large" in resp.get_json()["message"]



# ─────────────────────────── 批量操作与依赖 ───────────────────────────


class TestBatchRoutes:
    def _post(self, env, client, tail, payload):
        return client.post(f"{env['base']}/batch/{tail}",
                           headers=env["headers"], json=payload)

    def test_batch_update_status(self, env, client):
        t1 = _mk_task(env["user"], env["project"])
        t2 = _mk_task(env["user"], env["project"])
        db.session.commit()
        assert self._post(env, client, "update-status",
                          {"task_ids": []}).status_code == 400
        assert self._post(env, client, "update-status",
                          {"task_ids": [t1.id]}).status_code == 400
        resp = self._post(env, client, "update-status",
                          {"task_ids": [t1.id, t2.id, 999999],
                           "status": "DONE"})
        assert resp.status_code == 200
        assert resp.get_json()["data"]["updated"] == 2  # 不存在的忽略
        db.session.expire_all()
        assert db.session.get(Task, t1.id).status.value == "done"

    def test_batch_update_priority(self, env, client):
        t1 = _mk_task(env["user"], env["project"])
        db.session.commit()
        assert self._post(env, client, "update-priority",
                          {"task_ids": [t1.id]}).status_code == 400
        resp = self._post(env, client, "update-priority",
                          {"task_ids": [t1.id], "priority": "HIGH"})
        assert resp.get_json()["data"]["updated"] == 1
        db.session.expire_all()
        assert db.session.get(Task, t1.id).priority.value == "high"

    def test_batch_assign(self, env, client):
        t1 = _mk_task(env["user"], env["project"])
        db.session.commit()
        assert self._post(env, client, "assign",
                          {"task_ids": [t1.id]}).status_code == 400
        assignees = [{"type": "user", "id": env["user"].id,
                      "name": "me"}]
        resp = self._post(env, client, "assign",
                          {"task_ids": [t1.id], "assignees": assignees})
        assert resp.get_json()["data"]["updated"] == 1
        db.session.expire_all()
        assert db.session.get(Task, t1.id).assignees == assignees

    def test_batch_delete(self, env, client):
        t1 = _mk_task(env["user"], env["project"])
        t2 = _mk_task(env["user"], env["project"])
        db.session.commit()
        id1, id2 = t1.id, t2.id  # 请求后原实例已被删除并过期
        assert self._post(env, client, "delete",
                          {"task_ids": []}).status_code == 400
        resp = self._post(env, client, "delete",
                          {"task_ids": [id1, id2]})
        assert resp.get_json()["data"]["deleted"] == 2
        # 批量硬删后用查询确认（身份映射中残留过期实例，get 会报错）
        assert db.session.query(Task).filter_by(id=id1).first() is None

    def test_batch_size_limit(self, env, client, monkeypatch):
        from api.tasks import routes_batch as rb
        monkeypatch.setattr(rb, "MAX_BATCH_SIZE", 2)
        ids = [1, 2, 3]
        for tail, payload in (
            ("update-status", {"task_ids": ids, "status": "DONE"}),
            ("update-priority", {"task_ids": ids, "priority": "HIGH"}),
            ("delete", {"task_ids": ids}),
            ("assign", {"task_ids": ids, "assignees": []}),
        ):
            resp = self._post(env, client, tail, payload)
            assert resp.status_code == 400, tail
            assert "Max 2" in resp.get_json()["message"]

    def test_dependencies_flow(self, env, client):
        task = _mk_task(env["user"], env["project"])
        url = f"{env['base']}/{task.id}/dependencies"
        resp = client.get(url, headers=env["headers"])
        assert resp.get_json()["data"] == {"blocking": [], "blocked_by": []}

        resp = client.put(url, headers=env["headers"], json={
            "blocking_task_ids": [11, 12],
            "blocked_by_task_ids": [13]})
        assert resp.status_code == 200
        resp = client.get(url, headers=env["headers"])
        data = resp.get_json()["data"]
        assert data == {"blocking": [11, 12], "blocked_by": [13]}

        assert client.get(f"{env['base']}/999999/dependencies",
                          headers=env["headers"]).status_code == 404
        assert client.put(f"{env['base']}/999999/dependencies",
                          headers=env["headers"],
                          json={}).status_code == 404


# ─────────────────────────── Agent 聊天 ───────────────────────────


class TestAgentChat:
    def _chat(self, env, client, payload, header_token="valid-session",
              agent_status=AgentStatus.ACTIVE):
        from api import agent_common as ac
        fake_session = SimpleNamespace(agent_id=env["agent"].id)

        def fake_verify(cls, token):
            # 只有固定令牌有效，其余一律拒绝
            if token != "valid-session":
                return None
            return fake_session

        monkeypatch = pytest.MonkeyPatch()
        try:
            monkeypatch.setattr(
                ac.AgentSession, "verify_session_token",
                classmethod(fake_verify))
            env["agent"].status = agent_status
            db.session.commit()
            headers = {"Authorization": f"Bearer {header_token}"}
            return client.post(
                f"{env['base']}/agent/{env['task'].id}/chat",
                json=payload, headers=headers)
        finally:
            monkeypatch.undo()

    def test_auth_matrix(self, env, client):
        # 缺失头
        resp = client.post(f"{env['base']}/agent/{env['task'].id}/chat",
                           json={"content": "hi"})
        assert resp.status_code == 401
        # 无效 token
        resp = self._chat(env, client, {"content": "hi"},
                          header_token="wrong-token")
        assert resp.status_code == 401
        # 非活跃 agent
        resp = self._chat(env, client, {"content": "hi"},
                          agent_status=AgentStatus.PAUSED)
        assert resp.status_code == 401

    def test_content_required(self, env, client):
        resp = self._chat(env, client, {"parent_id": 1})
        assert resp.status_code == 400

    def test_send_and_reply(self, env, client, monkeypatch):
        import api.user_websocket as uws_mod
        pushed = []
        monkeypatch.setattr(uws_mod, "push_to_task_room",
                            lambda tid, event, payload:
                            pushed.append((tid, event, payload)))

        resp = self._chat(env, client, {"content": "progress update"})
        assert resp.status_code == 201
        data = resp.get_json()["data"]
        assert data["content"] == "progress update"
        assert pushed[0][1] == "task_comment"

        # 回复：parent 在同一任务
        parent_id = data["id"]
        resp = self._chat(env, client, {"content": "reply",
                                        "parent_id": parent_id})
        assert resp.status_code == 201
        assert resp.get_json()["data"]["parent_id"] == parent_id

        # parent 跨任务 → 404
        other_task = _mk_task(env["user"], env["project"])
        db.session.commit()
        resp = self._chat(env, client, {"content": "x",
                                        "parent_id": other_task.id})
        assert resp.status_code == 404

        db.session.expire_all()
        logs = TaskLog.query.filter_by(task_id=env["task"].id).all()
        assert len(logs) == 2
        assert all(log.actor_type.value == "agent" for log in logs)
