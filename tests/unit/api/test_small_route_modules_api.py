"""小模块清扫（迭代 34）：task_labels / review / delegation /
agent_performance 四个路由模块单元回归。

覆盖：任务标签（内置标签种子幂等与复活、自有/项目两级标签 CRUD、
重名 409 与停用复活、内置/他人保护、软删除）、任务评审（REVIEW
队列分页、approve→DONE 含完成时间、reject→IN_PROGRESS 含反馈追加、
非 REVIEW 400）、任务委派（agent 委派/回收、assignees JSON 增删、
auto-assign 与 WebSocket 推送打桩不阻断、可委派列表）、Agent 绩效
（审计事件聚合、成功/错误率、平均时长、7 天日活、除零保护）。
"""

import uuid
from datetime import datetime, timedelta

import pytest
from flask_jwt_extended import create_access_token

from models import (
    Agent,
    AgentAuditEvent,
    AgentStatus,
    Organization,
    Project,
    Task,
    TaskLabel,
    TaskStatus,
    User,
    db,
)

_TASK_ID_SEQ = iter(range(80_000_001, 80_100_000))


@pytest.fixture(scope="function", autouse=True)
def _isolated_app(monkeypatch):
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

    # 委派链路的三个外部副作用全部打桩
    from services.agent_runtime_controller import AgentRuntimeController
    import api.agent_runtime_websocket as ws_mod
    import api.user_websocket as uws_mod
    assigned, pushed, room_pushed = [], [], []
    monkeypatch.setattr(AgentRuntimeController, "auto_assign_task",
                        lambda task: assigned.append(task.id))
    monkeypatch.setattr(ws_mod, "push_task_to_agent",
                        lambda aid, payload: pushed.append((aid, payload)))
    monkeypatch.setattr(uws_mod, "push_to_task_room",
                        lambda tid, event, payload:
                        room_pushed.append((tid, event)))
    app.config["_assigned"] = assigned
    app.config["_pushed"] = pushed
    app.config["_room_pushed"] = room_pushed

    yield app
    db.session.remove()
    db.drop_all()
    ctx.pop()


@pytest.fixture
def client(_isolated_app):
    return _isolated_app.test_client()


def _uh(prefix="sm"):
    u = User(username=f"{prefix}_{uuid.uuid4().hex[:8]}",
             email=f"{prefix}_{uuid.uuid4().hex[:6]}@t.io")
    db.session.add(u)
    db.session.flush()
    return u


def _headers_for(user):
    return {"Authorization": f"Bearer {create_access_token(identity=str(user.id))}"}


def _mk_project(owner, org=None):
    row = Project(name=f"p_{uuid.uuid4().hex[:6]}", owner_id=owner.id,
                  organization_id=org.id if org else None)
    db.session.add(row)
    db.session.flush()
    return row


def _mk_task(owner, project, status="TODO", **kw):
    row = Task(id=next(_TASK_ID_SEQ), title=f"t_{uuid.uuid4().hex[:6]}",
               content="c", status=status, priority="MEDIUM",
               project_id=project.id, owner_id=owner.id, **kw)
    db.session.add(row)
    db.session.flush()
    return row


def _mk_agent(workspace_id, owner, status=AgentStatus.ACTIVE, name=None,
              **kw):
    row = Agent(workspace_id=workspace_id, owner_id=owner.id,
                creator_user_id=owner.id,
                name=name or f"ag_{uuid.uuid4().hex[:6]}",
                status=status, **kw)
    db.session.add(row)
    db.session.flush()
    return row


@pytest.fixture
def env(_isolated_app):
    user = _uh("ow")
    org = Organization(name=f"o_{uuid.uuid4().hex[:6]}",
                       slug=f"o_{uuid.uuid4().hex[:6]}", owner_id=user.id)
    db.session.add(org)
    db.session.flush()
    project = _mk_project(user, org=org)
    task = _mk_task(user, project)
    agent = _mk_agent(org.id, user)
    db.session.commit()
    return {
        "app": _isolated_app, "user": user, "org": org,
        "project": project, "task": task, "agent": agent,
        "headers": _headers_for(user),
        "base": "/todo-for-ai/api/v1",
    }


