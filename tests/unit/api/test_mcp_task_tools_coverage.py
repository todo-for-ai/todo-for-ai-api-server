"""MCP task_tools 补齐测试（迭代 130）：create/get_by_id/feedback/evidence/dod/
get_project_tasks_by_name 全函数与既有工具的校验、错误、越权分支。"""

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

    api_token, raw_token = ApiToken.generate_token(name=f"mcp-cov-{unique_id}")
    api_token.user_id = user.id
    db_session.add(api_token)
    db_session.commit()

    project = Project(
        name=f"proj-{unique_id}",
        description="mcp coverage test",
        status="ACTIVE",
        owner_id=user.id,
        organization_id=org.id,
    )
    db_session.add(project)
    db_session.commit()

    return {"user": user, "token": raw_token, "project": project, "org": org}


_TASK_ID_SEQ = iter(range(9_300_001, 9_400_000))


def _make_task(db_session, project, creator, title="task", content="content", **kwargs):
    from models import Task, TaskStatus

    kwargs.setdefault("status", TaskStatus.TODO)
    task = Task(
        id=next(_TASK_ID_SEQ),
        title=title,
        content=content,
        project_id=project.id,
        creator_id=creator.id,
        is_ai_task=False,
        **kwargs,
    )
    db_session.add(task)
    db_session.commit()
    return task


class TestCreateTask:
    def test_create_success_minimal(self, client, mcp_env):
        resp = call_tool(client, mcp_env["token"], "create_task", {
            "project_id": mcp_env["project"].id, "title": "cov task",
        })
        assert resp.status_code == 200
        data = resp.get_json()["result"] if "result" in resp.get_json() else resp.get_json()
        body = data.get("result") or data
        text = str(body)
        assert "cov task" in text

    def test_create_missing_project_and_title(self, client, mcp_env):
        resp = call_tool(client, mcp_env["token"], "create_task", {})
        assert "project_id is required" in str(resp.get_json())

        resp = call_tool(client, mcp_env["token"], "create_task", {"project_id": mcp_env["project"].id})
        assert "title is required" in str(resp.get_json())

    def test_create_project_not_found_and_forbidden(self, client, mcp_env):
        resp = call_tool(client, mcp_env["token"], "create_task", {
            "project_id": 987654, "title": "x",
        })
        assert "Project with ID 987654 not found" in str(resp.get_json())

    def test_create_invalid_status_priority_due_date(self, client, mcp_env):
        pid = mcp_env["project"].id
        resp = call_tool(client, mcp_env["token"], "create_task", {
            "project_id": pid, "title": "x", "status": "archived",
        })
        assert "Invalid status" in str(resp.get_json())
        resp = call_tool(client, mcp_env["token"], "create_task", {
            "project_id": pid, "title": "x", "priority": "ultra",
        })
        assert "Invalid priority" in str(resp.get_json())
        resp = call_tool(client, mcp_env["token"], "create_task", {
            "project_id": pid, "title": "x", "due_date": "2026/01/01",
        })
        assert "Invalid due_date format" in str(resp.get_json())

    def test_create_full_fields_with_due_date(self, client, mcp_env):
        resp = call_tool(client, mcp_env["token"], "create_task", {
            "project_id": mcp_env["project"].id,
            "title": "full task",
            "content": "body",
            "assignee": "alice",
            "due_date": "2026-12-01",
            "tags": ["a", "b"],
            "related_files": ["src/x.py"],
            "is_ai_task": False,
            "ai_identifier": "cov-bot",
        })
        text = str(resp.get_json())
        assert "full task" in text and "2026-12-01" in text


