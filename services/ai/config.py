"""AI 容错配置：DB 读取 + 60s 进程内缓存。"""

import logging
import threading
import time

# 默认配置常量（当数据库配置不存在时使用）
DEFAULT_CONFIG = {
    'connect_timeout': 30,
    'read_timeout': 120,
    'max_retries': 5,
    'retry_backoff_factor': 1.0,
    'max_retry_wait_time': 60,
    'rate_limit_requests': 60,
    'rate_limit_window': 60,
    'cache_ttl': 300
}

# 全局配置缓存（每60秒刷新一次）
_config_cache = {}
_config_cache_lock = threading.Lock()
_config_last_update = 0
_CONFIG_CACHE_TTL = 60


def get_ai_config():
    """
    获取 AI 容错配置（带缓存）

    优先从数据库读取，如果失败则使用默认配置
    """
    global _config_cache, _config_last_update

    now = time.time()

    # 检查缓存是否过期
    with _config_cache_lock:
        if _config_cache and (now - _config_last_update) < _CONFIG_CACHE_TTL:
            return _config_cache.copy()

    # 从数据库读取配置
    try:
        from models.system_settings import SystemSettings
        config = SystemSettings.get_ai_resilience_config()

        with _config_cache_lock:
            _config_cache = config
            _config_last_update = now

        return config.copy()
    except Exception as e:
        # 数据库读取失败，使用默认配置
        logging.getLogger(__name__).warning(
            f"Failed to load AI config from database: {e}, using defaults")
        return DEFAULT_CONFIG.copy()


def invalidate_ai_config_cache():
    """使配置缓存失效（配置更新时调用）"""
    global _config_cache, _config_last_update
    with _config_cache_lock:
        _config_cache = {}
        _config_last_update = 0
