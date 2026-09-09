"""Agent 生产力分析（api/agents/productivity.py + services/agent_productivity_analytics.py）
回归测试：概览/趋势/告警/分组/热力图/周对比/闲置排行的全分支到行。

自建 User/Agent 不走 factory（删除 Agent 级联 assignments 触发 NOT NULL，
同 UserActivity/reputations 坑）；TaskAssignment 用高位随机 task_id。
"""

import uuid

import pytest

BASE_URL = "/todo-for-ai/api/v1"


def _headers(user):
    from flask_jwt_extended import create_access_token
    return {"Authorization": f"Bearer {create_access_token(identity=str(user.id))}"}


def _now(**kw):
    import datetime as dt
    return dt.datetime.utcnow() - dt.timedelta(**kw)


def _make_user(db_session):
    from models import User
    user = User(username=f"pu_{uuid.uuid4().hex[:8]}",
                email=f"pu_{uuid.uuid4().hex[:6]}@t.io")
    db_session.add(user)
    db_session.commit()
    return user


def _make_agent(db_session, user, kind="assistant", name=None):
    from models import Agent, AgentKind
    agent = Agent(name=name or f"pp_{uuid.uuid4().hex[:8]}", owner_id=user.id)
    agent.kind = AgentKind(kind) if isinstance(kind, str) else kind
    db_session.add(agent)
    db_session.commit()
    return agent


def _assign(db_session, agent_id, state, created_at=None, claimed_at=None,
            completed_at=None, last_heartbeat_at=None):
    from models import TaskAssignment, TaskAssignmentState
    db_session.add(TaskAssignment(
        task_id=int(uuid.uuid4().hex[:6], 16) + 20_000_000,
        agent_id=agent_id,
        state=TaskAssignmentState(state) if isinstance(state, str) else state,
        created_at=created_at or _now(),
        claimed_at=claimed_at,
        completed_at=completed_at,
        last_heartbeat_at=last_heartbeat_at,
    ))
    db_session.commit()


def _done(db_session, agent_id, completed_at, **kw):
    claimed = kw.pop("claimed_at", None) or _now(days=1)
    _assign(db_session, agent_id, "done",
            created_at=kw.pop("created_at", None) or _now(days=1),
            claimed_at=claimed, completed_at=completed_at, **kw)


# ────────────────────────── 概览 ──────────────────────────

def test_productivity_empty_workspace(client, db_session):
    user = _make_user(db_session)
    resp = client.get(f"{BASE_URL}/agents/productivity", headers=_headers(user))
    assert resp.get_json()["data"] == {"days": 30, "items": []}


def test_productivity_all_state_buckets_and_sorting(client, db_session):
    user = _make_user(db_session)
    a1 = _make_agent(db_session, user, name="busy")
    a2 = _make_agent(db_session, user, name="other")
    # a1：done（带时长）+ failed + cancelled + expired + in_progress + done（回退时长）
    _done(db_session, a1.id, _now(hours=20))
    _assign(db_session, a1.id, "failed")
    _assign(db_session, a1.id, "cancelled")
    _assign(db_session, a1.id, "expired")
    _assign(db_session, a1.id, "running")
    _done(db_session, a1.id, _now(hours=30))  # 时长 6h（claimed 1 天前）
    # a2：done（completed_at 缺失 → 不计时长）
    _assign(db_session, a2.id, "done", completed_at=None)
    _assign(db_session, a2.id, "done", completed_at=_now(hours=5),
            claimed_at=_now(hours=10))  # completed < claimed → 不计时长

    resp = client.get(f"{BASE_URL}/agents/productivity?days=7&limit=1",
                      headers=_headers(user))
    data = resp.get_json()["data"]
    assert data["days"] == 7
    assert len(data["items"]) == 1  # limit 截断，a1 done=2 居首
    top = data["items"][0]
    assert top["name"] == "busy"
    assert (top["total"], top["done"], top["failed"], top["cancelled"],
            top["expired"], top["in_progress"]) == (6, 2, 1, 1, 1, 1)
    assert top["completion_rate"] == round(2 / 6 * 100, 1)
    assert top["avg_completion_hours"] == 4.0  # completed(20h前) - claimed(24h前) = 4h


def test_productivity_invalid_params_fall_back(client, db_session):
    user = _make_user(db_session)
    resp = client.get(f"{BASE_URL}/agents/productivity?days=abc&limit=xyz",
                      headers=_headers(user))
    assert resp.get_json()["data"]["days"] == 30
    assert len(resp.get_json()["data"]["items"]) == 0


