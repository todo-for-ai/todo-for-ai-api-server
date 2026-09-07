"""AI 基础设施包（services/ai/）单元回归。

覆盖：容错配置缓存/回退、限流滑窗与统计、双级缓存（内存/Redis 回填/失效/
统计）、审计缓冲与落库失败重排、LLM 调用全分支（限流/缓存/缺 key/成功/
解析失败/429/401/400 各变体/连接与读取超时/网络/未知异常）。
拆分自 ai_service.py 的行为钉子测试——拆包不改变任何外部行为。
"""

import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

from services.ai import audit_logger as audit_mod
from services.ai import config as ai_config_mod
from services.ai import rate_limiter as rl_mod
from services.ai import response_cache as rc_mod
from services.ai.config import (
    DEFAULT_CONFIG,
    get_ai_config,
    invalidate_ai_config_cache,
)
from services.ai.errors import AIErrorCode, AIRequestContext
from services.ai.llm_service import LLMService, call_llm_production, llm_service
from services.ai.rate_limiter import RateLimiter
from services.ai.response_cache import AIResponseCache


@pytest.fixture(autouse=True)
def _no_flush_threads(monkeypatch):
    """定时刷新线程替换为假线程（不真跑 while 循环），但创建路径仍被覆盖。"""
    monkeypatch.setattr(
        audit_mod, "Thread",
        MagicMock(side_effect=lambda target=None, **kw: SimpleNamespace(
            start=MagicMock(), target=target)))


@pytest.fixture(autouse=True)
def _reset_ai_config_cache():
    invalidate_ai_config_cache()
    yield
    invalidate_ai_config_cache()


class TestAIConfig:
    def test_reads_db_and_caches(self, monkeypatch):
        calls = []

        def fake_db():
            calls.append(1)
            return {'connect_timeout': 5}
        monkeypatch.setattr(
            "models.system_settings.SystemSettings.get_ai_resilience_config",
            staticmethod(fake_db))

        first = get_ai_config()
        second = get_ai_config()
        assert first == {'connect_timeout': 5}
        assert second == {'connect_timeout': 5}
        assert len(calls) == 1  # TTL 内走缓存
        first['connect_timeout'] = 999  # 返回的是副本
        assert get_ai_config()['connect_timeout'] == 5

    def test_invalidate_forces_reread(self, monkeypatch):
        calls = []

        def fake_db():
            calls.append(1)
            return {'rate_limit_window': 30}
        monkeypatch.setattr(
            "models.system_settings.SystemSettings.get_ai_resilience_config",
            staticmethod(fake_db))
        get_ai_config()
        invalidate_ai_config_cache()
        get_ai_config()
        assert len(calls) == 2

    def test_db_failure_falls_back_to_defaults(self, monkeypatch):
        def boom():
            raise RuntimeError("no db")
        monkeypatch.setattr(
            "models.system_settings.SystemSettings.get_ai_resilience_config",
            staticmethod(boom))
        assert get_ai_config() == DEFAULT_CONFIG


class TestAIRequestContext:
    def test_defaults_and_to_dict(self):
        ctx = AIRequestContext(request_id="r1", user_id=1, user_email="e@t.io",
                               feature="f")
        assert ctx.error_code is AIErrorCode.SUCCESS
        assert ctx.created_at is not None
        data = ctx.to_dict()
        assert data["error_code"] == "SUCCESS"
        assert data["created_at"] == ctx.created_at.isoformat()
        assert data["prompt_tokens"] == 0

    def test_custom_created_at_preserved(self):
        from datetime import datetime
        ts = datetime(2026, 9, 7)
        ctx = AIRequestContext(request_id="r", user_id=1, user_email="",
                               feature="f", created_at=ts)
        assert ctx.created_at is ts


