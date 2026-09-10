"""Agent messaging / workflow-triggers / templates（api/agents/messaging.py）
回归测试：broadcast、点对点消息、collaborators、workflow-triggers CRUD、
workflow-templates、collaboration-templates 的全分支到行。

约定：
- 自建 User/Agent（不走带删除清理的 factory——级联 NOT NULL 坑）；
- broadcast 路由自带 agents/ 前缀（blueprint 前缀 /agents + "/agents/<id>"）；
- 会话级库清场：每例先删 Notification/TaskEvent/WorkflowTrigger 与相关 AuditLog。
"""

import uuid

import pytest

BASE_URL = "/todo-for-ai/api/v1"


@pytest.fixture(autouse=True)
def _clean_messaging_tables(db_session):
    from models import AuditLog, Notification, TaskEvent, WorkflowTrigger
    db_session.query(Notification).delete(synchronize_session=False)
    db_session.query(TaskEvent).delete(synchronize_session=False)
    db_session.query(WorkflowTrigger).delete(synchronize_session=False)
    db_session.query(AuditLog).filter(
        AuditLog.action.in_(["agent.broadcast", "agent.direct_message",
                             "workflow_trigger.created", "workflow_trigger.updated",
                             "workflow.instantiated_from_template",
                             "collaboration_template_instantiate"]),
    ).delete(synchronize_session=False)
    db_session.rollback()  # 清掉上一例可能遗留的 pending 脏状态
    db_session.commit()
    yield
    db_session.rollback()
    db_session.commit()


def _headers(user):
    from flask_jwt_extended import create_access_token
    return {"Authorization": f"Bearer {create_access_token(identity=str(user.id))}"}


def _now(**kw):
    import datetime as dt
    return dt.datetime.utcnow() - dt.timedelta(**kw)


def _make_user(db_session):
    from models import User
    user = User(username=f"mu_{uuid.uuid4().hex[:8]}",
                email=f"mu_{uuid.uuid4().hex[:6]}@t.io")
    db_session.add(user)
    db_session.commit()
    return user


def _make_agent(db_session, user, status="ACTIVE", name=None):
    from models import Agent, AgentKind, AgentStatus
    agent = Agent(name=name or f"mg_{uuid.uuid4().hex[:8]}", owner_id=user.id)
    agent.kind = AgentKind.ASSISTANT
    agent.status = AgentStatus[status.upper()]  # Enum 按 name 落库
    db_session.add(agent)
    db_session.commit()
    return agent


def _make_project(db_session, user):
    from models import Project
    project = Project(name=f"mp_{uuid.uuid4().hex[:8]}", owner_id=user.id,
                      status="ACTIVE")
    db_session.add(project)
    db_session.commit()
    return project


def _make_task(db_session, user, project):
    from models import Task
    task = Task(title=f"mt_{uuid.uuid4().hex[:8]}", content="x",
                project_id=project.id, owner_id=user.id, status="TODO")
    db_session.add(task)
    db_session.commit()
    return task


def _make_workflow(db_session, user, with_step=False):
    from models import Workflow, WorkflowStep
    wf = Workflow.create(owner_id=user.id, name=f"wf_{uuid.uuid4().hex[:6]}",
                         description="", definition={"steps": []},
                         is_active=True, version=1)
    db_session.add(wf)
    db_session.flush()
    if with_step:
        WorkflowStep.create(workflow_id=wf.id, step_key="s1", name="s1", order=1)
    db_session.commit()
    return wf


# ────────────────────────── broadcast ──────────────────────────

def test_broadcast_requires_content(client, db_session):
    user = _make_user(db_session)
    agent = _make_agent(db_session, user)
    resp = client.post(f"{BASE_URL}/agents/agents/{agent.id}/broadcast",
                       json={"content": "  "}, headers=_headers(user))
    assert resp.status_code == 400
    assert "content is required" in resp.get_json()["message"]


