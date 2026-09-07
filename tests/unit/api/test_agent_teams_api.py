"""Agent 团队管理 API（api/agent_teams.py）单元回归。

覆盖：团队 CRUD（列表过滤/搜索/分页、重名 409、软删除归档）、成员管理
（添加去重/跨工作区 404/角色枚举回退/顺序分配/更新/移除/批量重排序）、
团队项目关联（添加去重/跨工作区 404/移除）。全部走真实鉴权链
（owner + JWT）。
"""

import uuid

import pytest
from flask_jwt_extended import create_access_token

from models import (
    Agent,
    AgentStatus,
    AgentTeam,
    AgentTeamMember,
    AgentTeamMemberRole,
    AgentTeamProject,
    AgentTeamStatus,
    Organization,
    Project,
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


BASE = "/todo-for-ai/api/v1/workspaces/{ws}/agent-teams"


@pytest.fixture
def env(_isolated_app):
    u = User(username=f"tm_{uuid.uuid4().hex[:8]}",
             email=f"tm_{uuid.uuid4().hex[:6]}@t.io")
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
        "base": BASE.format(ws=org.id),
    }


@pytest.fixture
def user(_isolated_app):
    def _make():
        u = User(username=f"tu_{uuid.uuid4().hex[:8]}",
                 email=f"tu_{uuid.uuid4().hex[:6]}@t.io")
        db.session.add(u)
        db.session.commit()
        return u
    return _make


@pytest.fixture
def agent(env):
    def _make(name=None):
        row = Agent(workspace_id=env["org"].id, owner_id=env["user"].id,
                    creator_user_id=env["user"].id,
                    name=name or f"ag_{uuid.uuid4().hex[:6]}",
                    status=AgentStatus.ACTIVE)
        db.session.add(row)
        db.session.commit()
        return row
    return _make


@pytest.fixture
def team(env):
    def _make(name=None, status=AgentTeamStatus.ACTIVE):
        row = AgentTeam(workspace_id=env["org"].id,
                        created_by_user_id=env["user"].id,
                        name=name or f"team_{uuid.uuid4().hex[:6]}",
                        status=status)
        db.session.add(row)
        db.session.commit()
        return row
    return _make


@pytest.fixture
def member(env):
    def _make(team_row, ag, role=AgentTeamMemberRole.MEMBER, order_index=0):
        row = AgentTeamMember(team_id=team_row.id, agent_id=ag.id,
                              added_by_user_id=env["user"].id, role=role,
                              order_index=order_index)
        db.session.add(row)
        db.session.commit()
        return row
    return _make


def _add_member_api(client, env, team_row, agent_id, **fields):
    return client.post(f"{env['base']}/{team_row.id}/members",
                       headers=env["headers"],
                       json={"agent_id": agent_id, **fields})


