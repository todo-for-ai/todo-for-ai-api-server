"""迭代 49：_dispatch_helpers.py（派发策略与分配辅助）全分支回归。

直测：交接取消、认领分配+运行创建、派发策略归一化（全字段/边界/归属校验）、
coordinator 策略读取、可认领任务收集、可用 worker 挑选、候选序列化。
"""

import uuid
from datetime import datetime, timedelta

import pytest

from app import create_app
from models import (
    db,
    Agent,
    AgentKind,
    AgentRun,
    AgentRunStatus,
    AgentStatus,
    Project,
    Task,
    TaskAssignment,
    TaskAssignmentState,
    TaskPriority,
    TaskStatus,
)
from api.agents import _dispatch_helpers as dh


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
    )
    db.session.add(agent)
    db.session.commit()
    return {"user": user, "org": org, "project": project, "agent": agent}


def _make_task(env, status=TaskStatus.TODO, priority=TaskPriority.MEDIUM):
    task = Task(
        title=f"t_{uuid.uuid4().hex[:6]}",
        project_id=env["project"].id,
        owner_id=env["user"].id,
        is_ai_task=True,
        status=status,
        priority=priority,
    )
    db.session.add(task)
    db.session.commit()
    return task


def _make_assignment(env, task, state=TaskAssignmentState.CLAIMED):
    assignment = TaskAssignment(
        task_id=task.id,
        agent_id=env["agent"].id,
        assigned_by_user_id=env["user"].id,
        state=state,
        lease_expires_at=datetime.utcnow() + timedelta(hours=1),
        claimed_at=datetime.utcnow(),
        last_heartbeat_at=datetime.utcnow(),
        progress_rate=0,
        created_by=env["user"].email,
    )
    db.session.add(assignment)
    db.session.commit()
    return assignment


def _make_agent(env, name, **kw):
    agent = Agent(name=name, owner_id=env["user"].id,
                  creator_user_id=env["user"].id, status=AgentStatus.ACTIVE, **kw)
    db.session.add(agent)
    db.session.commit()
    return agent


# ── cancel_assignment_for_handoff ────────────────────────────────────


class TestCancelAssignmentForHandoff:
    def _assignment_with_run(self, env, run_status=AgentRunStatus.RUNNING):
        task = _make_task(env, TaskStatus.IN_PROGRESS)
        assignment = _make_assignment(env, task)
        run = AgentRun.create(
            task_id=task.id, agent_id=env["agent"].id,
            assignment_id=assignment.id,
            status=run_status, started_at=datetime.utcnow(),
        )
        db.session.flush()
        return assignment, run

    def test_cancels_running_run(self, client, env):
        now = datetime.utcnow()
        assignment, run = self._assignment_with_run(env)
        result = dh.cancel_assignment_for_handoff(assignment, now)
        assert result is run
        assert assignment.state == TaskAssignmentState.CANCELLED
        assert assignment.completed_at == now
        assert run.status == AgentRunStatus.CANCELLED
        assert run.ended_at == now

    def test_waiting_human_run_also_cancelled(self, client, env):
        assignment, run = self._assignment_with_run(env, run_status=AgentRunStatus.WAITING_HUMAN)
        wh_now = datetime.utcnow()
        dh.cancel_assignment_for_handoff(assignment, wh_now)
        assert run.status == AgentRunStatus.CANCELLED

    def test_terminal_run_left_alone(self, client, env):
        assignment, run = self._assignment_with_run(env, run_status=AgentRunStatus.SUCCEEDED)
        result = dh.cancel_assignment_for_handoff(assignment, datetime.utcnow())
        assert result is run
        assert run.status == AgentRunStatus.SUCCEEDED
        assert assignment.state == TaskAssignmentState.CANCELLED

    def test_no_run_returns_none(self, client, env):
        assignment = _make_assignment(env, _make_task(env))
        assert dh.cancel_assignment_for_handoff(assignment, datetime.utcnow()) is None


# ── create_assignment_with_run ───────────────────────────────────────