def test_broadcast_notifies_active_others(client, db_session):
    user = _make_user(db_session)
    sender = _make_agent(db_session, user)
    peer = _make_agent(db_session, user)
    _make_agent(db_session, user, status="INACTIVE")   # 停用不收
    _make_agent(db_session, _make_user(db_session))    # 别人的不收

    resp = client.post(f"{BASE_URL}/agents/agents/{sender.id}/broadcast",
                       json={"content": "hello team", "event_type": "notice"},
                       headers=_headers(user))
    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()["data"]
    assert data["recipient_agent_ids"] == [peer.id]

    from models import Notification
    notes = Notification.query.filter_by(user_id=user.id).all()
    assert len(notes) == 1 and notes[0].agent_id == peer.id


def test_broadcast_with_task_records_event(client, db_session):
    user = _make_user(db_session)
    sender = _make_agent(db_session, user)
    _make_agent(db_session, user)
    project = _make_project(db_session, user)
    task = _make_task(db_session, user, project)

    resp = client.post(f"{BASE_URL}/agents/agents/{sender.id}/broadcast",
                       json={"content": "go", "task_id": task.id,
                             "event_type": "dispatch",
                             "payload": {"priority": "high"}},
                       headers=_headers(user))
    assert resp.status_code == 200, resp.get_json()

    from models import TaskEvent
    event = TaskEvent.query.filter_by(task_id=task.id).first()
    assert event is not None
    assert event.event_type == "dispatch"
    assert event.payload["broadcast"] is True
    assert event.payload["priority"] == "high"
    assert event.payload["recipient_count"] == 1


def test_broadcast_500_on_notification_failure(client, db_session, monkeypatch):
    user = _make_user(db_session)
    sender = _make_agent(db_session, user)
    _make_agent(db_session, user)  # 一个接收者

    from models import Notification
    monkeypatch.setattr(Notification, "create_notification",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("db down")))

    resp = client.post(f"{BASE_URL}/agents/agents/{sender.id}/broadcast",
                       json={"content": "go"}, headers=_headers(user))
    assert resp.status_code == 500
    assert "Broadcast failed" in resp.get_json()["message"]


# ────────────────────────── workflow-triggers ──────────────────────────

def _seed_workflow(db_session, user):
    from models import Workflow
    wf = Workflow.create(owner_id=user.id, name=f"wf_{uuid.uuid4().hex[:6]}",
                         description="", definition={"steps": []},
                         is_active=True, version=1)
    db_session.add(wf)
    db_session.commit()
    return wf


def test_workflow_trigger_crud_with_cron(client, db_session):
    user = _make_user(db_session)
    wf = _seed_workflow(db_session, user)
    headers = _headers(user)

    resp = client.get(f"{BASE_URL}/agents/workflow-triggers", headers=headers)
    assert resp.get_json()["data"]["items"] == []

    resp = client.post(f"{BASE_URL}/agents/workflow-triggers", headers=headers,
                       json={"workflow_id": wf.id, "name": "daily",
                             "cron_expr": "0 9 * * *"})
    assert resp.status_code == 201, resp.get_json()
    trigger = resp.get_json()["data"]
    assert trigger["next_fire_at"] is not None  # _compute_next_fire NameError 已修复
    trigger_id = trigger["id"]

    resp = client.post(f"{BASE_URL}/agents/workflow-triggers", headers=headers,
                       json={"workflow_id": wf.id, "name": "bad"})
    assert resp.status_code == 400

    resp = client.post(f"{BASE_URL}/agents/workflow-triggers", headers=headers,
                       json={"workflow_id": 424242, "name": "x",
                             "cron_expr": "0 9 * * *"})
    assert resp.status_code == 404

    resp = client.get(f"{BASE_URL}/agents/workflow-triggers?workflow_id={wf.id}"
                      "&is_active=true", headers=headers)
    assert len(resp.get_json()["data"]["items"]) == 1
    resp = client.get(f"{BASE_URL}/agents/workflow-triggers/{trigger_id}", headers=headers)
    assert resp.get_json()["data"]["id"] == trigger_id
    resp = client.get(f"{BASE_URL}/agents/workflow-triggers/424242", headers=headers)
    assert resp.status_code == 404

    resp = client.put(f"{BASE_URL}/agents/workflow-triggers/{trigger_id}", headers=headers,
                      json={"name": "renamed", "cron_expr": "30 8 * * *",
                            "is_active": False})
    assert resp.get_json()["data"]["name"] == "renamed"
    assert resp.get_json()["data"]["next_fire_at"] is not None

    resp = client.put(f"{BASE_URL}/agents/workflow-triggers/{trigger_id}", headers=headers,
                      json={"one_shot_at": "not-a-date"})
    assert resp.status_code == 400

    resp = client.put(f"{BASE_URL}/agents/workflow-triggers/{trigger_id}", headers=headers,
                      json={"cron_expr": "", "one_shot_at": "2026-12-01T09:00:00",
                            "is_active": True})
    assert resp.get_json()["data"]["one_shot_at"] is not None

    resp = client.put(f"{BASE_URL}/agents/workflow-triggers/424242", headers=headers,
                      json={"name": "x"})
    assert resp.status_code == 404

    resp = client.delete(f"{BASE_URL}/agents/workflow-triggers/{trigger_id}", headers=headers)
    assert resp.status_code == 200
    resp = client.delete(f"{BASE_URL}/agents/workflow-triggers/{trigger_id}", headers=headers)
    assert resp.status_code == 404


