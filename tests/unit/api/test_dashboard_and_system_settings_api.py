"""仪表盘与系统设置 API（api/dashboard.py + api/system_settings.py）单元回归。

覆盖：仪表盘双层缓存（redis + 进程内回退 + stale 降级读与后台异步
刷新去重）、 owned/participated 双范围统计、大数据集降级（阈值内才
统计 AI 执行数与最近任务）、可访问组织聚合（owner/member 角色解析、
无绑定回退）、组织 Agent 活跃度（7 天窗口、outer join 零尝试）、
热力图与活跃摘要（连续活跃天数、最活跃日）、各端点 500 兜底；
系统设置：管理员门禁、LLM 配置读写（api_key 加密落库、非管理员掩码、
部分更新合并）、通用设置 get/set、连接测试五种 provider 分支与
超时/连接错误/未知异常矩阵。
"""

import base64
import json
import uuid
from datetime import date, datetime, timedelta

import pytest
from flask_jwt_extended import create_access_token

from models import (
    Agent,
    AgentStatus,
    AgentTaskAttempt,
    AgentTaskAttemptState,
    Organization,
    OrganizationMember,
    OrganizationMemberRole,
    OrganizationMemberStatus,
    OrganizationRole,
    OrganizationRoleDefinition,
    OrganizationAgentMember,
    OrganizationAgentMemberStatus,
    Project,
    ProjectMember,
    ProjectMemberStatus,
    Task,
    User,
    UserActivity,
    db,
)


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

    # 仪表盘缓存：redis 两函数替换为进程内 store，防真实 Redis 污染
    from api import dashboard as dash
    store = {}
    monkeypatch.setattr(dash, "redis_get_json", lambda k: store.get(k))
    monkeypatch.setattr(
        dash, "redis_set_json",
        lambda k, v, ttl=None: store.update({k: v}))
    dash.dashboard_fallback_cache.clear()
    dash._dashboard_stats_refreshing_users.clear()

    # LLM 配置加密管理器替换为可逆假实现
    from models.system_settings import SystemSettings

    class _FakeEncryptor:
        def encrypt(self, plaintext):
            token = base64.b64encode(plaintext.encode()).decode()
            return f"enc:{token}", "key-v1"

        def decrypt(self, ciphertext):
            assert ciphertext.startswith("enc:")
            return base64.b64decode(ciphertext[4:]).decode()

    monkeypatch.setattr(
        SystemSettings, "_get_encryption_manager",
        classmethod(lambda cls: _FakeEncryptor()))

    yield app
    db.session.remove()
    db.drop_all()
    dash.dashboard_fallback_cache.clear()
    dash._dashboard_stats_refreshing_users.clear()
    store.clear()
    ctx.pop()


@pytest.fixture
def client(_isolated_app):
    return _isolated_app.test_client()


def _uh(prefix="du", role=None):
    u = User(username=f"{prefix}_{uuid.uuid4().hex[:8]}",
             email=f"{prefix}_{uuid.uuid4().hex[:6]}@t.io",
             role=role) if role else User(
        username=f"{prefix}_{uuid.uuid4().hex[:8]}",
        email=f"{prefix}_{uuid.uuid4().hex[:6]}@t.io")
    db.session.add(u)
    db.session.flush()
    return u


def _headers_for(user):
    return {"Authorization": f"Bearer {create_access_token(identity=str(user.id))}"}


def _mk_project(owner, org=None, name=None):
    row = Project(name=name or f"p_{uuid.uuid4().hex[:6]}",
                  owner_id=owner.id,
                  organization_id=org.id if org else None)
    db.session.add(row)
    db.session.flush()
    return row


def _mk_task(owner, project, status="TODO", is_ai=False, **kw):
    import itertools
    if not hasattr(_mk_task, "_seq"):
        _mk_task._seq = itertools.count(70_000_000)
    row = Task(id=next(_mk_task._seq), title=f"t_{uuid.uuid4().hex[:6]}",
               content="c", status=status, priority="MEDIUM",
               project_id=project.id, owner_id=owner.id,
               is_ai_task=is_ai, **kw)
    db.session.add(row)
    db.session.flush()
    return row


def _mk_activity(user, day, count=3):
    row = UserActivity(user_id=user.id, activity_date=day,
                       total_activity_count=count,
                       task_created_count=1, task_updated_count=1,
                       task_status_changed_count=1, task_completed_count=0,
                       activity_level=2)
    db.session.add(row)
    db.session.flush()
    return row


