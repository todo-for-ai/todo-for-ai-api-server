"""Wave 2：HTTP 通用连接器回归（借鉴 Dify http-request 节点）。

覆盖：配置校验（api_key 可选/method 白名单/url 必填/headers/body 形状）、
{{...}} 占位符渲染进 url/headers/body、SSRF 私网防护（含 allow_private_hosts
显式放行）、同步派发执行链路、单步测试运行预览、DSL 导出保留 http 形状字段。
"""

import json
import os
import uuid

import pytest

os.environ.setdefault(
    "SECRET_ENCRYPTION_KEY", "uCuDTIUbpnE0Z47hrUqyNY8w7SjtIwKxnvZTduXeN30="
)

from app import create_app
from models import (
    db,
    Agent,
    AgentStatus,
    Organization,
    Project,
    StepStatus,
    Workflow,
    WorkflowRun,
    WorkflowStatus,
    WorkflowStep,
    WorkflowStepRun,
)
from api.agents import workflow_external_steps as wex
from api.agents import _workflow_helpers as wh

BASE_URL = "/todo-for-ai/api/v1"


@pytest.fixture(scope="function", autouse=True)
def _isolated_app():
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


@pytest.fixture
def db_session(_isolated_app):
    from models import db
    with _isolated_app.app_context():
        yield db.session
    db.session.rollback()


@pytest.fixture
def env(_isolated_app, db_session):
    from models import User

    user = User(username=f"u_{uuid.uuid4().hex[:8]}", email=f"u_{uuid.uuid4().hex[:6]}@t.io")
    db.session.add(user)
    db.session.flush()
    org = Organization(name=f"o_{uuid.uuid4().hex[:8]}", slug=f"o_{uuid.uuid4().hex[:8]}", owner_id=user.id)
    db.session.add(org)
    db.session.flush()
    project = Project(name=f"p_{uuid.uuid4().hex[:6]}", owner_id=user.id)
    db.session.add(project)
    agent = Agent(
        name=f"agent_{uuid.uuid4().hex[:6]}",
        workspace_id=org.id,
        owner_id=user.id,
        creator_user_id=user.id,
        status=AgentStatus.ACTIVE,
        capabilities=["code"],
    )
    db.session.add(agent)
    db.session.commit()
    return {"user": user, "org": org, "project": project, "agent": agent}


def _make_task(env):
    from models import Task, TaskStatus
    t = Task(project_id=env["project"].id, title="root", owner_id=env["user"].id,
             is_ai_task=True, status=TaskStatus.IN_PROGRESS)
    db.session.add(t)
    db.session.commit()
    return t


def _make_wf(env, steps):
    wf = Workflow(owner_id=env["user"].id, name=f"wf_{uuid.uuid4().hex[:6]}",
                  definition={"steps": [{"step_key": s["key"],
                                         "depends_on": s.get("depends_on") or []} for s in steps]})
    db.session.add(wf)
    db.session.flush()
    for i, s in enumerate(steps):
        db.session.add(WorkflowStep(
            workflow_id=wf.id, step_key=s["key"], name=s.get("name", s["key"]),
            order=i, depends_on=s.get("depends_on"),
            integration_config=s.get("integration_config"),
        ))
    db.session.commit()
    return wf


# ── 配置校验 ──────────────────────────────────────────────────────────


class TestHttpValidation:
    def test_api_key_optional_for_http(self):
        cfg = wex.normalize_incoming_integration_config(
            {"provider": "http", "url": "https://api.example.com/hook"})
        assert cfg["url"] == "https://api.example.com/hook"
        assert "api_key" not in cfg

    def test_api_key_encrypted_when_present(self):
        from services.github_app import decrypt_str
        cfg = wex.normalize_incoming_integration_config(
            {"provider": "http", "url": "https://x", "api_key": "tok-1"})
        assert decrypt_str(cfg["api_key"]) == "tok-1"

    def test_method_whitelist(self):
        with pytest.raises(ValueError, match="method"):
            wex.normalize_incoming_integration_config(
                {"provider": "http", "url": "https://x", "method": "TRACE"})
        cfg = wex.normalize_incoming_integration_config(
            {"provider": "http", "url": "https://x", "method": "get"})
        assert cfg["method"] == "get"  # 原样存储，执行时统一大写

    def test_url_required(self):
        with pytest.raises(ValueError, match="url"):
            wex.normalize_incoming_integration_config({"provider": "http"})

    def test_headers_must_be_scalar_map(self):
        with pytest.raises(ValueError, match="headers"):
            wex.normalize_incoming_integration_config(
                {"provider": "http", "url": "https://x",
                 "headers": {"a": {"nested": "obj"}}})

    def test_body_must_be_object_or_string(self):
        with pytest.raises(ValueError, match="body"):
            wex.normalize_incoming_integration_config(
                {"provider": "http", "url": "https://x", "body": 42})