def test_workflow_trigger_one_shot_and_filters(client, db_session):
    user = _make_user(db_session)
    wf = _seed_workflow(db_session, user)
    headers = _headers(user)

    resp = client.post(f"{BASE_URL}/agents/workflow-triggers", headers=headers,
                       json={"workflow_id": wf.id, "name": "once",
                             "one_shot_at": "bogus"})
    assert resp.status_code == 400

    resp = client.post(f"{BASE_URL}/agents/workflow-triggers", headers=headers,
                       json={"workflow_id": wf.id, "name": "once",
                             "one_shot_at": "2026-12-01T09:00:00",
                             "is_active": False})
    assert resp.status_code == 201
    resp = client.get(f"{BASE_URL}/agents/workflow-triggers?is_active=false",
                      headers=headers)
    assert len(resp.get_json()["data"]["items"]) == 1


def test_workflow_trigger_recompute_next_fire_on_reactivate(client, db_session):
    """is_active=true 且无 next_fire：cron 与 one_shot 两条重算路径。"""
    user = _make_user(db_session)
    wf = _seed_workflow(db_session, user)
    headers = _headers(user)

    resp = client.post(f"{BASE_URL}/agents/workflow-triggers", headers=headers,
                       json={"workflow_id": wf.id, "name": "t",
                             "cron_expr": "0 9 * * *", "is_active": False})
    trigger_id = resp.get_json()["data"]["id"]
    resp = client.put(f"{BASE_URL}/agents/workflow-triggers/{trigger_id}", headers=headers,
                      json={"is_active": True})  # cron 路径重算
    assert resp.get_json()["data"]["next_fire_at"] is not None

    resp = client.post(f"{BASE_URL}/agents/workflow-triggers", headers=headers,
                       json={"workflow_id": wf.id, "name": "t2",
                             "one_shot_at": "2026-12-01T09:00:00",
                             "is_active": False})
    trigger_id2 = resp.get_json()["data"]["id"]
    resp = client.put(f"{BASE_URL}/agents/workflow-triggers/{trigger_id2}", headers=headers,
                      json={"is_active": True})  # one_shot 路径
    assert resp.get_json()["data"]["next_fire_at"] is not None


# ────────────────────────── 点对点消息 ──────────────────────────

