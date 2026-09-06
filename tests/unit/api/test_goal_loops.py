"""GoalLoop 目标循环 v2（计划式拆解）回归测试。

覆盖：创建即拆解出计划、逐轮物化步骤、计划耗尽评审宣告完成、轮数上限、
失败评审受阻 stalled、计划扩展（extend）、暂停不推进、权限、agent 工作区
校验、LLM 拆解输出解析、角色上下文注入任务内容。
"""

import json
import uuid

import pytest

from app import create_app
from models import db

BASE_URL = "/todo-for-ai/api/v1/projects"


@pytest.fixture(scope="function", autouse=True)
def _isolated_app(monkeypatch):
    """每测试独立内存库 + 默认 scripted 规划器（确定性，3 步计划）。"""
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
    """user/org/agent(+岗位角色)/project + owner JWT。"""
    from flask_jwt_extended import create_access_token
    from models import Agent, AgentRoleTemplate, Organization, Project, User

    user = User(username=f"u_{uuid.uuid4().hex[:8]}", email=f"u_{uuid.uuid4().hex[:6]}@t.io")
    db.session.add(user)
    db.session.flush()
    org = Organization(name=f"o_{uuid.uuid4().hex[:8]}", slug=f"o_{uuid.uuid4().hex[:8]}", owner_id=user.id)
    db.session.add(org)
    db.session.flush()
    template = AgentRoleTemplate(
        workspace_id=None,
        created_by_user_id=user.id,
        name=f"pm_{uuid.uuid4().hex[:6]}",
        display_name="产品经理",
        category="pm",
        is_builtin=True,
    )
    db.session.add(template)
    db.session.flush()
    agent = Agent(
        name=f"agent_{uuid.uuid4().hex[:6]}",
        workspace_id=org.id,
        owner_id=user.id,
        creator_user_id=user.id,
        status="ACTIVE",
        runner_enabled=True,
        role_template_id=template.id,
    )
    db.session.add(agent)
    db.session.flush()
    project = Project(name=f"p_{uuid.uuid4().hex[:6]}", owner_id=user.id, organization_id=org.id)
    db.session.add(project)
    db.session.commit()

    token = create_access_token(identity=str(user.id))
    return {
        "user": user, "org": org, "agent": agent, "project": project, "template": template,
        "headers": {"Authorization": f"Bearer {token}"},
    }


def _finish_task(task, status_value="done"):
    """任务置终态并触发循环钩子（模拟 agent 提交/人工关闭）。"""
    from models import TaskStatus
    from services.goal_loop_service import notify_task_finished
    task.status = TaskStatus(status_value)
    db.session.commit()
    notify_task_finished(task.id)


