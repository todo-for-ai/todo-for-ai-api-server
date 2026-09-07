"""滑动窗口限流器（支持 DB 动态配置）。"""

import threading
import time
from typing import Any, Dict, List

from services.ai.config import get_ai_config


class RateLimiter:
    """滑动窗口限流器（支持动态配置）"""

    def __init__(self, max_requests: int = None, window_size: int = None):
        # 初始使用传入值或默认值，稍后从数据库读取
        self._initial_max_requests = max_requests
        self._initial_window_size = window_size
        self.max_requests = max_requests or 60
        self.window_size = window_size or 60
        self.requests: Dict[str, List[float]] = {}
        self._lock = threading.Lock()
        self._config_initialized = False

    def _ensure_config(self):
        """确保配置已加载（延迟初始化）"""
        if not self._config_initialized:
            try:
                config = get_ai_config()
                if self._initial_max_requests is None:
                    self.max_requests = config.get('rate_limit_requests', 60)
                if self._initial_window_size is None:
                    self.window_size = config.get('rate_limit_window', 60)
            except Exception:
                # 配置加载失败，使用默认值
                pass
            self._config_initialized = True

    def is_allowed(self, key: str) -> tuple:
        """
        检查是否允许请求
        返回: (是否允许, 剩余配额)
        """
        self._ensure_config()

        now = time.time()
        window_start = now - self.window_size

        with self._lock:
            # 清理过期请求
            if key in self.requests:
                self.requests[key] = [
                    ts for ts in self.requests[key] if ts > window_start
                ]
            else:
                self.requests[key] = []

            # 检查配额
            current_count = len(self.requests[key])
            if current_count >= self.max_requests:
                retry_after = int(self.requests[key][0] + self.window_size - now)
                return False, retry_after

            # 记录请求
            self.requests[key].append(now)
            remaining = self.max_requests - current_count - 1
            return True, remaining

    def get_stats(self, key: str) -> Dict[str, Any]:
        """获取限流统计"""
        self._ensure_config()

        now = time.time()
        window_start = now - self.window_size

        with self._lock:
            if key not in self.requests:
                return {"current": 0, "remaining": self.max_requests}

            valid_requests = [
                ts for ts in self.requests[key] if ts > window_start
            ]
            return {
                "current": len(valid_requests),
                "remaining": self.max_requests - len(valid_requests),
                "window_size": self.window_size
            }