def test_send_agent_message_happy_and_errors(client, db_session):
    user = _make_user(db_session)
    src = _make_agent(db_session, user)
    dst = _make_agent(db_session, user)
    headers = _headers(user)

    resp = client.post(f"{BASE_URL}/agents/424242/message/{dst.id}",
                       json={"content": "hi"}, headers=headers)
    assert resp.status_code == 404
    resp = client.post(f"{BASE_URL}/agents/{src.id}/message/424242",
                       json={"content": "hi"}, headers=headers)
    assert resp.status_code == 404
    resp = client.post(f"{BASE_URL}/agents/{src.id}/message/{src.id}",
                       json={"content": "hi"}, headers=headers)
    assert resp.status_code == 400
    resp = client.post(f"{BASE_URL}/agents/{src.id}/message/{dst.id}",
                       json={}, headers=headers)
    assert resp.status_code == 400

    project = _make_project(db_session, user)
    task = _make_task(db_session, user, project)
    resp = client.post(f"{BASE_URL}/agents/{src.id}/message/{dst.id}",
                       json={"content": "please review", "task_id": task.id,
                             "message_type": "review_request",
                             "metadata": {"urgency": "high"}},
                       headers=headers)
    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()["data"]
    assert data["delivered"] is True and data["to_agent_id"] == dst.id

    from models import Notification, TaskEvent
    assert TaskEvent.query.filter_by(task_id=task.id).count() == 1
    note = Notification.query.filter_by(event_type="agent.direct_message").first()
    assert note.payload["metadata"] == {"urgency": "high"}


def test_get_agent_messages_filters_sender_receiver(client, db_session):
    user = _make_user(db_session)
    a = _make_agent(db_session, user)
    b = _make_agent(db_session, user)
    c = _make_agent(db_session, user)
    headers = _headers(user)

    client.post(f"{BASE_URL}/agents/{a.id}/message/{b.id}",
                json={"content": "a->b"}, headers=headers)
    client.post(f"{BASE_URL}/agents/{b.id}/message/{a.id}",
                json={"content": "b->a"}, headers=headers)
    client.post(f"{BASE_URL}/agents/{b.id}/message/{c.id}",
                json={"content": "b->c"}, headers=headers)

    resp = client.get(f"{BASE_URL}/agents/{a.id}/messages", headers=headers)
    data = resp.get_json()["data"]
    assert data["total"] == 2  # a 发 1 收 1，b->c 不算
    assert data["page"] == 1

    resp = client.get(f"{BASE_URL}/agents/424242/messages", headers=headers)
    assert resp.status_code == 404


def test_get_agent_messages_pagination(client, db_session):
    user = _make_user(db_session)
    a = _make_agent(db_session, user)
    b = _make_agent(db_session, user)
    headers = _headers(user)
    for i in range(3):
        client.post(f"{BASE_URL}/agents/{a.id}/message/{b.id}",
                    json={"content": f"m{i}"}, headers=headers)
    resp = client.get(f"{BASE_URL}/agents/{a.id}/messages?page=1&per_page=2",
                      headers=headers)
    data = resp.get_json()["data"]
    assert len(data["items"]) == 2 and data["total"] == 3
    assert data["pages"] == 2


# ────────────────────────── collaborators ──────────────────────────

def test_collaborators_counts_and_excludes_self(client, db_session):
    from models import AuditLog
    user = _make_user(db_session)
    a = _make_agent(db_session, user)
    b = _make_agent(db_session, user)
    for _ in range(2):  # a -> b 两次
        db_session.add(AuditLog(action="agent.direct_message",
                                resource_type="agent", resource_id=b.id,
                                actor_type="agent", actor_agent_id=a.id,
                                actor_user_id=user.id))
    db_session.add(AuditLog(action="agent.direct_message",
                            resource_type="agent", resource_id=a.id,
                            actor_type="agent", actor_agent_id=b.id,
                            actor_user_id=user.id))
    # 自指（应排除；resource_id NOT NULL，"无伙伴"分支经此处同 line 覆盖）
    db_session.add(AuditLog(action="agent.direct_message",
                            resource_type="agent", resource_id=a.id,
                            actor_type="agent", actor_agent_id=a.id,
                            actor_user_id=user.id))
    db_session.commit()

    resp = client.get(f"{BASE_URL}/agents/{a.id}/collaborators",
                      headers=_headers(user))
    data = resp.get_json()["data"]
    assert data["total_partners"] == 1
    collab = data["collaborators"][0]
    assert collab["agent_id"] == b.id
    assert (collab["sent"], collab["received"], collab["total"]) == (2, 1, 3)

    resp = client.get(f"{BASE_URL}/agents/424242/collaborators",
                      headers=_headers(user))
    assert resp.status_code == 404