class TestRateLimiter:
    def test_allows_up_to_limit_then_denies(self):
        rl = RateLimiter(max_requests=2, window_size=60)
        assert rl.is_allowed("k") == (True, 1)
        assert rl.is_allowed("k") == (True, 0)
        denied, retry_after = rl.is_allowed("k")
        assert denied is True or denied is False
        assert denied is False
        assert isinstance(retry_after, int)

    def test_expired_records_pruned(self):
        rl = RateLimiter(max_requests=1, window_size=60)
        rl.requests["k"] = [time.time() - 120]  # 窗口外的旧请求
        allowed, remaining = rl.is_allowed("k")
        assert allowed is True
        assert remaining == 0

    def test_get_stats(self):
        rl = RateLimiter(max_requests=5, window_size=60)
        assert rl.get_stats("none") == {"current": 0, "remaining": 5}
        rl.is_allowed("k")
        stats = rl.get_stats("k")
        assert stats == {"current": 1, "remaining": 4, "window_size": 60}

    def test_config_failure_keeps_defaults(self, monkeypatch):
        def boom():
            raise RuntimeError("db down")
        monkeypatch.setattr(rl_mod, "get_ai_config", boom)
        rl = RateLimiter()
        allowed, _ = rl.is_allowed("k")
        assert allowed is True
        assert rl.max_requests == 60 and rl.window_size == 60
        assert rl._config_initialized is True

    def test_dynamic_config_applies_once(self, monkeypatch):
        monkeypatch.setattr(rl_mod, "get_ai_config",
                            lambda: {'rate_limit_requests': 3,
                                     'rate_limit_window': 10})
        rl = RateLimiter()
        rl.is_allowed("k")
        assert rl.max_requests == 3 and rl.window_size == 10


class TestAIResponseCache:
    @pytest.fixture(autouse=True)
    def _isolate_redis(self, monkeypatch):
        """全量门禁下早前测试会连上真实 Redis 单例——这里替换为进程内假实现，
        避免内存缓存失效后从真实 Redis 读回旧值。"""
        store = {}
        monkeypatch.setattr("core.redis_client.get_json",
                            lambda key: store.get(key))
        monkeypatch.setattr("core.redis_client.set_json",
                            lambda key, data, ttl=None: store.update({key: data}))

    def test_set_get_roundtrip_and_hits(self):
        cache = AIResponseCache(ttl=60)
        cache.set("f", {"q": 1}, {"answer": 42})
        assert cache.get("f", {"q": 1}) == {"answer": 42}
        assert cache.get("f", {"q": 1}) == {"answer": 42}
        stats = cache.get_stats()
        assert stats["total_entries"] == 1
        assert stats["total_hits"] == 2

    def test_expired_entry_deleted_on_get(self, monkeypatch):
        monkeypatch.setattr("core.redis_client.get_json", lambda k: None)
        cache = AIResponseCache(ttl=60)
        cache.set("f", {"q": 1}, {"a": 1})
        key = cache._generate_key("f", {"q": 1})
        cache._memory_cache[key]["expires_at"] = time.time() - 1
        assert cache.get("f", {"q": 1}) is None
        assert key not in cache._memory_cache

    def test_redis_backfills_memory(self, monkeypatch):
        monkeypatch.setattr("core.redis_client.get_json",
                            lambda key: {"from": "redis"})
        cache = AIResponseCache(ttl=60)
        assert cache.get("f", {"q": 2}) == {"from": "redis"}
        key = cache._generate_key("f", {"q": 2})
        assert cache._memory_cache[key]["data"] == {"from": "redis"}
        assert cache._memory_cache[key]["hits"] == 1

    def test_redis_get_failure_silent(self, monkeypatch):
        def boom(key):
            raise RuntimeError("no redis")
        monkeypatch.setattr("core.redis_client.get_json", boom)
        cache = AIResponseCache(ttl=60)
        assert cache.get("f", {"q": 3}) is None

    def test_set_writes_redis(self, monkeypatch):
        recorded = {}
        monkeypatch.setattr("core.redis_client.set_json",
                            lambda key, data, ttl: recorded.update(
                                key=key, data=data, ttl=ttl))
        cache = AIResponseCache(ttl=77)
        cache.set("f", {"q": 4}, {"a": 4})
        assert recorded["ttl"] == 77
        assert recorded["data"] == {"a": 4}
        assert recorded["key"].startswith("ai:f:")

    def test_set_redis_failure_silent(self, monkeypatch):
        def boom(key, data, ttl):
            raise RuntimeError("no redis")
        monkeypatch.setattr("core.redis_client.set_json", boom)
        cache = AIResponseCache(ttl=60)
        cache.set("f", {"q": 5}, {"a": 5})  # 不抛
        assert cache.get("f", {"q": 5}) == {"a": 5}

    def test_invalidate_by_feature_and_all(self, monkeypatch):
        monkeypatch.setattr("core.redis_client.get_json", lambda k: None)
        cache = AIResponseCache(ttl=60)
        cache.set("fa", {"q": 1}, {"a": 1})
        cache.set("fb", {"q": 2}, {"a": 2})
        cache.invalidate(feature="fa")
        assert cache.get("fa", {"q": 1}) is None
        assert cache.get("fb", {"q": 2}) == {"a": 2}
        cache.invalidate()
        assert cache.get("fb", {"q": 2}) is None

    def test_stats_counts_expired(self):
        cache = AIResponseCache(ttl=60)
        cache.set("f", {"q": 1}, {"a": 1})
        key = cache._generate_key("f", {"q": 1})
        cache._memory_cache[key]["expires_at"] = time.time() - 1
        stats = cache.get_stats()
        assert stats == {"total_entries": 1, "expired_entries": 1,
                         "total_hits": 0}

    def test_dynamic_ttl_and_failure_fallback(self, monkeypatch):
        monkeypatch.setattr(rc_mod, "get_ai_config",
                            lambda: {'cache_ttl': 42})
        cache = AIResponseCache()
        cache._ensure_config()
        assert cache.ttl == 42

        def boom():
            raise RuntimeError("db down")
        monkeypatch.setattr(rc_mod, "get_ai_config", boom)
        cache2 = AIResponseCache()
        cache2._ensure_config()
        assert cache2.ttl == 300


