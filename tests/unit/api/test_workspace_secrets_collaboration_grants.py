"""机密协作拓扑与授权链 API（routes_collaboration.py + routes_grants.py）单元回归。

覆盖：协作拓扑（出入边聚合/协作者统计/project 过滤/include_inactive/
统计数字）、共享创建（单/批目标、target_selector 三模式、更新 vs 新建、
自共享/跨工作区/停用目标拒绝、过期与访问模式校验）、共享吊销（连带
吊销 grant）；授权链（创建默认值/校验矩阵/同 agent 拒绝/task 归属校验/
attempt 长度/列表过滤/吊销 409）。
"""

import uuid
from datetime import datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from flask_jwt_extended import create_access_token

from models import (
    Agent,
    AgentSecret,
    AgentSecretGrant,
    AgentSecretShare,
    AgentStatus,
    Organization,
    Project,
    Task,
    User,
    db,
)


@pytest.fixture(scope="function", autouse=True)
def _isolated_app(monkeypatch):
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
def user(_isolated_app):
    def _make():
        u = User(username=f"ug_{uuid.uuid4().hex[:8]}",
                 email=f"ug_{uuid.uuid4().hex[:6]}@t.io")
        db.session.add(u)
        db.session.commit()
        return u
    return _make


@pytest.fixture
def client(_isolated_app):
    return _isolated_app.test_client()


@pytest.fixture
def env(_isolated_app):
    u = User(username=f"cg_{uuid.uuid4().hex[:8]}",
             email=f"cg_{uuid.uuid4().hex[:6]}@t.io")
    db.session.add(u)
    db.session.flush()
    org = Organization(name=f"o_{uuid.uuid4().hex[:6]}",
                       slug=f"o_{uuid.uuid4().hex[:6]}", owner_id=u.id)
    db.session.add(org)
    db.session.commit()

    owner = Agent(workspace_id=org.id, owner_id=u.id, creator_user_id=u.id,
                  name="owner", status=AgentStatus.ACTIVE)
    target = Agent(workspace_id=org.id, owner_id=u.id, creator_user_id=u.id,
                   name="target", status=AgentStatus.ACTIVE)
    db.session.add_all([owner, target])
    db.session.commit()

    token = create_access_token(identity=str(u.id))
    return {
        "user": u, "org": org, "owner": owner, "target": target,
        "headers": {"Authorization": f"Bearer {token}"},
        "base": f"/todo-for-ai/api/v1/workspaces/{org.id}/agents/{owner.id}",
    }


def _secret(agent, name="s", project_id=None, is_active=True):
    row = AgentSecret.from_plaintext(
        agent_id=agent.id, workspace_id=agent.workspace_id, name=name,
        secret_value="v", user_id=1, created_by="test", project_id=project_id)
    row.is_active = is_active
    db.session.add(row)
    db.session.commit()
    return row


def _share(secret, owner, target, is_active=True, expires_at=None):
    row = AgentSecretShare(
        secret_id=secret.id, workspace_id=secret.workspace_id,
        owner_agent_id=owner.id, target_agent_id=target.id,
        access_mode="read", expires_at=expires_at, is_active=is_active,
        granted_by_user_id=1)
    db.session.add(row)
    db.session.commit()
    return row


def _grant(secret, owner, target, grant_id=None, status="active",
           max_uses=None, used_count=0, expires_at=None):
    row = AgentSecretGrant(
        grant_id=grant_id or f"g-{uuid.uuid4().hex[:10]}",
        secret_id=secret.id, workspace_id=secret.workspace_id,
        from_agent_id=owner.id, to_agent_id=target.id,
        grant_mode="leased", max_uses=max_uses, used_count=used_count,
        expires_at=expires_at, status=status,
        granted_by_user_id=1, created_by="test")
    db.session.add(row)
    db.session.commit()
    return row