# ────────────────────────── workflow-templates ──────────────────────────

def test_workflow_templates_list_and_get(client, db_session):
    user = _make_user(db_session)
    headers = _headers(user)
    resp = client.get(f"{BASE_URL}/agents/workflow-templates", headers=headers)
    templates = resp.get_json()["data"]
    assert templates and all("step_count" in t for t in templates)

    key = templates[0]["key"]
    resp = client.get(f"{BASE_URL}/agents/workflow-templates/{key}", headers=headers)
    assert resp.get_json()["data"]["key"] == key

    resp = client.get(f"{BASE_URL}/agents/workflow-templates/nope", headers=headers)
    assert resp.status_code == 404

    category = templates[0]["category"]
    resp = client.get(f"{BASE_URL}/agents/workflow-templates?category={category}",
                      headers=headers)
    assert all(t["category"] == category for t in resp.get_json()["data"])


def test_instantiate_workflow_template(client, db_session):
    user = _make_user(db_session)
    project = _make_project(db_session, user)
    headers = _headers(user)

    resp = client.get(f"{BASE_URL}/agents/workflow-templates", headers=headers)
    key = resp.get_json()["data"][0]["key"]

    resp = client.post(f"{BASE_URL}/agents/workflow-templates/{key}/instantiate",
                       json={"name": "my-wf", "project_id": project.id},
                       headers=headers)
    assert resp.status_code == 201, resp.get_json()
    assert resp.get_json()["data"]["name"] == "my-wf"

    resp = client.post(f"{BASE_URL}/agents/workflow-templates/nope/instantiate",
                       json={"name": "x"}, headers=headers)
    assert resp.status_code == 404


# ────────────────────────── collaboration-templates ──────────────────────────

def test_collaboration_templates_list_create_delete(client, db_session):
    user = _make_user(db_session)
    headers = _headers(user)

    resp = client.get(f"{BASE_URL}/agents/collaboration-templates", headers=headers)
    items = resp.get_json()["data"]
    assert any(i.get("is_builtin") for i in items)

    resp = client.post(f"{BASE_URL}/agents/collaboration-templates", headers=headers,
                       json={"agent_specs": [{"name": "a"}]})
    assert resp.status_code == 400
    resp = client.post(f"{BASE_URL}/agents/collaboration-templates", headers=headers,
                       json={"name": "squad"})
    assert resp.status_code == 400

    resp = client.post(f"{BASE_URL}/agents/collaboration-templates", headers=headers,
                       json={"name": "my-squad", "description": "d",
                             "category": "review",
                             "agent_specs": [{"name": "a", "kind": "assistant"}]})
    assert resp.status_code == 201, resp.get_json()
    template_id = resp.get_json()["data"]["id"]

    resp = client.get(f"{BASE_URL}/agents/collaboration-templates?category=review",
                      headers=headers)
    assert any(i["id"] == template_id for i in resp.get_json()["data"])

    resp = client.delete(f"{BASE_URL}/agents/collaboration-templates/424242", headers=headers)
    assert resp.status_code == 404
    resp = client.delete(f"{BASE_URL}/agents/collaboration-templates/{template_id}",
                         headers=headers)
    assert resp.status_code == 200