@pytest.fixture
def env(_isolated_app):
    user = _uh("ow")
    org = Organization(name=f"o_{uuid.uuid4().hex[:6]}",
                       slug=f"o_{uuid.uuid4().hex[:6]}", owner_id=user.id)
    db.session.add(org)
    db.session.flush()
    project = _mk_project(user, org=org)
    task = _mk_task(user, project)
    db.session.commit()
    return {
        "app": _isolated_app, "user": user, "org": org,
        "project": project, "task": task,
        "headers": _headers_for(user),
        "base": "/todo-for-ai/api/v1",
    }


# ─────────────────────────── 仪表盘：缓存层 ───────────────────────────


class TestDashboardCache:
    def test_miss_returns_none(self):
        from api import dashboard as dash
        value, stale = dash._dashboard_cache_get("k-miss", 120)
        assert value is None and stale is False

    def test_redis_hit_is_fresh(self):
        from api import dashboard as dash
        dash.redis_set_json("dashboard:k1", {"v": 1}, 60)
        value, stale = dash._dashboard_cache_get("k1", 120)
        assert value == {"v": 1} and stale is False

    def test_fallback_fresh_hit_and_expiry(self, monkeypatch):
        from api import dashboard as dash
        dash._dashboard_cache_set("k2", {"v": 2}, 120)
        # 屏蔽 redis 命中，专门验证进程内回退缓存
        monkeypatch.setattr(dash, "redis_get_json", lambda k: None)
        value, stale = dash._dashboard_cache_get("k2", 120)
        assert value == {"v": 2} and stale is False
        # 超出 fresh ttl 且无 stale 配置 → miss
        dash.dashboard_fallback_cache["k3"] = {
            "cached_at": datetime.utcnow().timestamp() - 999, "value": 3}
        value, stale = dash._dashboard_cache_get("k3", 120)
        assert value is None

    def test_stale_fallback_used_when_fresh_expired(self):
        from api import dashboard as dash
        dash.dashboard_fallback_cache["k4"] = {
            "cached_at": datetime.utcnow().timestamp() - 999, "value": 4}
        dash.dashboard_fallback_cache["k4:stale"] = {
            "cached_at": datetime.utcnow().timestamp(), "value": 4}
        value, stale = dash._dashboard_cache_get("k4", 120, 3600)
        assert value == 4 and stale is True

    def test_stale_redis_used_when_fresh_missing(self):
        from api import dashboard as dash
        dash.redis_set_json("dashboard:k5:stale", {"v": 5}, 3600)
        value, stale = dash._dashboard_cache_get("k5", 120, 3600)
        assert value == {"v": 5} and stale is True

    def test_set_writes_stale_copy(self):
        from api import dashboard as dash
        dash._dashboard_cache_set("k6", {"v": 6}, 120, 3600)
        assert dash.redis_get_json("dashboard:k6:stale") == {"v": 6}
        assert "k6:stale" in dash.dashboard_fallback_cache


class TestAsyncRefresh:
    def test_dedupes_concurrent_refresh(self, env, monkeypatch):
        from api import dashboard as dash
        started = []

        class _FakeThread:
            def __init__(self, target, args, daemon=None):
                started.append(args)

            def start(self):
                pass

        monkeypatch.setattr(dash.threading, "Thread", _FakeThread)
        dash._trigger_dashboard_stats_async_refresh(1, "ck")
        dash._trigger_dashboard_stats_async_refresh(1, "ck")
        assert len(started) == 1
        with dash._dashboard_stats_refresh_lock:
            dash._dashboard_stats_refreshing_users.discard(1)

    def test_background_success_sets_cache(self, env, monkeypatch):
        from api import dashboard as dash
        dash._refresh_dashboard_stats_in_background(
            env["app"], env["user"].id, "ck-success")
        assert "ck-success" in dash.dashboard_fallback_cache
        assert env["user"].id not in dash._dashboard_stats_refreshing_users

    def test_background_failure_clears_inflight(self, env, monkeypatch):
        from api import dashboard as dash
        monkeypatch.setattr(dash, "_build_dashboard_stats",
                            lambda uid: 1 / 0)
        dash._dashboard_stats_refreshing_users.add(env["user"].id)
        dash._refresh_dashboard_stats_in_background(
            env["app"], env["user"].id, "ck-fail")
        assert env["user"].id not in dash._dashboard_stats_refreshing_users


# ─────────────────────────── 仪表盘：统计装配 ───────────────────────────


