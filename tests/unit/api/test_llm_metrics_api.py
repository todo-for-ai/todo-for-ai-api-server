"""LLM 调用指标 API 测试：daemon 摄取（幂等/校验）+ 用户/组织/Agent 三视角聚合。"""

import uuid

import pytest

from flask_jwt_extended import create_access_token

BASE_URL = "/todo-for-ai/api/v1"


@pytest.fixture(autouse=True)
def _clean_llm_metrics(db_session):
    """指标行经 HTTP 摄取、无 factory 托管；不清会让复用的 user/org id 跨用例串数据。"""
    yield
    from models import LlmCallMetric

    LlmCallMetric.query.delete()
    db_session.commit()


def _make_runtime_world(client, db_session, user_factory, organization_factory, agent_factory):
    """建 用户+组织+Agent+key，经 introspect 换取 daemon 会话，返回全套上下文。"""
    from models import AgentKey

    user = user_factory()
    org = organization_factory(owner_id=user.id)
    agent = agent_factory(workspace_id=org.id, creator_user_id=user.id)

    key_row, raw_key = AgentKey.generate_key(
        name=f"llm-metrics-{uuid.uuid4().hex[:6]}",
        workspace_id=org.id,
        agent_id=agent.id,
        created_by_user_id=user.id,
    )
    db_session.add(key_row)
    db_session.commit()

    auth_resp = client.post(f"{BASE_URL}/agent/auth/introspect", json={"agent_key": raw_key})
    assert auth_resp.status_code == 200
    token = auth_resp.get_json()["data"]["access_token"]

    return {
        "user": user,
        "org": org,
        "agent": agent,
        "runtime_headers": {"Authorization": f"Bearer {token}"},
    }


def _user_jwt_headers(user):
    return {"Authorization": f"Bearer {create_access_token(identity=str(user.id))}"}


def _call(call_id="c1", status="success", **overrides):
    payload = {
        "call_id": call_id,
        "task_id": 10666234,
        "attempt_id": "att_1",
        "engine": "claude",
        "model": "deepseek-v4-pro",
        "base_url": "http://192.168.1.83:54988",
        "status": status,
        "duration_ms": 4810,
        "input_tokens": 576,
        "output_tokens": 235,
        "total_tokens": 811,
        "cache_read_tokens": 576,
        "cost_usd": 0.0102,
    }
    payload.update(overrides)
    return payload


class TestIngest:
    def test_batch_ingest_resolves_owner(self, client, db_session, user_factory,
                                          organization_factory, agent_factory):
        from models import LlmCallMetric

        ctx = _make_runtime_world(client, db_session, user_factory,
                                  organization_factory, agent_factory)
        resp = client.post(
            f"{BASE_URL}/agent/llm-metrics/batch",
            json={"calls": [_call("c1"), _call("c2", status="failed",
                                            error_code="ENGINE_FAILED", error_message="boom")]},
            headers=ctx["runtime_headers"],
        )
        assert resp.status_code == 200
        assert resp.get_json()["data"] == {"inserted": 2, "skipped": 0}

        rows = LlmCallMetric.query.filter(LlmCallMetric.agent_id == ctx["agent"].id).all()
        assert len(rows) == 2
        row = next(r for r in rows if r.call_id == "c1")
        assert row.workspace_id == ctx["org"].id
        assert row.owner_user_id == ctx["user"].id  # creator_user_id 回退归属
        assert row.status == "success"
        assert row.total_tokens == 811

    def test_duplicate_call_id_skipped(self, client, db_session, user_factory,
                                       organization_factory, agent_factory):
        ctx = _make_runtime_world(client, db_session, user_factory,
                                  organization_factory, agent_factory)
        payload = {"calls": [_call("dup-1")]}
        r1 = client.post(f"{BASE_URL}/agent/llm-metrics/batch", json=payload,
                         headers=ctx["runtime_headers"])
        r2 = client.post(f"{BASE_URL}/agent/llm-metrics/batch", json=payload,
                         headers=ctx["runtime_headers"])
        assert r1.get_json()["data"]["inserted"] == 1
        assert r2.get_json()["data"] == {"inserted": 0, "skipped": 1}

    def test_invalid_status_rejected(self, client, db_session, user_factory,
                                     organization_factory, agent_factory):
        ctx = _make_runtime_world(client, db_session, user_factory,
                                  organization_factory, agent_factory)
        resp = client.post(
            f"{BASE_URL}/agent/llm-metrics/batch",
            json={"calls": [_call("bad-1", status="bogus")]},
            headers=ctx["runtime_headers"],
        )
        assert resp.status_code == 400

    def test_batch_over_limit_rejected(self, client, db_session, user_factory,
                                       organization_factory, agent_factory):
        ctx = _make_runtime_world(client, db_session, user_factory,
                                  organization_factory, agent_factory)
        resp = client.post(
            f"{BASE_URL}/agent/llm-metrics/batch",
            json={"calls": [_call(f"c-{i}") for i in range(101)]},
            headers=ctx["runtime_headers"],
        )
        assert resp.status_code == 400

    def test_requires_agent_session(self, client):
        resp = client.post(f"{BASE_URL}/agent/llm-metrics/batch", json={"calls": []})
        assert resp.status_code == 401


