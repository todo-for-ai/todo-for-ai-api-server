"""Agent 综合健康度分析（api/agents/health.py + services/agent_health_analytics.py）
回归测试：评分四维加权 / 告警原因 / 按日趋势 / 状态迁移，全部分支到行。

会话级 SQLite 的隔离坑：AgentReputation 按 agent_id 唯一、自增 id 复用会撞
UNIQUE——每例先清场本文件涉及的表。
"""

import datetime as dt
import uuid

import pytest

BASE_URL = "/todo-for-ai/api/v1"


@pytest.fixture(autouse=True)
def _clean_analytics_tables(db_session):
    """清掉会影响本文件的遗留行（唯一约束 + 聚合口径）。"""
    from models import AgentConflict, AgentReputation, AuditLog, SandboxViolation
    db_session.query(AgentReputation).delete(synchronize_session=False)
    db_session.query(SandboxViolation).delete(synchronize_session=False)
    db_session.query(AgentConflict).delete(synchronize_session=False)
    db_session.query(AuditLog).filter(
        AuditLog.action == "reputation.update").delete(synchronize_session=False)
    db_session.commit()
    yield


def _headers(user):
    from flask_jwt_extended import create_access_token
    return {"Authorization": f"Bearer {create_access_token(identity=str(user.id))}"}


def _now(days_ago=0.0):
    return dt.datetime.utcnow() - dt.timedelta(days=days_ago)


def _make_user(db_session):
    from models import User
    user = User(username=f"hu_{uuid.uuid4().hex[:8]}",
                email=f"hu_{uuid.uuid4().hex[:6]}@t.io")
    db_session.add(user)
    db_session.commit()
    return user


def _make_agent(db_session, user, kind=None, name=None):
    from models import Agent, AgentKind
    agent = Agent(name=name or f"ha_{uuid.uuid4().hex[:8]}", owner_id=user.id)
    agent.kind = kind if kind is not None else AgentKind.ASSISTANT
    db_session.add(agent)
    db_session.commit()
    return agent


def _rep(db_session, agent_id, score):
    from models import AgentReputation
    db_session.add(AgentReputation(agent_id=agent_id, score=score))
    db_session.commit()


def _assignment(db_session, agent_id, state, created_at):
    from models import TaskAssignment, TaskAssignmentState
    db_session.add(TaskAssignment(
        task_id=int(uuid.uuid4().hex[:6], 16) + 10_000_000,
        agent_id=agent_id,
        state=TaskAssignmentState(state),
        created_at=created_at,
    ))
    db_session.commit()


def _conflict(db_session, user, agent_ids, days_ago=1):
    from models import AgentConflict, ConflictType
    db_session.add(AgentConflict(
        owner_id=user.id,
        conflict_type=ConflictType.DUPLICATE_CLAIM,
        title=f"c_{uuid.uuid4().hex[:6]}",
        agent_ids=agent_ids,
        created_at=_now(days_ago),
    ))
    db_session.commit()


def _violation(db_session, agent_id, days_ago=1):
    from models import SandboxViolation, SandboxViolationType
    db_session.add(SandboxViolation(
        execution_id=1, sandbox_id=1, agent_id=agent_id,
        violation_type=SandboxViolationType.NETWORK_BLOCKED,
        blocked_at=_now(days_ago),
    ))
    db_session.commit()


def _rep_audit(db_session, agent_id, new_score=None, delta=None, days_ago=0,
               created_at=None):
    from models import AuditLog
    detail = {}
    if new_score is not None:
        detail["new_score"] = new_score
    if delta is not None:
        detail["score_delta"] = delta
    db_session.add(AuditLog(
        actor_type="system", action="reputation.update",
        resource_type="agent", resource_id=agent_id,
        detail=detail,
        created_at=created_at if created_at is not None else _now(days_ago),
    ))
    db_session.commit()


# ────────────────────────── 权重归一化（纯函数） ──────────────────────────

