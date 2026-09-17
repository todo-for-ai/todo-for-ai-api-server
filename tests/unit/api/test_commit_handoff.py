"""commit 携带交接上下文（原子落库）+ shared-context 署名三通道 + 修复任务带原始描述。"""

import uuid

import pytest

from flask_jwt_extended import create_access_token

BASE_URL = "/todo-for-ai/api/v1"


def _make_runtime_world(client, db_session, user_factory, organization_factory, agent_factory, project_factory, task_factory):
    """用户+组织+项目+Agent+key，换 daemon 会话；任务由调用方按拓扑建。"""
    from models import AgentKey

    user = user_factory()
    org = organization_factory(owner_id=user.id)
    agent = agent_factory(workspace_id=org.id, creator_user_id=user.id)
    project = project_factory(owner_id=user.id, organization_id=org.id)

    key_row, raw_key = AgentKey.generate_key(
        name=f"handoff-{uuid.uuid4().hex[:6]}",
        workspace_id=org.id,
        agent_id=agent.id,
        created_by_user_id=user.id,
    )
    db_session.add(key_row)
    db_session.commit()

    auth_resp = client.post(f"{BASE_URL}/agent/auth/introspect", json={"agent_key": raw_key})
    token = auth_resp.get_json()["data"]["access_token"]

    return {
        "user": user, "org": org, "agent": agent, "project": project,
        "runtime_headers": {"Authorization": f"Bearer {token}"},
        "user_headers": {"Authorization": f"Bearer {create_access_token(identity=str(user.id))}"},
    }


def _mk_task(task_factory, project, org, title, blocked=None, content="do it"):
    return task_factory(
        project_id=project.id, owner_id=org.id, title=title, content=content,
        is_ai_task=True, blocked_by_task_ids=(blocked or []),
    )


def _commit(client, db_session, ctx, task, status="succeeded", shared=None):
    from datetime import datetime, timedelta

    from models import AgentTaskAttempt, AgentTaskAttemptState, AgentTaskLease

    attempt_id = f"att_{uuid.uuid4().hex[:8]}"
    lease_id = f"lea_{uuid.uuid4().hex[:8]}"
    now = datetime.utcnow()
    db_session.add(AgentTaskAttempt(
        attempt_id=attempt_id, task_id=task.id, agent_id=ctx["agent"].id,
        workspace_id=ctx["org"].id, state=AgentTaskAttemptState.ACTIVE,
        lease_id=lease_id, started_at=now, created_by="test",
    ))
    db_session.add(AgentTaskLease(
        lease_id=lease_id, task_id=task.id, attempt_id=attempt_id,
        agent_id=ctx["agent"].id, workspace_id=ctx["org"].id,
        expires_at=now + timedelta(minutes=10), active=True, version=1, created_by="test",
    ))
    db_session.commit()

    payload = {
        "attempt_id": attempt_id,
        "lease_id": lease_id,
        "status": status,
        "result": {"output": "done", "processed_by": "claude"},
        "failure_code": "FAILED" if status == "failed" else None,
        "failure_reason": "boom" if status == "failed" else None,
    }
    if shared is not None:
        payload["shared_context"] = shared
    return client.post(
        f"{BASE_URL}/agent/tasks/{task.id}/commit",
        json=payload,
        headers={**ctx["runtime_headers"], "Idempotency-Key": attempt_id},
    )


def _cleanup_experiences(db_session):
    """失败自愈写入的 agent_experiences 挂在 agent 上；factory teardown 删
    Agent 会把行 FK 置 NULL 撞 NOT NULL，断言完成后先清掉。"""
    from models import AgentExperience

    db_session.rollback()
    AgentExperience.query.delete()
    db_session.commit()


