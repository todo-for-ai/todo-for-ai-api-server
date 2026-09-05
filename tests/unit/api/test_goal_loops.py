"""GoalLoop 目标循环的回归测试。

覆盖：创建即推进、终态触发下一轮、scripted complete 收尾、轮数上限、
连续受阻 stalled、暂停不推进/恢复续跑、权限、agent 工作区校验、
LLM 规划器输出解析。
"""

import json
import uuid

import pytest

from app import create_app
from models import db

BASE_URL = "/todo-for-ai/api/v1/projects"


@pytest.fixture(scope="function", autouse=True)
def _isolated_app(monkeypatch):
    """每测试独立内存库 + 默认 scripted 规划器（确定性）。"""
    monkeypatch.setenv("GOAL_LOOP_PLANNER", "scripted")
    monkeypatch.setenv("GOAL_LOOP_SCRIPTED_ROUNDS", "3")
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
def client(_isolated_app):
    return _isolated_app.test_client()


@pytest.fixture
def env(_isolated_app):
    """user/org/agent/project + owner JWT。"""
    from flask_jwt_extended import create_access_token
    from models import Agent, Organization, Project, User

    user = User(username=f"u_{uuid.uuid4().hex[:8]}", email=f"u_{uuid.uuid4().hex[:6]}@t.io")
    db.session.add(user)
    db.session.flush()
    org = Organization(name=f"o_{uuid.uuid4().hex[:8]}", slug=f"o_{uuid.uuid4().hex[:8]}", owner_id=user.id)
    db.session.add(org)
    db.session.flush()
    agent = Agent(
        name=f"agent_{uuid.uuid4().hex[:6]}",
        workspace_id=org.id,
        creator_user_id=user.id,
        status="ACTIVE",
        runner_enabled=True,
    )
    db.session.add(agent)
    db.session.flush()
    project = Project(name=f"p_{uuid.uuid4().hex[:6]}", owner_id=user.id, organization_id=org.id)
    db.session.add(project)
    db.session.commit()

    token = create_access_token(identity=str(user.id))
    return {
        "user": user, "org": org, "agent": agent, "project": project,
        "headers": {"Authorization": f"Bearer {token}"},
    }


def _finish_task(task):
    """把任务置 DONE 并触发循环钩子（模拟 agent 提交终态）。"""
    from models import TaskStatus
    from services.goal_loop_service import notify_task_finished
    task.status = TaskStatus.DONE
    db.session.commit()
    notify_task_finished(task.id)