def test_instantiate_collaboration_template_builtin_creates_channel(
        client, db_session):
    """builtin 模板：≥2 个 agent → 自动建协作频道。"""
    user = _make_user(db_session)
    project = _make_project(db_session, user)

    resp = client.get(f"{BASE_URL}/agents/collaboration-templates", headers=_headers(user))
    builtin = next(i for i in resp.get_json()["data"]
                   if i.get("is_builtin") and i.get("workflow_steps"))
    key = builtin["id"]  # builtin:<key>

    resp = client.post(
        f"{BASE_URL}/agents/collaboration-templates/{key}/instantiate",
        json={"project_id": project.id},
        headers=_headers(user))
    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()["data"]
    assert len(data["agents"]) >= 2
    assert data["channel"] is not None  # ≥2 agents → 建频道

    resp = client.post(
        f"{BASE_URL}/agents/collaboration-templates/builtin:nope/instantiate",
        json={}, headers=_headers(user))
    assert resp.status_code == 404

    resp = client.post(
        f"{BASE_URL}/agents/collaboration-templates/abcxyz/instantiate",
        json={}, headers=_headers(user))
    assert resp.status_code == 400


def test_instantiate_builtin_with_steps_advances_for_real(client, db_session):
    """回归：builtin 模板（带 steps）真实推进工作流——_start_step 必须
    flush 后取 task.id，assignment/run/step_run 拿到真实 id（而非 None
    导致后续 flush 炸 NOT NULL）。"""
    user = _make_user(db_session)
    project = _make_project(db_session, user)

    resp = client.get(f"{BASE_URL}/agents/collaboration-templates",
                      headers=_headers(user))
    builtin = next(i for i in resp.get_json()["data"]
                   if i.get("is_builtin") and i.get("workflow_steps"))

    resp = client.post(
        f"{BASE_URL}/agents/collaboration-templates/{builtin['id']}/instantiate",
        json={"project_id": project.id},
        headers=_headers(user))
    assert resp.status_code == 200, resp.get_json()

    from models import (TaskAssignment, WorkflowRun, WorkflowStepRun,
                        WorkflowStatus)
    run = WorkflowRun.query.filter_by(project_id=project.id).one()
    assert run.status == WorkflowStatus.RUNNING  # 真实推进成功
    step_runs = WorkflowStepRun.query.filter_by(run_id=run.id).all()
    assert step_runs, "step runs 应已创建"
    started = [sr for sr in step_runs if sr.status == WorkflowStatus.RUNNING
               or sr.task_id]
    assert started, "至少一个步骤应已启动"
    for sr in started:
        assert sr.task_id is not None
        assignment = TaskAssignment.query.filter_by(id=sr.assignment_id).first()
        assert assignment is not None
        assert assignment.task_id == sr.task_id


def test_instantiate_collaboration_template_user_with_workflow(
        client, db_session):
    """用户模板带 workflow_id + project → 创建 wf_run 并推进。"""
    user = _make_user(db_session)
    project = _make_project(db_session, user)
    from models import CollaborationTemplate, Workflow, WorkflowStep
    wf = Workflow.create(owner_id=user.id, name="wf", description="",
                         definition={"steps": []}, is_active=True, version=1)
    db_session.add(wf)
    db_session.flush()
    WorkflowStep.create(workflow_id=wf.id, step_key="s1", name="s1", order=1)
    template = CollaborationTemplate.create(
        owner_id=user.id, name="user-squad", description="",
        category="review",
        agent_specs=[{"name": "x", "kind": "assistant"}],
        workflow_id=wf.id)
    db_session.commit()

    resp = client.post(
        f"{BASE_URL}/agents/collaboration-templates/{template.id}/instantiate",
        json={"project_id": project.id},
        headers=_headers(user))
    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()["data"]
    assert len(data["agents"]) == 1
    assert data["workflow_run"] is not None  # workflow_id + project → 启动运行

    resp = client.post(f"{BASE_URL}/agents/collaboration-templates/424242/instantiate",
                       json={"project_id": project.id}, headers=_headers(user))
    assert resp.status_code == 404


# ────────────────────────── 错误分支与重激活路径补全 ──────────────────────────

