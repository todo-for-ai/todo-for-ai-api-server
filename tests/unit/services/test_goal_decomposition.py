"""目标分解服务（services/goal_decomposition.py）单元回归。

覆盖：Epic → 任务图全链路——LLM 响应三态错误（空内容/解析失败/
无任务）、网关二次转义的字符串 JSON 再解析、任务落地（标题截断、
非法优先级回退 medium、DoD 类型白名单过滤与 500 字符截断）、
depends_on 双向依赖写入（blocking/blocked_by 去重、自依赖忽略、
非法序号跳过、无效条目跳过）、显式 project_id 与工作区回退解析
（无项目报错）。
"""

import uuid

import pytest

from models import (
    Epic,
    Goal,
    Organization,
    Project,
    Task,
    User,
    db,
)
from services.goal_decomposition import (
    _normalize_dod,
    _resolve_project_id,
    expand_epic_to_tasks,
)

_VALID_LLM = {
    "content": """{
        "tasks": [
            {"title": "任务一", "description": "d1", "priority": "high",
             "dod": [{"type": "test", "value": "pytest -q"},
                     {"type": "bogus", "value": "dropped"},
                     "not-a-dict"],
             "depends_on": []},
            {"title": "任务二", "description": "d2", "priority": "warp",
             "dod": [{"type": "build", "value": "npm run build"}],
             "depends_on": [1, 1, "1", 2, "x", 9]}
        ],
        "execution_order": "顺序"
    }""",
}


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


@pytest.fixture
def env(_isolated_app):
    user = User(username=f"gd_{uuid.uuid4().hex[:8]}",
                email=f"gd_{uuid.uuid4().hex[:6]}@t.io")
    db.session.add(user)
    db.session.flush()
    org = Organization(name=f"o_{uuid.uuid4().hex[:6]}",
                       slug=f"o_{uuid.uuid4().hex[:6]}", owner_id=user.id)
    db.session.add(org)
    db.session.flush()
    project = Project(name=f"p_{uuid.uuid4().hex[:6]}", owner_id=user.id,
                      organization_id=org.id)
    db.session.add(project)
    goal = Goal(workspace_id=org.id, title="把留存提升 20%",
                metrics=["DAU 10k"], owner_id=user.id)
    db.session.add(goal)
    db.session.flush()
    epic = Epic(goal_id=goal.id, title="签到系统", description="连续签到")
    db.session.add(epic)
    db.session.commit()
    return {"user": user, "org": org, "project": project,
            "goal": goal, "epic": epic}


def _patch_llm(monkeypatch, response):
    import services.ai_service as ai
    seen = {}
    def fake_call(**kwargs):
        seen.update(kwargs)
        return response
    monkeypatch.setattr(ai, "call_llm_production", fake_call)
    return seen


def _epic_tasks(epic):
    return Task.query.filter_by(epic_id=epic.id).order_by(Task.id).all()


class TestExpandEpicToTasks:
    def test_success_creates_tasks_with_dod_and_dependencies(
            self, env, monkeypatch):
        seen = _patch_llm(monkeypatch, _VALID_LLM)
        result = expand_epic_to_tasks(env["epic"], project_id=env["project"].id)
        assert seen["feature"] == "goal_expand_epic"
        assert sorted(result["tasks"]) == result["tasks"]
        assert len(result["tasks"]) == 2
        assert result["execution_order"] == "顺序"

        tasks = _epic_tasks(env["epic"])
        assert [t.title for t in tasks] == ["任务一", "任务二"]
        first, second = tasks
        assert first.priority.value == "high"
        assert second.priority.value == "medium"  # 非法优先级回退
        # DoD 白名单：bogus 被过滤
        assert [d["type"] for d in first.dod] == ["test"]
        # depends_on 双向写入且去重
        assert second.blocked_by_task_ids == [first.id]
        assert first.blocking_task_ids == [second.id]
        assert all(t.is_ai_task for t in tasks)

    def test_string_data_double_decoded(self, env, monkeypatch):
        import json
        _patch_llm(monkeypatch, {
            "content": json.dumps(json.dumps({
                "tasks": [{"title": "串起来的"}],
                "execution_order": "并行"}))})
        result = expand_epic_to_tasks(env["epic"],
                                      project_id=env["project"].id)
        assert len(result["tasks"]) == 1
        assert result["execution_order"] == "并行"

    def test_empty_content_raises(self, env, monkeypatch):
        _patch_llm(monkeypatch, {"content": None})
        with pytest.raises(ValueError, match="empty content"):
            expand_epic_to_tasks(env["epic"])

    def test_parse_failure_raises(self, env, monkeypatch):
        from api.ai_task_split import parse_llm_json_response
        _patch_llm(monkeypatch, {"content": "totally not json"})
        with pytest.raises(ValueError, match="parse failed"):
            expand_epic_to_tasks(env["epic"])

    def test_no_tasks_raises(self, env, monkeypatch):
        _patch_llm(monkeypatch, {"content": '{"tasks": []}'})
        with pytest.raises(ValueError, match="no tasks"):
            expand_epic_to_tasks(env["epic"])

    def test_invalid_items_skipped(self, env, monkeypatch):
        _patch_llm(monkeypatch, {"content": """{
            "tasks": ["not-a-dict", {"description": "no title"},
                      {"title": "合法任务"}]
        }"""})
        result = expand_epic_to_tasks(env["epic"],
                                      project_id=env["project"].id)
        assert len(result["tasks"]) == 1

    def test_resolve_project_fallback_within_workspace(self, env,
                                                       monkeypatch):
        _patch_llm(monkeypatch, _VALID_LLM)
        # 不传 project_id：回退 goal 工作区内最新项目
        result = expand_epic_to_tasks(env["epic"])
        assert result["tasks"]
        tasks = _epic_tasks(env["epic"])
        assert all(t.project_id == env["project"].id for t in tasks)

    def test_resolve_project_no_project_raises(self, env):
        from models import Project
        Project.query.filter_by(
            organization_id=env["org"].id).delete()
        db.session.commit()
        with pytest.raises(ValueError, match="no project"):
            _resolve_project_id(env["epic"])


class TestNormalizeDod:
    def test_non_list_returns_empty(self):
        assert _normalize_dod("x") == []
        assert _normalize_dod(None) == []

    def test_type_whitelist_and_truncation(self):
        from models import TaskEvidenceRecord
        out = _normalize_dod([
            {"type": "TEST", "value": "v"},
            {"type": "manual", "value": "human only"},
            {"type": "command", "value": "y" * 600},
            {"no-type": 1},
        ])
        assert [d["type"] for d in out] == ["test", "manual", "command"]
        assert out[2]["value"] == "y" * 500
        assert all(set(d) == {"type", "value"} for d in out)
        assert "manual" in TaskEvidenceRecord.TYPES  # 白名单语义对照
