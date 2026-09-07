"""自定义提示词 API（api/custom_prompts.py）单元回归。

覆盖：列表（类型过滤/激活过滤/分页）、创建（必填/类型枚举/名称长度/
重名/任务按钮自动排序）、单查/更新/删除属主校验、更新重名拒绝、
project-prompts 与 task-button-prompts 过滤、按钮重排序三档校验、
项目提示词预览、初始化默认（已有拒绝/语言回退）、重置、导出、
导入（缺字段/坏类型/重名跳过与计数）。
"""

import uuid

import pytest

from models import CustomPrompt, PromptType, UserSettings, db


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
    monkeypatch.setattr("api.custom_prompts.invalidate_user_caches",
                        lambda uid, **kw: None)
    yield app
    db.session.remove()
    db.drop_all()
    ctx.pop()


@pytest.fixture
def client(_isolated_app):
    return _isolated_app.test_client()


BASE = "/todo-for-ai/api/v1/custom-prompts"


@pytest.fixture
def env(_isolated_app):
    from flask_jwt_extended import create_access_token
    u = _make_user()
    token = create_access_token(identity=str(u.id))
    return {"user": u, "headers": {"Authorization": f"Bearer {token}"}}


def _make_user():
    import uuid as _uuid
    from models import User
    u = User(username=f"cp_{uuid.uuid4().hex[:8]}",
             email=f"cp_{uuid.uuid4().hex[:6]}@t.io")
    db.session.add(u)
    db.session.commit()
    return u


def _prompt(user, name="p1", ptype=PromptType.PROJECT, is_active=True,
            order_index=0, content="内容"):
    row = CustomPrompt.create_prompt(
        user_id=user.id, prompt_type=ptype, name=name, content=content,
        order_index=order_index)
    row.is_active = is_active
    db.session.commit()
    return row


class TestListPrompts:
    def test_empty(self, client, env):
        resp = client.get(BASE, headers=env["headers"])
        assert resp.status_code == 200
        body = resp.get_json()["data"]
        assert body["items"] == [] and body["pagination"]["total"] == 0

    def test_type_filter_and_invalid(self, client, env):
        _prompt(env["user"], "proj", PromptType.PROJECT)
        _prompt(env["user"], "btn", PromptType.TASK_BUTTON)

        resp = client.get(f"{BASE}?prompt_type=project",
                          headers=env["headers"])
        names = [i["name"] for i in resp.get_json()["data"]["items"]]
        assert names == ["proj"]

        resp = client.get(f"{BASE}?prompt_type=bogus",
                          headers=env["headers"])
        assert resp.status_code == 400
        assert "prompt_type" in resp.get_json()["message"]

    def test_only_active_listed_by_default(self, client, env):
        _prompt(env["user"], "on", is_active=True)
        _prompt(env["user"], "off", is_active=False)
        resp = client.get(BASE, headers=env["headers"])
        names = {i["name"] for i in resp.get_json()["data"]["items"]}
        assert names == {"on"}


class TestCreatePrompt:
    def test_missing_required_fields(self, client, env):
        resp = client.post(BASE, headers=env["headers"],
                           json={"name": "x", "content": "c"})
        assert resp.status_code == 400

    def test_invalid_type(self, client, env):
        resp = client.post(BASE, headers=env["headers"], json={
            "prompt_type": "bogus", "name": "x", "content": "c"})
        assert resp.status_code == 400

    @pytest.mark.parametrize("name", ["", "   ", "x" * 256])
    def test_name_validation(self, client, env, name):
        resp = client.post(BASE, headers=env["headers"], json={
            "prompt_type": "project", "name": name, "content": "c"})
        assert resp.status_code == 400

    def test_duplicate_name_rejected(self, client, env):
        _prompt(env["user"], "dup")
        resp = client.post(BASE, headers=env["headers"], json={
            "prompt_type": "project", "name": "dup", "content": "c"})
        assert resp.status_code == 400
        assert "already exists" in resp.get_json()["message"]

    def test_task_button_auto_order_index(self, client, env):
        _prompt(env["user"], "b1", PromptType.TASK_BUTTON, order_index=3)
        resp = client.post(BASE, headers=env["headers"], json={
            "prompt_type": "task_button", "name": "b2", "content": "c"})
        assert resp.status_code == 200
        created = CustomPrompt.query.filter_by(name="b2").one()
        assert created.order_index == 4  # max+1

    def test_project_type_success(self, client, env):
        resp = client.post(BASE, headers=env["headers"], json={
            "prompt_type": "project", "name": "项目提示",
            "content": "正文", "description": "描述"})
        assert resp.status_code == 200
        assert resp.get_json()["data"]["name"] == "项目提示"