class TestScopeAndOrgStats:
    def test_owned_and_participated_scopes(self, env):
        from api.dashboard import _build_dashboard_stats
        member_user = _uh("mb")
        db.session.add(ProjectMember(
            project_id=env["project"].id, user_id=member_user.id,
            status=ProjectMemberStatus.ACTIVE))
        _mk_task(env["user"], env["project"], status="IN_PROGRESS",
                 is_ai=True)
        _mk_task(env["user"], env["project"], status="DONE", is_ai=True)
        db.session.commit()

        stats = _build_dashboard_stats(env["user"].id)
        assert stats["projects"]["total"] == 1
        assert stats["projects"]["active"] == 1
        assert stats["tasks"]["total"] == 3
        assert stats["tasks"]["todo"] == 1
        assert stats["tasks"]["in_progress"] == 1
        assert stats["tasks"]["done"] == 1
        assert stats["tasks"]["ai_executing"] == 1  # 仅未完成 AI 任务
        assert stats["scopes"]["participated"]["projects"]["total"] == 1
        assert len(stats["recent_tasks"]) == 3
        assert len(stats["recent_projects"]) == 1

    def test_large_dataset_skips_ai_count_and_recent(self, env, monkeypatch):
        from api import dashboard as dash
        monkeypatch.setattr(dash, "LARGE_DATASET_THRESHOLD", 0)
        _mk_task(env["user"], env["project"], is_ai=True)
        db.session.commit()
        stats = dash._build_dashboard_stats(env["user"].id)
        assert stats["tasks"]["ai_executing"] == 0  # 超阈值跳过计数
        assert stats["recent_tasks"] == []  # 超阈值跳过最近任务

    def test_accessible_organizations_role_resolution(self, env):
        from api.dashboard import _get_accessible_organizations
        admin_user = _uh("ad")
        legacy_user = _uh("lg")
        org2 = Organization(name=f"o2_{uuid.uuid4().hex[:6]}",
                            slug=f"o2_{uuid.uuid4().hex[:6]}",
                            owner_id=admin_user.id)
        org3 = Organization(name=f"o3_{uuid.uuid4().hex[:6]}",
                            slug=f"o3_{uuid.uuid4().hex[:6]}",
                            owner_id=admin_user.id)
        db.session.add_all([org2, org3])
        db.session.flush()
        m1 = OrganizationMember(organization_id=org2.id,
                                user_id=env["user"].id,
                                status=OrganizationMemberStatus.ACTIVE)
        m2 = OrganizationMember(organization_id=org3.id,
                                user_id=env["user"].id,
                                status=OrganizationMemberStatus.ACTIVE)
        # env 用户同时是 org4 的 owner 和 ACTIVE 成员：成员行应被跳过
        org4 = Organization(name=f"o4_{uuid.uuid4().hex[:6]}",
                            slug=f"o4_{uuid.uuid4().hex[:6]}",
                            owner_id=env["user"].id)
        db.session.add(org4)
        db.session.flush()
        m3 = OrganizationMember(organization_id=org4.id,
                                user_id=env["user"].id,
                                status=OrganizationMemberStatus.ACTIVE)
        # org5：仅绑非优先级自定义角色 → 取第一个 key
        org5 = Organization(name=f"o5_{uuid.uuid4().hex[:6]}",
                            slug=f"o5_{uuid.uuid4().hex[:6]}",
                            owner_id=admin_user.id)
        db.session.add(org5)
        db.session.flush()
        m4 = OrganizationMember(organization_id=org5.id,
                                user_id=env["user"].id,
                                status=OrganizationMemberStatus.ACTIVE)
        db.session.add_all([m1, m2, m3, m4])
        db.session.flush()
        admin_role = OrganizationRoleDefinition(
            organization_id=org2.id, key="admin", name="Admin",
            is_system=True, is_active=True)
        empty_role = OrganizationRoleDefinition(
            organization_id=org2.id, key="", name="Empty",
            is_system=False, is_active=True)
        custom_role = OrganizationRoleDefinition(
            organization_id=org5.id, key="custom_key", name="Custom",
            is_system=False, is_active=True)
        db.session.add_all([admin_role, empty_role, custom_role])
        db.session.flush()
        db.session.add(OrganizationMemberRole(
            organization_id=org2.id, member_id=m1.id, role_id=admin_role.id))
        # m2 只绑了空 key 角色 → 回退 None
        db.session.add(OrganizationMemberRole(
            organization_id=org3.id, member_id=m2.id, role_id=empty_role.id))
        db.session.add(OrganizationMemberRole(
            organization_id=org5.id, member_id=m4.id,
            role_id=custom_role.id))
        db.session.commit()

        org_map = _get_accessible_organizations(env["user"].id)
        assert org_map[env["org"].id]["my_role"] == "owner"
        assert org_map[org2.id]["my_role"] == "admin"
        assert org_map[org3.id]["my_role"] is None
        assert org_map[org4.id]["my_role"] == "owner"  # owner 优先于成员行
        assert org_map[org5.id]["my_role"] == "custom_key"

    def test_org_agent_stats_empty(self):
        from api.dashboard import _build_organization_agent_stats
        stats = _build_organization_agent_stats({})
        assert stats["summary"] == {"total": 0, "total_agents": 0,
                                    "active_agents_7d": 0}
        assert stats["top_organizations"] == []

    def test_org_agent_stats_window_and_sorting(self, env):
        from api.dashboard import (
            _build_organization_agent_stats,
            _get_accessible_organizations,
        )
        other_owner = _uh("oo")
        org2 = Organization(name=f"busy_{uuid.uuid4().hex[:6]}",
                            slug=f"b_{uuid.uuid4().hex[:6]}",
                            owner_id=other_owner.id)
        db.session.add(org2)
        db.session.flush()
        m = OrganizationMember(organization_id=org2.id,
                               user_id=env["user"].id,
                               status=OrganizationMemberStatus.ACTIVE)
        db.session.add(m)

        agent_a = Agent(workspace_id=org2.id, owner_id=other_owner.id,
                        creator_user_id=other_owner.id,
                        name=f"a_{uuid.uuid4().hex[:6]}",
                        status=AgentStatus.ACTIVE)
        agent_b = Agent(workspace_id=org2.id, owner_id=other_owner.id,
                        creator_user_id=other_owner.id,
                        name=f"b_{uuid.uuid4().hex[:6]}",
                        status=AgentStatus.ACTIVE)
        db.session.add_all([agent_a, agent_b])
        db.session.flush()
        db.session.add(OrganizationAgentMember(
            organization_id=org2.id, agent_id=agent_a.id,
            status=OrganizationAgentMemberStatus.ACTIVE))
        db.session.add(OrganizationAgentMember(
            organization_id=org2.id, agent_id=agent_b.id,
            status=OrganizationAgentMemberStatus.REMOVED))  # 已移除不计
        db.session.add(OrganizationAgentMember(
            organization_id=env["org"].id, agent_id=agent_a.id,
            status=OrganizationAgentMemberStatus.INVITED))

        task = _mk_task(env["user"], env["project"])
        now = datetime.utcnow()
        db.session.add(AgentTaskAttempt(
            attempt_id=f"at_{uuid.uuid4().hex[:6]}", task_id=task.id,
            agent_id=agent_a.id, workspace_id=org2.id,
            state=AgentTaskAttemptState.ACTIVE,
            lease_id="l1", started_at=now))
        db.session.add(AgentTaskAttempt(
            attempt_id=f"at_{uuid.uuid4().hex[:6]}", task_id=task.id,
            agent_id=agent_a.id, workspace_id=org2.id,
            state=AgentTaskAttemptState.CREATED,
            lease_id="l2", started_at=now - timedelta(days=30)))
        db.session.commit()

        org_map = _get_accessible_organizations(env["user"].id)
        stats = _build_organization_agent_stats(org_map)
        assert stats["summary"]["total"] >= 2
        assert stats["summary"]["total_agents"] == 2  # REMOVED 不计
        top = {i["organization_id"]: i for i in stats["top_organizations"]}
        assert top[org2.id]["total_agents"] == 1
        assert top[org2.id]["active_agents_7d"] == 1  # CREATED 30 天前不算
        assert top[org2.id]["last_agent_activity_at"] is not None