# ────────────────────────── 趋势 ──────────────────────────

def test_trend_buckets_done_failed_by_kind_and_skip_null_date(client, db_session):
    user = _make_user(db_session)
    a1 = _make_agent(db_session, user, kind="assistant")
    a2 = _make_agent(db_session, user, kind="external")
    _assign(db_session, a1.id, "done", completed_at=_now(days=1))
    _assign(db_session, a2.id, "failed", completed_at=_now(days=1))
    _assign(db_session, a1.id, "done", completed_at=None)  # 无日期 → 跳过

    resp = client.get(f"{BASE_URL}/agents/productivity/trend?days=7",
                      headers=_headers(user))
    data = resp.get_json()["data"]
    assert data["total_done"] == 1 and data["total_failed"] == 1
    assert len(data["trend"]) == 1
    day = data["trend"][0]
    assert day["done"] == 1 and day["failed"] == 1
    assert day["by_kind"]["assistant"] == {"done": 1, "failed": 0}
    assert data["by_kind_totals"]["external"] == {"done": 0, "failed": 1}


def test_trend_empty_workspace(client, db_session):
    user = _make_user(db_session)
    resp = client.get(f"{BASE_URL}/agents/productivity/trend", headers=_headers(user))
    assert resp.get_json()["data"] == {"days": 30, "trend": [], "total_done": 0,
                                       "total_failed": 0, "by_kind_totals": {}}


# ────────────────────────── 告警 ──────────────────────────

def test_alerts_reasons_and_sorting(client, db_session):
    user = _make_user(db_session)
    low_completion = _make_agent(db_session, user, name="lowc")
    high_failure = _make_agent(db_session, user, name="highf")
    healthy = _make_agent(db_session, user, name="healthy")
    # lowc：4 个全 failed（完成率 0，失败率 100）
    for _ in range(4):
        _assign(db_session, low_completion.id, "failed")
    # highf：3 done + 2 failed（完成率 60 达标，失败率 40 超标）
    for _ in range(3):
        _done(db_session, high_failure.id, _now(hours=5))
    for _ in range(2):
        _assign(db_session, high_failure.id, "failed")
    # healthy：3 done（全部达标）
    for _ in range(3):
        _done(db_session, healthy.id, _now(hours=5))

    resp = client.get(f"{BASE_URL}/agents/productivity/alerts?min_assignments=3",
                      headers=_headers(user))
    data = resp.get_json()["data"]
    names = [i["name"] for i in data["items"]]
    assert names == ["lowc", "highf"]  # 完成率升序
    assert data["items"][0]["reasons"] == ["完成率 0.0% < 50.0%",
                                            "失败率 100.0% > 30.0%"]
    assert data["items"][1]["reasons"] == ["失败率 40.0% > 30.0%"]
    assert all("healthy" not in n for n in names)


def test_alerts_under_min_assignments_skipped(client, db_session):
    user = _make_user(db_session)
    a = _make_agent(db_session, user, name="small")
    _assign(db_session, a.id, "failed")
    resp = client.get(f"{BASE_URL}/agents/productivity/alerts?min_assignments=3",
                      headers=_headers(user))
    assert resp.get_json()["data"]["items"] == []
    assert resp.get_json()["data"]["min_assignments"] == 3


def test_alerts_invalid_params_fall_back(client, db_session):
    user = _make_user(db_session)
    _make_agent(db_session, user)  # 空 workspace 的响应键是短形（days+items）
    resp = client.get(
        f"{BASE_URL}/agents/productivity/alerts?days=x&min_completion_rate=y"
        "&max_failure_rate=z&min_assignments=w",
        headers=_headers(user))
    data = resp.get_json()["data"]
    assert (data["days"], data["min_completion_rate"], data["max_failure_rate"],
            data["min_assignments"]) == (30, 50, 30, 3)


# ────────────────────────── 按 kind 分组 ──────────────────────────

def test_by_kind_grouping_and_unknown(client, db_session):
    user = _make_user(db_session)
    a1 = _make_agent(db_session, user, kind="assistant")
    a2 = _make_agent(db_session, user, kind="external")
    _done(db_session, a1.id, _now(hours=6))
    _assign(db_session, a2.id, "failed")

    resp = client.get(f"{BASE_URL}/agents/productivity/by-kind?days=7",
                      headers=_headers(user))
    items = {i["kind"]: i for i in resp.get_json()["data"]["items"]}
    assert set(items) == {"assistant", "external"}  # kind=None 会被列默认值顶掉
    assert items["assistant"]["agent_count"] == 1
    assert items["assistant"]["avg_completion_hours"] is not None
    assert items["external"]["completion_rate"] == 0.0
    assert items["external"]["failure_rate"] == 100.0
    # assistant 完成率 100 排前
    order = [i["kind"] for i in resp.get_json()["data"]["items"]]
    assert order == ["assistant", "external"]


