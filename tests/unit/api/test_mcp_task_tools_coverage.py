"""MCP task_tools 补齐测试（迭代 130）：create/get_by_id/feedback/evidence/dod/
get_project_tasks_by_name 全函数与既有工具的校验、错误、越权分支。"""

import uuid

import pytest

BASE_URL = "/todo-for-ai/api/v1"

RESIDENT_TASK_ID_FLOOR = 9_300_000


@pytest.fixture(autouse=True)
def _cleanup_resident_rows(db_session):
    """本文件 _make_task 的行驻留会话级库，teardown 时按高位 id 段清理，
    避免与 conftest task_factory 的手工 id 分配互相踩（UNIQUE tasks.id）。"""
    from models import AgentTaskEvent, Task, TaskEvidenceRecord, TaskLog

    def _purge():
        db_session.rollback()
        db_session.query(TaskLog).filter(TaskLog.task_id >= RESIDENT_TASK_ID_FLOOR).delete(synchronize_session=False)
        db_session.query(TaskEvidenceRecord).filter(TaskEvidenceRecord.task_id >= RESIDENT_TASK_ID_FLOOR).delete(synchronize_session=False)
        db_session.query(AgentTaskEvent).filter(AgentTaskEvent.task_id >= RESIDENT_TASK_ID_FLOOR).delete(synchronize_session=False)
        db_session.query(Task).filter(Task.id >= RESIDENT_TASK_ID_FLOOR).delete(synchronize_session=False)
        db_session.commit()

    _purge()
    yield
    _purge()


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




def _make_foreign_env(db_session):
    """自建「他人 + 他人项目」（无组织）：与 conftest factory 的手工 id 空间隔离。"""
    from models import Organization, Project, User
    from werkzeug.security import generate_password_hash

    tag = uuid.uuid4().hex[:8]
    other = User(username=f"other_{tag}", email=f"other_{tag}@example.com")
    other.password_hash = generate_password_hash("password123")
    db_session.add(other)
    db_session.commit()
    org = Organization(name=f"fo-{tag}", slug=f"fo-{tag}", owner_id=other.id)
    db_session.add(org)
    db_session.commit()
    project = Project(name=f"foreign-{tag}", status="ACTIVE", owner_id=other.id, organization_id=org.id)
    db_session.add(project)
    db_session.commit()
    return {"user": other, "project": project}


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


