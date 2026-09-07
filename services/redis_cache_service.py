"""高并发 Redis 缓存服务（兼容门面）。

实现已拆分至 services/cache/ 包：constants / lock / local / core。
既有导入路径（from services.redis_cache_service import ...）保持不变。
"""

from services.cache.constants import (  # noqa: F401
    BATCH_SIZE,
    DEFAULT_TTL,
    LOCAL_CACHE_TTL,
    LOCK_TIMEOUT,
)
from services.cache.lock import DistributedLock  # noqa: F401
from services.cache.local import LocalCache  # noqa: F401
from services.cache.core import (  # noqa: F401
    CacheException,
    HighPerformanceCache,
    cached,
    cache_service,
    clear_cache,
    get_cache,
    get_cache_stats,
)
