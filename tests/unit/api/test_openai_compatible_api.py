"""OpenAI 兼容 API（api/openai_compatible.py）单元回归。

覆盖：三档认证装饰器（API Token / Agent Session / JWT 及其降级链）、
缓存管理器（双级读写/分布式锁/失效广播/带锁获取）、chat 请求校验全分支、
chat 非流式与流式（含系统提示词剥离、缓存命中、LLM 失败透传）、
embeddings（str→list 归一/缓存）、usage 聚合、cache 失效管理端点。
Redis 全部打桩为进程内实现。
"""

import time
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from api import openai_compatible as oc
from models import db


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
    yield app
    db.session.remove()
    db.drop_all()
    ctx.pop()


@pytest.fixture(autouse=True)
def _fake_redis(monkeypatch):
    """openai_compatible 的 redis 三件套打桩 + 清空本地缓存。"""
    store = {}

    client = SimpleNamespace(
        set=MagicMock(return_value=True),
        eval=MagicMock(return_value=1),
        delete=MagicMock(),
        publish=MagicMock(),
        scan=MagicMock(side_effect=[(0, [])]),
        info=MagicMock(return_value={}),
    )
    monkeypatch.setattr(oc, "get_redis_client", lambda: client)
    monkeypatch.setattr(oc, "get_json", lambda k: store.get(k))
    monkeypatch.setattr(oc, "set_json",
                        lambda k, v, ttl=None: store.update({k: v}))
    oc.cache_manager._local_cache.clear()
    holder = SimpleNamespace(client=client, store=store)
    yield holder
    oc.cache_manager._local_cache.clear()


@pytest.fixture
def client(_isolated_app):
    return _isolated_app.test_client()


@pytest.fixture
def jwt_user(_isolated_app):
    """真实 User 行 + JWT 头（走装饰器的 JWT 认证分支）。"""
    from flask_jwt_extended import create_access_token
    from models import User
    import uuid

    u = User(username=f"oa_{uuid.uuid4().hex[:8]}",
             email=f"oa_{uuid.uuid4().hex[:6]}@t.io")
    db.session.add(u)
    db.session.commit()
    token = create_access_token(identity=str(u.id))
    return {"user": u, "headers": {"Authorization": f"Bearer {token}"}}


def _stub_user(role="user"):
    from models import UserRole
    return SimpleNamespace(id=7, email="u@t.io", role=UserRole(role),
                           is_active=lambda: True)