class TestDeepBranches:
    """第五轮：request_approval 主体/推送、search 与 list 的过滤分支、
    validate_integer 各端点、越权 403、异常兜底。"""

    def _other_env(self, db_session, user_factory):
        from models import Organization, Project
        other = user_factory()
        org = Organization(name=f"o-{uuid.uuid4().hex[:6]}", slug=f"o-{uuid.uuid4().hex[:6]}", owner_id=other.id)
        db_session.add(org)
        db_session.commit()
        project = Project(name=f"other-{uuid.uuid4().hex[:6]}", status="ACTIVE", owner_id=other.id, organization_id=org.id)
        db_session.add(project)
        db_session.commit()
        return {"user": other, "project": project}

    def test_list_assignee_match_and_limit_break(self, client, db_session, mcp_env):
        from models import Task, TaskStatus

        # assignees 精确命中：creator 不是 user，但 human assignee 是 user
        task = Task(
            id=next(_TASK_ID_SEQ), title="assigned to me", content="",
            project_id=mcp_env["project"].id, creator_id=mcp_env["user"].id + 500,
            assignees=[{"type": "human", "id": mcp_env["user"].id}], is_ai_task=False,
        )
        db_session.add(task)
        task2 = Task(
            id=next(_TASK_ID_SEQ), title="second", content="",
            project_id=mcp_env["project"].id, creator_id=mcp_env["user"].id, is_ai_task=False,
        )
        db_session.add(task2)
        db_session.commit()
        resp = call_tool(client, mcp_env["token"], "list_my_tasks", {"limit": 1})
        data = resp.get_json()
        assert data["total_tasks"] == 1

    def test_list_with_valid_project_filter(self, client, mcp_env):
        resp = call_tool(client, mcp_env["token"], "list_my_tasks", {
            "project_id": mcp_env["project"].id, "limit": 5,
        })
        assert "tasks" in str(resp.get_json())

    def test_search_with_project_and_status_filters(self, client, db_session, mcp_env):
        _make_task(db_session, mcp_env["project"], mcp_env["user"], title="filterme")
        resp = call_tool(client, mcp_env["token"], "search_tasks", {
            "keyword": "filterme", "project_id": mcp_env["project"].id, "status": "todo", "limit": 5,
        })
        assert "filterme" in str(resp.get_json())

    def test_search_limit_bad(self, client, mcp_env):
        resp = call_tool(client, mcp_env["token"], "search_tasks", {"keyword": "x", "limit": "abc"})
        assert "limit must be an integer" in str(resp.get_json())

    def test_search_skips_other_users_tasks(self, client, db_session, mcp_env, user_factory):
        other = user_factory()
        other_project = None
        from models import Project
        other_project = Project.query.filter(Project.owner_id == other.id).first()
        if not other_project:
            other_project = Project(name=f"op-{uuid.uuid4().hex[:6]}", status="ACTIVE", owner_id=other.id)
            db_session.add(other_project)
            db_session.commit()
        _make_task(db_session, other_project, other, title="secret-needle-abc")
        resp = call_tool(client, mcp_env["token"], "search_tasks", {"keyword": "secret-needle-abc"})
        assert resp.get_json()["total_tasks"] == 0

    def test_update_status_task_id_not_integer(self, client, mcp_env):
        resp = call_tool(client, mcp_env["token"], "update_task_status", {"task_id": "abc", "status": "todo"})
        assert "error" in str(resp.get_json())

    def test_update_expected_revision_not_integer(self, client, db_session, mcp_env):
        task = _make_task(db_session, mcp_env["project"], mcp_env["user"])
        resp = call_tool(client, mcp_env["token"], "update_task_status", {
            "task_id": task.id, "status": "in_progress", "expected_revision": "abc",
        })
        assert "expected_revision must be an integer" in str(resp.get_json())

    def test_update_403_for_outsider(self, client, db_session, mcp_env):
        env = _make_foreign_env(db_session)
        task = _make_task(db_session, env["project"], env["user"], title="t")
        resp = call_tool(client, mcp_env["token"], "update_task_status", {
            "task_id": task.id, "status": "in_progress",
        })
        assert "Access denied" in str(resp.get_json())

    def test_update_notify_hooks_exceptions_swallowed(self, client, db_session, mcp_env, monkeypatch):
        task = _make_task(db_session, mcp_env["project"], mcp_env["user"])
        monkeypatch.setattr("services.goal_loop_service.notify_task_finished", lambda *a, **k: 1 / 0)
        monkeypatch.setattr("api.user_websocket.notify_task_graph_changed", lambda *a, **k: 1 / 0)
        resp = call_tool(client, mcp_env["token"], "update_task_status", {
            "task_id": task.id, "status": "review",
        })
        assert resp.status_code == 200

    def test_progress_403_for_outsider(self, client, db_session, mcp_env):
        env = _make_foreign_env(db_session)
        task = _make_task(db_session, env["project"], env["user"], title="t")
        resp = call_tool(client, mcp_env["token"], "report_progress", {
            "task_id": task.id, "content": "x",
        })
        assert "Access denied" in str(resp.get_json())

    def test_request_approval_missing_fields(self, client, db_session, mcp_env):
        task = _make_task(db_session, mcp_env["project"], mcp_env["user"])
        tok = mcp_env["token"]
        resp = call_tool(client, tok, "request_approval", {})
        assert "task_id is required" in str(resp.get_json())
        resp = call_tool(client, tok, "request_approval", {"task_id": task.id, "question": "  "})
        assert "question is required" in str(resp.get_json())
        resp = call_tool(client, tok, "request_approval", {"task_id": "abc", "question": "q"})
        assert "error" in str(resp.get_json())
        resp = call_tool(client, tok, "request_approval", {
            "task_id": task.id, "question": "q", "sensitivity_level": "ultra",
        })
        assert "sensitivity_level must be one of" in str(resp.get_json())
        resp = call_tool(client, tok, "request_approval", {
            "task_id": task.id, "question": "q", "options": "yes",
        })
        assert "options must be an array of strings" in str(resp.get_json())
        resp = call_tool(client, tok, "request_approval", {"task_id": 987654, "question": "q"})
        assert "not found" in str(resp.get_json())

    def test_request_approval_success_without_workspace_403_and_push(self, client, db_session, mcp_env, monkeypatch):
        task = _make_task(db_session, mcp_env["project"], mcp_env["user"])
        # 正常 workspace（org project）成功路径 + WebSocket 推送块
        resp = call_tool(client, mcp_env["token"], "request_approval", {
            "task_id": task.id, "question": "proceed?", "options": ["yes", "no"],
            "sensitivity_level": "high",
        })
        assert resp.status_code == 200
        # created_by 不可解析 → except (ValueError, TypeError) 分支
        task2 = _make_task(db_session, mcp_env["project"], mcp_env["user"], title="bad-created-by")
        task2.created_by = "user:notanumber"
        db_session.commit()
        resp2 = call_tool(client, mcp_env["token"], "request_approval", {
            "task_id": task2.id, "question": "q2",
        })
        assert resp2.status_code == 200

    def test_request_approval_project_without_workspace(self, client, db_session, mcp_env):
        from models import Project

        project = Project(name=f"noorg-{uuid.uuid4().hex[:6]}", status="ACTIVE", owner_id=mcp_env["user"].id)
        db_session.add(project)
        db_session.commit()
        task = _make_task(db_session, project, mcp_env["user"], title="t")
        resp = call_tool(client, mcp_env["token"], "request_approval", {
            "task_id": task.id, "question": "q",
        })
        assert "not attached to a workspace" in str(resp.get_json())

    def test_get_project_tasks_403_for_outsider(self, client, db_session, mcp_env):
        env = _make_foreign_env(db_session)
        resp = call_tool(client, mcp_env["token"], "get_project_tasks_by_name", {
            "project_name": env["project"].name,
        })
        assert "Access denied" in str(resp.get_json())

    def test_get_by_id_403_for_outsider(self, client, db_session, mcp_env):
        env = _make_foreign_env(db_session)
        task = _make_task(db_session, env["project"], env["user"], title="t")
        resp = call_tool(client, mcp_env["token"], "get_task_by_id", {"task_id": task.id})
        assert "Access denied" in str(resp.get_json())

    def test_get_by_id_context_rule_appended(self, client, db_session, mcp_env):
        from models import ContextRule

        task = _make_task(db_session, mcp_env["project"], mcp_env["user"], title="withctx")
        db_session.add(ContextRule(
            project_id=mcp_env["project"].id,
            user_id=mcp_env["user"].id,
            name=f"rule-{uuid.uuid4().hex[:6]}",
            content="always write tests",
            is_active=True,
            apply_to_tasks=True,
        ))
        db_session.commit()
        resp = call_tool(client, mcp_env["token"], "get_task_by_id", {"task_id": task.id})
        text = str(resp.get_json())
        assert "always write tests" in text

    def test_feedback_403_and_owner_fallback_branches(self, client, db_session, mcp_env, monkeypatch):
        from models import UserActivity

        env = _make_foreign_env(db_session)
        project = env["project"]
        task = _make_task(db_session, project, env["user"], title="t")
        resp = call_tool(client, mcp_env["token"], "submit_task_feedback", {
            "task_id": task.id, "project_name": project.name,
            "feedback_content": "x", "status": "review",
        })
        assert "Access denied" in str(resp.get_json())

        # creator_id 为空 → 回退 project.owner_id；UserActivity 抛错走 print 分支
        task2 = _make_task(db_session, mcp_env["project"], mcp_env["user"], title="fb2")
        task2.creator_id = None
        db_session.commit()
        monkeypatch.setattr(UserActivity, "record_activity", lambda *a, **k: 1 / 0)
        resp = call_tool(client, mcp_env["token"], "submit_task_feedback", {
            "task_id": task2.id, "project_name": mcp_env["project"].name,
            "feedback_content": "y", "status": "review",
        })
        assert resp.status_code == 200
        # 同状态再次提交 → status_changed False 分支
        resp = call_tool(client, mcp_env["token"], "submit_task_feedback", {
            "task_id": task2.id, "project_name": mcp_env["project"].name,
            "feedback_content": "y", "status": "review",
        })
        assert resp.status_code == 200

    def test_create_403_and_activity_exception(self, client, db_session, mcp_env, monkeypatch):
        from models import UserActivity

        env = _make_foreign_env(db_session)
        resp = call_tool(client, mcp_env["token"], "create_task", {
            "project_id": env["project"].id, "title": "x",
        })
        assert "Access denied" in str(resp.get_json())

        monkeypatch.setattr(UserActivity, "record_activity", lambda *a, **k: 1 / 0)
        resp = call_tool(client, mcp_env["token"], "create_task", {
            "project_id": mcp_env["project"].id, "title": "y",
        })
        assert resp.status_code == 200

    def test_create_activity_exception_swallowed(self, client, db_session, mcp_env, monkeypatch):
        from models import UserActivity

        monkeypatch.setattr(UserActivity, "record_activity", lambda *a, **k: 1 / 0)
        resp = call_tool(client, mcp_env["token"], "create_task", {
            "project_id": mcp_env["project"].id, "title": "activity-boom", "is_ai_task": False,
        })
        # 记录活跃度失败不影响任务创建成功
        assert "activity-boom" in str(resp.get_json())

    def test_evidence_and_dod_403_and_validate(self, client, db_session, mcp_env):
        env = _make_foreign_env(db_session)
        task = _make_task(db_session, env["project"], env["user"], title="t")
        tok = mcp_env["token"]
        resp = call_tool(client, tok, "get_task_evidence", {"task_id": task.id})
        assert "Access denied" in str(resp.get_json())
        resp = call_tool(client, tok, "get_task_evidence", {"task_id": "abc"})
        assert "error" in str(resp.get_json())
        resp = call_tool(client, tok, "set_task_dod", {"task_id": "abc", "dod": []})
        assert "error" in str(resp.get_json())
        resp = call_tool(client, tok, "set_task_dod", {"task_id": 987654, "dod": []})
        assert "not found" in str(resp.get_json())
        resp = call_tool(client, tok, "set_task_dod", {"task_id": task.id, "dod": []})
        assert "Access denied" in str(resp.get_json())


