"""API Token 管理（api/api_tokens.py）单元回归。

覆盖：Token CRUD（列表脱敏、重名 400、原始 token 仅创建时返回一次、
更新名称/描述/过期/启停、物理删除）、reveal 解密（损坏密文 400、
他人/停用 404）、verify（MCP 认证：无效/过期 401、usage_count 自增、
用户公开信息）。
历史注记：本模块原带全库零引用的 require_api_token_auth 装饰器
（MCP 实际使用 api/mcp/auth.py 的同名实现），已删除；
UserProjectPin 的零引用类方法 get_user_pins / reorder_pins 一并删除
（路由各自内联实现同样逻辑）。
"""

import uuid
from datetime import datetime, timedelta

import pytest
from flask_jwt_extended import create_access_token

from models import ApiToken, User, db


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


def _uh(prefix="tk"):
    u = User(username=f"{prefix}_{uuid.uuid4().hex[:8]}",
             email=f"{prefix}_{uuid.uuid4().hex[:6]}@t.io")
    db.session.add(u)
    db.session.flush()
    return u


def _headers_for(user):
    return {"Authorization": f"Bearer {create_access_token(identity=str(user.id))}"}


@pytest.fixture
def env(_isolated_app):
    user = _uh("ow")
    other = _uh("ot")
    db.session.commit()
    return {
        "user": user, "other": other,
        "headers": _headers_for(user),
        "base": "/todo-for-ai/api/v1/api-tokens",
    }


def _mk_token(user, name="tk", expires_days=None, active=True):
    # 名字保持原样：重名测试依赖精确名称匹配
    api_token, raw = ApiToken.generate_token(
        name=name, expires_days=expires_days)
    api_token.user_id = user.id
    api_token.is_active = active
    db.session.add(api_token)
    db.session.commit()
    return api_token, raw


class TestTokenCrud:
    def test_list_empty(self, env, client):
        resp = client.get(env["base"], headers=env["headers"])
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["items"] == [] and data["pagination"]["total"] == 0

    def test_create_requires_name(self, env, client):
        resp = client.post(env["base"], headers=env["headers"], json={})
        assert resp.status_code == 400
        assert "name is required" in resp.get_json()["message"]

    def test_create_returns_raw_once(self, env, client):
        resp = client.post(env["base"], headers=env["headers"],
                           json={"name": "my-token",
                                 "description": "d",
                                 "expires_days": 30})
        assert resp.status_code == 201
        data = resp.get_json()["data"]
        assert data["token"].startswith("todo4ai-")
        assert "token_hash" not in data
        assert data["expires_at"] is not None

        row = db.session.get(ApiToken, data["id"])
        assert row.user_id == env["user"].id
        # 原始 token 能通过哈希校验
        assert ApiToken.verify_token(data["token"]) is not None

    def test_create_duplicate_active_name(self, env, client):
        _mk_token(env["user"], name="dup")
        resp = client.post(env["base"], headers=env["headers"],
                           json={"name": "dup"})
        assert resp.status_code == 400
        assert "already exists" in resp.get_json()["message"]

    def test_duplicate_allowed_after_deactivation(self, env, client):
        row, _ = _mk_token(env["user"], name="dup")
        row.is_active = False
        db.session.commit()
        resp = client.post(env["base"], headers=env["headers"],
                           json={"name": row.name})
        assert resp.status_code == 201

    def test_update_flow(self, env, client):
        row, _ = _mk_token(env["user"], name="old")
        other, _ = _mk_token(env["user"], name="taken")
        url = f"{env['base']}/{row.id}"

        assert client.put(f"{env['base']}/999999",
                          headers=env["headers"],
                          json={"name": "x"}).status_code == 404
        assert client.put(url, headers=env["headers"],
                          json={}).status_code == 400
        assert client.put(url, headers=env["headers"],
                          json={"name": other.name}
                          ).status_code == 400

        resp = client.put(url, headers=env["headers"], json={
            "name": "renamed", "description": "new desc",
            "expires_days": 7, "is_active": False})
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["name"] == "renamed"
        assert data["is_active"] is False
        db.session.expire_all()
        refreshed = db.session.get(ApiToken, row.id)
        assert refreshed.description == "new desc"
        assert refreshed.expires_at > datetime.utcnow() + timedelta(days=6)

        # expires_days 置空 → 取消过期
        client.put(url, headers=env["headers"],
                   json={"expires_days": None})
        db.session.expire_all()
        assert db.session.get(ApiToken, row.id).expires_at is None

    def test_update_other_users_token_404(self, env, client):
        row, _ = _mk_token(env["other"], name="theirs")
        for method, url, payload in (
            ("put", f"{env['base']}/{row.id}", {"name": "x"}),
            ("get", f"{env['base']}/{row.id}/reveal", None),
            ("delete", f"{env['base']}/{row.id}", None),
        ):
            resp = getattr(client, method)(url, headers=env["headers"],
                                           json=payload)
            assert resp.status_code == 404, url

    def test_reveal_and_delete(self, env, client):
        row, raw = _mk_token(env["user"], name="rv")
        resp = client.get(f"{env['base']}/{row.id}/reveal",
                          headers=env["headers"])
        assert resp.status_code == 200
        assert resp.get_json()["data"]["token"] == raw

        # 停用后不可 reveal
        row.is_active = False
        db.session.commit()
        assert client.get(f"{env['base']}/{row.id}/reveal",
                          headers=env["headers"]).status_code == 404

        row.is_active = True
        db.session.commit()
        assert client.delete(f"{env['base']}/{row.id}",
                             headers=env["headers"]).status_code == 200
        assert db.session.get(ApiToken, row.id) is None

    def test_reveal_corrupted_ciphertext_400(self, env, client):
        row, _ = _mk_token(env["user"], name="bad")
        row.token_encrypted = "not-a-valid-ciphertext"
        db.session.commit()
        resp = client.get(f"{env['base']}/{row.id}/reveal",
                          headers=env["headers"])
        assert resp.status_code == 400
        assert "Unable to decrypt" in resp.get_json()["message"]

    def test_reveal_inactive_404(self, env, client):
        row, _ = _mk_token(env["user"], name="off", active=False)
        resp = client.get(f"{env['base']}/{row.id}/reveal",
                          headers=env["headers"])
        assert resp.status_code == 404


