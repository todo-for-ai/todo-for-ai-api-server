"""P1.4 外部 Agent 闭环 MCP 工具测试：list_my_tasks / search_tasks /
update_task_status / report_progress / request_approval。

覆盖外部 Agent（Claude Code / Cursor 等）以 API token 走完
"发现任务 → 开工 → 汇报进度 → 请求人工决策 → 完成" 的 MCP 接入闭环。
"""

import uuid

import pytest

BASE_URL = "/todo-for-ai/api/v1"


def call_tool(client, token, name, arguments=None):
    return client.post(
        f"{BASE_URL}/mcp/call",
        json={"name": name, "arguments": arguments or {}},
        headers={"Authorization": f"Bearer {token}"},
    )


@pytest.fixture
def mcp_env(app, db_session, user_factory):
    """用户 + API token + 自有项目（挂 workspace）的 MCP 调用环境。

    用户/组织直接创建、不走 factory：端点产生的 UserActivity 等行
    会在 factory 清理删除用户时触发级联断言（见 test_task_dod_evidence.py）。
    """
    from models import ApiToken, Organization, Project, User
    from werkzeug.security import generate_password_hash

    unique_id = uuid.uuid4().hex[:8]
    user = User(
        username=f"mcpuser_{unique_id}",
        email=f"mcp_{unique_id}@example.com",
    )
    user.password_hash = generate_password_hash("password123")
    db_session.add(user)
    db_session.commit()

    org = Organization(
        name=f"mcp-org-{unique_id}",
        slug=f"mcp-org-{unique_id}",
        owner_id=user.id,
    )
    db_session.add(org)
    db_session.commit()

    api_token, raw_token = ApiToken.generate_token(name=f"mcp-test-{unique_id}")
    api_token.user_id = user.id
    db_session.add(api_token)
    db_session.commit()

    project = Project(
        name=f"proj-{unique_id}",
        description="mcp loop test",
        status="ACTIVE",
        owner_id=user.id,
        organization_id=org.id,
    )
    db_session.add(project)
    db_session.commit()

    return {"user": user, "token": raw_token, "project": project, "org": org}


_TASK_ID_SEQ = iter(range(9_000_001, 9_100_000))


def _make_task(db_session, project, creator, title="task", content="content", **kwargs):
    """测试任务用独立高位 id 段：conftest 的 task_factory 每个测试从 id=1 起
    手工分配并在清理时删除，本文件的行会驻留会话级库，若走数据库自增会
    撞掉后续 task_factory 的手工 id（UNIQUE tasks.id）。

    is_ai_task 默认 False：runtime pull 按 owner_id == agent.workspace_id
    跨 ID 空间匹配任务，驻留的 is_ai_task 待办任务会被后续 runtime 用例
    优先拉走（用户 ID 与组织 ID 数值可能相同）。
    """
    from models import Task, TaskStatus

    task = Task(
        id=next(_TASK_ID_SEQ),
        title=title,
        content=content,
        project_id=project.id,
        creator_id=creator.id,
        owner_id=creator.id,
        status=kwargs.pop("status", TaskStatus.TODO),
        priority=kwargs.pop("priority", "MEDIUM"),
        is_ai_task=kwargs.pop("is_ai_task", False),
        **kwargs,
    )
    db_session.add(task)
    db_session.commit()
    return task