# ── SSRF 防护 ─────────────────────────────────────────────────────────


class TestSsrfGuard:
    def test_blocks_loopback_and_private_literals(self):
        for url in ("http://127.0.0.1:50110/api", "http://192.168.1.1/x",
                    "http://10.0.0.2/", "http://172.16.0.9/", "http://169.254.1.1/",
                    "http://[::1]/", "ftp://example.com/file"):
            with pytest.raises(ValueError):
                wex.assert_public_url(url)

    def test_allows_public_literal(self):
        # 公网字面量 IP 无需 DNS 即可校验通过
        wex.assert_public_url("https://8.8.8.8/resolve")

    def test_call_blocks_private_even_when_mocked(self, env, monkeypatch):
        # 即使网络层被 mock，SSRF 守卫也必须在发出请求前拦截
        monkeypatch.setattr(wex.http_client, "request",
                            lambda *a, **kw: pytest.fail("SSRF guard should have blocked"))
        ok, out, err = wex._call_http_workflow(
            {"provider": "http"}, {"method": "POST", "url": "http://127.0.0.1:8080/steal"}, 5)
        assert ok is False and "SSRF guard" in err

    def test_allow_private_hosts_explicit_bypass(self, env, monkeypatch):
        class R:
            status_code = 200
            text = '{"ok": true}'

        captured = {}

        def fake_request(method, url, **kw):
            captured["url"] = url
            return R()

        monkeypatch.setattr(wex.http_client, "request", fake_request)
        ok, out, err = wex._call_http_workflow(
            {"provider": "http", "allow_private_hosts": True},
            {"method": "GET", "url": "http://127.0.0.1:9999/local"}, 5)
        assert ok is True and captured["url"] == "http://127.0.0.1:9999/local"


# ── 渲染与执行 ────────────────────────────────────────────────────────


