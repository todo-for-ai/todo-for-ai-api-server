"""工作流执行闭环 + 外部平台连接器（Dify / Coze）回归。

两件事：
1. 执行闭环——步骤任务经 runtime commit / 人工置 DONE / 评审通过到终态时，
   maybe_autocomplete_for_task 自动完成对应 WorkflowStepRun 并推进 DAG。
   此前该回调只能由人经 workflow-runs API 手动触发，真实 Agent 执行的
   步骤会永远停在 RUNNING。
2. 连接器——integration_config 步骤由平台直接调用 Dify / Coze 工作流 API
   执行，不再创建 Agent 任务；api_key 密文入库、API 响应脱敏。
"""

import json
import os
import uuid
from datetime import datetime, timedelta

import pytest

# 连接器把 api_key 加密入库；套件其余加密相关测试共用同一测试密钥
os.environ.setdefault(
    "SECRET_ENCRYPTION_KEY", "uCuDTIUbpnE0Z47hrUqyNY8w7SjtIwKxnvZTduXeN30="
)

from app import create_app
from models import (
    db,
    Agent,
    AgentStatus,
    Project,
    SharedContext,
    StepStatus,
    Task,
    TaskStatus,
    Workflow,
    WorkflowRun,
    WorkflowStatus,
    WorkflowStep,
    WorkflowStepRun,
)
from api.agents import _workflow_helpers as wh
from api.agents import workflow_completion as wcomp
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
    """模块级 db_session——工厂夹具必须与本文件的内存库同一引擎。"""
    from models import db
    with _isolated_app.app_context():
        yield db.session
    db.session.rollback()


@pytest.fixture
def env(_isolated_app, db_session):
    from models import Organization, User

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


def _make_workflow(env, steps=(), definition=None):
    wf = Workflow(
        owner_id=env["user"].id,
        name=f"wf_{uuid.uuid4().hex[:6]}",
        definition=definition if definition is not None else {
            # 与路由/模板约定一致：definition.steps 是 dict 列表
            "steps": [{"step_key": s.get("key"), "depends_on": s.get("depends_on") or []}
                      for s in steps],
        },
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
            on_failure=s.get("on_failure", "abort"),
            retry_count=s.get("retry_count", 0),
            agent_id=s.get("agent_id"),
            sub_workflow_id=s.get("sub_workflow_id"),
            integration_config=s.get("integration_config"),
            description=s.get("desc"),
        ))
    db.session.commit()
    return wf


def _make_run(env, wf, status=WorkflowStatus.PENDING, step_keys=(), root_task_id=None):
    run = WorkflowRun.create(
        workflow_id=wf.id,
        project_id=env["project"].id,
        owner_id=env["user"].id,
        status=status,
        root_task_id=root_task_id,
    )
    db.session.flush()
    for key in step_keys:
        WorkflowStepRun.create(run_id=run.id, step_key=key, status=StepStatus.PENDING, attempt=1)
    db.session.flush()
    return run


def _root_task(env):
    t = Task(project_id=env["project"].id, title="root", owner_id=env["user"].id,
             is_ai_task=True, status=TaskStatus.IN_PROGRESS)
    db.session.add(t)
    db.session.commit()
    return t


# ── 执行闭环：maybe_autocomplete_for_task ────────────────────────────