# ─────────────────────────── 任务标签 ───────────────────────────


class TestTaskLabels:
    def _base(self, env):
        return f"{env['base']}/task-labels"

    def test_list_seeds_builtins_and_orders(self, env, client):
        resp = client.get(self._base(env), headers=env["headers"])
        assert resp.status_code == 200
        items = resp.get_json()["data"]["items"]
        builtin_names = {i["name"] for i in items if i["is_builtin"]}
        assert {"task", "bug", "urgent"} <= builtin_names
        assert all(i["is_builtin"] for i in items[:1])  # 内置置顶

        # 二次请求幂等：不重复播种
        resp = client.get(self._base(env), headers=env["headers"])
        items = resp.get_json()["data"]["items"]
        assert sum(1 for i in items if i["name"] == "bug") == 1

    def test_list_project_filter(self, env, client):
        stranger = _uh("st")
        foreign = _mk_project(stranger)
        db.session.commit()
        base = self._base(env)

        assert client.get(f"{base}?project_id=999999",
                          headers=env["headers"]).status_code == 404
        assert client.get(f"{base}?project_id={foreign.id}",
                          headers=env["headers"]).status_code == 403
        resp = client.get(f"{base}?project_id={env['project'].id}",
                          headers=env["headers"])
        assert resp.status_code == 200

    def test_create_validation_and_dup(self, env, client):
        base = self._base(env)
        assert client.post(base, headers=env["headers"],
                           json={}).status_code == 400
        assert client.post(base, headers=env["headers"],
                           json={"name": "   "}).status_code == 400
        assert client.post(base, headers=env["headers"],
                           json={"name": "x", "project_id": 999999}
                           ).status_code == 404

        resp = client.post(base, headers=env["headers"],
                           json={"name": "MyLabel", "color": "#ff0000",
                                 "description": "d"})
        assert resp.status_code == 201
        data = resp.get_json()["data"]
        assert data["name"] == "mylabel"  # 归一小写
        assert data["color"] == "#ff0000"

        # 重名 409（大小写归一后）
        assert client.post(base, headers=env["headers"],
                           json={"name": "mylabel"}).status_code == 409

    def test_create_reactivates_inactive(self, env, client):
        client.post(self._base(env), headers=env["headers"],
                    json={"name": "temp"})
        label = TaskLabel.query.filter_by(owner_id=env["user"].id,
                                          name="temp").first()
        label.is_active = False
        db.session.commit()
        resp = client.post(self._base(env), headers=env["headers"],
                           json={"name": "temp", "color": "#123456"})
        assert resp.status_code == 200
        assert "reactivated" in resp.get_json()["message"]
        db.session.expire_all()
        label = db.session.get(TaskLabel, label.id)
        assert label.is_active is True
        assert label.color == "#123456"

    def test_update_guards_and_fields(self, env, client):
        base = self._base(env)
        client.post(base, headers=env["headers"], json={"name": "upd"})
        label = TaskLabel.query.filter_by(owner_id=env["user"].id,
                                          name="upd").first()
        builtin = TaskLabel.query.filter_by(is_builtin=True,
                                            name="bug").first()
        url = f"{base}/{label.id}"

        assert client.put(f"{base}/999999", headers=env["headers"],
                          json={"name": "x"}).status_code == 404
        assert client.put(f"{base}/{builtin.id}", headers=env["headers"],
                          json={"name": "x"}).status_code == 400

        other_user = _uh("ot")
        foreign_label = TaskLabel(owner_id=other_user.id, name="foreign",
                                  color="#000000", is_active=True)
        db.session.add(foreign_label)
        db.session.commit()
        assert client.put(f"{base}/{foreign_label.id}",
                          headers=env["headers"],
                          json={"name": "x"}).status_code == 403

        # 改成已有名 → 409
        client.post(base, headers=env["headers"], json={"name": "taken2"})
        assert client.put(url, headers=env["headers"],
                          json={"name": "taken2"}).status_code == 409

        resp = client.put(url, headers=env["headers"],
                          json={"name": "upd2", "color": "#654321",
                                "description": "dd", "is_active": False})
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["name"] == "upd2" and data["is_active"] is False

    def test_delete_guards_and_soft_delete(self, env, client):
        base = self._base(env)
        client.post(base, headers=env["headers"], json={"name": "del-me"})
        label = TaskLabel.query.filter_by(owner_id=env["user"].id,
                                          name="del-me").first()
        builtin = TaskLabel.query.filter_by(is_builtin=True,
                                            name="task").first()

        assert client.delete(f"{base}/999999",
                             headers=env["headers"]).status_code == 404
        assert client.delete(f"{base}/{builtin.id}",
                             headers=env["headers"]).status_code == 400

        other_user = _uh("ot")
        foreign_label = TaskLabel(owner_id=other_user.id, name="foreign2",
                                  color="#000000", is_active=True)
        db.session.add(foreign_label)
        db.session.commit()
        assert client.delete(f"{base}/{foreign_label.id}",
                             headers=env["headers"]).status_code == 403

        resp = client.delete(f"{base}/{label.id}", headers=env["headers"])
        assert resp.status_code == 200
        db.session.expire_all()
        assert db.session.get(TaskLabel, label.id).is_active is False


    def test_list_and_create_500(self, env, client, monkeypatch):
        from api import task_labels as tl

        def boom(*a, **kw):
            raise RuntimeError("db down")

        monkeypatch.setattr(tl, "ensure_builtin_labels", boom)
        assert client.get(self._base(env),
                          headers=env["headers"]).status_code == 500
        assert client.post(self._base(env), headers=env["headers"],
                           json={"name": "x"}).status_code == 500

    def test_update_and_delete_500(self, env, client, monkeypatch):
        from api import task_labels as tl
        client.post(self._base(env), headers=env["headers"],
                    json={"name": "d500"})
        label = TaskLabel.query.filter_by(owner_id=env["user"].id,
                                          name="d500").first()

        class _BoomQuery:
            def get(self, *a, **kw):
                raise RuntimeError("db down")

        monkeypatch.setattr(tl.TaskLabel, "query", _BoomQuery())
        assert client.put(f"{self._base(env)}/{label.id}",
                          headers=env["headers"],
                          json={"name": "x"}).status_code == 500
        assert client.delete(f"{self._base(env)}/{label.id}",
                             headers=env["headers"]).status_code == 500

    def test_update_non_json_400(self, env, client):
        client.post(self._base(env), headers=env["headers"],
                    json={"name": "nj"})
        label = TaskLabel.query.filter_by(owner_id=env["user"].id,
                                          name="nj").first()
        assert client.put(f"{self._base(env)}/{label.id}",
                          headers=env["headers"], data="junk",
                          content_type="text/plain").status_code == 400

    def test_create_project_forbidden(self, env, client):
        stranger = _uh("st")
        foreign = _mk_project(stranger)
        db.session.commit()
        resp = client.post(self._base(env), headers=env["headers"],
                           json={"name": "x",
                                 "project_id": foreign.id})
        assert resp.status_code == 403



