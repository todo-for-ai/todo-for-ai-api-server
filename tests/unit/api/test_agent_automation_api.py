"""Agent 自动化 API（api/agent_automation 包 + api/channels.py）单元回归。

覆盖：触发器 CRUD（task_event/cron 双类型全校验分支、重名 409、Patch
类型分派、删除=停用）、Runner 配置（读写/执行模式/沙箱策略清洗/版本自增）、
运行列表与详情（状态过滤/分页/404）、通知渠道全端点（user/org/project
三 scope 的 list/create、Patch 保 secret、Delete、effective-channels
三级回退与事件过滤）、shared 辅助（cron 解析各分支/next_fire_at/布尔
与整数归一/事件校验/scope 三型权限解析）、幂等键生成。
历史注记：原包内 routes_channels.py 是 api/channels.py 解耦时的死副本
（零引用），本迭代已删除；api/channels.py 是唯一在册实现。
"""

import uuid
from datetime import datetime

import pytest
from flask import g
from flask_jwt_extended import create_access_token

from models import (
    Agent,
    AgentRun,
    AgentRunState,
    AgentStatus,
    AgentTrigger,
    AgentMisfirePolicy,
    AgentTriggerType,
    NotificationChannel,
    Organization,
    OrganizationMember,
    OrganizationMemberStatus,
    OrganizationRole,
    Project,
    ProjectMember,
    ProjectMemberStatus,
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


def _mk_user(prefix="au"):
    u = User(username=f"{prefix}_{uuid.uuid4().hex[:8]}",
             email=f"{prefix}_{uuid.uuid4().hex[:6]}@t.io")
    db.session.add(u)
    db.session.flush()
    return u


def _mk_org(owner, prefix="ao"):
    org = Organization(name=f"{prefix}_{uuid.uuid4().hex[:6]}",
                       slug=f"{prefix}_{uuid.uuid4().hex[:6]}",
                       owner_id=owner.id)
    db.session.add(org)
    db.session.flush()
    return org


def _mk_agent(workspace_id, creator, prefix="aa"):
    row = Agent(workspace_id=workspace_id, owner_id=creator.id,
                creator_user_id=creator.id,
                name=f"{prefix}_{uuid.uuid4().hex[:6]}",
                status=AgentStatus.ACTIVE)
    db.session.add(row)
    db.session.flush()
    return row


def _mk_project(owner, org=None, prefix="ap"):
    row = Project(name=f"{prefix}_{uuid.uuid4().hex[:6]}",
                  owner_id=owner.id,
                  organization_id=org.id if org else None)
    db.session.add(row)
    db.session.flush()
    return row


def _mk_channel(scope_type, scope_id, name="ch", channel_type="in_app",
                enabled=True, is_default=False, events=None, config=None,
                creator=None):
    creator = creator or _mk_user("chc")
    row = NotificationChannel(
        scope_type=scope_type, scope_id=scope_id, name=name,
        channel_type=channel_type, enabled=enabled, is_default=is_default,
        events=events, config=config or {},
        created_by_user_id=creator.id, updated_by_user_id=creator.id,
        created_by=creator.email)
    db.session.add(row)
    db.session.flush()
    return row


def _headers_for(user):
    token = create_access_token(identity=str(user.id))
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def env(_isolated_app):
    """主场景：owner 用户 + 其组织 + 组织内 Agent + 项目。"""
    user = _mk_user("ow")
    org = _mk_org(user, "wo")
    agent = _mk_agent(org.id, user)
    project = _mk_project(user, org=org)
    db.session.commit()
    return {
        "user": user, "org": org, "agent": agent, "project": project,
        "headers": _headers_for(user),
        "base": "/todo-for-ai/api/v1",
    }


# ─────────────────────────── shared 辅助函数 ───────────────────────────


class TestSharedHelpers:
    def test_normalize_int_valid_and_fallback(self):
        from api.agent_automation.shared import _normalize_int
        assert _normalize_int("12", 5) == 12
        assert _normalize_int(7, 5) == 7
        assert _normalize_int("abc", 5) == 5
        assert _normalize_int(None, 5) == 5

    def test_parse_bool_matrix(self):
        from api.agent_automation.shared import _parse_bool
        assert _parse_bool(True) is True
        assert _parse_bool(False) is False
        assert _parse_bool(1) is True
        assert _parse_bool(0) is False
        assert _parse_bool(2.5) is True
        assert _parse_bool("yes") is True
        assert _parse_bool(" ON ") is True
        assert _parse_bool("no") is False
        assert _parse_bool(" Off ") is False
        assert _parse_bool("maybe", True) is True
        assert _parse_bool(None, False) is False

    def test_parse_cron_field_rejects_garbage(self):
        from api.agent_automation.shared import _parse_cron_field
        assert _parse_cron_field("", 0, 59) is None
        assert _parse_cron_field(None, 0, 59) is None
        assert _parse_cron_field("*/x", 0, 59) is None
        assert _parse_cron_field("*/0", 0, 59) is None
        assert _parse_cron_field("10-5", 0, 59) is None
        assert _parse_cron_field("60", 0, 59) is None
        assert _parse_cron_field("-1", 0, 59) is None
        assert _parse_cron_field("5,,10", 0, 59) is None
        assert _parse_cron_field("abc", 0, 59) is None

    def test_parse_cron_field_accepts_forms(self):
        from api.agent_automation.shared import _parse_cron_field
        assert _parse_cron_field("*", 0, 2) == {0, 1, 2}
        assert _parse_cron_field("*/2", 0, 6) == {0, 2, 4, 6}
        assert _parse_cron_field("5-7", 0, 59) == {5, 6, 7}
        assert _parse_cron_field("5,7", 0, 59) == {5, 7}
        assert _parse_cron_field("0-59", 0, 59) == set(range(0, 60))

    def test_parse_cron_expr_shape(self):
        from api.agent_automation.shared import _parse_cron_expr
        assert _parse_cron_expr(None) is None
        assert _parse_cron_expr("* * *") is None
        assert _parse_cron_expr("* * * * * *") is None
        assert _parse_cron_expr("* * * * 9") is None  # dow 越界 → 整体无效
        parsed = _parse_cron_expr("*/15 0 1,15 1-12 *")
        assert parsed == {
            "minute": {0, 15, 30, 45},
            "hour": {0},
            "dom": {1, 15},
            "month": set(range(1, 13)),
            "dow": set(range(0, 7)),
        }

    def test_compute_next_fire_at_invalid(self):
        from api.agent_automation.shared import _compute_next_fire_at
        assert _compute_next_fire_at("not a cron") is None

    def test_compute_next_fire_at_rounds_up_to_next_match(self):
        from api.agent_automation.shared import _compute_next_fire_at
        base = datetime(2026, 9, 8, 10, 7, 30)
        assert _compute_next_fire_at("*/15 * * * *", base_time=base) == \
            datetime(2026, 9, 8, 10, 15)

    def test_compute_next_fire_at_dow_sunday_is_zero(self):
        from api.agent_automation.shared import _compute_next_fire_at
        # 2026-09-13 是周日，2026-09-14 是周一
        base = datetime(2026, 9, 8, 0, 0)
        assert _compute_next_fire_at("0 12 * * 0", base_time=base) == \
            datetime(2026, 9, 13, 12, 0)
        assert _compute_next_fire_at("0 8 * * 1", base_time=base) == \
            datetime(2026, 9, 14, 8, 0)

    def test_compute_next_fire_at_gives_up_after_a_year(self):
        from api.agent_automation.shared import _compute_next_fire_at
        # 2 月没有 31 号：扫满 366 天后返回 None
        assert _compute_next_fire_at(
            "0 0 31 2 *", base_time=datetime(2026, 3, 1)) is None

    def test_validate_task_events(self):
        from api.agent_automation.shared import _validate_task_events
        assert _validate_task_events([]) is None
        assert _validate_task_events("created") is None
        assert _validate_task_events(["bogus"]) is None
        assert _validate_task_events(["CREATED ", "created", "updated"]) == \
            ["created", "updated"]

    def test_resolve_channel_scope_unsupported_type(self, env, _isolated_app):
        from api.agent_automation import shared
        with _isolated_app.test_request_context():
            g.current_user = env["user"]
            row, err = shared._resolve_channel_scope("galaxy", 1)
            assert row is None and err is not None

    def test_can_manage_scope_unknown_type_is_false(self, env, _isolated_app):
        from api.agent_automation import shared
        with _isolated_app.test_request_context():
            g.current_user = env["user"]
            assert shared._can_manage_scope(
                {"scope_type": "galaxy", "scope_id": 1}) is False

    def test_list_channels_by_scope_orders_by_update(self, env):
        from api.agent_automation import shared
        c1 = _mk_channel("user", env["user"].id, name="old")
        c2 = _mk_channel("user", env["user"].id, name="new")
        db.session.commit()
        rows = shared._list_channels_by_scope("user", env["user"].id)
        assert [r["name"] for r in rows] == [c2.name, c1.name]
        assert all(r["config"] == {} or isinstance(r["config"], dict)
                   for r in rows)


# ─────────────────────────── 触发器路由 ───────────────────────────


def _trigger_payload(**kw):
    payload = {"name": f"t_{uuid.uuid4().hex[:6]}",
               "trigger_type": "task_event",
               "task_event_types": ["created"]}
    payload.update(kw)
    return payload


class TestTriggerRoutes:
    def _url(self, env, agent=None):
        agent = agent or env["agent"]
        return (f"{env['base']}/workspaces/{env['org'].id}"
                f"/agents/{agent.id}/triggers")

    def test_list_agent_missing_404(self, env, client):
        resp = client.get(
            f"{env['base']}/workspaces/{env['org'].id}/agents/999999/triggers",
            headers=env["headers"])
        assert resp.status_code == 404

    def test_list_stranger_forbidden(self, env, client):
        stranger = _mk_user("st")
        db.session.commit()
        resp = client.get(self._url(env), headers=_headers_for(stranger))
        assert resp.status_code == 403

    def test_list_sorted_by_priority(self, env, client):
        low = AgentTrigger(workspace_id=env["org"].id,
                           agent_id=env["agent"].id, name="low",
                           trigger_type="task_event", priority=10,
                           task_event_types=["created"],
                           created_by=env["user"].email)
        high = AgentTrigger(workspace_id=env["org"].id,
                            agent_id=env["agent"].id, name="high",
                            trigger_type="cron", priority=90,
                            cron_expr="0 0 * * *", timezone="UTC",
                            created_by=env["user"].email)
        db.session.add_all([low, high])
        db.session.commit()

        resp = client.get(self._url(env), headers=env["headers"])
        assert resp.status_code == 200
        items = resp.get_json()["data"]["items"]
        assert [i["name"] for i in items] == ["low", "high"]

    def test_create_agent_missing_404(self, env, client):
        resp = client.post(
            f"{env['base']}/workspaces/{env['org'].id}/agents/999999/triggers",
            headers=env["headers"], json=_trigger_payload())
        assert resp.status_code == 404

    def test_create_stranger_forbidden(self, env, client):
        stranger = _mk_user("st")
        db.session.commit()
        resp = client.post(self._url(env), headers=_headers_for(stranger),
                           json=_trigger_payload())
        assert resp.status_code == 403

    def test_create_requires_fields(self, env, client):
        resp = client.post(self._url(env), headers=env["headers"],
                           json={"trigger_type": "task_event"})
        assert resp.status_code == 400

    def test_create_empty_name(self, env, client):
        resp = client.post(self._url(env), headers=env["headers"],
                           json=_trigger_payload(name="   "))
        assert resp.status_code == 400

    def test_create_invalid_trigger_type(self, env, client):
        resp = client.post(self._url(env), headers=env["headers"],
                           json=_trigger_payload(trigger_type="magic"))
        assert resp.status_code == 400

    def test_create_duplicate_name_conflict(self, env, client):
        payload = _trigger_payload()
        assert client.post(self._url(env), headers=env["headers"],
                           json=payload).status_code == 201
        dup = client.post(self._url(env), headers=env["headers"],
                          json=_trigger_payload(name=payload["name"]))
        assert dup.status_code == 409

    def test_create_rejects_non_utc_timezone(self, env, client):
        resp = client.post(self._url(env), headers=env["headers"],
                           json=_trigger_payload(timezone="Asia/Shanghai"))
        assert resp.status_code == 400

    def test_create_invalid_misfire_policy(self, env, client):
        resp = client.post(self._url(env), headers=env["headers"],
                           json=_trigger_payload(misfire_policy="explode"))
        assert resp.status_code == 400

    def test_create_task_event_requires_valid_events(self, env, client):
        resp = client.post(self._url(env), headers=env["headers"],
                           json=_trigger_payload(task_event_types=[]))
        assert resp.status_code == 400
        resp = client.post(self._url(env), headers=env["headers"],
                           json=_trigger_payload(task_event_types=["nope"]))
        assert resp.status_code == 400

    def test_create_task_event_defaults(self, env, client):
        resp = client.post(self._url(env), headers=env["headers"],
                           json=_trigger_payload())
        assert resp.status_code == 201
        data = resp.get_json()["data"]
        assert data["priority"] == 100
        assert data["task_event_types"] == ["created"]
        assert data["task_filter"] == {}
        assert data["misfire_policy"] == "catch_up_once"
        assert data["enabled"] is True

    def test_create_cron_requires_expr(self, env, client):
        resp = client.post(self._url(env), headers=env["headers"],
                           json=_trigger_payload(trigger_type="cron",
                                                 cron_expr="   "))
        assert resp.status_code == 400

    def test_create_cron_invalid_expr(self, env, client):
        resp = client.post(self._url(env), headers=env["headers"],
                           json=_trigger_payload(trigger_type="cron",
                                                 cron_expr="garbage"))
        assert resp.status_code == 400

    def test_create_cron_clamps_windows_and_next_fire(self, env, client):
        resp = client.post(
            self._url(env), headers=env["headers"],
            json=_trigger_payload(trigger_type="cron", cron_expr="0 0 * * *",
                                  misfire_policy="skip",
                                  catch_up_window_seconds=1,
                                  dedup_window_seconds=999999,
                                  priority="abc"))
        assert resp.status_code == 201
        data = resp.get_json()["data"]
        assert data["catch_up_window_seconds"] == 10
        assert data["dedup_window_seconds"] == 3600
        assert data["priority"] == 100
        assert data["misfire_policy"] == "skip"
        assert data["next_fire_at"] is not None
        assert data["timezone"] == "UTC"

    def test_patch_agent_missing_404(self, env, client):
        resp = client.patch(
            f"{env['base']}/workspaces/{env['org'].id}/agents/999999"
            f"/triggers/1", headers=env["headers"], json={"enabled": False})
        assert resp.status_code == 404

    def test_patch_stranger_forbidden(self, env, client):
        stranger = _mk_user("st")
        db.session.commit()
        resp = client.patch(f"{self._url(env)}/1",
                            headers=_headers_for(stranger),
                            json={"enabled": False})
        assert resp.status_code == 403

    def test_patch_trigger_of_other_agent_404(self, env, client):
        trigger = AgentTrigger(workspace_id=env["org"].id,
                               agent_id=env["agent"].id, name="tg",
                               trigger_type="task_event",
                               task_event_types=["created"],
                               created_by=env["user"].email)
        db.session.add(trigger)
        other_agent = _mk_agent(env["org"].id, env["user"])
        db.session.commit()
        resp = client.patch(
            f"{env['base']}/workspaces/{env['org'].id}"
            f"/agents/{other_agent.id}/triggers/{trigger.id}",
            headers=env["headers"], json={"enabled": False})
        assert resp.status_code == 404

    def test_patch_empty_and_duplicate_name(self, env, client):
        first = AgentTrigger(workspace_id=env["org"].id,
                             agent_id=env["agent"].id, name="first",
                             trigger_type="task_event",
                             task_event_types=["created"],
                             created_by=env["user"].email)
        second = AgentTrigger(workspace_id=env["org"].id,
                              agent_id=env["agent"].id, name="second",
                              trigger_type="task_event",
                              task_event_types=["created"],
                              created_by=env["user"].email)
        db.session.add_all([first, second])
        db.session.commit()

        resp = client.patch(f"{self._url(env)}/{first.id}",
                            headers=env["headers"], json={"name": "  "})
        assert resp.status_code == 400
        resp = client.patch(f"{self._url(env)}/{first.id}",
                            headers=env["headers"], json={"name": "second"})
        assert resp.status_code == 409
        resp = client.patch(f"{self._url(env)}/{first.id}",
                            headers=env["headers"], json={"name": "renamed"})
        assert resp.status_code == 200
        assert resp.get_json()["data"]["name"] == "renamed"

    def test_patch_non_json_body_400(self, env, client):
        trigger = AgentTrigger(workspace_id=env["org"].id,
                               agent_id=env["agent"].id, name="nj",
                               trigger_type="task_event",
                               task_event_types=["created"],
                               created_by=env["user"].email)
        db.session.add(trigger)
        db.session.commit()
        resp = client.patch(f"{self._url(env)}/{trigger.id}",
                            headers=env["headers"],
                            data="not json",
                            content_type="text/plain")
        assert resp.status_code == 400

    def test_patch_invalid_misfire_policy(self, env, client):
        trigger = AgentTrigger(workspace_id=env["org"].id,
                               agent_id=env["agent"].id, name="mp",
                               trigger_type="task_event",
                               task_event_types=["created"],
                               created_by=env["user"].email)
        db.session.add(trigger)
        db.session.commit()
        resp = client.patch(f"{self._url(env)}/{trigger.id}",
                            headers=env["headers"],
                            json={"misfire_policy": "explode"})
        assert resp.status_code == 400

    def test_patch_scalar_fields_and_clamps(self, env, client):
        trigger = AgentTrigger(workspace_id=env["org"].id,
                               agent_id=env["agent"].id, name="scalars",
                               trigger_type="task_event",
                               task_event_types=["created"],
                               created_by=env["user"].email)
        db.session.add(trigger)
        db.session.commit()

        resp = client.patch(
            f"{self._url(env)}/{trigger.id}", headers=env["headers"],
            json={"enabled": "false", "priority": 7,
                  "task_filter": {"labels": ["x"]},
                  "misfire_policy": "skip",
                  "catch_up_window_seconds": 1,
                  "dedup_window_seconds": 999999})
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["enabled"] is False
        assert data["priority"] == 7
        assert data["task_filter"] == {"labels": ["x"]}
        assert data["misfire_policy"] == "skip"
        assert data["catch_up_window_seconds"] == 10
        assert data["dedup_window_seconds"] == 3600

    def test_patch_task_filter_must_be_object(self, env, client):
        trigger = AgentTrigger(workspace_id=env["org"].id,
                               agent_id=env["agent"].id, name="tf",
                               trigger_type="task_event",
                               task_event_types=["created"],
                               created_by=env["user"].email)
        db.session.add(trigger)
        db.session.commit()
        resp = client.patch(f"{self._url(env)}/{trigger.id}",
                            headers=env["headers"],
                            json={"task_filter": ["nope"]})
        assert resp.status_code == 400

    def test_patch_task_event_types_branches(self, env, client):
        trigger = AgentTrigger(workspace_id=env["org"].id,
                               agent_id=env["agent"].id, name="evt",
                               trigger_type="task_event",
                               task_event_types=["created"],
                               created_by=env["user"].email)
        db.session.add(trigger)
        db.session.commit()
        resp = client.patch(f"{self._url(env)}/{trigger.id}",
                            headers=env["headers"],
                            json={"task_event_types": ["bogus"]})
        assert resp.status_code == 400
        resp = client.patch(f"{self._url(env)}/{trigger.id}",
                            headers=env["headers"],
                            json={"task_event_types": ["completed"]})
        assert resp.status_code == 200
        assert resp.get_json()["data"]["task_event_types"] == ["completed"]

    def test_patch_cron_branches(self, env, client):
        trigger = AgentTrigger(workspace_id=env["org"].id,
                               agent_id=env["agent"].id, name="cron",
                               trigger_type="cron", cron_expr="0 0 * * *",
                               timezone="UTC",
                               created_by=env["user"].email)
        db.session.add(trigger)
        db.session.commit()
        base = f"{self._url(env)}/{trigger.id}"
        assert client.patch(base, headers=env["headers"],
                            json={"cron_expr": ""}).status_code == 400
        assert client.patch(base, headers=env["headers"],
                            json={"cron_expr": "junk"}).status_code == 400
        resp = client.patch(base, headers=env["headers"],
                            json={"cron_expr": "30 2 * * *"})
        assert resp.status_code == 200
        assert resp.get_json()["data"]["cron_expr"] == "30 2 * * *"

    def test_patch_ignores_type_mismatched_fields(self, env, client):
        # task_event 触发器带 cron_expr：应被忽略而非报错
        trigger = AgentTrigger(workspace_id=env["org"].id,
                               agent_id=env["agent"].id, name="mix",
                               trigger_type="task_event",
                               task_event_types=["created"],
                               created_by=env["user"].email)
        db.session.add(trigger)
        db.session.commit()
        resp = client.patch(f"{self._url(env)}/{trigger.id}",
                            headers=env["headers"],
                            json={"cron_expr": "0 0 * * *"})
        assert resp.status_code == 200
        assert resp.get_json()["data"]["task_event_types"] == ["created"]

    def test_delete_disables_trigger(self, env, client):
        trigger = AgentTrigger(workspace_id=env["org"].id,
                               agent_id=env["agent"].id, name="bye",
                               trigger_type="task_event",
                               task_event_types=["created"],
                               created_by=env["user"].email)
        db.session.add(trigger)
        db.session.commit()
        resp = client.delete(f"{self._url(env)}/{trigger.id}",
                             headers=env["headers"])
        assert resp.status_code == 200
        assert resp.get_json()["data"]["enabled"] is False
        db.session.expire_all()
        assert db.session.get(AgentTrigger, trigger.id).enabled is False

    def test_delete_missing_trigger_404(self, env, client):
        resp = client.delete(f"{self._url(env)}/999999",
                             headers=env["headers"])
        assert resp.status_code == 404

    def test_delete_agent_missing_404(self, env, client):
        resp = client.delete(
            f"{env['base']}/workspaces/{env['org'].id}/agents/999999"
            f"/triggers/1", headers=env["headers"])
        assert resp.status_code == 404

    def test_delete_stranger_forbidden(self, env, client):
        trigger = AgentTrigger(workspace_id=env["org"].id,
                               agent_id=env["agent"].id, name="locked",
                               trigger_type="task_event",
                               task_event_types=["created"],
                               created_by=env["user"].email)
        stranger = _mk_user("st")
        db.session.add(trigger)
        db.session.commit()
        resp = client.delete(f"{self._url(env)}/{trigger.id}",
                             headers=_headers_for(stranger))
        assert resp.status_code == 403


# ─────────────────────────── Runner 配置路由 ───────────────────────────


class TestRunnerConfigRoutes:
    def _url(self, env, agent=None):
        agent = agent or env["agent"]
        return (f"{env['base']}/workspaces/{env['org'].id}"
                f"/agents/{agent.id}/runner-config")

    def test_get_agent_missing_404(self, env, client):
        resp = client.get(
            f"{env['base']}/workspaces/{env['org'].id}/agents/999999"
            f"/runner-config", headers=env["headers"])
        assert resp.status_code == 404

    def test_get_stranger_forbidden(self, env, client):
        stranger = _mk_user("st")
        db.session.commit()
        resp = client.get(self._url(env), headers=_headers_for(stranger))
        assert resp.status_code == 403

    def test_get_defaults(self, env, client):
        resp = client.get(self._url(env), headers=env["headers"])
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["execution_mode"] == "external_pull"
        assert data["runner_enabled"] is False
        assert data["sandbox_profile"] == "standard"
        assert data["sandbox_policy"] == {"network_mode": "whitelist",
                                          "allowed_domains": []}
        assert data["runner_config_version"] == 1

    def test_patch_agent_missing_404(self, env, client):
        resp = client.patch(
            f"{env['base']}/workspaces/{env['org'].id}/agents/999999"
            f"/runner-config", headers=env["headers"], json={})
        assert resp.status_code == 404

    def test_patch_non_json_body_400(self, env, client):
        resp = client.patch(self._url(env), headers=env["headers"],
                            data="not json", content_type="text/plain")
        assert resp.status_code == 400

    def test_patch_stranger_forbidden(self, env, client):
        stranger = _mk_user("st")
        db.session.commit()
        resp = client.patch(self._url(env), headers=_headers_for(stranger),
                            json={})
        assert resp.status_code == 403

    def test_patch_invalid_execution_mode(self, env, client):
        resp = client.patch(self._url(env), headers=env["headers"],
                            json={"execution_mode": "warp"})
        assert resp.status_code == 400

    def test_patch_valid_execution_mode(self, env, client):
        resp = client.patch(self._url(env), headers=env["headers"],
                            json={"execution_mode": "MANAGED_RUNNER",
                                  "runner_enabled": "true"})
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["execution_mode"] == "managed_runner"
        assert data["runner_enabled"] is True
        assert data["runner_config_version"] == 2
        assert data["config_version"] == 2

    def test_patch_sandbox_profile_normalization(self, env, client):
        resp = client.patch(self._url(env), headers=env["headers"],
                            json={"sandbox_profile": "  "})
        assert resp.status_code == 200
        assert resp.get_json()["data"]["sandbox_profile"] == "standard"

    def test_patch_sandbox_policy_validation(self, env, client):
        assert client.patch(self._url(env), headers=env["headers"],
                            json={"sandbox_policy": ["nope"]}
                            ).status_code == 400
        assert client.patch(self._url(env), headers=env["headers"],
                            json={"sandbox_policy": {
                                "allowed_domains": "not-a-list"}}
                            ).status_code == 400

    def test_patch_sandbox_policy_sanitized(self, env, client):
        resp = client.patch(
            self._url(env), headers=env["headers"],
            json={"sandbox_policy": {
                "network_mode": " Allowlist ",
                "allowed_domains": [" A.COM ", "", "b.org", 42]}})
        assert resp.status_code == 200
        assert resp.get_json()["data"]["sandbox_policy"] == {
            "network_mode": "allowlist",
            "allowed_domains": ["a.com", "b.org", "42"],
        }


# ─────────────────────────── 运行路由 ───────────────────────────


class TestRunRoutes:
    def _base(self, env, agent=None):
        agent = agent or env["agent"]
        return (f"{env['base']}/workspaces/{env['org'].id}"
                f"/agents/{agent.id}/runs")

    def _mk_run(self, env, run_id, state="queued", scheduled=None):
        row = AgentRun(run_id=run_id, workspace_id=env["org"].id,
                       agent_id=env["agent"].id, state=state,
                       scheduled_at=scheduled or datetime(2026, 9, 1, 0, 0),
                       trigger_reason="manual")
        db.session.add(row)
        return row

    def test_list_agent_missing_404(self, env, client):
        resp = client.get(
            f"{env['base']}/workspaces/{env['org'].id}/agents/999999/runs",
            headers=env["headers"])
        assert resp.status_code == 404

    def test_list_stranger_forbidden(self, env, client):
        stranger = _mk_user("st")
        db.session.commit()
        resp = client.get(self._base(env), headers=_headers_for(stranger))
        assert resp.status_code == 403

    def test_list_invalid_state_filter(self, env, client):
        resp = client.get(f"{self._base(env)}?state=warp",
                          headers=env["headers"])
        assert resp.status_code == 400

    def test_list_pagination_and_state(self, env, client):
        self._mk_run(env, "r1", "queued", datetime(2026, 9, 1))
        self._mk_run(env, "r2", "succeeded", datetime(2026, 9, 2))
        self._mk_run(env, "r3", "queued", datetime(2026, 9, 3))
        db.session.commit()

        resp = client.get(self._base(env), headers=env["headers"])
        assert resp.status_code == 200
        body = resp.get_json()["data"]
        assert body["pagination"]["total"] == 3
        assert [i["run_id"] for i in body["items"]] == ["r3", "r2", "r1"]

        resp = client.get(f"{self._base(env)}?state=queued&page=2&per_page=1",
                          headers=env["headers"])
        body = resp.get_json()["data"]
        assert body["pagination"]["total"] == 2
        assert [i["run_id"] for i in body["items"]] == ["r1"]
        assert body["pagination"]["has_prev"] is True
        assert body["pagination"]["has_next"] is False

    def test_detail_flow(self, env, client):
        stranger = _mk_user("st")
        run = self._mk_run(env, "rx")
        db.session.commit()
        base = f"{self._base(env)}/{run.run_id}"

        assert client.get(f"{env['base']}/workspaces/{env['org'].id}"
                          f"/agents/999999/runs/{run.run_id}",
                          headers=env["headers"]).status_code == 404
        assert client.get(base, headers=_headers_for(stranger)
                          ).status_code == 403
        assert client.get(f"{self._base(env)}/missing",
                          headers=env["headers"]).status_code == 404

        resp = client.get(base, headers=env["headers"])
        assert resp.status_code == 200
        assert resp.get_json()["data"]["run_id"] == "rx"


# ─────────────────────────── 通知渠道路由（api/channels.py）───────────────────────────


class TestChannelScopeResolution:
    def test_user_scope_other_user_forbidden(self, env, client):
        other = _mk_user("ot")
        db.session.commit()
        resp = client.get(f"{env['base']}/users/{other.id}/channels",
                          headers=env["headers"])
        assert resp.status_code == 403

    def test_org_scope_missing_404_and_stranger_403(self, env, client):
        assert client.get(f"{env['base']}/organizations/999999/channels",
                          headers=env["headers"]).status_code == 404
        stranger = _mk_user("st")
        other_org = _mk_org(stranger, "oo")
        db.session.commit()
        resp = client.get(
            f"{env['base']}/organizations/{other_org.id}/channels",
            headers=env["headers"])
        assert resp.status_code == 403

    def test_org_member_can_read_but_not_manage(self, env, client):
        outsider = _mk_user("mb")
        other_org = _mk_org(outsider, "oo")
        db.session.add(OrganizationMember(
            organization_id=other_org.id, user_id=env["user"].id,
            role=OrganizationRole.MEMBER,
            status=OrganizationMemberStatus.ACTIVE))
        db.session.commit()
        base = f"{env['base']}/organizations/{other_org.id}/channels"
        assert client.get(base, headers=env["headers"]).status_code == 200
        resp = client.post(base, headers=env["headers"],
                           json={"name": "n", "channel_type": "in_app"})
        assert resp.status_code == 403

    def test_project_scope_missing_404(self, env, client):
        resp = client.get(f"{env['base']}/projects/999999/channels",
                          headers=env["headers"])
        assert resp.status_code == 404

    def test_project_scope_stranger_forbidden(self, env, client):
        stranger = _mk_user("st")
        stranger_project = _mk_project(stranger)
        db.session.commit()
        resp = client.get(
            f"{env['base']}/projects/{stranger_project.id}/channels",
            headers=env["headers"])
        assert resp.status_code == 403

    def test_project_member_reads_but_cannot_create(self, env, client):
        outsider = _mk_user("pm")
        db.session.add(ProjectMember(
            project_id=env["project"].id, user_id=outsider.id,
            status=ProjectMemberStatus.ACTIVE))
        db.session.commit()
        base = f"{env['base']}/projects/{env['project'].id}/channels"
        assert client.get(base, headers=_headers_for(outsider)
                          ).status_code == 200
        resp = client.post(base, headers=_headers_for(outsider),
                           json={"name": "n", "channel_type": "in_app"})
        assert resp.status_code == 403


class TestChannelCreate:
    def _post(self, client, env, url, payload):
        return client.post(url, headers=env["headers"], json=payload)

    def test_create_user_channel(self, env, client):
        resp = self._post(client, env, f"{env['base']}/users/{env['user'].id}/channels",
                          {"name": "mine", "channel_type": "in_app",
                           "events": ["created", "task.updated", "created"]})
        assert resp.status_code == 201
        data = resp.get_json()["data"]
        assert data["scope_type"] == "user"
        assert data["events"] == ["task.created", "task.updated"]
        assert data["enabled"] is True
        assert data["is_default"] is False

    def test_create_requires_fields(self, env, client):
        resp = self._post(client, env, f"{env['base']}/users/{env['user'].id}/channels",
                          {"name": "x"})
        assert resp.status_code == 400

    def test_create_user_channel_of_other_forbidden(self, env, client):
        other = _mk_user("ot")
        db.session.commit()
        resp = self._post(client, env,
                          f"{env['base']}/users/{other.id}/channels",
                          {"name": "x", "channel_type": "in_app"})
        assert resp.status_code == 403

    def test_create_invalid_channel_type(self, env, client):
        resp = self._post(client, env, f"{env['base']}/users/{env['user'].id}/channels",
                          {"name": "x", "channel_type": "smoke-signal"})
        assert resp.status_code == 400

    def test_create_events_must_be_array(self, env, client):
        resp = self._post(client, env, f"{env['base']}/users/{env['user'].id}/channels",
                          {"name": "x", "channel_type": "in_app",
                           "events": "created"})
        assert resp.status_code == 400

    def test_create_events_unsupported(self, env, client):
        resp = self._post(client, env, f"{env['base']}/users/{env['user'].id}/channels",
                          {"name": "x", "channel_type": "in_app",
                           "events": ["task.danced"]})
        assert resp.status_code == 400

    def test_create_config_must_be_http_url(self, env, client):
        resp = self._post(client, env, f"{env['base']}/users/{env['user'].id}/channels",
                          {"name": "x", "channel_type": "webhook",
                           "config": {"url": "ftp://nope"}})
        assert resp.status_code == 400

    def test_create_empty_name(self, env, client):
        resp = self._post(client, env, f"{env['base']}/users/{env['user'].id}/channels",
                          {"name": "   ", "channel_type": "in_app"})
        assert resp.status_code == 400

    def test_create_truncates_long_name(self, env, client):
        resp = self._post(client, env, f"{env['base']}/users/{env['user'].id}/channels",
                          {"name": "a" * 200, "channel_type": "in_app"})
        assert resp.status_code == 201
        assert len(resp.get_json()["data"]["name"]) == 128

    def test_create_org_channel_defaults(self, env, client):
        resp = self._post(
            client, env,
            f"{env['base']}/organizations/{env['org'].id}/channels",
            {"name": "orgch", "channel_type": "in_app",
             "enabled": "false", "is_default": "yes"})
        assert resp.status_code == 201
        data = resp.get_json()["data"]
        assert data["scope_type"] == "organization"
        assert data["enabled"] is False
        assert data["is_default"] is True

    def test_create_project_channel_with_webhook_headers(self, env, client):
        resp = self._post(
            client, env, f"{env['base']}/projects/{env['project'].id}/channels",
            {"name": "proj", "channel_type": "webhook",
             "config": {"url": "https://hook.example/x",
                        "headers": {"Authorization": "Bearer t",
                                    "X-Custom": "v", "": "dropped"}}})
        assert resp.status_code == 201
        data = resp.get_json()["data"]
        assert data["config"]["url"] == "https://hook.example/x"
        assert data["config"]["headers"]["Authorization"] == "******"
        assert data["config"]["headers"]["X-Custom"] == "v"


class TestChannelPatchDelete:
    def test_patch_missing_404(self, env, client):
        resp = client.patch(f"{env['base']}/channels/999999",
                            headers=env["headers"], json={"name": "n"})
        assert resp.status_code == 404

    def test_patch_non_json_body_400(self, env, client):
        row = _mk_channel("user", env["user"].id, name="nj")
        db.session.commit()
        resp = client.patch(f"{env['base']}/channels/{row.id}",
                            headers=env["headers"],
                            data="not json", content_type="text/plain")
        assert resp.status_code == 400

    def test_delete_missing_404(self, env, client):
        resp = client.delete(f"{env['base']}/channels/999999",
                             headers=env["headers"])
        assert resp.status_code == 404

    def test_patch_user_channel_of_other_forbidden(self, env, client):
        other = _mk_user("ot")
        row = _mk_channel("user", other.id, name="theirs")
        db.session.commit()
        resp = client.patch(f"{env['base']}/channels/{row.id}",
                            headers=env["headers"], json={"name": "n"})
        assert resp.status_code == 403

    def test_patch_org_channel_member_forbidden(self, env, client):
        outsider = _mk_user("mb")
        other_org = _mk_org(outsider, "oo")
        db.session.add(OrganizationMember(
            organization_id=other_org.id, user_id=env["user"].id,
            role=OrganizationRole.MEMBER,
            status=OrganizationMemberStatus.ACTIVE))
        row = _mk_channel("organization", other_org.id, name="orgch")
        db.session.commit()
        resp = client.patch(f"{env['base']}/channels/{row.id}",
                            headers=env["headers"], json={"name": "n"})
        assert resp.status_code == 403

    def test_delete_org_channel_member_forbidden(self, env, client):
        outsider = _mk_user("mb")
        other_org = _mk_org(outsider, "oo")
        db.session.add(OrganizationMember(
            organization_id=other_org.id, user_id=env["user"].id,
            role=OrganizationRole.MEMBER,
            status=OrganizationMemberStatus.ACTIVE))
        row = _mk_channel("organization", other_org.id, name="orgch")
        db.session.commit()
        resp = client.delete(f"{env['base']}/channels/{row.id}",
                             headers=env["headers"])
        assert resp.status_code == 403

    def test_delete_channel_of_unresolvable_scope_403(self, env, client):
        # 渠道挂在别人的 user scope 上：resolve 阶段即拒绝
        other = _mk_user("ot")
        row = _mk_channel("user", other.id, name="theirs")
        db.session.commit()
        resp = client.delete(f"{env['base']}/channels/{row.id}",
                             headers=env["headers"])
        assert resp.status_code == 403

    def test_patch_full_matrix(self, env, client):
        row = _mk_channel("user", env["user"].id, name="ch",
                          channel_type="webhook",
                          config={"url": "https://a.example/x"})
        db.session.commit()
        url = f"{env['base']}/channels/{row.id}"

        assert client.patch(url, headers=env["headers"],
                            json={"name": "  "}).status_code == 400
        assert client.patch(url, headers=env["headers"],
                            json={"events": "created"}).status_code == 400
        assert client.patch(url, headers=env["headers"],
                            json={"events": ["task.danced"]}
                            ).status_code == 400
        assert client.patch(url, headers=env["headers"],
                            json={"config": {"url": "nope"}}
                            ).status_code == 400

        resp = client.patch(
            url, headers=env["headers"],
            json={"name": "renamed", "enabled": "off", "is_default": "on",
                  "events": ["mentioned"],
                  "config": {"url": "https://b.example/y",
                             "headers": {"Authorization": "z"}}})
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["name"] == "renamed"
        assert data["enabled"] is False
        assert data["is_default"] is True
        assert data["events"] == ["task.mentioned"]
        assert data["config"]["headers"]["Authorization"] == "******"
        db.session.expire_all()
        assert db.session.get(NotificationChannel, row.id) \
            .updated_by_user_id == env["user"].id

    def test_patch_preserves_secret_for_feishu_and_dingtalk(self, env, client):
        feishu = _mk_channel("user", env["user"].id, name="fs",
                             channel_type="feishu",
                             config={"webhook_url": "https://f.example/a",
                                     "secret": "keepme"})
        ding = _mk_channel("user", env["user"].id, name="dg",
                           channel_type="dingtalk",
                           config={"webhook_url": "https://d.example/a",
                                   "secret": "holdme"})
        db.session.commit()
        client.patch(f"{env['base']}/channels/{feishu.id}",
                     headers=env["headers"],
                     json={"config": {"webhook_url": "https://f.example/b"}})
        client.patch(f"{env['base']}/channels/{ding.id}",
                     headers=env["headers"],
                     json={"config": {"webhook_url": "https://d.example/b"}})
        db.session.expire_all()
        assert db.session.get(NotificationChannel, feishu.id) \
            .config["secret"] == "keepme"
        assert db.session.get(NotificationChannel, ding.id) \
            .config["secret"] == "holdme"

    def test_delete_user_channel(self, env, client):
        row = _mk_channel("user", env["user"].id, name="gone")
        db.session.commit()
        resp = client.delete(f"{env['base']}/channels/{row.id}",
                             headers=env["headers"])
        assert resp.status_code == 200
        db.session.expire_all()
        assert db.session.get(NotificationChannel, row.id) is None


class TestEffectiveChannels:
    def _get(self, client, env, project_id, query=""):
        return client.get(
            f"{env['base']}/projects/{project_id}/effective-channels{query}",
            headers=env["headers"])

    def test_project_missing_404(self, env, client):
        assert self._get(client, env, 999999).status_code == 404

    def test_stranger_forbidden(self, env, client):
        stranger = _mk_user("st")
        db.session.commit()
        resp = client.get(
            f"{env['base']}/projects/{env['project'].id}"
            f"/effective-channels", headers=_headers_for(stranger))
        assert resp.status_code == 403

    def test_no_channels_resolves_none(self, env, client):
        resp = self._get(client, env, env["project"].id)
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["selected_scope"] == {"scope_type": "none",
                                          "scope_id": None}
        assert data["items"] == []
        assert data["event_type"] is None

    def test_project_level_selected(self, env, client):
        row = _mk_channel("project", env["project"].id, name="p1")
        _mk_channel("project", env["project"].id, name="off", enabled=False)
        db.session.commit()
        resp = self._get(client, env, env["project"].id,
                         "?event_type=task.status_changed")
        data = resp.get_json()["data"]
        assert data["event_type"] == "task.status_changed"
        assert data["selected_scope"] == {
            "scope_type": "project", "scope_id": env["project"].id}
        # events 为空的渠道对任意事件生效
        assert [i["name"] for i in data["items"]] == ["p1"]

    def test_org_fallback_when_project_channels_filtered_out(self, env, client):
        _mk_channel("project", env["project"].id, name="p1",
                    events=["task.created"])
        org_row = _mk_channel("organization", env["org"].id, name="o1",
                              events=["task.status_changed"])
        db.session.commit()
        resp = self._get(client, env, env["project"].id,
                         "?event_type=task.status_changed")
        data = resp.get_json()["data"]
        assert data["selected_scope"] == {
            "scope_type": "organization", "scope_id": env["org"].id}
        assert [i["name"] for i in data["items"]] == ["o1"]

    def test_user_level_fallback(self, env, client):
        _mk_channel("project", env["project"].id, name="p-off",
                    enabled=False)
        user_row = _mk_channel("user", env["user"].id, name="u1",
                               events=["task.created"])
        db.session.commit()
        resp = self._get(client, env, env["project"].id)
        data = resp.get_json()["data"]
        assert data["selected_scope"] == {
            "scope_type": "user", "scope_id": env["user"].id}
        assert [i["name"] for i in data["items"]] == ["u1"]

    def test_project_without_org_skips_org_level(self, env, client):
        lone_project = _mk_project(env["user"], org=None)
        _mk_channel("project", lone_project.id, name="p-off", enabled=False)
        _mk_channel("user", env["user"].id, name="u1")
        db.session.commit()
        resp = self._get(client, env, lone_project.id)
        data = resp.get_json()["data"]
        assert data["selected_scope"] == {
            "scope_type": "user", "scope_id": env["user"].id}


# ─────────────────────────── 包级辅助 ───────────────────────────


class TestPackageHelpers:
    def test_trigger_idempotency_key_stable_and_scoped(self, env):
        from api.agent_automation import make_trigger_idempotency_key
        t1 = AgentTrigger(workspace_id=env["org"].id,
                          agent_id=env["agent"].id, name="a",
                          trigger_type="task_event",
                          task_event_types=["created"],
                          created_by=env["user"].email)
        t2 = AgentTrigger(workspace_id=env["org"].id,
                          agent_id=env["agent"].id, name="b",
                          trigger_type="task_event",
                          task_event_types=["created"],
                          created_by=env["user"].email)
        db.session.add_all([t1, t2])
        db.session.commit()
        k1 = make_trigger_idempotency_key(t1, "manual", {"x": 1})
        k1_again = make_trigger_idempotency_key(t1, "manual", {"x": 1})
        k2 = make_trigger_idempotency_key(t2, "manual", {"x": 1})
        assert k1 == k1_again
        assert k1 != k2
        assert k1.startswith(f"trg:{t1.id}:")
        digest_part = k1.split(":")[-1]
        assert len(digest_part) == 32

    def test_dead_channels_module_removed(self):
        import os
        import api.agent_automation as pkg
        pkg_dir = os.path.dirname(pkg.__file__)
        assert not os.path.exists(
            os.path.join(pkg_dir, "routes_channels.py"))

    def test_routes_are_registered_on_app(self):
        # channels.py 独立蓝图 + agent_automation 包路由都必须真实在册
        from app import create_app
        app = create_app("testing")
        rules = {str(r) for r in app.url_map.iter_rules()}
        assert any("/runner-config" in r for r in rules)
        assert any("/triggers" in r for r in rules)
        assert any(r.endswith("/runs") for r in rules)
        assert any("/users/<int:user_id>/channels" in r for r in rules)
        assert any("/channels/<int:channel_id>" in r for r in rules)
        assert any("effective-channels" in r for r in rules)
