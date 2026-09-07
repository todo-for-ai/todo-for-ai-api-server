"""Agent 工作区机密 API（api/agent_workspace_secrets/routes_secrets.py）单元回归。

覆盖：机密列表（share 计数/include_shared 共享来源过滤）、创建（校验矩阵/
project_shared 项目归属/重名 409）、reveal（解密回显/计数/已吊销 400）、
共享 reveal（无授权 404/不可读 403/已吊销 400/成功含所有方信息）、rotate
（哈希密文轮换/已吊销 400/空值 400）、revoke（连带吊销 share 与 grant）、
shares 列表（include_inactive/is_expired 计算）。
"""

import uuid

import pytest
from flask_jwt_extended import create_access_token
from cryptography.fernet import Fernet

from models import (
    Agent,
    AgentSecret,
    AgentSecretGrant,
    AgentSecretShare,
    AgentStatus,
    Organization,
    Project,
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
        u = User(username=f"uu_{uuid.uuid4().hex[:8]}",
                 email=f"uu_{uuid.uuid4().hex[:6]}@t.io")
        db.session.add(u)
        db.session.commit()
        return u
    return _make


@pytest.fixture
def client(_isolated_app):
    return _isolated_app.test_client()


@pytest.fixture
def env(_isolated_app):
    u = User(username=f"ws_{uuid.uuid4().hex[:8]}",
             email=f"ws_{uuid.uuid4().hex[:6]}@t.io")
    db.session.add(u)
    db.session.flush()
    org = Organization(name=f"o_{uuid.uuid4().hex[:6]}",
                       slug=f"o_{uuid.uuid4().hex[:6]}", owner_id=u.id)
    db.session.add(org)
    db.session.commit()

    owner = Agent(workspace_id=org.id, owner_id=u.id, creator_user_id=u.id,
                  name="owner-agent", status=AgentStatus.ACTIVE)
    target = Agent(workspace_id=org.id, owner_id=u.id, creator_user_id=u.id,
                   name="target-agent", status=AgentStatus.ACTIVE)
    db.session.add_all([owner, target])
    db.session.commit()

    token = create_access_token(identity=str(u.id))
    return {
        "user": u, "org": org, "owner": owner, "target": target,
        "headers": {"Authorization": f"Bearer {token}"},
        "base": f"/todo-for-ai/api/v1/workspaces/{org.id}/agents/{owner.id}",
    }


def _secret(agent, name="api-key", is_active=True):
    row = AgentSecret.from_plaintext(
        agent_id=agent.id, workspace_id=agent.workspace_id, name=name,
        secret_value="plain-value", user_id=1, created_by="test")
    row.is_active = is_active
    db.session.add(row)
    db.session.commit()
    return row


def _share(secret, owner, target, is_active=True, expires_at=None,
           access_mode="read"):
    row = AgentSecretShare(
        secret_id=secret.id, workspace_id=secret.workspace_id,
        owner_agent_id=owner.id, target_agent_id=target.id,
        access_mode=access_mode, expires_at=expires_at,
        is_active=is_active, granted_by_user_id=1,
    )
    db.session.add(row)
    db.session.commit()
    return row


@pytest.fixture
def outsider(_isolated_app, user):
    token = create_access_token(identity=str(user().id))
    return {"headers": {"Authorization": f"Bearer {token}"}}


def _owner_headers(env):
    return env["headers"]


class TestListSecrets:
    def test_empty(self, client, env):
        resp = client.get(env["base"] + "/secrets", headers=env["headers"])
        assert resp.status_code == 200
        assert resp.get_json()["data"]["items"] == []

    def test_owned_with_share_counts(self, client, env):
        secret = _secret(env["owner"])
        _share(secret, env["owner"], env["target"])
        _share(secret, env["owner"], env["target"], is_active=False)  # 不计

        resp = client.get(env["base"] + "/secrets", headers=env["headers"])
        items = resp.get_json()["data"]["items"]
        assert len(items) == 1
        assert items[0]["source"] == "owned"
        assert items[0]["shared_to_agent_count"] == 1
        assert items[0]["secret_value"] if False else "secret_value" not in items[0]

    def test_include_shared_filters_revoked_and_self(self, client, env):
        other = Agent(workspace_id=env["org"].id, owner_id=env["user"].id,
                      creator_user_id=env["user"].id, name="other-agent",
                      status=AgentStatus.ACTIVE)
        db.session.add(other)
        db.session.commit()

        shared_secret = _secret(other, "shared-key")
        _share(shared_secret, other, env["target"])  # 有效共享 → appear
        _secret(other, "revoked-shared", is_active=False)
        revoked_share = _share(_secret(other, "dead"), other, env["target"],
                               is_active=False)  # 非激活 share → skip
        self_share = _secret(env["target"], "self-key")
        _share(self_share, env["target"], env["target"])  # 自己共享给自己 → skip

        resp = client.get(env["base"].replace(
            f"/agents/{env['owner'].id}", f"/agents/{env['target'].id}"
        ) + "/secrets?include_shared=true", headers=env["headers"])
        items = resp.get_json()["data"]["items"]
        shared = [i for i in items if i["source"] == "shared"]
        assert len(shared) == 1
        assert shared[0]["name"] == "shared-key"
        assert shared[0]["owner_agent_name"] == "other-agent"


    def test_agent_404(self, client, env):
        resp = client.get(
            env["base"].replace(f"/agents/{env['owner'].id}", "/agents/999999")
            + "/secrets", headers=env["headers"])
        assert resp.status_code == 404

    def test_detail_access_denied_403(self, client, env, outsider):
        resp = client.get(env["base"] + "/secrets",
                          headers=outsider["headers"])
        assert resp.status_code == 403

    def test_shared_loop_skips_missing_secret_and_self(self, client, env,
                                                       monkeypatch):
        """share 指向已删除 secret / 自 sharing 均跳过；owner 缺失 → 名字 None。"""
        import uuid as _uuid
        from models import Agent as Ag
        target = env["target"]
        other = Agent(workspace_id=env["org"].id, owner_id=env["user"].id,
                      creator_user_id=env["user"].id, name="other-agent",
                      status=AgentStatus.ACTIVE)
        db.session.add(other)
        db.session.commit()
        ghost_secret = _secret(other, "ghost-owned")
        # 幽灵 share：owner_agent_id 指向不存在的 Agent（sqlite 不强外键）
        ghost = AgentSecretShare(
            secret_id=ghost_secret.id, workspace_id=env["org"].id,
            owner_agent_id=999999, target_agent_id=target.id,
            granted_by_user_id=env["user"].id)
        db.session.add(ghost)
        # 已吊销 secret 的 share → skip（84 行）
        dead = _secret(target, "dead-key", is_active=False)
        dead_owner = Agent(workspace_id=env["org"].id, owner_id=env["user"].id,
                           creator_user_id=env["user"].id, name="dead-owner",
                           status=AgentStatus.ACTIVE)
        db.session.add(dead_owner)
        db.session.commit()
        dead_share = AgentSecretShare(
            secret_id=dead.id, workspace_id=env["org"].id,
            owner_agent_id=dead_owner.id, target_agent_id=target.id,
            granted_by_user_id=env["user"].id)
        db.session.add(dead_share)
        db.session.commit()

        resp = client.get(
            env["base"].replace(f"/agents/{env['owner'].id}",
                                f"/agents/{target.id}")
            + "/secrets?include_shared=true", headers=env["headers"])
        names = {i["name"] for i in resp.get_json()["data"]["items"]
                 if i["source"] == "shared"}
        assert names == {"ghost-owned"}  # owner 缺失 → owner_agent_name=None
        assert "dead-key" not in names  # 已吊销 secret 的 share 被跳过

    def test_revoke_secret_404(self, client, env):
        resp = client.post(f"{env['base']}/secrets/999999/revoke",
                           headers=env["headers"])
        assert resp.status_code == 404


class TestCreateSecret:
    def test_success_with_defaults(self, client, env):
        resp = client.post(env["base"] + "/secrets", headers=env["headers"],
                           json={"name": "k1", "secret_value": "v1"})
        assert resp.status_code == 201
        data = resp.get_json()["data"]
        assert data["name"] == "k1"
        assert "secret_value" not in data  # 不回显明文

    def test_missing_fields_400(self, client, env):
        assert client.post(env["base"] + "/secrets", headers=env["headers"],
                           json={"name": "x"}).status_code == 400
        resp = client.post(env["base"] + "/secrets", headers=env["headers"],
                           json={"name": "  ", "secret_value": "v"})
        assert resp.status_code == 400
        resp = client.post(env["base"] + "/secrets", headers=env["headers"],
                           json={"name": "x", "secret_value": ""})
        assert resp.status_code == 400

    def test_invalid_type_and_scope(self, client, env):
        resp = client.post(env["base"] + "/secrets", headers=env["headers"],
                           json={"name": "x", "secret_value": "v",
                                 "secret_type": "nope"})
        assert resp.status_code == 400
        resp = client.post(env["base"] + "/secrets", headers=env["headers"],
                           json={"name": "x", "secret_value": "v",
                                 "scope_type": "galactic"})
        assert resp.status_code == 400

    def test_project_shared_requires_project(self, client, env):
        resp = client.post(env["base"] + "/secrets", headers=env["headers"],
                           json={"name": "x", "secret_value": "v",
                                 "scope_type": "project_shared"})
        assert resp.status_code == 400

    def test_project_shared_wrong_workspace(self, client, env, user):
        from models import Organization as Org
        other_org = Org(name=f"o_{uuid.uuid4().hex[:6]}",
                        slug=f"o_{uuid.uuid4().hex[:6]}", owner_id=user().id)
        db.session.add(other_org)
        db.session.flush()
        project = Project(name=f"p_{uuid.uuid4().hex[:6]}",
                          owner_id=env["user"].id,
                          organization_id=other_org.id)
        db.session.add(project)
        db.session.commit()

        resp = client.post(env["base"] + "/secrets", headers=env["headers"],
                           json={"name": "x", "secret_value": "v",
                                 "scope_type": "project_shared",
                                 "project_id": project.id})
        assert resp.status_code == 400

    def test_project_shared_success(self, client, env):
        project = Project(name=f"p_{uuid.uuid4().hex[:6]}",
                          owner_id=env["user"].id,
                          organization_id=env["org"].id)
        db.session.add(project)
        db.session.commit()
        resp = client.post(env["base"] + "/secrets", headers=env["headers"],
                           json={"name": "x", "secret_value": "v",
                                 "scope_type": "project_shared",
                                 "project_id": str(project.id)})
        assert resp.status_code == 201

    def test_duplicate_active_name_409(self, client, env):
        client.post(env["base"] + "/secrets", headers=env["headers"],
                    json={"name": "dup", "secret_value": "v"})
        resp = client.post(env["base"] + "/secrets", headers=env["headers"],
                           json={"name": "dup", "secret_value": "v2"})
        assert resp.status_code == 409


    def test_agent_404_and_manage_403(self, client, env, outsider, user):
        # agent 404
        resp = client.post(
            env["base"].replace(f"/agents/{env['owner'].id}", "/agents/999999")
            + "/secrets", headers=env["headers"], json={"name": "x",
                                                       "secret_value": "v"})
        assert resp.status_code == 404
        # 外人无管理权 403
        resp = client.post(env["base"] + "/secrets",
                           headers=outsider["headers"],
                           json={"name": "x", "secret_value": "v"})
        assert resp.status_code == 403

    def test_project_id_non_integer_400(self, client, env):
        resp = client.post(env["base"] + "/secrets", headers=env["headers"],
                           json={"name": "x", "secret_value": "v",
                                 "scope_type": "project_shared",
                                 "project_id": "abc"})
        assert resp.status_code == 400
        assert "must be an integer" in resp.get_json()["message"]

    def test_project_in_other_workspace_400(self, client, env, user):
        from models import Organization as Org
        other_org = Org(name=f"o_{uuid.uuid4().hex[:6]}",
                        slug=f"o_{uuid.uuid4().hex[:6]}", owner_id=user().id)
        db.session.add(other_org)
        db.session.flush()
        project = Project(name=f"p_{uuid.uuid4().hex[:6]}",
                          owner_id=env["user"].id,
                          organization_id=other_org.id)
        db.session.add(project)
        db.session.commit()
        resp = client.post(env["base"] + "/secrets", headers=env["headers"],
                           json={"name": "x", "secret_value": "v",
                                 "scope_type": "project_shared",
                                 "project_id": project.id})
        assert resp.status_code == 400
        assert "does not belong" in resp.get_json()["message"]


class TestReveal:
    def test_reveal_success_bumps_usage(self, client, env):
        secret = _secret(env["owner"])
        resp = client.post(
            f"{env['base']}/secrets/{secret.id}/reveal",
            headers=env["headers"])
        assert resp.status_code == 200
        assert resp.get_json()["data"]["secret_value"] == "plain-value"
        assert secret.usage_count == 1
        assert secret.last_used_at is not None

    def test_reveal_revoked_400(self, client, env):
        secret = _secret(env["owner"], is_active=False)
        resp = client.post(
            f"{env['base']}/secrets/{secret.id}/reveal",
            headers=env["headers"])
        assert resp.status_code == 400

    def test_reveal_404(self, client, env):
        resp = client.post(f"{env['base']}/secrets/999999/reveal",
                           headers=env["headers"])
        assert resp.status_code == 404


    def test_agent_404_and_manage_403(self, client, env, outsider):
        resp = client.post(
            env["base"].replace(f"/agents/{env['owner'].id}", "/agents/999999")
            + "/secrets/1/reveal", headers=env["headers"])
        assert resp.status_code == 404
        resp = client.post(env["base"] + "/secrets/1/reveal",
                           headers=outsider["headers"])
        assert resp.status_code == 403


class TestSharedReveal:
    def _setup_share(self, env, access_mode="read", is_active=True,
                     expires_at=None):
        secret = _secret(env["owner"], "shared-secret")
        return secret, _share(secret, env["owner"], env["target"],
                              is_active=is_active, expires_at=expires_at,
                              access_mode=access_mode)

    def _reveal_url(self, env):
        return (env["base"].replace(f"/agents/{env['owner'].id}",
                                    f"/agents/{env['target'].id}")
                + f"/shared-secrets/{{sid}}/reveal")

    def test_no_share_404(self, client, env):
        secret = _secret(env["owner"])
        url = self._reveal_url(env).format(sid=secret.id)
        resp = client.post(url, headers=env["headers"])
        assert resp.status_code == 404

    def test_success_includes_owner_info(self, client, env):
        secret, share = self._setup_share(env)
        url = self._reveal_url(env).format(sid=secret.id)
        resp = client.post(url, headers=env["headers"])
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["secret_value"] == "plain-value"
        assert data["owner_agent_id"] == env["owner"].id
        assert data["owner_agent_name"] == "owner-agent"
        assert data["share_id"] == share.id

    def test_non_read_mode_403(self, client, env):
        secret, _share_row = self._setup_share(env, access_mode="write")
        url = self._reveal_url(env).format(sid=secret.id)
        resp = client.post(url, headers=env["headers"])
        assert resp.status_code == 403

    def test_revoked_secret_400(self, client, env):
        secret = _secret(env["owner"], is_active=False)
        _share(secret, env["owner"], env["target"])
        url = self._reveal_url(env).format(sid=secret.id)
        resp = client.post(url, headers=env["headers"])
        assert resp.status_code == 400

    def test_expired_share_404(self, client, env):
        from datetime import datetime, timedelta
        secret = _secret(env["owner"])
        _share(secret, env["owner"], env["target"],
               expires_at=datetime.utcnow() - timedelta(hours=1))
        url = self._reveal_url(env).format(sid=secret.id)
        resp = client.post(url, headers=env["headers"])
        assert resp.status_code == 404


    def test_agent_404_and_manage_403(self, client, env, outsider):
        resp = client.post(
            env["base"].replace(f"/agents/{env['owner'].id}", "/agents/999999")
            + "/secrets/1/reveal", headers=env["headers"])
        assert resp.status_code == 404
        resp = client.post(env["base"] + "/secrets/1/reveal",
                           headers=outsider["headers"])
        assert resp.status_code == 403


class TestRotate:
    def test_rotate_success(self, client, env):
        secret = _secret(env["owner"])
        old_hash = secret.secret_hash
        resp = client.post(f"{env['base']}/secrets/{secret.id}/rotate",
                           headers=env["headers"],
                           json={"secret_value": "new-value"})
        assert resp.status_code == 200
        assert secret.secret_hash != old_hash
        assert secret.is_active is True
        assert secret.reveal() == "new-value"

    def test_rotate_revoked_400(self, client, env):
        secret = _secret(env["owner"], is_active=False)
        resp = client.post(f"{env['base']}/secrets/{secret.id}/rotate",
                           headers=env["headers"],
                           json={"secret_value": "x"})
        assert resp.status_code == 400

    def test_rotate_empty_value_400(self, client, env):
        secret = _secret(env["owner"])
        resp = client.post(f"{env['base']}/secrets/{secret.id}/rotate",
                           headers=env["headers"], json={"secret_value": ""})
        assert resp.status_code == 400

    def test_rotate_missing_field_400(self, client, env):
        secret = _secret(env["owner"])
        resp = client.post(f"{env['base']}/secrets/{secret.id}/rotate",
                           headers=env["headers"], json={})
        assert resp.status_code == 400


    def test_agent_404_and_manage_403(self, client, env, outsider):
        resp = client.post(
            env["base"].replace(f"/agents/{env['owner'].id}", "/agents/999999")
            + "/secrets/1/rotate", headers=env["headers"],
            json={"secret_value": "v"})
        assert resp.status_code == 404
        resp = client.post(env["base"] + "/secrets/1/rotate",
                           headers=outsider["headers"],
                           json={"secret_value": "v"})
        assert resp.status_code == 403


class TestRevoke:
    def test_revoke_and_rotate_agent_404_manage_403(self, client, env,
                                                    outsider):
        # revoke: agent 404 + 外人 403
        resp = client.post(
            env["base"].replace(f"/agents/{env['owner'].id}", "/agents/999999")
            + "/secrets/1/revoke", headers=env["headers"])
        assert resp.status_code == 404
        resp = client.post(env["base"] + "/secrets/1/revoke",
                           headers=outsider["headers"])
        assert resp.status_code == 403
        # rotate: agent 404 + 外人 403 + secret 404
        resp = client.post(
            env["base"].replace(f"/agents/{env['owner'].id}", "/agents/999999")
            + "/secrets/1/rotate", headers=env["headers"],
            json={"secret_value": "v"})
        assert resp.status_code == 404
        resp = client.post(env["base"] + "/secrets/1/rotate",
                           headers=outsider["headers"],
                           json={"secret_value": "v"})
        assert resp.status_code == 403
        resp = client.post(f"{env['base']}/secrets/999999/rotate",
                           headers=env["headers"], json={"secret_value": "v"})
        assert resp.status_code == 404

    def test_shared_reveal_agent_404_manage_403(self, client, env, outsider):
        resp = client.post(
            env["base"].replace(f"/agents/{env['owner'].id}", "/agents/999999")
            + "/shared-secrets/1/reveal", headers=env["headers"])
        assert resp.status_code == 404
        resp = client.post(env["base"] + "/shared-secrets/1/reveal",
                           headers=outsider["headers"])
        assert resp.status_code == 403

    def test_shares_list_secret_404(self, client, env):
        resp = client.get(f"{env['base']}/secrets/999999/shares",
                          headers=env["headers"])
        assert resp.status_code == 404

    def test_revoke_cascades_shares_and_grants(self, client, env):
        secret = _secret(env["owner"])
        share = _share(secret, env["owner"], env["target"])
        grant = AgentSecretGrant(
            grant_id=f"g-{uuid.uuid4().hex[:10]}", secret_id=secret.id,
            workspace_id=env["org"].id, from_agent_id=env["owner"].id,
            to_agent_id=env["target"].id, status="active",
        )
        db.session.add(grant)
        db.session.commit()

        resp = client.post(f"{env['base']}/secrets/{secret.id}/revoke",
                           headers=env["headers"])
        assert resp.status_code == 200
        assert secret.is_active is False
        assert share.is_active is False
        assert share.revoked_by_user_id == env["user"].id
        assert grant.status == "revoked"


    def test_agent_404_manage_403_secret_404(self, client, env, outsider):
        # agent 404
        resp = client.get(
            env["base"].replace(f"/agents/{env['owner'].id}", "/agents/999999")
            + "/secrets/1/shares", headers=env["headers"])
        assert resp.status_code == 404
        # 外人 403
        resp = client.get(env["base"] + "/secrets/1/shares",
                          headers=outsider["headers"])
        assert resp.status_code == 403
        # secret 404
        resp = client.get(env["base"] + "/secrets/999999/shares",
                          headers=env["headers"])
        assert resp.status_code == 404


class TestSharesList:
    def test_empty(self, client, env):
        secret = _secret(env["owner"])
        resp = client.get(f"{env['base']}/secrets/{secret.id}/shares",
                          headers=env["headers"])
        assert resp.get_json()["data"]["items"] == []

    def test_lists_with_inactive_and_expired_flags(self, client, env):
        from datetime import datetime, timedelta
        secret = _secret(env["owner"])
        active = _share(secret, env["owner"], env["target"])
        inactive = _share(secret, env["owner"], env["target"],
                          is_active=False)
        expired = _share(secret, env["owner"], env["target"],
                         expires_at=datetime.utcnow() - timedelta(hours=1))

        base = f"{env['base']}/secrets/{secret.id}/shares"
        # 默认过滤仅看 is_active：过期但未停用的 share 仍列出（带 is_expired 标记）
        resp = client.get(base, headers=env["headers"])
        items = resp.get_json()["data"]["items"]
        assert len(items) == 2
        flags = {i["id"]: i["is_expired"] for i in items}
        assert flags[expired.id] is True
        assert flags[active.id] is False

        resp = client.get(f"{base}?include_inactive=true",
                          headers=env["headers"])
        assert len(resp.get_json()["data"]["items"]) == 3
