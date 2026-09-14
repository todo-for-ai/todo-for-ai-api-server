"""OpenAI API 缓存管理器：双级缓存（Redis + 内存）+ 分布式锁一致性（原样搬移）。"""

import time
import json
import uuid
import hashlib
from typing import Dict, Optional

from flask import g

from api import openai_compatible as _pkg
from api.openai_compatible._core import (
    CACHE_TTL_SECONDS,
    CACHE_KEY_PREFIX,
    CACHE_CONSISTENCY_LOCK_PREFIX,
    CACHE_INVALIDATION_CHANNEL,
)


class OpenAICacheManager:
    """
    OpenAI API 缓存管理器

    特性：
    - 双级缓存 (Redis + 内存)
    - 缓存一致性保障 (分布式锁、失效广播)
    - 缓存穿透保护
    """

    def __init__(self):
        self._local_cache: Dict[str, Dict] = {}
        self._local_cache_ttl = 60  # 本地缓存60秒

    def _generate_cache_key(self, feature: str, params: Dict) -> str:
        """生成缓存key"""
        key_data = json.dumps(params, sort_keys=True, ensure_ascii=False)
        hash_val = hashlib.sha256(key_data.encode()).hexdigest()[:32]
        return f"{CACHE_KEY_PREFIX}{feature}:{hash_val}"

    def _acquire_lock(self, key: str, timeout: int = 10) -> bool:
        """获取分布式锁 (防止缓存击穿)"""
        redis_client = _pkg.get_redis_client()
        if not redis_client:
            return True  # Redis不可用，跳过锁

        lock_key = f"{CACHE_CONSISTENCY_LOCK_PREFIX}{key}"
        lock_value = str(uuid.uuid4())

        try:
            # NX: 只有key不存在时才设置, EX: 设置过期时间
            acquired = redis_client.set(lock_key, lock_value, nx=True, ex=timeout)
            if acquired:
                # 将锁值存入 g，用于释放
                g.cache_lock_value = lock_value
                return True
            return False
        except Exception:
            return True  # 出错时允许继续

    def _release_lock(self, key: str):
        """释放分布式锁"""
        redis_client = _pkg.get_redis_client()
        if not redis_client:
            return

        lock_key = f"{CACHE_CONSISTENCY_LOCK_PREFIX}{key}"
        lock_value = getattr(g, 'cache_lock_value', None)

        if not lock_value:
            return

        try:
            # 使用Lua脚本确保原子性释放
            lua_script = """
            if redis.call("get", KEYS[1]) == ARGV[1] then
                return redis.call("del", KEYS[1])
            else
                return 0
            end
            """
            redis_client.eval(lua_script, 1, lock_key, lock_value)
        except Exception:
            pass

    def get(self, feature: str, params: Dict) -> Optional[Dict]:
        """
        获取缓存

        策略：
        1. 先查本地缓存
        2. 再查Redis缓存
        3. 返回并回填本地缓存
        """
        cache_key = self._generate_cache_key(feature, params)
        now = time.time()

        # 1. 检查本地缓存
        if cache_key in self._local_cache:
            entry = self._local_cache[cache_key]
            if entry['expires_at'] > now:
                entry['hits'] += 1
                return entry['data']
            else:
                del self._local_cache[cache_key]

        # 2. 检查Redis缓存
        try:
            data = _pkg.get_json(cache_key)
            if data:
                # 回填本地缓存
                self._local_cache[cache_key] = {
                    'data': data,
                    'expires_at': now + self._local_cache_ttl,
                    'hits': 1
                }
                return data
        except Exception:
            pass

        return None

    def set(self, feature: str, params: Dict, data: Dict, ttl: int = None):
        """
        设置缓存

        策略：
        1. 写入Redis (主存储)
        2. 更新本地缓存
        3. 发布失效广播 (如果是更新操作)
        """
        if ttl is None:
            ttl = CACHE_TTL_SECONDS

        cache_key = self._generate_cache_key(feature, params)
        now = time.time()

        # 1. 写入Redis
        try:
            _pkg.set_json(cache_key, data, ttl)
        except Exception:
            pass

        # 2. 更新本地缓存
        self._local_cache[cache_key] = {
            'data': data,
            'expires_at': now + self._local_cache_ttl,
            'hits': 0
        }

    def invalidate(self, feature: str = None, params: Dict = None):
        """
        使缓存失效

        支持：
        - 按feature批量失效
        - 按精确key失效
        - 分布式广播失效
        """
        redis_client = _pkg.get_redis_client()

        if params:
            # 精确失效
            cache_key = self._generate_cache_key(feature, params)
            self._local_cache.pop(cache_key, None)
            if redis_client:
                try:
                    redis_client.delete(cache_key)
                    # 广播失效消息
                    redis_client.publish(CACHE_INVALIDATION_CHANNEL, cache_key)
                except Exception:
                    pass
        elif feature:
            # 按feature批量失效
            keys_to_remove = [
                k for k in self._local_cache.keys()
                if k.startswith(f"{CACHE_KEY_PREFIX}{feature}:")
            ]
            for k in keys_to_remove:
                del self._local_cache[k]

            if redis_client:
                try:
                    pattern = f"{CACHE_KEY_PREFIX}{feature}:*"
                    cursor = 0
                    while True:
                        cursor, keys = redis_client.scan(cursor, match=pattern, count=100)
                        if keys:
                            redis_client.delete(*keys)
                            for key in keys:
                                redis_client.publish(CACHE_INVALIDATION_CHANNEL, key)
                        if cursor == 0:
                            break
                except Exception:
                    pass
        else:
            # 全部失效
            self._local_cache.clear()

    def get_with_lock(self, feature: str, params: Dict) -> tuple[Optional[Dict], bool]:
        """
        带锁的缓存获取

        返回: (缓存数据, 是否获取到锁)
        - 如果有缓存，返回(数据, False)
        - 如果没缓存且获取到锁，返回(None, True)
        - 如果没缓存且没获取到锁，返回(None, False) - 需要等待
        """
        cache_key = self._generate_cache_key(feature, params)

        # 先尝试获取缓存
        data = self.get(feature, params)
        if data:
            return data, False

        # 尝试获取锁
        if self._acquire_lock(cache_key):
            # 获取锁后再次检查缓存 (双重检查)
            data = self.get(feature, params)
            if data:
                self._release_lock(cache_key)
                return data, False
            return None, True

        return None, False

    def release_lock_after_set(self, feature: str, params: Dict):
        """设置缓存后释放锁"""
        cache_key = self._generate_cache_key(feature, params)
        self._release_lock(cache_key)