class TestAIAuditLogger:
    def _ctx(self, request_id="r"):
        return AIRequestContext(request_id=request_id, user_id=1,
                                user_email="e@t.io", feature="f")

    def test_log_buffers_and_flush_writes_db(self):
        logger = audit_mod.AIAuditLogger()
        logger.log(self._ctx("r1"))
        assert len(logger._buffer) == 1

        rows = []

        class FakeLog:
            def __init__(self, **kw):
                rows.append(kw)

        fake_db = SimpleNamespace(
            session=SimpleNamespace(add=lambda row: None, commit=lambda: None))
        with patch("models.AIRequestLog", FakeLog), \
                patch("models.db", fake_db):
            logger.flush()
        assert len(rows) == 1
        assert rows[0]["request_id"] == "r1"
        assert rows[0]["error_code"] == AIErrorCode.SUCCESS.value
        assert logger._buffer == []

    def test_flush_failure_requeues(self, capsys):
        logger = audit_mod.AIAuditLogger()
        logger.log(self._ctx("r1"))

        def boom():
            raise RuntimeError("db down")
        fake_db = SimpleNamespace(
            session=SimpleNamespace(add=lambda row: None, commit=boom))
        with patch("models.AIRequestLog", MagicMock()), \
                patch("models.db", fake_db):
            logger.flush()
        assert "[AI Audit] Failed to save logs" in capsys.readouterr().out
        assert len(logger._buffer) == 1  # 已重排回缓冲

    def test_bulk_log_triggers_flush(self):
        logger = audit_mod.AIAuditLogger()
        logger.flush = MagicMock()
        for i in range(100):
            logger.log(self._ctx(f"r{i}"))
        logger.flush.assert_called()

    def test_flush_empty_noop(self):
        logger = audit_mod.AIAuditLogger()
        with patch("models.AIRequestLog", MagicMock()) as fake_log:
            logger.flush()
        fake_log.assert_not_called()

    def test_flush_timer_loop_flushes_then_waits(self, monkeypatch):
        """周期闭包体：sleep → flush → 循环；第二次 sleep 抛出以退出 while True。"""
        started = []

        def fake_thread(target=None, **kw):
            instance = SimpleNamespace(start=MagicMock(), target=target)
            started.append(instance)
            return instance
        monkeypatch.setattr(audit_mod, "Thread", fake_thread)

        logger = audit_mod.AIAuditLogger()
        logger.flush = MagicMock()
        target = started[0].target

        class _Break(Exception):
            pass
        ticks = {"n": 0}

        def fake_sleep(seconds):
            ticks["n"] += 1
            if ticks["n"] >= 2:
                raise _Break()
        monkeypatch.setattr(audit_mod, "time", SimpleNamespace(sleep=fake_sleep))
        with pytest.raises(_Break):
            target()
        logger.flush.assert_called_once()
        # 定时线程本身已按 daemon 启动（假实现）
        started[0].start.assert_called_once()


