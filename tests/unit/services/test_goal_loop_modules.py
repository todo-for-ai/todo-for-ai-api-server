"""GoalLoop 拆分后各内聚模块的纯函数/边界分支覆盖测试。"""

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app import create_app
from models import db
from services.goal_loop import dispatch, planning, watchdog


@pytest.fixture(scope="module", autouse=True)
def _app_ctx():
    """部分助手会回源 DB，需要应用上下文（内存库即可）。"""
    app = create_app("testing")
    app.config.update({"TESTING": True,
                       "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
                       "SQLALCHEMY_ENGINE_OPTIONS": {}})
    ctx = app.app_context()
    ctx.push()
    db.create_all()
    yield app
    db.session.remove()
    db.drop_all()
    ctx.pop()


def _loop(**overrides):
    """规划器测试用的最小 loop 替身（不触 DB）。"""
    base = dict(
        id=1,
        title="t",
        goal_text="g",
        done_definition=None,
        rounds_limit=5,
        time_budget_hours=None,
        started_at=None,
        workspace_id=1,
        agent=SimpleNamespace(id=9, role_template=None),
        director_agent_id=None,
        director=SimpleNamespace(role_template=None),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ── planning.extract_json ──

def test_extract_json_plain():
    assert planning.extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_invalid_raises():
    with pytest.raises(json.JSONDecodeError):
        planning.extract_json("这不是 JSON")


def test_extract_json_fence_wrapped_invalid_inner():
    with pytest.raises(json.JSONDecodeError):
        planning.extract_json("```json\n{broken}\n```")


def test_extract_json_fence_wrapped_valid():
    assert planning.extract_json("```json\n{\"a\": 1}\n```") == {"a": 1}


def test_extract_json_surrounding_text():
    assert planning.extract_json('好的：{"a": 1} 以上') == {"a": 1}


def test_valid_steps_edges():
    assert not planning.valid_steps(None, 5)
    assert not planning.valid_steps([], 5)
    assert not planning.valid_steps([{"content": "无标题"}], 5)
    assert not planning.valid_steps([{"title": "x"}], 0)
    assert planning.valid_steps([{"title": "x"}], 5)


def test_llm_call_success_and_failure(monkeypatch):
    loop = _loop(created_by=7)

    monkeypatch.setattr(
        "services.ai_service.call_llm_production",
        lambda **kw: {"success": True, "data": '{"ok": 1}'},
    )
    assert planning.llm_call(loop, "s", "u") == {"ok": 1}

    monkeypatch.setattr(
        "services.ai_service.call_llm_production",
        lambda **kw: {"success": False, "error": "boom"},
    )
    with pytest.raises(RuntimeError, match="llm_failed"):
        planning.llm_call(loop, "s", "u")


def test_llm_call_passes_loop_owner_and_params(monkeypatch):
    import services.ai_service as ai
    loop = _loop(created_by=42)
    captured = {}

    def fake(feature, messages, user_id, use_cache, temperature, max_tokens):
        captured.update(feature=feature, user_id=user_id,
                        temperature=temperature, max_tokens=max_tokens)
        return {"success": True, "data": "{}"}

    monkeypatch.setattr(ai, "call_llm_production", fake)
    planning.llm_call(loop, "s", "u")
    assert captured == {"feature": "goal_loop", "user_id": 42,
                        "temperature": 0.4, "max_tokens": 2000}


def test_budget_line_variants():
    started = _loop(time_budget_hours=24,
                    started_at=datetime.utcnow() - timedelta(hours=10))
    line = planning.budget_line(started)
    assert "剩余时间预算" in line and "/24 小时" in line
    overdue = _loop(time_budget_hours=24,
                    started_at=datetime.utcnow() - timedelta(hours=100))
    assert "0.0/24" in planning.budget_line(overdue)
    assert planning.budget_line(_loop(time_budget_hours=None,
                                      started_at=datetime.utcnow())) == ""
    not_started = _loop(time_budget_hours=48, started_at=None)
    assert "尚未开跑" in planning.budget_line(not_started)


def test_decompose_rejects_bad_plan(monkeypatch):
    monkeypatch.setattr(dispatch, "executor_pool", lambda loop: [])
    monkeypatch.setattr(planning, "llm_call",
                        lambda loop, s, u: {"steps": "not-a-list"})
    with pytest.raises(RuntimeError, match="llm_bad_plan"):
        planning.decompose(_loop())


def test_review_rejects_bad_action_and_empty_extend(monkeypatch):
    monkeypatch.setattr(dispatch, "executor_pool", lambda loop: [])
    monkeypatch.setattr(planning, "llm_call",
                        lambda loop, s, u: {"action": "nonsense"})
    with pytest.raises(RuntimeError, match="llm_bad_action"):
        planning.review(_loop(), "done")

    monkeypatch.setattr(planning, "llm_call",
                        lambda loop, s, u: {"action": "extend"})
    with pytest.raises(RuntimeError, match="llm_extend_without_steps"):
        planning.review(_loop(), "done")


def test_planner_mode_and_scripted_paths(monkeypatch):
    monkeypatch.setenv("GOAL_LOOP_PLANNER", "scripted")
    assert planning.planner_mode() == "scripted"
    loop = _loop()
    assert len(planning.call_decompose(loop)) == 3
    assert planning.call_review(loop, "done")["action"] == "complete"
    assert planning.call_review(loop, "failed")["action"] == "blocked"


# ── dispatch 纯函数 ──

def test_role_context_without_template():
    agent = SimpleNamespace(role_template=None)
    assert dispatch.role_context(agent) == {"role": None, "role_description": None}


def test_role_context_with_template():
    agent = SimpleNamespace(role_template=SimpleNamespace(
        display_name="产品经理", category="pm", description="d"))
    ctx = dispatch.role_context(agent)
    assert ctx["role"] == "产品经理" and ctx["role_category"] == "pm"


def test_director_prefers_explicit_director():
    bound = SimpleNamespace(id=1)
    director = SimpleNamespace(id=2)
    loop = SimpleNamespace(agent=bound, director=director, director_agent_id=2)
    assert dispatch.director(loop) is director
    fallback = SimpleNamespace(agent=bound, director=None, director_agent_id=None)
    assert dispatch.director(fallback) is bound


def test_available_executor_roles_skips_unbound_and_respects_limit(monkeypatch):
    loop = SimpleNamespace(workspace_id=1)
    pool = [
        SimpleNamespace(role_template=None),                       # 无角色 → continue
        SimpleNamespace(role_template=SimpleNamespace(display_name="开发")),
        SimpleNamespace(role_template=SimpleNamespace(display_name="测试")),
    ]
    monkeypatch.setattr(dispatch, "executor_pool", lambda loop: pool)
    roles = dispatch.available_executor_roles(loop, limit=2)
    assert roles == ["开发", "测试"]


def test_pick_executor_skips_unbound_candidates(monkeypatch):
    fallback = SimpleNamespace(id=1, role_template=None)
    loop = SimpleNamespace(workspace_id=1, agent=fallback, director_agent_id=None,
                           director=None)
    pool = [
        SimpleNamespace(id=2, role_template=None),
        SimpleNamespace(id=3, role_template=SimpleNamespace(display_name="测试工程师",
                                                            name="qa_tpl")),
    ]
    monkeypatch.setattr(dispatch, "executor_pool", lambda loop: pool)
    picked = dispatch.pick_executor(loop, {"role": "测试工程师"})
    assert picked is pool[1]
    # 无匹配 → 回退绑定 Agent
    assert dispatch.pick_executor(loop, {"role": "不存在"}) is fallback


# ── watchdog ──

def test_stuck_task_hours_invalid_env(monkeypatch):
    monkeypatch.setenv("GOAL_LOOP_STUCK_TASK_HOURS", "not-a-number")
    assert watchdog._stuck_task_hours() == watchdog.DEFAULT_STUCK_TASK_HOURS


def test_stuck_task_hours_default(monkeypatch):
    monkeypatch.delenv("GOAL_LOOP_STUCK_TASK_HOURS", raising=False)
    assert watchdog._stuck_task_hours() == 6.0
