"""高并发缓存包（services/cache/）单元回归。

覆盖：分布式锁（无 Redis 降级/获取/阻塞超时/Lua 释放/续期循环）、L1 本地缓存
（命中/过期/LRU 淘汰/统计）、L1+L2 编排（回填/防击穿双重检查/批量/模式失效/
统计）、cached 装饰器与门面单例。全部以假 Redis/假线程驱动，无真实 IO。
"""

import hashlib
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from services.cache import constants as cache_constants
from services.cache import core as cache_core
from services.cache import local as local_mod
from services.cache import lock as lock_mod
from services.cache.constants import DEFAULT_TTL, LOCAL_CACHE_TTL, LOCK_TIMEOUT
from services.cache.core import (
    CacheException,
    HighPerformanceCache,
    cached,
    cache_service,
    clear_cache,
    get_cache,
    get_cache_stats,
)
from services.cache.local import LocalCache
from services.cache.lock import DistributedLock


class _FakeThread:
    """记录 start 的假线程（不真跑 target）。"""

    instances = []

    def __init__(self, target=None, daemon=None):
        self.target = target
        self.daemon = daemon
        self.started = False
        _FakeThread.instances.append(self)

    def start(self):
        self.started = True

    def join(self, timeout=None):
        pass

    @classmethod
    def reset(cls):
        cls.instances = []


@pytest.fixture
def fake_lock_threading(monkeypatch):
    """锁模块的 threading 命名空间：Thread 假实现，其余保持真实现。"""
    _FakeThread.reset()
    fake_ns = SimpleNamespace(Thread=_FakeThread, Event=threading.Event,
                              current_thread=threading.current_thread,
                              RLock=threading.RLock)
    monkeypatch.setattr(lock_mod, "threading", fake_ns)
    return _FakeThread


class TestDistributedLock:
    def test_acquire_without_redis_returns_true(self):
        lock = DistributedLock(None, "k")
        assert lock.acquire() is True
        lock.release()  # 未真正获取，应为 no-op

    def test_acquire_success_starts_renewal(self, fake_lock_threading):
        redis = MagicMock()
        redis.set.return_value = True
        lock = DistributedLock(redis, "order:1", timeout=LOCK_TIMEOUT)
        assert lock.acquire() is True
        kwargs = redis.set.call_args.kwargs
        assert kwargs == {"nx": True, "ex": LOCK_TIMEOUT}
        assert lock.lock_key == "lock:order:1"
        thread = _FakeThread.instances[0]
        assert thread.started is True and thread.daemon is True

    def test_acquire_nonblocking_conflict_returns_false(self, fake_lock_threading):
        redis = MagicMock()
        redis.set.return_value = False
        lock = DistributedLock(redis, "k")
        assert lock.acquire(blocking=False) is False
        assert _FakeThread.instances == []

    def test_acquire_blocking_times_out(self, fake_lock_threading, monkeypatch):
        redis = MagicMock()
        redis.set.return_value = False
        clock = {"t": 0.0}
        sleeps = []
        fake_time = SimpleNamespace(
            time=lambda: clock["t"],
            sleep=lambda s: sleeps.append(s) or clock.update(t=clock["t"] + 0.5))
        monkeypatch.setattr(lock_mod, "time", fake_time)

        lock = DistributedLock(redis, "k")
        assert lock.acquire(blocking=True, blocking_timeout=1.0) is False
        assert redis.set.call_count >= 2
        assert sleeps  # 重试前有等待

    def test_release_noop_states(self, fake_lock_threading):
        lock = DistributedLock(None, "k")
        lock.release()  # 未获取 + 无 redis
        redis = MagicMock()
        redis.set.return_value = True
        lock2 = DistributedLock(redis, "k")
        lock2.release()  # 未获取但有 redis
        redis.eval.assert_not_called()

    def test_release_evals_lua_and_resets(self, fake_lock_threading):
        redis = MagicMock()
        redis.set.return_value = True
        lock = DistributedLock(redis, "k")
        lock.acquire()
        lock.release()
        assert redis.eval.call_count == 1
        assert lock._acquired is False

    def test_release_swallows_eval_failure(self, fake_lock_threading):
        redis = MagicMock()
        redis.set.return_value = True
        redis.eval.side_effect = RuntimeError("redis gone")
        lock = DistributedLock(redis, "k")
        lock.acquire()
        lock.release()  # 不抛
        assert lock._acquired is False

    def test_renewal_loop_extends_until_stop(self, fake_lock_threading):
        redis = MagicMock()
        redis.set.return_value = True
        redis.eval.return_value = 1
        lock = DistributedLock(redis, "k", timeout=9)
        lock.acquire()
        thread = _FakeThread.instances[0]

        waits = iter([False, False, True])
        lock._stop_renewal = SimpleNamespace(wait=lambda t: next(waits))
        thread.target()  # 直接驱动续期循环
        assert redis.eval.call_count == 2

    def test_renewal_loop_breaks_on_eval_error(self, fake_lock_threading):
        redis = MagicMock()
        redis.set.return_value = True
        redis.eval.side_effect = RuntimeError("gone")
        lock = DistributedLock(redis, "k")
        lock.acquire()
        thread = _FakeThread.instances[0]

        waits = iter([False, True])
        lock._stop_renewal = SimpleNamespace(wait=lambda t: next(waits))
        thread.target()  # eval 失败 → break

    def test_renewal_loop_breaks_when_released(self, fake_lock_threading):
        redis = MagicMock()
        redis.set.return_value = True
        lock = DistributedLock(redis, "k")
        lock.acquire()
        lock._acquired = False  # 模拟已被释放
        thread = _FakeThread.instances[0]

        waits = iter([False, True])
        lock._stop_renewal = SimpleNamespace(wait=lambda t: next(waits))
        thread.target()
        redis.eval.assert_not_called()

    def test_context_manager_acquires_and_releases(self, fake_lock_threading):
        redis = MagicMock()
        redis.set.return_value = True
        with DistributedLock(redis, "k") as lock:
            assert lock._acquired is True
        assert lock._acquired is False