class TestAutocompleteForTask:
    def _running_step_with_task(self, env, wf, task, step_key="a"):
        run = _make_run(env, wf, status=WorkflowStatus.RUNNING, step_keys=[step_key])
        sr = run.step_runs[0]
        sr.status = StepStatus.RUNNING
        sr.started_at = datetime.utcnow()
        sr.agent_id = env["agent"].id
        sr.task_id = task.id
        db.session.commit()
        return run, sr

    def test_success_completes_step_and_writes_context(self, env):
        wf = _make_workflow(env, [{"key": "a", "caps": ["code"]}])
        task = _root_task(env)
        run, sr = self._running_step_with_task(env, wf, task)

        done = wcomp.maybe_autocomplete_for_task(
            task.id, success=True, result_summary="all-green", source="agent_commit")

        assert done is not None and done.status == StepStatus.SUCCEEDED
        entry = SharedContext.query.filter_by(task_id=task.id, key="step_result_a").first()
        assert entry is not None and entry.value == "all-green"
        assert run.status == WorkflowStatus.SUCCEEDED

    def test_success_advances_downstream_step(self, env):
        wf = _make_workflow(env, [
            {"key": "a", "caps": ["code"]},
            {"key": "b", "depends_on": ["a"]},
        ])
        task = _root_task(env)
        run = _make_run(env, wf, status=WorkflowStatus.RUNNING, step_keys=["a", "b"])
        sr_a, sr_b = run.step_runs
        sr_a.status = StepStatus.RUNNING
        sr_a.started_at = datetime.utcnow()
        sr_a.agent_id = env["agent"].id
        sr_a.task_id = task.id
        db.session.commit()

        wcomp.maybe_autocomplete_for_task(task.id, success=True, result_summary="ok")

        assert sr_a.status == StepStatus.SUCCEEDED
        assert sr_b.status == StepStatus.RUNNING
        assert sr_b.task_id is not None  # 下游步骤任务已自动创建
        assert run.status == WorkflowStatus.RUNNING

    def test_failure_marks_step_failed(self, env):
        wf = _make_workflow(env, [{"key": "a"}])
        task = _root_task(env)
        run, sr = self._running_step_with_task(env, wf, task)

        wcomp.maybe_autocomplete_for_task(task.id, success=False, error="boom")

        assert sr.status == StepStatus.FAILED
        assert "boom" in sr.error
        assert run.status == WorkflowStatus.FAILED

    def test_failure_with_retry_redispatches_step(self, env):
        wf = _make_workflow(env, [{"key": "a", "retry_count": 1}])
        task = _root_task(env)
        run, sr = self._running_step_with_task(env, wf, task)
        old_task_id = task.id

        wcomp.maybe_autocomplete_for_task(task.id, success=False, error="boom")

        # 重试立即重新派发：旧任务解绑，新任务挂到步骤上
        assert sr.status == StepStatus.RUNNING
        assert sr.attempt == 2
        assert sr.task_id is not None and sr.task_id != old_task_id

    def test_ignores_terminal_step_and_unbound_task(self, env):
        wf = _make_workflow(env, [{"key": "a"}])
        task = _root_task(env)
        run, sr = self._running_step_with_task(env, wf, task)
        sr.status = StepStatus.SUCCEEDED
        db.session.commit()

        assert wcomp.maybe_autocomplete_for_task(task.id, success=True) is None
        assert wcomp.maybe_autocomplete_for_task(None, success=True) is None
        assert wcomp.maybe_autocomplete_for_task(task.id + 999999, success=True) is None
        assert sr.status == StepStatus.SUCCEEDED  # 不被二次改写