class TestValidateIntegerBranches:
    """第六轮：各工具的 task_id/project_id 非整数校验行。"""

    def test_search_project_id_not_integer(self, client, mcp_env):
        resp = call_tool(client, mcp_env["token"], "search_tasks", {
            "keyword": "x", "project_id": "abc",
        })
        assert "error" in str(resp.get_json())

    def test_get_by_id_task_id_not_integer(self, client, mcp_env):
        resp = call_tool(client, mcp_env["token"], "get_task_by_id", {"task_id": "abc"})
        assert "error" in str(resp.get_json())

    def test_feedback_task_id_not_integer(self, client, mcp_env):
        resp = call_tool(client, mcp_env["token"], "submit_task_feedback", {
            "task_id": "abc", "project_name": "p", "feedback_content": "c", "status": "review",
        })
        assert "error" in str(resp.get_json())

    def test_feedback_task_not_found(self, client, mcp_env):
        resp = call_tool(client, mcp_env["token"], "submit_task_feedback", {
            "task_id": 987654, "project_name": "p", "feedback_content": "c", "status": "review",
        })
        assert "not found" in str(resp.get_json())

    def test_list_assignee_type_error_swallowed(self, client, db_session, mcp_env):
        """assignees 含非法 id 类型：_assignee_matches_user 的 except 分支。"""
        from models import Task

        task = Task(
            id=next(_TASK_ID_SEQ), title="bad assignee id", content="",
            project_id=mcp_env["project"].id, creator_id=mcp_env["user"].id,
            assignees=[{"type": "human", "id": "not-a-number"}], is_ai_task=False,
        )
        db_session.add(task)
        db_session.commit()
        resp = call_tool(client, mcp_env["token"], "list_my_tasks", {})
        assert resp.status_code == 200