class TestNormalizeWeights:
    def test_none_returns_defaults(self):
        from services.agent_health_analytics import normalize_health_weights
        assert normalize_health_weights(None) == {
            "reputation": 0.4, "completion": 0.3, "conflict": 0.15, "violation": 0.15}

    def test_partial_and_invalid_values_fall_back(self):
        from services.agent_health_analytics import normalize_health_weights
        w = normalize_health_weights({"reputation": "x", "completion": 1})
        assert w["reputation"] == pytest.approx(0.4 / 1.7)  # 非法值回退默认再归一
        assert w["completion"] == pytest.approx(1 / 1.7)

    def test_negative_clamped_to_zero(self):
        from services.agent_health_analytics import normalize_health_weights
        w = normalize_health_weights({"reputation": -5, "completion": 1,
                                      "conflict": 1, "violation": 1})
        assert w["reputation"] == 0.0
        assert sum(w.values()) == pytest.approx(1.0)

    def test_all_zero_resets_to_defaults(self):
        from services.agent_health_analytics import normalize_health_weights
        w = normalize_health_weights({"reputation": 0, "completion": 0,
                                      "conflict": 0, "violation": 0})
        assert w["reputation"] == pytest.approx(0.4)


# ────────────────────────── 综合健康分 ──────────────────────────

def test_health_endpoint_no_agents(client, db_session):
    user = _make_user(db_session)
    resp = client.get(f"{BASE_URL}/agents/health", headers=_headers(user))
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["data"] == {"days": 30, "items": []}


def test_health_endpoint_full_scoring_and_sorting(client, db_session):
    user = _make_user(db_session)
    strong = _make_agent(db_session, user, name="strong")
    weak = _make_agent(db_session, user, name="weak")
    _rep(db_session, strong.id, 90)
    _rep(db_session, weak.id, 30)
    # weak：近 7 天 2 个分配 1 个完成
    _assignment(db_session, weak.id, "done", _now(1))
    _assignment(db_session, weak.id, "failed", _now(2))
    _violation(db_session, weak.id, days_ago=1)
    _conflict(db_session, user, [weak.id], days_ago=1)

    resp = client.get(f"{BASE_URL}/agents/health?days=7", headers=_headers(user))
    assert resp.status_code == 200, resp.get_json()
    items = resp.get_json()["data"]["items"]
    assert [i["name"] for i in items] == ["strong", "weak"]  # 降序
    weak_item = items[1]
    assert weak_item["health_score"] < items[0]["health_score"]
    assert weak_item["total_assignments"] == 2
    assert weak_item["done_assignments"] == 1
    assert weak_item["completion_rate"] == 50.0
    assert weak_item["conflicts"] == 1
    assert weak_item["sandbox_violations"] == 1


def test_health_days_clamped(client, db_session):
    user = _make_user(db_session)
    resp = client.get(f"{BASE_URL}/agents/health?days=9999", headers=_headers(user))
    assert resp.get_json()["data"]["days"] == 365
    resp = client.get(f"{BASE_URL}/agents/health?days=abc", headers=_headers(user))
    assert resp.get_json()["data"]["days"] == 30


def test_health_custom_weights_breakdown(client, db_session):
    """全部权重压在一个维度上 → 综合分应接近该维度子分。"""
    user = _make_user(db_session)
    agent = _make_agent(db_session, user)
    _rep(db_session, agent.id, 80)
    resp = client.get(
        f"{BASE_URL}/agents/health?w_reputation=1&w_completion=0&w_conflict=0&w_violation=0",
        headers=_headers(user))
    item = resp.get_json()["data"]["items"][0]
    assert item["health_score"] == 80.0


def test_health_recommendations_branches(client, db_session):
    user = _make_user(db_session)
    low_rep = _make_agent(db_session, user, name="low_rep")
    _rep(db_session, low_rep.id, 30)
    busy = _make_agent(db_session, user, name="busy_conflict_violation")
    _assignment(db_session, busy.id, "failed", _now(1))
    _conflict(db_session, user, [busy.id], days_ago=1)
    _violation(db_session, busy.id, days_ago=1)
    idle = _make_agent(db_session, user, name="idle")  # 无任何记录

    resp = client.get(f"{BASE_URL}/agents/health/alerts?min_health_score=100",
                      headers=_headers(user))
    assert resp.status_code == 200
    items = {i["name"]: i for i in resp.get_json()["data"]["items"]}
    assert items["low_rep"]["recommendations"]
    assert any("声誉" in r for r in items["low_rep"]["reasons"])
    # busy：完成率 0 + 冲突 + 违规
    recs = " ".join(items["busy_conflict_violation"]["recommendations"])
    assert "完成率偏低" in recs and "协作冲突" in recs and "沙盒违规" in recs
    # idle：无记录 → "主动领取任务" 建议
    assert any("主动领取任务" in r for r in items["idle"]["recommendations"])