class TestCreateAssignmentWithRun:
    def test_creates_claimed_assignment_and_running_run(self, client, env):
        task = _make_task(env)
        now = datetime.utcnow()
        assignment, run = dh.create_assignment_with_run(
            task, env["agent"], env["user"], now, lease_seconds=600,
            run_metadata={"claim_mode": "manual"},
        )
        db.session.flush()
        assert assignment.state == TaskAssignmentState.CLAIMED
        assert assignment.lease_expires_at == now + timedelta(seconds=600)
        assert run.status == AgentRunStatus.RUNNING
        assert run.assignment_id == assignment.id
        assert run.task_id == task.id

    def test_offline_agent_wakes_active(self, client, env):
        from models import AgentStatus
        env["agent"].status = AgentStatus.OFFLINE
        db.session.commit()
        now = datetime.utcnow()
        _, _run = dh.create_assignment_with_run(_make_task(env), env["agent"],
                                                env["user"], now, 1800, {})
        assert env["agent"].status == AgentStatus.ACTIVE
        assert env["agent"].last_seen_at == now


# ── normalize_dispatch_policy ────────────────────────────────────────


class TestNormalizeDispatchPolicy:
    def test_none_defaults(self):
        policy = dh.normalize_dispatch_policy(None)
        assert policy["auto_dispatch_enabled"] is False
        assert policy["project_id"] is None
        assert policy["max_assignments"] == 20
        assert policy["lease_seconds"] == 1800
        assert policy["match_capabilities"] is True
        assert policy["require_capability_match"] is False
        assert policy["candidate_agent_ids"] == []
        assert policy["include_self"] is False

    def test_non_dict_rejected(self):
        import pytest as _pytest
        with _pytest.raises(ValueError, match="must be an object"):
            dh.normalize_dispatch_policy("nope")

    def test_auto_dispatch_enabled(self):
        assert dh.normalize_dispatch_policy({"auto_dispatch_enabled": True})["auto_dispatch_enabled"] is True

    def test_project_id_variants(self, client, env):
        assert dh.normalize_dispatch_policy({"project_id": ""})["project_id"] is None
        assert dh.normalize_dispatch_policy({"project_id": None})["project_id"] is None
        assert dh.normalize_dispatch_policy({"project_id": "12"})["project_id"] == 12
        import pytest as _pytest
        with _pytest.raises(ValueError, match="positive integer"):
            dh.normalize_dispatch_policy({"project_id": -1})
        with _pytest.raises(ValueError, match="positive integer"):
            dh.normalize_dispatch_policy({"project_id": 0})

    def test_project_ownership_checked_with_user(self, client, env):
        import pytest as _pytest
        with _pytest.raises(ValueError, match="does not belong"):
            dh.normalize_dispatch_policy({"project_id": 987654}, current_user=env["user"])
        assert dh.normalize_dispatch_policy(
            {"project_id": env["project"].id}, current_user=env["user"])["project_id"] == env["project"].id

    def test_max_assignments_clamped(self):
        assert dh.normalize_dispatch_policy({})["max_assignments"] == 20
        # falsy（None/0）回退到默认值 20——引擎语义：0 视为未提供
        assert dh.normalize_dispatch_policy({"max_assignments": None})["max_assignments"] == 20
        assert dh.normalize_dispatch_policy({"max_assignments": 0})["max_assignments"] == 20
        assert dh.normalize_dispatch_policy({"max_assignments": 100})["max_assignments"] == 20
        assert dh.normalize_dispatch_policy({"max_assignments": 5})["max_assignments"] == 5

    def test_lease_seconds_clamped(self):
        assert dh.normalize_dispatch_policy({"lease_seconds": None})["lease_seconds"] == 1800
        assert dh.normalize_dispatch_policy({"lease_seconds": 10})["lease_seconds"] == 60
        assert dh.normalize_dispatch_policy({"lease_seconds": 999999})["lease_seconds"] == 86400

    def test_capability_flags(self):
        p = dh.normalize_dispatch_policy({"match_capabilities": False})
        assert p["match_capabilities"] is False
        p = dh.normalize_dispatch_policy({"require_capability_match": True})
        assert p["require_capability_match"] is True
        # match_capabilities=False 时 require 被强制关闭
        p = dh.normalize_dispatch_policy({"match_capabilities": False, "require_capability_match": True})
        assert p["require_capability_match"] is False

    def test_include_self(self):
        assert dh.normalize_dispatch_policy({"include_self": True})["include_self"] is True

    def test_candidate_ids_variants(self):
        assert dh.normalize_dispatch_policy({"candidate_agent_ids": None})["candidate_agent_ids"] == []
        assert dh.normalize_dispatch_policy({"candidate_agent_ids": ""})["candidate_agent_ids"] == []
        p = dh.normalize_dispatch_policy({"candidate_agent_ids": [3, 1, 3, "2"]})
        assert p["candidate_agent_ids"] == [3, 1, 2]
        import pytest as _pytest
        with _pytest.raises(ValueError, match="list of agent ids"):
            dh.normalize_dispatch_policy({"candidate_agent_ids": "nope"})
        with _pytest.raises(ValueError, match="positive integers"):
            dh.normalize_dispatch_policy({"candidate_agent_ids": [0]})

    def test_candidate_ids_ownership_checked(self, client, env):
        import pytest as _pytest
        owned = _make_agent(env, f"own_{uuid.uuid4().hex[:4]}")
        p = dh.normalize_dispatch_policy(
            {"candidate_agent_ids": [owned.id]}, current_user=env["user"])
        assert p["candidate_agent_ids"] == [owned.id]
        with _pytest.raises(ValueError, match="belong to current user"):
            dh.normalize_dispatch_policy(
                {"candidate_agent_ids": [owned.id, 987654321]}, current_user=env["user"])

    def test_match_off_forces_require_off(self):
        p = dh.normalize_dispatch_policy({
            "match_capabilities": False, "require_capability_match": True})
        assert p["require_capability_match"] is False