class TestPlanBasedLoop:
    def test_create_decomposes_plan_and_materializes_step1(self, client, env):
        resp = client.post(
            f"{BASE_URL}/{env['project'].id}/goal-loops",
            json={"title": "冲榜", "goal_text": "把登录页可用性做到 95 分", "rounds_limit": 5},
            headers=env["headers"],
        )
        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()["data"]
        # 拆解出 3 步计划，第 1 步已物化
        assert len(data["plan"]) == 3
        assert data["plan_index"] == 1
        assert data["plan_revision"] == 1
        assert data["rounds_done"] == 1
        assert "计划步骤 1" in data["tasks"][0]["title"]
        # 角色上下文注入任务内容
        from models import Task
        task = db.session.get(Task, data["tasks"][0]["id"])
        assert "产品经理" in (task.content or "")

    def test_success_path_materializes_steps_then_completes(self, client, env):
        resp = client.post(
            f"{BASE_URL}/{env['project'].id}/goal-loops",
            json={"title": "三轮达成", "goal_text": "目标Y"},
            headers=env["headers"],
        )
        loop = resp.get_json()["data"]
        for _ in range(3):
            from models import Task
            _finish_task(db.session.get(Task, loop["tasks"][-1]["id"]))
            loop = client.get(
                f"{BASE_URL}/goal-loops/{loop['id']}", headers=env["headers"]
            ).get_json()["data"]

        assert loop["status"] == "done"
        assert "目标达成" in (loop["completion_summary"] or "")
        assert loop["rounds_done"] == 3
        # 计划步骤全部物化，无评审扩展
        assert loop["plan_revision"] == 1

    def test_failure_triggers_review_and_stall(self, client, env):
        resp = client.post(
            f"{BASE_URL}/{env['project'].id}/goal-loops",
            json={"title": "受阻", "goal_text": "目标W"},
            headers=env["headers"],
        )
        loop = resp.get_json()["data"]
        # 上轮取消（失败）→ 评审 blocked → stall=1；再失败一次 → stalled
        from models import Task
        _finish_task(db.session.get(Task, loop["tasks"][-1]["id"]), "cancelled")
        _finish_task(db.session.get(Task, loop["tasks"][-1]["id"]), "cancelled")

        loop = client.get(
            f"{BASE_URL}/goal-loops/{loop['id']}", headers=env["headers"]
        ).get_json()["data"]
        assert loop["status"] == "stalled"
        assert "无法推进" in (loop["last_error"] or "")

        # 恢复：继续走完剩余计划（scripted 评审仅在失败/耗尽时触发）
        client.post(f"{BASE_URL}/goal-loops/{loop['id']}/resume", headers=env["headers"])
        loop = client.get(
            f"{BASE_URL}/goal-loops/{loop['id']}", headers=env["headers"]
        ).get_json()["data"]
        assert loop["status"] == "running"
        assert loop["rounds_done"] == 2  # resume kick 物化第 2 步

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
        before = loop["rounds_done"]
        _finish_task(db.session.get(Task, loop["tasks"][-1]["id"]))
        loop = client.get(
            f"{BASE_URL}/goal-loops/{loop['id']}", headers=env["headers"]
        ).get_json()["data"]
        assert loop["rounds_done"] == before

    def test_llm_extend_replans_remaining_plan(self, _isolated_app, env, monkeypatch):
        from services import goal_loop_service as svc
        monkeypatch.setenv("GOAL_LOOP_PLANNER", "llm")

        def fake_llm(loop, system_prompt, user_prompt, **kwargs):
            if "拆解为有序" in system_prompt:
                return {"steps": [
                    {"title": "步骤一", "content": "做第一步"},
                    {"title": "步骤二", "content": "做第二步"},
                ]}
            # 评审：上轮失败 → 扩展新计划
            return {"action": "extend", "steps": [
                {"title": "补救步骤", "content": "换一种方式重做"},
            ], "reason": "原方案受阻"}

        monkeypatch.setattr(svc, "_llm_call", fake_llm)
        loop = svc.create_loop(
            project=env["project"], agent=env["agent"],
            title="LLM 计划", goal_text="目标L", created_by=env["user"].id,
        )
        assert [s["title"] for s in loop.plan] == ["步骤一", "步骤二"]

        from models import Task
        _finish_task(db.session.get(Task, svc.loop_tasks(loop.id)[0].id), "cancelled")
        db.session.expire(loop)
        # 评审 extend：剩余计划被替换，补救步骤已物化
        assert loop.plan_index == 2
        assert loop.plan_revision == 2
        tasks = svc.loop_tasks(loop.id)
        assert tasks[-1].title == "补救步骤"
        assert len(tasks) == 2

    def test_paused_loop_does_not_advance(self, client, env):
        resp = client.post(
            f"{BASE_URL}/{env['project'].id}/goal-loops",
            json={"title": "暂停", "goal_text": "目标P"},
            headers=env["headers"],
        )
        loop = resp.get_json()["data"]
        client.post(f"{BASE_URL}/goal-loops/{loop['id']}/pause", headers=env["headers"])

        from models import Task
        _finish_task(db.session.get(Task, loop["tasks"][0]["id"]))

        detail = client.get(
            f"{BASE_URL}/goal-loops/{loop['id']}", headers=env["headers"]
        ).get_json()["data"]
        assert detail["status"] == "paused"
        assert detail["rounds_done"] == 1

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
        _finish_task(db.session.get(Task, loop["tasks"][0]["id"]))
        detail = client.get(
            f"{BASE_URL}/goal-loops/{loop['id']}", headers=env["headers"]
        ).get_json()["data"]
        assert detail["status"] == "stopped"
        assert detail["rounds_done"] == 1


class TestLLMOutputParsing:
    def test_extract_json_tolerates_markdown_fence(self, _isolated_app):
        from services.goal_loop_service import _extract_json
        raw = "```json\n{\"action\": \"complete\", \"reason\": \"完成\"}\n```"
        assert _extract_json(raw)["action"] == "complete"

    def test_extract_json_tolerates_surrounding_text(self, _isolated_app):
        from services.goal_loop_service import _extract_json
        raw = '好的，规划如下：{"steps": [{"title": "x", "content": "y"}]} 以上。'
        assert _extract_json(raw)["steps"][0]["title"] == "x"


