"""上下文规则 API（api/context_rules.py）单元回归。

覆盖：列表全部分支（项目权限/scope/is_active/apply_to_*/搜索/排序/分页/
项目信息批量装配）、创建校验（必填/项目 404/403/成功默认值/异常回滚）、
单查/更新/删除的属主校验、激活与停用、build-context、规则广场（公开规则/
分页）、复制（非公开 404/默认全局/目标项目 403/自定义名）、全局规则
（缓存命中写回/排序矩阵/未激活过滤）、merged 与 preview。
Redis 与失效函数打桩为进程内实现，模块级回退缓存在用例间清空。
"""

import uuid
from types import SimpleNamespace

import pytest

from models import ContextRule, Project, User, db
from services.marketplace import install_to_workspace  # noqa: F401 (占位对齐)


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

    # redis 两函数替换为进程内 store；失效函数打桩
    from api import context_rules as cr
    store = {}
    monkeypatch.setattr(cr, "redis_get_json", lambda k: store.get(k))
    monkeypatch.setattr(
        cr, "redis_set_json",
        lambda k, v, ttl=None: store.update({k: v}))
    monkeypatch.setattr(cr, "invalidate_user_caches", lambda uid, **kw: None)
    cr.context_rules_fallback_cache.clear()

    yield app
    db.session.remove()
    db.drop_all()
    cr.context_rules_fallback_cache.clear()
    store.clear()
    ctx.pop()


@pytest.fixture
def client(_isolated_app):
    return _isolated_app.test_client()


BASE = "/todo-for-ai/api/v1/context-rules"


@pytest.fixture
def user():
    import uuid as _uuid
    def _make():
        u = User(username=f"cu_{_uuid.uuid4().hex[:8]}",
                 email=f"cu_{_uuid.uuid4().hex[:6]}@t.io")
        db.session.add(u)
        db.session.commit()
        return u
    return _make


@pytest.fixture
def env(_isolated_app):
    import uuid as _uuid
    from flask_jwt_extended import create_access_token

    user = User(username=f"cr_{_uuid.uuid4().hex[:8]}",
                email=f"cr_{_uuid.uuid4().hex[:6]}@t.io")
    db.session.add(user)
    db.session.flush()
    project = Project(name=f"cp_{_uuid.uuid4().hex[:6]}", owner_id=user.id)
    db.session.add(project)
    db.session.commit()
    token = create_access_token(identity=str(user.id))
    return {
        "user": user, "project": project,
        "headers": {"Authorization": f"Bearer {token}"},
    }


def _rule(user, name="rule", project_id=None, is_active=True,
          is_public=False, priority=0, content="内容", apply_tasks=True,
          apply_projects=False, **kw):
    row = ContextRule(
        user_id=user.id, project_id=project_id, name=name, content=content,
        is_active=is_active, is_public=is_public, priority=priority,
        apply_to_tasks=apply_tasks, apply_to_projects=apply_projects, **kw)
    db.session.add(row)
    db.session.commit()
    return row


class TestCacheHelpers:
    def test_get_miss_returns_none(self):
        from api import context_rules as cr
        assert cr._context_rules_cache_get("k1") is None

    def test_set_then_get_hits_fallback(self, monkeypatch):
        from api import context_rules as cr
        cr._context_rules_cache_set("k2", [{"v": 1}])
        assert cr._context_rules_cache_get("k2") == [{"v": 1}]

    def test_fresh_fallback_hit_with_redis_miss(self, monkeypatch):
        from datetime import datetime
        from api import context_rules as cr
        cr.context_rules_fallback_cache["fresh"] = {
            "cached_at": datetime.utcnow().timestamp(),
            "value": {"v": 3},
        }
        assert cr._context_rules_cache_get("fresh") == {"v": 3}

    def test_expired_fallback_returns_none(self, monkeypatch):
        from datetime import datetime, timedelta
        from api import context_rules as cr
        cr.context_rules_fallback_cache["k3"] = {
            "cached_at": (datetime.utcnow() - timedelta(seconds=999)).timestamp(),
            "value": "stale",
        }
        assert cr._context_rules_cache_get("k3") is None


