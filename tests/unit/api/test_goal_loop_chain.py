"""目标链式接续（loop chaining）语义回归。

长跑的最后一公里：单个循环到终态后 Agent 就闲置了——项目里排队的
下一个目标不会自动接续。现在循环带 successor_loop_id（PAUSED 挂起的
后继），前驱到终态（done/limit_reached/stalled/stopped）自动唤醒后继
并推进第一轮；A→B→C 链起来即 FIFO 目标流水线，Agent 不因单目标完成
而断档。
"""

import uuid

import pytest

BASE_URL = "/todo-for-ai/api/v1"


@pytest.fixture(scope="function", autouse=True)
def _isolated_app():
    from app import create_app
    from models import db

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
def db_session(_isolated_app):
    from models import db
    with _isolated_app.app_context():
        yield db.session
    db.session.rollback()


@pytest.fixture
def client(_isolated_app):
    return _isolated_app.test_client()


@pytest.fixture(autouse=True)
def _scripted_planner(monkeypatch):
    monkeypatch.setenv('GOAL_LOOP_PLANNER', 'scripted')


@pytest.fixture
def env(db_session, _isolated_app, user_factory, organization_factory, agent_factory, project_factory):
    from flask_jwt_extended import create_access_token

    user = user_factory()
    org = organization_factory(owner_id=user.id)
    agent = agent_factory(workspace_id=org.id, runner_enabled=True)
    project = project_factory(owner_id=user.id, organization_id=org.id)
    with _isolated_app.test_request_context():
        token = create_access_token(identity=str(user.id))
    yield {
        "user": user, "org": org, "agent": agent, "project": project,
        "headers": {"Authorization": f"Bearer {token}"},
    }
    # 引用 agent 的行必须先于 agent 销毁清理，否则 FK NOT NULL 炸 teardown
    from models import db as _db, AgentExperience, AgentMemory
    _db.session.rollback()
    AgentExperience.query.filter_by(agent_id=agent.id).delete()
    AgentMemory.query.filter_by(agent_id=agent.id).delete()
    _db.session.commit()


def _make_loop(db_session, env, *, start=True, rounds_limit=10, stall_limit=5):
    from services import goal_loop_service as svc

    return svc.create_loop(
        project=env["project"], agent=env["agent"],
        title=f"loop_{uuid.uuid4().hex[:6]}", goal_text="把目标做成",
        created_by=env["user"].id, rounds_limit=rounds_limit,
        stall_limit=stall_limit, start=start,
    )


def _finish_round(db_session, loop, status):
    """把循环当前活跃任务置终态并推进循环（status 为 TaskStatus 枚举成员）。"""
    from models import Task
    from services import goal_loop_service as svc

    tasks = svc.loop_tasks(loop.id)
    assert tasks, "loop should have a materialized round task"
    task = db_session.get(Task, tasks[-1].id)
    task.status = status
    db_session.commit()
    return svc.maybe_advance(loop.id, trigger_task_id=task.id)


def _state(db_session, loop):
    db_session.expire(loop)
    return loop


def _exhaust_plan(db_session, loop):
    """把计划指针推到末尾：下一次推进直达评审（scripted 评审按上轮状态决策）。"""
    db_session.expire(loop)
    loop.plan_index = len(loop.plan or [])
    db_session.commit()


# ── 创建链：chain_next 内联 / successor_loop_id 直引 ──

def test_create_with_chain_next_creates_paused_successor(client, env):
    resp = client.post(
        f"{BASE_URL}/projects/{env['project'].id}/goal-loops",
        headers=env["headers"],
        json={"title": "主目标", "goal_text": "先做这个",
              "chain_next": {"title": "下一个目标", "goal_text": "然后做这个"}},
    )
    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()["data"]
    assert data["status"] == "running"
    successor_id = data["successor_loop_id"]
    assert successor_id

    detail = client.get(f"{BASE_URL}/projects/goal-loops/{successor_id}",
                        headers=env["headers"])
    succ = detail.get_json()["data"]
    assert succ["status"] == "paused"
    assert succ["tasks"] == []  # 挂起态未被推进


def test_create_with_successor_loop_id_direct(client, db_session, env):
    successor = _make_loop(db_session, env, start=False)
    resp = client.post(
        f"{BASE_URL}/projects/{env['project'].id}/goal-loops",
        headers=env["headers"],
        json={"title": "主目标", "goal_text": "先做这个",
              "successor_loop_id": successor.id},
    )
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["data"]["successor_loop_id"] == successor.id


def test_create_successor_validation(client, db_session, env):
    base = f"{BASE_URL}/projects/{env['project'].id}/goal-loops"
    payload = {"title": "t", "goal_text": "g"}

    # 自引用
    resp = client.post(base, headers=env["headers"],
                       json={**payload, "successor_loop_id": 999999})
    assert resp.status_code == 404

    running = _make_loop(db_session, env, start=True)
    resp = client.post(base, headers=env["headers"],
                       json={**payload, "successor_loop_id": running.id})
    assert resp.status_code == 400
    assert resp.get_json()["error_details"]["code"] == "SUCCESSOR_NOT_PAUSED"


# ── 终态自动接续 ──

def test_promote_on_done(db_session, env):
    from models import TaskStatus
    a = _make_loop(db_session, env)
    b = _make_loop(db_session, env, start=False)
    from services import goal_loop_service as svc
    svc.set_successor(a.id, b.id)
    _exhaust_plan(db_session, a)

    result = _finish_round(db_session, _state(db_session, a), TaskStatus.DONE)
    # scripted 评审看到上轮 done → complete → A 终态，B 被唤醒
    assert _state(db_session, a).status.value == "done"
    assert _state(db_session, b).status.value == "running"
    from services import goal_loop_service
    assert goal_loop_service.loop_tasks(b.id), "promoted loop should materialize round 1"


