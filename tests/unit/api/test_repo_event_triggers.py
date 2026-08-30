"""Tests for P2.5 repo event triggers (GitHub PR events driving AgentRun)."""

from datetime import datetime, timedelta

import json
import hmac as hmac_mod
import hashlib
import sys
import os
import uuid
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../"))

BASE_URL = "/todo-for-ai/api/v1"
WEBHOOK_SECRET = "test-webhook-secret"


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

    return _create


@pytest.fixture
def app_configured(db_session, monkeypatch):
    """webhook secret 环境变量（App 配置表未配置时的回退）。"""
    monkeypatch.setenv("GITHUB_APP_WEBHOOK_SECRET", WEBHOOK_SECRET)


@pytest.fixture(autouse=True)
def _cleanup_rows(db_session):
    from models import AgentRun, AgentTaskEvent, AgentTaskLease, AgentTrigger, Budget

    def _purge():
        db_session.rollback()
        db_session.query(AgentTaskEvent).delete(synchronize_session=False)
        db_session.query(AgentTaskLease).delete(synchronize_session=False)
        db_session.query(AgentRun).delete(synchronize_session=False)
        db_session.query(AgentTrigger).delete(synchronize_session=False)
        db_session.query(Budget).delete(synchronize_session=False)
        db_session.commit()

    _purge()
    yield
    _purge()


@pytest.fixture
def repo_setup(db_session, user_factory, organization_factory, agent_factory, project_factory, task_factory):
    """workspace + agent + org 绑定项目 + 任务。

    yield 后清理本上下文创建的 AgentRun 行——agent teardown 会尝试把
    runs.agent_id 置空（NOT NULL），必须先删行。
    """
    created = []

    def _create(*, allowed=True):
        user = user_factory()
        org = organization_factory(owner_id=user.id)
        agent = agent_factory(workspace_id=org.id, runner_enabled=True)
        project = project_factory(owner_id=user.id, organization_id=org.id)
        task = task_factory(
            project_id=project.id, owner_id=org.id, is_ai_task=True, title="Repo event task",
        )
        from models import TaskEvidenceRecord
        db_session.add(TaskEvidenceRecord(
            task_id=task.id, evidence_type="pr", status="unknown",
            detail={"pr_number": 5, "repo": "acme/widget"}, created_by="test",
        ))
        db_session.commit()
        ctx = {"user": user, "org": org, "agent": agent, "project": project, "task": task}
        created.append(ctx)
        return ctx

    yield _create

    from models import AgentRun
    db_session.rollback()
    for ctx in created:
        db_session.query(AgentRun).filter_by(agent_id=ctx["agent"].id).delete(synchronize_session=False)
    db_session.commit()


def _make_repo_trigger(db_session, org, agent, *, event_types, repo_full_names=None, project_ids=None):
    from models import AgentTrigger

    trigger = AgentTrigger(
        workspace_id=org.id,
        agent_id=agent.id,
        name=f"repo trigger {uuid.uuid4().hex[:6]}",
        trigger_type="repo_event",
        enabled=True,
        task_event_types=event_types,
        task_filter={
            **({"repo_full_names": repo_full_names} if repo_full_names else {}),
            **({"project_ids": project_ids} if project_ids else {}),
        },
        created_by="test",
    )
    db_session.add(trigger)
    db_session.commit()
    return trigger


def _signed_post(client, payload):
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac_mod.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        f"{BASE_URL}/github/app/webhook",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": sig,
        },
    )


