"""AI 任务拆分 API（api/ai_task_split.py）单元回归。

覆盖：输入清洗与子任务校验（标题截断/优先级回退/预估时间钳制/
依赖整数化）、LLM JSON 解析四级降级（直解析/```json 块/``` 块/
花括号提取/失败带原文）、拆分主端点（LLM 限流 429、错误 500、解析
失败、空子任务、无标题子任务、成功链路含父任务标签与 auto-assign
联动、缓存参数透传）、子任务列表（标签元数据解析与排序）、子任务
删除（父任务无标签的历史 NameError 500 已修）、重排序（长度校验）。

历史注记：主端点默认 atomic 分支原用 `db.session.begin()`——请求内
事务已开启时必抛 InvalidRequestError 被吃成 500，成功路径上线即坏；
已改为依赖请求级单一事务 + 失败统一 rollback 的天然原子性。
"""

import itertools
import uuid
from datetime import datetime

import pytest
from flask_jwt_extended import create_access_token

from models import (
    Project,
    Task,
    User,
    db,
)

_TASK_ID_SEQ = itertools.count(60_000_000)

VALID_LLM_PAYLOAD = """{
    "subtasks": [
        {"title": "设计接口", "description": "定义字段",
         "priority": "high", "estimated_hours": 3.5, "depends_on": []},
        {"title": "实现接口", "description": "写代码",
         "priority": "urgent", "estimated_hours": 8, "depends_on": [1]}
    ],
    "execution_order": "顺序",
    "dependencies": "2 依赖 1",
    "estimated_total_hours": 11.5
}"""


def _llm_success(content=VALID_LLM_PAYLOAD):
    return {"success": True, "data": content, "cached": False,
            "context": {"request_id": "req-1"},
            "usage": {"total_tokens": 42}}


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

    # 拆分成功链路会触发 runtime 自动派单，测试中打桩
    from services.agent_runtime_controller import AgentRuntimeController
    assigned = []
    monkeypatch.setattr(AgentRuntimeController, "auto_assign_task",
                        lambda task: assigned.append(task.id))
    app.config["_assigned"] = assigned

    yield app
    db.session.remove()
    db.drop_all()
    ctx.pop()


@pytest.fixture
def client(_isolated_app):
    return _isolated_app.test_client()


@pytest.fixture
def env(_isolated_app):
    user = User(username=f"sp_{uuid.uuid4().hex[:8]}",
                email=f"sp_{uuid.uuid4().hex[:6]}@t.io")
    db.session.add(user)
    db.session.flush()
    project = Project(name=f"p_{uuid.uuid4().hex[:6]}", owner_id=user.id)
    db.session.add(project)
    db.session.flush()
    task = Task(id=next(_TASK_ID_SEQ), title="Big parent task",
                content="do everything", status="TODO", priority="HIGH",
                project_id=project.id, owner_id=user.id,
                creator_id=user.id)
    db.session.add(task)
    db.session.commit()
    return {
        "user": user, "org": None, "project": project, "task": task,
        "app": _isolated_app,
        "headers": {"Authorization": f"Bearer {create_access_token(identity=str(user.id))}"},
        "base": "/todo-for-ai/api/v1",
    }


def _mk_subtask(env, parent, order=1, title="sub", tags_extra=None):
    row = Task(id=next(_TASK_ID_SEQ), title=title, content="c",
               status="TODO", priority="MEDIUM",
               project_id=parent.project_id, owner_id=env["user"].id,
               creator_id=env["user"].id, is_ai_task=True,
               tags=[f"parent_task:{parent.id}",
                     f"subtask_order:{order}"] + (tags_extra or []))
    db.session.add(row)
    db.session.flush()
    return row


def _patch_llm(monkeypatch, result):
    from api import ai_task_split as mod
    seen = {}
    def fake_call(**kwargs):
        seen.update(kwargs)
        return result
    monkeypatch.setattr(mod, "call_llm_production", fake_call)
    return seen