# ─────────────────────────── 仪表盘：端点 ───────────────────────────


class TestDashboardEndpoints:
    def test_stats_fresh_build_then_cache_hit(self, env, client, monkeypatch):
        calls = []
        from api import dashboard as dash
        real_build = dash._build_dashboard_stats

        def counting_build(uid):
            calls.append(uid)
            return real_build(uid)
        monkeypatch.setattr(dash, "_build_dashboard_stats", counting_build)

        first = client.get(f"{env['base']}/dashboard/stats", headers=env["headers"])
        assert first.status_code == 200
        assert first.get_json()["data"]["projects"]["total"] == 1
        second = client.get(f"{env['base']}/dashboard/stats", headers=env["headers"])
        assert second.status_code == 200
        assert len(calls) == 1  # 第二次命中缓存

    def test_stats_stale_serves_and_triggers_refresh(self, env, client,
                                                     monkeypatch):
        from api import dashboard as dash
        triggered = []
        monkeypatch.setattr(
            dash, "_trigger_dashboard_stats_async_refresh",
            lambda uid, ck: triggered.append((uid, ck)))
        cache_key = f"user:{env['user'].id}:stats:v2"
        stale_payload = {"projects": {"total": 999, "active": 0},
                         "tasks": {"total": 0}}
        dash.dashboard_fallback_cache[cache_key] = {
            "cached_at": datetime.utcnow().timestamp() - 999,
            "value": stale_payload}
        dash.dashboard_fallback_cache[f"{cache_key}:stale"] = {
            "cached_at": datetime.utcnow().timestamp(),
            "value": stale_payload}

        resp = client.get(f"{env['base']}/dashboard/stats", headers=env["headers"])
        assert resp.status_code == 200
        assert resp.get_json()["data"]["projects"]["total"] == 999
        assert triggered == [(env["user"].id, cache_key)]

    def test_stats_build_failure_500(self, env, client, monkeypatch):
        from api import dashboard as dash
        def boom(uid):
            raise RuntimeError("stats down")
        monkeypatch.setattr(dash, "_build_dashboard_stats", boom)
        resp = client.get(f"{env['base']}/dashboard/stats", headers=env["headers"])
        assert resp.status_code == 500

    def test_heatmap_build_and_cache(self, env, client, monkeypatch):
        calls = []
        monkeypatch.setattr(
            UserActivity, "get_user_activity_heatmap",
            classmethod(lambda cls, uid, days=365: calls.append(days)
                        or [days]))
        first = client.get(f"{env['base']}/dashboard/activity-heatmap",
                           headers=env["headers"])
        assert first.status_code == 200
        assert first.get_json()["data"]["heatmap_data"] == [365]
        second = client.get(f"{env['base']}/dashboard/activity-heatmap",
                            headers=env["headers"])
        assert second.get_json()["data"]["heatmap_data"] == [365]
        assert len(calls) == 1  # 第二次走缓存

    def test_heatmap_failure_500(self, env, client, monkeypatch):
        monkeypatch.setattr(
            UserActivity, "get_user_activity_heatmap",
            classmethod(lambda cls, uid, days=365: 1 / 0))
        resp = client.get(f"{env['base']}/dashboard/activity-heatmap",
                          headers=env["headers"])
        assert resp.status_code == 500

    def test_summary_streak_and_most_active(self, env, client):
        today = date.today()
        _mk_activity(env["user"], today, count=9)
        _mk_activity(env["user"], today - timedelta(days=1), count=2)
        _mk_activity(env["user"], today - timedelta(days=3), count=1)
        db.session.commit()
        resp = client.get(f"{env['base']}/dashboard/activity-summary",
                          headers=env["headers"])
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["consecutive_active_days"] == 2  # 昨天有、前天断
        assert data["most_active_day"]["count"] == 9
        assert data["stats_7d"]["total_activities"] == 12
        # 第二次命中缓存
        again = client.get(f"{env['base']}/dashboard/activity-summary",
                           headers=env["headers"])
        assert again.get_json()["data"]["most_active_day"]["count"] == 9

    def test_summary_failure_500(self, env, client, monkeypatch):
        from api import dashboard as dash
        monkeypatch.setattr(
            dash.UserActivity, "get_user_activity_stats",
            classmethod(lambda cls, uid, days=30: 1 / 0))
        resp = client.get(f"{env['base']}/dashboard/activity-summary",
                          headers=env["headers"])
        assert resp.status_code == 500

    def test_consecutive_days_cap(self, env, monkeypatch):
        from api import dashboard as dash

        class _AlwaysActive:
            total_activity_count = 1

        class _StubQuery:
            def filter_by(self, **kw):
                return self

            def first(self):
                return _AlwaysActive()

        class _StubUserActivity:
            query = _StubQuery()

        monkeypatch.setattr(dash, "UserActivity", _StubUserActivity)
        assert dash._get_consecutive_active_days(1) == 365

    def test_consecutive_days_direct_real_rows(self, env):
        # 直调真实函数：今天+昨天活跃、前天断档 → 走 else-break 返回 2
        from api.dashboard import _get_consecutive_active_days
        today = date.today()
        _mk_activity(env["user"], today, count=1)
        _mk_activity(env["user"], today - timedelta(days=1), count=1)
        _mk_activity(env["user"], today - timedelta(days=3), count=1)
        db.session.commit()
        assert _get_consecutive_active_days(env["user"].id) == 2
        # 完全无活动 → 首查即 else-break → 0
        other = _uh("na")
        db.session.commit()
        assert _get_consecutive_active_days(other.id) == 0

    def test_consecutive_days_query_failure_returns_zero(self, env,
                                                         monkeypatch):
        from api import dashboard as dash

        class _BoomQuery:
            def filter_by(self, **kw):
                raise RuntimeError("db down")

        class _StubUserActivity:
            query = _BoomQuery()

        monkeypatch.setattr(dash, "UserActivity", _StubUserActivity)
        assert dash._get_consecutive_active_days(1) == 0