@pytest.fixture
def service():
    s = LLMService()
    s.rate_limiter = MagicMock()
    s.rate_limiter.is_allowed.return_value = (True, 5)
    s.cache = MagicMock()
    s.cache.get.return_value = None
    return s


def _resp(status_code=200, payload=None, text="", json_raises=False):
    mock_json = MagicMock()
    if json_raises:
        mock_json.side_effect = json.JSONDecodeError("Expecting value", "doc", 0)
    else:
        mock_json.return_value = payload if payload is not None else {}
    return SimpleNamespace(status_code=status_code, text=text, json=mock_json)


class TestLLMHelpers:
    def test_request_id_format(self, service):
        import re
        assert re.match(r"^ai-[0-9a-f]{16}-\d{10}$",
                        service._generate_request_id())

    def test_create_session_mounts_retry_adapter(self, service):
        with patch("services.ai.llm_service.requests.Session") as session_cls:
            session = session_cls.return_value
            result = service._create_session({'max_retries': 5,
                                              'retry_backoff_factor': 1.0,
                                              'max_retry_wait_time': 60})
        assert result is session
        mounted = {c.args[0] for c in session.mount.call_args_list}
        assert mounted == {"http://", "https://"}

    def test_get_config_from_db_and_fallback(self, service):
        with patch("models.SystemSettings.get_llm_config",
                   return_value={'api_key': 'k'}):
            assert service._get_config() == {'api_key': 'k'}

        def boom():
            raise RuntimeError("x")
        with patch("models.SystemSettings.get_llm_config", side_effect=boom):
            cfg = service._get_config()
        assert cfg['provider'] == 'openai'
        assert cfg['api_key'] == ''

    def test_session_recreated_on_config_change(self, service, monkeypatch):
        configs = [{'max_retries': 5}, {'max_retries': 9}, {'max_retries': 9}]
        monkeypatch.setattr("services.ai.llm_service.get_ai_config",
                            lambda: configs.pop(0))
        with patch("services.ai.llm_service.requests.Session") as session_cls:
            session_cls.side_effect = [MagicMock(), MagicMock(), MagicMock()]
            s1 = service._get_session()
            s2 = service._get_session()  # 配置变化 → 重建
            s3 = service._get_session()  # 配置未变 → 复用
        assert session_cls.call_count == 2
        assert s1 is not s2 and s2 is s3

    def test_create_session_loads_config_when_none(self, service, monkeypatch):
        monkeypatch.setattr("services.ai.llm_service.get_ai_config",
                            lambda: dict(DEFAULT_CONFIG))
        with patch("services.ai.llm_service.requests.Session") as session_cls:
            service._create_session()
        session_cls.assert_called_once()

    def test_error_result_variants(self, service):
        ctx = AIRequestContext(request_id="r", user_id=1, user_email="",
                               feature="f")
        start = time.time()
        base = service._error_result(ctx, start)
        assert base["success"] is False
        assert "retry_after" not in base

        full = service._error_result(ctx, start, AIErrorCode.TIMEOUT,
                                     "msg", retry_after=9, raw="x")
        assert full["error_code"] == AIErrorCode.TIMEOUT.value
        assert full["retry_after"] == 9
        assert full["raw"] == "x"
        assert ctx.latency_ms >= 0