class TestAuthDecorator:
    def test_missing_header_401(self, client):
        resp = client.get("/v1/models")
        assert resp.status_code == 401

    def test_bad_scheme_401(self, client):
        resp = client.get("/v1/models", headers={"Authorization": "Basic x"})
        assert resp.status_code == 401

    def test_api_token_branch(self, client, monkeypatch):
        stub_user = _stub_user()
        stub_token = SimpleNamespace(user=stub_user, id=3)
        monkeypatch.setattr("models.ApiToken.verify_token",
                            staticmethod(lambda t: stub_token))
        resp = client.get("/v1/models", headers={"Authorization": "Bearer api-tok"})
        assert resp.status_code == 200

    def test_agent_session_branch(self, client, monkeypatch):
        monkeypatch.setattr("models.ApiToken.verify_token",
                            staticmethod(lambda t: None))
        session = SimpleNamespace(agent_id=5)
        monkeypatch.setattr("models.AgentSession.verify_session_token",
                            classmethod(lambda cls, t: session))
        active_agent = SimpleNamespace(
            status=SimpleNamespace(value="active"))
        monkeypatch.setattr("models.Agent.query",
                            MagicMock(get=MagicMock(return_value=active_agent)))
        resp = client.get("/v1/models", headers={"Authorization": "Bearer sess-tok"})
        assert resp.status_code == 200

    def test_agent_session_inactive_agent_falls_through(self, client, monkeypatch):
        monkeypatch.setattr("models.ApiToken.verify_token",
                            staticmethod(lambda t: None))
        session = SimpleNamespace(agent_id=5)
        monkeypatch.setattr("models.AgentSession.verify_session_token",
                            classmethod(lambda cls, t: session))
        monkeypatch.setattr("models.Agent.query",
                            MagicMock(get=MagicMock(return_value=None)))
        # JWT 也无效 → 401
        resp = client.get("/v1/models", headers={"Authorization": "Bearer sess"})
        assert resp.status_code == 401

    def test_jwt_branch_with_active_user(self, client, jwt_user):
        resp = client.get("/v1/models", headers=jwt_user["headers"])
        assert resp.status_code == 200

    def test_jwt_inactive_user_401(self, client, monkeypatch):
        from flask_jwt_extended import create_access_token
        from models import User
        import uuid
        u = User(username=f"in_{uuid.uuid4().hex[:6]}",
                 email=f"in_{uuid.uuid4().hex[:6]}@t.io")
        db.session.add(u)
        db.session.commit()
        token = create_access_token(identity=str(u.id))
        u.status = "SUSPENDED"
        db.session.commit()

        resp = client.get("/v1/models",
                          headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401


class TestCacheManager:
    def test_key_generation(self):
        manager = oc.OpenAICacheManager()
        key = manager._generate_cache_key("chat", {"a": 1})
        assert key.startswith("openai:chat:")

    def test_local_cache_roundtrip_and_expiry(self, _fake_redis):
        manager = oc.OpenAICacheManager()
        manager.set("chat", {"q": 1}, {"answer": 42})
        assert manager.get("chat", {"q": 1}) == {"answer": 42}

        # 过期 → 本地删除（redis 也清空，避免回填干扰）
        key = manager._generate_cache_key("chat", {"q": 1})
        manager._local_cache[key]["expires_at"] = time.time() - 1
        _fake_redis.store.pop(key, None)
        assert manager.get("chat", {"q": 1}) is None
        assert key not in manager._local_cache

    def test_redis_hit_backfills_local(self, _fake_redis):
        key = oc.cache_manager._generate_cache_key("chat", {"q": 2})
        _fake_redis.store[key] = {"from": "redis"}
        assert oc.cache_manager.get("chat", {"q": 2}) == {"from": "redis"}
        assert oc.cache_manager._local_cache[key]["hits"] == 1

    def test_redis_read_failure_silent(self, _fake_redis, monkeypatch):
        def boom(k):
            raise RuntimeError("redis down")
        monkeypatch.setattr(oc, "get_json", boom)
        assert oc.cache_manager.get("chat", {"q": 3}) is None

    def test_set_writes_both_layers(self, _fake_redis):
        oc.cache_manager.set("chat", {"q": 4}, {"a": 4}, ttl=77)
        key = oc.cache_manager._generate_cache_key("chat", {"q": 4})
        assert key in _fake_redis.store
        assert oc.cache_manager._local_cache[key]["data"] == {"a": 4}

    def test_invalidate_exact_key(self, _fake_redis):
        manager = oc.OpenAICacheManager()
        manager.set("chat", {"q": 9}, {"a": 9})
        key = manager._generate_cache_key("chat", {"q": 9})
        manager.invalidate(feature="chat", params={"q": 9})
        assert key not in manager._local_cache
        _fake_redis.client.delete.assert_called_once_with(key)
        _fake_redis.client.publish.assert_called_once()

    def test_invalidate_by_feature_scans(self, _fake_redis):
        manager = oc.OpenAICacheManager()
        manager.set("chat", {"q": 1}, {"a": 1})
        _fake_redis.client.scan.side_effect = [(0, ["openai:chat:x"])]
        manager.invalidate(feature="chat")
        _fake_redis.client.delete.assert_called_once_with("openai:chat:x")

    def test_invalidate_scan_failure_silent(self, _fake_redis):
        _fake_redis.client.scan.side_effect = RuntimeError("down")
        oc.cache_manager.invalidate(feature="chat")  # 不抛

    def test_invalidate_all_clears_local(self, _fake_redis):
        manager = oc.OpenAICacheManager()
        manager.set("chat", {"q": 1}, {"a": 1})
        manager.invalidate()
        assert manager._local_cache == {}

    def test_lock_lifecycle(self, _fake_redis):
        manager = oc.OpenAICacheManager()
        key = manager._generate_cache_key("chat", {"q": 1})
        assert manager._acquire_lock(key) is True
        manager.release_lock_after_set("chat", {"q": 1})
        _fake_redis.client.eval.assert_called_once()

    def test_lock_without_redis_passes(self, monkeypatch):
        monkeypatch.setattr(oc, "get_redis_client", lambda: None)
        manager = oc.OpenAICacheManager()
        assert manager._acquire_lock("k") is True
        manager._release_lock("k")  # 无 redis no-op

    def test_lock_conflict_and_double_check(self, _fake_redis, monkeypatch):
        manager = oc.OpenAICacheManager()
        _fake_redis.client.set.return_value = False  # 锁被占用
        data, got_lock = manager.get_with_lock("chat", {"q": 1})
        assert data is None and got_lock is False

        # 占用锁后缓存出现（双重检查）→ 返回数据并释放锁
        _fake_redis.client.set.return_value = False
        manager._local_cache[manager._generate_cache_key("chat", {"q": 1})] = {
            "data": {"x": 1}, "expires_at": time.time() + 60, "hits": 0}
        data, got_lock = manager.get_with_lock("chat", {"q": 1})
        assert data == {"x": 1} and got_lock is False

    def test_lock_release_on_double_check_hit(self, _fake_redis, monkeypatch):
        manager = oc.OpenAICacheManager()
        _fake_redis.client.set.return_value = True  # 获取锁成功
        # 第一次 get 未命中，获取锁后的双重检查命中 redis
        responses = iter([None, {"cached": True}])
        monkeypatch.setattr(oc, "get_json", lambda k: next(responses, None))
        data, got_lock = manager.get_with_lock("chat", {"q": 2})
        assert data == {"cached": True} and got_lock is False
        _fake_redis.client.eval.assert_called_once()  # 锁已释放


class TestChatRequestValidation:
    def _handler(self):
        return oc.OpenAIRequestHandler()

    def _base(self, **overrides):
        payload = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "hi"}],
        }
        payload.update(overrides)
        return payload

    def test_missing_messages(self):
        valid, err = self._handler().validate_chat_request({"model": "gpt-4"})
        assert valid is False and "messages" in err

    def test_messages_empty_or_not_list(self):
        for bad in ([], "x"):
            valid, err = self._handler().validate_chat_request(
                self._base(messages=bad))
            assert valid is False

    def test_too_many_messages(self):
        msgs = [{"role": "user", "content": "x"}] * 101
        valid, err = self._handler().validate_chat_request(self._base(messages=msgs))
        assert valid is False and "100" in err

    def test_message_shape_errors(self):
        h = self._handler()
        cases = [
            self._base(messages=["str"]),
            self._base(messages=[{"content": "c"}]),
            self._base(messages=[{"role": "user"}]),
            self._base(messages=[{"role": "wizard", "content": "c"}]),
        ]
        for payload in cases:
            valid, err = h.validate_chat_request(payload)
            assert valid is False

    def test_model_required(self):
        valid, err = self._handler().validate_chat_request(self._base(model=""))
        assert valid is False and "model is required" in err

    @pytest.mark.parametrize("field,lo,hi", [
        ("temperature", 0, 2), ("max_tokens", 1, 32000),
        ("top_p", 0, 1), ("presence_penalty", -2, 2),
        ("frequency_penalty", -2, 2),
    ])
    def test_range_validations(self, field, lo, hi):
        h = self._handler()
        valid, _ = h.validate_chat_request(self._base(**{field: (lo + hi) / 2}))
        assert valid is True
        valid, err = h.validate_chat_request(self._base(**{field: hi + 1}))
        assert valid is False
        valid, _ = h.validate_chat_request(self._base(**{field: lo - 1}))
        assert valid is False

    def test_n_must_be_one(self):
        valid, err = self._handler().validate_chat_request(self._base(n=2))
        assert valid is False and "n=1" in err

    def test_stop_validations(self):
        h = self._handler()
        assert h.validate_chat_request(self._base(stop="ok"))[0] is True
        assert h.validate_chat_request(self._base(stop=["a", "b"]))[0] is True
        assert h.validate_chat_request(
            self._base(stop="x" * 501))[0] is False
        assert h.validate_chat_request(
            self._base(stop=["a", "b", "c", "d", "e"]))[0] is False
        assert h.validate_chat_request(
            self._base(stop=["x" * 501]))[0] is False

    def test_valid_request_passes(self):
        assert self._handler().validate_chat_request(self._base()) == (True, "")