class TestCommitClosesLoop:
    """端到端：Agent 经 runtime 协议提交步骤任务 → 步骤自动完成 → DAG 推进。"""

    @pytest.fixture
    def runtime(self, client, env, db_session, user_factory, organization_factory, agent_factory):
        from models import AgentKey

        key_row, raw_key = AgentKey.generate_key(
            name=f"Key {uuid.uuid4().hex[:6]}", workspace_id=env["org"].id,
            agent_id=env["agent"].id, created_by_user_id=env["user"].id,
        )
        db.session.add(key_row)
        db.session.commit()
        resp = client.post(f"{BASE_URL}/agent/auth/introspect", json={"agent_key": raw_key})
        assert resp.status_code == 200
        token = resp.get_json()["data"]["access_token"]
        return {"headers": {"Authorization": f"Bearer {token}"}}

    def _attempt_and_lease(self, agent, task):
        from models import AgentTaskAttempt, AgentTaskAttemptState, AgentTaskLease

        attempt_id = f"att_{uuid.uuid4().hex[:8]}"
        lease_id = f"lea_{uuid.uuid4().hex[:8]}"
        db.session.add(AgentTaskAttempt(
            attempt_id=attempt_id, task_id=task.id, agent_id=agent.id,
            workspace_id=agent.workspace_id, state=AgentTaskAttemptState.ABORTED,
            lease_id=lease_id,
            started_at=datetime.utcnow() - timedelta(seconds=60),
            ended_at=datetime.utcnow(), created_by="test",
        ))
        db.session.add(AgentTaskLease(
            lease_id=lease_id, task_id=task.id, attempt_id=attempt_id,
            agent_id=agent.id, workspace_id=agent.workspace_id,
            expires_at=datetime.utcnow() + timedelta(seconds=300), active=True, created_by="test",
        ))
        db.session.commit()
        return attempt_id, lease_id

    def test_commit_success_completes_step_and_starts_next(self, client, env, runtime):
        wf = _make_workflow(env, [
            {"key": "a", "caps": ["code"]},
            {"key": "b", "depends_on": ["a"]},
        ])
        run = _make_run(env, wf, status=WorkflowStatus.RUNNING, step_keys=["a", "b"],
                        root_task_id=_root_task(env).id)
        wh._advance_workflow(run)
        db.session.commit()
        sr_a = run.step_runs[0]
        task_a = Task.query.get(sr_a.task_id)
        assert task_a is not None and sr_a.status == StepStatus.RUNNING

        attempt_id, lease_id = self._attempt_and_lease(env["agent"], task_a)
        resp = client.post(
            f"{BASE_URL}/agent/tasks/{task_a.id}/commit",
            json={"attempt_id": attempt_id, "lease_id": lease_id,
                  "status": "succeeded", "result": {"output": "step-a-done"}},
            headers=runtime["headers"],
        )
        assert resp.status_code == 200, resp.get_json()

        db.session.expire_all()
        sr_a, sr_b = run.step_runs
        assert sr_a.status == StepStatus.SUCCEEDED
        entry = SharedContext.query.filter_by(task_id=task_a.id, key="step_result_a").first()
        assert entry is not None and entry.value == "step-a-done"
        assert sr_b.status == StepStatus.RUNNING and sr_b.task_id is not None

    def test_commit_failure_fails_step(self, client, env, runtime):
        wf = _make_workflow(env, [{"key": "a", "caps": ["code"]}])
        run = _make_run(env, wf, status=WorkflowStatus.RUNNING, step_keys=["a"],
                        root_task_id=_root_task(env).id)
        wh._advance_workflow(run)
        db.session.commit()
        task_a = Task.query.get(run.step_runs[0].task_id)
        attempt_id, lease_id = self._attempt_and_lease(env["agent"], task_a)

        resp = client.post(
            f"{BASE_URL}/agent/tasks/{task_a.id}/commit",
            json={"attempt_id": attempt_id, "lease_id": lease_id,
                  "status": "failed", "failure_reason": "tests broke"},
            headers=runtime["headers"],
        )
        assert resp.status_code == 200, resp.get_json()

        db.session.expire_all()
        assert run.step_runs[0].status == StepStatus.FAILED
        assert "tests broke" in run.step_runs[0].error


# ── 外部平台连接器（Dify / Coze） ─────────────────────────────────────