# ─────────────────────────── 纯函数 ───────────────────────────


class TestSanitizeInput:
    def test_empty_and_none(self):
        from api.ai_task_split import sanitize_input
        assert sanitize_input("") == ""
        assert sanitize_input(None) == ""

    def test_strips_tags_and_whitespace(self):
        from api.ai_task_split import sanitize_input
        assert sanitize_input("  <b>hello</b> ") == "hello"

    def test_truncates(self):
        from api.ai_task_split import sanitize_input
        out = sanitize_input("x" * 3000, 2000)
        assert len(out) == 2000


class TestValidateSubtaskData:
    def test_missing_title(self):
        from api.ai_task_split import validate_subtask_data
        ok, msg = validate_subtask_data({"description": "x"}, 1)
        assert not ok and "Title is required" in msg

    def test_long_title_truncated(self):
        from api.ai_task_split import validate_subtask_data
        sub = {"title": "t" * 500}
        ok, _ = validate_subtask_data(sub, 1)
        assert ok and len(sub["title"]) == 200

    def test_bad_priority_falls_back(self):
        from api.ai_task_split import validate_subtask_data
        sub = {"title": "a", "priority": "WHENEVER"}
        validate_subtask_data(sub, 1)
        assert sub["priority"] == "medium"

    def test_estimated_hours_matrix(self):
        from api.ai_task_split import validate_subtask_data
        sub = {"title": "a", "estimated_hours": 3.456}
        validate_subtask_data(sub, 1)
        assert sub["estimated_hours"] == 3.5

        for bad in (-1, 1001, "abc"):
            sub = {"title": "a", "estimated_hours": bad}
            validate_subtask_data(sub, 1)
            assert sub["estimated_hours"] is None

        sub = {"title": "a"}
        validate_subtask_data(sub, 1)
        assert "estimated_hours" not in sub  # 缺省时不落键

    def test_depends_on_matrix(self):
        from api.ai_task_split import validate_subtask_data
        sub = {"title": "a", "depends_on": "nope"}
        validate_subtask_data(sub, 1)
        assert sub["depends_on"] == []

        sub = {"title": "a", "depends_on": [1, "2", "x", 3.5, None]}
        validate_subtask_data(sub, 1)
        assert sub["depends_on"] == [1, 2]


class TestParseLlmJsonResponse:
    def test_empty(self):
        from api.ai_task_split import parse_llm_json_response
        assert parse_llm_json_response("") == {
            "success": False, "error": "Empty response"}

    def test_direct_json(self):
        from api.ai_task_split import parse_llm_json_response
        out = parse_llm_json_response('{"subtasks": [1]}')
        assert out["success"] and out["data"]["subtasks"] == [1]

    def test_markdown_json_block(self):
        from api.ai_task_split import parse_llm_json_response
        out = parse_llm_json_response('前言```json\n{"a": 1}\n```后记')
        assert out["success"] and out["data"] == {"a": 1}

    def test_plain_code_block(self):
        from api.ai_task_split import parse_llm_json_response
        out = parse_llm_json_response('```\n{"b": 2}\n```')
        assert out["success"] and out["data"] == {"b": 2}

    def test_brace_extraction(self):
        from api.ai_task_split import parse_llm_json_response
        out = parse_llm_json_response('结果如下 {"c": 3} 请查收')
        assert out["success"] and out["data"] == {"c": 3}

    def test_total_failure_keeps_raw(self):
        from api.ai_task_split import parse_llm_json_response
        out = parse_llm_json_response("完全不是 JSON")
        assert not out["success"] and out["raw"] == "完全不是 JSON"

    def test_invalid_json_block_falls_through(self):
        from api.ai_task_split import parse_llm_json_response
        out = parse_llm_json_response('```json\n{bad\n```')
        assert not out["success"]

    def test_broken_brace_block_falls_through(self):
        from api.ai_task_split import parse_llm_json_response
        out = parse_llm_json_response('{ bad }')
        assert not out["success"] and out["raw"] == '{ bad }'