class TestGetUpdateDelete:
    def test_get_found_404_and_isolation(self, client, env):
        mine = _prompt(env["user"], "mine")
        resp = client.get(f"{BASE}/{mine.id}", headers=env["headers"])
        assert resp.status_code == 200

        other = _make_user()
        foreign = _prompt(other, "foreign")
        resp = client.get(f"{BASE}/{foreign.id}", headers=env["headers"])
        assert resp.status_code == 404

    def test_update_fields(self, client, env):
        p = _prompt(env["user"], "old")
        resp = client.put(f"{BASE}/{p.id}", headers=env["headers"],
                          json={"name": "new", "content": "新内容",
                                "is_active": False})
        assert resp.status_code == 200
        assert p.name == "new" and p.is_active is False

    def test_update_404(self, client, env):
        resp = client.put(f"{BASE}/999999", headers=env["headers"],
                          json={"name": "x"})
        assert resp.status_code == 404

    def test_update_name_dup_rejected(self, client, env):
        _prompt(env["user"], "a")
        p = _prompt(env["user"], "b")
        resp = client.put(f"{BASE}/{p.id}", headers=env["headers"],
                          json={"name": "a"})
        assert resp.status_code == 400

    def test_delete_success_and_404(self, client, env):
        p = _prompt(env["user"], "del")
        assert client.delete(f"{BASE}/{p.id}",
                             headers=env["headers"]).status_code == 200
        assert client.delete(f"{BASE}/{p.id}",
                             headers=env["headers"]).status_code == 404


class TestTypeEndpoints:
    def test_project_and_task_button_lists(self, client, env):
        _prompt(env["user"], "pj", PromptType.PROJECT)
        btn = _prompt(env["user"], "bt", PromptType.TASK_BUTTON)
        btn.is_active = False
        db.session.commit()

        resp = client.get(f"{BASE}/project-prompts", headers=env["headers"])
        names = [i["name"] for i in resp.get_json()["data"]]
        assert names == ["pj"]

        resp = client.get(f"{BASE}/task-button-prompts",
                          headers=env["headers"])
        assert resp.get_json()["data"] == []  # 默认只列激活的，bt 已停用

        resp = client.get(f"{BASE}/task-button-prompts?is_active=false",
                          headers=env["headers"])
        assert [i["name"] for i in resp.get_json()["data"]] == ["bt"]


class TestReorder:
    def test_missing_prompt_orders(self, client, env):
        resp = client.put(f"{BASE}/task-buttons/reorder",
                          headers=env["headers"], json={})
        assert resp.status_code == 400

    def test_non_list_rejected(self, client, env):
        resp = client.put(f"{BASE}/task-buttons/reorder",
                          headers=env["headers"],
                          json={"prompt_orders": "nope"})
        assert resp.status_code == 400

    def test_item_missing_keys_rejected(self, client, env):
        resp = client.put(f"{BASE}/task-buttons/reorder",
                          headers=env["headers"],
                          json={"prompt_orders": [{"id": 1}]})
        assert resp.status_code == 400

    def test_reorder_success(self, client, env):
        b1 = _prompt(env["user"], "b1", PromptType.TASK_BUTTON, order_index=1)
        b2 = _prompt(env["user"], "b2", PromptType.TASK_BUTTON, order_index=2)
        resp = client.put(f"{BASE}/task-buttons/reorder",
                          headers=env["headers"], json={"prompt_orders": [
                              {"id": b1.id, "order_index": 20},
                              {"id": b2.id, "order_index": 10},
                          ]})
        assert resp.status_code == 200
        db.session.expire_all()
        assert b1.order_index == 20 and b2.order_index == 10