class TestTeamCRUD:
    def test_list_empty(self, client, env):
        resp = client.get(env["base"], headers=env["headers"])
        assert resp.status_code == 200
        body = resp.get_json()["data"]
        assert body["items"] == [] and body["pagination"]["total"] == 0

    def test_list_filters_archived_by_default(self, client, env, team):
        active = team(name="活跃队")
        team(name="归档队", status=AgentTeamStatus.ARCHIVED)
        resp = client.get(env["base"], headers=env["headers"])
        names = [i["name"] for i in resp.get_json()["data"]["items"]]
        assert names == [active.name]

    def test_list_status_and_search(self, client, env, team):
        team(name="dev 队")
        archived = team(name="旧 dev 队", status=AgentTeamStatus.ARCHIVED)
        resp = client.get(f"{env['base']}?status=archived",
                          headers=env["headers"])
        names = [i["name"] for i in resp.get_json()["data"]["items"]]
        assert names == [archived.name]
        resp = client.get(f"{env['base']}?search=dev",
                          headers=env["headers"])
        assert resp.get_json()["data"]["pagination"]["total"] == 1

    def test_create_and_duplicate_409(self, client, env):
        resp = client.post(env["base"], headers=env["headers"],
                           json={"name": "突击队", "description": "d"})
        assert resp.status_code == 201
        resp = client.post(env["base"], headers=env["headers"],
                           json={"name": "突击队"})
        assert resp.status_code == 409

    def test_create_missing_name_400(self, client, env):
        resp = client.post(env["base"], headers=env["headers"],
                           json={"description": "x"})
        assert resp.status_code == 400

    def test_get_team_with_members_toggle(self, client, env, team, agent, member):
        t = team(name="详情队")
        member(t, agent())
        resp = client.get(f"{env['base']}/{t.id}", headers=env["headers"])
        assert resp.status_code == 200
        assert "members" in resp.get_json()["data"]

        resp = client.get(f"{env['base']}/{t.id}?include_members=false",
                          headers=env["headers"])
        assert "members" not in resp.get_json()["data"]

    def test_get_404(self, client, env):
        assert client.get(f"{env['base']}/999999",
                          headers=env["headers"]).status_code == 404

    def test_update_rename_conflict_and_fields(self, client, env, team):
        team(name="占位队")
        t = team(name="改名队")
        resp = client.put(f"{env['base']}/{t.id}", headers=env["headers"],
                          json={"name": "占位队"})
        assert resp.status_code == 409

        resp = client.put(f"{env['base']}/{t.id}", headers=env["headers"],
                          json={"name": "新名", "config": {"k": 1},
                                "non_editable": "x"})
        assert resp.status_code == 200
        assert t.name == "新名" and t.config == {"k": 1}

    def test_update_404_and_non_json(self, client, env, team):
        resp = client.put(f"{env['base']}/999999", headers=env["headers"],
                          json={"name": "x"})
        assert resp.status_code == 404
        existing = team(name="存在队")
        resp = client.put(f"{env['base']}/{existing.id}",
                          headers=env["headers"], data="plain",
                          content_type="text/plain")
        assert resp.status_code == 400

    def test_delete_soft_archives(self, client, env, team):
        t = team(name="将归档")
        resp = client.delete(f"{env['base']}/{t.id}", headers=env["headers"])
        assert resp.status_code == 200
        assert t.status == AgentTeamStatus.ARCHIVED

    def test_delete_404(self, client, env):
        assert client.delete(f"{env['base']}/999999",
                             headers=env["headers"]).status_code == 404