class TestAgentRoleBinding:
    def test_update_agent_role_template(self, client, env):
        from flask_jwt_extended import create_access_token
        from models import User

        headers = {"Authorization": f"Bearer {create_access_token(identity=str(env['user'].id))}"}
        from api.agents import agents_bp  # noqa: F401 确保路由已注册

        resp = client.put(
            f"/todo-for-ai/api/v1/agents/{env['agent'].id}",
            json={"role_template_id": env["template"].id},
            headers=headers,
        )
        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()["data"]
        assert data["role"]["display_name"] == "产品经理"

    def test_update_agent_role_invalid_template_rejected(self, client, env):
        from flask_jwt_extended import create_access_token

        headers = {"Authorization": f"Bearer {create_access_token(identity=str(env['user'].id))}"}
        resp = client.put(
            f"/todo-for-ai/api/v1/agents/{env['agent'].id}",
            json={"role_template_id": 987654},
            headers=headers,
        )
        assert resp.status_code == 400

    def test_update_agent_role_clear(self, client, env):
        from flask_jwt_extended import create_access_token

        headers = {"Authorization": f"Bearer {create_access_token(identity=str(env['user'].id))}"}
        resp = client.put(
            f"/todo-for-ai/api/v1/agents/{env['agent'].id}",
            json={"role_template_id": None},
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.get_json()["data"]["role"] is None


class TestMultiAgentOrchestration:
    """v3 多 Agent 编排：指挥者拆解评审 + 步骤岗位路由执行者。"""

    @pytest.fixture
    def team(self, env):
        """在 env 之上追加第二个 Agent（测试工程师角色）作执行者。"""
        from models import Agent, AgentRoleTemplate

        template = AgentRoleTemplate(
            workspace_id=None,
            created_by_user_id=env["user"].id,
            name=f"qa_{uuid.uuid4().hex[:6]}",
            display_name="测试工程师",
            category="qa",
            is_builtin=True,
        )
        db.session.add(template)
        db.session.flush()
        executor = Agent(
            name=f"agent_{uuid.uuid4().hex[:6]}",
            workspace_id=env["org"].id,
            owner_id=env["user"].id,
            creator_user_id=env["user"].id,
            status="ACTIVE",
            runner_enabled=True,
            role_template_id=template.id,
        )
        db.session.add(executor)
        db.session.commit()
        env["executor"] = executor
        env["executor_template"] = template
        return env

    def test_create_with_director_persists_and_returns_director(self, client, team):
        resp = client.post(
            f"{BASE_URL}/{team['project'].id}/goal-loops",
            json={
                "title": "指挥模式",
                "goal_text": "目标D",
                "agent_id": team["executor"].id,
                "director_agent_id": team["agent"].id,
            },
            headers=team["headers"],
        )
        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()["data"]
        assert data["director_agent_id"] == team["agent"].id
        assert data["director_display_name"] or data["director_name"]
        assert data["agent_id"] == team["executor"].id

    def test_director_not_found_rejected(self, client, team):
        resp = client.post(
            f"{BASE_URL}/{team['project'].id}/goal-loops",
            json={"title": "x", "goal_text": "y", "director_agent_id": 987654321},
            headers=team["headers"],
        )
        assert resp.status_code == 400
        assert resp.get_json()["error_details"]["code"] == "DIRECTOR_NOT_FOUND"

    def test_director_workspace_mismatch_rejected(self, client, team):
        from models import Agent

        stranger = Agent(
            name=f"agent_{uuid.uuid4().hex[:6]}",
            workspace_id=team["org"].id + 5000,
            creator_user_id=team["user"].id,
            status="ACTIVE",
            runner_enabled=True,
        )
        db.session.add(stranger)
        db.session.commit()

        resp = client.post(
            f"{BASE_URL}/{team['project'].id}/goal-loops",
            json={
                "title": "x", "goal_text": "y",
                "director_agent_id": stranger.id,
            },
            headers=team["headers"],
        )
        assert resp.status_code == 400
        assert resp.get_json()["error_details"]["code"] == "DIRECTOR_WORKSPACE_MISMATCH"

    def test_planner_prompt_uses_director_role_and_lists_executors(self, _isolated_app, team, monkeypatch):
        from services import goal_loop_service as svc
        monkeypatch.setenv("GOAL_LOOP_PLANNER", "llm")

        captured = {}

        def fake_llm(loop, system_prompt, user_prompt, **kwargs):
            captured["user"] = user_prompt
            return {"steps": [{"title": "步骤", "content": "做"}]}

        monkeypatch.setattr(svc, "_llm_call", fake_llm)
        svc.create_loop(
            project=team["project"], agent=team["executor"],
            title="指挥上下文", goal_text="目标C",
            created_by=team["user"].id, director=team["agent"],
        )
        # 指挥者的角色上下文进入规划提示词，且列出可用执行者角色
        assert "指挥者角色：产品经理" in captured["user"]
        assert "可用执行者角色" in captured["user"]
        assert "测试工程师" in captured["user"]

    def test_step_role_routes_task_to_matching_executor(self, _isolated_app, team, monkeypatch):
        from services import goal_loop_service as svc
        from models import AgentTaskAttempt, Task
        monkeypatch.setenv("GOAL_LOOP_PLANNER", "llm")

        def fake_llm(loop, system_prompt, user_prompt, **kwargs):
            if "拆解为有序" in system_prompt:
                return {"steps": [
                    {"title": "用例设计", "content": "编写测试用例", "role": "测试工程师"},
                    {"title": "无岗位步骤", "content": "自由执行"},
                ]}
            return {"action": "complete", "reason": "完成"}

        monkeypatch.setattr(svc, "_llm_call", fake_llm)
        loop = svc.create_loop(
            project=team["project"], agent=team["agent"],
            title="岗位路由", goal_text="目标R",
            created_by=team["user"].id,
        )
        # 第 1 步声明 role=测试工程师 → 路由给执行者而非绑定 Agent
        attempt = AgentTaskAttempt.query.filter_by(task_id=svc.loop_tasks(loop.id)[0].id).first()
        assert attempt is not None
        assert attempt.agent_id == team["executor"].id
        assert "【执行角色：测试工程师】" in db.session.get(Task, attempt.task_id).content

        # 第 2 步无 role → 退回绑定 Agent
        _finish_task(db.session.get(Task, svc.loop_tasks(loop.id)[0].id))
        tasks = svc.loop_tasks(loop.id)
        attempt2 = AgentTaskAttempt.query.filter_by(task_id=tasks[-1].id).first()
        assert attempt2 is not None
        assert attempt2.agent_id == team["agent"].id

    def test_step_role_no_match_falls_back_to_bound_agent(self, _isolated_app, team, monkeypatch):
        from services import goal_loop_service as svc
        from models import AgentTaskAttempt, Task
        monkeypatch.setenv("GOAL_LOOP_PLANNER", "llm")

        def fake_llm(loop, system_prompt, user_prompt, **kwargs):
            if "拆解为有序" in system_prompt:
                return {"steps": [{"title": "x", "content": "y", "role": "不存在的岗位"}]}
            return {"action": "complete", "reason": "完成"}

        monkeypatch.setattr(svc, "_llm_call", fake_llm)
        loop = svc.create_loop(
            project=team["project"], agent=team["agent"],
            title="回退", goal_text="目标F",
            created_by=team["user"].id,
        )
        attempt = AgentTaskAttempt.query.filter_by(task_id=svc.loop_tasks(loop.id)[0].id).first()
        assert attempt is not None
        assert attempt.agent_id == team["agent"].id

    def test_loop_tasks_api_returns_executor_per_round(self, _isolated_app, team, monkeypatch):
        from services import goal_loop_service as svc
        from models import Task
        monkeypatch.setenv("GOAL_LOOP_PLANNER", "llm")

        def fake_llm(loop, system_prompt, user_prompt, **kwargs):
            if "拆解为有序" in system_prompt:
                return {"steps": [{"title": "s1", "content": "c1", "role": "测试工程师"}]}
            return {"action": "complete", "reason": "完成"}

        monkeypatch.setattr(svc, "_llm_call", fake_llm)
        loop = svc.create_loop(
            project=team["project"], agent=team["agent"],
            title="API 执行者", goal_text="目标A",
            created_by=team["user"].id,
        )
        _finish_task(db.session.get(Task, svc.loop_tasks(loop.id)[0].id))
        resp = _isolated_app.test_client().get(
            f"{BASE_URL}/goal-loops/{loop.id}",
            headers={"Authorization": team["headers"]["Authorization"]},
        )
        data = resp.get_json()["data"]
        assert data["tasks"][0]["agent_id"] == team["executor"].id
        assert data["director_agent_id"] is None