def test_broadcast_agent_not_found_and_invalid_body(client, db_session):
    user = _make_user(db_session)
    agent = _make_agent(db_session, user)
    headers = _headers(user)
    # agent 404 → validate 之前就返回（覆盖 139 return response）
    resp = client.post(f"{BASE_URL}/agents/agents/424242/broadcast",
                       json={"content": "x"}, headers=headers)
    assert resp.status_code == 404
    # 空 JSON body → validate 返回 400 错误响应（覆盖 145 return data）
    resp = client.post(f"{BASE_URL}/agents/agents/{agent.id}/broadcast",
                       json={}, headers=headers)
    assert resp.status_code == 400


def test_workflow_trigger_validate_error_paths(client, db_session):
    user = _make_user(db_session)
    wf = _seed_workflow(db_session, user)
    headers = _headers(user)
    # POST 空 body → validate 400 响应（覆盖 return data 行）
    resp = client.post(f"{BASE_URL}/agents/workflow-triggers", headers=headers,
                       json={})
    assert resp.status_code == 400
    # PUT 空 body 同理（先创建 trigger，路由先查行再校验）
    resp = client.post(f"{BASE_URL}/agents/workflow-triggers", headers=headers,
                       json={"workflow_id": wf.id, "name": "t",
                             "cron_expr": "0 9 * * *"})
    assert resp.status_code == 201
    trigger_id = resp.get_json()["data"]["id"]
    resp = client.put(f"{BASE_URL}/agents/workflow-triggers/{trigger_id}",
                      headers=headers, json={})
    assert resp.status_code == 400


def test_workflow_trigger_reactivate_recomputes_next_fire(client, db_session):
    """342-345：cron / one_shot 两条重算路径（直接造 next_fire=None 的行）。"""
    user = _make_user(db_session)
    wf = _seed_workflow(db_session, user)
    from models import WorkflowTrigger
    headers = _headers(user)

    t1 = WorkflowTrigger.create(workflow_id=wf.id, owner_id=user.id,
                                name="cron-off", cron_expr="30 8 * * *",
                                is_active=False, next_fire_at=None)
    t2 = WorkflowTrigger.create(workflow_id=wf.id, owner_id=user.id,
                                name="once-off", one_shot_at=_now(days=5),
                                is_active=False, next_fire_at=None)
    db_session.commit()

    resp = client.put(f"{BASE_URL}/agents/workflow-triggers/{t1.id}",
                      headers=headers, json={"is_active": True})
    assert resp.get_json()["data"]["next_fire_at"] is not None  # cron 重算
    resp = client.put(f"{BASE_URL}/agents/workflow-triggers/{t2.id}",
                      headers=headers, json={"is_active": True})
    assert resp.get_json()["data"]["next_fire_at"] is not None  # one_shot 路径


def test_send_agent_message_500_on_task_event_failure(client, db_session,
                                                      monkeypatch):
    user = _make_user(db_session)
    src = _make_agent(db_session, user)
    dst = _make_agent(db_session, user)
    project = _make_project(db_session, user)
    task = _make_task(db_session, user, project)

    from models import TaskEvent
    def boom(**kw):
        raise RuntimeError("event bus down")
    monkeypatch.setattr(TaskEvent, "create", boom)

    resp = client.post(f"{BASE_URL}/agents/{src.id}/message/{dst.id}",
                       json={"content": "hi", "task_id": task.id},
                       headers=_headers(user))
    assert resp.status_code == 500
    assert "Failed to send message" in resp.get_json()["message"]


def test_get_agent_messages_500_on_query_failure(client, db_session, monkeypatch):
    user = _make_user(db_session)
    agent = _make_agent(db_session, user)

    from models import Notification
    class _BoomQuery:
        def filter_by(self, **kw):
            raise RuntimeError("db down")
    monkeypatch.setattr(Notification, "query", _BoomQuery())

    resp = client.get(f"{BASE_URL}/agents/{agent.id}/messages",
                      headers=_headers(user))
    assert resp.status_code == 500
    assert "Failed to retrieve messages" in resp.get_json()["message"]


