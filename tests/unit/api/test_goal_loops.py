"""GoalLoop 目标循环 v2（计划式拆解）回归测试。

覆盖：创建即拆解出计划、逐轮物化步骤、计划耗尽评审宣告完成、轮数上限、
失败评审受阻 stalled、计划扩展（extend）、暂停不推进、权限、agent 工作区
校验、LLM 拆解输出解析、角色上下文注入任务内容。
"""

import json
import types
import uuid

import pytest

from app import create_app
from models import db, GoalLoop, GoalLoopStatus

BASE_URL = "/todo-for-ai/api/v1/projects"

from services.goal_loop import planning as goal_loop_planning


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


@pytest.fixture
def team(env):
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

        monkeypatch.setattr(goal_loop_planning, "llm_call", fake_llm)
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

        monkeypatch.setattr(goal_loop_planning, "llm_call", fake_llm)
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

        monkeypatch.setattr(goal_loop_planning, "llm_call", fake_llm)
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

        monkeypatch.setattr(goal_loop_planning, "llm_call", fake_llm)
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

        monkeypatch.setattr(goal_loop_planning, "llm_call", fake_llm)
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


class TestMultiDayEndurance:
    """多日续航：时长预算护栏 + 看门狗（卡死轮次处置 / 漏触发自愈）。"""

    def test_create_persists_time_budget_and_stall_limit(self, client, env):
        resp = client.post(
            f"{BASE_URL}/{env['project'].id}/goal-loops",
            json={
                "title": "长跑", "goal_text": "连续跑几天攻克目标",
                "rounds_limit": 500, "time_budget_hours": 72, "stall_limit": 10,
            },
            headers=env["headers"],
        )
        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()["data"]
        assert data["time_budget_hours"] == 72
        assert data["rounds_limit"] == 500

    def test_create_rejects_out_of_range_guards(self, client, env):
        resp = client.post(
            f"{BASE_URL}/{env['project'].id}/goal-loops",
            json={"title": "x", "goal_text": "y", "rounds_limit": 5001},
            headers=env["headers"],
        )
        assert resp.status_code == 400
        resp = client.post(
            f"{BASE_URL}/{env['project'].id}/goal-loops",
            json={"title": "x", "goal_text": "y", "time_budget_hours": 1000},
            headers=env["headers"],
        )
        assert resp.status_code == 400

    def _backdate(self, loop, hours):
        from datetime import datetime, timedelta
        loop.started_at = datetime.utcnow() - timedelta(hours=hours)
        db.session.commit()

    def test_time_budget_trips_limit_reached_on_advance(self, client, env):
        from services import goal_loop_service as svc
        loop = svc.create_loop(
            project=env["project"], agent=env["agent"],
            title="超时", goal_text="目标T", created_by=env["user"].id,
            time_budget_hours=48,
        )
        self._backdate(loop, 72)
        result = svc.maybe_advance(loop.id)
        db.session.expire(loop)
        assert result["reason"] == "time_budget_exhausted"
        assert loop.status.value == "limit_reached"
        assert "时长预算" in (loop.last_error or "")

    def test_review_prompt_includes_remaining_budget(self, _isolated_app, env, monkeypatch):
        from services import goal_loop_service as svc
        monkeypatch.setenv("GOAL_LOOP_PLANNER", "llm")
        captured = {}

        def fake_llm(loop, system_prompt, user_prompt, **kwargs):
            captured["user"] = user_prompt
            return {"steps": [{"title": "s", "content": "c"}]}

        monkeypatch.setattr(goal_loop_planning, "llm_call", fake_llm)
        loop = svc.create_loop(
            project=env["project"], agent=env["agent"],
            title="预算上下文", goal_text="目标B", created_by=env["user"].id,
            time_budget_hours=24,
        )
        # 拆解发生在开跑前 → 显示「时长预算（尚未开跑）」变体；开跑后为「剩余时间预算」
        assert "时长预算：24 小时" in captured["user"]

    def test_watchdog_cancels_stuck_round_and_counts_stall(self, _isolated_app, env, monkeypatch):
        from datetime import datetime, timedelta
        from services import goal_loop_service as svc
        from models import Task
        monkeypatch.setenv("GOAL_LOOP_STUCK_TASK_HOURS", "6")
        loop = svc.create_loop(
            project=env["project"], agent=env["agent"],
            title="卡死", goal_text="目标K", created_by=env["user"].id,
        )
        task = db.session.get(Task, svc.loop_tasks(loop.id)[0].id)
        task.updated_at = datetime.utcnow() - timedelta(hours=10)
        db.session.commit()

        result = svc.watchdog_sweep()
        db.session.expire(loop)
        db.session.expire(task)
        assert result["stuck_cancelled"] == 1
        assert task.status.value == "cancelled"
        # scripted 评审对失败轮 blocked → 受阻计数 1（未达默认 stall_limit=2，仍在运行）
        assert loop.stall_count == 1
        assert loop.status.value == "running"

    def test_watchdog_kicks_missed_trigger(self, _isolated_app, env, monkeypatch):
        """模拟服务重启丢触发：轮次任务已终态但循环没推进 → sweep 幂等补推进。"""
        from services import goal_loop_service as svc
        from models import Task, TaskStatus
        monkeypatch.setenv("GOAL_LOOP_STUCK_TASK_HOURS", "6")
        loop = svc.create_loop(
            project=env["project"], agent=env["agent"],
            title="漏触发", goal_text="目标M", created_by=env["user"].id,
        )
        task = db.session.get(Task, svc.loop_tasks(loop.id)[0].id)
        task.status = TaskStatus.DONE
        db.session.commit()
        # 不调用 notify_task_finished，直接 sweep
        result = svc.watchdog_sweep()
        assert result["kicked"] >= 1
        db.session.expire(loop)
        assert len(svc.loop_tasks(loop.id)) == 2

    def test_watchdog_finishes_time_exhausted_loop(self, _isolated_app, env):
        from services import goal_loop_service as svc
        loop = svc.create_loop(
            project=env["project"], agent=env["agent"],
            title="巡检超时", goal_text="目标W", created_by=env["user"].id,
            time_budget_hours=24,
        )
        self._backdate(loop, 30)
        result = svc.watchdog_sweep()
        db.session.expire(loop)
        assert result["time_exhausted"] == 1
        assert loop.status.value == "limit_reached"
        assert "时长预算" in (loop.last_error or "")