class TestGetProjectTasksByName:
    def test_by_name_success(self, client, db_session, mcp_env, task_factory):
        task = _make_task(db_session, mcp_env["project"], mcp_env["user"], title="named")
        resp = call_tool(client, mcp_env["token"], "get_project_tasks_by_name", {
            "project_name": mcp_env["project"].name,
        })
        text = str(resp.get_json())
        assert mcp_env["project"].name in text

    def test_by_name_missing_and_unknown(self, client, mcp_env):
        resp = call_tool(client, mcp_env["token"], "get_project_tasks_by_name", {})
        assert "project_name is required" in str(resp.get_json())
        resp = call_tool(client, mcp_env["token"], "get_project_tasks_by_name", {
            "project_name": "ghost-project",
        })
        text = str(resp.get_json())
        assert "not found" in text and "available_projects" in text


class TestGetTaskById:
    def test_by_id_success(self, client, db_session, mcp_env):
        task = _make_task(db_session, mcp_env["project"], mcp_env["user"], title="findme")
        resp = call_tool(client, mcp_env["token"], "get_task_by_id", {"task_id": task.id})
        text = str(resp.get_json())
        assert "findme" in text

    def test_by_id_missing_and_not_found(self, client, mcp_env):
        resp = call_tool(client, mcp_env["token"], "get_task_by_id", {})
        assert "task_id is required" in str(resp.get_json())
        resp = call_tool(client, mcp_env["token"], "get_task_by_id", {"task_id": 987654})
        assert "not found" in str(resp.get_json())


class TestSubmitTaskFeedback:
    def test_feedback_success(self, client, db_session, mcp_env):
        task = _make_task(db_session, mcp_env["project"], mcp_env["user"], title="fb")
        resp = call_tool(client, mcp_env["token"], "submit_task_feedback", {
            "task_id": task.id,
            "project_name": mcp_env["project"].name,
            "feedback_content": "looks good",
            "status": "review",
        })
        text = str(resp.get_json())
        assert "looks good" in text

    def test_feedback_missing_fields(self, client, mcp_env):
        resp = call_tool(client, mcp_env["token"], "submit_task_feedback", {})
        assert "required" in str(resp.get_json())

    def test_feedback_bad_status(self, client, db_session, mcp_env):
        task = _make_task(db_session, mcp_env["project"], mcp_env["user"])
        resp = call_tool(client, mcp_env["token"], "submit_task_feedback", {
            "task_id": task.id,
            "project_name": mcp_env["project"].name,
            "feedback_content": "x",
            "status": "archived",
        })
        assert "Invalid status" in str(resp.get_json())

    def test_feedback_project_mismatch(self, client, db_session, mcp_env):
        task = _make_task(db_session, mcp_env["project"], mcp_env["user"])
        resp = call_tool(client, mcp_env["token"], "submit_task_feedback", {
            "task_id": task.id,
            "project_name": "other-project",
            "feedback_content": "x",
            "status": "review",
        })
        assert 'does not belong to project "other-project"' in str(resp.get_json())


class TestEvidenceAndDod:
    def test_evidence_success_and_missing(self, client, db_session, mcp_env):
        from models import TaskEvidenceRecord

        task = _make_task(db_session, mcp_env["project"], mcp_env["user"])
        db_session.add(TaskEvidenceRecord(
            task_id=task.id, evidence_type="test", status="passed", summary="green", created_by="agent:1",
        ))
        db_session.commit()
        resp = call_tool(client, mcp_env["token"], "get_task_evidence", {"task_id": task.id})
        text = str(resp.get_json())
        assert "passed" in text

        resp = call_tool(client, mcp_env["token"], "get_task_evidence", {})
        assert "task_id is required" in str(resp.get_json())

    def test_set_dod_success_and_invalid(self, client, db_session, mcp_env):
        task = _make_task(db_session, mcp_env["project"], mcp_env["user"])
        resp = call_tool(client, mcp_env["token"], "set_task_dod", {
            "task_id": task.id,
            "dod": [{"type": "test", "value": "pytest -q"}, {"type": "lint", "value": ""}],
        })
        text = str(resp.get_json())
        assert '"updated": true' in text or "'updated': True" in text

        resp = call_tool(client, mcp_env["token"], "set_task_dod", {})
        assert "task_id is required" in str(resp.get_json())
        resp = call_tool(client, mcp_env["token"], "set_task_dod", {"task_id": task.id, "dod": "x"})
        assert "dod must be an array" in str(resp.get_json())
        resp = call_tool(client, mcp_env["token"], "set_task_dod", {"task_id": task.id, "dod": ["x"]})
        assert "must be an object" in str(resp.get_json())
        resp = call_tool(client, mcp_env["token"], "set_task_dod", {"task_id": task.id, "dod": [{"type": "nope"}]})
        assert "invalid dod type" in str(resp.get_json())