# ─────────────────────────── 任务评审 ───────────────────────────


class TestReviewRoutes:
    def test_pending_reviews_filter_and_pagination(self, env, client):
        review1 = _mk_task(env["user"], env["project"], status="REVIEW")
        review2 = _mk_task(env["user"], env["project"], status="REVIEW")
        _mk_task(env["user"], env["project"], status="DONE")  # 不入列
        db.session.commit()
        url = (f"{env['base']}/tasks/workspaces/{env['org'].id}"
               f"/reviews/pending")
        resp = client.get(url, headers=env["headers"])
        items = resp.get_json()["data"]["items"]
        assert len(items) == 2
        assert all(t["status"] == "review" for t in items)

        resp = client.get(f"{url}?page=1&per_page=1",
                          headers=env["headers"])
        assert len(resp.get_json()["data"]["items"]) == 1

    def test_review_validation(self, env, client):
        url = f"{env['base']}/tasks/{env['task'].id}/review"
        assert client.post(url, headers=env["headers"],
                           json={}).status_code == 400
        assert client.post(url, headers=env["headers"],
                           json={"decision": "maybe"}
                           ).status_code == 400
        assert client.post(f"{env['base']}/tasks/999999/review",
                           headers=env["headers"],
                           json={"decision": "approve"}
                           ).status_code == 404
        # 非 REVIEW 状态
        assert client.post(url, headers=env["headers"],
                           json={"decision": "approve"}
                           ).status_code == 400

    def test_approve_flow(self, env, client):
        task = _mk_task(env["user"], env["project"], status="REVIEW")
        db.session.commit()
        resp = client.post(f"{env['base']}/tasks/{task.id}/review",
                           headers=env["headers"],
                           json={"decision": "approve",
                                 "comment": "LGTM"})
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["status"] == "done"
        assert data["completion_rate"] == 100
        assert data["completed_at"] is not None

    def test_reject_flow_appends_feedback(self, env, client):
        task = _mk_task(env["user"], env["project"], status="REVIEW",
                        feedback_content="old feedback")
        db.session.commit()
        resp = client.post(f"{env['base']}/tasks/{task.id}/review",
                           headers=env["headers"],
                           json={"decision": "reject",
                                 "comment": "missing tests"})
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["status"] == "in_progress"
        db.session.expire_all()
        refreshed = db.session.get(Task, task.id)
        assert "[Rejected] missing tests" in refreshed.feedback_content
        assert "old feedback" in refreshed.feedback_content