class TestVerifyToken:
    def _url(self, env):
        return f"{env['base']}/verify"

    def test_missing_token_400(self, env, client):
        resp = client.post(self._url(env), json={})
        assert resp.status_code == 400

    def test_invalid_token_401(self, env, client):
        resp = client.post(self._url(env), json={"token": "bogus"})
        assert resp.status_code == 401

    def test_valid_token_increments_usage(self, env, client):
        row, raw = _mk_token(env["user"], name="vc")
        resp = client.post(self._url(env), json={"token": raw})
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["user"]["id"] == env["user"].id
        assert "token_hash" not in data["token"]
        db.session.expire_all()
        assert db.session.get(ApiToken, row.id).usage_count == 1

    def test_expired_token_401(self, env, client):
        row, raw = _mk_token(env["user"], name="exp")
        row.expires_at = datetime.utcnow() - timedelta(days=1)
        db.session.commit()
        resp = client.post(self._url(env), json={"token": raw})
        assert resp.status_code == 401

    def test_endpoints_500_matrix(self, env, client, monkeypatch):
        """全部 6 个端点的 catch-all 500 兜底。"""
        from models import ApiToken as Token

        # list：to_dict 抛错
        monkeypatch.setattr(Token, "to_dict",
                            lambda self, include_sensitive=False: 1 / 0)
        row, raw = _mk_token(env["user"], name="lst")
        assert client.get(env["base"],
                          headers=env["headers"]).status_code == 500
        monkeypatch.undo()

        # create：generate_token 抛错
        monkeypatch.setattr(
            Token, "generate_token",
            classmethod(lambda cls, *a, **kw: 1 / 0))
        assert client.post(env["base"], headers=env["headers"],
                           json={"name": "x"}).status_code == 500
        monkeypatch.undo()

        # update / delete：commit 抛错（不能 patch ApiToken.query——
        # unified_auth 对 Bearer 一律先试 token 认证，会把认证链炸穿）
        from models import db as models_db
        row_u, _ = _mk_token(env["user"], name="up")
        row_d, _ = _mk_token(env["user"], name="del")
        session_obj = models_db.session()
        monkeypatch.setattr(session_obj, "commit", lambda: 1 / 0)
        assert client.put(f"{env['base']}/{row_u.id}",
                          headers=env["headers"],
                          json={"name": "x"}).status_code == 500
        assert client.delete(f"{env['base']}/{row_d.id}",
                             headers=env["headers"]).status_code == 500
        monkeypatch.undo()

        # reveal：get_decrypted_token 抛错
        monkeypatch.setattr(Token, "get_decrypted_token",
                            lambda self: 1 / 0)
        row2, _ = _mk_token(env["user"], name="rv2")
        assert client.get(f"{env['base']}/{row2.id}/reveal",
                          headers=env["headers"]).status_code == 500
        monkeypatch.undo()

        # verify：verify_token 抛错
        monkeypatch.setattr(
            Token, "verify_token",
            classmethod(lambda cls, token: 1 / 0))
        assert client.post(f"{env['base']}/verify", json={"token": "t"}
                           ).status_code == 500

    def test_dead_decorator_removed(self):
        from api import api_tokens as mod
        assert not hasattr(mod, "require_api_token_auth")