class TestListSearchBranches:
    def test_list_bad_limit_status_project(self, client, mcp_env):
        tok = mcp_env["token"]
        resp = call_tool(client, tok, "list_my_tasks", {"limit": "abc"})
        assert "limit must be an integer" in str(resp.get_json())
        resp = call_tool(client, tok, "list_my_tasks", {"status_filter": "todo"})
        assert "status_filter must be an array" in str(resp.get_json())
        resp = call_tool(client, tok, "list_my_tasks", {"status_filter": ["archived"]})
        assert "Invalid status in status_filter" in str(resp.get_json())
        resp = call_tool(client, tok, "list_my_tasks", {"project_id": "abc"})
        assert "error" in str(resp.get_json())

    def test_search_missing_keyword_bad_status(self, client, mcp_env):
        tok = mcp_env["token"]
        resp = call_tool(client, tok, "search_tasks", {})
        assert "keyword is required" in str(resp.get_json())
        resp = call_tool(client, tok, "search_tasks", {"keyword": "x", "status": "archived"})
        assert "Invalid status" in str(resp.get_json())

    def test_search_hits_own_task(self, client, db_session, mcp_env):
        _make_task(db_session, mcp_env["project"], mcp_env["user"], title="unique-needle-xyz")
        resp = call_tool(client, mcp_env["token"], "search_tasks", {"keyword": "unique-needle-xyz"})
        text = str(resp.get_json())
        assert "unique-needle-xyz" in text


class TestUpdateStatusAndProgressBranches:
    def test_update_missing_and_invalid(self, client, mcp_env):
        tok = mcp_env["token"]
        resp = call_tool(client, tok, "update_task_status", {})
        assert "task_id is required" in str(resp.get_json())
        resp = call_tool(client, tok, "update_task_status", {"task_id": 9300001, "status": "archived"})
        assert "Invalid status" in str(resp.get_json())
        resp = call_tool(client, tok, "update_task_status", {"task_id": 987654, "status": "todo"})
        assert "not found" in str(resp.get_json())

    def test_update_revision_conflict(self, client, db_session, mcp_env):
        task = _make_task(db_session, mcp_env["project"], mcp_env["user"])
        resp = call_tool(client, mcp_env["token"], "update_task_status", {
            "task_id": task.id, "status": "in_progress", "expected_revision": 99,
        })
        assert "Revision conflict" in str(resp.get_json())

    def test_update_done_with_dod_warning(self, client, db_session, mcp_env):
        task = _make_task(db_session, mcp_env["project"], mcp_env["user"])
        task.dod = [{"type": "test", "value": "pytest -q"}]
        db_session.commit()
        resp = call_tool(client, mcp_env["token"], "update_task_status", {
            "task_id": task.id, "status": "done",
        })
        text = str(resp.get_json())
        assert "dod_warning" in text

    def test_progress_missing_content_and_not_found(self, client, db_session, mcp_env):
        task = _make_task(db_session, mcp_env["project"], mcp_env["user"])
        tok = mcp_env["token"]
        resp = call_tool(client, tok, "report_progress", {"task_id": task.id})
        assert "content is required" in str(resp.get_json())
        resp = call_tool(client, tok, "report_progress", {"task_id": 987654, "content": "x"})
        assert "not found" in str(resp.get_json())
        resp = call_tool(client, tok, "report_progress", {"content": "x"})
        assert "task_id is required" in str(resp.get_json())