# ─────────────────────────── 系统设置：LLM 配置 ───────────────────────────


@pytest.fixture
def admin_env(_isolated_app):
    admin = _uh("ad", role="ADMIN")
    user = _uh("nu")
    db.session.commit()
    return {
        "admin": admin, "user": user,
        "admin_headers": _headers_for(admin),
        "user_headers": _headers_for(user),
        "base": "/todo-for-ai/api/v1",
    }


class TestLlmConfigEndpoints:
    def test_non_admin_gets_masked_key(self, admin_env, client):
        from models.system_settings import SystemSettings
        SystemSettings.set_llm_config(
            {"provider": "openai", "api_base": "https://a.b",
             "api_key": "secret", "model": "gpt-x"})
        db.session.commit()
        resp = client.get(f"{admin_env['base']}/system-settings/llm-config",
                          headers=admin_env["user_headers"])
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["api_key"] == "***hidden***"
        assert data["model"] == "gpt-x"

    def test_admin_gets_full_config(self, admin_env, client):
        from models.system_settings import SystemSettings
        SystemSettings.set_llm_config(
            {"provider": "openai", "api_base": "https://a.b",
             "api_key": "secret", "model": "gpt-x"})
        db.session.commit()
        resp = client.get(f"{admin_env['base']}/system-settings/llm-config",
                          headers=admin_env["admin_headers"])
        assert resp.get_json()["data"]["api_key"] == "secret"

    def test_get_without_config_returns_defaults(self, admin_env, client):
        resp = client.get(f"{admin_env['base']}/system-settings/llm-config",
                          headers=admin_env["admin_headers"])
        data = resp.get_json()["data"]
        assert data["provider"] == "openai"
        assert data["api_key"] == ""

    def test_update_requires_admin(self, admin_env, client):
        resp = client.put(f"{admin_env['base']}/system-settings/llm-config",
                          headers=admin_env["user_headers"],
                          json={"provider": "x", "api_base": "y",
                                "model": "z"})
        assert resp.status_code == 403

    def test_update_requires_json(self, admin_env, client):
        resp = client.put(f"{admin_env['base']}/system-settings/llm-config",
                          headers=admin_env["admin_headers"], data="junk",
                          content_type="text/plain")
        assert resp.status_code == 400

    def test_update_requires_fields(self, admin_env, client):
        resp = client.put(f"{admin_env['base']}/system-settings/llm-config",
                          headers=admin_env["admin_headers"],
                          json={"provider": "x"})
        assert resp.status_code == 400
        assert "api_base" in resp.get_json()["message"]

    def test_update_merges_with_existing(self, admin_env, client):
        from models.system_settings import SystemSettings
        SystemSettings.set_llm_config(
            {"provider": "openai", "api_base": "https://old",
             "api_key": "keepme", "model": "old-model"})
        db.session.commit()
        resp = client.put(f"{admin_env['base']}/system-settings/llm-config",
                          headers=admin_env["admin_headers"],
                          json={"provider": "anthropic",
                                "api_base": "https://new",
                                "model": "claude"})
        assert resp.status_code == 200
        stored = SystemSettings.get_llm_config()
        assert stored["provider"] == "anthropic"
        assert stored["api_key"] == "keepme"  # 部分更新保留旧密钥

    def test_get_failure_500(self, admin_env, client, monkeypatch):
        from models.system_settings import SystemSettings
        monkeypatch.setattr(
            SystemSettings, "get_llm_config",
            classmethod(lambda cls: 1 / 0))
        resp = client.get(f"{admin_env['base']}/system-settings/llm-config",
                          headers=admin_env["admin_headers"])
        assert resp.status_code == 500