class FakeClock:
    """可控时钟：local 模块的 time 替身。"""

    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def fake_local_time(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(local_mod, "time", SimpleNamespace(time=clock.time))
    return clock


class TestLocalCache:
    def test_miss_increments_access(self, fake_local_time):
        cache = LocalCache()
        assert cache.get("nope") is None
        assert cache.get_stats()["access_count"] == 1
        assert cache.get_stats()["hit_rate"] == 0

    def test_roundtrip_and_hits(self, fake_local_time):
        cache = LocalCache()
        cache.set("k", {"v": 1})
        assert cache.get("k") == {"v": 1}
        assert cache.get("k") == {"v": 1}
        stats = cache.get_stats()
        assert stats["hit_count"] == 2
        assert stats["size"] == 1
        assert stats["hit_rate"] == 1.0

    def test_expired_entry_deleted_on_get(self, fake_local_time):
        cache = LocalCache(ttl=10)
        cache.set("k", "v")
        fake_local_time.advance(11)
        assert cache.get("k") is None
        assert "k" not in cache._cache

    def test_delete_and_clear(self, fake_local_time):
        cache = LocalCache()
        cache.set("a", 1)
        cache.set("b", 2)
        cache.delete("a")
        assert cache.get("a") is None
        cache.clear()
        assert cache.get("b") is None

    def test_cleanup_expired_on_set(self, fake_local_time):
        cache = LocalCache(ttl=5)
        cache.set("old", 1)
        fake_local_time.advance(6)
        cache.set("new", 2)  # set 内部清理过期项
        assert "old" not in cache._cache

    def test_lru_eviction(self, fake_local_time):
        cache = LocalCache(max_size=2)
        cache.set("a", 1)
        cache.set("b", 2)
        fake_local_time.advance(1)
        cache.get("a")  # 刷新 a 的最近访问
        fake_local_time.advance(1)
        cache.set("c", 3)  # 容量满 → 淘汰最久未访问的 b
        assert "a" in cache._cache and "c" in cache._cache
        assert "b" not in cache._cache

    def test_evict_lru_empty_cache_guard(self, fake_local_time):
        cache = LocalCache(max_size=0)  # 0 容量：set 时对空缓存触发淘汰守卫
        cache.set("k", 1)
        assert cache.get("k") == 1


@pytest.fixture
def cache(monkeypatch):
    """redis 三件套全部替换为 mock 的 L1+L2 缓存（单一 redis 实例便于断言）。"""
    svc = HighPerformanceCache()
    redis = MagicMock()
    monkeypatch.setattr(cache_core, "get_redis_client", lambda: redis)
    monkeypatch.setattr(cache_core, "redis_get_json", MagicMock(return_value=None))
    monkeypatch.setattr(cache_core, "redis_set_json", MagicMock())
    svc._redis_mock = redis
    return svc


class TestHighPerformanceCache:
    def test_generate_key_format(self, cache):
        key = cache._generate_key("user", "42")
        expected = f"cache:data:user:{hashlib.md5(b'42').hexdigest()[:16]}"
        assert key == expected

    def test_get_l1_hit(self, cache):
        key = cache._generate_key("ns", "id")
        cache._local_cache.set(key, "local-value")
        assert cache.get("ns", "id") == "local-value"

    def test_get_redis_hit_backfills_local(self, cache, monkeypatch):
        monkeypatch.setattr(cache_core, "redis_get_json",
                            MagicMock(return_value={"v": 2}))
        assert cache.get("ns", "id") == {"v": 2}
        key = cache._generate_key("ns", "id")
        assert cache._local_cache.get(key) == {"v": 2}

    def test_get_miss_runs_fallback_and_caches(self, cache, monkeypatch):
        monkeypatch.setattr(cache_core, "DistributedLock",
                            lambda redis, key: MagicMock())
        written = {}
        monkeypatch.setattr(cache_core, "redis_set_json",
                            lambda key, data, ttl: written.update(key=key))
        result = cache.get("ns", "id", fallback=lambda: "computed", ttl=120)
        assert result == "computed"
        assert written["key"] == cache._generate_key("ns", "id")
        assert cache._local_cache.get(cache._generate_key("ns", "id")) == "computed"

    def test_get_fallback_failure_wrapped(self, cache, monkeypatch):
        monkeypatch.setattr(cache_core, "DistributedLock",
                            lambda redis, key: MagicMock())

        def boom():
            raise RuntimeError("db down")
        with pytest.raises(CacheException, match="Fallback execution failed"):
            cache.get("ns", "id", fallback=boom)

    def test_get_miss_without_fallback_returns_none(self, cache):
        assert cache.get("ns", "id") is None

    def test_get_swallows_redis_read_failure(self, cache, monkeypatch):
        monkeypatch.setattr(cache_core, "redis_get_json",
                            MagicMock(side_effect=RuntimeError("boom")))
        monkeypatch.setattr(cache_core, "DistributedLock",
                            lambda redis, key: MagicMock())
        assert cache.get("ns", "id", fallback=lambda: "ok") == "ok"

    def test_get_double_check_hits_local(self, cache, monkeypatch):
        monkeypatch.setattr(cache_core, "DistributedLock",
                            lambda redis, key: MagicMock())
        key = cache._generate_key("ns", "id")
        # 第一次：L1 miss；进入锁后双重检查：L1 hit
        cache._local_cache.get = MagicMock(
            side_effect=[None, "double-check-hit"])
        assert cache.get("ns", "id", fallback=lambda: "x") == "double-check-hit"

    def test_get_double_check_hits_redis(self, cache, monkeypatch):
        monkeypatch.setattr(cache_core, "DistributedLock",
                            lambda redis, key: MagicMock())
        data = MagicMock(side_effect=[None, {"second": "redis"}])
        monkeypatch.setattr(cache_core, "redis_get_json", data)
        cache._local_cache.get = MagicMock(return_value=None)
        assert cache.get("ns", "id", fallback=lambda: "x") == {"second": "redis"}

    def test_set_writes_both_layers(self, cache, monkeypatch):
        written = {}
        monkeypatch.setattr(cache_core, "redis_set_json",
                            lambda key, data, ttl: written.update(
                                key=key, ttl=ttl))
        cache.set("ns", "id", {"v": 1}, ttl=300)
        assert written["ttl"] == 300
        assert cache._local_cache.get(
            cache._generate_key("ns", "id")) == {"v": 1}

    def test_set_local_ttl_capped(self, cache, fake_local_time, monkeypatch):
        monkeypatch.setattr(cache_core, "get_redis_client", lambda: None)
        cache.set("ns", "id", "v", ttl=DEFAULT_TTL, use_local=True)
        entry = cache._local_cache._cache[
            cache._generate_key("ns", "id")]
        assert entry["expires_at"] - fake_local_time.now == LOCAL_CACHE_TTL

    def test_set_redis_failure_silent(self, cache, monkeypatch):
        monkeypatch.setattr(cache_core, "redis_set_json",
                            MagicMock(side_effect=RuntimeError("down")))
        cache.set("ns", "id", "v")  # 不抛
        assert cache._local_cache.get(
            cache._generate_key("ns", "id")) == "v"

    def test_delete_purges_both_and_publishes(self, cache, fake_local_time):
        redis = cache._redis_mock
        cache.set("ns", "id", "v")
        cache.delete("ns", "id")
        redis.delete.assert_called_once_with(cache._generate_key("ns", "id"))
        redis.publish.assert_called_once_with("cache:invalidation",
                                              cache._generate_key("ns", "id"))
        assert cache._local_cache.get(
            cache._generate_key("ns", "id")) is None

    def test_delete_without_redis_still_clears_local(self, cache, monkeypatch):
        monkeypatch.setattr(cache_core, "get_redis_client", lambda: None)
        cache.set("ns", "id", "v")
        cache.delete("ns", "id")
        assert cache._local_cache.get(
            cache._generate_key("ns", "id")) is None

    def test_delete_redis_failure_silent(self, cache):
        redis = cache._redis_mock
        redis.delete.side_effect = RuntimeError("down")
        cache.delete("ns", "id")  # 不抛

    def test_invalidate_pattern_scans_and_publishes(self, cache):
        redis = cache._redis_mock
        redis.scan.side_effect = [(7, ["k1", "k2"]), (0, [])]
        cache.invalidate_pattern("ns", "*")
        redis.delete.assert_called_once_with("k1", "k2")
        assert redis.publish.call_count == 2

    def test_invalidate_all_scans_without_publish(self, cache):
        redis = cache._redis_mock
        redis.scan.side_effect = [(0, ["k1"])]
        cache.invalidate_all()
        redis.delete.assert_called_once_with("k1")
        redis.publish.assert_not_called()

    def test_invalidate_redis_failure_silent(self, cache):
        redis = cache._redis_mock
        redis.scan.side_effect = RuntimeError("down")
        cache.invalidate_pattern("ns")  # 不抛
        cache.invalidate_all()  # 不抛

    def test_invalidate_clears_local(self, cache):
        cache.set("ns", "id", "v")
        cache.invalidate_pattern("ns")
        assert cache._local_cache._cache == {}

    def test_mget_fills_missing_via_fallback(self, cache):
        cache.set("ns", "hit", "cached")
        filled = {}

        def fallback(missing):
            filled["missing"] = list(missing)
            return {"miss-1": "v1", "miss-2": "v2"}

        results = cache.mget("ns", ["hit", "miss-1", "miss-2"], fallback=fallback)
        assert results == {"hit": "cached", "miss-1": "v1", "miss-2": "v2"}
        assert filled["missing"] == ["miss-1", "miss-2"]

    def test_mget_fallback_returns_nothing(self, cache):
        results = cache.mget("ns", ["x"], fallback=lambda missing: None)
        assert results == {}

    def test_mset_loops_set(self, cache, monkeypatch):
        calls = []
        monkeypatch.setattr(cache, "set",
                            lambda ns, ident, data, ttl: calls.append((ns, ident, data, ttl)))
        cache.mset("ns", {"a": 1, "b": 2}, ttl=42)
        assert calls == [("ns", "a", 1, 42), ("ns", "b", 2, 42)]

    def test_get_stats_without_redis(self, cache, monkeypatch):
        monkeypatch.setattr(cache_core, "get_redis_client", lambda: None)
        stats = cache.get_stats()
        assert stats["redis"] == {}
        assert stats["local_cache"]["max_size"] == 10000

    def test_get_stats_with_redis_info(self, cache):
        redis = cache._redis_mock
        redis.info.return_value = {"connected_clients": 3,
                                   "used_memory_human": "1M",
                                   "keyspace_hits": 7,
                                   "keyspace_misses": 3}
        stats = cache.get_stats()
        assert stats["redis"]["connected_clients"] == 3
        assert stats["redis"]["hit_rate"] == 7 / 10 * 100

    def test_get_stats_info_failure_silent(self, cache):
        redis = cache._redis_mock
        redis.info.side_effect = RuntimeError("down")
        assert cache.get_stats()["redis"] == {}


class TestCachedDecorator:
    def test_default_key_uses_func_signature(self, monkeypatch):
        instance = MagicMock()
        # get 直通 fallback，验证装饰器包装的原始函数被执行
        instance.get.side_effect = lambda ns, ident, fb, ttl: fb()
        monkeypatch.setattr(cache_core, "HighPerformanceCache",
                            lambda: instance)

        @cached(namespace="user", ttl=99)
        def get_user(user_id):
            return "real"

        assert get_user(5) == "real"
        args = instance.get.call_args
        assert args.args[0] == "user"
        assert "get_user" in args.args[1]
        assert args.args[3] == 99

    def test_key_func_variant(self, monkeypatch):
        instance = MagicMock()
        instance.get.return_value = "ok"
        monkeypatch.setattr(cache_core, "HighPerformanceCache",
                            lambda: instance)

        @cached(namespace="p", key_func=lambda shop_id, category=None: f"{shop_id}:{category}")
        def get_products(shop_id, category=None):
            return "real"

        assert get_products(1, category="x") == "ok"
        assert instance.get.call_args.args[1] == "1:x"

    def test_cache_delete_and_invalidate_helpers(self, monkeypatch):
        instance = MagicMock()
        monkeypatch.setattr(cache_core, "HighPerformanceCache",
                            lambda: instance)

        @cached(namespace="user", key_func=lambda uid: uid)
        def get_user(uid):
            return uid

        get_user.cache_delete(7)
        instance.delete.assert_called_once_with("user", 7)
        get_user.cache_invalidate()
        instance.invalidate_pattern.assert_called_once_with("user")

    def test_cache_delete_default_key(self, monkeypatch):
        instance = MagicMock()
        monkeypatch.setattr(cache_core, "HighPerformanceCache",
                            lambda: instance)

        @cached(namespace="user")
        def get_user(uid):
            return uid

        get_user.cache_delete(3)
        identifier = instance.delete.call_args.args[1]
        assert identifier.startswith("get_user:")


class TestModuleHelpers:
    def test_get_cache_returns_singleton(self):
        assert get_cache() is cache_service

    def test_clear_cache_invalidates_all(self, monkeypatch):
        fake = MagicMock()
        monkeypatch.setattr(cache_core, "cache_service", fake)
        cache_core.clear_cache()
        fake.invalidate_all.assert_called_once()

    def test_get_cache_stats_delegates(self, monkeypatch):
        fake = MagicMock()
        fake.get_stats.return_value = {"local_cache": {}, "redis": {}}
        monkeypatch.setattr(cache_core, "cache_service", fake)
        assert "local_cache" in cache_core.get_cache_stats()

    def test_constants_unchanged(self):
        assert (DEFAULT_TTL, LOCAL_CACHE_TTL, LOCK_TIMEOUT,
                cache_constants.BATCH_SIZE) == (300, 60, 10, 100)


def test_facade_identity():
    """门面与包实现共享同一批符号与单例。"""
    import services.redis_cache_service as facade
    import services.cache.core as core
    assert facade.cache_service is core.cache_service
    assert facade.HighPerformanceCache is core.HighPerformanceCache
    assert facade.DistributedLock is core.DistributedLock
    assert facade.DEFAULT_TTL == 300