# ── get_coordinator_dispatch_policy / resolve_dispatch_options ───────


class TestCoordinatorPolicy:
    def test_config_none_returns_defaults(self, client, env):
        coordinator = _make_agent(env, f"c_{uuid.uuid4().hex[:4]}")
        coordinator.config = None
        db.session.commit()
        policy = dh.get_coordinator_dispatch_policy(coordinator, current_user=env["user"])
        assert policy["auto_dispatch_enabled"] is False

    def test_config_non_dict_returns_defaults(self, client, env):
        coordinator = _make_agent(env, f"c_{uuid.uuid4().hex[:4]}")
        coordinator.config = "not-a-dict"
        db.session.commit()
        policy = dh.get_coordinator_dispatch_policy(coordinator, current_user=env["user"])
        assert policy["max_assignments"] == 20

    def test_stored_policy_normalized(self, client, env):
        coordinator = _make_agent(env, f"c_{uuid.uuid4().hex[:4]}")
        coordinator.config = {"dispatch_policy": {"auto_dispatch_enabled": True,
                                                  "max_assignments": 7}}
        db.session.commit()
        policy = dh.get_coordinator_dispatch_policy(coordinator, current_user=env["user"])
        assert policy["auto_dispatch_enabled"] is True
        assert policy["max_assignments"] == 7

    def test_resolve_merges_overrides(self, client, env):
        coordinator = _make_agent(env, f"c_{uuid.uuid4().hex[:4]}")
        coordinator.config = {"dispatch_policy": {"max_assignments": 5,
                                                  "lease_seconds": 900}}
        db.session.commit()
        resolved, stored = dh.resolve_dispatch_options(
            coordinator, {"max_assignments": 12, "lease_seconds": 600},
            current_user=env["user"])
        assert resolved["max_assignments"] == 12
        assert resolved["lease_seconds"] == 600
        assert stored["max_assignments"] == 5
        assert stored["lease_seconds"] == 900


# ── collect_claimable_tasks ──────────────────────────────────────────


class TestCollectClaimableTasks:
    def test_priority_order_and_excludes_terminal(self, client, env):
        high = _make_task(env, TaskStatus.TODO, priority=TaskPriority.HIGH)
        low = _make_task(env, TaskStatus.IN_PROGRESS, priority=TaskPriority.LOW)
        mid = _make_task(env, TaskStatus.REVIEW, priority=TaskPriority.MEDIUM)
        done = _make_task(env, TaskStatus.DONE)  # 终态排除
        cancelled = _make_task(env, TaskStatus.CANCELLED)

        claimable = dh.collect_claimable_tasks(env["user"])
        ids = [t.id for t in claimable]
        # 钉住当前行为：终态（DONE/CANCELLED）被过滤；
        # 注意 priority 为枚举名落库（HIGH/LOW/MEDIUM），desc 字母序与
        # 业务优先级语义不一致（HIGH 反而最后）——排序语义修正属行为
        # 变更，另行裁决（见 QUALITY_PLAN 迭代 49 观察项）。
        assert set(ids) == {high.id, mid.id, low.id}
        assert done.id not in ids and cancelled.id not in ids

    def test_busy_task_skipped(self, client, env):
        task = _make_task(env, TaskStatus.TODO)
        _make_assignment(env, task)  # 活跃分配 → 不可认领
        claimable = dh.collect_claimable_tasks(env["user"])
        assert all(t.id != task.id for t in claimable)

    def test_project_filter(self, client, env):
        task = _make_task(env)
        other_proj = Project(name=f"p2_{uuid.uuid4().hex[:6]}", owner_id=env["user"].id)
        db.session.add(other_proj)
        db.session.flush()
        other = Task(project_id=other_proj.id, title="other", owner_id=env["user"].id,
                     is_ai_task=True, status=TaskStatus.TODO)
        db.session.add(other)
        db.session.commit()

        ids = [t.id for t in dh.collect_claimable_tasks(env["user"], project_id=env["project"].id)]
        assert task.id in ids and other.id not in ids