class TestMembers:
    def test_add_member_success_order_and_role(self, client, env, team, agent):
        t = team()
        resp = _add_member_api(client, env, t, agent().id)
        assert resp.status_code == 201
        assert resp.get_json()["data"]["order_index"] == 1
        resp = _add_member_api(client, env, t, agent().id,
                               role="leader", responsibility="评审")
        assert resp.status_code == 201
        assert resp.get_json()["data"]["order_index"] == 2
        assert resp.get_json()["data"]["role"] == "leader"
        assert t.member_count == 2

    def test_add_member_invalid_role_falls_back(self, client, env, team, agent):
        t = team()
        resp = _add_member_api(client, env, t, agent().id, role="wizard")
        assert resp.status_code == 201
        assert resp.get_json()["data"]["role"] == "member"

    def test_add_duplicate_409(self, client, env, team, agent):
        t = team()
        ag = agent()
        assert _add_member_api(client, env, t, ag.id).status_code == 201
        resp = _add_member_api(client, env, t, ag.id)
        assert resp.status_code == 409

    def test_add_agent_from_other_workspace_404(self, client, env, team, agent):
        t = team()
        foreign = agent()
        foreign.workspace_id = env["org"].id + 999
        db.session.commit()
        resp = _add_member_api(client, env, t, foreign.id)
        assert resp.status_code == 404

    def test_add_missing_agent_id_400(self, client, env, team):
        resp = client.post(f"{env['base']}/{team().id}/members",
                           headers=env["headers"], json={})
        assert resp.status_code == 400

    def test_add_to_missing_team_404(self, client, env, agent):
        resp = client.post(f"{env['base']}/999999/members",
                           headers=env["headers"],
                           json={"agent_id": agent().id})
        assert resp.status_code == 404

    def test_list_members(self, client, env, team, agent, member):
        t = team()
        member(t, agent(), order_index=2)
        member(t, agent(), order_index=1)
        resp = client.get(f"{env['base']}/{t.id}/members",
                          headers=env["headers"])
        items = resp.get_json()["data"]["items"]
        assert [i["order_index"] for i in items] == [1, 2]  # 按 order_index 排序

    def test_list_members_missing_team_404(self, client, env):
        assert client.get(f"{env['base']}/999999/members",
                          headers=env["headers"]).status_code == 404

    def test_update_member(self, client, env, team, agent, member):
        t = team()
        m = member(t, agent())
        resp = client.put(f"{env['base']}/{t.id}/members/{m.id}",
                          headers=env["headers"],
                          json={"role": "leader", "responsibility": "把关",
                                "config": {"tone": "strict"},
                                "notifications_enabled": True})
        assert resp.status_code == 200
        assert m.role == AgentTeamMemberRole.LEADER
        assert m.responsibility == "把关"
        assert m.config == {"tone": "strict"}

    def test_update_member_non_json_400(self, client, env, team, agent, member):
        t = team()
        m = member(t, agent())
        resp = client.put(f"{env['base']}/{t.id}/members/{m.id}",
                          headers=env["headers"], data="plain",
                          content_type="text/plain")
        assert resp.status_code == 400

    def test_update_member_invalid_role_kept(self, client, env, team, agent,
                                             member):
        t = team()
        m = member(t, agent(), role=AgentTeamMemberRole.LEADER)
        resp = client.put(f"{env['base']}/{t.id}/members/{m.id}",
                          headers=env["headers"], json={"role": "wizard"})
        assert resp.status_code == 200
        assert m.role == AgentTeamMemberRole.LEADER  # 非法角色被忽略

    def test_update_member_404(self, client, env, team):
        resp = client.put(f"{env['base']}/{team().id}/members/999999",
                          headers=env["headers"], json={"role": "leader"})
        assert resp.status_code == 404

    def test_remove_member_updates_count(self, client, env, team, agent, member):
        t = team()
        m = member(t, agent())
        resp = client.delete(f"{env['base']}/{t.id}/members/{m.id}",
                             headers=env["headers"])
        assert resp.status_code == 200
        assert t.member_count == 0

    def test_remove_member_404s(self, client, env, team, agent, member):
        t = team()
        resp = client.delete(f"{env['base']}/999999/members/1",
                             headers=env["headers"])
        assert resp.status_code == 404  # team 404
        resp = client.delete(f"{env['base']}/{t.id}/members/999999",
                             headers=env["headers"])
        assert resp.status_code == 404  # member 404

    def test_reorder_members(self, client, env, team, agent, member):
        t = team()
        m1 = member(t, agent(), order_index=1)
        m2 = member(t, agent(), order_index=2)
        resp = client.post(f"{env['base']}/{t.id}/members/reorder",
                           headers=env["headers"], json={"orders": [
                               {"member_id": m1.id, "order_index": 5},
                               {"member_id": m2.id, "order_index": 6},
                           ]})
        assert resp.status_code == 200
        db.session.expire_all()
        assert m1.order_index == 5 and m2.order_index == 6

    def test_reorder_missing_orders_400(self, client, env, team):
        resp = client.post(f"{env['base']}/{team().id}/members/reorder",
                           headers=env["headers"], json={})
        assert resp.status_code == 400

    def test_reorder_missing_team_404(self, client, env):
        resp = client.post(f"{env['base']}/999999/members/reorder",
                           headers=env["headers"], json={"orders": []})
        assert resp.status_code == 404


