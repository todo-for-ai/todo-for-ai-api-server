"""Redis 分布式锁：SET NX EX 获取、Lua 原子释放、自动续期。"""

import threading
import time

from services.cache.constants import LOCK_TIMEOUT


class DistributedLock:
    """
    分布式锁实现 (基于Redis)

    使用 Redis 的 SET NX EX 实现安全的分布式锁
    支持自动续期和优雅释放
    """

    def __init__(self, redis_client, lock_key: str, timeout: int = LOCK_TIMEOUT):
        self.redis = redis_client
        self.lock_key = f"lock:{lock_key}"
        self.timeout = timeout
        self.lock_value = f"{threading.current_thread().ident}:{time.time()}"
        self._acquired = False
        self._renewal_thread = None
        self._stop_renewal = threading.Event()

    def acquire(self, blocking: bool = True, blocking_timeout: float = None) -> bool:
        """
        获取锁

        Args:
            blocking: 是否阻塞等待
            blocking_timeout: 阻塞超时时间(秒)

        Returns:
            是否获取成功
        """
        if not self.redis:
            return True  # Redis不可用时跳过锁

        start_time = time.time()

        while True:
            # 尝试获取锁
            acquired = self.redis.set(
                self.lock_key,
                self.lock_value,
                nx=True,
                ex=self.timeout
            )

            if acquired:
                self._acquired = True
                # 启动续期线程
                self._start_renewal()
                return True

            if not blocking:
                return False

            # 检查超时
            if blocking_timeout and (time.time() - start_time) >= blocking_timeout:
                return False

            # 短暂等待后重试
            time.sleep(0.1)

    def release(self):
        """释放锁 (使用Lua脚本保证原子性)"""
        if not self._acquired or not self.redis:
            return

        # 停止续期
        self._stop_renewal.set()
        if self._renewal_thread:
            self._renewal_thread.join(timeout=1)

        # Lua脚本原子释放
        lua_script = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("del", KEYS[1])
        else
            return 0
        end
        """

        try:
            self.redis.eval(lua_script, 1, self.lock_key, self.lock_value)
        except Exception:
            pass
        finally:
            self._acquired = False

    def _start_renewal(self):
        """启动锁续期线程"""
        def renew():
            while not self._stop_renewal.wait(self.timeout / 3):
                if not self._acquired:
                    break
                try:
                    # 延长锁过期时间
                    lua_script = """
                    if redis.call("get", KEYS[1]) == ARGV[1] then
                        return redis.call("expire", KEYS[1], ARGV[2])
                    else
                        return 0
                    end
                    """
                    self.redis.eval(lua_script, 1, self.lock_key, self.lock_value, self.timeout)
                except Exception:
                    break

        self._renewal_thread = threading.Thread(target=renew, daemon=True)
        self._renewal_thread.start()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()
        return False
