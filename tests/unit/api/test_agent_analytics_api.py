"""Agent 分析与监控 API（api/agent_analytics.py）单元回归。

本文件是委托层：健康监控/Secret 分析/批量操作的业务都在
services/agent_health、services/secret_analytics、services/agent_batch_ops
（均已 100% 收口），此处打桩三个服务 Getter，专注验证路由层的
工作区访问门禁（404/403/manage 403）、参数解析与透传（days/
threshold/limit/force/include_secrets/import_mode/agent_ids）、
响应形态（CSV 下载头、report 404 分支、非 JSON 400、缺参 400、
非法 AgentStatus 400）。
"""

import uuid

import pytest
from flask_jwt_extended import create_access_token

from models import (
    Agent,
    AgentStatus,
    Organization,
    OrganizationMember,
    OrganizationMemberStatus,
    OrganizationRole,
    User,
    db,
)


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
    ctx.pop()


@pytest.fixture
def client(_isolated_app):
    return _isolated_app.test_client()


def _uh(prefix="au"):
    u = User(username=f"{prefix}_{uuid.uuid4().hex[:8]}",
             email=f"{prefix}_{uuid.uuid4().hex[:6]}@t.io")
    db.session.add(u)
    db.session.flush()
    return u


def _headers_for(user):
    return {"Authorization": f"Bearer {create_access_token(identity=str(user.id))}"}