class TestWorkspaceGuards:
    """参数化覆盖每个端点的"工作区不存在 404 / 无权 403"守卫行。"""

    ENDPOINTS = [
        ("get", "", None),
        ("post", "", {"name": "x"}),
        ("get", "/1", None),
        ("put", "/1", {"name": "x"}),
        ("delete", "/1", None),
        ("get", "/1/members", None),
        ("post", "/1/members", {"agent_id": 1}),
        ("put", "/1/members/1", {"role": "leader"}),
        ("delete", "/1/members/1", None),
        ("post", "/1/members/reorder", {"orders": []}),
        ("get", "/1/projects", None),
        ("post", "/1/projects", {"project_id": 1}),
        ("delete", "/1/projects/1", None),
    ]

    def test_unknown_workspace_404_on_every_endpoint(self, client, env, user):
        outsider_token = create_access_token(identity=str(user().id))
        headers = {"Authorization": f"Bearer {outsider_token}"}
        for method, suffix, payload in self.ENDPOINTS:
            resp = client.open(
                f"/todo-for-ai/api/v1/workspaces/999999/agent-teams{suffix}",
                method=method, headers=headers, json=payload)
            assert resp.status_code == 404, (method, suffix)

    def test_outsider_403_on_every_endpoint(self, client, env, user):
        outsider_token = create_access_token(identity=str(user().id))
        headers = {"Authorization": f"Bearer {outsider_token}"}
        for method, suffix, payload in self.ENDPOINTS:
            resp = client.open(
                f"{env['base']}{suffix}", method=method,
                headers=headers, json=payload)
            assert resp.status_code == 403, (method, suffix)

    def test_unknown_status_value_falls_back(self, client, env, team):
        team(name="存在队")
        resp = client.get(f"{env['base']}?status=bogus",
                          headers=env["headers"])
        assert resp.status_code == 200
        assert resp.get_json()["data"]["items"] == []


class TestTeamProjects:
    def test_list_projects_missing_team_404(self, client, env):
        resp = client.get(f"{env['base']}/999999/projects",
                          headers=env["headers"])
        assert resp.status_code == 404

    def test_list_projects_empty(self, client, env, team):
        resp = client.get(f"{env['base']}/{team().id}/projects",
                          headers=env["headers"])
        assert resp.get_json()["data"] == {"items": [], "total": 0}

    def test_add_and_remove_project(self, client, env, team):
        t = team()
        project = Project(name=f"p_{uuid.uuid4().hex[:6]}",
                          owner_id=env["user"].id,
                          organization_id=env["org"].id)
        db.session.add(project)
        db.session.commit()

        resp = client.post(f"{env['base']}/{t.id}/projects",
                           headers=env["headers"],
                           json={"project_id": project.id, "role": "owner"})
        assert resp.status_code == 201
        assert t.task_count == 1

        # 重复关联 409
        resp = client.post(f"{env['base']}/{t.id}/projects",
                           headers=env["headers"],
                           json={"project_id": project.id})
        assert resp.status_code == 409

        # 解除关联
        resp = client.delete(
            f"{env['base']}/{t.id}/projects/{project.id}",
            headers=env["headers"])
        assert resp.status_code == 200
        assert t.task_count == 0

    def test_add_project_from_other_workspace_404(self, client, env, team,
                                                  user):
        other_org = Organization(name=f"o_{uuid.uuid4().hex[:6]}",
                                 slug=f"o_{uuid.uuid4().hex[:6]}",
                                 owner_id=user().id)
        db.session.add(other_org)
        db.session.flush()
        project = Project(name=f"p_{uuid.uuid4().hex[:6]}",
                          owner_id=user().id,
                          organization_id=other_org.id)
        db.session.add(project)
        db.session.commit()
        resp = client.post(f"{env['base']}/{team().id}/projects",
                           headers=env["headers"],
                           json={"project_id": project.id})
        assert resp.status_code == 404

    def test_add_missing_team_404(self, client, env):
        resp = client.post(f"{env['base']}/999999/projects",
                           headers=env["headers"], json={"project_id": 1})
        assert resp.status_code == 404

    def test_add_missing_project_id_400(self, client, env, team):
        resp = client.post(f"{env['base']}/{team().id}/projects",
                           headers=env["headers"], json={})
        assert resp.status_code == 400

    def test_add_to_missing_team_404(self, client, env):
        resp = client.post(f"{env['base']}/999999/projects",
                           headers=env["headers"], json={"project_id": 1})
        assert resp.status_code == 404

    def test_remove_missing_association_404(self, client, env, team):
        resp = client.delete(f"{env['base']}/{team().id}/projects/999999",
                             headers=env["headers"])
        assert resp.status_code == 404

    def test_remove_missing_team_404(self, client, env):
        resp = client.delete(f"{env['base']}/999999/projects/1",
                             headers=env["headers"])
        assert resp.status_code == 404
