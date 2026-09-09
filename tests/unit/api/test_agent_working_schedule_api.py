"""Agent 工作时间区间：API 端点 + 派发门禁回归测试。"""

import uuid
from datetime import datetime

import pytest

BASE_URL = "/todo-for-ai/api/v1"


@pytest.fixture(autouse=True)
def _purge_leftover_leases(db_session):
    """会话级 SQLite 的隔离坑：其他用例遗留的 AgentTaskLease/Attempt 不会级联删除，
    而自增 id 回收后会让本文件的租约计数 / 领取判定读到别人的行——每例先清场。"""
    from models import AgentTaskAttempt, AgentTaskLease
    db_session.query(AgentTaskLease).delete(synchronize_session=False)
    db_session.query(AgentTaskAttempt).delete(synchronize_session=False)
    db_session.commit()
    yield

# 永远不在窗口内（过去的 dates 区间）
NEVER_SCHEDULE = {
    'enabled': True, 'timezone': 'UTC',
    'includes': [{
        'type': 'dates', 'start_date': '2020-01-01', 'end_date': '2020-01-02',
        'start_time': '00:00', 'end_time': '24:00',
    }],
}
# 全天候在窗口内
ALWAYS_SCHEDULE = {
    'enabled': True, 'timezone': 'UTC',
    'includes': [{'type': 'daily', 'start_time': '00:00', 'end_time': '24:00'}],
}


