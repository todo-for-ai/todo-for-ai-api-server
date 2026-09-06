"""GoalLoop 多日续航看门狗调度器。

周期性运行 services.goal_loop_service.watchdog_sweep：时长预算终态、卡死
轮次处置、漏触发自愈——让循环可以无人值守连续跑几天。

通过 GOAL_LOOP_WATCHDOG_ENABLED=true 开启（默认关，与全局编排调度器同一
门控风格）。多 worker 部署只在其中一个 worker 开启，线程不做跨进程协调。
"""
import logging
import os
import threading

logger = logging.getLogger(__name__)

_scheduler_thread = None
_scheduler_lock = threading.Lock()
_stop_event = threading.Event()
_last_run = None  # dict: {finished_at, summary}


def enabled() -> bool:
    return (os.getenv('GOAL_LOOP_WATCHDOG_ENABLED', 'false').strip().lower() in ('1', 'true', 'yes', 'on'))


def interval_seconds(default=300) -> int:
    try:
        return max(30, int(os.getenv('GOAL_LOOP_WATCHDOG_INTERVAL_SECONDS', '') or default))
    except ValueError:
        return default


def _watchdog_loop(app, interval):
    from services.goal_loop_service import watchdog_sweep
    while not _stop_event.wait(interval):
        try:
            with app.app_context():
                result = watchdog_sweep()
                summary = dict(result)
                # 云端空闲 Pod 回收（无集群配置时内部静默跳过）
                try:
                    from services.workspace_runtime_policy import recycle_idle_pods
                    from services.agent_runtime_controller import get_agent_controller
                    recycle = recycle_idle_pods(get_agent_controller())
                    summary['pods_checked'] = recycle.get('checked', 0)
                    summary['pods_recycled'] = recycle.get('recycled', 0)
                except Exception as e:  # noqa: BLE001
                    logger.warning("[GOAL_LOOP_WATCHDOG] Pod recycle skipped: %s", e)
                global _last_run
                from datetime import datetime
                _last_run = {
                    'finished_at': datetime.utcnow().isoformat(),
                    'summary': summary,
                }
                if any(result[k] for k in ('time_exhausted', 'stuck_cancelled', 'kicked')):
                    logger.info("[GOAL_LOOP_WATCHDOG] %s", result)
        except Exception as e:  # noqa: BLE001
            logger.exception("[GOAL_LOOP_WATCHDOG] Sweep failed: %s", e)


def start_goal_loop_watchdog(app):
    """按环境变量门控启动看门狗线程；重复调用安全。"""
    global _scheduler_thread, _stop_event
    if not enabled():
        return False
    interval = interval_seconds()
    with _scheduler_lock:
        if _scheduler_thread is not None and _scheduler_thread.is_alive():
            return False
        _stop_event.clear()
        _scheduler_thread = threading.Thread(
            target=_watchdog_loop,
            args=(app, interval),
            name="goal-loop-watchdog",
            daemon=True,
        )
        _scheduler_thread.start()
        logger.info("[GOAL_LOOP_WATCHDOG] Scheduler started: interval=%ss", interval)
        return True


def stop_goal_loop_watchdog():
    global _scheduler_thread
    _stop_event.set()
    thread = _scheduler_thread
    if thread and thread.is_alive():
        thread.join(timeout=5)
    _scheduler_thread = None


def last_run():
    return _last_run