class TestHandlerHelpers:
    def test_request_id_format(self):
        import re
        rid = oc.OpenAIRequestHandler().generate_request_id()
        assert re.match(r"^chatcmpl-[0-9a-f]{24}$", rid)

    def test_cache_params_extraction(self):
        params = oc.OpenAIRequestHandler().build_cache_params(
            {"model": "m", "messages": [], "temperature": 0.2})
        assert params["temperature"] == 0.2
        assert params["top_p"] == 1.0

    def test_create_response_with_and_without_usage(self):
        h = oc.OpenAIRequestHandler()
        resp = h.create_response("hello", "gpt-4")
        assert resp["choices"][0]["message"]["content"] == "hello"
        assert resp["usage"]["total_tokens"] == len("hello") // 4 * 2
        resp2 = h.create_response("x", "gpt-4", usage={"total_tokens": 7})
        assert resp2["usage"] == {"total_tokens": 7}

    def test_stream_chunks(self):
        h = oc.OpenAIRequestHandler()
        import json as _json
        chunk = h.create_stream_chunk("hi", "gpt-4")
        assert chunk.startswith("data: ") and chunk.endswith("\n\n")
        payload = _json.loads(chunk[len("data: "):])
        assert payload["choices"][0]["finish_reason"] is None
        assert payload["choices"][0]["delta"]["content"] == "hi"

        final = h.create_stream_chunk("", "gpt-4", finish_reason="stop")
        final_payload = _json.loads(final[len("data: "):])
        assert final_payload["choices"][0]["finish_reason"] == "stop"


