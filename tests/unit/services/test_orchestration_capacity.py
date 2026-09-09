"""多 Agent 编排：并发容量门禁 + 预算接线 + 负载均衡选 Agent 回归测试。

- 工作区设置 max_concurrent_agents（0=不限，NULL=系统默认 5）；
- 「同时干活」按未过期活跃租约的 distinct agent 计数；
- auto_assign_task / goal_loop 派发前过 窗口 → 预算 → 容量 三道门，
  岗位匹配内按活跃租约数升序挑执行者（负载均衡）。

每个测试用独立 in-memory app，避免会话级共享库的 id 复用污染。
"""

import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app import create_app
from models import db


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


class FakeController:
    MAX_PODS_PER_WORKSPACE = 10
    POD_IDLE_TIMEOUT_MINUTES = 30


# ────────────────────────── 造数助手 ──────────────────────────

def _unique(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _make_user_org(**org_kwargs):
    from models import Organization, User
    user = User(username=_unique("u"), email=f"{_unique('u')}@t.io")
    db.session.add(user)
    db.session.flush()
    org = Organization(name=_unique("o"), slug=_unique("o"), owner_id=user.id,
                       **org_kwargs)
    db.session.add(org)
    db.session.commit()
    return user, org


def _make_agent(org, role_template=None, working_schedule=None, **kwargs):
    from models import Agent
    agent = Agent(name=_unique("a"), workspace_id=org.id,
                  runner_enabled=True, role_template=role_template,
                  working_schedule=working_schedule, **kwargs)
    db.session.add(agent)
    db.session.commit()
    return agent


def _make_project(user, org):
    from models import Project
    project = Project(name=_unique("p"), owner_id=user.id,
                      organization_id=org.id, status="ACTIVE")
    db.session.add(project)
    db.session.commit()
    return project


def _make_task(user, project):
    from models import Task
    task = Task(title=_unique("t"), content="x", project_id=project.id,
                owner_id=org_id_of(project), is_ai_task=True, status="TODO")
    db.session.add(task)
    db.session.commit()
    return task


def org_id_of(project):
    return project.organization_id


def _lease(agent, task_id, expires_in=600):
    from api.agent_common import generate_id, now_utc
    from models import AgentTaskLease
    now = now_utc()
    lease = AgentTaskLease(
        lease_id=generate_id("lea"), task_id=task_id, attempt_id=generate_id("att"),
        agent_id=agent.id, workspace_id=agent.workspace_id,
        expires_at=now + timedelta(seconds=expires_in), active=True,
        created_by="test",
    )
    db.session.add(lease)
    db.session.commit()
    return lease


# ────────────────────────── 设置读取 ──────────────────────────

def test_settings_default_includes_max_concurrent_agents():
    from services.workspace_runtime_policy import (
        DEFAULT_MAX_CONCURRENT_AGENTS, get_workspace_runtime_setting,
    )
    user, org = _make_user_org()
    setting = get_workspace_runtime_setting(FakeController(), org.id)
    assert setting["max_concurrent_agents"] == DEFAULT_MAX_CONCURRENT_AGENTS


def test_settings_row_overrides_max_concurrent_agents():
    from services.workspace_runtime_policy import (
        get_workspace_runtime_setting, set_workspace_runtime_setting,
    )
    user, org = _make_user_org()
    set_workspace_runtime_setting(org.id, max_concurrent_agents=0)  # 0=不限
    assert get_workspace_runtime_setting(FakeController(), org.id)["max_concurrent_agents"] == 0

    set_workspace_runtime_setting(org.id, max_concurrent_agents=3)
    setting = get_workspace_runtime_setting(FakeController(), org.id)
    assert setting["max_concurrent_agents"] == 3
    assert setting["max_pods"] == 10  # 其他字段不受影响


def test_config_override_for_default_concurrency(monkeypatch):
    from core.config import Config
    from services.workspace_runtime_policy import get_workspace_runtime_setting
    user, org = _make_user_org()
    monkeypatch.setattr(Config, "ORCHESTRATION_MAX_CONCURRENT_AGENTS", 7, raising=False)
    assert get_workspace_runtime_setting(FakeController(), org.id)["max_concurrent_agents"] == 7


# ────────────────────────── 容量门禁 ──────────────────────────

def test_active_agent_counts_distinct_agents_with_unexpired_leases():
    from services.workspace_runtime_policy import active_agent_count
    user, org = _make_user_org()
    a1 = _make_agent(org)
    a2 = _make_agent(org)
    assert active_agent_count(org.id) == 0

    _lease(a1, task_id=101)
    assert active_agent_count(org.id) == 1

    _lease(a2, task_id=102)
    assert active_agent_count(org.id) == 2

    # 同一 Agent 的第二个任务不增加「干活 Agent 数」
    _lease(a1, task_id=103)
    assert active_agent_count(org.id) == 2


def test_check_dispatch_capacity_semantics():
    from services.workspace_runtime_policy import (
        check_dispatch_capacity, set_workspace_runtime_setting,
    )
    user, org = _make_user_org()
    a1 = _make_agent(org)
    a2 = _make_agent(org)

    # 不限（0）
    set_workspace_runtime_setting(org.id, max_concurrent_agents=0)
    assert check_dispatch_capacity(org.id, a2.id)["allowed"] is True

    # 上限 2，当前 0 个在干 → 放行
    set_workspace_runtime_setting(org.id, max_concurrent_agents=2)
    result = check_dispatch_capacity(org.id, a1.id)
    assert result == {"limit": 2, "active_agents": 0, "allowed": True, "reason": None}

    # a1、a2 都在干活（2/2）→ 新并发被挡，但已在岗者继续放行
    _lease(a1, task_id=201)
    _lease(a2, task_id=202)
    blocked = check_dispatch_capacity(org.id, a1.id + 100000)  # 不存在的第三方
    assert blocked["allowed"] is False and blocked["reason"] == "WORKSPACE_AGENT_CONCURRENCY_LIMIT"
    assert check_dispatch_capacity(org.id, a1.id)["allowed"] is True
    assert check_dispatch_capacity(org.id, a2.id)["allowed"] is True


# ────────────────────────── auto_assign_task 接线 ──────────────────────────

def test_auto_assign_spreads_tasks_across_agents():
    """负载均衡：第一个任务给 id 较小者，其后其持有租约 → 下一个任务路由给空闲者。"""
    from services.agent_runtime_controller import AgentRuntimeController
    user, org = _make_user_org()
    a1 = _make_agent(org)
    a2 = _make_agent(org)
    assert a1.id < a2.id
    project = _make_project(user, org)

    task1 = _make_task(user, project)
    AgentRuntimeController.auto_assign_task(task1)
    task2 = _make_task(user, project)
    AgentRuntimeController.auto_assign_task(task2)

    from models import AgentTaskLease, TaskStatus
    leases = AgentTaskLease.query.filter(
        AgentTaskLease.task_id.in_([task1.id, task2.id])).all()
    assigned = {l.agent_id for l in leases}
    assert assigned == {a1.id, a2.id}  # 两个任务摊到了两个 Agent
    db.session.expire_all()
    assert task1.status == TaskStatus.IN_PROGRESS
    assert task2.status == TaskStatus.IN_PROGRESS


def test_auto_assign_skipped_when_workspace_at_capacity():
    from services.agent_runtime_controller import AgentRuntimeController
    from services.workspace_runtime_policy import set_workspace_runtime_setting
    user, org = _make_user_org()
    busy = _make_agent(org)
    candidate = _make_agent(org)
    _lease(busy, task_id=301)  # busy 占掉唯一名额
    set_workspace_runtime_setting(org.id, max_concurrent_agents=1)
    # busy 不在候选集（runner_enabled=False），候选只剩 candidate → 被容量门挡下
    busy.runner_enabled = False
    db.session.commit()

    project = _make_project(user, org)
    task = _make_task(user, project)
    AgentRuntimeController.auto_assign_task(task)

    from models import AgentTaskLease, TaskStatus
    assert AgentTaskLease.query.filter_by(agent_id=candidate.id).count() == 0
    db.session.expire_all()
    assert task.status == TaskStatus.TODO


def test_auto_assign_allows_already_working_agent_at_capacity():
    from services.agent_runtime_controller import AgentRuntimeController
    from services.workspace_runtime_policy import set_workspace_runtime_setting
    user, org = _make_user_org()
    working = _make_agent(org)
    _lease(working, task_id=401)
    set_workspace_runtime_setting(org.id, max_concurrent_agents=1)

    project = _make_project(user, org)
    task = _make_task(user, project)
    AgentRuntimeController.auto_assign_task(task)  # 已在岗者可继续接

    from models import AgentTaskLease
    assert AgentTaskLease.query.filter_by(agent_id=working.id, task_id=task.id).count() == 1


def test_auto_assign_respects_token_budget(monkeypatch):
    from services.agent_runtime_controller import AgentRuntimeController
    user, org = _make_user_org()
    agent = _make_agent(org)
    project = _make_project(user, org)
    task = _make_task(user, project)

    import services.budget_service as budget_service
    monkeypatch.setattr(budget_service, "check_budgets",
                        lambda **kwargs: [{"resource": "tokens", "scope_type": "workspace"}])
    raised = {}
    monkeypatch.setattr(budget_service, "raise_budget_exceeded",
                        lambda **kwargs: raised.setdefault("violations", kwargs["violations"]))

    AgentRuntimeController.auto_assign_task(task)

    from models import AgentTaskLease, TaskStatus
    assert AgentTaskLease.query.filter_by(agent_id=agent.id).count() == 0
    assert raised["violations"][0]["resource"] == "tokens"
    db.session.expire_all()
    assert task.status == TaskStatus.TODO


# ────────────────────────── API：设置 + 活跃水位 ──────────────────────────

def test_runtime_settings_api_roundtrip_with_orchestration(client):
    from flask_jwt_extended import create_access_token
    user, org = _make_user_org()
    headers = {"Authorization": f"Bearer {create_access_token(identity=str(user.id))}"}
    base = f"/todo-for-ai/api/v1/workspaces/{org.id}/runtime/settings"

    resp = client.get(base, headers=headers)
    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()["data"]
    assert data["settings"]["max_concurrent_agents"] == 5
    assert data["orchestration"] == {"active_agents": 0}

    resp = client.put(base, headers=headers,
                      json={"max_concurrent_agents": 2, "max_pods": 4})
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["data"]["settings"]["max_concurrent_agents"] == 2

    resp = client.put(base, headers=headers, json={"max_concurrent_agents": 201})
    assert resp.status_code == 400

    resp = client.put(base, headers=headers, json={"max_concurrent_agents": "abc"})
    assert resp.status_code == 400


# ────────────────────────── goal_loop 编排拆分接线 ──────────────────────────

def _loop_stub(org, bound_agent):
    return SimpleNamespace(workspace_id=org.id, agent=bound_agent)


def test_goal_loop_executor_pool_filters_out_of_window_agents():
    from services.goal_loop.dispatch import executor_pool
    never = {'enabled': True, 'timezone': 'UTC',
             'includes': [{'type': 'dates', 'start_date': '2020-01-01',
                           'end_date': '2020-01-02'}]}
    user, org = _make_user_org()
    awake = _make_agent(org)
    sleeping = _make_agent(org, working_schedule=never)
    pool = executor_pool(_loop_stub(org, awake))
    assert [a.id for a in pool] == [awake.id]
    assert sleeping.id not in [a.id for a in pool]


def test_goal_loop_pick_executor_load_balances_within_role():
    from services.goal_loop.dispatch import pick_executor
    from services.workspace_runtime_policy import agent_active_lease_count
    from models import AgentRoleTemplate

    user, org = _make_user_org()
    template = AgentRoleTemplate(name=_unique("dev"), display_name="开发",
                                 category="engineering", is_builtin=True,
                                 status="ACTIVE", created_by_user_id=user.id)
    db.session.add(template)
    db.session.commit()
    idle = _make_agent(org, role_template=template)
    busy = _make_agent(org, role_template=template)
    assert busy.id > idle.id
    _lease(busy, task_id=501)
    _lease(busy, task_id=502)

    step = {"title": "写代码", "role": "开发"}
    picked = pick_executor(_loop_stub(org, busy), step)
    assert picked.id == idle.id
    assert agent_active_lease_count(idle.id) == 0


def test_goal_loop_pick_executor_falls_back_to_bound_agent():
    from services.goal_loop.dispatch import pick_executor
    user, org = _make_user_org()
    bound = _make_agent(org)  # 无岗位模板，任何 role 都匹配不上
    picked = pick_executor(_loop_stub(org, bound), {"title": "x", "role": "测试"})
    assert picked.id == bound.id


def test_goal_loop_assign_blocked_at_capacity_leaves_task_todo():
    from services.goal_loop.dispatch import assign_task_to_agent
    from services.workspace_runtime_policy import set_workspace_runtime_setting
    user, org = _make_user_org()
    executor = _make_agent(org)
    other = _make_agent(org)
    _lease(other, task_id=601)
    set_workspace_runtime_setting(org.id, max_concurrent_agents=1)

    project = _make_project(user, org)
    task = _make_task(user, project)
    assign_task_to_agent(task, executor)

    from models import AgentTaskLease, TaskStatus
    assert AgentTaskLease.query.filter_by(agent_id=executor.id).count() == 0
    db.session.expire_all()
    assert task.status == TaskStatus.TODO


def test_goal_loop_assign_respects_budget(monkeypatch):
    from services.goal_loop.dispatch import assign_task_to_agent
    user, org = _make_user_org()
    executor = _make_agent(org)
    project = _make_project(user, org)
    task = _make_task(user, project)

    import services.budget_service as budget_service
    monkeypatch.setattr(budget_service, "check_budgets",
                        lambda **kwargs: [{"resource": "duration_minutes"}])
    monkeypatch.setattr(budget_service, "raise_budget_exceeded", lambda **kwargs: None)

    assign_task_to_agent(task, executor)

    from models import AgentTaskLease, TaskStatus
    assert AgentTaskLease.query.filter_by(agent_id=executor.id).count() == 0
    db.session.expire_all()
    assert task.status == TaskStatus.TODO
