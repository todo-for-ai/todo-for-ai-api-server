"""L1+L2 缓存编排：本地缓存 + Redis + 分布式锁防击穿 + cached 装饰器。"""

import hashlib
from functools import wraps
from typing import Any, Callable, Dict, List

from core.redis_client import get_redis_client, get_json as redis_get_json, set_json as redis_set_json

from services.cache.constants import DEFAULT_TTL, LOCAL_CACHE_TTL
from services.cache.local import LocalCache
from services.cache.lock import DistributedLock


class CacheException(Exception):
    """缓存异常"""
    pass


class HighPerformanceCache:
    """
    高性能缓存服务

    特性:
    - L1本地缓存 + L2 Redis缓存
    - 分布式锁防止缓存击穿
    - 缓存一致性保障
    - 批量操作
    - 自动降级
    """

    def __init__(self):
        self._local_cache = LocalCache()
        self._lock_prefix = "cache:lock:"
        self._cache_prefix = "cache:data:"
        self._invalidation_channel = "cache:invalidation"

    def _generate_key(self, namespace: str, identifier: str) -> str:
        """生成缓存key"""
        hash_val = hashlib.md5(identifier.encode()).hexdigest()[:16]
        return f"{self._cache_prefix}{namespace}:{hash_val}"

    def _get_redis(self):
        """获取Redis客户端"""
        return get_redis_client()

    def get(self, namespace: str, identifier: str, fallback: Callable = None,
            ttl: int = DEFAULT_TTL, use_local: bool = True) -> Any:
        """
        获取缓存

        Args:
            namespace: 命名空间
            identifier: 缓存标识
            fallback: 缓存未命中时的回掉函数
            ttl: 缓存过期时间
            use_local: 是否使用本地缓存

        Returns:
            缓存数据或fallback结果
        """
        cache_key = self._generate_key(namespace, identifier)

        # 1. 尝试L1本地缓存
        if use_local:
            data = self._local_cache.get(cache_key)
            if data is not None:
                return data

        # 2. 尝试L2 Redis缓存
        redis = self._get_redis()
        if redis:
            try:
                data = redis_get_json(cache_key)
                if data is not None:
                    # 回填本地缓存
                    if use_local:
                        self._local_cache.set(cache_key, data)
                    return data
            except Exception:
                pass

        # 3. 缓存未命中，执行fallback
        if fallback:
            # 使用分布式锁防止缓存击穿
            lock = DistributedLock(redis, f"{namespace}:{identifier}")

            with lock:
                # 双重检查
                if use_local:
                    data = self._local_cache.get(cache_key)
                    if data is not None:
                        return data

                if redis:
                    try:
                        data = redis_get_json(cache_key)
                        if data is not None:
                            if use_local:
                                self._local_cache.set(cache_key, data)
                            return data
                    except Exception:
                        pass

                # 执行fallback
                try:
                    data = fallback()
                except Exception as e:
                    raise CacheException(f"Fallback execution failed: {e}")

                # 写入缓存
                self.set(namespace, identifier, data, ttl, use_local)
                return data

        return None

    def set(self, namespace: str, identifier: str, data: Any,
            ttl: int = DEFAULT_TTL, use_local: bool = True):
        """设置缓存"""
        cache_key = self._generate_key(namespace, identifier)

        # 1. 写入L2 Redis
        redis = self._get_redis()
        if redis:
            try:
                redis_set_json(cache_key, data, ttl)
            except Exception:
                pass

        # 2. 写入L1本地缓存
        if use_local:
            self._local_cache.set(cache_key, data, min(ttl, LOCAL_CACHE_TTL))

    def delete(self, namespace: str, identifier: str):
        """删除缓存"""
        cache_key = self._generate_key(namespace, identifier)

        # 1. 删除本地缓存
        self._local_cache.delete(cache_key)

        # 2. 删除Redis缓存
        redis = self._get_redis()
        if redis:
            try:
                redis.delete(cache_key)
                # 广播失效消息
                redis.publish(self._invalidation_channel, cache_key)
            except Exception:
                pass

    def invalidate_pattern(self, namespace: str, pattern: str = "*"):
        """按模式使缓存失效"""
        pattern_key = f"{self._cache_prefix}{namespace}:{pattern}"

        # 清除本地缓存
        self._local_cache.clear()

        # 清除Redis缓存
        redis = self._get_redis()
        if redis:
            try:
                cursor = 0
                while True:
                    cursor, keys = redis.scan(cursor, match=pattern_key, count=100)
                    if keys:
                        redis.delete(*keys)
                        for key in keys:
                            redis.publish(self._invalidation_channel, key)
                    if cursor == 0:
                        break
            except Exception:
                pass

    def invalidate_all(self):
        """使所有缓存失效"""
        self._local_cache.clear()

        redis = self._get_redis()
        if redis:
            try:
                cursor = 0
                while True:
                    cursor, keys = redis.scan(cursor, match=f"{self._cache_prefix}*", count=100)
                    if keys:
                        redis.delete(*keys)
                    if cursor == 0:
                        break
            except Exception:
                pass

    def mget(self, namespace: str, identifiers: List[str], fallback: Callable = None,
             ttl: int = DEFAULT_TTL) -> Dict[str, Any]:
        """
        批量获取缓存

        Args:
            namespace: 命名空间
            identifiers: 标识列表
            fallback: 回掉函数，接收缺失的标识列表

        Returns:
            标识到数据的映射
        """
        results = {}
        missing = []

        # 1. 从缓存获取
        for identifier in identifiers:
            data = self.get(namespace, identifier, use_local=True)
            if data is not None:
                results[identifier] = data
            else:
                missing.append(identifier)

        # 2. 使用fallback获取缺失的数据
        if missing and fallback:
            missing_data = fallback(missing)
            if missing_data:
                for identifier, data in missing_data.items():
                    self.set(namespace, identifier, data, ttl)
                    results[identifier] = data

        return results

    def mset(self, namespace: str, data_map: Dict[str, Any], ttl: int = DEFAULT_TTL):
        """批量设置缓存"""
        for identifier, data in data_map.items():
            self.set(namespace, identifier, data, ttl)

    def get_stats(self) -> Dict:
        """获取缓存统计"""
        redis = self._get_redis()
        redis_info = {}

        if redis:
            try:
                info = redis.info()
                redis_info = {
                    'connected_clients': info.get('connected_clients', 0),
                    'used_memory_human': info.get('used_memory_human', 'N/A'),
                    'keyspace_hits': info.get('keyspace_hits', 0),
                    'keyspace_misses': info.get('keyspace_misses', 0),
                    'hit_rate': info.get('keyspace_hits', 0) / (info.get('keyspace_hits', 0) + info.get('keyspace_misses', 1)) * 100
                }
            except Exception:
                pass

        return {
            'local_cache': self._local_cache.get_stats(),
            'redis': redis_info
        }


