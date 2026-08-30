"""Tests for P2.6 budget/quota enforcement (model, service, gates)."""

from datetime import datetime, timedelta

import pytest

from services.budget_service import check_budgets, raise_budget_exceeded

BASE_URL = "/todo-for-ai/api/v1"


@pytest.fixture(autouse=True)
def _cleanup_budget_rows(db_session):
    """预算相关行跨测试清理（会话级测试库的 id 回收会互相污染）。"""
    from models import (
        AgentRun, AgentTaskEvent, AgentTaskLease, AIRequestLog, Budget,
    )

    def _purge():
        db_session.rollback()
        db_session.query(AgentTaskEvent).delete(synchronize_session=False)
        db_session.query(AgentTaskLease).delete(synchronize_session=False)
        db_session.query(AgentRun).delete(synchronize_session=False)
        db_session.query(AIRequestLog).delete(synchronize_session=False)
        db_session.query(Budget).delete(synchronize_session=False)
        db_session.commit()

    _purge()
    yield
    _purge()


@pytest.fixture
def budget_factory(db_session):
    from models import Budget

    def _create(**kwargs):
        defaults = {
            "scope_type": "agent",
            "workspace_id": 1,
            "resource": "concurrent",
            "limit_value": 5,
            "period": "total",
        }
        defaults.update(kwargs)
        budget = Budget(**defaults)
        db_session.add(budget)
        db_session.commit()
        return budget

    yield _create


class TestBudgetModel:
    def test_create_budget(self, db_session, budget_factory):
        budget = budget_factory(resource="tokens", limit_value=100000, period="monthly")
        assert budget.id is not None
        assert budget.is_active is True
        assert "tokens" in repr(budget)

    def test_to_dict_fields(self, db_session, budget_factory):
        budget = budget_factory()
        data = budget.to_dict()
        assert data["resource"] == "concurrent"
        assert data["limit_value"] == 5
        assert data["period"] == "total"


class TestPeriodStart:
    def test_windows(self):
        from services.budget_service import period_start

        now = datetime(2026, 8, 31, 10, 30)
        assert period_start("total", now) is None
        assert period_start("daily", now) == datetime(2026, 8, 31)
        assert period_start("weekly", now) == datetime(2026, 8, 31) - timedelta(days=0 if now.weekday() == 0 else now.weekday())
        monthly = period_start("monthly", now)
        assert monthly.day == 1 and monthly.hour == 0


class TestBudgetChecks:
    def test_concurrent_budget_violation(self, db_session, budget_factory, organization_factory, agent_factory):
        from models import AgentTaskLease

        org = organization_factory()
        agent = agent_factory(workspace_id=org.id)
        budget = budget_factory(
            scope_type="agent", agent_id=agent.id, workspace_id=org.id,
            resource="concurrent", limit_value=1,
        )
        db_session.add(AgentTaskLease(
            lease_id="lea_b1", task_id=1, attempt_id="att_b1",
            agent_id=agent.id, workspace_id=org.id,
            expires_at=datetime.utcnow() + timedelta(seconds=300), active=True, created_by="test",
        ))
        db_session.commit()

        from services.budget_service import check_budgets

        violations = check_budgets(workspace_id=org.id, agent_id=agent.id)
        concurrent = [v for v in violations if v["resource"] == "concurrent"]
        assert concurrent and concurrent[0]["used"] >= 1 and concurrent[0]["budget_id"] == budget.id

    def test_duration_budget(self, db_session, budget_factory, organization_factory, agent_factory):
        from models import AgentRun

        org = organization_factory()
        agent = agent_factory(workspace_id=org.id)
        budget_factory(
            scope_type="agent", agent_id=agent.id, workspace_id=org.id,
            resource="duration_minutes", limit_value=10,
        )
        now = datetime.utcnow()
        db_session.add(AgentRun(
            run_id="run_dur1", workspace_id=org.id, agent_id=agent.id,
            trigger_id=None, trigger_reason="t", state="succeeded",
            scheduled_at=now - timedelta(minutes=30),
            started_at=now - timedelta(minutes=30), ended_at=now,
            attempt_count=1, created_by="test",
        ))
        db_session.commit()

        from services.budget_service import check_budgets

        from services.budget_service import check_budgets

        violations = check_budgets(workspace_id=org.id, agent_id=agent.id)
        duration = [v for v in violations if v["resource"] == "duration_minutes"]
        assert duration and duration[0]["used"] >= 25  # ~30 分钟

        # 清理 run 行：agent teardown 会尝试把 runs 的 agent_id 置空（NOT NULL 冲突）
        db_session.query(AgentRun).delete(synchronize_session=False)
        db_session.commit()

    def test_workspace_token_budget(self, db_session, budget_factory, organization_factory):
        from models import AIRequestLog

        org = organization_factory()  # owner 自动创建并登记
        budget = budget_factory(
            scope_type="workspace", workspace_id=org.id,
            resource="tokens", limit_value=100, period="monthly",
        )
        db_session.add(AIRequestLog(
            request_id="req_tok1", user_id=org.owner_id, feature="test",
            prompt_tokens=60, completion_tokens=50, total_tokens=110,
        ))
        db_session.commit()

        from services.budget_service import check_budgets

        violations = check_budgets(workspace_id=org.id)
        tokens = [v for v in violations if v["resource"] == "tokens"]
        assert tokens and tokens[0]["used"] >= 110

    def test_agent_scope_tokens_not_tracked(self, db_session, budget_factory, organization_factory, agent_factory):
        org = organization_factory()
        agent = agent_factory(workspace_id=org.id)
        budget_factory(
            scope_type="agent", agent_id=agent.id, workspace_id=org.id,
            resource="tokens", limit_value=100,
        )
        from services.budget_service import check_budgets

        violations = check_budgets(workspace_id=org.id, agent_id=agent.id)
        assert not [v for v in violations if v["resource"] == "tokens"]