class TestListRules:
    def test_empty_list(self, client, env):
        resp = client.get(BASE, headers=env["headers"])
        assert resp.status_code == 200
        assert resp.get_json()["data"]["items"] == []

    def test_only_own_rules_listed(self, client, env, user):
        import uuid as _uuid
        other = User(username=f"o_{_uuid.uuid4().hex[:6]}",
                     email=f"o_{_uuid.uuid4().hex[:6]}@t.io")
        db.session.add(other)
        db.session.commit()
        _rule(other, "别人的")
        _rule(env["user"], "我的")

        resp = client.get(BASE, headers=env["headers"])
        names = [i["name"] for i in resp.get_json()["data"]["items"]]
        assert names == ["我的"]

    def test_project_filter_access_denied(self, client, env, user):
        import uuid as _uuid
        other_project = Project(name=f"p_{_uuid.uuid4().hex[:6]}",
                                owner_id=user().id)
        db.session.add(other_project)
        db.session.commit()
        resp = client.get(
            f"{BASE}?project_id={other_project.id}", headers=env["headers"])
        assert resp.status_code == 403
        assert resp.get_json()["error_details"]["code"] == "PROJECT_ACCESS_DENIED"

    def test_project_filter_scopes_results(self, client, env):
        _rule(env["user"], "in-project", project_id=env["project"].id)
        _rule(env["user"], "global-rule")
        resp = client.get(
            f"{BASE}?project_id={env['project'].id}", headers=env["headers"])
        names = [i["name"] for i in resp.get_json()["data"]["items"]]
        assert names == ["in-project"]

    def test_global_scope_filter(self, client, env):
        _rule(env["user"], "in-project", project_id=env["project"].id)
        _rule(env["user"], "global-rule")
        resp = client.get(f"{BASE}?scope=global", headers=env["headers"])
        names = [i["name"] for i in resp.get_json()["data"]["items"]]
        assert names == ["global-rule"]

    def test_flag_filters(self, client, env):
        _rule(env["user"], "active", is_active=True)
        _rule(env["user"], "inactive", is_active=False)
        resp = client.get(f"{BASE}?is_active=true", headers=env["headers"])
        names = {i["name"] for i in resp.get_json()["data"]["items"]}
        assert names == {"active"}

        resp = client.get(f"{BASE}?apply_to_projects=true",
                          headers=env["headers"])
        assert resp.get_json()["data"]["items"] == []

    def test_search_matches_name_description_content(self, client, env):
        _rule(env["user"], "Python 规范")
        _rule(env["user"], "其它", description="关于 python 的事")
        _rule(env["user"], "别的", content="写 python 代码")
        _rule(env["user"], "无关项")
        resp = client.get(f"{BASE}?search=python", headers=env["headers"])
        names = {i["name"] for i in resp.get_json()["data"]["items"]}
        assert names == {"Python 规范", "其它", "别的"}

    def test_sorting(self, client, env):
        _rule(env["user"], "b", priority=5)
        _rule(env["user"], "a", priority=1)
        resp = client.get(f"{BASE}?sort_by=name&sort_order=asc",
                          headers=env["headers"])
        names = [i["name"] for i in resp.get_json()["data"]["items"]]
        assert names == ["a", "b"]
        resp = client.get(f"{BASE}?sort_by=priority&sort_order=desc",
                          headers=env["headers"])
        names = [i["name"] for i in resp.get_json()["data"]["items"]]
        assert names == ["b", "a"]
        # updated_at / 未知键走默认 created_at 分支（模型无 rule_type，不再 500）
        for key in ("updated_at", "created_at", "rule_type"):
            resp = client.get(f"{BASE}?sort_by={key}", headers=env["headers"])
            assert resp.status_code == 200

    def test_project_info_enriched(self, client, env):
        _rule(env["user"], "with-proj", project_id=env["project"].id)
        resp = client.get(BASE, headers=env["headers"])
        item = resp.get_json()["data"]["items"][0]
        assert item["project"]["id"] == env["project"].id
        assert "name" in item["project"]

    def test_list_exception_maps_500(self, client, env, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("args down")
        monkeypatch.setattr("api.context_rules.get_request_args", boom)
        resp = client.get(BASE, headers=env["headers"])
        assert resp.status_code == 500


class TestTailBranches:
    """收尾分支：各端点异常兜底、可选过滤、剩余排序组合。"""

    def test_apply_to_tasks_filter(self, client, env):
        _rule(env["user"], "任务用", apply_tasks=True)
        _rule(env["user"], "非任务用", apply_tasks=False)
        resp = client.get(f"{BASE}?apply_to_tasks=false",
                          headers=env["headers"])
        names = {i["name"] for i in resp.get_json()["data"]["items"]}
        assert names == {"非任务用"}

    def test_get_rule_inner_exception_500(self, client, env, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("db")
        monkeypatch.setattr("api.context_rules.get_current_user", boom)
        resp = client.get(f"{BASE}/1", headers=env["headers"])
        assert resp.status_code == 500

    def test_update_validation_error_passthrough(self, client, env):
        rule = _rule(env["user"], "u1")
        resp = client.put(f"{BASE}/{rule.id}", headers=env["headers"],
                          data="plain", content_type="text/plain")
        assert resp.status_code == 400

    def test_update_inner_exception_500(self, client, env, monkeypatch):
        def boom(uid, **kw):
            raise RuntimeError("cache down")
        rule = _rule(env["user"], "u2")
        monkeypatch.setattr("api.context_rules.invalidate_user_caches", boom)
        resp = client.put(f"{BASE}/{rule.id}", headers=env["headers"],
                          json={"name": "n"})
        assert resp.status_code == 500

    def test_delete_inner_exception_500(self, client, env, monkeypatch):
        rule = _rule(env["user"], "d1")
        monkeypatch.setattr("api.context_rules.invalidate_user_caches",
                            lambda uid, **kw: (_ for _ in ()).throw(
                                RuntimeError("cache down")))
        resp = client.delete(f"{BASE}/{rule.id}", headers=env["headers"])
        assert resp.status_code == 500

    def test_activate_inner_exception_500(self, client, env, monkeypatch):
        rule = _rule(env["user"], "a1")
        monkeypatch.setattr("api.context_rules.invalidate_user_caches",
                            lambda uid, **kw: (_ for _ in ()).throw(
                                RuntimeError("cache down")))
        resp = client.post(f"{BASE}/{rule.id}/activate",
                           headers=env["headers"])
        assert resp.status_code == 500

    def test_deactivate_inner_exception_500(self, client, env, monkeypatch):
        rule = _rule(env["user"], "a2")
        monkeypatch.setattr("api.context_rules.invalidate_user_caches",
                            lambda uid, **kw: (_ for _ in ()).throw(
                                RuntimeError("cache down")))
        resp = client.post(f"{BASE}/{rule.id}/deactivate",
                           headers=env["headers"])
        assert resp.status_code == 500

    def test_build_context_inner_exception_500(self, client, env, monkeypatch):
        monkeypatch.setattr(
            ContextRule, "build_context_string",
            classmethod(lambda cls, **kw: (_ for _ in ()).throw(
                RuntimeError("build down"))))
        resp = client.post(f"{BASE}/build-context", headers=env["headers"],
                           json={"for_projects": True})
        assert resp.status_code == 500

    def test_global_sort_combinations(self, client, env):
        g = _rule(env["user"], "g1", priority=2)
        g.project_id = None
        db.session.commit()
        for params in ("sort_by=priority&sort_order=asc",
                       "sort_by=created_at&sort_order=asc",
                       "sort_by=name&sort_order=desc",
                       "sort_by=created_at"):
            resp = client.get(f"{BASE}/global?{params}",
                              headers=env["headers"])
            assert resp.status_code == 200

    def test_global_inner_exception_500(self, client, env, monkeypatch):
        def boom(key):
            raise RuntimeError("cache down")
        monkeypatch.setattr("api.context_rules._context_rules_cache_get", boom)
        resp = client.get(f"{BASE}/global", headers=env["headers"])
        assert resp.status_code == 500

    def test_merged_inner_exception_500(self, client, env, monkeypatch):
        monkeypatch.setattr(
            ContextRule, "build_context_string",
            classmethod(lambda cls, **kw: (_ for _ in ()).throw(
                RuntimeError("build down"))))
        resp = client.get(f"{BASE}/merged", headers=env["headers"])
        assert resp.status_code == 500

    def test_preview_without_project_global_only(self, client, env):
        _rule(env["user"], "global-p")
        _rule(env["user"], "proj-p", project_id=env["project"].id)
        resp = client.get(f"{BASE}/preview", headers=env["headers"])
        data = resp.get_json()["data"]
        names = [r["name"] for r in data["rules"]]
        assert names == ["global-p"]

    def test_preview_inner_exception_500(self, client, env, monkeypatch):
        monkeypatch.setattr(
            ContextRule, "build_context_string",
            classmethod(lambda cls, **kw: (_ for _ in ()).throw(
                RuntimeError("build down"))))
        resp = client.get(f"{BASE}/preview", headers=env["headers"])
        assert resp.status_code == 500


class TestCreateRule:
    def test_missing_required_fields(self, client, env):
        resp = client.post(BASE, headers=env["headers"], json={"name": "x"})
        assert resp.status_code == 400

    def test_non_json_rejected(self, client, env):
        resp = client.post(BASE, headers=env["headers"], data="x",
                           content_type="text/plain")
        assert resp.status_code == 400

    def test_project_not_found(self, client, env):
        resp = client.post(BASE, headers=env["headers"], json={
            "name": "r", "content": "c", "project_id": 999999})
        assert resp.status_code == 404
        assert resp.get_json()["error_details"]["code"] == "PROJECT_NOT_FOUND"

    def test_project_access_denied(self, client, env, user):
        import uuid as _uuid
        other_project = Project(name=f"p_{_uuid.uuid4().hex[:6]}",
                                owner_id=user().id)
        db.session.add(other_project)
        db.session.commit()
        resp = client.post(BASE, headers=env["headers"], json={
            "name": "r", "content": "c", "project_id": other_project.id})
        assert resp.status_code == 403

    def test_create_success_with_defaults(self, client, env):
        resp = client.post(BASE, headers=env["headers"], json={
            "name": "新规则", "content": "先读 README"})
        assert resp.status_code == 201
        data = resp.get_json()["data"]
        assert data["priority"] == 0
        assert data["is_active"] is True
        assert data["apply_to_tasks"] is True
        assert data["apply_to_projects"] is False

    def test_create_exception_rolls_back(self, client, env, monkeypatch):
        def boom(**kw):
            raise RuntimeError("create down")
        monkeypatch.setattr("api.context_rules.ContextRule.create",
                            classmethod(boom))
        resp = client.post(BASE, headers=env["headers"], json={
            "name": "r", "content": "c"})
        assert resp.status_code == 500
        db.session.rollback()


class TestGetUpdateDelete:
    def _own_rule(self, env, **kw):
        return _rule(env["user"], **kw)

    def test_get_found_and_404(self, client, env):
        rule = self._own_rule(env, name="mine")
        resp = client.get(f"{BASE}/{rule.id}", headers=env["headers"])
        assert resp.status_code == 200
        resp = client.get(f"{BASE}/999999", headers=env["headers"])
        assert resp.status_code == 404

    def test_cannot_get_others(self, client, env, user):
        rule = _rule(user(), "别人的")
        resp = client.get(f"{BASE}/{rule.id}", headers=env["headers"])
        assert resp.status_code == 404

    def test_update_fields(self, client, env):
        rule = self._own_rule(env, name="old")
        resp = client.put(f"{BASE}/{rule.id}", headers=env["headers"],
                          json={"name": "new", "priority": 9,
                                "is_active": False})
        assert resp.status_code == 200
        assert rule.name == "new" and rule.priority == 9
        assert rule.is_active is False

    def test_update_404(self, client, env):
        resp = client.put(f"{BASE}/999999", headers=env["headers"],
                          json={"name": "x"})
        assert resp.status_code == 404

    def test_delete_204_then_gone(self, client, env):
        rule = self._own_rule(env, name="del")
        resp = client.delete(f"{BASE}/{rule.id}", headers=env["headers"])
        assert resp.status_code == 204
        assert ContextRule.query.filter_by(id=rule.id).first() is None

    def test_delete_404(self, client, env):
        assert client.delete(f"{BASE}/999999",
                             headers=env["headers"]).status_code == 404


class TestActivateDeactivate:
    def test_activate_and_deactivate(self, client, env):
        rule = _rule(env["user"], "toggle", is_active=False)
        resp = client.post(f"{BASE}/{rule.id}/activate",
                           headers=env["headers"])
        assert resp.status_code == 200 and rule.is_active is True
        resp = client.post(f"{BASE}/{rule.id}/deactivate",
                           headers=env["headers"])
        assert resp.status_code == 200 and rule.is_active is False

    def test_activate_404(self, client, env):
        assert client.post(f"{BASE}/999999/activate",
                           headers=env["headers"]).status_code == 404

    def test_deactivate_404(self, client, env):
        assert client.post(f"{BASE}/999999/deactivate",
                           headers=env["headers"]).status_code == 404


class TestBuildContext:
    def test_build_context_success(self, client, env):
        _rule(env["user"], "规范", content="用 pytest", priority=9,
              apply_projects=True)
        resp = client.post(f"{BASE}/build-context", headers=env["headers"],
                           json={"project_id": env["project"].id})
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert "### 规范" in data["context_string"]
        assert data["rules_applied"] == 1

    def test_build_context_empty_payload_still_ok(self, client, env):
        _rule(env["user"], "全局规范", content="g", apply_projects=True)
        resp = client.post(f"{BASE}/build-context", headers=env["headers"],
                           json={"for_projects": True})
        assert resp.status_code == 200
        assert "全局规范" in resp.get_json()["data"]["context_string"]

    def test_build_context_invalid_body(self, client, env):
        resp = client.post(f"{BASE}/build-context",
                           headers=env["headers"], data="x",
                           content_type="text/plain")
        assert resp.status_code == 400


class TestMarketplace:
    def test_public_rules_listed(self, client, env, user):
        _rule(env["user"], "private", is_public=False)
        _rule(user(), "public-one", is_public=True)
        resp = client.get(f"{BASE}/marketplace", headers=env["headers"])
        assert resp.status_code == 200
        names = [i["name"] for i in resp.get_json()["data"]["items"]]
        assert names == ["public-one"]
        pagination = resp.get_json()["data"]["pagination"]
        assert pagination["total"] == 1

    def test_public_rules_exception_maps_500(self, client, env, monkeypatch):
        def boom(**kw):
            raise RuntimeError("db")
        monkeypatch.setattr("api.context_rules.ContextRule.get_public_rules",
                            classmethod(boom))
        resp = client.get(f"{BASE}/marketplace", headers=env["headers"])
        assert resp.status_code == 500


class TestCopyRule:
    def test_non_public_source_404(self, client, env, user):
        rule = _rule(user(), "私有", is_public=False)
        resp = client.post(f"{BASE}/{rule.id}/copy", headers=env["headers"],
                           json={})
        assert resp.status_code == 404
        assert resp.get_json()["error_details"]["code"] == "RULE_NOT_FOUND"

    def test_copy_default_global_with_default_name(self, client, env, user):
        source = _rule(user(), "模板规则", is_public=True)
        resp = client.post(f"{BASE}/{source.id}/copy",
                           headers=env["headers"], json={"copy_as_global": True})
        assert resp.status_code == 201
        data = resp.get_json()["data"]
        assert data["name"] == "模板规则 - 副本"
        assert data["user_id"] == env["user"].id

    def test_copy_empty_body_rejected(self, client, env, user):
        source = _rule(user(), "模板", is_public=True)
        resp = client.post(f"{BASE}/{source.id}/copy",
                           headers=env["headers"], json={})
        assert resp.status_code == 400

    def test_copy_to_denied_project_403(self, client, env, user):
        import uuid as _uuid
        source = _rule(user(), "模板", is_public=True)
        other_project = Project(name=f"p_{_uuid.uuid4().hex[:6]}",
                                owner_id=user().id)
        db.session.add(other_project)
        db.session.commit()
        resp = client.post(f"{BASE}/{source.id}/copy",
                           headers=env["headers"], json={
                               "copy_as_global": False,
                               "target_project_id": other_project.id})
        assert resp.status_code == 403

    def test_copy_with_custom_name_to_own_project(self, client, env, user):
        source = _rule(user(), "模板", is_public=True)
        resp = client.post(f"{BASE}/{source.id}/copy",
                           headers=env["headers"], json={
                               "name": "我的副本", "copy_as_global": False,
                               "target_project_id": env["project"].id})
        assert resp.status_code == 201
        assert resp.get_json()["data"]["name"] == "我的副本"

    def test_copy_exception_rolls_back(self, client, env, user, monkeypatch):
        source = _rule(user(), "模板", is_public=True)
        monkeypatch.setattr("api.context_rules.ContextRule",
                            ContextRule, raising=False)
        monkeypatch.setattr(
            ContextRule, "copy_to_user",
            lambda self, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
        resp = client.post(f"{BASE}/{source.id}/copy",
                           headers=env["headers"],
                           json={"name": "会炸的副本"})
        assert resp.status_code == 500
        db.session.rollback()


class TestGlobalRules:
    def test_global_lists_only_global_or_public_orphan(self, client, env, user):
        g1 = _rule(env["user"], "我的全局", priority=9)
        g1.project_id = None
        p1 = _rule(user(), "他人公开孤儿", is_public=True, priority=5)
        p1.project_id = None
        _rule(env["user"], "项目规则", project_id=env["project"].id)
        private_orphan = _rule(user(), "他人私有孤儿", priority=7)
        private_orphan.project_id = None
        db.session.commit()

        resp = client.get(f"{BASE}/global?sort_by=priority&sort_order=desc",
                          headers=env["headers"])
        names = [r["name"] for r in resp.get_json()["data"]]
        assert names == ["我的全局", "他人公开孤儿"]  # priority desc

    def test_global_sort_and_inactive_filter(self, client, env):
        a = _rule(env["user"], "a规则", priority=1)
        a.project_id = None
        b = _rule(env["user"], "b规则", priority=2)
        b.project_id = None
        dead = _rule(env["user"], "dead", priority=99, is_active=False)
        dead.project_id = None
        db.session.commit()

        resp = client.get(f"{BASE}/global?sort_by=name&sort_order=asc",
                          headers=env["headers"])
        names = [r["name"] for r in resp.get_json()["data"]]
        assert names == ["a规则", "b规则"]  # dead 被过滤

        resp = client.get(f"{BASE}/global?is_active=false",
                          headers=env["headers"])
        names = {r["name"] for r in resp.get_json()["data"]}
        assert "dead" in names

    def test_global_cache_second_call(self, client, env, monkeypatch):
        g = _rule(env["user"], "cached", priority=1)
        g.project_id = None
        db.session.commit()

        first = client.get(f"{BASE}/global", headers=env["headers"])
        # 数据库被清空后仍能命中缓存
        ContextRule.query.delete()
        db.session.commit()
        second = client.get(f"{BASE}/global", headers=env["headers"])
        assert second.status_code == 200
        assert second.get_json()["data"] == first.get_json()["data"]


class TestMergedAndPreview:
    def test_merged_with_project(self, client, env):
        _rule(env["user"], "proj-rule", project_id=env["project"].id,
              priority=5, apply_projects=True)
        _rule(env["user"], "other-proj", project_id=999)
        resp = client.get(f"{BASE}/merged?project_id={env['project'].id}",
                          headers=env["headers"])
        data = resp.get_json()["data"]
        names = [r["name"] for r in data["rules"]]
        assert names == ["proj-rule"]
        assert "proj-rule" in data["content"]

    def test_merged_without_project_global_only(self, client, env):
        _rule(env["user"], "global-only")
        _rule(env["user"], "proj", project_id=env["project"].id)
        resp = client.get(f"{BASE}/merged", headers=env["headers"])
        names = [r["name"] for r in resp.get_json()["data"]["rules"]]
        assert names == ["global-only"]

    def test_merged_cached(self, client, env):
        client.get(f"{BASE}/merged", headers=env["headers"])
        ContextRule.query.delete()
        db.session.commit()
        resp = client.get(f"{BASE}/merged", headers=env["headers"])
        assert resp.status_code == 200
        assert resp.get_json()["data"]["rules"] == []

    def test_preview_shape(self, client, env):
        _rule(env["user"], "p1", project_id=env["project"].id,
              apply_projects=True)
        resp = client.get(f"{BASE}/preview?project_id={env['project'].id}",
                          headers=env["headers"])
        data = resp.get_json()["data"]
        assert data["preview_info"]["total_rules"] == 1
        assert data["preview_info"]["project_id"] == env["project"].id
        assert data["preview_info"]["content_length"] > 0
        assert "generated_at" in data["preview_info"]
