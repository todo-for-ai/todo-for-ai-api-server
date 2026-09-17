"""Wave 1（借鉴 Dify）回归：DSL 导入导出 + 单步测试运行 + {{sys.*}} 占位符。

对标文档 docs/DIFY_WORKFLOW_BENCHMARK.md——
- DSL：可移植/可分享的工作流定义（Dify app_dsl_service 概念）：
  导出清洗敏感与环境相关字段（api_key 永不出平台、agent_id 置空、
  子工作流按名导出），导入校验版本/唯一性/依赖/成环并按名解析子工作流。
- 单步测试运行（Dify WorkflowEntry.single_step_run 概念）：
  agent 步骤出任务预览（零副作用）、连接器步骤真实调用远端。
"""

import json
import os
import uuid
from unittest import mock

import pytest
import yaml

# 连接器导入/单步测试要解密 api_key；worktree 里没有 .env，必须显式给键
# （套件其余加密相关测试共用同一测试密钥）
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
    Workflow,
    WorkflowStep,
)
from api.agents import workflow_dsl as wdsl
from api.agents import workflow_external_steps as wex

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
        capabilities=["code_review"],
    )
    db.session.add(agent)
    db.session.commit()
    return {"user": user, "org": org, "project": project, "agent": agent}


def _headers(user):
    from flask_jwt_extended import create_access_token
    return {"Authorization": f"Bearer {create_access_token(identity=str(user.id))}"}


def _make_wf(env, steps=(), definition=None, name=None):
    wf = Workflow(
        owner_id=env["user"].id,
        name=name or f"wf_{uuid.uuid4().hex[:6]}",
        definition=definition or {},
    )
    db.session.add(wf)
    db.session.flush()
    for i, s in enumerate(steps):
        db.session.add(WorkflowStep(
            workflow_id=wf.id,
            step_key=s["key"],
            name=s.get("name", s["key"]),
            order=i,
            depends_on=s.get("depends_on"),
            required_capabilities=s.get("caps"),
            integration_config=s.get("integration_config"),
            sub_workflow_id=s.get("sub_workflow_id"),
            retry_count=s.get("retry_count", 0),
        ))
    db.session.commit()
    return wf


# ── 导出：结构 + 敏感清洗 ─────────────────────────────────────────────


class TestExport:
    def test_export_cleans_sensitive_and_env_fields(self, env):
        sub = _make_wf(env, [{"key": "s1"}])
        wf = _make_wf(env, [
            {"key": "a", "caps": ["code_review"]},
            {"key": "ext",
             "integration_config": {"provider": "dify", "api_key": "app-secret",
                                    "inputs": {"q": "{{sys.run_id}}"}}},
            {"key": "sub", "depends_on": ["ext"], "sub_workflow_id": sub.id},
        ])
        dsl = wdsl.export_workflow_dsl(wf)

        assert dsl[wdsl.DSL_MAGIC] == wdsl.DSL_MARKER
        assert dsl["version"] == wdsl.DSL_VERSION
        assert dsl["workflow"]["name"] == wf.name
        assert [s["step_key"] for s in dsl["steps"]] == ["a", "ext", "sub"]
        # api_key 永不出平台
        ext_cfg = dsl["steps"][1]["integration_config"]
        assert "api_key" not in ext_cfg and ext_cfg["provider"] == "dify"
        # 子工作流按名导出
        assert dsl["steps"][2]["sub_workflow_name"] == sub.name
        # YAML 可序列化且回读一致
        text = wdsl.dumps_workflow_dsl(dsl)
        parsed = yaml.safe_load(text)
        assert parsed["steps"][2]["sub_workflow_name"] == sub.name

    def test_export_dangling_subworkflow_adds_warning(self, env):
        wf = _make_wf(env, [{"key": "a", "sub_workflow_id": 987654}])
        dsl = wdsl.export_workflow_dsl(wf)
        assert dsl["steps"][0]["sub_workflow_name"] is None
        assert any("987654" in w for w in dsl.get("warnings", []))


