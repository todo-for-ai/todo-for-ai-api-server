"""AI 响应缓存（内存 + Redis 双级，支持 DB 动态 TTL）。"""

import hashlib
import json
import threading
import time
from typing import Any, Dict, Optional

from services.ai.config import get_ai_config


class AIResponseCache:
    """AI 响应缓存（内存 + Redis 双级缓存，支持动态配置）"""

    def __init__(self, ttl: int = None):
        self._initial_ttl = ttl
        self.ttl = ttl or 300
        self._memory_cache: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._config_initialized = False

    def _ensure_config(self):
        """确保配置已加载（延迟初始化）"""
        if not self._config_initialized:
            try:
                config = get_ai_config()
                if self._initial_ttl is None:
                    self.ttl = config.get('cache_ttl', 300)
            except Exception:
                # 配置加载失败，使用默认值
                pass
            self._config_initialized = True

    def _generate_key(self, feature: str, params: Dict[str, Any]) -> str:
        """生成缓存 key"""
        key_data = json.dumps(params, sort_keys=True, ensure_ascii=False)
        return f"ai:{feature}:{hashlib.sha256(key_data.encode()).hexdigest()[:32]}"

    def get(self, feature: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """获取缓存"""
        self._ensure_config()

        key = self._generate_key(feature, params)

        with self._lock:
            if key in self._memory_cache:
                entry = self._memory_cache[key]
                if entry['expires_at'] > time.time():
                    entry['hits'] += 1
                    return entry['data']
                else:
                    del self._memory_cache[key]

        # 尝试从 Redis 获取（如果可用）
        try:
            from core.redis_client import get_json
            data = get_json(key)
            if data:
                # 回填内存缓存
                with self._lock:
                    self._memory_cache[key] = {
                        'data': data,
                        'expires_at': time.time() + self.ttl,
                        'hits': 1
                    }
                return data
        except Exception:
            pass

        return None

    def set(self, feature: str, params: Dict[str, Any], data: Dict[str, Any]):
        """设置缓存"""
        self._ensure_config()

        key = self._generate_key(feature, params)

        with self._lock:
            self._memory_cache[key] = {
                'data': data,
                'expires_at': time.time() + self.ttl,
                'hits': 0
            }

        # 写入 Redis（如果可用）
        try:
            from core.redis_client import set_json
            set_json(key, data, self.ttl)
        except Exception:
            pass

    def invalidate(self, feature: str = None):
        """清除缓存"""
        self._ensure_config()

        with self._lock:
            if feature:
                keys_to_remove = [
                    k for k in self._memory_cache.keys()
                    if k.startswith(f"ai:{feature}:")
                ]
                for k in keys_to_remove:
                    del self._memory_cache[k]
            else:
                self._memory_cache.clear()

    def get_stats(self) -> Dict[str, Any]:
        """获取缓存统计"""
        self._ensure_config()

        with self._lock:
            total = len(self._memory_cache)
            expired = sum(
                1 for e in self._memory_cache.values()
                if e['expires_at'] <= time.time()
            )
            hits = sum(e['hits'] for e in self._memory_cache.values())
            return {
                "total_entries": total,
                "expired_entries": expired,
                "total_hits": hits
            }
