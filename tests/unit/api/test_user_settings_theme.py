"""用户皮肤（theme）设置的端点回归测试。

皮肤系统到人级别持久化：user_settings.theme 存色板 id，
GET /user-settings 透出、PUT /user-settings 更新（格式校验）。
"""

import uuid

import pytest

from app import create_app
from models import db

BASE_URL = "/todo-for-ai/api/v1/user-settings"


@pytest.fixture(scope="function", autouse=True)
def _isolated_app():
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
def auth(_isolated_app):
    from flask_jwt_extended import create_access_token
    from models import User

    user = User(username=f"u_{uuid.uuid4().hex[:8]}", email=f"u_{uuid.uuid4().hex[:6]}@t.io")
    db.session.add(user)
    db.session.commit()
    with _isolated_app.app_context():
        token = create_access_token(identity=str(user.id))
    return {"user": user, "headers": {"Authorization": f"Bearer {token}"}}


class TestUserThemeSettings:
    def test_default_theme_is_sky(self, client, auth):
        resp = client.get(BASE_URL, headers=auth["headers"])
        assert resp.status_code == 200
        assert resp.get_json()["data"]["theme"] == "sky"

    def test_update_theme(self, client, auth):
        resp = client.put(BASE_URL, json={"theme": "gameboy"}, headers=auth["headers"])
        assert resp.status_code == 200
        assert resp.get_json()["data"]["theme"] == "gameboy"

        # GET 回读（到人级别持久化）
        resp = client.get(BASE_URL, headers=auth["headers"])
        assert resp.get_json()["data"]["theme"] == "gameboy"

    def test_update_theme_invalid_format_rejected(self, client, auth):
        resp = client.put(BASE_URL, json={"theme": "BAD THEME!"}, headers=auth["headers"])
        assert resp.status_code == 400

    def test_update_theme_too_long_rejected(self, client, auth):
        resp = client.put(BASE_URL, json={"theme": "x" * 33}, headers=auth["headers"])
        assert resp.status_code == 400

    def test_theme_isolated_per_user(self, client, auth):
        from flask_jwt_extended import create_access_token
        from models import User

        other = User(username=f"u_{uuid.uuid4().hex[:8]}", email=f"u_{uuid.uuid4().hex[:6]}@t.io")
        db.session.add(other)
        db.session.commit()
        other_headers = {"Authorization": f"Bearer {create_access_token(identity=str(other.id))}"}

        client.put(BASE_URL, json={"theme": "fc"}, headers=auth["headers"])

        resp = client.get(BASE_URL, headers=other_headers)
        assert resp.get_json()["data"]["theme"] == "sky"