# ── 导入：校验 + 创建 ─────────────────────────────────────────────────


class TestImport:
    def _dsl(self, steps, version="1.0", workflow=None, **extra):
        d = {
            wdsl.DSL_MAGIC: wdsl.DSL_MARKER,
            "version": version,
            "workflow": workflow or {"name": "导入流", "description": "d", "max_parallel_steps": 2},
            "steps": steps,
        }
        d.update(extra)
        return d

    def test_import_creates_workflow_and_steps(self, env):
        dsl = self._dsl([
            {"step_key": "a", "name": "A", "required_capabilities": ["code_review"]},
            {"step_key": "b", "depends_on": ["a"], "retry_count": 2},
        ], layout={"a": {"x": 10, "y": 20}})
        wf, warnings = wdsl.import_workflow_dsl(env["user"].id, dsl)
        db.session.commit()
        assert wf.name == "导入流"
        assert wf.max_parallel_steps == 2
        assert [s.step_key for s in wf.steps] == ["a", "b"]
        assert wf.definition["layout"] == {"a": {"x": 10, "y": 20}}
        assert wf.steps[1].retry_count == 2
        assert warnings == []

    def test_import_rejects_bad_marker_and_version(self, env):
        with pytest.raises(wdsl.DslError):
            wdsl.import_workflow_dsl(env["user"].id, {"steps": []})
        with pytest.raises(wdsl.DslError, match="版本不兼容"):
            wdsl.import_workflow_dsl(env["user"].id, self._dsl(
                [{"step_key": "a"}], version="2.0"))

    def test_import_rejects_duplicate_and_unknown_dep_and_cycle(self, env):
        with pytest.raises(wdsl.DslError, match="重复"):
            wdsl.import_workflow_dsl(env["user"].id, self._dsl([
                {"step_key": "a"}, {"step_key": "a"}]))
        with pytest.raises(wdsl.DslError, match="不存在的步骤"):
            wdsl.import_workflow_dsl(env["user"].id, self._dsl([
                {"step_key": "a", "depends_on": ["ghost"]}]))
        with pytest.raises(wdsl.DslError, match="环"):
            wdsl.import_workflow_dsl(env["user"].id, self._dsl([
                {"step_key": "a", "depends_on": ["b"]},
                {"step_key": "b", "depends_on": ["a"]}]))
        with pytest.raises(wdsl.DslError, match="不存在的步骤"):
            wdsl.import_workflow_dsl(env["user"].id, self._dsl([
                {"step_key": "a",
                 "condition": {"step_key": "ghost", "operator": "succeeded"}}]))

    def test_import_resolves_subworkflow_by_name(self, env):
        sub = _make_wf(env, [{"key": "s1"}], name=f"子流_{uuid.uuid4().hex[:6]}")
        wf, _ = wdsl.import_workflow_dsl(env["user"].id, self._dsl([
            {"step_key": "a", "sub_workflow_name": sub.name}]))
        db.session.commit()
        assert wf.steps[0].sub_workflow_id == sub.id

    def test_import_rejects_missing_subworkflow_with_names(self, env):
        with pytest.raises(wdsl.DslError, match="不存在"):
            wdsl.import_workflow_dsl(env["user"].id, self._dsl([
                {"step_key": "a", "sub_workflow_name": "不存在的流"}]))

    def test_import_connector_without_api_key_is_stored_keyless(self, env):
        wf, _ = wdsl.import_workflow_dsl(env["user"].id, self._dsl([
            {"step_key": "ext",
             "integration_config": {"provider": "dify", "inputs": {"q": "x"}}}]))
        db.session.commit()
        cfg = wf.steps[0].integration_config
        assert cfg["provider"] == "dify"
        assert not cfg.get("api_key")  # 密钥留待用户在 UI 补填

    def test_import_invalid_connector_provider_rejected(self, env):
        with pytest.raises(wdsl.DslError, match="连接器配置无效"):
            wdsl.import_workflow_dsl(env["user"].id, self._dsl([
                {"step_key": "ext",
                 "integration_config": {"provider": "zapier", "inputs": {}}}]))

    def test_roundtrip_export_then_import(self, env):
        wf = _make_wf(env, [
            {"key": "a", "caps": ["code_review"]},
            {"key": "b", "depends_on": ["a"]},
        ], definition={"layout": {"a": {"x": 1, "y": 2}, "b": {"x": 3, "y": 4}}},
            name=f"源流_{uuid.uuid4().hex[:6]}")
        text = wdsl.dumps_workflow_dsl(wdsl.export_workflow_dsl(wf))
        wf2, _ = wdsl.import_workflow_dsl(
            env["user"].id, wdsl.loads_workflow_dsl(text), name_override="副本")
        db.session.commit()
        assert wf2.name == "副本"
        assert [s.step_key for s in wf2.steps] == ["a", "b"]
        assert wf2.steps[1].depends_on == ["a"]
        assert wf2.definition["layout"]["b"] == {"x": 3, "y": 4}