class _Recorder:
    """记录调用并返回预设值的最小服务桩。"""

    def __init__(self, result=None):
        self.calls = []
        self.result = result

    def __getattr__(self, name):
        def _method(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if isinstance(self.result, dict) and name in self.result:
                return self.result[name]
            return self.result
        return _method


@pytest.fixture
def stubs(monkeypatch):
    from api import agent_analytics as mod
    health = _Recorder(result={"status": "healthy"})
    secret = _Recorder(result={"ok": True})
    batch = _Recorder(result={"processed": 1})
    monkeypatch.setattr(mod, "get_health_monitor", lambda: health)
    monkeypatch.setattr(mod, "get_secret_analyzer", lambda: secret)
    monkeypatch.setattr(mod, "get_batch_operations", lambda: batch)
    return {"health": health, "secret": secret, "batch": batch}


@pytest.fixture
def env(_isolated_app):
    user = _uh("ow")
    org = Organization(name=f"o_{uuid.uuid4().hex[:6]}",
                       slug=f"o_{uuid.uuid4().hex[:6]}", owner_id=user.id)
    db.session.add(org)
    db.session.flush()
    agent = Agent(workspace_id=org.id, owner_id=user.id,
                  creator_user_id=user.id,
                  name=f"ag_{uuid.uuid4().hex[:6]}",
                  status=AgentStatus.ACTIVE)
    db.session.add(agent)
    db.session.commit()
    return {
        "user": user, "org": org, "agent": agent,
        "headers": _headers_for(user),
        "base": "/todo-for-ai/api/v1",
    }


def _member_of(env, role=OrganizationRole.MEMBER):
    member_user = _uh("mb")
    db.session.add(OrganizationMember(
        organization_id=env["org"].id, user_id=member_user.id,
        role=role, status=OrganizationMemberStatus.ACTIVE))
    db.session.commit()
    return member_user


def _last(stub):
    name, args, kwargs = stub.calls[-1]
    return name, args, kwargs


# ─────────────────────────── 健康监控 ───────────────────────────


class TestHealthRoutes:
    def test_agent_health_ok(self, env, client, stubs):
        resp = client.get(
            f"{env['base']}/workspaces/{env['org'].id}"
            f"/agents/{env['agent'].id}/health", headers=env["headers"])
        assert resp.status_code == 200
        assert resp.get_json()["data"] == {"status": "healthy"}
        name, args, _ = _last(stubs["health"])
        assert name == "check_agent_health"
        assert args[0] == env["agent"].id

    def test_agent_health_agent_missing_404(self, env, client, stubs):
        resp = client.get(
            f"{env['base']}/workspaces/{env['org'].id}/agents/999999/health",
            headers=env["headers"])
        assert resp.status_code == 404
        assert not stubs["health"].calls

    def test_agent_health_stranger_403(self, env, client, stubs):
        stranger = _uh("st")
        db.session.commit()
        resp = client.get(
            f"{env['base']}/workspaces/{env['org'].id}"
            f"/agents/{env['agent'].id}/health",
            headers=_headers_for(stranger))
        assert resp.status_code == 403

    def test_health_summary(self, env, client, stubs):
        resp = client.get(
            f"{env['base']}/workspaces/{env['org'].id}"
            f"/agents/health/summary", headers=env["headers"])
        assert resp.status_code == 200
        name, args, _ = _last(stubs["health"])
        assert name == "get_health_summary"
        assert args[0] == env["org"].id

    def test_workspace_missing_404(self, env, client, stubs):
        resp = client.get(f"{env['base']}/workspaces/999999"
                          f"/agents/health/summary",
                          headers=env["headers"])
        assert resp.status_code == 404


# ─────────────────────────── Secret 分析 ───────────────────────────


class TestSecretAnalyticsRoutes:
    def _url(self, env, tail, secret_id=5):
        return (f"{env['base']}/workspaces/{env['org'].id}"
                f"/secrets/{secret_id}/analytics/{tail}")

    def test_trends_default_and_custom_days(self, env, client, stubs):
        client.get(self._url(env, "trends"), headers=env["headers"])
        name, args, _ = _last(stubs["secret"])
        assert (name, args) == ("get_usage_trends", (5, 30))
        client.get(f"{self._url(env, 'trends')}?days=7",
                   headers=env["headers"])
        name, args, _ = _last(stubs["secret"])
        assert args == (5, 7)
        resp = client.get(self._url(env, "trends"), headers=env["headers"])
        data = resp.get_json()["data"]
        assert data["secret_id"] == 5 and data["days"] == 30

    def test_anomalies_threshold(self, env, client, stubs):
        client.get(f"{self._url(env, 'anomalies')}?threshold=3.5",
                   headers=env["headers"])
        name, args, _ = _last(stubs["secret"])
        assert (name, args) == ("detect_anomalies", (5, 3.5))

    def test_heatmap_days(self, env, client, stubs):
        client.get(f"{self._url(env, 'heatmap')}?days=14",
                   headers=env["headers"])
        name, args, _ = _last(stubs["secret"])
        assert (name, args) == ("get_usage_heatmap", (5, 14))

    def test_top_callers_limit(self, env, client, stubs):
        client.get(f"{self._url(env, 'top-callers')}?limit=3",
                   headers=env["headers"])
        name, args, _ = _last(stubs["secret"])
        assert (name, args) == ("get_top_callers", (5, 3))

    def test_report_ok(self, env, client, stubs):
        stubs["secret"].result = {"total_calls": 9}
        resp = client.get(self._url(env, "report"), headers=env["headers"])
        assert resp.status_code == 200
        assert resp.get_json()["data"]["total_calls"] == 9

    def test_report_error_maps_404(self, env, client, stubs):
        stubs["secret"].result = {"error": "not found"}
        resp = client.get(self._url(env, "report"), headers=env["headers"])
        assert resp.status_code == 404

    def test_workspace_stats(self, env, client, stubs):
        resp = client.get(
            f"{env['base']}/workspaces/{env['org'].id}"
            f"/secrets/analytics/stats", headers=env["headers"])
        assert resp.status_code == 200
        name, args, _ = _last(stubs["secret"])
        assert name == "get_workspace_secret_stats"

    def test_stranger_blocked_on_all(self, env, client, stubs):
        stranger = _uh("st")
        db.session.commit()
        headers = _headers_for(stranger)
        for tail in ("trends", "anomalies", "heatmap", "top-callers",
                     "report"):
            resp = client.get(self._url(env, tail), headers=headers)
            assert resp.status_code == 403, tail
        resp = client.get(
            f"{env['base']}/workspaces/{env['org'].id}"
            f"/secrets/analytics/stats", headers=headers)
        assert resp.status_code == 403
        assert not stubs["secret"].calls


# ─────────────────────────── 批量操作 ───────────────────────────


class TestBatchRoutes:
    def test_export_csv(self, env, client, stubs):
        resp = client.get(
            f"{env['base']}/workspaces/{env['org'].id}"
            f"/agents/export/csv", headers=env["headers"])
        assert resp.status_code == 200
        assert resp.mimetype == "text/csv"
        assert f"agents_{env['org'].id}.csv" in resp.headers[
            "Content-Disposition"]
        name, args, _ = _last(stubs["batch"])
        assert (name, args[0]) == ("export_agents_to_csv", env["org"].id)
        assert args[1] is None  # 未指定 agent_ids

        client.get(f"{env['base']}/workspaces/{env['org'].id}"
                   f"/agents/export/csv?agent_ids=1&agent_ids=2",
                   headers=env["headers"])
        name, args, _ = _last(stubs["batch"])
        assert args[1] == [1, 2]

    def test_export_json_flags(self, env, client, stubs):
        client.get(f"{env['base']}/workspaces/{env['org'].id}"
                   f"/agents/export/json", headers=env["headers"])
        name, args, _ = _last(stubs["batch"])
        assert args == (env["org"].id, None, False)

        client.get(f"{env['base']}/workspaces/{env['org'].id}"
                   f"/agents/export/json?include_secrets=true&agent_ids=7",
                   headers=env["headers"])
        name, args, _ = _last(stubs["batch"])
        assert args == (env["org"].id, [7], True)

    def test_import_gates_and_mode(self, env, client, stubs):
        url = (f"{env['base']}/workspaces/{env['org'].id}/agents/import")
        assert client.post(url, headers=env["headers"], data="junk",
                           content_type="text/plain").status_code == 400
        member = _member_of(env)
        assert client.post(url, headers=_headers_for(member),
                           json={"agents": []}).status_code == 403
        assert client.post(f"{env['base']}/workspaces/999999/agents/import",
                           headers=env["headers"],
                           json={}).status_code == 404
        resp = client.post(f"{url}?mode=upsert", headers=env["headers"],
                           json={"agents": [{"name": "x"}]})
        assert resp.status_code == 200
        name, args, _ = _last(stubs["batch"])
        assert name == "import_agents_from_json"
        assert args[0] == env["org"].id
        assert args[2] == {"agents": [{"name": "x"}]}
        assert args[3] == "upsert"

    def test_rotate_secrets(self, env, client, stubs):
        url = (f"{env['base']}/workspaces/{env['org'].id}"
               f"/agents/batch/rotate-secrets")
        assert client.post(url, headers=env["headers"], data="junk",
                           content_type="text/plain").status_code == 400
        assert client.post(url, headers=env["headers"],
                           json={"agent_ids": []}).status_code == 400
        resp = client.post(url, headers=env["headers"],
                           json={"agent_ids": [1, 2]})
        assert resp.status_code == 200
        name, args, _ = _last(stubs["batch"])
        assert name == "batch_rotate_secrets"
        assert args == (env["org"].id, [1, 2], env["user"].id)

    def test_update_status(self, env, client, stubs):
        url = (f"{env['base']}/workspaces/{env['org'].id}"
               f"/agents/batch/update-status")
        assert client.post(url, headers=env["headers"],
                           json={"agent_ids": [1]}
                           ).status_code == 400  # 缺 status
        assert client.post(url, headers=env["headers"],
                           json={"status": "active"}
                           ).status_code == 400  # 缺 agent_ids
        assert client.post(url, headers=env["headers"],
                           json={"agent_ids": [1],
                                 "status": "warp"}).status_code == 400
        resp = client.post(url, headers=env["headers"],
                           json={"agent_ids": [3], "status": "paused"})
        assert resp.status_code == 200
        name, args, _ = _last(stubs["batch"])
        assert name == "batch_update_agent_status"
        assert args[1] == [3]
        assert args[2] == AgentStatus.PAUSED

    def test_delete_agents_force_flag(self, env, client, stubs):
        url = (f"{env['base']}/workspaces/{env['org'].id}"
               f"/agents/batch/delete")
        assert client.post(url, headers=env["headers"],
                           json={"force": True}).status_code == 400
        resp = client.post(url, headers=env["headers"],
                           json={"agent_ids": [9], "force": True})
        assert resp.status_code == 200
        name, args, _ = _last(stubs["batch"])
        assert name == "batch_delete_agents"
        assert args == (env["org"].id, [9], env["user"].id, True)

        client.post(url, headers=env["headers"], json={"agent_ids": [9]})
        name, args, _ = _last(stubs["batch"])
        assert args[3] is False

    def test_member_without_manage_blocked_on_all_batch(self, env, client,
                                                        stubs):
        member = _member_of(env)
        headers = _headers_for(member)
        base = f"{env['base']}/workspaces/{env['org'].id}/agents"
        assert client.post(f"{base}/import", headers=headers,
                           json={}).status_code == 403
        assert client.post(f"{base}/batch/rotate-secrets", headers=headers,
                           json={"agent_ids": [1]}).status_code == 403
        assert client.post(f"{base}/batch/update-status", headers=headers,
                           json={"agent_ids": [1],
                                 "status": "active"}).status_code == 403
        assert client.post(f"{base}/batch/delete", headers=headers,
                           json={"agent_ids": [1]}).status_code == 403
        assert not stubs["batch"].calls

    def test_get_endpoints_404_and_stranger_matrix(self, env, client,
                                                   stubs):
        """每个 GET 端点的工作区 404 与陌生人 403 早退。"""
        ws = env["org"].id
        aid = env["agent"].id
        get_urls = [
            f"{env['base']}/workspaces/{ws}/agents/{aid}/health",
            f"{env['base']}/workspaces/{ws}/agents/health/summary",
            f"{env['base']}/workspaces/{ws}/secrets/5/analytics/trends",
            f"{env['base']}/workspaces/{ws}/secrets/5/analytics/anomalies",
            f"{env['base']}/workspaces/{ws}/secrets/5/analytics/heatmap",
            f"{env['base']}/workspaces/{ws}/secrets/5/analytics/top-callers",
            f"{env['base']}/workspaces/{ws}/secrets/5/analytics/report",
            f"{env['base']}/workspaces/{ws}/secrets/analytics/stats",
            f"{env['base']}/workspaces/{ws}/agents/export/csv",
            f"{env['base']}/workspaces/{ws}/agents/export/json",
        ]
        stranger = _uh("st")
        db.session.commit()
        stranger_headers = _headers_for(stranger)
        for url in get_urls:
            missing_ws = url.replace(f"/workspaces/{ws}/",
                                     "/workspaces/999999/")
            assert client.get(missing_ws,
                              headers=env["headers"]).status_code == 404, url
            assert client.get(url,
                              headers=stranger_headers
                              ).status_code == 403, url
        assert not stubs["batch"].calls and not stubs["secret"].calls

    def test_post_endpoints_gates_matrix(self, env, client, stubs):
        """每个批量 POST 端点的工作区 404、陌生人 403 与非 JSON 400。"""
        ws = env["org"].id
        post_urls = [
            f"{env['base']}/workspaces/{ws}/agents/import",
            f"{env['base']}/workspaces/{ws}/agents/batch/rotate-secrets",
            f"{env['base']}/workspaces/{ws}/agents/batch/update-status",
            f"{env['base']}/workspaces/{ws}/agents/batch/delete",
        ]
        body = {"agent_ids": [1], "status": "active", "agents": []}
        stranger = _uh("st")
        db.session.commit()
        stranger_headers = _headers_for(stranger)
        for url in post_urls:
            assert client.post(url, headers=env["headers"], data="junk",
                               content_type="text/plain"
                               ).status_code == 400, url
            missing_ws = url.replace(f"/workspaces/{ws}/",
                                     "/workspaces/999999/")
            assert client.post(missing_ws, headers=env["headers"],
                               json=body).status_code == 404, url
            assert client.post(url, headers=stranger_headers,
                               json=body).status_code == 403, url
