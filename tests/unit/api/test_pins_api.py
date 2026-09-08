"""用户项目 Pin API（api/pins.py）单元回归。

覆盖：双层缓存（redis + 进程内回退，TTL 20s）、Pin/取消/重Pin复活、
仅可 Pin 自己拥有的项目、10 个上限（已 Pin 项目不受限）、重排序
（格式校验、未知项目跳过）、check/stats/task-counts（缓存命中、
待执行任务单次聚合）、用户缓存失效联动；模型层死代码清理注记：
get_user_pins / reorder_pins 两个零引用类方法已删除（路由各自内联
实现同样逻辑）。
"""

import uuid
from datetime import datetime

import pytest
from flask_jwt_extended import create_access_token

from models import (
    Project,
    Task,
    User,
    UserProjectPin,
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

    # redis 两函数替换为进程内 store，防真实 Redis 污染
    from api import pins as pins_mod
    store = {}
    monkeypatch.setattr(pins_mod, "redis_get_json", lambda k: store.get(k))
    monkeypatch.setattr(
        pins_mod, "redis_set_json",
        lambda k, v, ttl=None: store.update({k: v}))
    pins_mod.pins_fallback_cache.clear()
    pins_mod._test_store = store

    yield app
    db.session.remove()
    db.drop_all()
    pins_mod.pins_fallback_cache.clear()
    store.clear()
    ctx.pop()


@pytest.fixture
def client(_isolated_app):
    return _isolated_app.test_client()


@pytest.fixture
def env(_isolated_app):
    user = User(username=f"pn_{uuid.uuid4().hex[:8]}",
                email=f"pn_{uuid.uuid4().hex[:6]}@t.io")
    db.session.add(user)
    db.session.flush()
    p1 = Project(name=f"p1_{uuid.uuid4().hex[:6]}", owner_id=user.id,
                 color="#1890ff")
    p2 = Project(name=f"p2_{uuid.uuid4().hex[:6]}", owner_id=user.id)
    db.session.add_all([p1, p2])
    db.session.commit()
    return {
        "user": user, "p1": p1, "p2": p2,
        "headers": {"Authorization": f"Bearer {create_access_token(identity=str(user.id))}"},
        "base": "/todo-for-ai/api/v1/pins",
    }


def _mk_project(owner, name=None):
    row = Project(name=name or f"p_{uuid.uuid4().hex[:6]}",
                  owner_id=owner.id)
    db.session.add(row)
    db.session.flush()
    return row


@pytest.fixture
def invalidated(monkeypatch):
    from api import pins as pins_mod
    calls = []
    monkeypatch.setattr(pins_mod, "invalidate_user_caches",
                        lambda uid: calls.append(uid))
    return calls


class TestPinListAndCache:
    def test_empty_list(self, env, client):
        resp = client.get(env["base"], headers=env["headers"])
        assert resp.status_code == 200
        assert resp.get_json()["data"] == {"pins": [], "total": 0}

    def test_list_orders_and_caches(self, env, client):
        resp = client.post(env["base"], headers=env["headers"],
                           json={"project_id": env["p1"].id})
        assert resp.status_code == 200
        client.post(env["base"], headers=env["headers"],
                    json={"project_id": env["p2"].id})
        first = client.get(env["base"], headers=env["headers"])
        data = first.get_json()["data"]
        assert data["total"] == 2
        assert [p["pin_order"] for p in data["pins"]] == [0, 1]
        assert data["pins"][0]["project"]["name"] == env["p1"].name

        # 第二次命中缓存。注意：unpin 只调 invalidate_user_caches，
        # 不清 pins 自身缓存（20s TTL 内提供有界旧读——钉住该行为）
        client.delete(f"{env['base']}/{env['p1'].id}",
                      headers=env["headers"])
        cached = client.get(env["base"], headers=env["headers"])
        assert cached.get_json()["data"]["total"] == 2

    def test_inactive_pins_excluded(self, env, client):
        pin = UserProjectPin(user_id=env["user"].id,
                             project_id=env["p1"].id, pin_order=0,
                             is_active=False)
        db.session.add(pin)
        db.session.commit()
        resp = client.get(env["base"], headers=env["headers"])
        assert resp.get_json()["data"]["total"] == 0


class TestPinProject:
    def test_requires_project_id(self, env, client):
        resp = client.post(env["base"], headers=env["headers"], json={})
        assert resp.status_code == 400
        assert "project_id is required" in resp.get_json()["message"]

    def test_only_own_projects_pinnable(self, env, client):
        other = User(username=f"ot_{uuid.uuid4().hex[:8]}",
                     email=f"ot_{uuid.uuid4().hex[:6]}@t.io")
        db.session.add(other)
        db.session.flush()
        foreign = _mk_project(other)
        db.session.commit()
        resp = client.post(env["base"], headers=env["headers"],
                           json={"project_id": foreign.id})
        assert resp.status_code == 404
        assert "access denied" in resp.get_json()["message"]

    def test_pin_default_order_and_invalidation(self, env, client,
                                                invalidated):
        resp = client.post(env["base"], headers=env["headers"],
                           json={"project_id": env["p1"].id})
        assert resp.status_code == 200
        assert resp.get_json()["data"]["pin"]["pin_order"] == 0
        assert invalidated == [env["user"].id]

        resp = client.post(env["base"], headers=env["headers"],
                           json={"project_id": env["p2"].id})
        assert resp.get_json()["data"]["pin"]["pin_order"] == 1

    def test_repin_reactivates(self, env, client):
        client.post(env["base"], headers=env["headers"],
                    json={"project_id": env["p1"].id})
        client.delete(f"{env['base']}/{env['p1'].id}",
                      headers=env["headers"])
        resp = client.post(env["base"], headers=env["headers"],
                           json={"project_id": env["p1"].id,
                                 "pin_order": 5})
        assert resp.status_code == 200
        db.session.expire_all()
        pin = UserProjectPin.query.filter_by(
            user_id=env["user"].id, project_id=env["p1"].id).first()
        assert pin.is_active is True
        assert pin.pin_order == 5

    def test_max_ten_pins(self, env, client):
        # 先 Pin p1，再补满 10 个
        client.post(env["base"], headers=env["headers"],
                    json={"project_id": env["p1"].id})
        for i in range(9):
            project = _mk_project(env["user"])
            db.session.commit()
            resp = client.post(env["base"], headers=env["headers"],
                               json={"project_id": project.id})
            assert resp.status_code == 200

        eleventh = _mk_project(env["user"])
        db.session.commit()
        resp = client.post(env["base"], headers=env["headers"],
                           json={"project_id": eleventh.id})
        assert resp.status_code == 400
        assert "Maximum 10" in resp.get_json()["message"]

        # 已 Pin 的项目重新提交（复活路径）不受上限限制
        resp = client.post(env["base"], headers=env["headers"],
                           json={"project_id": env["p1"].id})
        assert resp.status_code == 200


class TestUnpinAndReorder:
    def test_unpin_missing_404(self, env, client):
        resp = client.delete(f"{env['base']}/999999",
                             headers=env["headers"])
        assert resp.status_code == 404

    def test_unpin_success(self, env, client, invalidated):
        client.post(env["base"], headers=env["headers"],
                    json={"project_id": env["p1"].id})
        resp = client.delete(f"{env['base']}/{env['p1'].id}",
                             headers=env["headers"])
        assert resp.status_code == 200
        db.session.expire_all()
        pin = UserProjectPin.query.filter_by(
            user_id=env["user"].id, project_id=env["p1"].id).first()
        assert pin.is_active is False
        assert invalidated == [env["user"].id] * 2

    def test_reorder_validation(self, env, client):
        url = f"{env['base']}/reorder"
        assert client.put(url, headers=env["headers"],
                          json={}).status_code == 400
        assert client.put(url, headers=env["headers"],
                          json={"pin_orders": ["x"]}
                          ).status_code == 400
        assert client.put(url, headers=env["headers"],
                          json={"pin_orders": [{"project_id": 1}]}
                          ).status_code == 400

    def test_reorder_updates_and_skips_unknown(self, env, client,
                                               invalidated):
        client.post(env["base"], headers=env["headers"],
                    json={"project_id": env["p1"].id})
        client.post(env["base"], headers=env["headers"],
                    json={"project_id": env["p2"].id})
        resp = client.put(f"{env['base']}/reorder", headers=env["headers"],
                          json={"pin_orders": [
                              {"project_id": env["p2"].id, "pin_order": 0},
                              {"project_id": 999999, "pin_order": 9},
                          ]})
        assert resp.status_code == 200
        db.session.expire_all()
        p2_pin = UserProjectPin.query.filter_by(
            user_id=env["user"].id, project_id=env["p2"].id).first()
        assert p2_pin.pin_order == 0
        assert invalidated[-1] == env["user"].id

    def test_fallback_cache_fresh_hit(self, monkeypatch):
        from api import pins as pins_mod
        monkeypatch.setattr(pins_mod, "redis_get_json", lambda k: None)
        pins_mod.pins_fallback_cache["fresh-key"] = {
            "cached_at": datetime.utcnow().timestamp(),
            "value": {"v": 7}}
        assert pins_mod._pins_cache_get("fresh-key") == {"v": 7}

    def test_fallback_cache_expired_miss(self, monkeypatch):
        from api import pins as pins_mod
        monkeypatch.setattr(pins_mod, "redis_get_json", lambda k: None)
        pins_mod.pins_fallback_cache["stale-key"] = {
            "cached_at": datetime.utcnow().timestamp() - 999,
            "value": {"v": 1}}
        assert pins_mod._pins_cache_get("stale-key") is None

    def test_pins_endpoint_500_matrix(self, env, client, monkeypatch):
        """全部 6 个端点的 catch-all 500 兜底。"""
        from api import pins as pins_mod

        def boom(*a, **kw):
            raise RuntimeError("db down")

        client.post(env["base"], headers=env["headers"],
                    json={"project_id": env["p1"].id})

        # GET list / check：query 属性抛错
        monkeypatch.setattr(pins_mod.UserProjectPin, "query",
                            property(lambda self: boom()))
        assert client.get(env["base"],
                          headers=env["headers"]).status_code == 500
        assert client.get(f"{env['base']}/check/1",
                          headers=env["headers"]).status_code == 500
        monkeypatch.undo()

        # POST pin：pin_project 抛错
        monkeypatch.setattr(pins_mod.UserProjectPin, "pin_project",
                            classmethod(lambda cls, *a, **kw: boom()))
        assert client.post(env["base"], headers=env["headers"],
                           json={"project_id": env["p1"].id}
                           ).status_code == 500
        monkeypatch.undo()

        # DELETE unpin：unpin_project 抛错
        monkeypatch.setattr(pins_mod.UserProjectPin, "unpin_project",
                            classmethod(lambda cls, *a, **kw: boom()))
        assert client.delete(f"{env['base']}/{env['p1'].id}",
                             headers=env["headers"]).status_code == 500
        monkeypatch.undo()

        # PUT reorder：query 属性抛错
        monkeypatch.setattr(pins_mod.UserProjectPin, "query",
                            property(lambda self: boom()))
        assert client.put(f"{env['base']}/reorder", headers=env["headers"],
                          json={"pin_orders": [
                              {"project_id": env["p1"].id,
                               "pin_order": 1}]}).status_code == 500
        monkeypatch.undo()

        # GET stats：get_user_pin_count 抛错
        monkeypatch.setattr(pins_mod.UserProjectPin, "get_user_pin_count",
                            classmethod(lambda cls, *a, **kw: boom()))
        assert client.get(f"{env['base']}/stats",
                          headers=env["headers"]).status_code == 500
        monkeypatch.undo()

        # GET task-counts：query 属性抛错
        monkeypatch.setattr(pins_mod.UserProjectPin, "query",
                            property(lambda self: boom()))
        assert client.get(f"{env['base']}/task-counts",
                          headers=env["headers"]).status_code == 500


class TestPinStatusStatsTaskCounts:
    def test_check_pin_status(self, env, client):
        resp = client.get(f"{env['base']}/check/{env['p1'].id}",
                          headers=env["headers"])
        assert resp.get_json()["data"]["is_pinned"] is False
        client.post(env["base"], headers=env["headers"],
                    json={"project_id": env["p1"].id})
        resp = client.get(f"{env['base']}/check/{env['p1'].id}",
                          headers=env["headers"])
        assert resp.get_json()["data"]["is_pinned"] is True

    def test_stats_counts_active_only(self, env, client):
        db.session.add(UserProjectPin(
            user_id=env["user"].id, project_id=env["p1"].id,
            pin_order=0, is_active=False))
        db.session.commit()
        resp = client.get(f"{env['base']}/stats", headers=env["headers"])
        data = resp.get_json()["data"]
        assert data["pin_count"] == 0
        assert data["remaining"] == 10
        # 未清缓存时的第二次 GET：直接命中缓存返回
        again = client.get(f"{env['base']}/stats", headers=env["headers"])
        assert again.get_json()["data"]["pin_count"] == 0
        client.post(env["base"], headers=env["headers"],
                    json={"project_id": env["p1"].id})
        # 上一次 stats 已写双层缓存——redis 与回退一并清掉再查
        from api import pins as pins_mod
        pins_mod.pins_fallback_cache.clear()
        pins_mod._test_store.clear()
        resp = client.get(f"{env['base']}/stats", headers=env["headers"])
        data = resp.get_json()["data"]
        assert data["pin_count"] == 1 and data["remaining"] == 9

    def test_task_counts_empty(self, env, client):
        resp = client.get(f"{env['base']}/task-counts",
                          headers=env["headers"])
        data = resp.get_json()["data"]
        assert data == {"task_counts": [], "total_pins": 0}

    def test_task_counts_pending_aggregation(self, env, client):
        project = _mk_project(env["user"])
        for offset, status in enumerate(("TODO", "IN_PROGRESS", "REVIEW",
                                         "DONE", "CANCELLED")):
            row = Task(id=72_000_001 + offset, title=f"t-{status}",
                       content="c", status=status, priority="MEDIUM",
                       project_id=project.id, owner_id=env["user"].id)
            db.session.add(row)
        db.session.commit()
        resp = client.post(env["base"], headers=env["headers"],
                           json={"project_id": project.id})
        assert resp.status_code == 200

        resp = client.get(f"{env['base']}/task-counts",
                          headers=env["headers"])
        data = resp.get_json()["data"]
        assert data["total_pins"] == 1
        entry = data["task_counts"][0]
        assert entry["project_id"] == project.id
        assert entry["pending_tasks"] == 3  # TODO/IN_PROGRESS/REVIEW
        assert entry["pin_order"] == 0

        # 第二次命中缓存
        again = client.get(f"{env['base']}/task-counts",
                           headers=env["headers"])
        assert again.get_json()["data"] == data