class TestListMyTasks:
    def test_lists_own_created_task(self, client, db_session, mcp_env):
        task = _make_task(db_session, mcp_env["project"], mcp_env["user"], title="build feature")
        resp = call_tool(client, mcp_env["token"], "list_my_tasks")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "tasks" in data
        ids = [t["id"] for t in data["tasks"]]
        assert task.id in ids

    def test_lists_task_assigned_to_user(self, client, db_session, mcp_env, user_factory):
        other = user_factory()
        task = _make_task(
            db_session, mcp_env["project"], other,
            title="assigned work",
            assignees=[{"type": "human", "id": mcp_env["user"].id}],
        )
        resp = call_tool(client, mcp_env["token"], "list_my_tasks")
        data = resp.get_json()
        assert task.id in [t["id"] for t in data["tasks"]]

    def test_excludes_agent_assignee_same_id(self, client, db_session, mcp_env,
                                             user_factory, project_factory):
        """他人项目里仅指派给同号 agent 的任务，不应命中当前用户。"""
        other = user_factory()
        other_project = project_factory(owner_id=other.id)
        _make_task(
            db_session, other_project, other,
            title="agent-only task",
            assignees=[{"type": "agent", "id": mcp_env["user"].id}],
        )
        data = call_tool(client, mcp_env["token"], "list_my_tasks").get_json()
        assert all(t["title"] != "agent-only task" for t in data["tasks"])

    def test_status_filter_and_done_excluded(self, client, db_session, mcp_env):
        from models import TaskStatus

        _make_task(db_session, mcp_env["project"], mcp_env["user"], title="open item")
        _make_task(db_session, mcp_env["project"], mcp_env["user"], title="closed item",
                   status=TaskStatus.DONE)
        data = call_tool(client, mcp_env["token"], "list_my_tasks").get_json()
        titles = [t["title"] for t in data["tasks"]]
        assert "open item" in titles
        assert "closed item" not in titles

    def test_invalid_status_rejected(self, client, mcp_env):
        resp = call_tool(client, mcp_env["token"], "list_my_tasks",
                         {"status_filter": ["bogus"]})
        assert "error" in resp.get_json()


class TestSearchTasks:
    def test_finds_by_title_keyword(self, client, db_session, mcp_env):
        _make_task(db_session, mcp_env["project"], mcp_env["user"],
                   title="implement oauth login flow")
        _make_task(db_session, mcp_env["project"], mcp_env["user"], title="unrelated")
        data = call_tool(client, mcp_env["token"], "search_tasks",
                         {"keyword": "oauth"}).get_json()
        assert data["total_tasks"] == 1
        assert data["tasks"][0]["title"] == "implement oauth login flow"

    def test_scoped_to_accessible_tasks(self, client, db_session, mcp_env,
                                        user_factory, project_factory):
        other_user = user_factory()
        other_project = project_factory(owner_id=other_user.id)
        _make_task(db_session, other_project, other_user,
                   title="secret oauth plan")
        data = call_tool(client, mcp_env["token"], "search_tasks",
                         {"keyword": "oauth"}).get_json()
        assert all(t["title"] != "secret oauth plan" for t in data["tasks"])

    def test_keyword_required(self, client, mcp_env):
        resp = call_tool(client, mcp_env["token"], "search_tasks", {})
        assert "error" in resp.get_json()


class TestUpdateTaskStatus:
    def test_transitions_status(self, client, db_session, mcp_env):
        from models import Task

        task = _make_task(db_session, mcp_env["project"], mcp_env["user"])
        data = call_tool(client, mcp_env["token"], "update_task_status",
                         {"task_id": task.id, "status": "in_progress"}).get_json()
        assert data["updated"] is True
        assert data["old_status"] == "todo"
        assert data["status"] == "in_progress"
        assert data["revision"] == 2
        db_session.expire_all()
        db_task = db_session.get(Task, task.id)
        assert db_task.status.value == "in_progress"

    def test_revision_conflict_rejected(self, client, db_session, mcp_env):
        task = _make_task(db_session, mcp_env["project"], mcp_env["user"])
        data = call_tool(client, mcp_env["token"], "update_task_status",
                         {"task_id": task.id, "status": "review",
                          "expected_revision": 99}).get_json()
        assert data.get("conflict") is True
        assert "current_revision" in data

    def test_expected_revision_match_succeeds(self, client, db_session, mcp_env):
        task = _make_task(db_session, mcp_env["project"], mcp_env["user"])
        data = call_tool(client, mcp_env["token"], "update_task_status",
                         {"task_id": task.id, "status": "review",
                          "expected_revision": 1}).get_json()
        assert data["updated"] is True

    def test_done_with_unmet_dod_warns_not_blocks(self, client, db_session, mcp_env):
        task = _make_task(db_session, mcp_env["project"], mcp_env["user"],
                          dod=[{"type": "test", "value": "pytest -q"}])
        data = call_tool(client, mcp_env["token"], "update_task_status",
                         {"task_id": task.id, "status": "done"}).get_json()
        assert data["updated"] is True
        assert "dod_warning" in data

    def test_invalid_status_rejected(self, client, db_session, mcp_env):
        task = _make_task(db_session, mcp_env["project"], mcp_env["user"])
        resp = call_tool(client, mcp_env["token"], "update_task_status",
                         {"task_id": task.id, "status": "shipping"})
        assert "error" in resp.get_json()