def test_recommendations_weakest_dimension_fallback(db_session):
    """全维度健康时返回最弱维度提示（当前用户名下 agent 数=0 不行，
    需要 1 个 agent 且全部子分相同）。"""
    from services.agent_health_analytics import compute_agent_health
    user = _make_user(db_session)
    agent = _make_agent(db_session, user)
    _rep(db_session, agent.id, 50)
    _assignment(db_session, agent.id, "done", _now(1))
    days, items = compute_agent_health(user.id, 30, with_recommendations=True)
    assert items[0]["recommendations"]
    assert "最弱维度" in items[0]["recommendations"][0]
    assert days == 30


def test_alerts_healthy_agents_excluded(client, db_session):
    user = _make_user(db_session)
    good = _make_agent(db_session, user, name="good")
    _rep(db_session, good.id, 95)
    resp = client.get(f"{BASE_URL}/agents/health/alerts?min_health_score=60",
                      headers=_headers(user))
    assert resp.get_json()["data"]["items"] == []
    assert resp.get_json()["data"]["min_health_score"] == 60


def test_alerts_min_health_score_invalid_falls_back(client, db_session):
    user = _make_user(db_session)
    resp = client.get(f"{BASE_URL}/agents/health/alerts?min_health_score=abc",
                      headers=_headers(user))
    assert resp.get_json()["data"]["min_health_score"] == 60


# ────────────────────────── 趋势 ──────────────────────────

def test_trend_requires_audit_and_handles_filters(client, db_session):
    user = _make_user(db_session)
    agent = _make_agent(db_session, user)
    other = _make_agent(db_session, user)
    _rep_audit(db_session, agent.id, new_score=90, delta=5, days_ago=0)
    _rep_audit(db_session, agent.id, new_score=85, delta=-5, days_ago=0)
    _rep_audit(db_session, agent.id, new_score=70, delta=5, days_ago=1)
    _rep_audit(db_session, agent.id, new_score=None, delta="bad", days_ago=1)  # 脏增量
    _rep_audit(db_session, agent.id, delta=3,
               created_at=dt.datetime(2020, 1, 1))  # 无日期（越界被过滤，不在 since 内）
    _conflict(db_session, user, [agent.id], days_ago=0)
    _violation(db_session, agent.id, days_ago=0)

    resp = client.get(f"{BASE_URL}/agents/health/trend", headers=_headers(user))
    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()["data"]
    assert data["total_conflicts"] == 1
    assert data["total_violations"] == 1
    dates = {t["date"]: t for t in data["trend"]}
    today = sorted(dates)[-1]
    assert dates[today]["avg_reputation"] == 85.0  # 同日多次取最后一次分值
    assert dates[today]["positive"] == 1 and dates[today]["negative"] == 1
    assert data["by_kind_overall"]

    # 过滤到不存在/不在名下的 agent
    resp = client.get(f"{BASE_URL}/agents/health/trend?agent_id=424242",
                      headers=_headers(user))
    data = resp.get_json()["data"]
    assert data["trend"] == [] and data["agent_id"] == 424242
    assert data["agent_name"] is None

    # 过滤到具体 agent：冲突计数按参与方过滤
    resp = client.get(f"{BASE_URL}/agents/health/trend?agent_id={agent.id}",
                      headers=_headers(user))
    data = resp.get_json()["data"]
    assert data["agent_name"] == agent.name
    assert data["total_conflicts"] == 1
    resp = client.get(f"{BASE_URL}/agents/health/trend?agent_id={other.id}",
                      headers=_headers(user))
    assert resp.get_json()["data"]["total_conflicts"] == 0

    # agent_id 非法 → 当作未传
    resp = client.get(f"{BASE_URL}/agents/health/trend?agent_id=abc",
                      headers=_headers(user))
    assert resp.get_json()["data"]["agent_id"] is None


