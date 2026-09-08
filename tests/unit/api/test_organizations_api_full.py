"""组织管理 API（api/organizations 包）单元回归。

覆盖：组织 CRUD（可见域 owner∪member、搜索/状态过滤/排序、五维计数
装配、slug 冲突自增、owner 成员与系统角色种子、归档事件分型）、成员
管理（邀请/重邀请复活/owner 保护/角色解析两种入参/状态机/移除）、
角色管理（系统角色种子与保护、key 去重后缀、删除后主角色重同步）、
组织事件（过滤器矩阵 + 分页）与 events 记录工具（截断/兜底/ip 注入/
非 dict payload）、shared 辅助直测（slug 化、角色解析、绑定回填、
主角色同步、可见域查询、用户缓存失效）。
"""

import uuid
from datetime import datetime

import pytest
from flask import g
from flask_jwt_extended import create_access_token

from models import (
    Agent,
    AgentStatus,
    Organization,
    OrganizationAgentMember,
    OrganizationAgentMemberStatus,
    OrganizationEvent,
    OrganizationMember,
    OrganizationMemberRole,
    OrganizationMemberStatus,
    OrganizationRole,
    OrganizationRoleDefinition,
    OrganizationStatus,
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


def _uh(prefix="ou"):
    import uuid as _uuid
    u = User(username=f"{prefix}_{_uuid.uuid4().hex[:8]}",
             email=f"{prefix}_{_uuid.uuid4().hex[:6]}@t.io")
    db.session.add(u)
    db.session.flush()
    return u


def _org(owner, name=None, **kw):
    row = Organization(name=name or f"o_{uuid.uuid4().hex[:6]}",
                       slug=f"s_{uuid.uuid4().hex[:12]}",
                       owner_id=owner.id, **kw)
    db.session.add(row)
    db.session.flush()
    return row


def _member(org, user, role=OrganizationRole.MEMBER,
            status=OrganizationMemberStatus.ACTIVE, **kw):
    row = OrganizationMember(organization_id=org.id, user_id=user.id,
                             role=role, status=status, **kw)
    db.session.add(row)
    db.session.flush()
    return row


def _headers_for(user):
    return {"Authorization": f"Bearer {create_access_token(identity=str(user.id))}"}


def _seed_system_roles(org):
    from api.organizations.shared import _ensure_system_roles
    _ensure_system_roles(org.id)
    return {r.key: r for r in OrganizationRoleDefinition.query.filter_by(
        organization_id=org.id).all()}


@pytest.fixture
def env(_isolated_app):
    user = _uh("ow")
    org = _org(user)
    project = Project(name=f"p_{uuid.uuid4().hex[:6]}", owner_id=user.id,
                      organization_id=org.id)
    db.session.add(project)
    db.session.commit()
    return {
        "user": user, "org": org, "project": project,
        "headers": _headers_for(user),
        "base": "/todo-for-ai/api/v1",
    }


# ─────────────────────────── shared 辅助 ───────────────────────────


class TestSharedHelpers:
    def test_slugify_role_key(self):
        from api.organizations.shared import _slugify_role_key
        assert _slugify_role_key("Tech Lead!") == "tech_lead"
        assert _slugify_role_key("  ") .startswith("role_")
        assert _slugify_role_key(None).startswith("role_")

    def test_normalize_optional_text(self):
        from api.organizations.shared import _normalize_optional_text
        assert _normalize_optional_text("  x ") == "x"
        assert _normalize_optional_text("") is None
        assert _normalize_optional_text(None) is None

    def test_ensure_system_roles_idempotent_and_reviving(self, env):
        _seed_system_roles(env["org"])
        # 手工破坏一条：改为非系统+停用+无名 → 再种子应修复
        row = OrganizationRoleDefinition.query.filter_by(
            organization_id=env["org"].id, key="admin").first()
        row.is_system = False
        row.is_active = False
        row.name = ""
        db.session.commit()
        _seed_system_roles(env["org"])
        db.session.expire_all()
        row = OrganizationRoleDefinition.query.filter_by(
            organization_id=env["org"].id, key="admin").first()
        assert row.is_system and row.is_active and row.name == "Admin"

    def test_org_roles_map_include_inactive(self, env):
        roles = _seed_system_roles(env["org"])
        roles["member"].is_active = False
        db.session.commit()
        from api.organizations.shared import _get_org_roles_map
        _, by_key_active = _get_org_roles_map(env["org"].id)
        assert "member" not in by_key_active
        _, by_key_all = _get_org_roles_map(env["org"].id, include_inactive=True)
        assert "member" in by_key_all

    def test_member_legacy_role_key(self):
        from api.organizations.shared import _member_legacy_role_key
        assert _member_legacy_role_key(None) == "member"
        assert _member_legacy_role_key(
            OrganizationMember(role=OrganizationRole.ADMIN)) == "admin"

    def test_backfill_member_role_bindings_removed(self):
        # 零调用的 legacy 回填函数已随本轮清理删除，防止无声回归
        from api.organizations import shared
        assert not hasattr(shared, "_backfill_member_role_bindings")

    def test_resolve_role_ids_from_payload(self, env):
        roles = _seed_system_roles(env["org"])
        from api.organizations.shared import _resolve_role_ids_from_payload
        org_id = env["org"].id

        assert _resolve_role_ids_from_payload(org_id, {}, 'member') == \
            [roles["member"].id]
        assert _resolve_role_ids_from_payload(
            org_id, {"role": "admin"}) == [roles["admin"].id]
        with pytest.raises(ValueError):
            _resolve_role_ids_from_payload(org_id, {"role": "nope"})
        # 字符串数字可解析、去重保序
        assert _resolve_role_ids_from_payload(
            org_id, {"role_ids": [str(roles["admin"].id),
                                  roles["admin"].id]}) == [roles["admin"].id]
        assert _resolve_role_ids_from_payload(
            org_id, {"role_ids": [roles["member"].id,
                                  roles["admin"].id]}) == \
            [roles["member"].id, roles["admin"].id]
        with pytest.raises(ValueError):
            _resolve_role_ids_from_payload(org_id, {"role_ids": "x"})
        with pytest.raises(ValueError):
            _resolve_role_ids_from_payload(org_id, {"role_ids": ["bad"]})
        with pytest.raises(ValueError):
            _resolve_role_ids_from_payload(org_id, {"role_ids": [999999]})
        assert _resolve_role_ids_from_payload(org_id, {"role_ids": None}) == []
        # 默认键不存在时回退空列表
        assert _resolve_role_ids_from_payload(org_id, {}, 'ghost') == []

    def test_replace_and_sync_primary_role(self, env):
        roles = _seed_system_roles(env["org"])
        outsider = _uh("mb")
        member = _member(env["org"], outsider)
        from api.organizations.shared import (
            _replace_member_roles,
            _sync_member_primary_role,
        )
        _replace_member_roles(member, [roles["viewer"].id,
                                       roles["admin"].id])
        keys = {b.role.key for b in
                OrganizationMemberRole.query.filter_by(member_id=member.id)}
        assert keys == {"viewer", "admin"}
        db.session.commit()
        db.session.expire_all()
        member = db.session.get(OrganizationMember, member.id)
        assert member.role == OrganizationRole.ADMIN  # 优先级高于 viewer
        _replace_member_roles(member, [])
        db.session.commit()
        db.session.expire_all()
        member = db.session.get(OrganizationMember, member.id)
        assert member.role == OrganizationRole.MEMBER  # 无系统角色回退

    def test_sync_primary_role_without_bindings(self, env):
        outsider = _uh("mb")
        member = _member(env["org"], outsider)
        db.session.commit()
        from api.organizations.shared import _sync_member_primary_role
        # 无任何系统角色绑定时回退 member
        _sync_member_primary_role(member)
        assert member.role == OrganizationRole.MEMBER

    def test_compute_primary_role_from_keys(self):
        from api.organizations.shared import _compute_primary_role_from_keys
        assert _compute_primary_role_from_keys(
            ["viewer", "admin"]) == "admin"
        # 未知键不占优先级，viewer 仍按 ROLE_PRIORITY 胜出
        assert _compute_primary_role_from_keys(
            ["zzz", "viewer"]) == "viewer"
        assert _compute_primary_role_from_keys(
            ["zzz", "yyy"]) == "zzz"
        assert _compute_primary_role_from_keys([]) is None

    def test_get_user_org_roles_map(self, env):
        roles = _seed_system_roles(env["org"])
        outsider = _uh("mb")
        member = _member(env["org"], outsider)
        from models import OrganizationMemberRole
        OrganizationMemberRole.create(
            organization_id=env["org"].id, member_id=member.id,
            role_id=roles["admin"].id)
        db.session.commit()
        from api.organizations.shared import _get_user_org_roles_map
        assert _get_user_org_roles_map([], outsider.id) == {}
        roles_map = _get_user_org_roles_map([env["org"].id], outsider.id)
        assert roles_map[env["org"].id] == ["admin"]
        # 未绑定成员走 legacy 回退
        legacy_user = _uh("lg")
        _member(env["org"], legacy_user, role=OrganizationRole.VIEWER)
        db.session.commit()
        roles_map = _get_user_org_roles_map([env["org"].id], legacy_user.id)
        assert roles_map[env["org"].id] == ["viewer"]

    def test_collect_and_invalidate_org_users(self, env, monkeypatch):
        invalidated = []
        from core import cache_invalidation
        monkeypatch.setattr(
            cache_invalidation, "invalidate_user_caches",
            lambda uid: invalidated.append(uid))
        from api.organizations import shared
        monkeypatch.setattr(
            shared, "invalidate_user_caches",
            lambda uid: invalidated.append(uid))
        outsider = _uh("mb")
        _member(env["org"], outsider)
        removed = _member(env["org"], _uh("rm"),
                          status=OrganizationMemberStatus.REMOVED)
        db.session.commit()
        assert shared._collect_org_user_ids(999999) == set()
        users = shared._collect_org_user_ids(env["org"].id)
        assert users == {env["user"].id, outsider.id}  # REMOVED 不在内
        shared._invalidate_org_users(env["org"].id)
        assert set(invalidated) == {env["user"].id, outsider.id}

    def test_accessible_org_query(self, env):
        other = _uh("ot")
        other_org = _org(other)
        member_user = _uh("mb")
        _member(env["org"], member_user)
        db.session.commit()
        from api.organizations.shared import _accessible_org_query
        owner_ids = {o.id for o in _accessible_org_query(env["user"]).all()}
        assert owner_ids == {env["org"].id}
        member_ids = {o.id for o in _accessible_org_query(member_user).all()}
        assert member_ids == {env["org"].id}
        stranger_ids = {o.id for o in _accessible_org_query(other).all()}
        assert stranger_ids == {other_org.id}


# ─────────────────────────── 组织 CRUD 路由 ───────────────────────────


class TestOrganizationRoutes:
    def test_list_visible_scope_and_counts(self, env, client):
        member_user = _uh("mb")
        _member(env["org"], member_user)
        agent = Agent(workspace_id=env["org"].id, owner_id=env["user"].id,
                      creator_user_id=env["user"].id,
                      name=f"ag_{uuid.uuid4().hex[:6]}",
                      status=AgentStatus.ACTIVE)
        db.session.add(agent)
        db.session.flush()
        db.session.add(OrganizationAgentMember(
            organization_id=env["org"].id, agent_id=agent.id,
            status=OrganizationAgentMemberStatus.ACTIVE))
        db.session.commit()

        resp = client.get(f"{env['base']}/organizations",
                          headers=_headers_for(member_user))
        assert resp.status_code == 200
        items = resp.get_json()["data"]["items"]
        assert len(items) == 1
        item = items[0]
        assert item["id"] == env["org"].id
        # 夹具里 owner 没有成员行（直插 Organization 不触发创建链路），
        # 成员数只含显式创建的 member
        assert item["member_count"] == 1
        assert item["agent_count"] == 1
        assert item["project_count"] == 1
        assert item["active_role_count"] == 0  # 未种子系统角色
        assert item["current_user_roles"] == ["member"]
        assert item["current_user_role"] == "member"

        resp = client.get(f"{env['base']}/organizations",
                          headers=env["headers"])
        item = resp.get_json()["data"]["items"][0]
        assert item["current_user_role"] == "owner"
        assert item["last_activity_at"] is not None

    def test_list_stranger_sees_empty(self, env, client):
        stranger = _uh("st")
        db.session.commit()
        resp = client.get(f"{env['base']}/organizations",
                          headers=_headers_for(stranger))
        assert resp.get_json()["data"]["items"] == []

    def test_list_search_status_sort(self, env, client):
        _org(env["user"], name="findme-alpha")
        archived = _org(env["user"], name="archived-one",
                        status=OrganizationStatus.ARCHIVED)
        db.session.commit()
        base = f"{env['base']}/organizations"
        headers = env["headers"]

        resp = client.get(f"{base}?search=findme", headers=headers)
        names = [i["name"] for i in resp.get_json()["data"]["items"]]
        assert all("findme" in n for n in names) and names

        resp = client.get(f"{base}?status=archived", headers=headers)
        names = [i["name"] for i in resp.get_json()["data"]["items"]]
        assert names == ["archived-one"]

        assert client.get(f"{base}?status=warp",
                          headers=headers).status_code == 400

        resp = client.get(f"{base}?sort_by=name&sort_order=asc",
                          headers=headers)
        names = [i["name"] for i in resp.get_json()["data"]["items"]]
        assert names == sorted(names)
        # 显式 updated_at / created_at 排序分支（缺省时 get_request_args
        # 默认给的是 created_at，updated_at 分支必须显式指定才触发）
        resp = client.get(f"{base}?sort_by=updated_at", headers=headers)
        assert resp.status_code == 200
        resp = client.get(f"{base}?sort_by=created_at&sort_order=asc",
                          headers=headers)
        assert resp.status_code == 200
        assert client.get(f"{base}?page=1&per_page=1",
                          headers=headers
                          ).get_json()["data"]["pagination"]["pages"] >= 1

    def test_create_org_flow(self, env, client):
        resp = client.post(f"{env['base']}/organizations",
                           headers=env["headers"],
                           json={"description": "no name"})
        assert resp.status_code == 400

        resp = client.post(f"{env['base']}/organizations",
                           headers=env["headers"],
                           json={"name": "Fresh Org", "slug": "fresh-org"})
        assert resp.status_code == 201
        payload = resp.get_json()["data"]
        assert payload["slug"] == "fresh-org"
        assert payload["current_user_role"] == "owner"
        org_id = payload["id"]

        # slug 冲突自动加后缀
        resp = client.post(f"{env['base']}/organizations",
                           headers=env["headers"],
                           json={"name": "Fresh Org Two", "slug": "fresh-org"})
        assert resp.status_code == 201
        assert resp.get_json()["data"]["slug"].startswith("fresh-org-")

        # owner 成员 + owner 角色绑定 + 创建事件
        member = OrganizationMember.query.filter_by(
            organization_id=org_id, user_id=env["user"].id).first()
        assert member is not None
        assert member.role == OrganizationRole.OWNER
        assert OrganizationMemberRole.query.filter_by(
            member_id=member.id).count() == 1
        event = OrganizationEvent.query.filter_by(
            organization_id=org_id, event_type="org.created").first()
        assert event is not None

    def test_get_org(self, env, client):
        assert client.get(f"{env['base']}/organizations/999999",
                          headers=env["headers"]).status_code == 404
        stranger = _uh("st")
        db.session.commit()
        assert client.get(
            f"{env['base']}/organizations/{env['org'].id}",
            headers=_headers_for(stranger)).status_code == 403
        resp = client.get(f"{env['base']}/organizations/{env['org'].id}",
                          headers=env["headers"])
        assert resp.status_code == 200
        assert resp.get_json()["data"]["current_user_role"] == "owner"

    def test_update_org(self, env, client):
        base = f"{env['base']}/organizations/{env['org'].id}"
        assert client.put(f"{env['base']}/organizations/999999",
                          headers=env["headers"],
                          json={"name": "x"}).status_code == 404
        member_user = _uh("mb")
        _member(env["org"], member_user)
        db.session.commit()
        assert client.put(base, headers=_headers_for(member_user),
                          json={"name": "x"}).status_code == 403

        # slug 冲突
        other = _org(env["user"])
        db.session.commit()
        assert client.put(base, headers=env["headers"],
                          json={"slug": other.slug}).status_code == 409
        # 非法状态
        assert client.put(base, headers=env["headers"],
                          json={"status": "warp"}).status_code == 400

        resp = client.put(base, headers=env["headers"],
                          json={"name": "Renamed Org",
                                "description": "new desc",
                                "slug": f"renamed-{uuid.uuid4().hex[:6]}"})
        assert resp.status_code == 200
        assert resp.get_json()["data"]["name"] == "Renamed Org"
        assert OrganizationEvent.query.filter_by(
            organization_id=env["org"].id,
            event_type="org.updated").count() == 1

        resp = client.put(base, headers=env["headers"],
                          json={"status": "archived"})
        assert resp.status_code == 200
        assert OrganizationEvent.query.filter_by(
            organization_id=env["org"].id,
            event_type="org.archived").count() == 1


# ─────────────────────────── 成员管理路由 ───────────────────────────


class TestMemberRoutes:
    def test_list_members(self, env, client):
        assert client.get(
            f"{env['base']}/organizations/999999/members",
            headers=env["headers"]).status_code == 404
        stranger = _uh("st")  # 非成员：无任何访问权
        member = _member(env["org"], _uh("mb"))
        db.session.commit()
        assert client.get(
            f"{env['base']}/organizations/{env['org'].id}/members",
            headers=_headers_for(stranger)).status_code == 403
        resp = client.get(
            f"{env['base']}/organizations/{env['org'].id}/members",
            headers=env["headers"])
        items = resp.get_json()["data"]["items"]
        assert len(items) == 1
        assert all("user" in i for i in items)
        assert all(i["status"] != "removed" for i in items)

    def test_invite_flow(self, env, client):
        base = f"{env['base']}/organizations/{env['org'].id}/members/invite"
        _seed_system_roles(env["org"])
        invitee = _uh("in")
        stranger = _uh("st")
        member_user = _uh("mb")
        _member(env["org"], member_user)
        db.session.commit()

        assert client.post(f"{env['base']}/organizations/999999/members/invite",
                           headers=env["headers"],
                           json={"email": invitee.email}).status_code == 404
        assert client.post(base, headers=_headers_for(member_user),
                           json={"email": invitee.email}).status_code == 403
        assert client.post(base, headers=env["headers"],
                           json={}).status_code == 400
        assert client.post(base, headers=env["headers"],
                           json={"email": "ghost@t.io"}).status_code == 404
        # owner 邮箱 → 409
        assert client.post(base, headers=env["headers"],
                           json={"email": env["user"].email}
                           ).status_code == 409
        # 非法角色
        assert client.post(base, headers=env["headers"],
                           json={"email": invitee.email, "role": "warp"}
                           ).status_code == 400
        # owner 角色禁止通过邀请授予
        roles = OrganizationRoleDefinition.query.filter_by(
            organization_id=env["org"].id, key="owner").first()
        assert client.post(base, headers=env["headers"],
                           json={"email": invitee.email,
                                 "role_ids": [roles.id]}
                           ).status_code == 400

        resp = client.post(base, headers=env["headers"],
                           json={"email": invitee.email, "role": "admin"})
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["user"]["id"] == invitee.id
        assert {r["key"] for r in data["roles"]} == {"admin"}
        assert OrganizationEvent.query.filter_by(
            organization_id=env["org"].id,
            event_type="member.invited").count() == 1

        # 再邀请一次（已存在）→ 幂等复活
        resp = client.post(base, headers=env["headers"],
                           json={"email": invitee.email})
        assert resp.status_code == 200

    def test_invite_revives_removed_member(self, env, client):
        base = f"{env['base']}/organizations/{env['org'].id}/members/invite"
        _seed_system_roles(env["org"])
        invitee = _uh("in")
        removed = _member(env["org"], invitee,
                          status=OrganizationMemberStatus.REMOVED)
        db.session.commit()
        resp = client.post(base, headers=env["headers"],
                           json={"email": invitee.email})
        assert resp.status_code == 200
        db.session.expire_all()
        member = db.session.get(OrganizationMember, removed.id)
        assert member.status == OrganizationMemberStatus.ACTIVE

    def test_update_member(self, env, client):
        org_base = f"{env['base']}/organizations/{env['org'].id}"
        member_base = f"{org_base}/members"
        _seed_system_roles(env["org"])
        target = _uh("tg")
        _member(env["org"], target, role=OrganizationRole.VIEWER)
        member_user = _uh("mb")
        _member(env["org"], member_user)
        db.session.commit()

        assert client.put(f"{org_base}/members/{target.id}",
                          headers=_headers_for(member_user),
                          json={"role": "admin"}).status_code == 403
        assert client.put(f"{org_base}/members/{env['user'].id}",
                          headers=env["headers"],
                          json={"role": "admin"}).status_code == 400
        assert client.put(f"{org_base}/members/999999",
                          headers=env["headers"],
                          json={"role": "admin"}).status_code == 404

        roles = OrganizationRoleDefinition.query.filter_by(
            organization_id=env["org"].id, key="owner").first()
        assert client.put(f"{member_base}/{target.id}",
                          headers=env["headers"],
                          json={"role_ids": [roles.id]}
                          ).status_code == 400
        assert client.put(f"{member_base}/{target.id}",
                          headers=env["headers"],
                          json={"role": "warp"}).status_code == 400
        assert client.put(f"{member_base}/{target.id}",
                          headers=env["headers"],
                          json={"status": "warp"}).status_code == 400

        resp = client.put(f"{member_base}/{target.id}",
                          headers=env["headers"],
                          json={"role": "admin", "status": "active"})
        assert resp.status_code == 200
        assert resp.get_json()["data"]["role"] == "admin"
        event = OrganizationEvent.query.filter_by(
            organization_id=env["org"].id,
            event_type="member.updated").first()
        assert event.payload["role_ids"] and event.payload["status"] == "active"

    def test_remove_member(self, env, client):
        org_base = f"{env['base']}/organizations/{env['org'].id}"
        target = _uh("tg")
        _member(env["org"], target)
        member_user = _uh("mb")
        _member(env["org"], member_user)
        db.session.commit()

        assert client.delete(
            f"{org_base}/members/{target.id}",
            headers=_headers_for(member_user)).status_code == 403
        assert client.delete(f"{org_base}/members/{env['user'].id}",
                             headers=env["headers"]).status_code == 400
        assert client.delete(f"{org_base}/members/999999",
                             headers=env["headers"]).status_code == 404

        resp = client.delete(f"{org_base}/members/{target.id}",
                             headers=env["headers"])
        assert resp.status_code == 200
        assert OrganizationMember.query.filter_by(
            organization_id=env["org"].id, user_id=target.id).first() is None
        assert OrganizationEvent.query.filter_by(
            organization_id=env["org"].id,
            event_type="member.removed").count() == 1


# ─────────────────────────── 角色管理路由 ───────────────────────────


class TestRoleRoutes:
    def test_list_roles(self, env, client):
        base = f"{env['base']}/organizations/{env['org'].id}/roles"
        assert client.get(f"{env['base']}/organizations/999999/roles",
                          headers=env["headers"]).status_code == 404
        stranger = _uh("st")
        db.session.commit()
        assert client.get(base, headers=_headers_for(stranger)
                          ).status_code == 403
        resp = client.get(base, headers=env["headers"])
        keys = [r["key"] for r in resp.get_json()["data"]["items"]]
        assert set(keys) == {"owner", "admin", "member", "viewer"}
        assert all(r["is_system"] for r in resp.get_json()["data"]["items"])

    def test_create_role(self, env, client):
        base = f"{env['base']}/organizations/{env['org'].id}/roles"
        member_user = _uh("mb")
        _member(env["org"], member_user)
        db.session.commit()

        assert client.post(f"{env['base']}/organizations/999999/roles",
                           headers=env["headers"],
                           json={"name": "x"}).status_code == 404
        assert client.post(base, headers=_headers_for(member_user),
                           json={"name": "x"}).status_code == 403
        assert client.post(base, headers=env["headers"],
                           json={"name": "   "}).status_code == 400

        resp = client.post(base, headers=env["headers"],
                           json={"title": "Tech Lead",
                                 "description": "leads",
                                 "content": "duties"})
        assert resp.status_code == 201
        first = resp.get_json()["data"]
        assert first["key"] == "tech_lead"
        assert first["title"] == "Tech Lead"

        # 重名 key 自动加后缀
        resp = client.post(base, headers=env["headers"],
                           json={"name": "Tech Lead"})
        second = resp.get_json()["data"]
        assert second["key"].startswith("tech_lead_")

        # 显式 key + name 兜底 title
        resp = client.post(base, headers=env["headers"],
                           json={"name": "QA", "key": "qa_engineer",
                                 "description": "   "})
        third = resp.get_json()["data"]
        assert third["key"] == "qa_engineer"
        assert third["description"] is None

    def test_update_role(self, env, client):
        org = env["org"]
        base = f"{env['base']}/organizations/{org.id}/roles"
        roles = _seed_system_roles(org)
        custom = OrganizationRoleDefinition.create(
            organization_id=org.id, key="tmp_role", name="Tmp",
            is_system=False, is_active=True,
            created_by=env["user"].email)
        db.session.commit()

        stranger_org = _org(_uh("st2"))
        db.session.commit()
        assert client.put(f"{env['base']}/organizations/999999/roles/1",
                          headers=env["headers"],
                          json={"name": "x"}).status_code == 404
        assert client.put(f"{env['base']}/organizations/{stranger_org.id}"
                          f"/roles/1", headers=env["headers"],
                          json={"name": "x"}).status_code == 403
        assert client.put(f"{base}/999999", headers=env["headers"],
                          json={"name": "x"}).status_code == 404
        # 系统角色禁停用
        assert client.put(f"{base}/{roles['admin'].id}",
                          headers=env["headers"],
                          json={"is_active": False}).status_code == 400
        # title 空白
        assert client.put(f"{base}/{custom.id}", headers=env["headers"],
                          json={"title": "  "}).status_code == 400

        resp = client.put(f"{base}/{custom.id}", headers=env["headers"],
                          json={"name": "Renamed", "description": "d",
                                "content": "c", "is_active": False})
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["name"] == "Renamed"
        assert data["is_active"] is False

    def test_delete_role_resyncs_primary(self, env, client):
        org = env["org"]
        base = f"{env['base']}/organizations/{org.id}/roles"
        roles = _seed_system_roles(org)
        outsider = _uh("mb")
        member = _member(org, outsider)
        from models import OrganizationMemberRole
        OrganizationMemberRole.create(
            organization_id=org.id, member_id=member.id,
            role_id=roles["viewer"].id)
        custom = OrganizationRoleDefinition.create(
            organization_id=org.id, key="del_role", name="Del",
            is_system=False, is_active=True, created_by=env["user"].email)
        db.session.flush()  # 拿到 custom.id 再建绑定
        OrganizationMemberRole.create(
            organization_id=org.id, member_id=member.id,
            role_id=custom.id)
        db.session.commit()

        stranger_org = _org(_uh("st2"))
        db.session.commit()
        assert client.delete(f"{env['base']}/organizations/999999/roles/1",
                             headers=env["headers"]).status_code == 404
        assert client.delete(
            f"{env['base']}/organizations/{stranger_org.id}/roles/1",
            headers=env["headers"]).status_code == 403
        assert client.delete(f"{base}/999999",
                             headers=env["headers"]).status_code == 404
        assert client.delete(f"{base}/{roles['admin'].id}",
                             headers=env["headers"]).status_code == 400

        resp = client.delete(f"{base}/{custom.id}", headers=env["headers"])
        assert resp.status_code == 200
        assert OrganizationRoleDefinition.query.get(custom.id) is None
        # 绑定被清掉后主角色重同步：viewer 是剩余唯一系统角色
        db.session.expire_all()
        member = db.session.get(OrganizationMember, member.id)
        assert member.role == OrganizationRole.VIEWER
        assert OrganizationMemberRole.query.filter_by(
            member_id=member.id,
            role_id=custom.id).count() == 0


# ─────────────────────────── 组织事件 ───────────────────────────


class TestEventRecording:
    def test_record_requires_org(self):
        from api.organizations.events import record_organization_event
        assert record_organization_event(None, "x") is None
        assert record_organization_event(0, "x") is None

    def test_record_normalizes_and_fallbacks(self, env, client):
        from api.organizations.events import record_organization_event
        with client.application.test_request_context("/", environ_base={"REMOTE_ADDR": "10.1.2.3"}):
            g.current_user = None
            event = record_organization_event(
                organization_id=env["org"].id,
                event_type="  spaced.type  ",
                actor_type="x" * 40,
                actor_id=42,
                actor_name=None,
                target_type="y" * 40,
                target_id="7",
                message="m" * 600,
                payload="not-a-dict",
                level="  ",
                source="  ",
            )
            db.session.add(event)
            db.session.commit()
        assert event.event_type == "spaced.type"
        assert len(event.actor_type) == 35  # 32 + "..."
        assert len(event.target_type) == 35
        assert event.actor_name == "42"  # 无名字回退 actor_id
        assert event.message.endswith("...")
        assert len(event.message) == 515  # 512 + "..."
        assert event.payload["raw_payload"] == "not-a-dict"
        assert event.payload["ip"] == "10.1.2.3"
        assert event.source == "api"
        assert event.level == "info"

    def test_record_ip_failure_keeps_payload(self, env, client, monkeypatch):
        from api.organizations import events as ev

        class _BoomRequest:
            @property
            def remote_addr(self):
                raise RuntimeError("no request")

        with client.application.test_request_context():
            monkeypatch.setattr(ev, "request", _BoomRequest())
            event = ev.record_organization_event(
                organization_id=env["org"].id, event_type="t",
                payload={"k": 1})
            db.session.add(event)
            db.session.commit()
        assert event.payload == {"k": 1}


class TestEventRoutes:
    def _mk_event(self, env, event_type="member.invited",
                  actor_type="user", at=None, **kw):
        row = OrganizationEvent(
            organization_id=env["org"].id, event_type=event_type,
            actor_type=actor_type, occurred_at=at or datetime(2026, 9, 1, 8, 0),
            **kw)
        db.session.add(row)
        return row

    def test_routes_matrix(self, env, client):
        base = f"{env['base']}/organizations/{env['org'].id}/events"
        self._mk_event(env, event_type="member.invited", actor_type="user",
                       at=datetime(2026, 9, 1, 8, 0),
                       project_id=env["project"].id, task_id=77)
        self._mk_event(env, event_type="org.updated", actor_type="agent",
                       at=datetime(2026, 9, 2, 8, 0))
        db.session.commit()

        assert client.get(f"{env['base']}/organizations/999999/events",
                          headers=env["headers"]).status_code == 404
        stranger = _uh("st")
        db.session.commit()
        assert client.get(base, headers=_headers_for(stranger)
                          ).status_code == 403

        def types(query=""):
            resp = client.get(f"{base}{query}", headers=env["headers"])
            return [i["event_type"]
                    for i in resp.get_json()["data"]["items"]]

        assert types() == ["org.updated", "member.invited"]
        assert types("?event_type=member.invited") == ["member.invited"]
        assert types("?actor_type=agent") == ["org.updated"]
        assert types(f"?project_id={env['project'].id}") == ["member.invited"]
        assert types("?task_id=77") == ["member.invited"]
        assert types("?from=2026-09-02T00:00:00") == ["org.updated"]
        assert types("?to=2026-09-01T23:59:59") == ["member.invited"]
        assert types("?from=garbage&to=also-bad") == [
            "org.updated", "member.invited"]  # 坏时间戳被忽略
        resp = client.get(f"{base}?page=1&per_page=1", headers=env["headers"])
        assert resp.get_json()["data"]["pagination"]["has_next"] is True


# ─────────────────────────── 内部错误兜底（500）与非 JSON 体 ───────────────────────────


class TestErrorFallbacks:
    def _boom(self, *a, **kw):
        raise RuntimeError("boom")

    def test_org_list_500(self, env, client, monkeypatch):
        from api.organizations import routes_organizations as ro
        monkeypatch.setattr(ro, "_get_user_org_roles_map", self._boom)
        assert client.get(f"{env['base']}/organizations",
                          headers=env["headers"]).status_code == 500

    def test_org_create_500(self, env, client, monkeypatch):
        from api.organizations import routes_organizations as ro
        monkeypatch.setattr(ro, "_ensure_system_roles", self._boom)
        resp = client.post(f"{env['base']}/organizations",
                           headers=env["headers"], json={"name": "x"})
        assert resp.status_code == 500

    def test_org_get_500(self, env, client, monkeypatch):
        monkeypatch.setattr(User, "get_organization_role", self._boom)
        assert client.get(
            f"{env['base']}/organizations/{env['org'].id}",
            headers=env["headers"]).status_code == 500

    def test_org_update_non_json_and_500(self, env, client, monkeypatch):
        base = f"{env['base']}/organizations/{env['org'].id}"
        assert client.put(base, headers=env["headers"], data="junk",
                          content_type="text/plain").status_code == 400
        from api.organizations import routes_organizations as ro
        monkeypatch.setattr(ro, "_invalidate_org_users", self._boom)
        assert client.put(base, headers=env["headers"],
                          json={"name": "x"}).status_code == 500

    def test_member_list_500(self, env, client, monkeypatch):
        from api.organizations import routes_members as rm
        monkeypatch.setattr(rm, "selectinload", self._boom)
        assert client.get(
            f"{env['base']}/organizations/{env['org'].id}/members",
            headers=env["headers"]).status_code == 500

    def test_member_update_org_404_non_json_and_500(self, env, client,
                                                    monkeypatch):
        member_base = f"{env['base']}/organizations/{{org}}/members/1"
        assert client.put(member_base.format(org=999999),
                          headers=env["headers"],
                          json={"role": "admin"}).status_code == 404
        target = _uh("tg")
        _member(env["org"], target)
        db.session.commit()
        url = member_base.format(org=env["org"].id)
        url = f"{env['base']}/organizations/{env['org'].id}/members/{target.id}"
        assert client.put(url, headers=env["headers"], data="junk",
                          content_type="text/plain").status_code == 400
        from api.organizations import routes_members as rm
        monkeypatch.setattr(rm, "record_organization_event", self._boom)
        assert client.put(url, headers=env["headers"],
                          json={"role": "admin"}).status_code == 500

    def test_member_invite_500(self, env, client, monkeypatch):
        _seed_system_roles(env["org"])
        invitee = _uh("in")
        db.session.commit()
        from api.organizations import routes_members as rm
        monkeypatch.setattr(rm, "_replace_member_roles", self._boom)
        resp = client.post(
            f"{env['base']}/organizations/{env['org'].id}/members/invite",
            headers=env["headers"], json={"email": invitee.email})
        assert resp.status_code == 500

    def test_member_remove_org_404_and_500(self, env, client, monkeypatch):
        target = _uh("tg")
        _member(env["org"], target)
        db.session.commit()
        url = f"{env['base']}/organizations/{env['org'].id}/members/{target.id}"
        assert client.put(f"{env['base']}/organizations/999999/members/1",
                          headers=env["headers"],
                          json={"role": "admin"}).status_code == 404
        assert client.delete(f"{env['base']}/organizations/999999/members/1",
                             headers=env["headers"]).status_code == 404
        from api.organizations import routes_members as rm
        monkeypatch.setattr(rm, "record_organization_event", self._boom)
        assert client.delete(url, headers=env["headers"]).status_code == 500

    def test_role_list_500(self, env, client, monkeypatch):
        from api.organizations import routes_roles as rr
        monkeypatch.setattr(rr, "_ensure_system_roles", self._boom)
        assert client.get(
            f"{env['base']}/organizations/{env['org'].id}/roles",
            headers=env["headers"]).status_code == 500

    def test_role_create_non_json_and_500(self, env, client, monkeypatch):
        base = f"{env['base']}/organizations/{env['org'].id}/roles"
        assert client.post(base, headers=env["headers"], data="junk",
                           content_type="text/plain").status_code == 400
        from api.organizations import routes_roles as rr
        monkeypatch.setattr(rr, "_slugify_role_key", self._boom)
        assert client.post(base, headers=env["headers"],
                           json={"name": "x"}).status_code == 500

    def test_role_update_non_json_and_500(self, env, client, monkeypatch):
        roles = _seed_system_roles(env["org"])
        url = (f"{env['base']}/organizations/{env['org'].id}"
               f"/roles/{roles['member'].id}")
        assert client.put(url, headers=env["headers"], data="junk",
                          content_type="text/plain").status_code == 400
        from api.organizations import routes_roles as rr
        monkeypatch.setattr(rr, "_normalize_optional_text", self._boom)
        # description 才会走 _normalize_optional_text
        assert client.put(url, headers=env["headers"],
                          json={"description": "x"}).status_code == 500

    def test_role_delete_500(self, env, client, monkeypatch):
        custom = OrganizationRoleDefinition.create(
            organization_id=env["org"].id, key="boom_role", name="Boom",
            is_system=False, is_active=True, created_by=env["user"].email)
        db.session.commit()
        url = (f"{env['base']}/organizations/{env['org'].id}"
               f"/roles/{custom.id}")
        from api.organizations import routes_roles as rr
        monkeypatch.setattr(rr, "_invalidate_org_users", self._boom)
        assert client.delete(url, headers=env["headers"]).status_code == 500