def test_by_kind_empty_workspace(client, db_session):
    user = _make_user(db_session)
    resp = client.get(f"{BASE_URL}/agents/productivity/by-kind", headers=_headers(user))
    assert resp.get_json()["data"] == {"days": 30, "items": []}


# ────────────────────────── 小时热力图 ──────────────────────────

def test_hourly_heatmap_matrix_peak_and_limit(client, db_session):
    user = _make_user(db_session)
    a1 = _make_agent(db_session, user, name="h1")
    a2 = _make_agent(db_session, user, name="h2")
    for hour in (9, 9, 10):
        completed = _now(days=1).replace(hour=hour)
        _done(db_session, a1.id, completed)
    _done(db_session, a2.id, _now(days=1).replace(hour=22))

    resp = client.get(f"{BASE_URL}/agents/productivity/hourly-heatmap?limit=1",
                      headers=_headers(user))
    data = resp.get_json()["data"]
    assert data["max_cell"] == 2
    assert data["peak_hour"] in (9, 10, 22)
    assert len(data["agents"]) == 1  # limit 截断，done 最多者居首
    assert data["agents"][0]["name"] == "h1" and data["agents"][0]["done"] == 3
    assert data["matrix"][str(a1.id)]["9"] == 2  # JSON 化后键是字符串
    assert data["hour_totals"][9] == 2 and data["hour_totals"][10] == 1


def test_hourly_heatmap_no_done_rows(client, db_session):
    user = _make_user(db_session)
    _make_agent(db_session, user)
    resp = client.get(f"{BASE_URL}/agents/productivity/hourly-heatmap",
                      headers=_headers(user))
    data = resp.get_json()["data"]
    assert data["matrix"] == {} and data["peak_hour"] is None
    assert data["max_cell"] == 0 and sum(data["hour_totals"]) == 0


def test_hourly_heatmap_empty_workspace(client, db_session):
    user = _make_user(db_session)
    resp = client.get(f"{BASE_URL}/agents/productivity/hourly-heatmap",
                      headers=_headers(user))
    assert resp.get_json()["data"] == {"days": 30, "agents": [], "matrix": {},
                                       "max_cell": 0, "peak_hour": None}


# ────────────────────────── 日历热力图 ──────────────────────────

def test_calendar_heatmap_matrix_and_date_range(client, db_session):
    user = _make_user(db_session)
    a1 = _make_agent(db_session, user, name="cal")
    _done(db_session, a1.id, _now(days=0))
    _done(db_session, a1.id, _now(days=1))

    resp = client.get(f"{BASE_URL}/agents/productivity/calendar-heatmap?days=3",
                      headers=_headers(user))
    data = resp.get_json()["data"]
    assert data["days"] == 3
    assert data["max_cell"] >= 1
    assert len(data["date_range"]) <= 4  # since+1 .. today
    assert data["agents"][0]["agent_id"] == a1.id
    assert sum(data["matrix"][str(a1.id)].values()) == 2


def test_calendar_heatmap_empty_workspace(client, db_session):
    user = _make_user(db_session)
    resp = client.get(f"{BASE_URL}/agents/productivity/calendar-heatmap?days=abc",
                      headers=_headers(user))
    data = resp.get_json()["data"]
    assert data == {"days": 90, "agents": [], "matrix": {},
                    "max_cell": 0, "date_range": []}


# ────────────────────────── 周对比 ──────────────────────────