# ── 路由：导出/导入 HTTP 面 ───────────────────────────────────────────


class TestDslRoutes:
    def test_export_and_import_over_http(self, client, env):
        wf = _make_wf(env, [{"key": "a"}, {"key": "b", "depends_on": ["a"]}])
        resp = client.get(
            f"{BASE_URL}/agents/workflows/{wf.id}/export", headers=_headers(env["user"]))
        assert resp.status_code == 200
        dsl_text = resp.get_json()["data"]["dsl_text"]
        assert "todo_for_ai" in dsl_text

        imp = client.post(
            f"{BASE_URL}/agents/workflows/import",
            json={"dsl_text": dsl_text, "name": "HTTP 导入"},
            headers=_headers(env["user"]),
        )
        assert imp.status_code == 201, imp.get_json()
        data = imp.get_json()["data"]
        assert data["name"] == "HTTP 导入"
        assert [s["step_key"] for s in data["steps"]] == ["a", "b"]

    def test_import_bad_yaml_returns_400(self, client, env):
        resp = client.post(
            f"{BASE_URL}/agents/workflows/import",
            json={"dsl_text": "a: [::bad"},
            headers=_headers(env["user"]),
        )
        assert resp.status_code == 400

    def test_import_rejects_plain_dict(self, client, env):
        resp = client.post(
            f"{BASE_URL}/agents/workflows/import",
            json={"dsl_text": json.dumps({"name": "not dsl"})},
            headers=_headers(env["user"]),
        )
        assert resp.status_code == 400

    def test_import_requires_ownership_scoped_names(self, client, env):
        from models import User
        other = User(username=f"o_{uuid.uuid4().hex[:8]}", email=f"o_{uuid.uuid4().hex[:6]}@t.io")
        db.session.add(other)
        db.session.commit()
        sub = _make_wf(env, [{"key": "s"}], name=f"私有流_{uuid.uuid4().hex[:6]}")
        dsl = wdsl.dumps_workflow_dsl(wdsl.export_workflow_dsl(
            _make_wf(env, [{"key": "a", "sub_workflow_id": sub.id}])))
        # other 用户导入：其名下没有同名子工作流 → 400
        resp = client.post(
            f"{BASE_URL}/agents/workflows/import",
            json={"dsl_text": dsl}, headers=_headers(other),
        )
        assert resp.status_code == 400
        assert "不存在" in resp.get_json()["message"]


# ── 单步测试运行 ──────────────────────────────────────────────────────