# ─────────────────────────── 系统设置：通用设置 ───────────────────────────


class TestGenericSettingsEndpoints:
    def test_get_all_requires_admin(self, admin_env, client):
        resp = client.get(f"{admin_env['base']}/system-settings",
                          headers=admin_env["user_headers"])
        assert resp.status_code == 403

    def test_get_all_masks_encrypted_values(self, admin_env, client):
        from models.system_settings import SystemSettings
        SystemSettings.set_llm_config({"provider": "openai"})
        db.session.commit()
        resp = client.get(f"{admin_env['base']}/system-settings",
                          headers=admin_env["admin_headers"])
        data = resp.get_json()["data"]
        assert data["llm_config"]["value"] == "[encrypted]"

    def test_get_setting_404_and_found(self, admin_env, client):
        base = f"{admin_env['base']}/system-settings"
        assert client.get(f"{base}/nope",
                          headers=admin_env["admin_headers"]
                          ).status_code == 404
        assert client.get(f"{base}/nope",
                          headers=admin_env["user_headers"]
                          ).status_code == 403
        from models.system_settings import SystemSettings
        SystemSettings.set_setting("feature_flag", {"on": True})
        db.session.commit()
        resp = client.get(f"{base}/feature_flag",
                          headers=admin_env["admin_headers"])
        assert resp.get_json()["data"] == {"on": True}

    def test_put_setting(self, admin_env, client):
        base = f"{admin_env['base']}/system-settings/maintenance"
        assert client.put(base, headers=admin_env["user_headers"],
                          json={"value": 1}).status_code == 403
        assert client.put(base, headers=admin_env["admin_headers"],
                          data="junk",
                          content_type="text/plain").status_code == 400
        resp = client.put(base, headers=admin_env["admin_headers"],
                          json={"value": {"enabled": True},
                                "description": "维护开关"})
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["value"] == {"enabled": True}
        assert data["description"] == "维护开关"
        assert data["updated_by"] == admin_env["admin"].id

    def test_endpoint_failure_500_matrix(self, admin_env, client,
                                         monkeypatch):
        from models.system_settings import SystemSettings
        base = f"{admin_env['base']}/system-settings"

        monkeypatch.setattr(
            SystemSettings, "get_all_settings",
            classmethod(lambda cls, include_encrypted=False: 1 / 0))
        assert client.get(base,
                          headers=admin_env["admin_headers"]
                          ).status_code == 500

        monkeypatch.setattr(
            SystemSettings, "get_setting",
            classmethod(lambda cls, key, default=None: 1 / 0))
        assert client.get(f"{base}/anykey",
                          headers=admin_env["admin_headers"]
                          ).status_code == 500

        monkeypatch.setattr(
            SystemSettings, "set_setting",
            classmethod(lambda cls, *a, **kw: 1 / 0))
        assert client.put(f"{base}/anykey",
                          headers=admin_env["admin_headers"],
                          json={"value": 1}).status_code == 500


    def test_update_failure_500(self, admin_env, client, monkeypatch):
        from models.system_settings import SystemSettings
        monkeypatch.setattr(
            SystemSettings, "set_llm_config",
            classmethod(lambda cls, config, updated_by=None: 1 / 0))
        resp = client.put(f"{admin_env['base']}/system-settings/llm-config",
                          headers=admin_env["admin_headers"],
                          json={"provider": "p", "api_base": "b",
                                "model": "m"})
        assert resp.status_code == 500

    def test_test_llm_endpoint_failure_500(self, admin_env, client,
                                           monkeypatch):
        from api import system_settings as ss
        monkeypatch.setattr(ss, "test_llm_api_connection",
                            lambda config: 1 / 0)
        resp = client.post(
            f"{admin_env['base']}/system-settings/test-llm",
            headers=admin_env["admin_headers"],
            json={"provider": "openai", "api_base": "https://a.b",
                  "api_key": "k"})
        assert resp.status_code == 500