def test_weekly_comparison_change_branches(client, db_session):
    user = _make_user(db_session)
    ramp = _make_agent(db_session, user, name="ramp")     # 上周 1 → 本周 2
    slow = _make_agent(db_session, user, name="slow")     # 上周 2 → 本周 1
    new = _make_agent(db_session, user, name="new")       # 仅本周 → +100
    import datetime as dt
    this_monday = (dt.datetime.utcnow()
                   - dt.timedelta(days=dt.datetime.utcnow().weekday()))
    this_monday = this_monday.replace(hour=8, microsecond=0)
    last_week = this_monday - dt.timedelta(weeks=1, hours=2)

    _done(db_session, ramp.id, this_monday + dt.timedelta(hours=1))
    _done(db_session, ramp.id, this_monday + dt.timedelta(hours=2))
    _done(db_session, ramp.id, last_week)
    _done(db_session, slow.id, this_monday + dt.timedelta(hours=1))
    _done(db_session, slow.id, last_week)
    _done(db_session, slow.id, last_week - dt.timedelta(hours=1))
    _done(db_session, new.id, this_monday + dt.timedelta(hours=3))

    resp = client.get(f"{BASE_URL}/agents/productivity/weekly-comparison",
                      headers=_headers(user))
    data = resp.get_json()["data"]
    agents = {a["name"]: a for a in data["agents"]}
    assert agents["ramp"] == {"agent_id": agents["ramp"]["agent_id"],
                              "name": "ramp", "this_week": 2, "last_week": 1,
                              "change_pct": 100.0}
    assert agents["slow"]["change_pct"] == -50.0
    assert agents["new"]["change_pct"] == 100.0
    assert data["total_this_week"] == 4 and data["total_last_week"] == 3


def test_weekly_comparison_empty_workspace(client, db_session):
    user = _make_user(db_session)
    resp = client.get(f"{BASE_URL}/agents/productivity/weekly-comparison?limit=abc",
                      headers=_headers(user))
    assert resp.get_json()["data"] == {"agents": [], "total_this_week": 0,
                                       "total_last_week": 0}


# ────────────────────────── 闲置排行 ──────────────────────────

def test_idle_ranking_stages_and_never(client, db_session):
    user = _make_user(db_session)
    active = _make_agent(db_session, user, name="active")
    stale = _make_agent(db_session, user, name="stale")
    never = _make_agent(db_session, user, name="never")
    active.last_seen_at = _now(hours=2)
    stale.last_seen_at = _now(days=400)
    db_session.commit()
    # stale 的最后活动其实是 10 天前的完成（heartbeat 更早）
    _assign(db_session, stale.id, "done", completed_at=_now(days=10),
            last_heartbeat_at=_now(days=12))

    resp = client.get(f"{BASE_URL}/agents/idle-ranking?limit=10",
                      headers=_headers(user))
    data = resp.get_json()["data"]
    assert data["total_agents"] == 3
    stages = {a["agent_name"]: a["stage"] for a in data["agents"]}
    assert stages == {"active": "active", "stale": "stale", "never": "never"}
    assert data["stage_counts"] == {"active": 1, "stale": 1, "never": 1}
    # 闲置最久的排最前；never（None → 排序键 -inf）按既有行为排最前
    by_name = {a["agent_name"]: a for a in data["agents"]}
    assert by_name["stale"]["stage"] == "stale"
    assert by_name["active"]["stage"] == "active"
    assert by_name["never"]["stage"] == "never"


def test_idle_ranking_dormant_and_null_timestamp_rows(client, db_session):
    """dormant 档 + 双时间戳皆空的 assignment 行（continue 分支）。"""
    user = _make_user(db_session)
    dormant = _make_agent(db_session, user, name="dormant")
    dormant.last_seen_at = _now(days=45)
    db_session.commit()
    _assign(db_session, dormant.id, "done", completed_at=None,
            last_heartbeat_at=None)
    resp = client.get(f"{BASE_URL}/agents/idle-ranking", headers=_headers(user))
    data = resp.get_json()["data"]
    dormant_item = {a["agent_name"]: a for a in data["agents"]}["dormant"]
    assert dormant_item["stage"] == "dormant"
    assert dormant_item["idle_hours"] == pytest.approx(45 * 24, abs=2.0)


def test_alerts_empty_workspace_short_shape(client, db_session):
    user = _make_user(db_session)
    resp = client.get(f"{BASE_URL}/agents/productivity/alerts", headers=_headers(user))
    # 原始行为：空工作区时响应只有 days + items（沿用，避免前端兼容问题）
    assert resp.get_json()["data"] == {"days": 30, "items": []}


def test_idle_ranking_heartbeat_newer_than_completion(client, db_session):
    user = _make_user(db_session)
    a = _make_agent(db_session, user, name="hb")
    a.last_seen_at = _now(days=40)  # dormant via last_seen
    db_session.commit()
    _assign(db_session, a.id, "done", completed_at=_now(days=50),
            last_heartbeat_at=_now(days=2))  # heartbeat 更新 → idle 档
    resp = client.get(f"{BASE_URL}/agents/idle-ranking", headers=_headers(user))
    agent = resp.get_json()["data"]["agents"][0]
    assert agent["stage"] == "idle"
    assert agent["idle_hours"] == pytest.approx(48.0, abs=1.0)