# ── find_available_worker_agents ─────────────────────────────────────


class TestFindAvailableWorkerAgents:
    def test_excludes_self_and_other_coordinators(self, client, env):
        coordinator = _make_agent(env, f"coord_{uuid.uuid4().hex[:4]}",
                                  kind=AgentKind.COORDINATOR)
        worker = _make_agent(env, f"worker_{uuid.uuid4().hex[:4]}")
        other_coord = _make_agent(env, f"ocoord_{uuid.uuid4().hex[:4]}",
                                  kind=AgentKind.COORDINATOR)

        available = dh.find_available_worker_agents(env["user"], coordinator)
        ids = {a.id for a in available}
        assert coordinator.id not in ids
        assert other_coord.id not in ids
        assert worker.id in ids

    def test_include_self(self, client, env):
        coordinator = _make_agent(env, f"coord_{uuid.uuid4().hex[:4]}",
                                  kind=AgentKind.COORDINATOR)
        available = dh.find_available_worker_agents(env["user"], coordinator, include_self=True)
        assert coordinator.id in {a.id for a in available}

    def test_busy_worker_skipped(self, client, env):
        worker = _make_agent(env, f"busy_{uuid.uuid4().hex[:4]}")
        task = _make_task(env, TaskStatus.IN_PROGRESS)
        db.session.add(TaskAssignment(
            task_id=task.id, agent_id=worker.id, assigned_by_user_id=env["user"].id,
            state=TaskAssignmentState.RUNNING,
            lease_expires_at=datetime.utcnow() + timedelta(hours=1),
        ))
        db.session.commit()
        coordinator = _make_agent(env, f"c_{uuid.uuid4().hex[:4]}")
        available = dh.find_available_worker_agents(env["user"], coordinator)
        assert worker.id not in {a.id for a in available}

    def test_candidate_agent_ids_filter(self, client, env):
        wanted = _make_agent(env, f"w1_{uuid.uuid4().hex[:4]}")
        _make_agent(env, f"w2_{uuid.uuid4().hex[:4]}")
        coordinator = _make_agent(env, f"c_{uuid.uuid4().hex[:4]}")
        available = dh.find_available_worker_agents(env["user"], coordinator,
                                                    candidate_agent_ids=[wanted.id])
        assert [a.id for a in available] == [wanted.id]

    def test_inactive_excluded(self, client, env):
        inactive = _make_agent(env, f"off_{uuid.uuid4().hex[:4]}")
        inactive.status = AgentStatus.OFFLINE
        db.session.commit()
        coordinator = _make_agent(env, f"c_{uuid.uuid4().hex[:4]}")
        available = dh.find_available_worker_agents(env["user"], coordinator)
        assert inactive.id not in {a.id for a in available}


# ── serialize_dispatch_candidate ─────────────────────────────────────


class TestSerializeDispatchCandidate:
    def _worker(self):
        return Agent(name=f"w_{uuid.uuid4().hex[:4]}", status=AgentStatus.ACTIVE)

    def _match(self, score=5, **extra):
        match = {"score": score, "matched_capabilities": ["code"],
                 "matched_tags": ["api"], "matched_text": ["fix bug"],
                 "missing_required": ["devops"], "experience_bonus": 3}
        match.update(extra)
        return match

    def test_capability_match_strategy(self, client, env):
        worker = self._worker()
        data = dh.serialize_dispatch_candidate(worker, self._match(score=5))
        assert data["strategy"] == "capability_match"
        assert data["score"] == 5
        assert data["matched_capabilities"] == ["code"]
        assert data["experience_bonus"] == 3

    def test_zero_score_falls_back_to_fifo(self, client, env):
        data = dh.serialize_dispatch_candidate(self._worker(), self._match(score=0))
        assert data["strategy"] == "priority_fifo"
        assert data["matched_tags"] == ["api"]

    def test_match_capabilities_false_forces_fifo(self, client, env):
        data = dh.serialize_dispatch_candidate(self._worker(), self._match(score=5),
                                               match_capabilities=False)
        assert data["strategy"] == "priority_fifo"