class TestRepoEventTriggers:
    def test_opened_event_creates_agent_run(self, client, db_session, app_configured, repo_setup):
        from models import AgentRun

        ctx = repo_setup()
        _make_repo_trigger(db_session, ctx["org"], ctx["agent"], event_types=["pull_request.opened"])

        resp = _signed_post(client, {
            "action": "opened",
            "pull_request": {"number": 5, "state": "open", "merged": False,
                             "head": {"ref": "agent/x"}, "base": {"ref": "main"}},
            "repository": {"full_name": "acme/widget"},
        })
        assert resp.status_code == 200

        runs = AgentRun.query.filter_by(workspace_id=ctx["org"].id).all()
        assert len(runs) == 1
        assert runs[0].trigger_reason == "repo.pull_request.opened"
        assert runs[0].input_payload["repo_full_name"] == "acme/widget"
        assert runs[0].input_payload["task_id"] == ctx["task"].id

    def test_event_type_mismatch_does_not_fire(self, client, db_session, app_configured, repo_setup):
        from models import AgentRun

        ctx = repo_setup()
        _make_repo_trigger(db_session, ctx["org"], ctx["agent"], event_types=["pull_request.merged"])

        _signed_post(client, {
            "action": "opened",
            "pull_request": {"number": 5, "state": "open", "merged": False},
            "repository": {"full_name": "acme/widget"},
        })

        assert AgentRun.query.count() == 0

    def test_repo_filter_scopes_triggers(self, client, db_session, app_configured, repo_setup, task_factory):
        from models import AgentRun

        ctx = repo_setup()
        _make_repo_trigger(
            db_session, ctx["org"], ctx["agent"],
            event_types=["pull_request.opened"],
            repo_full_names=["other/repo"],
        )

        _signed_post(client, {
            "action": "opened",
            "pull_request": {"number": 5, "state": "open", "merged": False},
            "repository": {"full_name": "acme/widget"},
        })

        assert AgentRun.query.count() == 0

        # 匹配仓库后触发（为 other/repo 建任务与 PR 证据，webhook 依赖证据定位任务）
        from models import TaskEvidenceRecord
        task2 = task_factory(
            project_id=ctx["project"].id, owner_id=ctx["user"].id, title="Other repo task",
        )
        db_session.add(TaskEvidenceRecord(
            task_id=task2.id, evidence_type="pr", status="unknown",
            detail={"pr_number": 6, "repo": "other/repo"}, created_by="test",
        ))
        db_session.commit()

        _signed_post(client, {
            "action": "opened",
            "pull_request": {"number": 6, "state": "open", "merged": False},
            "repository": {"full_name": "other/repo"},
        })
        assert AgentRun.query.count() == 1

    def test_budget_gate_blocks_repo_run(self, client, db_session, app_configured, repo_setup, budget_factory):
        from models import AgentRun, AgentTaskEvent

        ctx = repo_setup()
        _make_repo_trigger(db_session, ctx["org"], ctx["agent"], event_types=["pull_request.merged"])
        budget_factory(
            scope_type="agent", agent_id=ctx["agent"].id, workspace_id=ctx["org"].id,
            resource="concurrent", limit_value=0,
        )

        from api.agent_trigger_engine import emit_repo_event
        from services.budget_service import check_budgets
        print("GATE VIOLATIONS:", check_budgets(
            workspace_id=ctx["org"].id, agent_id=ctx["agent"].id,
        ))
        runs = emit_repo_event(
            ctx["task"], "pull_request.merged",
            payload={"pr_number": 5, "merged": True},
            repo_full_name="acme/widget",
        )

        assert runs == []
        assert AgentRun.query.count() == 0
        event = AgentTaskEvent.query.filter_by(workspace_id=ctx["org"].id).first()
        assert event is not None
        assert event.payload["interaction_type"] == "budget_exceeded"

    def test_idempotency_same_event_not_duplicated(self, client, db_session, app_configured, repo_setup):
        from models import AgentRun

        ctx = repo_setup()
        trigger = _make_repo_trigger(db_session, ctx["org"], ctx["agent"], event_types=["pull_request.opened"])

        payload = {
            "action": "opened",
            "pull_request": {"number": 5, "state": "open", "merged": False},
            "repository": {"full_name": "acme/widget"},
        }
        _signed_post(client, payload)
        _signed_post(client, payload)

        runs = AgentRun.query.filter_by(trigger_id=trigger.id).all()
        assert len(runs) == 1