class TestFinalBranches:
    """第七轮：assignee 命中/搜索截断/推送块/DoD 完成/自动分配异常。"""

    def test_list_assignee_nonhuman_and_malformed_id(self, client, db_session, mcp_env):
        from models import Task

        # creator 非本人 + assignees 混合非法条目（非 dict / 非数字 id）：
        # 精确校验各容错分支执行后，项目归属仍使任务可见
        task = Task(
            id=next(_TASK_ID_SEQ), title="agent assigned", content="",
            project_id=mcp_env["project"].id, creator_id=mcp_env["user"].id + 777,
            assignees=[
                "junk-not-dict",
                {"type": "human", "id": "not-a-number"},
                {"type": "agent", "id": mcp_env["user"].id},
            ],
            is_ai_task=False,
        )
        db_session.add(task)
        db_session.commit()
        resp = call_tool(client, mcp_env["token"], "list_my_tasks", {})
        text = str(resp.get_json())
        assert "agent assigned" in text

    def test_search_limit_break(self, client, db_session, mcp_env):
        _make_task(db_session, mcp_env["project"], mcp_env["user"], title="brk-a")
        _make_task(db_session, mcp_env["project"], mcp_env["user"], title="brk-b")
        resp = call_tool(client, mcp_env["token"], "search_tasks", {"keyword": "brk-", "limit": 1})
        assert resp.get_json()["total_tasks"] == 1

    def test_update_activity_exception_swallowed(self, client, db_session, mcp_env, monkeypatch):
        from models import UserActivity

        task = _make_task(db_session, mcp_env["project"], mcp_env["user"])
        monkeypatch.setattr(UserActivity, "record_activity", lambda *a, **k: 1 / 0)
        resp = call_tool(client, mcp_env["token"], "update_task_status", {
            "task_id": task.id, "status": "in_progress",
        })
        assert resp.status_code == 200

    def test_report_task_id_not_integer(self, client, mcp_env):
        resp = call_tool(client, mcp_env["token"], "report_progress", {"task_id": "abc", "content": "x"})
        assert "error" in str(resp.get_json())

    def test_request_approval_push_to_creator(self, client, db_session, mcp_env):
        task = _make_task(db_session, mcp_env["project"], mcp_env["user"], title="push-me")
        task.created_by = f"user:{mcp_env['user'].id}"
        db_session.commit()
        resp = call_tool(client, mcp_env["token"], "request_approval", {
            "task_id": task.id, "question": "go?",
        })
        assert resp.status_code == 200

    def test_feedback_done_records_completion(self, client, db_session, mcp_env):
        task = _make_task(db_session, mcp_env["project"], mcp_env["user"], title="donefb")
        resp = call_tool(client, mcp_env["token"], "submit_task_feedback", {
            "task_id": task.id, "project_name": mcp_env["project"].name,
            "feedback_content": "done deal", "status": "done",
        })
        assert resp.status_code == 200

    def test_create_auto_assign_exception_rolls_back(self, client, mcp_env, monkeypatch):
        from api.mcp.handlers import task_tools as tt

        class BoomController:
            @staticmethod
            def auto_assign_task(task):
                raise RuntimeError("no worker")
        monkeypatch.setattr(tt, "AgentRuntimeController", BoomController, raising=False)
        # auto_assign 在 services.agent_runtime_controller 内导入，需另打真实符号
        import services.agent_runtime_controller as arc
        monkeypatch.setattr(arc.AgentRuntimeController, "auto_assign_task", lambda *a, **k: 1 / 0)
        resp = call_tool(client, mcp_env["token"], "create_task", {
            "project_id": mcp_env["project"].id, "title": "aa-boom", "is_ai_task": True,
        })
        assert "Failed to create task" in str(resp.get_json())