class TestGoalLoopLifecycle:
    def test_create_loop_starts_round1_and_auto_assigns(self, client, env):
        resp = client.post(
            f"{BASE_URL}/{env['project'].id}/goal-loops",
            json={"title": "冲榜", "goal_text": "把登录页可用性做到 95 分",
                  "done_definition": "95 分达成", "rounds_limit": 5},
            headers=env["headers"],
        )
        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()["data"]
        assert data["status"] == "running"
        assert data["rounds_done"] == 1
        task = data["tasks"][0]
        # auto_assign_task 会把 TODO 任务立即置为 IN_PROGRESS（租约即开工）
        assert task["status"] == "in_progress"
        # 自动派发：存在活跃 attempt+lease
        from models import AgentTaskAttempt, AgentTaskLease
        assert AgentTaskAttempt.query.filter_by(task_id=task["id"]).first()
        assert AgentTaskLease.query.filter_by(task_id=task["id"]).first()

    def test_terminal_task_triggers_next_round(self, client, env):
        resp = client.post(
            f"{BASE_URL}/{env['project'].id}/goal-loops",
            json={"title": "循环", "goal_text": "目标X"},
            headers=env["headers"],
        )
        loop = resp.get_json()["data"]
        first = loop["tasks"][0]

        from models import Task
        _finish_task(db.session.get(Task, first["id"]))

        detail = client.get(
            f"{BASE_URL}/goal-loops/{loop['id']}", headers=env["headers"]
        ).get_json()["data"]
        assert detail["rounds_done"] == 2
        assert detail["tasks"][-1]["id"] != first["id"]

    def test_scripted_complete_marks_done(self, client, env):
        resp = client.post(
            f"{BASE_URL}/{env['project'].id}/goal-loops",
            json={"title": "三轮达成", "goal_text": "目标Y"},
            headers=env["headers"],
        )
        loop = resp.get_json()["data"]
        for _ in range(3):
            from models import Task
            t = loop["tasks"][-1]
            _finish_task(db.session.get(Task, t["id"]))
            loop = client.get(
                f"{BASE_URL}/goal-loops/{loop['id']}", headers=env["headers"]
            ).get_json()["data"]

        assert loop["status"] == "done"
        assert "目标达成" in (loop["completion_summary"] or "")

    def test_rounds_limit_guard(self, client, env, monkeypatch):
        monkeypatch.setenv("GOAL_LOOP_SCRIPTED_ROUNDS", "99")
        resp = client.post(
            f"{BASE_URL}/{env['project'].id}/goal-loops",
            json={"title": "受限", "goal_text": "目标Z", "rounds_limit": 2},
            headers=env["headers"],
        )
        loop = resp.get_json()["data"]
        from models import Task
        for _ in range(2):
            _finish_task(db.session.get(Task, loop["tasks"][-1]["id"]))
            loop = client.get(
                f"{BASE_URL}/goal-loops/{loop['id']}", headers=env["headers"]
            ).get_json()["data"]

        assert loop["status"] == "limit_reached"
        # 终态后再 finish 不再推进
        before = loop["rounds_done"]
        _finish_task(db.session.get(Task, loop["tasks"][-1]["id"]))
        loop = client.get(
            f"{BASE_URL}/goal-loops/{loop['id']}", headers=env["headers"]
        ).get_json()["data"]
        assert loop["rounds_done"] == before

    def test_stall_after_repeated_blocked(self, client, env, monkeypatch):
        from services import goal_loop_service as svc
        monkeypatch.setenv("GOAL_LOOP_PLANNER", "llm")

        def always_blocked(loop, history):
            return {"action": "blocked", "reason": "信息不足"}
        monkeypatch.setattr(svc, "_call_planner", always_blocked)

        resp = client.post(
            f"{BASE_URL}/{env['project'].id}/goal-loops",
            json={"title": "受阻", "goal_text": "目标W"},
            headers=env["headers"],
        )
        loop = resp.get_json()["data"]
        # 创建时第 1 次 blocked（stall=1），再推进 1 次到 stall_limit=2
        svc.maybe_advance(loop["id"])
        loop = client.get(
            f"{BASE_URL}/goal-loops/{loop['id']}", headers=env["headers"]
        ).get_json()["data"]
        assert loop["status"] == "stalled"
        assert "信息不足" in (loop["last_error"] or "")

        # 人工恢复后继续（重置计数）
        client.post(f"{BASE_URL}/goal-loops/{loop['id']}/resume", headers=env["headers"])
        loop = client.get(
            f"{BASE_URL}/goal-loops/{loop['id']}", headers=env["headers"]
        ).get_json()["data"]
        assert loop["status"] == "running"
        # resume 会 kick 一次，规划器仍 blocked → 重新计 1（人工干预已重置过计数）
        assert loop["stall_count"] == 1

    def test_paused_loop_does_not_advance(self, client, env):
        resp = client.post(
            f"{BASE_URL}/{env['project'].id}/goal-loops",
            json={"title": "暂停", "goal_text": "目标P"},
            headers=env["headers"],
        )
        loop = resp.get_json()["data"]
        client.post(f"{BASE_URL}/goal-loops/{loop['id']}/pause", headers=env["headers"])

        from models import Task
        from services.goal_loop_service import notify_task_finished
        t = db.session.get(Task, loop["tasks"][0]["id"])
        t.status = db.session.query(Task).get(t.id).status  # noop keep type
        from models import TaskStatus
        t.status = TaskStatus.DONE
        db.session.commit()
        notify_task_finished(t.id)

        detail = client.get(
            f"{BASE_URL}/goal-loops/{loop['id']}", headers=env["headers"]
        ).get_json()["data"]
        assert detail["status"] == "paused"
        assert detail["rounds_done"] == 1

        # 恢复 + kick 立即续跑
        client.post(f"{BASE_URL}/goal-loops/{loop['id']}/resume", headers=env["headers"])
        detail = client.get(
            f"{BASE_URL}/goal-loops/{loop['id']}", headers=env["headers"]
        ).get_json()["data"]
        assert detail["rounds_done"] == 2

    def test_permission_non_manager_forbidden(self, client, env):
        from flask_jwt_extended import create_access_token
        from models import User

        outsider = User(username=f"u_{uuid.uuid4().hex[:8]}", email=f"u_{uuid.uuid4().hex[:6]}@t.io")
        db.session.add(outsider)
        db.session.commit()
        headers = {"Authorization": f"Bearer {create_access_token(identity=str(outsider.id))}"}

        resp = client.post(
            f"{BASE_URL}/{env['project'].id}/goal-loops",
            json={"title": "x", "goal_text": "y"},
            headers=headers,
        )
        assert resp.status_code == 403

    def test_agent_workspace_mismatch_rejected(self, client, env):
        from models import Agent
        stranger = Agent(
            name=f"agent_{uuid.uuid4().hex[:6]}",
            workspace_id=env["org"].id + 10000,
            creator_user_id=env["user"].id,
            status="ACTIVE",
            runner_enabled=True,
        )
        db.session.add(stranger)
        db.session.commit()

        resp = client.post(
            f"{BASE_URL}/{env['project'].id}/goal-loops",
            json={"title": "x", "goal_text": "y", "agent_id": stranger.id},
            headers=env["headers"],
        )
        assert resp.status_code == 400
        assert resp.get_json()["error_details"]["code"] == "AGENT_WORKSPACE_MISMATCH"

    def test_stop_is_terminal(self, client, env):
        resp = client.post(
            f"{BASE_URL}/{env['project'].id}/goal-loops",
            json={"title": "停", "goal_text": "目标S"},
            headers=env["headers"],
        )
        loop = resp.get_json()["data"]
        client.post(f"{BASE_URL}/goal-loops/{loop['id']}/stop", headers=env["headers"])

        from models import Task
        from services.goal_loop_service import notify_task_finished
        _finish_task(db.session.get(Task, loop["tasks"][0]["id"]))
        detail = client.get(
            f"{BASE_URL}/goal-loops/{loop['id']}", headers=env["headers"]
        ).get_json()["data"]
        assert detail["status"] == "stopped"
        assert detail["rounds_done"] == 1