class TestIntegrationConfigNormalization:
    def test_plaintext_key_encrypted_at_rest(self):
        cfg = wex.normalize_incoming_integration_config(
            {"provider": "dify", "api_key": "app-plain"})
        assert cfg["api_key"] != "app-plain"
        assert self.decrypt_ok(cfg["api_key"], "app-plain")

    def decrypt_ok(self, cipher, expected):
        from services.github_app import decrypt_str
        return decrypt_str(cipher) == expected

    def test_masked_roundtrip_reuses_previous_cipher(self):
        prev = wex.normalize_incoming_integration_config(
            {"provider": "dify", "api_key": "app-secret"})
        cfg = wex.normalize_incoming_integration_config(
            {"provider": "dify", "api_key": "••••cret", "base_url": "https://x"}, previous=prev)
        assert cfg["api_key"] == prev["api_key"]  # 旧密文原样沿用
        assert cfg["base_url"] == "https://x"

    def test_invalid_provider_rejected(self):
        with pytest.raises(ValueError):
            wex.normalize_incoming_integration_config({"provider": "zapier", "api_key": "k"})

    def test_empty_returns_none(self):
        assert wex.normalize_incoming_integration_config(None) is None
        assert wex.normalize_incoming_integration_config({}) is None

    def test_coze_requires_workflow_id(self):
        with pytest.raises(ValueError):
            wex.normalize_incoming_integration_config(
                {"provider": "coze", "api_key": "pat-x"})

    def test_to_dict_masks_api_key(self, env):
        wf = _make_workflow(env, [{"key": "a", "integration_config":
                                   {"provider": "dify", "api_key": "app-plain-key"}}])
        step = wf.steps[0]
        pub = step.to_dict()["integration_config"]
        assert pub["api_key"].startswith("••••")
        assert pub["api_key_set"] is True
        assert "app-plain-key" not in json.dumps(pub)


class TestRenderInputs:
    def test_placeholders_resolved(self, env):
        wf = _make_workflow(env, [{"key": "a"}, {"key": "b", "depends_on": ["a"]}])
        root = _root_task(env)
        run = _make_run(env, wf, status=WorkflowStatus.RUNNING,
                        step_keys=["a", "b"], root_task_id=root.id)
        sr_a = run.step_runs[0]
        sr_a.status = StepStatus.SUCCEEDED
        sr_a.task_id = root.id
        db.session.add(SharedContext(task_id=root.id, key="step_result_a", value="RESEARCH-OUT"))
        run.context = {"topic": "quantum"}
        db.session.commit()

        step_b = wf.steps[1]
        config = {"inputs": {
            "upstream": "{{step_result_a}}",
            "topic": "{{context.topic}}",
            "title": "{{root_task_title}}",
            "run": "{{run_id}}",
            "key": "{{step_key}}",
            "unknown": "{{nope}}",
        }}
        rendered = wex.render_inputs(config, run, step_b)
        assert rendered["upstream"] == "RESEARCH-OUT"
        assert rendered["topic"] == "quantum"
        assert rendered["title"] == "root"
        assert rendered["run"] == str(run.id)
        assert rendered["key"] == "b"
        assert rendered["unknown"] == ""  # 未知占位符替换为空串


def _dify_response(status="succeeded", outputs=None, http=200):
    class R:
        status_code = http
        text = json.dumps({"data": {"status": status, "outputs": outputs or {}}})

        def json(self):
            return json.loads(self.text)
    return R()


def _coze_response(code=0, data="[1,2]", http=200):
    class R:
        status_code = http
        text = json.dumps({"code": code, "data": data, "msg": "ok" if code == 0 else "bad"})

        def json(self):
            return json.loads(self.text)
    return R()