def cached(namespace: str, ttl: int = DEFAULT_TTL, key_func: Callable = None):
    """
    缓存装饰器

    使用方法:
        @cached(namespace='user', ttl=300)
        def get_user(user_id):
            return User.query.get(user_id)

        @cached(namespace='product', key_func=lambda *args, **kwargs: f"{args[0]}:{kwargs.get('category', 'all')}")
        def get_products(shop_id, category=None):
            return Product.query.filter_by(shop_id=shop_id, category=category).all()
    """
    def decorator(func):
        cache = HighPerformanceCache()

        @wraps(func)
        def wrapper(*args, **kwargs):
            # 生成缓存key
            if key_func:
                identifier = key_func(*args, **kwargs)
            else:
                identifier = f"{func.__name__}:{str(args)}:{str(kwargs)}"

            # 获取缓存
            def fallback():
                return func(*args, **kwargs)

            return cache.get(namespace, identifier, fallback, ttl)

        # 添加缓存操作方法
        wrapper.cache_delete = lambda *args, **kwargs: cache.delete(
            namespace,
            key_func(*args, **kwargs) if key_func else f"{func.__name__}:{str(args)}:{str(kwargs)}"
        )
        wrapper.cache_invalidate = lambda: cache.invalidate_pattern(namespace)

        return wrapper
    return decorator


# ============== 全局实例 ==============

cache_service = HighPerformanceCache()


# ============== 辅助函数 ==============

def get_cache() -> HighPerformanceCache:
    """获取全局缓存服务实例"""
    return cache_service


def clear_cache():
    """清空所有缓存"""
    cache_service.invalidate_all()


def get_cache_stats() -> Dict:
    """获取缓存统计"""
    return cache_service.get_stats()