class TestHttpRenderAndExecute:
    def _run_with_ext(self, env, wf):
        run = WorkflowRun.create(workflow_id=wf.id, project_id=env["project"].id,
                                 owner_id=env["user"].id, status=WorkflowStatus.PENDING,
                                 root_task_id=_make_task(env).id)
        db.session.flush()
        for s in wf.steps:
            WorkflowStepRun.create(run_id=run.id, step_key=s.step_key,
                                   status=StepStatus.PENDING, attempt=1)
        db.session.commit()
        return run

    def test_render_http_request_placeholders(self, env):
        wf = _make_wf(env, [{"key": "ext", "integration_config": {
            "provider": "http", "method": "PUT", "url": "https://api.x.io/{{step_key}}",
            "headers": {"X-Run": "{{sys.run_id}}"},
            "body": {"topic": "{{context.topic}}"}}}])
        from types import SimpleNamespace
        wf_run = SimpleNamespace(id=7, workflow=wf, workflow_id=wf.id, project_id=1,
                                 owner_id=env["user"].id, root_task_id=None,
                                 root_task=None, context={"topic": "量子"})
        req = wex.render_http_request(wf.steps[0].integration_config, wf_run, wf.steps[0])
        assert req["method"] == "PUT"
        assert req["url"] == "https://api.x.io/ext"
        assert req["headers"]["X-Run"] == "7"
        assert req["body"] == {"topic": "量子"}

    def test_sync_dispatch_success_flow(self, env, monkeypatch):
        wf = _make_wf(env, [
            {"key": "hook", "integration_config": {
                "provider": "http", "method": "POST",
                "url": "https://93.184.216.34/{{step_key}}",
                "body": {"run": "{{sys.run_id}}"}}},
            {"key": "after", "depends_on": ["hook"]},
        ])
        run = self._run_with_ext(env, wf)
        captured = {}

        class R:
            status_code = 200
            text = '{"accepted": true}'

        def fake_request(method, url, **kw):
            captured.update(method=method, url=url, json=kw.get("json"))
            return R()

        monkeypatch.setattr(wex.http_client, "request", fake_request)
        wh._advance_workflow(run)  # TESTING → 同步执行
        db.session.commit()
        db.session.expire_all()

        sr_hook, sr_after = run.step_runs
        assert sr_hook.status == StepStatus.SUCCEEDED
        assert "accepted" in sr_hook.result_summary
        assert sr_after.status == StepStatus.RUNNING  # DAG 自动推进
        assert captured["method"] == "POST"
        assert captured["url"] == "https://93.184.216.34/hook"
        assert captured["json"] == {"run": str(run.id)}

    def test_sync_dispatch_http_500_fails_step(self, env, monkeypatch):
        wf = _make_wf(env, [{"key": "hook", "integration_config": {
            "provider": "http", "url": "https://93.184.216.34/fail"}}])
        run = self._run_with_ext(env, wf)

        class R:
            status_code = 500
            text = "boom"

        monkeypatch.setattr(wex.http_client, "request",
                            lambda *a, **kw: R())
        wh._advance_workflow(run)
        db.session.commit()
        db.session.expire_all()
        assert run.step_runs[0].status == StepStatus.FAILED
        assert "HTTP 500" in run.step_runs[0].error

    def test_ssrf_blocked_config_fails_step(self, env, monkeypatch):
        monkeypatch.setattr(wex.http_client, "request",
                            lambda *a, **kw: pytest.fail("must not reach network"))
        wf = _make_wf(env, [{"key": "hook", "integration_config": {
            "provider": "http", "url": "http://10.1.2.3/internal"}}])
        run = self._run_with_ext(env, wf)
        wh._advance_workflow(run)
        db.session.commit()
        db.session.expire_all()
        assert run.step_runs[0].status == StepStatus.FAILED
        assert "SSRF guard" in run.step_runs[0].error


# ── 单步测试运行 + DSL 导出 ───────────────────────────────────────────


class TestHttpTestRunAndDsl:
    def _headers(self, user):
        from flask_jwt_extended import create_access_token
        return {"Authorization": f"Bearer {create_access_token(identity=str(user.id))}"}

    def test_run_preview_shows_rendered_request(self, client, env, monkeypatch):
        wf = _make_wf(env, [{"key": "hook", "integration_config": {
            "provider": "http", "method": "POST",
            "url": "https://93.184.216.34/{{step_key}}",
            "body": {"k": "{{context.t}}"}}}])

        class R:
            status_code = 200
            text = '{"done": 1}'

        monkeypatch.setattr(wex.http_client, "request", lambda *a, **kw: R())
        resp = client.post(
            f"{BASE_URL}/agents/workflows/{wf.id}/steps/hook/test-run",
            json={"context": {"t": "v1"}},
            headers=self._headers(env["user"]),
        )
        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()["data"]
        assert data["provider"] == "http" and data["ok"] is True
        assert data["request"]["url"] == "https://93.184.216.34/hook"
        assert data["request"]["body"] == {"k": "v1"}

    def test_dsl_export_keeps_http_shape_drops_key(self, env):
        from api.agents import workflow_dsl as wdsl
        wf = _make_wf(env, [{"key": "hook", "integration_config": {
            "provider": "http", "method": "POST", "url": "https://api.example.com/x",
            "headers": {"X-A": "b"}, "api_key": "tok-secret"}}])
        dsl = wdsl.export_workflow_dsl(wf)
        cfg = dsl["steps"][0]["integration_config"]
        assert cfg["provider"] == "http" and cfg["url"] == "https://api.example.com/x"
        assert cfg["method"] == "POST" and cfg["headers"] == {"X-A": "b"}
        assert "api_key" not in cfg
        # 导入往返：无密钥的 http 配置可直接建流
        wf2, _ = wdsl.import_workflow_dsl(
            env["user"].id, wdsl.loads_workflow_dsl(wdsl.dumps_workflow_dsl(dsl)))
        db.session.commit()
        assert wf2.steps[0].integration_config["url"] == "https://api.example.com/x"