class TestGuardrailUpdates:
    """护栏调整：用户设置"跑多久/多少轮才停"，limit_reached 续命后继续跑。"""

    def _put(self, client, env, loop_id, payload):
        return client.put(
            f"{BASE_URL}/goal-loops/{loop_id}",
            json=payload,
            headers=env["headers"],
        )

    def test_update_extends_limit_reached_loop_and_resume_continues(self, client, env, monkeypatch):
        monkeypatch.setenv("GOAL_LOOP_SCRIPTED_ROUNDS", "99")
        resp = client.post(
            f"{BASE_URL}/{env['project'].id}/goal-loops",
            json={"title": "续命", "goal_text": "目标X", "rounds_limit": 2},
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

        # 调大轮数上限 → resume → 继续物化第 3 轮
        resp = self._put(client, env, loop["id"], {"rounds_limit": 5, "time_budget_hours": 48})
        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()["data"]
        assert data["rounds_limit"] == 5
        assert data["time_budget_hours"] == 48

        client.post(f"{BASE_URL}/goal-loops/{loop['id']}/resume", headers=env["headers"])
        data = client.get(
            f"{BASE_URL}/goal-loops/{loop['id']}", headers=env["headers"]
        ).get_json()["data"]
        assert data["status"] == "running"
        assert data["rounds_done"] == 3

    def test_update_partial_and_clear_budget(self, client, env):
        resp = client.post(
            f"{BASE_URL}/{env['project'].id}/goal-loops",
            json={"title": "局部更新", "goal_text": "目标U", "time_budget_hours": 48},
            headers=env["headers"],
        )
        loop_id = resp.get_json()["data"]["id"]
        # 清除时长预算（0 = 不限时）
        data = self._put(client, env, loop_id, {"time_budget_hours": 0}).get_json()["data"]
        assert data["time_budget_hours"] is None
        # 单独调 stall_limit
        data = self._put(client, env, loop_id, {"stall_limit": 8}).get_json()["data"]
        assert data["time_budget_hours"] is None

        from models import GoalLoop
        assert db.session.get(GoalLoop, loop_id).stall_limit == 8

    def test_update_rejects_out_of_range(self, client, env):
        resp = client.post(
            f"{BASE_URL}/{env['project'].id}/goal-loops",
            json={"title": "越界", "goal_text": "y"},
            headers=env["headers"],
        )
        loop_id = resp.get_json()["data"]["id"]
        assert self._put(client, env, loop_id, {"rounds_limit": 5001}).status_code == 400
        assert self._put(client, env, loop_id, {"time_budget_hours": 1000}).status_code == 400
        assert self._put(client, env, loop_id, {}).status_code == 400

    def test_update_rejected_on_terminal_loop(self, client, env):
        resp = client.post(
            f"{BASE_URL}/{env['project'].id}/goal-loops",
            json={"title": "终态", "goal_text": "y"},
            headers=env["headers"],
        )
        loop_id = resp.get_json()["data"]["id"]
        client.post(f"{BASE_URL}/goal-loops/{loop_id}/stop", headers=env["headers"])
        resp = self._put(client, env, loop_id, {"rounds_limit": 10})
        assert resp.status_code == 409
        assert resp.get_json()["error_details"]["code"] == "GOAL_LOOP_TERMINAL"

    def test_update_requires_manager(self, client, env):
        from flask_jwt_extended import create_access_token
        from models import User

        outsider = User(username=f"u_{uuid.uuid4().hex[:8]}", email=f"u_{uuid.uuid4().hex[:6]}@t.io")
        db.session.add(outsider)
        db.session.commit()
        headers = {"Authorization": f"Bearer {create_access_token(identity=str(outsider.id))}"}

        resp = client.post(
            f"{BASE_URL}/{env['project'].id}/goal-loops",
            json={"title": "权限", "goal_text": "y"},
            headers=env["headers"],
        )
        loop_id = resp.get_json()["data"]["id"]
        resp = client.put(
            f"{BASE_URL}/goal-loops/{loop_id}", json={"rounds_limit": 10}, headers=headers
        )
        assert resp.status_code == 403


class TestCloudExecutorLinkage:
    """编排↔云端联动：managed_runner 执行者派发前按需拉起 Pod；失败降级不阻塞。"""

    def _install_fake_controller(self, monkeypatch, raise_on_ensure=False):
        import types
        calls = []

        class _FakeProvider:
            name = "fake"

            def ensure_runtime(self, agent, agent_key, sandbox_profile=None):
                calls.append({"agent_id": agent.id})
                if raise_on_ensure:
                    raise RuntimeError("no cluster")
                return {"status": "created"}

        fake_mod = types.SimpleNamespace(get_runtime_provider=lambda: _FakeProvider())
        monkeypatch.setattr(
            "services.runtime_env.get_runtime_provider",
            fake_mod.get_runtime_provider,
        )
        return calls

    def _make_key(self, monkeypatch, key_value="agk_cloud"):
        import types
        row = types.SimpleNamespace(reveal=lambda: key_value)
        fake_agent_key = types.SimpleNamespace()
        fake_agent_key.query = types.SimpleNamespace(
            filter_by=lambda **kw: types.SimpleNamespace(first=lambda: row)
        )
        import models
        monkeypatch.setattr(models, "AgentKey", fake_agent_key, raising=True)
        return row

    def _llm_with_role_step(self, monkeypatch):
        from services import goal_loop_service as svc
        monkeypatch.setenv("GOAL_LOOP_PLANNER", "llm")

        def fake_llm(loop, system_prompt, user_prompt, **kwargs):
            if "拆解为有序" in system_prompt:
                return {"steps": [{"title": "s1", "content": "c1", "role": "测试工程师"}]}
            return {"action": "complete", "reason": "完成"}

        monkeypatch.setattr(goal_loop_planning, "llm_call", fake_llm)

    def test_managed_runner_executor_gets_pod_ensured(
        self, _isolated_app, team, monkeypatch
    ):
        from services import goal_loop_service as svc
        from models import AgentTaskAttempt, Task

        self._llm_with_role_step(monkeypatch)
        calls = self._install_fake_controller(monkeypatch)
        self._make_key(monkeypatch)
        team["executor"].execution_mode = "managed_runner"
        db.session.commit()

        loop = svc.create_loop(
            project=team["project"], agent=team["agent"],
            title="云端联动", goal_text="目标E",
            created_by=team["user"].id,
        )
        assert calls == [{"agent_id": team["executor"].id}]
        # 任务照常派发给执行者
        attempt = AgentTaskAttempt.query.filter_by(task_id=svc.loop_tasks(loop.id)[0].id).first()
        assert attempt is not None and attempt.agent_id == team["executor"].id

    def test_cloud_ensure_failure_degrades_gracefully(
        self, _isolated_app, team, monkeypatch
    ):
        """拉起 Pod 失败（如无集群）→ 任务仍按路由派发，循环不中断。"""
        from services import goal_loop_service as svc
        from models import AgentTaskAttempt

        self._llm_with_role_step(monkeypatch)
        self._install_fake_controller(monkeypatch, raise_on_ensure=True)
        self._make_key(monkeypatch)
        team["executor"].execution_mode = "managed_runner"
        db.session.commit()

        loop = svc.create_loop(
            project=team["project"], agent=team["agent"],
            title="降级", goal_text="目标G",
            created_by=team["user"].id,
        )
        attempt = AgentTaskAttempt.query.filter_by(task_id=svc.loop_tasks(loop.id)[0].id).first()
        assert attempt is not None and attempt.agent_id == team["executor"].id

    def test_external_pull_mode_skips_cloud_linkage(
        self, _isolated_app, team, monkeypatch
    ):
        from services import goal_loop_service as svc

        self._llm_with_role_step(monkeypatch)
        calls = self._install_fake_controller(monkeypatch)
        self._make_key(monkeypatch)
        # executor 保持 external_pull（默认）
        svc.create_loop(
            project=team["project"], agent=team["agent"],
            title="本地模式", goal_text="目标L",
            created_by=team["user"].id,
        )
        assert calls == []


class TestStateMachineGuards:
    """状态机边界分支：缺循环/并发/停机/受阻等守卫。"""

    def test_maybe_advance_loop_not_found(self, _isolated_app):
        from services import goal_loop_service as svc
        assert svc.maybe_advance(987654321)["reason"] == "loop_not_found"

    def test_maybe_advance_already_advancing(self, _isolated_app, env):
        from services import goal_loop_service as svc
        loop = svc.create_loop(project=env["project"], agent=env["agent"],
                               title="并发", goal_text="g", created_by=env["user"].id)
        GoalLoop.query.filter_by(id=loop.id).update({"advancing": 1})
        db.session.commit()
        result = svc.maybe_advance(loop.id)
        assert result["reason"] == "already_advancing"

    def test_maybe_advance_not_running(self, _isolated_app, env):
        from services import goal_loop_service as svc
        loop = svc.create_loop(project=env["project"], agent=env["agent"],
                               title="停机", goal_text="g", created_by=env["user"].id)
        svc.set_status(loop.id, GoalLoopStatus.PAUSED)
        result = svc.maybe_advance(loop.id)
        assert result["reason"].startswith("not_running:")

    def test_advance_locked_active_task_exists(self, _isolated_app, env):
        from services import goal_loop_service as svc
        loop = svc.create_loop(project=env["project"], agent=env["agent"],
                               title="在途", goal_text="g", created_by=env["user"].id)
        # 第 1 轮 in_progress → 直接调 _advance_locked 应报 active_task_exists
        result = svc._advance_locked(loop.id)
        assert result["reason"] == "active_task_exists"

    def test_maybe_advance_cas_rollback_on_error(self, _isolated_app, env, monkeypatch):
        """推进中途抛错时，finally 分支释放 advancing 并回滚。"""
        from services import goal_loop_service as svc
        loop = svc.create_loop(project=env["project"], agent=env["agent"],
                               title="CAS", goal_text="g", created_by=env["user"].id)

        def boom(loop_id, trigger_task_id=None):
            raise RuntimeError("inner boom")

        monkeypatch.setattr("services.goal_loop.state_machine._advance_locked", boom)
        commits = {"n": 0}
        real_commit = db.session.commit

        def counting_commit():
            commits["n"] += 1
            if commits["n"] >= 2:
                raise RuntimeError("commit boom")
            return real_commit()

        monkeypatch.setattr(db.session, "commit", counting_commit)
        with pytest.raises(RuntimeError, match="inner boom"):
            svc.maybe_advance(loop.id)
        monkeypatch.setattr(db.session, "commit", real_commit)
        GoalLoop.query.filter_by(id=loop.id).update({"advancing": 0})
        db.session.commit()

    def test_decompose_failure_counts_stall(self, _isolated_app, env, monkeypatch):
        from services import goal_loop_service as svc
        from services.goal_loop import planning as planning_mod
        monkeypatch.setenv("GOAL_LOOP_PLANNER", "llm")
        monkeypatch.setattr(planning_mod, "llm_call",
                            lambda loop, s, u: (_ for _ in ()).throw(RuntimeError("llm down")))
        loop = svc.create_loop(project=env["project"], agent=env["agent"],
                               title="拆解失败", goal_text="g", created_by=env["user"].id)
        db.session.expire(loop)
        assert loop.stall_count == 1
        assert "decompose_failed" in (loop.last_error or "")

    def test_not_running_after_planner_decompose(self, _isolated_app, env, monkeypatch):
        from services import goal_loop_service as svc
        from services.goal_loop import planning as planning_mod
        monkeypatch.setenv("GOAL_LOOP_PLANNER", "llm")

        def pause_and_plan(loop):
            GoalLoop.query.filter_by(id=loop.id).update(
                {"status": GoalLoopStatus.PAUSED})
            db.session.commit()
            return [{"title": "s", "content": "c"}]

        monkeypatch.setattr("services.goal_loop.state_machine.call_decompose", pause_and_plan)
        loop = svc.create_loop(project=env["project"], agent=env["agent"],
                               title="拆解后暂停", goal_text="g", created_by=env["user"].id)
        db.session.expire(loop)
        assert loop.status == GoalLoopStatus.PAUSED
        assert loop.plan is None

    def test_review_failure_counts_stall(self, _isolated_app, env, monkeypatch):
        from services import goal_loop_service as svc
        from services.goal_loop import planning as planning_mod
        from models import Task
        monkeypatch.setenv("GOAL_LOOP_PLANNER", "llm")
        monkeypatch.setattr(planning_mod, "llm_call", lambda loop, s, u: {
            "steps": [{"title": "s1", "content": "c1"}]})
        loop = svc.create_loop(project=env["project"], agent=env["agent"],
                               title="评审失败", goal_text="g", created_by=env["user"].id)
        monkeypatch.setattr(planning_mod, "call_review",
                            lambda loop, s: (_ for _ in ()).throw(RuntimeError("review down")))
        _finish_task(db.session.get(Task, svc.loop_tasks(loop.id)[0].id), "cancelled")
        db.session.expire(loop)
        assert loop.stall_count == 1
        assert "review_failed" in (loop.last_error or "")

    def test_not_running_after_planner_review(self, _isolated_app, env, monkeypatch):
        from services import goal_loop_service as svc
        from services.goal_loop import planning as planning_mod
        from models import Task
        monkeypatch.setenv("GOAL_LOOP_PLANNER", "llm")
        monkeypatch.setattr(planning_mod, "llm_call", lambda loop, s, u: {
            "steps": [{"title": "s1", "content": "c1"}]})
        loop = svc.create_loop(project=env["project"], agent=env["agent"],
                               title="评审后暂停", goal_text="g", created_by=env["user"].id)

        def pause_review(loop, last_status):
            GoalLoop.query.filter_by(id=loop.id).update(
                {"status": GoalLoopStatus.PAUSED})
            db.session.commit()
            return {"action": "complete", "reason": "r"}

        monkeypatch.setattr("services.goal_loop.state_machine.call_review", pause_review)
        _finish_task(db.session.get(Task, svc.loop_tasks(loop.id)[0].id))
        db.session.expire(loop)
        assert loop.status == GoalLoopStatus.PAUSED

    def test_extend_without_valid_steps_counts_stall(self, _isolated_app, env, monkeypatch):
        from services import goal_loop_service as svc
        from services.goal_loop import planning as planning_mod
        from models import Task
        monkeypatch.setenv("GOAL_LOOP_PLANNER", "llm")
        monkeypatch.setattr(planning_mod, "llm_call", lambda loop, s, u: (
            {"steps": [{"title": "s1", "content": "c1"}]}
            if "拆解为有序" in s else {"action": "extend"}))
        loop = svc.create_loop(project=env["project"], agent=env["agent"],
                               title="空扩展", goal_text="g", created_by=env["user"].id)
        monkeypatch.setattr("services.goal_loop.state_machine.call_review",
                            lambda loop, s: {"action": "extend"})
        _finish_task(db.session.get(Task, svc.loop_tasks(loop.id)[0].id), "cancelled")
        db.session.expire(loop)
        assert "extend_without_valid_steps" in (loop.last_error or "")

    def test_update_guardrails_unknown_loop(self, _isolated_app):
        from services import goal_loop_service as svc
        with pytest.raises(LookupError):
            svc.update_guardrails(987654321, rounds_limit=5)

    def test_set_status_unknown_loop(self, _isolated_app):
        from services import goal_loop_service as svc
        from models import GoalLoopStatus
        with pytest.raises(LookupError):
            svc.set_status(987654321, GoalLoopStatus.PAUSED)

    def test_notify_ignores_non_int_loop_tag(self, _isolated_app, env):
        from services import goal_loop_service as svc
        from models import Task, TaskStatus
        task = Task(title="t", content="c", project_id=env["project"].id,
                    owner_id=env["user"].id, is_ai_task=True,
                    status=TaskStatus.TODO)
        db.session.add(task)
        db.session.flush()
        task.add_tag("goal-loop:not-a-number")
        db.session.commit()
        svc.notify_task_finished(task.id)  # 不应抛异常

    def test_notify_swallows_advance_error(self, _isolated_app, env, monkeypatch):
        from services import goal_loop_service as svc
        from models import Task, TaskStatus
        task = Task(title="t", content="c", project_id=env["project"].id,
                    owner_id=env["user"].id, is_ai_task=True,
                    status=TaskStatus.TODO)
        db.session.add(task)
        db.session.flush()
        task.add_tag("goal-loop:1")
        db.session.commit()

        def boom(loop_id, trigger_task_id=None):
            raise RuntimeError("advance boom")

        monkeypatch.setattr(svc, "maybe_advance", boom)
        svc.notify_task_finished(task.id)  # 异常被吞掉（rollback 分支）


class TestWatchdogBranches:
    """看门狗巡检分支：竞态暂停 / 无时间戳 / 最近活跃跳过。"""

    def _mk_loop(self, env, title):
        from services import goal_loop_service as svc
        return svc.create_loop(project=env["project"], agent=env["agent"],
                               title=title, goal_text="g", created_by=env["user"].id)

    def test_sweep_skips_loop_paused_in_race(self, _isolated_app, env, monkeypatch):
        from services import goal_loop_service as svc
        from services.goal_loop import watchdog as wd
        loop = self._mk_loop(env, "竞态暂停")
        real_expire = db.session.expire

        def fake_expire(instance, attribute_names=None):
            real_expire(instance, attribute_names)
            if isinstance(instance, GoalLoop):
                instance.status = GoalLoopStatus.PAUSED

        monkeypatch.setattr(wd.db.session, "expire", fake_expire)
        result = svc.watchdog_sweep()
        assert result["checked"] >= 1 and result["stuck_cancelled"] == 0

    def test_sweep_skips_recent_active_task(self, _isolated_app, env):
        from services import goal_loop_service as svc
        loop = self._mk_loop(env, "最近活跃")
        result = svc.watchdog_sweep()
        assert result["stuck_cancelled"] == 0
        db.session.expire(loop)
        assert loop.status == GoalLoopStatus.RUNNING


class TestCoverageGapLines:
    """收尾：覆盖拆分模块剩余的降级/回滚分支。"""

    def test_assign_websocket_push_failure_swallowed(self, _isolated_app, env):
        from services import goal_loop_service as svc
        from models import AgentTaskAttempt, Task, TaskStatus
        task = Task(title="t", content="c", project_id=env["project"].id,
                    owner_id=env["user"].id, is_ai_task=True, status=TaskStatus.TODO)
        db.session.add(task)
        db.session.commit()

        def boom(agent_id, payload):
            raise RuntimeError("ws down")

        monkeypatch = getattr(__import__("pytest"), "MonkeyPatch")()
        monkeypatch.setattr("api.agent_runtime_websocket.push_task_to_agent", boom)
        try:
            svc._assign_task_to_agent(task, env["agent"])
        finally:
            monkeypatch.undo()
        assert task.status == TaskStatus.IN_PROGRESS
        assert AgentTaskAttempt.query.filter_by(task_id=task.id).count() == 1

    def test_advance_locked_not_running_direct(self, _isolated_app, env):
        from services import goal_loop_service as svc
        from models import GoalLoopStatus
        loop = svc.create_loop(project=env["project"], agent=env["agent"],
                               title="直调", goal_text="g", created_by=env["user"].id)
        svc.set_status(loop.id, GoalLoopStatus.PAUSED)
        result = svc._advance_locked(loop.id)
        assert result["reason"] == "not_running"

    def test_ensure_cloud_executor_skips_workspace_mismatch(self, _isolated_app, env):
        from services import goal_loop_service as svc
        from models import Agent
        stranger = Agent(name=f"a_{uuid.uuid4().hex[:6]}", workspace_id=env["org"].id + 777,
                         owner_id=env["user"].id, creator_user_id=env["user"].id,
                         status="ACTIVE", runner_enabled=True,
                         execution_mode="managed_runner")
        db.session.add(stranger)
        db.session.commit()
        loop = svc.create_loop(project=env["project"], agent=env["agent"],
                               title="跨区", goal_text="g", created_by=env["user"].id)
        # 工作区不一致 → 直接 return，不触发任何集群调用
        svc._ensure_cloud_executor(loop, stranger)

    def test_ensure_cloud_executor_without_key_warns(self, _isolated_app, env, monkeypatch):
        import types
        from services import goal_loop_service as svc
        from services.goal_loop import dispatch as goal_loop_dispatch
        import models

        row = types.SimpleNamespace(reveal=lambda: None)  # 解密失败 → 无密钥
        fake_ak = types.SimpleNamespace()
        fake_ak.query = types.SimpleNamespace(
            filter_by=lambda **kw: types.SimpleNamespace(first=lambda: row))
        monkeypatch.setattr(models, "AgentKey", fake_ak, raising=True)

        executor = env["agent"]
        executor.execution_mode = "managed_runner"
        db.session.commit()
        loop = svc.create_loop(project=env["project"], agent=env["agent"],
                               title="无钥", goal_text="g", created_by=env["user"].id)
        svc._ensure_cloud_executor(loop, executor)  # 不应抛异常

    def test_auto_assign_fallback_to_controller(self, _isolated_app, env, monkeypatch):
        from services import goal_loop_service as svc
        from models import Task, TaskStatus
        calls = []
        controller = types.SimpleNamespace()
        controller.auto_assign_task = lambda t: calls.append(t.id)
        import services.agent_runtime_controller as arc
        monkeypatch.setattr(arc, "AgentRuntimeController", controller)

        task = Task(title="t", content="c", project_id=env["project"].id,
                    owner_id=env["user"].id, is_ai_task=True, status=TaskStatus.TODO)
        db.session.add(task)
        db.session.commit()
        svc._auto_assign(task, None)
        assert calls == [task.id]

    def test_auto_assign_fallback_error_rolled_back(self, _isolated_app, env, monkeypatch):
        from services import goal_loop_service as svc
        from models import Task, TaskStatus
        task = Task(title="t", content="c", project_id=env["project"].id,
                    owner_id=env["user"].id, is_ai_task=True, status=TaskStatus.TODO)
        db.session.add(task)
        db.session.commit()

        controller = types.SimpleNamespace()
        def _boom(t):
            raise RuntimeError("controller down")
        controller.auto_assign_task = _boom
        import services.agent_runtime_controller as arc
        monkeypatch.setattr(arc, "AgentRuntimeController", controller)

        svc._auto_assign(task, None)  # 异常被吞掉（rollback 分支）
        db.session.expire(task)
        assert task.status == TaskStatus.TODO

    def test_notify_rollback_on_advance_error(self, _isolated_app, env, monkeypatch):
        from services import goal_loop_service as svc
        from services.goal_loop import state_machine
        from models import Task, TaskStatus
        task = Task(title="t", content="c", project_id=env["project"].id,
                    owner_id=env["user"].id, is_ai_task=True, status=TaskStatus.TODO)
        db.session.add(task)
        db.session.flush()
        task.add_tag("goal-loop:1")
        db.session.commit()

        def boom(loop_id, trigger_task_id=None):
            raise RuntimeError("advance boom")

        monkeypatch.setattr(state_machine, "maybe_advance", boom)
        svc.notify_task_finished(task.id)  # 异常被吞掉（rollback 分支）