def _unique(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _make_agent_key(client, db_session, user_factory, organization_factory,
                    agent_factory, **agent_kwargs):
    """创建 user+org+agent（runner_enabled）并换出 runtime 会话头。"""
    from models import AgentKey

    user = user_factory()
    org = organization_factory(owner_id=user.id)
    agent_kwargs.setdefault('runner_enabled', True)
    agent = agent_factory(workspace_id=org.id, owner_id=user.id, **agent_kwargs)
    key_row, raw_key = AgentKey.generate_key(
        name=_unique("win-key"), workspace_id=org.id,
        agent_id=agent.id, created_by_user_id=user.id,
    )
    db_session.add(key_row)
    db_session.commit()

    auth_resp = client.post(f"{BASE_URL}/agent/auth/introspect", json={"agent_key": raw_key})
    assert auth_resp.status_code == 200
    agent_headers = {"Authorization": f"Bearer {auth_resp.get_json()['data']['access_token']}"}
    return user, org, agent, agent_headers


def _user_headers(user):
    from flask_jwt_extended import create_access_token
    return {"Authorization": f"Bearer {create_access_token(identity=str(user.id))}"}


# ────────────────────────── 专用端点 ──────────────────────────

def test_working_schedule_roundtrip(client, db_session, user_factory,
                                    organization_factory, agent_factory):
    user, org, agent, _ = _make_agent_key(
        client, db_session, user_factory, organization_factory, agent_factory)
    headers = _user_headers(user)

    resp = client.get(f"{BASE_URL}/agents/{agent.id}/working-schedule", headers=headers)
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()["data"]
    assert body["working_schedule"] == {}
    assert body["evaluation"]["enabled"] is False
    assert body["evaluation"]["in_window"] is True

    resp = client.put(
        f"{BASE_URL}/agents/{agent.id}/working-schedule",
        json={"working_schedule": NEVER_SCHEDULE},
        headers=headers,
    )
    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()["data"]
    assert data["working_schedule"]["enabled"] is True
    assert data["working_schedule"]["includes"][0]["type"] == "dates"
    assert data["evaluation"]["in_window"] is False

    resp = client.get(f"{BASE_URL}/agents/{agent.id}/working-schedule", headers=headers)
    assert resp.get_json()["data"]["working_schedule"]["timezone"] == "UTC"

    db_session.expire_all()
    assert agent.working_schedule["enabled"] is True


def test_working_schedule_put_rejects_invalid(client, db_session, user_factory,
                                              organization_factory, agent_factory):
    user, org, agent, _ = _make_agent_key(
        client, db_session, user_factory, organization_factory, agent_factory)
    headers = _user_headers(user)

    bad = {"working_schedule": {"enabled": True,
                                "includes": [{"type": "weekly", "days_of_week": [9]}]}}
    resp = client.put(f"{BASE_URL}/agents/{agent.id}/working-schedule", json=bad, headers=headers)
    assert resp.status_code == 400
    assert "days_of_week" in resp.get_json()["message"]

    # 非法配置不应落库
    resp = client.get(f"{BASE_URL}/agents/{agent.id}/working-schedule", headers=headers)
    assert resp.get_json()["data"]["working_schedule"] == {}


def test_working_schedule_preview(client, db_session, user_factory,
                                  organization_factory, agent_factory):
    user, org, agent, _ = _make_agent_key(
        client, db_session, user_factory, organization_factory, agent_factory)

    # 候选配置（未保存）：daily 22:00-06:00 Shanghai，at=UTC 10:00（Shanghai 18:00）→ 窗外
    candidate = {
        'enabled': True, 'timezone': 'Asia/Shanghai',
        'includes': [{'type': 'daily', 'start_time': '22:00', 'end_time': '06:00'}],
    }
    resp = client.post(
        f"{BASE_URL}/agents/{agent.id}/working-schedule/preview",
        json={"schedule": candidate, "at": "2026-09-09T10:00:00"},
        headers=_user_headers(user),
    )
    assert resp.status_code == 200, resp.get_json()
    evaluation = resp.get_json()["data"]["evaluation"]
    assert evaluation["in_window"] is False
    assert evaluation["next_window_at"] == "2026-09-09T14:00:00"

    # 不传 schedule → 用已保存配置求值（当前为空 → 恒在窗口）
    resp = client.post(
        f"{BASE_URL}/agents/{agent.id}/working-schedule/preview",
        json={},
        headers=_user_headers(user),
    )
    assert resp.get_json()["data"]["evaluation"]["in_window"] is True

    # 非法 at
    resp = client.post(
        f"{BASE_URL}/agents/{agent.id}/working-schedule/preview",
        json={"schedule": candidate, "at": "not-a-date"},
        headers=_user_headers(user),
    )
    assert resp.status_code == 400


def test_update_agent_accepts_working_schedule(client, db_session, user_factory,
                                               organization_factory, agent_factory):
    user, org, agent, _ = _make_agent_key(
        client, db_session, user_factory, organization_factory, agent_factory)
    headers = _user_headers(user)

    resp = client.put(
        f"{BASE_URL}/agents/{agent.id}",
        json={"working_schedule": ALWAYS_SCHEDULE},
        headers=headers,
    )
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["data"]["working_schedule"]["enabled"] is True

    # 通用更新入口同样走严格校验
    bad = {"working_schedule": {"enabled": True, "timezone": "Bad/Zone"}}
    resp = client.put(f"{BASE_URL}/agents/{agent.id}", json=bad, headers=headers)
    assert resp.status_code == 400


# ────────────────────────── pull 门禁 ──────────────────────────

def _seed_ai_task(db_session, project_factory, task_factory, user, org, title):
    project = project_factory(owner_id=user.id, organization_id=org.id)
    return task_factory(project_id=project.id, owner_id=org.id, is_ai_task=True, title=title)


def test_pull_blocked_outside_working_window(client, db_session, user_factory,
                                             organization_factory, agent_factory,
                                             project_factory, task_factory):
    from models import AgentTaskLease

    user, org, agent, agent_headers = _make_agent_key(
        client, db_session, user_factory, organization_factory, agent_factory,
        working_schedule=NEVER_SCHEDULE,
    )
    _seed_ai_task(db_session, project_factory, task_factory, user, org, "Window gate")

    resp = client.post(f"{BASE_URL}/agent/tasks/pull", json={"max_tasks": 1}, headers=agent_headers)
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert data["tasks"] == []
    assert data["working_window"]["blocked"] is True
    assert data["working_window"]["in_window"] is False
    assert "agent_profile" in data
    assert data["agent_profile"]["working_schedule"] == NEVER_SCHEDULE

    # 没有产生租约/attempt
    assert AgentTaskLease.query.filter_by(agent_id=agent.id).count() == 0


def test_pull_allowed_inside_working_window(client, db_session, user_factory,
                                            organization_factory, agent_factory,
                                            project_factory, task_factory):
    user, org, agent, agent_headers = _make_agent_key(
        client, db_session, user_factory, organization_factory, agent_factory,
        working_schedule=ALWAYS_SCHEDULE,
    )
    _seed_ai_task(db_session, project_factory, task_factory, user, org, "In window")

    resp = client.post(f"{BASE_URL}/agent/tasks/pull", json={"max_tasks": 1}, headers=agent_headers)
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert len(data["tasks"]) == 1
    assert data["agent_profile"]["working_schedule"]["includes"][0]["type"] == "daily"


# ────────────────────────── claim 门禁 ──────────────────────────

def test_claim_blocked_outside_working_window(client, db_session, user_factory,
                                              organization_factory, agent_factory):
    user, org, agent, _ = _make_agent_key(
        client, db_session, user_factory, organization_factory, agent_factory,
        working_schedule=NEVER_SCHEDULE,
    )

    resp = client.post(
        f"{BASE_URL}/agents/{agent.id}/claim",
        json={},
        headers=_user_headers(user),
    )
    assert resp.status_code == 409, resp.get_json()
    assert resp.get_json()["message"] == "AGENT_OUT_OF_WORKING_WINDOW"

    # 全天候窗口可正常领取（无可领任务时返回 success None）
    client.put(
        f"{BASE_URL}/agents/{agent.id}/working-schedule",
        json={"working_schedule": ALWAYS_SCHEDULE},
        headers=_user_headers(user),
    )
    resp = client.post(f"{BASE_URL}/agents/{agent.id}/claim", json={}, headers=_user_headers(user))
    assert resp.status_code in (200, 201)


# ────────────────────────── auto_assign 门禁 ──────────────────────────

def test_auto_assign_skips_out_of_window_agent(db_session, user_factory,
                                               organization_factory, agent_factory,
                                               project_factory, task_factory):
    from services.agent_runtime_controller import AgentRuntimeController

    user = user_factory()
    org = organization_factory(owner_id=user.id)
    agent = agent_factory(workspace_id=org.id, runner_enabled=True,
                          working_schedule=NEVER_SCHEDULE)
    project = project_factory(owner_id=user.id, organization_id=org.id)
    task = task_factory(project_id=project.id, owner_id=org.id, is_ai_task=True)

    AgentRuntimeController.auto_assign_task(task)

    from models import AgentTaskLease, TaskStatus
    assert AgentTaskLease.query.filter_by(agent_id=agent.id).count() == 0
    db_session.expire_all()
    assert task.status == TaskStatus.TODO


def test_auto_assign_assigns_in_window_agent(db_session, user_factory,
                                             organization_factory, agent_factory,
                                             project_factory, task_factory):
    from services.agent_runtime_controller import AgentRuntimeController

    user = user_factory()
    org = organization_factory(owner_id=user.id)
    agent = agent_factory(workspace_id=org.id, runner_enabled=True,
                          working_schedule=ALWAYS_SCHEDULE)
    project = project_factory(owner_id=user.id, organization_id=org.id)
    task = task_factory(project_id=project.id, owner_id=org.id, is_ai_task=True)

    AgentRuntimeController.auto_assign_task(task)

    from models import AgentTaskLease, TaskStatus
    assert AgentTaskLease.query.filter_by(agent_id=agent.id).count() == 1
    db_session.expire_all()
    assert task.status == TaskStatus.IN_PROGRESS


# ────────────────────────── introspect 下发 ──────────────────────────

def test_introspect_includes_working_schedule(client, db_session, user_factory,
                                              organization_factory, agent_factory):
    from models import AgentKey

    user = user_factory()
    org = organization_factory(owner_id=user.id)
    agent = agent_factory(workspace_id=org.id, working_schedule=ALWAYS_SCHEDULE)
    key_row, raw_key = AgentKey.generate_key(
        name=_unique("win-key"), workspace_id=org.id,
        agent_id=agent.id, created_by_user_id=user.id,
    )
    db_session.add(key_row)
    db_session.commit()

    resp = client.post(f"{BASE_URL}/agent/auth/introspect", json={"agent_key": raw_key})
    assert resp.status_code == 200
    assert resp.get_json()["data"]["agent"]["working_schedule"] == ALWAYS_SCHEDULE