# ─────────────────────────── 连接测试 ───────────────────────────


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class TestLlmConnectionTest:
    def _url(self, env):
        return f"{env['base']}/system-settings/test-llm"

    def test_requires_admin(self, admin_env, client):
        resp = client.post(self._url(admin_env),
                           headers=admin_env["user_headers"], json={})
        assert resp.status_code == 403

    def test_requires_json(self, admin_env, client):
        resp = client.post(self._url(admin_env),
                           headers=admin_env["admin_headers"], data="junk",
                           content_type="text/plain")
        assert resp.status_code == 400

    def test_explicit_config_openai_success(self, admin_env, client,
                                            monkeypatch):
        import requests
        monkeypatch.setattr(
            requests, "get",
            lambda *a, **kw: _FakeResponse(200))
        resp = client.post(
            self._url(admin_env), headers=admin_env["admin_headers"],
            json={"provider": "openai", "api_base": "https://a.b",
                  "api_key": "k"})
        assert resp.status_code == 200
        assert "OpenAI" in resp.get_json()["data"]["message"]

    def test_stored_config_fallback(self, admin_env, client, monkeypatch):
        from models.system_settings import SystemSettings
        SystemSettings.set_llm_config(
            {"provider": "openai", "api_base": "https://stored",
             "api_key": "k", "model": "m"})
        db.session.commit()
        seen = {}
        def fake_get(url, **kw):
            seen["url"] = url
            return _FakeResponse(500)
        import requests
        monkeypatch.setattr(requests, "get", fake_get)
        resp = client.post(self._url(admin_env),
                           headers=admin_env["admin_headers"], json={})
        assert resp.status_code == 400
        assert seen["url"].startswith("https://stored")

    def test_missing_credentials_rejected(self, admin_env, client):
        resp = client.post(self._url(admin_env),
                           headers=admin_env["admin_headers"],
                           json={"provider": "openai"})
        assert resp.status_code == 400
        assert "API base URL" in resp.get_json()["message"]

    def test_ollama_without_api_key_allowed(self, admin_env, client,
                                            monkeypatch):
        # ollama 本地服务无需 key：不应被通用凭据守卫拒绝（历史 bug）
        import requests
        monkeypatch.setattr(
            requests, "get",
            lambda *a, **kw: _FakeResponse(200, {"models": [{"n": 1},
                                                            {"n": 2}]}))
        resp = client.post(
            self._url(admin_env), headers=admin_env["admin_headers"],
            json={"provider": "ollama", "api_base": "http://localhost:11434",
                  "api_key": ""})
        assert resp.status_code == 200
        assert "2" in resp.get_json()["data"]["message"]

    def test_ollama_non_200(self, admin_env, client, monkeypatch):
        import requests
        monkeypatch.setattr(requests, "get",
                            lambda *a, **kw: _FakeResponse(503))
        resp = client.post(
            self._url(admin_env), headers=admin_env["admin_headers"],
            json={"provider": "ollama", "api_base": "http://localhost:11434",
                  "api_key": ""})
        assert resp.status_code == 400
        assert "Ollama returned status 503" in resp.get_json()["message"]

    def test_azure_anthropic_non_200(self, admin_env, client, monkeypatch):
        import requests
        monkeypatch.setattr(requests, "get",
                            lambda *a, **kw: _FakeResponse(403))
        for provider, base in (("azure", "https://az"),
                               ("anthropic", "https://an")):
            resp = client.post(
                self._url(admin_env), headers=admin_env["admin_headers"],
                json={"provider": provider, "api_base": base,
                      "api_key": "k"})
            assert resp.status_code == 400
            assert "403" in resp.get_json()["message"]

    def test_azure_and_anthropic_headers(self, admin_env, client,
                                         monkeypatch):
        import requests
        captured = {}

        def fake_get(url, headers=None, **kw):
            captured[url] = headers or {}
            return _FakeResponse(200)
        monkeypatch.setattr(requests, "get", fake_get)

        resp = client.post(
            self._url(admin_env), headers=admin_env["admin_headers"],
            json={"provider": "azure", "api_base": "https://az",
                  "api_key": "az-key"})
        assert resp.status_code == 200
        assert captured["https://az/models"]["api-key"] == "az-key"

        resp = client.post(
            self._url(admin_env), headers=admin_env["admin_headers"],
            json={"provider": "anthropic", "api_base": "https://an",
                  "api_key": "an-key"})
        assert resp.status_code == 200
        assert captured["https://an/models"]["x-api-key"] == "an-key"

    def test_ollama_lists_models(self, admin_env, client, monkeypatch):
        import requests
        monkeypatch.setattr(
            requests, "get",
            lambda *a, **kw: _FakeResponse(200, {"models": [{"n": 1},
                                                            {"n": 2}]}))
        resp = client.post(
            self._url(admin_env), headers=admin_env["admin_headers"],
            json={"provider": "ollama", "api_base": "http://localhost:11434",
                  "api_key": ""})
        assert resp.status_code == 200
        assert "2" in resp.get_json()["data"]["message"]

    def test_unsupported_provider(self, admin_env, client):
        resp = client.post(
            self._url(admin_env), headers=admin_env["admin_headers"],
            json={"provider": "carrier-pigeon", "api_base": "https://x",
                  "api_key": "k"})
        assert resp.status_code == 400
        assert "Unsupported provider" in resp.get_json()["message"]

    def test_network_failures_matrix(self, admin_env, client, monkeypatch):
        import requests
        from api.system_settings import test_llm_api_connection
        config = {"provider": "openai", "api_base": "https://a.b",
                  "api_key": "k"}

        monkeypatch.setattr(requests, "get",
                            lambda *a, **kw: (_ for _ in ()).throw(
                                requests.exceptions.Timeout()))
        assert test_llm_api_connection(config) == {
            "success": False, "error": "Connection timeout"}

        monkeypatch.setattr(requests, "get",
                            lambda *a, **kw: (_ for _ in ()).throw(
                                requests.exceptions.ConnectionError()))
        out = test_llm_api_connection(config)
        assert out["success"] is False and "Connection error" in out["error"]

        monkeypatch.setattr(requests, "get",
                            lambda *a, **kw: (_ for _ in ()).throw(
                                RuntimeError("boom")))
        out = test_llm_api_connection(config)
        assert out == {"success": False, "error": "boom"}

    def test_non_200_status_reports_code(self, admin_env, client,
                                         monkeypatch):
        import requests
        monkeypatch.setattr(requests, "get",
                            lambda *a, **kw: _FakeResponse(401))
        resp = client.post(
            self._url(admin_env), headers=admin_env["admin_headers"],
            json={"provider": "openai", "api_base": "https://a.b",
                  "api_key": "k"})
        assert resp.status_code == 400
        assert "401" in resp.get_json()["message"]