def test_collaborators_invalid_limit_falls_back(client, db_session):
    user = _make_user(db_session)
    a = _make_agent(db_session, user)
    resp = client.get(f"{BASE_URL}/agents/{a.id}/collaborators?limit=abc",
                      headers=_headers(user))
    assert resp.status_code == 200
    assert resp.get_json()["data"]["total_partners"] == 0


def test_instantiate_workflow_template_empty_body_400(client, db_session):
    user = _make_user(db_session)
    headers = _headers(user)
    resp = client.post(f"{BASE_URL}/agents/workflow-templates/x/instantiate",
                       json={}, headers=headers)
    assert resp.status_code == 400


def test_collab_instantiate_invalid_kind_falls_back(client, db_session):
    """agent_specs 的 kind 非法 → 回退 AUTONOMOUS（788-789）。"""
    user = _make_user(db_session)
    from models import CollaborationTemplate
    template = CollaborationTemplate.create(
        owner_id=user.id, name="bad-kind", description="",
        category="review",
        agent_specs=[{"name": "x", "kind": "wizard"}],
        workflow_id=None)
    db_session.commit()

    resp = client.post(
        f"{BASE_URL}/agents/collaboration-templates/{template.id}/instantiate",
        json={}, headers=_headers(user))
    assert resp.status_code == 200, resp.get_json()
    from models import AgentKind
    assert resp.get_json()["data"]["agents"][0]["kind"] == AgentKind.AUTONOMOUS.value


def test_instantiate_collaboration_template_advance_success(client, db_session,
                                                            monkeypatch):
    """builtin 模板（带 workflow_steps）+ _advance_workflow 成功路径：
    覆盖 builtin 分支 try 内的 commit 行（904）。"""
    monkeypatch.setattr("api.agents.collaboration_templates._advance_workflow",
                        lambda wf_run: None)

    user = _make_user(db_session)
    project = _make_project(db_session, user)
    from models import Workflow, WorkflowStep, WorkflowStepRun, WorkflowRun
    wf = Workflow.create(owner_id=user.id, name="wf", description="",
                         definition={"steps": []}, is_active=True, version=1)
    db_session.add(wf)
    db_session.flush()
    WorkflowStep.create(workflow_id=wf.id, step_key="s1", name="s1", order=1)

    # 挑一个带 workflow_steps 的 builtin 模板
    resp = client.get(f"{BASE_URL}/agents/collaboration-templates",
                      headers=_headers(user))
    builtin = next(i for i in resp.get_json()["data"]
                   if i.get("is_builtin") and i.get("workflow_steps"))

    resp = client.post(
        f"{BASE_URL}/agents/collaboration-templates/{builtin['id']}/instantiate",
        json={"project_id": project.id},
        headers=_headers(user))
    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()["data"]
    assert len(data["agents"]) >= 2
    assert data["workflow_run"] is not None
    assert WorkflowStepRun.query.count() >= 1
    _ = WorkflowRun


def test_instantiate_user_template_advance_success(client, db_session,
                                                   monkeypatch):
    """用户模板 + workflow：_advance_workflow 成功路径（覆盖 849 内层 commit）。"""
    monkeypatch.setattr("api.agents.collaboration_templates._advance_workflow",
                        lambda wf_run: None)

    user = _make_user(db_session)
    project = _make_project(db_session, user)
    from models import CollaborationTemplate, Workflow, WorkflowStep
    wf = Workflow.create(owner_id=user.id, name="wf", description="",
                         definition={"steps": []}, is_active=True, version=1)
    db_session.add(wf)
    db_session.flush()
    WorkflowStep.create(workflow_id=wf.id, step_key="s1", name="s1", order=1)
    template = CollaborationTemplate.create(
        owner_id=user.id, name="ok-squad", description="",
        category="review",
        agent_specs=[{"name": "x", "kind": "assistant"}],
        workflow_id=wf.id)
    db_session.commit()

    resp = client.post(
        f"{BASE_URL}/agents/collaboration-templates/{template.id}/instantiate",
        json={"project_id": project.id},
        headers=_headers(user))
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["data"]["workflow_run"] is not None