class TestCommitSharedContext:
    def test_commit_writes_context_atomically(self, client, db_session, user_factory,
                                              organization_factory, agent_factory,
                                              project_factory, task_factory):
        from models import LlmCallMetric  # noqa: F401  (确保 models 全量加载)
        from models import SharedContext

        ctx = _make_runtime_world(client, db_session, user_factory, organization_factory,
                                  agent_factory, project_factory, task_factory)
        t1 = _mk_task(task_factory, ctx["project"], ctx["org"], "upstream")
        t2 = _mk_task(task_factory, ctx["project"], ctx["org"], "downstream", blocked=[t1.id])

        resp = _commit(client, db_session, ctx, t1, shared={"secret": "246810", "plan": "use-x"})
        assert resp.status_code == 200, resp.get_json()

        rows = {r.key: r for r in SharedContext.query.filter_by(task_id=t1.id).all()}
        assert rows["secret"].value == "246810"
        assert rows["secret"].author_agent_id == ctx["agent"].id

        # 下游 pull 的 payload.upstream 直接带交接（无需驱动脚本补写）
        pull = client.post(f"{BASE_URL}/agent/tasks/pull", json={"max_tasks": 1}, headers=ctx["runtime_headers"])
        assert pull.status_code == 200, pull.get_json()
        items = pull.get_json()["data"]["tasks"]
        assert items, "downstream task should be pullable"
        assert items[0]["task_id"] == t2.id
        upstream = items[0].get("upstream") or []
        merged = {k: v for e in upstream for k, v in (e.get("shared_context") or {}).items()}
        assert merged.get("secret") == "246810"
        assert merged.get("plan") == "use-x"

    def test_failed_commit_also_writes_context(self, client, db_session, user_factory,
                                               organization_factory, agent_factory,
                                               project_factory, task_factory):
        from models import SharedContext

        ctx = _make_runtime_world(client, db_session, user_factory, organization_factory,
                                  agent_factory, project_factory, task_factory)
        t1 = _mk_task(task_factory, ctx["project"], ctx["org"], "doomed",
                      content="ORIGINAL-BRIEF-MARKER")
        resp = _commit(client, db_session, ctx, t1, status="failed", shared={"partial": "learned-x"})
        assert resp.status_code == 200, resp.get_json()
        row = SharedContext.query.filter_by(task_id=t1.id, key="partial").first()
        assert row is not None and row.value == "learned-x"
        _cleanup_experiences(db_session)

    def test_invalid_shared_context_rejected(self, client, db_session, user_factory,
                                             organization_factory, agent_factory,
                                             project_factory, task_factory):
        ctx = _make_runtime_world(client, db_session, user_factory, organization_factory,
                                  agent_factory, project_factory, task_factory)
        t1 = _mk_task(task_factory, ctx["project"], ctx["org"], "t")
        resp = _commit(client, db_session, ctx, t1, shared={"bad": {"nested": "dict"}})
        assert resp.status_code == 400
        assert "shared_context" in resp.get_json()["message"]


class TestSharedContextAttribution:
    def test_workspace_agent_can_attribute(self, client, db_session, user_factory,
                                           organization_factory, agent_factory,
                                           project_factory, task_factory):
        """协作侧创建的 Agent（owner_id=NULL）可通过 creator 归属署名（此前必 404）。"""
        ctx = _make_runtime_world(client, db_session, user_factory, organization_factory,
                                  agent_factory, project_factory, task_factory)
        t1 = _mk_task(task_factory, ctx["project"], ctx["org"], "t")
        resp = client.put(
            f"{BASE_URL}/agents/tasks/{t1.id}/shared-context",
            json={"key": "k1", "value": "v1", "agent_id": ctx["agent"].id},
            headers=ctx["user_headers"],
        )
        assert resp.status_code == 201, resp.get_json()
        assert resp.get_json()["data"]["author_agent_id"] == ctx["agent"].id

    def test_outsider_still_denied(self, client, db_session, user_factory,
                                   organization_factory, agent_factory,
                                   project_factory, task_factory):
        ctx = _make_runtime_world(client, db_session, user_factory, organization_factory,
                                  agent_factory, project_factory, task_factory)
        outsider = user_factory()
        t1 = _mk_task(task_factory, ctx["project"], ctx["org"], "t")
        resp = client.put(
            f"{BASE_URL}/agents/tasks/{t1.id}/shared-context",
            json={"key": "k1", "value": "v1", "agent_id": ctx["agent"].id},
            headers={"Authorization": f"Bearer {create_access_token(identity=str(outsider.id))}"},
        )
        assert resp.status_code == 404


class TestRepairTaskCarriesOriginalBrief:
    def test_repair_task_includes_parent_content(self, client, db_session, user_factory,
                                                 organization_factory, agent_factory,
                                                 project_factory, task_factory):
        from services.failure_recovery import handle_failed_commit

        ctx = _make_runtime_world(client, db_session, user_factory, organization_factory,
                                  agent_factory, project_factory, task_factory)
        parent = _mk_task(task_factory, ctx["project"], ctx["org"], "original job",
                          content="PARENT-BRIEF-MARKER 严格按此执行")

        result = handle_failed_commit(
            parent, ctx["agent"], attempt_id=f"att_{uuid.uuid4().hex[:6]}",
            failure_code="ENGINE_FAILED", failure_reason="exit 1",
        )
        assert result["action"] == "repair_created", result
        from models import Task

        repair = db_session.get(Task, result["repair_task_id"])
        assert "PARENT-BRIEF-MARKER" in (repair.content or "")
        assert "原始任务描述" in (repair.content or "")
        _cleanup_experiences(db_session)
