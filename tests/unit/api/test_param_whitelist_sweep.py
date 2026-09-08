"""全局丢参模式排查回归（迭代 26）。

validate_json_request 白名单丢参与 get_request_args 死参数两类模式的
回归钉子，覆盖本轮修复的 6 处白名单缺失与 experiences 三端点死过滤。
"""

import json
import sys
import uuid

import pytest
from flask_jwt_extended import create_access_token

from models import (
    Agent,
    AgentExperience,
    AgentRoleTemplate,
    AgentStatus,
    AgentTeam,
    AgentTeamMember,
    AgentTeamMemberRole,
    AgentTeamStatus,
    Organization,
    Project,
    Task,
    TeamTaskOrchestration,
    User,
    db,
)


@pytest.fixture(scope="function", autouse=True)
def _isolated_app():
    from app import create_app
    app = create_app("testing")
    app.config.update({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SQLALCHEMY_ENGINE_OPTIONS": {},
        # 显式固定 JWT 密钥：本文件曾因 .env 覆盖导致签名校验失败
        "JWT_SECRET_KEY": "sweep-fixed-secret",
        "WTF_CSRF_ENABLED": False,
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
def user(_isolated_app):
    def _make():
        u = User(username=f"uu_{uuid.uuid4().hex[:8]}",
                 email=f"uu_{uuid.uuid4().hex[:6]}@t.io")
        db.session.add(u)
        db.session.commit()
        return u
    return _make


@pytest.fixture
def env(_isolated_app):
    u = User(username=f"sw_{uuid.uuid4().hex[:8]}",
             email=f"sw_{uuid.uuid4().hex[:6]}@t.io")
    db.session.add(u)
    db.session.flush()
    org = Organization(name=f"o_{uuid.uuid4().hex[:6]}",
                       slug=f"o_{uuid.uuid4().hex[:6]}", owner_id=u.id)
    db.session.add(org)
    db.session.commit()
    token = create_access_token(identity=str(u.id))
    return {
        "user": u, "org": org,
        "headers": {"Authorization": f"Bearer {token}"},
    }


class TestRoleTemplateWhitelist:
    def test_create_template_keeps_parent_template_id(self, client, env):
        parent = AgentRoleTemplate(
            workspace_id=None, created_by_user_id=env["user"].id,
            name="parent-tpl", display_name="父模板", category="developer",
            is_builtin=True, status="ACTIVE")
        db.session.add(parent)
        db.session.commit()
        resp = client.post(
            f"/todo-for-ai/api/v1/workspaces/{env['org'].id}/agent-role-templates",
            headers=env["headers"],
            json={"name": "child-tpl", "display_name": "子模板",
                  "parent_template_id": parent.id})
        import sys
        print("DBG create-tpl:", resp.status_code, resp.get_data(as_text=True)[:160], file=sys.stderr)
        assert resp.status_code == 201
        row = AgentRoleTemplate.query.filter_by(name="child-tpl").one()
        assert row.parent_template_id == parent.id

    def test_instantiate_keeps_agent_fields(self, client, env):
        tpl = AgentRoleTemplate(
            workspace_id=None, created_by_user_id=env["user"].id,
            name="inst-tpl", display_name="实例化模板", category="developer",
            is_builtin=True, status="ACTIVE",
            system_prompt="模板提示词", llm_provider="anthropic",
            llm_model="claude-x", temperature=0.3)
        db.session.add(tpl)
        db.session.commit()

        resp = client.post(
            f"/todo-for-ai/api/v1/workspaces/{env['org'].id}"
            f"/agent-role-templates/{tpl.id}/instantiate",
            headers=env["headers"],
            json={"name": "inst-agent", "display_name": "专属实例",
                  "system_prompt": "覆盖提示词", "llm_provider": "openai",
                  "llm_model": "gpt-4o", "temperature": 0.9,
                  "capability_tags": ["code"]})
        assert resp.status_code == 201
        agent = Agent.query.filter_by(name="inst-agent").one()
        assert agent.system_prompt == "覆盖提示词"
        assert agent.llm_provider == "openai"
        assert agent.llm_model == "gpt-4o"
        assert float(agent.temperature) == 0.9
        assert agent.capability_tags == ["code"]
        assert agent.display_name == "专属实例"


class TestOrchestrationWhitelist:
    def test_start_keeps_participants_config_and_subtasks(self, client, env):
        u = env["user"]
        team = AgentTeam(workspace_id=env["org"].id,
                         created_by_user_id=u.id, name="编排队",
                         status=AgentTeamStatus.ACTIVE)
        db.session.add(team)
        db.session.flush()
        a1 = Agent(workspace_id=env["org"].id, owner_id=u.id,
                   creator_user_id=u.id, name="o1",
                   status=AgentStatus.ACTIVE)
        a2 = Agent(workspace_id=env["org"].id, owner_id=u.id,
                   creator_user_id=u.id, name="o2",
                   status=AgentStatus.ACTIVE)
        db.session.add_all([a1, a2])
        db.session.flush()
        db.session.add(AgentTeamMember(team_id=team.id, agent_id=a1.id,
                                       added_by_user_id=u.id,
                                       role=AgentTeamMemberRole.MEMBER))
        db.session.add(AgentTeamMember(team_id=team.id, agent_id=a2.id,
                                       added_by_user_id=u.id,
                                       role=AgentTeamMemberRole.MEMBER))
        project = Project(name=f"p_{uuid.uuid4().hex[:6]}",
                          owner_id=u.id, organization_id=env["org"].id)
        db.session.add(project)
        db.session.flush()
        task = Task(project_id=project.id, owner_id=u.id,
                    title="编排任务", content="做完它", status="TODO")
        db.session.add(task)
        db.session.commit()

        resp = client.post(
            f"/todo-for-ai/api/v1/workspaces/{env['org'].id}"
            f"/tasks/{task.id}/orchestrate",
            headers=env["headers"],
            json={
                "team_id": team.id,
                "strategy": "parallel",
                "participating_agent_ids": [a1.id, a2.id],
                "config": {"max_rounds": 3},
                "output_aggregator": a1.id,
                "subtasks": [
                    {"assigned_agent_id": a1.id, "title": "子任务一",
                     "stage_index": 0, "order_index": 0},
                    {"assigned_agent_id": a2.id, "title": "子任务二",
                     "stage_index": 1, "order_index": 1},
                ],
            })
        import sys
        print("DBG orch:", resp.status_code, resp.get_data(as_text=True)[:200], file=sys.stderr)
        assert resp.status_code == 201
        orch = TeamTaskOrchestration.query.filter_by(task_id=task.id).one()
        assert set(orch.participating_agent_ids) == {a1.id, a2.id}
        assert orch.output_aggregator_agent_id == a1.id
        assert orch.config == {"max_rounds": 3}


class TestTeamProjectWhitelist:
    def test_add_project_keeps_role_and_config(self, client, env):
        project = Project(name=f"p_{uuid.uuid4().hex[:6]}",
                          owner_id=env["user"].id,
                          organization_id=env["org"].id)
        db.session.add(project)
        db.session.commit()
        t = AgentTeam(workspace_id=env["org"].id,
                      created_by_user_id=env["user"].id, name="关联队",
                      status=AgentTeamStatus.ACTIVE)
        db.session.add(t)
        db.session.commit()
        resp = client.post(
            f"/todo-for-ai/api/v1/workspaces/{env['org'].id}/agent-teams/{t.id}/projects",
            headers=env["headers"],
            json={"project_id": project.id,
                  "role": "primary", "config": {"k": "v"}})
        assert resp.status_code == 201
        assoc = resp.get_json()["data"]
        assert assoc["role"] == "primary"
        assert assoc["config"] == {"k": "v"}


class TestAiAssistantWhitelist:
    def test_task_assistant_passes_project_context_and_use_cache(
            self, client, env, monkeypatch):
        captured = {}

        def fake_llm(**kw):
            captured.update(messages=kw["messages"], use_cache=kw["use_cache"])
            return {"success": True,
                    "data": json.dumps({"title": "登录页", "description": "d",
                                        "subtasks": []}, ensure_ascii=False),
                    "usage": {}}
        monkeypatch.setattr("api.ai_task_assistant.call_llm_production",
                            fake_llm)

        resp = client.post("/todo-for-ai/api/v1/ai/task-assistant",
                           headers=env["headers"],
                           json={"description": "做个登录页",
                                 "project_context": "Flask + Vue 项目",
                                 "use_cache": False})
        import sys
        print("DBG assistant:", resp.status_code, resp.get_data(as_text=True)[:200], file=sys.stderr)
        assert resp.status_code == 200
        user_msg = captured["messages"][-1]["content"]
        assert "Flask + Vue 项目" in user_msg
        assert captured["use_cache"] is False

    def test_enhance_task_passes_description(self, client, env, monkeypatch):
        captured = {}

        def fake_llm(**kw):
            captured["llm_messages"] = kw["messages"]
            return {"success": True,
                    "data": json.dumps({"title": "优化标题",
                                        "description": "优化描述"},
                                       ensure_ascii=False),
                    "usage": {}}
        monkeypatch.setattr("api.ai_task_assistant.call_llm_production",
                            fake_llm)

        resp = client.post("/todo-for-ai/api/v1/ai/task-assistant/enhance",
                           headers=env["headers"],
                           json={"title": "登录页",
                                 "description": "支持扫码登录"})
        assert resp.status_code == 200
        assert captured["llm_messages"][-1]["content"].endswith("支持扫码登录")
        body = resp.get_json()["data"]
        assert body["description"] == "优化描述"


class TestExperienceFilters:
    def _agent_with_experiences(self, env):
        ag = Agent(workspace_id=env["org"].id, owner_id=env["user"].id,
                   creator_user_id=env["user"].id, name="exp-agent",
                   status=AgentStatus.ACTIVE)
        db.session.add(ag)
        db.session.flush()
        for exp_type, domain, task_type in (
                ("success_pattern", "python", "backend"),
                ("failure_pattern", "frontend", "ui"),
                ("success_pattern", "devops", "backend")):
            db.session.add(AgentExperience(
                agent_id=ag.id, experience_type=exp_type, domain=domain,
                task_type=task_type, is_valid=True, confidence=0.9))
        db.session.commit()
        return ag

    def test_type_domain_task_type_filters(self, client, env):
        ag = self._agent_with_experiences(env)
        base = f"/todo-for-ai/api/v1/agents/{ag.id}/experiences"

        resp = client.get(f"{base}?experience_type=success_pattern",
                          headers=env["headers"])
        items = resp.get_json()["items"]
        assert {e["experience_type"] for e in items} == {"success_pattern"}

        resp = client.get(f"{base}?domain=python", headers=env["headers"])
        assert all(e["domain"] == "python"
                   for e in resp.get_json()["items"])

        resp = client.get(f"{base}?task_type=ui", headers=env["headers"])
        assert all(e["task_type"] == "ui" for e in resp.get_json()["items"])

    def test_recommend_experiences_filters(self, client, env):
        ag = Agent(workspace_id=env["org"].id, owner_id=env["user"].id,
                   creator_user_id=env["user"].id, name="rec-agent",
                   status=AgentStatus.ACTIVE)
        db.session.add(ag)
        db.session.commit()
        db.session.add(AgentExperience(
            agent_id=ag.id, experience_type="success_pattern",
            domain="python", task_type="backend", is_valid=True,
            confidence=0.95, capabilities_used=["pytest"]))
        db.session.commit()

        base = f"/todo-for-ai/api/v1/agents/{ag.id}/experiences/recommend"
        for qs in ("domain=python", "task_type=backend",
                   "capabilities=pytest"):
            resp = client.get(f"{base}?{qs}", headers=env["headers"])
            assert resp.status_code == 200

    def test_shared_experiences_domain_filter(self, client, env):
        src_agent = Agent(workspace_id=env["org"].id, owner_id=env["user"].id,
                          creator_user_id=env["user"].id, name="src-agent",
                          status=AgentStatus.ACTIVE)
        view_agent = Agent(workspace_id=env["org"].id,
                           owner_id=env["user"].id,
                           creator_user_id=env["user"].id, name="view-agent",
                           status=AgentStatus.ACTIVE)
        db.session.add_all([src_agent, view_agent])
        db.session.flush()
        for domain in ("python", "frontend"):
            db.session.add(AgentExperience(
                agent_id=src_agent.id, experience_type="success_pattern",
                domain=domain, task_type="backend", is_shared=True,
                is_valid=True, confidence=0.9))
        db.session.commit()

        base = (f"/todo-for-ai/api/v1/agents/{view_agent.id}"
                f"/experiences/shared")
        for qs, want in (("domain=python", {"python"}),
                         ("domain=frontend", {"frontend"}),
                         ("", {"python", "frontend"})):
            resp = client.get(f"{base}?{qs}", headers=env["headers"])
            assert resp.status_code == 200
            entries = resp.get_json()["items"]
            assert {e["domain"] for e in entries} == want