class TestLLMGuards:
    def test_rate_limit_allowed_returns_none(self, service):
        ctx = AIRequestContext(request_id="r", user_id=1, user_email="",
                               feature="f")
        assert service._guard_rate_limit(ctx, 1, "f") is None
        service.rate_limiter.is_allowed.assert_called_once_with("1:f")

    def test_rate_limit_denied_builds_error(self, service):
        service.rate_limiter.is_allowed.return_value = (False, 12)
        ctx = AIRequestContext(request_id="r", user_id=1, user_email="",
                               feature="f")
        result = service._guard_rate_limit(ctx, 1, "f")
        assert result["success"] is False
        assert result["retry_after"] == 12
        assert result["error_code"] == AIErrorCode.RATE_LIMIT_EXCEEDED.value
        assert "Retry after 12s" in result["error"]

    def test_cache_guard_skipped_without_params(self, service):
        ctx = AIRequestContext(request_id="r", user_id=1, user_email="",
                               feature="f")
        assert service._guard_cache(ctx, "f", True, None, time.time()) is None
        assert service._guard_cache(ctx, "f", False, {"q": 1}, time.time()) is None
        service.cache.get.assert_not_called()

    def test_cache_guard_miss(self, service):
        service.cache.get.return_value = None
        ctx = AIRequestContext(request_id="r", user_id=1, user_email="",
                               feature="f")
        assert service._guard_cache(ctx, "f", True, {"q": 1}, time.time()) is None

    def test_cache_guard_hit_dict_with_content(self, service):
        service.cache.get.return_value = {"content": "hello"}
        ctx = AIRequestContext(request_id="r", user_id=1, user_email="",
                               feature="f")
        result = service._guard_cache(ctx, "f", True, {"q": 1}, time.time())
        assert result == {"success": True, "data": "hello", "cached": True,
                          "context": ctx.to_dict()}
        assert ctx.cache_hit is True

    def test_cache_guard_hit_raw_string_and_contentless_dict(self, service):
        ctx = AIRequestContext(request_id="r", user_id=1, user_email="",
                               feature="f")
        service.cache.get.return_value = "plain"
        assert service._guard_cache(ctx, "f", True, {"q": 1},
                                    time.time())["data"] == "plain"
        service.cache.get.return_value = {"other": 1}
        result = service._guard_cache(ctx, "f", True, {"q": 1}, time.time())
        assert result["data"] == {"other": 1}


class TestHTTPErrorMapping:
    def _ctx(self):
        return AIRequestContext(request_id="r", user_id=1, user_email="",
                                feature="f")

    def test_429(self, service):
        ctx = self._ctx()
        service._apply_http_error(_resp(429), ctx)
        assert ctx.error_code is AIErrorCode.RATE_LIMIT_EXCEEDED
        assert ctx.error_message == "Provider rate limit exceeded"

    def test_401(self, service):
        ctx = self._ctx()
        service._apply_http_error(_resp(401), ctx)
        assert ctx.error_code is AIErrorCode.API_KEY_INVALID

    def test_400_insufficient_quota_in_json(self, service):
        ctx = self._ctx()
        service._apply_http_error(
            _resp(400, {"error": {"message": "insufficient_quota"}}), ctx)
        assert ctx.error_code is AIErrorCode.INSUFFICIENT_FUNDS

    def test_400_insufficient_quota_in_text(self, service):
        ctx = self._ctx()
        service._apply_http_error(
            _resp(400, text="your quota ran out insufficient_quota",
                  json_raises=True), ctx)
        assert ctx.error_code is AIErrorCode.INSUFFICIENT_FUNDS

    def test_400_error_message(self, service):
        ctx = self._ctx()
        service._apply_http_error(
            _resp(400, {"error": {"message": "bad model"}}), ctx)
        assert ctx.error_code is AIErrorCode.UNKNOWN_ERROR
        assert ctx.error_message == "bad model"

    def test_400_plain_text(self, service):
        ctx = self._ctx()
        service._apply_http_error(
            _resp(400, text="oops long text " * 30, json_raises=True), ctx)
        assert ctx.error_message.startswith("Bad request: oops long text")

    def test_400_valid_json_without_error_key(self, service):
        ctx = self._ctx()
        service._apply_http_error(_resp(400, payload={}), ctx)
        assert ctx.error_message == "Bad request"

    def test_400_non_json_empty_text(self, service):
        # 行为钉子：空 text 的回退文案本身也是 "Bad request"
        ctx = self._ctx()
        service._apply_http_error(_resp(400, text="", json_raises=True), ctx)
        assert ctx.error_message == "Bad request: Bad request"

    def test_unknown_status_with_detail(self, service):
        ctx = self._ctx()
        service._apply_http_error(
            _resp(503, {"error": {"message": "overloaded"}}), ctx)
        assert ctx.error_message == "API returned 503: overloaded"

    def test_unknown_status_json_failure_uses_text(self, service):
        ctx = self._ctx()
        service._apply_http_error(
            _resp(500, text="server exploded " * 5, json_raises=True), ctx)
        assert ctx.error_message.startswith("API returned 500: server exploded")

    def test_unknown_status_no_detail(self, service):
        ctx = self._ctx()
        service._apply_http_error(_resp(500, text="", json_raises=True), ctx)
        assert ctx.error_message == "API returned 500"


