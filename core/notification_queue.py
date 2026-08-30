"""
通知投递 Redis 队列
"""

from datetime import datetime
from core.redis_client import get_redis_client

READY_QUEUE_KEY = 'notifications:deliveries:ready'
RETRY_ZSET_KEY = 'notifications:deliveries:retry'
DELIVERY_LOCK_PREFIX = 'notifications:deliveries:lock:'
DEFAULT_LOCK_TTL_SECONDS = 90


def enqueue_delivery(delivery_id):
    client = get_redis_client()
    if not client:
        return False
    client.lpush(READY_QUEUE_KEY, str(int(delivery_id)))
    return True


def enqueue_deliveries(delivery_ids):
    client = get_redis_client()
    if not client:
        return False
    values = [str(int(item)) for item in delivery_ids if str(item).isdigit()]
    if not values:
        return True
    client.lpush(READY_QUEUE_KEY, *values)
    return True


def schedule_delivery_retry(delivery_id, run_at):
    client = get_redis_client()
    if not client:
        return False
    score = float(run_at.timestamp() if isinstance(run_at, datetime) else float(run_at))
    client.zadd(RETRY_ZSET_KEY, {str(int(delivery_id)): score})
    return True


def promote_due_retries(now=None, limit=100):
    client = get_redis_client()
    if not client:
        return 0
    now_score = float((now or datetime.utcnow()).timestamp())
    ready_ids = client.zrangebyscore(RETRY_ZSET_KEY, '-inf', now_score, start=0, num=limit)
    if not ready_ids:
        return 0
    pipe = client.pipeline()
    for delivery_id in ready_ids:
        pipe.zrem(RETRY_ZSET_KEY, delivery_id)
        pipe.lpush(READY_QUEUE_KEY, delivery_id)
    pipe.execute()
    return len(ready_ids)


def pop_delivery(timeout_seconds=5):
    client = get_redis_client()
    if not client:
        return None
    result = client.brpop(READY_QUEUE_KEY, timeout=timeout_seconds)
    if not result:
        return None
    _, value = result
    try:
        return int(value)
    except Exception:
        return None


def acquire_delivery_lock(delivery_id, worker_id, ttl_seconds=DEFAULT_LOCK_TTL_SECONDS):
    client = get_redis_client()
    if not client:
        return True
    lock_key = f'{DELIVERY_LOCK_PREFIX}{int(delivery_id)}'
    return bool(client.set(lock_key, worker_id, nx=True, ex=ttl_seconds))


def release_delivery_lock(delivery_id, worker_id):
    client = get_redis_client()
    if not client:
        return True
    lock_key = f'{DELIVERY_LOCK_PREFIX}{int(delivery_id)}'
    current_value = client.get(lock_key)
    if current_value == worker_id:
        client.delete(lock_key)
    return True
