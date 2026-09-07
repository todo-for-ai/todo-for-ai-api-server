"""预算服务（services/budget_service.py）缺口补测。

补齐：月度周期起点、无成员组织 token 用量回 0、agent 范围过滤
（时长/并发）、未知资源 not_tracked、超限事件缺 task_id 拒绝、
非请求上下文下审计写入降级为日志。
"""

import uuid
from datetime import datetime

import pytest

from models import AgentRun, AgentTaskLease, Budget, db
from services.budget_service import (
    get_usage,
    period_start,
    raise_budget_exceeded,
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


def _budget(workspace_id=1, **kw):
    defaults = {
        "workspace_id": workspace_id, "scope_type": "workspace",
        "resource": "tokens", "limit_value": 1000, "period": "total",
        "is_active": True,
    }
    defaults.update(kw)
    row = Budget(**defaults)
    db.session.add(row)
    db.session.commit()
    return row


class TestPeriodStart:
    def test_monthly_returns_first_of_month(self):
        now = datetime(2026, 9, 8, 15, 30, 45)
        assert period_start("monthly", now) == datetime(2026, 9, 1)

    def test_daily_and_total_and_unknown(self):
        now = datetime(2026, 9, 8, 15, 30, 45)
        assert period_start("daily", now) == datetime(2026, 9, 8)
        assert period_start("total", now) is None
        assert period_start("fortnightly", now) is None  # 未知周期回 None


class TestGetUsageGaps:
    def test_token_usage_zero_without_known_members(self):
        # 工作区不存在（无成员可归属）→ token 用量记 0
        budget = _budget(workspace_id=999999)
        assert get_usage(budget) == {"used": 0, "not_tracked": False}

    def test_duration_agent_scope_filter(self):
        budget = _budget(scope_type="agent", agent_id=31,
                         resource="duration_minutes", period="monthly")
        db.session.add(AgentRun(
            workspace_id=1, agent_id=31,
            started_at=datetime(2026, 9, 8, 1, 0, 0),
            ended_at=datetime(2026, 9, 8, 1, 30, 0),
            state="succeeded",
        ))
        db.session.add(AgentRun(  # 其他 agent，不应计入
            workspace_id=1, agent_id=99,
            started_at=datetime(2026, 9, 8, 1, 0, 0),
            ended_at=datetime(2026, 9, 8, 2, 0, 0),
            state="succeeded",
        ))
        db.session.commit()
        assert get_usage(budget) == {"used": 30, "not_tracked": False}

    def test_concurrent_agent_scope_filter(self):
        budget = _budget(scope_type="agent", agent_id=31,
                         resource="concurrent", period="total")
        db.session.add(AgentTaskLease(
            lease_id=f"l-{uuid.uuid4().hex[:10]}", task_id=1,
            attempt_id="a1", agent_id=31, workspace_id=1,
            expires_at=datetime(2026, 9, 30), active=True,
        ))
        db.session.commit()
        assert get_usage(budget) == {"used": 1, "not_tracked": False}

    def test_unknown_resource_not_tracked(self):
        budget = _budget(resource="gpu_hours")
        assert get_usage(budget) == {"used": 0, "not_tracked": True}


class TestRaiseBudgetExceeded:
    def test_requires_task_id(self):
        with pytest.raises(ValueError, match="require a task_id"):
            raise_budget_exceeded(1, [{"budget_id": 5}], context={})

    def test_audit_runtime_error_degrades_to_log(
            self, monkeypatch):
        def boom(**kw):
            raise RuntimeError("no request context")
        monkeypatch.setattr("api.agent_common.write_agent_audit", boom)

        created = raise_budget_exceeded(
            1, [{"budget_id": 5, "scope_type": "workspace"}],
            context={"task_id": 1})
        assert len(created) == 1
        assert created[0].startswith("recp-") or created[0]