def test_promote_on_limit_reached(db_session, env):
    from models import TaskStatus
    a = _make_loop(db_session, env, rounds_limit=1)
    b = _make_loop(db_session, env, start=False)
    from services import goal_loop_service as svc
    svc.set_successor(a.id, b.id)

    _finish_round(db_session, _state(db_session, a), TaskStatus.DONE)
    assert _state(db_session, a).status.value == "limit_reached"
    assert _state(db_session, b).status.value == "running"


def test_promote_on_stalled(db_session, env):
    from models import TaskStatus
    a = _make_loop(db_session, env, stall_limit=1)
    b = _make_loop(db_session, env, start=False)
    from services import goal_loop_service as svc
    svc.set_successor(a.id, b.id)

    # scripted 评审看到非 done 上轮 → blocked → stall_count=1 ≥ stall_limit=1 → STALLED
    _finish_round(db_session, _state(db_session, a), TaskStatus.CANCELLED)
    assert _state(db_session, a).status.value == "stalled"
    assert _state(db_session, b).status.value == "running"


def test_no_promote_when_loop_paused(db_session, env):
    from models import GoalLoopStatus
    a = _make_loop(db_session, env)
    b = _make_loop(db_session, env, start=False)
    from services import goal_loop_service as svc
    svc.set_successor(a.id, b.id)

    svc.set_status(a.id, GoalLoopStatus.PAUSED)
    assert _state(db_session, b).status.value == "paused"


def test_promote_on_manual_stop(db_session, env):
    from models import GoalLoopStatus
    a = _make_loop(db_session, env)
    b = _make_loop(db_session, env, start=False)
    from services import goal_loop_service as svc
    svc.set_successor(a.id, b.id)

    svc.set_status(a.id, GoalLoopStatus.STOPPED)
    assert _state(db_session, a).status.value == "stopped"
    assert _state(db_session, b).status.value == "running"


def test_chain_three_loops_transitive(db_session, env):
    """A 完成 → B 起跑并完成 → C 接续（链式传递）。"""
    from models import TaskStatus
    a = _make_loop(db_session, env)
    b = _make_loop(db_session, env, start=False)
    c = _make_loop(db_session, env, start=False)
    from services import goal_loop_service as svc
    svc.set_successor(a.id, b.id)
    svc.set_successor(b.id, c.id)

    _exhaust_plan(db_session, a)
    _finish_round(db_session, _state(db_session, a), TaskStatus.DONE)  # A done → B running
    assert _state(db_session, b).status.value == "running"
    _exhaust_plan(db_session, b)
    _finish_round(db_session, _state(db_session, b), TaskStatus.DONE)  # B done → C running
    assert _state(db_session, b).status.value == "done"
    assert _state(db_session, c).status.value == "running"


# ── 改链 / 清链 ──

def test_put_set_and_clear_successor(client, db_session, env):
    a = _make_loop(db_session, env)
    b = _make_loop(db_session, env, start=False)
    url = f"{BASE_URL}/projects/goal-loops/{a.id}"

    resp = client.put(url, headers=env["headers"], json={"successor_loop_id": b.id})
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["data"]["successor_loop_id"] == b.id

    # 成环拒绝（服务层直验）：暂停 A 使其成为合法接续目标，B 的后继指回 A
    from models import GoalLoopStatus
    from services import goal_loop_service as svc
    svc.set_status(a.id, GoalLoopStatus.PAUSED)
    with pytest.raises(ValueError, match="successor_chain_cycle"):
        svc.set_successor(b.id, a.id)

    resp = client.put(url, headers=env["headers"], json={"successor_loop_id": None})
    assert resp.status_code == 200
    assert resp.get_json()["data"]["successor_loop_id"] is None


def test_set_successor_rejects_terminal_loop(db_session, env):
    from models import GoalLoopStatus
    from services import goal_loop_service as svc

    a = _make_loop(db_session, env)
    b = _make_loop(db_session, env, start=False)
    svc.set_status(a.id, GoalLoopStatus.STOPPED)
    with pytest.raises(ValueError):
        svc.set_successor(a.id, b.id)


# ── 派发工作时间窗门（fail-open）──

def test_dispatch_window_gate_blocks_out_of_window(db_session, env, monkeypatch):
    import services.agent_working_schedule as sched
    from models import AgentTaskLease, TaskStatus
    from services.goal_loop import dispatch

    a = _make_loop(db_session, env)
    from services import goal_loop_service as svc
    task = svc.loop_tasks(a.id)[0]
    task.status = TaskStatus.TODO
    agent = env["agent"]
    # create_loop 物化任务时已派发过一轮：清掉在途租约再验证窗口门
    AgentTaskLease.query.filter_by(task_id=task.id).update({"active": False})
    db_session.commit()

    monkeypatch.setattr(sched, "is_in_working_window", lambda schedule, at=None: False)
    dispatch.assign_task_to_agent(task, agent)
    db_session.expire(task)
    assert task.status == TaskStatus.TODO  # 留 TODO 等开窗
    assert AgentTaskLease.query.filter_by(task_id=task.id, active=True).count() == 0

    monkeypatch.setattr(sched, "is_in_working_window", lambda schedule, at=None: True)
    dispatch.assign_task_to_agent(task, agent)
    db_session.expire(task)
    assert AgentTaskLease.query.filter_by(task_id=task.id, active=True).count() == 1