class TestExternalStepDispatch:
    def _external_wf(self, env, provider="dify", retry_count=0):
        cfg = {"provider": provider, "api_key": "k-test",
               "inputs": {"q": "{{step_key}}"}}
        if provider == "coze":
            cfg["workflow_id"] = "wf-123"
        return _make_workflow(env, [
            {"key": "ext", "integration_config": cfg, "retry_count": retry_count},
            {"key": "after", "depends_on": ["ext"]},
        ])

    def _run_and_start(self, env, wf):
        run = _make_run(env, wf, status=WorkflowStatus.PENDING,
                        step_keys=["ext", "after"], root_task_id=_root_task(env).id)
        wh._advance_workflow(run)  # TESTING=True → 同步执行外呼
        db.session.commit()
        db.session.expire_all()
        return run

    def test_dify_success_advances_dag(self, env, monkeypatch):
        monkeypatch.setattr(wex.http_client, "post",
                            lambda url, **kw: _dify_response(outputs={"answer": "42"}))
        wf = self._external_wf(env)
        run = self._run_and_start(env, wf)

        sr_ext, sr_after = run.step_runs
        assert sr_ext.status == StepStatus.SUCCEEDED
        assert "42" in (sr_ext.result_summary or "")
        assert sr_ext.task_id is None  # 外部步骤不创建 Agent 任务
        assert sr_after.status == StepStatus.RUNNING and sr_after.task_id is not None
        assert run.status == WorkflowStatus.RUNNING

    def test_dify_remote_status_failure(self, env, monkeypatch):
        monkeypatch.setattr(wex.http_client, "post",
                            lambda url, **kw: _dify_response(status="failed"))
        run = self._run_and_start(env, self._external_wf(env))
        assert run.step_runs[0].status == StepStatus.FAILED
        # abort 策略：下游步骤 WAITING（非终态），run 保持 RUNNING
        assert run.step_runs[1].status == StepStatus.WAITING
        assert run.status == WorkflowStatus.RUNNING

    def test_dify_failure_with_retry_exhausts_then_fails(self, env, monkeypatch):
        monkeypatch.setattr(wex.http_client, "post",
                            lambda url, **kw: _dify_response(status="failed"))
        run = self._run_and_start(env, self._external_wf(env, retry_count=1))
        sr = run.step_runs[0]
        # 同步派发下重试立即执行：第 1 次失败→重试（attempt=2）→再失败→终态 FAILED
        assert sr.status == StepStatus.FAILED
        assert sr.attempt == 2

    def test_http_error_marks_failed(self, env, monkeypatch):
        monkeypatch.setattr(wex.http_client, "post",
                            lambda url, **kw: _dify_response(http=500))
        run = self._run_and_start(env, self._external_wf(env))
        sr = run.step_runs[0]
        assert sr.status == StepStatus.FAILED
        assert "HTTP 500" in sr.error

    def test_coze_success_parses_data(self, env, monkeypatch):
        captured = {}

        def fake_post(url, **kw):
            captured["url"] = url
            captured["payload"] = kw.get("json")
            return _coze_response(data='{"output": "done"}')

        monkeypatch.setattr(wex.http_client, "post", fake_post)
        run = self._run_and_start(env, self._external_wf(env, provider="coze"))

        sr = run.step_runs[0]
        assert sr.status == StepStatus.SUCCEEDED
        assert "done" in sr.result_summary
        assert captured["url"].endswith("/v1/workflow/run")
        assert captured["payload"]["workflow_id"] == "wf-123"
        assert captured["payload"]["parameters"]["q"] == "ext"  # 占位符已渲染

    def test_coze_error_code_marks_failed(self, env, monkeypatch):
        monkeypatch.setattr(wex.http_client, "post",
                            lambda url, **kw: _coze_response(code=4001, data="", http=200))
        run = self._run_and_start(env, self._external_wf(env, provider="coze"))
        assert run.step_runs[0].status == StepStatus.FAILED
        assert "4001" in run.step_runs[0].error

    def test_invalid_provider_config_falls_back_to_agent_task(self, env):
        # 直接写模型绕过路由校验：provider 非法 → 当普通 Agent 步骤执行
        wf = _make_workflow(env, [{"key": "ext",
                                   "integration_config": {"provider": "zapier", "api_key": "k"}}])
        run = _make_run(env, wf, step_keys=["ext"], root_task_id=_root_task(env).id)
        wh._advance_workflow(run)
        db.session.commit()
        assert run.step_runs[0].task_id is not None  # 走了 Agent 任务路径


# ── 路由层：integration_config 校验 + 版本快照 ────────────────────────