# ─────────────────────────── 拆分主端点 ───────────────────────────


class TestSplitTaskEndpoint:
    def _url(self, env):
        return f"{env['base']}/tasks/{env['task'].id}/ai-split"

    def test_parent_missing_404(self, env, client):
        resp = client.post(f"{env['base']}/tasks/999999/ai-split",
                           headers=env["headers"], json={})
        assert resp.status_code == 404

    def test_num_subtasks_must_be_int(self, env, client, monkeypatch):
        _patch_llm(monkeypatch, _llm_success())
        resp = client.post(self._url(env), headers=env["headers"],
                           json={"num_subtasks": "three"})
        assert resp.status_code == 400

    def test_rate_limit_maps_429(self, env, client, monkeypatch):
        from services.ai_service import AIErrorCode
        _patch_llm(monkeypatch, {
            "success": False,
            "error_code": AIErrorCode.RATE_LIMIT_EXCEEDED.value,
            "error": "slow down"})
        resp = client.post(self._url(env), headers=env["headers"], json={})
        assert resp.status_code == 429

    def test_llm_error_maps_500(self, env, client, monkeypatch):
        _patch_llm(monkeypatch, {"success": False, "error": "upstream down"})
        resp = client.post(self._url(env), headers=env["headers"], json={})
        assert resp.status_code == 500
        assert "upstream down" in resp.get_json()["message"]

    def test_parse_failure_500(self, env, client, monkeypatch):
        _patch_llm(monkeypatch, _llm_success("完全不是 JSON"))
        resp = client.post(self._url(env), headers=env["headers"], json={})
        assert resp.status_code == 500
        assert "Parse failed" in resp.get_json()["message"]

    def test_empty_subtasks_500(self, env, client, monkeypatch):
        _patch_llm(monkeypatch, _llm_success('{"subtasks": []}'))
        resp = client.post(self._url(env), headers=env["headers"], json={})
        assert resp.status_code == 500
        assert "failed to generate" in resp.get_json()["message"]

    def test_subtask_without_title_500(self, env, client, monkeypatch):
        _patch_llm(monkeypatch, _llm_success(
            '{"subtasks": [{"description": "no title"}]}'))
        resp = client.post(self._url(env), headers=env["headers"], json={})
        assert resp.status_code == 500
        assert "Title is required" in resp.get_json()["message"]

    def test_happy_path(self, env, client, monkeypatch):
        seen = _patch_llm(monkeypatch, _llm_success())
        parent = env["task"]
        parent.tags = ["has_subtasks:9", "other"]
        db.session.commit()

        resp = client.post(self._url(env), headers=env["headers"],
                           json={"num_subtasks": 2})
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["total_subtasks"] == 2
        assert data["parent_task_id"] == parent.id
        assert data["execution_order"] == "顺序"
        assert data["estimated_total_hours"] == 11.5
        assert data["ai_metadata"]["tokens_used"] == 42
        assert data["ai_metadata"]["request_id"] == "req-1"
        assert [s["order"] for s in data["subtasks"]] == [1, 2]
        assert data["subtasks"][1]["depends_on"] == [1]

        # 父任务标签：旧计数被清除，新计数 + ai_split 标记
        db.session.expire_all()
        refreshed = db.session.get(Task, parent.id)
        assert "has_subtasks:2" in refreshed.tags
        assert "ai_split:true" in refreshed.tags
        assert "has_subtasks:9" not in refreshed.tags
        assert "other" in refreshed.tags

        # 子任务落库且标记 AI 任务、按序派单（引号定界 LIKE，与端点同款）
        subtasks = Task.query.filter(
            Task.tags.like(f'%\"parent_task:{parent.id}\"%')).all()
        assert len(subtasks) == 2
        assert all(t.is_ai_task for t in subtasks)
        assert list(env["app"].config["_assigned"]) == sorted(
            t.id for t in subtasks)

        # LLM 调用参数：特性/缓存/钳制
        assert seen["feature"] == "task_split"
        assert seen["cache_params"]["num_subtasks"] == 2
        assert seen["use_cache"] is True
        assert seen["max_tokens"] == 2500

    def test_no_cache_skips_cache_params(self, env, client, monkeypatch):
        seen = _patch_llm(monkeypatch, _llm_success())
        resp = client.post(self._url(env), headers=env["headers"],
                           json={"use_cache": False})
        assert resp.status_code == 200
        assert seen["cache_params"] is None
        assert seen["use_cache"] is False

    def test_num_subtasks_clamped(self, env, client, monkeypatch):
        seen = _patch_llm(monkeypatch, _llm_success())
        resp = client.post(self._url(env), headers=env["headers"],
                           json={"num_subtasks": 500})
        assert resp.status_code == 200
        assert seen["cache_params"]["num_subtasks"] == 20

    def test_auth_user_missing_401(self, env, client, monkeypatch):
        from api import ai_task_split as mod
        monkeypatch.setattr(mod, "get_current_user", lambda: None)
        resp = client.post(self._url(env), headers=env["headers"], json={})
        assert resp.status_code == 401

    def test_db_error_rollback_500(self, env, client, monkeypatch):
        from sqlalchemy.exc import SQLAlchemyError
        from api import ai_task_split as mod
        _patch_llm(monkeypatch, _llm_success())

        def boom(*a, **kw):
            raise SQLAlchemyError("insert failed")
        monkeypatch.setattr(mod, "_create_subtasks", boom)
        resp = client.post(self._url(env), headers=env["headers"], json={})
        assert resp.status_code == 500
        assert "Database error" in resp.get_json()["message"]

    def test_unexpected_error_handled_500(self, env, client, monkeypatch):
        from api import ai_task_split as mod
        _patch_llm(monkeypatch, _llm_success())

        def boom(content):
            raise RuntimeError("unexpected")
        monkeypatch.setattr(mod, "parse_llm_json_response", boom)
        resp = client.post(self._url(env), headers=env["headers"], json={})
        assert resp.status_code == 500
        assert "unexpected" in resp.get_json()["message"]


