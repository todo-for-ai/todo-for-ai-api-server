"""
缓存失效工具
"""

from core.redis_client import get_redis_client


def _delete_keys_by_pattern(client, pattern):
    keys = list(client.scan_iter(match=pattern, count=200))
    if keys:
        client.delete(*keys)
    return len(keys)


def invalidate_user_caches(user_id, invalidate_dashboard=False):
    """失效用户相关缓存（Redis + 当前进程内存回退缓存）"""
    if not user_id:
        return

    client = get_redis_client()
    if client:
        patterns = [
            f"projects:list:user:{user_id}:*",
            f"tasks:list:user:{user_id}:*",
            f"pins:user:{user_id}:*",
            f"context-rules:user:{user_id}:*",
            f"mcp:project_stats:user:{user_id}",
        ]
        if invalidate_dashboard:
            patterns.insert(0, f"dashboard:user:{user_id}:*")
        for pattern in patterns:
            _delete_keys_by_pattern(client, pattern)

    # 失效 dashboard 内存回退缓存（可选，默认保留以提升大数据量场景首屏性能）
    if invalidate_dashboard:
        try:
            from api.dashboard import dashboard_fallback_cache
            prefix = f"user:{user_id}:"
            for key in [k for k in dashboard_fallback_cache.keys() if k.startswith(prefix)]:
                dashboard_fallback_cache.pop(key, None)
        except Exception:
            pass

    # 失效 projects 内存回退缓存
    try:
        from api.projects import projects_list_fallback_cache
        prefix = f"user:{user_id}:"
        for key in [k for k in projects_list_fallback_cache.keys() if k.startswith(prefix)]:
            projects_list_fallback_cache.pop(key, None)
    except Exception:
        pass

    # 失效 tasks 列表内存回退缓存
    try:
        from api.tasks import tasks_list_fallback_cache
        prefix = f"user:{user_id}:"
        for key in [k for k in tasks_list_fallback_cache.keys() if k.startswith(prefix)]:
            tasks_list_fallback_cache.pop(key, None)
    except Exception:
        pass

    # 失效 pins 内存回退缓存
    try:
        from api.pins import pins_fallback_cache
        prefix = f"user:{user_id}:"
        for key in [k for k in pins_fallback_cache.keys() if k.startswith(prefix)]:
            pins_fallback_cache.pop(key, None)
    except Exception:
        pass

    # 失效 context-rules 内存回退缓存
    try:
        from api.context_rules import context_rules_fallback_cache
        prefix = f"user:{user_id}:"
        for key in [k for k in context_rules_fallback_cache.keys() if k.startswith(prefix)]:
            context_rules_fallback_cache.pop(key, None)
    except Exception:
        pass

    # 失效 mcp 内存回退缓存
    try:
        from api.mcp import project_stats_cache
        project_stats_cache.pop(f"user:{user_id}", None)
    except Exception:
        pass