class TestWorkflowRoutesIntegration:
    def _headers(self, user):
        from flask_jwt_extended import create_access_token
        return {"Authorization": f"Bearer {create_access_token(identity=str(user.id))}"}

    def test_create_rejects_bad_provider(self, client, env):
        resp = client.post(
            f"{BASE_URL}/agents/workflows",
            json={"name": "wf", "steps": [
                {"step_key": "a", "integration_config": {"provider": "zapier", "api_key": "k"}}]},
            headers=self._headers(env["user"]),
        )
        assert resp.status_code == 400

    def test_create_with_dify_config_and_masked_readback(self, client, env):
        resp = client.post(
            f"{BASE_URL}/agents/workflows",
            json={"name": "wf-dify", "steps": [
                {"step_key": "a", "integration_config":
                    {"provider": "dify", "api_key": "app-live-key", "inputs": {"x": "1"}}}]},
            headers=self._headers(env["user"]),
        )
        assert resp.status_code == 201, resp.get_json()
        wf = resp.get_json()["data"]
        cfg = wf["steps"][0]["integration_config"]
        assert cfg["api_key"].startswith("••••") and cfg["api_key_set"] is True
        assert "app-live-key" not in json.dumps(resp.get_json())

        # PUT 时回传脱敏值 → 密文沿用（解密后仍是原明文）
        put = client.put(
            f"{BASE_URL}/agents/workflows/{wf['id']}",
            json={"steps": [
                {"step_key": "a", "integration_config":
                    {"provider": "dify", "api_key": cfg["api_key"]}}]},
            headers=self._headers(env["user"]),
        )
        assert put.status_code == 200, put.get_json()
        stored = WorkflowStep.query.filter_by(workflow_id=wf["id"], step_key="a").first()
        from services.github_app import decrypt_str
        assert decrypt_str(stored.integration_config["api_key"]) == "app-live-key"


# ── 列表端点回归：主干上这批 GET 从未工作过 ───────────────────────────


class TestWorkflowListEndpoints:
    """GET /agents/workflows 与 /agents/workflow-runs 曾有三连 bug：
    dict.get(type=) TypeError、paginate_query(query, args) 传 dict、
    ApiResponse.paginated 不存在、分页后再 to_dict 双重序列化。"""

    def _headers(self, user):
        from flask_jwt_extended import create_access_token
        return {"Authorization": f"Bearer {create_access_token(identity=str(user.id))}"}

    def test_list_workflows_returns_steps_and_masked_config(self, client, env):
        wf = _make_workflow(env, [
            {"key": "a"},
            {"key": "ext", "integration_config": {"provider": "dify", "api_key": "app-list-key"}},
        ])
        resp = client.get(
            f"{BASE_URL}/agents/workflows?per_page=100",
            headers=self._headers(env["user"]),
        )
        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()["data"]
        assert "items" in data and "pagination" in data
        mine = next(w for w in data["items"] if w["id"] == wf.id)
        assert [s["step_key"] for s in mine["steps"]] == ["a", "ext"]
        cfg = mine["steps"][1]["integration_config"]
        assert cfg["api_key"].startswith("••••") and cfg["api_key_set"] is True

    def test_list_workflows_is_active_filter(self, client, env):
        wf = _make_workflow(env, [{"key": "a"}])
        client.put(
            f"{BASE_URL}/agents/workflows/{wf.id}",
            json={"is_active": False},
            headers=self._headers(env["user"]),
        )
        active = client.get(
            f"{BASE_URL}/agents/workflows?is_active=true", headers=self._headers(env["user"]))
        inactive = client.get(
            f"{BASE_URL}/agents/workflows?is_active=false", headers=self._headers(env["user"]))
        assert active.status_code == 200 and inactive.status_code == 200
        assert all(w["is_active"] for w in active.get_json()["data"]["items"])
        assert all(not w["is_active"] for w in inactive.get_json()["data"]["items"])

    def test_list_workflow_runs_pagination_shape(self, client, env):
        wf = _make_workflow(env, [{"key": "a"}])
        _make_run(env, wf, step_keys=["a"])
        resp = client.get(
            f"{BASE_URL}/agents/workflow-runs?per_page=10&workflow_id={wf.id}",
            headers=self._headers(env["user"]),
        )
        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()["data"]
        assert len(data["items"]) == 1
        assert data["items"][0]["workflow_id"] == wf.id
        assert "step_runs" in data["items"][0]

    def test_audit_logs_list_ok(self, client, env):
        resp = client.get(
            f"{BASE_URL}/agents/audit-logs?per_page=10",
            headers=self._headers(env["user"]),
        )
        assert resp.status_code == 200, resp.get_json()
        assert "items" in resp.get_json()["data"]