class TestReportProgress:
    def test_appends_task_log(self, client, db_session, mcp_env):
        from models import TaskLog, TaskLogActorType

        task = _make_task(db_session, mcp_env["project"], mcp_env["user"])
        data = call_tool(client, mcp_env["token"], "report_progress",
                         {"task_id": task.id, "content": "50% done, tests passing"}).get_json()
        assert data["reported"] is True
        row = db_session.get(TaskLog, data["log_id"])
        assert row is not None
        assert row.task_id == task.id
        assert row.actor_type == TaskLogActorType.AGENT
        assert row.actor_user_id == mcp_env["user"].id

    def test_requires_content(self, client, db_session, mcp_env):
        task = _make_task(db_session, mcp_env["project"], mcp_env["user"])
        resp = call_tool(client, mcp_env["token"], "report_progress", {"task_id": task.id})
        assert "error" in resp.get_json()


class TestRequestApproval:
    def test_creates_pending_approval_visible_in_queue(self, client, app, db_session, mcp_env):
        from flask_jwt_extended import create_access_token

        from models import AgentTaskEvent

        task = _make_task(db_session, mcp_env["project"], mcp_env["user"])
        data = call_tool(client, mcp_env["token"], "request_approval", {
            "task_id": task.id,
            "question": "May I force-push to the release branch?",
            "sensitivity_level": "high",
        }).get_json()
        assert data["status"] == "pending_approval"
        assert data["interaction_id"]

        row = AgentTaskEvent.query.filter_by(
            workspace_id=mcp_env["project"].organization_id,
            event_type="interaction_request",
        ).order_by(AgentTaskEvent.id.desc()).first()
        assert row is not None
        assert row.agent_id is None
        assert row.payload["source"] == "mcp"
        assert row.payload["governance"]["requires_approval"] is True

        # 审批队列可见，且来源标识回退到用户名而非 "Agent #None"
        headers = {"Authorization": f"Bearer {create_access_token(identity=str(mcp_env['user'].id))}"}
        resp = client.get(
            f"{BASE_URL}/workspaces/{mcp_env['project'].organization_id}/approvals/pending",
            headers=headers,
        )
        items = resp.get_json()["data"]["items"]
        matched = [i for i in items if i["interaction_id"] == data["interaction_id"]]
        assert matched, "MCP 发起的审批请求应出现在 pending 队列"
        assert matched[0]["agent_name"] == mcp_env["user"].username

    def test_human_can_decide_mcp_request(self, client, app, db_session, mcp_env):
        from flask_jwt_extended import create_access_token

        task = _make_task(db_session, mcp_env["project"], mcp_env["user"])
        data = call_tool(client, mcp_env["token"], "request_approval", {
            "task_id": task.id,
            "question": "Approve budget increase",
        }).get_json()

        headers = {"Authorization": f"Bearer {create_access_token(identity=str(mcp_env['user'].id))}"}
        ws = mcp_env["project"].organization_id
        resp = client.post(
            f"{BASE_URL}/workspaces/{ws}/tasks/{task.id}/interactions/{data['interaction_id']}/approval",
            json={"decision": "approved", "reason": "ok"},
            headers=headers,
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["data"]["decision"] == "approved"

        # 决议后不再出现在 pending 队列
        pend = client.get(f"{BASE_URL}/workspaces/{ws}/approvals/pending", headers=headers)
        items = pend.get_json()["data"]["items"]
        assert all(i["interaction_id"] != data["interaction_id"] for i in items)

    def test_requires_accessible_task(self, client, db_session, mcp_env,
                                      user_factory, project_factory):
        other = user_factory()
        other_project = project_factory(owner_id=other.id)
        foreign_task = _make_task(db_session, other_project, other)
        resp = call_tool(client, mcp_env["token"], "request_approval",
                         {"task_id": foreign_task.id, "question": "hello"})
        assert "error" in resp.get_json()

    def test_invalid_sensitivity_rejected(self, client, db_session, mcp_env):
        task = _make_task(db_session, mcp_env["project"], mcp_env["user"])
        resp = call_tool(client, mcp_env["token"], "request_approval",
                         {"task_id": task.id, "question": "q",
                          "sensitivity_level": "extreme"})
        assert "error" in resp.get_json()