# ─────────────────────────── 子任务查询 / 删除 / 排序 ───────────────────────────


class TestSubtasksEndpoint:
    def test_parent_missing_404(self, env, client):
        resp = client.get(f"{env['base']}/tasks/999999/subtasks",
                          headers=env["headers"])
        assert resp.status_code == 404

    def test_list_orders_and_metadata(self, env, client):
        parent = env["task"]
        _mk_subtask(env, parent, order=2, title="second",
                    tags_extra=["estimated_hours:4.5"])
        _mk_subtask(env, parent, order=1, title="first",
                    tags_extra=["estimated_hours:oops"])
        db.session.commit()
        resp = client.get(f"{env['base']}/tasks/{parent.id}/subtasks",
                          headers=env["headers"])
        data = resp.get_json()["data"]
        assert data["total"] == 2
        assert [s["title"] for s in data["subtasks"]] == ["first", "second"]
        first, second = data["subtasks"]
        assert first["estimated_hours"] is None  # 坏值忽略
        assert second["estimated_hours"] == 4.5
        assert first["status"] == "todo"

    def test_list_unexpected_error_500(self, env, client, monkeypatch):
        from api import ai_task_split as mod

        class BoomQuery:
            def filter(self, *a, **kw):
                raise RuntimeError("db down")
        monkeypatch.setattr(mod, "Task", type("BoomTask", (), {
            "query": property(lambda self: BoomQuery())}))
        resp = client.get(f"{env['base']}/tasks/1/subtasks",
                          headers=env["headers"])
        assert resp.status_code == 500