# ─────────────────────────── 任务委派 ───────────────────────────


class TestDelegationRoutes:
    def test_delegate_validation(self, env, client):
        url = f"{env['base']}/tasks/{env['task'].id}/delegate"
        assert client.post(url, headers=env["headers"],
                           json={}).status_code == 400
        assert client.post(f"{env['base']}/tasks/999999/delegate",
                           headers=env["headers"],
                           json={"agent_id": env["agent"].id}
                           ).status_code == 404

        inactive = _mk_agent(env["org"].id, env["user"],
                             status=AgentStatus.PAUSED)
        db.session.commit()
        assert client.post(url, headers=env["headers"],
                           json={"agent_id": inactive.id}
                           ).status_code == 404

    def test_delegate_success_and_side_effects(self, env, client):
        env["agent"].display_name = "Worker A"
        env["agent"].capability_tags = ["python"]
        db.session.commit()
        resp = client.post(f"{env['base']}/tasks/{env['task'].id}/delegate",
                           headers=env["headers"],
                           json={"agent_id": env["agent"].id})
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["status"] == "in_progress"
        assert data["assignees"][0]["name"] == "Worker A"

        cfg = env["app"].config
        assert cfg["_assigned"] == [env["task"].id]  # auto-assign 触发
        assert cfg["_pushed"][0][0] == env["agent"].id  # WebSocket 推送
        assert cfg["_room_pushed"] == [(env["task"].id, "task_updated")]

        # 重复委派同一 agent：不产生重复 assignee
        client.post(f"{env['base']}/tasks/{env['task'].id}/delegate",
                    headers=env["headers"],
                    json={"agent_id": env["agent"].id})
        db.session.expire_all()
        refreshed = db.session.get(Task, env["task"].id)
        assert len(refreshed.assignees) == 1
        assert len(env["app"].config["_assigned"]) == 2  # 派单每次都触发


    def test_delegate_side_effect_failures_swallowed(self, env, client,
                                                     monkeypatch):
        """auto-assign / WebSocket / 房间推送任一失败不阻断委派。"""
        from services.agent_runtime_controller import AgentRuntimeController
        import api.agent_runtime_websocket as ws_mod
        import api.user_websocket as uws_mod

        def boom(*a, **kw):
            raise RuntimeError("side effect down")

        monkeypatch.setattr(AgentRuntimeController, "auto_assign_task", boom)
        monkeypatch.setattr(ws_mod, "push_task_to_agent", boom)
        monkeypatch.setattr(uws_mod, "push_to_task_room", boom)
        resp = client.post(f"{env['base']}/tasks/{env['task'].id}/delegate",
                           headers=env["headers"],
                           json={"agent_id": env["agent"].id})
        assert resp.status_code == 200
        assert resp.get_json()["data"]["status"] == "in_progress"

    def test_reclaim(self, env, client):
        task = _mk_task(
            env["user"], env["project"],
            status="IN_PROGRESS",
            assignees=[{"type": "agent", "id": env["agent"].id,
                        "name": "Worker A"},
                       {"type": "user", "id": env["user"].id,
                        "name": "human"}])
        db.session.commit()
        resp = client.post(f"{env['base']}/tasks/{task.id}/reclaim",
                           headers=env["headers"])
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["status"] == "todo"
        assert data["assignees"] == [{"type": "user",
                                      "id": env["user"].id,
                                      "name": "human"}]
        assert client.post(f"{env['base']}/tasks/999999/reclaim",
                           headers=env["headers"]).status_code == 404

    def test_delegatable_agents(self, env, client):
        _mk_agent(env["org"].id, env["user"],
                  status=AgentStatus.DISABLED, name="zzz-disabled")
        _mk_agent(env["org"].id, env["user"], name="aaa-active",
                  capability_tags=["go"])
        db.session.commit()
        url = (f"{env['base']}/tasks/workspaces/{env['org'].id}"
               f"/delegatable-agents")
        resp = client.get(url, headers=env["headers"])
        data = resp.get_json()["data"]
        names = [a["name"] for a in data]
        assert "zzz-disabled" not in names
        assert names[0].startswith("aaa-active")  # 按名称排序
        assert all(a["status"] == "active" for a in data)