class TestTestRun:
    def test_agent_step_preview_no_side_effects(self, client, env):
        wf = _make_wf(env, [{"key": "a", "caps": ["code_review"], "name": "审查"}])
        before = Workflow.query.count()
        resp = client.post(
            f"{BASE_URL}/agents/workflows/{wf.id}/steps/a/test-run",
            json={"instructions": "重点看登录模块"},
            headers=_headers(env["user"]),
        )
        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()["data"]
        assert data["mode"] == "agent_preview"
        assert data["agent"]["id"] == env["agent"].id
        assert data["task_preview"]["title"] == "审查"
        assert "重点看登录模块" in data["task_preview"]["content"]
        assert Workflow.query.count() == before  # 零副作用

    def test_external_step_real_call(self, client, env, monkeypatch):
        wf = _make_wf(env, [{"key": "ext", "integration_config": {
            "provider": "dify", "api_key": "k", "inputs": {"run": "{{sys.run_id}}", "q": "{{context.topic}}"}}}])
        captured = {}

        def fake_post(url, **kw):
            captured["json"] = kw.get("json")
            return _dify_ok({"answer": "42"})

        monkeypatch.setattr(wex.http_client, "post", fake_post)
        resp = client.post(
            f"{BASE_URL}/agents/workflows/{wf.id}/steps/ext/test-run",
            json={"context": {"topic": "量子"}},
            headers=_headers(env["user"]),
        )
        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()["data"]
        assert data["ok"] is True, json.dumps(data, ensure_ascii=False)
        assert data["mode"] == "external"
        assert "42" in data["output"]
        # 占位符：{{sys.run_id}} 在无运行上下文下为空串，{{context.topic}} 渲染
        assert captured["json"]["inputs"]["q"] == "量子"
        assert "run" in captured["json"]["inputs"]

    def test_external_step_error_returned_not_raised(self, client, env, monkeypatch):
        wf = _make_wf(env, [{"key": "ext", "integration_config": {
            "provider": "coze", "api_key": "k", "workflow_id": "wf-1"}}])

        class R:
            status_code = 500
            text = "boom"

            def json(self):
                raise ValueError("no json")

        monkeypatch.setattr(wex.http_client, "post", lambda url, **kw: R())
        resp = client.post(
            f"{BASE_URL}/agents/workflows/{wf.id}/steps/ext/test-run",
            json={}, headers=_headers(env["user"]),
        )
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["ok"] is False and "HTTP 500" in data["error"]

    def test_unknown_step_returns_404(self, client, env):
        wf = _make_wf(env, [{"key": "a"}])
        resp = client.post(
            f"{BASE_URL}/agents/workflows/{wf.id}/steps/ghost/test-run",
            json={}, headers=_headers(env["user"]),
        )
        assert resp.status_code == 404


# ── {{sys.*}} 占位符 ──────────────────────────────────────────────────


class TestSysPlaceholders:
    def test_sys_variables_render(self, env):
        from datetime import datetime
        from models import WorkflowRun, WorkflowStepRun, StepStatus, WorkflowStatus, Task, TaskStatus

        wf = _make_wf(env, [{"key": "a"}, {"key": "ext", "depends_on": ["a"]}])
        root = Task(project_id=env["project"].id, title="根任务", owner_id=env["user"].id,
                    is_ai_task=True, status=TaskStatus.IN_PROGRESS)
        db.session.add(root)
        db.session.commit()
        run = WorkflowRun.create(workflow_id=wf.id, project_id=env["project"].id,
                                 owner_id=env["user"].id,
                                 status=WorkflowStatus.RUNNING, root_task_id=root.id)
        db.session.flush()
        for k in ("a", "ext"):
            WorkflowStepRun.create(run_id=run.id, step_key=k, status=StepStatus.PENDING)
        db.session.commit()

        rendered = wex.render_inputs(
            {"inputs": {
                "rid": "{{sys.run_id}}",
                "wid": "{{sys.workflow_id}}",
                "wname": "{{sys.workflow_name}}",
                "title": "{{sys.root_task_title}}",
                "step": "{{sys.step_key}}",
                "unknown": "{{sys.nope}}",
            }},
            run, wf.steps[1])
        assert rendered["rid"] == str(run.id)
        assert rendered["wid"] == str(wf.id)
        assert rendered["wname"] == wf.name
        assert rendered["title"] == "根任务"
        assert rendered["step"] == "ext"
        assert rendered["unknown"] == ""  # 未知 sys.* 不透传原文


def _dify_ok(outputs):
    class R:
        status_code = 200
        text = json.dumps({"data": {"status": "succeeded", "outputs": outputs}})

        def json(self):
            return json.loads(self.text)
    return R()