class TestPreviewAndDefaults:
    def test_preview_404_for_task_button_type(self, client, env):
        btn = _prompt(env["user"], "bt", PromptType.TASK_BUTTON)
        resp = client.post(f"{BASE}/project-prompts/{btn.id}/preview",
                           headers=env["headers"], json={})
        assert resp.status_code == 404

    def test_preview_success_with_and_without_body(self, client, env):
        p = _prompt(env["user"], "项目模板")
        resp = client.post(f"{BASE}/project-prompts/{p.id}/preview",
                           headers=env["headers"], json={"project_id": 7})
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["project_id"] == 7
        assert data["raw_content"] == "内容"

        resp = client.post(f"{BASE}/project-prompts/{p.id}/preview",
                           headers=env["headers"])
        assert resp.status_code == 200

    def test_initialize_rejects_existing_prompts(self, client, env):
        _prompt(env["user"], "already")
        resp = client.post(f"{BASE}/initialize-defaults",
                           headers=env["headers"], json={})
        assert resp.status_code == 400

    def test_initialize_creates_defaults(self, client, env):
        resp = client.post(f"{BASE}/initialize-defaults",
                           headers=env["headers"], json={"language": "en"})
        assert resp.status_code == 200
        names = {p.name for p in CustomPrompt.query.all()}
        assert "Default Project Template" in names

    def test_reset_replaces_existing(self, client, env):
        _prompt(env["user"], "old-one")
        resp = client.post(f"{BASE}/reset-to-defaults",
                           headers=env["headers"], json={})
        assert resp.status_code == 200
        names = {p.name for p in CustomPrompt.query.all()}
        assert "old-one" not in names
        assert any("默认" in n or "Default" in n for n in names)