def test_trend_empty_workspace_shape(client, db_session):
    user = _make_user(db_session)
    resp = client.get(f"{BASE_URL}/agents/health/trend?days=abc", headers=_headers(user))
    data = resp.get_json()["data"]
    assert data == {"days": 30, "trend": [], "total_positive": 0,
                    "total_negative": 0, "by_kind_overall": {}}


# ────────────────────────── 状态迁移 ──────────────────────────

def test_state_transitions_flow(client, db_session):
    user = _make_user(db_session)
    agent = _make_agent(db_session, user)
    # 三天三个档位：healthy → degraded → critical + 一次同档重复
    _rep_audit(db_session, agent.id, new_score=90, days_ago=3)
    _rep_audit(db_session, agent.id, new_score=95, days_ago=2)   # healthy 重复
    _rep_audit(db_session, agent.id, new_score=65, days_ago=1)   # 降档
    _rep_audit(db_session, agent.id, new_score=30, days_ago=0)   # 再降档
    # detail 非字典（历史脏数据）→ 分值回退 0 → critical
    from models import AuditLog
    db_session.add(AuditLog(
        actor_type="system", action="reputation.update",
        resource_type="agent", resource_id=agent.id,
        detail="legacy-string", created_at=_now(days_ago=5)))
    db_session.commit()
    resp = client.get(f"{BASE_URL}/agents/health/state-transitions",
                      headers=_headers(user))
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert data["days"] == 30
    states = {s["name"]: s["count"] for s in data["states"]}
    assert states == {"healthy": 2, "degraded": 1, "critical": 2}
    assert data["total_transitions"] == 3  # critical→healthy、healthy→degraded、degraded→critical
    assert data["flows"][0]["value"] == 1  # 每条迁移 1 次


def test_state_transitions_days_clamped_to_min_seven(client, db_session):
    user = _make_user(db_session)
    resp = client.get(f"{BASE_URL}/agents/health/state-transitions?days=3",
                      headers=_headers(user))
    assert resp.get_json()["data"]["days"] == 7
    resp = client.get(f"{BASE_URL}/agents/health/state-transitions?days=abc",
                      headers=_headers(user))
    assert resp.get_json()["data"]["days"] == 30


def test_state_transitions_empty_workspace(client, db_session):
    user = _make_user(db_session)
    resp = client.get(f"{BASE_URL}/agents/health/state-transitions",
                      headers=_headers(user))
    assert resp.get_json()["data"] == {"days": 30, "transitions": [], "states": []}


# ────────────────────────── 服务级边界 ──────────────────────────

def test_compute_agent_health_ignores_rows_without_owner_match(db_session):
    """别人的 Agent 不计入（owner 过滤）。"""
    from services.agent_health_analytics import compute_agent_health
    mine = _make_user(db_session)
    theirs = _make_user(db_session)
    _make_agent(db_session, theirs)
    days, items = compute_agent_health(mine.id, 30)
    assert items == []


def test_compute_state_transitions_counts_via_direct_service(db_session):
    """直接调服务层：迁移统计与 states 计数（sqlite 字符串日期兼容）。"""
    from services.agent_health_analytics import compute_state_transitions
    user = _make_user(db_session)
    agent = _make_agent(db_session, user)
    _rep_audit(db_session, agent.id, new_score=90, days_ago=2)
    _rep_audit(db_session, agent.id, new_score=40, days_ago=1)
    data = compute_state_transitions(user.id, 30)
    assert data["total_transitions"] == 1
    assert data["flows"][0] == {"source": "healthy", "target": "critical", "value": 1}


def test_compute_health_trend_counts_via_direct_service(db_session):
    from services.agent_health_analytics import compute_health_trend
    user = _make_user(db_session)
    agent = _make_agent(db_session, user)
    _rep_audit(db_session, agent.id, new_score=90, delta=5, days_ago=0)
    _conflict(db_session, user, [agent.id], days_ago=0)
    data = compute_health_trend(user.id, 30)
    assert data["trend"]
    assert data["total_conflicts"] == 1
    assert data["by_kind_overall"]