# ─────────────────────────── Agent 绩效 ───────────────────────────


class TestAgentPerformance:
    def _url(self, env, agent_id=None, days=None):
        url = (f"{env['base']}/workspaces/{env['org'].id}"
               f"/agents/{agent_id or env['agent'].id}/performance")
        return f"{url}?days={days}" if days else url

    def test_agent_missing_or_wrong_workspace_404(self, env, client):
        assert client.get(self._url(env, agent_id=999999),
                          headers=env["headers"]).status_code == 404
        other_org_agent = _mk_agent(_mk_org_for(env).id, _uh("oo"))
        db.session.commit()
        assert client.get(self._url(env, agent_id=other_org_agent.id),
                          headers=env["headers"]).status_code == 404

    def test_metrics_aggregation(self, env, client):
        now = datetime.utcnow()
        for event_type, duration in (("task_complete", 1000),
                                     ("task_complete", 3000),
                                     ("task_error", None),
                                     ("task_failure", 500)):
            db.session.add(AgentAuditEvent(
                workspace_id=env["org"].id,
                event_type=event_type,
                actor_type="agent", actor_id=str(env["agent"].id),
                target_type="task", target_id="1",
                actor_agent_id=env["agent"].id,
                duration_ms=duration,
                occurred_at=now))
        db.session.commit()
        resp = client.get(self._url(env, days=7), headers=env["headers"])
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["tasks_total"] == 4
        assert data["tasks_completed"] == 2
        assert data["success_rate"] == 50.0
        assert data["error_count"] == 2
        assert data["error_rate"] == 50.0
        assert data["avg_duration_ms"] == 1500  # (1000+3000+500)/3
        assert len(data["daily_activity"]) == 7
        assert data["daily_activity"][-1]["events"] == 4  # 全在今天
        assert data["period_days"] == 7

    def test_zero_events_zero_division(self, env, client):
        resp = client.get(self._url(env), headers=env["headers"])
        data = resp.get_json()["data"]
        assert data["tasks_total"] == 0
        assert data["success_rate"] == 0.0
        assert data["error_rate"] == 0.0
        assert data["avg_duration_ms"] == 0


def _mk_org_for(env):
    other = _uh("oo")
    org = Organization(name=f"o_{uuid.uuid4().hex[:6]}",
                       slug=f"o_{uuid.uuid4().hex[:6]}",
                       owner_id=other.id)
    db.session.add(org)
    db.session.flush()
    return org
