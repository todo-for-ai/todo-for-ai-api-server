"""高并发缓存包（从 redis_cache_service.py 拆分）。

- constants.py: TTL/锁超时等常量
- lock.py: Redis 分布式锁（SET NX EX + Lua 原子释放 + 自动续期）
- local.py: L1 进程内 LRU 缓存
- core.py: L1+L2 编排（HighPerformanceCache）、cached 装饰器、全局实例

对外入口保持 services.redis_cache_service 门面不变。
"""