class TestCollaborationTopology:
    def test_empty_topology(self, client, env):
        resp = client.get(env["base"] + "/secrets/collaboration",
                          headers=env["headers"])
        assert resp.status_code == 200
        body = resp.get_json()["data"]
        assert body["stats"]["edge_count"] == 0
        assert body["edges"] == []
        assert body["outgoing_collaborators"] == []

    def test_outgoing_and_incoming_aggregation(self, client, env):
        third = Agent(workspace_id=env["org"].id, owner_id=env["user"].id,
                      creator_user_id=env["user"].id, name="third",
                      status=AgentStatus.ACTIVE)
        db.session.add(third)
        db.session.commit()

        owned = _secret(env["owner"], "mine")
        _share(owned, env["owner"], env["target"])       # outgoing → target
        _share(owned, env["owner"], third)               # outgoing → third
        foreign = _secret(third, "theirs")
        _share(foreign, third, env["owner"])             # incoming ← third

        resp = client.get(env["base"] + "/secrets/collaboration",
                          headers=env["headers"])
        body = resp.get_json()["data"]
        assert body["stats"]["outgoing_share_count"] == 2
        assert body["stats"]["incoming_share_count"] == 1
        assert body["stats"]["edge_count"] == 3
        assert body["stats"]["active_edge_count"] == 3
        out_names = {c["agent_name"] for c in body["outgoing_collaborators"]}
        assert out_names == {"target", "third"}
        assert body["incoming_collaborators"][0]["agent_name"] == "third"
        directions = {e["direction"] for e in body["edges"]}
        assert directions == {"outgoing", "incoming"}

    def test_default_hides_inactive_and_expired(self, client, env):
        secret = _secret(env["owner"])
        _share(secret, env["owner"], env["target"], is_active=False)
        _share(secret, env["owner"], env["target"],
               expires_at=datetime.utcnow() - timedelta(hours=1))
        resp = client.get(env["base"] + "/secrets/collaboration",
                          headers=env["headers"])
        body = resp.get_json()["data"]
        assert body["stats"]["edge_count"] == 0

        resp = client.get(
            f"{env['base']}/secrets/collaboration?include_inactive=true",
            headers=env["headers"])
        body = resp.get_json()["data"]
        assert body["stats"]["edge_count"] == 2
        assert body["stats"]["active_edge_count"] == 0

    def test_project_filter_and_invalid_project(self, client, env):
        project = Project(name=f"p_{uuid.uuid4().hex[:6]}",
                          owner_id=env["user"].id,
                          organization_id=env["org"].id)
        db.session.add(project)
        db.session.commit()
        mine = _secret(env["owner"], "with-proj", project_id=project.id)
        _share(mine, env["owner"], env["target"])
        other = _secret(env["owner"], "no-proj")
        _share(other, env["owner"], env["target"])

        base = f"{env['base']}/secrets/collaboration"
        resp = client.get(f"{base}?project_id={project.id}",
                          headers=env["headers"])
        body = resp.get_json()["data"]
        assert body["stats"]["edge_count"] == 1
        assert body["edges"][0]["secret_name"] == "with-proj"
        assert body["stats"]["project_id"] == project.id

        resp = client.get(f"{base}?project_id=abc", headers=env["headers"])
        assert resp.status_code == 400
        resp = client.get(f"{base}?project_id=999999", headers=env["headers"])
        assert resp.status_code == 400

    def test_agent_404_and_access_403(self, client, env, user):
        resp = client.get(
            env["base"].replace(f"/agents/{env['owner'].id}", "/agents/999999")
            + "/secrets/collaboration", headers=env["headers"])
        assert resp.status_code == 404
        outsider = user()
        token = create_access_token(identity=str(outsider.id))
        resp = client.get(env["base"] + "/secrets/collaboration",
                          headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 403


class TestCreateShares:
    def _share_url(self, env, secret):
        return f"{env['base']}/secrets/{secret.id}/shares"

    def test_create_single_target(self, client, env):
        secret = _secret(env["owner"])
        resp = client.post(self._share_url(env, secret),
                           headers=env["headers"],
                           json={"target_agent_id": env["target"].id})
        assert resp.status_code == 200
        summary = resp.get_json()["data"]["summary"]
        assert summary == {"created": 1, "updated": 0, "total": 1,
                           "target_selector": "manual",
                           "selector_project_id": None,
                           "resolved_target_count": 1}

    def test_update_existing_share(self, client, env):
        secret = _secret(env["owner"])
        _share(secret, env["owner"], env["target"])
        resp = client.post(self._share_url(env, secret),
                           headers=env["headers"],
                           json={"target_agent_id": env["target"].id,
                                 "access_mode": "read"})
        assert resp.status_code == 200
        assert resp.get_json()["data"]["summary"]["updated"] == 1

    def test_share_to_self_400(self, client, env):
        secret = _secret(env["owner"])
        resp = client.post(self._share_url(env, secret),
                           headers=env["headers"],
                           json={"target_agent_id": env["owner"].id})
        assert resp.status_code == 400

    def test_target_not_in_workspace_404(self, client, env, user):
        secret = _secret(env["owner"])
        other = Agent(workspace_id=env["org"].id + 999,
                      owner_id=env["user"].id, creator_user_id=user().id,
                      name="ghost", status=AgentStatus.ACTIVE)
        db.session.add(other)
        db.session.commit()
        resp = client.post(self._share_url(env, secret),
                           headers=env["headers"],
                           json={"target_agent_id": other.id})
        assert resp.status_code == 404

    def test_inactive_target_400(self, client, env):
        secret = _secret(env["owner"])
        env["target"].status = AgentStatus.DISABLED
        db.session.commit()
        resp = client.post(self._share_url(env, secret),
                           headers=env["headers"],
                           json={"target_agent_id": env["target"].id})
        assert resp.status_code == 400

    def test_revoked_secret_400(self, client, env):
        secret = _secret(env["owner"], is_active=False)
        resp = client.post(self._share_url(env, secret),
                           headers=env["headers"],
                           json={"target_agent_id": env["target"].id})
        assert resp.status_code == 400

    @pytest.mark.parametrize("payload", [
        {"target_agent_id": "abc"},
        {"target_agent_ids": "nope"},
        {"target_agent_ids": [1, "x"]},
        {"target_selector": "galactic"},
        {"access_mode": "write"},
        {"expires_at": "not-a-date"},
        {"expires_at": "2020-01-01T00:00:00"},
    ])
    def test_validation_errors(self, client, env, payload):
        secret = _secret(env["owner"])
        payload = {"model": None, **payload}
        payload.pop("model", None)
        body = {"target_agent_id": env["target"].id, **payload}
        resp = client.post(self._share_url(env, secret),
                           headers=env["headers"], json=body)
        assert resp.status_code == 400

    def test_workspace_active_selector(self, client, env):
        third = Agent(workspace_id=env["org"].id, owner_id=env["user"].id,
                      creator_user_id=env["user"].id, name="third",
                      status=AgentStatus.ACTIVE)
        disabled = Agent(workspace_id=env["org"].id, owner_id=env["user"].id,
                         creator_user_id=env["user"].id, name="disabled",
                         status=AgentStatus.DISABLED)
        db.session.add_all([third, disabled])
        db.session.commit()
        secret = _secret(env["owner"])

        resp = client.post(self._share_url(env, secret),
                           headers=env["headers"],
                           json={"target_selector": "workspace_active"})
        assert resp.status_code == 200
        summary = resp.get_json()["data"]["summary"]
        assert summary["resolved_target_count"] == 2  # 除自己外全部 active
        assert summary["created"] == 2

    def test_project_agents_selector_uses_secret_project(self, client, env):
        project = Project(name=f"p_{uuid.uuid4().hex[:6]}",
                          owner_id=env["user"].id,
                          organization_id=env["org"].id)
        db.session.add(project)
        db.session.commit()
        secret = _secret(env["owner"], project_id=project.id)
        env["target"].allowed_project_ids = [project.id]
        db.session.commit()

        resp = client.post(self._share_url(env, secret),
                           headers=env["headers"],
                           json={"target_selector": "project_agents"})
        assert resp.status_code == 200
        assert resp.get_json()["data"]["summary"]["created"] == 1

    def test_no_targets_resolved_400(self, client, env):
        # project_agents 选择器：没有任何 agent 的 allowed_project_ids 命中
        project = Project(name=f"p_{uuid.uuid4().hex[:6]}",
                          owner_id=env["user"].id,
                          organization_id=env["org"].id)
        db.session.add(project)
        db.session.commit()
        secret = _secret(env["owner"])
        resp = client.post(self._share_url(env, secret),
                           headers=env["headers"],
                           json={"target_selector": "project_agents",
                                 "selector_project_id": project.id})
        assert resp.status_code == 400
        assert "No target agents resolved" in resp.get_json()["message"]

    def test_project_agents_selector_requires_project(self, client, env):
        """secret 无 project 且未提供 selector_project_id → 400。"""
        secret = _secret(env["owner"])
        resp = client.post(self._share_url(env, secret),
                           headers=env["headers"],
                           json={"target_selector": "project_agents"})
        assert resp.status_code == 400
        assert "selector_project_id is required" in resp.get_json()["message"]

    def test_project_agents_selector_bad_project_400(self, client, env):
        secret = _secret(env["owner"])
        resp = client.post(self._share_url(env, secret),
                           headers=env["headers"],
                           json={"target_selector": "project_agents",
                                 "selector_project_id": 999999})
        assert resp.status_code == 400
        assert "does not belong" in resp.get_json()["message"]

    def test_selector_error_response_passthrough(self, client, env,
                                                 monkeypatch):
        """resolve 选择器返回错误响应 → 原样透传（防御未来分支）。"""
        from api.base import ApiResponse
        monkeypatch.setattr(
            "api.agent_workspace_secrets.routes_collaboration."
            "resolve_target_agent_ids_by_selector",
            lambda **kw: (None, ApiResponse.error(
                "selector down", 400).to_response()))
        secret = _secret(env["owner"])
        resp = client.post(self._share_url(env, secret),
                           headers=env["headers"],
                           json={"target_selector": "workspace_active"})
        assert resp.status_code == 400
        assert "selector down" in resp.get_json()["message"]

    def test_project_filter_skips_mismatched(self, client, env):
        """project 过滤：project_id 不匹配 → 边跳过。"""
        project = Project(name=f"p_{uuid.uuid4().hex[:6]}",
                          owner_id=env["user"].id,
                          organization_id=env["org"].id)
        db.session.add(project)
        db.session.commit()
        other = _secret(env["owner"], "other-proj")  # project_id=None → 不匹配
        _share(other, env["owner"], env["target"])
        matched = _secret(env["owner"], "matched", project_id=project.id)
        _share(matched, env["owner"], env["target"])

        resp = client.get(
            f"{env['base']}/secrets/collaboration?project_id={project.id}",
            headers=env["headers"])
        body = resp.get_json()["data"]
        assert body["stats"]["edge_count"] == 1
        assert body["edges"][0]["secret_name"] == "matched"

    def test_include_inactive_keeps_revoked_secret_edges(self, client, env):
        """include_inactive：share 保留且已吊销 secret 的边也保留。"""
        revoked = _secret(env["owner"], "revoked", is_active=False)
        _share(revoked, env["owner"], env["target"])
        live = _secret(env["owner"], "live")
        _share(live, env["owner"], env["target"])

        resp = client.get(
            f"{env['base']}/secrets/collaboration?include_inactive=true",
            headers=env["headers"])
        body = resp.get_json()["data"]
        assert body["stats"]["edge_count"] == 2
        names = {e["secret_name"] for e in body["edges"]}
        assert names == {"revoked", "live"}

    def test_include_inactive_with_project_filter_and_dangling(self, client, env):
        """include_inactive=true 时 134/147 的活跃过滤关闭：仅 project 不匹配
        与悬空 secret 分支生效。"""
        project = Project(name=f"p_{uuid.uuid4().hex[:6]}",
                          owner_id=env["user"].id,
                          organization_id=env["org"].id)
        db.session.add(project)
        db.session.commit()
        mine = _secret(env["owner"], "mine", project_id=project.id)
        share_mine = _share(mine, env["owner"], env["target"])
        other = _secret(env["owner"], "other")
        share_other = _share(other, env["owner"], env["target"])
        # 悬空：incoming 方向的 secret 被删
        foreign_secret = _secret(other, "foreign")
        share_foreign = _share(foreign_secret, other, env["owner"])
        db.session.delete(foreign_secret)
        db.session.commit()

        resp = client.get(
            f"{env['base']}/secrets/collaboration?include_inactive=true"
            f"&project_id={project.id}", headers=env["headers"])
        body = resp.get_json()["data"]
        assert body["stats"]["edge_count"] == 1
        assert body["edges"][0]["secret_name"] == "mine"
        # incoming 的悬空 share 跳过（131/144-148 生效）
        incoming = body["incoming_collaborators"]
        assert incoming == []
        assert share_mine and share_other and share_foreign

    def test_dangling_secret_share_skipped(self, client, env):
        """悬空 secret_id 的 share（sqlite 不强外键）→ 出/入两边都跳过。"""
        ghost_out = AgentSecretShare(
            secret_id=9999991, workspace_id=env["org"].id,
            owner_agent_id=env["owner"].id, target_agent_id=env["target"].id,
            granted_by_user_id=1)
        ghost_in = AgentSecretShare(
            secret_id=9999992, workspace_id=env["org"].id,
            owner_agent_id=env["target"].id, target_agent_id=env["owner"].id,
            granted_by_user_id=1)
        db.session.add_all([ghost_out, ghost_in])
        db.session.commit()

        resp = client.get(env["base"] + "/secrets/collaboration",
                          headers=env["headers"])
        body = resp.get_json()["data"]
        assert body["stats"]["edge_count"] == 0
        assert body["incoming_collaborators"] == []

    def test_incoming_inactive_skipped_by_default(self, client, env):
        """默认（不含 inactive）：incoming 的非激活 secret 边跳过。"""
        inactive = _secret(env["target"], "inactive-sec", is_active=False)
        _share(inactive, env["target"], env["owner"])
        resp = client.get(env["base"] + "/secrets/collaboration",
                          headers=env["headers"])
        body = resp.get_json()["data"]
        assert body["stats"]["incoming_share_count"] == 0
        assert body["stats"]["edge_count"] == 0

    def test_incoming_project_and_inactive_skips(self, client, env):
        """include_inactive 下 incoming 边：project 不匹配 / secret 非激活跳过。"""
        project = Project(name=f"p_{uuid.uuid4().hex[:6]}",
                          owner_id=env["user"].id,
                          organization_id=env["org"].id)
        db.session.add(project)
        db.session.commit()
        matched = _secret(env["owner"], "matched", project_id=project.id)
        _share(matched, env["target"], env["owner"])  # incoming
        mismatched = _secret(env["target"], "mismatched")
        _share(mismatched, env["target"], env["owner"])  # project None → skip
        inactive = _secret(env["target"], "inactive-sec", is_active=False)
        _share(inactive, env["target"], env["owner"])  # 非激活 → skip

        resp = client.get(
            f"{env['base']}/secrets/collaboration?include_inactive=true"
            f"&project_id={project.id}", headers=env["headers"])
        edges = resp.get_json()["data"]["edges"]
        assert [e["secret_name"] for e in edges] == ["matched"]

    def test_project_filter_and_inactive_skips(self, client, env):
        """非 include_inactive 下：project 不匹配/secret 非激活的边跳过。"""
        project = Project(name=f"p_{uuid.uuid4().hex[:6]}",
                          owner_id=env["user"].id,
                          organization_id=env["org"].id)
        db.session.add(project)
        db.session.commit()
        mine = _secret(env["owner"], "mine", project_id=project.id)
        _share(mine, env["owner"], env["target"])
        inactive = _secret(env["owner"], "inactive", is_active=False)
        _share(inactive, env["owner"], env["target"])

        resp = client.get(env["base"] + "/secrets/collaboration",
                          headers=env["headers"])
        edges = resp.get_json()["data"]["edges"]
        assert [e["secret_name"] for e in edges] == ["mine"]

    def test_shares_non_json_400(self, client, env):
        p = _secret(env["owner"])
        resp = client.post(self._share_url(env, p), headers=env["headers"],
                           data="plain", content_type="text/plain")
        assert resp.status_code == 400

    def test_shares_manage_403(self, client, env, user):
        from flask_jwt_extended import create_access_token
        outsider = user()
        token = create_access_token(identity=str(outsider.id))
        secret = _secret(env["owner"])
        resp = client.post(self._share_url(env, secret),
                           headers={"Authorization": f"Bearer {token}"},
                           json={"target_agent_id": env["target"].id})
        assert resp.status_code == 403



    def test_guard_404_403(self, client, env, user):
        outsider = user()
        token = create_access_token(identity=str(outsider.id))
        headers = {"Authorization": f"Bearer {token}"}
        secret = _secret(env["owner"])

        resp = client.post(
            env["base"].replace(f"/agents/{env['owner'].id}", "/agents/999999")
            + f"/secrets/{secret.id}/shares", headers=env["headers"], json={})
        assert resp.status_code == 404
        resp = client.post(self._share_url(env, secret), headers=headers,
                           json={})
        assert resp.status_code == 403
        resp = client.post(
            env["base"] + "/secrets/999999/shares", headers=env["headers"],
            json={})
        assert resp.status_code == 404


class TestRevokeShare:
    def test_revoke_manage_403(self, client, env, user):
        from api.base import ApiResponse  # noqa: F401
        secret = _secret(env["owner"])
        share = _share(secret, env["owner"], env["target"])
        outsider = user()
        token = create_access_token(identity=str(outsider.id))
        url = f"{env['base']}/secrets/{secret.id}/shares/{share.id}/revoke"
        resp = client.post(url,
                           headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 403

    def test_revoke_success_cascades_grants(self, client, env):
        secret = _secret(env["owner"])
        share = _share(secret, env["owner"], env["target"])
        grant = _grant(secret, env["owner"], env["target"])
        url = (f"{env['base']}/secrets/{secret.id}/shares/{share.id}/revoke")
        resp = client.post(url, headers=env["headers"])
        assert resp.status_code == 200
        assert share.is_active is False
        db.session.expire_all()
        assert grant.status == "revoked"

    def test_revoke_404s(self, client, env):
        secret = _secret(env["owner"])
        base = f"{env['base']}/secrets/{secret.id}/shares"
        assert client.post(f"{base}/999999/revoke",
                           headers=env["headers"]).status_code == 404
        assert client.post(
            env["base"].replace(f"/agents/{env['owner'].id}", "/agents/999999")
            + f"/secrets/{secret.id}/shares/1/revoke",
            headers=env["headers"]).status_code == 404
        assert client.post(
            env["base"] + "/secrets/999999/shares/1/revoke",
            headers=env["headers"]).status_code == 404

    def test_already_revoked_409(self, client, env):
        secret = _secret(env["owner"])
        share = _share(secret, env["owner"], env["target"], is_active=False)
        url = f"{env['base']}/secrets/{secret.id}/shares/{share.id}/revoke"
        resp = client.post(url, headers=env["headers"])
        assert resp.status_code == 409


class TestGrantModeDefaults:
    """leased/persistent 两种模式的默认过期与默认次数。"""

    @pytest.mark.parametrize("mode,max_uses,days", [
        ("leased", 100, 1),
        ("persistent", None, 30),
    ])
    def test_mode_defaults(self, client, env, mode, max_uses, days):
        secret = _secret(env["owner"])
        resp = client.post(
            f"{env['base']}/secrets/{secret.id}/grants",
            headers=env["headers"],
            json={"target_agent_id": env["target"].id, "grant_mode": mode})
        assert resp.status_code == 201
        data = resp.get_json()["data"]
        assert data["grant_mode"] == mode
        assert data["max_uses"] == max_uses

    def test_target_agent_id_invalid_400(self, client, env):
        secret = _secret(env["owner"])
        resp = client.post(
            f"{env['base']}/secrets/{secret.id}/grants",
            headers=env["headers"],
            json={"target_agent_id": "abc"})
        assert resp.status_code == 400

    def test_task_id_invalid_400(self, client, env):
        secret = _secret(env["owner"])
        resp = client.post(
            f"{env['base']}/secrets/{secret.id}/grants",
            headers=env["headers"],
            json={"target_agent_id": env["target"].id, "task_id": "abc"})
        assert resp.status_code == 400

    def test_list_agent_404_manage_403_secret_404(self, client, env, user):
        secret = _secret(env["owner"])
        base = f"{env['base']}/secrets/{secret.id}/grants"
        resp = client.get(
            env["base"].replace(f"/agents/{env['owner'].id}", "/agents/999999")
            + f"/secrets/{secret.id}/grants", headers=env["headers"])
        assert resp.status_code == 404

        outsider = user()
        token = create_access_token(identity=str(outsider.id))
        resp = client.get(base,
                          headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 403
        resp = client.get(f"{env['base']}/secrets/999999/grants",
                          headers=env["headers"])
        assert resp.status_code == 404


class TestGrantsList:
    def test_empty_and_filters(self, client, env):
        secret = _secret(env["owner"])
        base = f"{env['base']}/secrets/{secret.id}/grants"
        resp = client.get(base, headers=env["headers"])
        assert resp.get_json()["data"]["items"] == []

        active = _grant(secret, env["owner"], env["target"],
                        grant_id="g-active")
        _grant(secret, env["owner"], env["target"], grant_id="g-revoked",
               status="revoked")
        _grant(secret, env["owner"], env["target"], grant_id="g-expired",
               expires_at=datetime.utcnow() - timedelta(days=1))

        resp = client.get(base, headers=env["headers"])
        ids = {g["grant_id"] for g in resp.get_json()["data"]["items"]}
        assert ids == {"g-active"}

        resp = client.get(f"{base}?include_expired=true",
                          headers=env["headers"])
        ids = {g["grant_id"] for g in resp.get_json()["data"]["items"]}
        assert ids == {"g-active", "g-expired"}

        resp = client.get(f"{base}?include_inactive=true",
                          headers=env["headers"])
        ids = {g["grant_id"] for g in resp.get_json()["data"]["items"]}
        assert ids == {"g-active", "g-revoked"}

        resp = client.get(f"{base}?status=revoked&include_expired=true",
                          headers=env["headers"])
        ids = {g["grant_id"] for g in resp.get_json()["data"]["items"]}
        assert ids == {"g-revoked"}

        resp = client.get(f"{base}?status=bogus", headers=env["headers"])
        assert resp.status_code == 400

    def test_remaining_uses_and_names(self, client, env):
        g = _grant(secret=_secret(env["owner"]), owner=env["owner"],
                   target=env["target"], grant_id="g-rem",
                   max_uses=10, used_count=4)
        resp = client.get(
            f"{env['base']}/secrets/{g.secret_id}/grants",
            headers=env["headers"])
        item = resp.get_json()["data"]["items"][0]
        assert item["remaining_uses"] == 6
        assert item["from_agent_name"] == "owner"
        assert item["to_agent_name"] == "target"


class TestCreateGrant:
    def _post(self, client, env, secret, **payload):
        body = {"target_agent_id": env["target"].id, **payload}
        return client.post(
            f"{env['base']}/secrets/{secret.id}/grants",
            headers=env["headers"], json=body)

    def test_create_ephemeral_defaults(self, client, env):
        secret = _secret(env["owner"])
        resp = self._post(client, env, secret)
        assert resp.status_code == 201
        data = resp.get_json()["data"]
        assert data["grant_mode"] == "ephemeral"
        assert data["max_uses"] == 1
        assert data["remaining_uses"] == 1

    def test_same_agent_400(self, client, env):
        secret = _secret(env["owner"])
        resp = self._post(client, env, secret,
                          target_agent_id=env["owner"].id)
        assert resp.status_code == 400

    def test_target_404_and_inactive_400(self, client, env):
        secret = _secret(env["owner"])
        resp = self._post(client, env, secret, target_agent_id=999999)
        assert resp.status_code == 404
        env["target"].status = AgentStatus.DISABLED
        db.session.commit()
        resp = self._post(client, env, secret,
                          target_agent_id=env["target"].id)
        assert resp.status_code == 400

    def test_invalid_mode_and_bad_ints(self, client, env):
        secret = _secret(env["owner"])
        resp = self._post(client, env, secret, grant_mode="eternal")
        assert resp.status_code == 400
        resp = self._post(client, env, secret, max_uses=-5)
        assert resp.status_code == 400
        resp = self._post(client, env, secret, chain_id="abc")
        assert resp.status_code == 400

    def test_revoked_secret_400(self, client, env):
        secret = _secret(env["owner"], is_active=False)
        resp = self._post(client, env, secret)
        assert resp.status_code == 400

    def test_expires_at_validations(self, client, env):
        secret = _secret(env["owner"])
        resp = self._post(client, env, secret, expires_at="junk")
        assert resp.status_code == 400
        resp = self._post(client, env, secret,
                          expires_at="2020-01-01T00:00:00")
        assert resp.status_code == 400

    def test_task_must_belong_to_workspace(self, client, env, user):
        other_org = Organization(name=f"o_{uuid.uuid4().hex[:6]}",
                                 slug=f"o_{uuid.uuid4().hex[:6]}",
                                 owner_id=user().id)
        db.session.add(other_org)
        db.session.flush()
        project = Project(name=f"p_{uuid.uuid4().hex[:6]}",
                          owner_id=env["user"].id,
                          organization_id=other_org.id)
        db.session.add(project)
        db.session.flush()
        task = Task(project_id=project.id, owner_id=env["user"].id,
                    title="外部任务", status="TODO")
        db.session.add(task)
        db.session.commit()
        secret = _secret(env["owner"])

        resp = self._post(client, env, secret, task_id=task.id)
        assert resp.status_code == 400
        assert "task_id" in resp.get_json()["message"]

    def test_attempt_id_too_long_400(self, client, env):
        secret = _secret(env["owner"])
        resp = self._post(client, env, secret, attempt_id="a" * 65)
        assert resp.status_code == 400

    def test_create_manage_403(self, client, env, monkeypatch):
        from api.base import ApiResponse
        monkeypatch.setattr(
            "api.agent_workspace_secrets.routes_grants."
            "ensure_agent_manage_access",
            lambda user, agent: ApiResponse.forbidden(
                "Access denied").to_response())
        secret = _secret(env["owner"])
        resp = client.post(
            f"{env['base']}/secrets/{secret.id}/grants",
            headers=env["headers"],
            json={"target_agent_id": env["target"].id})
        assert resp.status_code == 403

    def test_missing_target_agent_id_400(self, client, env):
        secret = _secret(env["owner"])
        resp = client.post(
            f"{env['base']}/secrets/{secret.id}/grants",
            headers=env["headers"], json={})
        assert resp.status_code == 400

    def test_guard_404_403_400(self, client, env, user):
        secret = _secret(env["owner"])
        resp = client.post(
            env["base"].replace(f"/agents/{env['owner'].id}", "/agents/999999")
            + f"/secrets/{secret.id}/grants", headers=env["headers"],
            json={"target_agent_id": 1})
        assert resp.status_code == 404
        outsider = user()
        token = create_access_token(identity=str(outsider.id))
        resp = client.post(f"{env['base']}/secrets/{secret.id}/grants",
                           headers={"Authorization": f"Bearer {token}"},
                           json={"target_agent_id": 1})
        assert resp.status_code == 403
        resp = client.post(f"{env['base']}/secrets/999999/grants",
                           headers=env["headers"], json={"target_agent_id": 1})
        assert resp.status_code == 404


class TestRevokeGrant:
    def test_revoke_success_and_409(self, client, env):
        secret = _secret(env["owner"])
        g = _grant(secret, env["owner"], env["target"], grant_id="g-rev1")
        url = f"{env['base']}/secrets/{secret.id}/grants/{g.grant_id}/revoke"
        resp = client.post(url, headers=env["headers"])
        assert resp.status_code == 200
        assert g.status == "revoked"
        assert g.revoked_by_user_id == env["user"].id

        resp = client.post(url, headers=env["headers"])
        assert resp.status_code == 409

    def test_revoke_404s(self, client, env):
        secret = _secret(env["owner"])
        g = _grant(secret, env["owner"], env["target"], grant_id="g-r2")
        base = f"{env['base']}/secrets/{secret.id}/grants"
        assert client.post(f"{base}/no-such-grant/revoke",
                           headers=env["headers"]).status_code == 404
        assert client.post(
            env["base"].replace(f"/agents/{env['owner'].id}", "/agents/999999")
            + f"/secrets/{secret.id}/grants/{g.grant_id}/revoke",
            headers=env["headers"]).status_code == 404
        assert client.post(
            env["base"] + "/secrets/999999/grants/g-r2/revoke",
            headers=env["headers"]).status_code == 404