class TestDeleteSubtaskEndpoint:
    def test_missing_404(self, env, client):
        resp = client.delete(
            f"{env['base']}/tasks/{env['task'].id}/subtasks/999999",
            headers=env["headers"])
        assert resp.status_code == 404

    def test_delete_updates_parent_tag(self, env, client):
        parent = env["task"]
        parent.tags = ["has_subtasks:2"]
        s1 = _mk_subtask(env, parent, order=1)
        s2 = _mk_subtask(env, parent, order=2)
        db.session.commit()
        resp = client.delete(
            f"{env['base']}/tasks/{parent.id}/subtasks/{s1.id}",
            headers=env["headers"])
        assert resp.status_code == 200
        assert resp.get_json()["data"]["remaining_subtasks"] == 1
        db.session.expire_all()
        assert db.session.get(Task, parent.id).tags == ["has_subtasks:1"]

    def test_delete_with_untagged_parent_no_500(self, env, client):
        # 历史 bug：父任务无 tags 时 remaining 未赋值 → NameError 500
        parent = env["task"]
        parent.tags = None
        s1 = _mk_subtask(env, parent, order=1)
        db.session.commit()
        resp = client.delete(
            f"{env['base']}/tasks/{parent.id}/subtasks/{s1.id}",
            headers=env["headers"])
        assert resp.status_code == 200
        assert resp.get_json()["data"]["remaining_subtasks"] == 0

    def test_delete_auth_user_missing_401(self, env, client, monkeypatch):
        parent = env["task"]
        s1 = _mk_subtask(env, parent, order=1)
        db.session.commit()
        from api import ai_task_split as mod
        monkeypatch.setattr(mod, "get_current_user", lambda: None)
        resp = client.delete(
            f"{env['base']}/tasks/{parent.id}/subtasks/{s1.id}",
            headers=env["headers"])
        assert resp.status_code == 401

    def test_delete_unexpected_error_500(self, env, client, monkeypatch):
        parent = env["task"]
        _mk_subtask(env, parent, order=1)
        db.session.commit()
        from api import ai_task_split as mod

        class BoomQuery:
            def filter(self, *a, **kw):
                raise RuntimeError("db down")
        monkeypatch.setattr(mod, "Task", type("BoomTask", (), {
            "query": property(lambda self: BoomQuery())}))
        resp = client.delete(
            f"{env['base']}/tasks/{parent.id}/subtasks/1",
            headers=env["headers"])
        assert resp.status_code == 500


class TestReorderEndpoint:
    def _url(self, env):
        return f"{env['base']}/tasks/{env['task'].id}/subtasks/reorder"

    def test_requires_orders_field(self, env, client):
        resp = client.put(self._url(env), headers=env["headers"], json={})
        assert resp.status_code == 400

    def test_orders_must_be_list(self, env, client):
        resp = client.put(self._url(env), headers=env["headers"],
                          json={"orders": "1,2"})
        assert resp.status_code == 400

    def test_length_mismatch_400(self, env, client):
        parent = env["task"]
        _mk_subtask(env, parent, order=1)
        db.session.commit()
        resp = client.put(self._url(env), headers=env["headers"],
                          json={"orders": [1, 2, 3]})
        assert resp.status_code == 400

    def test_reorder_updates_tags(self, env, client):
        parent = env["task"]
        s1 = _mk_subtask(env, parent, order=1)
        s2 = _mk_subtask(env, parent, order=2)
        db.session.commit()
        resp = client.put(self._url(env), headers=env["headers"],
                          json={"orders": [5, 6]})
        assert resp.status_code == 200
        db.session.expire_all()
        assert db.session.get(Task, s1.id).tags == [
            f"parent_task:{parent.id}", "subtask_order:5"]
        assert db.session.get(Task, s2.id).tags == [
            f"parent_task:{parent.id}", "subtask_order:6"]

    def test_reorder_unexpected_error_500(self, env, client, monkeypatch):
        from api import ai_task_split as mod

        class BoomQuery:
            def filter(self, *a, **kw):
                raise RuntimeError("db down")
        monkeypatch.setattr(mod, "Task", type("BoomTask", (), {
            "query": property(lambda self: BoomQuery())}))
        resp = client.put(self._url(env), headers=env["headers"],
                          json={"orders": [1]})
        assert resp.status_code == 500
