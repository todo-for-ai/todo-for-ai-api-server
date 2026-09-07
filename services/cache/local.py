"""L1 进程内缓存：LRU 淘汰 + 过期清理。"""

import threading
import time
from typing import Any, Dict, Optional

from services.cache.constants import LOCAL_CACHE_TTL


class LocalCache:
    """
    本地内存缓存 (L1缓存)

    使用LRU策略，支持过期清理
    """

    def __init__(self, max_size: int = 10000, ttl: int = LOCAL_CACHE_TTL):
        self.max_size = max_size
        self.ttl = ttl
        self._cache: Dict[str, Dict] = {}
        self._lock = threading.RLock()
        self._access_count = 0
        self._hit_count = 0

    def get(self, key: str) -> Optional[Any]:
        """获取缓存"""
        with self._lock:
            self._access_count += 1

            if key not in self._cache:
                return None

            entry = self._cache[key]
            if entry['expires_at'] < time.time():
                # 已过期
                del self._cache[key]
                return None

            # 更新访问时间 (LRU)
            entry['last_access'] = time.time()
            entry['hits'] += 1
            self._hit_count += 1

            return entry['data']

    def set(self, key: str, data: Any, ttl: int = None):
        """设置缓存"""
        if ttl is None:
            ttl = self.ttl

        with self._lock:
            # 清理过期项
            self._cleanup_expired()

            # LRU清理: 如果超过最大大小，移除最久未访问的
            if len(self._cache) >= self.max_size:
                self._evict_lru()

            self._cache[key] = {
                'data': data,
                'expires_at': time.time() + ttl,
                'last_access': time.time(),
                'hits': 0
            }

    def delete(self, key: str):
        """删除缓存"""
        with self._lock:
            self._cache.pop(key, None)

    def clear(self):
        """清空缓存"""
        with self._lock:
            self._cache.clear()

    def _cleanup_expired(self):
        """清理过期项"""
        now = time.time()
        expired_keys = [
            k for k, v in self._cache.items()
            if v['expires_at'] < now
        ]
        for k in expired_keys:
            del self._cache[k]

    def _evict_lru(self):
        """LRU淘汰"""
        if not self._cache:
            return

        # 找到最久未访问的
        lru_key = min(self._cache.keys(), key=lambda k: self._cache[k]['last_access'])
        del self._cache[lru_key]

    def get_stats(self) -> Dict:
        """获取统计信息"""
        with self._lock:
            return {
                'size': len(self._cache),
                'max_size': self.max_size,
                'access_count': self._access_count,
                'hit_count': self._hit_count,
                'hit_rate': self._hit_count / self._access_count if self._access_count > 0 else 0
            }