class TestTailBranches:
    """锁异常容错、异步日志线程、各端点异常兜底。"""

    def test_jwt_non_int_identity_401(self, client, _isolated_app):
        from flask_jwt_extended import create_access_token
        with _isolated_app.test_request_context():
            token = create_access_token(identity="not-a-number")
        resp = client.get("/v1/models",
                          headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401

    def test_acquire_lock_exception_allows_continue(self, _fake_redis):
        _fake_redis.client.set.side_effect = RuntimeError("down")
        manager = oc.OpenAICacheManager()
        assert manager._acquire_lock("k") is True  # 出错放行

    def test_release_lock_without_value_noop(self, _fake_redis):
        manager = oc.OpenAICacheManager()
        manager._release_lock("k")  # g 中无锁值
        _fake_redis.client.eval.assert_not_called()

    def test_release_lock_eval_failure_silent(self, _fake_redis):
        manager = oc.OpenAICacheManager()
        key = manager._generate_cache_key("chat", {"q": 1})
        manager._acquire_lock(key)
        _fake_redis.client.eval.side_effect = RuntimeError("eval down")
        manager._release_lock(key)  # 不抛

    def test_set_redis_failure_silent(self, _fake_redis, monkeypatch):
        def boom(k, v, ttl=None):
            raise RuntimeError("down")
        monkeypatch.setattr(oc, "set_json", boom)
        manager = oc.OpenAICacheManager()
        manager.set("chat", {"q": 1}, {"a": 1})
        key = manager._generate_cache_key("chat", {"q": 1})
        assert manager._local_cache[key]["data"] == {"a": 1}

    def test_invalidate_exact_redis_failure_silent(self, _fake_redis):
        manager = oc.OpenAICacheManager()
        manager.set("chat", {"q": 1}, {"a": 1})
        _fake_redis.client.delete.side_effect = RuntimeError("down")
        manager.invalidate(feature="chat", params={"q": 1})  # 不抛

    def test_get_with_lock_acquired_but_still_empty(self, _fake_redis):
        manager = oc.OpenAICacheManager()
        _fake_redis.client.set.return_value = True  # 获取锁成功
        data, got_lock = manager.get_with_lock("chat", {"q": 1})
        assert data is None and got_lock is True  # 持锁方负责回源

    def test_log_request_thread_writes(self, client, jwt_user, monkeypatch):
        rows = []

        class FakeLog:
            def __init__(self, **kw):
                rows.append(kw)

        fake_session = SimpleNamespace(add=lambda r: rows.append(r),
                                       commit=lambda: None,
                                       rollback=lambda: None,
                                       remove=lambda: None)
        monkeypatch.setattr(db, "session", fake_session, raising=False)
        handler = oc.OpenAIRequestHandler()
        handler.log_request(1, "openai_chat", {"q": 1}, 5.0, cache_hit=False)
        time.sleep(0.3)  # 等守护线程完成
        assert len(rows) == 1
        assert rows[0].feature == "openai_chat"
        assert rows[0].cache_hit is False

    def test_log_request_import_failure_silent(self, client, jwt_user,
                                               monkeypatch):
        import sys
        handler = oc.OpenAIRequestHandler()
        monkeypatch.setitem(sys.modules, "models.ai_request_log", None)
        handler.log_request(1, "openai_chat", {}, 5.0)  # 导入失败被外层吞掉

    def test_models_endpoint_exception_500(self, client, jwt_user,
                                           monkeypatch):
        def boom(feature, params):
            raise RuntimeError("cache down")
        monkeypatch.setattr(oc.cache_manager, "get", boom)
        resp = client.get("/v1/models", headers=jwt_user["headers"])
        assert resp.status_code == 500

    def test_get_model_exception_500(self, client, jwt_user, monkeypatch):
        monkeypatch.setattr(oc, "SUPPORTED_MODELS", ["not-a-dict"])
        resp = client.get("/v1/models/gpt-4", headers=jwt_user["headers"])
        assert resp.status_code == 500

    def test_embeddings_exception_500(self, client, jwt_user, monkeypatch):
        def boom(feature, params):
            raise RuntimeError("cache down")
        monkeypatch.setattr(oc.cache_manager, "get", boom)
        resp = client.post("/v1/embeddings", headers=jwt_user["headers"],
                           json={"input": "x"})
        assert resp.status_code == 500

    def test_chat_passes_response_format(self, client, jwt_user, monkeypatch):
        captured = {}

        def fake_llm(**kw):
            captured.update(response_format=kw.get("response_format"))
            return {"success": True, "data": "ok", "usage": {}}
        monkeypatch.setattr("services.ai_service.call_llm_production", fake_llm)
        monkeypatch.setattr(oc.OpenAIRequestHandler, "log_request",
                            lambda self, *a, **kw: None)
        client.post("/v1/chat/completions", headers=jwt_user["headers"], json={
            "model": "gpt-4", "messages": [{"role": "user", "content": "q"}],
            "response_format": {"type": "json_object"}})
        assert captured["response_format"] == {"type": "json_object"}


class TestAgentSessionRoutes:
    """Agent Session 认证后 g.current_user 为 None → 扩展端点 401。"""

    def _agent_session_headers(self, client, monkeypatch):
        monkeypatch.setattr("models.ApiToken.verify_token",
                            staticmethod(lambda t: None))
        session = SimpleNamespace(agent_id=5)
        monkeypatch.setattr("models.AgentSession.verify_session_token",
                            classmethod(lambda cls, t: session))
        active_agent = SimpleNamespace(status=SimpleNamespace(value="active"))
        monkeypatch.setattr("models.Agent.query",
                            MagicMock(get=MagicMock(return_value=active_agent)))
        return {"Authorization": "Bearer sess"}

    def test_usage_401_without_user(self, client, monkeypatch):
        headers = self._agent_session_headers(client, monkeypatch)
        assert client.get("/v1/usage", headers=headers).status_code == 401

    def test_invalidate_401_without_user(self, client, monkeypatch):
        headers = self._agent_session_headers(client, monkeypatch)
        assert client.post("/v1/cache/invalidate", headers=headers,
                           json={}).status_code == 401


class TestModelsEndpoints:
    def test_list_models_and_cache(self, client, jwt_user, _fake_redis):
        resp = client.get("/v1/models", headers=jwt_user["headers"])
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["object"] == "list"
        ids = {m["id"] for m in body["data"]}
        assert "gpt-4" in ids
        # 第二次命中缓存
        resp2 = client.get("/v1/models", headers=jwt_user["headers"])
        assert resp2.get_json() == body

    def test_get_model_found_and_404(self, client, jwt_user):
        headers = jwt_user["headers"]
        assert client.get("/v1/models/gpt-4", headers=headers).status_code == 200
        resp = client.get("/v1/models/nope", headers=headers)
        assert resp.status_code == 404
        assert "Model not found" in resp.get_json()["message"]


class TestChatCompletions:
    def _post(self, client, jwt_user, payload):
        return client.post("/v1/chat/completions", headers=jwt_user["headers"],
                           json=payload)

    def test_validation_error_400(self, client, jwt_user):
        resp = self._post(client, jwt_user, {"model": "gpt-4", "messages": []})
        assert resp.status_code == 400

    def test_non_json_body_400(self, client, jwt_user):
        resp = client.post("/v1/chat/completions",
                           headers=jwt_user["headers"], data="plain",
                           content_type="text/plain")
        assert resp.status_code == 400

    def test_cache_hit_short_circuits(self, client, jwt_user, monkeypatch):
        handler = oc.OpenAIRequestHandler()
        params = handler.build_cache_params(
            {"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]})
        oc.cache_manager.set("chat", params, {"cached": True})

        llm = MagicMock()
        monkeypatch.setattr("services.ai_service.call_llm_production", llm)
        resp = self._post(client, jwt_user, {
            "model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]})
        assert resp.status_code == 200
        assert resp.get_json() == {"cached": True}
        llm.assert_not_called()

    def test_llm_failure_passthrough(self, client, jwt_user, monkeypatch):
        monkeypatch.setattr("services.ai_service.call_llm_production",
                            lambda **kw: {"success": False, "error": "boom",
                                          "error_code": 502})
        resp = self._post(client, jwt_user, {
            "model": "gpt-4", "messages": [{"role": "user", "content": "x"}]})
        assert resp.status_code == 502
        assert "boom" in resp.get_json()["message"]

    def test_success_non_stream_caches(self, client, jwt_user, monkeypatch):
        monkeypatch.setattr("services.ai_service.call_llm_production",
                            lambda **kw: {"success": True, "data": "answer",
                                          "usage": {"total_tokens": 3}})
        monkeypatch.setattr(oc.OpenAIRequestHandler, "log_request",
                            lambda self, *a, **kw: None)
        resp = self._post(client, jwt_user, {
            "model": "gpt-4", "messages": [{"role": "user", "content": "q"}]})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["choices"][0]["message"]["content"] == "answer"
        assert body["usage"]["total_tokens"] == 3
        # 响应已写缓存
        params = oc.OpenAIRequestHandler().build_cache_params(
            {"model": "gpt-4", "messages": [{"role": "user", "content": "q"}]})
        assert oc.cache_manager.get("chat", params) is not None

    def test_system_prompt_stripped_from_llm_messages(self, client, jwt_user,
                                                      monkeypatch):
        captured = {}

        def fake_llm(**kw):
            captured.update(messages=kw["messages"])
            return {"success": True, "data": "ok", "usage": {}}
        monkeypatch.setattr("services.ai_service.call_llm_production", fake_llm)
        monkeypatch.setattr(oc.OpenAIRequestHandler, "log_request",
                            lambda self, *a, **kw: None)
        self._post(client, jwt_user, {
            "model": "gpt-4",
            "messages": [{"role": "system", "content": "sys"},
                         {"role": "user", "content": "q"}]})
        assert captured["messages"] == [{"role": "user", "content": "q"}]

    def test_stream_response(self, client, jwt_user, monkeypatch):
        monkeypatch.setattr("services.ai_service.call_llm_production",
                            lambda **kw: {"success": True, "data": "hello",
                                          "usage": {}})
        monkeypatch.setattr(oc.time, "sleep", lambda s: None)
        monkeypatch.setattr(oc.OpenAIRequestHandler, "log_request",
                            lambda self, *a, **kw: None)
        resp = self._post(client, jwt_user, {
            "model": "gpt-4", "messages": [{"role": "user", "content": "q"}],
            "stream": True})
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert body.count("data: ") >= 2
        assert "[DONE]" in body

    def test_llm_exception_maps_500(self, client, jwt_user, monkeypatch):
        monkeypatch.setattr(
            "services.ai_service.call_llm_production",
            lambda **kw: (_ for _ in ()).throw(RuntimeError("exploded")))
        resp = self._post(client, jwt_user, {
            "model": "gpt-4", "messages": [{"role": "user", "content": "q"}]})
        assert resp.status_code == 500


class TestEmbeddings:
    def test_missing_input_400(self, client, jwt_user):
        resp = client.post("/v1/embeddings", headers=jwt_user["headers"], json={})
        assert resp.status_code == 400

    def test_string_input_coerced_and_cached(self, client, jwt_user):
        resp = client.post("/v1/embeddings", headers=jwt_user["headers"],
                           json={"input": "hello", "model": "ada"})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["model"] == "ada"
        assert len(body["data"]) == 1
        assert len(body["data"][0]["embedding"]) == 1536

        # 二次请求命中缓存（数据一致）
        resp2 = client.post("/v1/embeddings", headers=jwt_user["headers"],
                            json={"input": "hello", "model": "ada"})
        assert resp2.get_json() == body

    def test_list_input(self, client, jwt_user):
        resp = client.post("/v1/embeddings", headers=jwt_user["headers"],
                           json={"input": ["a", "b"]})
        assert len(resp.get_json()["data"]) == 2


class TestUsageAndInvalidate:
    def test_usage_exception_500(self, client, jwt_user, monkeypatch):
        monkeypatch.setattr(
            "api.openai_compatible.get_current_user",
            lambda: (_ for _ in ()).throw(RuntimeError("db")))
        resp = client.get("/v1/usage?days=7", headers=jwt_user["headers"])
        assert resp.status_code == 500

    def test_invalidate_exception_500(self, client, jwt_user, monkeypatch):
        monkeypatch.setattr(
            "api.openai_compatible.get_current_user",
            lambda: (_ for _ in ()).throw(RuntimeError("db")))
        resp = client.post("/v1/cache/invalidate", headers=jwt_user["headers"],
                           json={})
        assert resp.status_code == 500

    def test_usage_requires_user(self, client, monkeypatch):
        monkeypatch.setattr("models.ApiToken.verify_token",
                            staticmethod(lambda t: None))
        monkeypatch.setattr("models.AgentSession.verify_session_token",
                            classmethod(lambda cls, t: None))
        resp = client.get("/v1/usage", headers={"Authorization": "Bearer junk"})
        assert resp.status_code == 401

    def test_usage_aggregates(self, client, jwt_user):
        from models.ai_request_log import AIRequestLog
        from datetime import datetime
        for _ in range(2):
            db.session.add(AIRequestLog(
                request_id=f"r-{uuid.uuid4().hex[:8]}",
                user_id=jwt_user["user"].id, user_email="x@t.io",
                feature="openai_chat", latency_ms=100.0, cache_hit=True,
                error_code=0, created_at=datetime.utcnow(),
            ))
        db.session.commit()
        resp = client.get("/v1/usage?days=7", headers=jwt_user["headers"])
        assert resp.status_code == 200
        stats = resp.get_json()["data"]["stats"]
        assert stats[0]["request_count"] == 2
        assert stats[0]["cache_hit_rate"] == 100.0

    def test_invalidate_requires_user(self, client, monkeypatch):
        monkeypatch.setattr("models.ApiToken.verify_token",
                            staticmethod(lambda t: None))
        monkeypatch.setattr("models.AgentSession.verify_session_token",
                            classmethod(lambda cls, t: None))
        resp = client.post("/v1/cache/invalidate",
                           headers={"Authorization": "Bearer junk"}, json={})
        assert resp.status_code == 401

    def test_invalidate_requires_admin(self, client, jwt_user, monkeypatch):
        # 普通用户（JWT 认证通过）→ 403
        monkeypatch.setattr("api.openai_compatible.get_current_user",
                            lambda: jwt_user["user"])
        resp = client.post("/v1/cache/invalidate",
                           headers=jwt_user["headers"], json={})
        assert resp.status_code == 403
        assert "Admin" in resp.get_json()["message"]

    def test_invalidate_success_with_and_without_feature(self, client,
                                                         jwt_user,
                                                         monkeypatch):
        from models import UserRole
        admin = SimpleNamespace(id=jwt_user["user"].id,
                                role=UserRole.ADMIN)
        monkeypatch.setattr("api.openai_compatible.get_current_user",
                            lambda: admin)
        calls = []

        def fake_invalidate(feature=None, params=None):
            calls.append(feature)
        monkeypatch.setattr(oc.cache_manager, "invalidate", fake_invalidate)

        resp = client.post("/v1/cache/invalidate",
                           headers=jwt_user["headers"],
                           json={"feature": "chat"})
        assert resp.status_code == 200
        assert calls == ["chat"]

        resp = client.post("/v1/cache/invalidate",
                           headers=jwt_user["headers"], json={})
        assert resp.get_json()["data"]["feature"] == "all"
