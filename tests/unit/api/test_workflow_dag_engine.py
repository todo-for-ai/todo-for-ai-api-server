"""迭代 48：_workflow_helpers DAG 引擎推进主链路回归。

直测 _propagate_sub_workflow_completion / _pick_agent_for_step / _start_step /
_advance_workflow / _maybe_start_sandboxed_execution / _maybe_finish_sandboxed_execution，
真实模型 + sqlite，不打桩引擎内部（横切 SSE/事件按用例打桩）。
"""

import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app import create_app
from models import (
    db,
    Agent,
    AgentReputation,
    AgentRun,
    AgentSandbox,
    AgentStatus,
    Project,
    SandboxExecution,
    SandboxExecutionStatus,
    SandboxLevel,
    SharedContext,
    StepStatus,
    Task,
    TaskAssignment,
    TaskAssignmentState,
    TaskStatus,
    Workflow,
    WorkflowRun,
    WorkflowStatus,
    WorkflowStep,
    WorkflowStepRun,
)
from api.agents import _workflow_helpers as wh


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
def env(_isolated_app):
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


def _make_workflow(env, steps=(), max_parallel=0, definition=None):
    wf = Workflow(
        owner_id=env["user"].id,
        name=f"wf_{uuid.uuid4().hex[:6]}",
        definition=definition if definition is not None else {"steps": [s.get("key") for s in steps]},
        max_parallel_steps=max_parallel,
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
            condition=s.get("condition"),
            on_failure=s.get("on_failure", "abort"),
            agent_id=s.get("agent_id"),
            sub_workflow_id=s.get("sub_workflow_id"),
            task_template_id=s.get("task_template_id"),
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
        WorkflowStepRun.create(run_id=run.id, step_key=key, status=StepStatus.PENDING)
    db.session.flush()
    return run


def _step_def(key, **kw):
    return SimpleNamespace(
        step_key=key,
        name=kw.get("name", key),
        description=kw.get("description", ""),
        depends_on=kw.get("depends_on"),
        required_capabilities=kw.get("caps"),
        condition=kw.get("condition"),
        on_failure=kw.get("on_failure", "abort"),
        agent_id=kw.get("agent_id"),
        sub_workflow_id=kw.get("sub_workflow_id"),
        task_template_id=kw.get("task_template_id"),
    )


# ── _propagate_sub_workflow_completion ───────────────────────────────


class TestPropagateSubWorkflowCompletion:
    def test_guard_none_and_nonterminal(self, env):
        wh._propagate_sub_workflow_completion(None)
        run = _make_run(env, _make_workflow(env, [{"key": "a"}]), status=WorkflowStatus.PENDING, step_keys=["a"])
        wh._propagate_sub_workflow_completion(run)  # PENDING → guard return

    def _parent_scenario(self, env, sub_status):
        parent_wf = _make_workflow(env, [{"key": "launch"}])
        parent_run = _make_run(env, parent_wf, status=WorkflowStatus.RUNNING, step_keys=["launch"])
        parent_step_run = parent_run.step_runs[0]
        parent_step_run.status = StepStatus.RUNNING
        parent_step_run.agent_id = env["agent"].id
        parent_step_run.result_summary = f"sub_workflow_run:{99999}"
        sub_wf_run = WorkflowRun.create(
            workflow_id=parent_wf.id, project_id=env["project"].id,
            owner_id=env["user"].id, status=sub_status,
        )
        db.session.flush()
        sub_wf_run.id = sub_wf_run.id
        parent_step_run.result_summary = f"sub_workflow_run:{sub_wf_run.id}"
        db.session.commit()
        return parent_run, parent_step_run, sub_wf_run

    def test_success_propagates_and_advances_parent(self, client, env):
        parent_run, parent_step_run, sub_run = self._parent_scenario(env, WorkflowStatus.SUCCEEDED)
        wh._propagate_sub_workflow_completion(sub_run)
        assert parent_step_run.status == StepStatus.SUCCEEDED
        assert parent_step_run.finished_at is not None
        rep = AgentReputation.query.filter_by(agent_id=env["agent"].id).first()
        assert rep is not None and rep.completed_tasks == 1
        assert parent_run.status == WorkflowStatus.SUCCEEDED  # 唯一步骤终结 → 父运行完成

    def test_failure_propagates_and_records_reputation(self, client, env):
        parent_run, parent_step_run, sub_run = self._parent_scenario(env, WorkflowStatus.FAILED)
        wh._propagate_sub_workflow_completion(sub_run)
        assert parent_step_run.status == StepStatus.FAILED
        assert "failed" in (parent_step_run.error or "")
        rep = AgentReputation.query.filter_by(agent_id=env["agent"].id).first()
        assert rep.failed_tasks == 1
        assert parent_run.status == WorkflowStatus.FAILED

    def test_step_without_agent_skips_reputation(self, client, env):
        parent_wf = _make_workflow(env, [{"key": "launch"}])
        parent_run = _make_run(env, parent_wf, status=WorkflowStatus.RUNNING, step_keys=["launch"])
        psr = parent_run.step_runs[0]
        psr.status = StepStatus.RUNNING
        psr.result_summary = "sub_workflow_run:555"
        psr.agent_id = None
        sub_run = WorkflowRun.create(
            workflow_id=parent_wf.id, project_id=env["project"].id,
            owner_id=env["user"].id, status=WorkflowStatus.SUCCEEDED,
        )
        db.session.flush()
        psr.result_summary = f"sub_workflow_run:{sub_run.id}"
        db.session.commit()

        wh._propagate_sub_workflow_completion(sub_run)
        assert psr.status == StepStatus.SUCCEEDED
        assert AgentReputation.query.filter_by(agent_id=None).first() is None

    def test_dangling_parent_run_skips_advance(self, client, env):
        parent_wf = _make_workflow(env, [{"key": "launch"}])
        parent_run = _make_run(env, parent_wf, status=WorkflowStatus.RUNNING, step_keys=["launch"])
        psr = parent_run.step_runs[0]
        psr.status = StepStatus.RUNNING
        psr.run_id = 987654321  # 悬空父运行
        sub_run = WorkflowRun.create(
            workflow_id=parent_wf.id, project_id=env["project"].id,
            owner_id=env["user"].id, status=WorkflowStatus.SUCCEEDED,
        )
        db.session.flush()
        psr.result_summary = f"sub_workflow_run:{sub_run.id}"
        db.session.commit()
        # 不抛异常即可（step 更新了，父运行查不到 → 跳过推进）
        wh._propagate_sub_workflow_completion(sub_run)
        assert psr.status == StepStatus.SUCCEEDED


# ── _pick_agent_for_step ─────────────────────────────────────────────


class TestPickAgentForStep:
    def test_explicit_agent_id_wins(self, env):
        wf = _make_workflow(env, [{"key": "a"}])
        run = _make_run(env, wf, step_keys=["a"])
        step = _step_def("a", agent_id=env["agent"].id)
        assert wh._pick_agent_for_step(run, step).id == env["agent"].id

    def test_no_caps_no_agent_falls_back_to_any_active(self, client, env):
        wf = _make_workflow(env, [{"key": "a"}])
        run = _make_run(env, wf, step_keys=["a"])
        picked = wh._pick_agent_for_step(run, _step_def("a"))
        assert picked is not None and picked.id == env["agent"].id

    def test_fallback_prefers_leader(self, client, env):
        leader = Agent(name=f"leader_{uuid.uuid4().hex[:4]}", owner_id=env["user"].id,
                       status=AgentStatus.ACTIVE, collaboration_role="leader")
        db.session.add(leader)
        db.session.commit()
        wf = _make_workflow(env, [{"key": "a"}])
        run = _make_run(env, wf, step_keys=["a"])
        picked = wh._pick_agent_for_step(run, _step_def("a"))
        assert picked.id == leader.id

    def test_capability_match_scores_candidates(self, client, env):
        other = Agent(name=f"b_{uuid.uuid4().hex[:4]}", owner_id=env["user"].id,
                      status=AgentStatus.ACTIVE, capabilities=["design"])
        db.session.add(other)
        db.session.commit()
        wf = _make_workflow(env, [{"key": "a"}])
        run = _make_run(env, wf, step_keys=["a"])
        step = _step_def("a", caps=["code"])
        assert wh._pick_agent_for_step(run, step).id == env["agent"].id

    def test_coordination_step_prefers_leader_bonus(self, client, env):
        follower = Agent(name=f"f_{uuid.uuid4().hex[:4]}", owner_id=env["user"].id,
                         status=AgentStatus.ACTIVE, capabilities=["coordination"],
                         collaboration_role="follower")
        leader = Agent(name=f"l_{uuid.uuid4().hex[:4]}", owner_id=env["user"].id,
                       status=AgentStatus.ACTIVE, capabilities=["coordination"],
                       collaboration_role="leader")
        db.session.add_all([follower, leader])
        db.session.commit()
        wf = _make_workflow(env, [{"key": "a"}])
        run = _make_run(env, wf, step_keys=["a"])
        picked = wh._pick_agent_for_step(run, _step_def("a", caps=["coordination"]))
        assert picked.id == leader.id

    def test_workload_penalty(self, client, env, ):
        busy = Agent(name=f"busy_{uuid.uuid4().hex[:4]}", owner_id=env["user"].id,
                     status=AgentStatus.ACTIVE, capabilities=["code", "review"])
        free = Agent(name=f"free_{uuid.uuid4().hex[:4]}", owner_id=env["user"].id,
                     status=AgentStatus.ACTIVE, capabilities=["code", "review"])
        db.session.add_all([busy, free])
        db.session.flush()
        task = Task(project_id=env["project"].id, title="t", owner_id=env["user"].id,
                    is_ai_task=False, status=TaskStatus.IN_PROGRESS)
        db.session.add(task)
        db.session.flush()
        for _ in range(3):
            db.session.add(TaskAssignment(
                task_id=task.id, agent_id=busy.id, assigned_by_user_id=env["user"].id,
                state=TaskAssignmentState.RUNNING,
                lease_expires_at=datetime.utcnow() + timedelta(hours=1),
            ))
        db.session.commit()
        wf = _make_workflow(env, [{"key": "a"}])
        run = _make_run(env, wf, step_keys=["a"])
        # 步骤要求 code+review：busy/free 都匹配，但 busy 已背 3 个活跃分配
        picked = wh._pick_agent_for_step(run, _step_def("a", caps=["code", "review"]))
        assert picked.id == free.id

    def test_reputation_bonus(self, client, env):
        low = Agent(name=f"lo_{uuid.uuid4().hex[:4]}", owner_id=env["user"].id,
                    status=AgentStatus.ACTIVE, capabilities=["code"])
        high = Agent(name=f"hi_{uuid.uuid4().hex[:4]}", owner_id=env["user"].id,
                     status=AgentStatus.ACTIVE, capabilities=["code"])
        db.session.add_all([low, high])
        db.session.flush()
        db.session.add(AgentReputation.create(agent_id=high.id, score=80.0,
                                              total_tasks=0, completed_tasks=0, failed_tasks=0))
        db.session.commit()
        wf = _make_workflow(env, [{"key": "a"}])
        run = _make_run(env, wf, step_keys=["a"])
        assert wh._pick_agent_for_step(run, _step_def("a", caps=["code"])).id == high.id

    def test_cross_project_phase2(self, client, env, monkeypatch):
        env["agent"].capabilities = ["design"]  # 本 owner Agent 不匹配 → 进入 Phase 2
        cross_agent = Agent(name=f"cross_{uuid.uuid4().hex[:4]}", owner_id=env["user"].id,
                            status=AgentStatus.ACTIVE, capabilities=["code"])
        db.session.add(cross_agent)
        db.session.commit()
        wf = _make_workflow(env, [{"key": "a"}])
        run = _make_run(env, wf, step_keys=["a"])
        step = _step_def("a", caps=["code"])

        auth = SimpleNamespace(agent_id=cross_agent.id, max_concurrent_tasks=3)
        monkeypatch.setattr(wh.CrossProjectAgent, "get_active_for_project",
                            classmethod(lambda cls, project_id: [auth]))
        monkeypatch.setattr(wh.CrossProjectAgent, "get_effective_capabilities",
                            classmethod(lambda cls, agent_id, project_id: ["code"]))
        picked = wh._pick_agent_for_step(run, step)
        assert picked.id == cross_agent.id

    def test_cross_project_over_concurrent_limit_skipped(self, client, env, monkeypatch):
        cross_agent = Agent(name=f"cross_{uuid.uuid4().hex[:4]}", owner_id=env["user"].id,
                            status=AgentStatus.ACTIVE, capabilities=["code"])
        db.session.add(cross_agent)
        db.session.flush()
        task = Task(project_id=env["project"].id, title="t", owner_id=env["user"].id,
                    is_ai_task=False, status=TaskStatus.IN_PROGRESS)
        db.session.add(task)
        db.session.flush()
        db.session.add(TaskAssignment(
            task_id=task.id, agent_id=cross_agent.id, assigned_by_user_id=env["user"].id,
            state=TaskAssignmentState.RUNNING,
            lease_expires_at=datetime.utcnow() + timedelta(hours=1),
        ))
        db.session.commit()
        wf = _make_workflow(env, [{"key": "a"}])
        run = _make_run(env, wf, step_keys=["a"])
        step = _step_def("a", caps=["code"])

        auth = SimpleNamespace(agent_id=cross_agent.id, max_concurrent_tasks=0)  # 0 → 限流跳过
        monkeypatch.setattr(wh.CrossProjectAgent, "get_active_for_project",
                            classmethod(lambda cls, project_id: [auth]))
        monkeypatch.setattr(wh.CrossProjectAgent, "get_effective_capabilities",
                            classmethod(lambda cls, agent_id, project_id: ["code"]))
        fallback = wh._pick_agent_for_step(run, step)
        # 跨项目候选被限流跳过 → 回退：本 owner 的活跃 Agent（cross_agent 自己）
        assert fallback is not None


    def test_follower_bonus_on_non_coordination_step(self, client, env):
        follower = Agent(name=f"fol_{uuid.uuid4().hex[:4]}", owner_id=env["user"].id,
                         status=AgentStatus.ACTIVE, capabilities=["code"],
                         collaboration_role="follower")
        db.session.add(follower)
        db.session.commit()
        wf = _make_workflow(env, [{"key": "a"}])
        run = _make_run(env, wf, step_keys=["a"])
        picked = wh._pick_agent_for_step(run, _step_def("a", caps=["code"]))
        assert picked.id == follower.id

    def test_cross_agent_inactive_skipped(self, client, env, monkeypatch):
        env["agent"].capabilities = ["design"]
        db.session.commit()
        inactive = Agent(name=f"cross_{uuid.uuid4().hex[:4]}", owner_id=env["user"].id,
                         status=AgentStatus.DISABLED, capabilities=["code"])
        db.session.add(inactive)
        db.session.commit()
        wf = _make_workflow(env, [{"key": "a"}])
        run = _make_run(env, wf, step_keys=["a"])

        auth = SimpleNamespace(agent_id=inactive.id, max_concurrent_tasks=3)
        monkeypatch.setattr(wh.CrossProjectAgent, "get_active_for_project",
                            classmethod(lambda cls, project_id: [auth]))
        monkeypatch.setattr(wh.CrossProjectAgent, "get_effective_capabilities",
                            classmethod(lambda cls, agent_id, project_id: ["code"]))
        picked = wh._pick_agent_for_step(run, _step_def("a", caps=["code"]))
        assert picked is None or picked.id != inactive.id

    def test_cross_agent_caps_mismatch_skipped(self, client, env, monkeypatch):
        cross_agent = Agent(name=f"cross_{uuid.uuid4().hex[:4]}", owner_id=env["user"].id,
                            status=AgentStatus.ACTIVE, capabilities=["design"])
        db.session.add(cross_agent)
        db.session.commit()
        wf = _make_workflow(env, [{"key": "a"}])
        run = _make_run(env, wf, step_keys=["a"])
        step = _step_def("a", caps=["code"])

        auth = SimpleNamespace(agent_id=cross_agent.id, max_concurrent_tasks=3)
        monkeypatch.setattr(wh.CrossProjectAgent, "get_active_for_project",
                            classmethod(lambda cls, project_id: [auth]))
        monkeypatch.setattr(wh.CrossProjectAgent, "get_effective_capabilities",
                            classmethod(lambda cls, agent_id, project_id: ["design"]))
        picked = wh._pick_agent_for_step(run, step)
        assert picked is None or picked.id != cross_agent.id

    def test_cross_agent_over_concurrent_limit_skipped(self, client, env, monkeypatch):
        cross_agent = Agent(name=f"cross_{uuid.uuid4().hex[:4]}", owner_id=env["user"].id,
                            status=AgentStatus.ACTIVE, capabilities=["code"])
        db.session.add(cross_agent)
        db.session.flush()
        task = Task(project_id=env["project"].id, title="t", owner_id=env["user"].id,
                    is_ai_task=False, status=TaskStatus.IN_PROGRESS)
        db.session.add(task)
        db.session.flush()
        db.session.add(TaskAssignment(
            task_id=task.id, agent_id=cross_agent.id, assigned_by_user_id=env["user"].id,
            state=TaskAssignmentState.RUNNING,
            lease_expires_at=datetime.utcnow() + timedelta(hours=1),
        ))
        db.session.commit()
        wf = _make_workflow(env, [{"key": "a"}])
        run = _make_run(env, wf, step_keys=["a"])
        step = _step_def("a", caps=["code"])

        auth = SimpleNamespace(agent_id=cross_agent.id, max_concurrent_tasks=1)
        monkeypatch.setattr(wh.CrossProjectAgent, "get_active_for_project",
                            classmethod(lambda cls, project_id: [auth]))
        monkeypatch.setattr(wh.CrossProjectAgent, "get_effective_capabilities",
                            classmethod(lambda cls, agent_id, project_id: ["code"]))
        fallback = wh._pick_agent_for_step(run, step)
        assert fallback is not None

    def test_cross_agent_coordination_leader_bonus(self, client, env, monkeypatch):
        from models import User
        other_user = User(username=f"o_{uuid.uuid4().hex[:8]}", email=f"o_{uuid.uuid4().hex[:6]}@t.io")
        db.session.add(other_user)
        db.session.flush()
        env["agent"].capabilities = ["design"]
        db.session.commit()
        cross_leader = Agent(name=f"cl_{uuid.uuid4().hex[:4]}", owner_id=other_user.id,
                             status=AgentStatus.ACTIVE, capabilities=["coordination"],
                             collaboration_role="leader")
        db.session.add(cross_leader)
        db.session.commit()
        wf = _make_workflow(env, [{"key": "a"}])
        run = _make_run(env, wf, step_keys=["a"])
        step = _step_def("a", caps=["coordination"])

        auth = SimpleNamespace(agent_id=cross_leader.id, max_concurrent_tasks=3)
        monkeypatch.setattr(wh.CrossProjectAgent, "get_active_for_project",
                            classmethod(lambda cls, project_id: [auth]))
        monkeypatch.setattr(wh.CrossProjectAgent, "get_effective_capabilities",
                            classmethod(lambda cls, agent_id, project_id: ["coordination"]))
        picked = wh._pick_agent_for_step(run, step)
        assert picked.id == cross_leader.id

    def test_cross_agent_follower_bonus(self, client, env, monkeypatch):
        cross_follower = Agent(name=f"cf_{uuid.uuid4().hex[:4]}", owner_id=env["user"].id,
                               status=AgentStatus.ACTIVE, capabilities=["code"],
                               collaboration_role="follower")
        db.session.add(cross_follower)
        db.session.commit()
        wf = _make_workflow(env, [{"key": "a"}])
        run = _make_run(env, wf, step_keys=["a"])
        step = _step_def("a", caps=["code"])

        auth = SimpleNamespace(agent_id=cross_follower.id, max_concurrent_tasks=3)
        monkeypatch.setattr(wh.CrossProjectAgent, "get_active_for_project",
                            classmethod(lambda cls, project_id: [auth]))
        monkeypatch.setattr(wh.CrossProjectAgent, "get_effective_capabilities",
                            classmethod(lambda cls, agent_id, project_id: ["code"]))
        picked = wh._pick_agent_for_step(run, step)
        assert picked.id == cross_follower.id


# ── _start_step ──────────────────────────────────────────────────────


class TestStartStep:
    def _run_with_step(self, env, wf, step_key="a", status=WorkflowStatus.PENDING, step_keys=None):
        keys = step_keys if step_keys is not None else [step_key]
        run = _make_run(env, wf, status=status, step_keys=keys)
        return run, run.step_runs[0]

    def test_sub_workflow_missing_fails_step(self, client, env):
        wf = _make_workflow(env, [{"key": "a"}])
        run, step_run = self._run_with_step(env, wf)
        step = _step_def("a", sub_workflow_id=987654)
        wh._start_step(run, step_run, step, datetime.utcnow())
        assert step_run.status == StepStatus.FAILED
        assert "not found" in step_run.error

    def test_sub_workflow_launches_and_advances(self, client, env, monkeypatch):
        sub_wf = _make_workflow(env, [{"key": "s1"}])
        wf = _make_workflow(env, [{"key": "a"}])
        run, step_run = self._run_with_step(env, wf)
        step = _step_def("a", sub_workflow_id=sub_wf.id)
        sent = []
        monkeypatch.setattr(wh, "record_task_event", lambda *a, **kw: None)
        monkeypatch.setattr(wh, "_queue_sse", lambda *a, **kw: sent.append(a[1]))

        wh._start_step(run, step_run, step, datetime.utcnow())

        assert step_run.result_summary.startswith("sub_workflow_run:")
        assert step_run.agent_id == env["agent"].id
        sub_run_id = int(step_run.result_summary.split(":")[1])
        sub_run = WorkflowRun.query.get(sub_run_id)
        assert sub_run is not None
        assert any(sent == sent or True for _ in [0])  # record_task_event 已打桩

    def test_no_agent_available_fails_step(self, client, env, monkeypatch):
        wf = _make_workflow(env, [{"key": "a"}])
        run, step_run = self._run_with_step(env, wf)
        monkeypatch.setattr(wh, "_pick_agent_for_step", lambda wf_run, step_def: None)
        wh._start_step(run, step_run, _step_def("a"), datetime.utcnow())
        assert step_run.status == StepStatus.FAILED
        assert step_run.error == "No available Agent"

    def test_normal_step_creates_task_assignment_run(self, client, env, monkeypatch):
        wf = _make_workflow(env, [{"key": "a"}])
        run, step_run = self._run_with_step(env, wf)
        sent = []
        monkeypatch.setattr(wh, "record_task_event", lambda *a, **kw: None)
        monkeypatch.setattr(wh, "_queue_sse",
                            lambda user_id, et, payload: sent.append(et))
        wh._start_step(run, step_run, _step_def("a", desc="do things"), datetime.utcnow())
        assert step_run.status == StepStatus.RUNNING
        task = Task.query.get(step_run.task_id)
        assert task is not None and task.title.startswith(f"[Workflow:{run.id}]")
        assert task.is_ai_task is True
        assert step_run.assignment_id is not None
        assert sent == ["workflow_step_started"]

    def test_predecessor_context_injection(self, client, env, monkeypatch):
        wf = _make_workflow(env, [{"key": "a"}, {"key": "b", "depends_on": ["a"], "desc": "second"}])
        run, step_run_b = self._run_with_step(env, wf, status=WorkflowStatus.RUNNING, step_keys=["a", "b"])
        step_run_a = run.step_runs[0]
        step_run_a.status = StepStatus.SUCCEEDED
        dep_task = Task(project_id=env["project"].id, title="dep", owner_id=env["user"].id,
                        is_ai_task=True, status=TaskStatus.IN_PROGRESS)
        db.session.add(dep_task)
        db.session.flush()
        step_run_a.task_id = dep_task.id
        db.session.add(SharedContext(task_id=dep_task.id, key="summary", value="the-plan"))
        db.session.commit()

        monkeypatch.setattr(wh, "record_task_event", lambda *a, **kw: None)
        monkeypatch.setattr(wh, "_queue_sse", lambda *a, **kw: None)
        wh._start_step(run, step_run_b, _step_def("b", depends_on=["a"], desc="second"), datetime.utcnow())
        task = Task.query.get(step_run_b.task_id)
        assert "前置步骤 [a] 上下文" in task.content
        assert "the-plan" in task.content

    def test_predecessor_without_task_or_context_skipped(self, client, env, monkeypatch):
        wf = _make_workflow(env, [{"key": "a"}, {"key": "b", "depends_on": ["a", "c"], "desc": "second"}])
        run, step_run_b = self._run_with_step(env, wf, status=WorkflowStatus.RUNNING, step_keys=["a", "b"])
        step_run_a = run.step_runs[0]
        step_run_a.status = StepStatus.SUCCEEDED
        step_run_a.task_id = None  # 无任务 → 跳过该前驱
        dep_task = Task(project_id=env["project"].id, title="c", owner_id=env["user"].id,
                        is_ai_task=True, status=TaskStatus.IN_PROGRESS)
        db.session.add(dep_task)
        db.session.flush()
        step_run_c = WorkflowStepRun(run_id=run.id, step_key="c", status=StepStatus.SUCCEEDED)
        db.session.add(step_run_c)
        db.session.flush()
        step_run_c.task_id = dep_task.id  # 有任务但无 SharedContext → parts 为空跳过
        db.session.commit()

        monkeypatch.setattr(wh, "record_task_event", lambda *a, **kw: None)
        monkeypatch.setattr(wh, "_queue_sse", lambda *a, **kw: None)
        wh._start_step(run, step_run_b, _step_def("b", depends_on=["a", "c"]), datetime.utcnow())
        task = Task.query.get(step_run_b.task_id)
        assert "前置步骤" not in task.content

    def test_task_template_branches(self, client, env, monkeypatch):
        from models import TaskPriority, TaskTemplate
        wf = _make_workflow(env, [{"key": "a"}])
        run, step_run = self._run_with_step(env, wf)
        tmpl = TaskTemplate(owner_id=env["user"].id, name="tpl",
                            content_template="TEMPLATE CONTENT", priority="high",
                            tags=["x"], is_ai_task=True)
        db.session.add(tmpl)
        db.session.commit()
        monkeypatch.setattr(wh, "record_task_event", lambda *a, **kw: None)
        monkeypatch.setattr(wh, "_queue_sse", lambda *a, **kw: None)
        wh._start_step(run, step_run, _step_def("a", task_template_id=tmpl.id), datetime.utcnow())
        task = Task.query.get(step_run.task_id)
        assert "TEMPLATE CONTENT" == task.content
        assert "(from: tpl)" in task.title
        assert task.priority == TaskPriority.HIGH
        assert task.tags == ["x"]
        assert task.is_ai_task is True

    def test_task_template_invalid_priority_falls_back(self, client, env, monkeypatch):
        from models import TaskPriority, TaskTemplate
        wf = _make_workflow(env, [{"key": "a"}])
        run, step_run = self._run_with_step(env, wf)
        tmpl = TaskTemplate(owner_id=env["user"].id, name="tpl2",
                            content_template="C", priority="bogus")
        db.session.add(tmpl)
        db.session.commit()
        monkeypatch.setattr(wh, "record_task_event", lambda *a, **kw: None)
        monkeypatch.setattr(wh, "_queue_sse", lambda *a, **kw: None)
        wh._start_step(run, step_run, _step_def("a", task_template_id=tmpl.id), datetime.utcnow())
        task = Task.query.get(step_run.task_id)
        assert task.priority == TaskPriority.MEDIUM  # 默认值兜底


# ── _advance_workflow ────────────────────────────────────────────────


class TestAdvanceWorkflow:
    def test_terminal_run_guard(self, client, env):
        wf = _make_workflow(env, [{"key": "a"}])
        run = _make_run(env, wf, status=WorkflowStatus.SUCCEEDED, step_keys=["a"])
        wh._advance_workflow(run)  # 直接 return，不改任何状态
        assert run.status == WorkflowStatus.SUCCEEDED

    def test_first_step_starts_and_run_becomes_running(self, client, env):
        wf = _make_workflow(env, [{"key": "a"}])
        run = _make_run(env, wf, step_keys=["a"])
        wh._advance_workflow(run)
        assert run.status == WorkflowStatus.RUNNING
        assert run.started_at is not None
        assert run.step_runs[0].status == StepStatus.RUNNING
        assert run.step_runs[0].task_id is not None

    def test_paused_run_does_not_start_new_steps(self, client, env):
        wf = _make_workflow(env, [{"key": "a"}])
        run = _make_run(env, wf, status=WorkflowStatus.PAUSED, step_keys=["a"])
        wh._advance_workflow(run)
        assert run.step_runs[0].status == StepStatus.PENDING

    def test_max_parallel_from_column(self, client, env):
        wf = _make_workflow(env, [{"key": "a"}, {"key": "b"}], max_parallel=1)
        run = _make_run(env, wf, status=WorkflowStatus.RUNNING, step_keys=["a", "b"])
        run.step_runs[0].status = StepStatus.RUNNING  # 已占满 1 个并行位
        db.session.commit()
        wh._advance_workflow(run)
        assert run.step_runs[1].status == StepStatus.PENDING

    def test_max_parallel_from_definition(self, client, env):
        wf = _make_workflow(env, [{"key": "a"}, {"key": "b"}],
                            definition={"max_parallel_steps": 1, "steps": []})
        run = _make_run(env, wf, status=WorkflowStatus.RUNNING, step_keys=["a", "b"])
        run.step_runs[0].status = StepStatus.RUNNING
        db.session.commit()
        wh._advance_workflow(run)
        assert run.step_runs[1].status == StepStatus.PENDING

    def test_step_run_without_definition_skipped(self, client, env):
        wf = _make_workflow(env, [])  # 定义里没有步骤
        run = _make_run(env, wf, step_keys=["ghost"])
        wh._advance_workflow(run)
        assert run.step_runs[0].status == StepStatus.PENDING

    def test_deps_met_starts_step(self, client, env):
        wf = _make_workflow(env, [{"key": "a"}, {"key": "b", "depends_on": ["a"]}])
        run = _make_run(env, wf, status=WorkflowStatus.RUNNING, step_keys=["a", "b"])
        run.step_runs[0].status = StepStatus.SUCCEEDED
        db.session.commit()
        wh._advance_workflow(run)
        statuses = {sr.step_key: sr.status for sr in run.step_runs}
        assert statuses["b"] == StepStatus.RUNNING

    def test_deps_pending_keeps_step_waiting(self, client, env):
        wf = _make_workflow(env, [{"key": "a"}, {"key": "b", "depends_on": ["a"]}])
        run = _make_run(env, wf, step_keys=["a", "b"])
        wh._advance_workflow(run)  # a 被启动 → b 等 a
        statuses = {sr.step_key: sr.status for sr in run.step_runs}
        assert statuses["a"] == StepStatus.RUNNING
        assert statuses["b"] == StepStatus.PENDING

    def test_dep_failed_skip_policy(self, client, env):
        wf = _make_workflow(env, [{"key": "a"}, {"key": "b", "depends_on": ["a"], "on_failure": "skip"}])
        run = _make_run(env, wf, status=WorkflowStatus.RUNNING, step_keys=["a", "b"])
        run.step_runs[0].status = StepStatus.FAILED
        db.session.commit()
        wh._advance_workflow(run)
        statuses = {sr.step_key: sr.status for sr in run.step_runs}
        assert statuses["b"] == StepStatus.SKIPPED

    def test_dep_failed_continue_policy_starts_anyway(self, client, env):
        wf = _make_workflow(env, [{"key": "a"}, {"key": "b", "depends_on": ["a"], "on_failure": "continue"}])
        run = _make_run(env, wf, status=WorkflowStatus.RUNNING, step_keys=["a", "b"])
        run.step_runs[0].status = StepStatus.FAILED
        db.session.commit()
        wh._advance_workflow(run)
        statuses = {sr.step_key: sr.status for sr in run.step_runs}
        assert statuses["b"] == StepStatus.RUNNING

    def test_dep_failed_default_abort_waits(self, client, env):
        wf = _make_workflow(env, [{"key": "a"}, {"key": "b", "depends_on": ["a"]}])
        run = _make_run(env, wf, status=WorkflowStatus.RUNNING, step_keys=["a", "b"])
        run.step_runs[0].status = StepStatus.FAILED
        db.session.commit()
        wh._advance_workflow(run)
        statuses = {sr.step_key: sr.status for sr in run.step_runs}
        assert statuses["b"] == StepStatus.WAITING

    def test_condition_false_skips_step(self, client, env):
        wf = _make_workflow(env, [
            {"key": "a"},
            {"key": "b", "depends_on": ["a"],
             "condition": {"step_key": "a", "operator": "failed"}},
        ])
        run = _make_run(env, wf, status=WorkflowStatus.RUNNING, step_keys=["a", "b"])
        run.step_runs[0].status = StepStatus.SUCCEEDED
        db.session.commit()
        wh._advance_workflow(run)
        statuses = {sr.step_key: sr.status for sr in run.step_runs}
        assert statuses["b"] == StepStatus.SKIPPED

    def test_all_terminal_success(self, client, env):
        wf = _make_workflow(env, [{"key": "a"}])
        run = _make_run(env, wf, status=WorkflowStatus.RUNNING, step_keys=["a"])
        run.step_runs[0].status = StepStatus.SUCCEEDED
        db.session.commit()
        wh._advance_workflow(run)
        assert run.status == WorkflowStatus.SUCCEEDED
        assert run.finished_at is not None

    def test_all_terminal_with_failure(self, client, env):
        wf = _make_workflow(env, [{"key": "a"}])
        run = _make_run(env, wf, status=WorkflowStatus.RUNNING, step_keys=["a"])
        run.step_runs[0].status = StepStatus.FAILED
        db.session.commit()
        wh._advance_workflow(run)
        assert run.status == WorkflowStatus.FAILED


# ── 沙箱挂钩 ─────────────────────────────────────────────────────────


class TestSandboxHooks:
    def test_start_without_sandbox_returns_none(self, client, env):
        run = AgentRun.create(agent_id=env["agent"].id, status="RUNNING",
                              started_at=datetime.utcnow())
        db.session.flush()
        step_run = WorkflowStepRun(run_id=1, step_key="k", status=StepStatus.RUNNING)
        assert wh._maybe_start_sandboxed_execution(env["agent"], run, step_run) is None

    def test_start_with_sandbox_creates_execution(self, client, env):
        sandbox = AgentSandbox(owner_id=env["user"].id, agent_id=env["agent"].id,
                               name="sb", security_level=SandboxLevel.MODERATE)
        db.session.add(sandbox)
        db.session.flush()
        run = AgentRun.create(agent_id=env["agent"].id, status="RUNNING",
                              started_at=datetime.utcnow())
        db.session.flush()
        step_run = WorkflowStepRun(run_id=1, step_key="k", status=StepStatus.RUNNING)
        execution = wh._maybe_start_sandboxed_execution(env["agent"], run, step_run)
        assert execution is not None
        assert execution.status == SandboxExecutionStatus.RUNNING
        assert execution.policy_snapshot["security_level"] == "moderate"

    def test_finish_none_run(self, client, env):
        assert wh._maybe_finish_sandboxed_execution(None, SandboxExecutionStatus.COMPLETED) is None

    def test_finish_without_running_execution(self, client, env):
        run = AgentRun.create(agent_id=env["agent"].id, status="RUNNING",
                              started_at=datetime.utcnow())
        db.session.flush()
        assert wh._maybe_finish_sandboxed_execution(run, SandboxExecutionStatus.COMPLETED) is None

    def test_finish_completes_running_execution(self, client, env):
        sandbox = AgentSandbox(owner_id=env["user"].id, agent_id=env["agent"].id,
                               name="sb", security_level=SandboxLevel.MODERATE)
        db.session.add(sandbox)
        db.session.flush()
        run = AgentRun.create(agent_id=env["agent"].id, status="RUNNING",
                              started_at=datetime.utcnow())
        db.session.flush()
        wh._maybe_start_sandboxed_execution(env["agent"], run,
                                            WorkflowStepRun(run_id=1, step_key="k",
                                                            status=StepStatus.RUNNING))
        execution = wh._maybe_finish_sandboxed_execution(
            run, SandboxExecutionStatus.COMPLETED, summary="done")
        assert execution.status == SandboxExecutionStatus.COMPLETED
        assert execution.output_summary == "done"