class TestLLMPlannerParsing:
    def test_llm_planner_parses_markdown_wrapped_json(self, _isolated_app, env, monkeypatch):
        from services import goal_loop_service as svc

        monkeypatch.setenv("GOAL_LOOP_PLANNER", "llm")

        def fake_llm(feature, messages, **kwargs):
            payload = {
                "action": "continue",
                "title": "下一轮：修复登录",
                "content": "实现并自测登录页",
                "reason": "尚未达成",
            }
            return {
                "success": True,
                "data": f"```json\n{json.dumps(payload, ensure_ascii=False)}\n```",
            }

        monkeypatch.setattr(
            "services.ai_service.call_llm_production", fake_llm
        )
        loop = svc.create_loop(
            project=env["project"], agent=env["agent"],
            title="LLM 循环", goal_text="目标L", created_by=env["user"].id,
        )
        tasks = svc.loop_tasks(loop.id)
        assert len(tasks) == 1
        assert tasks[0].title.startswith("下一轮")

    def test_llm_planner_failure_registers_stall(self, _isolated_app, env, monkeypatch):
        from services import goal_loop_service as svc
        monkeypatch.setenv("GOAL_LOOP_PLANNER", "llm")

        def failing_llm(feature, messages, **kwargs):
            return {"success": False, "error": "LLM quota exhausted"}

        monkeypatch.setattr("services.ai_service.call_llm_production", failing_llm)
        result = svc.create_loop(
            project=env["project"], agent=env["agent"],
            title="失败循环", goal_text="目标F", created_by=env["user"].id,
        )
        # stall_limit=2：创建第 1 次 stall 计数 1，不伪装推进
        assert result.stall_count == 1
        assert "LLM quota exhausted" in (result.last_error or "")
        assert result.status.value == "running"