class TestQueries:
    def _seed(self, client, db_session, user_factory, organization_factory, agent_factory):
        ctx = _make_runtime_world(client, db_session, user_factory,
                                  organization_factory, agent_factory)
        resp = client.post(
            f"{BASE_URL}/agent/llm-metrics/batch",
            json={"calls": [_call("q1"), _call("q2", status="timeout",
                                            error_code="ENGINE_TIMEOUT")]},
            headers=ctx["runtime_headers"],
        )
        assert resp.status_code == 200
        return ctx

    def test_my_summary(self, client, db_session, user_factory, organization_factory, agent_factory):
        ctx = self._seed(client, db_session, user_factory, organization_factory, agent_factory)
        resp = client.get(f"{BASE_URL}/llm-metrics/mine", headers=_user_jwt_headers(ctx["user"]))
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["totals"]["calls"] == 2
        assert data["totals"]["success"] == 1
        assert data["totals"]["timeout"] == 1
        assert data["totals"]["total_tokens"] == 1622
        assert data["totals"]["p50_duration_ms"] == 4810
        assert len(data["by_agent"]) == 1
        assert data["by_agent"][0]["agent_id"] == ctx["agent"].id
        assert len(data["by_day"]) == 1

    def test_my_summary_empty(self, client, db_session, user_factory):
        user = user_factory()
        resp = client.get(f"{BASE_URL}/llm-metrics/mine", headers=_user_jwt_headers(user))
        assert resp.status_code == 200
        assert resp.get_json()["data"]["totals"]["calls"] == 0

    def test_workspace_summary_for_owner(self, client, db_session, user_factory,
                                         organization_factory, agent_factory):
        ctx = self._seed(client, db_session, user_factory, organization_factory, agent_factory)
        resp = client.get(
            f"{BASE_URL}/workspaces/{ctx['org'].id}/llm-metrics",
            headers=_user_jwt_headers(ctx["user"]),
        )
        assert resp.status_code == 200
        assert resp.get_json()["data"]["totals"]["calls"] == 2

    def test_workspace_summary_denied_for_outsider(self, client, db_session, user_factory,
                                                   organization_factory, agent_factory):
        ctx = self._seed(client, db_session, user_factory, organization_factory, agent_factory)
        outsider = user_factory()
        resp = client.get(
            f"{BASE_URL}/workspaces/{ctx['org'].id}/llm-metrics",
            headers=_user_jwt_headers(outsider),
        )
        assert resp.status_code == 403

    def test_agent_summary_requires_manage_access(self, client, db_session, user_factory,
                                                  organization_factory, agent_factory):
        ctx = self._seed(client, db_session, user_factory, organization_factory, agent_factory)
        ok = client.get(
            f"{BASE_URL}/llm-metrics/agents/{ctx['agent'].id}",
            headers=_user_jwt_headers(ctx["user"]),
        )
        assert ok.status_code == 200
        outsider = user_factory()
        denied = client.get(
            f"{BASE_URL}/llm-metrics/agents/{ctx['agent'].id}",
            headers=_user_jwt_headers(outsider),
        )
        assert denied.status_code == 403

    def test_invalid_hours_falls_back_to_default(self, client, db_session, user_factory,
                                                 organization_factory, agent_factory):
        ctx = self._seed(client, db_session, user_factory, organization_factory, agent_factory)
        resp = client.get(
            f"{BASE_URL}/llm-metrics/mine?hours=not-a-number",
            headers=_user_jwt_headers(ctx["user"]),
        )
        assert resp.status_code == 200
        assert resp.get_json()["data"]["window_hours"] == 168
