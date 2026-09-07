"""Agent 健康监控（services/agent_health.py）单元回归。

历史 bug 钉子：本模块原先查询 AgentTaskLease 上不存在的 leased_at /
is_complete / completed_at 列（任何调用必然 AttributeError → 端点 500），
已改为真实列（created_at / active / expires_at / updated_at）。
覆盖：单 Agent 三态判定、工作区巡检、汇总统计、单例。
"""

import uuid
from datetime import datetime, timedelta

import pytest

from models import Agent, AgentStatus, AgentTaskLease, db
from services.agent_health import (
    AgentHealthMonitor,
    AgentHealthStatus,
    get_health_monitor,
)


@pytest.fixture(scope="function", autouse=True)
def _isolated_app():
    from app import create_app
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
def agent():
    def _make(workspace_id=1, status=AgentStatus.ACTIVE):
        from models import User
        user = User(username=f"hu_{uuid.uuid4().hex[:8]}",
                    email=f"hu_{uuid.uuid4().hex[:6]}@t.io")
        db.session.add(user)
        db.session.flush()
        row = Agent(workspace_id=workspace_id, owner_id=user.id,
                    creator_user_id=user.id, name=f"h_{uuid.uuid4().hex[:6]}",
                    status=status)
        db.session.add(row)
        db.session.commit()
        return row
    return _make


def _lease(ag, *, active=True, expires_in=600, created_ago=0, released_ago=0):
    now = datetime.utcnow()
    row = AgentTaskLease(
        lease_id=f"l-{uuid.uuid4().hex[:12]}",
        task_id=abs(hash(uuid.uuid4().hex)) % (10 ** 8),
        attempt_id=f"a-{uuid.uuid4().hex[:8]}",
        agent_id=ag.id, workspace_id=ag.workspace_id,
        expires_at=now + timedelta(seconds=expires_in),
        active=active,
    )
    db.session.add(row)
    db.session.flush()
    if created_ago:
        AgentTaskLease.query.filter_by(id=row.id).update(
            {"created_at": now - timedelta(seconds=created_ago)})
    if released_ago:
        row.active = False
        AgentTaskLease.query.filter_by(id=row.id).update(
            {"active": False, "updated_at": now - timedelta(seconds=released_ago)})
    db.session.commit()
    return row


class TestCheckAgentHealth:
    def test_unknown_agent(self):
        result = AgentHealthMonitor().check_agent_health(999999)
        assert result == {"status": AgentHealthStatus.UNKNOWN,
                          "error": "Agent not found"}

    def test_online_with_recent_lease(self, agent):
        ag = agent()
        _lease(ag, created_ago=10)  # 10 秒前有租约活动
        result = AgentHealthMonitor().check_agent_health(ag.id)
        assert result["status"] == AgentHealthStatus.ONLINE
        assert result["is_online"] is True
        assert result["last_heartbeat"] is not None
        assert result["task_stats"]["active"] == 1

    def test_offline_when_heartbeat_stale(self, agent):
        ag = agent()
        _lease(ag, created_ago=3600)  # 1 小时前
        result = AgentHealthMonitor().check_agent_health(ag.id)
        assert result["status"] == AgentHealthStatus.OFFLINE
        assert result["is_online"] is False

    def test_offline_without_any_lease(self, agent):
        ag = agent()
        # 无租约时回退到 agent.updated_at——把它拨旧到 2 小时前
        Agent.query.filter_by(id=ag.id).update(
            {"updated_at": datetime.utcnow() - timedelta(hours=2)})
        db.session.commit()
        result = AgentHealthMonitor().check_agent_health(ag.id)
        assert result["status"] == AgentHealthStatus.OFFLINE
        assert result["last_heartbeat"] is not None  # 回退值=旧 updated_at

    def test_degraded_when_queue_over_threshold(self, agent, monkeypatch):
        ag = agent()
        _lease(ag)
        _lease(ag)
        monitor = AgentHealthMonitor()
        monkeypatch.setattr(monitor, "QUEUE_ALERT_THRESHOLD", 1)
        result = monitor.check_agent_health(ag.id)
        assert result["status"] == AgentHealthStatus.DEGRADED

    def test_completed_today_counts_released_leases(self, agent):
        ag = agent()
        _lease(ag, released_ago=60)  # 今天释放
        result = AgentHealthMonitor().check_agent_health(ag.id)
        assert result["task_stats"]["completed_today"] == 1


class TestWorkspaceChecks:
    def test_only_active_agents_checked(self, agent):
        online = agent()
        _lease(online, created_ago=10)
        agent()  # ACTIVE 无租约 → offline
        agent(status=AgentStatus.DISABLED)  # 不巡检

        results = AgentHealthMonitor().check_workspace_agents(1)
        assert len(results) == 2

    def test_summary_counts(self, agent):
        online = agent()
        _lease(online, created_ago=10)
        stale = agent()  # 拨旧 → offline（新建 agent 会被 updated_at 回退判为 online）
        Agent.query.filter_by(id=stale.id).update(
            {"updated_at": datetime.utcnow() - timedelta(hours=2)})
        db.session.commit()
        summary = AgentHealthMonitor().get_health_summary(1)
        assert summary["total"] == 2
        assert summary["online"] == 1
        assert summary["offline"] == 1
        assert summary["degraded"] == 0
        assert summary["online_rate"] == 50.0

    def test_summary_empty_workspace(self):
        summary = AgentHealthMonitor().get_health_summary(424242)
        assert summary == {"total": 0, "online": 0, "offline": 0,
                           "degraded": 0, "online_rate": 0, "agents": []}


    def test_heartbeat_none_without_lease_and_agent(self):
        monitor = AgentHealthMonitor()
        assert monitor._get_last_heartbeat(987654) is None


def test_singleton():
    assert get_health_monitor() is get_health_monitor()
    assert isinstance(get_health_monitor(), AgentHealthMonitor)