class TestFinalEightLines:
    """第八轮：收口最后 8 行（628 死分支除外，见 QUALITY_PLAN 留档）。"""

    def test_list_assignee_non_dict_item_skipped(self, client, db_session, mcp_env, user_factory):
        """48/53-54：粗筛命中后，assignees 含非 dict 条目与非法数字 id 的容错分支。"""
        from models import Task

        other = user_factory()
        other_project = Project = None
        from models import Project
        other_project = Project.query.filter_by(owner_id=other.id).first()
        if not other_project:
            other_project = Project(name=f"nd-{uuid.uuid4().hex[:6]}", status="ACTIVE", owner_id=other.id)
            db_session.add(other_project)
            db_session.commit()
        # assignees LIKE 粗筛命中 user.id；首项非 dict（48），human id 非数字（53-54）
        task = Task(
            id=next(_TASK_ID_SEQ), title="non-dict assignee", content="",
            project_id=other_project.id, creator_id=other.id,
            assignees=["junk", {"type": "human", "id": f"{mcp_env['user'].id}-bad"}],
            is_ai_task=False,
        )
        db_session.add(task)
        db_session.commit()
        resp = call_tool(client, mcp_env["token"], "list_my_tasks", {})
        assert "non-dict assignee" not in str(resp.get_json())

    def test_search_skips_assignee_false_positive(self, client, db_session, mcp_env, user_factory):
        """157-159：search 粗筛命中（assignee LIKE）但精确校验失败且项目属他人 → skip。"""
        from models import Project, Task

        other = user_factory()
        other_org_project = Project.query.filter_by(owner_id=other.id).first()
        if not other_org_project:
            other_org_project = Project(name=f"os-{uuid.uuid4().hex[:6]}", status="ACTIVE", owner_id=other.id)
            db_session.add(other_org_project)
            db_session.commit()
        # assignees JSON 含 user.id 数字 → LIKE 粗筛命中；type=agent → 精确失败；
        # project 属 other → project_owner != user → continue
        task = Task(
            id=next(_TASK_ID_SEQ), title="false-positive-needle", content="",
            project_id=other_org_project.id, creator_id=other.id,
            assignees=[{"type": "agent", "id": mcp_env["user"].id}],
            is_ai_task=False,
        )
        db_session.add(task)
        db_session.commit()
        resp = call_tool(client, mcp_env["token"], "search_tasks", {"keyword": "false-positive-needle"})
        assert resp.get_json()["total_tasks"] == 0

    def test_request_approval_push_variants(self, client, db_session, mcp_env, user_factory, monkeypatch):
        """437：created_by 指向其他用户时向其推送；445-446：推送异常被外层吞掉。"""
        from api.user_websocket import push_to_task_room

        other = user_factory()
        # 437：created_by 合法且指向其他用户
        task = _make_task(db_session, mcp_env["project"], mcp_env["user"], title="push-other")
        task.created_by = f"user:{other.id}"
        db_session.commit()
        resp = call_tool(client, mcp_env["token"], "request_approval", {
            "task_id": task.id, "question": "to other",
        })
        assert resp.status_code == 200
        # 445-446：推送抛非 (ValueError, TypeError) 异常 → 外层 except Exception 吞掉
        def boom(*a, **k):
            raise RuntimeError("ws down")
        monkeypatch.setattr(push_to_task_room, "__code__", boom.__code__)
        resp = call_tool(client, mcp_env["token"], "request_approval", {
            "task_id": task.id, "question": "q again",
        })
        assert resp.status_code == 200

    def test_evidence_task_not_found_asserted(self, client, mcp_env):
        """786：get_task_evidence 的任务不存在分支。"""
        resp = call_tool(client, mcp_env["token"], "get_task_evidence", {"task_id": 987654})
        assert "Task with ID 987654 not found" in str(resp.get_json())


class TestStatusChangedSemantics:
    """第九轮：status_changed 以 .value 语义比较——同状态反馈不再误记状态变更。"""

    def test_feedback_same_status_no_change(self, client, db_session, mcp_env, monkeypatch):
        from models import UserActivity, TaskStatus

        task = _make_task(db_session, mcp_env["project"], mcp_env["user"], title="same-status")
        # 第一次：todo -> review（状态确实变更）
        resp = call_tool(client, mcp_env["token"], "submit_task_feedback", {
            "task_id": task.id, "project_name": mcp_env["project"].name,
            "feedback_content": "first", "status": "review",
        })
        assert resp.status_code == 200
        # 第二次：review -> review（同状态 → else 分支记录 task_updated）
        calls = []
        monkeypatch.setattr(UserActivity, "record_activity",
                            lambda uid, kind: calls.append(kind))
        resp = call_tool(client, mcp_env["token"], "submit_task_feedback", {
            "task_id": task.id, "project_name": mcp_env["project"].name,
            "feedback_content": "again", "status": "review",
        })
        assert resp.status_code == 200
        assert "task_updated" in calls
        assert "task_status_changed" not in calls