class TestTailBranches:
    """各端点异常兜底与剩余校验分支。"""

    def _boom_user(self, monkeypatch):
        monkeypatch.setattr("api.custom_prompts.get_current_user",
                            lambda: (_ for _ in ()).throw(RuntimeError("db")))

    def test_list_exception(self, client, env, monkeypatch):
        self._boom_user(monkeypatch)
        assert client.get(BASE, headers=env["headers"]).status_code == 500

    def test_list_is_active_false_branch(self, client, env):
        _prompt(env["user"], "on", is_active=True)
        _prompt(env["user"], "off", is_active=False)
        resp = client.get(f"{BASE}?is_active=false", headers=env["headers"])
        names = {i["name"] for i in resp.get_json()["data"]["items"]}
        assert names == {"off"}

    def test_create_exception(self, client, env, monkeypatch):
        monkeypatch.setattr(
            CustomPrompt, "create_prompt",
            classmethod(lambda cls, **kw: (_ for _ in ()).throw(
                RuntimeError("down"))))
        resp = client.post(BASE, headers=env["headers"], json={
            "prompt_type": "project", "name": "x", "content": "c"})
        assert resp.status_code == 500

    def test_get_one_exception(self, client, env, monkeypatch):
        self._boom_user(monkeypatch)
        assert client.get(f"{BASE}/1", headers=env["headers"]).status_code == 500

    def test_update_non_json_400(self, client, env):
        p = _prompt(env["user"], "uj")
        resp = client.put(f"{BASE}/{p.id}", headers=env["headers"],
                          data="plain", content_type="text/plain")
        assert resp.status_code == 400

    def test_update_name_variants(self, client, env):
        p = _prompt(env["user"], "uv")
        resp = client.put(f"{BASE}/{p.id}", headers=env["headers"],
                          json={"name": "   "})
        assert resp.status_code == 400
        resp = client.put(f"{BASE}/{p.id}", headers=env["headers"],
                          json={"name": "x" * 256})
        assert resp.status_code == 400

    def test_update_exception(self, client, env, monkeypatch):
        p = _prompt(env["user"], "ue")
        self._boom_user(monkeypatch)
        resp = client.put(f"{BASE}/{p.id}", headers=env["headers"],
                          json={"name": "n"})
        assert resp.status_code == 500

    def test_delete_exception(self, client, env, monkeypatch):
        p = _prompt(env["user"], "de")
        self._boom_user(monkeypatch)
        resp = client.delete(f"{BASE}/{p.id}", headers=env["headers"])
        assert resp.status_code == 500

    def test_project_prompts_exception(self, client, env, monkeypatch):
        self._boom_user(monkeypatch)
        assert client.get(f"{BASE}/project-prompts",
                          headers=env["headers"]).status_code == 500

    def test_task_button_prompts_exception(self, client, env, monkeypatch):
        self._boom_user(monkeypatch)
        assert client.get(f"{BASE}/task-button-prompts",
                          headers=env["headers"]).status_code == 500

    def test_reorder_exception(self, client, env, monkeypatch):
        monkeypatch.setattr(
            CustomPrompt, "reorder_task_buttons",
            classmethod(lambda cls, *a: (_ for _ in ()).throw(
                RuntimeError("down"))))
        resp = client.put(f"{BASE}/task-buttons/reorder",
                          headers=env["headers"],
                          json={"prompt_orders": [{"id": 1, "order_index": 1}]})
        assert resp.status_code == 500

    def test_preview_exception(self, client, env, monkeypatch):
        p = _prompt(env["user"], "pe", PromptType.PROJECT)
        monkeypatch.setattr(
            "api.custom_prompts.get_current_user",
            lambda: (_ for _ in ()).throw(RuntimeError("db")))
        resp = client.post(f"{BASE}/project-prompts/{p.id}/preview",
                           headers=env["headers"], json={})
        assert resp.status_code == 500

    def test_initialize_exception(self, client, env, monkeypatch):
        monkeypatch.setattr(
            CustomPrompt, "initialize_user_defaults",
            classmethod(lambda cls, *a, **kw: (_ for _ in ()).throw(
                RuntimeError("seed down"))))
        resp = client.post(f"{BASE}/initialize-defaults",
                           headers=env["headers"], json={})
        assert resp.status_code == 500

    def test_reset_exception(self, client, env, monkeypatch):
        monkeypatch.setattr(
            CustomPrompt, "initialize_user_defaults",
            classmethod(lambda cls, *a, **kw: (_ for _ in ()).throw(
                RuntimeError("seed down"))))
        resp = client.post(f"{BASE}/reset-to-defaults",
                           headers=env["headers"], json={})
        assert resp.status_code == 500

    def test_export_exception(self, client, env, monkeypatch):
        self._boom_user(monkeypatch)
        assert client.get(f"{BASE}/export",
                          headers=env["headers"]).status_code == 500

    def test_import_per_item_exception_counted(self, client, env, monkeypatch):
        _prompt(env["user"], "exists")
        calls = {"n": 0}

        def flaky(cls, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("row down")
            return None  # 路由不使用返回值
        monkeypatch.setattr(CustomPrompt, "create_prompt", classmethod(flaky))

        resp = client.post(f"{BASE}/import", headers=env["headers"], json={
            "prompts": [
                {"prompt_type": "project", "name": "boom-item",
                 "content": "c"},                       # create_prompt 抛异常 → 跳过
                {"prompt_type": "project", "name": "ok2",
                 "content": "c"},                       # 成功
            ]})
        import json as _json
        print("BODY:", _json.dumps(resp.get_json(), ensure_ascii=False))
        print("CALLS:", calls)
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["skipped_count"] == 1 and data["imported_count"] == 1

    def test_import_exception_maps_500(self, client, env, monkeypatch):
        self._boom_user(monkeypatch)
        resp = client.post(f"{BASE}/import", headers=env["headers"],
                           json={"prompts": []})
        assert resp.status_code == 500


class TestExportImport:
    def test_export_shape(self, client, env):
        _prompt(env["user"], "e1")
        resp = client.get(f"{BASE}/export", headers=env["headers"])
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["user_id"] == env["user"].id
        assert [p["name"] for p in data["prompts"]] == ["e1"]

    def test_import_missing_prompts_field(self, client, env):
        resp = client.post(f"{BASE}/import", headers=env["headers"],
                           json={})
        assert resp.status_code == 400

    def test_import_non_list_rejected(self, client, env):
        resp = client.post(f"{BASE}/import", headers=env["headers"],
                           json={"prompts": "x"})
        assert resp.status_code == 400

    def test_import_mixed_results(self, client, env):
        _prompt(env["user"], "exists")
        resp = client.post(f"{BASE}/import", headers=env["headers"], json={
            "prompts": [
                {"prompt_type": "project", "name": "fresh",
                 "content": "c"},                      # 导入
                {"prompt_type": "project", "name": "exists",
                 "content": "c"},                      # 重名跳过
                {"name": "no-type", "content": "c"},   # 缺字段跳过
                {"prompt_type": "bogus", "name": "x",
                 "content": "c"},                      # 坏类型跳过
            ]})
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data == {"imported_count": 1, "skipped_count": 3,
                        "total_count": 4}