class TestBudgetExceededEvents:
    def test_raise_is_idempotent_within_period(self, db_session, budget_factory, organization_factory, agent_factory, project_factory, task_factory):
        from services.budget_service import raise_budget_exceeded
        from models import AgentTaskEvent

        org = organization_factory()
        agent = agent_factory(workspace_id=org.id)
        project = project_factory(owner_id=agent.creator_user_id, organization_id=org.id)
        task = task_factory(project_id=project.id, owner_id=org.id)
        budget_factory(
            scope_type="agent", agent_id=agent.id, workspace_id=org.id,
            resource="concurrent", limit_value=1,
        )
        violations = [{"budget_id": 1, "scope_type": "agent", "resource": "concurrent",
                       "period": "total", "limit": 1, "used": 2}]

        created = raise_budget_exceeded(org.id, violations, context={"agent_id": agent.id, "task_id": task.id})
        assert len(created) == 1
        again = raise_budget_exceeded(org.id, violations, context={"agent_id": agent.id, "task_id": task.id})
        assert again == []  # 幂等：同预算 pending 事件存在则不重复

        event = AgentTaskEvent.query.filter_by(
            workspace_id=org.id, event_type="interaction_request"
        ).first()
        assert event.payload["interaction_type"] == "budget_exceeded"


class TestPullBudgetGate:
    def test_pull_gate_blocks_dispatch(
        self, client, db_session, user_factory, organization_factory, agent_factory,
        project_factory, task_factory, budget_factory,
    ):
        """直接构造 runtime 会话（introspect），预算超限后 pull 返回空 + budget_block。"""
        from models import AgentKey, AgentTaskEvent
        import uuid
        from flask_jwt_extended import create_access_token

        user = user_factory()
        org = organization_factory(owner_id=user.id)
        agent = agent_factory(workspace_id=org.id, runner_enabled=True)
        key_row, raw_key = AgentKey.generate_key(
            name=f"Budget Key {uuid.uuid4().hex[:6]}", workspace_id=org.id,
            agent_id=agent.id, created_by_user_id=user.id,
        )
        db_session.add(key_row)
        db_session.commit()

        auth_resp = client.post(f"{BASE_URL}/agent/auth/introspect", json={"agent_key": raw_key})
        assert auth_resp.status_code == 200
        agent_headers = {"Authorization": f"Bearer {auth_resp.get_json()['data']['access_token']}"}

        project = project_factory(owner_id=user.id, organization_id=org.id)
        task = task_factory(project_id=project.id, owner_id=org.id, is_ai_task=True, title="Budget gate")
        budget_factory(
            scope_type="agent", agent_id=agent.id, workspace_id=org.id,
            resource="duration_minutes", limit_value=1, period="total",
        )
        # 制造已用量：30 分钟的 run
        from models import AgentRun
        now = datetime.utcnow()
        db_session.add(AgentRun(
            run_id="run_budget_gate", workspace_id=org.id, agent_id=agent.id,
            trigger_id=None, trigger_reason="t", state="succeeded",
            scheduled_at=now - timedelta(minutes=30),
            started_at=now - timedelta(minutes=30), ended_at=now,
            attempt_count=1, created_by="test",
        ))
        db_session.commit()

        resp = client.post(f"{BASE_URL}/agent/tasks/pull", json={"max_tasks": 1}, headers=agent_headers)
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["tasks"] == []
        assert data["budget_block"]["blocked"] is True

        event = AgentTaskEvent.query.filter_by(workspace_id=org.id, event_type="interaction_request").first()
        assert event is not None
        assert event.payload["interaction_type"] == "budget_exceeded"

        db_session.query(AgentRun).delete(synchronize_session=False)
        db_session.commit()

    def test_pull_allows_within_budget(
        self, client, db_session, user_factory, organization_factory, agent_factory,
        project_factory, task_factory, budget_factory,
    ):
        from models import AgentKey
        import uuid

        user = user_factory()
        org = organization_factory(owner_id=user.id)
        agent = agent_factory(workspace_id=org.id, runner_enabled=True)
        key_row, raw_key = AgentKey.generate_key(
            name=f"Budget Key {uuid.uuid4().hex[:6]}", workspace_id=org.id,
            agent_id=agent.id, created_by_user_id=user.id,
        )
        db_session.add(key_row)
        db_session.commit()

        auth_resp = client.post(f"{BASE_URL}/agent/auth/introspect", json={"agent_key": raw_key})
        agent_headers = {"Authorization": f"Bearer {auth_resp.get_json()['data']['access_token']}"}

        project = project_factory(owner_id=user.id, organization_id=org.id)
        task_factory(project_id=project.id, owner_id=org.id, is_ai_task=True, title="Within budget")
        budget_factory(
            scope_type="agent", agent_id=agent.id, workspace_id=org.id,
            resource="concurrent", limit_value=5,
        )

        resp = client.post(f"{BASE_URL}/agent/tasks/pull", json={"max_tasks": 1}, headers=agent_headers)
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert len(data["tasks"]) == 1
        assert "budget_block" not in data