class TestSuccessHandling:
    def test_parse_error_includes_raw_response(self, service):
        ctx = AIRequestContext(request_id="r", user_id=1, user_email="",
                               feature="f")
        result = service._handle_success(
            ctx, "f", _resp(200, text="<html>bad</html>", json_raises=True),
            time.time(), True, {"q": 1})
        assert result["success"] is False
        assert result["error_code"] == AIErrorCode.PARSE_ERROR.value
        assert result["raw_response"] == "<html>bad</html>"

    def test_success_writes_usage_and_cache(self, service):
        payload = {
            "choices": [{"message": {"content": "answer"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 5,
                      "total_tokens": 8},
            "model": "gpt-4",
        }
        ctx = AIRequestContext(request_id="r", user_id=1, user_email="",
                               feature="f")
        result = service._handle_success(ctx, "f", _resp(200, payload),
                                         time.time(), True, {"q": 1})
        assert result["success"] is True
        assert result["data"] == "answer"
        assert result["usage"]["total_tokens"] == 8
        assert result["model"] == "gpt-4"
        assert ctx.total_tokens == 8
        service.cache.set.assert_called_once_with("f", {"q": 1},
                                                  {"content": "answer"})

    def test_success_without_cache_params_skips_set(self, service):
        payload = {"choices": [{"message": {"content": "x"}}], "usage": {}}
        ctx = AIRequestContext(request_id="r", user_id=1, user_email="",
                               feature="f")
        service._handle_success(ctx, "f", _resp(200, payload), time.time(),
                                True, None)
        service.cache.set.assert_not_called()


class TestExecuteChat:
    @staticmethod
    def _inject_session(service, response):
        """注入 mock session（绕过 _get_session 的配置版本重建）。"""
        session = MagicMock()
        session.post.return_value = response
        service._get_session = lambda: session
        return session

    def test_sends_expected_request(self, service, monkeypatch):
        monkeypatch.setattr("services.ai.llm_service.get_ai_config",
                            lambda: {'connect_timeout': 7, 'read_timeout': 33})
        response = _resp(200, {"choices": [{"message": {"content": "ok"}}],
                               "usage": {}})
        session = self._inject_session(service, response)

        ctx = AIRequestContext(request_id="ai-req-1", user_id=1,
                               user_email="", feature="f")
        config = {'api_base': 'https://llm.example/v1/', 'api_key': 'sk-1',
                  'model': 'm1', 'temperature': 0.2, 'max_tokens': 100}
        result = service._execute_chat(
            ctx, "f", [{"role": "user", "content": "hi"}], config,
            time.time(), "ai-req-1", use_cache=False, cache_params=None,
            temperature=None, max_tokens=None, response_format="json")

        assert result["success"] is True
        args = session.post.call_args
        assert args.args[0] == "https://llm.example/v1/chat/completions"
        assert args.kwargs["headers"]["Authorization"] == "Bearer sk-1"
        assert args.kwargs["headers"]["X-Request-ID"] == "ai-req-1"
        assert args.kwargs["timeout"] == (7, 33)
        payload = args.kwargs["json"]
        assert payload["model"] == "m1"
        assert payload["response_format"] == {"type": "json_object"}

    def test_non_200_returns_error_result(self, service, monkeypatch):
        monkeypatch.setattr("services.ai.llm_service.get_ai_config",
                            lambda: {'connect_timeout': 7, 'read_timeout': 33})
        self._inject_session(service, _resp(429))

        ctx = AIRequestContext(request_id="r", user_id=1, user_email="",
                               feature="f")
        result = service._execute_chat(
            ctx, "f", [], {'api_key': 'k'}, time.time(), "r",
            use_cache=False, cache_params=None, temperature=None,
            max_tokens=None, response_format=None)
        assert result["success"] is False
        assert result["error_code"] == AIErrorCode.RATE_LIMIT_EXCEEDED.value


class TestLLMCallEndToEnd:
    def test_rate_limited(self, service):
        service.rate_limiter.is_allowed.return_value = (False, 3)
        result = service.call("f", [{"role": "user", "content": "x"}],
                              user_id=9)
        assert result["error_code"] == AIErrorCode.RATE_LIMIT_EXCEEDED.value
        assert result["retry_after"] == 3

    def test_cache_hit(self, service):
        service.cache.get.return_value = {"content": "cached!"}
        result = service.call("f", [], use_cache=True,
                              cache_params={"q": 1})
        assert result == {"success": True, "data": "cached!", "cached": True,
                          "context": result["context"]}

    def test_missing_api_key(self, service, monkeypatch):
        monkeypatch.setattr(service, "_get_config", lambda: {'api_key': ''})
        result = service.call("f", [])
        assert result["error_code"] == AIErrorCode.CONFIG_NOT_FOUND.value

    def test_success_flow(self, service, monkeypatch):
        monkeypatch.setattr(service, "_get_config",
                            lambda: {'api_key': 'k', 'model': 'm'})
        session = MagicMock()
        session.post.return_value = _resp(
            200, {"choices": [{"message": {"content": "ok"}}],
                  "usage": {"total_tokens": 4}, "model": "m"})
        service._get_session = lambda: session
        result = service.call("f", [{"role": "user", "content": "x"}],
                              use_cache=False)
        assert result["success"] is True
        assert result["data"] == "ok"
        session.post.assert_called_once()

    def _timeout_case(self, service, exc, needle, retry_after):
        monkeypatch = pytest.MonkeyPatch()
        try:
            monkeypatch.setattr(service, "_get_config",
                                lambda: {'api_key': 'k'})
            monkeypatch.setattr("services.ai.llm_service.get_ai_config",
                                lambda: {'connect_timeout': 7,
                                         'read_timeout': 33})
            session = MagicMock()
            session.post.side_effect = exc
            service._get_session = lambda: session
            result = service.call("f", [])
            assert result["error_code"] == AIErrorCode.TIMEOUT.value
            assert result["retry_after"] == retry_after
            assert needle in result["error"]
        finally:
            monkeypatch.undo()

    def test_connect_timeout(self, service):
        self._timeout_case(service, requests.exceptions.ConnectTimeout(),
                           "within 7s", 5)

    def test_read_timeout(self, service):
        self._timeout_case(service, requests.exceptions.ReadTimeout(),
                           "longer than 33s", 10)

    def test_generic_timeout(self, service):
        self._timeout_case(service, requests.exceptions.Timeout(), "timeout", 5)

    def test_network_error(self, service, monkeypatch):
        monkeypatch.setattr(service, "_get_config", lambda: {'api_key': 'k'})
        session = MagicMock()
        session.post.side_effect = requests.exceptions.ConnectionError("refused")
        service._get_session = lambda: session
        result = service.call("f", [])
        assert result["error_code"] == AIErrorCode.NETWORK_ERROR.value
        assert "refused" in result["error"]

    def test_unexpected_exception(self, service, monkeypatch):
        monkeypatch.setattr(service, "_get_config", lambda: {'api_key': 'k'})
        monkeypatch.setattr(service, "_execute_chat",
                            lambda *a, **kw: (_ for _ in ()).throw(
                                RuntimeError("bug")))
        result = service.call("f", [])
        assert result["error_code"] == AIErrorCode.UNKNOWN_ERROR.value
        assert "bug" in result["error"]


class TestProductionEntry:
    def test_call_llm_production_delegates(self, monkeypatch):
        captured = {}

        def fake_call(**kw):
            captured.update(kw)
            return {"success": True}
        monkeypatch.setattr(llm_service, "call", fake_call)
        result = call_llm_production("f", [{"role": "user", "content": "c"}],
                                     user_id=3, temperature=0.1)
        assert result == {"success": True}
        assert captured["feature"] == "f"
        assert captured["user_id"] == 3
        assert captured["temperature"] == 0.1

    def test_facade_reexports_same_singleton(self):
        import services.ai_service as facade
        import services.ai.llm_service as impl
        assert facade.llm_service is impl.llm_service
        assert facade.AIErrorCode is AIErrorCode
        assert facade.get_ai_config is get_ai_config
